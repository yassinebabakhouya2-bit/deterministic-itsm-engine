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

Outputs, under kecore-<client>/scoreboard/<id>/: dataset.jsonl (the frozen labeled set), results/,
results.jsonl, report.md, summary.json; scoreboard/latest.json names the last run; one row per run
in the ``kecorescores`` table (what the labeling tab displays).
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
from scoreboard.dataset import Ticket, parse_expected, ticket_from_dict
from scoreboard.metrics import Rate, calibrate, summarize, tickets_needed
from scoreboard.pricing import Prices
from scoreboard.report import build_report, fmt_rate, pct
from scoreboard.runner import run_engine

ENGINE = "kefind-funnel"
DEFAULT_MAX_WRONG = 0.05
MAX_APPLY_WRONG = 0.10  # a floor measured against a looser ceiling may be read, never applied
BATCH_SIZE = 25
LATEST = "scoreboard/latest.json"
_ID_RE = re.compile(r"[0-9A-Za-z-]{1,64}")  # always fullmatch


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
    return {"client": client, "run_id": run_id, "interpret": interpret_ticket, "max_wrong": float(max_wrong),
            "sb_id": sb_id}


def layout(sb_id: str) -> dict[str, str]:
    base = f"scoreboard/{sb_id}/"
    return {"dataset": base + "dataset.jsonl", "results_dir": base + "results/", "results": base + "results.jsonl",
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


def prepare(storage: pipeline.Storage, tickets_table, labels_table, payload: dict) -> dict:
    """Freezes the labeled set the run measures (dataset.jsonl): ticket text, accepted fiches, half."""
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
    data = "".join(json.dumps(t.to_dict(), ensure_ascii=False) + "\n" for t in tickets).encode("utf-8")
    storage.write(pipeline.kecore_container(client), layout(payload["sb_id"])["dataset"], data)
    return {"sb_id": payload["sb_id"], "kb_run_id": run_id, "count": len(tickets),
            "with_fiche": sum(1 for t in tickets if t.expected), "without_fiche": sum(1 for t in tickets if not t.expected),
            "missing": missing, "empty": empty, "dataset_sha256": _sha256(data)}


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


def batch(storage: pipeline.Storage, payload: dict, start: int, end: int, llm=None) -> dict:
    """Tickets [start, end) of the frozen set through the funnel, floor off; records to results/."""
    client = payload["client"]
    kbmap = finder.load_map(storage, client, payload["kb_run_id"])
    config = replace(finder.funnel_config(storage, client), min_show=0.0)
    counting = CountingLLM(llm) if llm is not None else None
    engine = FunnelEngine({client: kbmap}, config=config, name=ENGINE, llm=counting)
    records = run_engine(engine, _dataset(storage, payload)[start:end])
    data = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records).encode("utf-8")
    storage.write(pipeline.kecore_container(client), f"{layout(payload['sb_id'])['results_dir']}{start:05d}.jsonl", data)
    return {"start": start, "end": end, "records": len(records), "errors": sum(1 for r in records if r.get("error")),
            "interpret_failures": counting.failures if counting else 0,
            "interpret_refused": counting.refused if counting else 0}


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


def _read_records(storage: pipeline.Storage, payload: dict, ranges: list[list[int]]) -> list[dict]:
    container = pipeline.kecore_container(payload["client"])
    records: list[dict] = []
    for start, _end in ranges:
        data = storage.read(container, f"{layout(payload['sb_id'])['results_dir']}{start:05d}.jsonl")
        if data is None:
            raise FileNotFoundError(f"results of batch {start} are missing")
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


def _rate(data: dict) -> Rate:
    return Rate(data["k"], data["n"])


def report(storage: pipeline.Storage, scores_table, payload: dict, ranges: list[list[int]], prepared: dict,
           batches: list[dict] | None = None) -> dict:
    client = payload["client"]
    container = pipeline.kecore_container(client)
    paths = layout(payload["sb_id"])
    records = _read_records(storage, payload, ranges)
    tickets = _dataset(storage, payload)
    split = {t.ticket_id: t.meta.get("split", "A") for t in tickets}
    texts = {t.key: t.text for t in tickets}
    manifest = {"results.jsonl": {"dataset": paths["dataset"], "dataset_sha256": prepared.get("dataset_sha256"),
                                  "kb_run_id": payload["kb_run_id"], "interpret": payload["interpret"]}}
    markdown, summary = build_report({ENGINE: records}, Prices(), payload["max_wrong"], manifests=manifest,
                                     tickets_text=texts)
    current = finder.funnel_config(storage, client).min_show
    deployed = summarize("deployed", with_floor(records, current)) if records else None
    rec = recommend(records, split, payload["max_wrong"])
    failures = sum(int(b.get("interpret_failures") or 0) for b in (batches or []))
    refused = sum(int(b.get("interpret_refused") or 0) for b in (batches or []))
    source = deployed_source(storage, client)
    markdown = markdown.rstrip() + "\n\n" + "\n".join(_split_section(rec, current, deployed)).rstrip() + "\n"
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
    summary.update(sb_id=payload["sb_id"], client=client, kb_run_id=payload["kb_run_id"], prepared=prepared,
                   interpret=payload["interpret"], interpret_failures=failures, interpret_refused=refused,
                   deployed_source=source,
                   recommendation=rec, deployed_min_show=current,
                   deployed=deployed.to_dict() if deployed else None)
    storage.write(container, paths["results"], "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records).encode("utf-8"))
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
        if summary.get("kb_run_id") != current_map:
            raise ValueError(f"the floor was measured on map {summary.get('kb_run_id')}, the current map is "
                             f"{current_map}: run the scoreboard again on the current map")
        if summary.get("interpret") is not True:
            raise ValueError("the floor was measured without the model's interpretation, which /find uses: "
                             "run the scoreboard again with interpret true")
        if summary.get("interpret_failures"):
            raise ValueError(f"the model failed on {summary['interpret_failures']} ticket(s) during this run: run it again")
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
    "recommend", "with_floor", "split_of", "labels_of", "accepted", "layout", "BATCH_SIZE", "ENGINE",
]
