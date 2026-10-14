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

    def test_batches_go_two_at_a_time_unless_told_otherwise(self):
        self.assertEqual(sb.validate_scoreboard_request({"client": CLIENT}, [CLIENT], "sb1")["llm_parallelism"], 2)
        self.assertEqual(sb.validate_scoreboard_request({"client": CLIENT, "llm_parallelism": 8}, [CLIENT],
                                                        "sb1")["llm_parallelism"], 8)
        for value in (0, 9, True, "2", 1.5):
            with self.assertRaises(ValueError, msg=repr(value)):
                sb.validate_scoreboard_request({"client": CLIENT, "llm_parallelism": value}, [CLIENT], "sb1")

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


def reference(table, ref_id, text, expected):
    table.upsert({"PartitionKey": CLIENT, "RowKey": ref_id, "text": text, "expected": json.dumps(expected)})


class ReferenceQuestionsTest(unittest.TestCase):
    def test_unreadable_reference_rows_are_left_out(self):
        refs = MemoryTable()
        reference(refs, "ref-a1", "  comment   attribuer une ligne teams ", ["KB0233"])
        reference(refs, "ref-a2", "Ma machine à café fuit", [])
        reference(refs, "I9", "pas un identifiant de référence", ["KB0120"])
        reference(refs, "ref-a3", "", ["KB0120"])
        refs.upsert({"PartitionKey": CLIENT, "RowKey": "ref-a4", "text": "x", "expected": "{not json"})
        self.assertEqual(sb.references_of(refs, CLIENT), [("ref-a1", "comment attribuer une ligne teams", ["KB0233"]),
                                                          ("ref-a2", "Ma machine à café fuit", [])])
        self.assertEqual(sb.references_of(None, CLIENT), [])

    def test_reference_questions_are_measured_with_the_tickets_and_listed_one_by_one(self):
        storage = kb_storage(fiche(), *filler())
        tickets, labels, refs, scores = MemoryTable(), MemoryTable(), MemoryTable(), MemoryTable()
        ticket(tickets, "I1", "Active Directory : locked account")
        label(labels, "I1", ["KB0120"])
        reference(refs, "ref-locked", "Active Directory : locked account", ["KB0120"])
        reference(refs, "ref-wrong", "Active Directory : locked account", ["KB0900 - imprimante bourrage papier"])
        reference(refs, "ref-none", "la machine à café du 3e étage fuit", [])
        payload = sb.validate_scoreboard_request({"client": CLIENT, "interpret": False}, [CLIENT], "sb-ref")

        prepared = sb.prepare(storage, tickets, labels, payload, refs_table=refs)
        self.assertEqual((prepared["count"], prepared["references"]), (4, 3))
        dataset = pipeline.read_jsonl(storage.read("kecore-client-s", sb.layout("sb-ref")["dataset"]))
        self.assertEqual([d["ticket_id"] for d in dataset], ["I1", "ref-locked", "ref-none", "ref-wrong"])
        self.assertEqual(dataset[1]["meta"]["source"], "reference")
        payload["kb_run_id"] = prepared["kb_run_id"]
        ranges = pipeline.batches(prepared["count"], 2)
        for start, end in ranges:
            sb.batch(storage, payload, start, end)
        short = sb.report(storage, scores, payload, ranges, prepared)

        self.assertEqual(short["tickets"], 4)                       # one dataset, one set of numbers
        self.assertEqual(short["references"]["n"], 3)
        self.assertIn("ref-wrong", short["references"]["failed"])  # KB0120 shown, another fiche expected
        self.assertNotIn("ref-locked", short["references"]["failed"])
        report = sb.latest(storage, CLIENT)["report_md"]
        self.assertIn("## Reference questions (non-regression)", report)
        self.assertIn("**NO**", report)
        row = scores.get(CLIENT, "sb-ref")
        self.assertEqual((row["ref_n"], row["ref_k"]), (3, short["references"]["passed"]))

    def test_expecting_no_fiche_passes_only_when_no_fiche_is_shown(self):
        tickets = [sb.Ticket("ref-1", CLIENT, "q", expected=[], meta={"source": "reference", "labeled": []}),
                   sb.Ticket("ref-2", CLIENT, "q", expected=[], meta={"source": "reference", "labeled": []}),
                   sb.Ticket("I1", CLIENT, "q", expected=["KB1"], meta={})]
        records = [record("ref-1", [], kind="abstain"), record("ref-2", [], "KB1"), record("I1", ["KB1"], "KB1")]
        refs = sb.reference_results(records, tickets)
        self.assertEqual((refs["n"], refs["passed"], refs["failed"]), (2, 1, ["ref-2"]))


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

    def test_a_floor_measured_with_degraded_decisions_is_refused(self):
        rec = {"min_show": 0.42, "confirmed": True, "max_wrong": 0.05}
        self.refused(self.storage_with(rec, degraded={"base": 2, "route": None, "reasons": {}}))
        self.refused(self.storage_with(rec, degraded=3))                         # a bare count reads the same
        # the routed engine's degraded decisions are the gate's business, never the floor's
        applied = sb.apply(self.storage_with(rec, degraded={"base": 0, "route": 4}),
                           {"client": CLIENT, "scoreboard_id": "sb1", "reset": False}, now="T3")
        self.assertEqual(applied["funnel"]["min_show"], 0.42)

    def test_no_recommendation_is_refused(self):
        storage = self.storage_with({"min_show": None, "confirmed": False, "reason": "too few"})
        with self.assertRaises(ValueError):
            sb.apply(storage, {"client": CLIENT, "scoreboard_id": "sb1", "reset": False})

    def test_reset_removes_the_floor(self):
        storage = MemoryStorage()
        storage.write("kecore-client-s", "funnel-config.json", b'{"funnel": {"min_show": 0.42}}')
        result = sb.apply(storage, {"client": CLIENT, "scoreboard_id": None, "reset": True}, now="T2")
        self.assertEqual(result["funnel"], {})


# --- routing: the candidate tables measured next to today's engine, and their gate ------------------------

import routing_service  # noqa: E402
import test_routing_service as fx  # noqa: E402
from kecore.llm import LLMError  # noqa: E402
from kefind.tests.helpers import StubLLM  # noqa: E402
from kefind.tests.schema_llm import SchemaLLM  # noqa: E402
from test_tickets_service import FILLER_TOPICS  # noqa: E402

TEAMS_QUESTION = "comment attribuer une ligne teams"


class Scenario:
    """client-s with the routing fixtures' run (test_routing_service), one validated alias for the Teams fiche
    and its tables built (the candidate), 30 labeled tickets about the filler fiches (three phrasings each:
    enough questions for a gate) and the reference question the alias was written from."""

    def __init__(self, decisions=None, tickets=True, build=True, reference=True):
        self.storage = fx.write_run(fx.Storage())
        self.reviews = MemoryTable()
        for fiche_id, kwargs in (decisions if decisions is not None
                                 else [(fx.TEAMS, {"aliases": ["attribuer ligne teams"]})]):
            fx.decide(self.storage, self.reviews, fiche_id, **kwargs)
        if build:
            self.built = fx.routing_run(self.storage, self.reviews, "b1")
        self.tickets, self.labels, self.refs, self.scores = MemoryTable(), MemoryTable(), MemoryTable(), MemoryTable()
        if tickets:
            n = 0
            for i, topic in enumerate(FILLER_TOPICS):
                for text in (topic, f"problème {topic}", f"souci avec {topic} ce matin"):
                    ticket_id = f"I{n:03d}"
                    n += 1
                    self.tickets.upsert({"PartitionKey": CLIENT, "RowKey": ticket_id, "titre": text, "sujet": "",
                                         "description": ""})
                    label(self.labels, ticket_id, [f"KB09{i:02d} - {topic}"])
        if reference:
            self.refs.upsert(fx.ref_row(fx.REF, TEAMS_QUESTION, [fx.TEAMS]))

    def payload(self, sb_id="sb1", **body):
        return sb.validate_scoreboard_request({"client": CLIENT, "interpret": False, **body}, [CLIENT], sb_id)

    def prepare(self, payload):
        prepared = sb.prepare(self.storage, self.tickets, self.labels, payload, refs_table=self.refs)
        payload.update(kb_run_id=prepared["kb_run_id"], routing=prepared["routing"])
        return prepared

    def measure(self, sb_id="sb1", size=25, llm=None, embedder=None, intent_llm=None, between=None, **body):
        """What scoreboard_run does: prepare, every batch, report."""
        payload = self.payload(sb_id, **body)
        prepared = self.prepare(payload)
        if between:
            between(self)
        ranges = pipeline.batches(prepared["count"], size)
        parts = [sb.batch(self.storage, payload, start, end, llm=llm, embedder=embedder, intent_llm=intent_llm)
                 for start, end in ranges]
        short = sb.report(self.storage, self.scores, payload, ranges, prepared, parts)
        return short, prepared, parts

    def blob(self, name):
        return fx.blob(self.storage, name)

    def summary(self):
        return sb.latest(self.storage, CLIENT)["summary"]

    def records(self, sb_id="sb1", routed=False):
        return pipeline.read_jsonl(self.storage.read(fx.CONTAINER, sb.layout(sb_id)[
            "route_results" if routed else "results"]))

    def gate_name(self):
        candidate = self.blob("routing/candidate.json")
        return routing_service.gate_path(candidate["content_sha256"], candidate["run_id"])


def without_latency(records):
    return [{k: v for k, v in r.items() if k != "latency_s"} for r in records]


class RoutingPinTest(unittest.TestCase):
    def test_no_candidate_tables_change_nothing_in_the_measure(self):
        scenario = Scenario(build=False)
        short, prepared, parts = scenario.measure()
        self.assertEqual((prepared["routing"], prepared["routing_why_not"]), (None, None))
        self.assertTrue(all("route" not in part for part in parts))
        self.assertEqual(sorted(parts[0]), ["degraded", "embed_failures", "embed_refused", "end", "errors",
                                            "interpret_failures", "interpret_refused", "records", "start"])
        self.assertEqual(short["routing"], {"status": None, "build_id": None, "why_not": None})
        self.assertEqual(short["degraded"], {"base": 0, "route": None})
        self.assertFalse([name for (_, name) in scenario.storage.blobs if "results-route" in name or "gates/" in name])
        summary = scenario.summary()
        self.assertIsNone(summary["routing"])
        self.assertEqual(list(summary["engines"]), [sb.ENGINE])
        self.assertNotIn("by_route", summary)
        self.assertNotIn("route_level", summary["references"]["rows"][0])
        report = sb.latest(scenario.storage, CLIENT)["report_md"]
        self.assertNotIn("Routing", report)
        self.assertNotIn("Routed", report)
        row = scenario.scores.get(CLIENT, "sb1")
        self.assertEqual((row["route_status"], row["route_build"], row["route_wrong_k"], row["route_compared"],
                          row["degraded_k"]), ("", "", 0, 0, 0))

    def test_the_candidate_is_pinned_with_its_own_checked_hashes(self):
        scenario = Scenario()
        prepared = scenario.prepare(scenario.payload())
        tables = routing_service.load_tables(scenario.storage, CLIENT, "b1")
        self.assertEqual(prepared["routing"], {"build_id": "b1", "tables_sha256": tables.sha256,
                                               "content_sha256": tables.content_sha256, "kb_sha256": tables.kb_sha256})
        self.assertIsNone(prepared["routing_why_not"])

    def why_not(self, scenario, damage):
        damage(scenario)
        prepared = scenario.prepare(scenario.payload())
        self.assertIsNone(prepared["routing"])
        return prepared["routing_why_not"]

    def test_tables_that_cannot_be_measured_are_not_pinned_and_say_why(self):
        def write(name, data):
            return lambda s: s.storage.write(fx.CONTAINER, name, data)

        def edit_candidate(**changes):
            return lambda s: write("routing/candidate.json", json.dumps(
                {**s.blob("routing/candidate.json"), **changes}).encode())(s)

        def drop_tables(s):
            s.storage.blobs.pop((fx.CONTAINER, "routing/builds/b1/tables.json"))

        cases = {
            "unreadable": (write("routing/candidate.json", b"{nope"), "unreadable"),
            "no build": (write("routing/candidate.json", b"{}"), "names no build"),
            "another run": (edit_candidate(run_id=fx.RUN2), f"built on run {fx.RUN2}, not on {fx.RUN}"),
            "another sha": (edit_candidate(tables_sha256="0" * 64), "not the ones activated"),
            "no tables": (drop_tables, "no routing tables for build b1"),
            "damaged tables": (write("routing/builds/b1/tables.json", b'{"client": "client-s"}'),
                               "do not match their sha256"),
        }
        for case, (damage, expected) in cases.items():
            self.assertIn(expected, self.why_not(Scenario(), damage), case)

    def test_tables_of_another_code_version_or_knowledge_base_are_not_pinned(self):
        with unittest.mock.patch.object(routing_service, "CODE_VERSION", {"tables": 99}):
            self.assertIn("built for code", self.why_not(Scenario(), lambda s: None))
        scenario = Scenario()
        fiches_jsonl = (fx.CONTAINER, f"runs/{fx.RUN}/fiches.jsonl")
        scenario.storage.blobs[fiches_jsonl] = scenario.storage.blobs[fiches_jsonl].replace(b"SOURCE", b"EDITED", 1)
        self.assertIn("another state of the knowledge base", self.why_not(scenario, lambda s: None))

    def test_a_measure_without_tables_says_why_in_its_report(self):
        scenario = Scenario()
        scenario.storage.write(fx.CONTAINER, "routing/candidate.json", b"{nope")
        short, _, _ = scenario.measure()
        self.assertEqual((short["routing"]["status"], short["routing"]["build_id"]), (None, None))
        self.assertIn("unreadable", short["routing"]["why_not"])
        self.assertIn("Routing: the candidate tables were not measured", sb.latest(scenario.storage, CLIENT)["report_md"])


class RoutedMeasureTest(unittest.TestCase):
    def test_both_engines_answer_the_same_frozen_questions(self):
        scenario = Scenario()
        short, prepared, parts = scenario.measure(size=10)
        self.assertEqual(prepared["count"], 31)
        self.assertEqual([p["route"]["records"] for p in parts], [p["records"] for p in parts])
        base, routed = scenario.records(), scenario.records(routed=True)
        self.assertEqual([r["ticket_id"] for r in base], [r["ticket_id"] for r in routed])
        self.assertEqual({r["engine"] for r in routed}, {sb.ROUTE_ENGINE})
        self.assertTrue(all("route" not in r and "degraded" not in r for r in base))   # today's records, unchanged
        self.assertEqual({r["route"]["level"] for r in routed}, {"L1", "L3"})
        teams = next(r for r in routed if r["ticket_id"] == fx.REF)
        self.assertEqual(teams["route"], {"level": "L1", "rule": "alias", "key": "attribuer ligne teams",
                                          "fiche_id": fx.TEAMS})
        self.assertEqual((teams["kind"], teams["fiches"][0]), ("fiche", fx.TEAMS))
        # today's records are what a measure without tables writes, byte for byte but the clock
        unpinned = Scenario(build=False)
        unpinned.measure(size=10)
        self.assertEqual(without_latency(unpinned.records()), without_latency(base))
        self.assertEqual(short["exact"], unpinned.summary()["engines"][sb.ENGINE]["exact"])

    def test_the_gate_is_written_where_activation_reads_it_and_activation_accepts_it(self):
        scenario = Scenario()
        short, _, _ = scenario.measure()
        gate = scenario.blob(scenario.gate_name())
        candidate = scenario.blob("routing/candidate.json")
        for key in ("sb_id", "status", "passed", "build_id", "tables_sha256", "content_sha256", "kb_run_id", "reasons",
                    "wrong_exact", "route_first", "base_first", "compared", "excluded"):
            self.assertIn(key, gate)
        self.assertEqual((gate["sb_id"], gate["status"], gate["passed"], gate["build_id"], gate["kb_run_id"]),
                         ("sb1", "passed", True, "b1", fx.RUN))
        self.assertEqual((gate["content_sha256"], gate["tables_sha256"]),
                         (candidate["content_sha256"], candidate["tables_sha256"]))
        self.assertEqual((gate["wrong_exact"], gate["compared"], gate["excluded"], gate["routed_exact"]),
                         ({"k": 0, "n": 1, "ids": []}, 30, [fx.REF], 1))
        self.assertEqual((gate["route_first"]["k"], gate["base_first"]["k"]), (30, 30))
        self.assertEqual(short["routing"], {"status": "passed", "build_id": "b1", "why_not": None})
        raw = scenario.storage.read(fx.CONTAINER, scenario.gate_name()).decode("utf-8")
        self.assertEqual(raw, json.dumps(gate, ensure_ascii=False, sort_keys=True, indent=1) + "\n")
        pointer = fx.activate(scenario.storage, "b1")                     # what the Revue tab's button does
        self.assertEqual((pointer["build_id"], pointer["gate"]["status"], pointer["gate"]["sb_id"]),
                         ("b1", "passed", "sb1"))

    def test_the_summary_short_output_and_scores_row_carry_the_gate(self):
        scenario = Scenario()
        short, _, _ = scenario.measure()
        summary = scenario.summary()
        routing = summary["routing"]
        self.assertEqual((routing["engine"], routing["build_id"], routing["gate"]["status"], routing["seen"],
                          routing["failures"], routing["gate_file"]),
                         (sb.ROUTE_ENGINE, "b1", "passed", [fx.REF], 0, scenario.gate_name()))
        self.assertEqual(routing["counters"]["records"], 31)
        self.assertEqual(list(summary["engines"]), [sb.ENGINE, sb.ROUTE_ENGINE])
        self.assertEqual(set(summary["by_route"][sb.ROUTE_ENGINE]), {"L1", "L3"})
        self.assertEqual(summary["degraded"], {"base": 0, "route": 0,
                                               "reasons": {sb.ENGINE: {}, sb.ROUTE_ENGINE: {}}})
        self.assertEqual(summary["prepared"]["routing"]["build_id"], "b1")
        self.assertEqual(short["degraded"], {"base": 0, "route": 0})
        self.assertEqual((short["tickets"], short["interpret_failures"]), (31, 0))   # today's engine's, top-level
        row = scenario.scores.get(CLIENT, "sb1")
        self.assertEqual({k: row[k] for k in ("route_status", "route_wrong_k", "route_wrong_n", "route_first_k",
                                              "base_first_k", "route_compared", "route_build", "degraded_k")},
                         {"route_status": "passed", "route_wrong_k": 0, "route_wrong_n": 1, "route_first_k": 30,
                          "base_first_k": 30, "route_compared": 30, "route_build": "b1", "degraded_k": 0})
        report = sb.latest(scenario.storage, CLIENT)["report_md"]
        self.assertIn("## Routing gate (tables of build b1)", report)
        self.assertIn("Status: **passed**", report)
        self.assertIn("## By route", report)
        self.assertIn(scenario.gate_name(), report)
        self.assertTrue(scenario.storage.read(fx.CONTAINER, sb.layout("sb1")["route_results"]))

    def test_reference_rows_say_what_routing_did(self):
        scenario = Scenario()
        scenario.measure()
        row = scenario.summary()["references"]["rows"][0]
        self.assertEqual({k: row[k] for k in ("id", "route_level", "route_shown", "route_first", "route_passed", "seen")},
                         {"id": fx.REF, "route_level": "L1", "route_shown": fx.TEAMS, "route_first": fx.TEAMS,
                          "route_passed": True, "seen": True})
        report = sb.latest(scenario.storage, CLIENT)["report_md"]
        self.assertIn("| Question | Expected | Engine | Pass | Routed | Pass (routed) |", report)
        self.assertIn(f"L1 shows {' '.join(fx.TEAMS.split())} · seen", " ".join(report.split()))

    def test_a_seen_reference_is_left_out_of_right_first_but_its_wrong_exact_fiche_fails_the_gate(self):
        # the reviewer wrote "attribuer ligne teams" for the Teams fiche with the reference on screen (seen); a more
        # specific alias of another fiche routes that very reference elsewhere: the gate must see it
        question = "attribuer une ligne teams pour un compte"
        scenario = Scenario(decisions=[(fx.TEAMS, {"aliases": ["attribuer ligne teams"]}),
                                       (fx.ACCOUNT, {"aliases": ["attribuer ligne teams compte"]})], reference=False)
        scenario.refs.upsert(fx.ref_row(fx.REF, question, [fx.TEAMS]))
        short, _, _ = scenario.measure()
        gate = scenario.blob(scenario.gate_name())
        self.assertEqual(gate["excluded"], [fx.REF])                      # not in the right-first comparison...
        self.assertEqual(gate["compared"], 30)
        self.assertEqual(gate["wrong_exact"], {"k": 1, "n": 1, "ids": [fx.REF]})   # ...but counted as wrong exact
        self.assertEqual((gate["status"], gate["passed"], short["routing"]["status"]), ("failed", False, "failed"))
        self.assertTrue(any("shown as exact at L0-L2 are wrong" in r for r in gate["reasons"]))
        row = scenario.summary()["references"]["rows"][0]
        self.assertEqual((row["route_level"], row["route_shown"], row["route_passed"], row["seen"]),
                         ("L1", fx.ACCOUNT, False, True))
        self.assertEqual(scenario.scores.get(CLIENT, "sb1")["route_wrong_k"], 1)
        with self.assertRaises(ValueError):
            fx.activate(scenario.storage, "b1")

    def test_a_wrong_exact_fiche_on_an_unseen_ticket_also_costs_right_first(self):
        scenario = Scenario(decisions=[(fx.TEAMS, {"aliases": ["attribuer ligne teams"]}),
                                       (fx.ACCOUNT, {"aliases": ["attribuer ligne teams compte"]})], reference=False)
        scenario.tickets.upsert({"PartitionKey": CLIENT, "RowKey": "I900", "titre": "attribuer une ligne teams pour un compte"})
        label(scenario.labels, "I900", [fx.TEAMS])
        scenario.measure()
        gate = scenario.blob(scenario.gate_name())
        self.assertEqual((gate["status"], gate["compared"], gate["excluded"]), ("failed", 31, []))
        self.assertEqual((gate["route_first"]["k"], gate["base_first"]["k"]), (30, 31))
        self.assertEqual(gate["mcnemar"], {"base_only": 1, "route_only": 0, "p_value": 1.0})
        row = scenario.scores.get(CLIENT, "sb1")
        self.assertEqual((row["route_first_k"], row["base_first_k"], row["route_status"]), (30, 31, "failed"))

    def test_tables_built_for_another_state_of_the_knowledge_base_are_never_measured(self):
        def edit_a_fiche(s):  # the run's extracted text changes after prepare pinned the tables
            name = (fx.CONTAINER, f"runs/{fx.RUN}/fiches.jsonl")
            s.storage.blobs[name] = s.storage.blobs[name].replace(b"SOURCE", b"EDITED", 1)

        scenario = Scenario()
        short, _, parts = scenario.measure(between=edit_a_fiche)
        self.assertEqual(sum(p["route"]["errors"] for p in parts), 31)
        self.assertTrue(all("another state of the knowledge base" in r["error"] for r in scenario.records(routed=True)))
        self.assertEqual(short["routing"]["status"], "invalid")

    def test_seen_comes_from_the_decisions_the_tables_were_built_from(self):
        scenario = Scenario(reference=False)
        scenario.refs.upsert(fx.ref_row(fx.REF, TEAMS_QUESTION, [fx.TEAMS]))
        scenario.refs.upsert(fx.ref_row(fx.REF2, "comment débloquer le compte", [fx.ACCOUNT]))
        payload = scenario.payload()
        scenario.prepare(payload)
        dataset = sb._dataset(scenario.storage, payload)
        pin = payload["routing"]
        self.assertEqual(sb.seen_references(scenario.storage, CLIENT, pin, fx.RUN, dataset), ([fx.REF], None))
        # a decision taken after the build is not in its snapshot: it does not make a reference seen
        fx.decide(scenario.storage, scenario.reviews, fx.ACCOUNT, aliases=["débloquer compte"])
        self.assertEqual(sb.seen_references(scenario.storage, CLIENT, pin, fx.RUN, dataset), ([fx.REF], None))
        # no snapshot: every reference is left out of the comparison, and the note says why
        scenario.storage.blobs.pop((fx.CONTAINER, "routing/builds/b1/decisions.json"))
        seen, note = sb.seen_references(scenario.storage, CLIENT, pin, fx.RUN, dataset)
        self.assertEqual(seen, [fx.REF, fx.REF2])
        self.assertIn("decisions of build b1 cannot be read", note)
        self.assertEqual(sb.seen_references(scenario.storage, CLIENT, pin, fx.RUN, [d for d in dataset
                                                                                    if not d.ticket_id.startswith("ref-")]),
                         ([], None))

    def test_tables_that_no_longer_load_make_every_routed_decision_an_error_and_the_gate_invalid(self):
        scenario = Scenario()
        short, _, parts = scenario.measure(between=lambda s: s.storage.blobs.pop(
            (fx.CONTAINER, "routing/builds/b1/tables.json")))
        self.assertEqual(sum(p["route"]["errors"] for p in parts), 31)
        self.assertEqual(sum(p["errors"] for p in parts), 0)                 # today's engine is measured all the same
        routed = scenario.records(routed=True)
        self.assertTrue(all("pinned routing tables cannot be used" in r["error"] for r in routed))
        gate = scenario.blob(scenario.gate_name())
        self.assertEqual(gate["status"], "invalid")
        self.assertTrue(any("routed decision(s) failed" in r for r in gate["reasons"]))
        self.assertEqual(short["routing"]["status"], "invalid")

    def test_transient_model_failures_make_the_gate_invalid(self):
        scenario = Scenario()
        short, _, parts = scenario.measure(llm=StubLLM(LLMError("POST /x -> HTTP 429: quota")), interpret=True)
        self.assertEqual(sum(p["interpret_failures"] for p in parts), 31)    # today's engine: every question...
        self.assertEqual(sum(p["route"]["interpret_failures"] for p in parts), 30)   # ...the routed one: all but L1's
        gate = scenario.blob(scenario.gate_name())
        self.assertEqual((gate["status"], gate["failures"]), ("invalid", 61))
        self.assertTrue(any("61 transient model or embedding failure(s)" in r for r in gate["reasons"]))
        self.assertEqual(short["interpret_failures"], 31)                    # what apply() reads: today's engine only


class L2MeasureTest(unittest.TestCase):
    """Tables with an L2 key (a frozen taxonomy, pass C, a decision with an intent and an application)."""

    def scenario(self):
        storage = fx.write_run(fx.Storage())
        reviews = MemoryTable()
        fx.freeze(storage, reviews)
        fx.decide(storage, reviews, fx.TEAMS, intent="ATTRIBUER_LIGNE", app="teams", aliases=["attribuer ligne teams"])
        scenario = Scenario(build=False)
        scenario.storage, scenario.reviews = storage, reviews
        scenario.built = fx.routing_run(storage, reviews, "b1")
        self.assertEqual(routing_service.load_tables(storage, CLIENT, "b1").l2,
                         [{"intent": "ATTRIBUER_LIGNE", "app": "teams", "fiches": [fx.TEAMS]}])
        scenario.refs.upsert(fx.ref_row(fx.REF2, "donner un numéro de téléphone sur teams", [fx.TEAMS]))
        return scenario

    def test_l2_tables_are_never_measured_without_the_l2_model(self):
        scenario = self.scenario()
        short, _, parts = scenario.measure()
        self.assertEqual(sum(p["route"]["errors"] for p in parts), 32)
        self.assertIn("no L2 mapping model", scenario.records(routed=True)[0]["error"])
        self.assertEqual(short["routing"]["status"], "invalid")

    def test_the_l2_mapping_routes_and_its_failures_are_counted_and_degraded(self):
        mapping = SchemaLLM({"question_intent_app": {"intent": "", "app": ""},
                             ("question_intent_app", "donner un numéro de téléphone sur teams"):
                                 {"intent": "ATTRIBUER_LIGNE", "app": "teams"}})
        scenario = self.scenario()
        scenario.measure(intent_llm=mapping)
        routed = {r["ticket_id"]: r for r in scenario.records(routed=True)}
        self.assertEqual(routed[fx.REF2]["route"], {"level": "L2", "rule": "intent_app", "key": "ATTRIBUER_LIGNE|teams",
                                                    "fiche_id": fx.TEAMS})
        self.assertEqual(routed[fx.REF]["route"]["level"], "L1")                  # decided before L2: no call
        self.assertNotIn(("question_intent_app", TEAMS_QUESTION), mapping.calls)
        self.assertEqual(scenario.summary()["routing"]["counters"]["l2_failures"], 0)

        failing = SchemaLLM({"question_intent_app": LLMError("POST /x -> HTTP 429: quota")})
        scenario = self.scenario()
        short, _, parts = scenario.measure(intent_llm=failing)
        self.assertEqual(sum(p["route"]["l2_failures"] for p in parts), 31)      # every question L1 did not decide
        self.assertEqual(short["degraded"], {"base": 0, "route": 31})
        self.assertEqual(scenario.summary()["degraded"]["reasons"][sb.ROUTE_ENGINE], {"l2_failed:LLMError": 31})
        gate = scenario.blob(scenario.gate_name())
        self.assertEqual((gate["status"], gate["degraded"], gate["failures"]), ("invalid", 31, 31))
        report = sb.latest(scenario.storage, CLIENT)["report_md"]
        self.assertIn(f"31 decision(s) of {sb.ROUTE_ENGINE} were degraded (l2_failed:LLMError: 31)", report)
        self.assertEqual(scenario.scores.get(CLIENT, "sb1")["degraded_k"], 0)    # today's engine's count


class CountingEmbedderTest(unittest.TestCase):
    def test_failures_are_counted_and_the_model_id_is_the_wrapped_one(self):
        class Flaky:
            model_id = "m@test"

            def __init__(self, error):
                self.error = error

            def embed(self, texts, dimensions=None):
                if self.error:
                    raise self.error
                return [[1.0] for _ in texts]

        ok = sb.CountingEmbedder(Flaky(None))
        self.assertEqual((ok.model_id, ok.embed(["a"]), ok.failures, ok.refused), ("m@test", [[1.0]], 0, 0))
        for error, counts in ((LLMError("POST /e -> HTTP 503: busy"), (1, 0)), (LLMError("HTTP 400: too long"), (0, 1))):
            wrapped = sb.CountingEmbedder(Flaky(error))
            with self.assertRaises(LLMError):
                wrapped.embed(["a"])
            self.assertEqual((wrapped.failures, wrapped.refused), counts)


import unittest.mock  # noqa: E402

if __name__ == "__main__":
    unittest.main()
