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
     (min_show) chosen on half the labels and confirmed on the other half.
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
import kecore_pipeline as pipeline
import kefind_service as finder
import scoreboard_service as sb_svc
import semantic_service as semantic_svc
import tickets_service as tickets_svc
from kecore.llm import AzureOpenAIChat, AzureOpenAIEmbeddings, RecordingEmbeddings, RecordingLLM

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


def make_llm(payload: dict) -> RecordingLLM:
    # Built even in replay mode: the record key holds "<deployment>@<host>", taken from the client.
    # In replay mode RecordingLLM never calls it; a missing answer is an error, never a model call.
    inner = AzureOpenAIChat.from_config(llm_config())
    return RecordingLLM(inner, mode=payload["mode"], store=pipeline.StorageRecordStore(storage(), payload["client"]))


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
    semantic = None
    if payload.get("semantic", True):
        try:
            planned = yield context.call_activity("kecore_semantic_plan", payload)
            fiches = pipeline.batches(planned["fiches"], payload["batch_size"])
            cards = yield context.task_all(
                [context.call_activity("kecore_cards", {**payload, "start": start, "end": end}) for start, end in fiches]
            )
            exams = yield context.task_all(
                [context.call_activity("kecore_heldout", {**payload, "start": start, "end": end}) for start, end in fiches]
            )
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
    summary = yield context.call_activity("kecore_publish", {**payload, "summary": summary, "semantic": semantic})
    return summary


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
def kecore_semantic_index(payload: dict) -> dict:
    return semantic_svc.build_index(storage(), payload, make_build_embedder(payload))


@app.activity_trigger(input_name="payload")
def kecore_calibrate(payload: dict) -> dict:
    return semantic_svc.calibrate_run(storage(), payload, make_build_embedder(payload))


@app.activity_trigger(input_name="payload")
def kecore_publish(payload: dict) -> dict:
    return pipeline.publish(storage(), payload, payload["summary"], payload.get("semantic"))


_chat = None


def find_llm(client: str) -> RecordingLLM:
    """The model that interprets tickets, behind the client's record (kecore-<client>/find-cache/)."""
    global _chat
    if _chat is None:
        _chat = AzureOpenAIChat.from_config(llm_config())
    return RecordingLLM(_chat, mode="record", store=pipeline.StorageRecordStore(storage(), client, prefix="find-cache/"))


_embeddings = None


def _embedding_client() -> AzureOpenAIEmbeddings:
    global _embeddings
    if _embeddings is None:
        _embeddings = AzureOpenAIEmbeddings.from_config(embedding_config())
    return _embeddings


def make_build_embedder(payload: dict) -> RecordingEmbeddings:
    """The vectors of a run's index and exam, behind the client's record (kecore-<client>/embed-cache/):
    a run in mode "replay" reads them all and calls nothing."""
    return RecordingEmbeddings(_embedding_client(), mode=payload["mode"], dimensions=semantic_svc.DIMENSIONS,
                               store=pipeline.StorageRecordStore(storage(), payload["client"],
                                                                 prefix=semantic_svc.EMBED_RECORD_PREFIX))


def query_embedder(client: str, dimensions: int):
    """A question's vector, behind the client's record (kecore-<client>/find-cache/): the first vector
    recorded for a text is the one every later call gets. None when the deployment is not configured
    or its client cannot be built: /find then decides by words and says so (mode "degraded")."""
    try:
        return RecordingEmbeddings(_embedding_client(), mode="record", dimensions=dimensions,
                                   store=pipeline.StorageRecordStore(storage(), client, prefix="find-cache/"))
    except Exception:
        return None


def _query_embedder_for_run(payload: dict):
    """The tickets run and the scoreboard decide each ticket exactly as /kecore/find does: by meaning
    when the map has an index (same record, find-cache/, so a ticket asked live and replayed in a
    measurement gets one vector)."""
    return query_embedder(payload["client"], semantic_svc.DIMENSIONS)


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
    answer = finder.respond(kb_map, payload, config=_funnel_config(payload["client"]), llm=llm, embedder=embedder)
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
    llm = find_llm(payload["client"]) if payload["interpret"] else None
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
    payload = context.get_input()
    prepared = yield context.call_activity("scoreboard_prepare", payload)
    if not prepared["count"]:
        return {"client": payload["client"], "sb_id": payload["sb_id"], "error": "no labeled ticket or reference question yet",
                "prepared": prepared}
    payload = {**payload, "kb_run_id": prepared["kb_run_id"]}
    ranges = pipeline.batches(prepared["count"], sb_svc.BATCH_SIZE)
    parts = yield context.task_all(
        [context.call_activity("scoreboard_batch", {**payload, "start": start, "end": end}) for start, end in ranges]
    )
    summary = yield context.call_activity("scoreboard_report",
                                          {**payload, "ranges": ranges, "prepared": prepared, "batches": parts})
    return summary


@app.activity_trigger(input_name="payload")
def scoreboard_prepare(payload: dict) -> dict:
    from kecore_table import LABELS, REFERENCES

    return sb_svc.prepare(storage(), table(), table(LABELS), payload, refs_table=table(REFERENCES))


@app.activity_trigger(input_name="payload")
def scoreboard_batch(payload: dict) -> dict:
    llm = find_llm(payload["client"]) if payload["interpret"] else None
    return sb_svc.batch(storage(), payload, payload["start"], payload["end"], llm=llm,
                        embedder=_query_embedder_for_run(payload))


@app.activity_trigger(input_name="payload")
def scoreboard_report(payload: dict) -> dict:
    from kecore_table import SCORES

    return sb_svc.report(storage(), table(SCORES), payload, payload["ranges"], payload["prepared"],
                         payload.get("batches"))


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
