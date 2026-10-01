import unittest

from kefind.search import FicheSearchIndex, tokenize
from kefind.understand import understand

from .helpers import decompose_fiche

OUTLOOK = """\
## Symptôme
Outlook reste bloqué au démarrage, erreur 0x80070005.

## Résolution
1. Fermez Outlook.
2. Lancez Outlook en mode sans échec avec la commande outlook.exe /safe.
"""

TEAMS = """\
## Symptôme
Teams reste bloqué au démarrage, erreur 0x80070005.

## Résolution
1. Fermez Teams.
2. Supprimez le dossier de cache dans %APPDATA%\\Microsoft\\Teams.
"""

CAMERA = """\
## Symptôme
La caméra reste noire pendant les réunions dans Teams.

## Résolution
1. Vérifiez que le cache de la caméra n'est pas obstrué.
2. Redémarrez le poste.
"""

PRINTER = """\
## Symptôme
L'imprimante signale un bourrage papier.

## Résolution
1. Ouvrez le capot et retirez le papier coincé.
2. Redémarrez l'imprimante.
"""


class SearchTest(unittest.TestCase):
    def setUp(self):
        self.outlook = decompose_fiche("KB001", "clienta", "Outlook ne démarre plus", OUTLOOK)
        self.teams = decompose_fiche("KB002", "clienta", "Teams ne démarre plus", TEAMS)
        self.camera = decompose_fiche("KB003", "clienta", "Caméra noire dans Teams", CAMERA)
        self.printer = decompose_fiche("KB004", "clienta", "Imprimante en bourrage", PRINTER)
        self.index = FicheSearchIndex("clienta", [self.outlook, self.teams, self.camera, self.printer])

    def test_finds_the_right_fiche_on_a_small_kb(self):
        understanding = understand(None, "Outlook reste bloqué au démarrage sur le chargement du profil, erreur 0x80070005.")
        results = self.index.search(understanding)
        self.assertEqual(results[0].fiche_id, "KB001")

    def test_finds_by_meaning_without_exact_wording(self):
        # "plantée" ne figure dans aucune fiche ; "imprimante" et "papier" si.
        understanding = understand(None, "L'imprimante est plantée avec du papier coincé dedans.")
        results = self.index.search(understanding)
        self.assertEqual(results[0].fiche_id, "KB004")

    def test_entity_bonus_moves_the_ranking(self):
        # Deux fiches avec exactement le même texte (même score lexical et de sens) : seule
        # la carte d'identité de l'une porte un code d'erreur qui concorde avec le ticket.
        shared_text = (
            "## Symptôme\nLe poste ne répond plus correctement pendant l'utilisation habituelle.\n\n"
            "## Résolution\n1. Redémarrez le poste et observez si le comportement se reproduit.\n"
        )
        same_a = decompose_fiche("KBSAME-A", "clienta", "Poste qui ne répond plus (A)", shared_text)
        same_b = decompose_fiche("KBSAME-B", "clienta", "Poste qui ne répond plus (B)", shared_text)
        same_b.entities = [{"canonical": "err:0x80070005", "kind": "error", "count": 1}]
        index = FicheSearchIndex("clienta", [same_a, same_b])

        understanding = understand(None, "Le poste ne répond plus correctement, une fenêtre affiche le code 0x80070005.")
        results = {r.fiche_id: r for r in index.search(understanding)}
        self.assertGreater(results["KBSAME-B"].bonus, 0.0)
        self.assertEqual(results["KBSAME-A"].bonus, 0.0)
        self.assertGreater(results["KBSAME-B"].score, results["KBSAME-A"].score)

    def test_status_info_only_is_never_searched(self):
        info_only = decompose_fiche("KB005", "clienta", "Politique mots de passe",
                                     "Les mots de passe doivent faire 12 caractères et changer tous les 90 jours.")
        self.assertEqual(info_only.status, "info_only")
        index = FicheSearchIndex("clienta", [self.outlook, info_only])
        self.assertEqual([f.fiche_id for f in index.fiches], ["KB001"])

    def test_isolation_between_clients(self):
        other_client_fiche = decompose_fiche("KB999", "clientb", "Outlook ne démarre plus", OUTLOOK)
        with self.assertRaises(ValueError):
            FicheSearchIndex("clienta", [self.outlook, other_client_fiche])

    def test_custom_embedding_provider_is_used(self):
        calls = []

        class FixedEmbedder:
            def fit(self, corpus):
                calls.append(("fit", len(list(corpus))))

            def embed(self, texts):
                calls.append(("embed", len(list(texts))))
                return [[1.0, 0.0] for _ in texts]

        index = FicheSearchIndex("clienta", [self.outlook, self.teams], embedder=FixedEmbedder())
        understanding = understand(None, "peu importe le texte")
        results = index.search(understanding)
        self.assertEqual(len(results), 2)
        self.assertIn(("fit", 2), calls)
        self.assertTrue(any(call[0] == "embed" for call in calls))

    def test_tokenize_drops_stopwords_and_folds_accents(self):
        self.assertEqual(tokenize("L'Écran est bloqué"), ["ecran", "bloque"])


if __name__ == "__main__":
    unittest.main()
