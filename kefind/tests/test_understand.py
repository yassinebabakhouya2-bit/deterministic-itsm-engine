import unittest

from kecore.llm import LLMError

from kefind.understand import understand

from .helpers import StubLLM


class UnderstandTest(unittest.TestCase):
    def test_without_llm_falls_back_to_rules(self):
        result = understand(None, "Outlook ne démarre plus, erreur 0x80070005.")
        self.assertEqual(result.application, "outlook")
        self.assertIn("err:0x80070005", result.canonical_entities)
        self.assertTrue(result.symptom)
        self.assertEqual(result.notes, [])

    def test_entities_are_extracted_by_rules_not_llm(self):
        llm = StubLLM({"symptom": "l'application ne démarre plus", "application": None, "notes": []})
        result = understand(llm, "Teams reste bloqué, erreur 0x80070005, voir %APPDATA%\\Microsoft\\Teams")
        self.assertIn("err:0x80070005", result.canonical_entities)
        self.assertIn("app:teams", result.canonical_entities)
        self.assertEqual(llm.calls, ["ticket_understanding"])

    def test_llm_json_is_validated_and_used(self):
        llm = StubLLM({"symptom": "Le VPN se déconnecte toutes les 10 minutes", "application": "GlobalProtect",
                       "notes": ["depuis ce matin"]})
        result = understand(llm, "le vpn coupe")
        self.assertEqual(result.symptom, "Le VPN se déconnecte toutes les 10 minutes")
        self.assertEqual(result.application, "globalprotect")
        self.assertEqual(result.notes, ["depuis ce matin"])
        self.assertGreater(result.usage.input_tokens, 0)

    def test_malformed_llm_answer_falls_back(self):
        llm = StubLLM({"symptom": 123, "application": [], "notes": "not a list"})
        result = understand(llm, "Outlook bloqué")
        self.assertTrue(result.symptom)  # repli par règle
        self.assertEqual(result.application, "outlook")  # repli: trouvé par les entités
        self.assertEqual(result.notes, [])

    def test_llm_error_falls_back_without_crashing(self):
        llm = StubLLM(LLMError("boom"))
        result = understand(llm, "Outlook ne démarre plus")
        self.assertEqual(result.llm_error, "boom")
        self.assertEqual(result.application, "outlook")


if __name__ == "__main__":
    unittest.main()
