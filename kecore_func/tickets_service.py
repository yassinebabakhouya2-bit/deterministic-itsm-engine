"""POST /api/kecore/tickets/scrub and POST /api/kecore/tickets/run (V10 slice 4).

scrub: the client's raw ITSM export (``tickets-<client>/raw/*.csv``) is parsed and stripped of
personal data (``kecore.tickets.scrub``), written one row per ticket to Table Storage, and the
raw export deleted. No label, no judgment -- the code only gets tickets into a shape ``kefind``
can read.

run: every scrubbed ticket is passed, unlabeled, through ``kefind``'s funnel exactly as a real
request would be (same code as ``kefind_service.respond``, interpretation included when asked).
Nothing here knows which fiche a ticket should have reached -- this only tallies what the funnel
actually does (fiche shown / question asked / abstained, and why), a first signal before any
ticket is labeled. Real accuracy (exact-fiche@1 and the rest) needs labels and is the scoreboard,
later. Durable (like ``kecore_run``): a batch activity queries, then slices, Table Storage
itself, so only small tallies cross the orchestration boundary, never the ticket text.
"""

from __future__ import annotations

import kecore_pipeline as pipeline
import kefind_service as finder
from kecore.tickets import scrub as scrub_tickets
from kefind.funnel import FunnelConfig, find
from kefind.interpret import interpret

MAX_RUN_LIMIT = 2000
DEFAULT_RUN_LIMIT = 200
RUN_BATCH_SIZE = 25


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
    if run_id is not None and not isinstance(run_id, str):
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
    totals = {"tickets": 0, "skipped": 0, "masked": {}}
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
        storage.delete(container, name)
    return {"client": client, "files": len(names), **totals}


def ticket_count(table, client: str, limit: int) -> int:
    return min(table.count(client), limit)


def run_batch(storage: pipeline.Storage, table, payload: dict, start: int, end: int,
              llm=None, config: FunnelConfig | None = None) -> dict:
    """One slice [start, end) of the client's scrubbed tickets, tallied by what the funnel does."""
    client = payload["client"]
    run_id = payload["run_id"] or finder.latest_run(storage, client)
    if not run_id:
        raise FileNotFoundError("no KB map for this client yet")
    kbmap = finder.load_map(storage, client, run_id)
    entities = table.list(client)[start:end]
    kinds: dict[str, int] = {}
    reasons: dict[str, int] = {}
    empty = 0
    for entity in entities:
        text = "\n".join(str(entity.get(k, "")) for k in ("titre", "sujet", "description") if entity.get(k))
        if not text.strip():
            empty += 1
            continue
        interpretation = interpret(llm, text, kbmap.dictionary) if payload.get("interpret", True) and llm else None
        finding = find(kbmap, text, config=config, interpretation=interpretation)
        kinds[finding.kind] = kinds.get(finding.kind, 0) + 1
        reasons[finding.reason] = reasons.get(finding.reason, 0) + 1
    return {"run_id": run_id, "tickets": len(entities), "empty": empty, "kinds": kinds, "reasons": reasons}


def merge_runs(client: str, parts: list[dict]) -> dict:
    merged = {"client": client, "run_id": parts[0]["run_id"] if parts else None,
              "tickets": 0, "empty": 0, "kinds": {}, "reasons": {}}
    for part in parts:
        merged["tickets"] += part["tickets"]
        merged["empty"] += part["empty"]
        for key, count in part["kinds"].items():
            merged["kinds"][key] = merged["kinds"].get(key, 0) + count
        for key, count in part["reasons"].items():
            merged["reasons"][key] = merged["reasons"].get(key, 0) + count
    return merged


__all__ = [
    "validate_tickets_request", "validate_run_request", "scrub",
    "ticket_count", "run_batch", "merge_runs", "RUN_BATCH_SIZE",
]
