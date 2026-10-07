import unittest

from kecore.fiches import Fiche, load_folder
from kecore.llm import LLMError
from kecore.profile import Profile, build_dictionary, build_profile, remove_boilerplate

from .helpers import DEMO_KB, FakeLLM, TempDirTestCase


def erp_kb(count=4, term="Harmony"):
    """Fiches that each mention a client-specific business app via a trigger word."""
    fiches = []
    for i in range(count):
        text = (
            f"# Fiche {i}\n\n## Constat\nL'ERP {term} ne répond plus pour l'utilisateur {i}.\n\n"
            f"## Pistes\n- Redémarrez l'application {term}.\n- Videz le cache.\n"
        )
        fiches.append(Fiche(f"KB00200{i:02d}", "c", f"Fiche {i}", text))
    return fiches


def custom_kb(count=4):
    fiches = []
    for i in range(count):
        text = (
            f"# Fiche {i}\n\n## Constat\nLe poste {i} ne démarre pas.\n\n## Pistes\n- Rebranchez le câble {i}.\n"
            f"- Redémarrez le poste.\n\nDocument interne, ne pas diffuser sans accord de la DSI."
        )
        fiches.append(Fiche(f"KB00100{i:02d}", "c", f"Fiche {i}", text))
    return fiches


class ProfileTest(TempDirTestCase):
    def test_demo_kb_profile(self):
        fiches, _ = load_folder(DEMO_KB, "clienta")
        profile = build_profile("clienta", fiches)
        self.assertEqual(profile.fiches, 6)
        self.assertEqual(profile.boilerplate, ["pour toute question, contactez le support au 05 22 12 34 56."])
        self.assertEqual(profile.headings["resolution"]["role"], "resolution")
        self.assertEqual(profile.styles, {"numbered": 2, "bulleted": 2, "prose": 2})
        self.assertTrue(profile.stable)

    def test_unknown_headings_are_mapped_once_by_the_llm(self):
        llm = FakeLLM({"__headings__": {"mappings": [{"heading": "Pistes", "role": "resolution"},
                                                     {"heading": "Constat", "role": "symptom"}]}})
        profile = build_profile("c", custom_kb(), llm=llm)
        self.assertEqual(llm.calls.count("heading_roles"), 1)
        self.assertEqual(profile.headings["pistes"], {"count": 4, "role": "resolution", "source": "llm", "example": "Pistes"})
        lookup = profile.role_lookup()
        self.assertEqual(lookup("pistes"), "resolution")
        self.assertEqual(lookup("resolution"), "resolution")
        self.assertIn("document interne, ne pas diffuser sans accord de la dsi.", profile.boilerplate)

    def test_llm_failure_leaves_headings_unmapped(self):
        llm = FakeLLM({"__headings__": LLMError("quota")})
        profile = build_profile("c", custom_kb(), llm=llm)
        self.assertEqual(profile.headings["pistes"]["source"], "unmapped")
        self.assertEqual(profile.llm_usage, {"error": "quota"})

    def test_unstable_profile_trusts_only_keywords(self):
        profile = Profile("c", headings={"pistes": {"count": 9, "role": "resolution", "source": "llm", "example": "Pistes"}},
                          stable=False)
        self.assertIsNone(profile.role_lookup()("pistes"))

    def test_dictionary_needs_more_than_one_fiche(self):
        # a term named in a single fiche is noise, not a client's vocabulary.
        dictionary, usage = build_dictionary(erp_kb(count=2), min_fiches=3)
        self.assertEqual(dictionary, {})
        self.assertEqual(usage, {})

    def test_dictionary_keeps_a_term_named_across_fiches(self):
        dictionary, _ = build_dictionary(erp_kb(count=4), min_fiches=3)
        self.assertIn("harmony", dictionary)
        self.assertIn("Harmony", dictionary["harmony"])

    def test_dictionary_ignores_known_products(self):
        # "application Outlook" should not create a duplicate entry for a name entities.py already knows.
        dictionary, _ = build_dictionary(erp_kb(count=4, term="Outlook"), min_fiches=3)
        self.assertEqual(dictionary, {})

    def test_dictionary_llm_clusters_spellings(self):
        llm = FakeLLM({"__dictionary__": {"products": [{"canonical": "Harmony", "aliases": ["Harmony", "ERP Harmony"]}]}})
        dictionary, usage = build_dictionary(erp_kb(count=4), llm=llm, min_fiches=3)
        self.assertEqual(llm.calls, ["software_dictionary"])
        self.assertEqual(dictionary, {"harmony": ["ERP Harmony", "Harmony"]})
        self.assertEqual(usage["input_tokens"], 1000)

    def test_dictionary_llm_failure_falls_back_to_deterministic(self):
        llm = FakeLLM({"__dictionary__": LLMError("quota")})
        dictionary, usage = build_dictionary(erp_kb(count=4), llm=llm, min_fiches=3)
        self.assertIn("harmony", dictionary)
        self.assertEqual(usage, {"error": "quota"})

    def test_build_profile_populates_the_dictionary(self):
        profile = build_profile("c", erp_kb(count=4), dictionary_min_fiches=3)
        self.assertIn("harmony", profile.dictionary)

    def test_build_profile_can_skip_the_dictionary(self):
        profile = build_profile("c", erp_kb(count=4), with_dictionary=False)
        self.assertEqual(profile.dictionary, {})

    def test_save_load_and_boilerplate_removal(self):
        profile = build_profile("c", custom_kb())
        path = self.path("profile.json")
        profile.save(path)
        loaded = Profile.load(path)
        self.assertEqual(loaded.to_dict(), profile.to_dict())
        cleaned, removed = remove_boilerplate(custom_kb()[0].text, loaded)
        self.assertEqual(removed, 1)
        self.assertNotIn("ne pas diffuser", cleaned)


if __name__ == "__main__":
    unittest.main()
