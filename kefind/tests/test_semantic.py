"""The semantic mode: fiches found by meaning, decided by code, frozen per run (kefind.semantic,
kefind.cards, kefind.funnel._find_semantic)."""

import json
import unittest

from kecore.llm import LLMError
from kefind import semantic as sem
from kefind.cards import Card, anchors_for, entries_for, label_text, make_card, same_label_groups
from kefind.funnel import FunnelConfig, KBMap, find

from .semantic_helpers import BIASED_DIMS, DIMS, ConceptEmbedder, LanguageBiasedEmbedder, TitleLLM
from .test_funnel import filler, make

CARDS = {
    "KB0217 - Transfert d'appels TEAMS": Card(
        "KB0217 - Transfert d'appels TEAMS", "Rediriger les appels Teams d'un utilisateur vers un autre numéro",
        "Forward a user's Teams calls to another number",
        ["transfert d'appel teams", "comment rediriger mes appels teams", "forward teams calls"]),
    "KB0233 - Associate a phone line": Card(
        "KB0233 - Associate a phone line", "Attribuer une ligne téléphonique Teams à un utilisateur",
        "Assign a Teams phone line to a user",
        ["comment attribuer une ligne teams", "l'utilisateur n'a pas de numéro dans teams",
         "assign a phone number in teams"]),
    "KB0120 - LOCKED ACCOUNT": Card(
        "KB0120 - LOCKED ACCOUNT", "Débloquer un compte utilisateur verrouillé", "Unlock a locked user account",
        ["mon compte est bloqué", "compte verrouillé impossible de me connecter", "account locked"]),
}
CALIBRATED = {"thresholds": {"floor": 0.75, "margin": 0.05, "offer": 0.3, "source": "test"}}


def kb_fiches():
    return filler() + [
        make("KB0217 - Transfert d'appels TEAMS", "Ouvrez Teams. Configurez le transfert d'appels vers un autre numero.",
             entities=["app:teams"]),
        make("KB0233 - Associate a phone line", "Open the Teams admin center. Assign a phone line to the user.",
             entities=["app:teams"]),
        make("KB0120 - LOCKED ACCOUNT", "The user account is locked. Unlock the account in the console."),
    ]


def build_map(calibrated=True, cards=CARDS):
    plain = KBMap("clienta", kb_fiches())
    index, built = sem.build(entries_for(plain, cards), ConceptEmbedder(), "concept-embed@test", DIMS,
                             anchors=anchors_for(plain))
    if calibrated:
        index.calibration = {**CALIBRATED, "index_sha256": index.sha256}
    return KBMap("clienta", kb_fiches(), semantic=index), built


def ask(kbmap, text, **kwargs):
    vector = ConceptEmbedder().embed([sem.query_text(text)])[0]
    return find(kbmap, text, query_vector=vector, **kwargs)


class NormalizeTest(unittest.TestCase):
    def test_case_spaces_and_compatibility_forms_fold_to_one_text(self):
        self.assertEqual(sem.normalize_text("  Ligne  TEAMS\n"), "ligne teams")
        self.assertEqual(sem.query_text("<p>Ligne  Teams</p>"), sem.query_text("ligne teams"))

    def test_contact_details_never_reach_the_recorded_vector(self):
        text = sem.query_text("Ligne teams pour jean.dupont@corp.example, joignable au 06 12 34 56 78")
        self.assertNotIn("dupont", text)
        self.assertNotIn("56 78", text)
        self.assertIn("ligne teams", text)


class DecideTest(unittest.TestCase):
    th = sem.Thresholds(floor=0.7, margin=0.05, offer=0.3)

    def test_show_offer_abstain(self):
        self.assertEqual(sem.decide([(0.8, "a", 0), (0.7, "b", 1)], self.th), "show")
        self.assertEqual(sem.decide([(0.8, "a", 0), (0.78, "b", 1)], self.th), "offer")  # no lead
        self.assertEqual(sem.decide([(0.6, "a", 0)], self.th), "offer")  # under the floor
        self.assertEqual(sem.decide([(0.2, "a", 0)], self.th), "abstain")
        self.assertEqual(sem.decide([], self.th), "abstain")

    def test_a_strong_entity_or_a_designated_fiche_lowers_the_bar_to_the_offer_level(self):
        self.assertEqual(sem.decide([(0.4, "a", 0), (0.2, "b", 1)], self.th, strong=True), "show")
        self.assertEqual(sem.decide([(0.1, "a", 0)], self.th, designated=True), "show")

    def test_the_margin_is_compared_after_rounding(self):
        th = sem.Thresholds(floor=0.5, margin=0.05, offer=0.3)
        self.assertEqual(sem.decide([(0.85, "a", 0), (0.8, "b", 1)], th), "show")  # 0.85-0.8 is 0.04999... in floats

    def test_an_exact_tie_is_never_shown_whatever_the_margin(self):
        th = sem.Thresholds(floor=0.5, margin=0.0, offer=0.3)
        self.assertEqual(sem.decide([(0.83, "KB-A", 0), (0.83, "KB-B", 1)], th), "offer")

    def test_uncalibrated_or_withheld_thresholds_never_show_on_a_score_even_with_a_strong_entity(self):
        withheld = sem.Thresholds(floor=1.01, margin=0.05, offer=0.35, source="withheld")
        for th in (sem.UNCALIBRATED, withheld):
            self.assertEqual(sem.decide([(0.95, "a", 0), (0.2, "b", 1)], th, strong=True), "offer")
            self.assertEqual(sem.decide([(0.95, "a", 0), (0.2, "b", 1)], th, designated=True), "offer")
            self.assertEqual(sem.decide([(0.1, "a", 0)], th, designated=True), "show")  # the ticket named it

    def test_scores_are_exact_sums_whatever_the_order_or_python_version(self):
        self.assertEqual(sem.dot([1e16, 1.0, -1e16], [1.0, 1.0, 1.0]), 1.0)  # a plain left-to-right sum gives 0.0
        self.assertEqual(sem.dot([1e16, 1.0, -1e16], [1.0, 1.0, 1.0]), sem.dot([-1e16, 1.0, 1e16], [1.0, 1.0, 1.0]))


class FindBySemanticTest(unittest.TestCase):
    def setUp(self):
        self.kbmap, self.built = build_map()

    def test_the_french_question_reaches_the_english_fiche_by_meaning(self):
        finding = ask(self.kbmap, "comment attribuer une ligne teams")
        self.assertEqual((finding.kind, finding.reason, finding.fiche_id),
                         ("fiche", "semantic_clear_lead", "KB0233 - Associate a phone line"))
        step = next(s for s in finding.trace if s["step"] == "semantic")
        self.assertEqual(step["verdict"], "show")
        self.assertEqual(step["top"][0]["match"], "question")
        self.assertFalse(finding.degraded)

    def test_the_same_question_gives_the_same_decision_byte_for_byte(self):
        first = ask(self.kbmap, "comment attribuer une ligne teams").to_dict()
        again = ask(self.kbmap, "Comment  ATTRIBUER une ligne Teams").to_dict()  # normalized: the same question
        blobs = self.kbmap.semantic.to_blobs()
        reloaded = sem.SemanticIndex.from_blobs(blobs[sem.INDEX_BLOB], blobs[sem.VECTORS_BLOB],
                                                json.dumps({**CALIBRATED, "index_sha256": self.kbmap.semantic.sha256}).encode())
        replayed = ask(KBMap("clienta", kb_fiches(), semantic=reloaded), "comment attribuer une ligne teams").to_dict()
        self.assertEqual(json.dumps(first, sort_keys=True), json.dumps(replayed, sort_keys=True))
        self.assertEqual(first["fiche_id"], again["fiche_id"])
        self.assertEqual(first["score"], again["score"])

    def test_close_meanings_are_offered_not_shown(self):
        finding = ask(self.kbmap, "teams")
        self.assertEqual((finding.kind, finding.asks), ("question", "fiche"))
        offered = {o["fiche_id"] for o in finding.options}
        self.assertTrue({"KB0217 - Transfert d'appels TEAMS", "KB0233 - Associate a phone line"} <= offered)

    def test_nothing_close_is_an_abstention(self):
        finding = ask(self.kbmap, "le vpn se coupe")
        self.assertEqual((finding.kind, finding.reason), ("abstain", "semantic_nothing_close"))

    def test_without_calibration_no_fiche_is_ever_shown_alone(self):
        kbmap, _ = build_map(calibrated=False)
        finding = ask(kbmap, "comment attribuer une ligne teams")
        self.assertEqual((finding.kind, finding.reason), ("question", "semantic_below_floor"))
        self.assertEqual(finding.options[0]["fiche_id"], "KB0233 - Associate a phone line")

    def test_a_cited_fiche_number_is_shown_whatever_the_meaning(self):
        finding = ask(self.kbmap, "voir la KB0120 svp, rien à voir avec teams")
        self.assertEqual((finding.kind, finding.fiche_id, finding.designated), ("fiche", "KB0120 - LOCKED ACCOUNT", True))

    def test_a_question_without_vector_is_decided_by_words_and_flagged(self):
        finding = find(self.kbmap, "comment attribuer une ligne teams")
        self.assertTrue(finding.degraded)
        self.assertTrue(any(s.get("degraded") for s in finding.trace if s["step"] == "semantic"))

    def test_an_application_named_in_passing_does_not_filter(self):
        # "teams" is an application of two fiches; the account fiche has none: it must still win
        finding = ask(self.kbmap, "mon compte est bloqué depuis teams")
        self.assertEqual(finding.fiche_id, "KB0120 - LOCKED ACCOUNT")

    def test_a_map_refuses_another_map_s_index(self):
        with self.assertRaises(ValueError):
            KBMap("clienta", filler(), semantic=self.kbmap.semantic)


class IndexTest(unittest.TestCase):
    def test_a_question_closer_to_another_fiche_is_dropped(self):
        cards = dict(CARDS)
        line = cards["KB0233 - Associate a phone line"]
        cards["KB0233 - Associate a phone line"] = Card(line.fiche_id, line.solves_fr, line.solves_en,
                                                        line.questions + ["rediriger les appels vers un autre numero"])
        _, built = build_map(cards=cards)
        self.assertEqual([d["text"] for d in built["dropped"]], ["rediriger les appels vers un autre numero"])
        self.assertEqual(built["dropped"][0]["closer_to"], "KB0217 - Transfert d'appels TEAMS")

    def test_a_card_the_model_got_wrong_cannot_vouch_for_itself(self):
        # the model wrote KB0233's card about locked accounts: its own lines agree with each other, but the
        # yardstick is code-derived (labels, the fiches' own text), so every line of it goes
        cards = dict(CARDS)
        cards["KB0233 - Associate a phone line"] = Card(
            "KB0233 - Associate a phone line", "Débloquer un compte verrouillé", "Unlock a locked account",
            ["mon compte est bloqué", "compte verrouillé ce matin"])
        kbmap, built = build_map(cards=cards)
        wrong = [d for d in built["dropped"] if d["fiche_id"] == "KB0233 - Associate a phone line"]
        self.assertEqual(sorted(d["kind"] for d in wrong), ["question", "question", "solves", "solves"])
        self.assertTrue(all(d["closer_to"] == "KB0120 - LOCKED ACCOUNT" for d in wrong))
        kept = [e.kind for e in kbmap.semantic.entries if e.fiche_id == "KB0233 - Associate a phone line"]
        self.assertEqual(kept, ["label"])

    def test_the_balanced_rule_keeps_french_lines_of_an_english_fiche_the_code_rule_loses(self):
        # production, 2026-10-09: the code-only yardstick dropped 29% of the questions -- a French line of an
        # English fiche reads closer to ANY French fiche to the embedding model than to its own English text
        plain = KBMap("clienta", kb_fiches())
        entries, anchors = entries_for(plain, CARDS), anchors_for(plain)
        embedder = LanguageBiasedEmbedder()
        vectors = sem.embed_entries(entries, anchors, embedder, BIASED_DIMS)
        built = {rule: sem.build(entries, embedder, embedder.model_id, BIASED_DIMS, anchors=anchors, rule=rule,
                                 vectors=vectors)[1] for rule in sem.DROP_RULES}
        lost = {d["text"]: d["closer_to"] for d in built["code"]["dropped"] if d["fiche_id"] == "KB0120 - LOCKED ACCOUNT"}
        self.assertIn("mon compte est bloqué", lost)
        self.assertTrue(lost["mon compte est bloqué"].startswith("KB09"))  # an unrelated French filler fiche
        self.assertEqual(built["balanced"]["dropped"], [])
        self.assertEqual(built["none"]["dropped"], [])
        with self.assertRaises(ValueError):
            sem.build(entries, embedder, embedder.model_id, BIASED_DIMS, anchors=anchors, rule="loose", vectors=vectors)

    def test_blobs_round_trip_and_refuse_damage_or_another_index_s_calibration(self):
        kbmap, _ = build_map(calibrated=False)
        index = kbmap.semantic
        blobs = index.to_blobs()
        again = sem.SemanticIndex.from_blobs(blobs[sem.INDEX_BLOB], blobs[sem.VECTORS_BLOB])
        self.assertEqual(again.sha256, index.sha256)
        damaged = bytearray(blobs[sem.VECTORS_BLOB])
        damaged[5] ^= 0xFF
        with self.assertRaises(ValueError):
            sem.SemanticIndex.from_blobs(blobs[sem.INDEX_BLOB], bytes(damaged))
        with self.assertRaises(ValueError):
            sem.SemanticIndex.from_blobs(blobs[sem.INDEX_BLOB], blobs[sem.VECTORS_BLOB],
                                         json.dumps({**CALIBRATED, "index_sha256": "another"}).encode())

    def test_every_ranked_fiche_has_at_least_its_label(self):
        kbmap, _ = build_map()
        self.assertEqual(kbmap.semantic.fiche_ids, frozenset(kbmap.ranked))


class CardsTest(unittest.TestCase):
    def setUp(self):
        self.kbmap = KBMap("clienta", kb_fiches())

    def test_the_label_loses_its_number(self):
        self.assertEqual(label_text(self.kbmap, "KB0233 - Associate a phone line"), "Associate a phone line")

    def test_the_code_keeps_only_what_passes(self):
        llm = TitleLLM({"Associate a phone line": {
            "solves_fr": "Attribuer une ligne téléphonique Teams",
            "solves_en": "Assign a Teams phone line",
            "questions": ["comment attribuer une ligne teams", "Comment  attribuer une ligne Teams",  # duplicate
                          "ligne", "voir la KB0999 pour la ligne teams",  # too short, invented fiche number
                          "erreur 0x80070005 sur la ligne teams",  # invented error code
                          "écrire à support@corp.example pour une ligne", "assign a phone number in teams"],
        }})
        card = make_card(llm, self.kbmap, "KB0233 - Associate a phone line")
        self.assertEqual(card.questions, ["comment attribuer une ligne teams", "assign a phone number in teams"])
        self.assertEqual(sorted(d["reason"] for d in card.dropped),
                         ["adds err:0x80070005", "adds kb:999", "contact detail", "too short"])
        self.assertEqual(Card.from_dict(card.to_dict()), card)

    def test_fiches_with_the_same_name_but_different_texts_are_listed_not_merged(self):
        kbmap = KBMap("clienta", kb_fiches() + [
            make("KB0205 - How to add a printer to Printer Logic", "Open Printer Logic. Pick the printer."),
            make("KB0321 - How to add a printer to Printer Logic",
                 "Open the Printer Logic portal. Search the building. Install the printer. Print a test page."),
        ])
        self.assertEqual(same_label_groups(kbmap), [["KB0205 - How to add a printer to Printer Logic",
                                                     "KB0321 - How to add a printer to Printer Logic"]])
        self.assertEqual(same_label_groups(KBMap("clienta", kb_fiches())), [])

    def test_a_line_naming_an_application_the_fiche_does_not_name_is_refused(self):
        llm = TitleLLM({"Associate a phone line": {
            "solves_fr": "Attribuer une ligne téléphonique Teams", "solves_en": "Assign a Teams phone line",
            "questions": ["attribuer une ligne teams", "attribuer une ligne dans outlook"]}})
        card = make_card(llm, self.kbmap, "KB0233 - Associate a phone line")
        self.assertEqual(card.questions, ["attribuer une ligne teams"])
        self.assertEqual([d["reason"] for d in card.dropped], ["adds app:outlook"])

    def test_a_model_failure_leaves_the_fiche_findable_by_its_label(self):
        llm = TitleLLM({"Associate a phone line": LLMError("timeout")})
        card = make_card(llm, self.kbmap, "KB0233 - Associate a phone line")
        self.assertIn("timeout", card.error)
        entries = [e for e in entries_for(self.kbmap, {card.fiche_id: card}) if e.fiche_id == card.fiche_id]
        self.assertEqual([(e.kind, e.text) for e in entries], [("label", "associate a phone line")])


if __name__ == "__main__":
    unittest.main()
