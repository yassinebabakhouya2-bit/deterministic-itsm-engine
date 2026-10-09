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

    def delete(self, client, row_key):
        self.rows.pop((client, row_key), None)

    def list(self, client, select=None):
        rows = [dict(r) for (p, _), r in sorted(self.rows.items()) if p == client]
        return [{k: v for k, v in r.items() if k in select} for r in rows] if select else rows


SEEDS = {CLIENT: [("comment attribuer une ligne teams", ["KB0233"]), ("Mon compte est bloqué", ["KB0120"]),
                  ("une fiche absente de la carte", ["KB9999"]), ("une facture à valider", ["KB0400"]),
                  ("le vpn coupe", ["KB0200"]), ("la machine à café fuit", []),
                  ("vpn et compte bloqué", ["KB0200", "KB0120"])]}
PHONE_LINE = "KB0233 -  Associate a phone line"   # a real client-s id: the document's name, two spaces


def make_app(access=True, clients=(CLIENT,), me="alice", ref_seeds=SEEDS):
    tables = {name: MemoryTable() for name in ("tickets", "fiches", "labels", "scores", "refs")}
    for fiche_id, label, searchable in (("KB0120", "KB0120- LOCKED ACCOUNT", True), ("KB0200", "KB0200- VPN", True),
                                        ("KB0300", "KB0300- INFO", False),
                                        (PHONE_LINE, PHONE_LINE, True),
                                        ("KB0400 - Valider une facture", "KB0400 - Valider une facture", True),
                                        ("KB0400 - Ancienne facture", "KB0400 - Ancienne facture", True)):
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
            "label_access": lambda: access, "ref_seeds": ref_seeds}
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


# ------------------------------------------------------------ reference questions
def refs_of(tables):
    return {r["text"]: (json.loads(r["expected"]), r) for (_, _), r in tables["refs"].rows.items()}


def test_a_reference_question_is_saved_with_its_fiche_or_none():
    app, tables = make_app()
    c = app.test_client()
    r = post(c, f"/labels/{CLIENT}/refs", {"text": "  comment attribuer   une ligne teams ", "fiche": "KB0233",
                                           "decision": "fiche"})
    assert "msg=ref_saved" in r.headers["Location"]
    post(c, f"/labels/{CLIENT}/refs", {"text": "la machine à café fuit", "decision": "none"})
    refs = refs_of(tables)
    expected, row = refs["comment attribuer une ligne teams"]
    assert expected == [PHONE_LINE]                       # typed as "KB0233", stored as the real fiche id
    assert row["RowKey"] == labels_tab.ref_id("Comment attribuer une ligne TEAMS")
    assert row["RowKey"].startswith("ref-") and row["labeled_by_name"] == "Alice" and row["source"] == "form"
    assert refs["la machine à café fuit"][0] == []
    page = c.get(f"/labels/{CLIENT}/refs").get_data(as_text=True)
    assert "KB0233 -  Associate a phone line" in page and "aucune fiche" in page
    assert "<b>2</b> questions" in c.get(f"/labels?client={CLIENT}").get_data(as_text=True)


def test_the_same_question_twice_is_one_row_and_only_its_fiche_can_change():
    app, tables = make_app()
    c = app.test_client()
    post(c, f"/labels/{CLIENT}/refs", {"text": "Mon compte est bloqué", "fiche": "KB0200", "decision": "fiche"})
    again = post(c, f"/labels/{CLIENT}/refs", {"text": "mon compte  est BLOQUÉ", "fiche": "KB0120", "decision": "fiche"})
    assert "msg=ref_exists" in again.headers["Location"] and len(tables["refs"].rows) == 1
    (_, row), = refs_of(tables).values()
    edit = c.get(f"/labels/{CLIENT}/refs?id={row['RowKey']}").get_data(as_text=True)
    assert "Modifier la fiche attendue" in edit and 'value="KB0200"' in edit
    post(c, f"/labels/{CLIENT}/refs", {"id": row["RowKey"], "seen": row["labeled_at"], "fiche": "KB0120",
                                       "decision": "fiche", "text": "ignored on an edit"})
    assert refs_of(tables)["Mon compte est bloqué"][0] == ["KB0120"]


def test_an_edit_over_someone_elses_change_is_refused():
    app, tables = make_app()
    c = app.test_client()
    post(c, f"/labels/{CLIENT}/refs", {"text": "Mon compte est bloqué", "fiche": "KB0120", "decision": "fiche"})
    (_, row), = refs_of(tables).values()
    r = post(c, f"/labels/{CLIENT}/refs", {"id": row["RowKey"], "seen": "an older timestamp", "decision": "none"})
    assert "msg=ref_changed" in r.headers["Location"]
    assert refs_of(tables)["Mon compte est bloqué"][0] == ["KB0120"]


def test_bad_reference_input_is_refused():
    app, tables = make_app()
    c = app.test_client()
    assert "msg=ref_text" in post(c, f"/labels/{CLIENT}/refs", {"text": "ab", "decision": "none"}).headers["Location"]
    assert "msg=ref_text" in post(c, f"/labels/{CLIENT}/refs", {"text": "x" * 501, "decision": "none"}).headers["Location"]
    unknown = post(c, f"/labels/{CLIENT}/refs", {"text": "une question", "fiche": "KB9999", "decision": "fiche"})
    assert "msg=unknown_fiche" in unknown.headers["Location"]
    assert post(c, f"/labels/{CLIENT}/refs", {"text": "une question", "decision": "maybe"}).status_code == 400
    assert post(c, f"/labels/{CLIENT}/refs", {"text": "une question", "decision": "none"}, origin=False).status_code == 403
    assert c.get(f"/labels/{CLIENT}/refs?id=I1").status_code == 404
    assert post(c, f"/labels/{CLIENT}/refs/I1/delete", {}).status_code == 404
    assert tables["refs"].rows == {}


def test_reference_questions_need_the_group_and_the_client():
    app, _ = make_app(access=False)
    c = app.test_client()
    assert c.get(f"/labels/{CLIENT}/refs").status_code == 403
    assert post(c, f"/labels/{CLIENT}/refs", {"text": "question", "decision": "none"}).status_code == 403
    assert post(c, f"/labels/{CLIENT}/refs/seed", {}).status_code == 403
    app, _ = make_app(clients=("clienta",))
    assert app.test_client().get(f"/labels/{CLIENT}/refs").status_code == 403


def test_a_reference_question_is_escaped_and_can_be_deleted():
    app, tables = make_app()
    c = app.test_client()
    post(c, f"/labels/{CLIENT}/refs", {"text": "<script>alert(1)</script> écran noir", "decision": "none"})
    page = c.get(f"/labels/{CLIENT}/refs").get_data(as_text=True)
    assert "<script>alert(1)" not in page and "&lt;script&gt;" in page
    (_, row), = refs_of(tables).values()
    assert "msg=ref_deleted" in post(c, f"/labels/{CLIENT}/refs/{row['RowKey']}/delete", {}).headers["Location"]
    assert tables["refs"].rows == {}


def seed_states(page):
    import re
    return [re.sub(r"<[^>]+>.*", "", cell).strip() for cell in re.findall(r'<td class="seed-state">(.*?)</td>', page, re.S)]


def test_the_repository_questions_are_imported_on_a_click_create_only():
    app, tables = make_app()
    c = app.test_client()
    page = c.get(f"/labels/{CLIENT}/refs").get_data(as_text=True)
    assert "7 questions livrées avec le dépôt" in page and tables["refs"].rows == {}   # a page view writes nothing
    assert seed_states(page) == ["à importer", "à importer", "non importable", "non importable", "à importer",
                                 "à importer", "à importer"]
    assert "KB9999 : absente de la carte" in page and "KB0400 : ambiguë" in page
    assert "KB0200- VPN, KB0120- LOCKED ACCOUNT" in page                              # several fiches, separated
    post(c, f"/labels/{CLIENT}/refs", {"text": "Mon compte est bloqué", "fiche": "KB0200", "decision": "fiche"})
    assert "msg=ref_seeded_partial" in post(c, f"/labels/{CLIENT}/refs/seed", {}).headers["Location"]
    refs = refs_of(tables)
    assert refs["comment attribuer une ligne teams"][0] == [PHONE_LINE]               # a number, resolved
    assert refs["comment attribuer une ligne teams"][1]["source"] == "seed"
    assert refs["le vpn coupe"][0] == ["KB0200"]                                      # an exact catalog id
    assert refs["la machine à café fuit"][0] == []                                    # no fiche expected
    assert refs["vpn et compte bloqué"][0] == ["KB0200", "KB0120"]
    assert refs["Mon compte est bloqué"][0] == ["KB0200"]       # a person's label is never overwritten
    assert "une fiche absente de la carte" not in refs           # its fiche is not in the map
    assert "une facture à valider" not in refs                   # two fiches carry KB0400: never a guess
    page = c.get(f"/labels/{CLIENT}/refs").get_data(as_text=True)
    assert seed_states(page) == ["présente", "présente — fiche différente du dépôt", "non importable",
                                 "non importable", "présente", "présente", "présente"]
    post(c, f"/labels/{CLIENT}/refs/seed", {})
    assert len(tables["refs"].rows) == 5


def test_a_question_added_by_hand_for_an_unimportable_seed_no_longer_counts_as_skipped():
    app, tables = make_app(ref_seeds={CLIENT: [("une facture à valider", ["KB0400"])]})
    c = app.test_client()
    post(c, f"/labels/{CLIENT}/refs", {"text": "une facture à valider", "fiche": "KB0400 - Valider une facture",
                                       "decision": "fiche"})
    assert "msg=ref_seeded&" in post(c, f"/labels/{CLIENT}/refs/seed", {}).headers["Location"] + "&"
    assert seed_states(c.get(f"/labels/{CLIENT}/refs").get_data(as_text=True)) == ["présente"]


def test_a_stored_fiche_that_left_the_map_is_flagged():
    app, tables = make_app(ref_seeds={CLIENT: [("comment attribuer une ligne teams", ["KB0233"])]})
    c = app.test_client()
    post(c, f"/labels/{CLIENT}/refs/seed", {})
    tables["fiches"].rows.pop((CLIENT, PHONE_LINE.lower()))                 # the document was renamed
    tables["fiches"].upsert({"PartitionKey": CLIENT, "RowKey": "kb0233-new", "fiche_id": "KB0233 - Associate a phone line",
                             "label": "KB0233 - Associate a phone line", "searchable": True})
    page = c.get(f"/labels/{CLIENT}/refs").get_data(as_text=True)
    assert seed_states(page) == ["présente — fiche absente de la carte"]
    assert "(absente de la carte)" in page


def test_the_repository_file_holds_the_priority_case():
    seeds = labels_tab.load_ref_seeds()
    assert ("comment attribuer une ligne teams", ["KB0233"]) in seeds["client-s"]
    assert all(e for _, e in seeds["client-s"])


def test_a_fiche_named_by_its_number_is_found_only_when_it_is_unique():
    catalog = {f: {} for f in (PHONE_LINE, "KB0120- LOCKED ACCOUNT", "K0090 - VEEAM-Appel_Support1", "KB00308",
                               "KB0032 – How to clear the TEAMS cache", "KB0400 - A", "KB0400 - B", "KB02330 - Autre")}
    resolve = labels_tab.resolve_fiche
    assert resolve(PHONE_LINE, catalog) == (PHONE_LINE, "")              # an exact id stays as it is
    assert resolve("KB0233", catalog) == (PHONE_LINE, "")                # not KB02330: the number ends there
    assert resolve(" kb 233 ", catalog) == (PHONE_LINE, "")
    assert resolve("KB0120", catalog) == ("KB0120- LOCKED ACCOUNT", "")
    assert resolve("K0090", catalog) == ("K0090 - VEEAM-Appel_Support1", "")
    assert resolve("KB308", catalog) == ("KB00308", "")
    assert resolve("KB32", catalog) == ("KB0032 – How to clear the TEAMS cache", "")
    assert resolve("KB0400", catalog) == (None, "ambiguë : KB0400 - A | KB0400 - B")
    assert resolve("KB9999", catalog) == (None, "absente de la carte")
    assert resolve("Associate a phone line", catalog) == (None, "absente de la carte")   # no title guessing
    assert resolve("", catalog) == (None, "absente de la carte")


def test_a_technician_may_type_the_number_but_an_ambiguous_one_is_refused():
    app, tables = make_app()
    c = app.test_client()
    r = post(c, f"/labels/{CLIENT}/refs", {"text": "valider une facture harmony", "fiche": "KB0400", "decision": "fiche"})
    assert "msg=unknown_fiche" in r.headers["Location"] and tables["refs"].rows == {}
