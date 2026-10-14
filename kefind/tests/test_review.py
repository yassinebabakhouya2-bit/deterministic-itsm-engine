import hashlib
import json
import unittest

from kefind.review import (ALIAS_SOURCES, MAX_ALIASES, STATUSES, ChangedError, ReviewDecision, UnknownFicheError,
                           check_seen, current_decisions, current_status, decision_row_key, fiche_key,
                           fiche_statuses, seen_reference_ids, status_counts, validate_decision)

from .test_routing import TAXONOMY, StubTaxonomy

TEAMS = "KB0233 - Associate a phone line"
ACCOUNT = "KB0120 - Compte verrouillé"
OTHER = "KB0300 – Procédure VPN"   # an en dash, as in real document names
CURRENT = {TEAMS: "a" * 64, ACCOUNT: "b" * 64, OTHER: "c" * 64}
ROUTABLE = {TEAMS, ACCOUNT, OTHER}
APPS = {"teams", "outlook", "webex"}
REFERENCES = {"ref-1": {"text": "comment attribuer une ligne teams", "expected": [TEAMS]},
              "ref-2": {"text": "Mon compte est bloqué", "expected": json.dumps([ACCOUNT])},
              "ref-3": {"text": "impossible d'associer un numéro de téléphone dans teams", "expected": [TEAMS]}}
CODES = {ACCOUNT: ["err:0x80070005", "evt:4625"]}


def body(**changes) -> dict:
    data = {"client": "clienta", "by": "Yassine", "by_id": "u-1", "fiche_id": TEAMS, "source_sha256": CURRENT[TEAMS],
            "seen": "", "status": "validated", "intent": "ATTRIBUER_LIGNE", "primary_app": "teams",
            "aliases": [{"text": "attribuer  ligne Teams", "lang": "fr", "source": "pass_c"}], "codes": [],
            "note": ""}
    data.update(changes)
    return data


def validate(data, taxonomy=TAXONOMY, now="2026-10-13T10:00:00Z"):
    return validate_decision(data, current=CURRENT, routable=ROUTABLE, taxonomy=taxonomy, apps=APPS,
                             references=REFERENCES, candidate_codes=CODES, now=now)


def alias(text, lang="fr", source="manual", ref_id=None):
    item = {"text": text, "lang": lang, "source": source}
    if ref_id is not None:
        item["ref_id"] = ref_id
    return item


class KeysTest(unittest.TestCase):
    def test_fiche_key_is_the_catalog_row_key(self):
        self.assertEqual(fiche_key(OTHER), hashlib.sha256(OTHER.encode("utf-8")).hexdigest()[:32])
        self.assertRegex(fiche_key(TEAMS), r"^[0-9a-f]{32}$")

    def test_one_row_per_fiche_and_source_text(self):
        self.assertEqual(decision_row_key(TEAMS, "x"), hashlib.sha256(f"{TEAMS}\nx".encode("utf-8")).hexdigest()[:32])
        self.assertNotEqual(decision_row_key(TEAMS, "x"), decision_row_key(TEAMS, "y"))
        self.assertEqual((STATUSES, ALIAS_SOURCES), (("validated", "rejected"),
                                                     ("pass_a", "pass_c", "reference", "manual")))


class RowTest(unittest.TestCase):
    def decision(self, **changes):
        data = dict(fiche_id=TEAMS, source_sha256=CURRENT[TEAMS], status="validated", intent="ATTRIBUER_LIGNE",
                    intent_fingerprint="f" * 16, taxonomy_version=3, primary_app="teams",
                    aliases=[{"text": "attribuer ligne teams", "lang": "fr", "source": "pass_c", "ref_id": ""}],
                    codes=["err:0x80070005"], note="ok", decided_by="Yassine", decided_by_id="u-1",
                    decided_at="2026-10-13T10:00:00Z", revision=2, run_id="r1")
        data.update(changes)
        return ReviewDecision(**data)

    def test_to_row_never_holds_none(self):
        empty = ReviewDecision(fiche_id=TEAMS, source_sha256="x", status="rejected")
        row = empty.to_row("clienta")
        self.assertNotIn(None, row.values())
        self.assertEqual((row["intent"], row["primary_app"], row["aliases"], row["codes"]), ("", "", "[]", "[]"))
        self.assertEqual((row["taxonomy_version"], row["revision"]), (0, 0))
        self.assertIsInstance(row["revision"], int)
        self.assertEqual((row["PartitionKey"], row["RowKey"]), ("clienta", decision_row_key(TEAMS, "x")))

    def test_from_row_round_trip(self):
        original = self.decision()
        row = original.to_row("clienta")
        self.assertEqual(json.loads(row["aliases"]), original.aliases)
        back = ReviewDecision.from_row({**row, "Timestamp": "2026-10-13", "etag": "W/1"})
        self.assertEqual(back.to_dict(), original.to_dict())
        none = ReviewDecision.from_row(self.decision(intent=None, primary_app=None).to_row("clienta"))
        self.assertEqual((none.intent, none.primary_app), (None, None))

    def test_a_merge_dropping_none_still_overwrites_a_redecision(self):
        """The Azure Tables SDK drops None properties: a MERGE must still clear the previous intent."""
        stored = self.decision().to_row("clienta")
        redecided = self.decision(status="rejected", intent=None, primary_app=None, aliases=[], codes=[], revision=3)
        merged = {**stored, **{k: v for k, v in redecided.to_row("clienta").items() if v is not None}}
        back = ReviewDecision.from_row(merged)
        self.assertEqual((back.status, back.intent, back.primary_app, back.aliases, back.codes, back.revision),
                         ("rejected", None, None, [], [], 3))

    def test_from_dict_is_tolerant(self):
        decision = ReviewDecision.from_dict({"fiche_id": TEAMS, "status": "validated", "unknown": 1, "revision": "4",
                                             "aliases": [{"text": "b c", "lang": "fr"}, {"text": "a b", "lang": "fr"}],
                                             "codes": ["evt:2", "err:1"], "intent": "", "note": None})
        self.assertEqual((decision.source_sha256, decision.revision, decision.intent, decision.note), ("", 4, None, ""))
        self.assertEqual([a["text"] for a in decision.aliases], ["a b", "b c"])
        self.assertEqual(decision.aliases[0], {"text": "a b", "lang": "fr", "source": "", "ref_id": ""})
        self.assertEqual(decision.codes, ["err:1", "evt:2"])
        self.assertEqual(ReviewDecision.from_row({"aliases": "not json", "codes": "{}"}).aliases, [])


class ValidateTest(unittest.TestCase):
    def test_a_validated_decision(self):
        decision = validate(body(aliases=[alias("attribuer  ligne Teams", source="pass_c"),
                                          alias("attribuer une ligne TEAMS"),          # the same for L1: dropped
                                          alias("assign a Teams line", lang="en", source="pass_a"),
                                          alias("comment attribuer une ligne teams", source="reference",
                                                ref_id="ref-1")],
                                 note="  vu  "))
        self.assertEqual(decision.aliases, [
            {"text": "assign a Teams line", "lang": "en", "source": "pass_a", "ref_id": ""},
            {"text": "attribuer ligne Teams", "lang": "fr", "source": "pass_c", "ref_id": ""}])
        self.assertEqual((decision.status, decision.intent, decision.primary_app), ("validated", "ATTRIBUER_LIGNE", "teams"))
        self.assertEqual(decision.intent_fingerprint, TAXONOMY.fingerprint("ATTRIBUER_LIGNE"))
        self.assertEqual(decision.taxonomy_version, TAXONOMY.version)
        self.assertEqual((decision.decided_by, decision.decided_by_id, decision.decided_at, decision.note),
                         ("Yassine", "u-1", "2026-10-13T10:00:00Z", "vu"))
        self.assertEqual((decision.source_sha256, decision.revision, decision.run_id), (CURRENT[TEAMS], 0, ""))

    def test_a_reference_alias_keeps_its_question(self):
        decision = validate(body(aliases=[alias("comment attribuer une ligne teams", source="reference", ref_id="ref-1"),
                                          alias("lier un numéro teams", ref_id="ref-1")]))
        self.assertEqual([(a["source"], a["ref_id"]) for a in decision.aliases], [("reference", "ref-1"), ("manual", "")])

    def test_codes_and_empty_values(self):
        decision = validate(body(fiche_id=ACCOUNT, source_sha256=CURRENT[ACCOUNT], intent="", primary_app="",
                                 aliases=[], codes=["evt:4625", "err:0x80070005", "evt:4625"]))
        self.assertEqual((decision.intent, decision.primary_app, decision.intent_fingerprint, decision.taxonomy_version),
                         (None, None, "", 0))
        self.assertEqual(decision.codes, ["err:0x80070005", "evt:4625"])

    def test_no_taxonomy(self):
        self.assertIsNone(validate(body(intent=""), taxonomy=None).intent)
        with self.assertRaisesRegex(ValueError, "aucune taxonomie n'est figée"):
            validate(body(), taxonomy=None)

    def test_a_rejected_decision_stores_nothing_to_route(self):
        decision = validate(body(status="rejected", codes=["err:0x80070005"], note="fiche obsolète"))
        self.assertEqual((decision.intent, decision.primary_app, decision.aliases, decision.codes, decision.intent_fingerprint),
                         (None, None, [], [], ""))
        self.assertEqual(decision.note, "fiche obsolète")

    def test_unknown_or_changed_fiche(self):
        with self.assertRaises(UnknownFicheError):
            validate(body(fiche_id="KB9999 - absente"))
        with self.assertRaises(UnknownFicheError):
            validate_decision(body(), current={}, routable=ROUTABLE, taxonomy=TAXONOMY, apps=APPS,
                              references=REFERENCES, candidate_codes=CODES, now="t")
        self.assertTrue(issubclass(UnknownFicheError, LookupError))
        with self.assertRaisesRegex(ChangedError, "a changé"):
            validate(body(source_sha256="d" * 64))
        self.assertTrue(issubclass(ChangedError, ValueError))

    def assertRefused(self, data, message):
        with self.assertRaises(ValueError) as caught:
            validate(data)
        self.assertNotIsInstance(caught.exception, (ChangedError, UnknownFicheError))
        self.assertIn(message, str(caught.exception))

    def test_refusals_are_french(self):
        self.assertRefused("x", "un objet JSON est attendu")
        self.assertRefused(body(status="proposed"), "statut « proposed » inconnu")
        self.assertRefused(body(by="  "), "auteur de la décision manquant")
        self.assertRefused(body(by="x" * 121), "auteur trop long")
        self.assertRefused(body(by_id="x" * 65), "identifiant d'auteur trop long")
        self.assertRefused(body(note="x" * 501), "note trop longue")
        self.assertRefused(body(note=3), "champ « note » : texte attendu")
        self.assertRefused(body(intent="TRANSFERT_APPELS"), "intention « TRANSFERT_APPELS » absente de la taxonomie figée")
        self.assertRefused(body(primary_app="harmony"), "application « harmony » absente du vocabulaire")
        self.assertRefused(body(codes=["err:0x80070005"]), "code « err:0x80070005 » : pas un identifiant propre")
        self.assertRefused(body(fiche_id=ACCOUNT, source_sha256=CURRENT[ACCOUNT], codes=["err:0x1"]), "code « err:0x1 »")
        self.assertRefused(body(codes=["e"] * 21), "21 codes : 20 au plus")
        self.assertRefused(body(codes="err:1"), "codes : une liste de textes est attendue")

    def test_alias_refusals(self):
        self.assertRefused(body(aliases="a b"), "alias : une liste est attendue")
        self.assertRefused(body(aliases=[alias(f"alias numéro{i} test") for i in range(MAX_ALIASES + 1)]),
                           "41 alias : 40 au plus")
        self.assertRefused(body(aliases=["attribuer ligne"]), "alias n° 1 : illisible")
        self.assertRefused(body(aliases=[alias("  ")]), "alias n° 1 : texte vide")
        self.assertRefused(body(aliases=[alias("teams")]), "alias « teams » : de 2 à 12 mots")
        self.assertRefused(body(aliases=[alias(" ".join(["mot"] * 13))]), "de 2 à 12 mots")
        self.assertRefused(body(aliases=[alias("attribuer " + "x" * 120)]), "120 caractères au plus")
        self.assertRefused(body(aliases=[alias("écrire à jean.dupont@example.com")]), "alias n° 1 : une adresse e-mail")
        self.assertRefused(body(aliases=[alias("appeler le 06 12 34 56 78")]), "numéro de téléphone")
        self.assertRefused(body(aliases=[alias("imprimante étage 3")]),
                           "alias « imprimante étage 3 » : le routage ignore « 3 » : écrire l'alias sans, ou avec le mot entier")
        self.assertRefused(body(aliases=[alias("comment bloquer")]), "alias « comment bloquer » : moins de 2 mots utiles au routage")
        self.assertRefused(body(aliases=[alias("assign teams line", lang="de")]), "langue « de » inconnue (fr ou en)")
        self.assertRefused(body(aliases=[alias("assign teams line", source="model")]), "origine « model » inconnue")
        self.assertRefused(body(aliases=[alias("assign teams line", source="reference", ref_id="ref-9")]),
                           "question de référence « ref-9 » inconnue ou qui n'attend pas cette fiche")
        self.assertRefused(body(aliases=[alias("compte bloqué maintenant", source="reference", ref_id="ref-2")]),
                           "« ref-2 »")
        self.assertRefused(body(aliases=[alias("assign teams line", source="reference")]), "question de référence manquante")

    def test_check_seen(self):
        check_seen({"seen": ""}, None)
        stored = validate(body())
        check_seen({"seen": stored.decided_at}, stored)
        with self.assertRaisesRegex(ChangedError, "la décision de cette fiche a changé"):
            check_seen({"seen": ""}, stored)
        with self.assertRaises(ChangedError):
            check_seen({"seen": "2026-10-12T09:00:00Z"}, None)


def made(fiche_id, source, status="validated", at="2026-10-13T10:00:00Z", revision=0, aliases=()):
    return ReviewDecision(fiche_id=fiche_id, source_sha256=source, status=status, decided_at=at, revision=revision,
                          aliases=[{"text": a, "lang": "fr", "source": "manual", "ref_id": ""} for a in aliases])


class StatusTest(unittest.TestCase):
    def test_proposed_validated_rejected(self):
        self.assertEqual(current_status(TEAMS, CURRENT[TEAMS], []), ("proposed", None, None))
        mine = made(TEAMS, CURRENT[TEAMS])
        self.assertEqual(current_status(TEAMS, CURRENT[TEAMS], [mine]), ("validated", mine, None))
        rejected = made(TEAMS, CURRENT[TEAMS], status="rejected")
        self.assertEqual(current_status(TEAMS, CURRENT[TEAMS], [rejected])[0], "rejected")

    def test_stale_and_previous(self):
        older = made(TEAMS, "old-1", at="2026-10-01T09:00:00Z", revision=5)
        newer = made(TEAMS, "old-2", status="rejected", at="2026-10-05T09:00:00Z")
        self.assertEqual(current_status(TEAMS, CURRENT[TEAMS], [older, newer]), ("stale", None, newer))
        mine = made(TEAMS, CURRENT[TEAMS])
        self.assertEqual(current_status(TEAMS, CURRENT[TEAMS], [newer, mine, older]), ("validated", mine, newer))
        same_time = made(TEAMS, "old-3", at="2026-10-05T09:00:00Z", revision=1)
        self.assertIs(current_status(TEAMS, CURRENT[TEAMS], [newer, same_time])[2], same_time)

    def test_the_highest_revision_is_current(self):
        first = made(TEAMS, CURRENT[TEAMS], revision=0, at="2026-10-13T11:00:00Z")
        second = made(TEAMS, CURRENT[TEAMS], status="rejected", revision=1, at="2026-10-13T10:00:00Z")
        self.assertEqual(current_status(TEAMS, CURRENT[TEAMS], [first, second]), ("rejected", second, None))

    def test_other_fiches_decisions_are_ignored(self):
        self.assertEqual(current_status(TEAMS, CURRENT[TEAMS], [made(ACCOUNT, CURRENT[TEAMS])]), ("proposed", None, None))

    def test_current_decisions_and_counts(self):
        decisions = [made(TEAMS, CURRENT[TEAMS]), made(ACCOUNT, "old"), made(OTHER, CURRENT[OTHER], status="rejected"),
                     made("KB0052 - remplacée", "x")]   # not routable: ignored
        current = dict(CURRENT, **{"KB0052 - remplacée": "x"})
        routable = sorted(ROUTABLE | {"KB0400 - jamais revue"})
        self.assertEqual(sorted(current_decisions(decisions, current, routable)), [TEAMS, OTHER])
        self.assertEqual(status_counts(decisions, current, routable),
                         {"proposed": 1, "validated": 1, "rejected": 1, "stale": 1})
        statuses = fiche_statuses(decisions, current, routable)
        self.assertEqual(list(statuses), sorted(routable))
        # a fiche with no current source hash has no current decision
        self.assertEqual(status_counts([made(TEAMS, CURRENT[TEAMS])], {}, [TEAMS]),
                         {"proposed": 0, "validated": 0, "rejected": 0, "stale": 1})


class SeenReferencesTest(unittest.TestCase):
    def test_a_reference_an_alias_was_promoted_from(self):
        decision = made(TEAMS, CURRENT[TEAMS])
        decision.aliases = [{"text": "attribuer ligne teams bureau", "lang": "fr", "source": "reference",
                             "ref_id": "ref-1"}]
        self.assertEqual(seen_reference_ids({TEAMS: decision}, REFERENCES), ["ref-1"])

    def test_a_reworded_alias_whose_tokens_the_reference_holds(self):
        decision = made(TEAMS, CURRENT[TEAMS], aliases=["associer numéro téléphone teams"])
        self.assertEqual(seen_reference_ids({TEAMS: decision}, REFERENCES), ["ref-3"])
        both = made(TEAMS, CURRENT[TEAMS], aliases=["associer numéro téléphone teams", "attribuer ligne Teams"])
        self.assertEqual(seen_reference_ids({TEAMS: both}, REFERENCES), ["ref-1", "ref-3"])

    def test_only_aliases_of_an_expected_fiche_count(self):
        decision = made(ACCOUNT, CURRENT[ACCOUNT], aliases=["attribuer ligne teams"])
        self.assertEqual(seen_reference_ids({ACCOUNT: decision}, REFERENCES), [])
        account = made(ACCOUNT, CURRENT[ACCOUNT], aliases=["compte bloqué"])
        self.assertEqual(seen_reference_ids({ACCOUNT: account}, REFERENCES), ["ref-2"])   # expected as JSON text

    def test_rejected_or_unroutable_aliases_never_make_a_reference_seen(self):
        rejected = made(TEAMS, CURRENT[TEAMS], status="rejected", aliases=["attribuer ligne teams"])
        self.assertEqual(seen_reference_ids({TEAMS: rejected}, REFERENCES), [])
        short = made(TEAMS, CURRENT[TEAMS], aliases=["teams"])
        self.assertEqual(seen_reference_ids({TEAMS: short}, REFERENCES), [])
        self.assertEqual(seen_reference_ids({}, REFERENCES), [])


if __name__ == "__main__":
    unittest.main()
