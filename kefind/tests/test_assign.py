"""Pass C (kefind.assign): one intent of the frozen taxonomy and short core requests per fiche -- the model
chooses in the closed list, the code keeps what can route and says why it dropped the rest; a replay
rewrites the same bytes with no model call."""

import json
import unittest

from kecore.llm import LLMError, RecordingLLM
from kefind import enrich
from kefind.assign import (ASSIGN_PROMPT_VERSION, C_LLM_OVERRIDES, MAX_CORE_ALIASES, SCHEMA_NAME, SYSTEM_PROMPT,
                           Assignment, claimed_apps, make_assignment, schema, stats, user_prompt)
from kefind.enrich import MAX_FICHE_CHARS, Enrichment
from kefind.funnel import KBMap
from kefind.taxonomy import freeze

from .schema_llm import SchemaLLM
from .test_enrich import HARMONY, LOCKED, PHONE_LINE, TRANSFER, MemoryStore, kbmap, walk
from .test_funnel import filler, make

PHONE_TITLE = "Associate a phone line"
LOCKED_TITLE = "LOCKED ACCOUNT"

C_ANSWER = {
    "intent": "ATTRIBUTION_LIGNE_TELEPHONIQUE",
    "core_aliases_fr": ["attribuer ligne teams", "affecter  numéro teams", "Attribuer ligne Teams",
                        "attribuer lignes teams"],
    "core_aliases_en": ["assign teams number", "Assign Teams numbers"],
}


def taxonomy(version=1, description="Attribuer un numéro de téléphone à un utilisateur."):
    return freeze([
        {"id": "TRANSFERT_APPELS", "label_fr": "Transférer les appels", "description_fr": "Renvoyer les appels."},
        {"id": "ATTRIBUTION_LIGNE_TELEPHONIQUE", "label_fr": "Attribuer une ligne téléphonique",
         "description_fr": description},
        {"id": "DEVERROUILLAGE_COMPTE", "label_fr": "Déverrouiller un compte", "description_fr": ""},
    ], version, "Yassine", "2026-10-13T10:00:00Z", "b-1")


def llm(answer=C_ANSWER, title=PHONE_TITLE):
    return SchemaLLM({(SCHEMA_NAME, title): answer})


def assign(answer, fiche_id=PHONE_LINE, title=PHONE_TITLE, m=None, enrichment=None, t=None):
    return make_assignment(llm(answer, title), m or kbmap(), fiche_id, t or taxonomy(), enrichment)


def pass_a(fiche_id, m, **values):
    return Enrichment(fiche_id=fiche_id, label=m.label(fiche_id),
                      content_sha256=enrich.content_sha256(m.fiches[fiche_id]), **values)


class SchemaTest(unittest.TestCase):
    def test_the_schema_is_strict_and_the_intent_comes_from_the_frozen_taxonomy(self):
        t = taxonomy()
        s = schema(t.ids())
        for node in walk(s):
            if node.get("type") == "object":
                self.assertFalse(node["additionalProperties"])
                self.assertEqual(sorted(node["required"]), sorted(node["properties"]))
        self.assertEqual(s["properties"]["intent"]["enum"],
                         ["", "ATTRIBUTION_LIGNE_TELEPHONIQUE", "DEVERROUILLAGE_COMPTE", "TRANSFERT_APPELS"])
        self.assertEqual(sorted(s["properties"]), ["core_aliases_en", "core_aliases_fr", "intent"])
        self.assertEqual(schema([])["properties"]["intent"]["enum"], [""])

    def test_the_prompt_carries_the_title_the_intents_without_description_and_the_fiche(self):
        prompt = user_prompt(PHONE_TITLE, "<p>Open the Teams admin center.</p>", taxonomy())
        self.assertEqual(prompt.split("\n")[0], "Procedure title: Associate a phone line")
        self.assertIn("\nIntents (id: label):\nATTRIBUTION_LIGNE_TELEPHONIQUE: Attribuer une ligne téléphonique\n"
                      "DEVERROUILLAGE_COMPTE: Déverrouiller un compte\nTRANSFERT_APPELS: Transférer les appels\n",
                      prompt)
        self.assertNotIn("Renvoyer les appels.", prompt)                       # no description
        self.assertTrue(prompt.endswith("<<<PROCEDURE\nOpen the Teams admin center.\nPROCEDURE>>>"))   # clean_text
        long = user_prompt(PHONE_TITLE, "x" * (MAX_FICHE_CHARS + 50), taxonomy().to_dict())   # a dict reads too
        self.assertIn("\n<<<PROCEDURE\n" + "x" * MAX_FICHE_CHARS + "\nPROCEDURE>>>", long)

    def test_the_system_prompt_asks_for_short_core_requests_and_allows_no_intent(self):
        for words in ('"" if no intent of the list fits', "2 to 4 words", "no article", "up to 4", "up to 2",
                      "Never use a single letter or a single digit", "e-mail address", "data"):
            self.assertIn(words, SYSTEM_PROMPT)
        self.assertEqual(C_LLM_OVERRIDES, {"max_tokens": 800})


class AssignmentTest(unittest.TestCase):
    def test_kb0233_gets_its_intent_and_short_requests_that_route(self):
        m = kbmap()
        a = assign(C_ANSWER, m=m)
        self.assertEqual(a.intent, "ATTRIBUTION_LIGNE_TELEPHONIQUE")
        # "Attribuer ligne Teams" and "attribuer lignes teams" have the same routing tokens: one alias
        self.assertEqual(a.core_aliases_fr, ["attribuer ligne teams", "affecter numéro teams"])
        self.assertEqual(a.core_aliases_en, ["assign teams number"])     # the plural folds: the same alias
        self.assertEqual(a.dropped, [])
        self.assertEqual((a.fiche_id, a.label, a.error, a.version),
                         (PHONE_LINE, PHONE_LINE, None, ASSIGN_PROMPT_VERSION))
        self.assertEqual(a.content_sha256, enrich.content_sha256(m.fiches[PHONE_LINE]))
        t = taxonomy()
        self.assertEqual(sorted(a.provenance), ["model", "prompt_version", "request_sha256", "taxonomy_sha256",
                                                "taxonomy_version"])
        self.assertEqual((a.provenance["taxonomy_version"], a.provenance["taxonomy_sha256"]), (1, t.sha256))
        self.assertEqual((a.provenance["prompt_version"], a.provenance["model"]), (ASSIGN_PROMPT_VERSION, "schema-llm"))
        self.assertEqual(Assignment.from_dict(json.loads(json.dumps(a.to_dict()))), a)

    def test_an_empty_intent_is_no_intent_and_an_intent_outside_the_taxonomy_is_dropped(self):
        a = assign({**C_ANSWER, "intent": ""})
        self.assertIsNone(a.intent)
        self.assertEqual(a.dropped, [])
        a = assign({**C_ANSWER, "intent": "ATTRIBUTION_NUMERO"})
        self.assertIsNone(a.intent)
        self.assertEqual(a.dropped, [{"field": "intent", "text": "ATTRIBUTION_NUMERO",
                                      "reason": "not in the frozen taxonomy"}])
        a = assign({**C_ANSWER, "intent": "attribution_ligne_telephonique"})        # never canonicalized into the enum
        self.assertIsNone(a.intent)

    def test_core_aliases_that_cannot_route_or_that_invent_are_dropped_with_their_reason(self):
        answer = {"intent": "ATTRIBUTION_LIGNE_TELEPHONIQUE", "core_aliases_fr": [
            "ligne",                                  # one word
            "attribuer une nouvelle ligne teams",     # five words
            "appeler 0612345678 teams",               # a phone number
            "erreur 0x80070005 teams",                # an error code the fiche does not have
            "voir KB0999 teams",                      # a fiche number it does not cite
            "attribuer ligne outlook",                # an application the fiche does not claim
            "attribuer ligne 2",                      # a one-character token routing ignores
            "mapper lecteur H",                       # a one-letter token routing ignores
            "le la",                                  # no routing token
            "teams teams",                            # one routing token
            "attribuer ligne teams",
        ], "core_aliases_en": []}
        a = assign(answer)
        self.assertEqual(a.core_aliases_fr, ["attribuer ligne teams"])
        self.assertEqual([(d["text"], d["reason"]) for d in a.dropped], [
            ("ligne", "too short"),
            ("attribuer une nouvelle ligne teams", "too long"),
            ("appeler 0612345678 teams", "contact detail"),
            ("erreur 0x80070005 teams", "adds err:0x80070005"),
            ("voir KB0999 teams", "adds kb:999"),
            ("attribuer ligne outlook", "adds app:outlook"),
            ("attribuer ligne 2", "le routage ignore « 2 » : écrire l'alias sans, ou avec le mot entier"),
            ("mapper lecteur H", "le routage ignore « h » : écrire l'alias sans, ou avec le mot entier"),
            ("le la", "moins de 2 mots utiles au routage"),
            ("teams teams", "moins de 2 mots utiles au routage"),
        ])
        self.assertTrue(all(d["field"] == "core_aliases_fr" for d in a.dropped))
        self.assertEqual(a.intent, "ATTRIBUTION_LIGNE_TELEPHONIQUE")

    def test_six_aliases_per_language_at_most(self):
        fr = [f"attribuer ligne {w}" for w in ("teams", "fixe", "mobile", "interne", "externe", "directe", "standard",
                                                "secondaire")]
        a = assign({"intent": "", "core_aliases_fr": fr, "core_aliases_en": ["assign teams number"]})
        self.assertEqual(a.core_aliases_fr, fr[:MAX_CORE_ALIASES])
        self.assertEqual(a.dropped, [{"field": "core_aliases_fr", "text": alias, "reason": "more than 6 aliases"}
                                     for alias in fr[MAX_CORE_ALIASES:]])
        self.assertEqual(a.core_aliases_en, ["assign teams number"])            # the cap is per language

    def test_an_english_alias_with_the_routing_tokens_of_a_french_one_is_the_same_alias(self):
        a = assign({"intent": "", "core_aliases_fr": ["attribuer ligne teams"],
                    "core_aliases_en": ["Teams ligne attribuer", "assign teams line"]})
        self.assertEqual(a.core_aliases_en, ["assign teams line"])
        self.assertEqual(a.dropped, [])

    def test_the_applications_an_alias_may_name(self):
        m = kbmap({"harmony": ["Harmony"]})
        # read by kecore on the fiche (its entities)
        self.assertEqual(claimed_apps(m, PHONE_LINE, "", None), {"teams"})
        # read in the fiche's text with the client's dictionary
        a = assign({"intent": "", "core_aliases_fr": ["valider facture harmony"], "core_aliases_en": []},
                   HARMONY, "Valider une facture", m)
        self.assertEqual(a.core_aliases_fr, ["valider facture harmony"])
        # claimed by pass A only (the fiche never names it): allowed, a person validates it
        alias = {"intent": "DEVERROUILLAGE_COMPTE", "core_aliases_fr": ["débloquer compte active directory"],
                 "core_aliases_en": []}
        inferred = pass_a(LOCKED, m, primary_app="active-directory", supported_apps=["active-directory"],
                          apps_inferred=["active-directory"], app_inferred=True)
        a = assign(alias, LOCKED, LOCKED_TITLE, m, inferred)
        self.assertEqual(a.core_aliases_fr, ["débloquer compte active directory"])
        self.assertEqual(a.intent, "DEVERROUILLAGE_COMPTE")
        a = assign(alias, LOCKED, LOCKED_TITLE, m, inferred.to_dict())          # a stored proposal reads too
        self.assertEqual(a.core_aliases_fr, ["débloquer compte active directory"])
        # no pass A, pass A of another fiche, of another text or in error: the fiche does not claim it
        others = [None, pass_a(PHONE_LINE, m, primary_app="active-directory"),
                  Enrichment(fiche_id=LOCKED, label=LOCKED, content_sha256="0" * 64, primary_app="active-directory"),
                  pass_a(LOCKED, m, primary_app="active-directory", error="timeout")]
        for other in others:
            a = assign(alias, LOCKED, LOCKED_TITLE, m, other)
            self.assertEqual(a.core_aliases_fr, [], other)
            self.assertEqual(a.dropped, [{"field": "core_aliases_fr", "text": "débloquer compte active directory",
                                          "reason": "adds app:active-directory"}])

    def test_a_supported_application_of_pass_a_is_claimed_too(self):
        m = kbmap()
        support = pass_a(PHONE_LINE, m, primary_app="teams", supported_apps=["teams", "outlook"],
                         apps_inferred=["outlook"])
        a = assign({**C_ANSWER, "core_aliases_fr": ["attribuer ligne outlook"]}, m=m, enrichment=support)
        self.assertEqual(a.core_aliases_fr, ["attribuer ligne outlook"])

    def test_a_model_failure_gives_an_empty_assignment_with_its_error(self):
        a = assign(LLMError("the answer was cut at max_tokens (800)"))
        self.assertEqual((a.error, a.intent, a.core_aliases_fr), ("the answer was cut at max_tokens (800)", None, []))
        self.assertNotIn("model", a.provenance)
        self.assertEqual(a.provenance["taxonomy_version"], 1)
        a = assign(TimeoutError("The read operation timed out"))
        self.assertEqual(a.error, "TimeoutError: The read operation timed out")
        a = make_assignment(None, kbmap(), PHONE_LINE, taxonomy(), None)
        self.assertTrue(a.error.startswith("AttributeError: "))

    def test_a_malformed_answer_is_read_defensively(self):
        a = assign({"intent": 42, "core_aliases_fr": "attribuer ligne teams",
                    "core_aliases_en": [None, 3, "  ", "assign teams number"]})
        self.assertIsNone(a.intent)
        self.assertEqual(a.dropped, [{"field": "intent", "text": "42", "reason": "not in the frozen taxonomy"}])
        self.assertEqual((a.core_aliases_fr, a.core_aliases_en), ([], ["assign teams number"]))
        a = assign(["not", "a", "dict"])
        self.assertEqual((a.intent, a.core_aliases_fr, a.dropped, a.error), (None, [], [], None))
        a = assign({"intent": None})
        self.assertEqual((a.intent, a.dropped), (None, []))

    def test_pass_a_and_pass_c_answers_for_the_same_title_never_mix(self):
        model = SchemaLLM({(SCHEMA_NAME, PHONE_TITLE): C_ANSWER,
                           (enrich.SCHEMA_NAME, PHONE_TITLE): {"canonical_intent": "AUTRE_CHOSE"}})
        a = make_assignment(model, kbmap(), PHONE_LINE, taxonomy(), None)
        self.assertEqual(a.intent, "ATTRIBUTION_LIGNE_TELEPHONIQUE")
        self.assertEqual(model.calls, [(SCHEMA_NAME, PHONE_TITLE)])

    def test_assignment_reads_tolerantly(self):
        self.assertEqual(Assignment.from_dict({"fiche_id": "F", "unknown": 1}), Assignment(fiche_id="F", label="",
                                                                                          content_sha256=""))


class StatsTest(unittest.TestCase):
    def test_stats_count_errors_coverage_aliases_and_intents(self):
        m = kbmap()
        model = SchemaLLM({(SCHEMA_NAME, PHONE_TITLE): C_ANSWER,
                           (SCHEMA_NAME, "Transfert d'appels TEAMS"): {"intent": "TRANSFERT_APPELS",
                                                                       "core_aliases_fr": ["transférer appels teams",
                                                                                           "ligne"],
                                                                       "core_aliases_en": []},
                           (SCHEMA_NAME, LOCKED_TITLE): {"intent": "", "core_aliases_fr": [], "core_aliases_en": []},
                           (SCHEMA_NAME, "Valider une facture"): LLMError("429")})
        t = taxonomy()
        items = [make_assignment(model, m, f, t, None) for f in (PHONE_LINE, TRANSFER, LOCKED, HARMONY)]
        s = stats(items)
        self.assertEqual(s, {"fiches": 4, "errors": 1, "with_intent": 2, "without_intent": 1, "core_aliases": 4,
                             "dropped": 1, "by_intent": {"ATTRIBUTION_LIGNE_TELEPHONIQUE": 1, "TRANSFERT_APPELS": 1}})
        self.assertEqual(stats([i.to_dict() for i in items]), s)
        self.assertEqual(list(s["by_intent"]), sorted(s["by_intent"]))
        self.assertEqual(stats([]), {"fiches": 0, "errors": 0, "with_intent": 0, "without_intent": 0,
                                     "core_aliases": 0, "dropped": 0, "by_intent": {}})


class ReplayTest(unittest.TestCase):
    def test_a_replay_writes_the_same_bytes_with_no_model_call(self):
        store = MemoryStore()
        model = llm()
        recorded = make_assignment(RecordingLLM(model, store=store, mode="record"), kbmap(), PHONE_LINE, taxonomy(),
                                   None)
        self.assertEqual(model.calls, [(SCHEMA_NAME, PHONE_TITLE)])
        replayed = make_assignment(RecordingLLM(None, store=store, mode="replay", model_id=model.model_id), kbmap(),
                                   PHONE_LINE, taxonomy(), None)
        self.assertEqual(json.dumps(replayed.to_dict(), sort_keys=True, ensure_ascii=False),
                         json.dumps(recorded.to_dict(), sort_keys=True, ensure_ascii=False))
        self.assertEqual(len(model.calls), 1)

    def test_replay_without_a_record_fails_into_the_error_field_never_a_guess(self):
        a = make_assignment(RecordingLLM(None, store=MemoryStore(), mode="replay", model_id="x"), kbmap(), PHONE_LINE,
                            taxonomy(), None)
        self.assertIn("replay", a.error)
        self.assertIsNone(a.intent)

    def test_a_description_only_refreeze_replays_but_a_new_intent_is_a_new_request(self):
        store = MemoryStore()
        model = llm()
        first = make_assignment(RecordingLLM(model, store=store), kbmap(), PHONE_LINE, taxonomy(1), None)
        described = make_assignment(RecordingLLM(model, store=store), kbmap(), PHONE_LINE,
                                    taxonomy(2, "Autre description."), None)
        self.assertEqual(len(model.calls), 1)                  # same prompt and schema: answered from the record
        self.assertEqual(described.provenance["request_sha256"], first.provenance["request_sha256"])
        self.assertNotEqual(described.provenance["taxonomy_sha256"], first.provenance["taxonomy_sha256"])
        self.assertEqual(described.provenance["taxonomy_version"], 2)
        wider = freeze(taxonomy().intents + [{"id": "CREATION_COMPTE", "label_fr": "Créer un compte"}],
                       3, "Y", "t", "b")
        make_assignment(RecordingLLM(model, store=store), kbmap(), PHONE_LINE, wider, None)
        self.assertEqual(len(model.calls), 2)

    def test_a_changed_fiche_is_a_new_request_and_a_new_content_hash(self):
        store = MemoryStore()
        model = llm()
        before = make_assignment(RecordingLLM(model, store=store), kbmap(), PHONE_LINE, taxonomy(), None)
        changed = KBMap("clienta", filler() + [make(PHONE_LINE, "Open the Teams admin center. Assign a number.",
                                                    entities=["app:teams"])])
        after = make_assignment(RecordingLLM(model, store=store), changed, PHONE_LINE, taxonomy(), None)
        self.assertEqual(len(model.calls), 2)
        self.assertNotEqual(before.content_sha256, after.content_sha256)
        self.assertNotEqual(before.provenance["request_sha256"], after.provenance["request_sha256"])


if __name__ == "__main__":
    unittest.main()
