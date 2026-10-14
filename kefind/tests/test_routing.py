import hashlib
import json
import unittest

from kecore.text import clean_text
from kefind.enrich import apps_in
from kefind.funnel import MAX_TEXT_CHARS, KBMap
from kefind.review import ReviewDecision
from kefind.routing import (CODE_VERSION, L0_NUMBER_RE, L1_MAX_UNCOVERED, MIN_GATE_QUESTIONS, NORMALIZATION_VERSION,
                            RoutingTables, build_tables, candidate_codes, identifier_scope, kb_sha256, lookup_l2,
                            match_l0, match_l1, routable)

from .test_funnel import filler, make

TEAMS = "KB0233 - Associate a phone line"
WEBEX = "KB0240 - Webex phone line"
ACCOUNT = "KB0120 - Compte verrouillé"
OLD_VPN = "KB0052 - Ancienne procédure VPN"
VPN = "KB0300 - Procédure VPN"
PRINTER = "KB0400 - Imprimante bourrage"
PRINTER_COPY = "KB0401 - Imprimante bourrage copie"
GP = "KB0076 - Global Protect"
MYSUPPORT = "KB0076 - User not appearing"
OUTLOOK_START = "KB0500 - Outlook ne démarre pas"
OUTLOOK_SYNC = "KB0501 - Outlook synchronisation"
PASSWORD = "KB0600 - Réinitialiser un mot de passe"
SAP_PASSWORD = "KB0601 - Mot de passe SAP"
PRINTER_TEXT = ("Ouvrez le capot avant de l'imprimante. Retirez la feuille coincée doucement. "
                "Refermez le capot et relancez l'impression du document.")


def kbmap(dictionary=None, extra=()) -> KBMap:
    return KBMap("clienta", filler() + [
        make(TEAMS, "Attribuer une ligne téléphonique Teams à un utilisateur. Ouvrez le centre d'administration Teams.",
             entities=["app:teams"]),
        make(WEBEX, "Attribuer une ligne Webex. Ouvrez Control Hub.", entities=["app:webex"]),
        make(ACCOUNT, "Déverrouillez le compte dans la console. L'erreur 0x80070005 peut s'afficher.",
             entities=["err:0x80070005"]),
        make(OLD_VPN, "Ancienne procédure VPN. Cette fiche est remplacée par KB0300.", entities=["err:0x00000ABC"]),
        make(VPN, "Connectez le VPN GlobalProtect. Saisissez le portail.", entities=["app:globalprotect"]),
        make(PRINTER, PRINTER_TEXT, entities=["err:0x000000F1"]),
        make(PRINTER_COPY, PRINTER_TEXT, entities=["err:0x000000F1"]),
        make(GP, "Reconnectez GlobalProtect au portail.", entities=["app:globalprotect"]),
        make(MYSUPPORT, "Synchronisez l'annuaire MySupport."),
        make(OUTLOOK_START, "Outlook ne démarre plus. Lancez Outlook en mode sans échec. Erreur 0x800F081F.",
             entities=["app:outlook", "err:0x800F081F", "err:0x80004005"]),
        make(OUTLOOK_SYNC, "Outlook ne synchronise plus la boîte. Recréez le profil.",
             entities=["app:outlook", "err:0x80004005"]),
        make(PASSWORD, "Réinitialisez le mot de passe Windows dans la console."),
        make(SAP_PASSWORD, "Réinitialisez le mot de passe SAP dans la transaction SU01.", entities=["app:sap"]),
        *extra,
    ], dictionary=dictionary, run_id="r1")


def current_of(kb: KBMap) -> dict[str, str]:
    return {f: hashlib.sha256(f"text of {f}".encode("utf-8")).hexdigest() for f in kb.fiches}


class StubTaxonomy:
    """The frozen-taxonomy duck type (kefind.taxonomy.Taxonomy): version, sha256, intents, ids(), fingerprint()."""

    def __init__(self, intents, version=1):
        self.version = version
        self.intents = sorted(({"id": i, "label_fr": label, "description_fr": ""} for i, label in intents),
                              key=lambda i: i["id"])
        self.sha256 = hashlib.sha256(json.dumps(self.intents, sort_keys=True).encode()).hexdigest()

    def ids(self):
        return [i["id"] for i in self.intents]

    def fingerprint(self, intent_id):
        entry = next(i for i in self.intents if i["id"] == intent_id)
        text = f"{entry['id']}\n{entry['label_fr']}\n{entry['description_fr']}"
        return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


TAXONOMY = StubTaxonomy([("ATTRIBUER_LIGNE", "attribuer une ligne"), ("REINITIALISER_MOT_DE_PASSE", "réinitialiser"),
                         ("DEVERROUILLER_COMPTE", "déverrouiller un compte")])


def decided(fiche_id, current, status="validated", aliases=(), intent=None, app=None, codes=(), taxonomy=TAXONOMY,
            source=None, at="2026-10-13T10:00:00Z", revision=0) -> ReviewDecision:
    return ReviewDecision(
        fiche_id=fiche_id, source_sha256=source or current[fiche_id], status=status, intent=intent,
        intent_fingerprint=taxonomy.fingerprint(intent) if intent and taxonomy else "",
        taxonomy_version=taxonomy.version if intent and taxonomy else 0, primary_app=app,
        aliases=[{"text": a, "lang": "fr", "source": "manual", "ref_id": ""} for a in aliases], codes=list(codes),
        decided_by="Yassine", decided_at=at, revision=revision)


def question(tables, kb, text):
    cleaned = clean_text(text)[:MAX_TEXT_CHARS]
    scope = identifier_scope(kb, cleaned)
    apps = apps_in(cleaned, kb.dictionary) & set(tables.apps)
    return cleaned, scope, apps


def l0(tables, kb, text):
    cleaned, scope, _ = question(tables, kb, text)
    return match_l0(tables, kb, cleaned, scope)


def l1(tables, kb, text):
    cleaned, scope, apps = question(tables, kb, text)
    return match_l1(tables, cleaned, apps, scope)


class ConstantsTest(unittest.TestCase):
    def test_code_version(self):
        self.assertEqual(CODE_VERSION, {"tables": 1, "normalization": NORMALIZATION_VERSION, "route": 1,
                                        "map_prompt": 1, "l1_max_uncovered": L1_MAX_UNCOVERED})
        self.assertEqual((L1_MAX_UNCOVERED, MIN_GATE_QUESTIONS), (1, 30))

    def test_strict_fiche_number(self):
        found = [m.group(1) for m in L0_NUMBER_RE.finditer("KB0233, kb-0052, KB_0300 et KB 0400, 512 kb 300, x.KB0601, KB5")]
        self.assertEqual(found, ["233", "52", "300"])


class RoutableTest(unittest.TestCase):
    def test_superseded_and_duplicate_fiches_are_not_routable(self):
        kb = kbmap()
        ids = routable(kb)
        self.assertNotIn(OLD_VPN, ids)        # replaced by KB0300: today's engine shows KB0300 instead
        self.assertNotIn(PRINTER_COPY, ids)   # a duplicate of KB0400
        self.assertIn(VPN, ids)
        self.assertIn(PRINTER, ids)
        self.assertIn(OLD_VPN, kb.ranked)     # kbmap.ranked alone keeps the replaced fiche
        self.assertEqual(ids, sorted(ids))

    def test_candidate_codes_are_the_codes_one_routable_fiche_carries(self):
        codes = candidate_codes(kbmap())
        self.assertEqual(codes[ACCOUNT], ["err:0x80070005"])
        self.assertEqual(codes[OUTLOOK_START], ["err:0x800F081F"])     # 0x80004005 is shared with KB0501
        self.assertNotIn(OUTLOOK_SYNC, codes)
        self.assertEqual(codes[PRINTER], ["err:0x000000F1"])           # the duplicate's copy resolves to it
        self.assertEqual(codes[VPN], ["err:0x00000ABC"])               # the replaced fiche's code: its replacement
        self.assertNotIn(OLD_VPN, codes)


class L0Test(unittest.TestCase):
    def setUp(self):
        self.kb = kbmap()
        self.current = current_of(self.kb)

    def build(self, decisions=(), taxonomy=None):
        return build_tables(self.kb, self.current, taxonomy, list(decisions), {}, "b1")

    def test_numbers_of_routable_fiches(self):
        tables = self.build()
        self.assertEqual(tables.l0_numbers["233"], TEAMS)
        self.assertEqual(tables.l0_numbers["300"], VPN)
        self.assertEqual(tables.l0_numbers["400"], PRINTER)
        self.assertNotIn("52", tables.l0_numbers)    # superseded: not routable
        self.assertNotIn("401", tables.l0_numbers)   # duplicate: not routable

    def test_a_number_two_fiches_carry_is_a_collision(self):
        tables = self.build()
        self.assertNotIn("76", tables.l0_numbers)
        self.assertEqual(tables.collisions["l0_numbers"], {"76": [GP, MYSUPPORT]})
        result = l0(tables, self.kb, "Problème KB0076")
        self.assertFalse(result["decided"])
        self.assertEqual(result["scope"]["intersection"], [GP, MYSUPPORT])

    def test_a_fiche_number_decides(self):
        result = l0(self.build(), self.kb, "comment faire la kb0233 ?")
        self.assertEqual((result["decided"], result["rule"], result["key"], result["fiche_id"]),
                         (True, "fiche_number", "233", TEAMS))

    def test_superseded_and_duplicate_numbers_resolve_to_the_replacement_or_nothing(self):
        tables = self.build()
        old = l0(tables, self.kb, "procédure KB0052")
        self.assertEqual(old["scope"]["intersection"], [VPN])  # the graph resolves it to its replacement...
        self.assertFalse(old["decided"])                        # ...but only KB0300's own number routes
        self.assertTrue(l0(tables, self.kb, "procédure KB0300")["decided"])
        copy = l0(tables, self.kb, "voir KB0401")
        self.assertEqual(copy["scope"]["intersection"], [PRINTER])
        self.assertFalse(copy["decided"])
        for result in (old, copy):
            self.assertNotIn(result["fiche_id"], (OLD_VPN, PRINTER_COPY))

    def test_a_rejected_fiche_is_left_out_of_l0(self):
        tables = self.build([decided(TEAMS, self.current, status="rejected")])
        self.assertNotIn("233", tables.l0_numbers)
        self.assertFalse(l0(tables, self.kb, "KB0233")["decided"])
        self.assertEqual(tables.stats["rejected"], 1)
        # a fiche merely proposed (never reviewed) keeps its number: the number designates it
        self.assertIn("300", tables.l0_numbers)

    def test_a_number_written_loosely_restricts_the_scope_but_never_decides(self):
        result = l0(self.build(), self.kb, "voir KB 0233")
        self.assertEqual(result["scope"]["intersection"], [TEAMS])
        self.assertFalse(result["decided"])

    def test_codes_route_only_once_a_person_validated_them(self):
        self.assertFalse(l0(self.build(), self.kb, "erreur 0x80070005")["decided"])
        tables = self.build([decided(ACCOUNT, self.current, codes=["err:0x80070005"])])
        self.assertEqual(tables.l0_codes, {"err:0x80070005": ACCOUNT})
        result = l0(tables, self.kb, "J'ai l'erreur 0X80070005 au démarrage")
        self.assertEqual((result["decided"], result["rule"], result["key"], result["fiche_id"]),
                         (True, "identifier", "err:0x80070005", ACCOUNT))

    def test_two_identifiers_pointing_to_different_fiches_block_l0(self):
        tables = self.build([decided(ACCOUNT, self.current, codes=["err:0x80070005"])])
        result = l0(tables, self.kb, "erreur 0x80070005 puis 0x800F081F")
        self.assertEqual(result["scope"]["resolutions"], [[ACCOUNT], [OUTLOOK_START]])
        self.assertEqual(result["scope"]["intersection"], [])
        self.assertFalse(result["decided"])
        self.assertFalse(l0(tables, self.kb, "erreur 0x80070005 voir KB0233")["decided"])

    def test_a_shared_code_does_not_stop_a_number_that_agrees(self):
        tables = self.build()
        result = l0(tables, self.kb, "KB0500 erreur 0x80004005")
        self.assertEqual(result["scope"]["resolutions"], [[OUTLOOK_START], [OUTLOOK_START, OUTLOOK_SYNC]])
        self.assertEqual((result["decided"], result["fiche_id"]), (True, OUTLOOK_START))

    def test_a_code_no_longer_unique_is_skipped(self):
        # a stored decision claims a code two fiches carry (the KB changed since the review)
        tables = self.build([decided(OUTLOOK_SYNC, self.current, codes=["err:0x80004005"])])
        self.assertEqual(tables.l0_codes, {})
        self.assertEqual(tables.stats["skipped"]["code_not_unique"], 1)

    def test_no_identifier(self):
        result = l0(self.build(), self.kb, "mon compte est verrouillé")
        self.assertEqual(result["scope"], {"resolutions": [], "intersection": [], "union": [], "any": False})
        self.assertFalse(result["decided"])


class IdentifierScopeTest(unittest.TestCase):
    def test_a_number_no_fiche_carries_points_nowhere(self):
        kb = kbmap()
        scope = identifier_scope(kb, "mise à jour KB5034441 puis KB0233")
        self.assertEqual(scope, {"resolutions": [[TEAMS]], "intersection": [TEAMS], "union": [TEAMS], "any": True})

    def test_union_and_intersection(self):
        kb = kbmap()
        scope = identifier_scope(kb, "KB0300 et KB0052 et erreur 0x80070005")
        self.assertEqual(scope["resolutions"], [[VPN], [VPN], [ACCOUNT]])
        self.assertEqual((scope["intersection"], scope["union"], scope["any"]), ([], [ACCOUNT, VPN], True))


class L1Test(unittest.TestCase):
    def setUp(self):
        self.kb = kbmap()
        self.current = current_of(self.kb)

    def build(self, decisions=(), proposals=None, taxonomy=None):
        return build_tables(self.kb, self.current, taxonomy, list(decisions), proposals or {}, "b1")

    def test_a_validated_alias_routes_its_question(self):
        tables = self.build([decided(TEAMS, self.current, aliases=["attribuer ligne Teams"], app="teams")])
        self.assertEqual(tables.l1, [{"tokens": ["attribuer", "ligne", "team"], "fiche_id": TEAMS,
                                      "alias": "attribuer ligne Teams", "lang": "fr", "source": "manual",
                                      "ref_id": "", "app": "teams"}])
        result = l1(tables, self.kb, "comment attribuer une ligne teams")
        self.assertEqual((result["decided"], result["fiche_id"], result["key"], result["uncovered"]),
                         (True, TEAMS, "attribuer ligne Teams", 0))
        self.assertEqual(result["matches"], [{"fiche_id": TEAMS, "alias": "attribuer ligne Teams"}])

    def test_a_proposed_alias_never_routes(self):
        tables = self.build(proposals={TEAMS: {"intent": None, "app": "teams", "aliases": ["attribuer ligne teams"]}})
        self.assertEqual(tables.l1, [])
        self.assertFalse(l1(tables, self.kb, "attribuer une ligne teams")["decided"])

    def test_a_teams_alias_never_routes_a_webex_question(self):
        tables = self.build([decided(TEAMS, self.current, aliases=["attribuer une ligne téléphonique"], app="teams")])
        webex = l1(tables, self.kb, "attribuer une ligne téléphonique Webex")
        self.assertFalse(webex["decided"])
        self.assertEqual(webex["matches"], [])
        teams = l1(tables, self.kb, "attribuer une ligne téléphonique Teams")
        self.assertEqual((teams["decided"], teams["fiche_id"], teams["uncovered"]), (True, TEAMS, 1))
        # a question naming no application: the alias still applies
        self.assertTrue(l1(tables, self.kb, "attribuer une ligne téléphonique")["decided"])

    def test_the_most_specific_alias_wins(self):
        tables = self.build([decided(ACCOUNT, self.current, aliases=["compte bloqué"]),
                             decided(OUTLOOK_START, self.current, aliases=["compte bloqué outlook"], app="outlook")])
        result = l1(tables, self.kb, "mon compte Outlook est bloqué")
        self.assertEqual((result["decided"], result["fiche_id"], result["ambiguous"]), (True, OUTLOOK_START, False))
        self.assertEqual(len(result["matches"]), 2)
        self.assertEqual(l1(tables, self.kb, "mon compte est bloqué")["fiche_id"], ACCOUNT)

    def test_neither_alias_including_the_other_is_ambiguous(self):
        tables = self.build([decided(ACCOUNT, self.current, aliases=["compte bloqué"]),
                             decided(OUTLOOK_START, self.current, aliases=["outlook bloqué"], app="outlook")])
        result = l1(tables, self.kb, "compte outlook bloqué")
        self.assertEqual((result["decided"], result["ambiguous"], result["fiche_id"]), (False, True, None))

    def test_the_question_may_not_say_much_more_than_the_alias(self):
        tables = self.build([decided(OUTLOOK_START, self.current, aliases=["Outlook ne démarre pas"], app="outlook")])
        result = l1(tables, self.kb, "Outlook démarre mais ne synchronise pas")
        self.assertEqual((result["decided"], result["uncovered"], result["candidate"]), (False, 2, OUTLOOK_START))
        self.assertTrue(l1(tables, self.kb, "Outlook ne démarre pas du tout")["decided"])   # one token more: "tout"

    def test_a_long_text_mentioning_the_alias_words_is_not_routed(self):
        tables = self.build([decided(ACCOUNT, self.current, aliases=["compte bloqué"])])
        text = "Bonjour, suite à l'échange d'hier le compte du stagiaire est bloqué, merci de rappeler le client."
        result = l1(tables, self.kb, text)
        self.assertFalse(result["decided"])
        self.assertGreater(result["uncovered"], L1_MAX_UNCOVERED)

    def test_two_fiches_claiming_one_alias_collide_and_still_block(self):
        tables = self.build([decided(ACCOUNT, self.current, aliases=["compte bloqué"]),
                             decided(PASSWORD, self.current, aliases=["compte bloqué", "compte bloqué mot de passe"]),
                             decided(OUTLOOK_START, self.current, aliases=["outlook bloqué"], app="outlook")])
        self.assertEqual(tables.collisions["l1"], [{"tokens": ["bloque", "compte"], "fiches": [ACCOUNT, PASSWORD],
                                                    "aliases": ["compte bloqué"]}])
        self.assertNotIn(["bloque", "compte"], [e["tokens"] for e in tables.l1])
        self.assertFalse(l1(tables, self.kb, "compte bloqué")["decided"])
        # "outlook bloqué" alone in front would be exact; the colliding pair still competes with it
        blocked = l1(tables, self.kb, "compte outlook bloqué")
        self.assertEqual((blocked["decided"], blocked["ambiguous"]), (False, True))
        self.assertEqual(blocked["blockers"], [{"tokens": ["bloque", "compte"], "fiches": [ACCOUNT, PASSWORD]}])
        # a more specific alias of one of them still wins over the collision
        self.assertEqual(l1(tables, self.kb, "compte bloqué mot de passe")["fiche_id"], PASSWORD)

    def test_an_alias_a_fiche_under_review_proposes_is_blocked(self):
        tables = self.build([decided(TEAMS, self.current, aliases=["attribuer ligne teams"], app="teams")],
                            proposals={WEBEX: {"intent": None, "app": "webex", "aliases": ["attribuer une ligne Teams"]}})
        self.assertEqual(tables.l1, [])
        self.assertEqual(tables.blocked["l1"], [{"tokens": ["attribuer", "ligne", "team"], "fiches": [TEAMS],
                                                 "reason": "pending review", "pending": [WEBEX]}])
        self.assertFalse(l1(tables, self.kb, "attribuer une ligne teams")["decided"])

    def test_a_reviewed_fiche_never_blocks_with_its_proposals(self):
        tables = self.build([decided(TEAMS, self.current, aliases=["attribuer ligne teams"], app="teams"),
                             decided(WEBEX, self.current, status="rejected")],
                            proposals={WEBEX: {"intent": None, "app": "webex", "aliases": ["attribuer ligne teams"]}})
        self.assertEqual([e["fiche_id"] for e in tables.l1], [TEAMS])

    def test_a_stale_decisions_alias_blocks(self):
        old = decided(WEBEX, self.current, aliases=["attribuer ligne teams"], source="old-text-hash",
                      at="2026-10-01T09:00:00Z")
        tables = self.build([decided(TEAMS, self.current, aliases=["attribuer ligne teams"], app="teams"), old])
        self.assertEqual(tables.stats["stale"], 1)
        self.assertEqual(tables.blocked["l1"][0]["pending"], [WEBEX])
        self.assertFalse(l1(tables, self.kb, "attribuer ligne teams")["decided"])
        self.assertTrue(tables.verify())

    def test_a_fiche_back_in_review_never_hands_its_question_to_a_vaguer_alias(self):
        start = decided(OUTLOOK_START, self.current, aliases=["outlook ne démarre plus"], app="outlook")
        sync = decided(OUTLOOK_SYNC, self.current, aliases=["outlook démarre"], app="outlook")
        tables = self.build([start, sync])
        self.assertEqual(l1(tables, self.kb, "outlook ne démarre plus")["fiche_id"], OUTLOOK_START)
        # OUTLOOK_START's text changed: its decision is stale, its alias no longer routes -- and it must not
        # leave its question to the less specific alias of another fiche
        stale = decided(OUTLOOK_START, self.current, aliases=["outlook ne démarre plus"], app="outlook",
                        source="old-text-hash")
        tables = self.build([stale, sync])
        self.assertEqual(tables.pending, [{"tokens": ["demarre", "outlook", "plus"], "fiches": [OUTLOOK_START]}])
        result = l1(tables, self.kb, "outlook ne démarre plus")
        self.assertFalse(result["decided"])
        self.assertTrue(result["ambiguous"])
        self.assertEqual(l1(tables, self.kb, "outlook démarre")["fiche_id"], OUTLOOK_SYNC)
        self.assertEqual(tables.stats["pending_l1"], 1)
        self.assertTrue(tables.verify())

    def test_an_unreviewed_fiches_proposal_stops_a_vaguer_alias_never_decides(self):
        tables = self.build([decided(PASSWORD, self.current, aliases=["réinitialiser mot de passe"])],
                            proposals={SAP_PASSWORD: {"intent": None, "app": "sap",
                                                      "aliases": ["réinitialiser mot de passe SAP"]}})
        self.assertEqual([e["fiche_id"] for e in tables.l1], [PASSWORD])
        self.assertFalse(l1(tables, self.kb, "réinitialiser mot de passe SAP")["decided"])
        self.assertEqual(l1(tables, self.kb, "réinitialiser le mot de passe")["fiche_id"], PASSWORD)
        # what a fiche under review proposes is in the content: reviewing it changes the tables
        reviewed = self.build([decided(PASSWORD, self.current, aliases=["réinitialiser mot de passe"]),
                               decided(SAP_PASSWORD, self.current, status="rejected")],
                              proposals={SAP_PASSWORD: {"intent": None, "app": "sap",
                                                        "aliases": ["réinitialiser mot de passe SAP"]}})
        self.assertEqual(reviewed.pending, [])
        self.assertNotEqual(reviewed.content_sha256, tables.content_sha256)
        self.assertEqual(l1(reviewed, self.kb, "réinitialiser mot de passe SAP")["fiche_id"], PASSWORD)

    def test_a_question_citing_another_fiches_number_blocks_l1_outside_its_scope(self):
        tables = self.build([decided(TEAMS, self.current, aliases=["attribuer ligne teams"], app="teams")])
        outside = l1(tables, self.kb, "attribuer ligne teams KB0120")
        self.assertEqual((outside["decided"], outside["outside_scope"], outside["uncovered"]), (False, True, 1))
        inside = l1(tables, self.kb, "attribuer ligne teams KB0233")
        self.assertEqual((inside["decided"], inside["outside_scope"]), (True, False))

    def test_an_alias_routing_cannot_read_is_skipped(self):
        stored = decided(PRINTER, self.current, aliases=["imprimante étage 3", "bloquer"])   # bypassed validation
        tables = self.build([stored])
        self.assertEqual(tables.l1, [])
        self.assertEqual(tables.stats["skipped"]["alias_tokens"], 2)

    def test_an_application_left_the_vocabulary(self):
        kb = kbmap(dictionary={"harmony": ["Harmony"]})
        current = current_of(kb)
        stored = decided(PASSWORD, current, aliases=["réinitialiser mot de passe harmony"], app="harmony")
        self.assertEqual(len(build_tables(kb, current, None, [stored], {}, "b1").l1), 1)
        tables = build_tables(kbmap(), current, None, [stored], {}, "b1")   # the dictionary lost "harmony"
        self.assertEqual(tables.l1, [])
        self.assertEqual(tables.stats["skipped"]["app_not_in_vocabulary"], 1)

    def test_nothing_matches(self):
        tables = self.build([decided(TEAMS, self.current, aliases=["attribuer ligne teams"], app="teams")])
        result = l1(tables, self.kb, "")
        self.assertEqual(result, {"decided": False, "fiche_id": None, "key": None, "tokens": [], "matches": [],
                                  "blockers": [], "ambiguous": False, "uncovered": 0, "outside_scope": False,
                                  "candidate": None})


class L2Test(unittest.TestCase):
    def setUp(self):
        self.kb = kbmap()
        self.current = current_of(self.kb)

    def build(self, decisions=(), proposals=None, taxonomy=TAXONOMY):
        return build_tables(self.kb, self.current, taxonomy, list(decisions), proposals or {}, "b1")

    def lines(self):
        return [decided(TEAMS, self.current, intent="ATTRIBUER_LIGNE", app="teams"),
                decided(WEBEX, self.current, intent="ATTRIBUER_LIGNE", app="webex")]

    def test_keys(self):
        tables = self.build(self.lines() + [decided(PASSWORD, self.current, intent="REINITIALISER_MOT_DE_PASSE")])
        self.assertEqual(tables.l2, [{"intent": "ATTRIBUER_LIGNE", "app": "teams", "fiches": [TEAMS]},
                                     {"intent": "ATTRIBUER_LIGNE", "app": "webex", "fiches": [WEBEX]},
                                     {"intent": "REINITIALISER_MOT_DE_PASSE", "app": "", "fiches": [PASSWORD]}])
        self.assertEqual(tables.taxonomy["sha256"], TAXONOMY.sha256)
        self.assertEqual([i["id"] for i in tables.taxonomy["intents"]], TAXONOMY.ids())

    def test_no_intent_gives_nothing(self):
        tables = self.build(self.lines())
        self.assertEqual(lookup_l2(tables, None, "teams", True), {"exact": None, "offer": [], "reason": "no_intent"})
        self.assertEqual(lookup_l2(tables, "", "", False)["exact"], None)

    def test_an_exact_key_with_one_fiche(self):
        tables = self.build(self.lines())
        self.assertEqual(lookup_l2(tables, "ATTRIBUER_LIGNE", "teams", True),
                         {"exact": TEAMS, "offer": [], "reason": "intent_app"})
        self.assertEqual(lookup_l2(tables, "ATTRIBUER_LIGNE", "webex", False)["exact"], WEBEX)

    def test_several_fiches_for_a_key_are_offered(self):
        tables = self.build([decided(TEAMS, self.current, intent="ATTRIBUER_LIGNE", app="teams"),
                             decided(VPN, self.current, intent="ATTRIBUER_LIGNE", app="teams")])
        self.assertEqual(lookup_l2(tables, "ATTRIBUER_LIGNE", "teams", True),
                         {"exact": None, "offer": [TEAMS, VPN], "reason": "several_fiches"})
        self.assertEqual(tables.collisions["l2"], [{"intent": "ATTRIBUER_LIGNE", "app": "teams", "fiches": [TEAMS, VPN]}])

    def test_a_missing_application_key_offers_the_fiches_naming_no_application_never_exact(self):
        tables = self.build([decided(PASSWORD, self.current, intent="REINITIALISER_MOT_DE_PASSE")])
        self.assertEqual(lookup_l2(tables, "REINITIALISER_MOT_DE_PASSE", "sap", True),
                         {"exact": None, "offer": [PASSWORD], "reason": "no_fiche_for_app_generic_offered"})
        self.assertEqual(lookup_l2(tables, "ATTRIBUER_LIGNE", "zoom", True),
                         {"exact": None, "offer": [], "reason": "no_fiche"})

    def test_without_an_application(self):
        generic = self.build([decided(PASSWORD, self.current, intent="REINITIALISER_MOT_DE_PASSE")])
        self.assertEqual(lookup_l2(generic, "REINITIALISER_MOT_DE_PASSE", "", False),
                         {"exact": PASSWORD, "offer": [], "reason": "intent_no_app"})
        # the question names an application the tables cannot place: the generic fiche is only offered
        self.assertEqual(lookup_l2(generic, "REINITIALISER_MOT_DE_PASSE", None, True),
                         {"exact": None, "offer": [PASSWORD], "reason": "question_names_an_application"})
        # the intent's only fiche is about one application: offered, never exact
        sap = self.build([decided(SAP_PASSWORD, self.current, intent="REINITIALISER_MOT_DE_PASSE", app="sap")])
        self.assertEqual(lookup_l2(sap, "REINITIALISER_MOT_DE_PASSE", "", False),
                         {"exact": None, "offer": [SAP_PASSWORD], "reason": "fiche_of_an_application"})
        both = self.build([decided(PASSWORD, self.current, intent="REINITIALISER_MOT_DE_PASSE"),
                           decided(SAP_PASSWORD, self.current, intent="REINITIALISER_MOT_DE_PASSE", app="sap")])
        self.assertEqual(lookup_l2(both, "REINITIALISER_MOT_DE_PASSE", "", False),
                         {"exact": None, "offer": [PASSWORD, SAP_PASSWORD], "reason": "several_fiches"})
        self.assertEqual(lookup_l2(both, "REINITIALISER_MOT_DE_PASSE", "sap", True)["exact"], SAP_PASSWORD)

    def test_a_key_is_blocked_by_a_fiche_still_under_review(self):
        proposals = {SAP_PASSWORD: {"intent": "REINITIALISER_MOT_DE_PASSE", "app": "sap", "aliases": []},
                     OUTLOOK_SYNC: {"intent": "ATTRIBUER_LIGNE", "app": "", "aliases": []}}
        tables = self.build(self.lines() + [decided(PASSWORD, self.current, intent="REINITIALISER_MOT_DE_PASSE")],
                            proposals=proposals)
        # a pending fiche naming no application blocks every key of its intent
        self.assertEqual(tables.l2, [])
        self.assertEqual(lookup_l2(tables, "ATTRIBUER_LIGNE", "teams", True),
                         {"exact": None, "offer": [TEAMS, OUTLOOK_SYNC], "reason": "pending_review"})
        # a pending SAP fiche blocks the key without application: a question naming none may be about SAP
        self.assertEqual(lookup_l2(tables, "REINITIALISER_MOT_DE_PASSE", "", False),
                         {"exact": None, "offer": [PASSWORD, SAP_PASSWORD], "reason": "pending_review"})
        self.assertEqual(tables.blocked["l2"][-1], {"intent": "REINITIALISER_MOT_DE_PASSE", "app": "",
                                                    "fiches": [PASSWORD], "pending": [SAP_PASSWORD]})

    def test_a_fiche_whose_pass_c_gave_nothing_blocks_every_key(self):
        failed = {SAP_PASSWORD: {"intent": None, "app": "sap", "aliases": [], "intent_known": False}}
        tables = self.build(self.lines(), proposals=failed)
        self.assertEqual(tables.l2, [])
        self.assertEqual([e["pending"] for e in tables.blocked["l2"]], [[SAP_PASSWORD], [SAP_PASSWORD]])
        self.assertIsNone(lookup_l2(tables, "ATTRIBUER_LIGNE", "teams", True)["exact"])
        # pass C answered "no intent fits": known, blocks nothing
        none_fits = {SAP_PASSWORD: {"intent": None, "app": "sap", "aliases": [], "intent_known": True}}
        self.assertEqual(len(self.build(self.lines(), proposals=none_fits).l2), 2)

    def test_a_pending_fiche_of_another_application_does_not_block(self):
        tables = self.build(self.lines(), proposals={VPN: {"intent": "ATTRIBUER_LIGNE", "app": "zoom", "aliases": []}})
        self.assertEqual(lookup_l2(tables, "ATTRIBUER_LIGNE", "teams", True)["exact"], TEAMS)
        self.assertEqual(lookup_l2(tables, "ATTRIBUER_LIGNE", "zoom", True),
                         {"exact": None, "offer": [], "reason": "no_fiche"})

    def test_a_stale_fiches_previous_intent_blocks(self):
        old = decided(VPN, self.current, intent="ATTRIBUER_LIGNE", app="teams", source="old-text-hash")
        tables = self.build(self.lines() + [old])
        self.assertEqual(lookup_l2(tables, "ATTRIBUER_LIGNE", "teams", True)["reason"], "pending_review")
        self.assertEqual(lookup_l2(tables, "ATTRIBUER_LIGNE", "webex", True)["exact"], WEBEX)

    def test_an_intent_whose_meaning_changed_is_skipped(self):
        refrozen = StubTaxonomy([("ATTRIBUER_LIGNE", "attribuer une ligne fixe"),
                                 ("REINITIALISER_MOT_DE_PASSE", "réinitialiser")], version=2)
        tables = self.build(self.lines(), taxonomy=refrozen)
        self.assertEqual(tables.l2, [])
        self.assertEqual(tables.stats["skipped"]["intent_changed"], 2)
        self.assertEqual(lookup_l2(tables, "ATTRIBUER_LIGNE", "teams", True)["exact"], None)

    def test_an_intent_no_longer_in_the_taxonomy_is_skipped(self):
        smaller = StubTaxonomy([("REINITIALISER_MOT_DE_PASSE", "réinitialiser")], version=2)
        tables = self.build(self.lines(), taxonomy=smaller)
        self.assertEqual((tables.l2, tables.stats["skipped"]["intent_not_in_taxonomy"]), ([], 2))

    def test_no_taxonomy_no_l2(self):
        tables = self.build(self.lines(), taxonomy=None)
        self.assertEqual((tables.l2, tables.taxonomy, tables.blocked["l2"]), ([], {}, []))
        self.assertEqual(lookup_l2(tables, "ATTRIBUER_LIGNE", "teams", True)["exact"], None)


class TablesTest(unittest.TestCase):
    def setUp(self):
        self.kb = kbmap()
        self.current = current_of(self.kb)
        self.decisions = [
            decided(TEAMS, self.current, aliases=["attribuer ligne teams"], intent="ATTRIBUER_LIGNE", app="teams"),
            decided(ACCOUNT, self.current, aliases=["compte bloqué"], codes=["err:0x80070005"],
                    intent="DEVERROUILLER_COMPTE"),
            decided(WEBEX, self.current, status="rejected"),
            decided(VPN, self.current, aliases=["connecter vpn"], source="old-text-hash"),
        ]

    def build(self, decisions=None, build_id="b1", kb=None, current=None):
        return build_tables(kb or self.kb, current or self.current, TAXONOMY,
                            self.decisions if decisions is None else decisions, {}, build_id)

    def test_stats(self):
        tables = self.build()
        stats = tables.stats
        self.assertEqual({k: stats[k] for k in ("validated", "rejected", "stale")}, {"validated": 2, "rejected": 1, "stale": 1})
        self.assertEqual(stats["routable"], len(routable(self.kb)))
        self.assertEqual(stats["proposed"], stats["routable"] - 4)
        self.assertEqual((stats["l0_codes"], stats["l1"], stats["l2"]), (1, 2, 2))
        self.assertEqual(stats["skipped"], {"alias_tokens": 0, "intent_not_in_taxonomy": 0, "intent_changed": 0,
                                            "app_not_in_vocabulary": 0, "code_not_unique": 0})
        self.assertEqual((tables.client, tables.run_id, tables.build_id), ("clienta", "r1", "b1"))
        self.assertEqual(tables.code_version, CODE_VERSION)

    def test_content_sha256_ignores_build_and_run_ids_and_stats(self):
        one = self.build(build_id="b1")
        two = self.build(build_id="b2")
        self.assertEqual(one.content_sha256, two.content_sha256)
        self.assertNotEqual(one.sha256, two.sha256)
        two.run_id = "r2"
        two.stats = {"anything": 1}
        self.assertEqual(two.compute_content_sha256(), one.content_sha256)
        # the alias wording is not routing content: only its tokens are
        reworded = self.build([decided(TEAMS, self.current, aliases=["Attribuer une ligne TEAMS"],
                                       intent="ATTRIBUER_LIGNE", app="teams")] + self.decisions[1:])
        self.assertEqual(reworded.content_sha256, one.content_sha256)

    def test_content_sha256_follows_what_routes(self):
        base = self.build()
        changed = self.build([decided(TEAMS, self.current, aliases=["attribuer ligne teams"], intent="ATTRIBUER_LIGNE",
                                      app="webex")] + self.decisions[1:])
        self.assertNotEqual(changed.content_sha256, base.content_sha256)
        other_code = RoutingTables.from_dict(base.to_dict())
        other_code.code_version = {**CODE_VERSION, "route": 2}
        self.assertNotEqual(other_code.compute_content_sha256(), base.content_sha256)

    def test_deterministic_whatever_the_order_of_the_decisions(self):
        self.assertEqual(self.build().to_bytes(), self.build(list(reversed(self.decisions))).to_bytes())

    def test_serialization_and_verify(self):
        tables = self.build()
        self.assertTrue(tables.verify())
        raw = tables.to_bytes()
        self.assertTrue(raw.endswith(b"}\n"))
        loaded = RoutingTables.from_dict(json.loads(raw.decode("utf-8")))
        self.assertEqual(loaded.to_dict(), tables.to_dict())
        self.assertTrue(loaded.verify())
        self.assertEqual(loaded.to_bytes(), raw)
        tampered = json.loads(raw.decode("utf-8"))
        tampered["l1"][0]["fiche_id"] = WEBEX
        self.assertFalse(RoutingTables.from_dict(tampered).verify())
        cut = json.loads(raw.decode("utf-8"))
        cut["stats"]["l1"] = 99
        self.assertFalse(RoutingTables.from_dict(cut).verify())   # sha256 covers the stats too
        self.assertFalse(RoutingTables(client="c", run_id="r", build_id="b").verify())   # never sealed

    def test_from_dict_is_tolerant(self):
        tables = RoutingTables.from_dict({"client": "c", "unknown": 1, "blocked": {"l1": []}})
        self.assertEqual(tables.blocked, {"l1": [], "l2": []})
        self.assertEqual(tables.collisions, {"l0_numbers": {}, "l1": [], "l2": []})
        self.assertEqual(lookup_l2(tables, "X", "", False)["exact"], None)

    def test_kb_sha256(self):
        base = kb_sha256(self.kb, self.current)
        self.assertEqual(base, self.build().kb_sha256)
        self.assertEqual(base, kb_sha256(kbmap(), dict(self.current)))
        moved = dict(self.current, **{TEAMS: "edited"})
        self.assertNotEqual(kb_sha256(self.kb, moved), base)
        replaced_copy = dict(self.current, **{OLD_VPN: "edited"})   # a non-routable fiche's codes still resolve
        self.assertNotEqual(kb_sha256(self.kb, replaced_copy), base)
        self.assertNotEqual(kb_sha256(kbmap(dictionary={"harmony": ["Harmony"]}), self.current), base)
        added = kbmap(extra=[make("KB0700 - Nouvelle fiche", "Une nouvelle procédure. Suivez-la.")])
        self.assertNotEqual(kb_sha256(added, current_of(added)), kb_sha256(kbmap(), current_of(added)))
        # the decisions are not the KB: a new decision leaves it alone
        self.assertEqual(self.build(self.decisions[:1]).kb_sha256, base)


if __name__ == "__main__":
    unittest.main()
