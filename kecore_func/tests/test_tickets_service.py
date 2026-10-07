"""POST /api/kecore/tickets/scrub and /run (V10 slice 4): scrubbing real tickets into Table
Storage, then an unlabeled "blank pass" through kefind's funnel, tallied only (no ground truth
yet, so no correctness judgment -- see docs/operations-runbook.md).

Run from the repository root:  python -m unittest discover -s kecore_func/tests
"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for path in (ROOT, ROOT / "kecore_func"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import kecore_pipeline as pipeline  # noqa: E402
import tickets_service as svc  # noqa: E402
from kecore.decompose import DecomposedFiche, Step  # noqa: E402
from kefind.tests.helpers import StubLLM  # noqa: E402

RAW_CSV = (
    '"Date d\'émission";"N° de ticket";"N° d\'origine";"Priorité";"Criticité";'
    '"Impact";"Statut";"Meta Statut";"Cause réelle";"Titre";"Sujet";"Application / Service";'
    '"Description";"Bénéficiaire";"Demandeur";"Intervenant en cours";"Groupe en cours";'
    '"Entité complète";"Localisation complète";"Groupe responsable du sujet";"SLA";'
    '"Date de résolution";"Numéro SR";"Référence externe";"Enregistré par";"Origine";'
    '"Résolution";"Groupe de résolution";"Dernière modification";"1er groupe d\'affectation";'
    '"Résolu par (intervenant)";"Sujet complet"\n'
    "-;8;8;8;8;8;8;8;8;8;8;8;8;8;8;8;8;8;8;8;8;8;8;8;8;8;8;8;8;8;8;8\n"
    '"01/10/2026 18:16:09";"I261001_1220";"";"4";"03 - Moyenne";"03 - Région";"A prendre en compte";'
    '"En cours";"";"Compte bloqué";"Ouverture par email";"Windows";'
    '"Mon compte est bloqué, je n\'arrive plus à me connecter à Windows";'
    '"Jean Dupont";"Marie Martin";"Paul Durand";"Groupe A";"Entité X";"Site Y";"Groupe B";'
    '"4h";"";"";"";"Alice Technicienne";"Portail";"Résolu";"Groupe C";'
    '"02/10/2026";"Groupe D";"Bob Résolveur";"Sujet complet"\n'
).encode("utf-8-sig")


class MemoryStorage:
    def __init__(self):
        self.blobs: dict[tuple[str, str], bytes] = {}

    def list(self, container, prefix):
        return sorted(name for (c, name) in self.blobs if c == container and name.startswith(prefix))

    def read(self, container, name):
        return self.blobs.get((container, name))

    def write(self, container, name, data):
        self.blobs[(container, name)] = data

    def delete(self, container, name):
        self.blobs.pop((container, name), None)


class MemoryTable:
    def __init__(self):
        self.rows: dict[tuple[str, str], dict] = {}

    def upsert(self, entity):
        self.rows[(entity["PartitionKey"], entity["RowKey"])] = entity

    def list(self, client):
        return [row for (partition, _), row in sorted(self.rows.items()) if partition == client]

    def count(self, client):
        return len(self.list(client))


def fiche(client="client-s"):
    return DecomposedFiche(
        fiche_id="KB0120", client=client, title="KB0120- LOCKED ACCOUNT", source="",
        text="Déverrouillez le compte dans Active Directory.", text_sha256="", status="guided",
        confidence="high", reasons=[], sections=[],
        steps=[Step(n=1, start=0, end=38, text="Déverrouillez le compte dans Active Directory.",
                    kind="action", role="resolution")],
        entities=[{"canonical": "app:active-directory", "kind": "app", "count": 1}],
        references=[], methods={}, checks={},
    )


class ValidateTicketsRequestTest(unittest.TestCase):
    def test_a_known_client_is_accepted(self):
        self.assertEqual(svc.validate_tickets_request({"client": "client-s"}, ["client-s"]), {"client": "client-s"})

    def test_an_unknown_client_is_refused_without_listing_the_others(self):
        with self.assertRaises(ValueError) as caught:
            svc.validate_tickets_request({"client": "other"}, ["client-s", "client-v"])
        self.assertNotIn("client-v", str(caught.exception))

    def test_bad_bodies_are_refused(self):
        for body in ({}, {"client": 1}, ["client-s"]):
            with self.assertRaises(ValueError, msg=str(body)):
                svc.validate_tickets_request(body, ["client-s"])


class ValidateRunRequestTest(unittest.TestCase):
    def test_defaults(self):
        payload = svc.validate_run_request({"client": "client-s"}, ["client-s"])
        self.assertIsNone(payload["run_id"])
        self.assertTrue(payload["interpret"])
        self.assertEqual(payload["limit"], svc.DEFAULT_RUN_LIMIT)

    def test_bad_values_are_refused(self):
        for body in ({"client": "client-s", "run_id": 1}, {"client": "client-s", "interpret": "yes"},
                     {"client": "client-s", "limit": 0}, {"client": "client-s", "limit": True},
                     {"client": "client-s", "limit": svc.MAX_RUN_LIMIT + 1}):
            with self.assertRaises(ValueError, msg=str(body)):
                svc.validate_run_request(body, ["client-s"])


class ScrubTest(unittest.TestCase):
    def test_a_raw_export_becomes_table_rows_and_is_deleted(self):
        storage, table = MemoryStorage(), MemoryTable()
        storage.write("tickets-client-s", "raw/export.csv", RAW_CSV)
        report = svc.scrub(storage, table, "client-s")
        self.assertEqual(report["files"], 1)
        self.assertEqual(report["tickets"], 1)
        self.assertEqual(table.count("client-s"), 1)
        self.assertEqual(storage.list("tickets-client-s", "raw/"), [])

    def test_no_raw_file_is_a_clean_no_op(self):
        storage, table = MemoryStorage(), MemoryTable()
        report = svc.scrub(storage, table, "client-s")
        self.assertEqual(report, {"client": "client-s", "files": 0, "tickets": 0, "skipped": 0, "masked": {}})

    def test_non_csv_files_under_raw_are_ignored(self):
        storage, table = MemoryStorage(), MemoryTable()
        storage.write("tickets-client-s", "raw/notes.txt", b"not a ticket export")
        report = svc.scrub(storage, table, "client-s")
        self.assertEqual(report["files"], 0)
        self.assertEqual(table.count("client-s"), 0)


class RunBatchTest(unittest.TestCase):
    def setUp(self):
        self.storage = MemoryStorage()
        self.storage.write("kecore-client-s", "runs/r1/fiches.decomposed.jsonl",
                            (__import__("json").dumps(fiche().to_dict(), ensure_ascii=False) + "\n").encode("utf-8"))
        self.storage.write("kecore-client-s", "latest.json", b'{"run_id": "r1"}')
        self.table = MemoryTable()
        self.table.upsert({
            "PartitionKey": "client-s", "RowKey": "I261001_1220",
            "titre": "Compte bloqué", "sujet": "Ouverture par email",
            "description": "Mon compte est bloqué, je n'arrive plus à me connecter à Windows",
        })

    def test_a_ticket_is_tallied_by_what_the_funnel_does(self):
        payload = {"client": "client-s", "run_id": None, "interpret": False, "limit": 200}
        result = svc.run_batch(self.storage, self.table, payload, 0, 200)
        self.assertEqual(result["run_id"], "r1")
        self.assertEqual(result["tickets"], 1)
        self.assertEqual(result["empty"], 0)
        self.assertEqual(sum(result["kinds"].values()), 1)

    def test_interpretation_is_used_when_asked_and_an_llm_is_given(self):
        payload = {"client": "client-s", "run_id": None, "interpret": True, "limit": 200}
        llm = StubLLM({"terms": ["account locked", "locked account", "compte bloqué"], "application": None})
        result = svc.run_batch(self.storage, self.table, payload, 0, 200, llm=llm)
        self.assertEqual(result["tickets"], 1)
        self.assertTrue(llm.calls)

    def test_a_ticket_with_no_text_is_counted_empty_not_tallied(self):
        self.table.upsert({"PartitionKey": "client-s", "RowKey": "I261001_1221"})
        payload = {"client": "client-s", "run_id": None, "interpret": False, "limit": 200}
        result = svc.run_batch(self.storage, self.table, payload, 0, 200)
        self.assertEqual(result["tickets"], 2)
        self.assertEqual(result["empty"], 1)

    def test_no_kb_map_for_the_client_is_a_clear_error(self):
        payload = {"client": "client-v", "run_id": None, "interpret": False, "limit": 200}
        with self.assertRaises(FileNotFoundError):
            svc.run_batch(self.storage, self.table, payload, 0, 200)

    def test_batches_slice_the_same_client_without_overlap(self):
        self.table.upsert({"PartitionKey": "client-s", "RowKey": "I261001_1221", "titre": "Autre ticket"})
        payload = {"client": "client-s", "run_id": None, "interpret": False, "limit": 200}
        first = svc.run_batch(self.storage, self.table, payload, 0, 1)
        second = svc.run_batch(self.storage, self.table, payload, 1, 2)
        self.assertEqual(first["tickets"] + second["tickets"], 2)


class MergeRunsTest(unittest.TestCase):
    def test_tallies_add_up_across_batches(self):
        parts = [
            {"run_id": "r1", "tickets": 3, "empty": 1, "kinds": {"fiche": 2}, "reasons": {"close_title_match": 2}},
            {"run_id": "r1", "tickets": 2, "empty": 0, "kinds": {"question": 2}, "reasons": {"close_entities": 2}},
        ]
        merged = svc.merge_runs("client-s", parts)
        self.assertEqual(merged["tickets"], 5)
        self.assertEqual(merged["empty"], 1)
        self.assertEqual(merged["kinds"], {"fiche": 2, "question": 2})
        self.assertEqual(merged["run_id"], "r1")

    def test_no_parts_is_an_empty_merge(self):
        self.assertEqual(svc.merge_runs("client-s", []),
                          {"client": "client-s", "run_id": None, "tickets": 0, "empty": 0, "kinds": {}, "reasons": {}})


class TicketCountTest(unittest.TestCase):
    def test_capped_by_the_limit(self):
        table = MemoryTable()
        for i in range(5):
            table.upsert({"PartitionKey": "client-s", "RowKey": f"I{i}"})
        self.assertEqual(svc.ticket_count(table, "client-s", 3), 3)
        self.assertEqual(svc.ticket_count(table, "client-s", 200), 5)


if __name__ == "__main__":
    unittest.main()
