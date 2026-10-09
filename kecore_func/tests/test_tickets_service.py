"""POST /api/kecore/tickets/scrub, /rescrub and /runs (V10 slice 4): scrubbing real tickets into
Table Storage, re-cleaning stored rows, then an unlabeled "blank pass" through kefind's funnel,
tallied, with each ticket's finding kept on its row for the labeling tab.

Run from the repository root:  python -m unittest discover -s kecore_func/tests
"""

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for path in (ROOT, ROOT / "kecore_func"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

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
    '"4h";"";"";"";"Alice Technicienne";"Portail";"Résolu, prévenu alice@example.com";"Groupe C";'
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
    """Same contract as kecore_table.TableStorage: writes MERGE, reads sorted by RowKey, ETags."""

    def __init__(self):
        self.rows: dict[tuple[str, str], dict] = {}
        self.etags: dict[tuple[str, str], str] = {}
        self._version = 0

    def _touch(self, key):
        self._version += 1
        self.etags[key] = f"W/{self._version}"

    def upsert(self, entity):
        key = (entity["PartitionKey"], entity["RowKey"])
        self.rows[key] = {**self.rows.get(key, {}), **entity}
        self._touch(key)

    merge = upsert

    def read(self, client, row_key):
        key = (client, row_key)
        return (dict(self.rows[key]), self.etags[key]) if key in self.rows else (None, None)

    def create(self, entity):
        key = (entity["PartitionKey"], entity["RowKey"])
        if key in self.rows:
            return False
        self.upsert(entity)
        return True

    def merge_if(self, entity, etag):
        key = (entity["PartitionKey"], entity["RowKey"])
        if key not in self.rows or self.etags.get(key) != etag:
            return False
        self.upsert(entity)
        return True

    def get(self, client, row_key):
        row = self.rows.get((client, row_key))
        return dict(row) if row else None

    def list(self, client, select=None):
        rows = [dict(row) for (partition, _), row in sorted(self.rows.items()) if partition == client]
        if select:
            rows = [{k: v for k, v in row.items() if k in select} for row in rows]
        return rows

    def count(self, client):
        return len(self.list(client))

    def delete(self, client, row_key):
        self.rows.pop((client, row_key), None)
        self.etags.pop((client, row_key), None)


def fiche(fiche_id="KB0120", title="KB0120- LOCKED ACCOUNT", client="client-s", status="guided"):
    text = "Déverrouillez le compte dans Active Directory."
    return DecomposedFiche(
        fiche_id=fiche_id, client=client, title=title, source="", text=text, text_sha256="", status=status,
        confidence="high", reasons=[], sections=[],
        steps=[Step(n=1, start=0, end=len(text), text=text, kind="action", role="resolution")],
        entities=[{"canonical": "app:active-directory", "kind": "app", "count": 1}],
        references=[], methods={}, checks={},
    )


FILLER_TOPICS = ("imprimante bourrage papier", "badge accès parking", "écran externe sans signal",
                 "clavier sans fil pile", "souris bluetooth appairage", "casque audio micro",
                 "téléphone fixe tonalité", "station accueil dock", "batterie portable charge", "wifi invité code")


def filler():
    """Unrelated fiches, so that word statistics look like a real KB's and not like a 1-fiche one."""
    out = []
    for i, topic in enumerate(FILLER_TOPICS):
        text = f"Vérifiez {topic}. Redémarrez {topic}."
        out.append(DecomposedFiche(
            fiche_id=f"KB09{i:02d} - {topic}", client="client-s", title=f"KB09{i:02d} - {topic}", source="",
            text=text, text_sha256="", status="guided", confidence="high", reasons=[], sections=[],
            steps=[Step(n=1, start=0, end=len(text), text=text, kind="action", role="resolution")],
            entities=[], references=[], methods={}, checks={}))
    return out


def kb_storage(*fiches):
    storage = MemoryStorage()
    lines = "".join(json.dumps(f.to_dict(), ensure_ascii=False) + "\n" for f in (fiches or (fiche(),)))
    storage.write("kecore-client-s", "runs/r1/fiches.decomposed.jsonl", lines.encode("utf-8"))
    storage.write("kecore-client-s", "latest.json", b'{"run_id": "r1"}')
    return storage


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
        for body in ({"client": "client-s", "run_id": 1}, {"client": "client-s", "run_id": "../x"},
                     {"client": "client-s", "interpret": "yes"},
                     {"client": "client-s", "limit": 0}, {"client": "client-s", "limit": True},
                     {"client": "client-s", "limit": svc.MAX_RUN_LIMIT + 1}):
            with self.assertRaises(ValueError, msg=str(body)):
                svc.validate_run_request(body, ["client-s"])


class ScrubTest(unittest.TestCase):
    def test_a_raw_export_becomes_table_rows_and_is_deleted(self):
        storage, table = MemoryStorage(), MemoryTable()
        storage.write("tickets-client-s", "raw/export.csv", RAW_CSV)
        report = svc.scrub(storage, table, "client-s")
        self.assertEqual((report["files"], report["tickets"]), (1, 1))
        self.assertEqual(table.count("client-s"), 1)
        self.assertEqual(storage.list("tickets-client-s", "raw/"), [])
        self.assertEqual(table.get("client-s", "I261001_1220")["resolution"], "Résolu, prévenu [email]")

    def test_no_raw_file_is_a_clean_no_op(self):
        report = svc.scrub(MemoryStorage(), MemoryTable(), "client-s")
        self.assertEqual(report, {"client": "client-s", "files": 0, "tickets": 0, "skipped": 0, "masked": {}})

    def test_non_csv_files_under_raw_are_ignored(self):
        storage, table = MemoryStorage(), MemoryTable()
        storage.write("tickets-client-s", "raw/notes.txt", b"not a ticket export")
        self.assertEqual(svc.scrub(storage, table, "client-s")["files"], 0)
        self.assertEqual(table.count("client-s"), 0)

    def test_a_second_export_keeps_what_was_merged_into_a_row(self):
        storage, table = MemoryStorage(), MemoryTable()
        storage.write("tickets-client-s", "raw/a.csv", RAW_CSV)
        svc.scrub(storage, table, "client-s")
        table.merge({"PartitionKey": "client-s", "RowKey": "I261001_1220", "kefind_kind": "fiche"})
        storage.write("tickets-client-s", "raw/b.csv", RAW_CSV)
        svc.scrub(storage, table, "client-s")
        self.assertEqual(table.get("client-s", "I261001_1220")["kefind_kind"], "fiche")


class RescrubTest(unittest.TestCase):
    def test_rows_stored_before_the_fix_are_cleaned_in_place_once(self):
        table = MemoryTable()
        table.upsert({"PartitionKey": "client-s", "RowKey": "I1", "titre": "Compte bloqué",
                      "resolution": "Prévenu jean.dupont@example.com", "date_d_emission": "01/10/2026 18:16:09"})
        table.upsert({"PartitionKey": "client-s", "RowKey": "I2", "titre": "Rien à masquer"})
        first = svc.rescrub(table, "client-s")
        self.assertEqual((first["rows"], first["changed"]), (2, 1))
        self.assertEqual(first["masked"].get("[email]"), 1)
        self.assertEqual(table.get("client-s", "I1")["resolution"], "Prévenu [email]")
        self.assertEqual(table.get("client-s", "I1")["date_d_emission"], "01/10/2026 18:16:09")
        self.assertEqual(svc.rescrub(table, "client-s")["changed"], 0)


class CatalogTest(unittest.TestCase):
    def test_every_fiche_of_the_map_is_offered_and_gone_ones_removed(self):
        storage = kb_storage(fiche(), fiche("KB0200", "KB0200- INFO", status="info_only"))
        fiches = MemoryTable()
        fiches.upsert({"PartitionKey": "client-s", "RowKey": "stale", "fiche_id": "KB0999"})
        report = svc.catalog(storage, fiches, {"client": "client-s", "run_id": None})
        self.assertEqual((report["fiches"], report["removed"], report["run_id"]), (2, 1, "r1"))
        rows = {row["fiche_id"]: row for row in fiches.list("client-s")}
        self.assertEqual(set(rows), {"KB0120", "KB0200"})
        self.assertTrue(rows["KB0120"]["searchable"])
        self.assertFalse(rows["KB0200"]["searchable"])
        self.assertEqual(rows["KB0120"]["RowKey"], svc.fiche_row_key("KB0120"))

    def test_a_row_key_never_holds_a_character_tables_refuse(self):
        key = svc.fiche_row_key("Procédure #2 / VPN ?")
        self.assertRegex(key, r"^[0-9a-f]{32}$")


class RunBatchTest(unittest.TestCase):
    def setUp(self):
        self.storage = kb_storage()
        self.table = MemoryTable()
        self.table.upsert({
            "PartitionKey": "client-s", "RowKey": "I261001_1220",
            "titre": "Compte bloqué", "sujet": "Ouverture par email",
            "description": "Mon compte est bloqué, je n'arrive plus à me connecter à Windows",
        })
        self.payload = {"client": "client-s", "run_id": None, "interpret": False, "limit": 200}

    def test_a_ticket_is_tallied_and_its_finding_kept_on_its_row(self):
        result = svc.run_batch(self.storage, self.table, self.payload, 0, 200)
        self.assertEqual((result["run_id"], result["tickets"], result["empty"], result["errors"]), ("r1", 1, 0, 0))
        self.assertEqual(sum(result["kinds"].values()), 1)
        row = self.table.get("client-s", "I261001_1220")
        self.assertIn(row["kefind_kind"], ("fiche", "question", "abstain"))
        self.assertEqual(row["kefind_kb_run"], "r1")
        self.assertIsInstance(json.loads(row["kefind_candidates"]), list)
        self.assertEqual(row["titre"], "Compte bloqué")  # the finding is merged, the ticket kept

    def test_interpretation_is_used_when_asked_and_an_llm_is_given(self):
        llm = StubLLM({"terms": ["account locked", "locked account", "compte bloqué"], "application": None})
        result = svc.run_batch(self.storage, self.table, {**self.payload, "interpret": True}, 0, 200, llm=llm)
        self.assertEqual(result["tickets"], 1)
        self.assertTrue(llm.calls)
        self.assertTrue(self.table.get("client-s", "I261001_1220")["kefind_interpreted"])

    def test_a_ticket_with_no_text_is_counted_empty_not_tallied(self):
        self.table.upsert({"PartitionKey": "client-s", "RowKey": "I261001_1221"})
        result = svc.run_batch(self.storage, self.table, self.payload, 0, 200)
        self.assertEqual((result["tickets"], result["empty"]), (2, 1))

    def test_one_failing_ticket_is_counted_and_the_batch_goes_on(self):
        class Boom:
            def complete_json(self, *args, **kwargs):
                raise MemoryError("boom")  # not an LLMError: interpret lets it through

        self.table.upsert({"PartitionKey": "client-s", "RowKey": "I261001_1222", "titre": "Autre ticket"})
        result = svc.run_batch(self.storage, self.table, {**self.payload, "interpret": True}, 0, 200, llm=Boom())
        self.assertEqual((result["tickets"], result["errors"]), (2, 2))
        self.assertEqual(result["reasons"], {"error:MemoryError": 2})

    def test_no_kb_map_for_the_client_is_a_clear_error(self):
        with self.assertRaises(FileNotFoundError):
            svc.run_batch(self.storage, self.table, {**self.payload, "client": "client-v"}, 0, 200)

    def test_batches_slice_the_same_client_without_overlap(self):
        self.table.upsert({"PartitionKey": "client-s", "RowKey": "I261001_1221", "titre": "Autre ticket"})
        first = svc.run_batch(self.storage, self.table, self.payload, 0, 1)
        second = svc.run_batch(self.storage, self.table, self.payload, 1, 2)
        self.assertEqual(first["tickets"] + second["tickets"], 2)

    def test_the_client_calibrated_floor_is_applied(self):
        self.storage = kb_storage(fiche(), *filler())
        self.table.upsert({"PartitionKey": "client-s", "RowKey": "I261001_1220",
                           "titre": "Active Directory : locked account", "sujet": "", "description": ""})
        svc.run_batch(self.storage, self.table, self.payload, 0, 200)
        self.assertEqual(self.table.get("client-s", "I261001_1220")["kefind_kind"], "fiche")
        self.storage.write("kecore-client-s", "funnel-config.json", b'{"funnel": {"min_show": 0.99}}')
        svc.run_batch(self.storage, self.table, self.payload, 0, 200)
        row = self.table.get("client-s", "I261001_1220")
        self.assertEqual(row["kefind_kind"], "question")
        self.assertTrue(row["kefind_reason"].endswith("_below_min_show"), row["kefind_reason"])


class MergeRunsTest(unittest.TestCase):
    def test_tallies_add_up_across_batches(self):
        parts = [
            {"run_id": "r1", "tickets": 3, "empty": 1, "errors": 1, "kinds": {"fiche": 2}, "reasons": {"a": 2}},
            {"run_id": "r1", "tickets": 2, "empty": 0, "kinds": {"question": 2}, "reasons": {"b": 2}},
        ]
        merged = svc.merge_runs("client-s", parts)
        self.assertEqual((merged["tickets"], merged["empty"], merged["errors"]), (5, 1, 1))
        self.assertEqual(merged["kinds"], {"fiche": 2, "question": 2})
        self.assertEqual(merged["run_id"], "r1")

    def test_no_parts_is_an_empty_merge(self):
        self.assertEqual(svc.merge_runs("client-s", []),
                         {"client": "client-s", "run_id": None, "tickets": 0, "empty": 0, "errors": 0,
                          "interpret_failures": 0, "kinds": {}, "reasons": {}, "modes": {}})


class JsonlTest(unittest.TestCase):
    def test_a_line_separator_inside_a_string_does_not_split_the_line(self):
        import kecore_pipeline as pipeline

        items = [{"text": "avant\u2028après\u2029fin\u0085x"}, {"text": "deux"}]
        data = "".join(json.dumps(i, ensure_ascii=False) + "\n" for i in items).encode("utf-8")
        self.assertEqual(pipeline.read_jsonl(data), items)
        self.assertEqual(pipeline.read_jsonl(data.replace(b"\n", b"\r\n")), items)


class TicketCountTest(unittest.TestCase):
    def test_capped_by_the_limit(self):
        table = MemoryTable()
        for i in range(5):
            table.upsert({"PartitionKey": "client-s", "RowKey": f"I{i}"})
        self.assertEqual(svc.ticket_count(table, "client-s", 3), 3)
        self.assertEqual(svc.ticket_count(table, "client-s", 200), 5)


if __name__ == "__main__":
    unittest.main()
