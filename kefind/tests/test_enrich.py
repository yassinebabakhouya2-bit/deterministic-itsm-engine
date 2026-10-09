"""Pass A of the semantic enrichment (kefind.enrich): the model proposes, the code keeps what passes,
a replay rewrites the same bytes with no model call."""

import json
import unittest

from kecore.llm import LLMError, RecordingLLM
from kefind.enrich import (SCHEMA_NAME, Enrichment, app_vocabulary, canonical_intent, make_enrichment, schema,
                           stats, user_prompt)
from kefind.funnel import KBMap

from .semantic_helpers import TitleLLM
from .test_funnel import filler, make

PHONE_LINE = "KB0233 - Associate a phone line"
TRANSFER = "KB0217 - Transfert d'appels TEAMS"
LOCKED = "KB0120 - LOCKED ACCOUNT"
HARMONY = "KB0400 - Valider une facture"

KB0233_ANSWER = {
    "canonical_intent": "Attribution ligne téléphonique",
    "intent_label_fr": "Attribuer une ligne téléphonique à un utilisateur",
    "primary_app": "teams",
    "supported_apps": ["teams", "office"],
    "app_evidence": [{"app": "teams", "quote": "Open the Teams admin center"}],
    "semantic_aliases_fr": ["attribuer une ligne teams", "affecter un numéro teams", "Attribuer une ligne Teams",
                            "créer une ligne téléphonique teams"],
    "semantic_aliases_en": ["assign a teams phone number"],
    "trigger_keywords": ["ligne", "numéro", "teams", "attribuer"],
}


def kb_fiches():
    return filler() + [
        make(PHONE_LINE, "Open the Teams admin center. Assign a phone line to the user.", entities=["app:teams"]),
        make(TRANSFER, "Ouvrez Teams. Configurez le transfert d'appels vers un autre numero.", entities=["app:teams"]),
        make(LOCKED, "The user account is locked. Unlock the account in the console."),
        make(HARMONY, "Ouvrez Harmony. Validez la facture dans le circuit d'approbation."),
    ]


def kbmap(dictionary=None):
    return KBMap("clienta", kb_fiches(), dictionary)


class MemoryStore:
    def __init__(self):
        self.items = {}

    def read(self, relative):
        return self.items.get(relative)

    def write(self, relative, text):
        self.items[relative] = text


def walk(node):
    yield node
    for value in (node.get("properties") or {}).values():
        yield from walk(value)
    if isinstance(node.get("items"), dict):
        yield from walk(node["items"])


class SchemaTest(unittest.TestCase):
    def test_the_schema_is_strict_and_applications_come_from_the_closed_vocabulary(self):
        apps = app_vocabulary({"harmony": ["Harmony"]})
        self.assertIn("harmony", apps)
        self.assertIn("teams", apps)
        self.assertEqual(apps, sorted(apps))
        s = schema(apps)
        for node in walk(s):
            if node.get("type") == "object":
                self.assertFalse(node["additionalProperties"])
                self.assertEqual(sorted(node["required"]), sorted(node["properties"]))
        self.assertEqual(s["properties"]["primary_app"]["enum"], [""] + apps)
        self.assertEqual(s["properties"]["supported_apps"]["items"]["enum"], [""] + apps)

    def test_the_prompt_carries_the_title_the_vocabulary_and_the_fiche_between_markers(self):
        prompt = user_prompt("Associate a phone line", "Open the Teams admin center.", ["office", "teams"])
        self.assertTrue(prompt.startswith("Procedure title: Associate a phone line\n"))
        self.assertIn("Allowed application ids: office, teams", prompt)
        self.assertIn("<<<PROCEDURE\nOpen the Teams admin center.\nPROCEDURE>>>", prompt)

    def test_intent_labels_become_upper_snake_case_or_nothing(self):
        self.assertEqual(canonical_intent("Attribution ligne téléphonique"), "ATTRIBUTION_LIGNE_TELEPHONIQUE")
        self.assertEqual(canonical_intent("  déverrouillage-compte  "), "DEVERROUILLAGE_COMPTE")
        for bad in ("", "  ", "ab", "123_ABC", "x" * 80):
            self.assertIsNone(canonical_intent(bad), bad)


class ProposalTest(unittest.TestCase):
    def test_kb0233_gets_its_intent_teams_proven_by_a_quote_and_its_french_aliases(self):
        e = make_enrichment(TitleLLM({"Associate a phone line": KB0233_ANSWER}), kbmap(), PHONE_LINE)
        self.assertEqual(e.canonical_intent, "ATTRIBUTION_LIGNE_TELEPHONIQUE")
        self.assertEqual((e.primary_app, e.supported_apps), ("teams", ["teams", "office"]))
        self.assertEqual(e.app_evidence, [{"app": "teams", "quote": "Open the Teams admin center", "source": "quote"}])
        self.assertEqual(e.apps_inferred, ["office"])          # claimed, but the fiche never names it
        self.assertFalse(e.app_inferred)                       # the primary application is proven
        self.assertEqual(e.semantic_aliases_fr, ["attribuer une ligne teams", "affecter un numéro teams",
                                                 "créer une ligne téléphonique teams"])  # the duplicate is gone
        self.assertEqual(e.semantic_aliases_en, ["assign a teams phone number"])
        self.assertEqual(e.trigger_keywords, ["ligne", "numéro", "teams", "attribuer"])
        self.assertEqual((e.status, e.error, e.label), ("proposed", None, PHONE_LINE))
        self.assertEqual(Enrichment.from_dict(e.to_dict()), e)

    def test_an_application_the_fiche_never_names_is_kept_but_marked_inferred(self):
        answer = {**KB0233_ANSWER, "app_evidence": [{"app": "teams", "quote": "Assign a phone line in Teams"}]}
        fiches = filler() + [make(PHONE_LINE, "Open the admin center. Assign a phone line to the user.")]
        e = make_enrichment(TitleLLM({"Associate a phone line": answer}), KBMap("clienta", fiches), PHONE_LINE)
        self.assertEqual(e.primary_app, "teams")
        self.assertTrue(e.app_inferred)
        self.assertEqual(e.apps_inferred, ["teams", "office"])
        self.assertEqual(e.app_evidence, [])
        self.assertIn({"field": "app_evidence", "text": "teams: Assign a phone line in Teams",
                       "reason": "quote not found verbatim in the fiche"}, e.dropped)

    def test_an_application_kecore_read_on_the_fiche_needs_no_quote(self):
        answer = {**KB0233_ANSWER, "app_evidence": []}
        e = make_enrichment(TitleLLM({"Associate a phone line": answer}), kbmap(), PHONE_LINE)
        self.assertEqual(e.app_evidence, [{"app": "teams", "quote": None, "source": "fiche"}])
        self.assertFalse(e.app_inferred)

    def test_a_real_quote_that_does_not_name_the_application_is_no_evidence(self):
        answer = {**KB0233_ANSWER, "supported_apps": ["teams", "office"],
                  "app_evidence": [{"app": "office", "quote": "Assign a phone line to the user"}]}
        e = make_enrichment(TitleLLM({"Associate a phone line": answer}), kbmap(), PHONE_LINE)
        self.assertEqual(e.apps_inferred, ["office"])
        self.assertIn("the quote does not name this application", [d["reason"] for d in e.dropped])

    def test_applications_outside_the_closed_vocabulary_are_dropped(self):
        answer = {**KB0233_ANSWER, "primary_app": "slack-pro", "supported_apps": ["slack-pro", "teams"],
                  "app_evidence": [{"app": "slack-pro", "quote": "Open the Teams admin center"}]}
        e = make_enrichment(TitleLLM({"Associate a phone line": answer}), kbmap(), PHONE_LINE)
        self.assertIsNone(e.primary_app)
        self.assertEqual(e.supported_apps, ["teams"])
        reasons = {(d["field"], d["reason"]) for d in e.dropped}
        self.assertIn(("primary_app", "not in the client's application vocabulary"), reasons)
        self.assertIn(("app_evidence", "application not claimed by this proposal"), reasons)

    def test_a_client_dictionary_application_is_in_the_vocabulary(self):
        answer = {"canonical_intent": "VALIDATION_FACTURE", "intent_label_fr": "Valider une facture",
                  "primary_app": "harmony", "supported_apps": ["harmony"],
                  "app_evidence": [{"app": "harmony", "quote": "Ouvrez Harmony"}],
                  "semantic_aliases_fr": ["valider une facture harmony"], "semantic_aliases_en": [],
                  "trigger_keywords": ["facture"]}
        llm = TitleLLM({"Valider une facture": answer})
        with_dictionary = make_enrichment(llm, kbmap({"harmony": ["Harmony"]}), HARMONY)
        self.assertEqual((with_dictionary.primary_app, with_dictionary.app_inferred), ("harmony", False))
        self.assertEqual(with_dictionary.semantic_aliases_fr, ["valider une facture harmony"])
        without = make_enrichment(llm, kbmap(), HARMONY)                  # not a known application: dropped
        self.assertIsNone(without.primary_app)

    def test_aliases_that_invent_or_stray_are_dropped_with_their_reason(self):
        answer = {**KB0233_ANSWER, "semantic_aliases_fr": [
            "ligne",                                                     # too short
            "comment faire pour attribuer une ligne teams à un nouvel utilisateur",  # too long
            "erreur 0x80070005 sur la ligne teams",                      # invented error code
            "voir la KB0999 pour la ligne teams",                        # invented fiche number
            "écrire à support@corp.example pour une ligne",              # contact detail
            "attribuer une ligne dans outlook",                          # an application not claimed
            "attribuer une ligne teams"]}
        e = make_enrichment(TitleLLM({"Associate a phone line": answer}), kbmap(), PHONE_LINE)
        self.assertEqual(e.semantic_aliases_fr, ["attribuer une ligne teams"])
        self.assertEqual(sorted(d["reason"] for d in e.dropped if d["field"] == "semantic_aliases_fr"),
                         ["adds app:outlook", "adds err:0x80070005", "adds kb:999", "contact detail", "too long",
                          "too short"])

    def test_a_keyword_must_appear_in_a_kept_alias_or_in_the_fiche(self):
        answer = {**KB0233_ANSWER, "trigger_keywords": ["ligne", "fax", "Teams", "admin center",
                                                        "un mot clé beaucoup trop long"]}
        e = make_enrichment(TitleLLM({"Associate a phone line": answer}), kbmap(), PHONE_LINE)
        self.assertEqual(e.trigger_keywords, ["ligne", "teams", "admin center"])
        self.assertEqual(sorted((d["text"], d["reason"]) for d in e.dropped if d["field"] == "trigger_keywords"),
                         [("fax", "neither in a kept alias nor in the fiche"), ("un mot clé beaucoup trop long", "too long")])

    def test_an_unusable_intent_is_dropped_not_guessed(self):
        answer = {**KB0233_ANSWER, "canonical_intent": "??"}
        e = make_enrichment(TitleLLM({"Associate a phone line": answer}), kbmap(), PHONE_LINE)
        self.assertIsNone(e.canonical_intent)
        self.assertIn(("canonical_intent", "??"), {(d["field"], d["text"]) for d in e.dropped})

    def test_a_model_failure_gives_an_empty_proposal_with_its_error(self):
        e = make_enrichment(TitleLLM({"Associate a phone line": LLMError("timeout")}), kbmap(), PHONE_LINE)
        self.assertIn("timeout", e.error)
        self.assertEqual((e.canonical_intent, e.primary_app, e.semantic_aliases_fr), (None, None, []))
        self.assertEqual(e.provenance["prompt_version"], 1)

    def test_a_malformed_answer_is_read_defensively(self):
        answer = {"canonical_intent": 42, "primary_app": ["teams"], "supported_apps": "teams",
                  "app_evidence": ["teams"], "semantic_aliases_fr": [None, 3, "attribuer une ligne teams"],
                  "trigger_keywords": [None]}
        e = make_enrichment(TitleLLM({"Associate a phone line": answer}), kbmap(), PHONE_LINE)
        self.assertIsNone(e.primary_app)
        self.assertEqual(e.semantic_aliases_fr, [])     # names teams, which this answer never validly claims
        self.assertIsNone(e.error)

    def test_stats_point_at_what_a_person_should_look_at_first(self):
        llm = TitleLLM({"Associate a phone line": KB0233_ANSWER, "LOCKED ACCOUNT": LLMError("timeout")})
        m = kbmap()
        s = stats([make_enrichment(llm, m, PHONE_LINE), make_enrichment(llm, m, LOCKED)])
        self.assertEqual((s["fiches"], s["errors"], s["with_intent"], s["with_primary_app"]), (2, 1, 1, 1))
        self.assertEqual((s["app_inferred"], s["aliases"]), ([], 4))


class ReplayTest(unittest.TestCase):
    def test_a_replay_writes_the_same_bytes_with_no_model_call(self):
        store = MemoryStore()
        model = TitleLLM({"Associate a phone line": KB0233_ANSWER})
        recorded = make_enrichment(RecordingLLM(model, store=store, mode="record"), kbmap(), PHONE_LINE)
        self.assertEqual(len(model.calls), 1)
        self.assertEqual(model.calls[0][0], SCHEMA_NAME)
        replayed = make_enrichment(RecordingLLM(None, store=store, mode="replay", model_id=model.model_id),
                                   kbmap(), PHONE_LINE)
        self.assertEqual(json.dumps(replayed.to_dict(), sort_keys=True), json.dumps(recorded.to_dict(), sort_keys=True))
        self.assertEqual(len(model.calls), 1)

    def test_replay_without_a_record_fails_into_the_error_field_never_a_guess(self):
        e = make_enrichment(RecordingLLM(None, store=MemoryStore(), mode="replay", model_id="x"), kbmap(), PHONE_LINE)
        self.assertIn("replay", e.error)
        self.assertIsNone(e.canonical_intent)

    def test_a_changed_fiche_is_a_new_request_and_a_new_content_hash(self):
        store = MemoryStore()
        model = TitleLLM({"Associate a phone line": KB0233_ANSWER})
        before = make_enrichment(RecordingLLM(model, store=store), kbmap(), PHONE_LINE)
        changed = KBMap("clienta", filler() + [make(PHONE_LINE, "Open the Teams admin center. Assign a number.",
                                                    entities=["app:teams"])])
        after = make_enrichment(RecordingLLM(model, store=store), changed, PHONE_LINE)
        self.assertEqual(len(model.calls), 2)
        self.assertNotEqual(before.content_sha256, after.content_sha256)
        self.assertNotEqual(before.provenance["request_sha256"], after.provenance["request_sha256"])


if __name__ == "__main__":
    unittest.main()
