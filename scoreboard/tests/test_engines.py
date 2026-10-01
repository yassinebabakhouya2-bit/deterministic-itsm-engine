import json
import sys
import textwrap
import unittest

from scoreboard.dataset import Ticket
from scoreboard.engines import Decision, Usage, decision_from_dict, load_engine
from scoreboard.engines.fixture import FixtureEngine
from scoreboard.runner import expand_paths, load_results, make_record, manifest_path, run_engine

from .helpers import TempDirTestCase


class DecisionTest(unittest.TestCase):
    def test_validation(self):
        with self.assertRaises(ValueError):
            Decision("maybe")
        with self.assertRaises(ValueError):
            Decision("fiche", fiches=[])
        decision = Decision("fiche", fiches=["K1", None, " ", "K2"])
        self.assertEqual(decision.fiches, ["K1", "K2"])
        self.assertEqual(decision.shown, "K1")
        self.assertIsNone(Decision("abstain", fiches=["K1"]).shown)

    def test_from_dict(self):
        decision = decision_from_dict(
            {"kind": "question", "fiches": ["K1"], "question": "Wi-Fi ?", "score": "0.5", "usage": {"input_tokens": "10"}}
        )
        self.assertEqual(decision.kind, "question")
        self.assertEqual(decision.score, 0.5)
        self.assertEqual(decision.usage, Usage(input_tokens=10))
        with self.assertRaises(TypeError):
            decision_from_dict("fiche")


class LoadEngineTest(TempDirTestCase):
    def test_builtin_fixture(self):
        config = self.write("f.json", json.dumps({"name": "demo", "decisions": {"a/1": {"kind": "abstain"}}}))
        engine = load_engine("fixture", config)
        self.assertEqual(engine.name, "demo")
        self.assertEqual(engine.decide(Ticket("1", "a", "x")).kind, "abstain")

    def test_custom_factory(self):
        module_dir = self.path("plugins")
        module_dir.mkdir()
        (module_dir / "my_engine.py").write_text(
            textwrap.dedent(
                """
                class Always:
                    def __init__(self, config):
                        self.fiche = config.get("fiche", "K0")
                    def decide(self, ticket):
                        return {"kind": "fiche", "fiches": [self.fiche]}

                def build(config):
                    return Always(config)
                """
            ),
            encoding="utf-8",
        )
        sys.path.insert(0, str(module_dir))
        try:
            engine = load_engine("my_engine:build", config={"fiche": "K42"})
        finally:
            sys.path.remove(str(module_dir))
        self.assertEqual(engine.name, "my_engine:build")
        self.assertEqual(decision_from_dict(engine.decide(Ticket("1", "a", "x"))).shown, "K42")

    def test_unknown_engine_and_bad_config(self):
        with self.assertRaisesRegex(ValueError, "unknown engine"):
            load_engine("nope")
        with self.assertRaisesRegex(ValueError, "not valid JSON"):
            load_engine("fixture", self.write("bad.json", "{oops"))
        with self.assertRaisesRegex(ValueError, "not found"):
            load_engine("fixture", self.path("missing.json"))


class FixtureEngineTest(unittest.TestCase):
    def test_list_entries_cycle_per_run(self):
        engine = FixtureEngine({"a/1": [{"kind": "fiche", "fiches": ["K1"]}, {"kind": "abstain"}]})
        ticket = Ticket("1", "a", "x")
        self.assertEqual([engine.decide(ticket).kind for _ in range(3)], ["fiche", "abstain", "fiche"])
        self.assertEqual(engine.decide(Ticket("2", "a", "x")).kind, "abstain")


class Exploding:
    name = "exploding"

    def decide(self, ticket):
        if ticket.ticket_id == "2":
            raise RuntimeError("search service unreachable")
        return Decision("fiche", fiches=["K1"], score=1.0)


class RunnerTest(TempDirTestCase):
    def test_runs_continue_after_errors_and_skip_unlabeled(self):
        tickets = [Ticket("1", "a", "x", expected=["K1"]), Ticket("2", "a", "y", expected=["K2"]), Ticket("3", "a", "z")]
        out = self.path("out/exploding.results.jsonl")
        ticks = iter(range(100))
        records = run_engine(Exploding(), tickets, runs=2, out_path=out, clock=lambda: next(ticks))
        self.assertEqual(len(records), 4)
        self.assertEqual(records[1]["error"], "RuntimeError: search service unreachable")
        self.assertEqual(records[1]["kind"], "abstain")
        self.assertEqual(records[0]["latency_s"], 1)
        loaded = load_results([out])
        self.assertEqual(len(loaded["exploding"]), 4)
        self.assertEqual(manifest_path(out).name, "exploding.manifest.json")

    def test_record_keeps_titles_of_top_fiches_only(self):
        decision = Decision("fiche", fiches=[f"K{i}" for i in range(30)], titles={f"K{i}": f"T{i}" for i in range(30)})
        rec = make_record("e", 0, Ticket("1", "a", "x", expected=[]), decision)
        self.assertEqual(len(rec["fiches"]), 20)
        self.assertEqual(sorted(rec["titles"]), ["K0", "K1", "K2", "K3", "K4"])
        self.assertEqual(rec["expected"], [])

    def test_expand_paths_handles_globs(self):
        for name in ("b.results.jsonl", "a.results.jsonl"):
            self.write(f"res/{name}", "")
        paths = expand_paths([str(self.path("res")) + "/*.results.jsonl"])
        self.assertEqual([p.name for p in paths], ["a.results.jsonl", "b.results.jsonl"])
        with self.assertRaises(FileNotFoundError):
            expand_paths([str(self.path("res")) + "/*.nothing"])

    def test_load_results_rejects_other_files(self):
        path = self.write("x.results.jsonl", '{"hello": 1}\n')
        with self.assertRaisesRegex(ValueError, "missing 'engine'"):
            load_results([path])


if __name__ == "__main__":
    unittest.main()
