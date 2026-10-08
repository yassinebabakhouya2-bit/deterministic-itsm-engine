"""Review of the client dictionary (V10 slice 5, app/dictionary_tab.py)."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))

from flask import Flask  # noqa: E402

import dictionary_tab  # noqa: E402

CLIENT = "client-s"
DATA = {"client": CLIENT, "run_id": "r1", "min_observations": 3, "watching": 2,
        "ready": [{"term": "coupa", "spelling": "Coupa", "seen": 4}],
        "dictionary": [{"id": "veeam", "forms": ["VEEAM"], "rejected": False}],
        "decided": [], "decisions": {"rejected": [], "accepted": []}}


class Engine:
    def __init__(self, fail=None):
        self.decisions, self.fail = [], fail

    def dictionary(self, client):
        return DATA

    def decide(self, body):
        if self.fail:
            raise RuntimeError(self.fail)
        self.decisions.append(body)
        return {"status": "ok"}


def make_app(engine=None, access=True, clients=(CLIENT,)):
    engine = engine if engine is not None else Engine()
    app = Flask(__name__)
    app.register_blueprint(dictionary_tab.create_dictionary_blueprint(engine, {
        "allowed_clients": lambda: list(clients), "display_name": lambda: "Yassine", "label_access": lambda: access}))
    return app, engine


def post(c, data, origin=True):
    return c.post(f"/dictionary/{CLIENT}/decide", data=data, base_url="http://localhost",
                  headers={"Origin": "http://localhost"} if origin else {})


def test_access_is_refused_without_the_group_or_the_client():
    for app, _ in (make_app(access=False), make_app(clients=("clienta",))):
        c = app.test_client()
        assert c.get(f"/dictionary?client={CLIENT}").status_code == 403
        assert post(c, {"decision": "accept", "term": "coupa"}).status_code == 403


def test_the_page_lists_candidates_and_entries():
    page = make_app()[0].test_client().get("/dictionary").get_data(as_text=True)
    assert "Coupa" in page and "VEEAM" in page and "prochain run kecore" in page


def test_decisions_reach_the_engine_with_who_decided():
    app, engine = make_app()
    c = app.test_client()
    assert "msg=accepted" in post(c, {"decision": "accept", "term": "coupa"}).headers["Location"]
    post(c, {"decision": "accept_as", "term": "coupa", "canonical": "veeam"})
    post(c, {"decision": "reject", "term": "coupa"})
    post(c, {"decision": "reject_entry", "entry": "veeam"})
    assert engine.decisions == [
        {"client": CLIENT, "by": "Yassine", "term": "coupa", "accept": True},
        {"client": CLIENT, "by": "Yassine", "term": "coupa", "accept": True, "canonical": "veeam"},
        {"client": CLIENT, "by": "Yassine", "term": "coupa", "accept": False},
        {"client": CLIENT, "by": "Yassine", "entry": "veeam", "accept": False}]


def test_bad_posts_are_refused():
    app, engine = make_app()
    c = app.test_client()
    assert post(c, {"decision": "accept", "term": "coupa"}, origin=False).status_code == 403
    assert post(c, {"decision": "delete"}).status_code == 400
    assert post(c, {"decision": "accept_as", "term": "coupa", "canonical": "Not An Id"}).status_code == 400
    assert engine.decisions == []


def test_an_engine_refusal_is_shown_as_a_code():
    app, _ = make_app(Engine(fail="POST kecore/dictionary/decision -> HTTP 409: already decided"))
    assert "msg=error" in post(app.test_client(), {"decision": "accept", "term": "coupa"}).headers["Location"]


def test_without_an_engine_the_page_says_so():
    app = Flask(__name__)
    app.register_blueprint(dictionary_tab.create_dictionary_blueprint(None, {
        "allowed_clients": lambda: [CLIENT], "display_name": lambda: "Y", "label_access": lambda: True}))
    assert "pas relié" in app.test_client().get("/dictionary").get_data(as_text=True)
