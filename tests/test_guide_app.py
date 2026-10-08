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


def make_app(me="alice", clients=("client-s",), itsm=False, strong=True, engine=None, writeback=None, aoai=None,
            no_candidates=False):
    hit = lambda pid, score: {"parent_id": pid, "title": pid, "chunk_id": pid + "_0", "chunk": CHUNK, "@search.rerankerScore": score}
    docs = [] if no_candidates else ([hit("K1", 3.8), hit("K2", 1.0)] if strong else [hit("K1", 2.0), hit("K2", 1.9)])
    deps = dict(
        allowed_clients=lambda: list(clients), user_id=lambda: me, display_name=lambda: me.title(),
        itsm_access=itsm if callable(itsm) else (lambda: itsm), search_token=lambda: "tok", aoai=aoai or FakeAoai(),
        load_engine_config=lambda c: {"knowledge": {"index": "idx"}, "generation": {"model": "m", "seed": 1}},
        retrieve_hierarchy=lambda q, index, n, a, h, client_id=None: (docs, []),
        fetch_document_chunks=lambda pids, index, h, client_id=None: {
            pids[0]: [{"chunk_id": "k_0", "chunk": CHUNK}]}, engine=engine)
    app = Flask(__name__)
    store = MemoryStore()
    app.register_blueprint(diag_bp.create_diagnostic_blueprint(None, deps, store=store, writeback=writeback))
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
    page = c.get(f"/diag/s/{sid}?client_id=client-s").get_data(as_text=True)
    assert "Cherchez l&#39;icone engrenage." in page and "Trouvé dans la fiche" in page


def test_a_question_not_covered_by_the_fiche_says_so_instead_of_a_badge():
    class UngroundedAoai(FakeAoai):
        def _create(self, **kw):
            if kw["response_format"]["json_schema"]["name"] == "guide_help":
                out = {"answer_fr": "", "found_in_kb": False, "source_chunk_id": "c1"}
                return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(out)))])
            return super()._create(**kw)

    app, _ = make_app(aoai=UngroundedAoai())
    c = app.test_client()
    sid = sid_of(post(c, "/diag/new", {"client_id": "client-s", "text": "x"}))
    post(c, f"/diag/s/{sid}/reply", {"client_id": "client-s", "text": "question hors fiche"})
    page = c.get(f"/diag/s/{sid}?client_id=client-s").get_data(as_text=True)
    assert "n'est pas détaillé dans la fiche" in page and "Trouvé dans la fiche" not in page




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


# ---------------------------------------------------------------- V10 slices 5 and 6
class FakeEngine:
    """fn-kecore as the Diagnostic sees it (app/kecore_client.EngineClient)."""

    def __init__(self, kind="fiche", fail=False):
        self.kind, self.fail, self.calls = kind, fail, []

    def find(self, client, text, observe=None):
        self.calls.append("find")
        if self.fail:
            raise RuntimeError("engine down")
        decision = ({"kind": "fiche", "fiche_id": "KB0120"} if self.kind == "fiche"
                    else {"kind": "question", "options": [{"fiche_id": "KB0120"}, {"fiche_id": "KB0200"}]})
        return {"run_id": "20261007T204049Z-215423", "decision": decision,
                "candidates": [{"fiche_id": "KB0120", "label": "Réinitialiser son mot de passe Windows"},
                               {"fiche_id": "KB0200", "label": "Configurer Outlook"}]}

    def fiche(self, client, fiche_id, run_id=None):
        self.calls.append(("fiche", fiche_id))
        return {"fiche_id": fiche_id, "label": "Réinitialiser son mot de passe Windows", "prerequisites": [],
                "text": "Réinitialiser son mot de passe Windows",
                "steps": [{"n": 1, "text": "Appuyez sur Ctrl+Alt+Suppr puis choisissez Modifier un mot de passe."},
                          {"n": 2, "text": "Saisissez votre ancien mot de passe, puis le nouveau deux fois."}]}


class FakeWriteback:
    """The diagwriteback table (diag_tab.WritebackTable): atomic create, If-Match merge."""

    def __init__(self):
        self.rows, self.n = {}, 0

    def get(self, pk, rk):
        row = self.rows.get((pk, rk))
        return None if row is None else dict(row)

    def create(self, entity):
        key = (entity["PartitionKey"], entity["RowKey"])
        if key in self.rows:
            return False
        self.n += 1
        self.rows[key] = {**entity, "_etag": f"e{self.n}"}
        return True

    def merge(self, entity, etag):
        key = (entity["PartitionKey"], entity["RowKey"])
        if key not in self.rows or self.rows[key]["_etag"] != etag:
            return False
        self.n += 1
        self.rows[key] = {**self.rows[key], **entity, "_etag": f"e{self.n}"}
        return True


def page_of(c, sid):
    return c.get(f"/diag/s/{sid}?client_id=client-s").get_data(as_text=True)


def test_a_fiche_the_engine_shows_is_guided_word_for_word():
    engine = FakeEngine()
    app, _ = make_app(engine=engine)
    c = app.test_client()
    sid = sid_of(post(c, "/diag/new", {"client_id": "client-s", "text": "mot de passe expiré"}))
    page = page_of(c, sid)
    assert "par le moteur déterministe" in page and "Texte exact de la fiche" in page
    assert "Appuyez sur Ctrl+Alt+Suppr" in page and "Étape 1 sur 2" in page
    assert engine.calls[0] == "find"


def test_engine_down_the_search_index_answers():
    app, _ = make_app(engine=FakeEngine(fail=True))
    c = app.test_client()
    page = page_of(c, sid_of(post(c, "/diag/new", {"client_id": "client-s", "text": "reset mdp"})))
    assert "Cette fiche explique la reinitialisation." in page and "moteur déterministe" not in page


def test_a_question_of_the_engine_offers_its_fiches():
    app, _ = make_app(engine=FakeEngine(kind="question"))
    c = app.test_client()
    page = page_of(c, sid_of(post(c, "/diag/new", {"client_id": "client-s", "text": "probleme"})))
    assert "Quelle fiche correspond" in page and "Configurer Outlook" in page and "correspondance forte" not in page


def test_a_ticket_number_comes_from_itsm_agents_only_and_well_formed():
    app, _ = make_app(itsm=False)
    c = app.test_client()
    assert "error=ticket" in post(c, "/diag/new", {"client_id": "client-s", "text": "x",
                                                   "ticket": "INC0010023"}).headers["Location"]
    app, _ = make_app(itsm=True)
    c = app.test_client()
    assert "error=ticket" in post(c, "/diag/new", {"client_id": "client-s", "text": "x",
                                                   "ticket": "INC12"}).headers["Location"]
    sid = sid_of(post(c, "/diag/new", {"client_id": "client-s", "text": "x", "ticket": " inc0010023 "}))
    assert "ticket INC0010023" in page_of(c, sid)


def test_the_note_is_validated_once_and_never_carries_what_was_typed():
    wb = FakeWriteback()
    app, _ = make_app(itsm=True, writeback=wb)
    c = app.test_client()
    sid = sid_of(post(c, "/diag/new", {"client_id": "client-s", "text": "Jean Dupont 0612345678 reset mdp",
                                       "ticket": "INC0010023"}))
    page = page_of(c, sid)
    assert "Ticket INC0010023 : note de résolution" in page and 'value="work_note"' in page
    assert 'value="handover"' not in page                      # no ITSM action for a fiche titled "K1"
    r = post(c, f"/diag/s/{sid}/writeback", {"client_id": "client-s", "kind": "work_note"})
    assert "msg=wb_saved" in r.headers["Location"]
    row = wb.rows[("client-s", f"{sid}-work_note")]
    assert (row["ticketNumber"], row["status"], row["kind"]) == ("INC0010023", "validated", "work_note")
    assert "Ouvrez Parametres." in row["noteText"]
    assert not any(x in row["noteText"] for x in ("Dupont", "0612345678", "reset mdp"))
    again = post(c, f"/diag/s/{sid}/writeback", {"client_id": "client-s", "kind": "work_note"})
    assert "msg=wb_exists" in again.headers["Location"]
    page = page_of(c, sid)
    assert "validé, en attente de l&#39;exécuteur" in page and 'value="work_note"' not in page
    no_action = post(c, f"/diag/s/{sid}/writeback", {"client_id": "client-s", "kind": "handover"})
    assert "msg=wb_no_action" in no_action.headers["Location"]


def test_a_failed_write_can_be_validated_again_a_written_one_never():
    wb = FakeWriteback()
    app, _ = make_app(itsm=True, writeback=wb)
    c = app.test_client()
    sid = sid_of(post(c, "/diag/new", {"client_id": "client-s", "text": "reset mdp", "ticket": "INC0010023"}))
    post(c, f"/diag/s/{sid}/writeback", {"client_id": "client-s", "kind": "work_note"})
    key = ("client-s", f"{sid}-work_note")
    wb.rows[key].update(executionStatus="not_found", executedAtUtc="2026-10-08T10:00:00Z")
    page = page_of(c, sid)
    assert "ticket introuvable dans ServiceNow" in page and "Valider à nouveau la note" in page
    r = post(c, f"/diag/s/{sid}/writeback", {"client_id": "client-s", "kind": "work_note"})
    assert "msg=wb_saved" in r.headers["Location"]
    assert wb.rows[key]["executionStatus"] == "" and wb.rows[key]["executedAtUtc"] == ""
    wb.rows[key].update(executionStatus="success")
    r = post(c, f"/diag/s/{sid}/writeback", {"client_id": "client-s", "kind": "work_note"})
    assert "msg=wb_exists" in r.headers["Location"]


def test_the_write_back_is_for_itsm_agents_and_same_origin_only():
    flags = {"itsm": True}
    wb = FakeWriteback()
    app, _ = make_app(itsm=lambda: flags["itsm"], writeback=wb)
    c = app.test_client()
    sid = sid_of(post(c, "/diag/new", {"client_id": "client-s", "text": "reset mdp", "ticket": "INC0010023"}))
    assert c.post(f"/diag/s/{sid}/writeback", data={"client_id": "client-s", "kind": "work_note"},
                  headers={"Origin": "http://evil.example"}, base_url="http://localhost").status_code == 403
    assert post(c, f"/diag/s/{sid}/writeback", {"client_id": "client-s", "kind": "sql"}).status_code == 400
    assert post(c, f"/diag/s/{sid}/writeback", {"client_id": "other", "kind": "work_note"}).status_code == 404
    flags["itsm"] = False
    assert "note de résolution" not in page_of(c, sid)
    assert post(c, f"/diag/s/{sid}/writeback", {"client_id": "client-s", "kind": "work_note"}).status_code == 403
    assert wb.rows == {}


def test_without_a_ticket_nothing_can_be_written():
    wb = FakeWriteback()
    app, _ = make_app(itsm=True, writeback=wb)
    c = app.test_client()
    sid = sid_of(post(c, "/diag/new", {"client_id": "client-s", "text": "reset mdp"}))
    assert "note de résolution" not in page_of(c, sid)
    r = post(c, f"/diag/s/{sid}/writeback", {"client_id": "client-s", "kind": "work_note"})
    assert "msg=wb_invalid" in r.headers["Location"] and wb.rows == {}


def test_handover_proposes_the_action_the_fiche_title_calls_for():
    wb = FakeWriteback()
    app, _ = make_app(itsm=True, writeback=wb, engine=FakeEngine())
    c = app.test_client()
    sid = sid_of(post(c, "/diag/new", {"client_id": "client-s", "text": "mot de passe expiré",
                                       "ticket": "INC0012345"}))
    assert "Transférer au module ITSM (password_reset)" in page_of(c, sid)
    r = post(c, f"/diag/s/{sid}/writeback", {"client_id": "client-s", "kind": "handover"})
    assert "msg=wb_saved" in r.headers["Location"]
    row = wb.rows[("client-s", f"{sid}-handover")]
    assert row["handoverAction"] == "password_reset" and "Transféré au module ITSM" in row["noteText"]
    assert "KB0120" in row["noteText"] and "kefind:" not in row["noteText"]


def test_messages_are_codes_never_text_from_the_url():
    app, _ = make_app()
    c = app.test_client()
    assert "Appelez le 0800" not in c.get("/diag?client_id=client-s&error=Appelez le 0800").get_data(as_text=True)
    assert "Collez un texte ou joignez une capture." in c.get("/diag?client_id=client-s&error=empty").get_data(as_text=True)


def test_shipped_handover_rules():
    from guide.writeback import handover_action
    rules = diag_bp.WRITEBACK_CONFIG["handover"]
    assert diag_bp.WRITEBACK_CONFIG["table"] == "diagwriteback"
    assert handover_action("Réinitialiser la MFA (Microsoft Authenticator)", rules) == "mfa_reset"
    assert handover_action("Mot de passe oublié ou expiré", rules) == "password_reset"
    assert handover_action("Réinitialisation du mot de passe Windows", rules) == "password_reset"
    assert handover_action("Ajouter un utilisateur à un groupe de sécurité", rules) == "group_add"
    assert handover_action("Changer son mot de passe Windows", rules) is None
    assert handover_action("Configurer Outlook sur iPhone", rules) is None


def test_a_handover_rule_that_does_not_compile_is_dropped(tmp_path):
    p = tmp_path / "wb.yaml"
    p.write_text('handover:\n  - action: mfa_reset\n    label_pattern: "(unclosed"\n'
                 '  - action: group_add\n    label_pattern: "groupe"\n  - just a string\n', encoding="utf-8")
    cfg = diag_bp._load_writeback_config(p)
    assert [r["action"] for r in cfg["handover"]] == ["group_add"] and cfg["table"] == "diagwriteback"
    assert diag_bp._load_writeback_config(tmp_path / "missing.yaml") == {"table": "diagwriteback", "handover": [],
                                                                         "clients": []}


def test_write_back_only_for_the_clients_of_its_executor_and_incidents_only():
    wb = FakeWriteback()
    app, _ = make_app(itsm=True, writeback=wb, clients=("client-s", "clienta"))
    c = app.test_client()
    assert "error=ticket" in post(c, "/diag/new", {"client_id": "client-s", "text": "x",
                                                   "ticket": "RITM0012345"}).headers["Location"]
    sid = sid_of(post(c, "/diag/new", {"client_id": "clienta", "text": "reset mdp", "ticket": "INC0010023"}))
    assert "note de résolution" not in c.get(f"/diag/s/{sid}?client_id=clienta").get_data(as_text=True)
    assert post(c, f"/diag/s/{sid}/writeback", {"client_id": "clienta", "kind": "work_note"}).status_code == 403
    assert wb.rows == {}


def test_a_row_stuck_running_may_be_validated_again_after_a_while():
    from datetime import datetime, timedelta, timezone
    now = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)
    recent = {"executionStatus": "running", "executionStartedUtc": "2026-10-08T11:55:00.1234567Z"}
    stale = {"executionStatus": "running", "executionStartedUtc": "2026-10-08T11:00:00.1234567Z"}
    assert not diag_bp._retryable(recent, now) and diag_bp._retryable(stale, now)
    assert diag_bp._retryable({"executionStatus": "inactive"}, now)
    assert not diag_bp._retryable({"executionStatus": "dry_run"}, now)
    assert not diag_bp._retryable({"executionStatus": ""}, now + timedelta(days=9))
def test_button_clicks_dont_pile_up_as_chat_only_typed_text_does():
    app, _ = make_app()
    c = app.test_client()
    sid = sid_of(post(c, "/diag/new", {"client_id": "client-s", "text": "reset mdp"}))
    post(c, f"/diag/s/{sid}/reply", {"client_id": "client-s", "action": "done"})
    post(c, f"/diag/s/{sid}/reply", {"client_id": "client-s", "text": "ça ne marche pas vraiment"})
    page = page_of(c, sid)
    assert "C'est fait." not in page                    # the step list already marks it done
    assert "ça ne marche pas vraiment" in page           # what was actually typed stays


def test_a_reload_after_an_action_lands_back_on_the_current_step():
    app, _ = make_app()
    c = app.test_client()
    sid = sid_of(post(c, "/diag/new", {"client_id": "client-s", "text": "reset mdp"}))
    r = post(c, f"/diag/s/{sid}/reply", {"client_id": "client-s", "action": "done"})
    assert r.headers["Location"].endswith("#focus")
    page = page_of(c, sid)
    assert page.count('id="focus"') == 1                # exactly one anchor on the page


def test_a_pending_write_back_says_the_executor_polls_every_two_minutes():
    wb = FakeWriteback()
    app, _ = make_app(itsm=True, writeback=wb)
    c = app.test_client()
    sid = sid_of(post(c, "/diag/new", {"client_id": "client-s", "text": "reset mdp", "ticket": "INC0010023"}))
    r = post(c, f"/diag/s/{sid}/writeback", {"client_id": "client-s", "kind": "work_note"})
    assert r.headers["Location"].endswith("#writeback")
    page = page_of(c, sid)
    assert "toutes les 2 minutes" in page
    wb.rows[("client-s", f"{sid}-work_note")]["executionStatus"] = "success"
    assert "toutes les 2 minutes" not in page_of(c, sid)


# -------------------------------------------------------- V10 slice 7 (merge): open fallback
def fake_diagnostic_query_core_keyless(client_id, query, prior_turns, token, aoai,
                                       image_b64=None, image_mime=None, cfg=None):
    return {"answer": "Essayez de redémarrer le poste, puis relancez l'application.",
            "ambiguous": False, "unanswerable_reason": None,
            "primary_source": {"title": "KB-OPEN", "used": True, "sourceType": "text"},
            "related_sources": [{"title": "KB-ANNEXE", "used": True, "sourceType": "text"}],
            "prochaine_verification": "Vérifiez si le message d'erreur revient.",
            "diagnostic_termine": False, "detected_error_codes": [], "screen_reading": None,
            "_trace": {"primary_reranker_score": 2.0}}


def test_no_fiche_matches_falls_back_to_the_classic_assistants_own_answer(monkeypatch):
    import guide.ports as ports_mod
    monkeypatch.setattr(ports_mod, "diagnostic_query_core_keyless", fake_diagnostic_query_core_keyless)
    app, _ = make_app(no_candidates=True)
    c = app.test_client()
    sid = sid_of(post(c, "/diag/new", {"client_id": "client-s", "text": "problème jamais vu"}))
    page = page_of(c, sid)
    assert "Diagnostic ouvert" in page and "Confiance Moyenne" in page
    assert "Essayez de redémarrer le poste" in page
    assert "KB-OPEN" in page and "Vérifiez si le message d&#39;erreur revient." in page
    assert 'value="solved_yes"' in page and 'value="solved_no"' in page


def test_the_deterministic_engine_still_gets_first_and_repeated_refusal_before_the_open_answer(monkeypatch):
    import guide.ports as ports_mod
    monkeypatch.setattr(ports_mod, "diagnostic_query_core_keyless", fake_diagnostic_query_core_keyless)
    app, _ = make_app(engine=FakeEngine(), no_candidates=True)
    c = app.test_client()
    sid = sid_of(post(c, "/diag/new", {"client_id": "client-s", "text": "mot de passe expiré"}))
    page = page_of(c, sid)
    assert "par le moteur déterministe" in page and "Diagnostic ouvert" not in page  # kefind wins when it can


def test_resolving_from_the_open_answer_closes_the_session(monkeypatch):
    import guide.ports as ports_mod
    monkeypatch.setattr(ports_mod, "diagnostic_query_core_keyless", fake_diagnostic_query_core_keyless)
    app, _ = make_app(no_candidates=True)
    c = app.test_client()
    sid = sid_of(post(c, "/diag/new", {"client_id": "client-s", "text": "problème jamais vu"}))
    post(c, f"/diag/s/{sid}/reply", {"client_id": "client-s", "action": "solved_yes"})
    page = page_of(c, sid)
    assert "Problème résolu" in page
