"""Labeling tab (V10 slice 4, app/labels.py): access, the next-ticket draw, and what a label holds."""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "orchestration"))
sys.path.insert(0, str(ROOT / "app"))

from flask import Flask, render_template_string  # noqa: E402

import labels as labels_tab  # noqa: E402

CLIENT = "client-s"


class MemoryTable:
    def __init__(self):
        self.rows = {}
        self.version = 0

    def upsert(self, entity):
        key = (entity["PartitionKey"], entity["RowKey"])
        self.version += 1
        self.rows[key] = {**self.rows.get(key, {}), **entity, "_etag": str(self.version)}

    def get(self, client, row_key):
        row = self.rows.get((client, row_key))
        return dict(row) if row else None

    def create(self, entity):
        if (entity["PartitionKey"], entity["RowKey"]) in self.rows:
            raise labels_tab.Conflict()
        self.upsert(entity)

    def update(self, entity, etag):
        row = self.rows.get((entity["PartitionKey"], entity["RowKey"]))
        if row is None or row["_etag"] != etag:
            raise labels_tab.Conflict()
        self.upsert(entity)

    def list(self, client, select=None):
        rows = [dict(r) for (p, _), r in sorted(self.rows.items()) if p == client]
        return [{k: v for k, v in r.items() if k in select} for r in rows] if select else rows


def make_app(access=True, clients=(CLIENT,), me="alice"):
    tables = {name: MemoryTable() for name in ("tickets", "fiches", "labels", "scores")}
    for fiche_id, label, searchable in (("KB0120", "KB0120- LOCKED ACCOUNT", True), ("KB0200", "KB0200- VPN", True),
                                        ("KB0300", "KB0300- INFO", False)):
        tables["fiches"].upsert({"PartitionKey": CLIENT, "RowKey": fiche_id.lower(), "fiche_id": fiche_id,
                                 "label": label, "searchable": searchable})
    seeds = [("I1", "fiche", "KB0120"), ("I2", "fiche", "KB0120"), ("I3", "fiche", "KB0200"),
             ("I4", "question", ""), ("I5", "question", ""), ("I6", "abstain", "")]
    for ticket_id, kind, shown in seeds:
        tables["tickets"].upsert({
            "PartitionKey": CLIENT, "RowKey": ticket_id, "titre": f"Ticket {ticket_id}",
            "description": "Compte bloqué <script>alert(1)</script>", "resolution": "Déverrouillé",
            "kefind_kind": kind, "kefind_fiche": shown,
            "kefind_candidates": json.dumps([c for c in (shown, "KB0200", "KB0300") if c]),
            "kefind_question": "Laquelle de ces fiches ?" if kind == "question" else "", "kefind_kb_run": "r1"})
    deps = {"allowed_clients": lambda: list(clients), "user_id": lambda: me, "display_name": lambda: me.title(),
            "label_access": lambda: access}
    app = Flask(__name__)
    app.register_blueprint(labels_tab.create_labels_blueprint(tables, deps))

    @app.route("/probe")
    def probe():
        return render_template_string("{{ 'nav' if labels_nav else 'none' }}")

    return app, tables


def post(c, url, data, origin=True):
    headers = {"Origin": "http://localhost"} if origin else {}
    return c.post(url, data=data, headers=headers, base_url="http://localhost")


def test_without_the_group_everything_is_refused():
    app, _ = make_app(access=False)
    c = app.test_client()
    for url in ("/labels", f"/labels/{CLIENT}/next", f"/labels/{CLIENT}/t/I1"):
        assert c.get(url).status_code == 403, url
    assert post(c, f"/labels/{CLIENT}/t/I1", {"decision": "none"}).status_code == 403
    assert c.get("/probe").get_data(as_text=True) == "none"


def test_a_client_the_user_may_not_see_is_refused():
    app, _ = make_app(clients=("clienta",))
    c = app.test_client()
    assert c.get(f"/labels/{CLIENT}/t/I1").status_code == 403
    assert post(c, f"/labels/{CLIENT}/t/I1", {"decision": "none"}).status_code == 403


def test_home_counts_the_tickets_and_links_the_tab():
    app, _ = make_app()
    c = app.test_client()
    page = c.get("/labels").get_data(as_text=True)
    assert "Tickets réels de client-s" in page and "<b>6</b>" in page
    assert c.get("/probe").get_data(as_text=True) == "nav"


def test_the_next_ticket_comes_from_the_least_labeled_outcome():
    app, tables = make_app()
    c = app.test_client()
    first = c.get(f"/labels/{CLIENT}/next").headers["Location"]
    assert "/t/I6" in first  # abstain, question and fiche all at 0: alphabetical, abstain first
    post(c, f"/labels/{CLIENT}/t/I6", {"decision": "none"})
    second = c.get(f"/labels/{CLIENT}/next").headers["Location"]
    assert second.rsplit("/", 1)[-1] in ("I1", "I2", "I3")  # fiche and question at 0, abstain at 1: fiche < question
    post(c, second, {"decision": "skip"})
    third = c.get(f"/labels/{CLIENT}/next").headers["Location"]
    assert third.rsplit("/", 1)[-1] in ("I4", "I5")  # now question is the least labeled outcome


def test_confirming_the_engine_fiche_labels_it_and_records_the_proposal():
    app, tables = make_app()
    c = app.test_client()
    r = post(c, f"/labels/{CLIENT}/t/I1", {"decision": "confirm"})
    assert r.status_code == 302 and "/next" in r.headers["Location"]
    row = tables["labels"].get(CLIENT, "I1")
    assert json.loads(row["expected"]) == ["KB0120"]
    assert (row["decision"], row["proposal_kind"], row["proposal_fiche"], row["labeled_by_id"]) == ("confirm", "fiche", "KB0120", "alice")


def test_a_selection_may_hold_several_fiches_and_only_known_ones():
    app, tables = make_app()
    c = app.test_client()
    post(c, f"/labels/{CLIENT}/t/I4", {"decision": "select", "fiche": ["KB0200"], "other": "KB0300"})
    assert json.loads(tables["labels"].get(CLIENT, "I4")["expected"]) == ["KB0200", "KB0300"]
    r = post(c, f"/labels/{CLIENT}/t/I5", {"decision": "select", "other": "KB9999"})
    assert "msg=unknown_fiche" in r.headers["Location"]
    assert tables["labels"].get(CLIENT, "I5") is None
    page = c.get(r.headers["Location"]).get_data(as_text=True)
    assert "Fiche inconnue" in page


def test_no_fiche_and_skip_are_labels_of_their_own():
    app, tables = make_app()
    c = app.test_client()
    post(c, f"/labels/{CLIENT}/t/I5", {"decision": "none"})
    post(c, f"/labels/{CLIENT}/t/I6", {"decision": "skip"})
    assert tables["labels"].get(CLIENT, "I5")["expected"] == "[]"
    skipped = tables["labels"].get(CLIENT, "I6")
    assert skipped["skipped"] is True and skipped["expected"] == ""


def test_confirm_without_an_engine_fiche_is_refused():
    app, tables = make_app()
    c = app.test_client()
    r = post(c, f"/labels/{CLIENT}/t/I6", {"decision": "confirm"})
    assert r.status_code == 302 and "/t/I6" in r.headers["Location"]
    assert tables["labels"].get(CLIENT, "I6") is None


def test_a_post_from_another_site_is_refused():
    app, _ = make_app()
    c = app.test_client()
    assert post(c, f"/labels/{CLIENT}/t/I1", {"decision": "none"}, origin=False).status_code == 403


def test_bad_ids_and_decisions():
    app, _ = make_app()
    c = app.test_client()
    assert c.get(f"/labels/{CLIENT}/t/I99").status_code == 404
    assert c.get(f"/labels/{CLIENT}/t/..%2Fx").status_code == 404
    assert c.get(f"/labels/{CLIENT}/t/I1%0A").status_code == 404
    assert post(c, f"/labels/{CLIENT}/t/I1", {"decision": "delete"}).status_code == 400


def test_ticket_text_is_escaped_and_the_engine_proposal_shown():
    app, _ = make_app()
    page = app.test_client().get(f"/labels/{CLIENT}/t/I1").get_data(as_text=True)
    assert "<script>alert(1)</script>" not in page and "&lt;script&gt;" in page
    assert "montrée par le moteur" in page and "KB0120- LOCKED ACCOUNT" in page
    assert "non exploitable par le moteur" in page  # KB0300 among the candidates


def test_when_everything_is_labeled_next_goes_home():
    app, tables = make_app()
    c = app.test_client()
    for ticket_id in ("I1", "I2", "I3", "I4", "I5", "I6"):
        post(c, f"/labels/{CLIENT}/t/{ticket_id}", {"decision": "skip"})
    assert "/labels?client=client-s" in c.get(f"/labels/{CLIENT}/next").headers["Location"]


def test_a_label_saved_meanwhile_by_someone_else_is_never_overwritten():
    app, tables = make_app(me="alice")
    tables["labels"].upsert({"PartitionKey": CLIENT, "RowKey": "I2", "expected": '["KB0200"]',
                             "labeled_by_name": "Bob", "labeled_at": "2026-10-08T09:00:00+00:00"})
    c = app.test_client()
    r = post(c, f"/labels/{CLIENT}/t/I2", {"decision": "confirm", "seen": ""})
    assert "msg=changed" in r.headers["Location"]
    assert json.loads(tables["labels"].get(CLIENT, "I2")["expected"]) == ["KB0200"]
    # a correction made after seeing Bob's label goes through
    post(c, f"/labels/{CLIENT}/t/I2", {"decision": "confirm", "seen": "2026-10-08T09:00:00+00:00"})
    assert json.loads(tables["labels"].get(CLIENT, "I2")["expected"]) == ["KB0120"]


def test_each_labeler_has_an_order_of_their_own():
    assert labels_tab._order("alice", "I1") != labels_tab._order("bob", "I1")
    assert labels_tab._order("alice", "I1") == labels_tab._order("alice", "I1")


def test_a_message_is_a_code_never_free_text():
    app, _ = make_app()
    page = app.test_client().get(f"/labels/{CLIENT}/t/I1?msg=Votre+compte+est+suspendu").get_data(as_text=True)
    assert "suspendu" not in page



def test_two_first_labels_at_once_cannot_both_win():
    app, tables = make_app(me="alice")
    original_get = tables["labels"].get
    # Bob's label lands between Alice's read (nothing yet) and her write
    def racing_get(client, row_key):
        row = original_get(client, row_key)
        if row is None and not getattr(racing_get, "done", False):
            racing_get.done = True
            tables["labels"].upsert({"PartitionKey": client, "RowKey": row_key, "expected": '["KB0200"]',
                                     "labeled_at": "2026-10-08T09:00:00+00:00", "labeled_by_name": "Bob"})
        return row
    tables["labels"].get = racing_get
    r = post(app.test_client(), f"/labels/{CLIENT}/t/I3", {"decision": "confirm", "seen": ""})
    assert "msg=changed" in r.headers["Location"]
    assert json.loads(original_get(CLIENT, "I3")["expected"]) == ["KB0200"]
