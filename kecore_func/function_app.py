"""fn-kecore — the V10 KB decomposition (slice 2) and the fiche finder (slice 3), run in Azure.

POST /api/kecore/runs  (function key)  {"client": "client-s", "source_prefix": "Kbs/",
                                         "mode": "replay" | "record", "batch_size": 10,
                                         "limit": null, "with_dictionary": true}
  -> starts one Durable Functions run and answers with its status URLs.

The run: extract -> profile -> decompose (batches in parallel, at most 4 at a time, see
host.json) -> report. Each step is an activity that reads from and writes to blob storage
(kecore_pipeline.py); kecore itself is unchanged. Identity: the Function's managed identity,
for blob storage and for Azure OpenAI alike. A client not listed in KECORE_CLIENTS is refused.

POST /api/kecore/find  (function key)  {"client": "client-s", "text": "<ticket>",
                                         "answers": ["app:teams"], "run_id": null, "interpret": true}
  -> the decision (fiche / question / abstain), its trace, and the fiche's verified steps.
     The decision is code (kefind_service.py, kefind.funnel). With "interpret" (default), one
     model call first turns the ticket into search terms (kefind.interpret), recorded under the
     hash of the request in kecore-<client>/find-cache/: the same ticket gets the same terms.
     The KB map of a run is read once and kept in memory (4 maps at most).

POST /api/kecore/tickets/scrub  (function key)  {"client": "client-s"}
  -> scrubs every raw export under tickets-<client>/raw/*.csv (kecore.tickets.scrub: personal
     columns dropped, e-mail/phone masked in free text), writes one Table row per ticket, and
     deletes the raw export. No label, no judgment.

POST /api/kecore/tickets/runs  (function key)  {"client": "client-s", "run_id": null,
                                                 "interpret": true, "limit": 200}
  -> one Durable run of every scrubbed ticket through kefind's funnel, unlabeled: tallies what
     the funnel actually does (fiche shown / question asked / abstain, and why) -- a first signal
     before any ticket is hand-labeled. No correctness judgment: that needs labels (scoreboard,
     later). Same batching shape as /kecore/runs.
"""

from __future__ import annotations

import functools
import json
import os
import urllib.parse
import uuid
from datetime import datetime, timezone

import azure.durable_functions as df
import azure.functions as func

import kecore_pipeline as pipeline
import kefind_service as finder
import tickets_service as tickets_svc
from kecore.llm import AzureOpenAIChat, RecordingLLM

app = df.DFApp(http_auth_level=func.AuthLevel.FUNCTION)

_storage = None


def storage():
    global _storage
    if _storage is None:
        from kecore_blob import BlobStorage

        _storage = BlobStorage()
    return _storage


_table = None


def table():
    global _table
    if _table is None:
        from kecore_table import TableStorage

        _table = TableStorage()
    return _table


def allowed_clients() -> list[str]:
    return [c.strip() for c in os.environ.get("KECORE_CLIENTS", "").split(",") if c.strip()]


def llm_config() -> dict:
    return {
        "endpoint": os.environ["KECORE_AOAI_ENDPOINT"],
        "deployment": os.environ["KECORE_AOAI_DEPLOYMENT"],
        "api_version": os.environ.get("KECORE_AOAI_API_VERSION", "2024-10-21"),
        "auth": "entra",
    }


def model_id() -> str:
    config = llm_config()
    return f"{config['deployment']}@{urllib.parse.urlparse(config['endpoint']).netloc}"


def make_llm(payload: dict) -> RecordingLLM:
    # Built even in replay mode: the record key holds "<deployment>@<host>", taken from the client.
    # In replay mode RecordingLLM never calls it; a missing answer is an error, never a model call.
    inner = AzureOpenAIChat.from_config(llm_config())
    return RecordingLLM(inner, mode=payload["mode"], store=pipeline.StorageRecordStore(storage(), payload["client"]))


def _error(status: int, message: str) -> func.HttpResponse:
    return func.HttpResponse(json.dumps({"error": message}), status_code=status, mimetype="application/json")


@app.route(route="kecore/runs", methods=["POST"])
@app.durable_client_input(client_name="client")
async def kecore_start(req: func.HttpRequest, client) -> func.HttpResponse:
    try:
        body = req.get_json()
    except ValueError:
        return _error(400, "a JSON body is expected")
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:6]
    try:
        payload = pipeline.validate_request(body, allowed_clients(), run_id)
    except ValueError as exc:
        return _error(400, str(exc))
    instance_id = await client.start_new("kecore_run", client_input=payload)
    return client.create_check_status_response(req, instance_id)


@app.orchestration_trigger(context_name="context")
def kecore_run(context: df.DurableOrchestrationContext):
    payload = context.get_input()
    extracted = yield context.call_activity("kecore_extract", payload)
    if not extracted["fiches"]:
        return {"run_id": payload["run_id"], "error": "no readable fiche under this prefix", "extract": extracted}
    profiled = yield context.call_activity("kecore_profile", payload)
    ranges = pipeline.batches(extracted["fiches"], payload["batch_size"])
    stats = yield context.task_all(
        [context.call_activity("kecore_decompose", {**payload, "start": start, "end": end}) for start, end in ranges]
    )
    summary = yield context.call_activity(
        "kecore_report",
        {**payload, "ranges": ranges, "profile_stats": profiled, "batch_stats": stats, "warnings": extracted["warnings"]},
    )
    return summary


@app.activity_trigger(input_name="payload")
def kecore_extract(payload: dict) -> dict:
    return pipeline.extract(storage(), payload)


@app.activity_trigger(input_name="payload")
def kecore_profile(payload: dict) -> dict:
    return pipeline.profile(storage(), payload, llm=make_llm(payload))


@app.activity_trigger(input_name="payload")
def kecore_decompose(payload: dict) -> dict:
    return pipeline.decompose(storage(), payload, payload["start"], payload["end"], llm=make_llm(payload))


@app.activity_trigger(input_name="payload")
def kecore_report(payload: dict) -> dict:
    return pipeline.report(
        storage(), payload, payload["ranges"], payload["profile_stats"], payload["batch_stats"],
        payload["warnings"], model_id(),
    )


_chat = None


def find_llm(client: str) -> RecordingLLM:
    """The model that interprets tickets, behind the client's record (kecore-<client>/find-cache/)."""
    global _chat
    if _chat is None:
        _chat = AzureOpenAIChat.from_config(llm_config())
    return RecordingLLM(_chat, mode="record", store=pipeline.StorageRecordStore(storage(), client, prefix="find-cache/"))


@functools.lru_cache(maxsize=4)
def _kb_map(client: str, run_id: str):
    # a run's folder never changes once written: its map can be kept as long as the process lives
    return finder.load_map(storage(), client, run_id)


@app.route(route="kecore/find", methods=["POST"])
def kecore_find(req: func.HttpRequest) -> func.HttpResponse:
    try:
        body = req.get_json()
    except ValueError:
        return _error(400, "a JSON body is expected")
    try:
        payload = finder.validate_find_request(body, allowed_clients())
    except ValueError as exc:
        return _error(400, str(exc))
    run_id = payload["run_id"] or finder.latest_run(storage(), payload["client"])
    if not run_id:
        return _error(404, "no KB map for this client yet: start a run with POST /api/kecore/runs")
    try:
        kb_map = _kb_map(payload["client"], run_id)
    except FileNotFoundError:
        return _error(404, "no KB map for this run (unknown or unfinished run)")
    # the ticket text is neither logged nor stored; only the model's search terms are recorded
    llm = find_llm(payload["client"]) if payload["interpret"] else None
    return func.HttpResponse(json.dumps(finder.respond(kb_map, payload, llm=llm), ensure_ascii=False),
                             mimetype="application/json")


@app.route(route="kecore/tickets/scrub", methods=["POST"])
def kecore_tickets_scrub(req: func.HttpRequest) -> func.HttpResponse:
    try:
        body = req.get_json()
    except ValueError:
        return _error(400, "a JSON body is expected")
    try:
        payload = tickets_svc.validate_tickets_request(body, allowed_clients())
    except ValueError as exc:
        return _error(400, str(exc))
    report = tickets_svc.scrub(storage(), table(), payload["client"])
    return func.HttpResponse(json.dumps(report, ensure_ascii=False), mimetype="application/json")


@app.route(route="kecore/tickets/runs", methods=["POST"])
@app.durable_client_input(client_name="client")
async def kecore_tickets_start(req: func.HttpRequest, client) -> func.HttpResponse:
    try:
        body = req.get_json()
    except ValueError:
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
    ranges = pipeline.batches(count, tickets_svc.RUN_BATCH_SIZE)
    parts = yield context.task_all(
        [context.call_activity("tickets_run_batch", {**payload, "start": start, "end": end}) for start, end in ranges]
    )
    return tickets_svc.merge_runs(payload["client"], parts)


@app.activity_trigger(input_name="payload")
def tickets_count(payload: dict) -> int:
    return tickets_svc.ticket_count(table(), payload["client"], payload["limit"])


@app.activity_trigger(input_name="payload")
def tickets_run_batch(payload: dict) -> dict:
    # the ticket text is neither logged nor stored; only the model's search terms are recorded,
    # in the same per-client cache kecore/find already uses (find-cache/)
    llm = find_llm(payload["client"]) if payload["interpret"] else None
    return tickets_svc.run_batch(storage(), table(), payload, payload["start"], payload["end"], llm=llm)
