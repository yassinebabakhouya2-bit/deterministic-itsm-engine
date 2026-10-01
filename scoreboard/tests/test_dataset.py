import unittest

from scoreboard.dataset import (
    DatasetError,
    Ticket,
    describe,
    format_expected,
    load_tickets,
    parse_expected,
    save_tickets,
)

from .helpers import TempDirTestCase


class ParseExpectedTest(unittest.TestCase):
    def test_absent_or_blank_means_not_labeled(self):
        self.assertIsNone(parse_expected(None))
        self.assertIsNone(parse_expected(""))
        self.assertIsNone(parse_expected("   "))
        self.assertIsNone(parse_expected("|"))

    def test_none_words_mean_no_fiche(self):
        for word in ("none", "NONE", "Aucune", "aucun", "-", "n/a"):
            self.assertEqual(parse_expected(word), [], word)
        self.assertEqual(parse_expected([]), [])
        self.assertEqual(parse_expected(["none"]), [])

    def test_ids_are_split_trimmed_and_deduplicated(self):
        self.assertEqual(parse_expected("KB1| KB2 |KB1"), ["KB1", "KB2"])
        self.assertEqual(parse_expected([" KB1", "KB2", ""]), ["KB1", "KB2"])

    def test_none_mixed_with_ids_is_rejected(self):
        with self.assertRaises(DatasetError):
            parse_expected("KB1|none")
        with self.assertRaises(DatasetError):
            parse_expected(["KB1", "none"])

    def test_wrong_types_are_rejected(self):
        with self.assertRaises(DatasetError):
            parse_expected(5)
        with self.assertRaises(DatasetError):
            parse_expected(["KB1", 2])

    def test_format_is_the_inverse(self):
        for value in (None, [], ["KB1"], ["KB1", "KB2"]):
            self.assertEqual(parse_expected(format_expected(value)), value)


class TicketFileTest(TempDirTestCase):
    def test_round_trip_keeps_labels_and_their_absence(self):
        tickets = [
            Ticket("T-1", "clienta", "Outlook ne démarre plus", expected=["KB0010001"]),
            Ticket("T-2", "clienta", "Demande d'écran", expected=[]),
            Ticket("T-3", "client-s", "VPN coupe", category="Réseau"),
        ]
        path = self.path("tickets.jsonl")
        self.assertEqual(save_tickets(path, tickets), 3)
        loaded = load_tickets(path)
        self.assertEqual([t.to_dict() for t in loaded], [t.to_dict() for t in tickets])
        self.assertTrue(loaded[0].answerable)
        self.assertTrue(loaded[1].labeled and not loaded[1].answerable)
        self.assertFalse(loaded[2].labeled)
        self.assertIn("démarre", path.read_text(encoding="utf-8"))

    def test_duplicate_ticket_is_reported_with_its_line(self):
        path = self.write(
            "dup.jsonl",
            '{"ticket_id": "1", "client": "a", "text": "x"}\n\n{"ticket_id": "1", "client": "a", "text": "y"}\n',
        )
        with self.assertRaisesRegex(DatasetError, r"dup.jsonl:3: duplicate ticket a/1"):
            load_tickets(path)

    def test_same_id_for_two_clients_is_allowed(self):
        path = self.write(
            "two.jsonl",
            '{"ticket_id": 1, "client": "a", "text": "x"}\n{"ticket_id": 1, "client": "b", "text": "y"}\n',
        )
        self.assertEqual([t.key for t in load_tickets(path)], ["a/1", "b/1"])

    def test_missing_text_and_bad_json_are_reported(self):
        with self.assertRaisesRegex(DatasetError, "missing 'text'"):
            load_tickets(self.write("a.jsonl", '{"ticket_id": "1", "client": "a", "text": "  "}\n'))
        with self.assertRaisesRegex(DatasetError, "invalid JSON"):
            load_tickets(self.write("b.jsonl", "{not json}\n"))

    def test_describe_counts(self):
        tickets = [
            Ticket("1", "a", "x", expected=["K"]),
            Ticket("2", "a", "x", expected=[]),
            Ticket("3", "b", "x"),
        ]
        self.assertEqual(
            describe(tickets),
            {"tickets": 3, "labeled": 2, "with_fiche": 1, "without_fiche": 1, "unlabeled": 1, "by_client": {"a": 2, "b": 1}},
        )


if __name__ == "__main__":
    unittest.main()
