"""The semantic folder of a kecore run, built in Azure (semantic_service.py) and read by /kecore/find
(kefind_service.py): artifacts, sha256 chain, byte-identical replay, semantic and degraded answers."""

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for path in (ROOT, ROOT / "kecore_func", Path(__file__).resolve().parent):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import kecore_pipeline as pipeline  # noqa: E402
import kefind_service as finder  # noqa: E402
import semantic_service as svc  # noqa: E402
from kecore.llm import RecordingEmbeddings, RecordingLLM  # noqa: E402
from kefind.cards import label_text  # noqa: E402
from kefind.tests.semantic_helpers import DIMS, BrokenEmbedder, ConceptEmbedder, TitleLLM  # noqa: E402
from test_kecore_pipeline import demo_storage, run  # noqa: E402

RUN = "run-1"


def answers_for(kbmap):
    """Cards and exam questions for the demo KB, by the label the code shows the model."""
    by_number = {
        "KB0010006": ({"solves_fr": "Débloquer un compte verrouillé", "solves_en": "Unlock a locked account",
                       "questions": ["mon compte est bloqué", "compte verrouillé impossible de me connecter",
                                     "account locked"]},
                      {"messages": ["je n'arrive plus à me connecter, compte bloqué", "compte verrouillé ce matin"]}),
        "KB0010003": ({"solves_fr": "Retirer un bourrage papier de l'imprimante", "solves_en": "Clear a printer jam",
                       "questions": ["l'imprimante est en bourrage", "papier coincé dans l'imprimante", "printer jam"]},
                      {"messages": ["le papier est coincé", "plus moyen d'imprimer, ça bloque"]}),
    }
    cards, exam = {}, {}
    for fiche_id, (card, messages) in by_number.items():
        cards[label_text(kbmap, fiche_id)] = card
        exam[label_text(kbmap, fiche_id)] = messages
    return cards, exam


class SemanticRunTest(unittest.TestCase):
    def setUp(self):
        self.storage = demo_storage()
        self.payload = pipeline.validate_request({"client": "clienta", "source_prefix": "Kbs/", "batch_size": 2},
                                                 ["clienta"], RUN)
        run(self.storage, self.payload, publish=False)
        kbmap = finder.load_map(self.storage, "clienta", RUN)
        cards, exam = answers_for(kbmap)
        self.model = TitleLLM(cards, exam)
        self.embedder = ConceptEmbedder()

    def recorders(self, mode):
        llm = RecordingLLM(self.model if mode == "record" else None, mode=mode, model_id="title-llm@test",
                           store=pipeline.StorageRecordStore(self.storage, "clienta"))
        embedder = RecordingEmbeddings(self.embedder if mode == "record" else None, mode=mode,
                                       model_id="concept-embed@test", dimensions=DIMS,
                                       store=pipeline.StorageRecordStore(self.storage, "clienta", prefix=svc.EMBED_RECORD_PREFIX))
        return llm, embedder

    def build(self, mode):
        payload = {**self.payload, "mode": mode}
        llm, embedder = self.recorders(mode)
        planned = svc.plan(self.storage, payload)
        for start, end in pipeline.batches(planned["fiches"], 2):
            svc.cards(self.storage, payload, start, end, llm=llm)
            svc.heldout(self.storage, payload, start, end, llm=llm)
        built = svc.build_index(self.storage, payload, embedder)
        calibrated = svc.calibrate_run(self.storage, payload, embedder)
        return planned, built, calibrated, llm, embedder

    def blobs(self):
        p = svc.paths(RUN)
        return {k: self.storage.read("kecore-clienta", p[k]) for k in ("index", "vectors", "calibration")}

    def test_the_folder_is_complete_chained_and_replays_byte_for_byte(self):
        planned, built, calibrated, _, _ = self.build("record")
        self.assertEqual(planned["fiches"], 5)
        first = self.blobs()
        self.assertTrue(all(first.values()))
        calibration = json.loads(first["calibration"])
        # the published index is the winning variant, and the calibration names it
        self.assertIn(calibration["variant"], ("code", "balanced", "none"))
        self.assertEqual(calibration["index_sha256"], built["variants"][calibration["variant"]]["sha256"])
        self.assertEqual(set(calibration["variants"]), {"code", "balanced", "none"})
        self.assertEqual(set(calibrated["recall_test"]), {"@1", "@3", "@5"})
        self.assertIn("feasible", calibrated)

        _, _, _, llm, embedder = self.build("replay")  # nothing recorded is missing: zero calls
        self.assertEqual((llm.calls, embedder.calls), (0, 0))
        self.assertEqual(self.blobs(), first)

    def test_find_decides_by_meaning_and_says_so(self):
        self.build("record")
        kbmap = finder.load_map(self.storage, "clienta", RUN)
        self.assertIsNotNone(kbmap.semantic)
        query = RecordingEmbeddings(ConceptEmbedder(), dimensions=DIMS, model_id="concept-embed@test",
                                    store=pipeline.StorageRecordStore(self.storage, "clienta", prefix="find-cache/"))
        answer = finder.respond(kbmap, {"text": "mon compte est bloqué", "answers": [], "interpret": True},
                                llm=None, embedder=query)
        self.assertEqual((answer["mode"], answer["interpreted"]), ("semantic", None))
        self.assertTrue(answer["decision"]["reason"].startswith("semantic_"))
        self.assertEqual(answer["candidates"][0]["fiche_id"], "KB0010006")
        again = finder.respond(kbmap, {"text": "Mon  compte est BLOQUÉ", "answers": [], "interpret": True},
                               llm=None, embedder=query)
        self.assertEqual(again["decision"], answer["decision"])
        self.assertEqual(query.calls, 1)  # the second question is the same text once normalized

    def test_a_question_without_vector_is_decided_by_words_and_flagged(self):
        self.build("record")
        kbmap = finder.load_map(self.storage, "clienta", RUN)
        broken = RecordingEmbeddings(BrokenEmbedder(), dimensions=DIMS, model_id="concept-embed@test",
                                     store=pipeline.StorageRecordStore(self.storage, "clienta", prefix="find-cache/"))
        answer = finder.respond(kbmap, {"text": "mon compte est bloqué", "answers": [], "interpret": False},
                                llm=None, embedder=broken)
        self.assertEqual(answer["mode"], "degraded")
        self.assertIn("embedding failed", answer["semantic_error"])
        self.assertTrue(answer["decision"]["degraded"])
        other_model = RecordingEmbeddings(ConceptEmbedder(), dimensions=DIMS, model_id="another@test",
                                          store=pipeline.StorageRecordStore(self.storage, "clienta", prefix="find-cache/"))
        answer = finder.respond(kbmap, {"text": "mon compte est bloqué", "answers": [], "interpret": False},
                                llm=None, embedder=other_model)
        self.assertEqual(answer["semantic_error"], "the embedding model is not the index's")

    def test_a_half_built_or_damaged_index_is_never_used_and_never_fails_find(self):
        self.build("record")
        p = svc.paths(RUN)
        complete = {k: self.storage.read("kecore-clienta", p[k]) for k in ("vectors", "calibration")}
        cases = {
            "no calibration": lambda: self.storage.blobs.pop(("kecore-clienta", p["calibration"])),
            "no vectors": lambda: self.storage.blobs.pop(("kecore-clienta", p["vectors"])),
            "damaged vectors": lambda: self.storage.write(  # one float changed: the sha256 no longer matches
                "kecore-clienta", p["vectors"], bytes([complete["vectors"][0] ^ 0xFF]) + complete["vectors"][1:]),
        }
        for case, damage in cases.items():
            for name in ("vectors", "calibration"):
                self.storage.write("kecore-clienta", p[name], complete[name])
            damage()
            kbmap = finder.load_map(self.storage, "clienta", RUN)
            self.assertIsNone(kbmap.semantic, case)
            answer = finder.respond(kbmap, {"text": "mon compte est bloqué", "answers": [], "interpret": False})
            self.assertEqual(answer["mode"], "words", case)
            self.assertIn("decided by words", answer["semantic_error"], case)

    def test_the_index_is_written_vectors_first_and_index_json_last(self):
        written = []
        real = self.storage.write
        self.storage.write = lambda c, name, data: written.append(name) or real(c, name, data)
        self.build("record")
        p = svc.paths(RUN)
        served = [n for n in written if n in (p["vectors"], p["index"], p["calibration"])]
        self.assertEqual(served, [p["vectors"], p["index"], p["calibration"]])  # what /find loads: complete or nothing
        variants = [n for n in written if n.startswith(p["variants_dir"])]
        self.assertEqual(len(variants), 6)  # 3 rules x (vectors, index), each vectors first
        self.assertTrue(all(variants[i].endswith("vectors.f32") and variants[i + 1].endswith("index.json")
                            for i in range(0, 6, 2)))
        self.assertLess(written.index(variants[-1]), written.index(p["vectors"]))

    def test_a_run_without_semantic_folder_decides_by_words(self):
        kbmap = finder.load_map(self.storage, "clienta", RUN)
        answer = finder.respond(kbmap, {"text": "mon compte est bloqué", "answers": [], "interpret": False})
        self.assertEqual((answer["mode"], answer["semantic_error"]), ("words", None))


if __name__ == "__main__":
    unittest.main()
