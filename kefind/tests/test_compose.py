import unittest

from kecore.llm import LLMError

from kefind.compose import write_and_verify

from .helpers import StubLLM, decompose_fiche

FICHE = decompose_fiche(
    "KB001", "clienta", "Outlook ne démarre plus",
    "## Symptôme\nOutlook reste bloqué au démarrage.\n\n## Résolution\n"
    "1. Fermez Outlook.\n2. Lancez Outlook en mode sans échec avec la commande outlook.exe /safe.\n",
)


class ComposeTest(unittest.TestCase):
    def test_without_llm_falls_back_to_the_fiche_s_resolution_step(self):
        answer = write_and_verify(None, FICHE, "Outlook ne démarre plus")
        self.assertFalse(answer.citation_verified)
        self.assertEqual(answer.step_text, FICHE.steps[0].text)
        self.assertEqual(answer.summary, FICHE.title)

    def test_exact_citation_is_verified_and_shown(self):
        step = FICHE.steps[1]
        llm = StubLLM({"summary": "Relancer Outlook sans échec.", "citation": step.text})
        answer = write_and_verify(llm, FICHE, "Outlook ne démarre plus")
        self.assertTrue(answer.citation_verified)
        self.assertEqual(answer.step_number, step.n)
        self.assertEqual(answer.step_text, step.text)  # toujours l'extrait exact de la fiche
        self.assertEqual(answer.summary, "Relancer Outlook sans échec.")

    def test_citation_as_a_substring_of_a_step_is_still_verified(self):
        step = FICHE.steps[0]
        partial = step.text.rstrip(".")  # une sous-chaîne mot pour mot reste vérifiable
        llm = StubLLM({"summary": "Fermer Outlook.", "citation": partial})
        answer = write_and_verify(llm, FICHE, "Outlook ne démarre plus")
        self.assertTrue(answer.citation_verified)
        self.assertEqual(answer.step_text, step.text)

    def test_fabricated_citation_is_rejected_and_falls_back_to_the_source_extract(self):
        llm = StubLLM({"summary": "Résumé.", "citation": "Désinstallez complètement Windows et réinstallez-le."})
        answer = write_and_verify(llm, FICHE, "Outlook ne démarre plus")
        self.assertFalse(answer.citation_verified)
        # jamais le texte inventé du LLM : toujours une étape réelle de la fiche
        self.assertIn(answer.step_text, [s.text for s in FICHE.steps])
        self.assertNotEqual(answer.step_text, "Désinstallez complètement Windows et réinstallez-le.")

    def test_llm_error_falls_back_without_crashing(self):
        llm = StubLLM(LLMError("boom"))
        answer = write_and_verify(llm, FICHE, "Outlook ne démarre plus")
        self.assertFalse(answer.citation_verified)
        self.assertEqual(answer.llm_error, "boom")
        self.assertEqual(answer.step_text, FICHE.steps[0].text)

    def test_empty_citation_falls_back(self):
        llm = StubLLM({"summary": "Résumé.", "citation": ""})
        answer = write_and_verify(llm, FICHE, "Outlook ne démarre plus")
        self.assertFalse(answer.citation_verified)
        self.assertEqual(answer.step_text, FICHE.steps[0].text)


if __name__ == "__main__":
    unittest.main()
