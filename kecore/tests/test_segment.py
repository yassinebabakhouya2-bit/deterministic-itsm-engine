import unittest

from kecore.segment import first_verb, heading_key, keyword_role, rule_steps, split_sections, step_kind
from kecore.text import clean_text

OUTLOOK = """# KB0010001 - Outlook ne démarre plus

## Symptôme
Outlook reste bloqué sur l'écran « Chargement du profil ».

## Résolution
1. Fermez Outlook.
2. Lancez Outlook en mode sans échec avec la commande `outlook.exe /safe`.
3. Si Outlook démarre en mode sans échec, allez dans Fichier > Options > Compléments et désactivez les compléments COM.
4. Si le problème persiste, supprimez le fichier .ost dans %LOCALAPPDATA%\\Microsoft\\Outlook
   puis relancez Outlook.
5. Si cela ne fonctionne pas, passez à l'étape 2."""

PROSE = """Titre : VPN - déconnexions fréquentes
Problème : la connexion VPN se coupe toutes les 10 minutes.
Solution : Vérifiez que le poste est bien connecté au Wi-Fi d'entreprise. Ouvrez une invite de commande et exécutez
ipconfig /flushdns. Redémarrez ensuite le client VPN. En cas d'échec, consultez la fiche KB0010008."""


def texts(text, steps):
    return [text[s.start:s.end] for s in steps]


class HeadingTest(unittest.TestCase):
    def test_keys_and_roles(self):
        self.assertEqual(heading_key("## 2. Résolution :"), "resolution")
        self.assertEqual(keyword_role("resolution du probleme"), "resolution")
        self.assertEqual(keyword_role("solution de contournement"), "workaround")
        self.assertIsNone(keyword_role("diagnostic avance"))


class SectionTest(unittest.TestCase):
    def test_markdown_sections(self):
        sections = split_sections(OUTLOOK)
        self.assertEqual([s.role for s in sections], ["other", "symptom", "resolution"])
        self.assertTrue(OUTLOOK[sections[2].start:].startswith("1. Fermez"))

    def test_inline_labels(self):
        sections = split_sections(PROSE)
        self.assertEqual([(s.title, s.role) for s in sections], [("Titre", "title"), ("Problème", "symptom"), ("Solution", "resolution")])
        self.assertTrue(PROSE[sections[2].start:].startswith("Vérifiez"))

    def test_no_heading_is_unknown(self):
        self.assertEqual([s.role for s in split_sections("Redémarrez le poste.")], ["unknown"])


class RuleStepTest(unittest.TestCase):
    def test_numbered_list_with_conditions_failure_and_goto(self):
        steps = rule_steps(OUTLOOK, split_sections(OUTLOOK))
        self.assertEqual(len(steps), 5)
        self.assertEqual(texts(OUTLOOK, steps)[0], "Fermez Outlook.")
        self.assertTrue(texts(OUTLOOK, steps)[3].endswith("puis relancez Outlook."), "continuation line joined")
        self.assertEqual([s.number for s in steps], [1, 2, 3, 4, 5])
        third, fourth, fifth = steps[2], steps[3], steps[4]
        self.assertEqual(OUTLOOK[third.condition[0]:third.condition[1]], "Si Outlook démarre en mode sans échec")
        self.assertFalse(third.after_failure)
        self.assertTrue(fourth.after_failure)
        self.assertTrue(fifth.after_failure)
        self.assertEqual(fifth.goto, 2)

    def test_prose_sentences(self):
        steps = rule_steps(PROSE, split_sections(PROSE))
        self.assertEqual(
            texts(PROSE, steps),
            [
                "Vérifiez que le poste est bien connecté au Wi-Fi d'entreprise.",
                "Ouvrez une invite de commande et exécutez\nipconfig /flushdns.",
                "Redémarrez ensuite le client VPN.",
                "En cas d'échec, consultez la fiche KB0010008.",
            ],
        )
        self.assertEqual([s.kind for s in steps], ["check", "action", "action", "check"])
        self.assertTrue(steps[3].after_failure)

    def test_html_list_and_infinitives(self):
        html = clean_text("<p><b>Procédure</b></p><ul><li>Éteindre l'imprimante.</li><li>Retirer le bac 2.</li></ul>")
        steps = rule_steps(html, split_sections(html))
        self.assertEqual(texts(html, steps), ["Éteindre l'imprimante.", "Retirer le bac 2."])

    def test_description_lists_are_not_steps(self):
        text = "## Symptôme\n- L'écran reste noir.\n- Le voyant clignote.\n\n## Résolution\n- Rebranchez le câble."
        self.assertEqual(texts(text, rule_steps(text, split_sections(text))), ["Rebranchez le câble."])

    def test_information_only(self):
        text = "Les mots de passe doivent contenir 12 caractères.\nIls expirent après 90 jours."
        self.assertEqual(rule_steps(text, split_sections(text)), [])


class VerbTest(unittest.TestCase):
    def test_first_verb(self):
        cases = {
            "Cliquez sur OK.": "cliquez",
            "Ensuite, redémarrez le poste.": "redemarrez",
            "Dans Outlook, ouvrez Fichier.": "ouvrez",
            "Si l'erreur persiste, réinstallez le pilote.": "reinstallez",
            "Assurez-vous que le câble est branché.": "assurez-vous",
            "Pour toute question, contactez le support.": None,
            "Le poste redémarre en boucle.": None,
            "Restart the computer.": "restart",
        }
        for text, expected in cases.items():
            self.assertEqual(first_verb(text), expected, text)

    def test_kind(self):
        self.assertEqual(step_kind("Vérifiez que le Wi-Fi est actif.", "resolution"), "check")
        self.assertEqual(step_kind("Le voyant est-il vert ?", "resolution"), "check")
        self.assertEqual(step_kind("Supprimez le profil.", "resolution"), "action")
        self.assertEqual(step_kind("Avoir les droits d'administration.", "prerequisite"), "check")


if __name__ == "__main__":
    unittest.main()
