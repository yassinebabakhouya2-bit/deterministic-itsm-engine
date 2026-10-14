import json
import unicodedata
import unittest

from kecore.decompose import DecomposedFiche, Step
from kecore.exclusion import (DEFAULT_MIN_CHARS, ExclusionRules, apply, exclusion_of, loose_key, normalize,
                              resolve_force_include)

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
                                          "fiche_ids": ["B", "D"], "forced": [], "force_include_unmatched": [],
                                          "matched_loosely": []})
        self.assertEqual(result.to_dict(), {"rules": ExclusionRules().to_dict(), "kept": 2,
                                            "excluded": [e.to_dict() for e in result.excluded], "forced": [],
                                            "force_include_unmatched": [], "matched_loosely": []})

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


def empty(fiche_id):
    """A fiche the rules exclude (no step, next to no body), its id a client-s document name."""
    return fiche(fiche_id, title=fiche_id, source=f"Kbs/{fiche_id}.docx", text="A compléter.", steps=0)


def kept_ids(result):
    return [f.fiche_id for f in result.kept]


class ForceIncludeTest(unittest.TestCase):
    """force_include names a fiche by its exact id; an entry that lost the id's spacing or case names the ONE
    fiche it can only be; every entry's outcome is reported (runbook 19.18 #5)."""

    def test_an_exact_entry_forces_the_fiche_back_in_and_is_reported_as_forced(self):
        result = apply([fiche("A"), empty("KB0233 -  Vide"), fiche("C")],
                       ExclusionRules(force_include=("KB0233 -  Vide",)))
        self.assertEqual(kept_ids(result), ["A", "KB0233 -  Vide", "C"])          # in the run's order
        self.assertEqual(result.excluded, [])
        self.assertEqual(result.forced, ["KB0233 -  Vide"])
        self.assertEqual((result.matched_loosely, result.force_include_unmatched), ([], []))
        self.assertEqual(result.stats()["forced"], ["KB0233 -  Vide"])
        self.assertEqual(result.to_dict()["forced"], ["KB0233 -  Vide"])

    def test_an_entry_copied_with_collapsed_spaces_names_its_fiche_and_is_reported(self):
        result = apply([fiche("A"), empty("KB0233 -  Vide")], ExclusionRules(force_include=("KB0233 - Vide",)))
        self.assertEqual(kept_ids(result), ["A", "KB0233 -  Vide"])
        self.assertEqual(result.excluded, [])
        self.assertEqual(result.forced, ["KB0233 -  Vide"])
        self.assertEqual(result.force_include_unmatched, [])
        loosely = [{"entry": "KB0233 - Vide", "fiche_id": "KB0233 -  Vide"}]
        self.assertEqual(result.matched_loosely, loosely)
        for report in (result.to_dict(), result.stats()):
            self.assertEqual((report["matched_loosely"], report["forced"]), (loosely, ["KB0233 -  Vide"]))
        self.assertEqual(result.to_dict()["rules"]["force_include"], ["KB0233 - Vide"])  # the config as written

    def test_case_tabs_non_breaking_and_edge_spaces_are_ignored_loosely(self):
        for fiche_id, entry in (("KB0120- LOCKED ACCOUNT", "kb0120- locked account"),
                                ("KB0233 - Associate a phone line", "KB0233 - Associate a phone line"),
                                ("KB0233 - Associate a phone line", "KB0233\t-  associate a PHONE line"),
                                ("KB0233 - Associate a phone line", "  KB0233 - Associate a phone line \n"),
                                ("KB0400 - Straße", "KB0400 - STRASSE")):
            with self.subTest(fiche_id=fiche_id, entry=entry):
                result = apply([empty(fiche_id)], ExclusionRules(force_include=(entry,)))
                self.assertEqual(kept_ids(result), [fiche_id])
                self.assertEqual(result.forced, [fiche_id])
                self.assertEqual(result.matched_loosely, [{"entry": entry, "fiche_id": fiche_id}])
                self.assertEqual(result.force_include_unmatched, [])

    def test_an_accent_stored_decomposed_matches_the_same_accent_typed_as_one_character(self):
        stored = unicodedata.normalize("NFD", "KB0300 - Réinitialiser")    # as a Mac document name holds it
        typed = unicodedata.normalize("NFC", "KB0300 - Réinitialiser")
        self.assertNotEqual(stored, typed)
        result = apply([empty(stored)], ExclusionRules(force_include=(typed,)))
        self.assertEqual(result.forced, [stored])
        self.assertEqual(result.matched_loosely, [{"entry": typed, "fiche_id": stored}])

    def test_a_different_character_is_never_matched_loosely(self):
        for fiche_id, entry in (("KB0233 – Vide", "KB0233 - Vide"),                # en dash typed as a hyphen
                                ("KB0300 - Réinitialiser", "KB0300 - Reinitialiser"),  # accent typed in ASCII
                                ("KB0233 - Vide", "KB0233 - Vide."),
                                ("KB0233 - Vide", "KB0233 -Vide"),                 # a space lost, not collapsed
                                ("KB0120- LOCKED ACCOUNT", "KB0120 - LOCKED ACCOUNT"),  # a space added
                                ("KB0233 - Vide", "KB0233 - Vide​")):         # a zero-width space is no space
            with self.subTest(fiche_id=fiche_id, entry=entry):
                result = apply([empty(fiche_id)], ExclusionRules(force_include=(entry,)))
                self.assertEqual(kept_ids(result), [])
                self.assertEqual([e.fiche_id for e in result.excluded], [fiche_id])
                self.assertEqual((result.forced, result.matched_loosely), ([], []))
                self.assertEqual(result.force_include_unmatched, [entry])

    def test_an_entry_two_fiches_match_loosely_is_a_guess_and_forces_nothing(self):
        result = apply([empty("KB0500 -  Vide"), empty("KB0500 - VIDE")],
                       ExclusionRules(force_include=("kb0500 - vide",)))
        self.assertEqual(kept_ids(result), [])
        self.assertEqual([e.fiche_id for e in result.excluded], ["KB0500 -  Vide", "KB0500 - VIDE"])
        self.assertEqual((result.forced, result.matched_loosely), ([], []))
        self.assertEqual(result.force_include_unmatched, ["kb0500 - vide"])

    def test_an_exact_entry_is_never_second_guessed_by_a_loose_match(self):
        result = apply([empty("KB0500 -  Vide"), empty("KB0500 - Vide")],
                       ExclusionRules(force_include=("KB0500 - Vide",)))
        self.assertEqual(kept_ids(result), ["KB0500 - Vide"])
        self.assertEqual([e.fiche_id for e in result.excluded], ["KB0500 -  Vide"])
        self.assertEqual(result.forced, ["KB0500 - Vide"])
        self.assertEqual((result.matched_loosely, result.force_include_unmatched), ([], []))

    def test_an_entry_that_names_no_fiche_is_reported_in_the_result_and_its_stats(self):
        result = apply([fiche("A"), empty("B")], ExclusionRules(force_include=("KB9999 - Inconnue", "", "  ")))
        self.assertEqual(kept_ids(result), ["A"])
        self.assertEqual([e.fiche_id for e in result.excluded], ["B"])
        unmatched = ["KB9999 - Inconnue", "", "  "]
        self.assertEqual(result.force_include_unmatched, unmatched)
        self.assertEqual(result.to_dict()["force_include_unmatched"], unmatched)
        self.assertEqual(result.stats()["force_include_unmatched"], unmatched)
        self.assertEqual(result.stats()["fiche_ids"], ["B"])

    def test_forced_lists_only_fiches_the_rules_would_have_excluded(self):
        result = apply([fiche("A"), empty("B")], ExclusionRules(force_include=("A", "b")))
        self.assertEqual(kept_ids(result), ["A", "B"])
        self.assertEqual(result.forced, ["B"])                    # "A" was kept by the rules: nothing forced
        self.assertEqual(result.matched_loosely, [{"entry": "b", "fiche_id": "B"}])
        self.assertEqual(result.force_include_unmatched, [])      # "A" names a fiche: not unmatched either
        placeholder = fiche("KB0266 - LIBRE - A REUTILISER", title="KB0266 - LIBRE - A REUTILISER")
        result = apply([placeholder], ExclusionRules(force_include=("KB0266 - Libre - A Reutiliser",)))
        self.assertEqual(result.forced, ["KB0266 - LIBRE - A REUTILISER"])  # a placeholder is forced like the rest

    def test_entries_are_reported_once_in_the_config_order_and_forced_fiches_in_the_run_order(self):
        rules = ExclusionRules(force_include=("x", "a 1", "z 1", "x", "a 1", "w", "Z  1"))
        result = apply([empty("Z  1"), empty("A  1"), fiche("K"), empty("Z  1")], rules)
        self.assertEqual(result.force_include_unmatched, ["x", "w"])
        self.assertEqual(result.matched_loosely, [{"entry": "a 1", "fiche_id": "A  1"},
                                                  {"entry": "z 1", "fiche_id": "Z  1"}])
        self.assertEqual(result.forced, ["Z  1", "A  1"])
        self.assertEqual(kept_ids(result), ["Z  1", "A  1", "K", "Z  1"])

    def test_resolution_does_not_depend_on_the_order_fiches_come_in(self):
        ids = ["KB0500 -  Vide", "KB0500 - VIDE", "KB0233 -  Vide", "KB0120- LOCKED ACCOUNT"]
        entries = ["kb0500 - vide", "KB0233 - Vide", "KB0120 - LOCKED ACCOUNT", "KB0120- LOCKED ACCOUNT"]
        expected = ({"KB0233 -  Vide", "KB0120- LOCKED ACCOUNT"},
                    [{"entry": "KB0233 - Vide", "fiche_id": "KB0233 -  Vide"}],
                    ["kb0500 - vide", "KB0120 - LOCKED ACCOUNT"])
        self.assertEqual(resolve_force_include(entries, ids), expected)
        self.assertEqual(resolve_force_include(entries, list(reversed(ids))), expected)
        self.assertEqual(resolve_force_include([], ids), (set(), [], []))
        self.assertEqual(resolve_force_include(entries, []), (set(), [], entries))

    def test_the_report_is_deterministic_json_and_a_copy(self):
        fiches = [fiche("A"), empty("KB0233 -  Vide"), empty("KB0300 – Réinitialiser")]
        rules = ExclusionRules(force_include=("KB0233 - Vide", "KB0300 - Reinitialiser"))
        dumps = {json.dumps(apply(list(fiches), rules).to_dict(), sort_keys=True, ensure_ascii=False)
                 for _ in range(3)}
        self.assertEqual(len(dumps), 1)
        report = json.loads(dumps.pop())
        self.assertEqual(report["force_include_unmatched"], ["KB0300 - Reinitialiser"])
        self.assertEqual([e["fiche_id"] for e in report["excluded"]], ["KB0300 – Réinitialiser"])
        result = apply(list(fiches), rules)
        result.to_dict()["matched_loosely"][0]["entry"] = "changed"
        result.stats()["forced"].append("changed")
        self.assertEqual(result.matched_loosely[0]["entry"], "KB0233 - Vide")
        self.assertEqual(result.forced, ["KB0233 -  Vide"])

    def test_exclusion_of_alone_matches_force_include_exactly(self):
        rules = ExclusionRules(force_include=("KB0233 - Vide",))
        self.assertIsNotNone(exclusion_of(empty("KB0233 -  Vide"), rules))   # the loose match needs the whole run
        self.assertIsNone(exclusion_of(empty("KB0233 - Vide"), rules))

    def test_loose_key_collapses_whitespace_and_folds_case_only(self):
        self.assertEqual(loose_key("  KB0233 - \tAssociate  A Phone\nLine "), "kb0233 - associate a phone line")
        self.assertEqual(loose_key(unicodedata.normalize("NFD", "RÉSEAU")), "réseau")
        self.assertEqual(loose_key(""), "")
        self.assertNotEqual(loose_key("KB – Vide"), loose_key("KB - Vide"))
        self.assertNotEqual(loose_key("Réseau"), loose_key("Reseau"))


if __name__ == "__main__":
    unittest.main()
