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
            retries=5,
            forbidden_hint=lambda path: "your account needs the 'Cognitive Services OpenAI User' role on the resource",
            error_class=LLMError,
        )

    @classmethod
    def from_config(cls, data: dict) -> "AzureOpenAIChat":
        return cls(AzureOpenAIConfig.from_dict(data))

    def complete_json(self, system: str, user: str, schema: dict, schema_name: str) -> LLMResult:
        body = {
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "temperature": self.config.temperature,
            "seed": self.config.seed,
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


class RecordingLLM:
    """Answers from the record when the same request was made before.

    mode 'record': read the record, call the model on a miss and record it;
    'replay': read the record only (a miss is an error, nothing is called);
    'refresh': always call the model and overwrite the record.
    """

    def __init__(self, inner, cache_dir: str | Path, mode: str = "record", model_id: str | None = None):
        if mode not in ("record", "replay", "refresh"):
            raise ValueError("mode must be 'record', 'replay' or 'refresh'")
        self.inner = inner
        self.cache_dir = Path(cache_dir)
        self.mode = mode
        self.model_id = model_id or getattr(inner, "model_id", "unknown")
        self.calls = 0
        self.hits = 0

    def key(self, system: str, user: str, schema: dict, schema_name: str) -> str:
        request = {"model": self.model_id, "schema_name": schema_name, "schema": schema, "system": system, "user": user}
        return hashlib.sha256(json.dumps(request, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()

    def complete_json(self, system: str, user: str, schema: dict, schema_name: str) -> LLMResult:
        key = self.key(system, user, schema, schema_name)
        path = self.cache_dir / key[:2] / f"{key}.json"
        if path.is_file() and self.mode != "refresh":
            record = json.loads(path.read_text(encoding="utf-8"))
            self.hits += 1
            response = record["response"]
            return LLMResult(response["data"], LLMUsage(**response.get("usage", {})), response.get("model", ""), cached=True)
        if self.mode == "replay" or self.inner is None:
            raise LLMError("no recorded answer for this request (replay mode)")
        result = self.inner.complete_json(system, user, schema, schema_name)
        self.calls += 1
        path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "recorded_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            "model_id": self.model_id,
            "schema_name": schema_name,
            "response": {"data": result.data, "usage": asdict(result.usage), "model": result.model},
        }
        path.write_text(json.dumps(record, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        return result


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
