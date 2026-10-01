import unittest

from kefind.decide import Thresholds, decide

from .helpers import decompose_fiche


def _result(fiche, score, matched=None):
    from kefind.search import SearchResult

    return SearchResult(fiche.fiche_id, fiche, score, score, score, 0.0, matched or [])


OUTLOOK_FICHE = decompose_fiche("KBO", "clienta", "Outlook ne démarre plus",
                                 "## Résolution\n1. Fermez Outlook.\n2. Relancez Outlook.\n")
TEAMS_FICHE = decompose_fiche("KBT", "clienta", "Teams ne démarre plus",
                               "## Résolution\n1. Fermez Teams.\n2. Relancez Teams.\n")
CAMERA_FICHE = decompose_fiche("KBC", "clienta", "Caméra noire", "## Résolution\n1. Redémarrez la caméra.\n")

OUTLOOK_FICHE.entities = [{"canonical": "app:outlook", "kind": "app", "count": 1}]
TEAMS_FICHE.entities = [{"canonical": "app:teams", "kind": "app", "count": 1}]


class ThresholdsTest(unittest.TestCase):
    def test_from_dict_rejects_unknown_keys(self):
        with self.assertRaises(ValueError):
            Thresholds.from_dict({"min_score": 0.2, "nope": 1})

    def test_from_dict_defaults(self):
        self.assertEqual(Thresholds.from_dict(None), Thresholds())
        self.assertEqual(Thresholds.from_dict({"gap": 0.3}).min_score, Thresholds().min_score)


class DecideTest(unittest.TestCase):
    def setUp(self):
        self.thresholds = Thresholds(min_score=0.2, gap=0.15)

    def test_no_candidates_abstains(self):
        self.assertEqual(decide([], self.thresholds).kind, "abstain")

    def test_best_below_floor_abstains(self):
        outcome = decide([_result(OUTLOOK_FICHE, 0.1)], self.thresholds)
        self.assertEqual(outcome.kind, "abstain")

    def test_single_candidate_above_floor_is_a_fiche(self):
        outcome = decide([_result(OUTLOOK_FICHE, 0.5)], self.thresholds)
        self.assertEqual(outcome.kind, "fiche")
        self.assertEqual(outcome.top.fiche_id, "KBO")

    def test_clear_leader_is_a_fiche(self):
        outcome = decide([_result(OUTLOOK_FICHE, 0.8), _result(TEAMS_FICHE, 0.5)], self.thresholds)
        self.assertEqual(outcome.kind, "fiche")
        self.assertEqual(outcome.top.fiche_id, "KBO")

    def test_close_candidates_ask_a_question(self):
        outcome = decide([_result(OUTLOOK_FICHE, 0.60), _result(TEAMS_FICHE, 0.55)], self.thresholds)
        self.assertEqual(outcome.kind, "question")
        self.assertEqual({c.fiche_id for c in outcome.close}, {"KBO", "KBT"})
        self.assertIn("outlook", outcome.question)
        self.assertIn("teams", outcome.question)

    def test_question_uses_title_fallback_when_entities_do_not_discriminate(self):
        outcome = decide([_result(OUTLOOK_FICHE, 0.60), _result(CAMERA_FICHE, 0.57)], self.thresholds)
        self.assertEqual(outcome.kind, "question")
        self.assertIn(CAMERA_FICHE.title, outcome.question)

    def test_gap_is_measured_against_the_top_score_not_the_previous_one(self):
        # KBO et KBT sont proches de KBO (<gap) mais KBT et KBC aussi proches entre eux :
        # "close" doit rester mesuré contre le haut du classement, pas en chaîne.
        far = _result(CAMERA_FICHE, 0.30)
        outcome = decide([_result(OUTLOOK_FICHE, 0.60), _result(TEAMS_FICHE, 0.50), far], self.thresholds)
        self.assertEqual(outcome.kind, "question")
        self.assertNotIn(far, outcome.close)


if __name__ == "__main__":
    unittest.main()
