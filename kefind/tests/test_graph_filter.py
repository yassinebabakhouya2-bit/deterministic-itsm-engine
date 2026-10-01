import unittest

from kefind.graph_filter import filter_candidates
from kefind.search import SearchResult

from .helpers import decompose_fiche


class GraphFilterTest(unittest.TestCase):
    def test_no_op_returns_the_same_candidates_in_order(self):
        fiche = decompose_fiche("KB001", "clienta", "Titre", "## Résolution\n1. Faites ceci.\n")
        candidates = [SearchResult("KB001", fiche, 0.9, 0.9, 0.9, 0.0)]
        result = filter_candidates(candidates)
        self.assertEqual(result, candidates)
        self.assertIsNot(result, candidates)  # une copie, pas la même liste

    def test_a_graph_argument_is_accepted_and_still_ignored(self):
        candidates = []
        self.assertEqual(filter_candidates(candidates, graph={"doublons": []}), [])


if __name__ == "__main__":
    unittest.main()
