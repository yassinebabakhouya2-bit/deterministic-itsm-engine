import unittest

from kecore.text import NormalizedText, clean_text, normalize, strip_list_marker


class NormalizeTest(unittest.TestCase):
    def test_typography_spacing_case_and_markdown_do_not_count(self):
        self.assertEqual(normalize("L’écran  affiche « Erreur » : cliquez sur **OK**."),
                         normalize("l'écran affiche \"Erreur\": cliquez sur OK."))
        self.assertEqual(normalize("Tapez `ipconfig /all` puis Entrée"), normalize("tapez ipconfig /all puis entrée"))
        self.assertEqual(normalize("Fichier – Options"), normalize("Fichier - Options"))
        self.assertNotEqual(normalize("démarrer"), normalize("demarrer"), "accents are content")

    def test_list_marker(self):
        self.assertEqual(strip_list_marker("1. Fermez Outlook."), "Fermez Outlook.")
        self.assertEqual(strip_list_marker("Étape 2 : Ouvrez la console."), "Ouvrez la console.")
        self.assertEqual(strip_list_marker("- Éteindre l'imprimante."), "Éteindre l'imprimante.")


class FindTest(unittest.TestCase):
    def test_returns_the_original_span(self):
        text = "Intro.\n1. Fermez  Outlook.\n2. L’imprimante affiche « Erreur 0x80070005 » :\n   cliquez sur **OK**."
        nt = NormalizedText(text)
        start, end = nt.find("1. Fermez Outlook.")
        self.assertEqual(text[start:end], "Fermez  Outlook.")
        start, end = nt.find("L'imprimante affiche \"Erreur 0x80070005\": cliquez sur OK.")
        self.assertEqual(text[start:end], "L’imprimante affiche « Erreur 0x80070005 » :\n   cliquez sur **OK**.")

    def test_rewording_and_partial_words_are_rejected(self):
        nt = NormalizedText("Redémarrez le poste puis relancez Outlook.")
        self.assertIsNone(nt.find("Redémarrez l'ordinateur puis relancez Outlook."))
        self.assertIsNone(nt.find("émarrez le poste"))
        self.assertIsNone(nt.find("Re"))
        self.assertIsNotNone(nt.find("Redémarrez le poste…"))

    def test_order_is_kept_with_after(self):
        text = "Redémarrez le poste. Ouvrez Teams. Redémarrez le poste."
        nt = NormalizedText(text)
        first = nt.find("Redémarrez le poste")
        second = nt.find("Redémarrez le poste", after=first[1])
        self.assertEqual(first[0], 0)
        self.assertEqual(second[0], text.rindex("Redémarrez"))

    def test_contains(self):
        self.assertTrue(NormalizedText("Ouvrez  Outlook").contains("ouvrez outlook"))
        self.assertFalse(NormalizedText("Ouvrez Outlook").contains("   "))


class CleanTextTest(unittest.TestCase):
    def test_html_lists_become_items(self):
        html = "<p><b>Procédure</b></p><ul><li>Éteindre l'imprimante.</li><li>Rallumer&nbsp;l'imprimante.</li></ul>"
        self.assertEqual(clean_text(html), "Procédure\n\n- Éteindre l'imprimante.\n\n- Rallumer l'imprimante.")

    def test_nested_list_indent_is_kept(self):
        self.assertEqual(clean_text("1. Ouvrez Outlook\n    a. Cliquez sur Fichier"), "1. Ouvrez Outlook\n    a. Cliquez sur Fichier")


if __name__ == "__main__":
    unittest.main()
