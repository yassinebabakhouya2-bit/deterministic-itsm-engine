"""The routes and orchestrators of function_app.py, with Azure replaced by memory stubs.

The Functions decorators are swapped for identity decorators before the import, so each route is
the plain function it wraps. Skipped when azure-functions / azure-functions-durable are not
installed (they are in the Function's own requirements, not needed by the rest of the repo).

Run from the repository root:  python -m unittest discover -s kecore_func/tests
"""

import importlib
import json
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

    def test_no_label_no_scoreboard(self):
        out = drive(self.fa.scoreboard_run, Ctx({"client": "client-s", "sb_id": "s"},
                                                {"scoreboard_prepare": lambda p: {"count": 0}}))
        self.assertEqual(out["error"], "no labeled ticket yet")


import unittest.mock  # noqa: E402  (used by RoutesTest)

if __name__ == "__main__":
    unittest.main()
