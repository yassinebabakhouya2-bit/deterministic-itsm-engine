"""fn-kecore — the V10 KB decomposition (slice 2), the fiche finder (slice 3) and real tickets
with their scoreboard (slice 4), run in Azure.

POST /api/kecore/runs  (function key)  {"client": "client-s", "source_prefix": "Kbs/",
                                         "mode": "replay" | "record", "batch_size": 10,
                                         "limit": null, "with_dictionary": true}
  -> starts one Durable Functions run and answers with its status URLs.

The run: extract -> profile -> decompose (batches in parallel, at most 4 at a time, see
host.json) -> report -> the semantic index ("semantic": true, the default: cards, exam, index,
calibration -- semantic_service.py) -> publish (latest.json, last). Each step is an activity that
reads from and writes to blob storage (kecore_pipeline.py); kecore itself is unchanged. Identity:
the Function's managed identity, for blob storage and for Azure OpenAI alike. A client not listed
in KECORE_CLIENTS is refused.

POST /api/kecore/find  (function key)  {"client": "client-s", "text": "<ticket>",
                                         "answers": ["app:teams"], "run_id": null, "interpret": true}
  -> the decision (fiche / question / abstain), its trace, and the fiche's verified steps.
     The decision is code (kefind_service.py, kefind.funnel). When the run has a semantic index,
     the question is embedded once (text-embedding-3-large, recorded under the hash of its text in
     kecore-<client>/find-cache/: the same question always gets the same vector) and decided by
     meaning against the run's calibrated thresholds; "mode" says "semantic". Otherwise the words
     decide, with the client's calibrated settings (funnel-config.json) and, with "interpret"
     (default), one recorded model call that turns the ticket into search terms (kefind.interpret).
     The KB map of a run is read once and kept in memory (4 maps at most).
     Routing: when a person activated routing tables for the run served (and the kill switch is off),
     the tables answer first (kefind.route.answer: L0 number/code, L1 validated alias, L2 intent and
     application with one recorded mapping call, L3 today's engine offered, L4 abstain) and "route" says
     how; otherwise "route" is null and the answer is today's, "routing_error" saying why tables that exist
     were not used. A failing route never fails the answer: it is answered again without routing.

POST /api/kecore/tickets/scrub  (function key)  {"client": "client-s"}
  -> scrubs every raw export under tickets-<client>/raw/*.csv (kecore.tickets.scrub: personal
     columns dropped, every other column cleaned), writes one Table row per ticket, and deletes
     the raw export. No label, no judgment.

POST /api/kecore/tickets/rescrub  (function key)  {"client": "client-s"}
  -> the current cleaning re-applied in place to the rows already stored (the raw export is gone).

POST /api/kecore/tickets/runs  (function key)  {"client": "client-s", "run_id": null,
                                                 "interpret": true, "limit": 200}
  -> one Durable run of every scrubbed ticket through kefind's funnel, unlabeled: refreshes the
     catalog of fiches the labeling tab offers, keeps each ticket's finding on its row, and
     tallies what the funnel does (fiche shown / question asked / abstain, and why).

POST /api/kecore/scoreboard/runs  (function key)  {"client": "client-s", "run_id": null,
                                                     "interpret": true, "max_wrong": 0.05}
  -> one Durable run of every LABELED ticket through the funnel, measured by the scoreboard
     package (exact fiche @1, wrong fiche shown, recall@5, 95% intervals) with the floor
     (min_show) chosen on half the labels and confirmed on the other half. When the run's candidate
     routing tables exist, the same questions also go through them ("kefind-route") and the run writes
     their gate (routing/gates/), which an activation requires; "llm_parallelism" (default 2) batches at
     a time.
GET  /api/kecore/scoreboard/latest?client=client-s  (function key)
  -> the last scoreboard run: summary and Markdown report.
POST /api/kecore/funnel-config/apply  (function key)  {"client": "client-s", "scoreboard_id": "<id>"}
                                                       or {"client": "client-s", "reset": true}
  -> writes the client's funnel-config.json from a CONFIRMED recommendation (refused otherwise),
     the previous file kept under funnel-config.history/. The only way a threshold reaches /find.

GET  /api/kecore/fiche?client=client-s&fiche_id=KB0120[&run_id=...]  (function key)
  -> one fiche of the map: its verified steps, its neighbours and its own text (slice 5: the web
     app's Diagnostic guides with these steps).
GET  /api/kecore/dictionary?client=client-s  (function key)
POST /api/kecore/dictionary/decision  (function key)  {"client", "term"|"entry", "accept", "canonical", "by"}
  -> the client dictionary's review (slice 5, pilier 2): the current entries, the names live
     questions brought (every /find observes its question: only the candidate name and its count
     are kept), and a person's decisions, applied at the next kecore run.

The routing (routing_service.py; spec "Semantic enrichment at ingestion, deterministic routing at runtime"):
nothing routes unless a person validated it, and /find uses only tables a person activated.
POST /api/kecore/routing/runs  (function key)  {"client", "mode"?, "measure"?, "llm_parallelism"?, "batch_size"?}
  -> one Durable routing run (instance id = build id; 409 while another runs): pass B while no taxonomy
     is frozen, pass C when the frozen taxonomy or the run changed, then the routing tables ALWAYS (L0
     numbers and L1 aliases need neither), then their measure by the scoreboard ("measure", default true).
     A kecore run does the same before it publishes ("routing", default true), without the measure.
GET  /api/kecore/routing?client=  -> taxonomy, last build, run in progress, candidate, active, gate, effective
POST /api/kecore/routing/activate  {"client", "build_id", "by", "accept_insufficient"?, "reason"?}
  -> 200 {"active"}; 409 unless the build is on the served run and its measure passed (or was
     insufficient and the person accepts it with a reason). Clears the kill switch.
POST /api/kecore/routing/deactivate  {"client", "by"}  -> the sticky kill switch: 200 {"active": null, "killed": true}
GET  /api/kecore/review?client=[&status=]  GET /api/kecore/review/fiche?client=&key=
GET  /api/kecore/review/next?client=&status=proposed|stale
POST /api/kecore/review/decision  POST /api/kecore/review/carry  POST /api/kecore/review/taxonomy
  -> the Web App's "Revue" tab (app/review_tab.py): per-fiche decisions (table kefindreview), carried
     over to a changed text, and the taxonomy freeze. Refusals shown to the reviewer are French.
"""

from __future__ import annotations

import functools
import json
import os
import time
import urllib.parse
import uuid
from datetime import datetime, timezone

import azure.durable_functions as df
import azure.functions as func

import dictionary_service as dictionary_svc
import enrichment_service as enrichment_svc
import kecore_pipeline as pipeline
import kefind_service as finder
import routing_service as routing_svc
import scoreboard_service as sb_svc
import semantic_service as semantic_svc
import tickets_service as tickets_svc
from kecore.llm import AzureOpenAIChat, AzureOpenAIEmbeddings, RecordingEmbeddings, RecordingLLM
from kefind.assign import C_LLM_OVERRIDES
from kefind.route import check_tables
from kefind.taxonomy import B_LLM_OVERRIDES

app = df.DFApp(http_auth_level=func.AuthLevel.FUNCTION)

_storage = None


def storage():
    global _storage
    if _storage is None:
        from kecore_blob import BlobStorage

        _storage = BlobStorage()
    return _storage


_tables: dict = {}


def table(name: str | None = None):
    """One of the engine's tables (kecore_table: tickets, kefindfiches, ticketlabels, kecorescores)."""
    from kecore_table import TICKETS, TableStorage

    name = name or os.environ.get("KECORE_TICKETS_TABLE", TICKETS)
    if name not in _tables:
        _tables[name] = TableStorage(table=name)
    return _tables[name]


def allowed_clients() -> list[str]:
    return [c.strip() for c in os.environ.get("KECORE_CLIENTS", "").split(",") if c.strip()]


def llm_config() -> dict:
    return {
        "endpoint": os.environ["KECORE_AOAI_ENDPOINT"],
        "deployment": os.environ["KECORE_AOAI_DEPLOYMENT"],
        "api_version": os.environ.get("KECORE_AOAI_API_VERSION", "2024-10-21"),
        "auth": "entra",
    }


def embedding_config() -> dict:
    """The text-embedding-3-large deployment of the same Foundry resource -- same endpoint, same
    keyless role (Cognitive Services OpenAI User is resource-wide, not per-deployment)."""
    return {
        "endpoint": os.environ["KECORE_AOAI_ENDPOINT"],
        "deployment": os.environ.get("KECORE_AOAI_EMBEDDING_DEPLOYMENT", "text-embedding-3-large"),
        "api_version": os.environ.get("KECORE_AOAI_API_VERSION", "2024-10-21"),
        "auth": "entra",
    }


def model_id() -> str:
    config = llm_config()
    return f"{config['deployment']}@{urllib.parse.urlparse(config['endpoint']).netloc}"


# Two budgets for the model calls (kecore.azure.RestClient), runbook 19.18 #10 and the lot-1 review:
# - an offline run or a measurement (kecore run, tickets run, scoreboard) waits its turn on the shared
#   gpt-4o quota (30 kTPM, also serving the live Assistant): 12 new attempts after a 429, each waiting what
#   the service asks (at most 60 s) -- the first real pass-A run lost 21 fiches out of 180 with 5;
# - a live /kecore/find answers a Web App that stops waiting after 30 s (app/kecore_client.py): a short
#   read timeout and 2 new attempts, so a hung service is given up quickly instead of holding a worker.
BATCH_THROTTLE_RETRIES = 12
BATCH_BUDGET = {"throttle_retries": BATCH_THROTTLE_RETRIES}
LIVE_BUDGET = {"timeout_s": 25.0, "retries": 2, "throttle_retries": 2}


def make_llm(payload: dict, **overrides) -> RecordingLLM:
    """The model of a run, behind the client's record (llm-cache/), on the batch budget. ``overrides``: the
    settings a pass needs on top of it (pass B: a long answer, kefind.taxonomy.B_LLM_OVERRIDES; pass C: a
    short one, kefind.assign.C_LLM_OVERRIDES) -- none of them is in the record key except ``deployment``."""
    # Built even in replay mode: the record key holds "<deployment>@<host>", taken from the client.
    # In replay mode RecordingLLM never calls it; a missing answer is an error, never a model call.
    inner = AzureOpenAIChat.from_config({**llm_config(), **BATCH_BUDGET, **overrides})
    return RecordingLLM(inner, mode=payload["mode"], store=pipeline.StorageRecordStore(storage(), payload["client"]))


def pass_bc_deployment() -> dict:
    """{"deployment": KECORE_AOAI_BATCH_DEPLOYMENT} when that setting names one, else {}: passes B and C (the
    routing's offline passes, ~180 calls after each freeze) may run on their own deployment and quota,
    away from the live answer path. Passes B and C only: every other record keeps its key."""
    deployment = os.environ.get("KECORE_AOAI_BATCH_DEPLOYMENT", "").strip()
    return {"deployment": deployment} if deployment else {}


def taxonomy_llm(payload: dict) -> RecordingLLM:
    """Pass B: one long answer (a taxonomy), so a long read timeout and a single retry."""
    return make_llm(payload, **B_LLM_OVERRIDES, **pass_bc_deployment())


def assign_llm(payload: dict) -> RecordingLLM:
    """Pass C: one short answer per fiche, so a small max_tokens (Azure counts it against the quota)."""
    return make_llm(payload, **C_LLM_OVERRIDES, **pass_bc_deployment())


# JSON answers are ASCII (non-ASCII characters escaped) and declare their charset: the Python worker
# sends a bare "application/json", which Windows PowerShell 5.1 decodes as ISO-8859-1 -- a fiche id
# with an en dash came back as mojibake and the next call with it answered 404 (runbook 19.8).
JSON_MIMETYPE = "application/json; charset=utf-8"


def _error(status: int, message: str) -> func.HttpResponse:
    return func.HttpResponse(json.dumps({"error": message}), status_code=status, mimetype=JSON_MIMETYPE)


def _json(data, status: int = 200) -> func.HttpResponse:
    return func.HttpResponse(json.dumps(data), status_code=status, mimetype=JSON_MIMETYPE)


def _body(req: func.HttpRequest):
    try:
        return req.get_json()
    except ValueError:
        return None


def _new_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:6]


def _now() -> str:
    """The time an activity or a route records (never read inside an orchestrator: not deterministic)."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@app.route(route="kecore/runs", methods=["POST"])
@app.durable_client_input(client_name="client")
async def kecore_start(req: func.HttpRequest, client) -> func.HttpResponse:
    body = _body(req)
    if body is None:
        return _error(400, "a JSON body is expected")
    run_id = _new_id()
    try:
        payload = pipeline.validate_request(body, allowed_clients(), run_id)
    except ValueError as exc:
        return _error(400, str(exc))
    instance_id = await client.start_new("kecore_run", client_input=payload)
    return client.create_check_status_response(req, instance_id)


def _fan_out(context, name: str, payload: dict, ranges: list[list[int]]):
    """One activity per [start, end) range. In a "record" run the batches that may call the model go
    ``llm_parallelism`` at a time (a shared quota, runbook 19.18 #10); in "replay" nothing is called, all
    at once. Results come back in range order either way. Used with ``yield from``."""
    # An orchestration started before llm_parallelism existed has no such key: it keeps the shape it was
    # scheduled with (everything at once), or its replay after a redeploy would not match its history.
    if payload.get("mode") == "replay" or "llm_parallelism" not in payload:
        width = len(ranges)
    else:
        width = payload["llm_parallelism"]
    width = max(1, width)
    results: list = []
    for i in range(0, len(ranges), width):
        results += yield context.task_all(
            [context.call_activity(name, {**payload, "start": start, "end": end}) for start, end in ranges[i:i + width]]
        )
    return results


@app.orchestration_trigger(context_name="context")
def kecore_run(context: df.DurableOrchestrationContext):
    payload = context.get_input()
    extracted = yield context.call_activity("kecore_extract", payload)
    if not extracted["fiches"]:
        return {"run_id": payload["run_id"], "error": "no readable fiche under this prefix", "extract": extracted}
    profiled = yield context.call_activity("kecore_profile", payload)
    ranges = pipeline.batches(extracted["fiches"], payload["batch_size"])
    stats = yield from _fan_out(context, "kecore_decompose", payload, ranges)
    summary = yield context.call_activity(
        "kecore_report",
        {**payload, "ranges": ranges, "profile_stats": profiled, "batch_stats": stats, "warnings": extracted["warnings"]},
    )
    semantic = enrichment = None
    if payload.get("semantic", True):
        try:
            planned = yield context.call_activity("kecore_semantic_plan", payload)
            fiches = pipeline.batches(planned["fiches"], payload["batch_size"])
            cards = yield from _fan_out(context, "kecore_cards", payload, fiches)
            exams = yield from _fan_out(context, "kecore_heldout", payload, fiches)
            built = yield context.call_activity("kecore_semantic_index", payload)
            calibrated = yield context.call_activity("kecore_calibrate", payload)
            semantic = {
                "fiches": planned["fiches"],
                "cards": {k: sum(c[k] for c in cards) for k in ("questions", "dropped", "errors", "calls", "cached")},
                "exam": {k: sum(e[k] for e in exams) for k in ("queries", "dropped", "errors", "calls", "cached")},
                "index": built,
                "calibration": calibrated,
            }
        except Exception as exc:  # the run is still published: /find then decides by words, and says why
            semantic = {"error": str(exc)[:500]}
    if payload.get("enrich", True):
        # pass A of the enrichment (enrichment_service.py): proposals only, nothing routes on them yet
        try:
            planned = yield context.call_activity("kecore_semantic_plan", payload)
            fiches = pipeline.batches(planned["fiches"], payload["batch_size"])
            parts = yield from _fan_out(context, "kecore_enrich", payload, fiches)
            enrichment = yield context.call_activity("kecore_enrich_summary", payload)
            enrichment["llm"] = {k: sum(part[k] for part in parts) for k in ("calls", "cached")}
        except Exception as exc:  # the run is still published, and says why
            enrichment = {"error": str(exc)[:500]}
    extra = {}
    if "routing" in payload:  # a key older inputs lack: their publish keeps the summary their history holds
        routing = None
        if payload["routing"] is True:
            # the routing tables of the NEW run, before it is published: built on runs/<run>/ directly, so the
            # moment latest.json names this run, tables (and a carried-over activation) are there for it
            try:
                routing = yield from _routing_steps(context, {**payload, "build_id": payload["routing_build_id"],
                                                              "measure": False, "kind": "kecore"})
            except Exception as exc:  # the run is still published: /find answers without the new tables
                routing = {"error": str(exc)[:500]}
        extra["routing"] = routing
    summary = yield context.call_activity(
        "kecore_publish", {**payload, "summary": {**summary, "enrichment": enrichment, **extra}, "semantic": semantic})
    if payload.get("catalog") is True:
        # the fiches the labeling tab offers follow the published map (fix #4): whatever latest.json names
        # now (run_id None), so two runs racing never leave the older map's catalog behind
        try:
            catalog = yield context.call_activity("kecore_catalog", payload)
        except Exception as exc:  # the run is published; only the catalog is late (a tickets run refreshes it)
            catalog = {"error": str(exc)[:500]}
        summary = {**summary, "catalog": catalog}
    return summary


def _routing_steps(context, payload: dict):
    """One routing run (``yield from``): prepare (the lease, which passes are needed) -> pass B while no
    taxonomy is frozen -> pass C (``_fan_out``) when the frozen taxonomy or the run changed -> the tables,
    ALWAYS -> their measure when ``payload["measure"]`` (the scoreboard, sb_id "<build>-sb"). ``routing_finish``
    is called whatever happens (an exception included): the build's status, the lease released. Returns a
    short summary; nothing here reads the clock (orchestrator code is replayed)."""
    out = {"build_id": payload["build_id"], "run_id": payload.get("run_id"), "state": "failed", "error": None,
           "taxonomy": None, "assign": None, "tables": None, "carried": False, "why_not": None, "candidate": False}
    try:
        prepared = yield context.call_activity("routing_prepare", payload)
        out["run_id"] = prepared.get("run_id")
        if prepared.get("error"):
            out["error"] = prepared["error"]
        else:
            payload = {**payload, "run_id": prepared["run_id"]}  # every step reads the same map
            if prepared["needs_b"]:
                out["taxonomy"] = yield context.call_activity("routing_taxonomy", payload)
            elif prepared.get("skipped_b"):
                out["taxonomy"] = {"skipped": prepared["skipped_b"]}
            if prepared["needs_c"]:
                ranges = pipeline.batches(prepared["fiches"], payload["batch_size"])
                parts = yield from _fan_out(context, "routing_assign", payload, ranges)
                assigned = yield context.call_activity("routing_assign_summary", payload)
                out["assign"] = {**assigned, "llm": {k: sum(part[k] for part in parts) for k in ("calls", "cached")}}
            built = yield context.call_activity("routing_build", payload)
            out.update(tables=built["tables"], carried=built["carried"], why_not=built["why_not"],
                       candidate=built["candidate"], state="completed")
            if payload.get("measure"):
                out["scoreboard"] = yield from _scoreboard_steps(context, {
                    "client": payload["client"], "run_id": payload["run_id"], "interpret": True,
                    "max_wrong": sb_svc.DEFAULT_MAX_WRONG, "sb_id": f"{payload['build_id']}-sb",
                    "llm_parallelism": payload.get("llm_parallelism", pipeline.DEFAULT_LLM_PARALLELISM)})
    except Exception as exc:
        out.update(state="failed", error=str(exc)[:500])
    yield context.call_activity("routing_finish", {**payload, "outcome": {
        "state": out["state"], "error": out["error"], "run_id": out["run_id"]}})
    return out


@app.orchestration_trigger(context_name="context")
def routing_run(context: df.DurableOrchestrationContext):
    return (yield from _routing_steps(context, context.get_input()))


@app.activity_trigger(input_name="payload")
def kecore_extract(payload: dict) -> dict:
    return pipeline.extract(storage(), payload)


@app.activity_trigger(input_name="payload")
def kecore_profile(payload: dict) -> dict:
    from kecore_table import PENDING

    # the decisions of the dictionary review tab join the run (an unreadable table fails the run:
    # a rejected name must never come back silently)
    decided = dictionary_svc.decisions(table(PENDING), payload["client"]) if payload.get("with_dictionary", True) else None
    return pipeline.profile(storage(), payload, llm=make_llm(payload), decided=decided)


@app.activity_trigger(input_name="payload")
def kecore_decompose(payload: dict) -> dict:
    return pipeline.decompose(storage(), payload, payload["start"], payload["end"], llm=make_llm(payload))


@app.activity_trigger(input_name="payload")
def kecore_report(payload: dict) -> dict:
    return pipeline.report(
        storage(), payload, payload["ranges"], payload["profile_stats"], payload["batch_stats"],
        payload["warnings"], model_id(),
    )


@app.activity_trigger(input_name="payload")
def kecore_semantic_plan(payload: dict) -> dict:
    return semantic_svc.plan(storage(), payload)


@app.activity_trigger(input_name="payload")
def kecore_cards(payload: dict) -> dict:
    return semantic_svc.cards(storage(), payload, payload["start"], payload["end"], llm=make_llm(payload))


@app.activity_trigger(input_name="payload")
def kecore_heldout(payload: dict) -> dict:
    return semantic_svc.heldout(storage(), payload, payload["start"], payload["end"], llm=make_llm(payload))


@app.activity_trigger(input_name="payload")
def kecore_enrich(payload: dict) -> dict:
    return enrichment_svc.enrich(storage(), payload, payload["start"], payload["end"], llm=make_llm(payload))


@app.activity_trigger(input_name="payload")
def kecore_enrich_summary(payload: dict) -> dict:
    return enrichment_svc.summarize(storage(), payload)


@app.activity_trigger(input_name="payload")
def kecore_semantic_index(payload: dict) -> dict:
    return semantic_svc.build_index(storage(), payload, make_build_embedder(payload))


@app.activity_trigger(input_name="payload")
def kecore_calibrate(payload: dict) -> dict:
    return semantic_svc.calibrate_run(storage(), payload, make_build_embedder(payload))


@app.activity_trigger(input_name="payload")
def kecore_publish(payload: dict) -> dict:
    return pipeline.publish(storage(), payload, payload["summary"], payload.get("semantic"))


@app.activity_trigger(input_name="payload")
def kecore_catalog(payload: dict) -> dict:
    from kecore_table import FICHES

    return tickets_svc.catalog(storage(), table(FICHES), {"client": payload["client"], "run_id": None})


@app.activity_trigger(input_name="payload")
def routing_prepare(payload: dict) -> dict:
    return routing_svc.prepare(storage(), payload, _now())


@app.activity_trigger(input_name="payload")
def routing_taxonomy(payload: dict) -> dict:
    return routing_svc.propose_taxonomy(storage(), payload, taxonomy_llm(payload))


@app.activity_trigger(input_name="payload")
def routing_assign(payload: dict) -> dict:
    return routing_svc.assign(storage(), payload, payload["start"], payload["end"], assign_llm(payload))


@app.activity_trigger(input_name="payload")
def routing_assign_summary(payload: dict) -> dict:
    return routing_svc.assign_summary(storage(), payload)


@app.activity_trigger(input_name="payload")
def routing_build(payload: dict) -> dict:
    from kecore_table import REVIEW

    # every stored decision of the client (stale ones too: their previous aliases still block)
    return routing_svc.build(storage(), table(REVIEW).list(payload["client"]), payload, _now())


@app.activity_trigger(input_name="payload")
def routing_finish(payload: dict) -> dict:
    return routing_svc.finish(storage(), payload, payload.get("outcome") or {}, _now())


_chat: dict = {}


def find_llm(client: str, live: bool = True) -> RecordingLLM:
    """The model that interprets tickets, behind the client's record (kecore-<client>/find-cache/). ``live``:
    the short budget of a /kecore/find answer; False for the tickets run and the scoreboard (the batch budget).
    The record key is the same either way ("<deployment>@<host>"): one question, one recorded answer."""
    if live not in _chat:
        _chat[live] = AzureOpenAIChat.from_config({**llm_config(), **(LIVE_BUDGET if live else BATCH_BUDGET)})
    return RecordingLLM(_chat[live], mode="record", store=pipeline.StorageRecordStore(storage(), client, prefix="find-cache/"))


# L2 maps a question onto the frozen taxonomy: two short strings, so a small max_tokens (Azure counts it
# against the shared quota on every call) and, live, a short budget: a slow or failed mapping gives the
# question to L3 at once ("l2_failed"), it never holds the Diagnostic's 30 s.
L2_MAX_TOKENS = 150
L2_LIVE_BUDGET = {"timeout_s": 8.0, "retries": 0, "throttle_retries": 0, "max_tokens": L2_MAX_TOKENS}
L2_BATCH_BUDGET = {**BATCH_BUDGET, "max_tokens": L2_MAX_TOKENS}
_intent_chat: dict = {}


def intent_llm(client: str, live: bool = True) -> RecordingLLM:
    """The model of the L2 mapping (kefind.route.map_question), behind the client's find-cache/ record and on
    the same deployment as ``find_llm``: one recorded mapping per question, shared by live /find and the
    scoreboard (the budget is not in the record key). ``live``: the short /find budget; False: the batch one."""
    if live not in _intent_chat:
        _intent_chat[live] = AzureOpenAIChat.from_config({**llm_config(), **(L2_LIVE_BUDGET if live else L2_BATCH_BUDGET)})
    return RecordingLLM(_intent_chat[live], mode="record",
                        store=pipeline.StorageRecordStore(storage(), client, prefix="find-cache/"))


class _UnavailableModel:
    """A model client that could not be built (a missing setting...): every call fails the way the build
    did. For the L2 mapping that is an L2 failure like any other -- the question goes on to L3, marked
    degraded "l2_failed:<Exc>", and a measure holding one is invalid -- never L2 silently skipped."""

    def __init__(self, exc: Exception):
        self.exc = exc

    def complete_json(self, *args, **kwargs):
        raise self.exc.with_traceback(None)


def _model_or_unavailable(factory, *args, **kwargs):
    try:
        return factory(*args, **kwargs)
    except Exception as exc:
        return _UnavailableModel(exc)


_embeddings: dict = {}


def _embedding_client(live: bool = False) -> AzureOpenAIEmbeddings:
    if live not in _embeddings:
        _embeddings[live] = AzureOpenAIEmbeddings.from_config({**embedding_config(), **(LIVE_BUDGET if live else BATCH_BUDGET)})
    return _embeddings[live]


def make_build_embedder(payload: dict) -> RecordingEmbeddings:
    """The vectors of a run's index and exam, behind the client's record (kecore-<client>/embed-cache/):
    a run in mode "replay" reads them all and calls nothing."""
    return RecordingEmbeddings(_embedding_client(), mode=payload["mode"], dimensions=semantic_svc.DIMENSIONS,
                               store=pipeline.StorageRecordStore(storage(), payload["client"],
                                                                 prefix=semantic_svc.EMBED_RECORD_PREFIX))


def query_embedder(client: str, dimensions: int, live: bool = True):
    """A question's vector, behind the client's record (kecore-<client>/find-cache/): the first vector
    recorded for a text is the one every later call gets. None when the deployment is not configured
    or its client cannot be built: /find then decides by words and says so (mode "degraded")."""
    try:
        return RecordingEmbeddings(_embedding_client(live), mode="record", dimensions=dimensions,
                                   store=pipeline.StorageRecordStore(storage(), client, prefix="find-cache/"))
    except Exception:
        return None


def _query_embedder_for_run(payload: dict):
    """The tickets run and the scoreboard decide each ticket exactly as /kecore/find does: by meaning
    when the map has an index (same record, find-cache/, so a ticket asked live and replayed in a
    measurement gets one vector)."""
    return query_embedder(payload["client"], semantic_svc.DIMENSIONS, live=False)


@functools.lru_cache(maxsize=4)
def _published_map(client: str, run_id: str):
    # a published run's folder never changes again: its map can be kept as long as the process lives
    return finder.load_map(storage(), client, run_id)


def _kb_map(client: str, run_id: str, latest: str | None = None):
    """The map of ``run_id``. Kept in memory only once the run is published (latest.json names it, or
    its folder has published.json): a run still being built -- its semantic index not there yet -- is
    read fresh on every call, so no instance ever freezes a half-built map while another serves the
    finished one."""
    if latest is None:
        latest = finder.latest_run(storage(), client)
    container = pipeline.kecore_container(client)
    if run_id == latest or storage().read(container, pipeline.layout(run_id)["published"]) is not None:
        return _published_map(client, run_id)
    return finder.load_map(storage(), client, run_id)


_kb_map.cache_clear = _published_map.cache_clear


_CONFIG_TTL_S = 60
_configs: dict[str, tuple[float, object]] = {}


def _funnel_config(client: str):
    """The client's calibrated settings, re-read at most once a minute (an applied floor reaches
    every instance within that minute)."""
    cached = _configs.get(client)
    if cached is None or time.monotonic() - cached[0] > _CONFIG_TTL_S:
        cached = (time.monotonic(), finder.funnel_config(storage(), client))
        _configs[client] = cached
    return cached[1]


@app.route(route="kecore/find", methods=["POST"])
def kecore_find(req: func.HttpRequest) -> func.HttpResponse:
    body = _body(req)
    if body is None:
        return _error(400, "a JSON body is expected")
    try:
        payload = finder.validate_find_request(body, allowed_clients())
    except ValueError as exc:
        return _error(400, str(exc))
    latest = finder.latest_run(storage(), payload["client"])
    run_id = payload["run_id"] or latest
    if not run_id:
        return _error(404, "no KB map for this client yet: start a run with POST /api/kecore/runs")
    try:
        kb_map = _kb_map(payload["client"], run_id, latest)
    except FileNotFoundError:
        return _error(404, "no KB map for this run (unknown or unfinished run)")
    # the ticket text is neither logged nor stored; only the model's search terms are recorded
    llm = find_llm(payload["client"]) if payload["interpret"] else None
    embedder = query_embedder(payload["client"], kb_map.semantic.dimensions) if kb_map.semantic is not None else None
    # routing: only tables a person activated for this very run, unless killed; None -> today's answer exactly
    routing, current, routing_error = _active_routing(payload["client"], run_id, kb_map)
    mapping = _model_or_unavailable(intent_llm, payload["client"]) if routing is not None and routing.l2 else None
    answer = finder.respond(kb_map, payload, config=_funnel_config(payload["client"]), llm=llm, embedder=embedder,
                            routing=routing, routing_error=routing_error, current=current, intent_llm=mapping)
    if payload["observe"]:  # the dictionary's online loop (only names, never the text); never fails the answer
        try:
            from kecore_table import PENDING

            dictionary_svc.observe(table(PENDING), payload["client"], payload["text"], kb_map.dictionary,
                                   payload["observe"])
        except Exception:
            pass
    return _json(answer)


@app.route(route="kecore/fiche", methods=["GET"])
def kecore_fiche(req: func.HttpRequest) -> func.HttpResponse:
    try:
        payload = finder.validate_fiche_request(dict(req.params), allowed_clients())
    except ValueError as exc:
        return _error(400, str(exc))
    latest = finder.latest_run(storage(), payload["client"])
    run_id = payload["run_id"] or latest
    if not run_id:
        return _error(404, "no KB map for this client yet")
    try:
        kb_map = _kb_map(payload["client"], run_id, latest)
    except FileNotFoundError:
        return _error(404, "no KB map for this run")
    view = finder.fiche_payload(kb_map, payload["fiche_id"])
    if view is None:
        return _error(404, "unknown fiche")
    return _json(view)


@app.route(route="kecore/dictionary", methods=["GET"])
def kecore_dictionary(req: func.HttpRequest) -> func.HttpResponse:
    from kecore_table import PENDING

    client = req.params.get("client")
    if client not in set(allowed_clients()):
        return _error(400, "unknown client")
    run_id = finder.latest_run(storage(), client)
    dictionary = _kb_map(client, run_id, run_id).dictionary if run_id else {}
    try:
        return _json(dictionary_svc.review(storage(), table(PENDING), client, dictionary, run_id))
    except ValueError as exc:  # the hand-written dictionary-decisions.json is invalid
        return _error(500, str(exc))


@app.route(route="kecore/dictionary/decision", methods=["POST"])
def kecore_dictionary_decision(req: func.HttpRequest) -> func.HttpResponse:
    from kecore_table import PENDING

    body = _body(req)
    if body is None:
        return _error(400, "a JSON body is expected")
    try:
        payload = dictionary_svc.validate_decision(body, allowed_clients())
    except ValueError as exc:
        return _error(400, str(exc))
    try:
        return _json(dictionary_svc.decide(table(PENDING), payload))
    except FileNotFoundError as exc:  # never observed, or not seen in enough sessions yet
        return _error(404, str(exc))
    except ValueError as exc:  # already decided
        return _error(409, str(exc))


# --------------------------------------------------------------------------- slice 4: tickets


@app.route(route="kecore/tickets/scrub", methods=["POST"])
def kecore_tickets_scrub(req: func.HttpRequest) -> func.HttpResponse:
    body = _body(req)
    if body is None:
        return _error(400, "a JSON body is expected")
    try:
        payload = tickets_svc.validate_tickets_request(body, allowed_clients())
    except ValueError as exc:
        return _error(400, str(exc))
    return _json(tickets_svc.scrub(storage(), table(), payload["client"]))


@app.route(route="kecore/tickets/rescrub", methods=["POST"])
def kecore_tickets_rescrub(req: func.HttpRequest) -> func.HttpResponse:
    body = _body(req)
    if body is None:
        return _error(400, "a JSON body is expected")
    try:
        payload = tickets_svc.validate_tickets_request(body, allowed_clients())
    except ValueError as exc:
        return _error(400, str(exc))
    return _json(tickets_svc.rescrub(table(), payload["client"]))


@app.route(route="kecore/tickets/runs", methods=["POST"])
@app.durable_client_input(client_name="client")
async def kecore_tickets_start(req: func.HttpRequest, client) -> func.HttpResponse:
    body = _body(req)
    if body is None:
        return _error(400, "a JSON body is expected")
    try:
        payload = tickets_svc.validate_run_request(body, allowed_clients())
    except ValueError as exc:
        return _error(400, str(exc))
    instance_id = await client.start_new("tickets_run", client_input=payload)
    return client.create_check_status_response(req, instance_id)


@app.orchestration_trigger(context_name="context")
def tickets_run(context: df.DurableOrchestrationContext):
    payload = context.get_input()
    count = yield context.call_activity("tickets_count", payload)
    if not count:
        return {"client": payload["client"], "tickets": 0, "error": "no scrubbed tickets for this client yet"}
    catalogued = yield context.call_activity("tickets_catalog", payload)
    payload = {**payload, "run_id": catalogued["run_id"]}  # every batch reads the same map
    ranges = pipeline.batches(count, tickets_svc.RUN_BATCH_SIZE)
    parts = yield context.task_all(
        [context.call_activity("tickets_run_batch", {**payload, "start": start, "end": end}) for start, end in ranges]
    )
    return {**tickets_svc.merge_runs(payload["client"], parts), "catalog": catalogued}


@app.activity_trigger(input_name="payload")
def tickets_count(payload: dict) -> int:
    return tickets_svc.ticket_count(table(), payload["client"], payload["limit"])


@app.activity_trigger(input_name="payload")
def tickets_catalog(payload: dict) -> dict:
    from kecore_table import FICHES

    return tickets_svc.catalog(storage(), table(FICHES), payload)


@app.activity_trigger(input_name="payload")
def tickets_run_batch(payload: dict) -> dict:
    # the ticket text is neither logged nor stored elsewhere; only the model's search terms are
    # recorded, in the same per-client cache kecore/find already uses (find-cache/)
    llm = find_llm(payload["client"], live=False) if payload["interpret"] else None
    return tickets_svc.run_batch(storage(), table(), payload, payload["start"], payload["end"], llm=llm,
                                 embedder=_query_embedder_for_run(payload))


# --------------------------------------------------------------------------- slice 4: scoreboard


@app.route(route="kecore/scoreboard/runs", methods=["POST"])
@app.durable_client_input(client_name="client")
async def kecore_scoreboard_start(req: func.HttpRequest, client) -> func.HttpResponse:
    body = _body(req)
    if body is None:
        return _error(400, "a JSON body is expected")
    try:
        payload = sb_svc.validate_scoreboard_request(body, allowed_clients(), _new_id())
    except ValueError as exc:
        return _error(400, str(exc))
    instance_id = await client.start_new("scoreboard_run", client_input=payload)
    return client.create_check_status_response(req, instance_id)


@app.orchestration_trigger(context_name="context")
def scoreboard_run(context: df.DurableOrchestrationContext):
    return (yield from _scoreboard_steps(context, context.get_input()))


def _scoreboard_steps(context, payload: dict):
    """One scoreboard run (``yield from``): prepare (the frozen set, the candidate routing tables pinned) ->
    every batch -> report (and the routing gate). Shared by ``scoreboard_run`` and the measure that ends a
    routing run (``_routing_steps``). Batches go ``llm_parallelism`` at a time through ``_fan_out`` (they call
    the shared deployment); an input from before that key keeps the shape it was scheduled with, all at once.
    Every batch gets the pinned tables (``"routing"``: None when there are none), never re-reads the pointer."""
    prepared = yield context.call_activity("scoreboard_prepare", payload)
    if not prepared["count"]:
        return {"client": payload["client"], "sb_id": payload["sb_id"], "error": "no labeled ticket or reference question yet",
                "prepared": prepared}
    payload = {**payload, "kb_run_id": prepared["kb_run_id"], "routing": prepared.get("routing")}
    ranges = pipeline.batches(prepared["count"], sb_svc.BATCH_SIZE)
    parts = yield from _fan_out(context, "scoreboard_batch", payload, ranges)
    summary = yield context.call_activity("scoreboard_report",
                                          {**payload, "ranges": ranges, "prepared": prepared, "batches": parts})
    return summary


@app.activity_trigger(input_name="payload")
def scoreboard_prepare(payload: dict) -> dict:
    from kecore_table import LABELS, REFERENCES

    return sb_svc.prepare(storage(), table(), table(LABELS), payload, refs_table=table(REFERENCES))


@app.activity_trigger(input_name="payload")
def scoreboard_batch(payload: dict) -> dict:
    llm = find_llm(payload["client"], live=False) if payload["interpret"] else None
    # the routed engine maps questions for L2 as live /find does (same record, find-cache/), on the batch budget;
    # built only when tables are pinned: a measure without routing makes no call it did not make before
    mapping = _model_or_unavailable(intent_llm, payload["client"], live=False) if payload.get("routing") else None
    return sb_svc.batch(storage(), payload, payload["start"], payload["end"], llm=llm,
                        embedder=_query_embedder_for_run(payload), intent_llm=mapping)


@app.activity_trigger(input_name="payload")
def scoreboard_report(payload: dict) -> dict:
    from kecore_table import SCORES

    return sb_svc.report(storage(), table(SCORES), payload, payload["ranges"], payload["prepared"],
                         payload.get("batches"))


BUILD_COMMIT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "BUILD_COMMIT")


@app.route(route="kecore/version", methods=["GET"])
def kecore_version(req: func.HttpRequest) -> func.HttpResponse:
    """The git commit this code was packaged from (BUILD_COMMIT, written by scripts/deploy-kecore-function.ps1):
    the deploy script waits until the host answers with the commit it just shipped, never the previous host's
    list of functions (runbook 19.17-19.18)."""
    try:
        with open(BUILD_COMMIT, encoding="utf-8") as f:
            commit = f.read().strip() or "unknown"
    except OSError:
        commit = "unknown"
    return _json({"commit": commit})


@app.route(route="kecore/scoreboard/latest", methods=["GET"])
def kecore_scoreboard_latest(req: func.HttpRequest) -> func.HttpResponse:
    client = req.params.get("client")
    if client not in set(allowed_clients()):
        return _error(400, "unknown client")
    latest = sb_svc.latest(storage(), client)
    if latest is None:
        return _error(404, "no scoreboard run for this client yet: POST /api/kecore/scoreboard/runs")
    return _json(latest)


@app.route(route="kecore/funnel-config/apply", methods=["POST"])
def kecore_funnel_config_apply(req: func.HttpRequest) -> func.HttpResponse:
    body = _body(req)
    if body is None:
        return _error(400, "a JSON body is expected")
    try:
        payload = sb_svc.validate_apply_request(body, allowed_clients())
    except ValueError as exc:
        return _error(400, str(exc))
    try:
        result = sb_svc.apply(storage(), payload)
    except FileNotFoundError as exc:
        return _error(404, str(exc))
    except ValueError as exc:  # no recommendation, or not confirmed on half B: refused, nothing written
        return _error(409, str(exc))
    _configs.pop(payload["client"], None)  # this instance at once; the others within a minute
    return _json(result)


# --------------------------------------------------------------------------- routing: tables and review


# The active routing pointer, as /kecore/find reads it (at most once a minute per client and instance, like
# the funnel config): an activation or a deactivation drops it on the instance that handled it at once,
# every other instance within the minute. Per client: (monotonic time read, {run_id: (tables, current,
# error)}) -- a session pinned to an older run resolves its own entry (the carried-over "previous" one).
_ROUTING_TTL_S = 60
_routing_pointers: dict[str, tuple[float, object]] = {}
# The tables themselves never change under a sha256 (load_tables verifies it): kept by (client, sha256), so a
# pointer re-read costs two small blobs, not the tables. The run's source hashes (kb_sha256's input) likewise
# by (client, run): a run's fiches.jsonl is written once, at extraction.
_MAX_ROUTING_CACHE = 8
_routing_tables: dict[tuple[str, str], object] = {}
_run_sources: dict[tuple[str, str], dict] = {}


def _forget_routing(client: str) -> None:
    _routing_pointers.pop(client, None)


def _remember(cache: dict, key, value):
    if key not in cache and len(cache) >= _MAX_ROUTING_CACHE:
        cache.pop(next(iter(cache)))
    cache[key] = value
    return value


def _resolve_routing(client: str, run_id: str, kb_map) -> tuple[object, dict | None, str | None]:
    """The active tables that apply to ``run_id``, loaded and checked against the map they will route:
    (tables, current, None), (None, None, None) when routing was never activated, (None, None, why) when it
    is switched off, built for another run, or its tables do not load, verify or match this map."""
    entry, why = routing_svc.active_for(storage(), client, run_id)
    if entry is None:
        return None, None, why
    sha = entry.get("tables_sha256")
    if not isinstance(sha, str) or not sha:
        return None, None, "routing/active.json names no tables_sha256: activate the tables again"
    try:
        tables = _routing_tables.get((client, sha))
        if tables is None:
            tables = _remember(_routing_tables, (client, sha),
                               routing_svc.load_tables(storage(), client, entry.get("build_id"), sha))
        current = _run_sources.get((client, run_id))
        if current is None:
            current = _remember(_run_sources, (client, run_id), routing_svc.source_hashes(storage(), client, run_id))
        check_tables(tables, kb_map, current)  # client, run, code version, knowledge base (kb_sha256)
    except (ValueError, FileNotFoundError, routing_svc.StoredFileError) as exc:  # TablesMismatchError included
        return None, None, str(exc)[:500]
    return tables, current, None


def _active_routing(client: str, run_id: str, kb_map) -> tuple[object, dict | None, str | None]:
    """(tables, current source hashes, routing_error) for /kecore/find. Never raises: routing is optional, and
    anything wrong with it gives (None, None, why) -- today's answer, with ``routing_error`` saying why.
    The kill switch and the pointer are re-read at most once a minute per client (``_ROUTING_TTL_S``),
    at once on the instance that activated or deactivated (``_forget_routing``)."""
    try:
        now = time.monotonic()
        cached = _routing_pointers.get(client)
        if (not isinstance(cached, tuple) or len(cached) != 2 or not isinstance(cached[1], dict)
                or now - cached[0] > _ROUTING_TTL_S):
            cached = (now, {})
            _routing_pointers[client] = cached
        resolved = cached[1]
        if run_id not in resolved:
            resolved[run_id] = _resolve_routing(client, run_id, kb_map)
        return resolved[run_id]
    except Exception as exc:  # storage down, a bug: /find still answers, without routing
        return None, None, f"routing unavailable ({type(exc).__name__}: {str(exc)[:300]})"


def _client_param(req: func.HttpRequest) -> str | None:
    client = req.params.get("client")
    return client if isinstance(client, str) and client in set(allowed_clients()) else None


def _body_client(body) -> str | None:
    client = body.get("client") if isinstance(body, dict) else None
    return client if isinstance(client, str) and client in set(allowed_clients()) else None


def _review_map(client: str):
    """The map of the run /find serves (latest.json): the review is always about it. None before any run."""
    run_id = finder.latest_run(storage(), client)
    if not run_id:
        return None
    try:
        return _kb_map(client, run_id, run_id)
    except FileNotFoundError:
        return None


_NO_MAP = "no KB map for this client yet: start a run with POST /api/kecore/runs"


@app.route(route="kecore/routing/runs", methods=["POST"])
@app.durable_client_input(client_name="client")
async def kecore_routing_start(req: func.HttpRequest, client) -> func.HttpResponse:
    body = _body(req)
    if body is None:
        return _error(400, "a JSON body is expected")
    try:
        payload = routing_svc.validate_routing_request(body, allowed_clients(), _new_id())
    except ValueError as exc:
        return _error(400, str(exc))
    held = routing_svc.lease(storage(), payload["client"], _now())
    if held is not None:  # checked again by routing_prepare: two requests racing here still make one run
        return _error(409, f"a routing run is in progress ({held.get('build_id')}, since {held.get('started_at')})")
    instance_id = await client.start_new("routing_run", instance_id=payload["build_id"], client_input=payload)
    return client.create_check_status_response(req, instance_id)


@app.route(route="kecore/routing", methods=["GET"])
def kecore_routing_status(req: func.HttpRequest) -> func.HttpResponse:
    client = _client_param(req)
    if client is None:
        return _error(400, "unknown client")
    try:
        return _json(routing_svc.status(storage(), client, _now()))
    except routing_svc.StoredFileError as exc:
        return _error(500, str(exc))


@app.route(route="kecore/routing/activate", methods=["POST"])
def kecore_routing_activate(req: func.HttpRequest) -> func.HttpResponse:
    body = _body(req)
    if body is None:
        return _error(400, "a JSON body is expected")
    try:
        payload = routing_svc.validate_activate_request(body, allowed_clients())
    except ValueError as exc:
        return _error(400, str(exc))
    try:
        pointer = routing_svc.activate(storage(), payload, _now())
    except ValueError as exc:  # not on the served run, never measured, measure not passed: refused, nothing written
        return _error(409, str(exc))
    except routing_svc.StoredFileError as exc:
        return _error(500, str(exc))
    _forget_routing(payload["client"])  # this instance at once; the others within a minute
    return _json({"active": pointer})


@app.route(route="kecore/routing/deactivate", methods=["POST"])
def kecore_routing_deactivate(req: func.HttpRequest) -> func.HttpResponse:
    body = _body(req)
    if body is None:
        return _error(400, "a JSON body is expected")
    try:
        payload = routing_svc.validate_deactivate_request(body, allowed_clients())
    except ValueError as exc:
        return _error(400, str(exc))
    try:
        result = routing_svc.deactivate(storage(), payload, _now())
    except Exception as exc:  # the switch may already be written: this instance stops routing at once anyway
        return _error(500, f"deactivation incomplete ({type(exc).__name__}): send it again")
    finally:
        _forget_routing(payload["client"])
    return _json(result)


@app.route(route="kecore/review", methods=["GET"])
def kecore_review(req: func.HttpRequest) -> func.HttpResponse:
    from kecore_table import REFERENCES, REVIEW

    client = _client_param(req)
    if client is None:
        return _error(400, "unknown client")
    kb_map = _review_map(client)
    if kb_map is None:
        return _error(404, _NO_MAP)
    try:  # an unknown status filter is ignored, never refused: the counts stay global either way
        return _json(routing_svc.review_overview(storage(), table(REVIEW).list(client), table(REFERENCES).list(client),
                                                 client, kb_map, status_filter=req.params.get("status") or None,
                                                 now=_now()))
    except (routing_svc.StoredFileError, FileNotFoundError) as exc:
        return _error(500, str(exc))


@app.route(route="kecore/review/fiche", methods=["GET"])
def kecore_review_fiche(req: func.HttpRequest) -> func.HttpResponse:
    from kecore_table import REFERENCES, REVIEW

    client = _client_param(req)
    if client is None:
        return _error(400, "unknown client")
    key = req.params.get("key") or ""
    if not routing_svc.is_key(key):
        return _error(400, "key: 32 lowercase hexadecimal characters")
    kb_map = _review_map(client)
    if kb_map is None:
        return _error(404, _NO_MAP)
    try:
        return _json(routing_svc.review_fiche(storage(), table(REVIEW).list(client), table(REFERENCES).list(client),
                                              client, kb_map, key, now=_now()))
    except routing_svc.UnknownFicheError as exc:
        return _error(404, str(exc))
    except (routing_svc.StoredFileError, FileNotFoundError) as exc:
        return _error(500, str(exc))


@app.route(route="kecore/review/next", methods=["GET"])
def kecore_review_next(req: func.HttpRequest) -> func.HttpResponse:
    from kecore_table import REFERENCES, REVIEW

    client = _client_param(req)
    if client is None:
        return _error(400, "unknown client")
    status = req.params.get("status") or ""
    if status not in routing_svc.QUEUES:
        return _error(400, "status: proposed or stale")
    kb_map = _review_map(client)
    if kb_map is None:
        return _error(404, _NO_MAP)
    try:
        return _json({"key": routing_svc.next_key(storage(), table(REVIEW).list(client), table(REFERENCES).list(client),
                                                  client, kb_map, status)})
    except (routing_svc.StoredFileError, FileNotFoundError) as exc:
        return _error(500, str(exc))


@app.route(route="kecore/review/decision", methods=["POST"])
def kecore_review_decision(req: func.HttpRequest) -> func.HttpResponse:
    from kecore_table import REFERENCES, REVIEW

    body = _body(req)
    if not isinstance(body, dict):
        return _error(400, "a JSON body is expected")
    client = _body_client(body)
    if client is None:
        return _error(400, "unknown client")
    kb_map = _review_map(client)
    if kb_map is None:
        return _error(404, _NO_MAP)
    try:
        return _json(routing_svc.decide(table(REVIEW), storage(), client, body, kb_map, table(REFERENCES).list(client),
                                        _now()))
    except routing_svc.UnknownFicheError as exc:
        return _error(404, str(exc))
    except routing_svc.ChangedError as exc:  # the text or the decision changed since the reviewer saw it
        return _error(409, str(exc))
    except ValueError as exc:  # French: shown to the reviewer as it is
        return _error(400, str(exc))
    except (routing_svc.StoredFileError, FileNotFoundError) as exc:
        return _error(500, str(exc))


@app.route(route="kecore/review/carry", methods=["POST"])
def kecore_review_carry(req: func.HttpRequest) -> func.HttpResponse:
    from kecore_table import REFERENCES, REVIEW

    body = _body(req)
    if body is None:
        return _error(400, "a JSON body is expected")
    try:
        payload = routing_svc.validate_carry_request(body, allowed_clients())
    except ValueError as exc:
        return _error(400, str(exc))
    client = payload["client"]
    kb_map = _review_map(client)
    if kb_map is None:
        return _error(404, _NO_MAP)
    try:
        return _json(routing_svc.carry_forward(table(REVIEW), storage(), client, payload, kb_map,
                                               table(REFERENCES).list(client), _now()))
    except (routing_svc.StoredFileError, FileNotFoundError) as exc:
        return _error(500, str(exc))


@app.route(route="kecore/review/taxonomy", methods=["POST"])
def kecore_review_taxonomy(req: func.HttpRequest) -> func.HttpResponse:
    from kecore_table import REVIEW

    body = _body(req)
    if not isinstance(body, dict):
        return _error(400, "a JSON body is expected")
    client = _body_client(body)
    if client is None:
        return _error(400, "unknown client")
    try:
        return _json(routing_svc.freeze_taxonomy(storage(), table(REVIEW).list(client), body, _now(),
                                                 kbmap=_review_map(client)))
    except routing_svc.OrphansError as exc:  # validated fiches would lose their intent: confirm with orphans_ok
        return _json({"error": str(exc), "orphans": exc.orphans}, 409)
    except routing_svc.ChangedError as exc:  # another version was frozen meanwhile
        return _error(409, str(exc))
    except ValueError as exc:  # French, naming the row: the freeze form shows it next to what was posted
        return _error(400, str(exc))
    except (routing_svc.StoredFileError, FileNotFoundError) as exc:
        return _error(500, str(exc))
