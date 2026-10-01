import unittest

from scoreboard.metrics import (
    Rate,
    calibrate,
    compare,
    mcnemar_exact,
    percentile,
    summarize,
    tickets_needed,
    wilson,
)
from scoreboard.pricing import Prices

from .helpers import record


class WilsonTest(unittest.TestCase):
    def test_known_values(self):
        low, high = wilson(0, 10)
        self.assertEqual(low, 0.0)
        self.assertAlmostEqual(high, 0.2775, places=4)
        low, high = wilson(5, 10)
        self.assertAlmostEqual(low, 0.2366, places=4)
        self.assertAlmostEqual(high, 0.7634, places=4)
        low, high = wilson(45, 50)
        self.assertAlmostEqual(low, 0.786, places=3)
        self.assertAlmostEqual(high, 0.957, places=3)
        self.assertEqual(wilson(0, 0), (0.0, 1.0))

    def test_tickets_needed(self):
        self.assertEqual(tickets_needed(0.05), 73)
        self.assertEqual(tickets_needed(0.02), 189)
        self.assertLessEqual(wilson(0, 73)[1], 0.05)
        self.assertGreater(wilson(0, 72)[1], 0.05)
        self.assertIsNone(tickets_needed(0))

    def test_rate(self):
        rate = Rate(3, 4)
        self.assertEqual(rate.value, 0.75)
        self.assertLess(rate.low, 0.75)
        self.assertGreater(rate.high, 0.75)
        self.assertIsNone(Rate(0, 0).value)


class PercentileTest(unittest.TestCase):
    def test_interpolation(self):
        self.assertIsNone(percentile([], 50))
        self.assertEqual(percentile([3.0], 95), 3.0)
        self.assertEqual(percentile([1, 2, 3, 4], 50), 2.5)
        self.assertAlmostEqual(percentile(list(range(1, 101)), 95), 95.05)


def two_runs():
    run0 = [
        record("1", ["K1"], fiches=["K1", "K9"], score=3.0),  # exact
        record("2", ["K2"], fiches=["K7", "K2"], score=1.0),  # wrong, but K2 in top 5
        record("3", ["K3", "K33"], fiches=["K33"], score=2.5),  # exact (second acceptable id)
        record("4", [], fiches=["K4"], score=0.5),  # wrong: no fiche covers it
        record("5", [], kind="abstain"),  # correct none
        record("6", ["K6"], kind="question", fiches=["K6"]),  # no fiche shown, K6 in recall
    ]
    run1 = [dict(r, run=1) for r in run0]
    run1[0] = record("1", ["K1"], fiches=["K9"], score=2.9, run=1)  # unstable
    return run0 + run1


class SummaryTest(unittest.TestCase):
    def test_definitions(self):
        s = summarize("e", two_runs(), Prices())
        self.assertEqual((s.tickets, s.with_fiche, s.without_fiche, s.runs), (6, 4, 2, 2))
        self.assertEqual((s.exact.k, s.exact.n), (2, 4))
        self.assertEqual((s.wrong_shown.k, s.wrong_shown.n), (2, 6))
        self.assertEqual((s.no_fiche.k, s.questions.k), (2, 1))
        self.assertEqual((s.correct_none.k, s.correct_none.n), (1, 2))
        self.assertEqual((s.recall_at_k.k, s.recall_at_k.n), (4, 4))
        self.assertEqual((s.stability.k, s.stability.n), (5, 6))
        self.assertEqual(s.exact_by_run, [0.5, 0.25])
        self.assertEqual(s.errors, 0)
        self.assertEqual(s.cost_per_ticket, 0.0)

    def test_errors_and_cost(self):
        records = [
            record("1", ["K1"], fiches=["K1"], usage={"input_tokens": 1_000_000, "output_tokens": 100_000}),
            record("2", ["K2"], kind="abstain", error="SearchError: boom", latency=9.0),
        ]
        s = summarize("e", records, Prices())
        self.assertEqual(s.errors, 1)
        self.assertEqual(s.error_samples, ["SearchError: boom"])
        self.assertAlmostEqual(s.cost_per_ticket, (2.5 + 1.0) / 2)
        self.assertEqual(s.latency_p95, 0.1, "failed calls do not count in latency")
        self.assertIsNone(s.stability)


class CompareTest(unittest.TestCase):
    def test_mcnemar(self):
        self.assertAlmostEqual(mcnemar_exact(8, 2), 0.109375)
        self.assertAlmostEqual(mcnemar_exact(5, 0), 0.0625)
        self.assertEqual(mcnemar_exact(0, 0), 1.0)
        self.assertEqual(mcnemar_exact(3, 3), 1.0)

    def test_paired_counts(self):
        a = [record("1", ["K1"], fiches=["K1"]), record("2", ["K2"], fiches=["K2"]), record("3", ["K3"], fiches=["X"]),
             record("4", [], kind="abstain")]
        b = [record("1", ["K1"], fiches=["K1"]), record("2", ["K2"], fiches=["X"]), record("3", ["K3"], fiches=["K3"]),
             record("9", ["K9"], fiches=["K9"])]
        c = compare("a", a, "b", b)
        self.assertEqual((c.tickets, c.both, c.first_only, c.second_only, c.neither), (3, 1, 1, 1, 0))
        self.assertEqual(c.p_value, 1.0)


class CalibrationTest(unittest.TestCase):
    def test_recommends_lowest_safe_threshold(self):
        records = [record(str(i), [f"K{i}"], fiches=[f"K{i}"], score=10.0 + i) for i in range(80)]
        records += [record("w1", ["Kw1"], fiches=["X"], score=1.0), record("w2", [], fiches=["Y"], score=2.0)]
        cal = calibrate(records, max_wrong=0.05)
        self.assertIsNone(cal.rows[0].threshold)
        self.assertEqual(cal.rows[0].wrong_shown.k, 2)
        self.assertEqual(cal.recommended.threshold, 10.0)
        self.assertEqual(cal.recommended.wrong_shown.k, 0)
        self.assertEqual(cal.recommended.exact.k, 80)
        self.assertEqual(cal.recommended.no_fiche.k, 2)

    def test_unreachable_ceiling_and_unscored_engines(self):
        records = [record(str(i), [f"K{i}"], fiches=[f"K{i}"], score=1.0) for i in range(10)]
        self.assertIsNone(calibrate(records, max_wrong=0.05).recommended)
        self.assertIsNone(calibrate([record("1", ["K"], fiches=["K"])], 0.05))


if __name__ == "__main__":
    unittest.main()
