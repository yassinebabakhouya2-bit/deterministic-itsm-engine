"""Pass B (kefind.taxonomy): the pass-A intents merged into a closed taxonomy -- the model proposes, the
code checks and covers every intent, a person freezes it; a replay rewrites the same bytes with no call."""

import hashlib
import json
import unittest

from kecore.llm import LLMError, RecordingLLM
from kefind.enrich import Enrichment
from kefind.taxonomy import (B_LLM_OVERRIDES, MAX_DESCRIPTION_CHARS, MAX_LABEL_CHARS, MAX_TAXONOMY_INTENTS,
                             SCHEMA_NAME, SYSTEM_PROMPT, TAXONOMY_PROMPT_VERSION, Taxonomy, TaxonomyIntent,
                             TaxonomyProposal, freeze, make_taxonomy, named_apps, proposed_rows, removed_intents,
                             schema, taxonomy_sha256, user_prompt)

from .schema_llm import SchemaLLM
from .test_enrich import MemoryStore, walk

PHONE_LINE = "KB0233 - Associate a phone line"
TRANSFER_TEAMS = "KB0217 - Transfert d'appels TEAMS"
FORWARD_SKYPE = "KB0218 - Renvoi d'appels Skype"
LOCKED = "KB0120 - LOCKED ACCOUNT"
LOCKED_AD = "KB0121 - Compte bloqué AD"
PASSWORD = "KB0300 - Mot de passe Outlook"
BROKEN = "KB0400 - Pass A failed"
SILENT = "KB0401 - No intent"


def enrichment(fiche_id, intent, label="", app=None, error=None):
    return Enrichment(fiche_id=fiche_id, label=fiche_id, content_sha256="0" * 64, canonical_intent=intent,
                      intent_label_fr=label, primary_app=app, supported_apps=[app] if app else [], error=error)


def pass_a():
    """Pass A as client-s shows it: near-unique intents, one naming its application, an error, a fiche
    without intent."""
    return [
        enrichment(PHONE_LINE, "ATTRIBUTION_LIGNE_TELEPHONIQUE", "Attribuer une ligne téléphonique", "teams"),
        enrichment(TRANSFER_TEAMS, "TRANSFERT_APPELS_TEAMS", "Transférer les appels Teams", "teams"),
        enrichment(FORWARD_SKYPE, "TRANSFERT_APPELS", "Renvoyer les appels", "skype"),
        enrichment(LOCKED, "DEVERROUILLAGE_COMPTE", "", None),
        enrichment(LOCKED_AD, "DEVERROUILLAGE_COMPTE", "Déverrouiller un compte", "active-directory"),
        enrichment(PASSWORD, "REINITIALISATION_MOT_DE_PASSE", "Réinitialiser un mot de passe", "outlook"),
        enrichment(BROKEN, None, error="LLMError: 429"),
        enrichment(SILENT, None),
    ]
# sorted intents: I001 ATTRIBUTION_LIGNE_TELEPHONIQUE, I002 DEVERROUILLAGE_COMPTE,
# I003 REINITIALISATION_MOT_DE_PASSE, I004 TRANSFERT_APPELS, I005 TRANSFERT_APPELS_TEAMS


B_ANSWER = {"intents": [
    {"id": "TRANSFERT_APPELS", "label_fr": "Transférer les appels",
     "description_fr": "Renvoyer les appels entrants vers un autre numéro.", "members": ["I004", "I005"]},
    {"id": "Attribution ligne téléphonique", "label_fr": "  Attribuer   une ligne ",
     "description_fr": "Attribuer un numéro de téléphone à un utilisateur.", "members": ["I001"]},
    {"id": "DEVERROUILLAGE_COMPTE", "label_fr": "", "description_fr": "", "members": ["I002"]},
]}


def llm(answer=B_ANSWER):
    return SchemaLLM({(SCHEMA_NAME, "5"): answer})


def frozen(version=1, rows=None):
    rows = rows if rows is not None else [
        {"id": "TRANSFERT_APPELS", "label_fr": "Transférer les appels", "description_fr": "Renvoyer les appels."},
        {"id": "ATTRIBUTION_LIGNE_TELEPHONIQUE", "label_fr": "Attribuer une ligne", "description_fr": ""},
    ]
    return freeze(rows, version, "Yassine", "2026-10-13T10:00:00Z", "b-20261013")


class RowsTest(unittest.TestCase):
    def test_each_pass_a_intent_once_sorted_with_its_first_label_its_apps_and_fiches(self):
        rows = proposed_rows(pass_a())
        self.assertEqual([(r["n"], r["intent"]) for r in rows], [
            ("I001", "ATTRIBUTION_LIGNE_TELEPHONIQUE"), ("I002", "DEVERROUILLAGE_COMPTE"),
            ("I003", "REINITIALISATION_MOT_DE_PASSE"), ("I004", "TRANSFERT_APPELS"),
            ("I005", "TRANSFERT_APPELS_TEAMS")])
        self.assertEqual(rows[1], {"n": "I002", "intent": "DEVERROUILLAGE_COMPTE",
                                   "label_fr": "Déverrouiller un compte",       # KB0120's is empty: KB0121's
                                   "apps": ["active-directory"], "fiches": [LOCKED, LOCKED_AD]})
        self.assertEqual(rows[0]["apps"], ["teams"])

    def test_fiches_in_error_or_without_intent_are_left_out(self):
        fiches = {f for r in proposed_rows(pass_a()) for f in r["fiches"]}
        self.assertNotIn(BROKEN, fiches)
        self.assertNotIn(SILENT, fiches)
        rows = proposed_rows([enrichment(BROKEN, "ATTRIBUTION_LIGNE", error="timeout")])
        self.assertEqual(rows, [])

    def test_the_rows_never_depend_on_the_order_of_the_proposals_and_read_dicts_too(self):
        items = pass_a()
        self.assertEqual(proposed_rows(list(reversed(items))), proposed_rows(items))
        self.assertEqual(proposed_rows([e.to_dict() for e in items]), proposed_rows(items))

    def test_the_label_is_the_first_non_empty_one_in_fiche_id_order(self):
        rows = proposed_rows([enrichment("KB2", "CREATION_COMPTE", "Second"),
                              enrichment("KB1", "CREATION_COMPTE", "  Premier   libellé ")])
        self.assertEqual(rows[0]["label_fr"], "Premier libellé")

    def test_numbers_are_at_least_three_digits_wide_and_widen_with_the_count(self):
        many = [enrichment(f"KB{i:05d}", f"INTENTION_{i:05d}") for i in range(1000)]
        rows = proposed_rows(many)
        self.assertEqual((rows[0]["n"], rows[-1]["n"]), ("I0001", "I1000"))
        self.assertEqual(proposed_rows(many[:9])[-1]["n"], "I009")


class PromptTest(unittest.TestCase):
    def test_the_schema_is_strict(self):
        s = schema()
        objects = [node for node in walk(s) if node.get("type") == "object"]
        self.assertEqual(len(objects), 2)
        for node in objects:
            self.assertFalse(node["additionalProperties"])
            self.assertEqual(sorted(node["required"]), sorted(node["properties"]))
        entry = s["properties"]["intents"]["items"]
        self.assertEqual(sorted(entry["properties"]), ["description_fr", "id", "label_fr", "members"])
        self.assertEqual(entry["properties"]["members"]["items"], {"type": "string"})

    def test_the_prompt_starts_with_the_count_then_one_line_per_intent(self):
        prompt = user_prompt(proposed_rows(pass_a()))
        lines = prompt.split("\n")
        self.assertEqual(lines[0], "Proposed intents: 5")
        self.assertEqual(lines[1], "I001 | ATTRIBUTION_LIGNE_TELEPHONIQUE | Attribuer une ligne téléphonique | "
                                   "apps: teams | fiches: 1")
        self.assertEqual(lines[2], "I002 | DEVERROUILLAGE_COMPTE | Déverrouiller un compte | apps: active-directory | "
                                   "fiches: 2")
        self.assertEqual(len(lines), 6)
        self.assertNotIn(PHONE_LINE, prompt)                 # fiche ids stay out of the request

    def test_an_empty_label_or_no_application_reads_as_a_dash_and_a_pipe_never_breaks_a_line(self):
        rows = proposed_rows([enrichment("KB1", "CREATION_COMPTE", "créer | compte")])
        self.assertEqual(user_prompt(rows).split("\n")[1],
                         "I001 | CREATION_COMPTE | créer / compte | apps: - | fiches: 1")
        rows = proposed_rows([enrichment("KB1", "CREATION_COMPTE")])
        self.assertEqual(user_prompt(rows).split("\n")[1], "I001 | CREATION_COMPTE | - | apps: - | fiches: 1")

    def test_the_system_prompt_states_the_merging_rules(self):
        for words in ("UPPER_SNAKE_CASE French", "NEVER put an application name", "ONE short French sentence",
                      "exactly one entry", "whatever the application", "Keep different actions apart", "data"):
            self.assertIn(words, SYSTEM_PROMPT)

    def test_pass_b_gets_a_long_timeout_a_large_answer_and_one_retry(self):
        self.assertEqual(B_LLM_OVERRIDES, {"max_tokens": 12000, "timeout_s": 600.0, "retries": 1})


class ProposalTest(unittest.TestCase):
    def test_the_model_merges_and_the_code_covers_every_pass_a_intent(self):
        model = llm()
        p = make_taxonomy(model, pass_a(), {}, run_id="20261013-1")
        self.assertEqual(model.calls, [(SCHEMA_NAME, "5")])
        self.assertIsNone(p.error)
        self.assertEqual([i.id for i in p.intents], ["ATTRIBUTION_LIGNE_TELEPHONIQUE", "DEVERROUILLAGE_COMPTE",
                                                    "REINITIALISATION_MOT_DE_PASSE", "TRANSFERT_APPELS"])
        phone, locked, password, transfer = p.intents
        self.assertEqual(phone, TaxonomyIntent(
            id="ATTRIBUTION_LIGNE_TELEPHONIQUE", label_fr="Attribuer une ligne",
            description_fr="Attribuer un numéro de téléphone à un utilisateur.",
            members=["ATTRIBUTION_LIGNE_TELEPHONIQUE"], fiches=[PHONE_LINE], origin="model", warnings=[]))
        self.assertEqual((locked.label_fr, locked.fiches), ("Déverrouiller un compte", [LOCKED, LOCKED_AD]))
        self.assertEqual(transfer.members, ["TRANSFERT_APPELS", "TRANSFERT_APPELS_TEAMS"])
        self.assertEqual(transfer.fiches, [TRANSFER_TEAMS, FORWARD_SKYPE])
        self.assertEqual((password.origin, password.label_fr, password.members, password.fiches),
                         ("unmapped", "Réinitialiser un mot de passe", ["REINITIALISATION_MOT_DE_PASSE"], [PASSWORD]))
        self.assertEqual(p.dropped, [])
        self.assertEqual(p.source, {"intents": 5, "fiches": 6, "enrichment_errors": 1})
        self.assertEqual(p.warnings, ["1 pass-A fiches have an error: their intents are not in this proposal",
                                      "1 pass-A fiches have no intent",
                                      "1 pass-A intents were left out by the model: each is its own entry"])
        self.assertEqual((p.run_id, p.version), ("20261013-1", TAXONOMY_PROMPT_VERSION))
        self.assertEqual(sorted(p.provenance), ["model", "prompt_version", "request_sha256"])
        self.assertEqual(p.provenance["model"], "schema-llm")

    def test_every_check_refuses_with_its_reason(self):
        answer = {"intents": [
            {"id": "??", "label_fr": "x", "description_fr": "", "members": ["I001"]},
            {"id": "TRANSFERT_APPELS", "label_fr": "Transférer les appels", "description_fr": "",
             "members": ["I004", "I999", "TRANSFERT_APPELS_TEAMS", 7, "I004", "I0"]},
            {"id": "RENVOI_APPELS", "label_fr": "Renvoyer", "description_fr": "", "members": ["I004", "I5"]},
            {"id": "transfert appels", "label_fr": "autre libellé", "description_fr": "autre", "members": ["I002"]},
            {"id": "VIDE", "label_fr": "Rien", "description_fr": "", "members": []},
            {"id": "SEUL", "label_fr": "Seul", "description_fr": "", "members": ["I004"]},
            "not an entry",
        ]}
        p = make_taxonomy(llm(answer), pass_a(), {})
        self.assertEqual(p.dropped, [
            {"field": "id", "text": "??",
             "reason": "not an UPPER_SNAKE_CASE label of 3 to 64 characters: its members stay unmapped"},
            {"field": "members", "text": "TRANSFERT_APPELS: I999", "reason": "unknown member"},
            {"field": "members", "text": "TRANSFERT_APPELS: TRANSFERT_APPELS_TEAMS", "reason": "unknown member"},
            {"field": "members", "text": "TRANSFERT_APPELS: 7", "reason": "unknown member"},
            {"field": "members", "text": "TRANSFERT_APPELS: I0", "reason": "unknown member"},
            {"field": "members", "text": "RENVOI_APPELS: I004", "reason": "already in TRANSFERT_APPELS"},
            {"field": "id", "text": "transfert appels",
             "reason": "duplicate of TRANSFERT_APPELS: its members are merged into the first entry"},
            {"field": "members", "text": "SEUL: I004", "reason": "already in TRANSFERT_APPELS"},
            {"field": "intents", "text": "not an entry", "reason": "not an object"},
            {"field": "intents", "text": "VIDE", "reason": "no member"},
            {"field": "intents", "text": "SEUL", "reason": "no member"},
        ])
        by_id = {i.id: i for i in p.intents}
        self.assertEqual(sorted(by_id), ["ATTRIBUTION_LIGNE_TELEPHONIQUE", "REINITIALISATION_MOT_DE_PASSE",
                                         "RENVOI_APPELS", "TRANSFERT_APPELS"])
        # the duplicate's members merged into the first entry, its label and description refused
        self.assertEqual(by_id["TRANSFERT_APPELS"].members, ["DEVERROUILLAGE_COMPTE", "TRANSFERT_APPELS"])
        self.assertEqual(by_id["TRANSFERT_APPELS"].label_fr, "Transférer les appels")
        self.assertEqual(by_id["TRANSFERT_APPELS"].description_fr, "")
        self.assertEqual(by_id["RENVOI_APPELS"].members, ["TRANSFERT_APPELS_TEAMS"])   # "I5" is I005
        # the invalid id's member, claimed by nobody else, is its own entry
        self.assertEqual(by_id["ATTRIBUTION_LIGNE_TELEPHONIQUE"].origin, "unmapped")
        covered = sorted(m for i in p.intents for m in i.members)
        self.assertEqual(covered, sorted(r["intent"] for r in proposed_rows(pass_a())))  # each exactly once

    def test_an_id_naming_an_application_is_kept_with_a_warning(self):
        answer = {"intents": [{"id": "TRANSFERT_APPELS_TEAMS", "label_fr": "Transfert Teams", "description_fr": "",
                               "members": ["I005"]}]}
        p = make_taxonomy(llm(answer), pass_a(), {})
        by_id = {i.id: i for i in p.intents}
        self.assertEqual(by_id["TRANSFERT_APPELS_TEAMS"].warnings,
                         ["names the application teams: rename it before freezing"])
        self.assertEqual(by_id["TRANSFERT_APPELS_TEAMS"].origin, "model")
        self.assertIn("1 entries name an application: rename them before freezing", p.warnings)

    def test_an_unmapped_intent_naming_an_application_gets_the_warning_too(self):
        p = make_taxonomy(llm({"intents": []}), pass_a(), {})
        by_id = {i.id: i for i in p.intents}
        self.assertEqual(len(p.intents), 5)
        self.assertTrue(all(i.origin == "unmapped" for i in p.intents))
        self.assertEqual(by_id["TRANSFERT_APPELS_TEAMS"].warnings,
                         ["names the application teams: rename it before freezing"])
        self.assertEqual(by_id["TRANSFERT_APPELS"].warnings, [])
        self.assertIn("5 pass-A intents were left out by the model: each is its own entry", p.warnings)

    def test_application_names_come_from_the_ids_and_the_client_dictionary_spellings(self):
        dictionary = {"harmony": ["Harmony Web", "HRM"], "ad": ["AD"]}
        self.assertEqual(named_apps("TRANSFERT_APPELS_TEAMS", None), ["teams"])
        self.assertEqual(named_apps("TRANSFERT_APPEL_TEAM", None), ["teams"])          # folded like routing
        self.assertEqual(named_apps("ACCES_ACTIVE_DIRECTORY", None), ["active-directory"])
        self.assertEqual(named_apps("VALIDATION_FACTURE_HARMONY", dictionary), ["harmony"])
        self.assertEqual(named_apps("ACCES_WEB", dictionary), ["harmony"])            # a spelling token
        self.assertEqual(named_apps("RESET_HRM", dictionary), ["harmony"])            # 3 characters is enough
        self.assertEqual(named_apps("RESET_AD", dictionary), ["ad"])                  # an app id, whatever its length
        self.assertEqual(named_apps("RESET_AD", None), [])
        self.assertEqual(named_apps("ATTRIBUTION_LIGNE_TELEPHONIQUE", dictionary), [])
        self.assertEqual(named_apps("DEVERROUILLAGE_COMPTE", {"x": ["AD"]}), [])      # 2-character spelling: ignored

    def test_an_intent_the_model_left_out_joins_the_entry_that_has_its_id(self):
        answer = {"intents": [{"id": "TRANSFERT_APPELS", "label_fr": "Transférer", "description_fr": "",
                               "members": ["I005"]}]}
        p = make_taxonomy(llm(answer), pass_a(), {})
        transfer = {i.id: i for i in p.intents}["TRANSFERT_APPELS"]
        self.assertEqual(transfer.members, ["TRANSFERT_APPELS", "TRANSFERT_APPELS_TEAMS"])
        self.assertEqual(transfer.origin, "model")
        self.assertEqual(transfer.warnings,
                         ["TRANSFERT_APPELS was left out by the model and added here: it has this id"])
        self.assertEqual(len({i.id for i in p.intents}), len(p.intents))

    def test_labels_and_descriptions_are_collapsed_and_cut_an_empty_label_takes_pass_a_s(self):
        answer = {"intents": [
            {"id": "TRANSFERT_APPELS", "label_fr": "l " * 200, "description_fr": "d  " * 300, "members": ["I004"]},
            {"id": "DEVERROUILLAGE_COMPTE", "label_fr": "   ", "description_fr": "", "members": ["I002"]},
        ]}
        by_id = {i.id: i for i in make_taxonomy(llm(answer), pass_a(), {}).intents}
        self.assertEqual(by_id["TRANSFERT_APPELS"].label_fr, ("l " * 200)[:MAX_LABEL_CHARS])
        self.assertEqual(len(by_id["TRANSFERT_APPELS"].description_fr), MAX_DESCRIPTION_CHARS)
        self.assertNotIn("  ", by_id["TRANSFERT_APPELS"].description_fr)
        self.assertEqual(by_id["DEVERROUILLAGE_COMPTE"].label_fr, "Déverrouiller un compte")

    def test_an_entry_with_no_label_anywhere_says_so(self):
        items = [enrichment("KB1", "CREATION_COMPTE")]
        p = make_taxonomy(SchemaLLM({(SCHEMA_NAME, "1"): {"intents": []}}), items, {})
        self.assertEqual(p.intents[0].label_fr, "")
        self.assertEqual(p.intents[0].warnings, ["no label: write one before freezing"])

    def test_more_than_the_maximum_is_an_error_with_no_intent(self):
        many = [enrichment(f"KB{i:05d}", f"INTENTION_{i:05d}", "x") for i in range(MAX_TAXONOMY_INTENTS + 1)]
        p = make_taxonomy(SchemaLLM({SCHEMA_NAME: {"intents": []}}), many, {})
        self.assertEqual(p.intents, [])
        self.assertEqual(p.error, "the proposal has 201 intents, more than 200: pass A must be merged further")
        exactly = make_taxonomy(SchemaLLM({SCHEMA_NAME: {"intents": []}}), many[:MAX_TAXONOMY_INTENTS], {})
        self.assertIsNone(exactly.error)
        self.assertEqual(len(exactly.intents), MAX_TAXONOMY_INTENTS)

    def test_a_model_failure_gives_an_empty_proposal_with_its_error(self):
        p = make_taxonomy(llm(LLMError("the answer was cut at max_tokens (12000)")), pass_a(), {})
        self.assertEqual((p.intents, p.error), ([], "the answer was cut at max_tokens (12000)"))
        self.assertNotIn("model", p.provenance)
        self.assertEqual(p.provenance["prompt_version"], TAXONOMY_PROMPT_VERSION)
        p = make_taxonomy(llm(TimeoutError("The read operation timed out")), pass_a(), {})
        self.assertEqual((p.intents, p.error), ([], "TimeoutError: The read operation timed out"))
        p = make_taxonomy(None, pass_a(), {})
        self.assertTrue(p.error.startswith("AttributeError: "))

    def test_no_pass_a_intent_means_no_call(self):
        model = llm()
        p = make_taxonomy(model, [enrichment(BROKEN, None, error="timeout")], {})
        self.assertEqual((p.error, p.intents, model.calls), ("no pass-A intent to merge", [], []))
        self.assertEqual(p.source, {"intents": 0, "fiches": 0, "enrichment_errors": 1})

    def test_a_malformed_answer_is_read_defensively(self):
        for answer in ({"intents": "TRANSFERT_APPELS"}, {}, ["not", "a", "dict"],
                       {"intents": [{"id": 42, "members": "I001"}, {"id": None, "members": None},
                                    {"id": "CREATION", "label_fr": 3, "description_fr": ["x"], "members": ["I001"]}]}):
            p = make_taxonomy(llm(answer), pass_a(), {})
            self.assertIsNone(p.error, answer)
            covered = sorted(m for i in p.intents for m in i.members)
            self.assertEqual(covered, sorted(r["intent"] for r in proposed_rows(pass_a())), answer)
        creation = {i.id: i for i in p.intents}["CREATION"]
        self.assertEqual((creation.label_fr, creation.description_fr), ("Attribuer une ligne téléphonique", ""))

    def test_the_same_pass_a_in_any_order_is_the_same_request_and_the_same_proposal(self):
        a = make_taxonomy(llm(), pass_a(), {}, "r1")
        b = make_taxonomy(llm(), list(reversed(pass_a())), {}, "r1")
        self.assertEqual(a.to_dict(), b.to_dict())
        changed = pass_a()
        changed[0].intent_label_fr = "Associer une ligne"
        c = make_taxonomy(llm(), changed, {}, "r1")
        self.assertNotEqual(c.provenance["request_sha256"], a.provenance["request_sha256"])

    def test_the_proposal_round_trips_through_its_dict(self):
        p = make_taxonomy(llm(), pass_a(), {}, "r1")
        data = json.loads(json.dumps(p.to_dict()))
        self.assertEqual(TaxonomyProposal.from_dict(data), p)
        self.assertEqual(sorted(data["intents"][0]),
                         ["description_fr", "fiches", "id", "label_fr", "members", "origin", "warnings"])
        self.assertEqual(TaxonomyProposal.from_dict({"run_id": "r", "unknown": 1}), TaxonomyProposal(run_id="r"))
        self.assertEqual(TaxonomyIntent.from_dict({"id": "X_Y_Z", "extra": 1}), TaxonomyIntent(id="X_Y_Z", label_fr=""))


class ReplayTest(unittest.TestCase):
    def test_a_replay_writes_the_same_bytes_with_no_model_call(self):
        store = MemoryStore()
        model = llm()
        recorded = make_taxonomy(RecordingLLM(model, store=store, mode="record"), pass_a(), {}, "r1")
        self.assertEqual(model.calls, [(SCHEMA_NAME, "5")])
        replayed = make_taxonomy(RecordingLLM(None, store=store, mode="replay", model_id=model.model_id),
                                 list(reversed(pass_a())), {}, "r1")
        self.assertEqual(json.dumps(replayed.to_dict(), sort_keys=True, ensure_ascii=False),
                         json.dumps(recorded.to_dict(), sort_keys=True, ensure_ascii=False))
        self.assertEqual(len(model.calls), 1)

    def test_replay_without_a_record_fails_into_the_error_field(self):
        p = make_taxonomy(RecordingLLM(None, store=MemoryStore(), mode="replay", model_id="x"), pass_a(), {})
        self.assertIn("replay", p.error)
        self.assertEqual(p.intents, [])


class FreezeTest(unittest.TestCase):
    def test_a_frozen_taxonomy_is_canonical_sorted_and_hashed(self):
        t = freeze([{"id": " transfert appels ", "label_fr": " Transférer   les appels ", "description_fr": None},
                    {"id": "ATTRIBUTION_LIGNE", "label_fr": "Attribuer une ligne", "description_fr": "Une\nphrase."}],
                   3, "Yassine", "2026-10-13T10:00:00Z", "b-1")
        self.assertEqual(t.intents, [
            {"id": "ATTRIBUTION_LIGNE", "label_fr": "Attribuer une ligne", "description_fr": "Une phrase."},
            {"id": "TRANSFERT_APPELS", "label_fr": "Transférer les appels", "description_fr": ""}])
        self.assertEqual((t.version, t.frozen_by, t.frozen_at, t.source_build),
                         (3, "Yassine", "2026-10-13T10:00:00Z", "b-1"))
        self.assertEqual(t.sha256, taxonomy_sha256(t.intents))
        self.assertTrue(t.verify())
        self.assertEqual(t.ids(), ["ATTRIBUTION_LIGNE", "TRANSFERT_APPELS"])

    def test_a_proposal_freezes_as_is(self):
        p = make_taxonomy(llm(), pass_a(), {})
        t = freeze(p.to_dict()["intents"], 1, "Yassine", "2026-10-13T10:00:00Z", "b-1")
        self.assertEqual(t.ids(), [i.id for i in p.intents])
        self.assertEqual(t.sha256, taxonomy_sha256(p.intents))       # members, fiches, warnings are not content
        self.assertEqual(freeze(p.intents, 1, "Y", "t", "b").sha256, t.sha256)

    def test_each_refusal_names_the_row_in_french(self):
        ok = {"id": "ATTRIBUTION_LIGNE", "label_fr": "Attribuer une ligne", "description_fr": ""}
        cases = [
            ([ok, {"id": "", "label_fr": "x"}], "entrée 2 (sans identifiant) : identifiant manquant"),
            ([ok, {"label_fr": "x"}], "entrée 2 (sans identifiant) : identifiant manquant"),
            ([ok, "TRANSFERT"], "entrée 2 (sans identifiant) : ligne illisible"),
            ([{"id": "12_AB", "label_fr": "x"}],
             "entrée 1 (12_AB) : identifiant invalide — des mots en majuscules séparés par « _ », de 3 à 64 "
             "caractères, commençant par une lettre"),
            ([ok, {"id": "attribution ligne", "label_fr": "x"}],
             "entrée 2 (ATTRIBUTION_LIGNE) : identifiant déjà utilisé par l'entrée 1"),
            ([ok, {"id": "TRANSFERT_APPELS", "label_fr": "  "}], "entrée 2 (TRANSFERT_APPELS) : libellé manquant"),
            ([ok, {"id": "TRANSFERT_APPELS"}], "entrée 2 (TRANSFERT_APPELS) : libellé manquant"),
            ([ok, {"id": "TRANSFERT_APPELS", "label_fr": "x" * 121}],
             "entrée 2 (TRANSFERT_APPELS) : libellé trop long (121 caractères, 120 au plus)"),
            ([ok, {"id": "TRANSFERT_APPELS", "label_fr": "x", "description_fr": "d" * 201}],
             "entrée 2 (TRANSFERT_APPELS) : description trop longue (201 caractères, 200 au plus)"),
            ([ok, {"id": "TRANSFERT_APPELS", "label_fr": 7}], "entrée 2 (TRANSFERT_APPELS) : libellé illisible"),
            ([], "la taxonomie est vide : au moins une intention"),
            ("ATTRIBUTION_LIGNE", "la taxonomie doit être une liste d'intentions"),
            ([dict(ok, id=f"INTENTION_{i:03d}") for i in range(201)], "201 intentions : 200 au plus"),
        ]
        for rows, message in cases:
            with self.assertRaises(ValueError, msg=message) as caught:
                freeze(rows, 1, "Yassine", "t", "b")
            self.assertEqual(str(caught.exception), message)
        for version in (0, -1, True, "2", None):
            with self.assertRaises(ValueError):
                freeze([ok], version, "Yassine", "t", "b")

    def test_the_limits_themselves_are_accepted(self):
        rows = [{"id": f"INTENTION_{i:03d}", "label_fr": "x" * MAX_LABEL_CHARS,
                 "description_fr": "d" * MAX_DESCRIPTION_CHARS} for i in range(MAX_TAXONOMY_INTENTS)]
        self.assertEqual(len(freeze(rows, 1, "Y", "t", "b").intents), MAX_TAXONOMY_INTENTS)


class TaxonomyTest(unittest.TestCase):
    def test_a_fingerprint_is_the_meaning_of_an_id(self):
        t = frozen()
        expected = hashlib.sha256("TRANSFERT_APPELS\nTransférer les appels\nRenvoyer les appels.".encode("utf-8"))
        self.assertEqual(t.fingerprint("TRANSFERT_APPELS"), expected.hexdigest()[:16])
        self.assertEqual(len(t.fingerprint("ATTRIBUTION_LIGNE_TELEPHONIQUE")), 16)
        self.assertEqual(t.fingerprint("UNKNOWN_ID"), "")
        self.assertEqual(t.fingerprint(""), "")

    def test_the_hash_is_the_content_whatever_the_order_or_the_extra_keys(self):
        rows = [{"id": "B_B_B", "label_fr": "b", "description_fr": ""},
                {"id": "A_A_A", "label_fr": "a", "description_fr": "d"}]
        expected = hashlib.sha256(json.dumps(sorted(rows, key=lambda r: r["id"]), sort_keys=True, ensure_ascii=False,
                                             separators=(",", ":")).encode("utf-8")).hexdigest()
        self.assertEqual(taxonomy_sha256(rows), expected)
        self.assertEqual(taxonomy_sha256(list(reversed(rows))), expected)
        self.assertEqual(taxonomy_sha256([dict(r, members=["X"], fiches=["F"]) for r in rows]), expected)
        self.assertNotEqual(taxonomy_sha256([dict(rows[0], description_fr="new"), rows[1]]), expected)
        self.assertEqual(frozen(1).sha256, frozen(2).sha256)            # the version is not content

    def test_the_taxonomy_round_trips_and_reads_tolerantly(self):
        t = frozen()
        self.assertEqual(Taxonomy.from_dict(json.loads(json.dumps(t.to_dict()))), t)
        loose = Taxonomy.from_dict({"intents": [{"id": "B_B_B", "label_fr": "b"},
                                                {"id": "A_A_A", "label_fr": "a", "extra": 1}, "junk"], "other": 1})
        self.assertEqual((loose.version, loose.sha256, loose.frozen_by), (0, "", ""))
        self.assertEqual(loose.intents, [{"id": "A_A_A", "label_fr": "a", "description_fr": ""},
                                         {"id": "B_B_B", "label_fr": "b", "description_fr": ""}])   # sorted on load
        self.assertEqual(loose.ids(), ["A_A_A", "B_B_B"])
        self.assertFalse(loose.verify())
        tampered = Taxonomy.from_dict(dict(t.to_dict(), intents=[dict(i, label_fr="changé") for i in t.intents]))
        self.assertFalse(tampered.verify())

    def test_removed_intents_are_the_ids_gone_or_with_a_new_meaning(self):
        old = frozen(1)
        self.assertEqual(removed_intents(None, old), [])
        self.assertEqual(removed_intents(old, frozen(2)), [])
        new = frozen(2, [
            {"id": "TRANSFERT_APPELS", "label_fr": "Transférer les appels", "description_fr": "Autre sens."},
            {"id": "DEVERROUILLAGE_COMPTE", "label_fr": "Déverrouiller", "description_fr": ""},
        ])
        self.assertEqual(removed_intents(old, new), ["ATTRIBUTION_LIGNE_TELEPHONIQUE", "TRANSFERT_APPELS"])
        renamed_label = frozen(2, [
            {"id": "TRANSFERT_APPELS", "label_fr": "Renvoyer les appels", "description_fr": "Renvoyer les appels."},
            {"id": "ATTRIBUTION_LIGNE_TELEPHONIQUE", "label_fr": "Attribuer une ligne", "description_fr": ""},
        ])
        self.assertEqual(removed_intents(old, renamed_label), ["TRANSFERT_APPELS"])


if __name__ == "__main__":
    unittest.main()
