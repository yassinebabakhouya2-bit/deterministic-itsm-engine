import contextlib
import csv
import io
import json
import unittest

from scoreboard.cli import main, parse_rate
from scoreboard.dataset import load_tickets

from .helpers import EXAMPLES, TempDirTestCase


def run_cli(*args):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main([str(a) for a in args])
    return code, out.getvalue(), err.getvalue()


class ParseRateTest(unittest.TestCase):
    def test_forms(self):
        self.assertEqual(parse_rate("5%"), 0.05)
        self.assertEqual(parse_rate("0.05"), 0.05)
        self.assertEqual(parse_rate("2"), 0.02)
        self.assertEqual(parse_rate("2,5 %"), 0.025)


class EndToEndTest(TempDirTestCase):
    def test_import_label_run_report(self):
        export = self.write(
            "export.csv",
            "Numéro;Objet;Description\n"
            "101;Outlook;Outlook ne démarre plus depuis la mise à jour\n"
            "102;Écran;Demande d'un second écran\n"
            "103;Excel;Excel plante à l'ouverture d'un fichier du partage\n",
            encoding="cp1252",
        )
        tickets = self.path("work/tickets.jsonl")
        code, _, err = run_cli("import-tickets", export, "--client", "clienta", "--id-col", "Numéro",
                               "--text-col", "Objet", "--text-col", "Description", "--out", tickets)
        self.assertEqual(code, 0, err)
        self.assertIn("Imported 3 tickets", err)

        fixture = self.write(
            "engine.json",
            json.dumps(
                {
                    "name": "fixture-a",
                    "decisions": {
                        "clienta/101": {"kind": "fiche", "fiches": ["KB1"], "score": 2.0},
                        "clienta/102": {"kind": "fiche", "fiches": ["KB7"], "score": 0.4},
                        "clienta/103": [{"kind": "fiche", "fiches": ["KB3"], "score": 1.5}, {"kind": "abstain"}],
                    },
                    "candidates": {"clienta/101": [{"id": "KB1", "title": "Outlook gelé"}], "clienta/103": ["KB9", "KB3"]},
                }
            ),
        )
        sheet = self.path("work/labels.csv")
        code, _, err = run_cli("prepare-labels", tickets, "--out", sheet, "--engine", "fixture", "--engine-config", fixture)
        self.assertEqual(code, 0, err)
        with sheet.open(encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.reader(handle, delimiter=";"))
        self.assertEqual(len(rows), 4)
        column = rows[0].index("expected")
        for row, label in zip(rows[1:], ["1", "none", "2"]):
            row[column] = label
        with sheet.open("w", encoding="utf-8-sig", newline="") as handle:
            csv.writer(handle, delimiter=";").writerows(rows)

        labeled = self.path("work/labeled.jsonl")
        code, _, err = run_cli("apply-labels", sheet, "--tickets", tickets, "--out", labeled)
        self.assertEqual(code, 0, err)
        self.assertEqual([t.expected for t in load_tickets(labeled)], [["KB1"], [], ["KB3"]])

        code, out, _ = run_cli("validate", labeled)
        self.assertEqual(code, 0)
        self.assertIn("labeled        3", out)

        results = self.path("work/results")
        code, _, err = run_cli("run", labeled, "--engine", "fixture", "--engine-config", fixture,
                               "--runs", 3, "--out-dir", results)
        self.assertEqual(code, 0, err)
        self.assertTrue((results / "fixture-a.results.jsonl").is_file())
        manifest = json.loads((results / "fixture-a.manifest.json").read_text(encoding="utf-8"))
        self.assertEqual((manifest["tickets"], manifest["runs"]), (3, 3))
        self.assertEqual(len(manifest["dataset_sha256"]), 64)

        report = self.path("work/report.md")
        summary = self.path("work/summary.json")
        code, _, err = run_cli("report", str(results) + "/*.results.jsonl", "--tickets", labeled,
                               "--out", report, "--json", summary, "--max-wrong", "10%")
        self.assertEqual(code, 0, err)
        text = report.read_text(encoding="utf-8")
        self.assertIn("| fixture-a | 100.0% (", text)
        self.assertIn("## Abstention threshold", text)
        self.assertIn("No threshold can be proven within 10% on 3 tickets", text)
        self.assertIn("takes at least 35 tickets", text)
        self.assertIn("Demande d'un second écran", text)
        data = json.loads(summary.read_text(encoding="utf-8"))
        engine = data["engines"]["fixture-a"]
        self.assertEqual(engine["exact"]["k"], 2)
        self.assertEqual(engine["stability"]["k"], 2)

    def test_demo_files_compare_two_engines(self):
        results = self.path("results")
        for config in ("demo.standard.json", "demo.careful.json"):
            code, _, err = run_cli("run", EXAMPLES / "demo.tickets.jsonl", "--engine", "fixture",
                                   "--engine-config", EXAMPLES / config, "--runs", 2, "--out-dir", results)
            self.assertEqual(code, 0, err)
        code, _, err = run_cli("report", results / "demo-standard.results.jsonl", results / "demo-careful.results.jsonl",
                               "--tickets", EXAMPLES / "demo.tickets.jsonl")
        self.assertEqual(code, 0, err)
        text = (results / "report.md").read_text(encoding="utf-8")
        self.assertIn("## Head to head", text)
        self.assertIn("demo-standard vs demo-careful", text)
        self.assertIn("USD 0.0", text)
        self.assertIn("6 labeled tickets: 5 with a fiche, 1 without", text)

    def test_per_client_runs_keep_separate_files_and_flag_different_ticket_files(self):
        results = self.path("results")
        other = self.write("other.jsonl", '{"ticket_id": "X1", "client": "clientb", "text": "VPN", "expected": ["KB1"]}\n')
        code, _, err = run_cli("run", EXAMPLES / "demo.tickets.jsonl", "--engine", "fixture", "--engine-config",
                               EXAMPLES / "demo.standard.json", "--client", "clienta", "--runs", 1, "--out-dir", results)
        self.assertEqual(code, 0, err)
        code, _, err = run_cli("run", other, "--engine", "fixture", "--engine-config", EXAMPLES / "demo.standard.json",
                               "--client", "clientb", "--runs", 1, "--out-dir", results)
        self.assertEqual(code, 0, err)
        names = sorted(p.name for p in results.glob("*.results.jsonl"))
        self.assertEqual(names, ["demo-standard.clienta.results.jsonl", "demo-standard.clientb.results.jsonl"])
        code, _, err = run_cli("report", str(results) + "/*.results.jsonl")
        self.assertEqual(code, 0, err)
        text = (results / "report.md").read_text(encoding="utf-8")
        self.assertIn("7 labeled tickets", text)
        self.assertIn("2 ticket files", text)
        self.assertIn("different ticket files", text)

    def test_errors_are_short_messages(self):
        export = self.write("export.csv", "a;b\n1;x\n")
        code, _, err = run_cli("import-tickets", export, "--client", "c", "--text-col", "missing", "--out", self.path("t.jsonl"))
        self.assertEqual(code, 1)
        self.assertIn("error: text column 'missing' not found", err)
        code, _, err = run_cli("report", self.path("nothing/*.results.jsonl"))
        self.assertEqual(code, 1)
        self.assertIn("no file matches", err)
        code, _, _ = run_cli()
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
