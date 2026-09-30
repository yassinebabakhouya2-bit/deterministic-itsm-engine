import hashlib
import hmac
import io
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "orchestration"))
sys.path.insert(0, str(ROOT / "app"))

from flask import Flask

from diagnostic.service import DiagnosticService, MemoryStore, NotFound

import importlib.util

spec = importlib.util.spec_from_file_location("diag_bp", ROOT / "app" / "diag_tab.py")
diag_bp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(diag_bp)


class FakeAoai:
    def __init__(self):
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kw):
        name = kw["response_format"]["json_schema"]["name"]
        user = kw["messages"][1]["content"]
        if name == "diag_extract":
            vs = [{"name": "application", "value": "Outlook", "confidence": 0.95}] if "Outlook" in user else []
            out = {"variables": vs, "injection_suspected": False}
        elif name == "diag_ocr":
            out = {"readable": True, "findings": []}
        else:
            out = {"applicable": True, "reason": "", "missing_information": [], "preconditions": [],
                   "steps": [{"instruction": "Ouvrez Parametres.", "action_type": "user_instruction",
                              "source_chunk_id": "c1", "verbatim_from_kb": True}], "verification": ["ok"]}
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(out)))])


def make_app(me="alice", clients=("client-s",), itsm=False, strong=True):
    docs = ([{"parent_id": "K1", "title": "K1", "chunk_id": "k1_0", "chunk": "Texte illustratif de la fiche K1", "@search.rerankerScore": 3.8},
             {"parent_id": "K2", "title": "K2", "chunk_id": "k2_0", "@search.rerankerScore": 1.0}] if strong else
            [{"parent_id": "K1", "title": "K1", "chunk_id": "k1_0", "chunk": "Texte illustratif de la fiche K1", "@search.rerankerScore": 2.0},
             {"parent_id": "K2", "title": "K2", "chunk_id": "k2_0", "@search.rerankerScore": 1.9}])
    deps = dict(
        allowed_clients=lambda: list(clients), user_id=lambda: me, display_name=lambda: me.title(),
        itsm_access=lambda: itsm, search_token=lambda: "tok", aoai=FakeAoai(),
        load_engine_config=lambda c: {"knowledge": {"index": "idx"}, "generation": {"model": "m", "seed": 1}},
        retrieve_hierarchy=lambda q, index, n, a, h, client_id=None: (docs, []),
        fetch_document_chunks=lambda pids, index, h, client_id=None: {
            pids[0]: [{"chunk_id": "k1_0", "chunk": "Ouvrez Parametres."}]})
    app = Flask(__name__)
    store = MemoryStore()
    app.register_blueprint(diag_bp.create_diagnostic_blueprint(None, deps, store=store))
    return app, store


def post(c, url, **kw):
    return c.post(url, headers={"Origin": "http://localhost"}, base_url="http://localhost", **kw)


def test_new_session_reaches_a_plan_and_is_rendered():
    app, _ = make_app()
    c = app.test_client()
    r = post(c, "/diag/new", data={"client_id": "client-s", "text": "Outlook ne demarre plus"})
    assert r.status_code == 302
    page = c.get(r.headers["Location"]).get_data(as_text=True)
    assert "Plan proposé" in page and "Ouvrez Parametres." in page


def test_cross_site_post_is_refused():
    app, _ = make_app()
    assert app.test_client().post("/diag/new", data={"client_id": "client-s", "text": "x"}).status_code == 403


def test_client_not_allowed_is_refused():
    app, _ = make_app()
    r = post(app.test_client(), "/diag/new", data={"client_id": "other", "text": "Outlook"})
    assert r.status_code == 403


def test_other_user_cannot_see_a_session():
    app, store = make_app(me="alice")
    c = app.test_client()
    loc = post(c, "/diag/new", data={"client_id": "client-s", "text": "Outlook"}).headers["Location"]
    bob, _ = make_app(me="bob")
    # same store, different caller
    bob2 = Flask(__name__)
    deps_app, _ = make_app(me="bob")
    # rebuild bob's app on alice's store
    from flask import Flask as F
    app_b = F(__name__)
    d = dict(allowed_clients=lambda: ["client-s"], user_id=lambda: "bob", display_name=lambda: "Bob",
             itsm_access=lambda: False, search_token=lambda: "t", aoai=FakeAoai(),
             load_engine_config=lambda c: {}, retrieve_hierarchy=None, fetch_document_chunks=None)
    app_b.register_blueprint(diag_bp.create_diagnostic_blueprint(None, d, store=store))
    assert app_b.test_client().get(loc).status_code == 404


def test_weak_match_asks_a_question_then_reply_continues():
    app, _ = make_app(strong=False)
    c = app.test_client()
    loc = post(c, "/diag/new", data={"client_id": "client-s", "text": "Outlook bug"}).headers["Location"]
    page = c.get(loc).get_data(as_text=True)
    assert "En attente de votre réponse" in page and "Laquelle de ces situations" in page
    sid = loc.split("/diag/s/")[1].split("?")[0]
    r = post(c, f"/diag/s/{sid}/reply", data={"client_id": "client-s", "text": "Outlook classique"})
    assert r.status_code == 302


def test_screenshot_type_and_size_are_checked():
    app, _ = make_app()
    c = app.test_client()
    r = post(c, "/diag/new", data={"client_id": "client-s", "text": "Outlook",
                                   "screenshot": (io.BytesIO(b"<svg/>"), "x.svg", "image/svg+xml")},
             content_type="multipart/form-data")
    assert r.status_code == 400


def test_html_in_ticket_text_is_escaped():
    app, _ = make_app(strong=False)
    c = app.test_client()
    loc = post(c, "/diag/new", data={"client_id": "client-s", "text": "<script>alert(1)</script> Outlook"}).headers["Location"]
    page = c.get(loc).get_data(as_text=True)
    assert "<script>alert(1)</script>" not in page and "&lt;script&gt;" in page


def sign(secret, body, ts=None):
    ts = str(int(ts if ts is not None else time.time()))
    return {"X-KE-Timestamp": ts, "X-KE-Signature": "sha256=" + hmac.new(
        secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest(), "Content-Type": "application/json"}


def test_webhook_disabled_without_secret(monkeypatch):
    monkeypatch.delenv("DIAG_WEBHOOK_SECRET", raising=False)
    app, _ = make_app()
    assert app.test_client().post("/api/servicenow/webhook", data=b"{}").status_code == 404


def test_webhook_signature_replay_and_idempotence(monkeypatch):
    monkeypatch.setenv("DIAG_WEBHOOK_SECRET", "s3cret")
    monkeypatch.setenv("DIAG_WEBHOOK_CLIENT", "client-s")
    app, store = make_app()
    c = app.test_client()
    body = json.dumps({"event_id": "ev1", "ticket_number": "INC001", "short_description": "Outlook KO",
                       "description": "ne demarre plus"}).encode()
    assert c.post("/api/servicenow/webhook", data=body, headers=sign("wrong", body)).status_code == 401
    assert c.post("/api/servicenow/webhook", data=body, headers=sign("s3cret", body, time.time() - 900)).status_code == 401
    r = c.post("/api/servicenow/webhook", data=body, headers=sign("s3cret", body))
    assert r.status_code == 200 and r.get_json()["state"] == "ACTION_PROPOSED"
    assert r.get_json()["plan"]["steps"]
    again = c.post("/api/servicenow/webhook", data=body, headers=sign("s3cret", body))
    assert again.status_code == 200 and again.get_json()["outbox"] == []          # same event: no re-processing
    assert len(store.rows) == 1


def test_servicenow_sessions_visible_only_with_itsm_access(monkeypatch):
    monkeypatch.setenv("DIAG_WEBHOOK_SECRET", "s3cret")
    monkeypatch.setenv("DIAG_WEBHOOK_CLIENT", "client-s")
    app, store = make_app(me="agent", itsm=True)
    body = json.dumps({"event_id": "ev1", "ticket_number": "INC002", "description": "Outlook KO"}).encode()
    app.test_client().post("/api/servicenow/webhook", data=body, headers=sign("s3cret", body))
    assert "INC002" in app.test_client().get("/diag?client_id=client-s").get_data(as_text=True)
    app2, _ = make_app(me="agent", itsm=False)
    app2.blueprints["diag"].service.store.rows.update(store.rows)
    assert "INC002" not in app2.test_client().get("/diag?client_id=client-s").get_data(as_text=True)


def test_sweep_escalates_expired_waiting_sessions(monkeypatch):
    monkeypatch.setenv("DIAG_WEBHOOK_SECRET", "s3cret")
    monkeypatch.setenv("DIAG_WEBHOOK_CLIENT", "client-s")
    app, store = make_app(strong=False)
    c = app.test_client()
    post(c, "/diag/new", data={"client_id": "client-s", "text": "Outlook bug"})
    svc = app.blueprints["diag"].service
    from datetime import datetime, timedelta, timezone
    svc.now = lambda: datetime.now(timezone.utc) + timedelta(hours=30)
    r = c.post("/diag/internal/sweep", data=b"", headers=sign("s3cret", b""))
    assert r.get_json() == {"escalated": 1}
    row = next(iter(store.rows.values()))
    assert row["state"] == "HUMAN_ESCALATION"


def test_technical_failure_becomes_an_escalation_not_a_crash():
    app, _ = make_app()
    svc = app.blueprints["diag"].service
    def boom(client_id, images):
        raise RuntimeError("search down")
    svc.ports_factory = boom
    # the failure happens before advance(): surfaces as an error redirect, never a 500
    r = post(app.test_client(), "/diag/new", data={"client_id": "client-s", "text": "Outlook"})
    assert r.status_code == 302 and "error=" in r.headers["Location"]


def test_closest_fiche_is_shown_at_once_with_its_excerpt_while_the_diagnostic_continues():
    app, _ = make_app(strong=False)
    c = app.test_client()
    loc = post(c, "/diag/new", data={"client_id": "client-s", "text": "Outlook bug"}).headers["Location"]
    page = c.get(loc).get_data(as_text=True)
    assert "Fiche la plus proche" in page and "Texte illustratif de la fiche K1" in page
    assert "En attente de votre réponse" in page                 # ... and the question follows


def test_plan_page_does_not_repeat_the_provisional_fiche_card():
    app, _ = make_app()
    c = app.test_client()
    loc = post(c, "/diag/new", data={"client_id": "client-s", "text": "Outlook KO"}).headers["Location"]
    page = c.get(loc).get_data(as_text=True)
    assert "Plan proposé" in page and "Fiche la plus proche" not in page
