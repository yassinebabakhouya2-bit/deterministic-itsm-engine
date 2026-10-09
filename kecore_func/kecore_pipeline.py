"""The V10 decomposition as Azure runs it: kecore, unchanged, between blob reads and writes.

Each step reads its inputs from storage and writes its outputs back, so each one is a
separate, retryable Durable Functions activity and nothing lives on an operator's machine:

  extract    kb-<client>/<prefix>*               -> kecore-<client>/runs/<run>/fiches.jsonl
  profile    fiches.jsonl                         -> runs/<run>/profile.json
  decompose  fiches.jsonl[start:end] + profile    -> runs/<run>/decomposed/<index>.json
  report     decomposed/*                         -> runs/<run>/{fiches.decomposed.jsonl, report.md,
                                                     summary.json, graph.json, excluded.json}
  (semantic) the run's map                        -> runs/<run>/semantic/ (semantic_service.py)
  publish    the run's summary                    -> latest.json, last: a published run never changes

Every run keeps its own folder: the map of a client's KB is versioned, never overwritten. The map
kefind finds fiches on (slice 3) is that folder: fiches.decomposed.jsonl (verified steps and
entities), profile.json (the client's dictionary) and graph.json (relations between fiches).
The LLM record lives in kecore-<client>/llm-cache/ with the same layout and keys as the local
record it replaces, so a run in mode "replay" re-reads earlier answers and never calls the model.

This module never imports an Azure SDK: storage is any object with list/read/write, so the
whole pipeline is tested in memory.

System exclusion (kecore.exclusion, 2026-10-09): ``report`` keeps placeholder and empty fiches out of
the map -- fiches.decomposed.jsonl and graph.json hold only the fiches kept, so neither the semantic
index nor any decision ever sees the others; report.md and summary.json still describe every fiche
decomposed (the decomposition itself is unchanged), and excluded.json lists each exclusion with its
rule. The client's rules and its forced inclusions are kecore-<client>/exclusion-config.json.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict
from pathlib import PurePosixPath
from typing import Protocol

from kecore.decompose import DecomposedFiche, Decomposer
from kecore.exclusion import ExclusionRules
from kecore.exclusion import apply as apply_exclusion
from kecore.fiches import DOCUMENT_EXTENSIONS, Fiche, fiches_from_documents
from kecore.profile import Profile, build_profile
from kecore.report import build_report
from kefind.graph import build_graph

MODES = ("replay", "record")
LLM_RECORD_PREFIX = "llm-cache/"
LATEST = "latest.json"
DICTIONARY_DECISIONS = "dictionary-decisions.json"
EXCLUSION_CONFIG = "exclusion-config.json"
DEFAULT_BATCH = 10
MAX_BATCH = 50
_PREFIX_RE = re.compile(r"^[\w\-. /]{0,200}$")
_RUN_ID_RE = re.compile(r"^[0-9A-Za-z-]{1,64}$")
_FICHE_FIELDS = ("fiche_id", "client", "title", "text", "source", "meta")


class Storage(Protocol):
    def list(self, container: str, prefix: str) -> list[str]: ...

    def read(self, container: str, name: str) -> bytes | None: ...

    def write(self, container: str, name: str, data: bytes) -> None: ...

    def delete(self, container: str, name: str) -> None: ...


def kb_container(client: str) -> str:
    return f"kb-{client}"


def kecore_container(client: str) -> str:
    return f"kecore-{client}"


def ticket_container(client: str) -> str:
    return f"tickets-{client}"


def layout(run_id: str) -> dict[str, str]:
    base = f"runs/{run_id}/"
    return {
        "fiches": base + "fiches.jsonl",
        "profile": base + "profile.json",
        "decomposed_dir": base + "decomposed/",
        "decomposed": base + "fiches.decomposed.jsonl",
        "report": base + "report.md",
        "summary": base + "summary.json",
        "graph": base + "graph.json",
        "excluded": base + "excluded.json",
        "published": base + "published.json",
        "latest": LATEST,
    }


def validate_request(body, allowed_clients, run_id: str) -> dict:
    """The run request, checked before anything starts. Deny-by-default on the client."""
    if not isinstance(body, dict):
        raise ValueError("a JSON object is expected")
    client = body.get("client")
    if not isinstance(client, str) or client not in set(allowed_clients):
        raise ValueError("unknown client")
    prefix = body.get("source_prefix", "")
    if not isinstance(prefix, str) or not _PREFIX_RE.match(prefix) or ".." in prefix or prefix.startswith("/"):
        raise ValueError("source_prefix: letters, digits, spaces and - _ . / only, no '..', 200 characters at most")
    mode = body.get("mode", "record")
    if mode not in MODES:
        raise ValueError("mode must be 'replay' or 'record'")
    batch_size = body.get("batch_size", DEFAULT_BATCH)
    if isinstance(batch_size, bool) or not isinstance(batch_size, int) or not 1 <= batch_size <= MAX_BATCH:
        raise ValueError(f"batch_size must be an integer between 1 and {MAX_BATCH}")
    limit = body.get("limit")
    if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int) or limit < 1):
        raise ValueError("limit must be a positive integer")
    with_dictionary = body.get("with_dictionary", True)
    if not isinstance(with_dictionary, bool):
        raise ValueError("with_dictionary must be true or false")
    semantic = body.get("semantic", True)
    if not isinstance(semantic, bool):
        raise ValueError("semantic must be true or false")
    if not _RUN_ID_RE.match(run_id):
        raise ValueError("invalid run id")
    return {
        "client": client,
        "source_prefix": prefix,
        "mode": mode,
        "batch_size": batch_size,
        "limit": limit,
        "with_dictionary": with_dictionary,
        "semantic": semantic,
        "run_id": run_id,
    }


def batches(count: int, size: int) -> list[list[int]]:
    """[start, end) ranges covering ``count`` fiches, ``size`` at a time."""
    return [[start, min(start + size, count)] for start in range(0, count, size)]


class StorageRecordStore:
    """kecore.llm.RecordingLLM store in kecore-<client>/llm-cache/, same layout as the local record.

    ``prefix`` keeps another record apart: the interpretation of tickets lives in find-cache/."""

    def __init__(self, storage: Storage, client: str, prefix: str = LLM_RECORD_PREFIX):
        self.storage = storage
        self.container = kecore_container(client)
        self.prefix = prefix

    def read(self, relative: str) -> str | None:
        data = self.storage.read(self.container, self.prefix + relative)
        return None if data is None else data.decode("utf-8")

    def write(self, relative: str, text: str) -> None:
        self.storage.write(self.container, self.prefix + relative, text.encode("utf-8"))

    def write_if_absent(self, relative: str, text: str) -> bool:
        """The first record of a key wins (two questions embedded at the same time get one vector).
        A storage without a conditional write is read first: right for a single writer."""
        name = self.prefix + relative
        writer = getattr(self.storage, "write_if_absent", None)
        if writer is not None:
            return writer(self.container, name, text.encode("utf-8"))
        if self.storage.read(self.container, name) is not None:
            return False
        self.storage.write(self.container, name, text.encode("utf-8"))
        return True


def _jsonl(items) -> bytes:
    return "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in items).encode("utf-8")


def read_jsonl(data: bytes) -> list[dict]:
    # split on "\n" only: str.splitlines() also splits on U+2028 / U+2029 / U+0085, which
    # json.dumps(..., ensure_ascii=False) writes raw inside strings (a pasted ticket or a Word
    # fiche can hold one), and one such character would break every read of the file
    return [json.loads(line) for line in data.decode("utf-8").split("\n") if line.strip()]


def _json(data) -> bytes:
    return (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _require(storage: Storage, container: str, name: str) -> bytes:
    data = storage.read(container, name)
    if data is None:
        raise FileNotFoundError(f"{container}/{name} is missing")
    return data


def _load_fiches(storage: Storage, payload: dict) -> list[Fiche]:
    data = _require(storage, kecore_container(payload["client"]), layout(payload["run_id"])["fiches"])
    return [Fiche(**{k: v for k, v in item.items() if k in _FICHE_FIELDS}) for item in read_jsonl(data)]


def _load_profile(storage: Storage, payload: dict) -> Profile:
    data = _require(storage, kecore_container(payload["client"]), layout(payload["run_id"])["profile"])
    return Profile.from_dict(json.loads(data.decode("utf-8")))


def _llm_counts(llm) -> dict:
    if llm is None:
        return {"calls": 0, "cached": 0}
    return {"calls": llm.calls, "cached": llm.hits}


def extract(storage: Storage, payload: dict) -> dict:
    """Documents under the prefix of kb-<client> become fiches, in the same order as a local run."""
    client, prefix = payload["client"], payload["source_prefix"]
    source = kb_container(client)
    names = [n for n in storage.list(source, prefix) if PurePosixPath(n).suffix.lower() in DOCUMENT_EXTENSIONS]
    documents = [(name[len(prefix):], (lambda name=name: storage.read(source, name) or b"")) for name in names]
    fiches, warnings = fiches_from_documents(documents, client)
    if payload.get("limit"):
        fiches = fiches[: payload["limit"]]
    storage.write(kecore_container(client), layout(payload["run_id"])["fiches"], _jsonl(f.to_dict() for f in fiches))
    return {"documents": len(names), "fiches": len(fiches), "warnings": warnings}


def dictionary_rejected(storage: Storage, client: str) -> list[str]:
    """The dictionary entries a person rejected (ids or spellings), from kecore-<client>/dictionary-decisions.json:
    {"rejected": ["general", "montereau"]}. Missing file: nothing rejected."""
    data = storage.read(kecore_container(client), DICTIONARY_DECISIONS)
    if data is None:
        return []
    try:
        decisions = json.loads(data.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{DICTIONARY_DECISIONS} is not valid JSON ({exc})") from None
    rejected = decisions.get("rejected", []) if isinstance(decisions, dict) else None
    if not isinstance(rejected, list) or not all(isinstance(item, str) for item in rejected):
        raise ValueError(f'{DICTIONARY_DECISIONS} must look like {{"rejected": ["<entry id or spelling>", ...]}}')
    return rejected


def dictionary_accepted(storage: Storage, client: str) -> list[dict]:
    """The candidates a person accepted in the dictionary review (dictionary-decisions.json "accepted":
    [{"spelling", "canonical"}]); they join the dictionary the run learns."""
    data = storage.read(kecore_container(client), DICTIONARY_DECISIONS)
    if data is None:
        return []
    decisions = json.loads(data.decode("utf-8-sig"))
    accepted = decisions.get("accepted", []) if isinstance(decisions, dict) else []
    return [a for a in accepted if isinstance(a, dict) and isinstance(a.get("spelling"), str) and a["spelling"].strip()]


def merge_accepted(learned: Profile, accepted: list[dict], rejected: list[str]) -> int:
    """Adds each accepted spelling to its entry (``canonical``) or to a new one; a rejection wins."""
    refused = {r.lower() for r in rejected}
    added = 0
    for item in accepted:
        spelling = " ".join(item["spelling"].split())
        target = item.get("canonical") or re.sub(r"[^a-z0-9]+", "-", spelling.lower()).strip("-")
        if not target or spelling.lower() in refused or target in refused:
            continue
        forms = learned.dictionary.setdefault(target, [])
        if spelling not in forms:
            forms.append(spelling)
            added += 1
    return added


def profile(storage: Storage, payload: dict, llm=None, decided: dict | None = None) -> dict:
    """``decided``: the decisions of the dictionary review tab (dictionary_service.decisions), on top
    of the optional hand-written dictionary-decisions.json."""
    fiches = _load_fiches(storage, payload)
    decided = decided or {}
    rejected = (dictionary_rejected(storage, payload["client"]) + list(decided.get("rejected") or [])) \
        if payload["with_dictionary"] else []
    learned = build_profile(payload["client"], fiches, llm=llm, with_dictionary=payload["with_dictionary"],
                            dictionary_rejected=rejected)
    accepted = merge_accepted(learned, dictionary_accepted(storage, payload["client"]) + list(decided.get("accepted") or []),
                              rejected) if payload["with_dictionary"] else 0
    storage.write(kecore_container(payload["client"]), layout(payload["run_id"])["profile"], _json(learned.to_dict()))
    return {
        "headings": len(learned.headings),
        "stable": learned.stable,
        "dictionary": len(learned.dictionary),
        "dictionary_rejected": len(learned.dictionary_rejected),
        "dictionary_accepted": accepted,
        "llm": {**_llm_counts(llm), "heading_roles": learned.llm_usage, "dictionary": learned.dictionary_usage},
    }


def decompose(storage: Storage, payload: dict, start: int, end: int, llm=None) -> dict:
    fiches = _load_fiches(storage, payload)
    decomposer = Decomposer(profile=_load_profile(storage, payload), llm=llm)
    container = kecore_container(payload["client"])
    directory = layout(payload["run_id"])["decomposed_dir"]
    for index in range(start, min(end, len(fiches))):
        result = decomposer.decompose(fiches[index])
        storage.write(container, f"{directory}{index:05d}.json", json.dumps(result.to_dict(), ensure_ascii=False).encode("utf-8"))
    return {"start": start, "end": end, **_llm_counts(llm), "errors": decomposer.llm_errors, **asdict(decomposer.usage)}


def report(storage: Storage, payload: dict, ranges: list[list[int]], profile_stats: dict, batch_stats: list[dict],
           warnings: list[str], model_id: str | None) -> dict:
    """Gathers the run: the same report and summary as a local run and the graph between fiches
    (kefind.graph, code only). latest.json is ``publish``'s, after the semantic folder."""
    container = kecore_container(payload["client"])
    paths = layout(payload["run_id"])
    decomposed = []
    for start, end in ranges:
        for index in range(start, end):
            data = _require(storage, container, f"{paths['decomposed_dir']}{index:05d}.json")
            decomposed.append(DecomposedFiche.from_dict(json.loads(data.decode("utf-8"))))
    llm_stats = {}
    if model_id:
        llm_stats = {
            "model": model_id,
            "mode": payload["mode"],
            "calls": profile_stats["llm"]["calls"] + sum(b["calls"] for b in batch_stats),
            "cached": profile_stats["llm"]["cached"] + sum(b["cached"] for b in batch_stats),
            "errors": sum(b["errors"] for b in batch_stats),
            "input_tokens": sum(b.get("input_tokens", 0) for b in batch_stats),
            "output_tokens": sum(b.get("output_tokens", 0) for b in batch_stats),
        }
    exclusion = apply_exclusion(decomposed, exclusion_rules(storage, payload["client"]))
    markdown, summary = build_report(payload["client"], decomposed, _load_profile(storage, payload), llm_stats, warnings)
    summary["exclusion"] = exclusion.stats()
    markdown += _exclusion_section(exclusion)
    graph = build_graph(exclusion.kept)
    storage.write(container, paths["decomposed"], _jsonl(d.to_dict() for d in exclusion.kept))
    storage.write(container, paths["excluded"], _json(exclusion.to_dict()))
    storage.write(container, paths["report"], markdown.encode("utf-8"))
    storage.write(container, paths["summary"], _json(summary))
    storage.write(container, paths["graph"], _json(graph.to_dict()))
    keys = ("fiches", "guided", "citable", "info_only", "steps", "steps_verified", "mean_agreement")
    short = {key: summary.get(key) for key in keys}
    short["llm"] = summary.get("llm", {})
    short["dictionary"] = profile_stats.get("dictionary")
    short["graph"] = graph.stats()
    short["exclusion"] = summary["exclusion"]
    short["run_id"] = payload["run_id"]
    return short


def exclusion_rules(storage: Storage, client: str) -> ExclusionRules:
    """kecore-<client>/exclusion-config.json, else the defaults. A malformed file fails the run with a
    clear error rather than excluding fiches by rules nobody wrote."""
    data = storage.read(kecore_container(client), EXCLUSION_CONFIG)
    if data is None:
        return ExclusionRules()
    try:
        return ExclusionRules.from_dict(json.loads(data.decode("utf-8")))
    except (ValueError, AttributeError) as exc:
        raise ValueError(f"{kecore_container(client)}/{EXCLUSION_CONFIG} is invalid: {exc}") from exc


def _exclusion_section(exclusion) -> str:
    """The fiches kept out of the map, for a person to check (a wrong exclusion is undone by force_include)."""
    lines = ["", "## Fiches excluded from the map", ""]
    if not exclusion.excluded:
        return "\n".join(lines + ["None.", ""])
    lines += ["| Fiche | Title | Rule | Detail |", "|---|---|---|---|"]
    for e in exclusion.excluded:
        lines.append("| " + " | ".join(" ".join(str(x).split()).replace("|", "\\|")
                                       for x in (e.fiche_id, e.title, e.rule, e.detail)) + " |")
    return "\n".join(lines + [""])


def publish(storage: Storage, payload: dict, short: dict, semantic: dict | None = None) -> dict:
    """latest.json names this run, last of all: the run's folder is complete and will never change.
    ``semantic``: what the semantic build gave (or its error, or None when not asked). The run's own
    published.json says it was published, after latest.json moves on to a newer run (the Function keeps
    in memory only published runs: kecore_func.function_app._kb_map)."""
    summary = {**short, "semantic": semantic}
    container = kecore_container(payload["client"])
    storage.write(container, layout(payload["run_id"])["published"], _json({"run_id": payload["run_id"]}))
    storage.write(container, LATEST, _json({"run_id": payload["run_id"], "summary": summary}))
    return summary
