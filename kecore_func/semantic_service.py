"""The semantic index of a kecore run, built in Azure before the run is published.

After ``report`` (decomposed fiches, graph) and before ``publish`` (latest.json), five activities
add the run's semantic folder, kecore-<client>/runs/<run>/semantic/:

  plan       the run's map                        -> how many fiches are ranked
  cards      ranked fiches [start:end] + the model -> cards/<index>.json   (kefind.cards)
  heldout    ranked fiches [start:end] + the model -> heldout/<index>.json (kefind.calibrate: the exam)
  index      cards + the embedding model           -> variants/<rule>/{vectors.f32,index.json}, built.json
                                                     (kefind.semantic: one embedding pass, one index per
                                                     DROP_RULES rule)
  calibrate  variants + exam + the embedding model -> the winner's vectors.f32 then index.json, then
                                                     calibration.json (kefind.calibrate.calibrate_variants)

The model's answers are recorded in llm-cache/ (like the decomposition) and every vector in
embed-cache/ (kecore.llm.RecordingEmbeddings): a run in mode "replay" rebuilds the same folder
without one call, byte for byte (the sha256 of the vectors is in index.json, and calibration.json
names it). latest.json is written only after this folder is complete, so a published run never
changes and the Function may keep its map in memory. If any of it fails, the run is still published
-- /kecore/find then decides by words, as before, and latest.json says why. A run's index is only
ever used complete: vectors.f32 is written before index.json, and the map loads an index only when
its calibration.json exists (kefind_service.load_map); anything less is ignored, never half-used.

Like kecore_pipeline, this module never imports an Azure SDK: storage is any object with
list/read/write, so it is tested in memory.
"""

from __future__ import annotations

import json

import kecore_pipeline as pipeline
import kefind_service as finder
from kefind import semantic as sem
from kefind.calibrate import Heldout, calibrate_variants, heldout_for
from kefind.cards import Card, anchors_for, entries_for, make_card, same_label_groups

EMBED_RECORD_PREFIX = "embed-cache/"
DIMENSIONS = 1024  # text-embedding-3-large shortened (the model's own option); recorded in index.json


def paths(run_id: str) -> dict[str, str]:
    base = f"runs/{run_id}/"
    return {
        "cards_dir": base + "semantic/cards/",
        "heldout_dir": base + "semantic/heldout/",
        "index": base + sem.INDEX_BLOB,
        "vectors": base + sem.VECTORS_BLOB,
        "calibration": base + sem.CALIBRATION_BLOB,
        "built": base + "semantic/built.json",
        "variants_dir": base + "semantic/variants/",
    }


def _map(storage: pipeline.Storage, payload: dict):
    return finder.load_map(storage, payload["client"], payload["run_id"], with_semantic=False)


def _llm_counts(llm) -> dict:
    return {"calls": getattr(llm, "calls", 0), "cached": getattr(llm, "hits", 0)} if llm is not None else {"calls": 0, "cached": 0}


def plan(storage: pipeline.Storage, payload: dict) -> dict:
    return {"fiches": len(_map(storage, payload).ranked)}


def cards(storage: pipeline.Storage, payload: dict, start: int, end: int, llm=None) -> dict:
    kbmap = _map(storage, payload)
    container, directory = pipeline.kecore_container(payload["client"]), paths(payload["run_id"])["cards_dir"]
    errors = questions = dropped = 0
    for index in range(start, min(end, len(kbmap.ranked))):
        card = make_card(llm, kbmap, kbmap.ranked[index])
        errors += card.error is not None
        questions += len(card.questions)
        dropped += len(card.dropped)
        storage.write(container, f"{directory}{index:05d}.json", json.dumps(card.to_dict(), ensure_ascii=False).encode("utf-8"))
    return {"start": start, "end": end, "errors": errors, "questions": questions, "dropped": dropped, **_llm_counts(llm)}


def heldout(storage: pipeline.Storage, payload: dict, start: int, end: int, llm=None) -> dict:
    kbmap = _map(storage, payload)
    container, directory = pipeline.kecore_container(payload["client"]), paths(payload["run_id"])["heldout_dir"]
    errors = queries = dropped = 0
    for index in range(start, min(end, len(kbmap.ranked))):
        exam = heldout_for(llm, kbmap, kbmap.ranked[index])
        errors += exam.error is not None
        queries += len(exam.queries)
        dropped += len(exam.dropped)
        storage.write(container, f"{directory}{index:05d}.json", json.dumps(exam.to_dict(), ensure_ascii=False).encode("utf-8"))
    return {"start": start, "end": end, "errors": errors, "queries": queries, "dropped": dropped, **_llm_counts(llm)}


def _read_all(storage: pipeline.Storage, payload: dict, directory: str, count: int) -> list[dict]:
    container = pipeline.kecore_container(payload["client"])
    return [json.loads(pipeline._require(storage, container, f"{directory}{i:05d}.json").decode("utf-8"))
            for i in range(count)]


def build_index(storage: pipeline.Storage, payload: dict, embedder) -> dict:
    """One embedding pass, one index per rule of kefind.semantic.DROP_RULES, each under variants/<rule>/
    (vectors first). Nothing is served from here: the calibration picks one and publishes it."""
    kbmap = _map(storage, payload)
    p = paths(payload["run_id"])
    by_id = {c["fiche_id"]: Card.from_dict(c) for c in _read_all(storage, payload, p["cards_dir"], len(kbmap.ranked))}
    entries, anchors = entries_for(kbmap, by_id), anchors_for(kbmap)
    vectors = sem.embed_entries(entries, anchors, embedder, embedder.dimensions)
    container = pipeline.kecore_container(payload["client"])
    same_label = same_label_groups(kbmap)
    built_all: dict = {"same_label": same_label}
    summary: dict = {}
    for rule in sem.DROP_RULES:
        index, built = sem.build(entries, embedder, embedder.model_id, embedder.dimensions, anchors=anchors,
                                 rule=rule, vectors=vectors)
        blobs = index.to_blobs()
        storage.write(container, f"{p['variants_dir']}{rule}/vectors.f32", blobs[sem.VECTORS_BLOB])
        storage.write(container, f"{p['variants_dir']}{rule}/index.json", blobs[sem.INDEX_BLOB])
        built_all[rule] = {"sha256": index.sha256, **built}
        summary[rule] = {"sha256": index.sha256, "entries": index.stats["entries"], "by_kind": index.stats["by_kind"],
                         "dropped": index.stats["dropped_closer_to_another_fiche"]}
    storage.write(container, p["built"], pipeline._json(built_all))
    return {"fiches": len(kbmap.ranked), "entries_before_checks": len(entries), "anchors": len(anchors),
            "variants": summary, "same_label_groups": len(same_label),
            "embeddings": {"calls": embedder.calls, "cached": embedder.hits, "embedded": embedder.embedded}}


def calibrate_run(storage: pipeline.Storage, payload: dict, embedder) -> dict:
    """Calibrates every variant on the run's exam, publishes the winner as the run's index (vectors.f32,
    then index.json) and writes calibration.json last: the map only ever loads a complete index."""
    kbmap = _map(storage, payload)
    p = paths(payload["run_id"])
    container = pipeline.kecore_container(payload["client"])
    variants = {}
    for rule in sem.DROP_RULES:
        index = sem.SemanticIndex.from_blobs(
            pipeline._require(storage, container, f"{p['variants_dir']}{rule}/index.json"),
            pipeline._require(storage, container, f"{p['variants_dir']}{rule}/vectors.f32"))
        variants[rule] = (index, finder.with_index(kbmap, index))
    exams = [Heldout.from_dict(h) for h in _read_all(storage, payload, p["heldout_dir"], len(kbmap.ranked))]
    result = calibrate_variants(variants, exams, embedder)
    winner = variants[result["variant"]][0]
    blobs = winner.to_blobs()
    storage.write(container, p["vectors"], blobs[sem.VECTORS_BLOB])
    storage.write(container, p["index"], blobs[sem.INDEX_BLOB])
    storage.write(container, p["calibration"], (json.dumps(result, ensure_ascii=False, indent=1, sort_keys=True) + "\n").encode("utf-8"))
    return {"variant": result["variant"], "thresholds": result["thresholds"], "withheld": result["withheld"],
            "chosen": result["chosen"], "feasible": result["feasible"], "acceptance": result["acceptance"],
            "test": {k: result["test"][k] for k in ("right_shown", "wrong_shown", "questions", "abstain", "source_offered", "loo_shown")},
            "recall_test": result["recall_test"], "variants": result["variants"],
            "exam": {k: result["exam"][k] for k in ("fiches", "questions", "model_errors")},
            "embeddings": {"calls": embedder.calls, "cached": embedder.hits, "embedded": embedder.embedded}}


__all__ = ["plan", "cards", "heldout", "build_index", "calibrate_run", "paths", "EMBED_RECORD_PREFIX", "DIMENSIONS"]
