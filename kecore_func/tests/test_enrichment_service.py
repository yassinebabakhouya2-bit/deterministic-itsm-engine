"""Pass A of the enrichment in a kecore run (enrichment_service.py): one proposal file per ranked fiche,
a summary, and a replay that rewrites the same bytes with no model call."""

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for path in (ROOT, ROOT / "kecore_func", Path(__file__).resolve().parent):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import enrichment_service as svc  # noqa: E402
import kecore_pipeline as pipeline  # noqa: E402
import kefind_service as finder  # noqa: E402
from kecore.llm import RecordingLLM  # noqa: E402
from kefind.cards import label_text  # noqa: E402
from kefind.tests.semantic_helpers import TitleLLM  # noqa: E402
from test_kecore_pipeline import demo_storage, run  # noqa: E402

RUN = "run-1"


class EnrichmentRunTest(unittest.TestCase):
    def setUp(self):
        self.storage = demo_storage()
        self.payload = pipeline.validate_request({"client": "clienta", "source_prefix": "Kbs/", "batch_size": 2},
                                                 ["clienta"], RUN)
        run(self.storage, self.payload, publish=False)
        self.kbmap = finder.load_map(self.storage, "clienta", RUN)
        locked = label_text(self.kbmap, "KB0010006")
        self.model = TitleLLM({locked: {
            "canonical_intent": "DEVERROUILLAGE_COMPTE", "intent_label_fr": "Déverrouiller un compte",
            "primary_app": "", "supported_apps": [], "app_evidence": [],
            "semantic_aliases_fr": ["mon compte est bloqué", "compte verrouillé"],
            "semantic_aliases_en": ["account locked"], "trigger_keywords": ["compte", "bloqué"]}})

    def recorder(self, mode):
        return RecordingLLM(self.model if mode == "record" else None, mode=mode, model_id="title-llm@test",
                            store=pipeline.StorageRecordStore(self.storage, "clienta"))

    def build(self, mode):
        payload = {**self.payload, "mode": mode}
        llm = self.recorder(mode)
        count = len(self.kbmap.ranked)
        parts = [svc.enrich(self.storage, payload, start, end, llm=llm) for start, end in pipeline.batches(count, 2)]
        return parts, svc.summarize(self.storage, payload), llm

    def folder(self):
        prefix = svc.paths(RUN)["dir"]
        return {name: self.storage.read("kecore-clienta", name) for name in self.storage.list("kecore-clienta", prefix)}

    def test_one_proposal_per_ranked_fiche_then_a_summary(self):
        parts, summary, llm = self.build("record")
        count = len(self.kbmap.ranked)
        files = self.folder()
        self.assertEqual(len(files), count + 1)                       # proposals + summary.json
        self.assertEqual(summary["fiches"], count)
        self.assertEqual(summary["with_intent"], 1)                   # only the locked-account fiche was answered
        self.assertEqual(sum(p["aliases"] for p in parts), 3)
        index = self.kbmap.ranked.index("KB0010006")
        locked = json.loads(files[f"{svc.paths(RUN)['dir']}{index:05d}.json"])
        self.assertEqual((locked["canonical_intent"], locked["status"]), ("DEVERROUILLAGE_COMPTE", "proposed"))
        self.assertEqual(locked["semantic_aliases_fr"], ["mon compte est bloqué", "compte verrouillé"])
        self.assertEqual(llm.calls, count)

    def test_a_replay_rewrites_the_folder_byte_for_byte_with_no_call(self):
        self.build("record")
        first = self.folder()
        _, _, llm = self.build("replay")
        self.assertEqual((llm.calls, llm.hits), (0, len(self.kbmap.ranked)))
        self.assertEqual(self.folder(), first)

    def test_a_missing_proposal_fails_the_summary_rather_than_hiding_a_fiche(self):
        payload = {**self.payload, "mode": "record"}
        svc.enrich(self.storage, payload, 0, 1, llm=self.recorder("record"))
        with self.assertRaises(FileNotFoundError):
            svc.summarize(self.storage, payload)


if __name__ == "__main__":
    unittest.main()
