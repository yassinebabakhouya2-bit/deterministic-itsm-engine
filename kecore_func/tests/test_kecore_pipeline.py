"""The Azure pipeline gives exactly what a local kecore run gives, from blobs instead of files.

Run from the repository root:  python -m unittest discover -s kecore_func/tests
"""

import json
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for path in (ROOT, ROOT / "kecore_func"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import kecore_pipeline as pipeline  # noqa: E402
from kecore.decompose import Decomposer, summarize_kb  # noqa: E402
from kecore.exclusion import Exclusion, ExclusionResult  # noqa: E402
from kecore.fiches import load_folder  # noqa: E402
from kecore.llm import RecordingLLM  # noqa: E402
from kecore.profile import build_profile  # noqa: E402
from kecore.tests.helpers import DEMO_KB, FakeLLM, OUTLOOK_ANSWER  # noqa: E402

KEYS = ("fiches", "guided", "citable", "info_only", "steps", "steps_verified", "mean_agreement")


class MemoryStorage:
    def __init__(self):
        self.blobs: dict[tuple[str, str], bytes] = {}

    def list(self, container, prefix):
        return sorted(name for (c, name) in self.blobs if c == container and name.startswith(prefix))

    def read(self, container, name):
        return self.blobs.get((container, name))

    def write(self, container, name, data):
        self.blobs[(container, name)] = data


def demo_storage(prefix="Kbs/"):
    storage = MemoryStorage()
    for path in DEMO_KB.rglob("*"):
        if path.is_file():
            storage.write("kb-clienta", prefix + path.relative_to(DEMO_KB).as_posix(), path.read_bytes())
    storage.write("kb-clienta", "Audio/call.wav", b"not a fiche")  # outside the prefix, never read
    return storage


def run(storage, payload, make_llm=lambda: None, model_id=None, publish=True):
    extracted = pipeline.extract(storage, payload)
    profiled = pipeline.profile(storage, payload, llm=make_llm())
    ranges = pipeline.batches(extracted["fiches"], payload["batch_size"])
    stats = [pipeline.decompose(storage, payload, start, end, llm=make_llm()) for start, end in ranges]
    short = pipeline.report(storage, payload, ranges, profiled, stats, extracted["warnings"], model_id)
    if publish:
        pipeline.publish(storage, payload, short)
    return short


class ValidateTest(unittest.TestCase):
    def test_defaults(self):
        payload = pipeline.validate_request({"client": "clienta"}, ["clienta"], "20261006T010000Z-abc123")
        self.assertEqual(payload["mode"], "record")
        self.assertEqual(payload["batch_size"], pipeline.DEFAULT_BATCH)
        self.assertTrue(payload["with_dictionary"])
        self.assertTrue(payload["enrich"])
        self.assertEqual(payload["llm_parallelism"], pipeline.DEFAULT_LLM_PARALLELISM)

    def test_unknown_client_is_refused_without_listing_the_others(self):
        with self.assertRaises(ValueError) as caught:
            pipeline.validate_request({"client": "other"}, ["clienta", "client-s"], "r1")
        self.assertNotIn("client-s", str(caught.exception))

    def test_bad_values_are_refused(self):
        for body in ({"client": "clienta", "source_prefix": "../x"}, {"client": "clienta", "source_prefix": "/abs"},
                     {"client": "clienta", "mode": "refresh"}, {"client": "clienta", "batch_size": 0},
                     {"client": "clienta", "batch_size": True}, {"client": "clienta", "limit": -1},
                     {"client": "clienta", "with_dictionary": "yes"}, {"client": "clienta", "enrich": "yes"},
                     {"client": "clienta", "llm_parallelism": 0}, {"client": "clienta", "llm_parallelism": 9},
                     {"client": "clienta", "llm_parallelism": True}, {"client": "clienta", "llm_parallelism": "2"},
                     ["clienta"]):
            with self.assertRaises(ValueError, msg=str(body)):
                pipeline.validate_request(body, ["clienta"], "r1")

    def test_a_run_rebuilds_its_routing_tables_and_refreshes_the_catalog_unless_told_otherwise(self):
        payload = pipeline.validate_request({"client": "clienta"}, ["clienta"], "20261006T010000Z-abc123")
        self.assertEqual((payload["routing"], payload["routing_build_id"], payload["catalog"]),
                         (True, "20261006T010000Z-abc123", True))
        payload = pipeline.validate_request({"client": "clienta", "routing": False}, ["clienta"], "r1")
        self.assertEqual((payload["routing"], payload["catalog"]), (False, True))
        # PowerShell sends "false" as a string: refused, never read as true
        for value in ("false", "true", 0, 1, None):
            with self.assertRaisesRegex(ValueError, "routing must be true or false", msg=repr(value)):
                pipeline.validate_request({"client": "clienta", "routing": value}, ["clienta"], "r1")

    def test_batches_cover_everything_once(self):
        self.assertEqual(pipeline.batches(23, 10), [[0, 10], [10, 20], [20, 23]])
        self.assertEqual(pipeline.batches(0, 10), [])


class PipelineTest(unittest.TestCase):
    def payload(self, **overrides):
        body = {"client": "clienta", "source_prefix": "Kbs/", "batch_size": 4, **overrides}
        return pipeline.validate_request(body, ["clienta"], "run-1")

    def test_same_result_as_a_local_run(self):
        fiches, _ = load_folder(DEMO_KB, "clienta")
        local = summarize_kb([Decomposer(profile=build_profile("clienta", fiches)).decompose(f) for f in fiches])

        storage = demo_storage()
        short = run(storage, self.payload())
        self.assertEqual({k: short[k] for k in KEYS}, {k: local[k] for k in KEYS})
        for name in ("runs/run-1/fiches.jsonl", "runs/run-1/profile.json", "runs/run-1/report.md",
                     "runs/run-1/summary.json", "runs/run-1/fiches.decomposed.jsonl", "latest.json"):
            self.assertIsNotNone(storage.read("kecore-clienta", name), name)
        latest = json.loads(storage.read("kecore-clienta", "latest.json"))
        self.assertEqual(latest["run_id"], "run-1")

    def test_latest_is_written_by_publish_only_last(self):
        storage = demo_storage()
        short = run(storage, self.payload(), publish=False)
        self.assertIsNone(storage.read("kecore-clienta", "latest.json"))
        self.assertIsNone(storage.read("kecore-clienta", "runs/run-1/published.json"))
        pipeline.publish(storage, self.payload(), short, {"error": "x"})
        latest = json.loads(storage.read("kecore-clienta", "latest.json"))
        self.assertEqual((latest["run_id"], latest["summary"]["semantic"]), ("run-1", {"error": "x"}))
        self.assertEqual(json.loads(storage.read("kecore-clienta", "runs/run-1/published.json")), {"run_id": "run-1"})

    def test_placeholder_and_empty_fiches_stay_out_of_the_map_but_are_listed(self):
        storage = demo_storage()
        storage.write("kb-clienta", "Kbs/KB0010266 - LIBRE - A REUTILISER.md",
                      "# KB0010266 - LIBRE - A REUTILISER\n\nNuméro libre, à réutiliser pour une nouvelle fiche.\n".encode())
        storage.write("kb-clienta", "Kbs/KB0010267 - Vide.md", "# KB0010267 - Vide\n\nA compléter.\n".encode())
        short = run(storage, self.payload())
        excluded = json.loads(storage.read("kecore-clienta", "runs/run-1/excluded.json"))
        rules = {e["title"]: e["rule"] for e in excluded["excluded"]}
        self.assertEqual(rules, {"KB0010266 - LIBRE - A REUTILISER": "title_pattern", "KB0010267 - Vide": "empty"})
        kept = pipeline.read_jsonl(storage.read("kecore-clienta", "runs/run-1/fiches.decomposed.jsonl"))
        self.assertEqual(len(kept), excluded["kept"])
        self.assertFalse({e["fiche_id"] for e in excluded["excluded"]} & {f["fiche_id"] for f in kept})
        self.assertEqual(short["fiches"], len(kept) + 2)             # the report still describes every fiche
        self.assertEqual(short["exclusion"]["excluded"], 2)
        self.assertIn("## Fiches excluded from the map", storage.read("kecore-clienta", "runs/run-1/report.md").decode())
        graph = json.loads(storage.read("kecore-clienta", "runs/run-1/graph.json"))
        self.assertNotIn("KB0010266", json.dumps(graph))

    def test_a_person_can_force_a_fiche_back_in_and_a_broken_config_fails_the_run(self):
        storage = demo_storage()
        storage.write("kb-clienta", "Kbs/KB0010267 - Vide.md", "# KB0010267 - Vide\n\nA compléter.\n".encode())
        run(storage, self.payload())
        fiche_id = json.loads(storage.read("kecore-clienta", "runs/run-1/excluded.json"))["excluded"][0]["fiche_id"]
        storage.write("kecore-clienta", pipeline.EXCLUSION_CONFIG, json.dumps({"force_include": [fiche_id]}).encode())
        payload = pipeline.validate_request({"client": "clienta", "source_prefix": "Kbs/", "batch_size": 4},
                                            ["clienta"], "run-2")
        short = run(storage, payload)
        self.assertEqual(short["exclusion"]["excluded"], 0)
        storage.write("kecore-clienta", pipeline.EXCLUSION_CONFIG, b'{"min_chars": "deux cents"}')
        with self.assertRaises(ValueError) as caught:
            run(storage, pipeline.validate_request({"client": "clienta", "source_prefix": "Kbs/", "batch_size": 4},
                                                   ["clienta"], "run-3"))
        self.assertIn(pipeline.EXCLUSION_CONFIG, str(caught.exception))

    def test_limit_keeps_the_first_fiches_in_document_order(self):
        storage = demo_storage()
        extracted = pipeline.extract(storage, self.payload(limit=2))
        self.assertEqual(extracted["fiches"], 2)

    def test_replay_from_the_blob_record_calls_nothing_and_matches(self):
        storage = demo_storage()
        inner = FakeLLM({"KB0010001 - Outlook ne démarre plus": OUTLOOK_ANSWER})

        def recorder(mode):
            return lambda: RecordingLLM(inner if mode == "record" else None, mode=mode, model_id="fake-model@test",
                                        store=pipeline.StorageRecordStore(storage, "clienta"))

        recorded = run(storage, self.payload(mode="record"), recorder("record"), "fake-model@test")
        calls_after_record = len(inner.calls)
        self.assertGreater(calls_after_record, 0)
        self.assertTrue(storage.list("kecore-clienta", pipeline.LLM_RECORD_PREFIX))

        replayed = run(storage, self.payload(mode="replay"), recorder("replay"), "fake-model@test")
        self.assertEqual(len(inner.calls), calls_after_record)
        self.assertEqual({k: replayed[k] for k in KEYS}, {k: recorded[k] for k in KEYS})
        self.assertEqual(replayed["llm"]["calls"], 0)
        self.assertEqual(replayed["llm"]["errors"], 0)


VIDE = "KB0233 -  Vide"  # a client-s fiche id: the document name, double space included (runbook 19.18 #1)
SECTION = "## Fiches excluded from the map"


def rendered_code(markdown: str) -> str:
    """The text a CommonMark code span renders (spec 6.1): the backtick fences off, then one space off each
    end when both ends have one and the content is not only spaces."""
    fence = len(markdown) - len(markdown.lstrip("`"))
    assert fence and markdown.endswith("`" * fence), markdown
    inner = markdown[fence:-fence]
    if inner.startswith(" ") and inner.endswith(" ") and inner.strip(" "):
        inner = inner[1:-1]
    return inner


def table_cells(row: str) -> list[str]:
    """A GFM table row's cells as a renderer reads them: split on unescaped pipes, trimmed, then unescaped."""
    return [cell.strip().replace("\\|", "|") for cell in re.split(r"(?<!\\)\|", row)[1:-1]]


class ExclusionConfigTest(unittest.TestCase):
    """exclusion-config.json is written by hand, often from PowerShell 5.1, and its force_include entries are
    copied from a report: a BOM is accepted, UTF-16 is refused with what to do, and what each entry did is
    reported in excluded.json, the run's stats and report.md (runbook 19.18 #5)."""

    def setUp(self):
        self.storage = demo_storage()
        self.storage.write("kb-clienta", f"Kbs/{VIDE}.md", f"# {VIDE}\n\nA compléter.\n".encode())
        self.runs = 0

    def run_once(self):
        self.runs += 1
        payload = pipeline.validate_request({"client": "clienta", "source_prefix": "Kbs/", "batch_size": 4},
                                            ["clienta"], f"run-{self.runs}")
        return run(self.storage, payload)

    def config(self, data: bytes):
        self.storage.write("kecore-clienta", pipeline.EXCLUSION_CONFIG, data)

    def blob(self, name):
        return self.storage.read("kecore-clienta", f"runs/run-{self.runs}/{name}")

    def section(self):
        return self.blob("report.md").decode("utf-8").split(SECTION, 1)[1]

    def refused(self, data: bytes) -> str:
        self.config(data)
        with self.assertRaises(ValueError) as caught:
            pipeline.exclusion_rules(self.storage, "clienta")
        return str(caught.exception)

    def test_report_md_prints_each_excluded_id_exactly_so_a_copy_of_it_forces_the_fiche(self):
        short = self.run_once()
        self.assertEqual(short["exclusion"]["fiche_ids"], [VIDE])
        row = next(line for line in self.section().splitlines() if line.startswith("| `KB0233"))
        copied = rendered_code(table_cells(row)[0])
        self.assertEqual(copied, VIDE)                                    # double space kept
        self.assertIn("copy one into force_include as it is, spaces included", self.section())
        self.config(json.dumps({"force_include": [copied]}).encode())
        short = self.run_once()
        self.assertEqual(short["exclusion"]["forced"], [VIDE])
        self.assertEqual(short["exclusion"]["matched_loosely"], [])

    def test_a_config_saved_with_a_utf8_bom_is_accepted(self):
        # PowerShell 5.1 `Out-File -Encoding utf8`: a BOM in front, CRLF at the end
        text = json.dumps({"force_include": [VIDE, "KB0300 - Réinitialiser"], "min_chars": 40}, ensure_ascii=False)
        self.config(b"\xef\xbb\xbf" + text.encode("utf-8") + b"\r\n")
        self.assertEqual(pipeline.exclusion_rules(self.storage, "clienta").force_include,
                         (VIDE, "KB0300 - Réinitialiser"))
        short = self.run_once()
        self.assertEqual(short["exclusion"]["excluded"], 0)
        self.assertEqual(short["exclusion"]["forced"], [VIDE])
        self.assertEqual(short["exclusion"]["force_include_unmatched"], ["KB0300 - Réinitialiser"])
        self.assertIn("is invalid", self.refused(b"\xef\xbb\xbf{"))          # a BOM excuses no broken JSON

    def test_a_utf16_config_fails_the_run_with_what_to_do(self):
        text = json.dumps({"force_include": [VIDE]}, ensure_ascii=False) + "\r\n"
        expected = f"kecore-clienta/{pipeline.EXCLUSION_CONFIG} is UTF-16: save it as UTF-8"
        for label, data in (("Out-File, little-endian with a BOM", b"\xff\xfe" + text.encode("utf-16-le")),
                            ("big-endian with a BOM", b"\xfe\xff" + text.encode("utf-16-be")),
                            ("little-endian, no BOM", text.encode("utf-16-le")),
                            ("big-endian, no BOM", text.encode("utf-16-be")),
                            ("leading blank line, no BOM", ("\r\n" + text).encode("utf-16-le"))):
            with self.subTest(label):
                self.assertEqual(self.refused(data), expected)
        with self.assertRaises(ValueError) as caught:
            self.run_once()
        self.assertEqual(str(caught.exception), expected)
        self.assertIsNone(self.blob("excluded.json"))                     # report wrote nothing
        self.assertIsNone(self.blob("report.md"))
        self.assertIsNone(self.storage.read("kecore-clienta", "latest.json"))  # and nothing was published

    def test_a_utf32_or_ansi_config_fails_with_what_to_do(self):
        text = json.dumps({"force_include": ["KB0300 - Réinitialiser"]}, ensure_ascii=False)
        for data, says in ((b"\xff\xfe\x00\x00" + text.encode("utf-32-le"), "is UTF-32: save it as UTF-8"),
                           (text.encode("utf-32-be"), "is UTF-32: save it as UTF-8"),
                           (text.encode("cp1252"), "is not UTF-8")):  # Set-Content: the ANSI code page
            with self.subTest(says=says, head=data[:4]):
                message = self.refused(data)
                self.assertIn(says, message)
                self.assertIn(f"kecore-clienta/{pipeline.EXCLUSION_CONFIG}", message)
                self.assertTrue(message.endswith("save it as UTF-8"), message)

    def test_utf8_and_empty_configs_are_not_taken_for_utf16(self):
        for data in (b"", b"{", b"{}", b" {}", b"\xef\xbb\xbf{}", '{"force_include": ["é"]}'.encode()):
            self.assertIsNone(pipeline._wide_unicode(data), data)
        self.config(b"{}")
        self.assertEqual(pipeline.exclusion_rules(self.storage, "clienta"), pipeline.ExclusionRules())
        self.assertIn("is invalid", self.refused(b""))

    def test_a_force_include_copied_with_collapsed_spaces_is_matched_loosely_and_reported(self):
        self.config(json.dumps({"force_include": ["KB0233 - Vide"]}).encode())
        short = self.run_once()
        loosely = [{"entry": "KB0233 - Vide", "fiche_id": VIDE}]
        excluded = json.loads(self.blob("excluded.json"))
        latest = json.loads(self.storage.read("kecore-clienta", "latest.json"))
        for where, report in (("excluded.json", excluded), ("summary.json", json.loads(self.blob("summary.json"))["exclusion"]),
                              ("run output", short["exclusion"]), ("latest.json", latest["summary"]["exclusion"])):
            with self.subTest(where):
                self.assertEqual(report["matched_loosely"], loosely)
                self.assertEqual(report["forced"], [VIDE])
                self.assertEqual(report["force_include_unmatched"], [])
        self.assertEqual(excluded["excluded"], [])
        self.assertEqual(excluded["rules"]["force_include"], ["KB0233 - Vide"])  # the config as written
        kept = [f["fiche_id"] for f in pipeline.read_jsonl(self.blob("fiches.decomposed.jsonl"))]
        self.assertIn(VIDE, kept)                                                 # back in the map
        self.assertIn(VIDE, json.loads(self.blob("graph.json"))["nodes"])
        section = self.section()
        self.assertIn("### force_include entries matched loosely", section)
        self.assertIn("write the exact id instead", section)
        self.assertIn(f"- `KB0233 - Vide` names `{VIDE}`", section)
        self.assertIn(f"### Forced back in by force_include\n\nKept in the map although the rules exclude them:\n\n"
                      f"- `{VIDE}`\n", section)

    def test_an_entry_matching_no_fiche_is_reported_in_excluded_json_the_stats_and_report_md(self):
        unmatched = ["KB9999 - Inconnue", "KB0233 – Vide"]                  # an en dash is not the id's hyphen
        self.config(json.dumps({"force_include": unmatched}, ensure_ascii=False).encode())
        short = self.run_once()
        self.assertEqual(json.loads(self.blob("excluded.json"))["force_include_unmatched"], unmatched)
        self.assertEqual(json.loads(self.blob("summary.json"))["exclusion"]["force_include_unmatched"], unmatched)
        self.assertEqual(short["exclusion"]["force_include_unmatched"], unmatched)
        self.assertEqual(short["exclusion"]["fiche_ids"], [VIDE])                # still excluded
        self.assertEqual((short["exclusion"]["forced"], short["exclusion"]["matched_loosely"]), ([], []))
        section = self.section()
        self.assertIn("### force_include entries matching no fiche", section)
        self.assertIn("- `KB9999 - Inconnue`\n- `KB0233 – Vide`\n", section)
        self.assertIn("The exact ids of the excluded fiches are in the table above.", section)
        self.assertNotIn("### Forced back in", section)

    def test_a_forced_fiche_is_kept_in_the_map_and_listed_as_forced(self):
        self.run_once()
        exact = json.loads(self.blob("excluded.json"))["excluded"][0]["fiche_id"]
        self.assertEqual(exact, VIDE)
        self.config(json.dumps({"force_include": [exact]}).encode())
        short = self.run_once()
        self.assertEqual(short["exclusion"]["excluded"], 0)
        self.assertEqual(short["exclusion"]["forced"], [VIDE])
        self.assertEqual((short["exclusion"]["matched_loosely"], short["exclusion"]["force_include_unmatched"]),
                         ([], []))
        self.assertIn(VIDE, json.loads(self.blob("graph.json"))["nodes"])
        section = self.section()
        self.assertTrue(section.startswith("\n\nNone.\n"), section)
        self.assertIn(f"### Forced back in by force_include\n\nKept in the map although the rules exclude them:\n\n"
                      f"- `{VIDE}`\n", section)
        self.assertNotIn("matched loosely", section)
        self.assertNotIn("matching no fiche", section)

    def test_the_exclusion_outputs_are_deterministic(self):
        self.config(json.dumps({"force_include": ["kb0233 - vide", "KB9999", "KB0010267 - Vide"]}).encode())
        self.run_once()
        first = (self.blob("excluded.json"), self.section())
        self.run_once()
        self.assertEqual((self.blob("excluded.json"), self.section()), first)


class ExclusionSectionTest(unittest.TestCase):
    """report.md writes every id and force_include entry so that what renders is the id itself."""

    ODD = ["KB1 -  a|b", "`tick` id", " padded ", "a `` b", "   ", "ends with `", "tab\there"]

    def test_each_excluded_id_renders_exactly_and_each_row_stays_one_row(self):
        result = ExclusionResult(kept=[], excluded=[Exclusion(i, "Titre |  sur\ndeux lignes", "s.docx", "empty", "d")
                                                    for i in self.ODD])
        section = pipeline._exclusion_section(result)
        rows = [line for line in section.splitlines() if line.startswith("| ") and not line.startswith("| Fiche")]
        self.assertEqual(len(rows), len(self.ODD))
        for fiche_id, row in zip(self.ODD, rows):
            with self.subTest(fiche_id=fiche_id):
                cells = table_cells(row)
                self.assertEqual(len(cells), 4, row)
                self.assertEqual(rendered_code(cells[0]), fiche_id)
                self.assertEqual(cells[1:], ["Titre | sur deux lignes", "empty", "d"])

    def test_force_include_outcomes_render_exactly(self):
        loosely = [{"entry": "a `` b", "fiche_id": " A  `` B "}]
        result = ExclusionResult(kept=[], forced=["KB1 -  a|b", "`tick` id"], matched_loosely=loosely,
                                 force_include_unmatched=[" padded ", "ends with `", ""])
        section = pipeline._exclusion_section(result)
        self.assertTrue(section.startswith(f"\n{SECTION}\n\nNone.\n"))
        bullets = [line[2:] for line in section.splitlines() if line.startswith("- ")]
        forced, (loose,), unmatched = bullets[:2], bullets[2:3], bullets[3:]
        self.assertEqual([rendered_code(b) for b in forced], result.forced)
        entry, fiche_id = loose.split(" names ")
        self.assertEqual((rendered_code(entry), rendered_code(fiche_id)), ("a `` b", " A  `` B "))
        self.assertEqual([rendered_code(b) for b in unmatched[:2]], [" padded ", "ends with `"])
        self.assertEqual(unmatched[2], "*(empty)*")                 # an empty entry is said, not an empty span
        self.assertNotIn("in the table above", section)              # there is no table

    def test_nothing_to_say_is_said_briefly(self):
        self.assertEqual(pipeline._exclusion_section(ExclusionResult(kept=[])), f"\n{SECTION}\n\nNone.\n")


if __name__ == "__main__":
    unittest.main()
