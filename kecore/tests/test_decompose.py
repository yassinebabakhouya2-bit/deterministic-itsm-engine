import unittest

from kecore.decompose import DecomposedFiche, Decomposer, align, iou, summarize_kb
from kecore.fiches import Fiche, load_folder
from kecore.llm import LLMError
from kecore.profile import build_profile

from .helpers import DEMO_KB, OUTLOOK_ANSWER, FakeLLM, step

VPN_ANSWER = {
    "sections": [{"heading": "Problème", "role": "symptom"}, {"heading": "Solution", "role": "resolution"}],
    "steps": [
        step("Vérifiez que le poste est bien connecté au Wi-Fi d'entreprise.", kind="check"),
        step("Videz le cache DNS avec ipconfig /flushdns."),  # reworded: not in the fiche
        step("Redémarrez ensuite le client GlobalProtect."),
        step("En cas d'échec, consultez la fiche KB0010008.", kind="check", condition="En cas d'échec"),
    ],
}

# the self-check (2nd) pass finds only one of the three steps the first pass verified: low self-agreement.
VPN_SELF_CHECK_DISAGREES = {
    "sections": [{"heading": "Solution", "role": "resolution"}],
    "steps": [step("Redémarrez ensuite le client GlobalProtect.")],
}


def demo():
    fiches, _ = load_folder(DEMO_KB, "clienta")
    return {f.fiche_id: f for f in fiches}, build_profile("clienta", fiches)


class AlignTest(unittest.TestCase):
    def test_iou_and_one_to_one_matching(self):
        self.assertEqual(iou((0, 10), (0, 10)), 1.0)
        self.assertEqual(iou((0, 10), (10, 20)), 0.0)
        self.assertEqual(align([(0, 10), (12, 20)], [(0, 11), (13, 20), (30, 40)]), {0: 0, 1: 1})
        self.assertEqual(align([(0, 10)], [(8, 30)]), {})


class DecomposeTest(unittest.TestCase):
    def setUp(self):
        self.fiches, self.profile = demo()

    def test_agreement_makes_a_guided_fiche(self):
        llm = FakeLLM({"KB0010001 - Outlook ne démarre plus": OUTLOOK_ANSWER})
        result = Decomposer(self.profile, llm).decompose(self.fiches["KB0010001"])
        self.assertEqual((result.status, result.confidence), ("guided", "high"))
        self.assertEqual(result.methods["agreement"], 1.0)
        self.assertEqual(len(result.steps), 4)
        texts = [s.text for s in result.steps]
        self.assertEqual(texts[1], "Lancez Outlook en mode sans échec avec la commande `outlook.exe /safe`.")
        self.assertTrue(all(result.text[s.start:s.end] == s.text for s in result.steps))
        self.assertTrue(all(s.sources == ["llm", "rules"] for s in result.steps))
        self.assertEqual(result.steps[2].condition, "Si Outlook démarre en mode sans échec")
        self.assertEqual(result.steps[2].on_failure, 4)
        self.assertTrue(result.steps[3].after_failure)
        self.assertIsNone(result.steps[1].instruction, "the rewording added Win+R")
        self.assertEqual(result.steps[3].instruction, "Supprimez le fichier .ost puis relancez Outlook")
        self.assertEqual(result.checks["instructions_dropped"], 1)
        self.assertIn("cmd:outlook.exe /safe", result.steps[1].entities)
        self.assertEqual(result.boilerplate_removed, 1)
        self.assertNotIn("05 22", result.text)

    def test_a_reworded_quote_is_dropped_and_lowers_confidence(self):
        llm = FakeLLM({"VPN - déconnexions fréquentes": [VPN_ANSWER, VPN_SELF_CHECK_DISAGREES]})
        result = Decomposer(self.profile, llm).decompose(self.fiches["KB0010002"])
        self.assertEqual((result.status, result.confidence), ("citable", "low"))
        self.assertEqual(result.methods["llm_rejected_quotes"], 1)
        self.assertEqual(result.methods["agreement"], 0.5, "the self-check pass only confirms 1 of the 3 steps")
        self.assertEqual(len(result.steps), 3)
        self.assertNotIn("Videz le cache DNS", " ".join(s.text for s in result.steps))
        self.assertEqual(result.references, ["KB0010008"])
        self.assertEqual(result.steps[-1].refers_to, ["KB0010008"])
        self.assertTrue(any("not found in the fiche" in r for r in result.reasons))

    def test_information_only_and_failures(self):
        llm = FakeLLM({"Compte Windows verrouillé": LLMError("timeout")})
        decomposer = Decomposer(self.profile, llm)
        info = decomposer.decompose(self.fiches["KB0010020"])
        self.assertEqual((info.status, info.steps), ("info_only", []))
        failed = decomposer.decompose(self.fiches["KB0010006"])
        self.assertEqual(failed.status, "citable")
        self.assertTrue(any("LLM pass failed: timeout" in r for r in failed.reasons))
        self.assertEqual(decomposer.llm_errors, 1)
        self.assertEqual([s.role for s in failed.steps], ["prerequisite", "resolution", "resolution", "resolution"])
        self.assertEqual(failed.steps[0].kind, "check")

    def test_llm_without_steps_keeps_the_rules_steps(self):
        result = Decomposer(self.profile, FakeLLM({})).decompose(self.fiches["KB0010003"])
        self.assertEqual((result.status, result.methods["agreement"]), ("citable", None))
        self.assertEqual(result.methods["rules_agreement"], 0.0)
        self.assertEqual(len(result.steps), 3)
        self.assertTrue(all(s.sources == ["rules"] for s in result.steps))

    def test_rules_only(self):
        decomposer = Decomposer(self.profile)
        results = [decomposer.decompose(f) for f in self.fiches.values()]
        summary = summarize_kb(results)
        self.assertEqual((summary["guided"], summary["citable"], summary["info_only"]), (0, 5, 1))
        self.assertEqual(summary["steps"], summary["steps_verified"])
        camera = next(r for r in results if r.fiche_id == "KB0010005")
        self.assertEqual(camera.steps[2].goto, 4)

    def test_duplicates_and_out_of_order_quotes(self):
        fiche = Fiche("KB1", "c", "Ordre", "## Résolution\n- Fermez Outlook.\n- Relancez Outlook.")
        answer = {"sections": [], "steps": [step("Relancez Outlook."), step("Fermez Outlook."), step("Fermez Outlook.")]}
        result = Decomposer(None, FakeLLM({"Ordre": answer})).decompose(fiche)
        self.assertEqual([s.text for s in result.steps], ["Fermez Outlook.", "Relancez Outlook."])
        self.assertEqual(result.methods["llm_rejected_quotes"], 1)

    def test_round_trip(self):
        llm = FakeLLM({"KB0010001 - Outlook ne démarre plus": OUTLOOK_ANSWER})
        result = Decomposer(self.profile, llm).decompose(self.fiches["KB0010001"])
        again = DecomposedFiche.from_dict(result.to_dict())
        self.assertEqual(again.to_dict(), result.to_dict())
        self.assertEqual(llm.calls, ["fiche_steps", "fiche_steps"], "the first pass plus the self-check pass")


if __name__ == "__main__":
    unittest.main()


PASSWORD = """KB0012345 - Réinitialisation du mot de passe Windows

Résumé
L'utilisateur ne peut plus se connecter : message "Votre mot de passe a expiré".

Environnement : Windows 10 / Windows 11, Active Directory

Résolution :
Depuis le poste de l'utilisateur :
1) Appuyez sur Ctrl+Alt+Suppr puis cliquez sur "Modifier un mot de passe".
2) Saisissez l'ancien mot de passe puis le nouveau (12 caractères minimum).
   Remarque : le nouveau mot de passe ne doit pas reprendre les 5 derniers.
3) Si le message "Le domaine n'est pas disponible" apparaît, connectez le poste au VPN puis recommencez l'étape 1.

Si le problème persiste :
- vérifiez dans la console AD que le compte n'est pas verrouillé ;
- déverrouillez-le si besoin.

Escalade : groupe N2 Identité."""


class RealisticFicheTest(unittest.TestCase):
    def test_notes_fallback_heading_and_loop(self):
        result = Decomposer().decompose(Fiche("KB0012345", "c", "Mot de passe", PASSWORD))
        self.assertEqual([s["role"] for s in result.sections], ["preamble", "symptom", "resolution", "resolution", "escalation"])
        self.assertEqual(len(result.steps), 5, "the note inside step 2 does not cut the procedure")
        self.assertIn("Remarque", result.steps[1].text)
        third, fourth, fifth = result.steps[2:]
        self.assertEqual((third.goto, third.on_failure), (1, 4))
        self.assertEqual(fourth.condition, "Si le problème persiste")
        self.assertTrue(fourth.after_failure)
        self.assertEqual(fifth.condition, "Si le problème persiste")
        self.assertFalse(fifth.after_failure, "the heading covers the section, step 5 does not fall back from step 4")
        self.assertIn("key:ctrl+alt+del", result.steps[0].entities)
