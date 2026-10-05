import unittest

from kecore.entities import canonical_set, extract_entities, novel_technical_entities


def canon(text):
    return [e.canonical for e in extract_entities(text)]


class DynamicDictionaryTest(unittest.TestCase):
    def test_app_dictionary_extends_recognition(self):
        dictionary = {"harmony": ["Harmony", "ERP Harmony"]}
        self.assertEqual(canonical_set("L'ERP Harmony est bloqué", kinds=frozenset({"app"})), set())
        self.assertEqual(
            canonical_set("L'ERP Harmony est bloqué", kinds=frozenset({"app"}), app_dictionary=dictionary),
            {"app:harmony"},
        )

    def test_app_dictionary_does_not_shadow_known_apps(self):
        # a per-client dictionary must extend the static table, never override "outlook" itself.
        dictionary = {"harmony": ["Harmony"]}
        self.assertEqual(
            canonical_set("Outlook et Harmony sont bloqués", kinds=frozenset({"app"}), app_dictionary=dictionary),
            {"app:outlook", "app:harmony"},
        )

    def test_without_a_dictionary_behavior_is_unchanged(self):
        self.assertEqual(extract_entities("Harmony est bloqué"), extract_entities("Harmony est bloqué", None))


class EntityTest(unittest.TestCase):
    def test_error_codes_meet_whatever_their_spelling(self):
        self.assertEqual(
            canon("Erreur 0x80070005 (code -2147024891), puis code d'erreur 1603 et Event ID 4625."),
            ["err:0x80070005", "err:0x80070005", "err:1603", "evt:4625"],
        )
        self.assertEqual(canon("Le disque fait 500 Go, erreur 2 fois."), [])

    def test_fiche_numbers_updates_and_tickets(self):
        self.assertEqual(
            canon("Voir KB0010008, désinstaller KB5034441, ticket INC0012345."),
            ["kb:KB0010008", "update:KB5034441", "ticket:INC0012345"],
        )

    def test_paths_registry_urls(self):
        self.assertEqual(
            canon(r"Ouvrez C:\Program Files\Microsoft Office\root\Office16 puis %LOCALAPPDATA%\Microsoft\Outlook."),
            [r"path:c:\program files\microsoft office\root\office16", r"path:%localappdata%\microsoft\outlook"],
        )
        # an application named inside a path or a key is not counted as an application
        self.assertEqual(canon(r"Clé HKLM\Software\Policies\Microsoft\Office."),
                         [r"reg:hkey_local_machine\software\policies\microsoft\office"])
        self.assertEqual(canon("Allez sur https://Portal.Office.com/account/."), ["url:https://portal.office.com/account"])
        self.assertEqual(canon(r"Partage \\srv01\commun\docs"), [r"path:\\srv01\commun\docs"])

    def test_commands(self):
        self.assertEqual(canon("Exécutez ipconfig /flushdns puis gpupdate /force."),
                         ["cmd:ipconfig /flushdns", "cmd:gpupdate /force"])
        self.assertEqual(canon("Lancez SCANPST.EXE. Ouvrez services.msc."), ["cmd:scanpst.exe", "cmd:services.msc"])
        self.assertEqual(canon("Lancez outlook.exe /safe"), ["cmd:outlook.exe /safe", "app:outlook"])
        self.assertEqual(canon("Get-Service puis Restart-Service"), ["cmd:get-service", "cmd:restart-service"])

    def test_shortcuts_and_menus(self):
        self.assertEqual(canon("Appuyez sur Ctrl + Alt + Suppr puis sur la touche F5."), ["key:ctrl+alt+del", "key:f5"])
        self.assertEqual(canon("Faites Win+R."), ["key:win+r"])
        self.assertEqual(
            canon("Si Outlook démarre, allez dans Fichier > Options > Compléments et désactivez-les."),
            ["app:outlook", "menu:fichier > options > complements"],
        )
        self.assertEqual(canon("Panneau de configuration > Programmes"), ["menu:panneau de configuration > programmes"])
        self.assertEqual(canon("si la valeur > 100, contactez le support"), [])

    def test_apps_and_systems(self):
        self.assertEqual(canon("Teams et Microsoft Outlook sous Windows 11"), ["app:teams", "app:outlook", "os:windows-11"])
        self.assertIn("app:entra-id", canonical_set("Le compte Azure AD est bloqué"))


class NovelEntitiesTest(unittest.TestCase):
    def test_rewording_may_not_add_technical_content(self):
        quote = "Lancez Outlook en mode sans échec avec la commande outlook.exe /safe."
        self.assertEqual(novel_technical_entities("Appuyez sur Win+R et tapez outlook.exe /safe", quote), ["key:win+r"])
        self.assertEqual(novel_technical_entities("Lancez outlook.exe /safe", quote), [])
        self.assertEqual(novel_technical_entities("Puis exécutez ipconfig /flushdns", quote), ["cmd:ipconfig /flushdns"])

    def test_shorter_menu_path_is_not_new(self):
        reference = "allez dans Fichier > Options > Compléments"
        self.assertEqual(novel_technical_entities("Ouvrez Fichier > Options", reference), [])
        self.assertEqual(novel_technical_entities("Ouvrez Fichier > Imprimer", reference), ["menu:fichier > imprimer"])


if __name__ == "__main__":
    unittest.main()
