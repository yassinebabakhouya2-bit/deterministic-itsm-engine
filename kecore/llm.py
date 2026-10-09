"""Language-model calls, recorded so that every run can be replayed.

The engine never trusts a model's output as such: the model proposes, the code
verifies. Every answer is stored under the hash of its exact request; running
the same request again reads the record instead of calling the model, so a
decomposition is reproducible and costs nothing the second time.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import urllib.parse
from dataclasses import asdict, dataclass, field
from dataclasses import fields as dataclass_fields
from pathlib import Path

from .azure import COGNITIVE_SCOPE, RestClient, TokenProvider


class LLMError(RuntimeError):
    pass


@dataclass
class LLMUsage:
    input_tokens: int = 0
    output_tokens: int = 0

    def add(self, other: "LLMUsage") -> None:
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens


@dataclass
class LLMResult:
    data: dict
    usage: LLMUsage = field(default_factory=LLMUsage)
    model: str = ""
    cached: bool = False


@dataclass
class AzureOpenAIConfig:
    endpoint: str
    deployment: str = "gpt-4o"
    api_version: str = "2024-10-21"
    auth: str = "entra"
    temperature: float = 0.0
    seed: int = 7
    max_tokens: int = 4000
    timeout_s: float = 120.0
    retries: int = 5            # new attempts after a transport failure or a 5xx (kecore.azure.RestClient)
    throttle_retries: int = 5   # new attempts after a 429; offline runs raise it, the live path lowers both

    @classmethod
    def from_dict(cls, data: dict) -> "AzureOpenAIConfig":
        allowed = {f.name for f in dataclass_fields(cls)}
        unknown = sorted(set(data) - allowed - {"comment", "_comment"})
        if unknown:
            raise ValueError(f"unknown LLM setting(s): {', '.join(unknown)}. Allowed: {', '.join(sorted(allowed))}")
        endpoint = str(data.get("endpoint") or "")
        if not endpoint.startswith("https://") or "<" in endpoint:
            raise ValueError("LLM 'endpoint' must be your Azure OpenAI or Foundry URL, e.g. https://<account>.openai.azure.com")
        config = cls(**{k: v for k, v in data.items() if k in allowed})
        if config.auth not in ("entra", "key"):
            raise ValueError("LLM 'auth' must be 'entra' or 'key'")
        return config


class AzureOpenAIChat:
    """Chat completions with a strict JSON schema, keyless by default.

    Keyless needs the 'Cognitive Services OpenAI User' role on the resource;
    'auth': 'key' reads AZURE_OPENAI_API_KEY instead.
    """

    def __init__(self, config: AzureOpenAIConfig, client: RestClient | None = None):
        self.config = config
        host = urllib.parse.urlparse(config.endpoint).netloc
        self.model_id = f"{config.deployment}@{host}"
        tokens = TokenProvider(config.auth, scope=COGNITIVE_SCOPE, key_env="AZURE_OPENAI_API_KEY")
        self._client = client or RestClient(
            config.endpoint,
            config.api_version,
            tokens,
            timeout=config.timeout_s,
            retries=config.retries,
            throttle_retries=config.throttle_retries,
            forbidden_hint=lambda path: "your account needs the 'Cognitive Services OpenAI User' role on the resource",
            error_class=LLMError,
        )

    @classmethod
    def from_config(cls, data: dict) -> "AzureOpenAIChat":
        return cls(AzureOpenAIConfig.from_dict(data))

    def complete_json(self, system: str, user: str, schema: dict, schema_name: str, *,
                       temperature: float | None = None, seed: int | None = None) -> LLMResult:
        body = {
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "temperature": self.config.temperature if temperature is None else temperature,
            "seed": self.config.seed if seed is None else seed,
            "max_tokens": self.config.max_tokens,
            "response_format": {"type": "json_schema", "json_schema": {"name": schema_name, "strict": True, "schema": schema}},
        }
        path = f"/openai/deployments/{urllib.parse.quote(self.config.deployment, safe='')}/chat/completions"
        payload = self._client.request("POST", path, body)
        try:
            choice = payload["choices"][0]
            message = choice["message"]
        except (KeyError, IndexError, TypeError):
            raise LLMError(f"unexpected answer: {json.dumps(payload)[:300]}") from None
        if message.get("refusal"):
            raise LLMError(f"the model refused: {str(message['refusal'])[:200]}")
        if choice.get("finish_reason") == "length":
            raise LLMError(f"the answer was cut at max_tokens ({self.config.max_tokens})")
        try:
            data = json.loads(message.get("content") or "")
        except json.JSONDecodeError:
            raise LLMError("the model did not return JSON") from None
        usage = payload.get("usage") or {}
        return LLMResult(
            data=data,
            usage=LLMUsage(int(usage.get("prompt_tokens") or 0), int(usage.get("completion_tokens") or 0)),
            model=str(payload.get("model") or self.config.deployment),
        )


class AzureOpenAIEmbeddings:
    """Text embeddings (e.g. text-embedding-3-large), keyless by default -- same auth and retries as
    AzureOpenAIChat. A vector only ever feeds code: kefind ranks fiches by cosine similarity and the
    code decides on that number (kefind.semantic); the model never picks a fiche."""

    def __init__(self, config: AzureOpenAIConfig, client: RestClient | None = None):
        self.config = config
        host = urllib.parse.urlparse(config.endpoint).netloc
        self.model_id = f"{config.deployment}@{host}"
        tokens = TokenProvider(config.auth, scope=COGNITIVE_SCOPE, key_env="AZURE_OPENAI_API_KEY")
        self._client = client or RestClient(
            config.endpoint,
            config.api_version,
            tokens,
            timeout=config.timeout_s,
            retries=config.retries,
            throttle_retries=config.throttle_retries,
            forbidden_hint=lambda path: "your account needs the 'Cognitive Services OpenAI User' role on the resource",
            error_class=LLMError,
        )

    @classmethod
    def from_config(cls, data: dict) -> "AzureOpenAIEmbeddings":
        return cls(AzureOpenAIConfig.from_dict(data))

    def embed(self, texts, dimensions: int | None = None) -> list[list[float]]:
        """One vector per text, in the same order. ``dimensions`` shortens text-embedding-3 vectors
        (the model's own option). Empty texts are the caller's to avoid: the API refuses them."""
        texts = list(texts)
        if not texts:
            return []
        body: dict = {"input": texts}
        if dimensions:
            body["dimensions"] = int(dimensions)
        path = f"/openai/deployments/{urllib.parse.quote(self.config.deployment, safe='')}/embeddings"
        payload = self._client.request("POST", path, body)
        try:
            items = sorted(payload["data"], key=lambda d: d["index"])
            vectors = [[float(x) for x in item["embedding"]] for item in items]
        except (KeyError, TypeError, IndexError, ValueError):
            raise LLMError(f"unexpected answer: {json.dumps(payload)[:300]}") from None
        if len(vectors) != len(texts):
            raise LLMError(f"expected {len(texts)} embedding(s), got {len(vectors)}")
        if dimensions and any(len(v) != int(dimensions) for v in vectors):
            raise LLMError(f"expected vectors of {dimensions} dimensions")
        return vectors


class FileRecordStore:
    """The record as files under one folder: ``<key[:2]>/<key>.json``."""

    def __init__(self, root: str | Path):
        self.root = Path(root)

    def read(self, relative: str) -> str | None:
        path = self.root / relative
        return path.read_text(encoding="utf-8") if path.is_file() else None

    def write(self, relative: str, text: str) -> None:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def write_if_absent(self, relative: str, text: str) -> bool:
        """Writes only when nothing is recorded yet; False when a record was already there."""
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with path.open("x", encoding="utf-8") as handle:
                handle.write(text)
        except FileExistsError:
            return False
        return True


class RecordingLLM:
    """Answers from the record when the same request was made before.

    mode 'record': read the record, call the model on a miss and record it;
    'replay': read the record only (a miss is an error, nothing is called);
    'refresh': always call the model and overwrite the record.
    The first answer recorded for a request wins: two calls that miss at the same time (a retried
    activity, two instances) both return the recorded one (``write_if_absent`` when the store has it),
    so what a run wrote and what its replay rewrites are the same.

    Where the record lives is a ``store`` (``read(relative) -> str | None``, ``write(relative, text)``):
    a folder by default (``cache_dir``), a blob container when the engine runs in Azure. The layout
    and the keys are the same in both, so a record made in one place replays in the other.
    """

    def __init__(self, inner, cache_dir: str | Path | None = None, mode: str = "record", model_id: str | None = None,
                 store=None):
        if mode not in ("record", "replay", "refresh"):
            raise ValueError("mode must be 'record', 'replay' or 'refresh'")
        if store is None:
            if cache_dir is None:
                raise ValueError("RecordingLLM needs a cache_dir or a store")
            store = FileRecordStore(cache_dir)
        self.inner = inner
        self.cache_dir = Path(cache_dir) if cache_dir is not None else None
        self.store = store
        self.mode = mode
        self.model_id = model_id or getattr(inner, "model_id", "unknown")
        self.calls = 0
        self.hits = 0

    def key(self, system: str, user: str, schema: dict, schema_name: str,
            temperature: float | None = None, seed: int | None = None) -> str:
        request = {"model": self.model_id, "schema_name": schema_name, "schema": schema, "system": system, "user": user}
        # left out when unset, so a plain call's key (the only kind before this field existed) is unchanged and
        # keeps reading an existing cache; only a call with an explicit override (the self-check pass) gets a new key.
        if temperature is not None:
            request["temperature"] = temperature
        if seed is not None:
            request["seed"] = seed
        return hashlib.sha256(json.dumps(request, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()

    def complete_json(self, system: str, user: str, schema: dict, schema_name: str, *,
                       temperature: float | None = None, seed: int | None = None) -> LLMResult:
        key = self.key(system, user, schema, schema_name, temperature, seed)
        relative = f"{key[:2]}/{key}.json"
        raw = self.store.read(relative) if self.mode != "refresh" else None
        if raw is not None:
            record = json.loads(raw)
            self.hits += 1
            response = record["response"]
            return LLMResult(response["data"], LLMUsage(**response.get("usage", {})), response.get("model", ""), cached=True)
        if self.mode == "replay" or self.inner is None:
            raise LLMError("no recorded answer for this request (replay mode)")
        result = self.inner.complete_json(system, user, schema, schema_name, temperature=temperature, seed=seed)
        self.calls += 1
        record = {
            "recorded_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            "model_id": self.model_id,
            "schema_name": schema_name,
            "response": {"data": result.data, "usage": asdict(result.usage), "model": result.model},
        }
        body = json.dumps(record, ensure_ascii=False, indent=1) + "\n"
        writer = getattr(self.store, "write_if_absent", None)
        if self.mode == "refresh" or writer is None:
            self.store.write(relative, body)
            return result
        if writer(relative, body):
            return result
        raw = self.store.read(relative)  # recorded first by another call: that answer is the answer
        if raw is None:
            return result
        response = json.loads(raw)["response"]
        return LLMResult(response["data"], LLMUsage(**response.get("usage", {})), response.get("model", ""), cached=True)


class RecordingEmbeddings:
    """Vectors from the record when the same text was embedded before, with the same model and the
    same number of dimensions: once a text has a vector, it keeps it, so a ranking computed from it
    is reproducible byte for byte -- and a replay costs nothing.

    mode 'record': read the record, embed the missing texts and record them; 'replay': the record
    only (a missing text is an error, nothing is called); 'refresh': always embed and overwrite.
    The first answer recorded for a text wins: when two calls embed the same new text at the same
    time, the second one finds the first one's record and returns it (``write_if_absent`` when the
    store has it), so both see the same vector."""

    def __init__(self, inner, mode: str = "record", model_id: str | None = None, store=None,
                 cache_dir: str | Path | None = None, dimensions: int = 1024, batch_size: int = 64):
        if mode not in ("record", "replay", "refresh"):
            raise ValueError("mode must be 'record', 'replay' or 'refresh'")
        if store is None:
            if cache_dir is None:
                raise ValueError("RecordingEmbeddings needs a cache_dir or a store")
            store = FileRecordStore(cache_dir)
        if not isinstance(dimensions, int) or dimensions < 1:
            raise ValueError("dimensions must be a positive integer")
        self.inner = inner
        self.store = store
        self.mode = mode
        self.model_id = model_id or getattr(inner, "model_id", "unknown")
        self.dimensions = dimensions
        self.batch_size = max(1, int(batch_size))
        self.calls = 0  # requests sent to the model
        self.hits = 0  # texts answered from the record
        self.embedded = 0  # texts sent to the model

    def key(self, text: str) -> str:
        request = {"model": self.model_id, "dimensions": self.dimensions, "input": text}
        return hashlib.sha256(json.dumps(request, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()

    @staticmethod
    def _relative(key: str) -> str:
        return f"{key[:2]}/{key}.json"

    def _read(self, text: str) -> list[float] | None:
        raw = self.store.read(self._relative(self.key(text)))
        if raw is None:
            return None
        vector = json.loads(raw).get("vector")
        if not isinstance(vector, list) or len(vector) != self.dimensions:
            raise LLMError("a recorded vector does not match this model's dimensions")
        return [float(x) for x in vector]

    def _record(self, text: str, vector: list[float]) -> list[float]:
        relative = self._relative(self.key(text))
        body = json.dumps({
            "recorded_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            "model_id": self.model_id,
            "dimensions": self.dimensions,
            "vector": vector,
        }) + "\n"
        writer = getattr(self.store, "write_if_absent", None)
        if self.mode == "refresh" or writer is None:
            self.store.write(relative, body)
            return vector
        if writer(relative, body):
            return vector
        recorded = self._read(text)  # someone recorded this text first: theirs is the vector
        return recorded if recorded is not None else vector

    def embed(self, texts) -> list[list[float]]:
        texts = list(texts)
        if any(not isinstance(t, str) or not t.strip() for t in texts):
            raise ValueError("every text to embed must be a non-empty string")
        vectors: dict[str, list[float]] = {}
        missing: list[str] = []
        for text in dict.fromkeys(texts):  # each distinct text once, in first-seen order
            vector = None if self.mode == "refresh" else self._read(text)
            if vector is None:
                missing.append(text)
            else:
                vectors[text] = vector
                self.hits += 1
        if missing:
            if self.mode == "replay" or self.inner is None:
                raise LLMError(f"no recorded embedding for {len(missing)} text(s) (replay mode)")
            for start in range(0, len(missing), self.batch_size):
                chunk = missing[start:start + self.batch_size]
                answers = self.inner.embed(chunk, dimensions=self.dimensions)
                self.calls += 1
                self.embedded += len(chunk)
                if len(answers) != len(chunk) or any(len(v) != self.dimensions for v in answers):
                    raise LLMError("the embedding model answered the wrong number of vectors or dimensions")
                for text, vector in zip(chunk, answers):
                    vectors[text] = self._record(text, [float(x) for x in vector])
        return [vectors[text] for text in texts]


def load_llm_config(path: str | Path) -> dict:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        raise ValueError(f"LLM config not found: {path}") from None
    except json.JSONDecodeError as exc:
        raise ValueError(f"LLM config {path} is not valid JSON ({exc.msg}, line {exc.lineno})") from None
    if not isinstance(data, dict):
        raise ValueError(f"LLM config {path} must be a JSON object")
    return data
