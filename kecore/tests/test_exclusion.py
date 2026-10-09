import unittest

from kecore.decompose import DecomposedFiche, Step
from kecore.exclusion import DEFAULT_MIN_CHARS, ExclusionRules, apply, exclusion_of, normalize

PROSE = "Pour attribuer une ligne, ouvrez le centre d'administration et suivez la procédure décrite. " * 4


def fiche(fiche_id="KB0001", title="Associate a phone line", source="Kbs/KB0001 - Associate a phone line.docx",
          text=PROSE, steps=1):
    return DecomposedFiche(
        fiche_id=fiche_id, client="c", title=title, source=source, text=text, text_sha256="0" * 64,
        status="guided" if steps else "info_only", confidence="high", reasons=[], sections=[],
        steps=[Step(n=i + 1, start=0, end=5, text=f"Etape {i + 1}", kind="action", role="fix") for i in range(steps)],
        entities=[], references=[], methods={}, checks={})


class ExclusionTest(unittest.TestCase):
    def test_a_placeholder_is_excluded_whatever_its_spelling(self):
        for title, source in (("KB0266 - LIBRE - A REUTILISER", "Kbs/KB0266.docx"),
                              ("KB0266", "Kbs/KB0266 - Libre à réutiliser.docx"),
                              ("KB0266", "Kbs/kb0266_libre_a_reutiliser.pdf"),
                              ("Fiche ancienne - NE PAS UTILISER", "Kbs/x.docx")):
            why = exclusion_of(fiche(title=title, source=source), ExclusionRules())
            self.assertIsNotNone(why, title + source)
            self.assertEqual(why.rule, "title_pattern")

    def test_patterns_match_whole_words_only(self):
        rules = ExclusionRules(title_patterns=("LIBRE",))
        self.assertIsNone(exclusion_of(fiche(title="Installer LibreOffice"), rules))
        self.assertIsNotNone(exclusion_of(fiche(title="Numéro libre"), rules))
        self.assertIsNone(exclusion_of(fiche(title="Réutiliser un poste"), ExclusionRules()))

    def test_the_default_patterns_leave_real_titles_alone(self):
        for title in ("Espace disque libre insuffisant", "Supprimer les comptes obsolètes", "Associate a phone line"):
            self.assertIsNone(exclusion_of(fiche(title=title), ExclusionRules()), title)

    def test_an_empty_fiche_is_excluded_but_a_fiche_in_prose_is_kept(self):
        why = exclusion_of(fiche(text="Voir avec l'équipe.", steps=0), ExclusionRules())
        self.assertEqual(why.rule, "empty")
        self.assertIn(str(DEFAULT_MIN_CHARS), why.detail)
        self.assertIsNone(exclusion_of(fiche(steps=0), ExclusionRules()))           # long prose, no step: citable
        self.assertIsNone(exclusion_of(fiche(text="Court.", steps=2), ExclusionRules()))  # short but has steps

    def test_a_short_fiche_of_plain_information_is_kept_and_a_title_alone_is_empty(self):
        policy = fiche(title="KB0010020 - Politique des mots de passe", steps=0,
                       text="# KB0010020 - Politique des mots de passe\n\nLes mots de passe doivent contenir 12 "
                            "caractères minimum et sont valables 90 jours.")
        self.assertIsNone(exclusion_of(policy, ExclusionRules()))
        title_only = fiche(title="KB0233 - Associate a phone line in the Teams admin center for a new user", steps=0,
                           text="KB0233 - Associate a phone line in the Teams admin center for a new user")
        self.assertEqual(exclusion_of(title_only, ExclusionRules()).rule, "empty")

    def test_a_forced_fiche_is_always_kept(self):
        rules = ExclusionRules(force_include=("KB0266",))
        self.assertIsNone(exclusion_of(fiche(fiche_id="KB0266", title="LIBRE - A REUTILISER", text="", steps=0), rules))

    def test_apply_keeps_order_and_lists_every_exclusion(self):
        fiches = [fiche("A"), fiche("B", title="B - A REUTILISER"), fiche("C"), fiche("D", text="x", steps=0)]
        result = apply(fiches)
        self.assertEqual([f.fiche_id for f in result.kept], ["A", "C"])
        self.assertEqual([(e.fiche_id, e.rule) for e in result.excluded], [("B", "title_pattern"), ("D", "empty")])
        self.assertEqual(result.stats(), {"kept": 2, "excluded": 2, "by_rule": {"title_pattern": 1, "empty": 1},
                                          "fiche_ids": ["B", "D"]})

    def test_config_is_validated_and_missing_keys_keep_their_default(self):
        self.assertEqual(ExclusionRules.from_dict({}), ExclusionRules())
        self.assertEqual(ExclusionRules.from_dict({"min_chars": 50}).min_chars, 50)
        self.assertEqual(ExclusionRules.from_dict({"title_patterns": ["", "  ", "OBSOLETE"]}).title_patterns,
                         ("OBSOLETE",))
        for bad in ({"title_patterns": "LIBRE"}, {"min_chars": -1}, {"min_chars": True}, {"force_include": "KB1"}):
            with self.assertRaises(ValueError, msg=str(bad)):
                ExclusionRules.from_dict(bad)

    def test_normalize_ignores_case_accents_and_punctuation(self):
        self.assertEqual(normalize("Libre — à réutiliser!"), " LIBRE A REUTILISER ")


if __name__ == "__main__":
    unittest.main()
