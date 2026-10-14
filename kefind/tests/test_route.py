"""kefind.route: the one answer for live /find, the scoreboard and the tickets run (levels L0 to L4)."""

import hashlib
import json
import sys
import unittest
from pathlib import Path

from kecore.llm import LLMError
from kefind import semantic as sem
from kefind.cards import anchors_for, entries_for
from kefind.funnel import FunnelConfig, KBMap, find
from kefind.funnel_engine import FunnelEngine
from kefind.interpret import interpret
from kefind.review import ReviewDecision
from kefind.route import (EXACT_LEVELS, MAP_SCHEMA_NAME, MAP_SYSTEM_PROMPT, TablesMismatchError, answer, check_tables,
                          map_prompt, map_question, map_schema, question_vector)
from kefind.routing import CODE_VERSION, ROUTE_VERSION, RoutingTables, build_tables
from kefind.taxonomy import freeze
from scoreboard.dataset import Ticket
from scoreboard.runner import make_record, run_engine

from .helpers import StubLLM
from .schema_llm import SchemaLLM
from .semantic_helpers import DIMS, BrokenEmbedder, ConceptEmbedder
from .test_funnel import filler, make
from .test_semantic import CALIBRATED, CARDS

TRANSFER = "KB0217 - Transfert d'appels TEAMS"
TEAMS = "KB0233 - Associate a phone line"
ACCOUNT = "KB0120 - LOCKED ACCOUNT"
WEBEX = "KB0240 - Webex phone line"
PASSWORD = "KB0600 - Réinitialiser un mot de passe"
SAP_PASSWORD = "KB0601 - Mot de passe SAP"
DESK_PHONE = "KB0906 - téléphone fixe tonalité"   # a filler fiche, never reviewed

ROUTE_KEYS = {"version", "level", "rule", "key", "fiche_id", "tables_sha256", "content_sha256", "build_id", "run_id",
              "l0", "l1", "l2"}
L2_KEYS = {"intent", "app", "app_source", "model_app", "error", "exact", "offer", "reason"}


def fiches():
    return filler() + [
        make(TRANSFER, "Ouvrez Teams. Configurez le transfert d'appels vers un autre numero.", entities=["app:teams"]),
        make(TEAMS, "Open the Teams admin center. Assign a phone line to the user.", entities=["app:teams"]),
        make(ACCOUNT, "The user account is locked. Unlock the account in the console. Error 0x80070005 may show.",
             entities=["err:0x80070005"]),
        make(WEBEX, "Open Control Hub. Assign a Webex phone line.", entities=["app:webex"]),
        make(PASSWORD, "Réinitialisez le mot de passe Windows dans la console."),
        make(SAP_PASSWORD, "Réinitialisez le mot de passe SAP dans la transaction SU01.", entities=["app:sap"]),
    ]


def words_map(run_id="r1", client="clienta") -> KBMap:
    return KBMap(client, fiches(), run_id=run_id)


def semantic_map(calibrated=True) -> KBMap:
    plain = words_map()
    index, _ = sem.build(entries_for(plain, CARDS), ConceptEmbedder(), "concept-embed@test", DIMS,
                         anchors=anchors_for(plain))
    if calibrated:
        index.calibration = {**CALIBRATED, "index_sha256": index.sha256}
    return KBMap("clienta", fiches(), run_id="r1", semantic=index)


CURRENT = {f.fiche_id: hashlib.sha256(f"text of {f.fiche_id}".encode("utf-8")).hexdigest() for f in fiches()}
TAXONOMY = freeze([{"id": "ATTRIBUER_LIGNE", "label_fr": "Attribuer une ligne téléphonique"},
                   {"id": "TRANSFERER_APPELS", "label_fr": "Transférer des appels"},
                   {"id": "DEVERROUILLER_COMPTE", "label_fr": "Déverrouiller un compte"},
                   {"id": "REINITIALISER_MOT_DE_PASSE", "label_fr": "Réinitialiser un mot de passe"}],
                  1, "Yassine", "2026-10-14T09:00:00Z", "b0")


def decided(fiche_id, intent=None, app=None, aliases=(), codes=(), status="validated") -> ReviewDecision:
    return ReviewDecision(fiche_id=fiche_id, source_sha256=CURRENT[fiche_id], status=status, intent=intent,
                          intent_fingerprint=TAXONOMY.fingerprint(intent) if intent else "",
                          taxonomy_version=TAXONOMY.version if intent else 0, primary_app=app,
                          aliases=[{"text": a, "lang": "fr", "source": "manual", "ref_id": ""} for a in aliases],
                          codes=list(codes), decided_by="Yassine", decided_at="2026-10-14T10:00:00Z")


def decisions():
    return [decided(TEAMS, "ATTRIBUER_LIGNE", "teams", aliases=["attribuer ligne teams"]),
            decided(WEBEX, "ATTRIBUER_LIGNE", "webex"),
            decided(TRANSFER, "TRANSFERER_APPELS", "teams"),
            decided(ACCOUNT, "DEVERROUILLER_COMPTE", codes=["err:0x80070005"], aliases=["compte bloqué"]),
            decided(PASSWORD, "REINITIALISER_MOT_DE_PASSE"),
            decided(SAP_PASSWORD, "REINITIALISER_MOT_DE_PASSE", "sap")]


def tables_for(kbmap, decision_list=None, proposals=None, taxonomy=TAXONOMY, build_id="b1") -> RoutingTables:
    return build_tables(kbmap, CURRENT, taxonomy, decisions() if decision_list is None else decision_list,
                        proposals or {}, build_id)


def mapper(mapping=None, default=None) -> SchemaLLM:
    """The L2 model: {question: (intent, app)}, keyed the way the record is (the prompt's first line)."""
    answers = {(MAP_SCHEMA_NAME, sem.query_text(q)): {"intent": i, "app": a} for q, (i, a) in (mapping or {}).items()}
    return SchemaLLM(answers, default=default if default is not None else {"intent": "", "app": ""})


class FailingEmbedder(ConceptEmbedder):
    """The index's own model, out of service."""

    def embed(self, texts, dimensions=None):
        raise LLMError("the embedding service is down")


class RouteTest(unittest.TestCase):
    """Each level end to end, on real tables (kefind.routing.build_tables) and a real semantic index."""

    @classmethod
    def setUpClass(cls):
        cls.kbmap = semantic_map()

    def setUp(self):
        self.embedder = ConceptEmbedder()
        self.llm = StubLLM({"terms": ["phone line"], "application": None})

    def ask(self, text, tables=None, intent_llm=None, config=None, answers=(), current=CURRENT, kbmap=None, **kw):
        kbmap = kbmap or self.kbmap
        tables = tables if tables is not None else tables_for(kbmap)
        return answer(kbmap, text, answers, config, llm=self.llm, embedder=self.embedder, routing=tables,
                      current=current, intent_llm=intent_llm, **kw)

    def assert_exact(self, result, level, rule, key, fiche_id):
        self.assertEqual((result.route["level"], result.route["rule"], result.route["key"], result.route["fiche_id"]),
                         (level, rule, key, fiche_id))
        finding = result.finding
        self.assertEqual((finding.kind, finding.reason, finding.fiche_id, finding.fiches, finding.score,
                          finding.designated), ("fiche", f"route_{level.lower()}_{rule}", fiche_id, [fiche_id], None, True))
        self.assertEqual((result.mode, result.interpreted, result.degraded), ("routed", None, None))
        self.assertEqual(finding.trace, [{"step": "route", "level": level, "rule": rule, "key": key, "fiche_id": fiche_id},
                                         {"step": "decide", "kind": "fiche", "reason": finding.reason,
                                          "fiche_id": fiche_id, "asks": None}])
        self.assertEqual(self.llm.calls, [])        # no interpretation...
        self.assertEqual(self.embedder.calls, [])   # ...and no embedding for an exact route

    # --- L0 -------------------------------------------------------------------------------------------
    def test_l0_a_fiche_number_routes_before_anything_else(self):
        intent_llm = mapper()
        result = self.ask("voir la KB0120 svp, rien à voir avec teams", intent_llm=intent_llm)
        self.assert_exact(result, "L0", "fiche_number", "120", ACCOUNT)
        self.assertEqual(intent_llm.calls, [])
        self.assertEqual(result.route["l0"]["scope"]["intersection"], [ACCOUNT])
        self.assertEqual((result.route["l1"], result.route["l2"]), (None, None))
        self.assertEqual(result.usage, {"input_tokens": 0, "output_tokens": 0})

    def test_l0_a_validated_code_routes(self):
        result = self.ask("J'ai l'erreur 0X80070005 au démarrage")
        self.assert_exact(result, "L0", "identifier", "err:0x80070005", ACCOUNT)

    def test_l0_a_code_nobody_validated_does_not_route(self):
        tables = tables_for(self.kbmap, [d for d in decisions() if d.fiche_id != ACCOUNT])
        result = self.ask("J'ai l'erreur 0x80070005 au démarrage", tables=tables)
        self.assertFalse(result.route["l0"]["decided"])
        self.assertNotIn(result.route["level"], EXACT_LEVELS)

    # --- L1 -------------------------------------------------------------------------------------------
    def test_l1_a_validated_alias_routes_without_any_model_call(self):
        intent_llm = mapper()
        result = self.ask("comment attribuer une ligne teams", intent_llm=intent_llm)
        self.assert_exact(result, "L1", "alias", "attribuer ligne teams", TEAMS)
        self.assertEqual(intent_llm.calls, [])
        self.assertEqual(result.route["l1"], {"matches": [{"fiche_id": TEAMS, "alias": "attribuer ligne teams"}],
                                              "ambiguous": False, "uncovered": 0, "outside_scope": False,
                                              "uncovered_negation": False, "generic_for_app": False,
                                              "blockers": []})
        self.assertFalse(result.route["l0"]["decided"])
        self.assertIsNone(result.route["l2"])

    def test_l1_a_question_saying_much_more_falls_through_to_l2(self):
        question = "attribuer une ligne teams au nouveau stagiaire de demain"
        intent_llm = mapper({question: ("ATTRIBUER_LIGNE", "")})
        result = self.ask(question, intent_llm=intent_llm)
        self.assertEqual(result.route["l1"]["uncovered"], 3)
        self.assert_exact(result, "L2", "intent_app", "ATTRIBUER_LIGNE|teams", TEAMS)
        self.assertEqual(result.route["l2"]["app_source"], "code")   # the code read "teams": the model's "" ignored

    def test_l1_an_alias_of_another_application_never_routes(self):
        tables = tables_for(self.kbmap, [decided(TEAMS, "ATTRIBUER_LIGNE", "teams", aliases=["attribuer une ligne téléphonique"]),
                                         decided(WEBEX, "ATTRIBUER_LIGNE", "webex")])
        question = "attribuer une ligne téléphonique Webex"
        result = self.ask(question, tables=tables, intent_llm=mapper({question: ("ATTRIBUER_LIGNE", "teams")}))
        self.assertEqual(result.route["l1"]["matches"], [])
        # L2: the code reads Webex, the model's "teams" never overrides it
        self.assert_exact(result, "L2", "intent_app", "ATTRIBUER_LIGNE|webex", WEBEX)
        self.assertEqual((result.route["l2"]["app_source"], result.route["l2"]["model_app"]), ("code", "teams"))

    def test_l1_and_l2_never_contradict_the_question_s_identifiers(self):
        tables = tables_for(self.kbmap, [d for d in decisions() if d.fiche_id != ACCOUNT])   # the code routes nothing
        question = "attribuer ligne teams 0x80070005"
        result = self.ask(question, tables=tables, intent_llm=mapper({question: ("ATTRIBUER_LIGNE", "teams")}))
        self.assertFalse(result.route["l0"]["decided"])
        self.assertTrue(result.route["l1"]["outside_scope"])
        self.assertEqual((result.route["l2"]["exact"], result.route["l2"]["reason"]), (None, "outside_scope"))
        self.assertIn(result.route["level"], ("L3", "L4"))
        self.assertNotEqual(result.finding.kind, "fiche")

    # --- L2 -------------------------------------------------------------------------------------------
    def test_l2_an_exact_key_routes_with_the_model_s_application(self):
        question = "renvoyer mes appels sur mon portable"
        intent_llm = mapper({question: ("TRANSFERER_APPELS", "teams")})
        result = self.ask(question, intent_llm=intent_llm)
        self.assertEqual(intent_llm.calls, [(MAP_SCHEMA_NAME, sem.query_text(question))])
        self.assertEqual(result.route["l2"], {"intent": "TRANSFERER_APPELS", "app": "teams", "app_source": "model",
                                              "model_app": "teams", "error": None, "exact": TRANSFER, "offer": [],
                                              "reason": "intent_app"})
        self.assertEqual(result.usage, {"input_tokens": 100, "output_tokens": 50})
        self.assertEqual(result.route["key"], "TRANSFERER_APPELS|teams")
        self.assertEqual(result.finding.reason, "route_l2_intent_app")
        self.assertEqual((result.mode, self.embedder.calls, self.llm.calls), ("routed", [], []))

    def test_l2_the_code_read_application_gives_the_exact_fiche(self):
        question = "réinitialiser le mot de passe SAP"
        result = self.ask(question, intent_llm=mapper({question: ("REINITIALISER_MOT_DE_PASSE", "")}))
        self.assertEqual((result.route["level"], result.route["fiche_id"], result.route["key"]),
                         ("L2", SAP_PASSWORD, "REINITIALISER_MOT_DE_PASSE|sap"))

    def test_l2_several_fiches_are_offered_never_shown(self):
        question = "réinitialiser le mot de passe"
        result = self.ask(question, intent_llm=mapper({question: ("REINITIALISER_MOT_DE_PASSE", "")}))
        route, finding = result.route, result.finding
        self.assertEqual((route["level"], route["rule"], route["key"], route["fiche_id"]),
                         ("L2", "intent_app", "REINITIALISER_MOT_DE_PASSE|", None))
        self.assertEqual((route["l2"]["offer"], route["l2"]["reason"]), ([PASSWORD, SAP_PASSWORD], "several_fiches"))
        self.assertEqual((finding.kind, finding.asks, finding.reason, finding.fiche_id),
                         ("question", "fiche", "route_l2_choice", None))
        self.assertEqual([o["fiche_id"] for o in finding.options], [PASSWORD, SAP_PASSWORD])
        self.assertEqual(finding.fiches, [PASSWORD, SAP_PASSWORD])
        self.assertTrue(finding.question.startswith("Laquelle de ces fiches"))
        self.assertEqual(finding.trace[0], {"step": "route", "level": "L2", "rule": "intent_app",
                                            "key": "REINITIALISER_MOT_DE_PASSE|",
                                            "offered": [PASSWORD, SAP_PASSWORD], "ordered_by": "id"})
        self.assertEqual((result.mode, result.degraded, self.embedder.calls), ("routed", None, []))

    def test_l2_offers_only_what_the_question_s_identifiers_allow(self):
        question = "réinitialiser le mot de passe, voir KB 0601"   # written loosely: L0 does not decide
        result = self.ask(question, intent_llm=mapper({question: ("REINITIALISER_MOT_DE_PASSE", "")}))
        self.assertEqual((result.route["l0"]["decided"], result.route["l0"]["scope"]["union"]), (False, [SAP_PASSWORD]))
        self.assertEqual((result.route["level"], result.route["l2"]["offer"], result.route["l2"]["reason"]),
                         ("L2", [SAP_PASSWORD], "several_fiches"))
        self.assertEqual([o["fiche_id"] for o in result.finding.options], [SAP_PASSWORD])
        self.assertTrue(result.finding.question.startswith("Cette fiche correspond-elle"))
        elsewhere = "réinitialiser le mot de passe, voir KB 0217"
        outside = self.ask(elsewhere, intent_llm=mapper({elsewhere: ("REINITIALISER_MOT_DE_PASSE", "")}))
        self.assertEqual((outside.route["l2"]["offer"], outside.route["l2"]["reason"]), ([], "outside_scope"))
        self.assertIn(outside.route["level"], ("L3", "L4"))

    def test_l2_a_key_a_fiche_under_review_proposes_is_offered_with_it(self):
        tables = tables_for(self.kbmap, proposals={DESK_PHONE: {"intent": "ATTRIBUER_LIGNE", "app": "teams",
                                                                "aliases": []}})
        question = "attribuer un numéro dans teams à un utilisateur"
        result = self.ask(question, tables=tables, intent_llm=mapper({question: ("ATTRIBUER_LIGNE", "teams")}))
        self.assertEqual((result.route["level"], result.route["l2"]["reason"]), ("L2", "pending_review"))
        self.assertEqual(result.route["l2"]["offer"], [TEAMS, DESK_PHONE])
        self.assertEqual(result.finding.kind, "question")
        self.assertEqual([o["fiche_id"] for o in result.finding.options], [TEAMS, DESK_PHONE])

    def test_l2_an_offer_larger_than_a_choice_follows_today_s_ranking(self):
        tables = tables_for(self.kbmap, [decided(TRANSFER, "ATTRIBUER_LIGNE", "teams"),
                                         decided(TEAMS, "ATTRIBUER_LIGNE", "teams"),
                                         decided(WEBEX, "ATTRIBUER_LIGNE", "teams")])
        question = "comment attribuer une ligne teams"
        intent_llm = mapper({question: ("ATTRIBUER_LIGNE", "teams")})
        by_id = self.ask(question, tables=tables, intent_llm=intent_llm)
        self.assertEqual(by_id.finding.fiches, [TRANSFER, TEAMS, WEBEX])
        self.assertEqual([o["fiche_id"] for o in by_id.finding.options], [TRANSFER, TEAMS, WEBEX])
        self.assertEqual(self.embedder.calls, [])   # a choice that holds them all needs no ranking

        ranked = self.ask(question, tables=tables, intent_llm=intent_llm, config=FunnelConfig(max_choices=2))
        l3 = find(self.kbmap, question, query_vector=ConceptEmbedder().embed([sem.query_text(question)])[0],
                  config=FunnelConfig(max_choices=2), app_filter=True)
        self.assertEqual(l3.fiches[:2], [TEAMS, TRANSFER])   # meaning first; Webex is not a Teams fiche
        self.assertNotIn(WEBEX, l3.fiches)
        self.assertEqual(ranked.finding.fiches, [TEAMS, TRANSFER, WEBEX])   # the rest after, by id
        self.assertEqual([o["fiche_id"] for o in ranked.finding.options], [TEAMS, TRANSFER])
        self.assertEqual((ranked.finding.trace[0]["ordered_by"], ranked.route["level"], ranked.mode),
                         ("L3", "L2", "routed"))
        self.assertEqual(ranked.semantic_error, None)

    def test_l2_a_model_failure_is_degraded_and_goes_on_to_l3(self):
        question = "renvoyer mes appels sur mon portable"
        for error, cause in ((LLMError("429 too many requests"), "LLMError"), (RuntimeError("boom"), "RuntimeError"),
                             (LLMError(""), "LLMError")):
            result = self.ask(question, intent_llm=SchemaLLM({MAP_SCHEMA_NAME: error}))
            self.assertEqual(result.degraded, f"l2_failed:{cause}", cause)
            self.assertEqual((result.route["l2"]["reason"], result.route["l2"]["exact"]), ("map_failed", None))
            self.assertTrue(result.route["l2"]["error"])
            self.assertIn(result.route["level"], ("L3", "L4"))
            self.assertEqual(result.mode, "semantic")
        self.assertEqual(self.ask(question, intent_llm=SchemaLLM({MAP_SCHEMA_NAME: RuntimeError("boom")}))
                         .route["l2"]["error"], "RuntimeError: boom")

    def test_l2_a_mapping_the_model_refuses_for_good_is_measured_as_live_not_degraded(self):
        question = "renvoyer mes appels sur mon portable"
        for error in (LLMError("POST /chat -> HTTP 400: content_filter triggered"), LLMError("the model refused: no"),
                      LLMError("the answer was cut at max_tokens (150)"), LLMError("the model did not return JSON")):
            result = self.ask(question, intent_llm=SchemaLLM({MAP_SCHEMA_NAME: error}))
            self.assertIsNone(result.degraded, str(error))
            self.assertEqual((result.route["l2"]["reason"], result.route["l2"]["exact"]), ("map_refused", None))
            self.assertIn(result.route["level"], ("L3", "L4"))
        # throttling or the service down stay degraded: a rerun could answer otherwise
        throttled = self.ask(question, intent_llm=SchemaLLM({MAP_SCHEMA_NAME: LLMError("POST /chat -> HTTP 429: busy")}))
        self.assertEqual(throttled.degraded, "l2_failed:LLMError")

    def test_l2_and_the_embedding_both_failing_say_both(self):
        self.embedder = FailingEmbedder()
        result = self.ask("renvoyer mes appels sur mon portable", intent_llm=SchemaLLM({MAP_SCHEMA_NAME: LLMError("x")}))
        self.assertEqual(result.degraded, "l2_failed:LLMError; embedding_failed:LLMError")
        self.assertEqual((result.mode, result.semantic_error), ("degraded", "embedding failed (LLMError)"))

    def test_l2_needs_l2_tables_and_a_model(self):
        question = "renvoyer mes appels sur mon portable"
        intent_llm = mapper({question: ("TRANSFERER_APPELS", "teams")})
        no_l2 = tables_for(self.kbmap, taxonomy=None)
        self.assertEqual(no_l2.l2, [])
        result = self.ask(question, tables=no_l2, intent_llm=intent_llm)
        self.assertEqual((intent_llm.calls, result.route["l2"]), ([], None))
        self.assertNotIn(result.route["level"], EXACT_LEVELS)
        without_model = self.ask(question)
        self.assertIsNone(without_model.route["l2"])

    def test_l2_an_intent_outside_the_taxonomy_maps_to_nothing(self):
        question = "renvoyer mes appels sur mon portable"
        result = self.ask(question, intent_llm=mapper({question: ("SUPPRIMER_TOUT", "zoom-invented")}))
        self.assertEqual((result.route["l2"]["intent"], result.route["l2"]["model_app"], result.route["l2"]["reason"]),
                         (None, None, "no_intent"))
        self.assertNotIn(result.route["level"], EXACT_LEVELS)

    # --- L3 / L4 --------------------------------------------------------------------------------------
    def test_l3_a_fiche_today_s_engine_shows_is_offered_first_never_shown(self):
        question = "mon compte est bloqué depuis ce matin"   # the alias "compte bloqué" leaves 2 words uncovered
        tables = tables_for(self.kbmap)
        vector = ConceptEmbedder().embed([sem.query_text(question)])[0]
        base = find(self.kbmap, question, query_vector=vector)
        self.assertEqual((base.kind, base.fiche_id), ("fiche", ACCOUNT))
        result = self.ask(question, tables=tables)
        finding = result.finding
        self.assertEqual((result.route["level"], result.route["rule"], result.route["fiche_id"]), ("L3", "semantic", None))
        self.assertEqual((finding.kind, finding.asks, finding.fiche_id), ("question", "fiche", None))
        self.assertEqual(finding.reason, "route_l3_" + base.reason)
        self.assertEqual(finding.fiches, base.fiches)               # the same ranking: the same first fiche
        self.assertEqual(finding.options[0]["fiche_id"], base.fiche_id)
        self.assertEqual(len(finding.options), FunnelConfig().max_choices)
        self.assertEqual(finding.trace, base.trace + [{"step": "route", "level": "L3", "downgraded": True}])
        self.assertEqual((finding.score, finding.degraded, finding.designated), (base.score, False, False))
        self.assertEqual((result.mode, result.degraded, result.interpreted), ("semantic", None, None))

    def test_l3_a_fiche_shown_by_words_during_an_outage_is_downgraded_and_marked(self):
        question = "transfert d'appels teams ne fonctionne pas"
        base = find(self.kbmap, question)                       # no vector: the words decide
        self.assertEqual((base.kind, base.fiche_id, base.degraded), ("fiche", TRANSFER, True))
        result = answer(self.kbmap, question, routing=tables_for(self.kbmap), current=CURRENT)   # no embedder
        finding = result.finding
        self.assertEqual((finding.kind, finding.reason, finding.options[0]["fiche_id"], finding.fiches),
                         ("question", "route_l3_" + base.reason, TRANSFER, base.fiches))
        self.assertTrue(finding.degraded)
        self.assertEqual((result.route["level"], result.route["rule"], result.mode, result.degraded,
                          result.semantic_error), ("L3", "words", "degraded", "no_embedder", "no embedding client"))
        self.assertFalse(finding.reason.startswith("text_only"))   # the Diagnostic's words-only distrust is unrouted

    def test_l3_ranks_only_the_application_s_fiches_and_says_so(self):
        question = "mon compte est bloqué depuis teams"
        result = self.ask(question)
        steps = [s for s in result.finding.trace if s["step"] == "app_filter"]
        self.assertEqual(steps, [{"step": "app_filter", "app": "app:teams", "kept": 2}])
        self.assertEqual(sorted(result.finding.fiches), [TRANSFER, TEAMS])
        self.assertEqual(result.route["level"], "L3")

    def test_l3_a_question_is_kept(self):
        result = self.ask("teams")
        self.assertEqual((result.route["level"], result.route["rule"], result.finding.kind), ("L3", "semantic", "question"))
        self.assertFalse(result.finding.reason.startswith("route_"))

    def test_l4_nothing_close_abstains(self):
        result = self.ask("le vpn se coupe")
        self.assertEqual((result.route["level"], result.route["rule"], result.route["key"]), ("L4", "none", None))
        self.assertEqual((result.finding.kind, result.finding.reason), ("abstain", "semantic_nothing_close"))

    def test_l3_by_words_when_the_map_has_no_index(self):
        kbmap = words_map()
        result = self.ask("le transfert d'appels ne marche plus", kbmap=kbmap, tables=tables_for(kbmap),
                          interpret_ticket=False)
        self.assertIn(result.route["level"], ("L3", "L4"))
        if result.route["level"] == "L3":
            self.assertEqual(result.route["rule"], "words")
        self.assertEqual((result.mode, result.interpreted, result.degraded), ("words", None, None))
        self.assertNotEqual(result.finding.kind, "fiche")

    def test_the_technician_s_answers_reach_l3(self):
        result = self.ask("le téléphone ne marche plus", answers=["app:teams"])
        entities = next(s for s in result.finding.trace if s["step"] == "entities")
        self.assertEqual(entities["answers"], ["app:teams"])

    # --- the route ------------------------------------------------------------------------------------
    def test_the_route_s_shape(self):
        tables = tables_for(self.kbmap)
        question = "réinitialiser le mot de passe"
        for result in (self.ask("KB0233", tables=tables), self.ask("attribuer ligne teams", tables=tables),
                       self.ask(question, tables=tables,
                                intent_llm=mapper({question: ("REINITIALISER_MOT_DE_PASSE", "")})),
                       self.ask("teams", tables=tables), self.ask("le vpn se coupe", tables=tables)):
            route = result.route
            self.assertEqual(set(route), ROUTE_KEYS)
            self.assertEqual((route["version"], route["tables_sha256"], route["content_sha256"], route["build_id"],
                              route["run_id"]), (ROUTE_VERSION, tables.sha256, tables.content_sha256, "b1", "r1"))
            self.assertEqual(set(route["l0"]), {"decided", "rule", "key", "fiche_id", "scope"})
            if route["l1"] is not None:
                self.assertEqual(set(route["l1"]), {"matches", "ambiguous", "uncovered", "outside_scope", "uncovered_negation",
                                            "generic_for_app", "blockers"})
            if route["l2"] is not None:
                self.assertEqual(set(route["l2"]), L2_KEYS)
            json.dumps(route)   # plain data

    def test_the_same_question_gives_the_same_answer_byte_for_byte(self):
        question = "réinitialiser le mot de passe"
        intent_llm = mapper({question: ("REINITIALISER_MOT_DE_PASSE", "")})
        for text in (question, "comment attribuer une ligne teams", "mon compte est bloqué depuis ce matin", "KB0120"):
            first = self.ask(text, intent_llm=intent_llm).to_dict()
            again = answer(semantic_map(), text, (), None, llm=StubLLM({"terms": ["phone line"], "application": None}),
                           embedder=ConceptEmbedder(), routing=tables_for(semantic_map(), list(reversed(decisions()))),
                           current=dict(CURRENT), intent_llm=intent_llm).to_dict()
            self.assertEqual(json.dumps(first, sort_keys=True), json.dumps(again, sort_keys=True), text)

    # --- tables that do not belong to this map --------------------------------------------------------
    def test_tables_of_another_run_client_code_or_kb_are_refused(self):
        tables = tables_for(self.kbmap)
        check_tables(tables, self.kbmap, CURRENT)
        other_run = tables_for(words_map(run_id="r2"))
        other_client = RoutingTables.from_dict({**tables.to_dict(), "client": "clientb"})
        other_code = RoutingTables.from_dict({**tables.to_dict(), "code_version": {**CODE_VERSION, "route": 99}})
        for bad in (other_run, other_client, other_code):
            with self.assertRaises(TablesMismatchError):
                self.ask("KB0120", tables=bad)
        changed = dict(CURRENT, **{TEAMS: "edited"})
        with self.assertRaises(TablesMismatchError):
            self.ask("KB0120", current=changed)
        # without the run's source hashes the KB cannot be compared: the other checks still hold
        self.assertEqual(self.ask("KB0120", current=None).route["level"], "L0")

    def test_without_source_hashes_a_fiche_no_longer_routable_is_never_routed_to(self):
        tables = tables_for(self.kbmap)
        changed = [f if f.fiche_id != TEAMS else make(TEAMS, f.text, entities=["app:teams"], status="info_only")
                   for f in fiches()]
        kbmap = KBMap("clienta", changed, run_id="r1")
        question = "comment attribuer une ligne teams"
        intent_llm = mapper({question: ("ATTRIBUER_LIGNE", "teams")})
        result = answer(kbmap, question, routing=tables, current=None, intent_llm=intent_llm)
        self.assertEqual(result.route["l1"]["matches"], [{"fiche_id": TEAMS, "alias": "attribuer ligne teams"}])
        self.assertEqual((result.route["l2"]["exact"], result.route["l2"]["reason"]), (None, "not_routable"))
        self.assertNotIn(result.route["level"], EXACT_LEVELS)
        self.assertNotIn(TEAMS, result.finding.fiches)
        with self.assertRaises(TablesMismatchError):
            answer(kbmap, question, routing=tables, current={f.fiche_id: CURRENT[f.fiche_id] for f in changed})


class MapQuestionTest(unittest.TestCase):
    def setUp(self):
        self.tables = tables_for(words_map())

    def test_the_prompt_s_first_line_is_the_recorded_question(self):
        text = "Comment ATTRIBUER  une ligne Teams\npour jean.dupont@corp.example ?"
        prompt = map_prompt(text, self.tables)
        lines = prompt.split("\n")
        self.assertEqual(lines[0], "Question: " + sem.query_text(text))
        self.assertEqual(map_prompt("comment attribuer une ligne teams pour jean.dupont@corp.example ?",
                                    self.tables), prompt)   # two spellings of one question: one record
        self.assertNotIn("dupont", prompt)
        self.assertEqual(lines[1:6], ["Intents (id: label):", "ATTRIBUER_LIGNE: Attribuer une ligne téléphonique",
                                      "DEVERROUILLER_COMPTE: Déverrouiller un compte",
                                      "REINITIALISER_MOT_DE_PASSE: Réinitialiser un mot de passe",
                                      "TRANSFERER_APPELS: Transférer des appels"])
        self.assertEqual(lines[-1], "Allowed application ids: " + ", ".join(self.tables.apps))
        self.assertEqual(len(lines), 7)   # no description: the live call stays small
        self.assertEqual(SchemaLLM.key_of(prompt), sem.query_text(text))

    def test_the_schema_is_the_closed_lists(self):
        schema = map_schema(["A_B_C", "D_E_F"], ["sap", "teams"])
        self.assertEqual(schema["properties"]["intent"]["enum"], ["", "A_B_C", "D_E_F"])
        self.assertEqual(schema["properties"]["app"]["enum"], ["", "sap", "teams"])
        self.assertEqual((schema["required"], schema["additionalProperties"]), (["intent", "app"], False))
        llm = mapper(default={"intent": "", "app": ""})
        map_question(llm, "x y", self.tables, {})
        self.assertEqual(llm.schemas[0]["properties"]["intent"]["enum"][1:], TAXONOMY.ids())
        self.assertIn("Never guess", MAP_SYSTEM_PROMPT)

    def test_the_application_the_code_reads_wins(self):
        llm = mapper({"attribuer une ligne webex": ("ATTRIBUER_LIGNE", "teams")})
        mapped = map_question(llm, "attribuer une ligne webex", self.tables, {})
        self.assertEqual(mapped, {"intent": "ATTRIBUER_LIGNE", "app": "webex", "app_source": "code",
                                  "model_app": "teams", "error": None,
                                  "usage": {"input_tokens": 100, "output_tokens": 50}})

    def test_the_model_s_application_when_the_code_reads_none(self):
        llm = mapper({"ma boîte mail ne synchronise plus": ("ATTRIBUER_LIGNE", "outlook")})
        mapped = map_question(llm, "ma boîte mail ne synchronise plus", self.tables, {})
        self.assertEqual((mapped["app"], mapped["app_source"], mapped["model_app"]), ("outlook", "model", "outlook"))

    def test_several_applications_named_the_model_may_only_choose_among_them(self):
        question = "transférer les appels teams vers webex"
        among = map_question(mapper({question: ("TRANSFERER_APPELS", "teams")}), question, self.tables, {})
        self.assertEqual((among["app"], among["app_source"]), ("teams", "model"))
        elsewhere = map_question(mapper({question: ("TRANSFERER_APPELS", "sap")}), question, self.tables, {})
        self.assertEqual((elsewhere["app"], elsewhere["app_source"], elsewhere["model_app"]), (None, None, "sap"))

    def test_nothing_outside_the_closed_lists(self):
        mapped = map_question(mapper({"x y": ("NOT_AN_INTENT", "not-an-app")}), "x y", self.tables, {})
        self.assertEqual((mapped["intent"], mapped["app"], mapped["model_app"]), (None, None, None))
        empty = map_question(mapper({"x y": ("", "")}), "x y", self.tables, {})
        self.assertEqual((empty["intent"], empty["app"], empty["error"]), (None, None, None))
        garbage = map_question(SchemaLLM({MAP_SCHEMA_NAME: lambda *a: ["not", "a", "dict"]}), "x y", self.tables, {})
        self.assertEqual((garbage["intent"], garbage["app"], garbage["error"]), (None, None, None))

    def test_a_failure_never_raises(self):
        down = map_question(SchemaLLM({MAP_SCHEMA_NAME: LLMError("timeout")}), "attribuer une ligne teams", self.tables, {})
        self.assertEqual((down["intent"], down["error"], down["usage"]), (None, "timeout", {"input_tokens": 0, "output_tokens": 0}))
        self.assertEqual((down["app"], down["app_source"]), ("teams", "code"))
        crash = map_question(SchemaLLM({MAP_SCHEMA_NAME: ValueError("x" * 500)}), "q r", self.tables, {})
        self.assertTrue(crash["error"].startswith("ValueError: x"))
        self.assertEqual(len(crash["error"]), 300)
        self.assertEqual(map_question(None, "q r", self.tables, {})["error"], "no model client")


class QuestionVectorTest(unittest.TestCase):
    def test_reasons(self):
        kbmap = semantic_map()
        vector, error = question_vector(kbmap, ConceptEmbedder(), "comment attribuer une ligne teams")
        self.assertEqual((len(vector), error), (DIMS, None))
        self.assertEqual(question_vector(kbmap, None, "x"), (None, "no embedding client"))
        self.assertEqual(question_vector(kbmap, BrokenEmbedder(), "x"), (None, "the embedding model is not the index's"))
        self.assertEqual(question_vector(kbmap, FailingEmbedder(), "x"), (None, "embedding failed (LLMError)"))
        self.assertEqual(question_vector(words_map(), ConceptEmbedder(), "x"), (None, None))


def respond_before_routing(kbmap, payload, config=None, llm=None, embedder=None) -> dict:
    """kecore_func.kefind_service.respond as it was before routing (commit 71843ed), frozen here as the
    oracle: once the Function answers through kefind.route, comparing with it alone would prove nothing."""
    vector, semantic_error = None, getattr(kbmap, "semantic_error", None)
    if kbmap.semantic is not None:
        if embedder is None:
            semantic_error = "no embedding client"
        elif getattr(embedder, "model_id", None) != kbmap.semantic.model:
            semantic_error = "the embedding model is not the index's"
        else:
            try:
                vector = embedder.embed([sem.query_text(payload["text"])])[0]
            except Exception as exc:
                semantic_error = f"embedding failed ({type(exc).__name__})"
    interpretation = None
    asked = bool(payload.get("interpret", True)) and vector is None
    if asked and llm is not None:
        interpretation = interpret(llm, payload["text"], kbmap.dictionary)
    finding = find(kbmap, payload["text"], answers=payload["answers"], config=config, interpretation=interpretation,
                   query_vector=vector)
    interpreted = (interpretation is not None and interpretation.error is None) if asked else None
    mode = "semantic" if vector is not None else ("degraded" if kbmap.semantic is not None else "words")
    return {"mode": mode, "semantic_error": semantic_error, "interpreted": interpreted, "decision": finding.to_dict()}


class ParityTest(unittest.TestCase):
    """Without routing tables, ``answer`` is exactly what /find answered before routing existed
    (kecore_func.kefind_service.respond, and its frozen copy above): the same finding, mode,
    semantic_error and interpreted."""

    @classmethod
    def setUpClass(cls):
        path = str(Path(__file__).resolve().parents[2] / "kecore_func")
        if path not in sys.path:
            sys.path.append(path)
        import kefind_service

        cls.respond = staticmethod(kefind_service.respond)
        cls.semantic = semantic_map()

    def check(self, kbmap, text, *, answers=(), interpret_ticket=True, llm=None, embedder=None, config=None):
        payload = {"text": text, "answers": list(answers), "interpret": interpret_ticket}
        got = answer(kbmap, text, answers, config, llm=llm, embedder=embedder, interpret_ticket=interpret_ticket)
        for oracle in (self.respond, respond_before_routing):
            expected = oracle(kbmap, payload, config=config, llm=llm, embedder=embedder)
            self.assertEqual(json.dumps(got.finding.to_dict(), sort_keys=True),
                             json.dumps(expected["decision"], sort_keys=True), text)
            self.assertEqual((got.mode, got.semantic_error, got.interpreted),
                             (expected["mode"], expected["semantic_error"], expected["interpreted"]), text)
        self.assertIsNone(got.route)
        return got

    def test_words_map(self):
        interpreting = StubLLM({"terms": ["transfert d'appels"], "application": "Teams"})
        got = self.check(words_map(), "mes appels ne sont pas renvoyés", llm=interpreting)
        self.assertEqual((got.mode, got.interpreted, got.degraded), ("words", True, None))
        self.assertEqual(got.usage, {"input_tokens": 10, "output_tokens": 5})
        not_asked = self.check(words_map(), "mes appels ne sont pas renvoyés", llm=interpreting, interpret_ticket=False)
        self.assertEqual((not_asked.interpreted, not_asked.usage["input_tokens"]), (None, 0))
        self.assertEqual(self.check(words_map(), "mes appels ne sont pas renvoyés").interpreted, False)  # no model
        failing = self.check(words_map(), "mes appels", llm=StubLLM(LLMError("down")))
        self.assertEqual(failing.interpreted, False)
        self.check(words_map(), "erreur 0x80070005", answers=["app:teams"], config=FunnelConfig(max_choices=2))

    def test_semantic_map(self):
        llm = StubLLM({"terms": ["phone line"], "application": None})
        got = self.check(self.semantic, "comment attribuer une ligne teams", llm=llm, embedder=ConceptEmbedder())
        self.assertEqual((got.mode, got.interpreted, got.degraded, llm.calls), ("semantic", None, None, []))
        self.assertEqual((got.finding.kind, got.finding.fiche_id), ("fiche", TEAMS))

    def test_degraded_questions(self):
        llm = StubLLM({"terms": ["phone line"], "application": None})
        for embedder, degraded in ((None, "no_embedder"), (BrokenEmbedder(), "model_mismatch"),
                                   (FailingEmbedder(), "embedding_failed:LLMError")):
            got = self.check(self.semantic, "comment attribuer une ligne teams", llm=llm, embedder=embedder)
            self.assertEqual((got.mode, got.degraded, got.interpreted), ("degraded", degraded, True))
            self.assertTrue(got.finding.degraded)
            uninterpreted = self.check(self.semantic, "comment attribuer une ligne teams", embedder=embedder,
                                       interpret_ticket=False)
            self.assertIsNone(uninterpreted.interpreted)

    def test_the_map_s_own_semantic_error(self):
        kbmap = words_map()
        kbmap.semantic_error = "the run's semantic index is incomplete (no semantic/vectors.f32): decided by words"
        got = self.check(kbmap, "comment attribuer une ligne teams", embedder=ConceptEmbedder())
        self.assertEqual((got.mode, got.semantic_error, got.degraded), ("words", kbmap.semantic_error, None))

    def test_an_exact_route_still_reports_the_map_s_semantic_error(self):
        kbmap = words_map()
        kbmap.semantic_error = "the run's semantic index was refused (x): decided by words"
        result = answer(kbmap, "KB0233", routing=tables_for(kbmap), current=CURRENT)
        self.assertEqual((result.route["level"], result.semantic_error), ("L0", kbmap.semantic_error))


class FunnelEngineRouteTest(unittest.TestCase):
    def setUp(self):
        self.kbmap = semantic_map()
        self.tables = tables_for(self.kbmap)

    def engine(self, **kw):
        return FunnelEngine({"clienta": self.kbmap}, config=FunnelConfig(min_show=0.0), name="kefind-route",
                            embedder=ConceptEmbedder(), **kw)

    def test_decide_carries_the_route_and_the_degraded_mark(self):
        question = "renvoyer mes appels sur mon portable"
        engine = self.engine(routing=self.tables, current=CURRENT,
                             intent_llm=mapper({question: ("TRANSFERER_APPELS", "teams")}))
        decision = engine.decide(Ticket("T-1", "clienta", question))
        self.assertEqual((decision.kind, decision.fiches, decision.score), ("fiche", [TRANSFER], None))
        self.assertEqual((decision.route["level"], decision.route["fiche_id"], decision.degraded), ("L2", TRANSFER, None))
        self.assertEqual((decision.usage.input_tokens, decision.usage.output_tokens, decision.usage.search_calls),
                         (100, 50, 1))
        record = make_record("kefind-route", 0, Ticket("T-1", "clienta", question, expected=[TRANSFER]), decision)
        self.assertEqual(record["route"], {"level": "L2", "rule": "intent_app", "key": "TRANSFERER_APPELS|teams",
                                           "fiche_id": TRANSFER})

        failing = self.engine(routing=self.tables, current=CURRENT, intent_llm=SchemaLLM({MAP_SCHEMA_NAME: LLMError("x")}))
        downgraded = failing.decide(Ticket("T-2", "clienta", "mon compte est bloqué depuis ce matin"))
        self.assertEqual((downgraded.kind, downgraded.fiches[0], downgraded.route["level"]), ("question", ACCOUNT, "L3"))
        self.assertIsNotNone(downgraded.score)

    def test_without_routing_the_engine_decides_as_before(self):
        plain = self.engine()
        decision = plain.decide(Ticket("T-1", "clienta", "comment attribuer une ligne teams"))
        expected = find(self.kbmap, "comment attribuer une ligne teams",
                        query_vector=ConceptEmbedder().embed([sem.query_text("comment attribuer une ligne teams")])[0])
        self.assertEqual((decision.kind, decision.fiches, decision.score, decision.trace),
                         ("fiche", expected.fiches, expected.score, expected.trace))
        self.assertEqual((decision.route, decision.degraded), (None, None))
        self.assertEqual(plain.candidates(Ticket("T-1", "clienta", "comment attribuer une ligne teams"), 2)[0][0], TEAMS)
        no_vector = FunnelEngine({"clienta": self.kbmap}).decide(Ticket("T-1", "clienta", "comment attribuer une ligne"))
        self.assertEqual((no_vector.route, no_vector.degraded), (None, "no_embedder"))
        words = FunnelEngine({"clienta": words_map()}).decide(Ticket("T-1", "clienta", "comment attribuer une ligne"))
        self.assertIsNone(words.degraded)

    def test_tables_of_another_kb_are_an_error_record_never_a_routed_answer(self):
        engine = self.engine(routing=self.tables, current=dict(CURRENT, **{TEAMS: "edited"}))
        records = run_engine(engine, [Ticket("T-1", "clienta", "KB0233", expected=[TEAMS])])
        self.assertTrue(records[0]["error"].startswith("TablesMismatchError"))
        self.assertNotIn("route", records[0])


if __name__ == "__main__":
    unittest.main()
