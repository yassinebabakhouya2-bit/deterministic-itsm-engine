import json
import unittest

from scoreboard.dataset import DatasetError
from scoreboard.importers import PHONE_RE, Scrubber, clean_text, import_tickets, normalize_header

from .helpers import TempDirTestCase

try:
    import openpyxl
except ImportError:  # optional dependency
    openpyxl = None


class CleanTextTest(unittest.TestCase):
    def test_html_is_flattened_with_line_breaks(self):
        raw = "<p>Bonjour,</p><p>Outlook&nbsp;ne d&eacute;marre plus<br>depuis ce matin.</p>"
        self.assertEqual(clean_text(raw), "Bonjour,\n\nOutlook ne démarre plus\ndepuis ce matin.")

    def test_plain_text_with_angle_brackets_is_left_alone(self):
        self.assertEqual(clean_text("Fichier > Options > Compléments"), "Fichier > Options > Compléments")
        self.assertEqual(clean_text("si x < 5 et y > 3"), "si x < 5 et y > 3")

    def test_blank_runs_collapse(self):
        self.assertEqual(clean_text("a\r\n\r\n\r\n  b   c \n"), "a\n\nb c")


class ScrubTest(unittest.TestCase):
    def test_phone_formats_are_masked(self):
        for number in (
            "06 12 34 56 78",
            "0612345678",
            "+212 6 12 34 56 78",
            "+212612345678",
            "00212612345678",
            "+33 (0)6 12 34 56 78",
            "05.37.12.34.56",
            "01-23-45-67-89",
        ):
            self.assertRegex(f"appeler le {number} svp", PHONE_RE, number)

    def test_codes_and_dates_are_not_phones(self):
        for text in ("0x80070005", "KB0012345", "01.10.2026", "192.168.1.10", "Event 4625", "INC0012345678"):
            self.assertIsNone(PHONE_RE.search(text), text)

    def test_scrubber_counts(self):
        scrub = Scrubber()
        out = scrub("Contact: jean.dupont@example.com ou 06 12 34 56 78, code 0x80070005")
        self.assertEqual(out, "Contact: [email] ou [phone], code 0x80070005")
        self.assertEqual(dict(scrub.counts), {"[email]": 1, "[phone]": 1})

    def test_extra_pattern(self):
        scrub = Scrubber([r"\bMAT\d{5}\b"])
        self.assertEqual(scrub("poste MAT12345"), "poste [masked]")
        with self.assertRaises(DatasetError):
            Scrubber(["("])


class ImportCsvTest(TempDirTestCase):
    def test_windows_1252_semicolon_export(self):
        content = (
            "Numéro;Objet;Description;Catégorie\r\n"
            "I-001;Outlook bloqué;<p>Outlook ne démarre plus<br>Tél : 06 12 34 56 78</p>;Messagerie\r\n"
            "I-002;VPN;Coupure toutes les 10 minutes, écrire à a.b@corp.ma;Réseau\r\n"
            "I-001;Doublon;doublon;Messagerie\r\n"
            "I-003;;;Divers\r\n"
            ";;;\r\n"
        )
        path = self.write("export.csv", content, encoding="cp1252")
        tickets, report = import_tickets(
            path, "client-s", ["objet", "DESCRIPTION"], id_col="numero", category_col="Categorie"
        )
        self.assertEqual([t.ticket_id for t in tickets], ["I-001", "I-002"])
        self.assertEqual(tickets[0].text, "Outlook bloqué\nOutlook ne démarre plus\nTél : [phone]")
        self.assertEqual(tickets[0].category, "Messagerie")
        self.assertIn("[email]", tickets[1].text)
        self.assertEqual(report.source["encoding"], "cp1252")
        self.assertEqual(report.source["delimiter"], ";")
        self.assertEqual((report.rows, report.duplicates, report.skipped_empty), (4, 1, 1))
        self.assertEqual(report.masked, {"[phone]": 1, "[email]": 1})
        self.assertTrue(all(t.client == "client-s" and not t.labeled for t in tickets))

    def test_comma_utf8_with_quoted_newlines_and_generated_ids(self):
        content = '﻿subject,body\n"Imprimante","Bourrage papier,\nétage 2"\n"Teams","Caméra noire"\n'
        path = self.write("export.csv", content)
        tickets, report = import_tickets(path, "clienta", ["subject", "body"], scrub=False)
        self.assertEqual([t.ticket_id for t in tickets], ["clienta-0001", "clienta-0002"])
        self.assertEqual(tickets[0].text, "Imprimante\nBourrage papier,\nétage 2")
        self.assertEqual(report.source["delimiter"], ",")

    def test_missing_column_lists_the_columns(self):
        path = self.write("export.csv", "Numero;Objet\n1;x\n")
        with self.assertRaisesRegex(DatasetError, r"text column 'Description' not found.*'Numero', 'Objet'"):
            import_tickets(path, "a", ["Description"])

    def test_limit(self):
        path = self.write("export.csv", "id;t\n1;a\n2;b\n3;c\n")
        tickets, _ = import_tickets(path, "a", ["t"], id_col="id", limit=2)
        self.assertEqual(len(tickets), 2)


class ImportJsonTest(TempDirTestCase):
    def test_golden_set_with_expected_paths(self):
        lines = [
            {"id": "q1", "question": "Comment configurer le VPN ?", "source": "kb/clienta/KB0010002.md"},
            {"id": "q2", "question": "Imprimante en bourrage", "source": ["Kbs/KB0010003.pdf", "KB0010013.pdf"]},
            {"id": "q3", "question": "Écran supplémentaire", "source": "none"},
            {"id": "q4", "question": "Pas encore étiqueté"},
        ]
        path = self.write("golden.jsonl", "\n".join(json.dumps(l, ensure_ascii=False) for l in lines) + "\n")
        tickets, report = import_tickets(
            path, "clienta", ["question"], id_col="id", expected_col="source", expected_transform="basename"
        )
        self.assertEqual([t.expected for t in tickets], [["KB0010002"], ["KB0010003", "KB0010013"], [], None])
        self.assertEqual(report.labeled, 3)


@unittest.skipIf(openpyxl is None, "openpyxl not installed")
class ImportXlsxTest(TempDirTestCase):
    def test_xlsx_numeric_ids_and_dates(self):
        import datetime as dt

        workbook = openpyxl.Workbook()
        sheet = workbook.active
        sheet.title = "Export"
        sheet.append(["N° ticket", "Date", "Résumé"])
        sheet.append([12345, dt.datetime(2026, 9, 30, 10, 5), "Mot de passe expiré"])
        sheet.append([12346.0, None, "Compte verrouillé"])
        path = self.path("export.xlsx")
        workbook.save(path)
        tickets, report = import_tickets(path, "client-s", ["resume"], id_col="n ticket", category_col="date")
        self.assertEqual([t.ticket_id for t in tickets], ["12345", "12346"])
        self.assertEqual(tickets[0].category, "2026-09-30 10:05:00")
        self.assertEqual(report.source["sheet"], "Export")


class HeaderTest(unittest.TestCase):
    def test_normalize(self):
        self.assertEqual(normalize_header("  Numéro_du   Ticket "), "numero du ticket")


if __name__ == "__main__":
    unittest.main()
