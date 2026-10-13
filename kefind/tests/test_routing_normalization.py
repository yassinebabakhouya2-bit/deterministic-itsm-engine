import unittest

from kefind.routing import NORMALIZATION_VERSION, STOP_WORDS, fold_token, route_tokens, token_key


class RouteTokensTest(unittest.TestCase):
    def test_a_question_and_its_short_alias_meet(self):
        self.assertEqual(route_tokens("comment attribuer une ligne teams"), ("attribuer", "ligne", "team"))
        self.assertEqual(token_key("attribuer ligne Teams"), token_key("Comment attribuer une ligne TEAMS ?"))

    def test_accents_case_plural_and_feminine_fold_the_same_way(self):
        self.assertEqual(route_tokens("Mon compte est bloqué"), ("compte", "bloque"))
        self.assertEqual(route_tokens("Comptes BLOQUÉES"), ("compte", "bloque"))
        self.assertEqual(route_tokens("mots de passe"), route_tokens("mot de passe"))

    def test_ligatures_are_spelled_out(self):
        self.assertEqual(route_tokens("mise en œuvre"), ("mise", "oeuvre"))

    def test_negations_stay_tokens(self):
        self.assertIn("pas", route_tokens("teams ne fonctionne pas"))
        self.assertNotIn("ne", route_tokens("teams ne fonctionne pas"))

    def test_digits_and_single_characters(self):
        self.assertEqual(route_tokens("Windows 11 a b"), ("window", "11"))
        self.assertEqual(fold_token("2025"), "2025")

    def test_each_token_once_in_order(self):
        self.assertEqual(route_tokens("ligne teams ligne Teams"), ("ligne", "team"))

    def test_fold_rule(self):
        self.assertEqual([fold_token(t) for t in ("comptes", "acces", "access", "virus", "bloquee", "bus")],
                         ["compte", "acce", "access", "virus", "bloque", "bus"])

    def test_frozen(self):
        # changing the stop words or the rule changes what a person validated: bump the version instead
        self.assertEqual(NORMALIZATION_VERSION, 1)
        self.assertEqual(len(STOP_WORDS), 126)


if __name__ == "__main__":
    unittest.main()
