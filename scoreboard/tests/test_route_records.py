"""Routing marks on decisions and records (``route``, ``degraded``), and the report's "By route" section.

The rule these tests pin: an engine that knows nothing of routing produces exactly what it produced
before (same record keys, same bytes, same report), and a routed record keeps what the gate reads.
"""

import dataclasses
import json
import unittest

from scoreboard.dataset import Ticket
from scoreboard.engines import ROUTE_LEVELS, Decision, decision_from_dict
from scoreboard.engines.fixture import FixtureEngine
from scoreboard.report import build_report
from scoreboard.runner import ROUTE_RECORD_KEYS, load_results, make_record, run_engine

from .helpers import TempDirTestCase, record

FULL_ROUTE = {"version": 1, "level": "L1", "rule": "alias", "key": "attribuer ligne teams", "fiche_id": "KB0233",
              "tables_sha256": "ab" * 32, "content_sha256": "cd" * 32, "build_id": "b1", "run_id": "r1",
              "l0": None, "l1": {"matches": [], "ambiguous": False, "uncovered": 0, "outside_scope": False}, "l2": None}
OLD_KEYS = ["engine", "run", "ticket_id", "client", "expected", "kind", "fiches", "titles", "score", "question",
            "latency_s", "usage", "error"]


class DecisionRouteTest(unittest.TestCase):
    def test_defaults_are_none_and_last(self):
        decision = Decision("abstain")
        self.assertIsNone(decision.route)
        self.assertIsNone(decision.degraded)
        names = [f.name for f in dataclasses.fields(Decision)]
        self.assertEqual(names[-2:], ["route", "degraded"], "new fields go last, with defaults")
        self.assertEqual(names[:9], ["kind", "fiches", "score", "question", "usage", "latency_s", "titles", "trace",
                                     "error"], "existing positional order is unchanged")

    def test_every_level_is_accepted_and_the_route_is_copied(self):
        self.assertEqual(ROUTE_LEVELS, ("L0", "L1", "L2", "L3", "L4"))
        for level in ROUTE_LEVELS:
            route = {"level": level, "rule": "none"}
            decision = Decision("abstain", route=route)
            self.assertEqual(decision.route, route)
            route["level"] = "changed"
            self.assertEqual(decision.route["level"], level, "the decision keeps its own copy")

    def test_unknown_or_missing_level_is_refused(self):
        for route in ({"level": "L5"}, {"level": "l1"}, {"level": None}, {"rule": "alias"}, {"level": ""}):
            with self.subTest(route=route), self.assertRaisesRegex(ValueError, "unknown route level"):
                Decision("fiche", fiches=["K1"], route=route)
        for route in ("L1", ["L1"], 1):
            with self.subTest(route=route), self.assertRaisesRegex(ValueError, "must be a dict"):
                Decision("abstain", route=route)

    def test_degraded_empty_means_none(self):
        self.assertIsNone(Decision("abstain", degraded="").degraded)
        self.assertIsNone(Decision("abstain", degraded=None).degraded)
        self.assertEqual(Decision("abstain", degraded="no_embedder").degraded, "no_embedder")
        self.assertEqual(Decision("abstain", degraded="embedding_failed:LLMError").degraded, "embedding_failed:LLMError")

    def test_from_dict_maps_both_and_tolerates_old_dicts(self):
        decision = decision_from_dict({"kind": "fiche", "fiches": ["KB0233"], "route": FULL_ROUTE,
                                       "degraded": "l2_failed:TimeoutError"})
        self.assertEqual(decision.route, FULL_ROUTE)
        self.assertEqual(decision.degraded, "l2_failed:TimeoutError")
        old = decision_from_dict({"kind": "abstain"})
        self.assertIsNone(old.route)
        self.assertIsNone(old.degraded)
        blank = decision_from_dict({"kind": "abstain", "route": {}, "degraded": ""})
        self.assertIsNone(blank.route, "an empty route is no route")
        self.assertIsNone(blank.degraded)
        with self.assertRaisesRegex(ValueError, "unknown route level"):
            decision_from_dict({"kind": "abstain", "route": {"level": "L9"}})


class MakeRecordTest(unittest.TestCase):
    ticket = Ticket("1", "client-s", "comment attribuer une ligne teams", expected=["KB0233"])

    def test_without_marks_the_record_is_what_it_was(self):
        decision = Decision("fiche", fiches=["KB0233"], score=0.61, titles={"KB0233": "T"}, latency_s=0.123456)
        rec = make_record("kefind-funnel", 0, self.ticket, decision)
        self.assertEqual(list(rec), OLD_KEYS)
        self.assertEqual(
            json.dumps(rec, ensure_ascii=False),
            '{"engine": "kefind-funnel", "run": 0, "ticket_id": "1", "client": "client-s", "expected": ["KB0233"], '
            '"kind": "fiche", "fiches": ["KB0233"], "titles": {"KB0233": "T"}, "score": 0.61, "question": null, '
            '"latency_s": 0.1235, "usage": {"input_tokens": 0, "output_tokens": 0, "search_calls": 0}, "error": null}',
        )

    def test_route_is_projected_and_written_last(self):
        decision = Decision("fiche", fiches=["KB0233"], route=FULL_ROUTE)
        rec = make_record("kefind-route", 0, self.ticket, decision)
        self.assertEqual(list(rec), OLD_KEYS + ["route"])
        self.assertEqual(ROUTE_RECORD_KEYS, ("level", "rule", "key", "fiche_id"))
        self.assertEqual(rec["route"], {"level": "L1", "rule": "alias", "key": "attribuer ligne teams",
                                        "fiche_id": "KB0233"})

    def test_a_partial_route_reads_none_for_what_it_lacks(self):
        rec = make_record("kefind-route", 0, self.ticket, Decision("abstain", route={"level": "L4"}))
        self.assertEqual(rec["route"], {"level": "L4", "rule": None, "key": None, "fiche_id": None})

    def test_degraded_alone(self):
        rec = make_record("kefind-funnel", 0, self.ticket, Decision("question", fiches=["K1"], degraded="no_embedder"))
        self.assertEqual(list(rec), OLD_KEYS + ["degraded"])
        self.assertEqual(rec["degraded"], "no_embedder")
        both = make_record("kefind-route", 0, self.ticket,
                           Decision("abstain", route={"level": "L4"}, degraded="model_mismatch"))
        self.assertEqual(list(both), OLD_KEYS + ["route", "degraded"])


class RoutedEngine:
    name = "kefind-route"

    def decide(self, ticket):
        if ticket.ticket_id == "boom":
            raise RuntimeError("tables unreadable")
        return {"kind": "fiche", "fiches": ["KB0233"], "route": FULL_ROUTE, "degraded": None}


class RunEngineTest(TempDirTestCase):
    def test_routed_records_round_trip_through_the_results_file(self):
        tickets = [Ticket("1", "client-s", "x", expected=["KB0233"]), Ticket("boom", "client-s", "y", expected=[])]
        out = self.path("out/kefind-route.results.jsonl")
        records = run_engine(RoutedEngine(), tickets, out_path=out, clock=lambda: 0.0)
        self.assertEqual(records[0]["route"]["level"], "L1")
        self.assertNotIn("degraded", records[0])
        self.assertNotIn("route", records[1], "a crash is an abstain with an error, not a routed decision")
        self.assertEqual(records[1]["error"], "RuntimeError: tables unreadable")
        loaded = load_results([out])["kefind-route"]
        self.assertEqual(loaded, records)

    def test_fixture_engines_carry_routes_from_their_json(self):
        engine = FixtureEngine({"a/1": {"kind": "fiche", "fiches": ["K1"], "route": {"level": "L0", "rule": "fiche_number",
                                                                                      "key": "233", "fiche_id": "K1"}}})
        rec = run_engine(engine, [Ticket("1", "a", "KB0233", expected=["K1"])], clock=lambda: 0.0)[0]
        self.assertEqual(rec["route"], {"level": "L0", "rule": "fiche_number", "key": "233", "fiche_id": "K1"})


def routed(ticket_id, expected, level, kind="fiche", fiches=(), **kw):
    rec = record(ticket_id, expected, kind=kind, fiches=fiches, engine="kefind-route", **kw)
    rec["route"] = {"level": level, "rule": "x", "key": None, "fiche_id": fiches[0] if fiches else None}
    return rec


class ReportRouteTest(unittest.TestCase):
    def base(self):
        return [record("1", ["K1"], fiches=["K1"], score=0.9, engine="kefind-funnel"),
                record("2", ["K2"], fiches=["K2"], score=0.8, engine="kefind-funnel"),
                record("3", [], kind="abstain", engine="kefind-funnel")]

    def test_no_route_no_section_and_no_summary_key(self):
        records = self.base()
        explicit = [dict(r, route=None) for r in records]
        for results in ({"kefind-funnel": records}, {"kefind-funnel": records, "other": explicit}):
            markdown, summary = build_report(results, None, 0.05)
            self.assertNotIn("## By route", markdown)
            self.assertNotIn(" · L", markdown)
            self.assertNotIn("by_route", summary)

    def test_section_per_routed_engine_between_head_to_head_and_threshold(self):
        route = [routed("1", ["K1"], "L0", fiches=["K1"]),
                 routed("2", ["K2"], "L3", kind="question", fiches=["K2", "K1"], score=0.8),
                 routed("3", [], "L1", fiches=["K9"])]
        markdown, summary = build_report({"kefind-funnel": self.base(), "kefind-route": route}, None, 0.05)
        self.assertIn("## By route", markdown)
        start = markdown.index("## By route")
        end = markdown.index("\n## ", start)
        self.assertLess(markdown.index("## Head to head"), start)
        self.assertEqual(markdown[end + 1:].split("\n", 1)[0], "## Abstention threshold")
        section = markdown[start:end]
        self.assertIn("### kefind-route", section)
        self.assertNotIn("### kefind-funnel", section, "an engine that routed nothing gets no table")
        self.assertIn("| L0 | 1 | 1 | 1 | 100.0% (", section)
        self.assertIn("| L1 | 1 | 0 | 1 | – | 1 | – | 0 | 0 |", section)
        self.assertIn("| L3 | 1 | 1 | 0 | 0.0% (", section)
        self.assertIn("| – | 100.0% (", section.split("| L3 |")[1], "L3 has no wrong-exact column")
        self.assertIn("**1 fiche(s) shown as exact at L0-L2 are wrong:** 3.", section)
        self.assertEqual(list(summary["by_route"]), ["kefind-route"])
        self.assertEqual(summary["by_route"]["kefind-route"]["L1"]["wrong_exact"], 1)
        self.assertIn("| 3 | a | none | fiche K9 · L1 |", markdown, "a miss says which level decided it")
        json.dumps(summary)  # still serializable

    def test_the_demo_engines_write_no_mark_and_get_no_section(self):
        from scoreboard.dataset import load_tickets
        from scoreboard.engines import load_engine

        from .helpers import EXAMPLES

        tickets = load_tickets(EXAMPLES / "demo.tickets.jsonl")
        results = {}
        for config in ("demo.standard.json", "demo.careful.json"):
            engine = load_engine("fixture", EXAMPLES / config)
            results[engine.name] = run_engine(engine, tickets, runs=2, clock=lambda: 0.0)
        for records in results.values():
            self.assertTrue(records)
            for rec in records:
                self.assertEqual(list(rec), OLD_KEYS)
        markdown, summary = build_report(results, None, 0.05)
        self.assertNotIn("## By route", markdown)
        self.assertNotIn(" · L", markdown)
        self.assertNotIn("by_route", summary)

    def test_no_wrong_line_when_every_exact_is_right(self):
        markdown, _ = build_report({"kefind-route": [routed("1", ["K1"], "L1", fiches=["K1"])]}, None, 0.05)
        self.assertIn("## By route", markdown)
        self.assertNotIn("are wrong:", markdown)


if __name__ == "__main__":
    unittest.main()
