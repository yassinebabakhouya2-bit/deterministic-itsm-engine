"""POST /api/kecore/tickets/scrub, /tickets/rescrub and /tickets/runs (V10 slice 4).

scrub: the client's raw ITSM export (``tickets-<client>/raw/*.csv``) is parsed and stripped of
personal data (``kecore.tickets.scrub``), written one row per ticket to Table Storage, and the
raw export deleted. No label, no judgment -- the code only gets tickets into a shape ``kefind``
can read.

rescrub: the same cleaning re-applied to the rows already stored (``kecore.tickets.scrub_entity``).
The raw export is gone once scrubbed, so a stricter cleaning can only be applied in place.

run: every scrubbed ticket is passed, unlabeled, through ``kefind``'s funnel exactly as a real
request would be (same code as ``kefind_service.respond``, the client's calibrated settings
included, interpretation included when asked). Nothing here knows which fiche a ticket should have
reached: this tallies what the funnel actually does (fiche shown / question asked / abstained, and
why), and keeps each ticket's finding on its row (``kefind_*``) so the labeling tab can show the
engine's proposal next to the ticket. A run also refreshes the catalog of the map's fiches the
labeling tab offers (``kefindfiches``). Real accuracy needs labels: that is the scoreboard
(``scoreboard_service``). Durable (like ``kecore_run``): a batch activity queries, then slices,
Table Storage itself, so only small tallies cross the orchestration boundary, never ticket text.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from datetime import datetime, timezone

import kecore_pipeline as pipeline
import kefind_service as finder
from kecore.tickets import scrub as scrub_tickets
from kecore.tickets import scrub_entity, ticket_text
from kefind import semantic as sem
from kefind.funnel import FUNNEL_VERSION, FunnelConfig, find
from kefind.interpret import interpret

MAX_RUN_LIMIT = 2000
DEFAULT_RUN_LIMIT = 200
RUN_BATCH_SIZE = 25
MAX_QUESTION_CHARS = 500


def validate_tickets_request(body, allowed_clients) -> dict:
    if not isinstance(body, dict):
        raise ValueError("a JSON object is expected")
    client = body.get("client")
    if not isinstance(client, str) or client not in set(allowed_clients):
        raise ValueError("unknown client")
    return {"client": client}


def validate_run_request(body, allowed_clients) -> dict:
    payload = validate_tickets_request(body, allowed_clients)
    run_id = body.get("run_id")
    if run_id is not None and (not isinstance(run_id, str) or not finder.RUN_ID_RE.fullmatch(run_id)):
        raise ValueError("invalid run id")
    interpret_ticket = body.get("interpret", True)
    if not isinstance(interpret_ticket, bool):
        raise ValueError("interpret must be true or false")
    limit = body.get("limit", DEFAULT_RUN_LIMIT)
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= MAX_RUN_LIMIT:
        raise ValueError(f"limit: an integer between 1 and {MAX_RUN_LIMIT}")
    payload.update(run_id=run_id, interpret=interpret_ticket, limit=limit)
    return payload


def scrub(storage: pipeline.Storage, table, client: str) -> dict:
    """Scrub every raw CSV under ``tickets-<client>/raw/``, write Table rows, delete the raw export."""
    container = pipeline.ticket_container(client)
    names = [n for n in storage.list(container, "raw/") if n.lower().endswith(".csv")]
    if not names:
        return {"client": client, "files": 0, "tickets": 0, "skipped": 0, "masked": {}}
    totals = {"tickets": 0, "skipped": 0, "masked": {}, "unknown_columns": []}
    for name in names:
        data = storage.read(container, name)
        if data is None:
            continue
        tickets, report = scrub_tickets(data)
        for ticket in tickets:
            table.upsert(ticket.to_entity(client))
        totals["tickets"] += report.tickets
        totals["skipped"] += report.skipped
        for label, count in report.masked.items():
            totals["masked"][label] = totals["masked"].get(label, 0) + count
        totals["unknown_columns"] = sorted(set(totals["unknown_columns"]) | set(report.unknown_columns))
        storage.delete(container, name)
    return {"client": client, "files": len(names), **totals}


def rescrub(table, client: str) -> dict:
    """The current cleaning applied to every stored row of the client; only what changes is written."""
    counts: Counter = Counter()
    rows = table.list(client)
    changed = 0
    for row in rows:
        changes = scrub_entity(row, counts)
        if changes:
            table.merge({"PartitionKey": client, "RowKey": row["RowKey"], **changes})
            changed += 1
    return {"client": client, "rows": len(rows), "changed": changed, "masked": dict(sorted(counts.items()))}


def ticket_count(table, client: str, limit: int) -> int:
    return min(table.count(client), limit)


def fiche_row_key(fiche_id: str) -> str:
    """A fiche id is a document name: it may hold characters a RowKey refuses ('/', '#', '?')."""
    return hashlib.sha256(fiche_id.encode("utf-8")).hexdigest()[:32]


def _map_for(storage: pipeline.Storage, payload: dict):
    client = payload["client"]
    run_id = payload.get("run_id") or finder.latest_run(storage, client)
    if not run_id:
        raise FileNotFoundError("no KB map for this client yet")
    return finder.load_map(storage, client, run_id)


def catalog(storage: pipeline.Storage, fiches_table, payload: dict) -> dict:
    """The fiches a labeler may pick (every fiche of the map, searchable or not: a label says what the
    ticket needed, even a fiche the engine cannot reach). Rows of fiches gone from the map are removed."""
    kbmap = _map_for(storage, payload)
    client = payload["client"]
    keep = set()
    searchable = set(kbmap.searchable)
    for fiche_id, fiche in sorted(kbmap.fiches.items()):
        key = fiche_row_key(fiche_id)
        keep.add(key)
        fiches_table.upsert({
            "PartitionKey": client, "RowKey": key, "fiche_id": fiche_id, "label": kbmap.label(fiche_id),
            "status": fiche.status, "searchable": fiche_id in searchable,
            "canonical": kbmap.graph.prune([fiche_id])[0][0] if fiche_id in searchable else fiche_id,
            "kb_run": kbmap.run_id,
        })
    removed = 0
    for row in fiches_table.list(client, select=["RowKey"]):
        if row["RowKey"] not in keep:
            fiches_table.delete(client, row["RowKey"])
            removed += 1
    return {"client": client, "run_id": kbmap.run_id, "fiches": len(keep), "removed": removed}


def _finding_row(client: str, row_key: str, finding, run_id: str, interpreted: bool) -> dict:
    return {
        "PartitionKey": client, "RowKey": row_key,
        "kefind_kind": finding.kind, "kefind_reason": finding.reason,
        "kefind_fiche": finding.fiche_id or "",
        "kefind_candidates": json.dumps(list(finding.fiches[:5]), ensure_ascii=False),
        "kefind_score": "" if finding.score is None else f"{finding.score:.6f}",
        "kefind_designated": bool(finding.designated),
        "kefind_question": (finding.question or "")[:MAX_QUESTION_CHARS],
        "kefind_kb_run": run_id, "kefind_interpreted": interpreted, "kefind_funnel": FUNNEL_VERSION,
        "kefind_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def run_batch(storage: pipeline.Storage, table, payload: dict, start: int, end: int,
              llm=None, config: FunnelConfig | None = None, embedder=None) -> dict:
    """One slice [start, end) of the client's scrubbed tickets, tallied by what the funnel does; each
    ticket's finding is merged into its row. One ticket that fails is counted, never fatal. With a
    semantic index and an ``embedder`` (recorded), each ticket is decided by meaning, exactly as
    /kecore/find decides it; ``modes`` counts how each ticket was decided."""
    client = payload["client"]
    kbmap = _map_for(storage, payload)
    config = config or finder.funnel_config(storage, client)
    entities = table.list(client)[start:end]
    kinds: Counter = Counter()
    reasons: Counter = Counter()
    empty = 0
    errors = 0
    interpret_failures = 0
    modes: Counter = Counter()
    semantic = kbmap.semantic is not None and embedder is not None and getattr(embedder, "model_id", None) == kbmap.semantic.model
    for entity in entities:
        text = ticket_text(entity)
        if not text.strip():
            empty += 1
            continue
        try:
            vector = None
            if semantic:
                try:
                    vector = embedder.embed([sem.query_text(text)])[0]
                except Exception:  # decided by words for this ticket, and counted as degraded
                    vector = None
            modes["semantic" if vector is not None else "degraded" if kbmap.semantic is not None else "words"] += 1
            asked = payload.get("interpret", True) and llm and vector is None
            interpretation = interpret(llm, text, kbmap.dictionary) if asked else None
            finding = find(kbmap, text, config=config, interpretation=interpretation, query_vector=vector)
            interpreted = interpretation is not None and interpretation.error is None
            if interpretation is not None and interpretation.error is not None:
                interpret_failures += 1
            table.merge(_finding_row(client, entity["RowKey"], finding, kbmap.run_id, interpreted))
        except Exception as exc:  # one ticket must not void the run: counted and named, never hidden
            errors += 1
            kinds["error"] += 1
            reasons[f"error:{type(exc).__name__}"] += 1
            continue
        kinds[finding.kind] += 1
        reasons[finding.reason] += 1
    return {"run_id": kbmap.run_id, "tickets": len(entities), "empty": empty, "errors": errors,
            "interpret_failures": interpret_failures, "kinds": dict(kinds), "reasons": dict(reasons),
            "modes": dict(modes)}


def merge_runs(client: str, parts: list[dict]) -> dict:
    merged = {"client": client, "run_id": parts[0]["run_id"] if parts else None,
              "tickets": 0, "empty": 0, "errors": 0, "interpret_failures": 0, "kinds": {}, "reasons": {},
              "modes": {}}
    for part in parts:
        merged["tickets"] += part["tickets"]
        merged["empty"] += part["empty"]
        merged["errors"] += part.get("errors", 0)
        merged["interpret_failures"] += part.get("interpret_failures", 0)
        for key, count in part["kinds"].items():
            merged["kinds"][key] = merged["kinds"].get(key, 0) + count
        for key, count in part["reasons"].items():
            merged["reasons"][key] = merged["reasons"].get(key, 0) + count
        for key, count in (part.get("modes") or {}).items():
            merged["modes"][key] = merged["modes"].get(key, 0) + count
    return merged


__all__ = [
    "validate_tickets_request", "validate_run_request", "scrub", "rescrub", "catalog", "fiche_row_key",
    "ticket_count", "run_batch", "merge_runs", "RUN_BATCH_SIZE",
]
