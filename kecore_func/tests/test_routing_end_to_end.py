"""The routing chantier end to end, in memory, through the Function itself: its HTTP routes, its Durable
orchestrators driven step by step, and its REAL activities -- only Azure is replaced (memory blobs and
tables, model fakes behind the real record).

The story, as an operator and a reviewer live it on the demo knowledge base (kecore/examples/kb-demo):

1. POST kecore/runs: a kecore run extracts and decomposes the demo KB, pass A proposes intents and
   aliases, and -- before the run is published -- its routing step proposes a taxonomy (pass B) and builds
   the first tables (L0 numbers; no L1 alias yet: nobody validated one). /find answers exactly as before.
2. The "Revue" tab's routes: the overview, the next fiche to review, the fiche itself, then a decision
   that validates an alias promoted from a reference question and a short alias the reviewer typed.
3. POST kecore/routing/runs: the tables are rebuilt and measured by the scoreboard next to today's engine;
   a handful of questions makes the gate "insufficient".
4. Activation: refused as is, refused without a reason, accepted with a recorded override.
5. /find: L1 for the validated alias, L0 for a cited fiche number, L3 (offered) for the rest -- and never
   the tables on a run they were not built for.
6. Phase 2: the taxonomy frozen as proposed, pass C, the intent validated, a new measure and activation,
   then an L2 answer through one recorded mapping call, shared by the measure and live /find.
7. A later kecore run with unchanged content (a replay: no model call) carries the activation over, the
   previous run still served to sessions pinned on it; a deactivation is sticky against the next carry-over,
   and only a person's activation turns routing back on.

Skipped when azure-functions / azure-functions-durable are not installed (as test_function_app.py).

Run from the repository root:  python -m unittest discover -s kecore_func/tests
"""

import asyncio
import json
import sys
import unittest
import unittest.mock
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for path in (ROOT, ROOT / "kecore_func", Path(__file__).resolve().parent):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

try:
    import azure.durable_functions  # noqa: F401
    import azure.functions  # noqa: F401
    import azure.data.tables  # noqa: F401
except ImportError:  # pragma: no cover
    raise unittest.SkipTest("azure-functions / azure-functions-durable / azure-data-tables not installed")

import kecore_pipeline as pipeline  # noqa: E402
from kecore.llm import RecordingEmbeddings, RecordingLLM  # noqa: E402
from kecore.tests.helpers import DEMO_KB, OUTLOOK_ANSWER  # noqa: E402
from kefind.review import fiche_key  # noqa: E402
from kefind.semantic import query_text  # noqa: E402
from kefind.tests.schema_llm import SchemaLLM  # noqa: E402
from kefind.tests.semantic_helpers import DIMS, ConceptEmbedder  # noqa: E402
from test_function_app import Ctx, FakeDurableClient, drive_throwing, load_app, request  # noqa: E402
from test_routing_service import SdkTable, Storage  # noqa: E402
from test_tickets_service import MemoryTable  # noqa: E402

CLIENT = "clienta"
CONTAINER = "kecore-clienta"
RUN1, RUN2, RUN3, RUN4 = ("20261014T080000Z-aaaaaa", "20261015T080000Z-bbbbbb", "20261016T080000Z-cccccc",
                          "20261017T080000Z-dddddd")
BUILD1, BUILD2, BUILD3, BUILD4 = ("20261014T090000Z-b00001", "20261015T090000Z-b00002", "20261015T100000Z-b00003",
                                  "20261017T090000Z-b00004")

OUTLOOK, VPN, PRINTER, CAMERA, ACCOUNT = "KB0010001", "KB0010002", "KB0010003", "KB0010005", "KB0010006"
ROUTABLE = [OUTLOOK, VPN, PRINTER, ACCOUNT, CAMERA]       # in review order; KB0010020 (a policy) is not ranked

# the reference questions a labeler typed (refquestions), and two labeled tickets
REF_PROFILE = "ref-profil"
REFERENCES = {
    REF_PROFILE: ("Outlook reste bloqué sur le chargement du profil", [OUTLOOK]),   # promoted as an alias
    "ref-imprimante": ("l'imprimante signale un bourrage", [PRINTER]),
    "ref-vpn": ("le vpn globalprotect se coupe souvent", [VPN]),
    "ref-fiche": ("erreur sur la fiche KB0010006", [ACCOUNT]),                       # L0: a fiche number
    "ref-ouverture": ("outlook refuse de s'ouvrir au lancement", [OUTLOOK]),        # L2, once validated
    "ref-ecran": ("commander un nouvel écran", []),                                  # no fiche covers it
}
TICKETS = {"I001": ("Caméra noire pendant une réunion Teams", [CAMERA]),
           "I002": ("Compte Windows verrouillé, impossible de se connecter", [ACCOUNT])}

MANUAL_ALIAS = "outlook ne démarre plus"                  # typed by the reviewer
L1_QUESTION = "Outlook ne démarre plus ce matin"           # one word more than the alias: L1
COMPOUND_QUESTION = "Outlook démarre mais ne synchronise plus"   # two words more: never L1 (L3, offered)
L2_QUESTION = REFERENCES["ref-ouverture"][0]
INTENT = "REPARER_DEMARRAGE_MESSAGERIE"
START = datetime(2026, 10, 14, 8, 0, tzinfo=timezone.utc)

# --- the model fakes -----------------------------------------------------------------------------------------

def _by_title(answers: dict, default: dict):
    """An answer by the "Procedure title: <label>" line: the first keyword of ``answers`` in the label (the
    demo KB's labels come from its file names, whose accents are mojibake: a keyword is robust to that)."""
    def answer(system, user, schema):
        title = SchemaLLM.key_of(user)
        return next((data for word, data in answers.items() if word in title), default)
    return answer


def _pass_a(intent, label, app="", aliases=(), english=()):
    return {"canonical_intent": intent, "intent_label_fr": label, "primary_app": app,
            "supported_apps": [app] if app else [], "app_evidence": [], "semantic_aliases_fr": list(aliases),
            "semantic_aliases_en": list(english), "trigger_keywords": []}


PASS_A = {
    "Outlook": _pass_a("DEMARRAGE_MESSAGERIE", "réparer le démarrage d'Outlook", "outlook",
                       ["outlook bloqué au démarrage"], ["outlook does not start"]),
    "VPN": _pass_a("RECONNEXION_VPN", "rétablir le VPN", "globalprotect", ["le vpn se coupe"]),
    "Imprimante": _pass_a("BOURRAGE_IMPRIMANTE", "débloquer l'imprimante", "", ["bourrage papier imprimante"]),
    "Teams": _pass_a("CAMERA_NOIRE", "réparer la caméra", "teams", ["caméra noire dans teams"]),
    "Compte": _pass_a("DEVERROUILLAGE_COMPTE", "déverrouiller un compte", "active-directory",
                      ["compte windows verrouillé"]),
}
# pass B merges the five pass-A intents (I001..I005, sorted by intent); the camera one is left out by the model
TAXONOMY = {"intents": [
    {"id": INTENT, "label_fr": "Réparer le démarrage de la messagerie",
     "description_fr": "La messagerie ne démarre plus.", "members": ["I003"]},
    {"id": "RETABLIR_CONNEXION_VPN", "label_fr": "Rétablir la connexion VPN", "description_fr": "",
     "members": ["I005"]},
    {"id": "DEBLOQUER_IMPRIMANTE", "label_fr": "Débloquer une imprimante", "description_fr": "", "members": ["I001"]},
    {"id": "DEVERROUILLER_COMPTE", "label_fr": "Déverrouiller un compte", "description_fr": "", "members": ["I004"]},
]}
PASS_C = {
    "Outlook": {"intent": INTENT, "core_aliases_fr": ["réparer démarrage outlook"], "core_aliases_en": []},
    "VPN": {"intent": "RETABLIR_CONNEXION_VPN", "core_aliases_fr": ["vpn se coupe"], "core_aliases_en": []},
    "Imprimante": {"intent": "DEBLOQUER_IMPRIMANTE", "core_aliases_fr": ["bourrage imprimante"], "core_aliases_en": []},
    "Teams": {"intent": "CAMERA_NOIRE", "core_aliases_fr": ["caméra teams noire"], "core_aliases_en": []},
    "Compte": {"intent": "DEVERROUILLER_COMPTE", "core_aliases_fr": ["déverrouiller compte windows"],
               "core_aliases_en": []},
}


# the semantic folder (a run with "semantic": true): cards and exam questions for two fiches, none for the rest
CARDS = {
    "Compte": {"solves_fr": "Débloquer un compte verrouillé", "solves_en": "Unlock a locked account",
               "questions": ["mon compte est bloqué", "compte verrouillé impossible de me connecter",
                             "account locked"]},
    "Imprimante": {"solves_fr": "Retirer un bourrage papier de l'imprimante", "solves_en": "Clear a printer jam",
                   "questions": ["l'imprimante est en bourrage", "papier coincé dans l'imprimante", "printer jam"]},
}
EXAM = {"Compte": {"messages": ["je n'arrive plus à me connecter, compte bloqué", "compte verrouillé ce matin"]},
        "Imprimante": {"messages": ["le papier est coincé", "plus moyen d'imprimer, ça bloque"]}}


def run_model() -> SchemaLLM:
    """Every call a kecore run and a routing run make (llm-cache/): decomposition by article title (the
    Outlook fiche with its verified steps), headings, dictionary, the semantic cards and exam, pass A, pass B,
    pass C."""
    return SchemaLLM({
        "fiche_card": _by_title(CARDS, {"solves_fr": "", "solves_en": "", "questions": []}),
        "heldout_queries": _by_title(EXAM, {"messages": []}),
        "fiche_steps": lambda system, user, schema: (
            OUTLOOK_ANSWER if "Outlook ne démarre plus" in user.split("\n", 1)[0] else {"sections": [], "steps": []}),
        "heading_roles": {"mappings": []},
        "software_dictionary": {"products": []},
        "fiche_enrichment": _by_title(PASS_A, _pass_a("", "")),
        "intent_taxonomy": TAXONOMY,
        "fiche_intent_assignment": _by_title(PASS_C, {"intent": "", "core_aliases_fr": [], "core_aliases_en": []}),
    })


def question_model() -> SchemaLLM:
    """What /find and the scoreboard ask (find-cache/): the interpretation of a ticket (no extra term) and the
    L2 mapping of a question onto the frozen taxonomy -- only the L2 question maps onto an intent."""
    return SchemaLLM({
        "ticket_interpretation": {"terms": [], "application": None},
        ("question_intent_app", query_text(L2_QUESTION)): {"intent": INTENT, "app": "outlook"},
        "question_intent_app": {"intent": "", "app": ""},
    })


class Story:
    """client "clienta" on a fresh Function instance: the demo KB under kb-clienta/Kbs/, the reference questions
    and labeled tickets, every table and blob in memory, a clock that moves forward, the run and build ids the
    starters hand out, and the two model fakes behind the real records (llm-cache/, find-cache/)."""

    def __init__(self, test: unittest.TestCase, semantic: bool = False):
        self.test = test
        self.semantic = semantic
        self.fa = load_app()
        self.storage = Storage()
        for path in sorted(DEMO_KB.rglob("*")):
            if path.is_file():
                self.storage.write("kb-clienta", "Kbs/" + path.relative_to(DEMO_KB).as_posix(), path.read_bytes())
        self.tables = {"kefindreview": SdkTable()}
        self.fa.storage = lambda: self.storage
        self.fa.table = lambda name=None: self.tables.setdefault(name or "tickets", MemoryTable())
        for ref_id, (text, expected) in REFERENCES.items():
            self.table("refquestions").upsert({"PartitionKey": CLIENT, "RowKey": ref_id, "text": text,
                                               "expected": json.dumps(expected)})
        for ticket_id, (text, expected) in TICKETS.items():
            self.table("tickets").upsert({"PartitionKey": CLIENT, "RowKey": ticket_id, "titre": text, "sujet": "",
                                          "description": ""})
            self.table("ticketlabels").upsert({"PartitionKey": CLIENT, "RowKey": ticket_id,
                                               "expected": json.dumps(expected), "skipped": False})
        self.model, self.questions = run_model(), question_model()
        self.vectors = ConceptEmbedder()         # the embedding deployment: the index's vectors and the questions'
        self.query_vectors = True                # False: /find and the scoreboard get no query embedder
        self.minutes = 0
        self.ids: list[str] = []
        self.durable = FakeDurableClient()
        self.fa._kb_map.cache_clear()
        for cache in (self.fa._routing_pointers, self.fa._routing_tables, self.fa._run_sources, self.fa._configs):
            cache.clear()
        patches = [
            unittest.mock.patch.dict("os.environ", {"KECORE_CLIENTS": CLIENT}),
            unittest.mock.patch.object(self.fa, "_now", self.now),
            unittest.mock.patch.object(self.fa, "_new_id", lambda: self.ids.pop(0)),
            unittest.mock.patch.object(self.fa, "model_id", lambda: self.model.model_id),
            unittest.mock.patch.object(self.fa, "make_llm", self.make_llm),
            unittest.mock.patch.object(self.fa, "find_llm", lambda client, live=True: self.recorded(client)),
            unittest.mock.patch.object(self.fa, "intent_llm", lambda client, live=True: self.recorded(client)),
            # the embedding deployment's size (text-embedding-3-large shortened to 1024 in Azure) is the fake's:
            # the index is built, and every question embedded, at that one size, as in production
            unittest.mock.patch.object(self.fa.semantic_svc, "DIMENSIONS", DIMS),
            unittest.mock.patch.object(self.fa, "make_build_embedder", self.build_embedder),
            unittest.mock.patch.object(self.fa, "query_embedder", self.query_embedder),
        ]
        for patch in patches:
            patch.start()
            test.addCleanup(patch.stop)
        test.addCleanup(self.fa._kb_map.cache_clear)

    # --- the Function's surroundings ------------------------------------------------------------------------

    def now(self) -> str:
        """Every activity and route reads a later time (a lease, a pointer's history, a decision's seen): one
        minute later each time, so no lease ever looks dead and every stored time is distinct."""
        self.minutes += 1
        return (START + timedelta(minutes=self.minutes)).isoformat(timespec="seconds")

    def make_llm(self, payload, **overrides):
        """function_app.make_llm: the run's model behind the client's record (llm-cache/), in the run's mode."""
        return RecordingLLM(self.model, mode=payload["mode"], store=pipeline.StorageRecordStore(self.storage, CLIENT))

    def build_embedder(self, payload):
        """function_app.make_build_embedder: the index's vectors behind the client's record (embed-cache/)."""
        return RecordingEmbeddings(self.vectors, mode=payload["mode"], dimensions=self.fa.semantic_svc.DIMENSIONS,
                                   store=pipeline.StorageRecordStore(self.storage, CLIENT, prefix="embed-cache/"))

    def query_embedder(self, client, dimensions, live=True):
        """function_app.query_embedder: a question's vector behind find-cache/, or None (not configured)."""
        if not self.query_vectors:
            return None
        return RecordingEmbeddings(self.vectors, mode="record", dimensions=dimensions,
                                   store=pipeline.StorageRecordStore(self.storage, client, prefix="find-cache/"))

    def recorded(self, client):
        """function_app.find_llm / intent_llm: one record (find-cache/) for live /find and the scoreboard."""
        return RecordingLLM(self.questions, mode="record",
                            store=pipeline.StorageRecordStore(self.storage, client, prefix="find-cache/"))

    def table(self, name):
        return self.fa.table(name)

    def blob(self, name):
        data = self.storage.read(CONTAINER, name)
        return None if data is None else json.loads(data.decode("utf-8"))

    def edit(self, fiche_number, addition):
        """A document of the KB edited at its source (kb-clienta/Kbs/): ``addition`` appended to its text."""
        (name,) = [n for n in self.storage.list("kb-clienta", "Kbs/" + fiche_number)]
        self.storage.write("kb-clienta", name, self.storage.read("kb-clienta", name) + addition.encode("utf-8"))

    def forget(self):
        """Another instance, or this one a minute later: nothing about routing kept in memory."""
        for cache in (self.fa._routing_pointers, self.fa._routing_tables, self.fa._run_sources, self.fa._configs):
            cache.clear()

    # --- the routes, as the Web App and an operator call them -----------------------------------------------

    def call(self, handler, method, route, body=None, params=None, expect=200):
        response = handler(request(method, route, body, params))
        data = json.loads(response.get_body().decode("ascii"))           # ASCII JSON: every accent escaped
        self.test.assertEqual(response.status_code, expect, (route, data))
        return data

    def get(self, handler, route, expect=200, **params):
        return self.call(handler, "GET", route, params={"client": CLIENT, **params}, expect=expect)

    def post(self, handler, route, body, expect=200):
        return self.call(handler, "POST", route, {"client": CLIENT, **body}, expect=expect)

    def start(self, starter, route, body, instance_id):
        """A Durable starter route (202), and the orchestration input it scheduled."""
        self.ids.append(instance_id)
        before = len(self.durable.started)
        response = asyncio.run(starter(request("POST", route, {"client": CLIENT, **body}), self.durable))
        self.test.assertEqual(response.status_code, 202)
        self.test.assertEqual(len(self.durable.started), before + 1)
        return self.durable.started[-1]

    def orchestrate(self, orchestrator, payload, names):
        ctx = Ctx(payload, {name: getattr(self.fa, name) for name in names})
        out = drive_throwing(orchestrator, ctx)
        return out, [name for name, _ in ctx.calls]

    KECORE_STEPS = ("kecore_extract", "kecore_profile", "kecore_decompose", "kecore_report", "kecore_semantic_plan",
                    "kecore_cards", "kecore_heldout", "kecore_semantic_index", "kecore_calibrate",
                    "kecore_enrich", "kecore_enrich_summary", "routing_prepare", "routing_taxonomy", "routing_assign",
                    "routing_assign_summary", "routing_build", "routing_finish", "kecore_publish", "kecore_catalog")
    ROUTING_STEPS = ("routing_prepare", "routing_taxonomy", "routing_assign", "routing_assign_summary", "routing_build",
                     "routing_finish", "scoreboard_prepare", "scoreboard_batch", "scoreboard_report")

    def kecore_run(self, run_id, **body):
        """POST kecore/runs, then the kecore_run orchestration on the real activities."""
        name, _, payload = self.start(self.fa.kecore_start, "kecore/runs",
                                      {"source_prefix": "Kbs/", "batch_size": 2, "semantic": self.semantic, **body},
                                      run_id)
        self.test.assertEqual((name, payload["run_id"]), ("kecore_run", run_id))
        out, steps = self.orchestrate(self.fa.kecore_run, payload, self.KECORE_STEPS)
        self.test.assertEqual(self.blob("latest.json")["run_id"], run_id)          # published, last
        return out, steps

    def routing_run(self, build_id, **body):
        """POST kecore/routing/runs (instance id = build id), then the routing_run orchestration."""
        name, instance_id, payload = self.start(self.fa.kecore_routing_start, "kecore/routing/runs",
                                                {"batch_size": 2, **body}, build_id)
        self.test.assertEqual((name, instance_id, payload["build_id"]), ("routing_run", build_id, build_id))
        out, steps = self.orchestrate(self.fa.routing_run, payload, self.ROUTING_STEPS)
        self.test.assertEqual((out["state"], out["error"]), ("completed", None))
        self.test.assertIsNone(self.blob("routing/running.json"))                  # the lease released
        return out, steps

    def find(self, text, **body):
        return self.post(self.fa.kecore_find, "kecore/find", {"text": text, "interpret": True, **body})

    def review(self, **params):
        return self.get(self.fa.kecore_review, "kecore/review", **params)

    def fiche(self, fiche_id):
        return self.get(self.fa.kecore_review_fiche, "kecore/review/fiche", key=fiche_key(fiche_id))

    def decide(self, view, expect=200, **body):
        """POST kecore/review/decision from the fiche page the reviewer has on screen (its source hash, its seen)."""
        decision = {"by": "Yassine", "fiche_id": view["fiche_id"], "source_sha256": view["source_sha256"],
                    "seen": view["seen"], "status": "validated", "intent": "", "primary_app": "", "aliases": [],
                    "codes": [], "note": "", **body}
        return self.post(self.fa.kecore_review_decision, "kecore/review/decision", decision, expect=expect)

    def routing_status(self):
        return self.get(self.fa.kecore_routing_status, "kecore/routing")

    def activate(self, build_id, expect=200, **body):
        return self.post(self.fa.kecore_routing_activate, "kecore/routing/activate",
                         {"build_id": build_id, "by": "Yassine", **body}, expect=expect)

    # --- the stages ------------------------------------------------------------------------------------------

    def first_run(self):
        """1. The first kecore run: pass A, then (before publish) pass B and the L0-only tables."""
        return self.kecore_run(RUN1)

    def validate_outlook(self):
        """2. The reviewer validates the Outlook fiche: the reference question promoted, plus a typed alias."""
        view = self.fiche(OUTLOOK)
        promoted = next(r for r in view["references"] if r["ref_id"] == REF_PROFILE)
        aliases = [{"text": promoted["text"], "lang": "fr", "source": "reference", "ref_id": REF_PROFILE},
                   {"text": MANUAL_ALIAS, "lang": "fr", "source": "manual", "ref_id": ""}]
        return view, self.decide(view, aliases=aliases)

    def measure_and_activate(self, build_id, reason="5 questions only: the pilot accepts it"):
        """3-4. Rebuild and measure (gate insufficient), then activate with a recorded override."""
        out, steps = self.routing_run(build_id)
        self.test.assertEqual(out["scoreboard"]["routing"]["status"], "insufficient")
        active = self.activate(build_id, accept_insufficient=True, reason=reason)["active"]
        return out, steps, active

    def phase_one(self):
        self.first_run()
        self.validate_outlook()
        return self.measure_and_activate(BUILD1)

    def validate_intent(self):
        """6. After the freeze and pass C: the Outlook fiche validated again, now with its intent and app."""
        view = self.fiche(OUTLOOK)
        aliases = [{k: a[k] for k in ("text", "lang", "source", "ref_id")} for a in view["prefill"]["aliases"]
                   if a["checked"]]
        return view, self.decide(view, intent=view["proposal_c"]["intent"], primary_app="outlook", aliases=aliases)

    def phase_two(self):
        """5-6, as far as an active L2 route on run 2."""
        self.kecore_run(RUN2, routing=False)
        proposal = self.review()["taxonomy"]["proposed"]
        self.post(self.fa.kecore_review_taxonomy, "kecore/review/taxonomy",
                  {"by": "Yassine", "source_build": proposal["build_id"], "intents": proposal["intents"]})
        self.routing_run(BUILD2, measure=False)
        self.validate_intent()
        return self.measure_and_activate(BUILD3, reason="the pilot accepts 6 questions")


class EndToEndTest(unittest.TestCase):
    def test_phase_one_from_a_kecore_run_to_an_activated_l1_route(self):
        story = Story(self)
        # 1. the first kecore run: pass A, then -- before latest.json names the run -- pass B and the first tables
        out, steps = story.first_run()
        self.assertEqual(steps[-6:], ["routing_prepare", "routing_taxonomy", "routing_build", "routing_finish",
                                      "kecore_publish", "kecore_catalog"])
        self.assertNotIn("routing_assign", steps)                                  # no frozen taxonomy: no pass C
        self.assertEqual((out["enrichment"]["fiches"], out["enrichment"]["errors"], out["enrichment"]["with_intent"]),
                         (5, 0, 5))
        routing = out["routing"]
        self.assertEqual((routing["state"], routing["build_id"], routing["run_id"], routing["candidate"]),
                         ("completed", RUN1, RUN1, True))
        self.assertEqual((routing["taxonomy"]["intents"], routing["taxonomy"]["error"]), (5, None))
        self.assertEqual((routing["tables"]["l0_numbers"], routing["tables"]["l1"], routing["tables"]["l2"],
                          routing["tables"]["proposed"]), (5, 0, 0, 5))
        self.assertEqual((routing["carried"], routing["why_not"]), (False, "no active tables"))
        self.assertEqual(out["catalog"]["fiches"], 6)                              # the labeling tab's catalog
        self.assertEqual({row["kb_run"] for row in story.table("kefindfiches").list(CLIENT)}, {RUN1})
        self.assertEqual(story.blob("latest.json")["summary"]["routing"]["state"], "completed")
        self.assertEqual(story.blob("routing/candidate.json")["build_id"], RUN1)
        self.assertEqual(story.blob("routing/taxonomy-proposal.json"),
                         {"build_id": RUN1, "run_id": RUN1, "error": None})
        before = story.find(L1_QUESTION)                                          # nothing activated: today's answer
        self.assertEqual((before["route"], before["routing_error"], before["mode"]), (None, None, "words"))

        # 2. the review: what the tab shows, the next fiche, the fiche, a decision
        overview = story.review()
        self.assertEqual(overview["counts"], {"proposed": 5, "validated": 0, "rejected": 0, "stale": 0, "routable": 5})
        self.assertEqual([f["fiche_id"] for f in overview["fiches"]], ROUTABLE)  # those references expect, first
        self.assertEqual([f["referenced"] for f in overview["fiches"]], [True, True, True, True, False])
        self.assertIsNone(overview["taxonomy"]["frozen"])
        proposed = overview["taxonomy"]["proposed"]
        self.assertEqual(sorted(i["id"] for i in proposed["intents"]),
                         ["CAMERA_NOIRE", "DEBLOQUER_IMPRIMANTE", "DEVERROUILLER_COMPTE", INTENT,
                          "RETABLIR_CONNEXION_VPN"])
        self.assertEqual(next(i for i in proposed["intents"] if i["id"] == "CAMERA_NOIRE")["origin"], "unmapped")
        self.assertEqual(story.get(story.fa.kecore_review_next, "kecore/review/next", status="proposed"),
                         {"key": fiche_key(OUTLOOK)})
        view, decided = story.validate_outlook()
        self.assertEqual((view["status"], view["l0_number"], view["proposal_c"]), ("proposed", "10001", None))
        self.assertEqual(view["proposal_a"]["canonical_intent"], "DEMARRAGE_MESSAGERIE")
        self.assertEqual([a["text"] for a in view["prefill"]["aliases"]], ["outlook bloqué au démarrage",
                                                                         "outlook does not start"])
        self.assertFalse(any(a["checked"] for a in view["prefill"]["aliases"]))  # pass A is only offered
        self.assertEqual([r["ref_id"] for r in view["references"]], ["ref-ouverture", REF_PROFILE])
        self.assertEqual(decided["collisions"], [])
        self.assertEqual((decided["decision"]["revision"], decided["decision"]["run_id"]), (0, RUN1))
        self.assertEqual(sorted((a["source"], a["text"]) for a in decided["decision"]["aliases"]),
                         [("manual", MANUAL_ALIAS), ("reference", REFERENCES[REF_PROFILE][0])])
        story.decide(view, aliases=[], expect=409)                                # seen "" is not the stored decision
        self.assertEqual(story.review()["counts"]["validated"], 1)

        # 3. rebuild and measure: today's engine and the routed one on the same questions, the gate written
        self.assertEqual(story.find(L1_QUESTION)["route"], None)                  # validated is not active
        out, steps = story.routing_run(BUILD1)
        self.assertEqual(steps[:3], ["routing_prepare", "routing_build", "scoreboard_prepare"])  # pass B: once per run
        self.assertEqual(steps[-2:], ["scoreboard_report", "routing_finish"])
        self.assertEqual((out["tables"]["l1"], out["tables"]["validated"], out["carried"]), (2, 1, False))
        measured = out["scoreboard"]
        self.assertEqual((measured["sb_id"], measured["tickets"], measured["routing"]["build_id"]),
                         (f"{BUILD1}-sb", 8, BUILD1))
        self.assertEqual(measured["routing"]["status"], "insufficient")
        self.assertEqual(measured["degraded"], {"base": 0, "route": 0})
        candidate = story.blob("routing/candidate.json")
        gate = story.blob(f"routing/gates/{candidate['content_sha256']}-{RUN1}.json")
        self.assertEqual((gate["sb_id"], gate["status"], gate["build_id"], gate["kb_run_id"]),
                         (f"{BUILD1}-sb", "insufficient", BUILD1, RUN1))
        self.assertEqual((gate["wrong_exact"]["k"], gate["excluded"], gate["seen"]), (0, [REF_PROFILE], [REF_PROFILE]))
        self.assertEqual((gate["compared"], gate["failures"], gate["degraded"]), (6, 0, 0))
        self.assertEqual(gate["route_first"]["k"], gate["base_first"]["k"])      # L3 offers today's fiches first
        self.assertGreaterEqual(gate["routed_exact"], 2)                          # the promoted question, the number
        summary = story.blob(f"scoreboard/{BUILD1}-sb/summary.json")
        rows = {r["id"]: r for r in summary["references"]["rows"]}
        promoted = rows[REF_PROFILE]
        self.assertEqual((promoted["route_level"], promoted["route_shown"], promoted["seen"]), ("L1", OUTLOOK, True))
        self.assertEqual((rows["ref-fiche"]["route_level"], rows["ref-fiche"]["route_shown"]), ("L0", ACCOUNT))
        self.assertEqual(rows["ref-imprimante"]["route_level"], "L3")
        self.assertIn("## Routing gate", story.storage.read(CONTAINER, f"scoreboard/{BUILD1}-sb/report.md").decode())
        self.assertEqual(story.table("kecorescores").get(CLIENT, f"{BUILD1}-sb")["route_status"], "insufficient")
        status = story.routing_status()
        self.assertEqual((status["gate"]["status"], status["effective"], status["candidate"]["build_id"]),
                         ("insufficient", False, BUILD1))

        # 4. activation: refused as it is, refused without a reason, accepted with a recorded override
        refused = story.activate(BUILD1, expect=409)
        self.assertIn("insufficient", refused["error"])
        self.assertIn("accept it explicitly, with a reason", refused["error"])   # what the person can do about it
        self.assertIn("needs a reason", story.activate(BUILD1, accept_insufficient=True, expect=400)["error"])
        self.assertIsNone(story.blob("routing/active.json"))
        reason = "5 questions only: the pilot accepts it"
        active = story.activate(BUILD1, accept_insufficient=True, reason=reason)["active"]
        self.assertEqual((active["build_id"], active["run_id"], active["previous"], active["carried_from"]),
                         (BUILD1, RUN1, None, None))
        self.assertEqual(active["override"], {"by": "Yassine", "reason": reason})
        self.assertEqual(active["gate"]["status"], "insufficient")
        self.assertEqual(story.blob("routing/kill.json")["killed"], False)
        status = story.routing_status()
        self.assertEqual((status["effective"], status["killed"], status["active"]["build_id"]), (True, False, BUILD1))

        # 5. /find: L1 for the validated alias, L0 for a cited number, L3 offered for the rest
        answer = story.find(L1_QUESTION)
        route = answer["route"]
        self.assertEqual((route["level"], route["rule"], route["key"], route["fiche_id"], route["build_id"],
                          route["run_id"]), ("L1", "alias", MANUAL_ALIAS, OUTLOOK, BUILD1, RUN1))
        self.assertEqual((answer["mode"], answer["routing_error"], answer["interpreted"]), ("routed", None, None))
        self.assertEqual((answer["decision"]["kind"], answer["decision"]["reason"], answer["decision"]["designated"]),
                         ("fiche", "route_l1_alias", True))
        self.assertEqual(answer["fiche"]["fiche_id"], OUTLOOK)
        self.assertTrue(answer["fiche"]["steps"])                                 # the verified steps follow
        number = story.find("erreur sur la fiche KB0010006")
        self.assertEqual((number["route"]["level"], number["route"]["key"], number["decision"]["fiche_id"]),
                         ("L0", "10006", ACCOUNT))
        compound = story.find(COMPOUND_QUESTION)                                  # more than the alias says
        self.assertEqual((compound["route"]["level"], compound["decision"]["kind"]), ("L3", "question"))
        self.assertEqual(compound["route"]["l1"]["uncovered"], 2)
        self.assertIsNone(compound["decision"]["fiche_id"])

    def test_tables_never_serve_a_run_they_were_not_built_for(self):
        story = Story(self)
        story.phase_one()
        # a kecore run published without rebuilding the tables ("routing": false): its questions answer as before
        out, steps = story.kecore_run(RUN2, routing=False)
        self.assertFalse([step for step in steps if step.startswith("routing_")])
        self.assertIsNone(out["routing"])
        answer = story.find(L1_QUESTION)
        self.assertEqual((answer["run_id"], answer["route"], answer["mode"]), (RUN2, None, "words"))
        self.assertEqual(answer["routing_error"], f"the active routing tables were built for run {RUN1}, not {RUN2}")
        pinned = story.find(L1_QUESTION, run_id=RUN1)                         # a session still on the activated run
        self.assertEqual((pinned["run_id"], pinned["route"]["level"], pinned["route"]["build_id"]),
                         (RUN1, "L1", BUILD1))
        status = story.routing_status()
        self.assertEqual((status["run_id"], status["effective"], status["active"]["build_id"]), (RUN2, False, BUILD1))
        self.assertIn(f"built for run {RUN1}", status["why_not_effective"])
        # and those tables cannot be activated for the run /find serves now
        refused = story.activate(BUILD1, accept_insufficient=True, reason="again", expect=409)
        self.assertIn(f"/find serves run {RUN2}", refused["error"])

    def test_phase_two_a_frozen_taxonomy_pass_c_then_an_l2_route_from_one_recorded_mapping(self):
        story = Story(self)
        story.phase_one()
        story.kecore_run(RUN2, routing=False)
        # the taxonomy frozen as pass B proposed it ("figer tel quel"), from the review overview
        proposal = story.review()["taxonomy"]["proposed"]
        self.assertEqual((proposal["build_id"], proposal["run_id"], proposal["error"]), (RUN1, RUN1, None))
        frozen = story.post(story.fa.kecore_review_taxonomy, "kecore/review/taxonomy",
                            {"by": "Yassine", "source_build": proposal["build_id"], "intents": proposal["intents"]})
        self.assertEqual((frozen["version"], frozen["orphans"]), (1, []))
        self.assertEqual([i["id"] for i in frozen["intents"]], sorted(i["id"] for i in proposal["intents"]))
        # a routing run: pass C on the frozen taxonomy, in waves, then the tables (L2 still empty: no intent validated)
        out, steps = story.routing_run(BUILD2, measure=False)
        self.assertEqual(steps, ["routing_prepare"] + ["routing_assign"] * 3 + ["routing_assign_summary",
                                                                                  "routing_build", "routing_finish"])
        self.assertEqual((out["run_id"], out["assign"]["fiches"], out["assign"]["errors"],
                          out["assign"]["with_intent"]), (RUN2, 5, 0, 5))
        self.assertEqual((out["tables"]["l1"], out["tables"]["l2"]), (2, 0))
        self.assertEqual((out["carried"], out["candidate"]), (False, True))
        self.assertIn("content_sha256", out["why_not"])                      # a taxonomy changes what the tables are
        self.assertEqual(story.blob("routing/assignments.json"),
                         {"build_id": BUILD2, "run_id": RUN2, "taxonomy_sha256": frozen["sha256"], "errors": 0})
        self.assertEqual(story.find(L1_QUESTION)["route"], None)              # still the run-1 tables, not this run's
        # the reviewer validates the intent pass C proposed, and the application
        view, decided = story.validate_intent()
        self.assertEqual((view["status"], view["proposal_c"]["intent"], view["proposal_c"]["core_aliases_fr"]),
                         ("validated", INTENT, ["réparer démarrage outlook"]))
        self.assertEqual(view["prefill"]["intent"], "")                      # decided without one: nothing pre-ticked
        self.assertEqual(view["taxonomy"][0], {"id": "CAMERA_NOIRE", "label_fr": "réparer la caméra"})
        self.assertEqual((decided["decision"]["intent"], decided["decision"]["primary_app"],
                          decided["decision"]["revision"], decided["decision"]["taxonomy_version"]),
                         (INTENT, "outlook", 1, 1))
        self.assertEqual(len(decided["decision"]["aliases"]), 2)
        taxonomy = story.review()["taxonomy"]["frozen"]
        self.assertEqual((taxonomy["source_build"], {i["id"]: i["fiches"] for i in taxonomy["intents"]}[INTENT]),
                         (RUN1, 1))
        # rebuild and measure: the routed engine maps every question it does not decide at L0/L1 (find-cache/)
        out, steps, active = story.measure_and_activate(BUILD3, reason="the pilot accepts 6 questions")
        self.assertEqual(steps[:2], ["routing_prepare", "routing_build"])     # pass C is about this run: kept
        tables = story.fa.routing_svc.load_tables(story.storage, CLIENT, BUILD3)
        self.assertEqual(tables.l2, [{"intent": INTENT, "app": "outlook", "fiches": [OUTLOOK]}])
        self.assertEqual(tables.taxonomy["version"], 1)
        gate = story.blob(f"routing/gates/{tables.content_sha256}-{RUN2}.json")
        self.assertEqual((gate["status"], gate["wrong_exact"]["k"], gate["degraded"], gate["failures"]),
                         ("insufficient", 0, 0, 0))
        self.assertEqual(gate["by_level"]["L2"]["exact"], 1)                  # the L2 question, measured
        mapped = [key for name, key in story.questions.calls if name == "question_intent_app"]
        self.assertIn(query_text(L2_QUESTION), mapped)
        self.assertNotIn(query_text(L1_QUESTION), mapped)
        self.assertEqual((active["build_id"], active["run_id"], active["override"]["reason"]),
                         (BUILD3, RUN2, "the pilot accepts 6 questions"))
        # live /find: L2 from the record the measure wrote -- no new model call, one answer per question
        calls = len(story.questions.calls)
        answer = story.find(L2_QUESTION)
        route = answer["route"]
        self.assertEqual((route["level"], route["rule"], route["key"], route["fiche_id"], route["build_id"]),
                         ("L2", "intent_app", f"{INTENT}|outlook", OUTLOOK, BUILD3))
        self.assertEqual((route["l2"]["intent"], route["l2"]["app"], route["l2"]["app_source"], route["l2"]["reason"]),
                         (INTENT, "outlook", "code", "intent_app"))
        self.assertEqual((answer["decision"]["reason"], answer["mode"], answer["routing_error"]),
                         ("route_l2_intent_app", "routed", None))
        self.assertEqual(story.find(L1_QUESTION)["route"]["level"], "L1")     # L1 first: no mapping at all
        printer = story.find(REFERENCES["ref-imprimante"][0])                 # maps onto no intent: L3, offered
        self.assertEqual((printer["route"]["level"], printer["route"]["l2"]["reason"]), ("L3", "no_intent"))
        self.assertEqual(printer["candidates"][0]["fiche_id"], PRINTER)
        self.assertEqual(len(story.questions.calls), calls)

    def test_a_later_run_carries_the_activation_over_and_a_deactivation_is_sticky(self):
        story = Story(self)
        story.phase_one()
        story.phase_two()
        before = story.blob("routing/active.json")
        calls = len(story.model.calls)
        # a later kecore run on the same KB, replayed from the record: no model call, the same tables
        out, steps = story.kecore_run(RUN3, mode="replay")
        self.assertEqual(len(story.model.calls), calls)
        self.assertEqual((out["llm"]["calls"], out["enrichment"]["errors"]), (0, 0))
        self.assertNotIn("routing_taxonomy", steps)                           # frozen: pass B is over
        self.assertEqual(steps.count("routing_assign"), 3)                    # pass C of the new run, replayed
        routing = out["routing"]
        self.assertEqual((routing["state"], routing["carried"], routing["why_not"], routing["assign"]["errors"]),
                         ("completed", True, "", 0))
        active = story.blob("routing/active.json")
        self.assertEqual((active["build_id"], active["run_id"], active["carried_from"]), (RUN3, RUN3, BUILD3))
        self.assertEqual(active["previous"], {k: before[k] for k in ("build_id", "run_id", "tables_sha256",
                                                                      "content_sha256")})
        self.assertEqual((active["content_sha256"], active["kb_sha256"]),
                         (before["content_sha256"], before["kb_sha256"]))
        self.assertEqual((active["activated_by"], active["override"], active["gate"]),
                         (before["activated_by"], before["override"], before["gate"]))
        self.assertIn(before, [story.blob(name) for name in story.storage.list(CONTAINER, "routing/history/")])
        # the new run routes at once with its own tables; a session pinned on run 2 keeps the previous entry
        calls = len(story.questions.calls)
        served = story.find(L1_QUESTION)
        self.assertEqual((served["run_id"], served["route"]["level"], served["route"]["build_id"]), (RUN3, "L1", RUN3))
        mapped = story.find(L2_QUESTION)
        self.assertEqual((mapped["route"]["level"], mapped["route"]["build_id"]), ("L2", RUN3))
        self.assertEqual(len(story.questions.calls), calls)                  # the same tables: the same record
        pinned = story.find(L1_QUESTION, run_id=RUN2)
        self.assertEqual((pinned["run_id"], pinned["route"]["build_id"]), (RUN2, BUILD3))
        older = story.find(L1_QUESTION, run_id=RUN1)
        self.assertEqual((older["route"], older["routing_error"]),
                         (None, f"the active routing tables were built for run {RUN3}, not {RUN1}"))
        self.assertEqual(story.routing_status()["effective"], True)
        # a person deactivates: every answer is today's at once on this instance
        self.assertEqual(story.post(story.fa.kecore_routing_deactivate, "kecore/routing/deactivate", {"by": "Yassine"}),
                         {"active": None, "killed": True})
        off = story.find(L1_QUESTION)
        self.assertIsNone(off["route"])
        self.assertTrue(off["routing_error"].startswith("routing is switched off (by Yassine"))
        # the next kecore run, same content again: nothing carried over, the switch stays on
        out, _ = story.kecore_run(RUN4, mode="replay")
        self.assertEqual((out["routing"]["state"], out["routing"]["carried"], out["routing"]["why_not"]),
                         ("completed", False, "no active tables"))
        self.assertEqual((story.blob("routing/active.json"), story.blob("routing/kill.json")["killed"]), (None, True))
        story.forget()
        self.assertTrue(story.find(L1_QUESTION)["routing_error"].startswith("routing is switched off"))
        status = story.routing_status()
        self.assertEqual((status["killed"], status["effective"], status["candidate"]["build_id"]), (True, False, RUN4))
        # only a person's activation turns it back on: measured on the new run, accepted, routing again
        out, _, active = story.measure_and_activate(BUILD4, reason="back on after the incident")
        self.assertEqual((active["build_id"], active["run_id"], story.blob("routing/kill.json")["killed"]),
                         (BUILD4, RUN4, False))
        back = story.find(L1_QUESTION)
        self.assertEqual((back["route"]["level"], back["route"]["build_id"], back["routing_error"]),
                         ("L1", BUILD4, None))

    def test_an_edited_knowledge_base_is_measured_again_and_an_edited_fiche_goes_back_to_review(self):
        story = Story(self)
        story.phase_one()
        # a fiche nobody reviewed is edited: the same tables, another knowledge base -- nothing carried over, and
        # no decision goes back to review (the review key is each fiche's own extracted text)
        story.edit("KB0010003", "<p>Vérifier aussi le bac 1.</p>")
        out, _ = story.kecore_run(RUN2)
        built = story.fa.routing_svc.load_tables(story.storage, CLIENT, RUN2)
        self.assertEqual(built.content_sha256, story.blob("routing/active.json")["content_sha256"])
        self.assertNotEqual(built.kb_sha256, story.blob("routing/active.json")["kb_sha256"])
        self.assertEqual((out["routing"]["carried"], story.blob("routing/active.json")["build_id"]), (False, BUILD1))
        self.assertIn("the knowledge base changed", out["routing"]["why_not"])
        self.assertIsNone(story.find(L1_QUESTION)["route"])
        self.assertEqual(story.review()["counts"], {"proposed": 4, "validated": 1, "rejected": 0, "stale": 0,
                                                    "routable": 5})
        story.measure_and_activate(BUILD2)
        self.assertEqual(story.find(L1_QUESTION)["route"]["build_id"], BUILD2)
        # the reviewed fiche itself is edited: its decision is stale, its aliases no longer route (out of L1)
        story.edit(OUTLOOK, "\nSi rien ne fonctionne, recréez le profil Outlook.\n")
        out, _ = story.kecore_run(RUN3)
        self.assertEqual((out["routing"]["carried"], out["routing"]["tables"]["stale"], out["routing"]["tables"]["l1"]),
                         (False, 1, 0))
        self.assertIn("content_sha256", out["routing"]["why_not"])
        overview = story.review()
        first = overview["fiches"][0]
        self.assertEqual((overview["counts"]["stale"], first["fiche_id"], first["status"]), (1, OUTLOOK, "stale"))
        self.assertEqual(story.get(story.fa.kecore_review_next, "kecore/review/next", status="stale"),
                         {"key": fiche_key(OUTLOOK)})
        view = story.fiche(OUTLOOK)
        self.assertEqual((view["status"], view["decision"], view["previous_decision"]["revision"]), ("stale", None, 0))
        self.assertEqual(sorted(a["text"] for a in view["prefill"]["aliases"] if a["checked"]),
                         sorted([MANUAL_ALIAS, REFERENCES[REF_PROFILE][0]]))
        # the reviewer carries the previous decision onto the new text (checked again in full), then it is measured
        carried = story.post(story.fa.kecore_review_carry, "kecore/review/carry",
                             {"by": "Yassine", "keys": [fiche_key(OUTLOOK)]})
        self.assertEqual(carried, {"carried": 1, "failed": []})
        self.assertEqual(story.review()["counts"]["validated"], 1)
        again = story.post(story.fa.kecore_review_carry, "kecore/review/carry",
                           {"by": "Yassine", "keys": [fiche_key(OUTLOOK)]})
        self.assertEqual((again["carried"], len(again["failed"])), (0, 1))      # nothing stale left to carry
        out, _, active = story.measure_and_activate(BUILD3)
        self.assertEqual((out["tables"]["l1"], out["tables"]["stale"], active["run_id"]), (2, 0, RUN3))
        self.assertEqual(story.find(L1_QUESTION)["route"]["build_id"], BUILD3)

    def test_a_deactivation_cut_short_still_stops_routing_and_no_carry_over_restarts_it(self):
        story = Story(self)
        story.phase_one()
        # the switch is written first: a deactivation that fails right after it (the pointer's delete) has
        # already stopped routing -- the pointer left behind is never served, nor carried over
        delete = story.storage.delete

        def failing(container, name):
            if name == "routing/active.json":
                raise OSError("storage unavailable")
            return delete(container, name)

        with unittest.mock.patch.object(story.storage, "delete", failing):
            answer = story.fa.kecore_routing_deactivate(request("POST", "kecore/routing/deactivate",
                                                                {"client": CLIENT, "by": "Yassine"}))
        self.assertEqual(answer.status_code, 500)                     # "send it again", and this instance forgot
        self.assertIn("send it again", json.loads(answer.get_body())["error"])
        self.assertEqual((story.blob("routing/active.json")["build_id"], story.blob("routing/kill.json")["killed"]),
                         (BUILD1, True))
        story.forget()                                                          # within a minute, every instance
        self.assertTrue(story.find(L1_QUESTION)["routing_error"].startswith("routing is switched off (by Yassine"))
        # the next kecore run has the same content: without the switch it would carry the activation over
        out, _ = story.kecore_run(RUN2)
        self.assertEqual((out["routing"]["state"], out["routing"]["carried"]), ("completed", False))
        self.assertIn("kill switch", out["routing"]["why_not"])
        self.assertEqual((story.blob("routing/active.json")["build_id"], story.blob("routing/kill.json")["killed"]),
                         (BUILD1, True))
        self.assertIsNone(story.find(L1_QUESTION)["route"])
        status = story.routing_status()
        self.assertEqual((status["killed"], status["effective"]), (True, False))
        # the deactivation asked again completes; still nothing routes until a person activates
        self.assertEqual(story.post(story.fa.kecore_routing_deactivate, "kecore/routing/deactivate", {"by": "Yassine"}),
                         {"active": None, "killed": True})
        self.assertIsNone(story.blob("routing/active.json"))

    def test_a_semantic_run_routes_exact_answers_without_a_vector_and_a_degraded_measure_never_activates(self):
        story = Story(self, semantic=True)
        out, steps = story.first_run()
        self.assertIsNone(out["semantic"].get("error"))
        self.assertLess(steps.index("kecore_calibrate"), steps.index("routing_prepare"))
        self.assertEqual(story.find(REFERENCES["ref-imprimante"][0])["mode"], "semantic")   # an index, and its vectors
        story.validate_outlook()
        # measured without the query embedder: decisions are made by words, not as /find makes them -- invalid
        story.query_vectors = False
        out, _ = story.routing_run(BUILD1)
        measured = out["scoreboard"]
        self.assertEqual(measured["routing"]["status"], "invalid")
        self.assertGreater(measured["degraded"]["base"], 0)
        reasons = story.blob(f"scoreboard/{BUILD1}-sb/summary.json")["degraded"]["reasons"]
        self.assertEqual(set(reasons["kefind-funnel"]), {"no_embedder"})
        gate = story.blob(f"routing/gates/{story.blob('routing/candidate.json')['content_sha256']}-{RUN1}.json")
        self.assertTrue(any("degraded" in reason for reason in gate["reasons"]))
        self.assertEqual(gate["degraded"], measured["degraded"]["base"] + measured["degraded"]["route"])
        refused = story.activate(BUILD1, accept_insufficient=True, reason="the pilot accepts it", expect=409)
        self.assertIn("'invalid'", refused["error"])                          # no override for an invalid measure
        self.assertIsNone(story.blob("routing/active.json"))
        # measured again with it: nothing degraded, insufficient, activated with the override
        story.query_vectors = True
        out, _, active = story.measure_and_activate(BUILD2)
        self.assertEqual(out["scoreboard"]["degraded"], {"base": 0, "route": 0})
        self.assertEqual(active["build_id"], BUILD2)
        embedded = len(story.vectors.calls)
        answer = story.find(L1_QUESTION)                                      # never asked before: no vector needed
        self.assertEqual((answer["route"]["level"], answer["mode"], answer["semantic_error"]), ("L1", "routed", None))
        number = story.find("la fiche KB0010006 ne marche pas")
        self.assertEqual((number["route"]["level"], number["mode"]), ("L0", "routed"))
        self.assertEqual(len(story.vectors.calls), embedded)
        other = story.find("papier coincé dans le bac de l'imprimante")       # L3: today's semantic answer, offered
        self.assertEqual((other["route"]["level"], other["mode"], other["decision"]["kind"]),
                         ("L3", "semantic", "question"))
        self.assertEqual(len(story.vectors.calls), embedded + 1)
        # a question that gets no vector live: the routed answer is still today's (degraded), never an error
        story.query_vectors = False
        story.forget()
        degraded = story.find("papier coincé dans le bac de l'imprimante")
        self.assertEqual((degraded["route"]["level"], degraded["mode"]), ("L3", "degraded"))
        self.assertEqual(story.find(L1_QUESTION)["route"]["level"], "L1")    # exact answers need no vector at all


if __name__ == "__main__":
    unittest.main()
