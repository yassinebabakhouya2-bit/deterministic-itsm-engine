import unittest

from scoreboard.dataset import Ticket
from scoreboard.engines import load_engine

from kefind.decide import Thresholds
from kefind.engine import KefindEngine, factory

from .helpers import decompose_fiche

OUTLOOK_A = decompose_fiche("KB001", "clienta", "Outlook ne démarre plus",
                             "## Symptôme\nOutlook reste bloqué, erreur 0x80070005.\n\n"
                             "## Résolution\n1. Fermez Outlook.\n2. Relancez Outlook.\n")
PRINTER_A = decompose_fiche("KB002", "clienta", "Imprimante en bourrage",
                             "## Résolution\n1. Ouvrez le capot et retirez le papier.\n2. Redémarrez l'imprimante.\n")
OUTLOOK_B = decompose_fiche("KB901", "clientb", "Outlook ne démarre plus",
                             "## Symptôme\nOutlook reste bloqué, erreur 0x80070005.\n\n"
                             "## Résolution\n1. Réparez le profil Outlook.\n2. Relancez Outlook.\n")


def ticket(client: str, text: str, ticket_id: str = "T-1") -> Ticket:
    return Ticket(ticket_id=ticket_id, client=client, text=text, expected=None)


class KefindEngineTest(unittest.TestCase):
    def setUp(self):
        self.engine = KefindEngine(fiches_by_client={"clienta": [OUTLOOK_A, PRINTER_A], "clientb": [OUTLOOK_B]})

    def test_decide_returns_the_expected_fiche(self):
        decision = self.engine.decide(ticket("clienta", "Outlook reste bloqué au démarrage, erreur 0x80070005."))
        self.assertEqual(decision.kind, "fiche")
        self.assertEqual(decision.shown, "KB001")
        self.assertIsNotNone(decision.score)

    def test_candidates_are_scoped_to_the_ticket_s_client(self):
        pairs = self.engine.candidates(ticket("clienta", "Outlook ne démarre plus"), k=5)
        self.assertIn("KB001", [fid for fid, _ in pairs])
        self.assertNotIn("KB901", [fid for fid, _ in pairs])

    def test_isolation_between_clients_through_the_engine(self):
        decision_a = self.engine.decide(ticket("clienta", "Outlook reste bloqué, erreur 0x80070005.", "T-A"))
        decision_b = self.engine.decide(ticket("clientb", "Outlook reste bloqué, erreur 0x80070005.", "T-B"))
        self.assertEqual(decision_a.shown, "KB001")
        self.assertEqual(decision_b.shown, "KB901")

    def test_unknown_client_abstains_with_an_error(self):
        decision = self.engine.decide(ticket("client-inconnu", "quoi que ce soit"))
        self.assertEqual(decision.kind, "abstain")
        self.assertIsNotNone(decision.error)

    def test_write_trace_reports_citation_verification(self):
        decision = self.engine.decide(ticket("clienta", "Outlook reste bloqué au démarrage, erreur 0x80070005."))
        write_steps = [t for t in decision.trace if t.get("step") == "write"]
        self.assertEqual(len(write_steps), 1)
        self.assertIn("citation_verified", write_steps[0])


class FactoryTest(unittest.TestCase):
    def test_factory_builds_an_engine_from_inline_fiches(self):
        engine = factory({"fiches": {"clienta": [OUTLOOK_A.to_dict(), PRINTER_A.to_dict()]},
                           "thresholds": {"min_score": 0.1, "gap": 0.1}})
        self.assertIsInstance(engine, KefindEngine)
        decision = engine.decide(ticket("clienta", "Outlook reste bloqué, erreur 0x80070005."))
        self.assertEqual(decision.shown, "KB001")

    def test_factory_rejects_unknown_settings(self):
        with self.assertRaises(ValueError):
            factory({"not_a_real_setting": True})

    def test_loaded_through_scoreboard_s_load_engine(self):
        engine = load_engine("kefind.engine:factory", config={"fiches": {"clienta": [OUTLOOK_A.to_dict()]}})
        self.assertTrue(callable(engine.decide))
        self.assertTrue(engine.name)  # load_engine met le spec par défaut si l'engine n'en a pas

    def test_thresholds_from_config(self):
        engine = factory({"fiches": {"clienta": [OUTLOOK_A.to_dict(), PRINTER_A.to_dict()]},
                           "thresholds": {"min_score": 0.99}})
        self.assertEqual(engine.thresholds, Thresholds(min_score=0.99))


if __name__ == "__main__":
    unittest.main()
