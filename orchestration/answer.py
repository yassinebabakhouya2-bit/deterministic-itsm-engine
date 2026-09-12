# =====================================================================
# KnowledgeEngine v9 — Orchestration layer (Jalon 3)
#
# Retrieval (Azure AI Search, hybrid + semantic reranker)
#   -> hierarchy split (axiom A3, rank-based on @search.rerankerScore)
#   -> generation (Azure OpenAI Chat Completions, Structured Outputs,
#      temperature=0 + seed -> axiom A1)
#   -> typed, schema-enforced answer.
#
# Design note (2026-09-10): Foundry Agent Service (PromptAgentDefinition +
# AzureAISearchTool) was the first option considered -- fully managed,
# deployable via SDK/Bicep. Dropped because it does not support Structured
# Outputs (response_format: json_schema) -- only direct Chat Completions /
# Responses API calls on an Azure OpenAI deployment do. A reliable,
# schema-enforced answer (no hallucinated shape, explicit `ambiguous` flag
# instead of guessing) was the explicit priority for this milestone, so
# this module calls the aif-knowledgeengine2-v9 deployment directly.
# Prompt Flow and Foundry's visual Workflows were also ruled out: both are
# being retired (2027-04-20 and 2026-12-01 respectively).
# See project memory jalon3-orchestration.md for the full discussion.
#
# Design note (2026-09-10, after first live test): the model is NOT asked
# to reproduce source titles -- source titles/order are already known
# deterministically from retrieval, so having the LLM retype them is an
# avoidable divergence point (the first live run showed the model writing
# "KB-A-001" instead of the indexed "KB-A-001.md"). The model only returns
# booleans (which sources it actually used); this script fills in the
# real titles from the retrieval result afterwards.
#
# Design note (2026-09-11, Jalon 4): added a keyless path
# (answer_query_core_keyless + build_aoai_client_keyless) for callers that
# use Azure AD RBAC instead of admin keys -- namely app/app.py, which runs
# on an Azure Web App and has no `az login` session to fetch keys with.
# answer_query_core(), retrieve() and answer_query() (the CLI, validated in
# Jalon 3) are UNCHANGED in behavior: retrieve() now delegates to the new
# _search_request() helper, but with the exact same {"api-key": ...} header
# it always used. Nothing here required or received a live retest, by
# design -- see project memory jalon4-app-interface.md.
#
# Keys are retrieved at runtime via `az` (never stored) for the CLI path,
# same convention as eval/evaluate_rag.py. Requires az login.
# Usage: python answer.py --client clienta --query "..."
# =====================================================================
import argparse
import json
import subprocess
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import requests
import yaml
from azure.identity import DefaultAzureCredential, get_bearer_token_provider
from openai import AzureOpenAI

REPO_ROOT = Path(__file__).resolve().parent.parent
# config/ (tracked, synthetic demo clients) is checked before clients-local/
# (git-ignored, real clients) -- a real client config can never shadow a
# tracked one by accident.
CONFIG_DIRS = [REPO_ROOT / "config", REPO_ROOT / "clients-local"]

RG = "rg-knowledgeengine-v9"
SEARCH_SERVICE = "srch-knowledgeengine2-v9"
SEARCH_ENDPOINT = f"https://{SEARCH_SERVICE}.search.windows.net"
SEARCH_API_VERSION = "2024-07-01"

AOAI_ACCOUNT = "aif-knowledgeengine2-v9"
AOAI_ENDPOINT = f"https://{AOAI_ACCOUNT}.openai.azure.com"
# First API version supporting Structured Outputs (response_format:
# json_schema, strict) -- same version eval/evaluate_rag.py already uses.
AOAI_API_VERSION = "2024-08-01-preview"

SYSTEM_PROMPT = (
    "Tu es l'assistant de support IT du client. Reponds UNIQUEMENT a partir du "
    "CONTEXTE fourni, qui distingue une SOURCE PRIMAIRE et, dans l'ordre, des "
    "SOURCES ANNEXES numerotees (deja selectionnees et classees par pertinence -- "
    "axiome A3, tu n'as pas a re-choisir). Pour related_sources_used, renvoie un "
    "booleen par source annexe, dans le MEME ORDRE que le CONTEXTE (ne retape "
    "jamais les titres, ils sont deja connus). Si l'information ne se trouve pas "
    "dans le CONTEXTE, ou si la question est ambigue, dis-le explicitement "
    "(ambiguous=true, avec la raison) plutot que d'inventer une reponse."
)

# Strict JSON Schema for Structured Outputs -- Azure/OpenAI does not prescribe
# field names, only the enforcement mechanism. Deliberately does NOT include
# source titles: those are already known deterministically from retrieval: the
# model only reports which ones it used (booleans), titles are filled in by
# this script afterwards (see module docstring, 2026-09-10 note).
ANSWER_SCHEMA = {
    "type": "object",
    "properties": {
        "answer": {
            "type": "string",
            "description": "Reponse a la question, basee uniquement sur le CONTEXTE fourni.",
        },
        "primary_source_used": {
            "type": "boolean",
            "description": "true si la SOURCE PRIMAIRE a ete utilisee pour repondre.",
        },
        "related_sources_used": {
            "type": "array",
            "items": {"type": "boolean"},
            "description": (
                "Un booleen par SOURCE ANNEXE, dans le meme ordre que le CONTEXTE. "
                "Tableau vide si aucune annexe fournie ou utilisee."
            ),
        },
        "ambiguous": {
            "type": "boolean",
            "description": "true si la question ne peut pas etre repondue de facon fiable a partir du CONTEXTE.",
        },
        "unanswerable_reason": {
            "anyOf": [{"type": "string"}, {"type": "null"}],
            "description": "Si ambiguous=true, pourquoi. Sinon null.",
        },
    },
    "required": [
        "answer",
        "primary_source_used",
        "related_sources_used",
        "ambiguous",
        "unanswerable_reason",
    ],
    "additionalProperties": False,
}


def az(cmd: str) -> str:
    """Run an az command and return stdout (text)."""
    out = subprocess.run(cmd, capture_output=True, text=True, shell=True)
    if out.returncode != 0:
        raise RuntimeError(f"az command failed: {cmd}\n{out.stderr}")
    return out.stdout.strip()


def load_engine_config(client_id: str) -> dict:
    """Axiom A2: all client specifics come from engine.<client>.yaml -- this
    module never hardcodes a client."""
    for d in CONFIG_DIRS:
        p = d / f"engine.{client_id}.yaml"
        if p.exists():
            with open(p, encoding="utf-8") as f:
                return yaml.safe_load(f)
    searched = ", ".join(str(d) for d in CONFIG_DIRS)
    raise FileNotFoundError(f"No engine.{client_id}.yaml found in: {searched}")


def _search_request(
    query: str, index: str, top: int, headers: Dict[str, str], client_id: Optional[str] = None
) -> List[Dict]:
    """Raw Azure AI Search hybrid+semantic query -- same request body
    regardless of auth mechanism, only the headers differ (api-key vs a
    Bearer token). Returns docs sorted by @search.rerankerScore (desc), the
    real Azure-native ranking signal used for axiom A3. Shared by retrieve()
    (api-key, Jalon 3) and answer_query_core_keyless() (RBAC, Jalon 4).

    client_id (Jalon 5): when given, adds an explicit `filter: clientId eq
    '<client_id>'` -- defense in depth on top of the physical per-client
    index isolation (idx-<client>) already in place since Jalon 2. The
    clientId field has been projected onto every document since the
    skillset was built (search/skillset.template.json) but was never
    actually queried against until now. Single quotes are doubled (OData
    escaping) even though callers only ever pass our own known client ids,
    never raw user input."""
    body = {
        "search": query,
        "vectorQueries": [{"kind": "text", "text": query, "fields": "text_vector", "k": top}],
        "queryType": "semantic",
        "semanticConfiguration": "sem-config",
        "select": "title,chunk",
        "top": top,
    }
    if client_id:
        body["filter"] = f"clientId eq '{client_id.replace(chr(39), chr(39) * 2)}'"
    r = requests.post(
        f"{SEARCH_ENDPOINT}/indexes/{index}/docs/search?api-version={SEARCH_API_VERSION}",
        headers={**headers, "Content-Type": "application/json"},
        json=body,
        timeout=60,
    )
    r.raise_for_status()
    docs = r.json().get("value", [])
    return sorted(docs, key=lambda d: d.get("@search.rerankerScore", 0), reverse=True)


def retrieve(
    query: str, index: str, top: int, search_key: str, client_id: Optional[str] = None
) -> List[Dict]:
    """Hybrid + semantic search, api-key auth. Unchanged contract from Jalon 3
    -- used by answer_query_core() (CLI, eval/evaluate_rag.py). See
    _search_request() for the shared request logic (incl. the optional
    clientId filter added in Jalon 5) and answer_query_core_keyless() for
    the RBAC-based alternative (Jalon 4)."""
    return _search_request(query, index, top, {"api-key": search_key}, client_id=client_id)


def split_hierarchy(
    docs: List[Dict], primary_count: int, annex_count: int
) -> Tuple[Optional[Dict], List[Dict]]:
    """Axiom A3: rank-based split, not a score threshold -- Microsoft explicitly
    advises against fine-grained thresholds on @search.rerankerScore. Top-ranked
    result = primary, next annex_count = annexes."""
    if not docs:
        return None, []
    primary = docs[0] if primary_count >= 1 else None
    annexes = docs[primary_count : primary_count + annex_count]
    return primary, annexes


def format_context(primary: Optional[Dict], annexes: List[Dict]) -> str:
    parts = []
    if primary:
        score = primary.get("@search.rerankerScore", 0)
        parts.append(
            f"[SOURCE PRIMAIRE — {primary.get('title', '')} — score={score:.2f}]\n"
            f"{primary.get('chunk', '')}"
        )
    for i, a in enumerate(annexes, start=1):
        score = a.get("@search.rerankerScore", 0)
        parts.append(
            f"[SOURCE ANNEXE {i} — {a.get('title', '')} — score={score:.2f}]\n"
            f"{a.get('chunk', '')}"
        )
    return "\n\n".join(parts) if parts else "(aucune source pertinente trouvee)"


def generate(
    query: str,
    context: str,
    client: AzureOpenAI,
    model: str,
    temperature: float,
    seed: int,
) -> dict:
    resp = client.chat.completions.create(
        model=model,
        temperature=temperature,
        seed=seed,
        response_format={
            "type": "json_schema",
            "json_schema": {
                "name": "orchestrated_answer",
                "strict": True,
                "schema": ANSWER_SCHEMA,
            },
        },
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"CONTEXTE:\n{context}\n\nQUESTION: {query}"},
        ],
    )
    return json.loads(resp.choices[0].message.content)


def attach_sources(
    result: dict, primary: Optional[Dict], annexes: List[Dict]
) -> dict:
    """Replaces the model's boolean-only primary_source_used/related_sources_used
    with the public primary_source/related_sources contract, using the REAL
    titles from retrieval -- never a title the model retyped (see module
    docstring, 2026-09-10 note)."""
    primary_used = result.pop("primary_source_used", False)
    used_flags = result.pop("related_sources_used", [])

    result["primary_source"] = (
        {"title": primary.get("title"), "used": primary_used} if primary else None
    )

    related_sources = []
    for i, a in enumerate(annexes):
        used = used_flags[i] if i < len(used_flags) else False
        related_sources.append({"title": a.get("title"), "used": used})
    result["related_sources"] = related_sources
    return result


def answer_query_core(
    client_id: str,
    query: str,
    search_key: str,
    aoai_client: AzureOpenAI,
    cfg: Optional[dict] = None,
) -> dict:
    """Reusable core: no az calls, no client construction -- callers that need
    many answers (e.g. eval/evaluate_rag.py, batch-scoring a golden dataset)
    should fetch credentials ONCE and call this directly instead of going
    through answer_query(), which re-fetches keys on every call (fine for a
    single CLI invocation, wasteful in a loop). See orchestration/README.md,
    axiom A4 (separation) -- eval/ reuses this rather than duplicating it."""
    cfg = cfg or load_engine_config(client_id)
    index = cfg["knowledge"]["index"]
    primary_count = cfg["retrieval"]["primaryCount"]
    annex_count = cfg["retrieval"]["annexCount"]
    gen = cfg["generation"]

    docs = retrieve(
        query, index, top=primary_count + annex_count, search_key=search_key, client_id=client_id
    )
    primary, annexes = split_hierarchy(docs, primary_count, annex_count)
    context = format_context(primary, annexes)

    result = generate(query, context, aoai_client, gen["model"], gen["temperature"], gen["seed"])
    result = attach_sources(result, primary, annexes)

    # Traceability (axiom A5): attach which source was primary/annex, its real
    # reranker score, and the raw context string (golden-dataset evaluation
    # needs it verbatim -- see eval/evaluate_rag.py). Deliberately NOT asking
    # the model to self-report Groundedness/Relevance/Retrieval -- a model
    # grading its own answer is a hallucination risk. Those scores stay the
    # job of the batch Azure AI Foundry Evaluators pipeline (eval/), run
    # against a golden dataset, not a per-call estimate.
    result["_trace"] = {
        "client": client_id,
        "index": index,
        "context": context,
        "primary_title": primary.get("title") if primary else None,
        "primary_reranker_score": primary.get("@search.rerankerScore") if primary else None,
        "annex_titles": [a.get("title") for a in annexes],
    }
    return result


def answer_query(client_id: str, query: str) -> dict:
    """Convenience one-shot wrapper for interactive/CLI use: fetches keys via
    az and builds the AzureOpenAI client itself, then delegates to
    answer_query_core(). For batch use (many queries), call
    answer_query_core() directly with credentials fetched once."""
    cfg = load_engine_config(client_id)

    print("Retrieving keys (runtime, not stored)...")
    search_key = az(
        f"az search admin-key show --service-name {SEARCH_SERVICE} "
        f"--resource-group {RG} --query primaryKey -o tsv"
    )
    aoai_key = az(
        f"az cognitiveservices account keys list --name {AOAI_ACCOUNT} "
        f"--resource-group {RG} --query key1 -o tsv"
    )
    aoai_client = AzureOpenAI(
        azure_endpoint=AOAI_ENDPOINT, api_key=aoai_key, api_version=AOAI_API_VERSION
    )

    return answer_query_core(client_id, query, search_key, aoai_client, cfg=cfg)


# =====================================================================
# Keyless path (Jalon 4) -- Azure AD RBAC instead of admin keys.
#
# Added for app/app.py (Azure Web App), which has no `az login` session to
# fetch keys with and should not carry admin keys as app settings (axiom
# A5, same reasoning already applied to Search's own managed identity in
# infra/modules/roles.bicep). DefaultAzureCredential resolves to the App
# Service's system-assigned managed identity in Azure, and falls back to an
# interactive `az login` session locally (AzureCliCredential) -- so this
# also works for local dev, it is just not used by answer_query()/eval/
# above, which are unchanged and keep using admin keys as validated in
# Jalon 3.
#
# Requires the caller's identity to hold, on the target resources (see
# infra/modules/roles.bicep):
#   - "Search Index Data Reader" on the Search service
#   - "Cognitive Services OpenAI User" on the Foundry account (same role
#     already granted to Search's own identity, for embeddings)
# =====================================================================


def build_aoai_client_keyless(credential: DefaultAzureCredential) -> AzureOpenAI:
    token_provider = get_bearer_token_provider(
        credential, "https://cognitiveservices.azure.com/.default"
    )
    return AzureOpenAI(
        azure_endpoint=AOAI_ENDPOINT,
        azure_ad_token_provider=token_provider,
        api_version=AOAI_API_VERSION,
    )


def answer_query_core_keyless(
    client_id: str,
    query: str,
    search_bearer_token: str,
    aoai_client: AzureOpenAI,
    cfg: Optional[dict] = None,
) -> dict:
    """Same pipeline as answer_query_core(), RBAC/Bearer auth on Search
    instead of an admin api-key. Kept as a separate function rather than
    branching inside answer_query_core() so the validated Jalon 3 function
    and its callers (CLI, eval/evaluate_rag.py) stay byte-for-byte
    unchanged -- see module docstring, 2026-09-11 note."""
    cfg = cfg or load_engine_config(client_id)
    index = cfg["knowledge"]["index"]
    primary_count = cfg["retrieval"]["primaryCount"]
    annex_count = cfg["retrieval"]["annexCount"]
    gen = cfg["generation"]

    docs = _search_request(
        query,
        index,
        primary_count + annex_count,
        {"Authorization": f"Bearer {search_bearer_token}"},
        client_id=client_id,
    )
    primary, annexes = split_hierarchy(docs, primary_count, annex_count)
    context = format_context(primary, annexes)

    result = generate(query, context, aoai_client, gen["model"], gen["temperature"], gen["seed"])
    result = attach_sources(result, primary, annexes)
    result["_trace"] = {
        "client": client_id,
        "index": index,
        "context": context,
        "primary_title": primary.get("title") if primary else None,
        "primary_reranker_score": primary.get("@search.rerankerScore") if primary else None,
        "annex_titles": [a.get("title") for a in annexes],
    }
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="KnowledgeEngine v9 -- orchestration layer")
    parser.add_argument("--client", required=True, help="client id, e.g. clienta or client-v")
    parser.add_argument("--query", required=True)
    args = parser.parse_args()

    result = answer_query(args.client, args.query)
    print(json.dumps(result, ensure_ascii=False, indent=2))
