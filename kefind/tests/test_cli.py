import contextlib
import io
import unittest

from kefind import cli

from .helpers import EXAMPLES

FICHES = EXAMPLES / "fiches" / "clienta.jsonl"
FICHES_B = EXAMPLES / "fiches" / "clientb.jsonl"
TICKETS = EXAMPLES / "tickets.jsonl"


def run_cli(*args):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main([str(a) for a in args])
    return code, out.getvalue(), err.getvalue()


class CliTest(unittest.TestCase):
    def test_decide_prints_a_fiche_decision(self):
        code, out, err = run_cli(
            "decide", "--ticket", "Outlook reste bloqué au démarrage, erreur 0x80070005.",
            "--client", "clienta", "--fiches", FICHES,
        )
        self.assertEqual(code, 0, err)
        self.assertIn("décision : fiche", out)
        self.assertIn("KB0030001", out)

    def test_decide_isolates_clients(self):
        code, out, err = run_cli(
            "decide", "--ticket", "Outlook reste bloqué au démarrage, erreur 0x80070005.",
            "--client", "clientb", "--fiches", FICHES_B,
        )
        self.assertEqual(code, 0, err)
        self.assertIn("KB0040001", out)
        self.assertNotIn("KB0030001", out)

    def test_decide_on_an_ambiguous_ticket_asks_a_question(self):
        code, out, err = run_cli(
            "decide", "--ticket", "L'application reste bloquée au démarrage, erreur 0x80070005.",
            "--client", "clienta", "--fiches", FICHES,
        )
        self.assertEqual(code, 0, err)
        self.assertIn("décision : question", out)
        self.assertIn("question :", out)

    def test_decide_on_an_unrelated_ticket_abstains(self):
        code, out, err = run_cli(
            "decide", "--ticket", "Le clavier du poste ne répond plus du tout depuis ce matin.",
            "--client", "clienta", "--fiches", FICHES,
        )
        self.assertEqual(code, 0, err)
        self.assertIn("décision : abstain", out)

    def test_missing_fiches_file_is_a_clean_error(self):
        code, out, err = run_cli(
            "decide", "--ticket", "peu importe", "--client", "clienta", "--fiches", EXAMPLES / "missing.jsonl",
        )
        self.assertEqual(code, 1)
        self.assertIn("erreur :", err)

    def test_calibrate_runs_end_to_end(self):
        code, out, err = run_cli(
            "calibrate", "--tickets", TICKETS, "--client", "clienta", "--fiches", FICHES, "--max-wrong", "0.3",
        )
        self.assertEqual(code, 0, err)
        self.assertIn("seuil", out)

    def test_no_command_prints_help(self):
        code, out, err = run_cli()
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
