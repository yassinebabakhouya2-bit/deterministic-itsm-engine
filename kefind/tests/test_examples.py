"""Démo de bout en bout sur le petit jeu de données d'exemple (kefind/examples/) :
aucune dépendance externe, aucune LLM — juste les primitives de kecore et kefind."""

import unittest

from scoreboard.dataset import load_tickets

from kefind.engine import KefindEngine
from kefind.io import load_decomposed_jsonl

from .helpers import EXAMPLES


class ExamplesTest(unittest.TestCase):
    def setUp(self):
        clienta = load_decomposed_jsonl(EXAMPLES / "fiches" / "clienta.jsonl")
        clientb = load_decomposed_jsonl(EXAMPLES / "fiches" / "clientb.jsonl")
        self.engine = KefindEngine(fiches_by_client={"clienta": clienta, "clientb": clientb})
        self.tickets = {t.ticket_id: t for t in load_tickets(EXAMPLES / "tickets.jsonl")}

    def test_every_example_ticket_decides_without_error(self):
        for ticket in self.tickets.values():
            decision = self.engine.decide(ticket)
            self.assertIn(decision.kind, ("fiche", "question", "abstain"))
            self.assertIsNone(decision.error)

    def test_unambiguous_tickets_find_their_expected_fiche(self):
        for ticket_id in ("T-1", "T-2", "T-3", "T-4"):
            ticket = self.tickets[ticket_id]
            decision = self.engine.decide(ticket)
            self.assertEqual(decision.kind, "fiche", ticket_id)
            self.assertIn(decision.shown, ticket.expected, ticket_id)

    def test_the_unanswerable_ticket_does_not_show_a_fiche(self):
        ticket = self.tickets["T-7"]
        self.assertEqual(ticket.expected, [])
        decision = self.engine.decide(ticket)
        self.assertIsNone(decision.shown)

    def test_clientb_never_sees_clienta_s_fiches(self):
        ticket = self.tickets["T-6"]
        self.assertEqual(ticket.client, "clientb")
        decision = self.engine.decide(ticket)
        self.assertTrue(all(fid.startswith("KB004") for fid in decision.fiches), decision.fiches)


if __name__ == "__main__":
    unittest.main()
