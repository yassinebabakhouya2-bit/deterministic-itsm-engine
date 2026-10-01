"""Replay a labeled ticket set through an engine, several times.

Each decision is written to <out-dir>/<engine>.results.jsonl as soon as it is
made, so an interrupted run keeps what it has done. A manifest next to it
records what was run on what (dataset hash, runs, engine settings).
"""

from __future__ import annotations

import datetime as dt
import glob
import json
import platform
import re
import time
from pathlib import Path

from . import __version__
from .dataset import Ticket
from .engines import Decision, decision_from_dict

MAX_FICHES_KEPT = 20
MAX_TITLES_KEPT = 5


def safe_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-") or "engine"


def make_record(engine_name: str, run: int, ticket: Ticket, decision: Decision) -> dict:
    fiches = decision.fiches[:MAX_FICHES_KEPT]
    return {
        "engine": engine_name,
        "run": run,
        "ticket_id": ticket.ticket_id,
        "client": ticket.client,
        "expected": list(ticket.expected or []),
        "kind": decision.kind,
        "fiches": fiches,
        "titles": {f: decision.titles[f] for f in fiches[:MAX_TITLES_KEPT] if f in decision.titles},
        "score": decision.score,
        "question": decision.question,
        "latency_s": None if decision.latency_s is None else round(decision.latency_s, 4),
        "usage": decision.usage.to_dict(),
        "error": decision.error,
    }


def run_engine(engine, tickets: list[Ticket], runs: int = 1, out_path: str | Path | None = None,
               delay: float = 0.0, progress=None, clock=time.perf_counter, sleep=time.sleep) -> list[dict]:
    labeled = [t for t in tickets if t.labeled]
    records: list[dict] = []
    handle = None
    if out_path:
        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        handle = out_path.open("w", encoding="utf-8", newline="\n")
    try:
        for run in range(runs):
            for index, ticket in enumerate(labeled):
                started = clock()
                try:
                    decision = decision_from_dict(engine.decide(ticket))
                except Exception as exc:  # a crash is a result to report, not a reason to stop
                    decision = Decision("abstain", error=f"{type(exc).__name__}: {exc}")
                if decision.latency_s is None:
                    decision.latency_s = clock() - started
                record = make_record(engine.name, run, ticket, decision)
                records.append(record)
                if handle:
                    handle.write(json.dumps(record, ensure_ascii=False) + "\n")
                    handle.flush()
                if progress:
                    progress(run, index, len(labeled), record)
                if delay:
                    sleep(delay)
    finally:
        if handle:
            handle.close()
    return records


def manifest_path(results_path: str | Path) -> Path:
    results_path = Path(results_path)
    name = results_path.name
    stem = name[: -len(".results.jsonl")] if name.endswith(".results.jsonl") else results_path.stem
    return results_path.with_name(f"{stem}.manifest.json")


def write_manifest(results_path: str | Path, **details) -> Path:
    path = manifest_path(results_path)
    payload = {
        "scoreboard_version": __version__,
        "python": platform.python_version(),
        "written_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        **details,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def expand_paths(patterns: list[str]) -> list[Path]:
    """Expand globs ourselves: PowerShell passes 'results/*.jsonl' through unexpanded."""
    paths: list[Path] = []
    for pattern in patterns:
        matches = sorted(glob.glob(pattern)) if any(ch in pattern for ch in "*?[") else [pattern]
        if not matches:
            raise FileNotFoundError(f"no file matches {pattern}")
        for match in matches:
            path = Path(match)
            if path not in paths:
                paths.append(path)
    return paths


def load_results(paths: list[Path]) -> dict[str, list[dict]]:
    """Results grouped by engine name, in file order."""
    by_engine: dict[str, list[dict]] = {}
    for path in paths:
        with path.open(encoding="utf-8-sig") as handle:
            for lineno, line in enumerate(handle, 1):
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ValueError(f"{path}:{lineno}: invalid JSON ({exc.msg})") from None
                for key in ("engine", "run", "ticket_id", "client", "expected", "kind", "fiches"):
                    if key not in record:
                        raise ValueError(f"{path}:{lineno}: missing '{key}' (is this a results file?)")
                by_engine.setdefault(record["engine"], []).append(record)
    return by_engine


def load_manifests(paths: list[Path]) -> dict[str, dict]:
    """Manifests found next to the results files, keyed by results file name."""
    manifests: dict[str, dict] = {}
    for path in paths:
        candidate = manifest_path(path)
        if candidate.is_file():
            try:
                manifests[path.name] = json.loads(candidate.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                continue
    return manifests
