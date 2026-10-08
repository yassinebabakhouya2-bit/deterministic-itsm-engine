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


def test_from_env_needs_both_settings_and_a_resolved_key(monkeypatch):
    monkeypatch.delenv("KECORE_FUNCTION_URL", raising=False)
    monkeypatch.delenv("KECORE_FUNCTION_KEY", raising=False)
    assert kecore_client.from_env() is None
    monkeypatch.setenv("KECORE_FUNCTION_URL", "https://fn/api")
    monkeypatch.setenv("KECORE_FUNCTION_KEY", "@Microsoft.KeyVault(SecretUri=https://kv/secrets/x/)")
    assert kecore_client.from_env() is None  # App Service could not resolve the reference
    monkeypatch.setenv("KECORE_FUNCTION_KEY", "real-key")
    assert kecore_client.from_env().key == "real-key"
