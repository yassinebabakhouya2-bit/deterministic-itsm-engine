"""The routes and orchestrators of function_app.py, with Azure replaced by memory stubs.

The Functions decorators are swapped for identity decorators before the import, so each route is
the plain function it wraps. Skipped when azure-functions / azure-functions-durable are not
installed (they are in the Function's own requirements, not needed by the rest of the repo).

Run from the repository root:  python -m unittest discover -s kecore_func/tests
"""

import importlib
import json
import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for path in (ROOT, ROOT / "kecore_func", Path(__file__).resolve().parent):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

try:
    import azure.durable_functions as df
    import azure.functions as func
    import azure.data.tables  # noqa: F401  (kecore_table's constants are imported by some activities)
except ImportError:  # pragma: no cover
    raise unittest.SkipTest("azure-functions / azure-functions-durable / azure-data-tables not installed")

from test_tickets_service import MemoryStorage, MemoryTable, fiche, filler, kb_storage  # noqa: E402


class _IdentityApp:
    def __init__(self, *args, **kwargs):
        pass

    def _identity(self, *args, **kwargs):
        return lambda fn: fn

    route = durable_client_input = orchestration_trigger = activity_trigger = _identity


def load_app():
    real = df.DFApp
    df.DFApp = _IdentityApp
    try:
        sys.modules.pop("function_app", None)
        return importlib.import_module("function_app")
    finally:
        df.DFApp = real


class Ctx:
    """A Durable orchestration context that runs each activity at once."""

    def __init__(self, payload, activities):
        self.payload, self.activities, self.calls = payload, activities, []

    def get_input(self):
        return self.payload

    def call_activity(self, name, payload):
        self.calls.append((name, payload))
        return ("one", name, payload)

    def task_all(self, tasks):
        return ("all", tasks)


def drive(orchestrator, ctx):
    gen = orchestrator(ctx)
    value = None
    try:
        while True:
            task = gen.send(value)
            if task[0] == "one":
                value = ctx.activities[task[1]](task[2])
            else:
                value = [ctx.activities[t[1]](t[2]) for t in task[1]]
    except StopIteration as stop:
        return stop.value


def drive_throwing(orchestrator, ctx):
    """Like ``drive``, but an activity's exception is thrown INTO the orchestrator at its yield, as
    Durable Functions does (so the orchestrator's own try/except is exercised)."""
    gen = orchestrator(ctx)
    value, error = None, None
    try:
        while True:
            task = gen.throw(error) if error is not None else gen.send(value)
            error = None
            try:
                if task[0] == "one":
                    value = ctx.activities[task[1]](task[2])
                else:
                    value = [ctx.activities[t[1]](t[2]) for t in task[1]]
            except Exception as exc:
                error = exc
    except StopIteration as stop:
        return stop.value


def request(method, route, body=None, params=None):
    data = b"" if body is None else (body if isinstance(body, bytes) else json.dumps(body).encode("utf-8"))
    return func.HttpRequest(method=method, url=f"http://localhost/api/{route}", body=data, params=params or {})


class RoutesTest(unittest.TestCase):
    def setUp(self):
        self.fa = load_app()
        self.storage = kb_storage(fiche(), *filler())
        self.tables = {}
        self.fa.storage = lambda: self.storage
        self.fa.table = lambda name=None: self.tables.setdefault(name or "tickets", MemoryTable())
        self.env = unittest.mock.patch.dict("os.environ", {"KECORE_CLIENTS": "client-s,clienta"})
        self.env.start()
        self.fa._configs.clear()

    def tearDown(self):
        self.env.stop()

    def test_a_bad_body_or_an_unknown_client_is_a_400(self):
        for route, handler in (("kecore/tickets/scrub", self.fa.kecore_tickets_scrub),
                               ("kecore/tickets/rescrub", self.fa.kecore_tickets_rescrub),
                               ("kecore/funnel-config/apply", self.fa.kecore_funnel_config_apply)):
            self.assertEqual(handler(request("POST", route, b"not json")).status_code, 400, route)
            self.assertEqual(handler(request("POST", route, {"client": "client-v"})).status_code, 400, route)

    def test_rescrub_cleans_the_stored_rows(self):
        self.fa.table().upsert({"PartitionKey": "client-s", "RowKey": "I1", "resolution": "Vu avec a@b.fr"})
        response = self.fa.kecore_tickets_rescrub(request("POST", "kecore/tickets/rescrub", {"client": "client-s"}))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(json.loads(response.get_body())["changed"], 1)
        self.assertEqual(self.fa.table().get("client-s", "I1")["resolution"], "Vu avec [email]")

    def test_an_unconfirmed_floor_is_a_409_and_nothing_is_written(self):
        self.storage.write("kecore-client-s", "scoreboard/sb1/summary.json",
                           json.dumps({"recommendation": {"min_show": 0.4, "confirmed": False, "reason": "x"}}).encode())
        response = self.fa.kecore_funnel_config_apply(
            request("POST", "kecore/funnel-config/apply", {"client": "client-s", "scoreboard_id": "sb1"}))
        self.assertEqual(response.status_code, 409)
        self.assertIsNone(self.storage.read("kecore-client-s", "funnel-config.json"))

    def test_an_applied_floor_reaches_find_at_once_on_this_instance(self):
        self.storage.write("kecore-client-s", "scoreboard/sb1/summary.json",
                           json.dumps({"kb_run_id": "r1", "interpret": True, "interpret_failures": 0,
                                       "recommendation": {"min_show": 0.99, "confirmed": True,
                                                          "max_wrong": 0.05}}).encode())
        ticket = {"client": "client-s", "text": "Active Directory : locked account", "interpret": False}
        before = json.loads(self.fa.kecore_find(request("POST", "kecore/find", ticket)).get_body())
        self.assertEqual(before["decision"]["kind"], "fiche")
        applied = self.fa.kecore_funnel_config_apply(
            request("POST", "kecore/funnel-config/apply", {"client": "client-s", "scoreboard_id": "sb1"}))
        self.assertEqual(applied.status_code, 200)
        after = json.loads(self.fa.kecore_find(request("POST", "kecore/find", ticket)).get_body())
        self.assertEqual(after["decision"]["kind"], "question")

    def test_latest_scoreboard_is_a_404_before_the_first_run(self):
        response = self.fa.kecore_scoreboard_latest(request("GET", "kecore/scoreboard/latest", params={"client": "client-s"}))
        self.assertEqual(response.status_code, 404)
        bad = self.fa.kecore_scoreboard_latest(request("GET", "kecore/scoreboard/latest", params={"client": "x"}))
        self.assertEqual(bad.status_code, 400)


class Slice5RoutesTest(RoutesTest):
    """GET /kecore/fiche, the dictionary review and the online loop of /kecore/find (V10 slice 5)."""

    def test_a_fiche_comes_with_its_verified_steps_and_its_text(self):
        response = self.fa.kecore_fiche(request("GET", "kecore/fiche", params={"client": "client-s", "fiche_id": "KB0120"}))
        self.assertEqual(response.status_code, 200)
        view = json.loads(response.get_body())
        self.assertEqual((view["fiche_id"], view["run_id"]), ("KB0120", "r1"))
        self.assertTrue(view["steps"] and view["text"])
        unknown = self.fa.kecore_fiche(request("GET", "kecore/fiche", params={"client": "client-s", "fiche_id": "NOPE"}))
        self.assertEqual(unknown.status_code, 404)
        bad = self.fa.kecore_fiche(request("GET", "kecore/fiche", params={"client": "other", "fiche_id": "KB0120"}))
        self.assertEqual(bad.status_code, 400)

    def find(self, observe=None):
        ticket = {"client": "client-s", "text": "L'application Coupa ne démarre plus", "interpret": False}
        if observe is not None:
            ticket["observe"] = observe
        return self.fa.kecore_find(request("POST", "kecore/find", ticket))

    def review(self):
        return json.loads(self.fa.kecore_dictionary(
            request("GET", "kecore/dictionary", params={"client": "client-s"})).get_body())

    def test_live_questions_of_distinct_sessions_feed_the_review_and_a_decision_is_recorded_once(self):
        for session in ("a1", "a1", "a1", "b2", "c3"):
            self.assertEqual(self.find(observe=session * 8).status_code, 200)
        self.assertEqual([r["spelling"] for r in self.review()["ready"]], ["Coupa"])
        decision = {"client": "client-s", "term": "coupa", "accept": True, "by": "Yassine"}
        first = self.fa.kecore_dictionary_decision(request("POST", "kecore/dictionary/decision", decision))
        self.assertEqual(first.status_code, 200)
        again = self.fa.kecore_dictionary_decision(request("POST", "kecore/dictionary/decision", decision))
        self.assertEqual(again.status_code, 409)
        self.assertEqual(self.review()["decisions"]["accepted"], [{"spelling": "Coupa", "canonical": None}])
        unknown = dict(decision, term="sage")
        self.assertEqual(self.fa.kecore_dictionary_decision(
            request("POST", "kecore/dictionary/decision", unknown)).status_code, 404)

    def test_one_session_asking_again_and_again_is_one_observation(self):
        for _ in range(5):
            self.find(observe="ab" * 16)
        self.find()  # no session key: not observed at all
        self.assertEqual(self.review()["ready"], [])
        self.assertEqual(self.review()["watching"], 1)

    def test_answers_are_ascii_json_declaring_utf8(self):
        # Windows PowerShell 5.1 decodes a bare "application/json" as ISO-8859-1: a fiche id with an en dash
        # came back as mojibake and the next call with it was a 404 (runbook 19.8)
        response = self.fa._json({"fiche_id": "KB0163 – Débloquer une URL"})
        self.assertEqual(response.mimetype, "application/json; charset=utf-8")
        body = response.get_body().decode("ascii")  # every non-ASCII character escaped
        self.assertEqual(json.loads(body)["fiche_id"], "KB0163 – Débloquer une URL")
        self.assertEqual(self.fa._error(404, "fiche « x » inconnue").mimetype, "application/json; charset=utf-8")

    def test_find_says_whether_the_ticket_was_interpreted(self):
        from types import SimpleNamespace

        from kecore.llm import LLMError, LLMUsage

        class Model:
            def __init__(self, fail):
                self.fail = fail

            def complete_json(self, system, user, schema, name):
                if self.fail:
                    raise LLMError("timeout")
                return SimpleNamespace(data={"terms": ["account locked"], "application": None}, usage=LLMUsage(),
                                       model="m", cached=False)

        ticket = {"client": "client-s", "text": "Mon compte est bloqué"}
        outcomes = []
        for fail in (False, True):
            with unittest.mock.patch.object(self.fa, "find_llm", lambda client, fail=fail: Model(fail)):
                outcomes.append(json.loads(self.fa.kecore_find(request("POST", "kecore/find", ticket)).get_body())["interpreted"])
        not_asked = json.loads(self.fa.kecore_find(request("POST", "kecore/find", dict(ticket, interpret=False))).get_body())
        self.assertEqual(outcomes + [not_asked["interpreted"]], [True, False, None])

    def test_find_embeds_the_question_when_the_run_has_an_index(self):
        from kefind import semantic as sem
        from kefind.cards import entries_for
        from kefind.tests.semantic_helpers import DIMS, ConceptEmbedder

        kbmap = self.fa.finder.load_map(self.storage, "client-s", "r1")
        index, _ = sem.build(entries_for(kbmap, {}), ConceptEmbedder(), "concept-embed@test", DIMS)
        for name, data in index.to_blobs().items():
            self.storage.write("kecore-client-s", f"runs/r1/{name}", data)
        # an index is used only with its calibration (here: none found, so nothing is shown alone)
        calibration = {"index_sha256": index.sha256, "thresholds": sem.UNCALIBRATED.to_dict()}
        self.storage.write("kecore-client-s", f"runs/r1/{sem.CALIBRATION_BLOB}", json.dumps(calibration).encode())
        self.fa._kb_map.cache_clear()
        seen = {}

        class Query:
            model_id = "concept-embed@test"

            def embed(self, texts):
                seen["texts"] = texts
                return ConceptEmbedder().embed(texts)

        class NoModel:  # the semantic mode interprets nothing: any call is a failure of this test
            def complete_json(self, *args, **kwargs):
                raise AssertionError("the interpretation model was called in semantic mode")

        with unittest.mock.patch.object(self.fa, "query_embedder", lambda client, dims: seen.update(dims=dims) or Query()), \
                unittest.mock.patch.object(self.fa, "find_llm", lambda client: NoModel()):
            answer = json.loads(self.fa.kecore_find(request("POST", "kecore/find", {
                "client": "client-s", "text": "Compte  VERROUILLÉ", "interpret": True})).get_body())
        self.assertEqual((answer["mode"], seen["dims"], seen["texts"]), ("semantic", DIMS, ["compte verrouillé"]))
        self.assertTrue(answer["decision"]["reason"].startswith("semantic_"))

    def test_only_published_runs_are_kept_in_memory(self):
        loads = []
        real = self.fa.finder.load_map
        self.fa._kb_map.cache_clear()
        with unittest.mock.patch.object(self.fa.finder, "load_map",
                                        lambda storage, client, run_id: loads.append(run_id) or real(storage, client, "r1")):
            for _ in range(2):
                self.fa._kb_map("client-s", "r1", "r1")  # the published latest run: read once
            for _ in range(2):
                self.fa._kb_map("client-s", "r-building", "r1")  # still being built: read fresh every time
            self.storage.write("kecore-client-s", "runs/r-old/published.json", b'{"run_id": "r-old"}')
            for _ in range(2):
                self.fa._kb_map("client-s", "r-old", "r1")  # published earlier, pinned by a session: read once
        self.assertEqual(loads, ["r1", "r-building", "r-building", "r-old"])

    def test_the_query_embedder_is_none_when_the_deployment_is_not_configured(self):
        self.fa._embeddings = None
        with unittest.mock.patch.dict("os.environ", {}, clear=True):
            self.assertIsNone(self.fa.query_embedder("client-s", 1024))

    def test_a_bad_session_key_is_a_400(self):
        self.assertEqual(self.find(observe="Session-1").status_code, 400)

    def test_a_broken_pending_table_never_fails_the_answer(self):
        self.fa.table = lambda name=None: (_ for _ in ()).throw(RuntimeError("table down")) if name == "kefindpending" \
            else self.tables.setdefault(name or "tickets", MemoryTable())
        self.assertEqual(self.find(observe="ab" * 16).status_code, 200)

    def test_the_version_route_says_which_commit_was_packaged(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "BUILD_COMMIT")
            with unittest.mock.patch.object(self.fa, "BUILD_COMMIT", path):
                self.assertEqual(json.loads(self.fa.kecore_version(request("GET", "kecore/version")).get_body()),
                                 {"commit": "unknown"})                     # a tree deployed without the script
                with open(path, "w", encoding="utf-8") as f:
                    f.write("7798a72c0ffee\n")
                self.assertEqual(json.loads(self.fa.kecore_version(request("GET", "kecore/version")).get_body()),
                                 {"commit": "7798a72c0ffee"})

    def test_the_next_run_reads_the_review_decisions(self):
        for session in ("a1", "b2", "c3"):
            self.find(observe=session * 8)
        self.fa.kecore_dictionary_decision(request("POST", "kecore/dictionary/decision",
                                                   {"client": "client-s", "term": "coupa", "accept": False}))
        seen = {}
        fake = lambda storage, payload, llm=None, decided=None: seen.update(decided=decided) or {}  # noqa: E731
        with unittest.mock.patch.object(self.fa.pipeline, "profile", fake), \
                unittest.mock.patch.object(self.fa, "make_llm", lambda payload: None):
            self.fa.kecore_profile({"client": "client-s", "with_dictionary": True})
        self.assertEqual(seen["decided"], {"rejected": ["Coupa"], "accepted": []})


class OrchestratorsTest(unittest.TestCase):
    def setUp(self):
        self.fa = load_app()

    def test_the_ticket_run_catalogues_then_runs_every_batch_on_one_map(self):
        ctx = Ctx({"client": "client-s", "run_id": None, "interpret": True, "limit": 60},
                  {"tickets_count": lambda p: 60,
                   "tickets_catalog": lambda p: {"run_id": "r9", "fiches": 3},
                   "tickets_run_batch": lambda p: {"run_id": p["run_id"], "tickets": p["end"] - p["start"], "empty": 0,
                                                   "errors": 0, "kinds": {"fiche": p["end"] - p["start"]}, "reasons": {}}})
        out = drive(self.fa.tickets_run, ctx)
        self.assertEqual((out["tickets"], out["kinds"], out["catalog"]["fiches"]), (60, {"fiche": 60}, 3))
        batches = [(c[1]["start"], c[1]["end"], c[1]["run_id"]) for c in ctx.calls if c[0] == "tickets_run_batch"]
        self.assertEqual(batches, [(0, 25, "r9"), (25, 50, "r9"), (50, 60, "r9")])

    def test_no_ticket_no_run(self):
        out = drive(self.fa.tickets_run, Ctx({"client": "client-s", "limit": 5}, {"tickets_count": lambda p: 0}))
        self.assertIn("error", out)

    def test_the_scoreboard_run_prepares_measures_then_reports(self):
        ctx = Ctx({"client": "client-s", "run_id": None, "interpret": True, "max_wrong": 0.05, "sb_id": "sb1"},
                  {"scoreboard_prepare": lambda p: {"count": 30, "kb_run_id": "r9"},
                   "scoreboard_batch": lambda p: {"records": p["end"] - p["start"]},
                   "scoreboard_report": lambda p: {"ranges": p["ranges"], "kb": p["kb_run_id"]}})
        self.assertEqual(drive(self.fa.scoreboard_run, ctx), {"ranges": [[0, 25], [25, 30]], "kb": "r9"})

    def kecore_activities(self, fail=None, **overrides):
        def activity(name, result):
            def run(p):
                if name == fail:
                    raise RuntimeError(f"{name} failed")
                return result(p) if callable(result) else result
            return run

        names = {
            "kecore_extract": {"fiches": 5, "warnings": []},
            "kecore_profile": {},
            "kecore_decompose": lambda p: {"start": p["start"]},
            "kecore_report": {"run_id": "r1", "fiches": 5},
            "kecore_semantic_plan": {"fiches": 5},
            "kecore_cards": lambda p: {"questions": 10, "dropped": 1, "errors": 0, "calls": 2, "cached": 0},
            "kecore_heldout": lambda p: {"queries": 4, "dropped": 0, "errors": 0, "calls": 2, "cached": 0},
            "kecore_semantic_index": {"sha256": "abc", "entries": 30},
            "kecore_calibrate": {"feasible": True},
            "kecore_enrich": lambda p: {"errors": 0, "aliases": 6, "dropped": 1, "calls": 3, "cached": 1},
            "kecore_enrich_summary": {"fiches": 5, "errors": 0, "app_inferred": ["KB2"]},
            "kecore_publish": lambda p: {**p["summary"], "semantic": p["semantic"]},
            "kecore_catalog": lambda p: {"client": p["client"], "run_id": "r1", "fiches": 5, "removed": 0},
            # the routing run (routing_service.py), inside a kecore run or on its own
            "routing_prepare": lambda p: {"client": p["client"], "run_id": p.get("run_id") or "r1",
                                          "build_id": p["build_id"], "fiches": 5, "taxonomy": None, "needs_b": True,
                                          "needs_c": False},
            "routing_taxonomy": lambda p: {"build_id": p["build_id"], "intents": 3, "error": None},
            "routing_assign": lambda p: {"start": p["start"], "end": p["end"], "errors": 0, "calls": 2, "cached": 1,
                                         "core_aliases": 3, "dropped": 0},
            "routing_assign_summary": {"fiches": 5, "errors": 0, "with_intent": 4},
            "routing_build": {"tables": {"l0_numbers": 5, "l1": 2}, "carried": False, "why_not": "no active tables",
                              "candidate": True},
            "routing_finish": lambda p: {"state": p["outcome"]["state"], "build_id": p["build_id"]},
            "scoreboard_prepare": lambda p: {"count": 30, "kb_run_id": p["run_id"]},
            "scoreboard_batch": lambda p: {"records": p["end"] - p["start"]},
            "scoreboard_report": lambda p: {"sb_id": p["sb_id"], "kb_run_id": p["kb_run_id"]},
            **overrides,
        }
        return {name: activity(name, result) for name, result in names.items()}

    def test_a_kecore_run_publishes_last_after_its_semantic_folder(self):
        ctx = Ctx({"client": "client-s", "run_id": "r1", "batch_size": 4, "semantic": True}, self.kecore_activities())
        out = drive_throwing(self.fa.kecore_run, ctx)
        order = [c[0] for c in ctx.calls]
        self.assertEqual(order[-1], "kecore_publish")
        self.assertLess(order.index("kecore_report"), order.index("kecore_semantic_plan"))
        self.assertLess(order.index("kecore_semantic_index"), order.index("kecore_calibrate"))
        self.assertEqual(out["semantic"]["cards"]["questions"], 20)  # two batches of fiches
        self.assertEqual(out["semantic"]["index"]["sha256"], "abc")

    def test_a_failed_semantic_build_still_publishes_the_run_and_says_why(self):
        ctx = Ctx({"client": "client-s", "run_id": "r1", "batch_size": 4, "semantic": True},
                  self.kecore_activities(fail="kecore_semantic_index"))
        out = drive_throwing(self.fa.kecore_run, ctx)
        self.assertEqual([c[0] for c in ctx.calls][-1], "kecore_publish")
        self.assertIn("kecore_semantic_index failed", out["semantic"]["error"])

    def test_a_run_without_semantic_skips_it(self):
        ctx = Ctx({"client": "client-s", "run_id": "r1", "batch_size": 4, "semantic": False}, self.kecore_activities())
        out = drive_throwing(self.fa.kecore_run, ctx)
        self.assertNotIn("kecore_cards", [c[0] for c in ctx.calls])
        self.assertIsNone(out["semantic"])

    def test_the_enrichment_runs_before_publish_and_lands_in_the_run_summary(self):
        ctx = Ctx({"client": "client-s", "run_id": "r1", "batch_size": 4, "semantic": True, "enrich": True},
                  self.kecore_activities())
        out = drive_throwing(self.fa.kecore_run, ctx)
        order = [c[0] for c in ctx.calls]
        self.assertEqual(order[-1], "kecore_publish")
        self.assertLess(order.index("kecore_calibrate"), order.index("kecore_enrich"))
        self.assertLess(order.index("kecore_enrich"), order.index("kecore_enrich_summary"))
        self.assertEqual(order.count("kecore_enrich"), 2)                     # two batches of fiches
        self.assertEqual(out["enrichment"]["app_inferred"], ["KB2"])
        self.assertEqual(out["enrichment"]["llm"], {"calls": 6, "cached": 2})

    def test_a_failed_enrichment_still_publishes_the_run_with_its_semantic_folder(self):
        ctx = Ctx({"client": "client-s", "run_id": "r1", "batch_size": 4, "semantic": True, "enrich": True},
                  self.kecore_activities(fail="kecore_enrich"))
        out = drive_throwing(self.fa.kecore_run, ctx)
        self.assertEqual([c[0] for c in ctx.calls][-1], "kecore_publish")
        self.assertIn("kecore_enrich failed", out["enrichment"]["error"])
        self.assertEqual(out["semantic"]["index"]["sha256"], "abc")

    def test_a_run_without_enrichment_skips_it_and_its_semantic_folder_does_not_need_it(self):
        ctx = Ctx({"client": "client-s", "run_id": "r1", "batch_size": 4, "semantic": False, "enrich": False},
                  self.kecore_activities())
        out = drive_throwing(self.fa.kecore_run, ctx)
        self.assertFalse({"kecore_enrich", "kecore_enrich_summary", "kecore_semantic_plan"} & {c[0] for c in ctx.calls})
        self.assertIsNone(out["enrichment"])
        ctx = Ctx({"client": "client-s", "run_id": "r1", "batch_size": 4, "semantic": False, "enrich": True},
                  self.kecore_activities())
        self.assertEqual(drive_throwing(self.fa.kecore_run, ctx)["enrichment"]["fiches"], 5)

    def waves(self, payload):
        sizes = []

        class Recording(Ctx):
            def task_all(self, tasks):
                sizes.append([t[1] for t in tasks])
                return super().task_all(tasks)

        ctx = Recording(payload, self.kecore_activities())
        out = drive_throwing(self.fa.kecore_run, ctx)
        return out, sizes, ctx

    def test_a_record_run_calls_the_model_two_batches_at_a_time_by_default(self):
        out, sizes, ctx = self.waves({"client": "client-s", "run_id": "r1", "batch_size": 1, "mode": "record",
                                      "semantic": True, "enrich": True, "llm_parallelism": 2})
        self.assertTrue(sizes and all(len(wave) <= 2 for wave in sizes))
        self.assertEqual(sum(len(w) for w in sizes if w[0] == "kecore_decompose"), 5)   # nothing skipped
        self.assertEqual(sum(len(w) for w in sizes if w[0] == "kecore_enrich"), 5)
        decomposed = [c[1]["start"] for c in ctx.calls if c[0] == "kecore_decompose"]
        self.assertEqual(decomposed, [0, 1, 2, 3, 4])                                   # in range order
        self.assertEqual(out["semantic"]["cards"]["questions"], 50)

    def test_a_replay_runs_every_batch_at_once_and_the_width_is_a_parameter(self):
        _, sizes, _ = self.waves({"client": "client-s", "run_id": "r1", "batch_size": 1, "mode": "replay",
                                  "semantic": False, "enrich": True, "llm_parallelism": 2})
        self.assertEqual([len(w) for w in sizes], [5, 5])
        _, sizes, _ = self.waves({"client": "client-s", "run_id": "r1", "batch_size": 1, "mode": "record",
                                  "semantic": False, "enrich": False, "llm_parallelism": 3})
        self.assertEqual([len(w) for w in sizes], [3, 2])

    def test_offline_runs_wait_their_turn_and_a_live_question_gives_up_quickly(self):
        env = {"KECORE_AOAI_ENDPOINT": "https://acct.openai.azure.com", "KECORE_AOAI_DEPLOYMENT": "gpt-4o"}
        with unittest.mock.patch.dict(os.environ, env), \
                unittest.mock.patch.object(self.fa, "storage", lambda: None), \
                unittest.mock.patch.object(self.fa, "_chat", {}), \
                unittest.mock.patch.object(self.fa, "_embeddings", {}):
            run = self.fa.make_llm({"client": "client-s", "mode": "record"}).inner._client
            live = self.fa.find_llm("client-s").inner._client
            measured = self.fa.find_llm("client-s", live=False).inner._client
            live_vectors = self.fa.query_embedder("client-s", 1024).inner._client
            measured_vectors = self.fa.query_embedder("client-s", 1024, live=False).inner._client
            built_vectors = self.fa.make_build_embedder({"client": "client-s", "mode": "record"}).inner._client
        for client in (run, measured, measured_vectors, built_vectors):
            self.assertEqual((client.throttle_retries, client.retries, client.timeout),
                             (self.fa.BATCH_THROTTLE_RETRIES, 5, 120.0))
        for client in (live, live_vectors):
            self.assertEqual((client.throttle_retries, client.retries, client.timeout), (2, 2, 25.0))

    def test_measurements_use_the_batch_budget_and_find_the_live_one(self):
        seen = []
        fake_llm = lambda client, live=True: seen.append(("llm", live))  # noqa: E731
        fake_vectors = lambda client, dims, live=True: seen.append(("vectors", live))  # noqa: E731
        with unittest.mock.patch.object(self.fa, "find_llm", fake_llm), \
                unittest.mock.patch.object(self.fa, "query_embedder", fake_vectors), \
                unittest.mock.patch.object(self.fa, "storage", lambda: None), \
                unittest.mock.patch.object(self.fa, "table", lambda name=None: None), \
                unittest.mock.patch.object(self.fa.sb_svc, "batch", lambda *a, **k: {}), \
                unittest.mock.patch.object(self.fa.tickets_svc, "run_batch", lambda *a, **k: {}):
            self.fa.scoreboard_batch({"client": "client-s", "interpret": True, "start": 0, "end": 1})
            self.fa.tickets_run_batch({"client": "client-s", "interpret": True, "start": 0, "end": 1, "run_id": "r1"})
        self.assertEqual(seen, [("llm", False), ("vectors", False)] * 2)

    def test_a_run_started_before_llm_parallelism_existed_replays_with_its_original_shape(self):
        out, sizes, _ = self.waves({"client": "client-s", "run_id": "r1", "batch_size": 1, "mode": "record",
                                    "semantic": True, "enrich": True})            # no llm_parallelism: an old input
        self.assertEqual(sorted({len(w) for w in sizes}), [5])
        self.assertEqual(out["semantic"]["cards"]["questions"], 50)

    def test_no_label_no_scoreboard(self):
        out = drive(self.fa.scoreboard_run, Ctx({"client": "client-s", "sb_id": "s"},
                                                {"scoreboard_prepare": lambda p: {"count": 0}}))
        self.assertEqual(out["error"], "no labeled ticket or reference question yet")


class RoutingOrchestrationTest(unittest.TestCase):
    """The routing run on its own (POST kecore/routing/runs) and inside a kecore run, before it publishes."""

    kecore_activities = OrchestratorsTest.kecore_activities

    def setUp(self):
        self.fa = load_app()

    def kecore_payload(self, **overrides):
        payload = {"client": "client-s", "run_id": "r1", "batch_size": 4, "mode": "record", "semantic": False,
                   "enrich": True, "llm_parallelism": 2, "routing": True, "routing_build_id": "r1", "catalog": True}
        return {**payload, **overrides}

    def routing_payload(self, **overrides):
        return {"client": "client-s", "mode": "record", "llm_parallelism": 2, "batch_size": 1, "build_id": "b1",
                "measure": False, "run_id": None, "kind": "manual", **overrides}

    def test_a_kecore_run_rebuilds_the_routing_tables_before_it_publishes_then_refreshes_the_catalog(self):
        ctx = Ctx(self.kecore_payload(), self.kecore_activities())
        out = drive_throwing(self.fa.kecore_run, ctx)
        order = [c[0] for c in ctx.calls]
        self.assertEqual(order[order.index("kecore_enrich_summary") + 1:],
                         ["routing_prepare", "routing_taxonomy", "routing_build", "routing_finish", "kecore_publish",
                          "kecore_catalog"])
        prepare = dict(ctx.calls)["routing_prepare"]
        self.assertEqual((prepare["build_id"], prepare["run_id"], prepare["measure"], prepare["kind"]),
                         ("r1", "r1", False, "kecore"))
        published = dict(ctx.calls)["kecore_publish"]["summary"]
        self.assertEqual(published["routing"]["state"], "completed")                 # in latest.json too
        self.assertEqual((out["routing"]["build_id"], out["routing"]["candidate"]), ("r1", True))
        self.assertEqual(out["catalog"], {"client": "client-s", "run_id": "r1", "fiches": 5, "removed": 0})
        self.assertEqual(dict(ctx.calls)["kecore_catalog"]["client"], "client-s")

    def test_a_failed_routing_step_is_finished_and_the_run_is_still_published(self):
        ctx = Ctx(self.kecore_payload(), self.kecore_activities(fail="routing_build"))
        out = drive_throwing(self.fa.kecore_run, ctx)
        order = [c[0] for c in ctx.calls]
        self.assertEqual(order[-3:], ["routing_finish", "kecore_publish", "kecore_catalog"])
        self.assertEqual(dict(ctx.calls)["routing_finish"]["outcome"],
                         {"state": "failed", "error": "routing_build failed", "run_id": "r1"})
        self.assertEqual((out["routing"]["state"], out["routing"]["error"]), ("failed", "routing_build failed"))
        ctx = Ctx(self.kecore_payload(), self.kecore_activities(fail="routing_finish"))
        out = drive_throwing(self.fa.kecore_run, ctx)
        self.assertEqual(out["routing"], {"error": "routing_finish failed"})
        self.assertEqual([c[0] for c in ctx.calls][-2:], ["kecore_publish", "kecore_catalog"])

    def test_routing_and_catalog_can_be_left_out_and_a_failed_catalog_never_fails_the_run(self):
        ctx = Ctx(self.kecore_payload(routing=False), self.kecore_activities(fail="kecore_catalog"))
        out = drive_throwing(self.fa.kecore_run, ctx)
        self.assertFalse({c[0] for c in ctx.calls} & {"routing_prepare", "routing_build", "routing_finish"})
        self.assertIsNone(dict(ctx.calls)["kecore_publish"]["summary"]["routing"])
        self.assertEqual(out["catalog"], {"error": "kecore_catalog failed"})

    def test_a_kecore_run_started_before_routing_existed_replays_with_its_original_shape(self):
        old = {"client": "client-s", "run_id": "r1", "batch_size": 4, "mode": "record", "semantic": True,
               "enrich": True, "llm_parallelism": 2}                       # no routing / catalog keys: an old input
        ctx = Ctx(old, self.kecore_activities())
        out = drive_throwing(self.fa.kecore_run, ctx)
        order = [c[0] for c in ctx.calls]
        self.assertEqual(order[-1], "kecore_publish")
        self.assertFalse(any(name.startswith("routing_") or name == "kecore_catalog" for name in order))
        self.assertNotIn("routing", dict(ctx.calls)["kecore_publish"]["summary"])
        self.assertNotIn("catalog", out)

    def test_the_catalog_activity_follows_whatever_map_is_published_now(self):
        seen = []
        tables = {"kefindfiches": "fiches table"}
        with unittest.mock.patch.object(self.fa, "storage", lambda: "storage"), \
                unittest.mock.patch.object(self.fa, "table", lambda name=None: tables[name]), \
                unittest.mock.patch.object(self.fa.tickets_svc, "catalog",
                                           lambda storage, table, p: seen.append((storage, table, p)) or {"fiches": 3}):
            out = self.fa.kecore_catalog(self.kecore_payload())
        self.assertEqual(out, {"fiches": 3})
        self.assertEqual(seen, [("storage", "fiches table", {"client": "client-s", "run_id": None})])

    def test_a_routing_run_assigns_in_waves_builds_measures_then_finishes(self):
        prepared = lambda p: {"client": p["client"], "run_id": "r7", "build_id": p["build_id"], "fiches": 5,  # noqa: E731
                              "taxonomy": {"version": 1, "sha256": "t"}, "needs_b": False, "needs_c": True}
        waves = []

        class Recording(Ctx):
            def task_all(self, tasks):
                waves.append([t[1] for t in tasks])
                return super().task_all(tasks)

        ctx = Recording(self.routing_payload(measure=True), self.kecore_activities(routing_prepare=prepared))
        out = drive_throwing(self.fa.routing_run, ctx)
        order = [c[0] for c in ctx.calls]
        self.assertEqual([len(w) for w in waves if w[0] == "routing_assign"], [2, 2, 1])
        self.assertEqual(order[:1] + order[6:9], ["routing_prepare", "routing_assign_summary", "routing_build",
                                                  "scoreboard_prepare"])
        self.assertEqual(order[-1], "routing_finish")
        self.assertNotIn("routing_taxonomy", order)
        # every step after prepare reads the run it resolved
        self.assertEqual({c[1]["run_id"] for c in ctx.calls if c[0].startswith("routing_") and c[0] != "routing_prepare"},
                         {"r7"})
        measure = dict(ctx.calls)["scoreboard_prepare"]
        self.assertEqual((measure["sb_id"], measure["run_id"], measure["interpret"], measure["client"]),
                         ("b1-sb", "r7", True, "client-s"))
        self.assertEqual(dict(ctx.calls)["routing_finish"]["outcome"], {"state": "completed", "error": None,
                                                                         "run_id": "r7"})
        self.assertEqual(out["assign"]["llm"], {"calls": 10, "cached": 5})
        self.assertEqual((out["state"], out["scoreboard"]), ("completed", {"sb_id": "b1-sb", "kb_run_id": "r7"}))

    def test_a_routing_run_refused_at_prepare_only_finishes(self):
        refused = lambda p: {"client": p["client"], "run_id": None, "build_id": p["build_id"], "fiches": 0,  # noqa: E731
                             "taxonomy": None, "needs_b": False, "needs_c": False,
                             "error": "a routing run is in progress (b0, since t)"}
        ctx = Ctx(self.routing_payload(), self.kecore_activities(routing_prepare=refused))
        out = drive_throwing(self.fa.routing_run, ctx)
        self.assertEqual([c[0] for c in ctx.calls], ["routing_prepare", "routing_finish"])
        self.assertEqual((out["state"], out["error"]), ("failed", "a routing run is in progress (b0, since t)"))
        self.assertEqual(dict(ctx.calls)["routing_finish"]["outcome"]["state"], "failed")

    def test_routing_finish_is_called_after_any_failure(self):
        for name in ("routing_prepare", "routing_taxonomy", "routing_build", "scoreboard_batch"):
            ctx = Ctx(self.routing_payload(measure=True), self.kecore_activities(fail=name))
            out = drive_throwing(self.fa.routing_run, ctx)
            self.assertEqual([c[0] for c in ctx.calls][-1], "routing_finish", name)
            self.assertEqual((out["state"], out["error"]), ("failed", f"{name} failed"), name)

    def test_a_measure_with_nothing_to_measure_leaves_the_build_completed(self):
        ctx = Ctx(self.routing_payload(measure=True),
                  self.kecore_activities(scoreboard_prepare=lambda p: {"count": 0}))
        out = drive_throwing(self.fa.routing_run, ctx)
        self.assertEqual(out["state"], "completed")
        self.assertEqual(out["scoreboard"]["error"], "no labeled ticket or reference question yet")


class ModelBudgetTest(unittest.TestCase):
    ENV = {"KECORE_AOAI_ENDPOINT": "https://acct.openai.azure.com", "KECORE_AOAI_DEPLOYMENT": "gpt-4o"}

    def setUp(self):
        self.fa = load_app()

    def clients(self, env):
        with unittest.mock.patch.dict(os.environ, {**self.ENV, **env}), \
                unittest.mock.patch.object(self.fa, "storage", lambda: None), \
                unittest.mock.patch.object(self.fa, "_chat", {}), \
                unittest.mock.patch.object(self.fa, "_intent_chat", {}):
            payload = {"client": "client-s", "mode": "record"}
            return {"run": self.fa.make_llm(payload), "b": self.fa.taxonomy_llm(payload), "c": self.fa.assign_llm(payload),
                    "find": self.fa.find_llm("client-s"), "l2": self.fa.intent_llm("client-s"),
                    "l2_batch": self.fa.intent_llm("client-s", live=False)}

    def test_passes_b_and_c_get_their_budgets_and_may_get_their_own_deployment(self):
        clients = self.clients({})
        config = lambda name: clients[name].inner.config  # noqa: E731
        self.assertEqual((config("b").max_tokens, config("b").timeout_s, config("b").retries), (12000, 600.0, 1))
        self.assertEqual(clients["b"].inner._client.timeout, 600.0)
        self.assertEqual((config("c").max_tokens, config("c").timeout_s, config("c").throttle_retries),
                         (800, 120.0, self.fa.BATCH_THROTTLE_RETRIES))
        self.assertEqual(config("run").max_tokens, 4000)
        self.assertEqual({clients[n].model_id for n in clients}, {"gpt-4o@acct.openai.azure.com"})
        clients = self.clients({"KECORE_AOAI_BATCH_DEPLOYMENT": "gpt-4o-enrich"})
        self.assertEqual({n: clients[n].model_id.split("@")[0] for n in clients},
                         {"run": "gpt-4o", "b": "gpt-4o-enrich", "c": "gpt-4o-enrich", "find": "gpt-4o", "l2": "gpt-4o",
                          "l2_batch": "gpt-4o"})

    def test_the_l2_mapping_is_short_gives_up_fast_live_and_shares_the_find_record(self):
        clients = self.clients({})
        live, batch = clients["l2"], clients["l2_batch"]
        self.assertEqual((live.inner.config.max_tokens, live.inner._client.timeout, live.inner._client.retries,
                          live.inner._client.throttle_retries), (150, 8.0, 0, 0))
        self.assertEqual((batch.inner.config.max_tokens, batch.inner._client.throttle_retries),
                         (150, self.fa.BATCH_THROTTLE_RETRIES))
        self.assertEqual((live.mode, live.store.prefix, live.model_id), ("record", "find-cache/", clients["find"].model_id))

    def test_the_routing_activities_use_their_own_model_clients(self):
        seen = {}
        with unittest.mock.patch.object(self.fa, "storage", lambda: "storage"), \
                unittest.mock.patch.object(self.fa, "taxonomy_llm", lambda p: "B"), \
                unittest.mock.patch.object(self.fa, "assign_llm", lambda p: "C"), \
                unittest.mock.patch.object(self.fa, "table", lambda name=None: {"kefindreview": RowsTable()}[name]), \
                unittest.mock.patch.object(self.fa.routing_svc, "propose_taxonomy",
                                           lambda storage, p, llm: seen.update(b=llm) or {}), \
                unittest.mock.patch.object(self.fa.routing_svc, "assign",
                                           lambda storage, p, s, e, llm: seen.update(c=(s, e, llm)) or {}), \
                unittest.mock.patch.object(self.fa.routing_svc, "build",
                                           lambda storage, rows, p, now: seen.update(rows=rows) or {}):
            payload = {"client": "client-s", "start": 0, "end": 3}
            self.fa.routing_taxonomy(payload)
            self.fa.routing_assign(payload)
            self.fa.routing_build(payload)
        self.assertEqual(seen, {"b": "B", "c": (0, 3, "C"), "rows": [{"RowKey": "x"}]})


class RowsTable:
    def list(self, client, select=None):
        return [{"RowKey": "x"}]


class FakeDurableClient:
    """What durable_client_input hands a starter: records start_new, answers like the check-status response."""

    def __init__(self):
        self.started = []

    async def start_new(self, name, instance_id=None, client_input=None):
        self.started.append((name, instance_id, client_input))
        return instance_id

    def create_check_status_response(self, req, instance_id):
        return func.HttpResponse(json.dumps({"id": instance_id}), status_code=202, mimetype="application/json")


class RoutingRoutesTest(unittest.TestCase):
    """The routing and review routes the Web App's "Revue" tab calls (app/kecore_client.py)."""

    def setUp(self):
        import test_routing_service as fixtures

        self.fx = fixtures
        self.fa = load_app()
        self.storage = fixtures.write_run(fixtures.Storage())
        self.tables = {}
        self.fa.storage = lambda: self.storage
        self.fa.table = lambda name=None: self.tables.setdefault(name or "tickets", MemoryTable())
        self.env = unittest.mock.patch.dict("os.environ", {"KECORE_CLIENTS": "client-s,clienta"})
        self.env.start()
        self.fa._kb_map.cache_clear()
        self.fa._routing_pointers.clear()

    def tearDown(self):
        self.env.stop()
        self.fa._kb_map.cache_clear()

    def call(self, handler, method, route, body=None, params=None):
        response = handler(request(method, route, body, params))
        raw = response.get_body()
        self.assertEqual(response.mimetype, "application/json; charset=utf-8")
        raw.decode("ascii")                                                    # every non-ASCII character escaped
        return response.status_code, json.loads(raw)

    def decision(self, fiche_id, **kwargs):
        return {**self.fx.body(self.storage, fiche_id, **kwargs)}

    def test_every_route_refuses_a_bad_body_or_an_unknown_client(self):
        posts = ((self.fa.kecore_routing_activate, "kecore/routing/activate"),
                 (self.fa.kecore_routing_deactivate, "kecore/routing/deactivate"),
                 (self.fa.kecore_review_decision, "kecore/review/decision"),
                 (self.fa.kecore_review_carry, "kecore/review/carry"),
                 (self.fa.kecore_review_taxonomy, "kecore/review/taxonomy"))
        for handler, route in posts:
            self.assertEqual(handler(request("POST", route, b"not json")).status_code, 400, route)
            self.assertEqual(handler(request("POST", route, {"client": "client-v", "by": "Y"})).status_code, 400, route)
            self.assertEqual(handler(request("POST", route, {"client": ["client-s"]})).status_code, 400, route)
        gets = ((self.fa.kecore_routing_status, "kecore/routing"), (self.fa.kecore_review, "kecore/review"),
                (self.fa.kecore_review_fiche, "kecore/review/fiche"), (self.fa.kecore_review_next, "kecore/review/next"))
        for handler, route in gets:
            self.assertEqual(handler(request("GET", route, params={"client": "client-v"})).status_code, 400, route)
        bad_key = self.fa.kecore_review_fiche(request("GET", "kecore/review/fiche", params={"client": "client-s",
                                                                                             "key": "../x"}))
        self.assertEqual(bad_key.status_code, 400)
        bad_queue = self.fa.kecore_review_next(request("GET", "kecore/review/next", params={"client": "client-s",
                                                                                             "status": "validated"}))
        self.assertEqual(bad_queue.status_code, 400)

    def test_the_review_reads_the_served_runs_fiches(self):
        status, data = self.call(self.fa.kecore_review, "GET", "kecore/review", params={"client": "client-s"})
        self.assertEqual((status, data["run_id"], data["counts"]["routable"]), (200, self.fx.RUN, self.fx.ROUTABLE))
        status, filtered = self.call(self.fa.kecore_review, "GET", "kecore/review",
                                     params={"client": "client-s", "status": "nonsense"})
        self.assertEqual((status, len(filtered["fiches"])), (200, self.fx.ROUTABLE))
        status, data = self.call(self.fa.kecore_review_fiche, "GET", "kecore/review/fiche",
                                 params={"client": "client-s", "key": self.fx.fiche_key(self.fx.WEBEX)})
        self.assertEqual((status, data["fiche_id"]), (200, self.fx.WEBEX))                  # an en dash, escaped
        status, _ = self.call(self.fa.kecore_review_fiche, "GET", "kecore/review/fiche",
                              params={"client": "client-s", "key": "f" * 32})
        self.assertEqual(status, 404)
        status, data = self.call(self.fa.kecore_review_next, "GET", "kecore/review/next",
                                 params={"client": "client-s", "status": "proposed"})
        self.assertEqual((status, data), (200, {"key": self.fx.fiche_key(self.fx.ACCOUNT)}))
        for handler, route in ((self.fa.kecore_review, "kecore/review"), (self.fa.kecore_review_fiche, "kecore/review/fiche"),
                               (self.fa.kecore_review_next, "kecore/review/next")):
            status, _ = self.call(handler, "GET", route, params={"client": "clienta", "key": "f" * 32,
                                                                 "status": "stale"})
            self.assertEqual(status, 404, route)                                             # no map for clienta yet

    def test_a_decision_is_validated_stored_and_refused_with_the_right_status(self):
        route = "kecore/review/decision"
        decision = self.decision(self.fx.TEAMS, aliases=["attribuer ligne teams"])
        status, data = self.call(self.fa.kecore_review_decision, "POST", route, decision)
        self.assertEqual((status, data["decision"]["fiche_id"], data["collisions"]), (200, self.fx.TEAMS, []))
        self.assertEqual(len(self.tables["kefindreview"].list("client-s")), 1)
        status, data = self.call(self.fa.kecore_review_decision, "POST", route, decision)       # seen "" is stale now
        self.assertEqual(status, 409)
        status, data = self.call(self.fa.kecore_review_decision, "POST", route,
                                 self.decision(self.fx.ACCOUNT, aliases=["imprimante étage 3"]))
        self.assertEqual((status, data["error"]), (400, "alias « imprimante étage 3 » : le routage ignore « 3 » : "
                                                        "écrire l'alias sans, ou avec le mot entier"))
        status, _ = self.call(self.fa.kecore_review_decision, "POST", route, dict(decision, fiche_id=self.fx.OLD_VPN))
        self.assertEqual(status, 404)

    def test_carry_and_freeze(self):
        status, data = self.call(self.fa.kecore_review_carry, "POST", "kecore/review/carry",
                                 {"client": "client-s", "by": "Y", "keys": [self.fx.fiche_key(self.fx.TEAMS)]})
        self.assertEqual((status, data["carried"], len(data["failed"])), (200, 0, 1))
        self.assertEqual(self.call(self.fa.kecore_review_carry, "POST", "kecore/review/carry",
                                   {"client": "client-s", "by": "Y", "keys": ["nope"]})[0], 400)
        route = "kecore/review/taxonomy"
        freeze = {"client": "client-s", "by": "Yassine", "source_build": "", "intents": self.fx.FROZEN}
        status, data = self.call(self.fa.kecore_review_taxonomy, "POST", route, freeze)
        self.assertEqual((status, data["version"]), (200, 1))
        status, data = self.call(self.fa.kecore_review_taxonomy, "POST", route,
                                 dict(freeze, intents=[{"id": "X_Y_Z", "label_fr": ""}]))
        self.assertEqual((status, data["error"]), (400, "entrée 1 (X_Y_Z) : libellé manquant"))
        decision = self.decision(self.fx.TEAMS, intent="ATTRIBUER_LIGNE")
        self.assertEqual(self.call(self.fa.kecore_review_decision, "POST", "kecore/review/decision", decision)[0], 200)
        status, data = self.call(self.fa.kecore_review_taxonomy, "POST", route, dict(freeze, intents=self.fx.FROZEN[1:]))
        self.assertEqual((status, data["orphans"]), (409, [{"fiche_id": self.fx.TEAMS, "label": self.fx.TEAMS,
                                                              "intent": "ATTRIBUER_LIGNE"}]))
        status, data = self.call(self.fa.kecore_review_taxonomy, "POST", route,
                                 dict(freeze, intents=self.fx.FROZEN[1:], orphans_ok=True))
        self.assertEqual((status, data["version"]), (200, 2))

    def test_a_concurrent_freeze_and_a_damaged_file_have_their_own_status(self):
        fx, route = self.fx, "kecore/review/taxonomy"
        freeze = {"client": "client-s", "by": "Yassine", "source_build": "", "intents": fx.FROZEN}
        self.assertEqual(self.call(self.fa.kecore_review_taxonomy, "POST", route, freeze)[0], 200)
        self.storage.write(fx.CONTAINER, "taxonomy.history/v2.json", b"{}")         # another freeze landed first
        status, data = self.call(self.fa.kecore_review_taxonomy, "POST", route, dict(freeze, intents=fx.FROZEN[:1]))
        self.assertEqual(status, 409)
        self.assertNotIn("orphans", data)                                           # no confirm box for this one
        self.assertIn("une autre version", data["error"])
        status, data = self.call(self.fa.kecore_review_taxonomy, "POST", route, dict(freeze, intents="x"))
        self.assertEqual(status, 400)
        taxonomy = json.loads(self.storage.read(fx.CONTAINER, "taxonomy.json"))
        taxonomy["intents"][0]["label_fr"] = "edited by hand"
        self.storage.write(fx.CONTAINER, "taxonomy.json", json.dumps(taxonomy).encode())
        for handler, method, path, body, params in (
                (self.fa.kecore_review, "GET", "kecore/review", None, {"client": "client-s"}),
                (self.fa.kecore_review_fiche, "GET", "kecore/review/fiche", None,
                 {"client": "client-s", "key": fx.fiche_key(fx.TEAMS)}),
                (self.fa.kecore_review_decision, "POST", "kecore/review/decision", self.decision(fx.TEAMS), None),
                (self.fa.kecore_review_carry, "POST", "kecore/review/carry",
                 {"client": "client-s", "by": "Y", "keys": [fx.fiche_key(fx.TEAMS)]}, None),
                (self.fa.kecore_review_taxonomy, "POST", route, freeze, None)):
            status, data = self.call(handler, method, path, body, params)
            self.assertEqual(status, 500, path)
            self.assertIn("taxonomy.json does not match its sha256", data["error"], path)
        self.assertEqual(self.fa.kecore_review_decision(request("POST", "kecore/review/decision",
                                                                dict(self.decision(fx.TEAMS), client="clienta"))
                                                        ).status_code, 404)                 # no map for clienta yet

    def test_the_routing_status_before_any_run(self):
        status, data = self.call(self.fa.kecore_routing_status, "GET", "kecore/routing", params={"client": "clienta"})
        self.assertEqual((status, data["run_id"], data["active"], data["effective"]), (200, None, None, False))
        self.assertEqual(data["why_not_effective"], "no KB map for this client yet")

    def test_activation_and_deactivation(self):
        fx = self.fx
        fx.routing_run(self.storage, self.fa.table("kefindreview"), "b1")
        self.fa._routing_pointers["client-s"] = (0.0, "cached")
        body = {"client": "client-s", "build_id": "b1", "by": "Yassine"}
        status, data = self.call(self.fa.kecore_routing_activate, "POST", "kecore/routing/activate", body)
        self.assertEqual(status, 409)
        self.assertIn("never measured", data["error"])
        self.assertIn("client-s", self.fa._routing_pointers)                         # a refusal changes nothing
        fx.write_gate(self.storage, "b1")
        status, data = self.call(self.fa.kecore_routing_activate, "POST", "kecore/routing/activate", body)
        self.assertEqual((status, data["active"]["build_id"]), (200, "b1"))
        self.assertNotIn("client-s", self.fa._routing_pointers)                      # this instance at once
        status, data = self.call(self.fa.kecore_routing_status, "GET", "kecore/routing", params={"client": "client-s"})
        self.assertEqual((status, data["effective"], data["killed"]), (200, True, False))
        self.fa._routing_pointers["client-s"] = (0.0, "cached")
        status, data = self.call(self.fa.kecore_routing_deactivate, "POST", "kecore/routing/deactivate",
                                 {"client": "client-s", "by": "Yassine"})
        self.assertEqual((status, data), (200, {"active": None, "killed": True}))
        self.assertNotIn("client-s", self.fa._routing_pointers)
        self.assertEqual(self.call(self.fa.kecore_routing_activate, "POST", "kecore/routing/activate",
                                   dict(body, by=""))[0], 400)

    def test_a_deactivation_that_fails_halfway_still_stops_this_instance(self):
        self.fa._routing_pointers["client-s"] = (0.0, "cached")

        def broken(storage, payload, now):
            raise OSError("blob service unavailable")

        with unittest.mock.patch.object(self.fa.routing_svc, "deactivate", broken):
            status, data = self.call(self.fa.kecore_routing_deactivate, "POST", "kecore/routing/deactivate",
                                     {"client": "client-s", "by": "Yassine"})
        self.assertEqual(status, 500)
        self.assertIn("send it again", data["error"])
        self.assertNotIn("client-s", self.fa._routing_pointers)

    def test_a_routing_run_end_to_end_on_the_real_activities(self):
        fx = self.fx
        reviews = self.fa.table("kefindreview")
        fx.freeze(self.storage, reviews)
        fx.decide(self.storage, reviews, fx.TEAMS, intent="ATTRIBUER_LIGNE", app="teams", aliases=["attribuer ligne teams"])
        names = ("routing_prepare", "routing_taxonomy", "routing_assign", "routing_assign_summary", "routing_build",
                 "routing_finish")
        with unittest.mock.patch.object(self.fa, "taxonomy_llm", lambda p: fx.taxonomy_llm()), \
                unittest.mock.patch.object(self.fa, "assign_llm", lambda p: fx.assign_llm()), \
                unittest.mock.patch.object(self.fa, "_now", lambda: fx.NOW):
            payload = self.fa.routing_svc.validate_routing_request({"client": "client-s", "measure": False,
                                                                    "batch_size": 4}, ["client-s"], "b1")
            ctx = Ctx(payload, {name: getattr(self.fa, name) for name in names})
            out = drive_throwing(self.fa.routing_run, ctx)
        self.assertEqual((out["state"], out["error"], out["run_id"], out["assign"]["fiches"]),
                         ("completed", None, fx.RUN, fx.ROUTABLE))
        self.assertEqual([c[0] for c in ctx.calls].count("routing_assign"), 4)
        tables = self.fa.routing_svc.load_tables(self.storage, "client-s", "b1")
        self.assertEqual(tables.l2, [{"intent": "ATTRIBUER_LIGNE", "app": "teams", "fiches": [fx.TEAMS]}])
        self.assertEqual(out["tables"], tables.stats)
        self.assertIsNone(fx.blob(self.storage, "routing/running.json"))
        self.assertEqual(fx.blob(self.storage, "routing/latest-build.json")["state"], "completed")
        self.assertEqual(fx.blob(self.storage, "routing/candidate.json")["build_id"], "b1")

    def test_a_routing_run_starts_with_the_build_id_as_instance_id_unless_one_is_running(self):
        import asyncio

        durable = FakeDurableClient()
        with unittest.mock.patch.object(self.fa, "_new_id", lambda: "20261014T120000Z-abc123"):
            response = asyncio.run(self.fa.kecore_routing_start(
                request("POST", "kecore/routing/runs", {"client": "client-s", "measure": False}), durable))
        self.assertEqual(response.status_code, 202)
        name, instance_id, payload = durable.started[0]
        self.assertEqual((name, instance_id, payload["build_id"], payload["measure"]),
                         ("routing_run", "20261014T120000Z-abc123", "20261014T120000Z-abc123", False))
        self.fx.svc.prepare(self.storage, self.fx.payload("b0"), self.fa._now())       # a run in progress
        response = asyncio.run(self.fa.kecore_routing_start(
            request("POST", "kecore/routing/runs", {"client": "client-s"}), durable))
        self.assertEqual(response.status_code, 409)
        self.assertIn("a routing run is in progress (b0", json.loads(response.get_body())["error"])
        self.assertEqual(len(durable.started), 1)
        bad = asyncio.run(self.fa.kecore_routing_start(request("POST", "kecore/routing/runs", {"client": "x"}), durable))
        self.assertEqual(bad.status_code, 400)


class FindRoutingTest(unittest.TestCase):
    """/kecore/find through the router: only tables a person activated for the served run, never when killed,
    never tables that are not this map's; a failing route falls back to today's answer."""

    QUESTION = "comment attribuer une ligne teams"

    def setUp(self):
        import test_routing_service as fixtures

        self.fx = fixtures
        self.fa = load_app()
        self.storage = fixtures.write_run(fixtures.Storage())
        self.tables = {}
        self.fa.storage = lambda: self.storage
        self.fa.table = lambda name=None: self.tables.setdefault(name or "tickets", MemoryTable())
        self.env = unittest.mock.patch.dict("os.environ", {"KECORE_CLIENTS": "client-s,clienta"})
        self.env.start()
        self.forget()

    def tearDown(self):
        self.env.stop()
        self.fa._kb_map.cache_clear()

    def forget(self):
        """A fresh instance: no map, pointer, tables or source hashes in memory."""
        self.fa._kb_map.cache_clear()
        for cache in (self.fa._routing_pointers, self.fa._routing_tables, self.fa._run_sources, self.fa._configs):
            cache.clear()

    def reviews(self):
        return self.fa.table("kefindreview")

    def activate(self, build_id="b1", aliases=("attribuer ligne teams",), **decision):
        if aliases is not None:
            self.fx.decide(self.storage, self.reviews(), self.fx.TEAMS, aliases=list(aliases), **decision)
        self.fx.routing_run(self.storage, self.reviews(), build_id)
        self.fx.write_gate(self.storage, build_id)
        response = self.fa.kecore_routing_activate(request("POST", "kecore/routing/activate",
                                                           {"client": "client-s", "build_id": build_id, "by": "Yassine"}))
        self.assertEqual(response.status_code, 200)

    def find(self, text=QUESTION, **body):
        response = self.fa.kecore_find(request("POST", "kecore/find",
                                               {"client": "client-s", "text": text, "interpret": False, **body}))
        self.assertEqual(response.status_code, 200)
        return json.loads(response.get_body())

    def assert_unrouted(self, answer, routing_error):
        self.assertIsNone(answer["route"])
        if routing_error is None:
            self.assertIsNone(answer["routing_error"])
        else:
            self.assertIn(routing_error, answer["routing_error"])
        self.assertFalse(answer["decision"]["reason"].startswith("route_"))
        self.assertNotEqual(answer["mode"], "routed")

    def test_without_active_tables_find_answers_as_before_with_null_route(self):
        import kefind_service as finder

        answer = self.find()
        self.assert_unrouted(answer, None)
        kbmap = finder.load_map(self.storage, "client-s", self.fx.RUN)
        today = finder.respond(kbmap, {"text": self.QUESTION, "answers": [], "interpret": False},
                               config=finder.funnel_config(self.storage, "client-s"))
        self.assertEqual(answer, json.loads(json.dumps(today)))
        self.assertEqual(list(answer)[-2:], ["route", "routing_error"])
        self.fx.decide(self.storage, self.reviews(), self.fx.TEAMS, aliases=["attribuer ligne teams"])
        self.fx.routing_run(self.storage, self.reviews(), "b1")                # built, measured: still not active
        self.fx.write_gate(self.storage, "b1")
        self.forget()
        self.assert_unrouted(self.find(), None)

    def test_active_tables_on_the_served_run_route_a_validated_alias_exactly(self):
        self.activate()
        answer = self.find()
        route = answer["route"]
        self.assertEqual((route["level"], route["rule"], route["key"], route["fiche_id"], route["build_id"],
                          route["run_id"]), ("L1", "alias", "attribuer ligne teams", self.fx.TEAMS, "b1", self.fx.RUN))
        self.assertEqual(route["tables_sha256"], self.fx.blob(self.storage, "routing/active.json")["tables_sha256"])
        self.assertEqual((answer["mode"], answer["routing_error"], answer["interpreted"]), ("routed", None, None))
        decision = answer["decision"]
        self.assertEqual((decision["kind"], decision["reason"], decision["fiche_id"], decision["designated"]),
                         ("fiche", "route_l1_alias", self.fx.TEAMS, True))
        self.assertEqual((answer["fiche"]["fiche_id"], answer["candidates"][0]["fiche_id"]), (self.fx.TEAMS, self.fx.TEAMS))
        other = self.find("imprimante bourrage papier")                       # no validated route: L3, offered
        self.assertEqual((other["route"]["level"], other["decision"]["kind"]), ("L3", "question"))
        self.assertEqual(other["candidates"][0]["fiche_id"], self.fx.PRINTER)

    def test_the_kill_switch_stops_routing_at_once_on_this_instance(self):
        self.activate()
        self.assertEqual(self.find()["route"]["level"], "L1")
        response = self.fa.kecore_routing_deactivate(request("POST", "kecore/routing/deactivate",
                                                             {"client": "client-s", "by": "Yassine"}))
        self.assertEqual(response.status_code, 200)
        self.assert_unrouted(self.find(), "routing is switched off (by Yassine")

    def test_tables_of_another_run_never_route_the_served_one(self):
        self.activate()
        self.fx.write_run(self.storage, self.fx.RUN2)                          # a kecore run published, no rebuild
        self.forget()
        self.assert_unrouted(self.find(), f"built for run {self.fx.RUN}, not {self.fx.RUN2}")
        pinned = self.find(run_id=self.fx.RUN)                                # a session still on the activated run
        self.assertEqual((pinned["route"]["level"], pinned["run_id"]), ("L1", self.fx.RUN))

    def test_a_carried_over_activation_serves_both_runs(self):
        self.activate()
        self.fx.write_run(self.storage, self.fx.RUN2)
        built = self.fx.routing_run(self.storage, self.reviews(), "b2", now=self.fx.LATER)
        self.assertTrue(built["built"]["carried"])
        self.forget()
        served = self.find()
        self.assertEqual((served["run_id"], served["route"]["build_id"]), (self.fx.RUN2, "b2"))
        pinned = self.find(run_id=self.fx.RUN)
        self.assertEqual((pinned["run_id"], pinned["route"]["build_id"]), (self.fx.RUN, "b1"))

    def test_tables_that_are_not_the_activated_ones_or_not_this_code_s_never_route(self):
        self.activate()
        active = self.fx.blob(self.storage, "routing/active.json")
        self.storage.write(self.fx.CONTAINER, "routing/active.json",
                           json.dumps({**active, "tables_sha256": "0" * 64}).encode("utf-8"))
        self.forget()
        self.assert_unrouted(self.find(), "are not the ones activated (sha256)")
        self.storage.write(self.fx.CONTAINER, "routing/active.json", json.dumps(active).encode("utf-8"))
        self.forget()
        with unittest.mock.patch.object(self.fa.routing_svc, "CODE_VERSION", {**self.fa.routing_svc.CODE_VERSION,
                                                                               "route": 99}):
            self.assert_unrouted(self.find(), "were built for code")
        self.forget()
        self.storage.write(self.fx.CONTAINER, "routing/active.json", json.dumps({**active, "tables_sha256": ""}).encode())
        self.assert_unrouted(self.find(), "names no tables_sha256")
        self.forget()
        self.storage.write(self.fx.CONTAINER, "routing/active.json", json.dumps(active).encode("utf-8"))
        self.storage.write(self.fx.CONTAINER, "routing/builds/b1/tables.json", b'{"client": "client-s"}')
        self.assert_unrouted(self.find(), "do not match their sha256")
        self.storage.write(self.fx.CONTAINER, "routing/active.json", b"{not json")
        self.forget()
        self.assert_unrouted(self.find(), "routing/active.json is not valid JSON")

    def test_tables_built_for_another_state_of_the_knowledge_base_never_route(self):
        self.activate()
        name = f"runs/{self.fx.RUN}/fiches.jsonl"                            # a fiche's text changed under the run
        self.storage.write(self.fx.CONTAINER, name, self.storage.read(self.fx.CONTAINER, name).replace(b"SOURCE", b"EDIT", 1))
        self.forget()
        self.assert_unrouted(self.find(), "another state of the knowledge base")

    def test_a_route_that_raises_answers_again_without_routing(self):
        import kefind.route

        self.activate()
        unrouted = self.find("imprimante bourrage papier")
        with unittest.mock.patch.object(kefind.route, "match_l1", side_effect=RuntimeError("bug")):
            answer = self.find("imprimante bourrage papier")
        self.assert_unrouted(answer, "routing failed (RuntimeError)")
        self.assertEqual(answer["routing_error"], "routing failed (RuntimeError)")
        self.forget()
        with unittest.mock.patch.object(self.fa, "_active_routing", lambda client, run_id, kb_map: (None, None, None)):
            today = self.find("imprimante bourrage papier")
        self.assertEqual({k: v for k, v in answer.items() if k != "routing_error"},
                         {k: v for k, v in today.items() if k != "routing_error"})
        self.assertEqual(unrouted["route"]["level"], "L3")

    def test_reading_the_routing_state_never_fails_an_answer(self):
        self.activate()
        self.forget()
        with unittest.mock.patch.object(self.fa.routing_svc, "active_for", side_effect=OSError("storage down")):
            self.assert_unrouted(self.find(), "routing unavailable (OSError: storage down)")

    def test_the_pointer_is_read_at_most_once_a_minute_and_the_tables_once(self):
        self.activate()
        self.forget()
        svc = self.fa.routing_svc
        clock = [1000.0]
        with unittest.mock.patch.object(svc, "active_for", wraps=svc.active_for) as pointer, \
                unittest.mock.patch.object(svc, "load_tables", wraps=svc.load_tables) as loads, \
                unittest.mock.patch.object(svc, "source_hashes", wraps=svc.source_hashes) as hashes, \
                unittest.mock.patch.object(self.fa.time, "monotonic", lambda: clock[0]):
            for _ in range(3):
                self.assertEqual(self.find()["route"]["level"], "L1")
            self.assertEqual((pointer.call_count, loads.call_count, hashes.call_count), (1, 1, 1))
            # another instance deactivated: this one still routes for at most a minute...
            self.storage.write(self.fx.CONTAINER, "routing/kill.json", json.dumps(
                {"killed": True, "by": "Yassine", "at": self.fx.NOW}).encode("utf-8"))
            clock[0] += 59
            self.assertEqual(self.find()["route"]["level"], "L1")
            clock[0] += 2                                                     # ...then reads the switch again
            self.assert_unrouted(self.find(), "routing is switched off")
            self.assertEqual(pointer.call_count, 2)
            self.storage.write(self.fx.CONTAINER, "routing/kill.json", json.dumps({"killed": False}).encode("utf-8"))
            clock[0] += 61
            self.assertEqual(self.find()["route"]["level"], "L1")
            self.assertEqual((pointer.call_count, loads.call_count, hashes.call_count), (3, 1, 1))   # tables kept

    def l2_tables(self):
        self.fx.freeze(self.storage, self.reviews())
        self.activate(aliases=("attribuer ligne teams",), intent="ATTRIBUER_LIGNE", app="teams")
        tables = self.fa.routing_svc.load_tables(self.storage, "client-s", "b1")
        self.assertEqual(tables.l2, [{"intent": "ATTRIBUER_LIGNE", "app": "teams", "fiches": [self.fx.TEAMS]}])

    def test_l2_maps_the_question_with_the_live_model(self):
        from kefind.tests.schema_llm import SchemaLLM

        self.l2_tables()
        question = "donner un numéro de téléphone sur teams"
        mapping = SchemaLLM({("question_intent_app", question): {"intent": "ATTRIBUER_LIGNE", "app": "teams"}})
        calls = []
        with unittest.mock.patch.object(self.fa, "intent_llm", lambda client, **kw: calls.append((client, kw)) or mapping):
            answer = self.find(question)
            self.assertEqual(self.find()["route"]["level"], "L1")              # L1 decides: no mapping call
        self.assertEqual((answer["route"]["level"], answer["route"]["key"], answer["decision"]["fiche_id"]),
                         ("L2", "ATTRIBUER_LIGNE|teams", self.fx.TEAMS))
        self.assertEqual(calls[0], ("client-s", {}))                           # the live budget (live=True)
        self.assertEqual(mapping.calls, [("question_intent_app", question)])

    def test_l2_without_a_model_client_is_an_l2_failure_never_a_silent_skip(self):
        self.l2_tables()
        with unittest.mock.patch.object(self.fa, "intent_llm", side_effect=KeyError("KECORE_AOAI_ENDPOINT")):
            answer = self.find("donner un numéro de téléphone sur teams")
        self.assertEqual((answer["route"]["level"], answer["route"]["l2"]["reason"]), ("L3", "map_failed"))
        self.assertIn("KECORE_AOAI_ENDPOINT", answer["route"]["l2"]["error"])
        self.assertIsNone(answer["routing_error"])

    def test_tables_without_l2_build_no_mapping_model(self):
        self.activate()
        built = []
        with unittest.mock.patch.object(self.fa, "intent_llm", lambda client, **kw: built.append(client)):
            self.assertEqual(self.find("donner un numéro de téléphone sur teams")["route"]["level"], "L3")
        self.assertEqual(built, [])


class ScoreboardRoutingTest(unittest.TestCase):
    """The scoreboard run measures the pinned tables in every batch, a few batches at a time."""

    def setUp(self):
        self.fa = load_app()

    def waves(self, payload, prepared):
        sizes = []

        class Recording(Ctx):
            def task_all(self, tasks):
                sizes.append(len(tasks))
                return super().task_all(tasks)

        ctx = Recording(payload, {"scoreboard_prepare": lambda p: prepared,
                                  "scoreboard_batch": lambda p: {"records": p["end"] - p["start"]},
                                  "scoreboard_report": lambda p: {"report": p}})
        return drive(self.fa.scoreboard_run, ctx)["report"], sizes, ctx

    def test_every_batch_measures_the_pinned_tables_two_at_a_time(self):
        pin = {"build_id": "b1", "tables_sha256": "t", "content_sha256": "c", "kb_sha256": "k"}
        report, sizes, ctx = self.waves({"client": "client-s", "sb_id": "sb1", "interpret": True, "llm_parallelism": 2},
                                        {"count": 80, "kb_run_id": "r9", "routing": pin})
        self.assertEqual(sizes, [2, 2])                                      # 4 batches of 25, two at a time
        batches = [c[1] for c in ctx.calls if c[0] == "scoreboard_batch"]
        self.assertEqual([(b["start"], b["routing"], b["kb_run_id"]) for b in batches],
                         [(0, pin, "r9"), (25, pin, "r9"), (50, pin, "r9"), (75, pin, "r9")])
        self.assertEqual((report["prepared"]["routing"], report["routing"], len(report["batches"])), (pin, pin, 4))

    def test_an_input_from_before_llm_parallelism_runs_its_batches_at_once_and_pins_nothing_it_lacks(self):
        report, sizes, ctx = self.waves({"client": "client-s", "sb_id": "sb1", "interpret": True},
                                        {"count": 80, "kb_run_id": "r9"})           # an old prepared: no "routing"
        self.assertEqual(sizes, [4])
        self.assertTrue(all(c[1]["routing"] is None for c in ctx.calls if c[0] == "scoreboard_batch"))

    def test_the_batch_maps_questions_for_l2_only_when_tables_are_pinned(self):
        seen = []
        with unittest.mock.patch.object(self.fa, "find_llm", lambda client, live=True: "interpret"), \
                unittest.mock.patch.object(self.fa, "query_embedder", lambda client, dims, live=True: "vectors"), \
                unittest.mock.patch.object(self.fa, "intent_llm", lambda client, live=True: ("l2", live)), \
                unittest.mock.patch.object(self.fa, "storage", lambda: None), \
                unittest.mock.patch.object(self.fa.sb_svc, "batch", lambda *a, **k: seen.append(k) or {}):
            payload = {"client": "client-s", "interpret": True, "start": 0, "end": 1}
            self.fa.scoreboard_batch(payload)
            self.fa.scoreboard_batch({**payload, "routing": {"build_id": "b1"}})
        self.assertEqual(seen, [{"llm": "interpret", "embedder": "vectors", "intent_llm": None},
                                {"llm": "interpret", "embedder": "vectors", "intent_llm": ("l2", False)}])

    def test_a_mapping_model_that_cannot_be_built_fails_each_l2_call_instead(self):
        seen = []
        with unittest.mock.patch.object(self.fa, "find_llm", lambda client, live=True: None), \
                unittest.mock.patch.object(self.fa, "query_embedder", lambda client, dims, live=True: None), \
                unittest.mock.patch.object(self.fa, "intent_llm", side_effect=KeyError("KECORE_AOAI_ENDPOINT")), \
                unittest.mock.patch.object(self.fa, "storage", lambda: None), \
                unittest.mock.patch.object(self.fa.sb_svc, "batch", lambda *a, **k: seen.append(k["intent_llm"]) or {}):
            self.fa.scoreboard_batch({"client": "client-s", "interpret": False, "start": 0, "end": 1,
                                      "routing": {"build_id": "b1"}})
        with self.assertRaises(KeyError):
            seen[0].complete_json("system", "Question: x", {}, "question_intent_app")


class RoutingMeasureEndToEndTest(unittest.TestCase):
    """POST kecore/routing/runs' orchestration on the real activities, measure included: the tables are built,
    measured next to today's engine, their gate written -- then a person activates them and /find routes."""

    def setUp(self):
        import test_routing_service as fixtures
        import test_scoreboard_service as scoreboard_fixtures

        self.fx = fixtures
        self.fa = load_app()
        self.scenario = scoreboard_fixtures.Scenario(build=False, unseen=True)
        self.storage = self.scenario.storage
        self.tables = {"tickets": self.scenario.tickets, "ticketlabels": self.scenario.labels,
                       "refquestions": self.scenario.refs, "kefindreview": self.scenario.reviews,
                       "kecorescores": self.scenario.scores}
        self.fa.storage = lambda: self.storage
        self.fa.table = lambda name=None: self.tables.setdefault(name or "tickets", MemoryTable())
        self.env = unittest.mock.patch.dict("os.environ", {"KECORE_CLIENTS": "client-s"})
        self.env.start()
        self.fa._kb_map.cache_clear()

    def tearDown(self):
        self.env.stop()
        self.fa._kb_map.cache_clear()

    def test_build_measure_activate_then_find_routes(self):
        fx = self.fx
        names = ("routing_prepare", "routing_taxonomy", "routing_assign", "routing_assign_summary", "routing_build",
                 "routing_finish", "scoreboard_prepare", "scoreboard_batch", "scoreboard_report")
        with unittest.mock.patch.object(self.fa, "taxonomy_llm", lambda p: fx.taxonomy_llm()), \
                unittest.mock.patch.object(self.fa, "assign_llm", lambda p: fx.assign_llm()), \
                unittest.mock.patch.object(self.fa, "find_llm", lambda client, live=True: None), \
                unittest.mock.patch.object(self.fa, "intent_llm", side_effect=AssertionError("no L2 tables")), \
                unittest.mock.patch.object(self.fa, "_now", lambda: fx.NOW):
            payload = self.fa.routing_svc.validate_routing_request({"client": "client-s", "batch_size": 4},
                                                                   ["client-s"], "b1")
            ctx = Ctx(payload, {name: getattr(self.fa, name) for name in names})
            out = drive_throwing(self.fa.routing_run, ctx)
        self.assertEqual((out["state"], out["error"], out["scoreboard"]["routing"]),
                         ("completed", None, {"status": "passed", "build_id": "b1", "why_not": None}))
        measured = [c[1] for c in ctx.calls if c[0] == "scoreboard_batch"]
        self.assertEqual({b["routing"]["build_id"] for b in measured}, {"b1"})
        self.assertEqual(out["scoreboard"]["sb_id"], "b1-sb")
        gate = self.scenario.blob(self.scenario.gate_name())
        self.assertEqual((gate["sb_id"], gate["status"]), ("b1-sb", "passed"))
        status = json.loads(self.fa.kecore_routing_status(request("GET", "kecore/routing",
                                                                  params={"client": "client-s"})).get_body())
        self.assertEqual((status["gate"]["status"], status["effective"]), ("passed", False))
        # nothing routes yet: a person activates
        ask = {"client": "client-s", "text": "comment attribuer une ligne teams", "interpret": False}
        self.assertIsNone(json.loads(self.fa.kecore_find(request("POST", "kecore/find", ask)).get_body())["route"])
        response = self.fa.kecore_routing_activate(request("POST", "kecore/routing/activate",
                                                           {"client": "client-s", "build_id": "b1", "by": "Yassine"}))
        self.assertEqual(response.status_code, 200)
        answer = json.loads(self.fa.kecore_find(request("POST", "kecore/find", ask)).get_body())
        self.assertEqual((answer["route"]["level"], answer["decision"]["fiche_id"]), ("L1", fx.TEAMS))


import unittest.mock  # noqa: E402  (used by RoutesTest)

if __name__ == "__main__":
    unittest.main()
