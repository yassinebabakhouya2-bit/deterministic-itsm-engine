import unittest

from kecore.fiches import Fiche, load_folder
from kecore.llm import LLMError
from kecore.profile import Profile, build_profile, remove_boilerplate

from .helpers import DEMO_KB, FakeLLM, TempDirTestCase


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
        self.assertEqual(llm.calls, ["heading_roles"])
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
