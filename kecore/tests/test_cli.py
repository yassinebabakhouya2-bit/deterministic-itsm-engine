import contextlib
import io
import json
import unittest
from unittest import mock

from kecore import cli
from kecore.decompose import DecomposedFiche

from .helpers import DEMO_KB, OUTLOOK_ANSWER, FakeLLM, TempDirTestCase


def run_cli(*args):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main([str(a) for a in args])
    return code, out.getvalue(), err.getvalue()


class CliTest(TempDirTestCase):
    def test_decompose_without_llm_then_show(self):
        out = self.path("out")
        code, _, err = run_cli("decompose", DEMO_KB, "--client", "clienta", "--out-dir", out)
        self.assertEqual(code, 0, err)
        self.assertIn("0 guided, 5 citable, 1 info only; 19 of 19 steps on a verified extract.", err)
        for name in ("fiches.decomposed.jsonl", "report.md", "summary.json", "profile.json"):
            self.assertTrue((out / name).is_file(), name)
        report = (out / "report.md").read_text(encoding="utf-8")
        self.assertIn("every step on a verified extract: PASS", report)
        self.assertIn("KB0010008: referenced by KB0010002", report)
        code, printed, _ = run_cli("show", out / "fiches.decomposed.jsonl", "KB0010001")
        self.assertEqual(code, 0)
        self.assertIn("3. Si Outlook démarre en mode sans échec", printed)
        self.assertIn("on failure: go to step 4", printed)

    def test_record_then_replay(self):
        config = self.write("llm.json", json.dumps({"endpoint": "https://acct.openai.azure.com", "deployment": "gpt-4o"}))
        cache = self.path("cache")
        fake = FakeLLM({"KB0010001 - Outlook ne démarre plus": OUTLOOK_ANSWER})
        fake.model_id = "gpt-4o@acct.openai.azure.com"
        with mock.patch.object(cli.AzureOpenAIChat, "from_config", return_value=fake):
            code, _, err = run_cli("decompose", DEMO_KB, "--client", "clienta", "--llm-config", config,
                                   "--cache-dir", cache, "--out-dir", self.path("recorded"))
        self.assertEqual(code, 0, err)
        self.assertIn("1 guided", err)
        calls = len(fake.calls)
        # 6 fiches, first pass each; only KB0010001 finds steps, so only it gets a self-check 2nd pass.
        self.assertEqual(calls, 7)

        # The real Azure client is built, but replay never calls it.
        code, _, err = run_cli("decompose", DEMO_KB, "--client", "clienta", "--llm-config", config, "--replay",
                               "--cache-dir", cache, "--out-dir", self.path("replayed"))
        self.assertEqual(code, 0, err)
        self.assertIn("1 guided", err)
        summary = json.loads((self.path("replayed") / "summary.json").read_text(encoding="utf-8"))
        self.assertEqual((summary["llm"]["calls"], summary["llm"]["cached"]), (0, 7))
        first = (self.path("recorded") / "fiches.decomposed.jsonl").read_text(encoding="utf-8").splitlines()
        again = (self.path("replayed") / "fiches.decomposed.jsonl").read_text(encoding="utf-8").splitlines()
        self.assertEqual([DecomposedFiche.from_dict(json.loads(l)).steps for l in first],
                         [DecomposedFiche.from_dict(json.loads(l)).steps for l in again], "a replay gives the same steps")

    def test_import_fiches_and_errors(self):
        export = self.write("kb.csv", "Ref;Titre;Solution\nKB0010030;Écran noir;Rebranchez le câble.\n")
        fiches = self.path("fiches.jsonl")
        code, _, err = run_cli("import-fiches", export, "--client", "c", "--id-col", "ref", "--title-col", "titre",
                               "--body-col", "solution", "--out", fiches)
        self.assertEqual(code, 0, err)
        code, _, err = run_cli("decompose", fiches, "--client", "c", "--out-dir", self.path("o"))
        self.assertEqual(code, 0, err)
        code, _, err = run_cli("decompose", self.path("missing"), "--client", "c")
        self.assertEqual(code, 1)
        self.assertIn("error: not found", err)
        code, _, err = run_cli("decompose", DEMO_KB, "--client", "c", "--llm-config", self.path("nope.json"))
        self.assertEqual(code, 1)
        self.assertIn("LLM config not found", err)


if __name__ == "__main__":
    unittest.main()
