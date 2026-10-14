"""The Web App's client of fn-kecore (V10 slice 5)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

import pytest  # noqa: E402

import kecore_client  # noqa: E402


class Response:
    def __init__(self, status, data):
        self.status_code, self._data = status, data
        self.text = str(data)

    def json(self):
        return self._data


class Session:
    def __init__(self, *responses):
        self.responses, self.calls = list(responses), []

    def request(self, method, url, params=None, json=None, headers=None, timeout=None):
        self.calls.append({"method": method, "url": url, "params": params, "json": json, "headers": headers})
        return self.responses.pop(0)


def test_find_sends_the_key_in_a_header_never_in_the_url():
    session = Session(Response(200, {"decision": {"kind": "fiche"}}))
    client = kecore_client.EngineClient("https://fn/api/", "secret-key", session=session)
    assert client.find("client-s", "Compte bloqué")["decision"]["kind"] == "fiche"
    call = session.calls[0]
    assert call["url"] == "https://fn/api/kecore/find" and "secret-key" not in call["url"]
    assert call["headers"] == {"x-functions-key": "secret-key"}
    assert call["json"]["client"] == "client-s" and call["json"]["interpret"] is True
    assert "observe" not in call["json"]


def test_find_passes_the_session_hash_for_the_dictionary_loop():
    session = Session(Response(200, {"decision": {"kind": "abstain"}}))
    kecore_client.EngineClient("https://fn/api", "k", session=session).find("client-s", "x", observe="ab" * 16)
    assert session.calls[0]["json"]["observe"] == "ab" * 16


def test_no_map_is_none_and_an_error_says_what_failed():
    client = kecore_client.EngineClient("https://fn/api", "k", session=Session(Response(404, {"error": "no map"}),
                                                                                Response(500, {"error": "boom"})))
    assert client.find("client-s", "x") is None
    with pytest.raises(kecore_client.EngineError, match="HTTP 500: boom"):
        client.find("client-s", "x")


def test_the_fiche_route_takes_the_run(monkeypatch):
    session = Session(Response(200, {"fiche_id": "KB0120"}))
    kecore_client.EngineClient("https://fn/api", "k", session=session).fiche("client-s", "KB0120", "r1")
    assert session.calls[0]["params"] == {"client": "client-s", "fiche_id": "KB0120", "run_id": "r1"}


class NotJson(Response):
    def json(self):
        raise ValueError("not JSON")


KEY = "a" * 32


@pytest.mark.parametrize("call, method, route, params, body", [
    (lambda c: c.review("client-s"), "GET", "kecore/review", {"client": "client-s"}, None),
    (lambda c: c.review("client-s", status="stale"), "GET", "kecore/review", {"client": "client-s", "status": "stale"},
     None),
    (lambda c: c.review_fiche("client-s", KEY), "GET", "kecore/review/fiche", {"client": "client-s", "key": KEY}, None),
    (lambda c: c.review_next("client-s", "proposed"), "GET", "kecore/review/next",
     {"client": "client-s", "status": "proposed"}, None),
    (lambda c: c.review_decide({"client": "client-s", "status": "validated"}), "POST", "kecore/review/decision", None,
     {"client": "client-s", "status": "validated"}),
    (lambda c: c.review_carry({"client": "client-s", "by": "Y", "keys": [KEY]}), "POST", "kecore/review/carry", None,
     {"client": "client-s", "by": "Y", "keys": [KEY]}),
    (lambda c: c.review_taxonomy({"client": "client-s", "intents": []}), "POST", "kecore/review/taxonomy", None,
     {"client": "client-s", "intents": []}),
    (lambda c: c.routing_status("client-s"), "GET", "kecore/routing", {"client": "client-s"}, None),
    (lambda c: c.routing_run({"client": "client-s", "measure": True}), "POST", "kecore/routing/runs", None,
     {"client": "client-s", "measure": True}),
    (lambda c: c.routing_activate({"client": "client-s", "build_id": "b1", "by": "Y"}), "POST",
     "kecore/routing/activate", None, {"client": "client-s", "build_id": "b1", "by": "Y"}),
    (lambda c: c.routing_deactivate({"client": "client-s", "by": "Y"}), "POST", "kecore/routing/deactivate", None,
     {"client": "client-s", "by": "Y"}),
])
def test_the_review_and_routing_routes(call, method, route, params, body):
    session = Session(Response(200, {"ok": True}))
    assert call(kecore_client.EngineClient("https://fn/api/", "secret-key", session=session)) == {"ok": True}
    sent = session.calls[0]
    assert (sent["method"], sent["url"], sent["params"], sent["json"]) == (method, f"https://fn/api/{route}", params, body)
    assert sent["headers"] == {"x-functions-key": "secret-key"} and "secret-key" not in sent["url"]


def test_an_engine_error_keeps_the_status_the_message_and_the_orphans():
    orphans = [{"fiche_id": "KB0233 -  Associate", "intent": "ATTRIBUTION_LIGNE"}]
    client = kecore_client.EngineClient("https://fn/api", "k", session=Session(
        Response(409, {"error": "des fiches perdraient leur intention", "orphans": orphans}),
        Response(400, {"error": "entrée 2 (X) : libellé manquant"})))
    with pytest.raises(kecore_client.EngineError) as caught:
        client.review_taxonomy({"client": "client-s"})
    exc = caught.value
    assert str(exc) == "POST kecore/review/taxonomy -> HTTP 409: des fiches perdraient leur intention"  # unchanged form
    assert (exc.status, exc.error, exc.orphans) == (409, "des fiches perdraient leur intention", orphans)
    with pytest.raises(kecore_client.EngineError) as caught:
        client.review_taxonomy({"client": "client-s"})
    assert (caught.value.status, caught.value.error, caught.value.orphans) == (400, "entrée 2 (X) : libellé manquant", [])
    assert "HTTP 4" in str(caught.value)          # what the dictionary tab tests


def test_an_error_body_that_is_not_a_json_object_is_still_an_engine_error():
    client = kecore_client.EngineClient("https://fn/api", "k", session=Session(
        NotJson(502, "<html>Bad gateway</html>"), Response(500, ["not", "an", "object"]),
        Response(409, {"error": "x", "orphans": "not a list"}), Response(500, {"detail": "no error key"})))
    for status, error in ((502, "<html>Bad gateway</html>"), (500, "['not', 'an', 'object']"), (409, "x"), (500, "")):
        with pytest.raises(kecore_client.EngineError) as caught:
            client.review("client-s")
        assert (caught.value.status, caught.value.error, caught.value.orphans) == (status, error, [])


def test_an_engine_error_built_by_hand_stays_compatible():
    exc = kecore_client.EngineError("POST kecore/find -> HTTP 500: boom")
    assert isinstance(exc, RuntimeError) and str(exc) == "POST kecore/find -> HTTP 500: boom"
    assert (exc.status, exc.error, exc.data, exc.orphans) == (None, "", {}, [])


def test_from_env_needs_both_settings_and_a_resolved_key(monkeypatch):
    monkeypatch.delenv("KECORE_FUNCTION_URL", raising=False)
    monkeypatch.delenv("KECORE_FUNCTION_KEY", raising=False)
    assert kecore_client.from_env() is None
    monkeypatch.setenv("KECORE_FUNCTION_URL", "https://fn/api")
    monkeypatch.setenv("KECORE_FUNCTION_KEY", "@Microsoft.KeyVault(SecretUri=https://kv/secrets/x/)")
    assert kecore_client.from_env() is None  # App Service could not resolve the reference
    monkeypatch.setenv("KECORE_FUNCTION_KEY", "real-key")
    assert kecore_client.from_env().key == "real-key"
