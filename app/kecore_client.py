# =====================================================================
# The Web App's client of the deterministic engine (fn-kecore, V10 slice 5).
#
# The engine runs in its own Function App; the Web App only calls its API. The function key is
# never in code or in a file: App Service resolves the app setting KECORE_FUNCTION_KEY from Key
# Vault (a Key Vault reference, infra/modules/kecore-link.bicep) with the Web App's managed
# identity, which may read that one secret and nothing else. Without the two settings the client
# is absent and the Diagnostic keeps the search index alone, as before.
# =====================================================================
import os

import requests


class EngineError(RuntimeError):
    pass


class EngineClient:
    def __init__(self, base_url: str, key: str, timeout: float = 30.0, session=None):
        self.base_url = base_url.rstrip("/")
        self.key = key
        self.timeout = timeout
        self.session = session or requests.Session()

    def _call(self, method: str, route: str, *, params=None, body=None, allow_404=False):
        response = self.session.request(method, f"{self.base_url}/{route}", params=params, json=body,
                                        headers={"x-functions-key": self.key}, timeout=self.timeout)
        if allow_404 and response.status_code == 404:
            return None
        if response.status_code >= 400:
            try:
                message = response.json().get("error", "")
            except ValueError:
                message = response.text[:200]
            raise EngineError(f"{method} {route} -> HTTP {response.status_code}: {message}")
        return response.json()

    def find(self, client: str, text: str, answers=(), interpret: bool = True, observe=None):
        """The engine's decision for a ticket; None when the client has no KB map yet. ``observe``: a hash
        of the asking session, for the dictionary's online loop (a name counts once per session)."""
        body = {"client": client, "text": text[:20000], "answers": list(answers), "interpret": interpret}
        if observe:
            body["observe"] = observe
        return self._call("POST", "kecore/find", allow_404=True, body=body)

    def fiche(self, client: str, fiche_id: str, run_id=None):
        params = {"client": client, "fiche_id": fiche_id}
        if run_id:
            params["run_id"] = run_id
        return self._call("GET", "kecore/fiche", params=params)

    def dictionary(self, client: str):
        return self._call("GET", "kecore/dictionary", params={"client": client})

    def decide(self, body: dict):
        return self._call("POST", "kecore/dictionary/decision", body=body)


def from_env():
    """The engine client when the Web App is linked to the engine (two app settings), else None."""
    url, key = os.environ.get("KECORE_FUNCTION_URL", ""), os.environ.get("KECORE_FUNCTION_KEY", "")
    if not url or not key or key.startswith("@Microsoft.KeyVault"):  # an unresolved reference is not a key
        return None
    return EngineClient(url, key)


__all__ = ["EngineClient", "EngineError", "from_env"]
