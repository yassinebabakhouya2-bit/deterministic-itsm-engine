"""POST /api/kecore/scoreboard/runs, GET /api/kecore/scoreboard/latest and
POST /api/kecore/funnel-config/apply (V10 slice 4): the scoreboard on Azure, and the one way a
calibrated threshold reaches production.

A scoreboard run replays every labeled ticket of a client (labels: ``ticketlabels``, written by the
Web App's labeling tab) through kefind's funnel exactly as ``/kecore/find`` runs it -- same map, same
interpretation record -- and measures it with the ``scoreboard`` package, unchanged: the engine is
``kefind.funnel_engine.FunnelEngine``, the replay ``scoreboard.runner.run_engine``, the report
``scoreboard.report.build_report`` (exact fiche @1, wrong fiche shown, recall@5, abstention, each
with its 95% Wilson interval) and the threshold sweep ``scoreboard.metrics.calibrate``.

The funnel runs with its calibrated floor OFF (``min_show = 0``) so the sweep sees every fiche it
would show; what the floor currently deployed does is computed from the same records. Labeled
tickets are split in two halves by a hash of their id (stable from run to run): the threshold is
chosen on half A and confirmed on half B, as the scoreboard README asks -- a threshold is applied
only when both halves hold it under the ceiling. A labeled fiche also accepts what the KB graph maps
it to (its canonical twin, the fiche replacing it): that is the fiche the engine is able to show.

Reference questions (``refquestions``, typed in the labeling tab with the fiche they need, or none)
are measured with the labeled tickets, in the same dataset and the same numbers -- they are ground truth
too -- and also listed one by one in the report's non-regression section: a question passes when the
expected fiche is the one shown, or, expecting none, when no fiche is shown. Their ids start with
``ref-`` (``REF_PREFIX``), so they never collide with a ticket id.

Routing (spec "Semantic enrichment at ingestion, deterministic routing at runtime"): when the run's
candidate routing tables exist (routing/candidate.json, written by a routing build on this map), the run
pins them at prepare and measures a second engine, ``kefind-route`` (``ROUTE_ENGINE``), on the same
frozen questions: the same funnel answering through the tables first, exactly as /find will once a person
activates them. The two engines share one interpretation model and one embedder (one record), the routed
one also gets the L2 mapping model. The report then judges the tables (``scoreboard.metrics.routing_gate``:
no wrong fiche shown as exact, the right fiche first at least as often as without routing, enough
questions, nothing degraded or failed) and writes the verdict to routing/gates/<content>-<run>.json --
the only thing ``routing_service.activate`` accepts. Reference questions a reviewer had on screen while
writing the tables ("seen", ``kefind.review.seen_reference_ids``) are left out of the right-first
comparison, never out of the wrong-exact count.

Degraded decisions (no question vector, the embedding model is not the index's, a failed L2 call: runbook
19.18 #9) are counted per engine: they were not made the way /find makes them, so a run holding one
cannot apply a floor and its routing gate is invalid.

Outputs, under kecore-<client>/scoreboard/<id>/: dataset.jsonl (the frozen labeled set), results/ and
results.jsonl (today's engine), results-route/ and results-route.jsonl (the routed engine, when tables
were pinned), report.md, summary.json; scoreboard/latest.json names the last run; one row per run in the
``kecorescores`` table (what the labeling tab displays).
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import replace
from datetime import datetime, timezone

import kecore_pipeline as pipeline
import kefind_service as finder
from kecore.tickets import ticket_text
from kefind.funnel_engine import FunnelEngine
from kefind.review import ReviewDecision, current_decisions, seen_reference_ids
from kefind.route import TablesMismatchError, check_tables
from kefind.routing import routable
from scoreboard.dataset import Ticket, parse_expected, ticket_from_dict
from scoreboard.metrics import (Rate, calibrate, canonical, first_fiche, route_level, routing_gate, summarize,
                                tickets_needed)
from scoreboard.pricing import Prices
from scoreboard.report import build_report, fmt_rate, pct
from scoreboard.runner import run_engine

ENGINE = "kefind-funnel"
ROUTE_ENGINE = "kefind-route"
DEFAULT_MAX_WRONG = 0.05
MAX_APPLY_WRONG = 0.10  # a floor measured against a looser ceiling may be read, never applied
BATCH_SIZE = 25
LATEST = "scoreboard/latest.json"
_ID_RE = re.compile(r"[0-9A-Za-z-]{1,64}")  # always fullmatch
REF_PREFIX = "ref-"
MAX_REF_TEXT = 500


def validate_scoreboard_request(body, allowed_clients, sb_id: str) -> dict:
    if not isinstance(body, dict):
        raise ValueError("a JSON object is expected")
    client = body.get("client")
    if not isinstance(client, str) or client not in set(allowed_clients):
        raise ValueError("unknown client")
    run_id = body.get("run_id")
    if run_id is not None and (not isinstance(run_id, str) or not finder.RUN_ID_RE.fullmatch(run_id)):
        raise ValueError("invalid run id")
    interpret_ticket = body.get("interpret", True)
    if not isinstance(interpret_ticket, bool):
        raise ValueError("interpret must be true or false")
    max_wrong = body.get("max_wrong", DEFAULT_MAX_WRONG)
    if isinstance(max_wrong, bool) or not isinstance(max_wrong, (int, float)) or not 0 < max_wrong < 0.5:
        raise ValueError("max_wrong: a share between 0 and 0.5 (0.05 = at most 5% wrong fiches shown)")
    if not _ID_RE.fullmatch(sb_id):
        raise ValueError("invalid scoreboard id")
    # the batches call the shared gpt-4o deployment (interpretation, L2 mapping): a few at a time, like a
    # record kecore run (runbook 19.18 #10); function_app._fan_out
    llm_parallelism = body.get("llm_parallelism", pipeline.DEFAULT_LLM_PARALLELISM)
    if isinstance(llm_parallelism, bool) or not isinstance(llm_parallelism, int) \
            or not 1 <= llm_parallelism <= pipeline.MAX_LLM_PARALLELISM:
        raise ValueError(f"llm_parallelism must be an integer between 1 and {pipeline.MAX_LLM_PARALLELISM}")
    return {"client": client, "run_id": run_id, "interpret": interpret_ticket, "max_wrong": float(max_wrong),
            "sb_id": sb_id, "llm_parallelism": llm_parallelism}


def layout(sb_id: str) -> dict[str, str]:
    base = f"scoreboard/{sb_id}/"
    return {"dataset": base + "dataset.jsonl", "results_dir": base + "results/", "results": base + "results.jsonl",
            "route_results_dir": base + "results-route/", "route_results": base + "results-route.jsonl",
            "report": base + "report.md", "summary": base + "summary.json"}


def split_of(ticket_id: str) -> str:
    """Half A or B, from the ticket id alone: the same ticket stays in the same half."""
    return "A" if int(hashlib.sha256(ticket_id.encode("utf-8")).hexdigest()[:8], 16) % 2 == 0 else "B"


def labels_of(labels_table, client: str) -> dict[str, list[str]]:
    """ticket id -> acceptable fiche ids ([] = no fiche covers the ticket). Skipped tickets are left out."""
    out: dict[str, list[str]] = {}
    for row in labels_table.list(client):
        if row.get("skipped"):
            continue
        try:
            expected = parse_expected(json.loads(row.get("expected") or "null"))
        except (ValueError, TypeError):
            continue
        if expected is not None:
            out[row["RowKey"]] = expected
    return out


def references_of(refs_table, client: str) -> list[tuple[str, str, list[str]]]:
    """(id, question, expected fiche ids) of the client's reference questions, in id order; a row
    without a ``ref-`` id, a question or a readable expectation is left out."""
    out = []
    for row in refs_table.list(client) if refs_table is not None else []:
        ref_id, text = row.get("RowKey") or "", " ".join(str(row.get("text") or "").split())
        if not ref_id.startswith(REF_PREFIX) or not _ID_RE.fullmatch(ref_id) or not text:
            continue
        try:
            expected = parse_expected(json.loads(row.get("expected") or "null"))
        except (ValueError, TypeError):
            continue
        if expected is not None:
            out.append((ref_id, text[:MAX_REF_TEXT], expected))
    return out


def accepted(kbmap, expected: list[str]) -> list[str]:
    """The labeled fiches, plus what the graph maps each one to (the fiche the engine can show)."""
    out: list[str] = []
    for fiche_id in expected:
        for candidate in [fiche_id] + (kbmap.graph.prune([fiche_id])[0][:1] if fiche_id in kbmap.fiches else []):
            if candidate not in out:
                out.append(candidate)
    return out


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _routing_svc():
    """routing_service imports this module (the reference questions): imported when needed, never at load."""
    import routing_service

    return routing_service


def pin_routing(storage: pipeline.Storage, client: str, run_id: str, kbmap) -> tuple[dict | None, str | None]:
    """The routing tables this run measures: the candidate (routing/candidate.json) when it was built on
    ``run_id``, its tables load and verify (their own sha256, the candidate's, this code's version) and they
    are this map's (``kefind.route.check_tables``: client, run, knowledge base). Returns (pin, None) with
    pin = {"build_id", "tables_sha256", "content_sha256", "kb_sha256"} -- the tables' own values, checked --
    or (None, why): why is None when there is no candidate at all (nothing to measure, nothing to say)."""
    routing_svc = _routing_svc()
    data = storage.read(pipeline.kecore_container(client), routing_svc.CANDIDATE)
    if data is None:
        return None, None
    try:
        candidate = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None, f"{routing_svc.CANDIDATE} is unreadable: rebuild the routing tables"
    if not isinstance(candidate, dict) or not candidate.get("build_id"):
        return None, f"{routing_svc.CANDIDATE} names no build: rebuild the routing tables"
    if candidate.get("run_id") != run_id:
        return None, f"the candidate routing tables were built on run {candidate.get('run_id')}, not on {run_id}"
    try:
        tables = routing_svc.load_tables(storage, client, candidate["build_id"], candidate.get("tables_sha256"))
        current = routing_svc.source_hashes(storage, client, run_id)
        check_tables(tables, kbmap, current)
    except (ValueError, FileNotFoundError, routing_svc.StoredFileError) as exc:  # TablesMismatchError included
        return None, f"the candidate routing tables cannot be measured ({str(exc)[:300]})"
    if tables.build_id != candidate["build_id"]:
        return None, f"the tables of build {candidate['build_id']} say they are build {tables.build_id}"
    return {"build_id": candidate["build_id"], "tables_sha256": tables.sha256,
            "content_sha256": tables.content_sha256, "kb_sha256": tables.kb_sha256}, None


def prepare(storage: pipeline.Storage, tickets_table, labels_table, payload: dict, refs_table=None,
            now: str | None = None) -> dict:
    """Freezes the labeled set the run measures (dataset.jsonl): ticket text, accepted fiches, half --
    the labeled tickets, then the reference questions -- and pins the candidate routing tables of the run
    (``pin_routing``): "routing" is what every batch measures with them, None when there are none."""
    client = payload["client"]
    run_id = payload.get("run_id") or finder.latest_run(storage, client)
    if not run_id:
        raise FileNotFoundError("no KB map for this client yet")
    kbmap = finder.load_map(storage, client, run_id)
    labels = labels_of(labels_table, client)
    tickets: list[Ticket] = []
    missing = empty = 0
    for ticket_id in sorted(labels):
        entity = tickets_table.get(client, ticket_id)
        if entity is None:
            missing += 1
            continue
        text = ticket_text(entity)
        if not text.strip():
            empty += 1
            continue
        tickets.append(Ticket(ticket_id, client, text, expected=accepted(kbmap, labels[ticket_id]),
                              meta={"split": split_of(ticket_id), "labeled": labels[ticket_id]}))
    references = references_of(refs_table, client)
    for ref_id, text, expected in references:
        tickets.append(Ticket(ref_id, client, text, expected=accepted(kbmap, expected),
                              meta={"split": split_of(ref_id), "labeled": expected, "source": "reference"}))
    data = "".join(json.dumps(t.to_dict(), ensure_ascii=False) + "\n" for t in tickets).encode("utf-8")
    storage.write(pipeline.kecore_container(client), layout(payload["sb_id"])["dataset"], data)
    pin, why_not = pin_routing(storage, client, run_id, kbmap)
    if pin is not None:  # when the labels were frozen: a gate never yields to a measure frozen before it
        pin = {**pin, "prepared_at": now or datetime.now(timezone.utc).isoformat(timespec="microseconds")}
    return {"sb_id": payload["sb_id"], "kb_run_id": run_id, "count": len(tickets),
            "with_fiche": sum(1 for t in tickets if t.expected), "without_fiche": sum(1 for t in tickets if not t.expected),
            "references": len(references), "missing": missing, "empty": empty, "dataset_sha256": _sha256(data),
            "routing": pin, "routing_why_not": why_not}


def _dataset(storage: pipeline.Storage, payload: dict) -> list[Ticket]:
    data = storage.read(pipeline.kecore_container(payload["client"]), layout(payload["sb_id"])["dataset"])
    if data is None:
        raise FileNotFoundError("the frozen labeled set of this scoreboard run is missing")
    return [ticket_from_dict(item) for item in pipeline.read_jsonl(data)]


_TRANSIENT_RE = re.compile(r"HTTP (?:408|429|5\d\d)\b|cannot reach|timed out", re.IGNORECASE)


class CountingLLM:
    """Counts the model calls that failed. ``kefind.interpret`` turns a failure into "no term" and
    goes on (right for one ticket at the desk); a measurement must know it happened. Transient
    failures (throttling, server, network) make the run unfaithful: a rerun would give other numbers.
    Deterministic ones (content filter, refusal, malformed answer) fail the same way at the desk:
    the measurement is faithful, they are only reported."""

    def __init__(self, llm):
        self.llm = llm
        self.failures = 0
        self.refused = 0

    def complete_json(self, *args, **kwargs):
        try:
            return self.llm.complete_json(*args, **kwargs)
        except Exception as exc:
            if _TRANSIENT_RE.search(str(exc)):
                self.failures += 1
            else:
                self.refused += 1
            raise

    def __getattr__(self, name):
        return getattr(self.llm, name)


class CountingEmbedder:
    """``CountingLLM`` for the question vectors: a failed embedding makes the decision degraded (decided by
    words, ``kefind.route``); a transient failure also makes the measure unfaithful (a rerun would embed).
    ``model_id`` (compared with the index's) and every other attribute are the wrapped embedder's."""

    def __init__(self, embedder):
        self.embedder = embedder
        self.failures = 0
        self.refused = 0

    def embed(self, *args, **kwargs):
        try:
            return self.embedder.embed(*args, **kwargs)
        except Exception as exc:
            if _TRANSIENT_RE.search(str(exc)):
                self.failures += 1
            else:
                self.refused += 1
            raise

    def __getattr__(self, name):
        return getattr(self.embedder, name)


class _FailedEngine:
    """The routed engine when its pinned tables cannot be used in this batch: every decision fails (an
    error record each), so the gate is invalid -- never a measure of something else."""

    def __init__(self, error: str):
        self.name = ROUTE_ENGINE
        self.error = error

    def decide(self, ticket):
        raise RuntimeError(self.error)


def _counts(counter) -> tuple[int, int]:
    return (counter.failures, counter.refused) if counter is not None else (0, 0)


def degraded_count(records: list[dict]) -> int:
    """Decisions not made the way /find makes them (``Decision.degraded``), every run included."""
    return sum(1 for r in records if r.get("degraded"))


def _write_records(storage: pipeline.Storage, client: str, folder: str, start: int, records: list[dict]) -> None:
    data = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records).encode("utf-8")
    storage.write(pipeline.kecore_container(client), f"{folder}{start:05d}.jsonl", data)


def _routed_engine(storage: pipeline.Storage, payload: dict, kbmap, config, llm, embedder, intent_llm):
    """``kefind-route``: the same funnel, the same interpretation model and embedder, answering through the
    pinned tables first (``FunnelEngine(routing=...)``, as /find does once they are active). Tables that no
    longer load as pinned, or L2 tables without the L2 model, give ``_FailedEngine``: a routed measure
    without L2 would say nothing about live L2."""
    routing_svc = _routing_svc()
    client, pin = payload["client"], payload["routing"]
    try:
        tables = routing_svc.load_tables(storage, client, pin["build_id"], pin["tables_sha256"])
        current = routing_svc.source_hashes(storage, client, payload["kb_run_id"])
    except (KeyError, TypeError, ValueError, FileNotFoundError, routing_svc.StoredFileError) as exc:
        return _FailedEngine(f"the pinned routing tables cannot be used ({type(exc).__name__}: {str(exc)[:200]})")
    if tables.l2 and intent_llm is None:
        return _FailedEngine("the routing tables have L2 keys and no L2 mapping model was given")
    return FunnelEngine({client: kbmap}, config=config, name=ROUTE_ENGINE, llm=llm, embedder=embedder,
                        routing=tables, intent_llm=intent_llm, current=current)


def batch(storage: pipeline.Storage, payload: dict, start: int, end: int, llm=None, embedder=None,
          intent_llm=None) -> dict:
    """Tickets [start, end) of the frozen set through the funnel, floor off; records to results/. With
    pinned routing tables (``payload["routing"]``) the same tickets also go through the routed engine
    (``intent_llm``: its L2 mapping), records to results-route/.

    The counters of today's engine stay top-level, as before routing ("interpret_failures" is what
    ``apply`` reads); the routed engine's are under "route". Both engines share one CountingLLM and one
    CountingEmbedder (one record: the routed engine reads what the base one recorded), so the routed
    counts are what its own decisions added."""
    client = payload["client"]
    kbmap = finder.load_map(storage, client, payload["kb_run_id"])
    config = replace(finder.funnel_config(storage, client), min_show=0.0)
    counting = CountingLLM(llm) if llm is not None else None
    vectors = CountingEmbedder(embedder) if embedder is not None else None
    tickets = _dataset(storage, payload)[start:end]
    engine = FunnelEngine({client: kbmap}, config=config, name=ENGINE, llm=counting, embedder=vectors)
    records = run_engine(engine, tickets)
    paths = layout(payload["sb_id"])
    _write_records(storage, client, paths["results_dir"], start, records)
    interpret_base, embed_base = _counts(counting), _counts(vectors)
    out = {"start": start, "end": end, "records": len(records), "errors": sum(1 for r in records if r.get("error")),
           "interpret_failures": interpret_base[0], "interpret_refused": interpret_base[1],
           "embed_failures": embed_base[0], "embed_refused": embed_base[1], "degraded": degraded_count(records)}
    if payload.get("routing"):
        mapping = CountingLLM(intent_llm) if intent_llm is not None else None
        routed = run_engine(_routed_engine(storage, payload, kbmap, config, counting, vectors, mapping), tickets)
        _write_records(storage, client, paths["route_results_dir"], start, routed)
        interpret_all, embed_all = _counts(counting), _counts(vectors)
        l2 = _counts(mapping)
        out["route"] = {
            "records": len(routed), "errors": sum(1 for r in routed if r.get("error")),
            "interpret_failures": interpret_all[0] - interpret_base[0],
            "interpret_refused": interpret_all[1] - interpret_base[1],
            "embed_failures": embed_all[0] - embed_base[0], "embed_refused": embed_all[1] - embed_base[1],
            "l2_failures": l2[0], "l2_refused": l2[1], "degraded": degraded_count(routed),
        }
    return out


def with_floor(records: list[dict], threshold: float | None) -> list[dict]:
    """What the funnel does with ``min_show = threshold``: a scored fiche under it is offered, not shown.
    An unscored fiche (designated by the ticket itself) is always shown, as in kefind.funnel._show."""
    if not threshold:
        return list(records)
    out = []
    for record in records:
        if record["kind"] == "fiche" and record.get("score") is not None and float(record["score"]) < threshold:
            record = {**record, "kind": "question"}
        out.append(record)
    return out


def _half(records: list[dict], split: dict[str, str], name: str) -> list[dict]:
    return [r for r in records if split.get(r["ticket_id"]) == name]


def recommend(records: list[dict], split: dict[str, str], max_wrong: float) -> dict:
    """The floor chosen on half A, confirmed (or not) on half B."""
    half_a, half_b = _half(records, split, "A"), _half(records, split, "B")
    out = {"max_wrong": max_wrong, "tickets_a": len(half_a), "tickets_b": len(half_b),
           "tickets_needed_per_half": tickets_needed(max_wrong), "min_show": None, "confirmed": False,
           "reason": ""}
    if not half_a or not half_b:
        out["reason"] = "one half of the labeled tickets is empty: label more tickets"
        return out
    cal = calibrate(half_a, max_wrong)
    if cal is None:
        out["reason"] = "no scored fiche shown on half A: nothing to calibrate"
        return out
    if cal.recommended is None:
        needed = tickets_needed(max_wrong)
        out["reason"] = (f"no threshold can be proven on half A ({len(half_a)} tickets; at least {needed} are needed "
                         f"even with zero wrong fiche)" if needed and len(half_a) < needed
                         else "no threshold keeps half A's wrong fiches under the ceiling")
        return out
    threshold = cal.recommended.threshold or 0.0
    checked = summarize("B", with_floor(half_b, threshold))
    out.update(min_show=threshold, wrong_b=checked.wrong_shown.to_dict(), exact_b=checked.exact.to_dict(),
               exact_a=cal.recommended.exact.to_dict(), wrong_a=cal.recommended.wrong_shown.to_dict())
    out["confirmed"] = checked.wrong_shown.high is not None and checked.wrong_shown.high <= max_wrong
    out["reason"] = ("confirmed on half B" if out["confirmed"]
                     else f"half B's wrong fiches reach {pct(checked.wrong_shown.high)} at the top of their interval")
    return out


def _read_records(storage: pipeline.Storage, payload: dict, ranges: list[list[int]], folder: str = "results_dir"
                  ) -> list[dict]:
    container = pipeline.kecore_container(payload["client"])
    records: list[dict] = []
    for start, _end in ranges:
        data = storage.read(container, f"{layout(payload['sb_id'])[folder]}{start:05d}.jsonl")
        if data is None:
            what = "routed results" if folder == "route_results_dir" else "results"
            raise FileNotFoundError(f"{what} of batch {start} are missing")
        records += pipeline.read_jsonl(data)
    return records


def _split_section(rec: dict, current: float, deployed) -> list[str]:
    lines = ["## Calibrated floor (min_show), chosen on half A, confirmed on half B", ""]
    lines.append(f"Labeled tickets: half A {rec['tickets_a']}, half B {rec['tickets_b']} "
                 f"(a {pct(rec['max_wrong'], 0)} ceiling needs at least {rec['tickets_needed_per_half']} per half "
                 "even with zero wrong fiche).")
    lines.append("")
    if rec["min_show"] is None:
        lines.append(f"No floor recommended: {rec['reason']}.")
    else:
        verdict = "**confirmed**: it can be applied" if rec["confirmed"] else "**not confirmed**: do not apply it"
        lines.append(f"Floor chosen on half A: **{rec['min_show']:.4g}** -> on half B, exact fiche @1 "
                     f"{fmt_rate(_rate(rec['exact_b']))}, wrong fiche shown {fmt_rate(_rate(rec['wrong_b']))}; {verdict}.")
    lines += ["", f"Floor deployed now: {current:.4g}" + (
        f" -> exact fiche @1 {fmt_rate(deployed.exact)}, wrong fiche shown {fmt_rate(deployed.wrong_shown)}, "
        f"no fiche shown {pct(deployed.no_fiche.value)}." if deployed else "."), ""]
    return lines


def _shown(record: dict | None) -> str | None:
    return record["fiches"][0] if record and record.get("kind") == "fiche" and record.get("fiches") else None


def _passes(record: dict | None) -> bool:
    """The expected fiche shown, or (expecting none) no fiche shown -- and no error."""
    if not record or record.get("error"):
        return False
    got = _shown(record)
    return bool((got in record["expected"]) if record["expected"] else got is None)


def reference_results(records: list[dict], tickets: list[Ticket], routed: list[dict] | None = None,
                      seen=()) -> dict:
    """Each reference question, passed or failed: the expected fiche shown, or (expecting none) no fiche shown.
    With the routed engine's records, each row also says what routing did: "route_level", "route_shown",
    "route_first", "route_passed" (the same rule), and "seen" (a reviewer had it on screen while writing the
    tables: left out of the gate's right-first comparison)."""
    by_id = {r["ticket_id"]: r for r in canonical(records)}  # the first run, as the headline numbers read it
    routed_by_id = {r["ticket_id"]: r for r in canonical(routed)} if routed is not None else None
    seen = set(seen or ())
    rows = []
    for t in tickets:
        if t.meta.get("source") != "reference" or t.ticket_id not in by_id:
            continue
        record = by_id[t.ticket_id]
        row = {"id": t.ticket_id, "text": t.text, "expected": list(t.meta.get("labeled") or []),
               "kind": record.get("kind"), "shown": _shown(record), "first": (record.get("fiches") or [None])[0],
               "error": record.get("error"), "passed": _passes(record)}
        if routed_by_id is not None:
            other = routed_by_id.get(t.ticket_id)
            row.update(route_level=route_level(other) if other else None, route_shown=_shown(other),
                       route_first=((other or {}).get("fiches") or [None])[0], route_passed=_passes(other),
                       seen=t.ticket_id in seen)
        rows.append(row)
    return {"n": len(rows), "passed": sum(r["passed"] for r in rows),
            "failed": [r["id"] for r in rows if not r["passed"]], "rows": rows}


def _reference_section(refs: dict) -> list[str]:
    if not refs["n"]:
        return []
    routed = any("route_passed" in r for r in refs["rows"])
    lines = ["## Reference questions (non-regression)", "",
             f"{refs['passed']} / {refs['n']} pass (expected fiche shown, or no fiche shown when none is expected).",
             ""]
    if routed:
        lines += [f"With routing: {sum(1 for r in refs['rows'] if r.get('route_passed'))} / {refs['n']} pass; "
                  "\"seen\": a reviewer had the question on screen while writing the tables (left out of the gate's "
                  "right-first comparison, never out of its wrong-exact count).", ""]
        lines += ["| Question | Expected | Engine | Pass | Routed | Pass (routed) |", "|---|---|---|---|---|---|"]
    else:
        lines += ["| Question | Expected | Engine | Pass |", "|---|---|---|---|"]
    for r in refs["rows"]:
        if r["error"]:
            engine = f"error: {r['error']}"
        elif r["shown"]:
            engine = f"shows {r['shown']}"
        else:
            engine = f"{r['kind']} (first: {r['first'] or '-'})"
        cells = [r["text"][:90], ", ".join(r["expected"]) or "none", engine, "yes" if r["passed"] else "**NO**"]
        if routed:
            what = f"shows {r['route_shown']}" if r["route_shown"] else f"first: {r['route_first'] or '-'}"
            cells += [f"{r['route_level'] or 'error'} {what}" + (" · seen" if r["seen"] else ""),
                      "yes" if r["route_passed"] else "**NO**"]
        lines.append("| " + " | ".join(" ".join(str(c).split()).replace("|", "\\|") for c in cells) + " |")
    return lines + [""]


def seen_references(storage: pipeline.Storage, client: str, pin: dict, kb_run_id: str, tickets: list[Ticket]
                    ) -> tuple[list[str], str | None]:
    """The reference questions of the frozen set a reviewer had on screen while writing the pinned tables
    (``kefind.review.seen_reference_ids`` over the decisions those tables were built from: the build's
    decisions.json, current on the run). When that snapshot cannot be read, EVERY reference question is
    left out of the comparison (a tuned question must never score the tables written from it) and the note
    says why. Returns (sorted ids, note)."""
    references = {t.ticket_id: {"text": t.text, "expected": list(t.expected or [])}
                  for t in tickets if t.meta.get("source") == "reference"}
    if not references:
        return [], None
    routing_svc = _routing_svc()
    name = routing_svc.build_paths(pin["build_id"])["decisions"]
    try:
        data = storage.read(pipeline.kecore_container(client), name)
        if data is None:
            raise FileNotFoundError(f"{name} is missing")
        rows = json.loads(data.decode("utf-8"))
        if not isinstance(rows, list):
            raise ValueError(f"{name} is not a list")
        decisions = [ReviewDecision.from_dict(row) for row in rows if isinstance(row, dict)]
        kbmap = finder.load_map(storage, client, kb_run_id, with_semantic=False)
        chosen = current_decisions(decisions, routing_svc.source_hashes(storage, client, kb_run_id), routable(kbmap))
    except (FileNotFoundError, UnicodeDecodeError, ValueError, TypeError) as exc:
        return sorted(references), (f"every reference question is left out of the comparison: the decisions of "
                                    f"build {pin['build_id']} cannot be read ({str(exc)[:200]})")
    return seen_reference_ids(chosen, references), None


def _gate_section(gate: dict, pin: dict, gate_file: str, seen_note: str | None) -> list[str]:
    verdicts = {"passed": "the tables may be activated",
                "insufficient": "nothing proves them wrong, nothing proves them right: activating them needs a "
                                "person's explicit, recorded override",
                "failed": "they must not be activated: fix or reject the decisions named below",
                "invalid": "this measure says nothing either way: run it again"}
    right = gate["right_first"]
    lines = [f"## Routing gate (tables of build {pin['build_id']})", "",
             f"Status: **{gate['status']}** -- {verdicts.get(gate['status'], '')}.", "",
             f"- Wrong fiche shown as exact (L0-L2): {gate['wrong_exact']['k']} of {gate['wrong_exact']['n']} "
             "questions decided at L0-L2" + (f" ({', '.join(gate['wrong_exact']['ids'])})" if gate['wrong_exact']['ids']
                                              else "") + ".",
             f"- Right fiche first, on {gate['compared']} compared questions: without routing "
             f"{fmt_rate(_rate(right['base']))}, with routing {fmt_rate(_rate(right['route']))} (McNemar: "
             f"{gate['mcnemar']['base_only']} better without, {gate['mcnemar']['route_only']} better with, "
             f"p = {gate['mcnemar']['p_value']:.3g}).",
             f"- Seen by a reviewer, left out of the comparison: {len(gate['excluded'])}"
             + (f" ({', '.join(gate['excluded'])})" if gate["excluded"] else "") + "."]
    if seen_note:
        lines.append(f"- {seen_note}.")
    lines += [f"- {reason}" for reason in gate["reasons"]]
    lines += ["", f"{ROUTE_ENGINE} shows a fiche alone only at L0-L2, where a person validated the route; at L3 it "
                  "offers the fiches today's engine finds, the right one first as often. Its \"exact fiche @1\" above "
                  "is therefore not today's engine's: the gate compares the right fiche first.", "",
              f"Verdict file: `{gate_file}` (what POST /api/kecore/routing/activate reads).", ""]
    return lines


def _rate(data: dict) -> Rate:
    return Rate(data["k"], data["n"])


_ROUTE_COUNTERS = ("records", "errors", "interpret_failures", "interpret_refused", "embed_failures", "embed_refused",
                   "l2_failures", "l2_refused", "degraded")


def _sum(batches: list[dict], key: str, sub: str | None = None) -> int:
    return sum(int(((b.get(sub) or {}) if sub else b).get(key) or 0) for b in batches)


def _degraded_reasons(records: list[dict]) -> dict[str, int]:
    """How many decisions carry each degraded mark ("no_embedder", "embedding_failed:LLMError"...), sorted."""
    counts: dict[str, int] = {}
    for record in records:
        if record.get("degraded"):
            counts[str(record["degraded"])] = counts.get(str(record["degraded"]), 0) + 1
    return dict(sorted(counts.items()))


def _gate_bytes(data: dict) -> bytes:
    """As routing_service writes its files: sorted keys, indent 1, UTF-8, a final newline."""
    return (json.dumps(data, ensure_ascii=False, sort_keys=True, indent=1) + "\n").encode("utf-8")


def _gate_file(gate: dict, pin: dict, sb_id: str, kb_run_id: str, seen: list[str], failures: int, degraded: int
               ) -> dict:
    """routing/gates/<content_sha256>-<kb_run_id>.json: the verdict ``routing_service.activate`` reads
    (it checks content_sha256 and kb_run_id against the tables it activates; GATE_KEYS are shown in the
    Revue tab). route_first / base_first are the gate's right-first rates."""
    return {"sb_id": sb_id, "status": gate["status"], "passed": gate["passed"], "build_id": pin["build_id"],
            "tables_sha256": pin["tables_sha256"], "content_sha256": pin["content_sha256"],
            "kb_sha256": pin.get("kb_sha256"), "kb_run_id": kb_run_id, "reasons": list(gate["reasons"]),
            "wrong_exact": gate["wrong_exact"], "route_first": gate["right_first"]["route"],
            "base_first": gate["right_first"]["base"], "compared": gate["compared"], "excluded": gate["excluded"],
            "tickets": gate["tickets"], "routed_exact": gate["routed_exact"], "mcnemar": gate["mcnemar"],
            "by_level": gate["by_level"], "seen": list(seen), "failures": failures, "degraded": degraded,
            "prepared_at": pin.get("prepared_at") or ""}


def report(storage: pipeline.Storage, scores_table, payload: dict, ranges: list[list[int]], prepared: dict,
           batches: list[dict] | None = None) -> dict:
    """The measure: report.md, summary.json, results.jsonl (and results-route.jsonl), latest.json, the
    kecorescores row -- and, when routing tables were pinned at prepare, their gate (routing/gates/)."""
    client = payload["client"]
    container = pipeline.kecore_container(client)
    paths = layout(payload["sb_id"])
    batches = list(batches or [])
    pin = (prepared or {}).get("routing") or None
    records = _read_records(storage, payload, ranges)
    routed = _read_records(storage, payload, ranges, "route_results_dir") if pin else None
    tickets = _dataset(storage, payload)
    split = {t.ticket_id: t.meta.get("split", "A") for t in tickets}
    texts = {t.key: t.text for t in tickets}
    manifest = {"results.jsonl": {"dataset": paths["dataset"], "dataset_sha256": prepared.get("dataset_sha256"),
                                  "kb_run_id": payload["kb_run_id"], "interpret": payload["interpret"]}}
    results = {ENGINE: records}
    if routed is not None:
        manifest["results-route.jsonl"] = {**manifest["results.jsonl"], "routing": dict(pin)}
        results[ROUTE_ENGINE] = routed
    markdown, summary = build_report(results, Prices(), payload["max_wrong"], manifests=manifest, tickets_text=texts)
    current = finder.funnel_config(storage, client).min_show
    deployed = summarize("deployed", with_floor(records, current)) if records else None
    rec = recommend(records, split, payload["max_wrong"])
    failures = _sum(batches, "interpret_failures")
    refused = _sum(batches, "interpret_refused")
    embed_failures = _sum(batches, "embed_failures")
    degraded = {"base": degraded_count(records), "route": degraded_count(routed) if routed is not None else None,
                "reasons": {ENGINE: _degraded_reasons(records),
                            **({ROUTE_ENGINE: _degraded_reasons(routed)} if routed is not None else {})}}
    source = deployed_source(storage, client)

    routing = gate = None
    seen: list[str] = []
    if pin:
        seen, seen_note = seen_references(storage, client, pin, payload["kb_run_id"], tickets)
        counters = {key: _sum(batches, key, "route") for key in _ROUTE_COUNTERS}
        transient = (failures + embed_failures + counters["interpret_failures"] + counters["embed_failures"]
                     + counters["l2_failures"])
        both_degraded = degraded["base"] + (degraded["route"] or 0)
        gate = routing_gate(records, routed, seen, failures=transient, degraded=both_degraded)
        gate_name = _routing_svc().gate_path(pin["content_sha256"], payload["kb_run_id"])
        gate_file = _gate_file(gate, pin, payload["sb_id"], payload["kb_run_id"], seen, transient, both_degraded)
        # the verdict on file is the one of the measure whose labels were frozen LAST: an older measure
        # finishing late never replaces a newer one (a "failed" found on more labels stays)
        try:
            on_file = json.loads((storage.read(container, gate_name) or b"null").decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            on_file = None
        newer = isinstance(on_file, dict) and str(on_file.get("prepared_at") or "") > gate_file["prepared_at"]
        if not newer:
            storage.write(container, gate_name, _gate_bytes(gate_file))
        routing = {"engine": ROUTE_ENGINE, **pin, "gate": gate, "gate_file": gate_name, "gate_file_kept": newer,
                   "seen": seen,
                   "seen_note": seen_note, "failures": transient, "counters": counters}

    refs = reference_results(records, tickets, routed, seen)
    markdown = markdown.rstrip() + "\n\n" + "\n".join(_split_section(rec, current, deployed)).rstrip() + "\n"
    if refs["n"]:
        markdown += "\n" + "\n".join(_reference_section(refs)).rstrip() + "\n"
    if gate is not None:
        section = _gate_section(gate, pin, routing["gate_file"], routing["seen_note"])
        markdown += "\n" + "\n".join(section).rstrip() + "\n"
    elif prepared.get("routing_why_not"):
        markdown += f"\nRouting: the candidate tables were not measured -- {prepared['routing_why_not']}.\n"
    if source.get("kb_run_id") and current and source["kb_run_id"] != payload["kb_run_id"]:
        markdown += (f"\n**Warning:** the floor deployed now ({current:.4g}) was calibrated on map "
                     f"{source['kb_run_id']}, not on this one ({payload['kb_run_id']}).\n")
    if failures:
        markdown += (f"\n**Warning:** the model failed to interpret {failures} ticket(s) (throttling, server or "
                     "network); they were measured on their own words only. This run's floor cannot be applied: "
                     "run it again.\n")
    if refused:
        markdown += (f"\nThe model declined or failed for good on {refused} ticket(s) (content filter, refusal, "
                     "malformed answer): /find does the same on them, so they are measured as they really go.\n")
    for name, count in ((ENGINE, degraded["base"]), (ROUTE_ENGINE, degraded["route"])):
        if count:
            why = ", ".join(f"{mark}: {n}" for mark, n in degraded["reasons"][name].items())
            markdown += (f"\n**Warning:** {count} decision(s) of {name} were degraded ({why}): they were not made "
                         "the way /find makes them. This run's floor cannot be applied and its routing gate is "
                         "invalid: fix the cause, then run it again.\n")
    summary.update(sb_id=payload["sb_id"], client=client, kb_run_id=payload["kb_run_id"], prepared=prepared,
                   interpret=payload["interpret"], interpret_failures=failures, interpret_refused=refused,
                   embed_failures=embed_failures, deployed_source=source,
                   recommendation=rec, deployed_min_show=current, references=refs,
                   deployed=deployed.to_dict() if deployed else None, degraded=degraded, routing=routing)
    storage.write(container, paths["results"], "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records).encode("utf-8"))
    if routed is not None:
        storage.write(container, paths["route_results"],
                      "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in routed).encode("utf-8"))
    storage.write(container, paths["report"], markdown.encode("utf-8"))
    storage.write(container, paths["summary"], pipeline._json(summary))
    storage.write(container, LATEST, pipeline._json({"sb_id": payload["sb_id"]}))
    engine = summary["engines"][ENGINE]
    short = {
        "client": client, "sb_id": payload["sb_id"], "kb_run_id": payload["kb_run_id"], "tickets": engine["tickets"],
        "with_fiche": engine["with_fiche"], "without_fiche": engine["without_fiche"],
        "exact": engine["exact"], "wrong_shown": engine["wrong_shown"], "no_fiche": engine["no_fiche"],
        "recall_at_5": engine["recall_at_k"], "errors": engine["errors"],
        "recommended_min_show": rec["min_show"], "confirmed": rec["confirmed"], "reason": rec["reason"],
        "deployed_min_show": current, "interpret": payload["interpret"], "interpret_failures": failures,
        "interpret_refused": refused,
        "references": {"n": refs["n"], "passed": refs["passed"], "failed": refs["failed"]},
        "routing": {"status": gate["status"] if gate else None, "build_id": pin["build_id"] if pin else None,
                    "why_not": None if pin else prepared.get("routing_why_not")},
        "degraded": {"base": degraded["base"], "route": degraded["route"]},
    }
    scores_table.upsert({
        "PartitionKey": client, "RowKey": payload["sb_id"], "kb_run": payload["kb_run_id"],
        "tickets": engine["tickets"], "with_fiche": engine["with_fiche"],
        "exact_k": engine["exact"]["k"], "exact_n": engine["exact"]["n"],
        "wrong_k": engine["wrong_shown"]["k"], "wrong_n": engine["wrong_shown"]["n"],
        "recall_k": engine["recall_at_k"]["k"], "recall_n": engine["recall_at_k"]["n"],
        "recommended_min_show": "" if rec["min_show"] is None else f"{rec['min_show']:.6f}",
        "confirmed": rec["confirmed"], "reason": rec["reason"][:500], "max_wrong": payload["max_wrong"],
        "deployed_min_show": current, "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "ref_k": refs["passed"], "ref_n": refs["n"], "ref_failed": json.dumps(refs["failed"])[:2000],
        # the routing gate of the pinned tables ("" / 0: no tables measured); never None: the Tables SDK drops it
        "route_status": gate["status"] if gate else "", "route_build": pin["build_id"] if pin else "",
        "route_wrong_k": gate["wrong_exact"]["k"] if gate else 0,
        "route_wrong_n": gate["wrong_exact"]["n"] if gate else 0,
        "route_first_k": gate["right_first"]["route"]["k"] if gate else 0,
        "base_first_k": gate["right_first"]["base"]["k"] if gate else 0,
        "route_compared": gate["compared"] if gate else 0, "degraded_k": degraded["base"],
    })
    return short


def deployed_source(storage: pipeline.Storage, client: str) -> dict:
    """Where the deployed floor comes from (funnel-config.json's "source"), {} when there is none."""
    data = storage.read(pipeline.kecore_container(client), finder.FUNNEL_CONFIG)
    if data is None:
        return {}
    source = json.loads(data.decode("utf-8")).get("source")
    return source if isinstance(source, dict) else {}


def latest(storage: pipeline.Storage, client: str) -> dict | None:
    container = pipeline.kecore_container(client)
    pointer = storage.read(container, LATEST)
    if pointer is None:
        return None
    sb_id = json.loads(pointer.decode("utf-8"))["sb_id"]
    paths = layout(sb_id)
    summary = storage.read(container, paths["summary"])
    markdown = storage.read(container, paths["report"])
    if summary is None or markdown is None:
        return None
    return {"sb_id": sb_id, "summary": json.loads(summary.decode("utf-8")), "report_md": markdown.decode("utf-8")}


def validate_apply_request(body, allowed_clients) -> dict:
    if not isinstance(body, dict):
        raise ValueError("a JSON object is expected")
    client = body.get("client")
    if not isinstance(client, str) or client not in set(allowed_clients):
        raise ValueError("unknown client")
    reset = body.get("reset", False)
    if not isinstance(reset, bool):
        raise ValueError("reset must be true or false")
    sb_id = body.get("scoreboard_id")
    if not reset and (not isinstance(sb_id, str) or not _ID_RE.fullmatch(sb_id)):
        raise ValueError("scoreboard_id: the id of the scoreboard run whose floor to apply (or reset: true)")
    return {"client": client, "scoreboard_id": sb_id, "reset": reset}


def apply(storage: pipeline.Storage, payload: dict, now: str | None = None) -> dict:
    """Writes the client's funnel-config.json: the confirmed floor of one scoreboard run, or no floor
    (reset). The previous file is kept under funnel-config.history/ first."""
    client = payload["client"]
    container = pipeline.kecore_container(client)
    now = now or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    previous = storage.read(container, finder.FUNNEL_CONFIG)
    current = json.loads(previous.decode("utf-8")) if previous else {"funnel": {}}
    funnel = dict(current.get("funnel") or {})
    if payload["reset"]:
        funnel.pop("min_show", None)
        source = {"reset": True, "applied_at": now}
    else:
        data = storage.read(container, layout(payload["scoreboard_id"])["summary"])
        if data is None:
            raise FileNotFoundError("unknown scoreboard run")
        summary = json.loads(data.decode("utf-8"))
        rec = summary.get("recommendation") or {}
        if rec.get("min_show") is None:
            raise ValueError(f"this scoreboard run recommends no floor ({rec.get('reason') or 'no recommendation'})")
        if not rec.get("confirmed"):
            raise ValueError(f"the floor was not confirmed on half B ({rec.get('reason')}): it is not applied")
        current_map = finder.latest_run(storage, client)
        if current_map and storage.read(container, f"runs/{current_map}/semantic/calibration.json") is not None:
            raise ValueError("the current map decides by meaning, with its own calibration (semantic/calibration.json); "
                             "min_show only applies to the word path and cannot be set from a scoreboard of it")
        if summary.get("kb_run_id") != current_map:
            raise ValueError(f"the floor was measured on map {summary.get('kb_run_id')}, the current map is "
                             f"{current_map}: run the scoreboard again on the current map")
        if summary.get("interpret") is not True:
            raise ValueError("the floor was measured without the model's interpretation, which /find uses: "
                             "run the scoreboard again with interpret true")
        if summary.get("interpret_failures"):
            raise ValueError(f"the model failed on {summary['interpret_failures']} ticket(s) during this run: run it again")
        degraded = summary.get("degraded")
        degraded = degraded.get("base") if isinstance(degraded, dict) else degraded
        if degraded:
            raise ValueError(f"{degraded} decision(s) of this run were degraded (not made the way /find makes them): "
                             "fix the cause, then run it again")
        if float(rec.get("max_wrong") or 1) > MAX_APPLY_WRONG:
            raise ValueError(f"the floor was chosen for a ceiling of {rec.get('max_wrong')}: only a ceiling of "
                             f"{MAX_APPLY_WRONG} or stricter may be applied")
        funnel["min_show"] = rec["min_show"]
        source = {"scoreboard_id": payload["scoreboard_id"], "kb_run_id": summary.get("kb_run_id"),
                  "applied_at": now, "evidence": {k: rec.get(k) for k in ("max_wrong", "exact_b", "wrong_b",
                                                                          "tickets_a", "tickets_b")}}
    if previous is not None:
        storage.write(container, f"funnel-config.history/{now}.json", previous)
    config = {"funnel": funnel, "source": source}
    storage.write(container, finder.FUNNEL_CONFIG, pipeline._json(config))
    return {"client": client, **config}


__all__ = [
    "validate_scoreboard_request", "validate_apply_request", "prepare", "batch", "report", "latest", "apply",
    "recommend", "with_floor", "split_of", "labels_of", "accepted", "layout", "BATCH_SIZE", "ENGINE", "ROUTE_ENGINE",
    "pin_routing", "seen_references", "reference_results", "degraded_count", "CountingLLM", "CountingEmbedder",
]
