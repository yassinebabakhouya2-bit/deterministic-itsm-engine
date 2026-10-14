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
import scoreboard_service as sb  # noqa: E402
import semantic_service as svc  # noqa: E402
import tickets_service  # noqa: E402
from kecore.llm import LLMError, RecordingEmbeddings, RecordingLLM  # noqa: E402
from kefind import semantic as sem  # noqa: E402
from kefind.cards import label_text  # noqa: E402
from kefind.funnel import fiche_view, find  # noqa: E402
from kefind.interpret import interpret  # noqa: E402
from kefind.tests.helpers import StubLLM  # noqa: E402
from kefind.tests.semantic_helpers import DIMS, BrokenEmbedder, ConceptEmbedder, TitleLLM  # noqa: E402
from test_kecore_pipeline import demo_storage, run  # noqa: E402
from test_tickets_service import MemoryTable  # noqa: E402

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


def respond_before_routing(kbmap, payload, config=None, llm=None, embedder=None) -> dict:
    """kefind_service.respond exactly as it was before routing existed (commit 2231942), frozen here as the
    oracle of /find's JSON: without active tables the answer must stay this, plus "route" and
    "routing_error" (both null)."""
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
    return {
        "client": kbmap.client,
        "run_id": kbmap.run_id,
        "mode": mode,
        "semantic_error": semantic_error,
        "interpreted": interpreted,
        "decision": finding.to_dict(),
        "fiche": fiche_view(kbmap, finding.fiche_id) if finding.fiche_id else None,
        "candidates": [{"fiche_id": f, "label": kbmap.label(f)} for f in finding.fiches],
    }


class RespondWithoutRoutingTest(SemanticRunTest):
    """/find through kefind.route.answer: with no routing tables its JSON is today's, byte for byte, plus
    "route": null and "routing_error" (null, or why tables that exist were not used)."""

    def recorded(self, model_id="concept-embed@test", inner=None):
        return RecordingEmbeddings(inner or ConceptEmbedder(), dimensions=DIMS, model_id=model_id,
                                   store=pipeline.StorageRecordStore(self.storage, "clienta", prefix="find-cache/"))

    def check(self, kbmap, text, answers=(), interpret_ticket=True, llm=None, embedder=None, routing_error=None):
        payload = {"text": text, "answers": list(answers), "interpret": interpret_ticket}
        expected = respond_before_routing(kbmap, payload, llm=llm, embedder=embedder)
        got = finder.respond(kbmap, payload, llm=llm, embedder=embedder, routing_error=routing_error)
        self.assertEqual(list(got), list(expected) + ["route", "routing_error"])
        self.assertEqual(json.dumps({k: got[k] for k in expected}), json.dumps(expected), text)
        self.assertEqual((got["route"], got["routing_error"]), (None, routing_error))
        return got

    def test_a_words_map(self):
        kbmap = finder.load_map(self.storage, "clienta", RUN)
        terms = StubLLM({"terms": ["compte verrouillé", "locked account"], "application": None})
        for llm in (None, terms, StubLLM(LLMError("POST /x -> HTTP 429: quota"))):
            for interpret_ticket in (True, False):
                for text in ("mon compte est bloqué", "KB0010003 papier coincé", "rien à voir avec la KB"):
                    self.check(kbmap, text, interpret_ticket=interpret_ticket, llm=llm)
        self.check(kbmap, "mon compte est bloqué", answers=["app:windows"])
        got = self.check(kbmap, "mon compte est bloqué", routing_error="routing is switched off (by Y at T)")
        self.assertEqual(got["routing_error"], "routing is switched off (by Y at T)")

    def test_a_semantic_map_and_every_degraded_question(self):
        self.build("record")
        kbmap = finder.load_map(self.storage, "clienta", RUN)
        terms = StubLLM({"terms": ["compte verrouillé"], "application": None})
        modes = []
        # the broken embedder first: once a vector is recorded for the question, every later call reads it
        for embedder in (self.recorded(inner=BrokenEmbedder()), None, self.recorded("another@test"), self.recorded()):
            for llm in (None, terms):
                modes.append(self.check(kbmap, "mon compte est bloqué", llm=llm, embedder=embedder)["mode"])
        self.assertEqual(modes, ["degraded"] * 6 + ["semantic"] * 2)


class DegradedMeasureTest(SemanticRunTest):
    """Runbook 19.18 #9: a decision made without the question's vector on a map that decides by meaning is not
    the decision /find makes. The scoreboard counts it (per engine, by reason) and the tickets run marks it."""

    def measure(self, sb_id, embedder):
        tickets, labels, scores = MemoryTable(), MemoryTable(), MemoryTable()
        for ticket_id, text, expected in (("I1", "mon compte est bloqué", "KB0010006"),
                                          ("I2", "papier coincé dans l'imprimante", "KB0010003")):
            tickets.upsert({"PartitionKey": "clienta", "RowKey": ticket_id, "titre": text})
            labels.upsert({"PartitionKey": "clienta", "RowKey": ticket_id, "expected": json.dumps([expected])})
        payload = sb.validate_scoreboard_request({"client": "clienta", "run_id": RUN, "interpret": False},
                                                 ["clienta"], sb_id)
        prepared = sb.prepare(self.storage, tickets, labels, payload)
        payload.update(kb_run_id=prepared["kb_run_id"], routing=prepared["routing"])
        part = sb.batch(self.storage, payload, 0, 2, embedder=embedder)
        short = sb.report(self.storage, scores, payload, [[0, 2]], prepared, [part])
        return short, part, scores.get("clienta", sb_id), sb.latest(self.storage, "clienta")

    def recorded(self, inner, model_id="concept-embed@test"):
        return RecordingEmbeddings(inner, dimensions=DIMS, model_id=model_id,
                                   store=pipeline.StorageRecordStore(self.storage, "clienta", prefix="find-cache/"))

    def test_degraded_decisions_are_counted_by_reason_and_reported(self):
        self.build("record")
        short, part, row, latest = self.measure("sb-none", None)
        self.assertEqual((part["degraded"], short["degraded"], row["degraded_k"]), (2, {"base": 2, "route": None}, 2))
        self.assertEqual(latest["summary"]["degraded"]["reasons"], {sb.ENGINE: {"no_embedder": 2}})
        self.assertIn(f"2 decision(s) of {sb.ENGINE} were degraded (no_embedder: 2)", latest["report_md"])
        records = pipeline.read_jsonl(self.storage.read("kecore-clienta", sb.layout("sb-none")["results"]))
        self.assertEqual({r["degraded"] for r in records}, {"no_embedder"})

        short, part, row, latest = self.measure("sb-broken", self.recorded(BrokenEmbedder()))
        self.assertEqual(latest["summary"]["degraded"]["reasons"], {sb.ENGINE: {"embedding_failed:LLMError": 2}})
        self.assertEqual((part["embed_failures"], part["embed_refused"]), (0, 2))   # "down", not a 5xx: no retry helps

        short, part, row, latest = self.measure("sb-ok", self.recorded(ConceptEmbedder()))
        self.assertEqual((part["degraded"], short["degraded"]["base"], row["degraded_k"]), (0, 0, 0))
        self.assertNotIn("were degraded", latest["report_md"])
        records = pipeline.read_jsonl(self.storage.read("kecore-clienta", sb.layout("sb-ok")["results"]))
        self.assertTrue(all("degraded" not in r for r in records))

    def test_a_transient_embedding_failure_is_counted_as_a_failure(self):
        self.build("record")

        class Throttled:
            model_id = "concept-embed@test"

            def embed(self, texts, dimensions=None):
                raise LLMError("POST /embeddings -> HTTP 429: quota")

        short, part, row, latest = self.measure("sb-429", Throttled())
        self.assertEqual((part["embed_failures"], part["embed_refused"], latest["summary"]["embed_failures"]), (2, 0, 2))
        self.assertEqual(latest["summary"]["degraded"]["reasons"], {sb.ENGINE: {"embedding_failed:LLMError": 2}})

    def test_the_tickets_run_marks_each_ticket(self):
        self.build("record")
        table = MemoryTable()
        table.upsert({"PartitionKey": "clienta", "RowKey": "I1", "titre": "mon compte est bloqué"})
        payload = {"client": "clienta", "run_id": RUN, "interpret": False, "limit": 10}
        for embedder, mark, mode in ((None, "no_embedder", "degraded"),
                                     (self.recorded(ConceptEmbedder(), "another@test"), "model_mismatch", "degraded"),
                                     (self.recorded(BrokenEmbedder()), "embedding_failed:LLMError", "degraded"),
                                     (self.recorded(ConceptEmbedder()), "", "semantic")):
            result = tickets_service.run_batch(self.storage, table, payload, 0, 10, embedder=embedder)
            self.assertEqual((table.get("clienta", "I1")["kefind_degraded"], result["modes"]), (mark, {mode: 1}), mark)
            self.assertFalse(table.get("clienta", "I1")["kefind_interpreted"])


if __name__ == "__main__":
    unittest.main()
