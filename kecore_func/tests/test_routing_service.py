"""kecore_func/routing_service.py: passes B and C in a routing run, the review of the routing proposals,
the routing tables, their activation and the sticky kill switch -- everything in memory.

Run from the repository root:  python -m unittest discover -s kecore_func/tests
"""

import hashlib
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for path in (ROOT, ROOT / "kecore_func", Path(__file__).resolve().parent):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import enrichment_service as enrichment_svc  # noqa: E402
import kecore_pipeline as pipeline  # noqa: E402
import kefind_service as finder  # noqa: E402
import routing_service as svc  # noqa: E402
from kecore.llm import LLMError, RecordingLLM  # noqa: E402
from kefind.enrich import Enrichment, content_sha256  # noqa: E402
from kefind.enrich import stats as enrich_stats  # noqa: E402
from kefind.review import ReviewDecision, decision_row_key, fiche_key  # noqa: E402
from kefind.routing import CODE_VERSION, RoutingTables  # noqa: E402
from kefind.taxonomy import Taxonomy  # noqa: E402
from kefind.tests.schema_llm import SchemaLLM  # noqa: E402
from kefind.tests.test_funnel import make  # noqa: E402
from test_tickets_service import MemoryStorage, MemoryTable, filler  # noqa: E402

CLIENT = "client-s"
CONTAINER = "kecore-client-s"
RUN, RUN2 = "20261014T080000Z-aaaaaa", "20261015T080000Z-bbbbbb"
NOW = "2026-10-14T12:00:00+00:00"
LATER = "2026-10-14T13:00:00+00:00"

TEAMS = "KB0233 -  Associate a phone line"      # two spaces: an id is never collapsed
WEBEX = "KB0240 – Webex phone line"             # an en dash
ACCOUNT = "KB0120- LOCKED ACCOUNT"
OLD_VPN = "KB0300 - Ancien VPN"                 # replaced by KB0301: never routable
NEW_VPN = "KB0301 - Nouveau VPN"
PRINTER = "KB0900 - imprimante bourrage papier"
REF = "ref-" + "c" * 24
REF2 = "ref-" + "d" * 24
ROUTABLE = 14                                   # 15 ranked fiches, the superseded one left out

TEXTS = {
    TEAMS: ("Open the Teams admin center. Assign a phone line to the user.", ["app:teams"]),
    WEBEX: ("Open Control Hub. Assign a Webex phone line.", ["app:webex"]),
    ACCOUNT: ("The user account is locked. Unlock the account in the console. Error 0x80070005 may show.",
              ["err:0x80070005"]),
    OLD_VPN: ("Cette procédure est remplacée par KB0301. Ouvrez le client VPN.", []),
    NEW_VPN: ("Ouvrez le nouveau client VPN. Connectez-vous avec votre compte.", []),
}

PASS_A = {
    TEAMS: {"canonical_intent": "ATTRIBUTION_LIGNE_TELEPHONIQUE", "intent_label_fr": "attribuer une ligne",
            "primary_app": "teams", "supported_apps": ["teams"],
            "semantic_aliases_fr": ["attribuer une ligne teams", "ligne 3 teams pour un compte"],
            "semantic_aliases_en": ["assign teams phone number"]},
    WEBEX: {"canonical_intent": "ATTRIBUTION_LIGNE_WEBEX", "intent_label_fr": "attribuer une ligne webex",
            "primary_app": "webex", "supported_apps": ["webex"], "apps_inferred": ["webex"], "app_inferred": True,
            "semantic_aliases_fr": ["attribuer une ligne webex"]},
    ACCOUNT: {"canonical_intent": "DEVERROUILLAGE_COMPTE", "intent_label_fr": "déverrouiller un compte",
              "semantic_aliases_fr": ["débloquer un compte"]},
    OLD_VPN: {"canonical_intent": "CONNEXION_VPN"},
    NEW_VPN: {"canonical_intent": "CONNEXION_VPN", "semantic_aliases_fr": ["connexion vpn impossible"]},
}
FILLER_A = {"canonical_intent": "MATERIEL_PERIPHERIQUE", "intent_label_fr": "matériel"}

TAXONOMY_ANSWER = {"intents": [
    {"id": "ATTRIBUER_LIGNE", "label_fr": "Attribuer une ligne téléphonique", "description_fr": "Donner un numéro.",
     "members": ["I001", "I002"]},
    {"id": "DEVERROUILLER_COMPTE", "label_fr": "Déverrouiller un compte", "description_fr": "", "members": ["I004"]}]}
FROZEN = [{"id": "ATTRIBUER_LIGNE", "label_fr": "Attribuer une ligne téléphonique", "description_fr": "Donner un numéro."},
          {"id": "DEVERROUILLER_COMPTE", "label_fr": "Déverrouiller un compte", "description_fr": ""}]
NO_ASSIGNMENT = {"intent": "", "core_aliases_fr": [], "core_aliases_en": []}
ASSIGN = {
    "Associate a phone line": {"intent": "ATTRIBUER_LIGNE", "core_aliases_fr": ["attribuer ligne teams"],
                               "core_aliases_en": []},
    "Webex phone line": {"intent": "ATTRIBUER_LIGNE", "core_aliases_fr": ["attribuer ligne webex"], "core_aliases_en": []},
    "LOCKED ACCOUNT": {"intent": "DEVERROUILLER_COMPTE", "core_aliases_fr": ["débloquer compte"], "core_aliases_en": []},
}


class Storage(MemoryStorage):
    """With the conditional write BlobStorage has (If-None-Match: *)."""

    def write_if_absent(self, container, name, data):
        if (container, name) in self.blobs:
            return False
        self.blobs[(container, name)] = data
        return True


class SdkTable(MemoryTable):
    """Like the Azure Tables SDK: a None property is not sent, so a MERGE keeps the value stored before."""

    def upsert(self, entity):
        super().upsert({k: v for k, v in entity.items() if v is not None})

    merge = upsert


def fiches(texts=None):
    texts = {**TEXTS, **(texts or {})}
    return filler() + [make(fiche_id, text, entities=entities, client=CLIENT) for fiche_id, (text, entities) in texts.items()]


def _jsonl(items) -> bytes:
    return "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in items).encode("utf-8")


def write_run(storage, run_id=RUN, items=None, publish=True, pass_a=True, errors=(), sources=None):
    """A kecore run's folder as the pipeline leaves it: fiches.jsonl (extracted texts), the map, pass A."""
    items = fiches() if items is None else items
    sources = sources or {}
    storage.write(CONTAINER, f"runs/{run_id}/fiches.jsonl", _jsonl(
        {"fiche_id": f.fiche_id, "client": CLIENT, "title": f.title, "text": sources.get(f.fiche_id, "SOURCE " + f.text),
         "source": ""} for f in items))
    storage.write(CONTAINER, f"runs/{run_id}/fiches.decomposed.jsonl", _jsonl(f.to_dict() for f in items))
    if publish:
        storage.write(CONTAINER, "latest.json", json.dumps({"run_id": run_id}).encode("utf-8"))
    if pass_a:
        kbmap = finder.load_map(storage, CLIENT, run_id, with_semantic=False)
        proposals = []
        for i, fiche_id in enumerate(kbmap.ranked):
            base = {"fiche_id": fiche_id, "label": kbmap.label(fiche_id),
                    "content_sha256": content_sha256(kbmap.fiches[fiche_id])}
            item = Enrichment(**base, error="LLMError: HTTP 429") if fiche_id in errors else \
                Enrichment(**base, **PASS_A.get(fiche_id, FILLER_A))
            proposals.append(item)
            storage.write(CONTAINER, f"runs/{run_id}/enrichment/{i:05d}.json", enrichment_svc._bytes(item.to_dict()))
        storage.write(CONTAINER, f"runs/{run_id}/enrichment/summary.json", enrichment_svc._bytes(enrich_stats(proposals)))
    return storage


def blob(storage, name):
    data = storage.read(CONTAINER, name)
    return None if data is None else json.loads(data.decode("utf-8"))


def kbmap(storage, run_id=RUN):
    return finder.load_map(storage, CLIENT, run_id, with_semantic=False)


def payload(build_id="b1", run_id=None, **extra):
    return {"client": CLIENT, "mode": "record", "llm_parallelism": 2, "batch_size": 4, "build_id": build_id,
            "measure": False, "run_id": run_id, "kind": "manual", **extra}


def taxonomy_llm(answer=None):
    return SchemaLLM({"intent_taxonomy": answer if answer is not None else TAXONOMY_ANSWER})


def assign_llm(answers=None):
    answers = ASSIGN if answers is None else answers
    return SchemaLLM({"fiche_intent_assignment": lambda system, user, schema: answers.get(SchemaLLM.key_of(user),
                                                                                         NO_ASSIGNMENT)})


def routing_run(storage, table, build_id, now=NOW, run_id=None, b_llm=None, c_llm=None):
    """What the routing_run orchestration does, step by step (function_app._routing_steps)."""
    p = payload(build_id, run_id)
    prepared = svc.prepare(storage, p, now)
    if prepared.get("error"):
        svc.finish(storage, p, {"state": "failed", "error": prepared["error"], "run_id": prepared["run_id"]}, now)
        return prepared
    p["run_id"] = prepared["run_id"]
    out = dict(prepared)
    if prepared["needs_b"]:
        out["taxonomy_summary"] = svc.propose_taxonomy(storage, p, b_llm or taxonomy_llm())
    if prepared["needs_c"]:
        llm = c_llm or assign_llm()
        out["parts"] = [svc.assign(storage, p, start, end, llm)
                        for start, end in pipeline.batches(prepared["fiches"], p["batch_size"])]
        out["assign_summary"] = svc.assign_summary(storage, p)
    out["built"] = svc.build(storage, table.list(CLIENT), p, now)
    svc.finish(storage, p, {"state": "completed", "run_id": p["run_id"]}, now)
    return out


def freeze(storage, table, intents=FROZEN, by="Yassine", now=NOW, **extra):
    body = {"client": CLIENT, "by": by, "source_build": "b1", "intents": [dict(i) for i in intents], **extra}
    return svc.freeze_taxonomy(storage, table.list(CLIENT), body, now, kbmap=kbmap(storage))


def body(storage, fiche_id, status="validated", intent="", app="", aliases=(), codes=(), seen="", by="Yassine",
         run_id=RUN, note="", references=()):
    current = svc.source_hashes(storage, CLIENT, run_id)
    aliases = [{"text": t, "lang": "fr", "source": "manual", "ref_id": ""} for t in aliases]
    aliases += [{"text": t, "lang": "fr", "source": "reference", "ref_id": r} for r, t in references]
    return {"client": CLIENT, "by": by, "fiche_id": fiche_id, "source_sha256": current[fiche_id], "seen": seen,
            "status": status, "intent": intent, "primary_app": app, "aliases": aliases, "codes": list(codes),
            "note": note}


def decide(storage, table, fiche_id, refs=(), now=NOW, run_id=RUN, **kwargs):
    return svc.decide(table, storage, CLIENT, body(storage, fiche_id, run_id=run_id, **kwargs),
                      kbmap(storage, run_id), list(refs), now)


def ref_row(ref_id, text, expected):
    return {"PartitionKey": CLIENT, "RowKey": ref_id, "text": text, "expected": json.dumps(expected)}


def write_gate(storage, build_id, status="passed"):
    summary = blob(storage, f"routing/builds/{build_id}/summary.json")
    gate = {"sb_id": f"{build_id}-sb", "status": status, "passed": status == "passed", "build_id": build_id,
            "tables_sha256": summary["tables_sha256"], "content_sha256": summary["content_sha256"],
            "kb_run_id": summary["run_id"], "reasons": [] if status == "passed" else ["only 3 comparable questions"],
            "wrong_exact": {"k": 0, "n": 3, "ids": []}, "route_first": {"k": 3, "n": 3}, "base_first": {"k": 2, "n": 3},
            "compared": 3, "excluded": []}
    storage.write(CONTAINER, svc.gate_path(summary["content_sha256"], summary["run_id"]),
                  json.dumps(gate).encode("utf-8"))
    return gate


def activate(storage, build_id, by="Yassine", now=NOW, **extra):
    request = svc.validate_activate_request({"client": CLIENT, "build_id": build_id, "by": by, **extra}, [CLIENT])
    return svc.activate(storage, request, now)


class SourceHashesTest(unittest.TestCase):
    def test_the_review_key_is_the_extracted_text_before_boilerplate_removal(self):
        storage = write_run(Storage())
        hashes = svc.source_hashes(storage, CLIENT, RUN)
        text = TEXTS[TEAMS][0]
        self.assertEqual(hashes[TEAMS], hashlib.sha256(("SOURCE " + text).encode("utf-8")).hexdigest())
        self.assertNotEqual(hashes[TEAMS], content_sha256(kbmap(storage).fiches[TEAMS]))
        self.assertEqual(len(hashes), 15)

    def test_the_first_fiche_of_an_id_wins_and_a_run_without_fiches_jsonl_is_refused(self):
        storage = Storage()
        storage.write(CONTAINER, f"runs/{RUN}/fiches.jsonl", _jsonl([{"fiche_id": "A", "text": "one"},
                                                                     {"fiche_id": "A", "text": "two"}]))
        self.assertEqual(svc.source_hashes(storage, CLIENT, RUN), {"A": hashlib.sha256(b"one").hexdigest()})
        with self.assertRaises(FileNotFoundError):
            svc.source_hashes(storage, CLIENT, RUN2)


class RequestsTest(unittest.TestCase):
    def test_a_routing_run_request_has_safe_defaults(self):
        self.assertEqual(svc.validate_routing_request({"client": CLIENT}, [CLIENT], "20261014T120000Z-abc123"), {
            "client": CLIENT, "mode": "record", "llm_parallelism": 2, "batch_size": 10, "build_id": "20261014T120000Z-abc123",
            "measure": True, "run_id": None, "kind": "manual"})

    def test_bad_routing_run_requests_are_refused(self):
        for request in ({}, {"client": "other"}, {"client": CLIENT, "mode": "refresh"},
                        {"client": CLIENT, "llm_parallelism": 0}, {"client": CLIENT, "llm_parallelism": 9},
                        {"client": CLIENT, "llm_parallelism": True}, {"client": CLIENT, "batch_size": 51},
                        {"client": CLIENT, "measure": "false"}, ["client-s"]):
            with self.assertRaises(ValueError, msg=str(request)):
                svc.validate_routing_request(request, [CLIENT], "b1")
        with self.assertRaises(ValueError):
            svc.validate_routing_request({"client": CLIENT}, [CLIENT], "../b1")

    def test_activation_needs_a_person_and_an_override_needs_a_reason(self):
        ok = svc.validate_activate_request({"client": CLIENT, "build_id": "b1", "by": " Yassine  B "}, [CLIENT])
        self.assertEqual(ok, {"client": CLIENT, "build_id": "b1", "by": "Yassine B", "accept_insufficient": False,
                              "reason": ""})
        for request in ({"client": CLIENT, "build_id": "b1"}, {"client": CLIENT, "build_id": "b/1", "by": "Y"},
                        {"client": CLIENT, "build_id": "b1", "by": "Y", "accept_insufficient": True},
                        {"client": CLIENT, "build_id": "b1", "by": "Y", "accept_insufficient": "yes", "reason": "r"},
                        {"client": CLIENT, "build_id": "b1", "by": "Y" * 121},
                        {"client": "other", "build_id": "b1", "by": "Y"}):
            with self.assertRaises(ValueError, msg=str(request)):
                svc.validate_activate_request(request, [CLIENT])
        override = svc.validate_activate_request({"client": CLIENT, "build_id": "b1", "by": "Y",
                                                  "accept_insufficient": True, "reason": " 2 questions  only "}, [CLIENT])
        self.assertEqual(override["reason"], "2 questions only")

    def test_carry_and_deactivation_requests(self):
        key = "a" * 32
        self.assertEqual(svc.validate_carry_request({"client": CLIENT, "by": "Y", "keys": [key, key]}, [CLIENT]),
                         {"client": CLIENT, "by": "Y", "by_id": "", "keys": [key]})
        for request in ({"client": CLIENT, "by": "Y", "keys": []}, {"client": CLIENT, "by": "Y", "keys": ["A" * 32]},
                        {"client": CLIENT, "keys": [key]}, {"client": CLIENT, "by": "Y", "keys": key}):
            with self.assertRaises(ValueError, msg=str(request)):
                svc.validate_carry_request(request, [CLIENT])
        self.assertEqual(svc.validate_deactivate_request({"client": CLIENT, "by": "Y"}, [CLIENT]),
                         {"client": CLIENT, "by": "Y"})
        with self.assertRaises(ValueError):
            svc.validate_deactivate_request({"client": CLIENT, "by": ""}, [CLIENT])


class LeaseTest(unittest.TestCase):
    def setUp(self):
        self.storage = write_run(Storage())

    def test_one_routing_run_at_a_time_and_only_its_owner_frees_it(self):
        self.assertNotIn("error", svc.prepare(self.storage, payload("b1"), NOW))
        refused = svc.prepare(self.storage, payload("b2", kind="kecore"), LATER)
        self.assertEqual(refused["error"], f"a routing run is in progress (b1, since {NOW})")
        self.assertEqual(blob(self.storage, svc.LEASE), {"build_id": "b1", "kind": "manual", "started_at": NOW})
        self.assertEqual(svc.lease(self.storage, CLIENT, LATER)["build_id"], "b1")
        svc.build(self.storage, [], payload("b1", RUN), NOW)                       # b1 is building
        svc.finish(self.storage, payload("b2"), {"state": "failed", "error": refused["error"]}, LATER)
        self.assertEqual(blob(self.storage, svc.LEASE)["build_id"], "b1")          # another run's lease is never freed
        self.assertEqual(blob(self.storage, svc.LATEST_BUILD)["build_id"], "b1")   # nor its state hidden
        self.assertEqual(blob(self.storage, "routing/builds/b2/status.json"),       # the refusal is still said
                         {"state": "failed", "error": refused["error"], "finished_at": LATER})
        svc.finish(self.storage, payload("b1"), {"state": "completed", "run_id": RUN}, LATER)
        self.assertIsNone(blob(self.storage, svc.LEASE))
        self.assertEqual(blob(self.storage, svc.LATEST_BUILD),
                         {"build_id": "b1", "run_id": RUN, "state": "completed", "kind": "manual"})
        # an activity retried by Durable after the lease was freed still records its own outcome
        svc.finish(self.storage, payload("b1"), {"state": "failed", "error": "x", "run_id": RUN}, LATER)
        self.assertEqual(blob(self.storage, svc.LATEST_BUILD)["state"], "failed")
        self.assertNotIn("error", svc.prepare(self.storage, payload("b3"), LATER))

    def test_a_retried_prepare_keeps_its_own_lease(self):
        svc.prepare(self.storage, payload("b1"), NOW)
        self.assertNotIn("error", svc.prepare(self.storage, payload("b1"), LATER))

    def test_a_dead_runs_lease_is_replaced_after_four_hours_without_a_heartbeat(self):
        svc.prepare(self.storage, payload("b1"), NOW)
        self.assertIn("error", svc.prepare(self.storage, payload("b2"), "2026-10-14T15:59:00+00:00"))
        self.assertIsNone(svc.lease(self.storage, CLIENT, "2026-10-14T16:01:00+00:00"))
        self.assertNotIn("error", svc.prepare(self.storage, payload("b2"), "2026-10-14T16:01:00+00:00"))
        self.assertEqual(blob(self.storage, svc.LEASE)["build_id"], "b2")

    def test_a_live_runs_heartbeat_keeps_its_lease(self):
        svc.prepare(self.storage, payload("b1"), NOW)
        svc.heartbeat(self.storage, CLIENT, "b1", "2026-10-14T15:00:00+00:00")
        svc.heartbeat(self.storage, CLIENT, "b2", "2026-10-14T18:00:00+00:00")     # another run's: ignored
        self.assertIn("error", svc.prepare(self.storage, payload("b2"), "2026-10-14T18:59:00+00:00"))
        self.assertEqual(blob(self.storage, svc.LEASE)["build_id"], "b1")
        self.assertNotIn("error", svc.prepare(self.storage, payload("b2"), "2026-10-14T19:01:00+00:00"))

    def test_a_storage_without_a_conditional_write_still_takes_one_lease(self):
        storage = write_run(MemoryStorage())
        self.assertNotIn("error", svc.prepare(storage, payload("b1"), NOW))
        self.assertIn("error", svc.prepare(storage, payload("b2"), NOW))


class PrepareTest(unittest.TestCase):
    def setUp(self):
        self.storage, self.table = write_run(Storage()), MemoryTable()

    def test_no_map_no_routing_run_and_the_failure_is_recorded(self):
        empty = Storage()
        prepared = svc.prepare(empty, payload("b1"), NOW)
        self.assertEqual(prepared["error"], "no KB map for this client yet: start a kecore run first")
        svc.finish(empty, payload("b1"), {"state": "failed", "error": prepared["error"]}, NOW)
        self.assertEqual(blob(empty, "routing/latest-build.json"),
                         {"build_id": "b1", "run_id": "", "state": "failed", "kind": "manual"})
        self.assertEqual(blob(empty, "routing/builds/b1/status.json"),
                         {"state": "failed", "error": prepared["error"], "finished_at": NOW})
        self.assertIsNone(blob(empty, svc.LEASE))
        self.assertEqual(svc.prepare(empty, payload("b2", run_id=RUN), NOW)["error"], f"no KB map for run {RUN}")

    def test_a_run_without_pass_a_has_no_routing_run(self):
        storage = write_run(Storage(), pass_a=False)
        self.assertIn("has no pass-A summary", svc.prepare(storage, payload("b1"), NOW)["error"])

    def test_pass_b_once_per_run_while_no_taxonomy_is_frozen_and_never_on_a_partial_pass_a(self):
        prepared = svc.prepare(self.storage, payload("b1"), NOW)
        self.assertEqual({k: prepared[k] for k in ("run_id", "fiches", "taxonomy", "needs_b", "needs_c")},
                         {"run_id": RUN, "fiches": ROUTABLE, "taxonomy": None, "needs_b": True, "needs_c": False})
        svc.propose_taxonomy(self.storage, payload("b1", RUN), taxonomy_llm())
        svc.finish(self.storage, payload("b1"), {"state": "completed"}, NOW)
        self.assertFalse(svc.prepare(self.storage, payload("b2"), NOW)["needs_b"])
        svc.finish(self.storage, payload("b2"), {"state": "completed"}, NOW)
        failing = write_run(Storage(), errors=(WEBEX,))
        skipped = svc.prepare(failing, payload("b1"), NOW)
        # pass B waits for a complete pass A, the run goes on: its L0/L1 tables are still built
        self.assertNotIn("error", skipped)
        self.assertFalse(skipped["needs_b"])
        self.assertEqual(skipped["skipped_b"], "pass A has 1 errors: pass B waits for a kecore record run (only the "
                                               "missing calls are made); the L0/L1 tables are built meanwhile")
        built = svc.build(failing, [], payload("b1", RUN), NOW)
        self.assertEqual(built["build_id"], "b1")

    def test_a_failed_proposal_is_asked_again(self):
        svc.prepare(self.storage, payload("b1"), NOW)
        summary = svc.propose_taxonomy(self.storage, payload("b1", RUN), taxonomy_llm(LLMError("HTTP 429")))
        self.assertEqual(summary["error"], "HTTP 429")
        svc.finish(self.storage, payload("b1"), {"state": "completed"}, NOW)
        self.assertTrue(svc.prepare(self.storage, payload("b2"), NOW)["needs_b"])

    def test_pass_c_when_the_frozen_taxonomy_or_the_run_changed_or_had_errors(self):
        freeze(self.storage, self.table)
        prepared = svc.prepare(self.storage, payload("b1"), NOW)
        taxonomy = svc.frozen_taxonomy(self.storage, CLIENT)
        self.assertEqual((prepared["needs_b"], prepared["needs_c"]), (False, True))
        self.assertEqual(prepared["taxonomy"], {"version": 1, "sha256": taxonomy.sha256})
        self.assertEqual(blob(self.storage, "routing/builds/b1/taxonomy.json"), taxonomy.to_dict())
        svc.finish(self.storage, payload("b1"), {"state": "completed"}, NOW)
        routing_run(self.storage, self.table, "b2")
        self.assertFalse(svc.prepare(self.storage, payload("b3"), NOW)["needs_c"])         # nothing changed
        svc.finish(self.storage, payload("b3"), {"state": "completed"}, NOW)
        pointer = blob(self.storage, svc.ASSIGNMENTS)
        self.storage.write(CONTAINER, svc.ASSIGNMENTS, json.dumps({**pointer, "errors": 2}).encode())
        self.assertTrue(svc.prepare(self.storage, payload("b4"), NOW)["needs_c"])          # errors: ask them again
        svc.finish(self.storage, payload("b4"), {"state": "completed"}, NOW)
        self.storage.write(CONTAINER, svc.ASSIGNMENTS, json.dumps(pointer).encode())
        freeze(self.storage, self.table, FROZEN + [{"id": "CONNEXION_VPN", "label_fr": "Se connecter au VPN"}])
        self.assertTrue(svc.prepare(self.storage, payload("b5"), NOW)["needs_c"])          # a new taxonomy
        svc.finish(self.storage, payload("b5"), {"state": "completed"}, NOW)
        write_run(self.storage, RUN2)
        self.assertTrue(svc.prepare(self.storage, payload("b6"), NOW)["needs_c"])          # a new run

    def test_a_hand_edited_taxonomy_is_refused(self):
        freeze(self.storage, self.table)
        data = blob(self.storage, svc.TAXONOMY)
        data["intents"][0]["label_fr"] = "Autre chose"
        self.storage.write(CONTAINER, svc.TAXONOMY, json.dumps(data).encode())
        self.assertIn("does not match its sha256", svc.prepare(self.storage, payload("b1"), NOW)["error"])


class PassBTest(unittest.TestCase):
    def setUp(self):
        self.storage = write_run(Storage())
        svc.prepare(self.storage, payload("b1"), NOW)

    def test_the_proposal_covers_the_routable_fiches_and_is_named_by_its_pointer(self):
        summary = svc.propose_taxonomy(self.storage, payload("b1", RUN), taxonomy_llm())
        proposal = blob(self.storage, "routing/builds/b1/taxonomy.proposed.json")
        self.assertEqual([i["id"] for i in proposal["intents"]],
                         ["ATTRIBUER_LIGNE", "CONNEXION_VPN", "DEVERROUILLER_COMPTE", "MATERIEL_PERIPHERIQUE"])
        self.assertEqual(proposal["source"], {"intents": 5, "fiches": ROUTABLE, "enrichment_errors": 0})  # no KB0300
        self.assertNotIn(OLD_VPN, proposal["intents"][1]["fiches"])
        self.assertEqual(blob(self.storage, svc.TAXONOMY_PROPOSAL), {"build_id": "b1", "run_id": RUN, "error": None})
        self.assertEqual((summary["intents"], summary["error"], summary["llm"]), (4, None, {"calls": 0, "cached": 0}))

    def test_a_replay_rewrites_the_proposal_byte_for_byte_with_no_model_call(self):
        store = pipeline.StorageRecordStore(self.storage, CLIENT)
        recorded = RecordingLLM(taxonomy_llm(), mode="record", store=store)
        self.assertEqual(svc.propose_taxonomy(self.storage, payload("b1", RUN), recorded)["llm"],
                         {"calls": 1, "cached": 0})
        first = self.storage.read(CONTAINER, "routing/builds/b1/taxonomy.proposed.json")
        replayed = RecordingLLM(None, mode="replay", model_id="schema-llm@test", store=store)
        self.assertEqual(svc.propose_taxonomy(self.storage, payload("b1", RUN), replayed)["llm"],
                         {"calls": 0, "cached": 1})
        self.assertEqual(self.storage.read(CONTAINER, "routing/builds/b1/taxonomy.proposed.json"), first)


class PassCTest(unittest.TestCase):
    def setUp(self):
        self.storage, self.table = write_run(Storage()), MemoryTable()
        freeze(self.storage, self.table)
        svc.prepare(self.storage, payload("b1"), NOW)
        self.p = payload("b1", RUN)

    def run_pass_c(self, llm):
        parts = [svc.assign(self.storage, self.p, start, end, llm) for start, end in pipeline.batches(ROUTABLE, 4)]
        return parts, svc.assign_summary(self.storage, self.p)

    def test_one_proposal_per_routable_fiche_indexed_by_fiche_id(self):
        parts, summary = self.run_pass_c(assign_llm())
        self.assertEqual([(p["start"], p["end"]) for p in parts], [(0, 4), (4, 8), (8, 12), (12, 14)])
        self.assertEqual((summary["fiches"], summary["errors"], summary["with_intent"]), (ROUTABLE, 0, 3))
        index = blob(self.storage, "routing/builds/b1/assign/index.json")
        self.assertEqual(len(index), ROUTABLE)
        self.assertNotIn(OLD_VPN, index)
        teams = blob(self.storage, f"routing/builds/b1/assign/{index[TEAMS]}.json")
        self.assertEqual((teams["fiche_id"], teams["intent"], teams["core_aliases_fr"]),
                         (TEAMS, "ATTRIBUER_LIGNE", ["attribuer ligne teams"]))
        taxonomy = svc.frozen_taxonomy(self.storage, CLIENT)
        self.assertEqual(blob(self.storage, svc.ASSIGNMENTS),
                         {"build_id": "b1", "run_id": RUN, "taxonomy_sha256": taxonomy.sha256, "errors": 0})
        self.assertEqual(blob(self.storage, "routing/builds/b1/assign/summary.json"), summary)

    def test_a_fiche_the_model_fails_on_stays_that_fiches_error(self):
        answers = dict(ASSIGN, **{"LOCKED ACCOUNT": LLMError("HTTP 429")})
        parts, summary = self.run_pass_c(assign_llm(answers))
        self.assertEqual((sum(p["errors"] for p in parts), summary["errors"]), (1, 1))
        self.assertEqual(blob(self.storage, svc.ASSIGNMENTS)["errors"], 1)
        svc.finish(self.storage, self.p, {"state": "completed"}, NOW)
        self.assertTrue(svc.prepare(self.storage, payload("b2"), NOW)["needs_c"])

    def test_the_summary_needs_every_file(self):
        svc.assign(self.storage, self.p, 0, 4, assign_llm())
        with self.assertRaises(FileNotFoundError):
            svc.assign_summary(self.storage, self.p)

    def test_pass_c_reads_the_taxonomy_the_build_copied_not_a_freeze_made_meanwhile(self):
        first = svc.frozen_taxonomy(self.storage, CLIENT)
        freeze(self.storage, self.table, FROZEN + [{"id": "CONNEXION_VPN", "label_fr": "Se connecter au VPN"}])
        self.run_pass_c(assign_llm())
        index = blob(self.storage, "routing/builds/b1/assign/index.json")
        teams = blob(self.storage, f"routing/builds/b1/assign/{index[TEAMS]}.json")
        self.assertEqual(teams["provenance"]["taxonomy_sha256"], first.sha256)
        with self.assertRaises(FileNotFoundError):  # a build without a taxonomy copy has no pass C
            svc.assign(self.storage, payload("nope", RUN), 0, 4, assign_llm())

    def test_a_replay_rewrites_every_file_byte_for_byte(self):
        store = pipeline.StorageRecordStore(self.storage, CLIENT)
        self.run_pass_c(RecordingLLM(assign_llm(), mode="record", store=store))
        before = {k: v for k, v in self.storage.blobs.items() if "/assign/" in k[1]}
        replayed = RecordingLLM(None, mode="replay", model_id="schema-llm@test", store=store)
        parts, summary = self.run_pass_c(replayed)
        self.assertEqual((sum(p["calls"] for p in parts), summary["errors"]), (0, 0))
        self.assertEqual({k: v for k, v in self.storage.blobs.items() if "/assign/" in k[1]}, before)


class BuildTest(unittest.TestCase):
    def setUp(self):
        self.storage, self.table = write_run(Storage()), MemoryTable()

    def tables(self, build_id):
        return svc.load_tables(self.storage, CLIENT, build_id)

    def test_phase_one_routes_numbers_and_validated_aliases_with_no_taxonomy(self):
        decide(self.storage, self.table, TEAMS, aliases=["attribuer ligne teams"], app="teams")
        out = routing_run(self.storage, self.table, "b1")
        self.assertTrue(out["needs_b"])
        tables = self.tables("b1")
        self.assertEqual(tables.l0_numbers["233"], TEAMS)
        self.assertEqual(tables.l0_numbers["301"], NEW_VPN)
        self.assertNotIn("300", tables.l0_numbers)                                # a replaced fiche never routes
        self.assertEqual([(e["fiche_id"], e["alias"], e["app"]) for e in tables.l1],
                         [(TEAMS, "attribuer ligne teams", "teams")])
        self.assertEqual((tables.l2, tables.taxonomy), ([], {}))
        self.assertEqual(tables.stats["validated"], 1)
        self.assertEqual(blob(self.storage, "routing/builds/b1/decisions.json")[0]["fiche_id"], TEAMS)
        self.assertEqual(blob(self.storage, "routing/latest-build.json"),
                         {"build_id": "b1", "run_id": RUN, "state": "completed", "kind": "manual"})
        self.assertEqual(blob(self.storage, "routing/builds/b1/status.json")["state"], "completed")
        self.assertEqual(blob(self.storage, svc.CANDIDATE),
                         {"build_id": "b1", "run_id": RUN, "tables_sha256": tables.sha256,
                          "content_sha256": tables.content_sha256, "kb_sha256": tables.kb_sha256})
        self.assertEqual(out["built"]["tables"], tables.stats)
        self.assertEqual((out["built"]["carried"], out["built"]["why_not"]), (False, "no active tables"))

    def test_a_fiche_under_review_blocks_the_alias_it_proposes(self):
        # WEBEX is not reviewed yet and pass A proposes "attribuer une ligne webex" for it
        decide(self.storage, self.table, TEAMS, aliases=["attribuer ligne webex"])
        routing_run(self.storage, self.table, "b1")
        tables = self.tables("b1")
        self.assertEqual(tables.l1, [])
        self.assertEqual(tables.blocked["l1"], [{"tokens": ["attribuer", "ligne", "webex"], "fiches": [TEAMS],
                                                 "reason": "pending review", "pending": [WEBEX]}])

    def test_phase_two_routes_by_intent_and_application_and_a_partial_review_never_makes_a_key_unique(self):
        freeze(self.storage, self.table)
        decide(self.storage, self.table, ACCOUNT, intent="DEVERROUILLER_COMPTE")
        decide(self.storage, self.table, TEAMS, intent="ATTRIBUER_LIGNE", app="teams")
        routing_run(self.storage, self.table, "b1")
        tables = self.tables("b1")
        self.assertEqual(tables.taxonomy["version"], 1)
        self.assertEqual(tables.l2, [{"intent": "ATTRIBUER_LIGNE", "app": "teams", "fiches": [TEAMS]},
                                     {"intent": "DEVERROUILLER_COMPTE", "app": "", "fiches": [ACCOUNT]}])
        # the same fiche validated with no application: WEBEX, still under review, proposes the same intent
        decide(self.storage, self.table, TEAMS, intent="ATTRIBUER_LIGNE", seen=NOW, now=LATER)
        routing_run(self.storage, self.table, "b2", now=LATER)
        tables = self.tables("b2")
        self.assertEqual(tables.blocked["l2"], [{"intent": "ATTRIBUER_LIGNE", "app": "", "fiches": [TEAMS],
                                                 "pending": [WEBEX]}])
        self.assertNotIn("ATTRIBUER_LIGNE", [e["intent"] for e in tables.l2])
        self.assertEqual(blob(self.storage, "routing/builds/b2/summary.json")["assign_build"], "b1")

    def test_a_stale_decision_routes_nothing(self):
        decide(self.storage, self.table, TEAMS, aliases=["attribuer ligne teams"])
        write_run(self.storage, RUN2, sources={TEAMS: "the document was edited"})
        routing_run(self.storage, self.table, "b1")
        tables = self.tables("b1")
        self.assertEqual((tables.run_id, tables.l1, tables.stats["stale"]), (RUN2, [], 1))

    def test_the_candidate_follows_the_latest_or_newest_run_never_an_older_one(self):
        routing_run(self.storage, self.table, "b1")
        write_run(self.storage, RUN2, publish=False)                     # a kecore run, before it publishes
        routing_run(self.storage, self.table, "b2", run_id=RUN2)
        self.assertEqual(blob(self.storage, svc.CANDIDATE)["build_id"], "b2")   # newer than the candidate's run
        self.storage.write(CONTAINER, "latest.json", json.dumps({"run_id": RUN2}).encode())
        out = routing_run(self.storage, self.table, "b3", run_id=RUN)     # an older run, no longer served
        self.assertFalse(out["built"]["candidate"])
        self.assertEqual(blob(self.storage, svc.CANDIDATE)["build_id"], "b2")

    def test_an_unreadable_candidate_pointer_is_replaced_not_a_dead_end(self):
        self.storage.write(CONTAINER, svc.CANDIDATE, b"{cut")
        out = routing_run(self.storage, self.table, "b1")
        self.assertTrue(out["built"]["candidate"])
        self.assertEqual(blob(self.storage, svc.CANDIDATE)["build_id"], "b1")

    def test_review_json_holds_what_the_overview_lists(self):
        freeze(self.storage, self.table)
        routing_run(self.storage, self.table, "b1")
        review = blob(self.storage, "routing/builds/b1/review.json")
        self.assertEqual((review["run_id"], review["assign_build"]), (RUN, "b1"))
        self.assertEqual(review["fiches"][WEBEX], {"intent_a": "ATTRIBUTION_LIGNE_WEBEX", "primary_app_a": "webex",
                                                   "app_inferred": True, "error_a": None,
                                                   "intent_c": "ATTRIBUER_LIGNE", "error_c": None})
        self.assertNotIn(OLD_VPN, review["fiches"])


class CarryOverTest(unittest.TestCase):
    """An activation follows a new run only when the new tables answer exactly as the active ones."""

    def setUp(self):
        self.storage, self.table = write_run(Storage()), MemoryTable()
        decide(self.storage, self.table, TEAMS, aliases=["attribuer ligne teams"], app="teams")
        routing_run(self.storage, self.table, "b1")
        write_gate(self.storage, "b1")
        self.active = activate(self.storage, "b1")

    def rebuild(self, build_id, run_id=RUN2, **sources):
        return routing_run(self.storage, self.table, build_id, run_id=run_id, now=LATER)["built"]

    def test_same_tables_on_the_same_knowledge_base_carry_over_and_the_old_run_keeps_being_served(self):
        write_run(self.storage, RUN2, publish=False)
        built = self.rebuild("b2")
        self.assertEqual((built["carried"], built["why_not"]), (True, ""))
        active = blob(self.storage, svc.ACTIVE)
        self.assertEqual((active["build_id"], active["run_id"], active["carried_from"]), ("b2", RUN2, "b1"))
        self.assertEqual((active["activated_by"], active["gate"], active["override"]),
                         ("Yassine", self.active["gate"], None))
        self.assertEqual(active["previous"], {k: self.active[k] for k in ("build_id", "run_id", "tables_sha256",
                                                                          "content_sha256")})
        self.assertEqual(svc.active_for(self.storage, CLIENT, RUN)[0]["build_id"], "b1")   # until RUN2 is published
        self.assertEqual(svc.active_for(self.storage, CLIENT, RUN2)[0]["build_id"], "b2")
        history = [name for (_, name) in self.storage.blobs if name.startswith(svc.HISTORY)]
        self.assertEqual(len(history), 1)
        self.assertEqual(json.loads(self.storage.read(CONTAINER, history[0]))["build_id"], "b1")
        # an activity retried by Durable carries nothing twice
        again = svc.build(self.storage, self.table.list(CLIENT), payload("b2", RUN2), LATER)
        self.assertTrue(again["carried"])
        self.assertEqual(blob(self.storage, svc.ACTIVE)["previous"]["build_id"], "b1")

    def test_a_changed_knowledge_base_or_changed_tables_need_a_new_measure(self):
        write_run(self.storage, RUN2, publish=False, sources={PRINTER: "a new version of the printer document"})
        built = self.rebuild("b2")
        self.assertFalse(built["carried"])
        self.assertIn("kb_sha256", built["why_not"])
        write_run(self.storage, RUN2, publish=False)
        decide(self.storage, self.table, ACCOUNT, aliases=["débloquer compte"], run_id=RUN2)
        built = self.rebuild("b3")
        self.assertFalse(built["carried"])
        self.assertIn("content_sha256", built["why_not"])
        self.assertEqual(blob(self.storage, svc.ACTIVE)["build_id"], "b1")

    def test_a_failed_measure_of_the_active_tables_stops_them_and_their_carry_over(self):
        self.assertEqual(svc.active_for(self.storage, CLIENT, RUN)[0]["build_id"], "b1")
        write_gate(self.storage, "b1", status="failed")       # a later measure found a wrong exact fiche
        entry, why = svc.active_for(self.storage, CLIENT, RUN)
        self.assertIsNone(entry)
        self.assertIn("latest measure of the active routing tables failed", why)
        status = svc.status(self.storage, CLIENT, NOW)
        self.assertFalse(status["effective"])
        write_run(self.storage, RUN2, publish=False)
        built = self.rebuild("b2")
        self.assertFalse(built["carried"])
        self.assertIn("latest measure of the active tables failed", built["why_not"])
        self.assertEqual(blob(self.storage, svc.ACTIVE)["build_id"], "b1")

    def test_the_kill_switch_is_sticky_against_carry_over(self):
        svc.deactivate(self.storage, {"client": CLIENT, "by": "Yassine"}, NOW)
        self.storage.write(CONTAINER, svc.ACTIVE, json.dumps(self.active).encode())  # even with a pointer left
        write_run(self.storage, RUN2, publish=False)
        built = self.rebuild("b2")
        self.assertFalse(built["carried"])
        self.assertIn("kill switch", built["why_not"])
        self.assertEqual(blob(self.storage, svc.ACTIVE)["build_id"], "b1")

    def test_a_retried_build_never_reports_a_carry_over_the_kill_switch_undid(self):
        write_run(self.storage, RUN2, publish=False)
        self.assertTrue(self.rebuild("b2")["carried"])
        carried = blob(self.storage, svc.ACTIVE)
        svc.deactivate(self.storage, {"client": CLIENT, "by": "Marie"}, LATER)
        self.storage.write(CONTAINER, svc.ACTIVE, json.dumps(carried).encode())     # even with its pointer left
        again = svc.build(self.storage, self.table.list(CLIENT), payload("b2", RUN2), LATER)
        self.assertFalse(again["carried"])
        self.assertIn("kill switch", again["why_not"])
        self.assertEqual(svc.active_for(self.storage, CLIENT, RUN2)[0], None)

    def test_a_pointer_changed_during_the_build_is_left_alone(self):
        storage = self.storage
        reads = {"n": 0}
        real = storage.read

        def read(container, name):
            data = real(container, name)
            if name == svc.ACTIVE and data is not None:
                reads["n"] += 1
                if reads["n"] == 2:  # a person activated other tables between the comparison and the write
                    storage.write(container, name, json.dumps({**self.active, "build_id": "bX"}).encode())
                    return real(container, name)
            return data

        storage.read = read
        write_run(storage, RUN2, publish=False)
        built = svc.build(storage, self.table.list(CLIENT), payload("b2", RUN2), LATER)
        self.assertEqual((built["carried"], built["why_not"]), (False, "the active pointer changed meanwhile: left as it is"))
        self.assertEqual(blob(storage, svc.ACTIVE)["build_id"], "bX")

    def test_an_older_run_never_takes_the_activation(self):
        write_run(self.storage, RUN2)
        routing_run(self.storage, self.table, "b2", run_id=RUN2)
        write_gate(self.storage, "b2")
        activate(self.storage, "b2")
        built = svc.build(self.storage, self.table.list(CLIENT), payload("b3", RUN), LATER)
        self.assertFalse(built["carried"])
        self.assertIn("newer run", built["why_not"])


class LoadTablesTest(unittest.TestCase):
    def setUp(self):
        self.storage = write_run(Storage())
        routing_run(self.storage, MemoryTable(), "b1")
        self.name = "routing/builds/b1/tables.json"

    def rewrite(self, change, seal=False):
        tables = RoutingTables.from_dict(blob(self.storage, self.name))
        change(tables)
        if seal:
            tables.seal()
        self.storage.write(CONTAINER, self.name, tables.to_bytes())

    def test_tables_are_served_only_as_built_for_this_code_and_client(self):
        tables = svc.load_tables(self.storage, CLIENT, "b1", expected_sha=None)
        self.assertTrue(tables.verify())
        with self.assertRaises(ValueError):
            svc.load_tables(self.storage, CLIENT, "b1", expected_sha="0" * 64)
        with self.assertRaises(FileNotFoundError):
            svc.load_tables(self.storage, CLIENT, "b9")
        with self.assertRaises(ValueError):
            svc.load_tables(self.storage, CLIENT, "../b1")
        self.rewrite(lambda t: t.l0_numbers.update({"999": TEAMS}))
        with self.assertRaisesRegex(ValueError, "do not match their sha256"):
            svc.load_tables(self.storage, CLIENT, "b1")
        self.rewrite(lambda t: setattr(t, "code_version", {**CODE_VERSION, "route": 99}), seal=True)
        with self.assertRaisesRegex(ValueError, "built for code"):
            svc.load_tables(self.storage, CLIENT, "b1")
        self.rewrite(lambda t: setattr(t, "code_version", dict(CODE_VERSION)) or setattr(t, "client", "client-v"),
                     seal=True)
        with self.assertRaisesRegex(ValueError, "client-v"):
            svc.load_tables(self.storage, CLIENT, "b1")
        self.storage.write(CONTAINER, self.name, b"{not json")
        with self.assertRaisesRegex(ValueError, "unreadable"):
            svc.load_tables(self.storage, CLIENT, "b1")


class ActivationTest(unittest.TestCase):
    def setUp(self):
        self.storage, self.table = write_run(Storage()), MemoryTable()
        decide(self.storage, self.table, TEAMS, aliases=["attribuer ligne teams"], app="teams")
        routing_run(self.storage, self.table, "b1")

    def test_activation_needs_tables_of_the_served_run_and_a_passed_measure(self):
        with self.assertRaisesRegex(ValueError, "unknown build b9"):
            activate(self.storage, "b9")
        with self.assertRaisesRegex(ValueError, "never measured"):
            activate(self.storage, "b1")
        for status in ("failed", "invalid"):
            write_gate(self.storage, "b1", status)
            with self.assertRaisesRegex(ValueError, status):
                activate(self.storage, "b1")
        write_gate(self.storage, "b1", "insufficient")
        with self.assertRaisesRegex(ValueError, "insufficient"):
            activate(self.storage, "b1")
        self.assertIsNone(blob(self.storage, svc.ACTIVE))                      # nothing written by a refusal
        pointer = activate(self.storage, "b1", accept_insufficient=True, reason="2 reference questions only")
        self.assertEqual(pointer["override"], {"by": "Yassine", "reason": "2 reference questions only"})
        self.assertEqual(pointer["gate"]["status"], "insufficient")
        self.assertEqual(set(pointer["gate"]), set(svc.GATE_KEYS))
        write_gate(self.storage, "b1", "passed")
        pointer = activate(self.storage, "b1", now=LATER)
        tables = svc.load_tables(self.storage, CLIENT, "b1")
        self.assertEqual(pointer, {"build_id": "b1", "run_id": RUN, "tables_sha256": tables.sha256,
                                   "content_sha256": tables.content_sha256, "kb_sha256": tables.kb_sha256,
                                   "activated_by": "Yassine", "activated_at": LATER, "gate": pointer["gate"],
                                   "override": None, "carried_from": None, "previous": None})
        self.assertEqual(blob(self.storage, svc.ACTIVE), pointer)
        self.assertEqual(len([n for (_, n) in self.storage.blobs if n.startswith(svc.HISTORY)]), 1)

    def test_a_build_of_another_run_than_the_served_one_is_refused(self):
        write_gate(self.storage, "b1")
        write_run(self.storage, RUN2)                                       # a newer run is published
        with self.assertRaisesRegex(ValueError, f"/find serves run {RUN2}"):
            activate(self.storage, "b1")

    def test_a_gate_file_about_other_tables_is_refused(self):
        gate = write_gate(self.storage, "b1")
        summary = blob(self.storage, "routing/builds/b1/summary.json")
        self.storage.write(CONTAINER, svc.gate_path(summary["content_sha256"], RUN),
                           json.dumps({**gate, "content_sha256": "0" * 64}).encode())
        with self.assertRaisesRegex(ValueError, "not about these tables"):
            activate(self.storage, "b1")

    def test_deactivation_is_a_sticky_kill_switch_only_an_activation_clears(self):
        write_gate(self.storage, "b1")
        activate(self.storage, "b1")
        self.assertEqual(svc.active_for(self.storage, CLIENT, RUN)[0]["build_id"], "b1")
        self.assertEqual(svc.deactivate(self.storage, {"client": CLIENT, "by": "Marie"}, LATER),
                         {"active": None, "killed": True})
        self.assertEqual(blob(self.storage, svc.KILL), {"killed": True, "by": "Marie", "at": LATER})
        self.assertIsNone(blob(self.storage, svc.ACTIVE))
        self.assertEqual(svc.active_for(self.storage, CLIENT, RUN),
                         (None, f"routing is switched off (by Marie at {LATER})"))
        status = svc.status(self.storage, CLIENT, LATER)
        self.assertEqual((status["killed"], status["effective"], status["active"]), (True, False, None))
        activate(self.storage, "b1", now=LATER)
        self.assertEqual(blob(self.storage, svc.KILL), {"killed": False, "by": "Yassine", "at": LATER})
        self.assertEqual(svc.active_for(self.storage, CLIENT, RUN)[0]["build_id"], "b1")

    def test_an_unreadable_kill_switch_counts_as_on_and_active_for_never_raises(self):
        write_gate(self.storage, "b1")
        activate(self.storage, "b1")
        self.storage.write(CONTAINER, svc.KILL, b"{oops")
        entry, why = svc.active_for(self.storage, CLIENT, RUN)
        self.assertIsNone(entry)
        self.assertIn("switched off", why)
        self.storage.write(CONTAINER, svc.KILL, b'{"killed": false}')
        self.storage.write(CONTAINER, svc.ACTIVE, b"{oops")
        entry, why = svc.active_for(self.storage, CLIENT, RUN)
        self.assertIsNone(entry)
        self.assertIn("not valid JSON", why)

    def test_never_activated_is_no_route_and_no_error(self):
        self.assertEqual(svc.active_for(self.storage, CLIENT, RUN), (None, None))


class StatusTest(unittest.TestCase):
    def setUp(self):
        self.storage, self.table = write_run(Storage()), MemoryTable()
        decide(self.storage, self.table, TEAMS, aliases=["attribuer ligne teams"], app="teams")
        routing_run(self.storage, self.table, "b1")

    def test_status_says_what_is_served_and_why_not(self):
        status = svc.status(self.storage, CLIENT, NOW)
        self.assertEqual(set(status), {"client", "run_id", "taxonomy", "latest_build", "running", "candidate", "active",
                                       "effective", "killed", "gate", "why_not_effective"})
        self.assertEqual((status["run_id"], status["taxonomy"], status["running"], status["gate"], status["effective"]),
                         (RUN, None, None, None, False))
        self.assertEqual(status["candidate"]["stats"]["l1"], 1)
        self.assertEqual(status["why_not_effective"], "no active tables")
        gate = write_gate(self.storage, "b1")
        self.assertEqual(svc.status(self.storage, CLIENT, NOW)["gate"], {k: gate[k] for k in svc.GATE_KEYS})
        activate(self.storage, "b1")
        status = svc.status(self.storage, CLIENT, NOW)
        self.assertEqual((status["effective"], status["why_not_effective"], status["active"]["build_id"]),
                         (True, None, "b1"))
        svc.prepare(self.storage, payload("b2"), LATER)
        self.assertEqual(svc.status(self.storage, CLIENT, LATER)["running"]["build_id"], "b2")
        svc.finish(self.storage, payload("b2"), {"state": "failed", "error": "x"}, LATER)
        write_run(self.storage, RUN2)                                      # published, no carry-over
        status = svc.status(self.storage, CLIENT, LATER)
        self.assertFalse(status["effective"])
        self.assertEqual(status["why_not_effective"], f"the active routing tables were built for run {RUN}, not {RUN2}")

    def test_tables_damaged_after_activation_are_not_effective(self):
        write_gate(self.storage, "b1")
        activate(self.storage, "b1")
        tables = blob(self.storage, "routing/builds/b1/tables.json")
        tables["l0_numbers"]["999"] = TEAMS
        self.storage.write(CONTAINER, "routing/builds/b1/tables.json", json.dumps(tables).encode())
        status = svc.status(self.storage, CLIENT, NOW)
        self.assertFalse(status["effective"])
        self.assertIn("do not match their sha256", status["why_not_effective"])


OVERVIEW_KEYS = {"client", "run_id", "build", "running", "taxonomy", "counts", "fiches", "collisions", "blocked", "routing"}
FICHE_ROW_KEYS = {"key", "fiche_id", "label", "status", "intent_a", "intent_c", "intent", "primary_app", "app_inferred",
                  "error", "referenced"}
FICHE_KEYS = {"client", "run_id", "build_id", "key", "fiche_id", "label", "source_sha256", "status", "position",
              "text_excerpt", "proposal_a", "proposal_c", "decision", "previous_decision", "prefill", "taxonomy", "apps",
              "candidate_codes", "l0_number", "references", "seen"}


class ReviewOverviewTest(unittest.TestCase):
    def setUp(self):
        self.storage, self.table = write_run(Storage()), MemoryTable()
        self.refs = [ref_row(REF, "comment attribuer une ligne teams", [TEAMS])]

    def overview(self, **kwargs):
        return svc.review_overview(self.storage, self.table.list(CLIENT), self.refs, CLIENT, kbmap(self.storage),
                                   now=NOW, **kwargs)

    def test_the_overview_lists_every_routable_fiche_in_review_order(self):
        freeze(self.storage, self.table)
        decide(self.storage, self.table, ACCOUNT, intent="DEVERROUILLER_COMPTE", aliases=["débloquer compte"])
        decide(self.storage, self.table, TEAMS, aliases=["attribuer ligne webex"])  # blocked by WEBEX's proposal
        routing_run(self.storage, self.table, "b1")
        data = self.overview()
        self.assertEqual(set(data), OVERVIEW_KEYS)
        self.assertEqual(data["counts"], {"proposed": 12, "validated": 2, "rejected": 0, "stale": 0, "routable": ROUTABLE})
        self.assertEqual(len(data["fiches"]), ROUTABLE)
        self.assertNotIn(OLD_VPN, [row["fiche_id"] for row in data["fiches"]])
        self.assertTrue(all(set(row) == FICHE_ROW_KEYS for row in data["fiches"]))
        # referenced first, then fiches of a collision or a blocked key, then by label
        self.assertEqual([row["fiche_id"] for row in data["fiches"][:3]], [TEAMS, WEBEX, ACCOUNT])
        teams, webex, account = data["fiches"][:3]
        self.assertEqual((teams["key"], teams["referenced"], teams["status"]), (fiche_key(TEAMS), True, "validated"))
        self.assertEqual((webex["intent_a"], webex["intent_c"], webex["primary_app"], webex["app_inferred"]),
                         ("ATTRIBUTION_LIGNE_WEBEX", "ATTRIBUER_LIGNE", "webex", True))
        self.assertEqual((account["intent"], account["primary_app"], account["app_inferred"]),
                         ("DEVERROUILLER_COMPTE", None, False))
        self.assertEqual(data["taxonomy"]["frozen"]["intents"][1], {**FROZEN[1], "fiches": 1})
        self.assertEqual(data["taxonomy"]["frozen"]["source_build"], "b1")
        self.assertIsNone(data["taxonomy"]["proposed"])
        self.assertEqual(data["blocked"]["l1"], [{"tokens": ["attribuer", "ligne", "webex"],
                                                  "fiches": [{"fiche_id": TEAMS, "label": TEAMS}], "reason": "pending review",
                                                  "pending": [{"fiche_id": WEBEX, "label": WEBEX}]}])
        self.assertEqual(data["build"], {"build_id": "b1", "kind": "manual", "run_id": RUN, "state": "completed"})
        self.assertEqual(set(data["routing"]), {"candidate", "active", "effective", "killed", "gate", "why_not_effective"})
        self.assertEqual(data["routing"]["candidate"]["build_id"], "b1")

    def test_a_status_filter_keeps_the_counts_and_an_unknown_one_is_ignored(self):
        decide(self.storage, self.table, TEAMS, aliases=["attribuer ligne teams"])
        data = self.overview(status_filter="validated")
        self.assertEqual([row["fiche_id"] for row in data["fiches"]], [TEAMS])
        self.assertEqual(data["counts"]["proposed"], ROUTABLE - 1)
        self.assertEqual(len(self.overview(status_filter="bogus")["fiches"]), ROUTABLE)

    def test_the_overview_reads_the_same_with_or_without_the_builds_review_json(self):
        freeze(self.storage, self.table)
        routing_run(self.storage, self.table, "b1")
        fast = self.overview()
        del self.storage.blobs[(CONTAINER, "routing/builds/b1/review.json")]
        self.assertEqual(self.overview()["fiches"], fast["fiches"])

    def test_pass_bs_proposal_is_listed_until_a_taxonomy_is_frozen(self):
        routing_run(self.storage, self.table, "b1")
        proposed = self.overview()["taxonomy"]["proposed"]
        self.assertEqual((proposed["build_id"], proposed["run_id"], proposed["error"]), ("b1", RUN, None))
        self.assertEqual([i["id"] for i in proposed["intents"]][0], "ATTRIBUER_LIGNE")
        self.assertEqual(proposed["intents"][0]["members"], ["ATTRIBUTION_LIGNE_TELEPHONIQUE", "ATTRIBUTION_LIGNE_WEBEX"])
        self.assertEqual(proposed["source"]["fiches"], ROUTABLE)

    def test_collisions_name_their_fiches(self):
        decide(self.storage, self.table, TEAMS, aliases=["attribuer ligne vpn"])
        decide(self.storage, self.table, NEW_VPN, aliases=["attribuer ligne vpn"])
        routing_run(self.storage, self.table, "b1")
        self.assertEqual(self.overview()["collisions"]["l1"], [
            {"tokens": ["attribuer", "ligne", "vpn"], "fiches": [{"fiche_id": TEAMS, "label": TEAMS},
                                                                {"fiche_id": NEW_VPN, "label": NEW_VPN}],
             "aliases": ["attribuer ligne vpn"]}])


class ReviewFicheTest(unittest.TestCase):
    def setUp(self):
        self.storage, self.table = write_run(Storage()), MemoryTable()
        self.refs = [ref_row(REF, "comment attribuer une ligne teams", [TEAMS]),
                     ref_row(REF2, "attribuer ligne teams stagiaire svp", [TEAMS]),
                     ref_row("ref-" + "e" * 24, "mon compte est bloqué", [ACCOUNT])]

    def fiche(self, fiche_id, run_id=RUN):
        return svc.review_fiche(self.storage, self.table.list(CLIENT), self.refs, CLIENT, kbmap(self.storage, run_id),
                                fiche_key(fiche_id), now=NOW)

    def test_an_unknown_a_replaced_or_a_malformed_key_is_refused(self):
        with self.assertRaises(svc.UnknownFicheError):
            self.fiche("not a fiche")
        with self.assertRaises(svc.UnknownFicheError):
            self.fiche(OLD_VPN)
        with self.assertRaises(ValueError):
            svc.review_fiche(self.storage, [], [], CLIENT, kbmap(self.storage), "../etc", now=NOW)

    def test_a_proposed_fiche_comes_with_its_proposals_and_a_prefilled_form(self):
        freeze(self.storage, self.table)
        routing_run(self.storage, self.table, "b1")
        data = self.fiche(TEAMS)
        self.assertEqual(set(data), FICHE_KEYS)
        current = svc.source_hashes(self.storage, CLIENT, RUN)
        self.assertEqual((data["fiche_id"], data["key"], data["source_sha256"], data["status"], data["seen"]),
                         (TEAMS, fiche_key(TEAMS), current[TEAMS], "proposed", ""))
        self.assertEqual((data["run_id"], data["build_id"], data["l0_number"], data["candidate_codes"]),
                         (RUN, "b1", "233", []))
        self.assertEqual(data["position"], {"index": 2, "total": ROUTABLE})   # ACCOUNT is measured too, and first
        self.assertEqual(data["proposal_a"]["canonical_intent"], "ATTRIBUTION_LIGNE_TELEPHONIQUE")
        self.assertEqual(data["proposal_c"], {"intent": "ATTRIBUER_LIGNE", "core_aliases_fr": ["attribuer ligne teams"],
                                              "core_aliases_en": [], "dropped": [], "error": None})
        self.assertEqual(data["prefill"], {
            "intent": "ATTRIBUER_LIGNE", "primary_app": "teams", "codes": [],
            "aliases": [{"text": "attribuer ligne teams", "lang": "fr", "source": "pass_c", "ref_id": "", "checked": True},
                        {"text": "assign teams phone number", "lang": "en", "source": "pass_a", "ref_id": "",
                         "checked": False}]})  # "attribuer une ligne teams" is the same alias; "ligne 3" cannot route
        self.assertEqual(data["taxonomy"], [{"id": i["id"], "label_fr": i["label_fr"]} for i in FROZEN])
        self.assertIn("teams", data["apps"])
        self.assertEqual([(r["ref_id"], r["tokens"], r["is_alias"]) for r in data["references"]],
                         [(REF, ["attribuer", "ligne", "team"], False), (REF2, ["attribuer", "ligne", "team", "stagiaire"], False)])
        self.assertEqual(data["text_excerpt"], TEXTS[TEAMS][0])

    def test_an_inferred_application_is_never_preselected_and_codes_are_never_preticked(self):
        self.assertEqual(self.fiche(WEBEX)["prefill"]["primary_app"], "")
        account = self.fiche(ACCOUNT)
        self.assertEqual((account["candidate_codes"], account["prefill"]["codes"]), (["err:0x80070005"], []))
        self.assertIsNone(account["proposal_c"])                            # no frozen taxonomy, no pass C

    def test_a_validated_fiche_is_prefilled_with_its_decision(self):
        decision = decide(self.storage, self.table, ACCOUNT, refs=self.refs, aliases=["débloquer compte"],
                          codes=["err:0x80070005"], references=[("ref-" + "e" * 24, "mon compte est bloqué")])["decision"]
        data = self.fiche(ACCOUNT)
        self.assertEqual((data["status"], data["seen"], data["decision"]), ("validated", NOW, decision))
        self.assertEqual(data["prefill"]["codes"], ["err:0x80070005"])
        self.assertEqual([(a["text"], a["source"], a["checked"]) for a in data["prefill"]["aliases"]],
                         [("débloquer compte", "manual", True), ("mon compte est bloqué", "reference", True)])
        self.assertTrue(data["references"][0]["is_alias"])

    def test_a_stale_fiche_shows_its_previous_decision_and_a_changed_intent_is_not_prefilled(self):
        freeze(self.storage, self.table)
        decide(self.storage, self.table, TEAMS, intent="ATTRIBUER_LIGNE", app="teams", aliases=["attribuer ligne teams"])
        write_run(self.storage, RUN2, sources={TEAMS: "edited"})
        data = self.fiche(TEAMS, RUN2)
        self.assertEqual((data["status"], data["decision"]), ("stale", None))
        self.assertEqual(data["previous_decision"]["intent"], "ATTRIBUER_LIGNE")
        self.assertEqual((data["prefill"]["intent"], data["prefill"]["primary_app"]), ("ATTRIBUER_LIGNE", "teams"))
        self.assertEqual(data["position"], {"index": 1, "total": 1})
        freeze(self.storage, self.table, [dict(FROZEN[0], label_fr="Attribuer un numéro"), FROZEN[1]], orphans_ok=True)
        self.assertEqual(self.fiche(TEAMS, RUN2)["prefill"]["intent"], "")

    def test_a_pass_a_proposal_about_another_text_proposes_nothing(self):
        index = kbmap(self.storage).ranked.index(TEAMS)
        name = f"runs/{RUN}/enrichment/{index:05d}.json"
        self.storage.write(CONTAINER, name, json.dumps({**blob(self.storage, name), "content_sha256": "0" * 64}).encode())
        data = self.fiche(TEAMS)
        self.assertEqual(data["proposal_a"]["canonical_intent"], "ATTRIBUTION_LIGNE_TELEPHONIQUE")  # shown as it is
        self.assertEqual((data["prefill"]["aliases"], data["prefill"]["primary_app"]), ([], ""))      # never offered

    def test_pass_c_of_another_run_is_never_shown(self):
        freeze(self.storage, self.table)
        routing_run(self.storage, self.table, "b1")
        write_run(self.storage, RUN2)
        self.assertIsNone(self.fiche(TEAMS, RUN2)["proposal_c"])


class NextKeyTest(unittest.TestCase):
    def setUp(self):
        self.storage, self.table = write_run(Storage()), MemoryTable()

    def next(self, status, refs=()):
        return svc.next_key(self.storage, self.table.list(CLIENT), list(refs), CLIENT, kbmap(self.storage), status)

    def test_the_queue_starts_with_measured_fiches_then_blocking_ones_then_by_label(self):
        self.assertEqual(self.next("proposed"), fiche_key(ACCOUNT))                       # by label
        self.assertEqual(self.next("proposed", [ref_row(REF, "imprimante bloquée", [PRINTER])]), fiche_key(PRINTER))
        decide(self.storage, self.table, TEAMS, aliases=["attribuer ligne webex"])      # WEBEX's proposal blocks it
        routing_run(self.storage, self.table, "b1")
        self.assertEqual(self.next("proposed"), fiche_key(WEBEX))
        self.assertIsNone(self.next("stale"))
        with self.assertRaises(ValueError):
            self.next("validated")


class DecideTest(unittest.TestCase):
    def setUp(self):
        self.storage, self.table = write_run(Storage()), SdkTable()

    def row(self, fiche_id):
        current = svc.source_hashes(self.storage, CLIENT, RUN)
        return self.table.get(CLIENT, decision_row_key(fiche_id, current[fiche_id]))

    def test_a_decision_is_stored_once_per_text_and_a_redecision_overwrites_every_field(self):
        freeze(self.storage, self.table)
        out = decide(self.storage, self.table, TEAMS, intent="ATTRIBUER_LIGNE", app="teams",
                     aliases=["attribuer ligne teams"], note="ok")
        self.assertEqual(out["collisions"], [])
        row = self.row(TEAMS)
        self.assertEqual((row["revision"], row["run_id"], row["intent"], row["primary_app"], row["decided_by"]),
                         (0, RUN, "ATTRIBUER_LIGNE", "teams", "Yassine"))
        # the Tables SDK drops None on MERGE: an emptied intent or application must still overwrite
        decide(self.storage, self.table, TEAMS, aliases=["attribuer ligne teams"], seen=NOW, now=LATER)
        row = self.row(TEAMS)
        self.assertEqual((row["revision"], row["intent"], row["primary_app"], row["intent_fingerprint"]), (1, "", "", ""))
        stored = ReviewDecision.from_row(row)
        self.assertEqual((stored.intent, stored.primary_app, stored.decided_at), (None, None, LATER))
        decide(self.storage, self.table, TEAMS, status="rejected", aliases=["attribuer ligne teams"], seen=LATER, now=LATER)
        row = self.row(TEAMS)
        self.assertEqual((row["status"], row["aliases"], row["revision"]), ("rejected", "[]", 2))

    def test_two_reviewers_never_overwrite_each_other(self):
        decide(self.storage, self.table, TEAMS, aliases=["attribuer ligne teams"])
        with self.assertRaisesRegex(svc.ChangedError, "a changé depuis son affichage"):
            decide(self.storage, self.table, TEAMS, aliases=["attribuer ligne voip"], seen="", now=LATER)
        self.assertEqual(ReviewDecision.from_row(self.row(TEAMS)).aliases[0]["text"], "attribuer ligne teams")

    def test_a_row_created_by_another_reviewer_meanwhile_is_a_conflict(self):
        table = self.table
        real_read = table.read

        def read(client, row_key):
            row, etag = real_read(client, row_key)
            if row is None:  # the other reviewer's insert lands right after this read
                other = body(self.storage, TEAMS, aliases=["attribuer ligne voip"])
                current = svc.source_hashes(self.storage, CLIENT, RUN)
                table.create(ReviewDecision(fiche_id=TEAMS, source_sha256=current[TEAMS], status="validated",
                                            aliases=other["aliases"], decided_by="Marie",
                                            decided_at=LATER).to_row(CLIENT))
            return row, etag

        table.read = read
        with self.assertRaises(svc.ChangedError):
            decide(self.storage, table, TEAMS, aliases=["attribuer ligne teams"])
        self.assertEqual(ReviewDecision.from_row(self.row(TEAMS)).decided_by, "Marie")

    def test_refusals_say_why_in_french(self):
        cases = [
            (dict(aliases=["ligne 3 teams"]), ValueError, "le routage ignore « 3 »"),
            (dict(codes=["err:0x80070005"]), ValueError, "pas un identifiant propre à cette fiche"),
            (dict(intent="ATTRIBUER_LIGNE"), ValueError, "aucune taxonomie n'est figée"),
            (dict(app="notanapp"), ValueError, "absente du vocabulaire"),
            (dict(by=""), ValueError, "auteur"),
        ]
        for kwargs, error, message in cases:
            with self.assertRaisesRegex(error, message, msg=str(kwargs)):
                decide(self.storage, self.table, TEAMS, **kwargs)
        changed = body(self.storage, TEAMS)
        changed["source_sha256"] = "0" * 64
        with self.assertRaisesRegex(svc.ChangedError, "le texte de la fiche a changé"):
            svc.decide(self.table, self.storage, CLIENT, changed, kbmap(self.storage), [], NOW)
        for fiche_id in ("nope", OLD_VPN):
            unknown = dict(body(self.storage, TEAMS), fiche_id=fiche_id)
            with self.assertRaises(svc.UnknownFicheError):
                svc.decide(self.table, self.storage, CLIENT, unknown, kbmap(self.storage), [], NOW)
        with self.assertRaises(ValueError):
            svc.decide(self.table, self.storage, CLIENT, ["not", "a", "dict"], kbmap(self.storage), [], NOW)
        self.assertEqual(self.table.list(CLIENT), [])

    def test_a_promoted_reference_question_is_that_question_word_for_word(self):
        refs = [ref_row(REF, "attribuer ligne teams svp", [TEAMS])]
        with self.assertRaisesRegex(ValueError, "ce n'est pas le texte de la question de référence"):
            decide(self.storage, self.table, TEAMS, refs=refs, references=[(REF, "attribuer ligne webex")])
        with self.assertRaisesRegex(ValueError, "question de référence"):           # a reference of another fiche
            decide(self.storage, self.table, ACCOUNT, refs=refs, references=[(REF, "attribuer ligne teams svp")])
        out = decide(self.storage, self.table, TEAMS, refs=refs, references=[(REF, "attribuer  ligne teams svp")])
        self.assertEqual(out["decision"]["aliases"], [{"text": "attribuer ligne teams svp", "lang": "fr",
                                                       "source": "reference", "ref_id": REF}])

    def test_a_shared_alias_is_reported_at_once(self):
        decide(self.storage, self.table, ACCOUNT, aliases=["débloquer compte"], codes=["err:0x80070005"])
        out = decide(self.storage, self.table, NEW_VPN, aliases=["débloquer un compte", "connexion vpn impossible"])
        self.assertEqual(out["collisions"], [{"alias": "débloquer un compte", "fiches": [ACCOUNT]}])


class CarryForwardTest(unittest.TestCase):
    def setUp(self):
        self.storage, self.table = write_run(Storage()), MemoryTable()
        freeze(self.storage, self.table)
        decide(self.storage, self.table, TEAMS, intent="ATTRIBUER_LIGNE", app="teams", aliases=["attribuer ligne teams"])
        decide(self.storage, self.table, ACCOUNT, aliases=["débloquer compte"], codes=["err:0x80070005"])
        decide(self.storage, self.table, NEW_VPN, status="rejected")

    def carry(self, keys, run_id=RUN2):
        request = svc.validate_carry_request({"client": CLIENT, "by": "Marie", "keys": keys}, [CLIENT])
        return svc.carry_forward(self.table, self.storage, CLIENT, request, kbmap(self.storage, run_id), [], LATER)

    def test_a_stale_fiches_previous_decision_is_posted_again_on_its_new_text(self):
        write_run(self.storage, RUN2, sources={TEAMS: "edited", NEW_VPN: "edited too"})
        out = self.carry([fiche_key(TEAMS), fiche_key(NEW_VPN)])
        self.assertEqual(out, {"carried": 2, "failed": []})
        current = svc.source_hashes(self.storage, CLIENT, RUN2)
        row = ReviewDecision.from_row(self.table.get(CLIENT, decision_row_key(TEAMS, current[TEAMS])))
        self.assertEqual((row.intent, row.primary_app, row.decided_by, row.run_id, row.revision, row.aliases[0]["text"]),
                         ("ATTRIBUER_LIGNE", "teams", "Marie", RUN2, 0, "attribuer ligne teams"))
        self.assertEqual(ReviewDecision.from_row(self.table.get(
            CLIENT, decision_row_key(NEW_VPN, current[NEW_VPN]))).status, "rejected")
        self.assertEqual(len(self.table.list(CLIENT)), 5)                   # the decisions on the old texts kept

    def test_what_cannot_be_carried_is_said_per_fiche(self):
        items = fiches({ACCOUNT: ("The user account is locked. Unlock it in the console.", [])})
        write_run(self.storage, RUN2, items=items, sources={ACCOUNT: "edited", TEAMS: "edited"})
        freeze(self.storage, self.table, [dict(FROZEN[0], label_fr="Attribuer un numéro"), FROZEN[1]], orphans_ok=True)
        out = self.carry([fiche_key(TEAMS), fiche_key(ACCOUNT), fiche_key(PRINTER), "f" * 32], RUN2)
        errors = {item["key"]: item["error"] for item in out["failed"]}
        self.assertEqual(out["carried"], 0)
        self.assertIn("a changé de sens", errors[fiche_key(TEAMS)])                    # its intent means something else
        self.assertIn("err:0x80070005", errors[fiche_key(ACCOUNT)])                    # the code left the text
        self.assertIn("n'est pas « à revoir »", errors[fiche_key(PRINTER)])
        self.assertIn("fiche inconnue", errors["f" * 32])


class FreezeTaxonomyTest(unittest.TestCase):
    def setUp(self):
        self.storage, self.table = write_run(Storage()), MemoryTable()

    def test_a_freeze_is_versioned_and_kept(self):
        out = freeze(self.storage, self.table)
        taxonomy = svc.frozen_taxonomy(self.storage, CLIENT)
        self.assertEqual((out["version"], out["sha256"], out["orphans"]), (1, taxonomy.sha256, []))
        self.assertEqual((taxonomy.frozen_by, taxonomy.frozen_at, taxonomy.source_build), ("Yassine", NOW, "b1"))
        self.assertEqual(blob(self.storage, "taxonomy.history/v1.json"), taxonomy.to_dict())
        self.assertTrue(freeze(self.storage, self.table)["unchanged"])                 # same content: no new version
        self.assertIsNone(blob(self.storage, "taxonomy.history/v2.json"))
        out = freeze(self.storage, self.table, FROZEN + [{"id": "connexion vpn", "label_fr": "VPN"}], now=LATER)
        self.assertEqual((out["version"], [i["id"] for i in out["intents"]][1]), (2, "CONNEXION_VPN"))
        self.assertEqual(blob(self.storage, "taxonomy.history/v2.json")["version"], 2)

    def test_a_refused_row_is_named_in_french_and_nothing_is_written(self):
        with self.assertRaisesRegex(ValueError, r"^entrée 2 \(DEVERROUILLER_COMPTE\) : libellé manquant$"):
            freeze(self.storage, self.table, [FROZEN[0], {"id": "DEVERROUILLER_COMPTE", "label_fr": ""}])
        with self.assertRaisesRegex(ValueError, "auteur manquant"):
            freeze(self.storage, self.table, by=" ")
        with self.assertRaisesRegex(ValueError, "source_build"):
            freeze(self.storage, self.table, source_build="../x")
        self.assertIsNone(blob(self.storage, svc.TAXONOMY))

    def test_a_freeze_that_takes_their_intent_from_validated_fiches_must_be_confirmed(self):
        freeze(self.storage, self.table)
        decide(self.storage, self.table, TEAMS, intent="ATTRIBUER_LIGNE", app="teams")
        with self.assertRaises(svc.OrphansError) as caught:
            freeze(self.storage, self.table, [FROZEN[1]], now=LATER)
        self.assertEqual(caught.exception.orphans, [{"fiche_id": TEAMS, "label": TEAMS, "intent": "ATTRIBUER_LIGNE"}])
        self.assertIn("perdraient leur intention", str(caught.exception))
        self.assertEqual(svc.frozen_taxonomy(self.storage, CLIENT).version, 1)        # nothing written
        relabelled = [dict(FROZEN[0], label_fr="Attribuer un numéro"), FROZEN[1]]
        with self.assertRaises(svc.OrphansError):                                       # a new meaning, same id
            freeze(self.storage, self.table, relabelled, now=LATER)
        out = freeze(self.storage, self.table, relabelled, now=LATER, orphans_ok=True)
        self.assertEqual((out["version"], out["orphans"][0]["fiche_id"]), (2, TEAMS))

    def test_without_a_map_each_fiches_latest_decision_counts(self):
        freeze(self.storage, self.table)
        decide(self.storage, self.table, TEAMS, intent="ATTRIBUER_LIGNE")
        request = {"client": CLIENT, "by": "Y", "intents": [FROZEN[1]]}
        with self.assertRaises(svc.OrphansError) as caught:
            svc.freeze_taxonomy(self.storage, self.table.list(CLIENT), request, LATER)
        self.assertEqual(caught.exception.orphans, [{"fiche_id": TEAMS, "label": TEAMS, "intent": "ATTRIBUER_LIGNE"}])

    def test_two_freezes_from_the_same_version_never_overwrite_each_other(self):
        freeze(self.storage, self.table)
        self.storage.write(CONTAINER, "taxonomy.history/v2.json", b"{}")               # the other freeze landed first
        with self.assertRaisesRegex(svc.ChangedError, "une autre version"):
            freeze(self.storage, self.table, FROZEN[:1], now=LATER)
        self.assertEqual(svc.frozen_taxonomy(self.storage, CLIENT).version, 1)

    def test_a_form_built_from_an_older_version_never_overwrites_a_newer_freeze(self):
        freeze(self.storage, self.table, base_version=0)
        freeze(self.storage, self.table, FROZEN[:1], now=LATER, base_version=1)       # reviewer B: v2
        with self.assertRaisesRegex(svc.ChangedError, r"figée entre-temps \(version 2\)"):
            freeze(self.storage, self.table, FROZEN, now=LATER, base_version=1)       # reviewer A, still on v1
        self.assertEqual(svc.frozen_taxonomy(self.storage, CLIENT).version, 2)
        for bad in (-1, "1", True):
            with self.assertRaises(ValueError):
                freeze(self.storage, self.table, FROZEN, base_version=bad)


if __name__ == "__main__":
    unittest.main()
