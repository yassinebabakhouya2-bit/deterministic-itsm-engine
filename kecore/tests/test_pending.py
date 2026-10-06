import unittest

from kecore.pending import PendingSynonyms, apply_decision, observe, ready_for_review
from kecore.profile import Profile



def empty_profile(client="c", dictionary=None):
    return Profile(client=client, dictionary=dictionary or {})


class PendingSynonymsTest(unittest.TestCase):
    def test_needs_more_than_one_observation(self):
        pending = PendingSynonyms(client="c")
        observe(pending, ["L'ERP Harmony ne répond plus."], profile=empty_profile())
        self.assertEqual(ready_for_review(pending, min_observations=2), [])
        self.assertEqual(pending.candidates["harmony"]["seen"], 1)

    def test_crosses_threshold_across_distinct_texts(self):
        pending = PendingSynonyms(client="c")
        texts = [
            "L'ERP Harmony ne répond plus pour l'utilisateur 1.",
            "Redémarrez l'application Harmony puis videz le cache.",
            "Le logiciel Harmony replante systématiquement.",
        ]
        observe(pending, texts, profile=empty_profile())
        ready = ready_for_review(pending, min_observations=3)
        self.assertEqual([key for key, _ in ready], ["harmony"])
        self.assertEqual(pending.candidates["harmony"]["seen"], 3)

    def test_repeated_mention_within_one_text_counts_once(self):
        pending = PendingSynonyms(client="c")
        text = "L'ERP Harmony... puis relancez l'application Harmony encore une fois."
        observe(pending, [text, text, text], profile=empty_profile())
        self.assertEqual(pending.candidates["harmony"]["seen"], 3)

    def test_known_term_is_skipped(self):
        pending = PendingSynonyms(client="c")
        profile = empty_profile(dictionary={"harmony": ["Harmony", "ERP Harmony"]})
        observe(pending, ["L'ERP Harmony ne répond plus."], profile=profile)
        self.assertEqual(pending.candidates, {})

    def test_accept_merges_into_profile_dictionary(self):
        pending = PendingSynonyms(client="c")
        profile = empty_profile()
        observe(pending, ["L'ERP Harmony ne répond plus.", "L'application Harmony replante.",
                           "Toujours Harmony qui plante."], profile=profile)
        key = next(iter(pending.candidates))
        apply_decision(pending, profile, key, accept=True)
        self.assertIn("harmony", profile.dictionary)
        self.assertIn("Harmony", profile.dictionary["harmony"])
        self.assertEqual(pending.candidates[key]["status"], "accepted")
        self.assertEqual(ready_for_review(pending, min_observations=1), [])

    def test_reject_is_never_proposed_again(self):
        pending = PendingSynonyms(client="c")
        profile = empty_profile()
        observe(pending, ["L'ERP Harmony ne répond plus."], profile=profile)
        key = next(iter(pending.candidates))
        apply_decision(pending, profile, key, accept=False)
        observe(pending, ["L'ERP Harmony encore.", "Harmony toujours en panne."], profile=profile)
        self.assertEqual(pending.candidates[key]["seen"], 1)  # frozen, decision stands
        self.assertNotIn("harmony", profile.dictionary)

    def test_deciding_twice_raises(self):
        pending = PendingSynonyms(client="c")
        profile = empty_profile()
        observe(pending, ["L'ERP Harmony ne répond plus."], profile=profile)
        key = next(iter(pending.candidates))
        apply_decision(pending, profile, key, accept=False)
        with self.assertRaises(ValueError):
            apply_decision(pending, profile, key, accept=True)

    def test_dict_roundtrip(self):
        pending = PendingSynonyms(client="c")
        observe(pending, ["L'ERP Harmony ne répond plus."], profile=empty_profile())
        restored = PendingSynonyms.from_dict(pending.to_dict())
        self.assertEqual(restored.to_dict(), pending.to_dict())

    def test_accept_with_explicit_canonical_folds_into_existing_entry(self):
        pending = PendingSynonyms(client="c")
        profile = empty_profile(dictionary={"harmony": ["Harmony"]})
        # a second spelling the known-term filter won't have skipped (different trigger word, different case)
        observe(pending, ["Le progiciel HarmonyPro bloque.", "Toujours le progiciel HarmonyPro.",
                           "Rien ne marche avec HarmonyPro."], profile=profile)
        key = next(iter(pending.candidates))
        apply_decision(pending, profile, key, accept=True, canonical="harmony")
        self.assertIn("HarmonyPro", profile.dictionary["harmony"])
        self.assertIn("Harmony", profile.dictionary["harmony"])


if __name__ == "__main__":
    unittest.main()
