"""The Azure pipeline gives exactly what a local kecore run gives, from blobs instead of files.

Run from the repository root:  python -m unittest discover -s kecore_func/tests
"""

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for path in (ROOT, ROOT / "kecore_func"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import kecore_pipeline as pipeline  # noqa: E402
from kecore.decompose import Decomposer, summarize_kb  # noqa: E402
from kecore.fiches import load_folder  # noqa: E402
from kecore.llm import RecordingLLM  # noqa: E402
from kecore.profile import build_profile  # noqa: E402
from kecore.tests.helpers import DEMO_KB, FakeLLM, OUTLOOK_ANSWER  # noqa: E402

KEYS = ("fiches", "guided", "citable", "info_only", "steps", "steps_verified", "mean_agreement")


class MemoryStorage:
    def __init__(self):
        self.blobs: dict[tuple[str, str], bytes] = {}

    def list(self, container, prefix):
        return sorted(name for (c, name) in self.blobs if c == container and name.startswith(prefix))

    def read(self, container, name):
        return self.blobs.get((container, name))

    def write(self, container, name, data):
        self.blobs[(container, name)] = data


def demo_storage(prefix="Kbs/"):
    storage = MemoryStorage()
    for path in DEMO_KB.rglob("*"):
        if path.is_file():
            storage.write("kb-clienta", prefix + path.relative_to(DEMO_KB).as_posix(), path.read_bytes())
    storage.write("kb-clienta", "Audio/call.wav", b"not a fiche")  # outside the prefix, never read
    return storage


def run(storage, payload, make_llm=lambda: None, model_id=None, publish=True):
    extracted = pipeline.extract(storage, payload)
    profiled = pipeline.profile(storage, payload, llm=make_llm())
    ranges = pipeline.batches(extracted["fiches"], payload["batch_size"])
    stats = [pipeline.decompose(storage, payload, start, end, llm=make_llm()) for start, end in ranges]
    short = pipeline.report(storage, payload, ranges, profiled, stats, extracted["warnings"], model_id)
    if publish:
        pipeline.publish(storage, payload, short)
    return short


class ValidateTest(unittest.TestCase):
    def test_defaults(self):
        payload = pipeline.validate_request({"client": "clienta"}, ["clienta"], "20261006T010000Z-abc123")
        self.assertEqual(payload["mode"], "record")
        self.assertEqual(payload["batch_size"], pipeline.DEFAULT_BATCH)
        self.assertTrue(payload["with_dictionary"])

    def test_unknown_client_is_refused_without_listing_the_others(self):
        with self.assertRaises(ValueError) as caught:
            pipeline.validate_request({"client": "other"}, ["clienta", "client-s"], "r1")
        self.assertNotIn("client-s", str(caught.exception))

    def test_bad_values_are_refused(self):
        for body in ({"client": "clienta", "source_prefix": "../x"}, {"client": "clienta", "source_prefix": "/abs"},
                     {"client": "clienta", "mode": "refresh"}, {"client": "clienta", "batch_size": 0},
                     {"client": "clienta", "batch_size": True}, {"client": "clienta", "limit": -1},
                     {"client": "clienta", "with_dictionary": "yes"}, ["clienta"]):
            with self.assertRaises(ValueError, msg=str(body)):
                pipeline.validate_request(body, ["clienta"], "r1")

    def test_batches_cover_everything_once(self):
        self.assertEqual(pipeline.batches(23, 10), [[0, 10], [10, 20], [20, 23]])
        self.assertEqual(pipeline.batches(0, 10), [])


class PipelineTest(unittest.TestCase):
    def payload(self, **overrides):
        body = {"client": "clienta", "source_prefix": "Kbs/", "batch_size": 4, **overrides}
        return pipeline.validate_request(body, ["clienta"], "run-1")

    def test_same_result_as_a_local_run(self):
        fiches, _ = load_folder(DEMO_KB, "clienta")
        local = summarize_kb([Decomposer(profile=build_profile("clienta", fiches)).decompose(f) for f in fiches])

        storage = demo_storage()
        short = run(storage, self.payload())
        self.assertEqual({k: short[k] for k in KEYS}, {k: local[k] for k in KEYS})
        for name in ("runs/run-1/fiches.jsonl", "runs/run-1/profile.json", "runs/run-1/report.md",
                     "runs/run-1/summary.json", "runs/run-1/fiches.decomposed.jsonl", "latest.json"):
            self.assertIsNotNone(storage.read("kecore-clienta", name), name)
        latest = json.loads(storage.read("kecore-clienta", "latest.json"))
        self.assertEqual(latest["run_id"], "run-1")

    def test_latest_is_written_by_publish_only_last(self):
        storage = demo_storage()
        short = run(storage, self.payload(), publish=False)
        self.assertIsNone(storage.read("kecore-clienta", "latest.json"))
        self.assertIsNone(storage.read("kecore-clienta", "runs/run-1/published.json"))
        pipeline.publish(storage, self.payload(), short, {"error": "x"})
        latest = json.loads(storage.read("kecore-clienta", "latest.json"))
        self.assertEqual((latest["run_id"], latest["summary"]["semantic"]), ("run-1", {"error": "x"}))
        self.assertEqual(json.loads(storage.read("kecore-clienta", "runs/run-1/published.json")), {"run_id": "run-1"})

    def test_placeholder_and_empty_fiches_stay_out_of_the_map_but_are_listed(self):
        storage = demo_storage()
        storage.write("kb-clienta", "Kbs/KB0010266 - LIBRE - A REUTILISER.md",
                      "# KB0010266 - LIBRE - A REUTILISER\n\nNuméro libre, à réutiliser pour une nouvelle fiche.\n".encode())
        storage.write("kb-clienta", "Kbs/KB0010267 - Vide.md", "# KB0010267 - Vide\n\nA compléter.\n".encode())
        short = run(storage, self.payload())
        excluded = json.loads(storage.read("kecore-clienta", "runs/run-1/excluded.json"))
        rules = {e["title"]: e["rule"] for e in excluded["excluded"]}
        self.assertEqual(rules, {"KB0010266 - LIBRE - A REUTILISER": "title_pattern", "KB0010267 - Vide": "empty"})
        kept = pipeline.read_jsonl(storage.read("kecore-clienta", "runs/run-1/fiches.decomposed.jsonl"))
        self.assertEqual(len(kept), excluded["kept"])
        self.assertFalse({e["fiche_id"] for e in excluded["excluded"]} & {f["fiche_id"] for f in kept})
        self.assertEqual(short["fiches"], len(kept) + 2)             # the report still describes every fiche
        self.assertEqual(short["exclusion"]["excluded"], 2)
        self.assertIn("## Fiches excluded from the map", storage.read("kecore-clienta", "runs/run-1/report.md").decode())
        graph = json.loads(storage.read("kecore-clienta", "runs/run-1/graph.json"))
        self.assertNotIn("KB0010266", json.dumps(graph))

    def test_a_person_can_force_a_fiche_back_in_and_a_broken_config_fails_the_run(self):
        storage = demo_storage()
        storage.write("kb-clienta", "Kbs/KB0010267 - Vide.md", "# KB0010267 - Vide\n\nA compléter.\n".encode())
        run(storage, self.payload())
        fiche_id = json.loads(storage.read("kecore-clienta", "runs/run-1/excluded.json"))["excluded"][0]["fiche_id"]
        storage.write("kecore-clienta", pipeline.EXCLUSION_CONFIG, json.dumps({"force_include": [fiche_id]}).encode())
        payload = pipeline.validate_request({"client": "clienta", "source_prefix": "Kbs/", "batch_size": 4},
                                            ["clienta"], "run-2")
        short = run(storage, payload)
        self.assertEqual(short["exclusion"]["excluded"], 0)
        storage.write("kecore-clienta", pipeline.EXCLUSION_CONFIG, b'{"min_chars": "deux cents"}')
        with self.assertRaises(ValueError) as caught:
            run(storage, pipeline.validate_request({"client": "clienta", "source_prefix": "Kbs/", "batch_size": 4},
                                                   ["clienta"], "run-3"))
        self.assertIn(pipeline.EXCLUSION_CONFIG, str(caught.exception))

    def test_limit_keeps_the_first_fiches_in_document_order(self):
        storage = demo_storage()
        extracted = pipeline.extract(storage, self.payload(limit=2))
        self.assertEqual(extracted["fiches"], 2)

    def test_replay_from_the_blob_record_calls_nothing_and_matches(self):
        storage = demo_storage()
        inner = FakeLLM({"KB0010001 - Outlook ne démarre plus": OUTLOOK_ANSWER})

        def recorder(mode):
            return lambda: RecordingLLM(inner if mode == "record" else None, mode=mode, model_id="fake-model@test",
                                        store=pipeline.StorageRecordStore(storage, "clienta"))

        recorded = run(storage, self.payload(mode="record"), recorder("record"), "fake-model@test")
        calls_after_record = len(inner.calls)
        self.assertGreater(calls_after_record, 0)
        self.assertTrue(storage.list("kecore-clienta", pipeline.LLM_RECORD_PREFIX))

        replayed = run(storage, self.payload(mode="replay"), recorder("replay"), "fake-model@test")
        self.assertEqual(len(inner.calls), calls_after_record)
        self.assertEqual({k: replayed[k] for k in KEYS}, {k: recorded[k] for k in KEYS})
        self.assertEqual(replayed["llm"]["calls"], 0)
        self.assertEqual(replayed["llm"]["errors"], 0)


if __name__ == "__main__":
    unittest.main()
