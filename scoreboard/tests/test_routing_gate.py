"""The routing metrics: right fiche first, decisions by level, and the gate routing tables must pass.

The gate is the only thing between a person's review and live exact fiches, so each way it can say
"passed" is pinned here: a wrong exact fiche anywhere (seen questions and questions no fiche covers
included) fails it, a measure that cannot be trusted is invalid rather than failed or passed, and a
measure that proves nothing is insufficient.
"""

import copy
import json
import random
import unittest

from scoreboard.metrics import (
    EXACT_LEVELS,
    GATE_STATUSES,
    MIN_GATE_QUESTIONS,
    by_route,
    first_fiche,
    is_routed_exact,
    is_wrong_exact,
    mcnemar_exact,
    right_first,
    route_level,
    routing_gate,
)

from .helpers import record

CLIENT = "client-s"


def base(ticket_id, expected, kind="fiche", fiches=(), **kw):
    kw.setdefault("score", 0.7 if kind == "fiche" else None)
    return record(ticket_id, expected, kind=kind, fiches=fiches, client=kw.pop("client", CLIENT),
                  engine="kefind-funnel", **kw)


def routed(ticket_id, expected, level, kind="fiche", fiches=(), **kw):
    rec = record(ticket_id, expected, kind=kind, fiches=fiches, client=kw.pop("client", CLIENT),
                 engine="kefind-route", **kw)
    rec["route"] = {"level": level, "rule": "alias" if level == "L1" else "semantic", "key": None,
                    "fiche_id": fiches[0] if fiches and level in EXACT_LEVELS else None}
    return rec


def tid(i):
    return f"T{i:02d}"


def scenario(n=MIN_GATE_QUESTIONS, exact=3):
    """n questions, each expecting its own fiche; today's engine shows it; the router shows the first
    `exact` ones as validated (L1) and offers the rest at L3 with the same fiche first."""
    b = [base(tid(i), [f"K{i}"], fiches=[f"K{i}", "KX"]) for i in range(n)]
    r = [routed(tid(i), [f"K{i}"], "L1", fiches=[f"K{i}"]) if i < exact
         else routed(tid(i), [f"K{i}"], "L3", kind="question", fiches=[f"K{i}", "KX"], score=0.7)
         for i in range(n)]
    return b, r


def replace(records, ticket_id, new):
    return [new if r["ticket_id"] == ticket_id else r for r in records]


class HelpersTest(unittest.TestCase):
    def test_constants(self):
        self.assertEqual(EXACT_LEVELS, ("L0", "L1", "L2"))
        self.assertEqual(MIN_GATE_QUESTIONS, 30)
        self.assertEqual(GATE_STATUSES, ("invalid", "failed", "insufficient", "passed"))

    def test_min_gate_questions_matches_kefind(self):
        try:
            from kefind.routing import MIN_GATE_QUESTIONS as KEFIND_MIN
        except ImportError as exc:  # pragma: no cover - kefind is in the same repository
            self.skipTest(f"kefind not importable: {exc}")
        self.assertEqual(MIN_GATE_QUESTIONS, KEFIND_MIN, "one threshold, two copies: keep them equal")

    def test_first_fiche(self):
        self.assertEqual(first_fiche(record("1", ["A"], kind="fiche", fiches=["A", "B"])), "A")
        self.assertEqual(first_fiche(record("1", ["B"], kind="question", fiches=["B", "A"])), "B")
        self.assertIsNone(first_fiche(record("1", ["A"], kind="abstain", fiches=["A"])),
                          "an abstention ranks candidates but offers none")
        self.assertIsNone(first_fiche(record("1", ["A"], kind="question", fiches=[])))
        self.assertIsNone(first_fiche({"expected": ["A"], "fiches": ["A"]}))

    def test_right_first(self):
        self.assertTrue(right_first(record("1", ["A"], kind="fiche", fiches=["A"])))
        self.assertTrue(right_first(record("1", ["A", "Z"], kind="question", fiches=["Z", "A"])))
        self.assertFalse(right_first(record("1", ["A"], kind="question", fiches=["B", "A"])), "first only")
        self.assertFalse(right_first(record("1", ["A"], kind="abstain", fiches=["A"])))
        self.assertFalse(right_first(record("1", [], kind="fiche", fiches=["A"])), "no fiche expected: never right")
        self.assertFalse(right_first(record("1", [], kind="abstain")))

    def test_route_level(self):
        rec = record("1", ["A"])
        self.assertIsNone(route_level(rec), "records written before routing have no route key")
        self.assertIsNone(route_level(dict(rec, route=None)))
        self.assertIsNone(route_level(dict(rec, route={})))
        self.assertEqual(route_level(dict(rec, route={"level": "L2"})), "L2")

    def test_routed_exact_needs_an_exact_level_and_a_shown_fiche(self):
        self.assertTrue(is_routed_exact(routed("1", ["A"], "L0", fiches=["A"])))
        self.assertTrue(is_routed_exact(routed("1", ["A"], "L2", fiches=["A"])))
        self.assertFalse(is_routed_exact(routed("1", ["A"], "L2", kind="question", fiches=["A", "B"])),
                         "an L2 choice offers fiches, it shows none as exact")
        self.assertFalse(is_routed_exact(routed("1", ["A"], "L3", fiches=["A"])), "L3 is never exact")
        self.assertFalse(is_routed_exact(base("1", ["A"], fiches=["A"])), "an unrouted fiche is not routed exact")

    def test_wrong_exact(self):
        self.assertFalse(is_wrong_exact(routed("1", ["A"], "L1", fiches=["A"])))
        self.assertTrue(is_wrong_exact(routed("1", ["A"], "L1", fiches=["B", "A"])))
        self.assertTrue(is_wrong_exact(routed("1", [], "L0", fiches=["A"])), "a fiche where none is expected is wrong")
        self.assertFalse(is_wrong_exact(routed("1", [], "L3", kind="question", fiches=["A"])))


class ByRouteTest(unittest.TestCase):
    def test_counts_per_level_in_order_with_none_last(self):
        records = [
            routed("4", ["K4"], "L4", kind="abstain", fiches=["K4"]),
            base("9", ["K9"], fiches=["K9"]),
            routed("1", ["K1"], "L1", fiches=["K1"]),
            routed("2", [], "L1", fiches=["K2"]),
            routed("3", ["K3"], "L3", kind="question", fiches=["K3", "K1"]),
            routed("5", ["K5"], "L0", fiches=["K6"]),
        ]
        levels = by_route(records)
        self.assertEqual(list(levels), ["L0", "L1", "L3", "L4", "none"], "a level nobody reached is absent")
        keys = ["n", "with_fiche", "shown", "exact", "wrong_exact", "right_first", "questions", "abstain"]
        for counts in levels.values():
            self.assertEqual(list(counts), keys)
        self.assertEqual(levels["L1"], {"n": 2, "with_fiche": 1, "shown": 2, "exact": 1, "wrong_exact": 1,
                                        "right_first": 1, "questions": 0, "abstain": 0})
        self.assertEqual(levels["L0"], {"n": 1, "with_fiche": 1, "shown": 1, "exact": 0, "wrong_exact": 1,
                                        "right_first": 0, "questions": 0, "abstain": 0})
        self.assertEqual(levels["L3"], {"n": 1, "with_fiche": 1, "shown": 0, "exact": 0, "wrong_exact": 0,
                                        "right_first": 1, "questions": 1, "abstain": 0})
        self.assertEqual(levels["L4"], {"n": 1, "with_fiche": 1, "shown": 0, "exact": 0, "wrong_exact": 0,
                                        "right_first": 0, "questions": 0, "abstain": 1})
        self.assertEqual(levels["none"], {"n": 1, "with_fiche": 1, "shown": 1, "exact": 1, "wrong_exact": 0,
                                          "right_first": 1, "questions": 0, "abstain": 0})

    def test_first_run_only_and_empty(self):
        self.assertEqual(by_route([]), {})
        records = [routed("1", ["K1"], "L1", fiches=["K1"]),
                   routed("1", ["K1"], "L3", kind="question", fiches=["K1"], run=1)]
        self.assertEqual(list(by_route(records)), ["L1"])

    def test_only_unrouted_records(self):
        self.assertEqual(list(by_route([base("1", ["K1"], fiches=["K1"])])), ["none"])


class GatePassedTest(unittest.TestCase):
    def test_passed_and_its_full_shape(self):
        b, r = scenario()
        gate = routing_gate(b, r)
        self.assertEqual(list(gate), ["status", "passed", "tickets", "compared", "excluded", "wrong_exact",
                                      "routed_exact", "routed_exact_unseen", "right_first", "mcnemar", "by_level",
                                      "reasons"])
        self.assertEqual(gate["status"], "passed")
        self.assertIs(gate["passed"], True)
        self.assertEqual(gate["tickets"], 30)
        self.assertEqual(gate["compared"], 30)
        self.assertEqual(gate["excluded"], [])
        self.assertEqual(gate["wrong_exact"], {"k": 0, "n": 3, "ids": []})
        self.assertEqual((gate["routed_exact"], gate["routed_exact_unseen"]), (3, 3))
        self.assertEqual(gate["right_first"]["base"]["k"], 30)
        self.assertEqual(gate["right_first"]["base"]["n"], 30)
        self.assertEqual(gate["right_first"]["route"], gate["right_first"]["base"])
        self.assertEqual(sorted(gate["right_first"]["route"]), ["ci95", "k", "n", "value"], "Rate.to_dict()")
        self.assertEqual(gate["mcnemar"], {"base_only": 0, "route_only": 0, "p_value": 1.0})
        self.assertEqual(gate["by_level"], by_route(r))
        self.assertEqual(list(gate["by_level"]), ["L1", "L3"])
        self.assertEqual(gate["reasons"], [])
        json.dumps(gate)  # stored as routing/gates/<content>-<run>.json

    def test_an_l3_offer_keeps_the_fiche_first_like_the_base_engine_showing_it(self):
        b, r = scenario()
        self.assertEqual(r[10]["kind"], "question")
        self.assertEqual(right_first(b[10]), right_first(r[10]))

    def test_exactly_the_minimum_passes_one_less_is_insufficient(self):
        b, r = scenario(n=12)
        self.assertEqual(routing_gate(b, r, min_questions=12)["status"], "passed")
        self.assertEqual(routing_gate(b, r, min_questions=13)["status"], "insufficient")

    def test_questions_no_fiche_covers_are_tickets_not_compared(self):
        b, r = scenario()
        b.append(base("ref-none", [], kind="abstain", fiches=["K1"]))
        r.append(routed("ref-none", [], "L4", kind="abstain", fiches=["K1"]))
        gate = routing_gate(b, r)
        self.assertEqual((gate["status"], gate["tickets"], gate["compared"]), ("passed", 31, 30))

    def test_an_offer_where_none_is_expected_is_not_a_wrong_exact(self):
        b, r = scenario()
        b.append(base("ref-none", [], kind="question", fiches=["K1"]))
        r.append(routed("ref-none", [], "L3", kind="question", fiches=["K1"]))
        gate = routing_gate(b, r)
        self.assertEqual(gate["status"], "passed")
        self.assertEqual(gate["wrong_exact"]["k"], 0)

    def test_equal_right_first_with_discordant_pairs_passes(self):
        b, r = scenario()
        r = replace(r, tid(5), routed(tid(5), ["K5"], "L4", kind="abstain"))           # base right only
        b = replace(b, tid(6), base(tid(6), ["K6"], kind="abstain", fiches=["K6"]))    # route right only
        gate = routing_gate(b, r)
        self.assertEqual(gate["status"], "passed")
        self.assertEqual(gate["mcnemar"], {"base_only": 1, "route_only": 1, "p_value": mcnemar_exact(1, 1)})
        self.assertEqual(gate["right_first"]["base"]["k"], gate["right_first"]["route"]["k"])


class GateFailedTest(unittest.TestCase):
    def assert_failed(self, gate, *fragments):
        self.assertEqual(gate["status"], "failed", gate["reasons"])
        self.assertIs(gate["passed"], False)
        for fragment in fragments:
            self.assertTrue(any(fragment in reason for reason in gate["reasons"]), (fragment, gate["reasons"]))

    def test_a_wrong_exact_fiche_on_a_compared_question(self):
        b, r = scenario()
        r = replace(r, tid(1), routed(tid(1), ["K1"], "L1", fiches=["K7"]))
        gate = routing_gate(b, r)
        self.assert_failed(gate, "1 fiche(s) shown as exact at L0-L2 are wrong (T01)")
        self.assertEqual(gate["wrong_exact"], {"k": 1, "n": 3, "ids": ["T01"]})

    def test_a_wrong_exact_fiche_fails_even_when_right_first_improves(self):
        b, r = scenario()
        b = [base(x["ticket_id"], x["expected"], kind="abstain") for x in b]  # base never right
        r = replace(r, tid(0), routed(tid(0), ["K0"], "L0", fiches=["K9"]))
        gate = routing_gate(b, r)
        self.assert_failed(gate, "are wrong (T00)")
        self.assertEqual(gate["mcnemar"]["base_only"], 0)
        self.assertEqual(gate["mcnemar"]["route_only"], 29)
        self.assertEqual(len(gate["reasons"]), 1)

    def test_a_wrong_exact_fiche_on_an_excluded_question_still_fails(self):
        """A seen reference is exactly where an alias written from it can lose to another fiche's."""
        b, r = scenario(n=31)
        r = replace(r, tid(30), routed(tid(30), ["K30"], "L1", fiches=["K2"]))
        gate = routing_gate(b, r, exclude_ids=[tid(30)])
        self.assert_failed(gate, "are wrong (T30)")
        self.assertEqual(gate["excluded"], ["T30"])
        self.assertEqual(gate["compared"], 30)
        self.assertEqual(gate["wrong_exact"], {"k": 1, "n": 4, "ids": ["T30"]})
        self.assertEqual(gate["mcnemar"], {"base_only": 0, "route_only": 0, "p_value": 1.0},
                         "the excluded question stays out of the comparison")
        self.assertEqual(len(gate["reasons"]), 1)

    def test_a_fiche_shown_as_exact_where_none_is_expected(self):
        for level in EXACT_LEVELS:
            with self.subTest(level=level):
                b, r = scenario()
                b.append(base("ref-none", [], kind="abstain"))
                r.append(routed("ref-none", [], level, fiches=["K3"]))
                gate = routing_gate(b, r)
                self.assert_failed(gate, "are wrong (ref-none)")
                self.assertEqual(gate["wrong_exact"], {"k": 1, "n": 4, "ids": ["ref-none"]})
                self.assertEqual(gate["compared"], 30)

    def test_an_excluded_question_no_fiche_covers_still_fails(self):
        b, r = scenario()
        b.append(base("ref-none", [], kind="abstain"))
        r.append(routed("ref-none", [], "L2", fiches=["K3"]))
        self.assert_failed(routing_gate(b, r, exclude_ids=["ref-none"]), "are wrong (ref-none)")

    def test_a_wrong_exact_fiche_in_a_later_run_counts(self):
        b, r = scenario()
        b2 = [dict(x, run=1) for x in b]
        r2 = [dict(x, run=1) for x in r]
        r2 = replace(r2, tid(2), dict(routed(tid(2), ["K2"], "L1", fiches=["K8"]), run=1))
        gate = routing_gate(b + b2, r + r2)
        self.assert_failed(gate, "are wrong (T02)")
        self.assertEqual(gate["wrong_exact"], {"k": 1, "n": 3, "ids": ["T02"]})
        self.assertEqual(gate["by_level"], by_route(r), "levels read the first run")

    def test_right_first_lower_than_today(self):
        b, r = scenario()
        r = replace(r, tid(12), routed(tid(12), ["K12"], "L4", kind="abstain", fiches=["K12"]))
        gate = routing_gate(b, r)
        self.assert_failed(gate, "the right fiche comes first less often with routing (29 of 30) than without (30 of 30)")
        self.assertEqual(gate["wrong_exact"]["k"], 0)
        self.assertEqual(gate["right_first"]["route"]["k"], 29)
        self.assertEqual(gate["mcnemar"], {"base_only": 1, "route_only": 0, "p_value": 1.0})

    def test_an_l3_offer_with_another_fiche_first_loses_right_first(self):
        b, r = scenario()
        r = replace(r, tid(20), routed(tid(20), ["K20"], "L3", kind="question", fiches=["KX", "K20"]))
        self.assert_failed(routing_gate(b, r), "less often with routing")

    def test_a_loss_on_an_excluded_question_does_not_count(self):
        b, r = scenario(n=31)
        r = replace(r, tid(30), routed(tid(30), ["K30"], "L4", kind="abstain"))
        gate = routing_gate(b, r, exclude_ids=[tid(30)])
        self.assertEqual(gate["status"], "passed", gate["reasons"])
        self.assertEqual(gate["mcnemar"]["base_only"], 0)

    def test_failed_wins_over_insufficient(self):
        b, r = scenario(n=5, exact=1)
        r = replace(r, tid(0), routed(tid(0), ["K0"], "L1", fiches=["K4"]))
        gate = routing_gate(b, r)
        self.assert_failed(gate, "are wrong (T00)", "only 5 question(s)")


class GateInvalidTest(unittest.TestCase):
    def assert_invalid(self, gate, *fragments):
        self.assertEqual(gate["status"], "invalid", gate["reasons"])
        self.assertIs(gate["passed"], False)
        for fragment in fragments:
            self.assertTrue(any(fragment in reason for reason in gate["reasons"]), (fragment, gate["reasons"]))

    def test_a_failed_base_decision(self):
        b, r = scenario()
        b = replace(b, tid(4), base(tid(4), ["K4"], kind="abstain", error="TimeoutError: slow"))
        self.assert_invalid(routing_gate(b, r), "1 base decision(s) failed (e.g. T04)")

    def test_a_failed_routed_decision(self):
        b, r = scenario()
        crashed = record(tid(4), ["K4"], kind="abstain", client=CLIENT, engine="kefind-route",
                         error="RuntimeError: tables unreadable")
        self.assert_invalid(routing_gate(b, replace(r, tid(4), crashed)), "1 routed decision(s) failed (e.g. T04)")

    def test_a_failed_decision_in_a_later_run(self):
        b, r = scenario()
        later = [dict(x, run=1) for x in r]
        later[0] = dict(later[0], error="LLMError: HTTP 500")
        self.assert_invalid(routing_gate(b, r + later), "routed decision(s) failed")

    def test_a_question_the_routed_engine_did_not_answer(self):
        b, r = scenario()
        gate = routing_gate(b, r[:-1])
        self.assert_invalid(gate, "did not answer the same questions", "1 missing, 0 extra", "client-s/T29")

    def test_a_question_only_the_routed_engine_answered(self):
        b, r = scenario()
        gate = routing_gate(b, r + [routed("T99", ["K1"], "L1", fiches=["K1"])])
        self.assert_invalid(gate, "0 missing, 1 extra")

    def test_the_same_ticket_id_for_another_client_is_another_question(self):
        b, r = scenario()
        moved = [dict(x, client="client-t") if x["ticket_id"] == tid(3) else x for x in r]
        self.assert_invalid(routing_gate(b, moved), "1 missing, 1 extra")

    def test_a_later_run_that_did_not_answer_every_question(self):
        b, r = scenario()
        self.assert_invalid(routing_gate(b, r + [dict(x, run=1) for x in r[:10]]), "routed engine's run(s) 1")
        self.assert_invalid(routing_gate(b + [dict(x, run=1) for x in b[:10]], r), "base engine's run(s) 1")

    def test_different_expected_fiches(self):
        b, r = scenario()
        r = replace(r, tid(7), routed(tid(7), ["K7", "K70"], "L3", kind="question", fiches=["K7"]))
        self.assert_invalid(routing_gate(b, r), "1 question(s) carry different expected fiches in the two results (e.g. T07)")

    def test_expected_fiches_in_another_order_are_the_same(self):
        b, r = scenario()
        b = replace(b, tid(7), base(tid(7), ["K7", "K70"], fiches=["K7"]))
        r = replace(r, tid(7), routed(tid(7), ["K70", "K7"], "L3", kind="question", fiches=["K7"]))
        self.assertEqual(routing_gate(b, r)["status"], "passed")

    def test_transient_failures(self):
        b, r = scenario()
        self.assert_invalid(routing_gate(b, r, failures=2), "2 transient model or embedding failure(s)")
        self.assertEqual(routing_gate(b, r, failures=0)["status"], "passed")

    def test_degraded_count(self):
        b, r = scenario()
        self.assert_invalid(routing_gate(b, r, degraded=1), "1 degraded decision(s)")

    def test_degraded_marks_on_records_even_when_the_count_says_zero(self):
        for engine in ("base", "routed"):
            with self.subTest(engine=engine):
                b, r = scenario()
                if engine == "base":
                    b[8] = dict(b[8], degraded="no_embedder")
                else:
                    r[8] = dict(r[8], degraded="l2_failed:TimeoutError")
                gate = routing_gate(b, r)
                self.assert_invalid(gate, "1 question(s) carry a degraded decision (e.g. T08)")

    def test_invalid_wins_over_failed_and_keeps_every_reason(self):
        b, r = scenario(n=5)
        r = replace(r, tid(0), routed(tid(0), ["K0"], "L1", fiches=["K4"]))
        gate = routing_gate(b, r, failures=1)
        self.assert_invalid(gate, "transient", "are wrong (T00)", "only 5 question(s)")
        self.assertEqual(gate["wrong_exact"]["ids"], ["T00"], "the numbers are still reported")

    def test_reasons_come_in_status_order(self):
        b, r = scenario(n=5)
        r = replace(r, tid(0), routed(tid(0), ["K0"], "L1", fiches=["K4"]))
        reasons = routing_gate(b, r, degraded=3)["reasons"]
        self.assertEqual(len(reasons), 4)
        self.assertIn("degraded", reasons[0])
        self.assertIn("are wrong", reasons[1])
        self.assertIn("comes first less often", reasons[2])
        self.assertIn("only 5", reasons[3])


class GateInsufficientTest(unittest.TestCase):
    def assert_insufficient(self, gate, *fragments):
        self.assertEqual(gate["status"], "insufficient", gate["reasons"])
        self.assertIs(gate["passed"], False)
        self.assertEqual(len(gate["reasons"]), len(fragments))
        for fragment in fragments:
            self.assertTrue(any(fragment in reason for reason in gate["reasons"]), (fragment, gate["reasons"]))

    def test_too_few_compared_questions(self):
        b, r = scenario(n=29)
        gate = routing_gate(b, r)
        self.assert_insufficient(gate, "only 29 question(s) with an expected fiche can be compared "
                                       "(0 excluded as seen by a reviewer); at least 30 are needed")
        self.assertEqual(gate["wrong_exact"], {"k": 0, "n": 3, "ids": []})

    def test_exclusions_count_against_the_minimum(self):
        b, r = scenario(n=32)
        gate = routing_gate(b, r, exclude_ids=[tid(29), tid(30), tid(31)])
        self.assert_insufficient(gate, "only 29 question(s) with an expected fiche can be compared "
                                       "(3 excluded as seen by a reviewer)")
        self.assertEqual(gate["excluded"], ["T29", "T30", "T31"])

    def test_questions_no_fiche_covers_do_not_make_up_the_minimum(self):
        b, r = scenario(n=29)
        b.append(base("ref-none", [], kind="abstain"))
        r.append(routed("ref-none", [], "L4", kind="abstain"))
        gate = routing_gate(b, r)
        self.assertEqual((gate["tickets"], gate["compared"]), (30, 29))
        self.assert_insufficient(gate, "only 29 question(s)")

    def test_no_routed_exact_decision(self):
        b, r = scenario(exact=0)
        gate = routing_gate(b, r)
        self.assert_insufficient(gate, "no question was decided at L0-L2")
        self.assertEqual(gate["routed_exact"], 0)
        self.assertEqual(gate["wrong_exact"], {"k": 0, "n": 0, "ids": []})

    def test_an_l2_choice_is_not_an_exact_decision(self):
        b, r = scenario(exact=0)
        r = replace(r, tid(0), routed(tid(0), ["K0"], "L2", kind="question", fiches=["K0", "K1"]))
        self.assert_insufficient(routing_gate(b, r), "no question was decided at L0-L2")

    def test_an_exact_decision_only_on_an_excluded_question_is_no_evidence(self):
        # the reference a reviewer wrote the alias from is decided by that alias: tautological, not evidence
        b, r = scenario(n=31, exact=0)
        r = replace(r, tid(30), routed(tid(30), ["K30"], "L1", fiches=["K30"]))
        gate = routing_gate(b, r, exclude_ids=[tid(30)])
        self.assert_insufficient(gate, "every question decided at L0-L2 was seen by a reviewer")
        self.assertEqual((gate["routed_exact"], gate["routed_exact_unseen"]), (1, 0))
        # one unseen question decided exactly is enough evidence
        b, r = scenario(n=31, exact=1)
        r = replace(r, tid(30), routed(tid(30), ["K30"], "L1", fiches=["K30"]))
        self.assertEqual(routing_gate(b, r, exclude_ids=[tid(30)])["status"], "passed")

    def test_both_reasons(self):
        b, r = scenario(n=3, exact=0)
        self.assert_insufficient(routing_gate(b, r), "only 3 question(s)", "no question was decided at L0-L2")

    def test_nothing_measured(self):
        gate = routing_gate([], [])
        self.assert_insufficient(gate, "only 0 question(s)", "no question was decided at L0-L2")
        self.assertEqual((gate["tickets"], gate["compared"]), (0, 0))
        self.assertEqual(gate["right_first"]["base"], {"k": 0, "n": 0, "value": None, "ci95": None})
        self.assertEqual(gate["by_level"], {})

    def test_a_zero_minimum_still_needs_one_compared_question(self):
        b, r = scenario(n=1, exact=1)
        self.assertEqual(routing_gate(b, r, min_questions=0)["status"], "passed")
        b, r = scenario(n=1, exact=1)
        gate = routing_gate(b, r, exclude_ids=[tid(0)], min_questions=0)
        self.assert_insufficient(gate, "at least 1 are needed", "every question decided at L0-L2 was seen")


class GateExclusionTest(unittest.TestCase):
    def test_ids_or_keys_and_unknown_ids_ignored(self):
        b, r = scenario(n=32)
        gate = routing_gate(b, r, exclude_ids=("ref-gone", f"{CLIENT}/{tid(31)}", tid(30), tid(30)))
        self.assertEqual(gate["excluded"], ["T30", "T31"])
        self.assertEqual(gate["compared"], 30)
        self.assertEqual(gate["status"], "passed")

    def test_a_bare_string_is_one_id(self):
        b, r = scenario(n=31)
        gate = routing_gate(b, r, exclude_ids=tid(30))
        self.assertEqual(gate["excluded"], ["T30"])
        self.assertEqual(gate["compared"], 30)

    def test_a_key_of_another_client_excludes_nothing(self):
        b, r = scenario(n=31)
        self.assertEqual(routing_gate(b, r, exclude_ids=[f"client-t/{tid(30)}"])["excluded"], [])

    def test_none_and_generators(self):
        b, r = scenario()
        self.assertEqual(routing_gate(b, r, exclude_ids=None)["excluded"], [])
        self.assertEqual(routing_gate(b, r, exclude_ids=(x for x in [tid(0)]))["excluded"], ["T00"])


class GateMcNemarTest(unittest.TestCase):
    def test_discordant_counts_and_exact_p_value(self):
        b, r = scenario(n=40, exact=6)
        for i in range(6):  # the router gets right what today's engine missed
            b = replace(b, tid(i), base(tid(i), [f"K{i}"], kind="abstain", fiches=[f"K{i}"]))
        b = replace(b, tid(39), base(tid(39), ["K39"], kind="fiche", fiches=["KX"]))  # both wrong
        r = replace(r, tid(39), routed(tid(39), ["K39"], "L3", kind="question", fiches=["KX"]))
        gate = routing_gate(b, r)
        self.assertEqual(gate["status"], "passed", gate["reasons"])
        self.assertEqual(gate["mcnemar"]["base_only"], 0)
        self.assertEqual(gate["mcnemar"]["route_only"], 6)
        self.assertAlmostEqual(gate["mcnemar"]["p_value"], 0.03125)
        self.assertEqual(gate["mcnemar"]["p_value"], mcnemar_exact(0, 6))
        self.assertEqual((gate["right_first"]["base"]["k"], gate["right_first"]["route"]["k"]), (33, 39))

    def test_both_directions(self):
        b, r = scenario(n=30, exact=3)
        r = replace(r, tid(10), routed(tid(10), ["K10"], "L4", kind="abstain"))
        r = replace(r, tid(11), routed(tid(11), ["K11"], "L4", kind="abstain"))
        b = replace(b, tid(12), base(tid(12), ["K12"], kind="question", fiches=["KX", "K12"]))
        gate = routing_gate(b, r)
        self.assertEqual(gate["mcnemar"], {"base_only": 2, "route_only": 1, "p_value": mcnemar_exact(2, 1)})
        self.assertEqual(gate["status"], "failed")


class GateDeterminismTest(unittest.TestCase):
    def messy(self):
        b, r = scenario(n=34, exact=5)
        r = replace(r, tid(1), routed(tid(1), ["K1"], "L1", fiches=["K9"]))
        r = replace(r, tid(2), routed(tid(2), ["K2"], "L0", fiches=["K8"]))
        b.append(base("ref-none", [], kind="abstain"))
        r.append(routed("ref-none", [], "L2", fiches=["K3"]))
        return b, r

    def test_input_order_does_not_change_the_result(self):
        b, r = self.messy()
        expected = routing_gate(b, r, exclude_ids=[tid(33), tid(32)])
        rng = random.Random(7)
        for _ in range(5):
            b2, r2 = b[:], r[:]
            rng.shuffle(b2)
            rng.shuffle(r2)
            got = routing_gate(b2, r2, exclude_ids=[tid(32), tid(33)])
            self.assertEqual(json.dumps(got, sort_keys=True), json.dumps(expected, sort_keys=True))
        self.assertEqual(expected["wrong_exact"]["ids"], ["T01", "T02", "ref-none"])
        self.assertEqual(expected["excluded"], ["T32", "T33"])

    def test_inputs_are_not_modified(self):
        b, r = self.messy()
        before = copy.deepcopy((b, r))
        routing_gate(b, r, exclude_ids=[tid(0)], failures=1, degraded=1)
        self.assertEqual((b, r), before)

    def test_long_id_lists_are_sampled_in_reasons(self):
        b, r = scenario()
        r = [routed(x["ticket_id"], x["expected"], "L1", fiches=["KX"]) for x in r[:8]] + r[8:]
        gate = routing_gate(b, r)
        self.assertEqual(gate["wrong_exact"]["k"], 8)
        self.assertEqual(len(gate["wrong_exact"]["ids"]), 8, "every id is reported")
        self.assertTrue(any("T00, T01, T02, T03, T04 and 3 more" in reason for reason in gate["reasons"]))


if __name__ == "__main__":
    unittest.main()
