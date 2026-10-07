"""Keyless calls to Azure data planes (AI Search, Azure OpenAI).

Tokens come from azure-identity when it is installed, otherwise from the Azure
CLI ('az login'). They are renewed before they expire and once on HTTP 401.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.request

SEARCH_SCOPE = "https://search.azure.com/.default"
COGNITIVE_SCOPE = "https://cognitiveservices.azure.com/.default"


class AzureError(RuntimeError):
    pass


class TokenProvider:
    """Bearer token for one Azure scope (or an API key read from the environment)."""

    def __init__(self, auth: str = "entra", scope: str = SEARCH_SCOPE, key_env: str = "AZURE_SEARCH_KEY",
                 key_header: str = "api-key", credential=None, runner=None, clock=time.time):
        if auth not in ("entra", "key"):
            raise ValueError("auth must be 'entra' or 'key'")
        self.auth = auth
        self.scope = scope
        self.key_env = key_env
        self.key_header = key_header
        self._credential = credential
        self._runner = runner or subprocess.run
        self._clock = clock
        self._token: str | None = None
        self._expires = 0.0

    def headers(self, force_refresh: bool = False) -> dict:
        if self.auth == "key":
            key = os.environ.get(self.key_env)
            if not key:
                raise AzureError(f"auth is 'key' but the {self.key_env} environment variable is not set")
            return {self.key_header: key}
        if force_refresh or self._token is None or self._clock() > self._expires - 120:
            self._token, self._expires = self._fetch()
        return {"Authorization": f"Bearer {self._token}"}

    def _fetch(self) -> tuple[str, float]:
        credential = self._credential
        if credential is None:
            try:
                from azure.identity import DefaultAzureCredential
            except ImportError:
                return self._fetch_from_cli()
            credential = self._credential = DefaultAzureCredential()
        token = credential.get_token(self.scope)
        return token.token, float(token.expires_on)

    def _fetch_from_cli(self) -> tuple[str, float]:
        az = shutil.which("az")
        if not az:
            raise AzureError(
                "no Azure credential available: run 'pip install azure-identity' "
                "or install the Azure CLI, then 'az login'"
            )
        resource = self.scope[: -len("/.default")] if self.scope.endswith("/.default") else self.scope
        proc = self._runner(
            [az, "account", "get-access-token", "--resource", resource, "-o", "json"],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip()[:300]
            raise AzureError(f"'az account get-access-token' failed (run 'az login' first): {detail}")
        data = json.loads(proc.stdout)
        expires = data.get("expires_on")
        # Older CLIs only give a local-time string: renew after 5 minutes instead of parsing it.
        expires_at = float(expires) if expires else self._clock() + 300
        return data["accessToken"], expires_at


def urllib_transport(method: str, url: str, headers: dict, body, timeout: float):
    """Returns (status, payload, response headers)."""
    data = None if body is None else json.dumps(body).encode("utf-8")
    request = urllib.request.Request(url, data=data, method=method)
    for key, value in headers.items():
        request.add_header(key, value)
    if data is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
            return response.status, (json.loads(raw) if raw else {}), dict(response.headers)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            payload = json.loads(raw) if raw else {}
        except ValueError:
            payload = {"raw": raw[:500]}
        return exc.code, payload, dict(exc.headers or {})
    except urllib.error.URLError as exc:
        raise AzureError(f"cannot reach {url.split('?')[0]}: {exc.reason}") from None


def error_message(payload) -> str:
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict) and error.get("message"):
            return str(error["message"])[:500]
        if payload.get("raw"):
            return str(payload["raw"])[:500]
    return json.dumps(payload)[:500]


class RestClient:
    """JSON over HTTPS with retries on throttling and one token renewal on 401."""

    def __init__(self, endpoint: str, api_version: str, tokens: TokenProvider, transport=urllib_transport,
                 timeout: float = 30.0, retries: int = 3, sleep=time.sleep, forbidden_hint=None,
                 error_class=AzureError):
        self.endpoint = endpoint.rstrip("/")
        self.api_version = api_version
        self.tokens = tokens
        self.transport = transport
        self.timeout = timeout
        self.retries = retries
        self.sleep = sleep
        self.forbidden_hint = forbidden_hint
        self.error_class = error_class

    def request(self, method: str, path: str, body=None) -> dict:
        url = f"{self.endpoint}{path}{'&' if '?' in path else '?'}api-version={self.api_version}"
        refreshed = False
        attempt = 0
        while True:
            try:
                response = self.transport(method, url, self.tokens.headers(), body, self.timeout)
            except AzureError as exc:
                # a transport-level failure (DNS, connect, read timeout) never reaches the status
                # checks below, so it gets its own retry here -- same backoff as a 429/5xx, then
                # surfaces through the caller's own error_class (e.g. LLMError), never the generic
                # AzureError, so callers that only catch their own error type (kefind.interpret)
                # see it and degrade instead of crashing.
                if attempt < self.retries:
                    self.sleep(_retry_delay({}, attempt))
                    attempt += 1
                    continue
                raise self.error_class(str(exc)) from None
            status, payload = response[0], response[1]
            headers = response[2] if len(response) > 2 else {}
            if status == 401 and self.tokens.auth == "entra" and not refreshed:
                self.tokens.headers(force_refresh=True)
                refreshed = True
                continue
            if status in (429, 500, 502, 503, 504) and attempt < self.retries:
                self.sleep(_retry_delay(headers, attempt))
                attempt += 1
                continue
            if status >= 400:
                hint = ""
                if status == 403 and self.forbidden_hint:
                    hint = f" ({self.forbidden_hint(path)})"
                raise self.error_class(f"{method} {path} -> HTTP {status}: {error_message(payload)}{hint}")
            return payload if isinstance(payload, dict) else {}


def _retry_delay(headers: dict, attempt: int) -> float:
    for key in ("retry-after-ms", "Retry-After-Ms", "x-ms-retry-after-ms"):
        if headers.get(key):
            try:
                return min(float(headers[key]) / 1000, 60.0)
            except ValueError:
                pass
    for key in ("retry-after", "Retry-After"):
        if headers.get(key):
            try:
                return min(float(headers[key]), 60.0)
            except ValueError:
                pass
    return float(min(2 ** attempt, 30))
