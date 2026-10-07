import json
import unittest

from kecore.decompose import DecomposedFiche, Step
from kefind.graph import KBGraph, build_graph, own_numbers

WORDS = [f"mot{i}" for i in range(100)]


def text(replace_every: int = 0, marker: str = "x") -> str:
    """100 distinct words; every ``replace_every``-th word changed, to control the overlap."""
    words = [f"{marker}{i}" if replace_every and i % replace_every == 0 else w for i, w in enumerate(WORDS)]
    return " ".join(words)


def fiche(fiche_id: str, body: str, title: str | None = None, status: str = "guided", steps=(),
          client: str = "clienta") -> DecomposedFiche:
    return DecomposedFiche(
        fiche_id=fiche_id, client=client, title=title if title is not None else fiche_id, source="", text=body,
        text_sha256="", status=status, confidence="high", reasons=[], sections=[],
        steps=[Step(n=i + 1, start=0, end=0, text=t, kind="action", role=role) for i, (t, role) in enumerate(steps)],
        entities=[], references=[], methods={}, checks={},
    )


class NumbersTest(unittest.TestCase):
    def test_number_from_id_then_title(self):
        self.assertEqual(own_numbers(fiche("KB0052 – Locked account", "")), [52])
        self.assertEqual(own_numbers(fiche("kb-0052", "")), [52])
        self.assertEqual(own_numbers(fiche("Procedure", "", title="KB 120 - Compte")), [120])
        self.assertEqual(own_numbers(fiche("Procedure", "", title="Compte verrouillé")), [])


class DuplicatesTest(unittest.TestCase):
    def test_near_identical_texts_form_a_group_with_one_canonical(self):
        graph = build_graph([
            fiche("KB0052 - Locked account", text(replace_every=50), status="citable"),
            fiche("KB0120 - Locked account", text(), steps=[("Déverrouillez le compte.", "resolution")]),
            fiche("KB0300 - Autre sujet", text(replace_every=2, marker="z")),
        ])
        self.assertEqual(graph.groups, [["KB0120 - Locked account", "KB0052 - Locked account"]])
        self.assertEqual(graph.nodes["KB0052 - Locked account"].duplicate_of, "KB0120 - Locked account")
        self.assertIsNone(graph.nodes["KB0300 - Autre sujet"].duplicate_of)

    def test_two_versions_with_the_same_number_are_duplicates(self):
        graph = build_graph([fiche("KB0232 - Creating a resource", text()),
                             fiche("KB0232 - Creating a resource1", text(replace_every=20))])
        self.assertEqual(len(graph.groups), 1)
        self.assertEqual(graph.conflicts, {})

    def test_same_number_but_different_texts_is_a_conflict_not_a_duplicate(self):
        graph = build_graph([fiche("KB0076 - Global Protect connection", text()),
                             fiche("KB0076 - User not appearing", text(replace_every=2, marker="z"))])
        self.assertEqual(graph.groups, [])
        self.assertEqual(graph.conflicts, {"76": ["KB0076 - Global Protect connection", "KB0076 - User not appearing"]})
        self.assertEqual(graph.resolve_number(76), ["KB0076 - Global Protect connection", "KB0076 - User not appearing"])

    def test_a_numbered_fiche_is_preferred_as_canonical_over_an_unnumbered_copy(self):
        graph = build_graph([fiche("LOCAMAT", text()), fiche("KB0042 - LOCAMAT", text())])
        self.assertEqual(graph.groups, [["KB0042 - LOCAMAT", "LOCAMAT"]])


class ReferencesTest(unittest.TestCase):
    def test_references_prerequisites_and_missing_numbers(self):
        graph = build_graph([
            fiche("KB0010 - Base", "Procédure de base."),
            fiche("KB0011 - Suite", "Commencez par la fiche KB0010. Voir aussi KB0999.",
                  steps=[("Appliquez d'abord la fiche KB0010.", "prerequisite")]),
            fiche("KB0012 - Autre", "Voir KB 10 pour le contexte."),
        ])
        self.assertEqual(graph.nodes["KB0011 - Suite"].references, ["KB0010 - Base"])
        self.assertEqual(graph.nodes["KB0011 - Suite"].prerequisites, ["KB0010 - Base"])
        self.assertEqual(graph.nodes["KB0012 - Autre"].references, ["KB0010 - Base"])
        self.assertEqual(graph.nodes["KB0012 - Autre"].prerequisites, [])
        self.assertEqual(graph.missing, {"KB0011 - Suite": [999]})

    def test_a_fiche_never_references_itself(self):
        graph = build_graph([fiche("KB0010 - Base", "Cette fiche KB0010 explique la base.")])
        self.assertEqual(graph.nodes["KB0010 - Base"].references, [])

    def test_replacement_in_both_directions(self):
        graph = build_graph([
            fiche("KB0052 - Ancienne", "Ancienne procédure."),
            fiche("KB0120 - Nouvelle", "Cette fiche annule et remplace la fiche KB0052."),
            fiche("KB0060 - Vieille", "Obsolète, remplacée par KB0061."),
            fiche("KB0061 - Récente", "Procédure récente."),
        ])
        self.assertEqual(graph.nodes["KB0052 - Ancienne"].superseded_by, ["KB0120 - Nouvelle"])
        self.assertEqual(graph.nodes["KB0060 - Vieille"].superseded_by, ["KB0061 - Récente"])
        self.assertEqual(graph.stats()["superseded"], 2)

    def test_replacing_words_without_a_number_change_nothing(self):
        graph = build_graph([fiche("KB0070 - Batterie", "Remplacez la batterie puis remplace le capot.")])
        self.assertEqual(graph.stats()["superseded"], 0)


class PruneTest(unittest.TestCase):
    def test_duplicates_and_replaced_fiches_are_mapped_order_kept(self):
        graph = build_graph([
            fiche("KB0052 - Locked account", text(replace_every=50), status="citable"),
            fiche("KB0120 - Locked account", text()),
            fiche("KB0060 - Vieille", "Obsolète, remplacée par KB0061."),
            fiche("KB0061 - Récente", "Procédure récente."),
            fiche("KB0005 - Seule", "Rien à voir."),
        ])
        kept, changes = graph.prune(["KB0005 - Seule", "KB0052 - Locked account", "KB0060 - Vieille",
                                     "KB0120 - Locked account"])
        self.assertEqual(kept, ["KB0005 - Seule", "KB0120 - Locked account", "KB0061 - Récente"])
        self.assertEqual([c["change"] for c in changes], ["duplicate_of", "superseded_by"])


class SerializationTest(unittest.TestCase):
    def test_round_trip_and_order_independence(self):
        fiches = [
            fiche("KB0010 - Base", "Procédure de base. " + text()),
            fiche("KB0011 - Suite", "Voir la fiche KB0010. " + text(replace_every=50)),
            fiche("KB0076 - A", text(replace_every=2, marker="a")),
            fiche("KB0076 - B", text(replace_every=2, marker="b")),
        ]
        graph = build_graph(fiches)
        again = KBGraph.from_dict(json.loads(json.dumps(graph.to_dict())))
        self.assertEqual(again.to_dict(), graph.to_dict())
        self.assertEqual(build_graph(list(reversed(fiches))).to_dict(), graph.to_dict())


if __name__ == "__main__":
    unittest.main()
