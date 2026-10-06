"""fn-kecore — the V10 KB decomposition, run in Azure (slice 2).

POST /api/kecore/runs  (function key)  {"client": "client-s", "source_prefix": "Kbs/",
                                         "mode": "replay" | "record", "batch_size": 10,
                                         "limit": null, "with_dictionary": true}
  -> starts one Durable Functions run and answers with its status URLs.

The run: extract -> profile -> decompose (batches in parallel, at most 4 at a time, see
host.json) -> report. Each step is an activity that reads from and writes to blob storage
(kecore_pipeline.py); kecore itself is unchanged. Identity: the Function's managed identity,
for blob storage and for Azure OpenAI alike. A client not listed in KECORE_CLIENTS is refused.
"""

from __future__ import annotations

import json
import os
import urllib.parse
import uuid
from datetime import datetime, timezone

import azure.durable_functions as df
import azure.functions as func

import kecore_pipeline as pipeline
from kecore.llm import AzureOpenAIChat, RecordingLLM

app = df.DFApp(http_auth_level=func.AuthLevel.FUNCTION)

_storage = None


def storage():
    global _storage
    if _storage is None:
        from kecore_blob import BlobStorage

        _storage = BlobStorage()
    return _storage


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
