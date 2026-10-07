"""POST /api/kecore/find: the fiche for a ticket, decided by code on the client's KB map (V10 slice 3).

The map is the one a kecore run froze under kecore-<client>/runs/<run_id>/:
fiches.decomposed.jsonl (verified steps and entities), profile.json (the client's dictionary) and
graph.json (relations between fiches; rebuilt by the same code for a run made before slice 3).
latest.json names the run used when the request names none. The decision is kefind.funnel, code
only; the steps returned are the fiche's own text, verified at decomposition. With "interpret"
(the default), the model first turns the ticket into search terms in the words of the fiches,
English and French (kefind.interpret, checked by code): they rank, they never filter or decide.

Like kecore_pipeline, this module never imports an Azure SDK: storage is any object with
list/read/write, so it is tested in memory.
"""

from __future__ import annotations

import json
import re

import kecore_pipeline as pipeline
from kecore.decompose import DecomposedFiche
from kecore.profile import Profile
from kefind.funnel import FunnelConfig, KBMap, fiche_view, find
from kefind.graph import KBGraph
from kefind.interpret import interpret

MAX_TEXT_CHARS = 20_000
MAX_ANSWERS = 10
_ANSWER_RE = re.compile(r"^[a-z]{2,8}:\S.{0,199}$", re.DOTALL)
_RUN_ID_RE = re.compile(r"^[0-9A-Za-z-]{1,64}$")


def validate_find_request(body, allowed_clients) -> dict:
    """The find request, checked before anything is read. Deny-by-default on the client."""
    if not isinstance(body, dict):
        raise ValueError("a JSON object is expected")
    client = body.get("client")
    if not isinstance(client, str) or client not in set(allowed_clients):
        raise ValueError("unknown client")
    text = body.get("text")
    if not isinstance(text, str) or not text.strip():
        raise ValueError("text: the ticket text is required")
    if len(text) > MAX_TEXT_CHARS:
        raise ValueError(f"text: {MAX_TEXT_CHARS} characters at most")
    answers = body.get("answers", [])
    if (not isinstance(answers, list) or len(answers) > MAX_ANSWERS
            or not all(isinstance(a, str) and _ANSWER_RE.match(a) for a in answers)):
        raise ValueError(f'answers: a list of at most {MAX_ANSWERS} entities such as "app:teams"')
    run_id = body.get("run_id")
    if run_id is not None and (not isinstance(run_id, str) or not _RUN_ID_RE.match(run_id)):
        raise ValueError("invalid run id")
    interpret_ticket = body.get("interpret", True)
    if not isinstance(interpret_ticket, bool):
        raise ValueError("interpret must be true or false")
    return {"client": client, "text": text, "answers": answers, "run_id": run_id, "interpret": interpret_ticket}


def latest_run(storage: pipeline.Storage, client: str) -> str | None:
    data = storage.read(pipeline.kecore_container(client), pipeline.LATEST)
    return json.loads(data.decode("utf-8")).get("run_id") if data else None


def load_map(storage: pipeline.Storage, client: str, run_id: str) -> KBMap:
    """The KB map of one run. FileNotFoundError when the run has no decomposed fiches."""
    container = pipeline.kecore_container(client)
    paths = pipeline.layout(run_id)
    data = storage.read(container, paths["decomposed"])
    if data is None:
        raise FileNotFoundError(f"{container}/{paths['decomposed']} is missing")
    fiches = [DecomposedFiche.from_dict(item) for item in pipeline.read_jsonl(data)]
    profile = storage.read(container, paths["profile"])
    dictionary = Profile.from_dict(json.loads(profile.decode("utf-8"))).dictionary if profile else {}
    graph = storage.read(container, paths["graph"])
    graph = KBGraph.from_dict(json.loads(graph.decode("utf-8"))) if graph else None
    return KBMap(client, fiches, dictionary, graph=graph, run_id=run_id)


def respond(kbmap: KBMap, payload: dict, config: FunnelConfig | None = None, llm=None) -> dict:
    """The answer to a find request. ``llm`` interprets the ticket when the request asks for it."""
    interpretation = None
    if payload.get("interpret", True) and llm is not None:
        interpretation = interpret(llm, payload["text"], kbmap.dictionary)
    finding = find(kbmap, payload["text"], answers=payload["answers"], config=config, interpretation=interpretation)
    return {
        "client": kbmap.client,
        "run_id": kbmap.run_id,
        "decision": finding.to_dict(),
        "fiche": fiche_view(kbmap, finding.fiche_id) if finding.fiche_id else None,
        "candidates": [{"fiche_id": f, "label": kbmap.label(f)} for f in finding.fiches],
    }


__all__ = ["validate_find_request", "latest_run", "load_map", "respond"]
