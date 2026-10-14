import unittest

from kefind.routing import NORMALIZATION_VERSION, STOP_WORDS, alias_problem, fold_token, ignored_characters, route_tokens, token_key


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

    def test_an_alias_never_relies_on_a_character_routing_ignores(self):
        self.assertEqual(token_key("imprimante étage 3"), token_key("imprimante étage 4"))
        self.assertEqual(ignored_characters("imprimante étage 3 à l'accueil"), ["3"])
        self.assertIn("« 3 »", alias_problem("imprimante étage 3"))
        self.assertIn("« h »", alias_problem("mapper lecteur H"))
        self.assertEqual(alias_problem("mot de passe"), "moins de 2 mots utiles au routage" if len(route_tokens("mot de passe")) < 2 else None)
        self.assertEqual(alias_problem("comment bloquer"), "moins de 2 mots utiles au routage")
        self.assertIsNone(alias_problem("attribuer une ligne Teams"))

    def test_english_negative_contractions_keep_their_negation(self):
        self.assertEqual(route_tokens("I can't sign in to Teams"), ("not", "sign", "team"))
        self.assertEqual(token_key("can't sign in to Teams"), token_key("cannot sign in to Teams"))
        self.assertEqual(token_key("I can’t sign in to Teams"), token_key("can not sign in Teams"))
        self.assertNotEqual(token_key("can't sign in to Teams"), token_key("sign in to Teams"))
        self.assertIn("not", route_tokens("Outlook doesn't start"))
        self.assertIn("not", route_tokens("Outlook won't start"))

    def test_frozen(self):
        # changing the stop words or the rule changes what a person validated: bump the version instead
        self.assertEqual(NORMALIZATION_VERSION, 2)
        self.assertEqual(len(STOP_WORDS), 126)


if __name__ == "__main__":
    unittest.main()
