"""The dictionary's online loop (V10 slice 5, pilier 2): live questions observed once per session,
candidates reviewed by a person, decisions recorded once and read by the next kecore run.

Run from the repository root:  python -m unittest discover -s kecore_func/tests
"""

import json
import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for path in (ROOT, ROOT / "kecore_func", Path(__file__).resolve().parent):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import dictionary_service as dico  # noqa: E402
import kecore_pipeline as pipeline  # noqa: E402
from test_tickets_service import MemoryStorage, MemoryTable  # noqa: E402

CLIENT = "client-s"
KNOWN = {"autocad": ["AutoCAD"], "veeam": ["VEEAM"]}


def key(n):
    return f"{n:032x}"


def observe(table, *texts, session=None):
    """One question per text; each from its own session unless ``session`` is given."""
    for i, text in enumerate(texts):
        dico.observe(table, CLIENT, text, KNOWN, key(session if session is not None else 1000 + i + len(table.rows)))


def row_of(table, term):
    return table.get(CLIENT, dico.term_row_key(CLIENT, term))


class ObserveTest(unittest.TestCase):
    def test_a_new_name_counts_once_per_session(self):
        table = MemoryTable()
        observe(table, "L'application Coupa plante. L'application Coupa encore.", session=1)
        observe(table, "le logiciel Coupa ne démarre pas", "Coupa toujours, l'application Coupa", session=1)
        self.assertEqual(row_of(table, "coupa")["seen"], 1)
        observe(table, "le logiciel Coupa ne démarre pas", session=2)
        self.assertEqual(row_of(table, "coupa")["seen"], 2)

    def test_the_spelling_is_stored_only_once_seen_in_enough_sessions(self):
        table = MemoryTable()
        observe(table, "l'application Coupa", session=1)
        observe(table, "l'application Coupa", session=2)
        stored = json.dumps(list(table.rows.values()), ensure_ascii=False)
        self.assertNotIn("Coupa", stored)
        self.assertNotIn("coupa", stored)
        observe(table, "l'application Coupa", session=3)
        row = row_of(table, "coupa")
        self.assertEqual((row["term"], row["spelling"], row["seen"], row["status"]), ("coupa", "Coupa", 3, "pending"))

    def test_the_question_and_long_capitalized_runs_are_never_stored(self):
        table = MemoryTable()
        for session in (1, 2, 3):
            observe(table, "L'application Mon Compte Pour Marie Curie plante chez Jean Dupont au 06 12 34 56 78",
                    session=session)
        self.assertEqual(table.rows, {})
        observe(table, "Le logiciel Coupa plante chez Jean Dupont au 06 12 34 56 78", session=4)
        stored = json.dumps(list(table.rows.values()), ensure_ascii=False)
        for fragment in ("Jean", "Dupont", "06 12", "Marie", "plante"):
            self.assertNotIn(fragment, stored)

    def test_a_name_the_dictionary_knows_is_not_a_candidate(self):
        table = MemoryTable()
        observe(table, "le logiciel AutoCAD plante", "l'outil VEEAM ne sauvegarde plus")
        self.assertEqual(table.rows, {})

    def test_no_session_key_no_observation(self):
        table = MemoryTable()
        for bad in (None, "", "ABCDEF0123456789", "sess-1", "0" * 15, "0" * 65):
            dico.observe(table, CLIENT, "l'application Coupa", KNOWN, bad)
        self.assertEqual(table.rows, {})

    def test_a_decided_candidate_stops_counting_and_keeps_its_decision(self):
        table = MemoryTable()
        observe(table, *["l'application Coupa"] * 3)
        dico.decide(table, dico.validate_decision({"client": CLIENT, "term": "coupa", "accept": False}, [CLIENT]))
        observe(table, "l'application Coupa")
        row = row_of(table, "coupa")
        self.assertEqual((row["seen"], row["status"]), (3, "rejected"))

    def test_a_hostile_text_is_scanned_in_linear_time(self):
        start = time.perf_counter()
        for text in ("l'" * 10000 + "x", "le " * 7000 + "x", "application " * 2000 + "x"):
            dico.candidate_terms(text, KNOWN)
        self.assertLess(time.perf_counter() - start, 1.0)


class ReviewAndDecideTest(unittest.TestCase):
    def setUp(self):
        self.storage, self.table = MemoryStorage(), MemoryTable()
        observe(self.table, *["l'application Coupa plante"] * 3, "le logiciel Sage figé")

    def review(self):
        return dico.review(self.storage, self.table, CLIENT, KNOWN, "r1")

    def decide(self, **body):
        return dico.decide(self.table, dico.validate_decision({"client": CLIENT, **body}, [CLIENT]))

    def test_only_candidates_seen_in_enough_sessions_are_offered(self):
        review = self.review()
        self.assertEqual([(r["term"], r["spelling"], r["seen"]) for r in review["ready"]], [("coupa", "Coupa", 3)])
        self.assertEqual(review["watching"], 1)
        self.assertEqual([d["id"] for d in review["dictionary"]], ["autocad", "veeam"])

    def test_accepting_is_recorded_once_and_read_by_the_next_run(self):
        result = self.decide(term="Coupa", accept=True, by="Yassine")
        self.assertEqual(result["status"], "accepted")
        self.assertEqual(dico.decisions(self.table, CLIENT), {"rejected": [], "accepted": [{"spelling": "Coupa", "canonical": None}]})
        review = self.review()
        self.assertEqual(review["ready"], [])
        self.assertEqual(review["decided"][0]["decided_by"], "Yassine")
        with self.assertRaises(ValueError):
            self.decide(term="coupa", accept=False)  # already decided

    def test_rejecting_a_candidate_or_an_entry(self):
        self.decide(term="coupa", accept=False)
        self.decide(entry="veeam", accept=False)
        self.assertEqual(dico.decisions(self.table, CLIENT)["rejected"], ["Coupa", "veeam"])
        self.assertTrue(next(d for d in self.review()["dictionary"] if d["id"] == "veeam")["rejected"])
        with self.assertRaises(ValueError):
            self.decide(entry="veeam", accept=False)

    def test_a_candidate_below_the_threshold_or_unknown_cannot_be_decided(self):
        for term in ("sage", "unknown"):
            with self.assertRaises(FileNotFoundError, msg=term):
                self.decide(term=term, accept=True)

    def test_a_decision_survives_a_concurrent_observation(self):
        table = self.table
        real = table.merge_if
        raced = []

        def racy(entity, etag):
            if not raced:  # another session observes the same name between the read and the write
                raced.append(1)
                observe(table, "l'application Coupa", session=99)
            return real(entity, etag)

        table.merge_if = racy
        self.decide(term="coupa", accept=True)
        row = row_of(table, "coupa")
        self.assertEqual((row["status"], row["seen"]), ("accepted", 4))

    def test_bad_decisions_are_refused(self):
        for body in ({"client": CLIENT, "term": "coupa"}, {"client": CLIENT, "accept": True},
                     {"client": CLIENT, "term": "coupa", "entry": "veeam", "accept": False},
                     {"client": CLIENT, "entry": "veeam", "accept": True},
                     {"client": CLIENT, "term": "coupa", "accept": True, "canonical": "Not An Id"},
                     {"client": "other", "term": "coupa", "accept": True}):
            with self.assertRaises(ValueError, msg=str(body)):
                dico.validate_decision(body, [CLIENT])

    def test_the_hand_written_file_is_shown_too_and_an_invalid_one_is_reported(self):
        self.storage.write("kecore-client-s", pipeline.DICTIONARY_DECISIONS, json.dumps({"rejected": ["autocad"]}).encode())
        self.assertTrue(next(d for d in self.review()["dictionary"] if d["id"] == "autocad")["rejected"])
        self.storage.write("kecore-client-s", pipeline.DICTIONARY_DECISIONS, b"{not json")
        with self.assertRaises(ValueError):
            self.review()


class NextRunTest(unittest.TestCase):
    def test_accepted_names_join_the_next_profile_unless_rejected(self):
        from kecore.profile import Profile

        table = MemoryTable()
        observe(table, *["l'application Coupa plante"] * 3, *["le logiciel Sage figé"] * 3)
        for term, canonical in (("coupa", None), ("sage", "veeam")):
            dico.decide(table, dico.validate_decision({"client": CLIENT, "term": term, "accept": True,
                                                       "canonical": canonical}, [CLIENT]))
        decided = dico.decisions(table, CLIENT)
        learned = Profile(client=CLIENT, dictionary={"veeam": ["VEEAM"]})
        added = pipeline.merge_accepted(learned, decided["accepted"], rejected=["coupa"])
        self.assertEqual(added, 1)
        self.assertEqual(learned.dictionary, {"veeam": ["VEEAM", "Sage"]})


if __name__ == "__main__":
    unittest.main()
