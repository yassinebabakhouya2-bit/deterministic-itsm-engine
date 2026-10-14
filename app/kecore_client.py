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
    """The engine refused or failed a call. ``str(exc)`` keeps its historical form
    "<METHOD> <route> -> HTTP <status>: <message>" (the tabs test "HTTP 4" in it), and the parts are also
    kept apart, because some answers matter beyond "refused": ``status`` (the HTTP status, None for an
    error built without one), ``error`` (the Function's own message -- French for the review routes, shown
    to the reviewer by the taxonomy freeze) and ``data`` (the parsed JSON body, {} when there was none: a
    freeze refused with 409 lists the fiches it would orphan, read as ``orphans``)."""

    def __init__(self, message: str = "", *, status: int | None = None, error: str = "", data=None):
        super().__init__(message)
        self.status = status
        self.error = error or ""
        self.data = data if isinstance(data, dict) else {}

    @property
    def orphans(self) -> list:
        value = self.data.get("orphans")
        return value if isinstance(value, list) else []


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
                data = response.json()
            except ValueError:
                data = None
            if isinstance(data, dict):
                message = str(data.get("error") or "")
            else:  # not JSON, or JSON that is not an object (a proxy's page, a bare string)
                data, message = None, response.text[:200]
            raise EngineError(f"{method} {route} -> HTTP {response.status_code}: {message}",
                              status=response.status_code, error=message, data=data)
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

    # --- review of the routing proposals and the routing tables (app/review_tab.py) -------------------
    # The Web App has no Blob role and does not ship kefind: proposals, decisions, the taxonomy and the
    # tables are read and written only through these routes, and fn-kecore validates every write again.

    def review(self, client: str, status=None):
        """The overview of the review: counts, fiches (``status`` filters them when given), collisions,
        the taxonomy (frozen and proposed) and the routing state (candidate, active, gate)."""
        params = {"client": client}
        if status:
            params["status"] = status
        return self._call("GET", "kecore/review", params=params)

    def review_fiche(self, client: str, key: str):
        """One fiche to review, by its key (sha256 of the fiche id, 32 hex): never the fiche id itself,
        a document name with spaces and dashes that does not belong in a URL."""
        return self._call("GET", "kecore/review/fiche", params={"client": client, "key": key})

    def review_next(self, client: str, status: str):
        """{"key": the next fiche of that status to review, or None}."""
        return self._call("GET", "kecore/review/next", params={"client": client, "status": status})

    def review_decide(self, body: dict):
        return self._call("POST", "kecore/review/decision", body=body)

    def review_carry(self, body: dict):
        return self._call("POST", "kecore/review/carry", body=body)

    def review_taxonomy(self, body: dict):
        return self._call("POST", "kecore/review/taxonomy", body=body)

    def routing_status(self, client: str):
        return self._call("GET", "kecore/routing", params={"client": client})

    def routing_run(self, body: dict):
        """Starts a rebuild of the routing tables (202, the durable check-status answer: it carries the
        durable system key in its URLs, so it is never shown in a page)."""
        return self._call("POST", "kecore/routing/runs", body=body)

    def routing_activate(self, body: dict):
        return self._call("POST", "kecore/routing/activate", body=body)

    def routing_deactivate(self, body: dict):
        return self._call("POST", "kecore/routing/deactivate", body=body)


def from_env():
    """The engine client when the Web App is linked to the engine (two app settings), else None."""
    url, key = os.environ.get("KECORE_FUNCTION_URL", ""), os.environ.get("KECORE_FUNCTION_KEY", "")
    if not url or not key or key.startswith("@Microsoft.KeyVault"):  # an unresolved reference is not a key
        return None
    return EngineClient(url, key)


__all__ = ["EngineClient", "EngineError", "from_env"]
