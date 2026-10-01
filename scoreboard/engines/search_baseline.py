"""Search baseline: Azure AI Search, first fiche shown.

The client's index queried with the ticket text (hybrid when the index has a
vectorizer, semantic reranking when it has a semantic configuration), and the
first fiche in the results shown as THE fiche. It proposes the candidates of
the labeling sheet and gives a first reference point. It never abstains unless
``min_score`` is set; the report's threshold calibration shows what abstaining
below a score would have done.

Authentication is keyless by default (Microsoft Entra ID): your account needs
the "Search Index Data Reader" role on the search service. The token comes from
azure-identity when it is installed, otherwise from the Azure CLI ('az login').
Use "auth": "key" with the AZURE_SEARCH_KEY environment variable only where API
keys are still enabled.
"""

from __future__ import annotations

import re
import time
import urllib.parse
from dataclasses import dataclass
from dataclasses import fields as dataclass_fields

from kecore.azure import AzureError, RestClient, TokenProvider, urllib_transport

from ..ids import DEFAULT_FICHE_REGEX, compile_pattern, extract_fiche_id
from . import Decision, Usage

DEFAULT_API_VERSION = "2024-07-01"
# Operators of the simple query syntax; a ticket is text, not a query.
SIMPLE_QUERY_OPERATORS = re.compile(r'[+\-|"*()~\\]')

# Search and token errors are one type for the command line.
SearchError = AzureError


def sanitize_query(text: str, max_chars: int = 1000) -> str:
    query = " ".join(SIMPLE_QUERY_OPERATORS.sub(" ", text).split())
    if len(query) > max_chars:
        cut = query[:max_chars]
        query = cut.rsplit(" ", 1)[0] if " " in cut else cut
    return query or "*"


def search_role_hint(path: str) -> str:
    role = "Search Index Data Reader" if "/docs" in path else "Search Service Contributor (or Owner)"
    return f"your account needs the '{role}' role on the search service"


class SearchClient(RestClient):
    def __init__(self, endpoint: str, api_version: str, tokens: TokenProvider, transport=urllib_transport,
                 timeout: float = 30.0, retries: int = 3, sleep=time.sleep):
        super().__init__(endpoint, api_version, tokens, transport=transport, timeout=timeout, retries=retries,
                         sleep=sleep, forbidden_hint=search_role_hint)


@dataclass
class SearchBaselineConfig:
    endpoint: str
    name: str = "search-baseline"
    index: str = "idx-{client}"
    api_version: str = DEFAULT_API_VERSION
    query_type: str = "semantic"
    semantic_configuration: str | None = None
    vector_field: str | None = None
    fiche_id_field: str = "title"
    fiche_id_regex: str | None = DEFAULT_FICHE_REGEX
    strip_extension: bool = True
    title_field: str | None = "title"
    filter: str | None = None
    top: int = 20
    max_query_chars: int = 1000
    min_score: float | None = None
    auth: str = "entra"
    timeout_s: float = 30.0

    @classmethod
    def from_dict(cls, data: dict) -> "SearchBaselineConfig":
        allowed = {f.name for f in dataclass_fields(cls)}
        unknown = sorted(set(data) - allowed - {"comment", "_comment"})
        if unknown:
            raise ValueError(f"unknown setting(s): {', '.join(unknown)}. Allowed: {', '.join(sorted(allowed))}")
        endpoint = str(data.get("endpoint") or "")
        if not endpoint.startswith("https://") or "<" in endpoint:
            raise ValueError("'endpoint' must be your search service URL, e.g. https://<service>.search.windows.net")
        config = cls(**{key: value for key, value in data.items() if key in allowed})
        if config.query_type not in ("semantic", "simple"):
            raise ValueError("'query_type' must be 'semantic' or 'simple'")
        if config.auth not in ("entra", "key"):
            raise ValueError("'auth' must be 'entra' or 'key'")
        if not config.fiche_id_field or "<" in config.fiche_id_field:
            raise ValueError("'fiche_id_field' must name the index field that identifies the fiche")
        if int(config.top) < 1:
            raise ValueError("'top' must be at least 1")
        return config


def hit_score(hit: dict) -> float | None:
    for key in ("@search.rerankerScore", "@search.score"):
        if hit.get(key) is not None:
            return float(hit[key])
    return None


class SearchBaselineEngine:
    def __init__(self, config: SearchBaselineConfig, client: SearchClient | None = None):
        self.config = config
        self.name = config.name
        self._pattern = compile_pattern(config.fiche_id_regex)
        self._client = client or SearchClient(
            config.endpoint, config.api_version, TokenProvider(config.auth), timeout=config.timeout_s
        )

    @classmethod
    def from_config(cls, data: dict) -> "SearchBaselineEngine":
        return cls(SearchBaselineConfig.from_dict(data))

    def build_request(self, ticket) -> tuple[str, dict]:
        cfg = self.config
        query = sanitize_query(ticket.text, cfg.max_query_chars)
        body: dict = {"search": query, "top": int(cfg.top)}
        select = [name for name in dict.fromkeys([cfg.fiche_id_field, cfg.title_field]) if name]
        body["select"] = ",".join(select)
        if cfg.query_type == "semantic":
            body["queryType"] = "semantic"
            if cfg.semantic_configuration:
                body["semanticConfiguration"] = cfg.semantic_configuration
        if cfg.vector_field:
            body["vectorQueries"] = [{"kind": "text", "text": query, "fields": cfg.vector_field, "k": int(cfg.top)}]
        if cfg.filter:
            body["filter"] = cfg.filter.replace("{client}", ticket.client.replace("'", "''"))
        index = cfg.index.replace("{client}", ticket.client)
        return f"/indexes/{urllib.parse.quote(index, safe='')}/docs/search", body

    def search(self, ticket) -> list[dict]:
        path, body = self.build_request(ticket)
        payload = self._client.request("POST", path, body)
        hits = payload.get("value", [])
        return hits if isinstance(hits, list) else []

    def rank(self, hits: list[dict]) -> tuple[list[str], dict[str, str], float | None]:
        """Collapse chunks into fiches, keeping the order of their best chunk."""
        fiches: list[str] = []
        titles: dict[str, str] = {}
        score = None
        for hit in hits:
            fiche = extract_fiche_id(hit.get(self.config.fiche_id_field), self._pattern, self.config.strip_extension)
            if fiche is None:
                continue
            if score is None:
                score = hit_score(hit)
            if fiche not in titles:
                fiches.append(fiche)
                title = hit.get(self.config.title_field) if self.config.title_field else None
                titles[fiche] = str(title).strip() if title else ""
        return fiches, titles, score

    def decide(self, ticket) -> Decision:
        started = time.perf_counter()
        hits = self.search(ticket)
        fiches, titles, score = self.rank(hits)
        elapsed = time.perf_counter() - started
        usage = Usage(search_calls=1)
        trace = [{"step": "search", "hits": len(hits), "fiches": len(fiches), "top_score": score}]
        if not fiches:
            return Decision("abstain", usage=usage, latency_s=elapsed, trace=trace)
        kind = "fiche"
        if self.config.min_score is not None and (score is None or score < self.config.min_score):
            kind = "abstain"
        return Decision(kind, fiches=fiches, score=score, titles=titles, usage=usage, latency_s=elapsed, trace=trace)

    def candidates(self, ticket, k: int) -> list[tuple[str, str]]:
        fiches, titles, _ = self.rank(self.search(ticket))
        return [(fiche, titles.get(fiche, "")) for fiche in fiches[:k]]


# --- index inspection: write the engine config without guessing field names ---

ID_FIELD_CANDIDATES = (
    "kb_number", "kbnumber", "number", "metadata_storage_name", "file_name", "filename", "source", "title", "parent_id",
)
TITLE_FIELD_CANDIDATES = ("title", "metadata_title", "document_title", "doc_title", "name", "metadata_storage_name")
CLIENT_FIELD_CANDIDATES = ("clientid", "client_id", "client", "tenantid", "tenant")


def fetch_index(endpoint: str, index: str, auth: str = "entra", api_version: str = DEFAULT_API_VERSION,
                client: SearchClient | None = None) -> dict:
    client = client or SearchClient(endpoint, api_version, TokenProvider(auth))
    return client.request("GET", f"/indexes/{urllib.parse.quote(index, safe='')}")


def describe_fields(index_def: dict) -> list[str]:
    lines = []
    for f in index_def.get("fields", []):
        flags = "".join(
            letter
            for letter, on in (
                ("S", f.get("searchable")),
                ("R", f.get("retrievable", True) is not False),
                ("F", f.get("filterable")),
                ("V", f.get("vectorSearchProfile") or f.get("dimensions")),
            )
            if on
        )
        lines.append(f"{f.get('name', '?'):<32} {f.get('type', '?'):<28} {flags}")
    return lines


def suggest_config(endpoint: str, index_def: dict, client: str | None = None) -> tuple[dict, list[str]]:
    all_fields = index_def.get("fields", [])

    def pick(names, predicate):
        by_lower: dict[str, str] = {}
        for f in all_fields:
            if predicate(f):
                by_lower.setdefault(str(f.get("name", "")).lower(), f["name"])
        return next((by_lower[n] for n in names if n in by_lower), None)

    def retrievable_text(f):
        return f.get("type") in ("Edm.String", "Collection(Edm.String)") and f.get("retrievable", True) is not False

    id_field = pick(ID_FIELD_CANDIDATES, retrievable_text)
    title_field = pick(TITLE_FIELD_CANDIDATES, retrievable_text)
    client_field = pick(CLIENT_FIELD_CANDIDATES, lambda f: f.get("filterable") and f.get("type") == "Edm.String")

    profiles = {p.get("name"): p for p in (index_def.get("vectorSearch") or {}).get("profiles", [])}
    vector_field = None
    for f in all_fields:
        profile = profiles.get(f.get("vectorSearchProfile") or f.get("vectorSearchProfileName"))
        if profile and (profile.get("vectorizer") or profile.get("vectorizerName")):
            vector_field = f["name"]
            break

    semantic = index_def.get("semantic") or {}
    names = [c.get("name") for c in semantic.get("configurations", []) if c.get("name")]
    semantic_name = semantic.get("defaultConfiguration") or (names[0] if names else None)

    index_name = str(index_def.get("name", ""))
    pattern = index_name.replace(client, "{client}") if client and client in index_name else index_name
    config = {
        "name": "search-baseline",
        "endpoint": endpoint.rstrip("/"),
        "index": pattern,
        "query_type": "semantic" if semantic_name else "simple",
        "semantic_configuration": semantic_name,
        "vector_field": vector_field,
        "fiche_id_field": id_field or title_field or "<field that names the fiche>",
        "fiche_id_regex": DEFAULT_FICHE_REGEX,
        "title_field": title_field,
        "top": 20,
        "auth": "entra",
    }
    notes = []
    if not semantic_name:
        notes.append("no semantic configuration: results are ranked by keyword (and vector) scores only")
    if not vector_field:
        notes.append("no vector field with a vectorizer: the query is keyword (+ semantic), not hybrid")
    if not id_field:
        notes.append("set 'fiche_id_field' by hand: a retrievable field that names the fiche")
    if client_field:
        notes.append(
            f"'{client_field}' is filterable: add \"filter\": \"{client_field} eq '{{client}}'\" "
            "if its values are the client names used in your tickets"
        )
    if pattern == index_name and client:
        notes.append(f"client {client!r} is not in the index name: check 'index'")
    return config, notes
