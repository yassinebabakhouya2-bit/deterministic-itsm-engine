"""Review of the routing proposals ("Revue", app/review_tab.py), against a fake engine recording bodies."""
import copy
import hashlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))

from flask import Flask  # noqa: E402
from markupsafe import escape  # noqa: E402

import dictionary_tab  # noqa: E402
import itsm  # noqa: E402
import kecore_client  # noqa: E402
import labels  # noqa: E402
import review_tab  # noqa: E402

CLIENT = "client-s"
FICHE = "KB0233 -  Associate a phone line"       # two spaces: never stripped, never in a URL
STALE = "KB0120 – Compte bloqué"                  # en dash
KEY = hashlib.sha256(FICHE.encode("utf-8")).hexdigest()[:32]
STALE_KEY = hashlib.sha256(STALE.encode("utf-8")).hexdigest()[:32]
OTHER_KEY = "b" * 32
SHA = "a" * 64
SEEN = "2026-10-12T09:00:00+00:00"
REF = "ref-" + "c" * 24
EVIL = "<script>alert(1)</script>"
SYSTEM_KEY = "durable-system-key-XYZ"

OVERVIEW = {
    "client": CLIENT, "run_id": "20261012T080000Z-abc123",
    "build": {"build_id": "20261012T090000Z-def456", "kind": "routing", "run_id": "20261012T080000Z-abc123",
              "state": "completed"},
    "running": None,
    "taxonomy": {
        "frozen": None,
        "proposed": {"build_id": "20261012T090000Z-def456", "run_id": "20261012T080000Z-abc123",
                     "intents": [
                         {"id": "ATTRIBUTION_LIGNE", "label_fr": "Attribuer une ligne", "description_fr": "Donner un numéro.",
                          "members": ["I001", "I004"], "fiches": [FICHE], "warnings": [], "origin": "model"},
                         {"id": "DEVERROUILLAGE_COMPTE", "label_fr": "Déverrouiller un compte", "description_fr": "",
                          "members": ["I002"], "fiches": [STALE, "KB0300"], "warnings": ["names the application teams"],
                          "origin": "unmapped"}],
                     "warnings": [], "error": None, "source": {"intents": 4, "fiches": 3, "enrichment_errors": 0}}},
    "counts": {"proposed": 1, "validated": 1, "rejected": 0, "stale": 1, "routable": 3},
    "fiches": [
        {"key": KEY, "fiche_id": FICHE, "label": "Associate a phone line " + EVIL, "status": "proposed",
         "intent_a": "ATTRIBUTION_LIGNE_TELEPHONIQUE", "intent_c": "ATTRIBUTION_LIGNE", "intent": None,
         "primary_app": "teams", "app_inferred": True, "error": None, "referenced": True},
        {"key": STALE_KEY, "fiche_id": STALE, "label": "Compte bloqué", "status": "stale",
         "intent_a": "DEVERROUILLAGE_COMPTE", "intent_c": None, "intent": None, "primary_app": None,
         "app_inferred": False, "error": None, "referenced": False},
        {"key": "not-a-key", "fiche_id": "KB0300 Imprimante", "label": "Imprimante", "status": "validated",
         "intent_a": None, "intent_c": None, "intent": "AJOUT_IMPRIMANTE", "primary_app": None,
         "app_inferred": False, "error": "LLMError: HTTP 429", "referenced": False}],
    "collisions": {"l0_numbers": {"90": ["KB0090 A", "KB0090 B"]},
                   "l1": [{"tokens": ["attribuer", "ligne", "team"],
                           "fiches": [{"fiche_id": FICHE, "label": "Associate"}, {"fiche_id": "KB0234", "label": "Other line"}],
                           "aliases": ["attribuer ligne teams"]}],
                   "l2": [{"intent": "ATTRIBUTION_LIGNE", "app": "", "fiches": ["KB0233", "KB0234"]}]},
    "blocked": {"l1": [{"tokens": ["reset", "mot", "passe"], "fiches": ["KB0400"], "reason": "proposal of KB0401"}],
                "l2": [{"intent": "DEVERROUILLAGE_COMPTE", "app": "entra-id", "fiches": ["KB0120"], "pending": ["KB0121"]}]},
    "routing": {"candidate": {"build_id": "20261012T090000Z-def456", "run_id": "20261012T080000Z-abc123",
                              "tables_sha256": "t" * 64, "content_sha256": "c" * 64,
                              "stats": {"l0_numbers": 150, "l0_codes": 2, "l1": 7, "l2": 0}},
                "active": None, "effective": False, "killed": False,
                "gate": {"sb_id": "20261012T090000Z-def456-sb", "status": "insufficient",
                         "reasons": ["only 2 comparable questions (30 needed)"],
                         "wrong_exact": {"k": 0, "n": 2, "ids": []},
                         "route_first": {"k": 2, "n": 2, "value": 1.0, "ci95": [0.3, 1.0]},
                         "base_first": {"k": 1, "n": 2, "value": 0.5, "ci95": [0.1, 0.9]},
                         "compared": 2, "excluded": ["ref-x"]}},
}

FICHE_DATA = {
    "client": CLIENT, "run_id": "20261012T080000Z-abc123", "build_id": "20261012T090000Z-def456", "key": KEY,
    "fiche_id": FICHE, "label": "Associate a phone line " + EVIL, "source_sha256": SHA, "status": "proposed",
    "position": {"index": 1, "total": 3}, "text_excerpt": "Ouvrir le centre d'administration Teams " + EVIL,
    "proposal_a": {"canonical_intent": "ATTRIBUTION_LIGNE_TELEPHONIQUE", "intent_label_fr": "attribuer une ligne",
                   "primary_app": "teams", "supported_apps": ["teams"], "apps_inferred": ["teams"],
                   "app_inferred": True, "app_evidence": [], "semantic_aliases_fr": ["attribuer une ligne teams"],
                   "semantic_aliases_en": ["assign phone number"], "trigger_keywords": ["ligne", "numéro"],
                   "dropped": [{"field": "semantic_aliases_fr", "text": "ligne 3", "reason": "single digit"}],
                   "error": None},
    "proposal_c": {"intent": "ATTRIBUTION_LIGNE", "core_aliases_fr": ["attribuer ligne teams"],
                   "core_aliases_en": [], "dropped": [], "error": None},
    "decision": None, "previous_decision": None,
    "prefill": {"intent": "ATTRIBUTION_LIGNE", "primary_app": "teams",
                "aliases": [{"text": "attribuer ligne teams", "lang": "fr", "source": "pass_c", "ref_id": "", "checked": True},
                            {"text": "attribuer une ligne " + EVIL, "lang": "fr", "source": "pass_a", "ref_id": "",
                             "checked": False},
                            {"text": "assign phone number", "lang": "en", "source": "pass_a", "ref_id": "", "checked": False}],
                "codes": ["err:0x80070005"]},
    "taxonomy": [{"id": "ATTRIBUTION_LIGNE", "label_fr": "Attribuer une ligne"},
                 {"id": "DEVERROUILLAGE_COMPTE", "label_fr": "Déverrouiller un compte"}],
    "apps": ["outlook", "teams", "webex"],
    "candidate_codes": ["err:0x80070005", "evt:4625"], "l0_number": "233",
    "references": [{"ref_id": REF, "text": "comment attribuer une ligne teams", "expected": [FICHE],
                    "tokens": ["attribuer", "ligne", "team"], "is_alias": False},
                   {"ref_id": "ref-" + "d" * 24, "text": "attribuer ligne teams svp", "expected": [FICHE],
                    "tokens": ["attribuer", "ligne", "team", "svp"], "is_alias": True}],
    "seen": SEEN,
}

STALE_DATA = {**FICHE_DATA, "key": STALE_KEY, "fiche_id": STALE, "label": "Compte bloqué", "status": "stale",
              "seen": "",
              "previous_decision": {"fiche_id": STALE, "source_sha256": "e" * 64, "status": "validated",
                                    "intent": None, "primary_app": "entra-id",
                                    "aliases": [{"text": "compte bloqué", "lang": "fr", "source": "manual", "ref_id": ""}],
                                    "codes": [], "note": "", "decided_by": "Yassine", "decided_at": "2026-10-01T10:00:00+00:00"}}


def http_error(method, route, status, error, **extra):
    data = {"error": error, **extra}
    return kecore_client.EngineError(f"{method} {route} -> HTTP {status}: {error}", status=status, error=error, data=data)


class Engine:
    """A fake fn-kecore: answers from the dictionaries above, records every body; ``fail[name]`` makes the
    named method raise that exception (a list of exceptions: one per call, then success)."""

    def __init__(self, overview=None, fiches=None, next_keys=None, collisions=(), carry_failed=()):
        self.overview = copy.deepcopy(overview if overview is not None else OVERVIEW)
        self.fiches = copy.deepcopy(fiches if fiches is not None else {KEY: FICHE_DATA, STALE_KEY: STALE_DATA})
        self.next_keys = dict(next_keys or {})
        self.collisions, self.carry_failed = list(collisions), list(carry_failed)
        self.fail: dict = {}
        self.calls: list = []

    def _maybe_fail(self, name):
        exc = self.fail.get(name)
        if isinstance(exc, list):
            exc = exc.pop(0) if exc else None
        if exc is not None:
            raise exc

    def review(self, client, status=None):
        self.calls.append(("review", {"client": client, "status": status}))
        self._maybe_fail("review")
        return copy.deepcopy(self.overview)

    def review_fiche(self, client, key):
        self.calls.append(("review_fiche", {"client": client, "key": key}))
        self._maybe_fail("review_fiche")
        if key not in self.fiches:
            raise http_error("GET", "kecore/review/fiche", 404, "fiche inconnue")
        return copy.deepcopy(self.fiches[key])

    def review_next(self, client, status):
        self.calls.append(("review_next", {"client": client, "status": status}))
        self._maybe_fail("review_next")
        return {"key": self.next_keys.get(status)}

    def review_decide(self, body):
        self.calls.append(("review_decide", body))
        self._maybe_fail("review_decide")
        return {"decision": {"status": body["status"]}, "collisions": self.collisions}

    def review_carry(self, body):
        self.calls.append(("review_carry", body))
        self._maybe_fail("review_carry")
        return {"carried": len(body["keys"]) - len(self.carry_failed), "failed": self.carry_failed}

    def review_taxonomy(self, body):
        self.calls.append(("review_taxonomy", body))
        self._maybe_fail("review_taxonomy")
        return {"version": 1, "sha256": "f" * 64, "intents": body["intents"], "orphans": []}

    def routing_status(self, client):
        return self.overview["routing"]

    def routing_run(self, body):
        self.calls.append(("routing_run", body))
        self._maybe_fail("routing_run")
        return {"id": "20261012T100000Z-aaa111",
                "statusQueryGetUri": f"https://fn/runtime/webhooks/durabletask/instances/x?code={SYSTEM_KEY}"}

    def routing_activate(self, body):
        self.calls.append(("routing_activate", body))
        self._maybe_fail("routing_activate")
        return {"active": {"build_id": body["build_id"]}}

    def routing_deactivate(self, body):
        self.calls.append(("routing_deactivate", body))
        self._maybe_fail("routing_deactivate")
        return {"active": None, "killed": True}

    def bodies(self, name):
        return [body for call, body in self.calls if call == name]


def make_app(engine=None, access=True, clients=(CLIENT,), me="Yassine", user_id="oid-1"):
    engine = engine if engine is not None else Engine()
    app = Flask(__name__)
    app.register_blueprint(review_tab.create_review_blueprint(engine, {
        "allowed_clients": lambda: list(clients), "user_id": lambda: user_id, "display_name": lambda: me,
        "label_access": lambda: access}))
    return app, engine


def client_of(app):
    return app.test_client()


def post(c, url, data=None, origin="http://localhost"):
    return c.post(url, data=data or {}, base_url="http://localhost", headers={"Origin": origin} if origin else {})


def get(c, url):
    return c.get(url, base_url="http://localhost")


def decision_form(**overrides):
    form = {"fiche_id": FICHE, "source_sha256": SHA, "seen": SEEN, "queue": "proposed", "decision": "validated",
            "intent": "ATTRIBUTION_LIGNE", "primary_app": "teams", "note": "",
            "alias": ["0"], "alias_text_0": "attribuer ligne teams", "alias_lang_0": "fr", "alias_source_0": "pass_c",
            "alias_ref_0": "",
            "alias_text_1": "attribuer une ligne", "alias_lang_1": "fr", "alias_source_1": "pass_a", "alias_ref_1": "",
            "code": ["err:0x80070005"]}
    form.update(overrides)
    return form


FICHE_URL = f"/review/{CLIENT}/f/{KEY}"
POST_URLS = [FICHE_URL, f"{FICHE_URL}/carry", f"/review/{CLIENT}/carry", f"/review/{CLIENT}/taxonomy",
             f"/review/{CLIENT}/routing/run", f"/review/{CLIENT}/routing/activate",
             f"/review/{CLIENT}/routing/deactivate"]


# ------------------------------------------------------------------------------------------- guards

def test_access_is_refused_without_the_group_or_the_client():
    for app, engine in (make_app(access=False), make_app(clients=("clienta",)), make_app(clients=())):
        c = client_of(app)
        for url in (f"/review?client={CLIENT}", f"/review/{CLIENT}/next", FICHE_URL, f"/review/{CLIENT}/taxonomy"):
            assert get(c, url).status_code == 403, url
        for url in POST_URLS:
            assert post(c, url, decision_form()).status_code == 403, url
        assert engine.calls == []
    for app, _ in (make_app(access=False), make_app(clients=())):   # (one's own first client is the default)
        assert get(client_of(app), "/review").status_code == 403


def test_a_client_not_allowed_in_the_query_is_refused():
    app, engine = make_app(clients=(CLIENT, "client-b"))
    assert get(client_of(app), "/review?client=client-x").status_code == 403
    assert engine.calls == []


def test_posts_from_another_origin_or_without_one_are_refused():
    app, engine = make_app()
    c = client_of(app)
    for url in POST_URLS:
        assert post(c, url, decision_form(), origin=None).status_code == 403, url
        assert post(c, url, decision_form(), origin="https://evil.example").status_code == 403, url
    assert engine.calls == []


def test_a_bad_key_is_a_404_and_never_reaches_the_engine():
    app, engine = make_app()
    c = client_of(app)
    for key in ("KB0233", KEY.upper(), KEY[:31], KEY + "0", KEY + "%0A", "..%2F" + KEY[:27]):
        assert get(c, f"/review/{CLIENT}/f/{key}").status_code == 404, key
        assert post(c, f"/review/{CLIENT}/f/{key}", decision_form()).status_code == 404, key
        assert post(c, f"/review/{CLIENT}/f/{key}/carry").status_code == 404, key
    assert engine.calls == []


def test_an_unknown_fiche_is_a_404():
    app, _ = make_app()
    assert get(client_of(app), f"/review/{CLIENT}/f/{OTHER_KEY}").status_code == 404


def test_without_an_engine_pages_say_so_and_posts_are_503():
    app, _ = make_app()
    app = Flask(__name__)
    app.register_blueprint(review_tab.create_review_blueprint(None, {
        "allowed_clients": lambda: [CLIENT], "user_id": lambda: "u", "display_name": lambda: "Y",
        "label_access": lambda: True}))
    c = client_of(app)
    assert "pas relié" in get(c, "/review").get_data(as_text=True)
    assert "pas relié" in get(c, FICHE_URL).get_data(as_text=True)
    assert "pas relié" in get(c, f"/review/{CLIENT}/taxonomy").get_data(as_text=True)
    assert get(c, f"/review/{CLIENT}/next").status_code == 302
    for url in POST_URLS:
        assert post(c, url, decision_form()).status_code == 503, url


# ------------------------------------------------------------------------------------------- overview

def test_the_overview_shows_counts_fiches_collisions_blocked_and_the_routing_panel():
    app, engine = make_app()
    page = get(client_of(app), "/review").get_data(as_text=True)
    assert engine.calls == [("review", {"client": CLIENT, "status": None})]
    for text in ("à valider", "à revoir", "fiches routables", "ATTRIBUTION_LIGNE_TELEPHONIQUE",
                 "déduite — à valider", "question de référence", "n° 90", "KB0090 A · KB0090 B",
                 "attribuer ligne team", "attribuer ligne teams", "Associate · Other line", "sans application",
                 "en attente de revue : KB0121", "proposal of KB0401", "Build 20261012T090000Z-def456",
                 "L1 alias : 7", "insuffisante", "2 / 2", "1 / 2", "only 2 comparable questions",
                 "Reconstruire et mesurer", "accepter une mesure insuffisante", "Activer",
                 "Reconduire tous les « à revoir » (1)", "tombent simplement en L3", "Routage inactif",
                 "proposition en erreur : LLMError: HTTP 429", "prochain « Reconstruire et mesurer »"):
        assert text in page, text
    assert "Désactiver" not in page               # nothing active, nothing to switch off
    assert 'http-equiv="refresh"' not in page
    assert f'<a href="/review/{CLIENT}/f/{KEY}">' in page and f'/review/{CLIENT}/f/{STALE_KEY}"' in page
    assert "/f/not-a-key" not in page              # a key without a key's shape is never linked
    assert 'name="build_id" value="20261012T090000Z-def456"' in page


def test_fiche_ids_never_appear_in_a_url():
    app, _ = make_app()
    c = client_of(app)
    for url in ("/review", FICHE_URL):
        page = get(c, url).get_data(as_text=True)
        for fiche_id in (FICHE, STALE, "KB0300 Imprimante"):
            assert f"href=\"/review/{CLIENT}/f/{fiche_id}" not in page
            assert f"action=\"/review/{CLIENT}/f/{fiche_id}" not in page


def test_the_overview_filters_by_status_and_ignores_an_unknown_one():
    app, engine = make_app()
    c = client_of(app)
    page = get(c, "/review?status=stale").get_data(as_text=True)
    assert engine.calls[-1] == ("review", {"client": CLIENT, "status": "stale"})
    assert STALE_KEY in page and f"/f/{KEY}" not in page
    page = get(c, "/review?status=bogus").get_data(as_text=True)
    assert engine.calls[-1] == ("review", {"client": CLIENT, "status": None})
    assert STALE_KEY in page and f"/f/{KEY}" in page


def test_labels_and_aliases_are_escaped_everywhere():
    app, _ = make_app()
    c = client_of(app)
    for url in ("/review", FICHE_URL):
        page = get(c, url).get_data(as_text=True)
        assert EVIL not in page, url
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page, url


def test_messages_are_codes_never_query_text():
    app, _ = make_app()
    c = client_of(app)
    page = get(c, "/review?msg=" + EVIL).get_data(as_text=True)
    assert "alert(1)" not in page.replace("&lt;script&gt;alert(1)&lt;/script&gt;", "")
    page = get(c, "/review?msg=Votre+compte+est+suspendu").get_data(as_text=True)
    assert "Votre compte est suspendu" not in page
    for url, code in (("/review", "validated"), (FICHE_URL, "deactivated"), (f"/review/{CLIENT}/taxonomy", "frozen")):
        assert str(escape(review_tab.MESSAGES[code])) in get(c, f"{url}?msg={code}").get_data(as_text=True), code


def test_a_read_error_shows_its_type_never_its_text():
    engine = Engine()
    engine.fail["review"] = http_error("GET", "kecore/review", 500, "secret internal detail")
    page = get(client_of(make_app(engine)[0]), "/review").get_data(as_text=True)
    assert "Lecture impossible : EngineError" in page and "secret internal detail" not in page
    engine.fail["review"] = http_error("GET", "kecore/review", 404, "no map")
    assert "lancez d&#39;abord un run kecore" in get(client_of(make_app(engine)[0]), "/review").get_data(as_text=True)


def test_a_running_rebuild_refreshes_the_page_every_30_seconds():
    overview = copy.deepcopy(OVERVIEW)
    overview["running"] = {"build_id": "20261012T100000Z-aaa111", "kind": "routing", "started_at": "2026-10-12T10:00:00Z"}
    page = get(client_of(make_app(Engine(overview))[0]), "/review").get_data(as_text=True)
    assert '<meta http-equiv="refresh" content="30">' in page
    assert "Reconstruction en cours depuis 2026-10-12T10:00:00Z" in page
    assert '<button class="b-main" disabled>Reconstruire et mesurer</button>' in page
    c = client_of(make_app()[0])
    assert '<button class="b-main" >Reconstruire et mesurer</button>' in get(c, "/review").get_data(as_text=True)
    assert 'http-equiv="refresh"' not in get(c, FICHE_URL).get_data(as_text=True)


def test_a_failed_build_is_shown_in_red():
    overview = copy.deepcopy(OVERVIEW)
    overview["build"]["state"] = "failed"
    page = get(client_of(make_app(Engine(overview))[0]), "/review").get_data(as_text=True)
    assert '<div class="msg err">Dernière reconstruction : 20261012T090000Z-def456' in page and "en échec" in page


def test_the_effective_banner_is_red_when_killed_or_active_but_not_served():
    active = {"build_id": "20261010T090000Z-old000", "run_id": "20261010T080000Z-old", "activated_by": "Yassine",
              "activated_at": "2026-10-10T12:00:00Z", "override": {"by": "Yassine", "reason": "2 questions " + EVIL},
              "carried_from": None, "previous": None}
    cases = [({"active": active, "effective": True, "killed": False}, "Routage actif", "msg ok"),
             ({"active": active, "effective": False, "killed": False}, "Tables actives mais non appliquées", "msg err"),
             ({"active": active, "effective": False, "killed": True}, "Routage désactivé", "msg err"),
             ({"active": None, "effective": False, "killed": True}, "Routage désactivé", "msg err")]
    pages = []
    for routing, text, css in cases:
        overview = copy.deepcopy(OVERVIEW)
        overview["routing"].update(routing)
        page = get(client_of(make_app(Engine(overview))[0]), "/review").get_data(as_text=True)
        pages.append(page)
        assert f'<div class="{css}"><b>{text}' in page, text
        assert ("Désactiver</button>" in page) == (routing["active"] is not None and not routing["killed"])
    # an override is said, with its (escaped) reason; a pointer not served says which run is served
    assert "Activées malgré une mesure insuffisante — motif : « 2 questions &lt;script&gt;" in pages[0]
    assert "construites pour le run 20261010T080000Z-old, la carte servie est 20261012T080000Z-abc123" in pages[1]


def test_the_overview_survives_an_engine_answer_with_missing_blocks():
    page = get(client_of(make_app(Engine({"client": CLIENT}))[0]), "/review").get_data(as_text=True)
    assert "Aucune fiche." in page and "Aucune collision" in page and "Pas encore mesurées." in page


def test_rendering_is_deterministic():
    app, _ = make_app()
    c = client_of(app)
    for url in ("/review", FICHE_URL, f"/review/{CLIENT}/taxonomy"):
        assert get(c, url).get_data() == get(c, url).get_data(), url


# ------------------------------------------------------------------------------------------- one fiche

def test_the_fiche_page_prefills_the_form():
    app, engine = make_app()
    page = get(client_of(app), FICHE_URL).get_data(as_text=True)
    assert engine.calls == [("review_fiche", {"client": CLIENT, "key": KEY})]
    assert f'<input type="hidden" name="fiche_id" value="{FICHE}">' in page
    assert f'<input type="hidden" name="source_sha256" value="{SHA}">' in page
    assert f'<input type="hidden" name="seen" value="{SEEN}">' in page
    assert '<input type="hidden" name="queue" value="proposed">' in page
    assert '<option value="ATTRIBUTION_LIGNE" selected>' in page
    assert '<option value="DEVERROUILLAGE_COMPTE" >' in page
    assert '<option value="teams" selected>' in page
    assert '<input type="checkbox" name="alias" value="0" checked>' in page    # pass C core alias: ticked
    assert '<input type="checkbox" name="alias" value="1" >' in page           # pass A: listed, unticked
    assert 'name="alias_text_1" value="attribuer une ligne &lt;script&gt;alert(1)&lt;/script&gt;"' in page
    assert 'name="alias_source_2" value="pass_a"' in page and 'name="alias_lang_2" value="en"' in page
    assert '<input type="checkbox" name="code" value="err:0x80070005" checked>' in page
    assert '<input type="checkbox" name="code" value="evt:4625" >' in page
    assert f'name="ref_id_0" value="{REF}"' in page and "promouvoir comme alias" in page
    assert "déjà un alias" in page and 'name="ref_id_1"' not in page          # an alias already: nothing to tick
    assert "lu par le routage : attribuer · ligne · team" in page
    assert "ne routent jamais seuls" in page and "ligne, numéro" in page       # trigger keywords: read only
    assert "déduite — à valider" in page and "Numéro de fiche <b>233</b>" in page
    assert "fiche 1 / 3" in page and "ligne 3" in page                        # position, dropped by the code
    for k in range(review_tab.MANUAL_ALIAS_ROWS):
        assert f'name="manual_text_{k}"' in page


def test_a_prefilled_value_no_longer_in_the_lists_is_said_not_selected():
    data = copy.deepcopy(FICHE_DATA)
    data["prefill"].update(intent="TRANSFERT_APPELS", primary_app="zoom")
    page = get(client_of(make_app(Engine(fiches={KEY: data}))[0]), FICHE_URL).get_data(as_text=True)
    assert "absent des listes actuelles" in page and "TRANSFERT_APPELS, zoom" in page
    assert " selected>" not in page


def test_a_re_decision_shows_the_current_decision_and_keeps_its_note():
    data = copy.deepcopy(FICHE_DATA)
    data.update(status="validated", decision={"fiche_id": FICHE, "status": "validated", "intent": "ATTRIBUTION_LIGNE",
                                              "primary_app": "teams", "aliases": [], "codes": [], "decided_by": "Ana",
                                              "decided_at": SEEN, "note": "vu avec <i>l'équipe</i>"})
    page = get(client_of(make_app(Engine(fiches={KEY: data}))[0]), FICHE_URL).get_data(as_text=True)
    assert "Décision actuelle" in page and f"validée par Ana le {SEEN}" in page
    assert ">vu avec &lt;i&gt;l&#39;équipe&lt;/i&gt;</textarea>" in page
    assert '<textarea name="note" maxlength="500" rows="2"></textarea>' in get(
        client_of(make_app()[0]), FICHE_URL).get_data(as_text=True)


def test_without_a_frozen_taxonomy_the_intent_stays_empty():
    data = copy.deepcopy(FICHE_DATA)
    data["taxonomy"] = []
    page = get(client_of(make_app(Engine(fiches={KEY: data}))[0]), FICHE_URL).get_data(as_text=True)
    assert '<input type="hidden" name="intent" value="">' in page and "Aucune taxonomie figée" in page


def test_a_decision_reaches_the_engine_with_the_hidden_fields_and_goes_to_the_next_fiche():
    app, engine = make_app()
    r = post(client_of(app), FICHE_URL, decision_form(note="vu avec l'équipe"))
    assert r.status_code == 302
    assert r.headers["Location"] == f"/review/{CLIENT}/next?status=proposed&msg=validated"
    assert engine.bodies("review_decide") == [{
        "client": CLIENT, "by": "Yassine", "by_id": "oid-1", "fiche_id": FICHE, "source_sha256": SHA, "seen": SEEN,
        "status": "validated", "intent": "ATTRIBUTION_LIGNE", "primary_app": "teams",
        "aliases": [{"text": "attribuer ligne teams", "lang": "fr", "source": "pass_c", "ref_id": ""}],
        "codes": ["err:0x80070005"], "note": "vu avec l'équipe"}]


def test_promoted_references_and_typed_aliases_reach_the_body_in_form_order():
    app, engine = make_app(user_id="")
    post(client_of(app), FICHE_URL, decision_form(
        alias=["1", "0", "1"], ref=["0"], ref_id_0=REF, ref_text_0="comment  attribuer une ligne teams",
        manual_text_0="  attribuer   numéro teams ", manual_lang_0="fr", manual_text_1="",
        manual_text_2="assign teams number", manual_lang_2="en", code=["evt:4625", "evt:4625"]))
    body = engine.bodies("review_decide")[0]
    assert "by_id" not in body                       # no user id: none sent
    assert body["aliases"] == [
        {"text": "attribuer ligne teams", "lang": "fr", "source": "pass_c", "ref_id": ""},
        {"text": "attribuer une ligne", "lang": "fr", "source": "pass_a", "ref_id": ""},
        {"text": "comment attribuer une ligne teams", "lang": "fr", "source": "reference", "ref_id": REF},
        {"text": "attribuer numéro teams", "lang": "fr", "source": "manual", "ref_id": ""},
        {"text": "assign teams number", "lang": "en", "source": "manual", "ref_id": ""}]
    assert body["codes"] == ["evt:4625"]


def test_a_rejection_and_a_stale_queue_go_to_the_right_next_fiche():
    app, engine = make_app()
    r = post(client_of(app), FICHE_URL, decision_form(decision="rejected", queue="stale"))
    assert r.headers["Location"] == f"/review/{CLIENT}/next?status=stale&msg=rejected"
    r = post(client_of(app), FICHE_URL, decision_form(queue="bogus"))
    assert r.headers["Location"] == f"/review/{CLIENT}/next?status=proposed&msg=validated"
    assert [b["status"] for b in engine.bodies("review_decide")] == ["rejected", "validated"]


def test_a_validated_alias_shared_with_another_fiche_is_flagged():
    app, _ = make_app(Engine(collisions=[{"alias": "attribuer ligne teams", "fiches": ["KB0234"]}]))
    r = post(client_of(app), FICHE_URL, decision_form())
    assert r.headers["Location"].endswith("msg=validated_collision")
    app, _ = make_app(Engine(collisions=[{"alias": "x y", "fiches": ["KB0234"]}]))
    assert post(client_of(app), FICHE_URL, decision_form(decision="rejected")).headers["Location"].endswith("msg=rejected")


def test_engine_refusals_come_back_to_the_fiche_as_codes():
    cases = [(http_error("POST", "kecore/review/decision", 409, "le texte a changé"), "changed"),
             (http_error("POST", "kecore/review/decision", 400, "alias « ligne 3 » : le routage ignore « 3 »"), "error"),
             (http_error("POST", "kecore/review/decision", 404, "fiche inconnue"), "error"),
             (http_error("POST", "kecore/review/decision", 500, "boom"), "unavailable"),
             (ConnectionError("reset"), "unavailable"),
             (RuntimeError("POST kecore/review/decision -> HTTP 409: changed"), "changed")]
    for exc, code in cases:
        engine = Engine()
        engine.fail["review_decide"] = exc
        r = post(client_of(make_app(engine)[0]), FICHE_URL, decision_form())
        assert r.headers["Location"] == f"/review/{CLIENT}/f/{KEY}?msg={code}", (exc, code)
        assert "ligne 3" not in r.headers["Location"]


def test_bad_decision_forms_are_refused_before_the_engine():
    app, engine = make_app()
    c = client_of(app)
    bad = [dict(decision="delete"), dict(decision=""), dict(fiche_id="KB0234 Other"), dict(fiche_id=""),
           dict(fiche_id=FICHE.replace("  ", " ")),        # the id must hash to the key: never collapsed
           dict(source_sha256="A" * 64), dict(source_sha256="a" * 63), dict(seen="<b>"),
           dict(intent="attribution_ligne"), dict(intent="AB"), dict(primary_app="Teams"), dict(primary_app="-x"),
           dict(alias=["x"]), dict(alias=["999"]), dict(alias=["-1"]), dict(alias_lang_0="de"),
           dict(alias_source_0="model"), dict(alias_ref_0="ref-bad"), dict(alias_text_0=""),
           dict(alias_text_0="x" * 501), dict(ref=["0"], ref_id_0="ref-x", ref_text_0="a b"),
           dict(ref=["0"], ref_id_0=REF, ref_text_0=""), dict(manual_text_0="a b", manual_lang_0="es"),
           dict(code=["err:\n0x5"]), dict(code=[""])]
    for overrides in bad:
        assert post(c, FICHE_URL, decision_form(**overrides)).status_code == 400, overrides
    assert engine.calls == []


def test_next_redirects_to_the_next_fiche_or_back_to_the_overview():
    engine = Engine(next_keys={"proposed": KEY, "stale": None})
    c = client_of(make_app(engine)[0])
    r = get(c, f"/review/{CLIENT}/next?msg=validated")
    assert r.headers["Location"] == f"/review/{CLIENT}/f/{KEY}?msg=validated"
    assert engine.calls[-1] == ("review_next", {"client": CLIENT, "status": "proposed"})
    assert get(c, f"/review/{CLIENT}/next?status=proposed&msg=" + EVIL).headers["Location"] == f"/review/{CLIENT}/f/{KEY}"
    assert get(c, f"/review/{CLIENT}/next?status=stale").headers["Location"] == f"/review?client={CLIENT}&status=stale&msg=done"
    assert get(c, f"/review/{CLIENT}/next?status=stale&msg=carried").headers["Location"].endswith("msg=carried")
    assert get(c, f"/review/{CLIENT}/next?status=validated").status_code == 400
    engine.next_keys["proposed"] = "../" + KEY      # a key that is not a key is never followed
    assert get(c, f"/review/{CLIENT}/next").headers["Location"] == f"/review?client={CLIENT}&status=proposed&msg=done"
    engine.fail["review_next"] = ConnectionError("down")
    assert get(c, f"/review/{CLIENT}/next").headers["Location"].endswith("msg=unavailable")


# ------------------------------------------------------------------------------------------- carry-over

def test_a_stale_fiche_shows_its_previous_decision_and_can_carry_it():
    app, engine = make_app(Engine(next_keys={"stale": None}))
    c = client_of(app)
    page = get(c, f"/review/{CLIENT}/f/{STALE_KEY}").get_data(as_text=True)
    assert "Décision précédente" in page and "« compte bloqué »" in page and "Reconduire la décision précédente" in page
    assert '<input type="hidden" name="queue" value="stale">' in page
    assert "Reconduire la décision précédente" not in get(c, FICHE_URL).get_data(as_text=True)
    r = post(c, f"/review/{CLIENT}/f/{STALE_KEY}/carry")
    assert r.headers["Location"] == f"/review/{CLIENT}/next?status=stale&msg=carried"
    assert engine.bodies("review_carry") == [{"client": CLIENT, "by": "Yassine", "keys": [STALE_KEY]}]


def test_a_refused_carry_comes_back_to_the_fiche():
    engine = Engine(carry_failed=[{"key": STALE_KEY, "error": "alias « compte bloqué » : ..."}])
    r = post(client_of(make_app(engine)[0]), f"/review/{CLIENT}/f/{STALE_KEY}/carry")
    assert r.headers["Location"] == f"/review/{CLIENT}/f/{STALE_KEY}?msg=carry_failed"
    engine = Engine()
    engine.fail["review_carry"] = http_error("POST", "kecore/review/carry", 500, "boom")
    r = post(client_of(make_app(engine)[0]), f"/review/{CLIENT}/f/{STALE_KEY}/carry")
    assert r.headers["Location"] == f"/review/{CLIENT}/f/{STALE_KEY}?msg=unavailable"


def test_carry_all_takes_the_stale_keys_from_the_engine_never_from_the_form():
    app, engine = make_app()
    c = client_of(app)
    r = post(c, f"/review/{CLIENT}/carry", {"keys": [KEY, OTHER_KEY]})
    assert r.headers["Location"] == f"/review?client={CLIENT}&status=stale&msg=carried"
    assert engine.calls[0] == ("review", {"client": CLIENT, "status": "stale"})
    assert engine.bodies("review_carry") == [{"client": CLIENT, "by": "Yassine", "keys": [STALE_KEY]}]
    engine.carry_failed = [{"key": STALE_KEY, "error": "x"}]
    assert post(c, f"/review/{CLIENT}/carry").headers["Location"].endswith("msg=carried_partial")
    overview = copy.deepcopy(OVERVIEW)
    overview["fiches"] = [f for f in overview["fiches"] if f["status"] != "stale"]
    app, engine = make_app(Engine(overview))
    assert post(client_of(app), f"/review/{CLIENT}/carry").headers["Location"].endswith("msg=nothing_stale")
    assert engine.bodies("review_carry") == []


# ------------------------------------------------------------------------------------------- taxonomy

def taxonomy_form(**overrides):
    form = {"source": "proposed", "source_build": "20261012T090000Z-def456", "decision": "freeze", "rows": "5",
            "orig_0": "ATTRIBUTION_LIGNE", "id_0": "ATTRIBUTION_LIGNE", "label_fr_0": "Attribuer une ligne",
            "description_fr_0": "Donner un numéro.",
            "orig_1": "DEVERROUILLAGE_COMPTE", "id_1": "DEVERROUILLAGE_COMPTE", "label_fr_1": "Déverrouiller",
            "description_fr_1": "",
            "orig_2": "", "id_2": "TRANSFERT_APPELS", "label_fr_2": "Transférer les appels", "description_fr_2": "",
            "orig_3": "", "id_3": "", "label_fr_3": "", "description_fr_3": "",
            "orig_4": "", "id_4": "  ", "label_fr_4": "", "description_fr_4": ""}
    form.update(overrides)
    return form


def test_the_taxonomy_page_shows_the_proposal_with_blank_rows_and_the_cost():
    app, _ = make_app()
    page = get(client_of(app), f"/review/{CLIENT}/taxonomy").get_data(as_text=True)
    assert 'name="source" value="proposed"' in page and 'name="source_build" value="20261012T090000Z-def456"' in page
    assert 'name="rows" value="5"' in page                        # 2 rows + 3 blank ones
    assert 'name="id_0" value="ATTRIBUTION_LIGNE"' in page and 'name="id_4" value=""' in page
    assert "I001, I004" in page and "2 fiche(s)" in page and "non regroupée" in page
    assert "names the application teams" in page
    assert "Figer la proposition telle quelle" in page
    assert "environ 3 appels du modèle, 30 à 50 min" in page      # the cost, before the freeze
    assert 'onsubmit="return confirm(' in page


def test_the_taxonomy_page_edits_the_frozen_one_first():
    overview = copy.deepcopy(OVERVIEW)
    overview["taxonomy"]["frozen"] = {"version": 2, "sha256": "f" * 64, "frozen_by": "Yassine",
                                      "frozen_at": "2026-10-11T10:00:00Z", "source_build": "20261011T090000Z-xyz000",
                                      "intents": [{"id": "ATTRIBUTION_LIGNE", "label_fr": "Attribuer",
                                                   "description_fr": "", "fiches": 4}]}
    c = client_of(make_app(Engine(overview))[0])
    page = get(c, f"/review/{CLIENT}/taxonomy").get_data(as_text=True)
    assert "Figée : version 2" in page and 'name="source" value="frozen"' in page
    assert 'name="source_build" value="20261011T090000Z-xyz000"' in page and "4 fiche(s)" in page
    assert "Figer la proposition telle quelle" not in page
    page = get(c, f"/review/{CLIENT}/taxonomy?source=proposed").get_data(as_text=True)
    assert 'name="source" value="proposed"' in page and "Figer la proposition telle quelle" in page


def test_the_taxonomy_page_without_any_proposal_says_what_to_do():
    overview = copy.deepcopy(OVERVIEW)
    overview["taxonomy"] = {"frozen": None, "proposed": None}
    page = get(client_of(make_app(Engine(overview))[0]), f"/review/{CLIENT}/taxonomy").get_data(as_text=True)
    assert "Pas encore de proposition" in page and 'name="rows"' not in page


def test_a_freeze_posts_the_rows_kept_skipping_deleted_and_blank_ones():
    app, engine = make_app()
    r = post(client_of(app), f"/review/{CLIENT}/taxonomy", taxonomy_form(delete=["1"]))
    assert r.headers["Location"] == f"/review?client={CLIENT}&msg=frozen"
    assert engine.bodies("review_taxonomy") == [{
        "client": CLIENT, "by": "Yassine", "source_build": "20261012T090000Z-def456",
        "intents": [{"id": "ATTRIBUTION_LIGNE", "label_fr": "Attribuer une ligne", "description_fr": "Donner un numéro."},
                    {"id": "TRANSFERT_APPELS", "label_fr": "Transférer les appels", "description_fr": ""}]}]


def test_freeze_as_is_takes_the_proposal_from_the_engine():
    app, engine = make_app()
    c = client_of(app)
    r = post(c, f"/review/{CLIENT}/taxonomy", taxonomy_form(decision="as_is", id_0="EDITED_IN_THE_FORM", delete=["1"]))
    assert r.headers["Location"].endswith("msg=frozen")
    assert engine.bodies("review_taxonomy")[0]["intents"] == [
        {"id": "ATTRIBUTION_LIGNE", "label_fr": "Attribuer une ligne", "description_fr": "Donner un numéro."},
        {"id": "DEVERROUILLAGE_COMPTE", "label_fr": "Déverrouiller un compte", "description_fr": ""}]
    r = post(c, f"/review/{CLIENT}/taxonomy", taxonomy_form(decision="as_is", source_build="20261001T000000Z-older0"))
    assert r.headers["Location"] == f"/review/{CLIENT}/taxonomy?msg=changed"   # another proposal since shown
    assert len(engine.bodies("review_taxonomy")) == 1
    assert post(c, f"/review/{CLIENT}/taxonomy", taxonomy_form(decision="as_is", source="frozen")).status_code == 400


def test_a_refused_freeze_re_renders_the_posted_rows_with_the_engine_message():
    engine = Engine()
    engine.fail["review_taxonomy"] = http_error("POST", "kecore/review/taxonomy", 400,
                                                "entrée 2 (TRANSFERT_APPELS) : libellé manquant <b>")
    r = post(client_of(make_app(engine)[0]), f"/review/{CLIENT}/taxonomy",
             taxonomy_form(delete=["1"], label_fr_2="", id_0="ATTRIBUTION_LIGNE_EDITEE"))
    page = r.get_data(as_text=True)
    assert r.status_code == 400
    assert "entrée 2 (TRANSFERT_APPELS) : libellé manquant &lt;b&gt;" in page
    assert 'name="id_0" value="ATTRIBUTION_LIGNE_EDITEE"' in page                 # the edit is kept
    assert 'name="id_1" value="DEVERROUILLAGE_COMPTE"' in page                    # the deleted row too, still ticked
    assert '<input type="checkbox" name="delete" value="1" checked>' in page
    assert 'name="id_2" value="TRANSFERT_APPELS"' in page
    assert '<td class="muted">2</td>' in page                                     # numbered as the engine counts
    assert 'name="rows" value="6"' in page                                        # 3 rows + 3 fresh blank ones
    assert "I001, I004" in page                                                   # members found again by id
    assert "orphans_ok" not in page


def test_orphans_ask_for_a_confirmation_then_freeze_with_it():
    engine = Engine()
    engine.fail["review_taxonomy"] = [http_error(
        "POST", "kecore/review/taxonomy", 409, "des fiches validées perdraient leur intention",
        orphans=[{"fiche_id": FICHE, "label": "Associate " + EVIL, "intent": "ATTRIBUTION_LIGNE"}, "KB0300"])]
    c = client_of(make_app(engine)[0])
    r = post(c, f"/review/{CLIENT}/taxonomy", taxonomy_form(delete=["0"]))
    page = r.get_data(as_text=True)
    assert r.status_code == 409
    assert "Fiches qui perdraient leur intention validée (2)" in page
    assert "Associate &lt;script&gt;" in page and "— ATTRIBUTION_LIGNE" in page and "KB0300" in page
    assert '<input type="checkbox" name="orphans_ok" value="1">' in page
    assert "orphans_ok" not in engine.bodies("review_taxonomy")[0]
    r = post(c, f"/review/{CLIENT}/taxonomy", taxonomy_form(delete=["0"], orphans_ok="1"))
    assert r.headers["Location"].endswith("msg=frozen")
    assert engine.bodies("review_taxonomy")[1]["orphans_ok"] is True


def test_a_freeze_conflict_without_orphans_is_shown_without_a_confirmation():
    engine = Engine()
    engine.fail["review_taxonomy"] = http_error("POST", "kecore/review/taxonomy", 409,
                                                "une autre version vient d'être figée")
    r = post(client_of(make_app(engine)[0]), f"/review/{CLIENT}/taxonomy", taxonomy_form())
    assert r.status_code == 409 and "une autre version vient d&#39;être figée" in r.get_data(as_text=True)
    assert "orphans_ok" not in r.get_data(as_text=True)


def test_an_unreachable_engine_on_freeze_redirects_with_a_code():
    engine = Engine()
    engine.fail["review_taxonomy"] = ConnectionError("down")
    r = post(client_of(make_app(engine)[0]), f"/review/{CLIENT}/taxonomy", taxonomy_form())
    assert r.headers["Location"] == f"/review/{CLIENT}/taxonomy?source=proposed&msg=unavailable"


def test_bad_taxonomy_forms_are_refused():
    app, engine = make_app()
    c = client_of(app)
    for overrides in (dict(decision="publish"), dict(source="mine"), dict(source_build="bad id"),
                      dict(rows="x"), dict(rows="9999"), dict(delete=["abc"]), dict(label_fr_0="x" * 1001)):
        assert post(c, f"/review/{CLIENT}/taxonomy", taxonomy_form(**overrides)).status_code == 400, overrides
    assert engine.bodies("review_taxonomy") == []


def test_a_large_taxonomy_can_delete_its_last_rows():
    rows = {"rows": "203", "source": "proposed", "source_build": "20261012T090000Z-def456", "decision": "freeze",
            "delete": ["202"]}
    for i in range(203):
        rows.update({f"id_{i}": f"INTENTION_{i:03d}", f"label_fr_{i}": f"Intention {i}"})
    app, engine = make_app()
    assert post(client_of(app), f"/review/{CLIENT}/taxonomy", rows).status_code == 302
    assert len(engine.bodies("review_taxonomy")[0]["intents"]) == 202


# ------------------------------------------------------------------------------------------- routing tables

def test_a_rebuild_starts_and_its_durable_answer_is_never_shown():
    app, engine = make_app()
    c = client_of(app)
    r = post(c, f"/review/{CLIENT}/routing/run")
    assert r.headers["Location"] == f"/review?client={CLIENT}&msg=run_started"
    assert SYSTEM_KEY not in r.get_data(as_text=True) and SYSTEM_KEY not in r.headers["Location"]
    assert engine.bodies("routing_run") == [{"client": CLIENT, "measure": True}]
    engine.fail["routing_run"] = http_error("POST", "kecore/routing/runs", 409, "a routing run is in progress")
    assert post(c, f"/review/{CLIENT}/routing/run").headers["Location"].endswith("msg=running")
    engine.fail["routing_run"] = http_error("POST", "kecore/routing/runs", 503, "busy")
    assert post(c, f"/review/{CLIENT}/routing/run").headers["Location"].endswith("msg=unavailable")


def test_activation_with_and_without_the_insufficient_override():
    app, engine = make_app()
    c = client_of(app)
    url = f"/review/{CLIENT}/routing/activate"
    build = "20261012T090000Z-def456"
    assert post(c, url, {"build_id": build}).headers["Location"] == f"/review?client={CLIENT}&msg=activated"
    r = post(c, url, {"build_id": build, "accept_insufficient": "1", "reason": "  2 questions   seulement "})
    assert r.headers["Location"].endswith("msg=activated")
    r = post(c, url, {"build_id": build, "reason": "ignored without the box"})
    assert engine.bodies("routing_activate") == [
        {"client": CLIENT, "build_id": build, "by": "Yassine"},
        {"client": CLIENT, "build_id": build, "by": "Yassine", "accept_insufficient": True, "reason": "2 questions seulement"},
        {"client": CLIENT, "build_id": build, "by": "Yassine"}]
    r = post(c, url, {"build_id": build, "accept_insufficient": "1", "reason": "   "})
    assert r.headers["Location"].endswith("msg=reason_missing") and len(engine.bodies("routing_activate")) == 3
    assert post(c, url, {"build_id": build, "accept_insufficient": "1", "reason": "x" * 501}).status_code == 400
    for bad in ("", "../x", "a b", "x" * 65):
        assert post(c, url, {"build_id": bad}).status_code == 400, bad
    engine.fail["routing_activate"] = http_error("POST", "kecore/routing/activate", 409, "gate not passed")
    assert post(c, url, {"build_id": build}).headers["Location"].endswith("msg=activate_refused")
    engine.fail["routing_activate"] = http_error("POST", "kecore/routing/activate", 400, "bad body")
    assert post(c, url, {"build_id": build}).headers["Location"].endswith("msg=error")
    assert len(engine.bodies("routing_activate")) == 5


def test_deactivation_reaches_the_engine_with_who_did_it():
    app, engine = make_app()
    c = client_of(app)
    r = post(c, f"/review/{CLIENT}/routing/deactivate")
    assert r.headers["Location"] == f"/review?client={CLIENT}&msg=deactivated"
    assert engine.bodies("routing_deactivate") == [{"client": CLIENT, "by": "Yassine"}]
    engine.fail["routing_deactivate"] = ConnectionError("down")
    assert post(c, f"/review/{CLIENT}/routing/deactivate").headers["Location"].endswith("msg=unavailable")


# ------------------------------------------------------------------------------------------- pure helpers, nav

def test_the_key_binding_matches_the_engine_key():
    assert review_tab.fiche_key(FICHE) == KEY and len(KEY) == 32
    assert review_tab.fiche_key(FICHE.replace("  ", " ")) != KEY


def test_freeze_intents_number_the_rows_as_the_engine_does():
    rows = [{"id": "A_B_C", "label_fr": "x", "description_fr": "", "delete": False, "n": None},
            {"id": "", "label_fr": "", "description_fr": " ", "delete": False, "n": None},
            {"id": "D_E_F", "label_fr": "", "description_fr": "", "delete": True, "n": None},
            {"id": " G_H_I ", "label_fr": " y ", "description_fr": "z", "delete": False, "n": None}]
    assert review_tab.freeze_intents(rows) == [{"id": "A_B_C", "label_fr": "x", "description_fr": ""},
                                               {"id": "G_H_I", "label_fr": "y", "description_fr": "z"}]
    assert [r["n"] for r in rows] == [1, None, None, 2]


def test_every_tab_links_to_the_review():
    for page in (labels.PAGE, dictionary_tab.PAGE, itsm.ITSM_PAGE):
        assert '<a href="/review">Revue</a>' in page
    assert '<a class="on" href="/review">Revue</a>' in review_tab.PAGE
