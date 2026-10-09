"""The scoreboard on Azure (V10 slice 4): labeled tickets replayed through kefind's funnel and
measured by the scoreboard package; a floor chosen on half A, applied only if half B confirms it.

Run from the repository root:  python -m unittest discover -s kecore_func/tests
"""

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for path in (ROOT, ROOT / "kecore_func", Path(__file__).resolve().parent):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import kecore_pipeline as pipeline  # noqa: E402
import scoreboard_service as sb  # noqa: E402
from test_tickets_service import MemoryStorage, MemoryTable, fiche, filler, kb_storage  # noqa: E402

CLIENT = "client-s"


def label(table, ticket_id, expected, skipped=False):
    table.upsert({"PartitionKey": CLIENT, "RowKey": ticket_id, "expected": json.dumps(expected), "skipped": skipped})


def ticket(table, ticket_id, title, description=""):
    table.upsert({"PartitionKey": CLIENT, "RowKey": ticket_id, "titre": title, "sujet": "", "description": description})


def record(ticket_id, expected, shown=None, score=None, kind=None):
    kind = kind or ("fiche" if shown else "abstain")
    return {"engine": sb.ENGINE, "run": 0, "ticket_id": ticket_id, "client": CLIENT, "expected": expected,
            "kind": kind, "fiches": [shown] if shown else [], "titles": {}, "score": score, "question": None,
            "latency_s": 0.01, "usage": {}, "error": None}


class ValidateTest(unittest.TestCase):
    def test_defaults(self):
        payload = sb.validate_scoreboard_request({"client": CLIENT}, [CLIENT], "20261008T010000Z-abc123")
        self.assertEqual((payload["max_wrong"], payload["interpret"], payload["run_id"]), (sb.DEFAULT_MAX_WRONG, True, None))

    def test_bad_values_are_refused(self):
        for body in ({"client": "other"}, {"client": CLIENT, "max_wrong": 0}, {"client": CLIENT, "max_wrong": 0.5},
                     {"client": CLIENT, "max_wrong": True}, {"client": CLIENT, "run_id": "../x"},
                     {"client": CLIENT, "interpret": "no"}, [CLIENT]):
            with self.assertRaises(ValueError, msg=str(body)):
                sb.validate_scoreboard_request(body, [CLIENT], "sb1")

    def test_apply_needs_a_run_or_a_reset(self):
        self.assertTrue(sb.validate_apply_request({"client": CLIENT, "reset": True}, [CLIENT])["reset"])
        for body in ({"client": CLIENT}, {"client": CLIENT, "scoreboard_id": "../x"}, {"client": "other", "reset": True}):
            with self.assertRaises(ValueError, msg=str(body)):
                sb.validate_apply_request(body, [CLIENT])


class SplitAndLabelsTest(unittest.TestCase):
    def test_a_ticket_always_falls_in_the_same_half_and_both_halves_fill(self):
        halves = [sb.split_of(f"I{i}") for i in range(200)]
        self.assertEqual(halves, [sb.split_of(f"I{i}") for i in range(200)])
        self.assertTrue(60 < halves.count("A") < 140)

    def test_skipped_and_unreadable_labels_are_left_out(self):
        labels = MemoryTable()
        label(labels, "I1", ["KB0120"])
        label(labels, "I2", [])
        label(labels, "I3", ["KB0120"], skipped=True)
        labels.upsert({"PartitionKey": CLIENT, "RowKey": "I4", "expected": "{not json"})
        self.assertEqual(sb.labels_of(labels, CLIENT), {"I1": ["KB0120"], "I2": []})

    def test_a_label_also_accepts_the_fiche_the_graph_maps_it_to(self):
        twin_a = fiche("KB0300", "KB0300- VPN RESET")
        twin_b = fiche("KB0301", "KB0301- VPN RESET")  # same text: duplicates in the graph
        kbmap = pipeline_map(kb_storage(twin_a, twin_b, *filler()))
        canonical = kbmap.graph.canonical("KB0301")
        self.assertIn(canonical, sb.accepted(kbmap, ["KB0301"]))
        self.assertEqual(sb.accepted(kbmap, ["UNKNOWN"]), ["UNKNOWN"])


def pipeline_map(storage):
    import kefind_service as finder
    return finder.load_map(storage, CLIENT, "r1")


class RecommendTest(unittest.TestCase):
    def half(self, prefix, good_score, bad_score):
        records = [record(f"{prefix}{i}", ["KB1"], "KB1", good_score) for i in range(4)]
        records += [record(f"{prefix}w{i}", ["KB1"], "KB2", bad_score) for i in range(4)]
        return records

    def test_a_floor_chosen_on_a_and_holding_on_b_is_confirmed(self):
        records = self.half("a", 0.9, 0.2) + self.half("b", 0.9, 0.2)
        split = {r["ticket_id"]: ("A" if r["ticket_id"].startswith("a") else "B") for r in records}
        rec = sb.recommend(records, split, 0.4)
        self.assertEqual((rec["min_show"], rec["confirmed"]), (0.9, True))

    def test_a_floor_that_fails_on_b_is_not_confirmed(self):
        records = self.half("a", 0.9, 0.2) + self.half("b", 0.9, 0.95)  # on B the wrong fiches score high
        split = {r["ticket_id"]: ("A" if r["ticket_id"].startswith("a") else "B") for r in records}
        rec = sb.recommend(records, split, 0.4)
        self.assertEqual(rec["min_show"], 0.9)
        self.assertFalse(rec["confirmed"])

    def test_too_few_tickets_say_how_many_are_needed(self):
        records = [record("a1", ["KB1"], "KB1", 0.9), record("b1", ["KB1"], "KB1", 0.9)]
        rec = sb.recommend(records, {"a1": "A", "b1": "B"}, 0.05)
        self.assertIsNone(rec["min_show"])
        self.assertIn("at least 73", rec["reason"])

    def test_the_floor_never_hides_a_designated_fiche(self):
        records = [record("t1", ["KB1"], "KB1", None), record("t2", ["KB1"], "KB1", 0.1)]
        floored = sb.with_floor(records, 0.5)
        self.assertEqual([r["kind"] for r in floored], ["fiche", "question"])


class EndToEndTest(unittest.TestCase):
    def test_labeled_tickets_are_measured_and_reported(self):
        storage = kb_storage(fiche(), *filler())
        tickets, labels, scores = MemoryTable(), MemoryTable(), MemoryTable()
        ticket(tickets, "I1", "Active Directory : locked account")
        ticket(tickets, "I2", "Imprimante bourrage papier au 2e étage")
        label(labels, "I1", ["KB0120"])
        label(labels, "I2", ["KB0900 - imprimante bourrage papier"])
        label(labels, "I3", [])  # labeled, but the ticket row is gone
        payload = sb.validate_scoreboard_request({"client": CLIENT, "interpret": False}, [CLIENT], "sb1")

        prepared = sb.prepare(storage, tickets, labels, payload)
        self.assertEqual((prepared["count"], prepared["missing"], prepared["kb_run_id"]), (2, 1, "r1"))
        payload["kb_run_id"] = prepared["kb_run_id"]
        ranges = pipeline.batches(prepared["count"], 1)
        parts = [sb.batch(storage, payload, start, end) for start, end in ranges]
        self.assertEqual(sum(p["records"] for p in parts), 2)

        short = sb.report(storage, scores, payload, ranges, prepared)
        self.assertEqual(short["tickets"], 2)
        self.assertEqual(short["exact"]["n"], 2)
        self.assertGreaterEqual(short["exact"]["k"], 1)
        row = scores.get(CLIENT, "sb1")
        self.assertEqual((row["exact_n"], row["kb_run"]), (2, "r1"))
        latest = sb.latest(storage, CLIENT)
        self.assertEqual(latest["sb_id"], "sb1")
        self.assertIn("# Scoreboard", latest["report_md"])
        self.assertIn("Calibrated floor", latest["report_md"])

    def test_the_sweep_runs_with_the_deployed_floor_off(self):
        storage = kb_storage(fiche(), *filler())
        storage.write("kecore-client-s", "funnel-config.json", b'{"funnel": {"min_show": 0.99}}')
        tickets, labels, scores = MemoryTable(), MemoryTable(), MemoryTable()
        ticket(tickets, "I1", "Active Directory : locked account")
        label(labels, "I1", ["KB0120"])
        payload = sb.validate_scoreboard_request({"client": CLIENT, "interpret": False}, [CLIENT], "sb2")
        prepared = sb.prepare(storage, tickets, labels, payload)
        payload["kb_run_id"] = prepared["kb_run_id"]
        sb.batch(storage, payload, 0, 1)
        short = sb.report(storage, scores, payload, [[0, 1]], prepared)
        self.assertEqual(short["exact"]["k"], 1)  # raw engine: the fiche is shown
        self.assertEqual(short["deployed_min_show"], 0.99)
        summary = sb.latest(storage, CLIENT)["summary"]
        self.assertEqual(summary["deployed"]["exact"]["k"], 0)  # with the deployed floor it is only offered


class InterpretationFailureTest(unittest.TestCase):
    def test_model_failures_are_counted_reported_and_block_the_floor(self):
        from kecore.llm import LLMError
        from kefind.tests.helpers import StubLLM

        storage = kb_storage(fiche(), *filler())
        tickets, labels, scores = MemoryTable(), MemoryTable(), MemoryTable()
        ticket(tickets, "I1", "Active Directory : locked account")
        label(labels, "I1", ["KB0120"])
        payload = sb.validate_scoreboard_request({"client": CLIENT}, [CLIENT], "sb3")
        prepared = sb.prepare(storage, tickets, labels, payload)
        payload["kb_run_id"] = prepared["kb_run_id"]
        part = sb.batch(storage, payload, 0, 1, llm=StubLLM(LLMError("POST /x -> HTTP 429: quota")))
        self.assertEqual((part["interpret_failures"], part["interpret_refused"]), (1, 0))
        short = sb.report(storage, scores, payload, [[0, 1]], prepared, batches=[part])
        self.assertEqual(short["interpret_failures"], 1)
        self.assertIn("failed to interpret 1 ticket", sb.latest(storage, CLIENT)["report_md"])

    def test_a_failure_that_repeats_at_the_desk_too_is_reported_not_blocking(self):
        from kecore.llm import LLMError
        from kefind.tests.helpers import StubLLM

        storage = kb_storage(fiche(), *filler())
        tickets, labels, scores = MemoryTable(), MemoryTable(), MemoryTable()
        ticket(tickets, "I1", "Active Directory : locked account")
        label(labels, "I1", ["KB0120"])
        payload = sb.validate_scoreboard_request({"client": CLIENT}, [CLIENT], "sb4")
        prepared = sb.prepare(storage, tickets, labels, payload)
        payload["kb_run_id"] = prepared["kb_run_id"]
        part = sb.batch(storage, payload, 0, 1, llm=StubLLM(LLMError("POST /x -> HTTP 400: content filter")))
        self.assertEqual((part["interpret_failures"], part["interpret_refused"]), (0, 1))
        short = sb.report(storage, scores, payload, [[0, 1]], prepared, batches=[part])
        self.assertEqual((short["interpret_failures"], short["interpret_refused"]), (0, 1))


class ApplyTest(unittest.TestCase):
    def storage_with(self, recommendation, **summary):
        storage = MemoryStorage()
        storage.write("kecore-client-s", "latest.json", b'{"run_id": "r1"}')
        data = {"kb_run_id": "r1", "interpret": True, "interpret_failures": 0, "recommendation": recommendation}
        data.update(summary)
        storage.write("kecore-client-s", sb.layout("sb1")["summary"], json.dumps(data).encode("utf-8"))
        return storage

    def refused(self, storage):
        with self.assertRaises(ValueError):
            sb.apply(storage, {"client": CLIENT, "scoreboard_id": "sb1", "reset": False})
        self.assertIsNone(storage.read("kecore-client-s", "funnel-config.json"))

    def test_a_floor_measured_on_another_map_is_refused(self):
        self.refused(self.storage_with({"min_show": 0.42, "confirmed": True, "max_wrong": 0.05}, kb_run_id="r0"))

    def test_a_floor_measured_without_interpretation_or_with_model_failures_is_refused(self):
        rec = {"min_show": 0.42, "confirmed": True, "max_wrong": 0.05}
        self.refused(self.storage_with(rec, interpret=False))
        self.refused(self.storage_with(rec, interpret_failures=3))

    def test_no_word_floor_is_applied_to_a_map_that_decides_by_meaning(self):
        storage = self.storage_with({"min_show": 0.42, "confirmed": True, "max_wrong": 0.05})
        storage.write("kecore-client-s", "runs/r1/semantic/calibration.json", b'{"index_sha256": "x"}')
        self.refused(storage)

    def test_a_floor_for_a_loose_ceiling_is_refused(self):
        self.refused(self.storage_with({"min_show": 0.42, "confirmed": True, "max_wrong": 0.2}))

    def test_a_confirmed_floor_is_applied_and_the_previous_config_kept(self):
        storage = self.storage_with({"min_show": 0.42, "confirmed": True, "max_wrong": 0.05})
        storage.write("kecore-client-s", "funnel-config.json", b'{"funnel": {"gap": 0.2}}')
        result = sb.apply(storage, {"client": CLIENT, "scoreboard_id": "sb1", "reset": False}, now="T1")
        self.assertEqual(result["funnel"], {"gap": 0.2, "min_show": 0.42})
        self.assertEqual(storage.read("kecore-client-s", "funnel-config.history/T1.json"), b'{"funnel": {"gap": 0.2}}')
        import kefind_service as finder
        self.assertEqual(finder.funnel_config(storage, CLIENT).min_show, 0.42)

    def test_an_unconfirmed_floor_is_refused(self):
        storage = self.storage_with({"min_show": 0.42, "confirmed": False, "reason": "half B fails"})
        with self.assertRaises(ValueError):
            sb.apply(storage, {"client": CLIENT, "scoreboard_id": "sb1", "reset": False})
        self.assertIsNone(storage.read("kecore-client-s", "funnel-config.json"))

    def test_no_recommendation_is_refused(self):
        storage = self.storage_with({"min_show": None, "confirmed": False, "reason": "too few"})
        with self.assertRaises(ValueError):
            sb.apply(storage, {"client": CLIENT, "scoreboard_id": "sb1", "reset": False})

    def test_reset_removes_the_floor(self):
        storage = MemoryStorage()
        storage.write("kecore-client-s", "funnel-config.json", b'{"funnel": {"min_show": 0.42}}')
        result = sb.apply(storage, {"client": CLIENT, "scoreboard_id": None, "reset": True}, now="T2")
        self.assertEqual(result["funnel"], {})


if __name__ == "__main__":
    unittest.main()
