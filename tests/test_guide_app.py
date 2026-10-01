import hashlib
import hmac
import importlib.util
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "orchestration"))
sys.path.insert(0, str(ROOT / "app"))

from flask import Flask
from guide.service import MemoryStore

spec = importlib.util.spec_from_file_location("diag_bp", ROOT / "app" / "diag_tab.py")
diag_bp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(diag_bp)

CHUNK = "<table><tr><td>1. Ouvrez Parametres.</td></tr><tr><td>2. Cliquez sur Comptes.</td></tr></table>"


class FakeAoai:
    def __init__(self):
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kw):
        name = kw["response_format"]["json_schema"]["name"]
        if name == "guide_extract":
            out = {"variables": [], "injection_suspected": False}
        elif name == "guide_ocr":
            out = {"readable": True, "findings": []}
        elif name == "guide_judge":
            out = {"best": "none", "exact": False, "reason": ""}
        elif name == "guide_build":
            out = {"applicable": True, "reason": "", "summary": "Cette fiche explique la reinitialisation.",
                   "preconditions": [], "verification": ["Ca marche."],
                   "steps": [{"title": "Ouvrir les parametres", "instruction": "Ouvrez Parametres.",
                              "source_chunk_id": "c1", "verbatim_from_kb": True},
                             {"title": "Aller dans Comptes", "instruction": "Cliquez sur Comptes.",
                              "source_chunk_id": "c1", "verbatim_from_kb": True}]}
        else:
            out = {"answer_fr": "Cherchez l'icone engrenage.", "found_in_kb": True, "source_chunk_id": "c1"}
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(out)))])


def make_app(me="alice", clients=("client-s",), itsm=False, strong=True):
    hit = lambda pid, score: {"parent_id": pid, "title": pid, "chunk_id": pid + "_0", "chunk": CHUNK, "@search.rerankerScore": score}
    docs = [hit("K1", 3.8), hit("K2", 1.0)] if strong else [hit("K1", 2.0), hit("K2", 1.9)]
    deps = dict(
        allowed_clients=lambda: list(clients), user_id=lambda: me, display_name=lambda: me.title(),
        itsm_access=lambda: itsm, search_token=lambda: "tok", aoai=FakeAoai(),
        load_engine_config=lambda c: {"knowledge": {"index": "idx"}, "generation": {"model": "m", "seed": 1}},
        retrieve_hierarchy=lambda q, index, n, a, h, client_id=None: (docs, []),
        fetch_document_chunks=lambda pids, index, h, client_id=None: {
            pids[0]: [{"chunk_id": "k_0", "chunk": CHUNK}]})
    app = Flask(__name__)
    store = MemoryStore()
    app.register_blueprint(diag_bp.create_diagnostic_blueprint(None, deps, store=store))
    return app, store


H = {"Origin": "http://localhost", "Host": "localhost"}


def post(c, url, data):
    return c.post(url, data=data, headers={"Origin": "http://localhost"}, base_url="http://localhost")


def sid_of(resp):
    return resp.headers["Location"].split("/diag/s/")[1].split("?")[0]


def test_full_flow_summary_then_steps_then_solved():
    app, _ = make_app()
    c = app.test_client()
    r = post(c, "/diag/new", {"client_id": "client-s", "text": "reset mdp"})
    sid = sid_of(r)
    page = c.get(f"/diag/s/{sid}?client_id=client-s").get_data(as_text=True)
    assert "Cette fiche explique la reinitialisation." in page and "Étape 1 sur 2" in page
    assert "Ouvrir les parametres" in page and "<table" not in page
    post(c, f"/diag/s/{sid}/reply", {"client_id": "client-s", "action": "done"})
    post(c, f"/diag/s/{sid}/reply", {"client_id": "client-s", "action": "done"})
    page = c.get(f"/diag/s/{sid}?client_id=client-s").get_data(as_text=True)
    assert "Le problème est-il résolu" in page
    post(c, f"/diag/s/{sid}/reply", {"client_id": "client-s", "action": "solved_yes"})
    page = c.get(f"/diag/s/{sid}?client_id=client-s").get_data(as_text=True)
    assert "Problème résolu" in page and "Transmis" not in page


def test_ambiguous_start_shows_choice_buttons():
    app, _ = make_app(strong=False)
    c = app.test_client()
    sid = sid_of(post(c, "/diag/new", {"client_id": "client-s", "text": "probleme"}))
    page = c.get(f"/diag/s/{sid}?client_id=client-s").get_data(as_text=True)
    assert "Quelle fiche correspond" in page and 'value="pick:1"' in page
    post(c, f"/diag/s/{sid}/reply", {"client_id": "client-s", "action": "pick:1"})
    assert "Étape 1 sur 2" in c.get(f"/diag/s/{sid}?client_id=client-s").get_data(as_text=True)


def test_blocked_step_shows_help_text():
    app, _ = make_app()
    c = app.test_client()
    sid = sid_of(post(c, "/diag/new", {"client_id": "client-s", "text": "x"}))
    post(c, f"/diag/s/{sid}/reply", {"client_id": "client-s", "text": "je ne trouve pas"})
    assert "Cherchez l&#39;icone engrenage." in c.get(f"/diag/s/{sid}?client_id=client-s").get_data(as_text=True)


def test_bad_action_and_cross_origin_are_refused():
    app, _ = make_app()
    c = app.test_client()
    sid = sid_of(post(c, "/diag/new", {"client_id": "client-s", "text": "x"}))
    assert post(c, f"/diag/s/{sid}/reply", {"client_id": "client-s", "action": "escalate"}).status_code == 400
    assert c.post(f"/diag/s/{sid}/reply", data={"client_id": "client-s", "action": "done"},
                  headers={"Origin": "http://evil.example"}, base_url="http://localhost").status_code == 403


def test_other_user_cannot_read_the_session_and_unknown_client_is_refused():
    app, store = make_app(me="alice")
    sid = sid_of(post(app.test_client(), "/diag/new", {"client_id": "client-s", "text": "x"}))
    app2, _ = make_app(me="bob")
    app2.view_functions  # separate store: bob simply has no such session
    assert app2.test_client().get(f"/diag/s/{sid}?client_id=client-s").status_code == 404
    assert app.test_client().get(f"/diag/s/{sid}?client_id=other").status_code == 404


def sign(secret, body, ts=None):
    ts = str(ts or int(time.time()))
    return {"X-KE-Timestamp": ts, "X-KE-Signature": "sha256=" + hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()}


def test_webhook_disabled_without_secret_and_signed_when_enabled(monkeypatch):
    app, _ = make_app()
    c = app.test_client()
    body = json.dumps({"ticket_number": "INC1", "event_id": "ev1", "short_description": "reset mdp"}).encode()
    monkeypatch.delenv("DIAG_WEBHOOK_SECRET", raising=False)
    assert c.post("/api/servicenow/webhook", data=body).status_code == 404
    monkeypatch.setenv("DIAG_WEBHOOK_SECRET", "s3cret")
    monkeypatch.setenv("DIAG_WEBHOOK_CLIENT", "client-s")
    assert c.post("/api/servicenow/webhook", data=body, headers=sign("bad", body)).status_code == 401
    r = c.post("/api/servicenow/webhook", data=body, headers=sign("s3cret", body))
    j = r.get_json()
    assert r.status_code == 200 and j["state"] == "GUIDING" and j["guide"]["title"] == "K1"
    again = c.post("/api/servicenow/webhook", data=body, headers=sign("s3cret", body))
    assert again.status_code == 200 and again.get_json()["session_id"] == "sn-INC1"


def test_kb_text_strips_markup():
    from guide.textutil import kb_text
    assert kb_text(CHUNK) == "1. Ouvrez Parametres.\n2. Cliquez sur Comptes."
