"""Pass A of the semantic enrichment in a kecore run, in Azure (kefind.enrich), before the run is published.

After ``report`` (and the semantic folder, when built) and before ``publish`` (latest.json), two
activities add the run's enrichment folder, kecore-<client>/runs/<run>/enrichment/:

  enrich    ranked fiches [start:end] + the model -> <index>.json   (one proposal per fiche, checked by code)
  summarize every proposal                        -> summary.json    (kefind.enrich.stats)

The model's answers are recorded in llm-cache/ like every other call of the run (kecore.llm.RecordingLLM,
first answer recorded wins), and a proposal holds no clock and no cache flag: a run in mode "replay"
rewrites the same folder byte for byte with no model call. Every proposal is ``status: "proposed"``;
nothing reads this folder to route a question yet (passes B/C, the review tab and the routing tables
come next, docs/v10-deterministic-engine.md). If the enrichment fails, the run is still published and
its summary says why -- exactly like the semantic folder.

Like kecore_pipeline, this module never imports an Azure SDK: storage is any object with list/read/write.
"""

from __future__ import annotations

import json

import kecore_pipeline as pipeline
import kefind_service as finder
from kefind.enrich import Enrichment, make_enrichment, stats


def paths(run_id: str) -> dict[str, str]:
    base = f"runs/{run_id}/enrichment/"
    return {"dir": base, "summary": base + "summary.json"}


def _map(storage: pipeline.Storage, payload: dict):
    return finder.load_map(storage, payload["client"], payload["run_id"], with_semantic=False)


def _bytes(data: dict) -> bytes:
    return (json.dumps(data, ensure_ascii=False, sort_keys=True, indent=1) + "\n").encode("utf-8")


def enrich(storage: pipeline.Storage, payload: dict, start: int, end: int, llm=None) -> dict:
    """The proposals of ranked fiches [start, end), one file each, in the map's order."""
    kbmap = _map(storage, payload)
    container, directory = pipeline.kecore_container(payload["client"]), paths(payload["run_id"])["dir"]
    errors = aliases = dropped = 0
    for index in range(start, min(end, len(kbmap.ranked))):
        proposal = make_enrichment(llm, kbmap, kbmap.ranked[index])
        errors += proposal.error is not None
        aliases += len(proposal.semantic_aliases_fr) + len(proposal.semantic_aliases_en)
        dropped += len(proposal.dropped)
        storage.write(container, f"{directory}{index:05d}.json", _bytes(proposal.to_dict()))
    counts = {"calls": getattr(llm, "calls", 0), "cached": getattr(llm, "hits", 0)} if llm is not None else {"calls": 0, "cached": 0}
    return {"start": start, "end": end, "errors": errors, "aliases": aliases, "dropped": dropped, **counts}


def summarize(storage: pipeline.Storage, payload: dict) -> dict:
    """Reads every proposal of the run (all must be there) and writes summary.json."""
    kbmap = _map(storage, payload)
    container, p = pipeline.kecore_container(payload["client"]), paths(payload["run_id"])
    proposals = [Enrichment.from_dict(json.loads(pipeline._require(storage, container, f"{p['dir']}{i:05d}.json")))
                 for i in range(len(kbmap.ranked))]
    summary = stats(proposals)
    storage.write(container, p["summary"], _bytes(summary))
    return summary


__all__ = ["paths", "enrich", "summarize"]
