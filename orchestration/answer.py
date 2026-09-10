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
# Keys are retrieved at runtime via `az` (never stored), same convention
# as eval/evaluate_rag.py. Requires az login.
# Usage: python answer.py --client clienta --query "..."
# =====================================================================
import argparse
import json
import subprocess
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import requests
import yaml
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
    "CONTEXTE fourni, qui distingue une SOURCE PRIMAIRE et des SOURCES ANNEXES "
    "(deja selectionnees et classees par pertinence -- axiome A3, tu n'as pas a "
    "re-choisir). Si l'information ne s'y trouve pas dans le CONTEXTE, ou si la "
    "question est ambigue, dis-le explicitement (ambiguous=true, avec la raison) "
    "plutot que d'inventer une reponse."
)

# Strict JSON Schema for Structured Outputs -- Azure/OpenAI does not prescribe
# field names, only the enforcement mechanism; these names are this project's
# own contract (see docs/architecture.md).
ANSWER_SCHEMA = {
    "type": "object",
    "properties": {
        "answer": {
            "type": "string",
            "description": "Reponse a la question, basee uniquement sur le CONTEXTE fourni.",
        },
        "primary_source": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "used": {
                    "type": "boolean",
                    "description": "true si cette source a ete utilisee pour repondre",
                },
            },
            "required": ["title", "used"],
            "additionalProperties": False,
        },
        "related_sources": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "used": {"type": "boolean"},
                },
                "required": ["title", "used"],
                "additionalProperties": False,
            },
            "description": "Annexes utilisees (0 a annexCount). Tableau vide si aucune.",
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
        "primary_source",
        "related_sources",
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


def retrieve(query: str, index: str, top: int, search_key: str) -> List[Dict]:
    """Hybrid + semantic search. Returns docs sorted by @search.rerankerScore
    (desc) -- the real Azure-native ranking signal used for axiom A3."""
    body = {
        "search": query,
        "vectorQueries": [{"kind": "text", "text": query, "fields": "text_vector", "k": top}],
        "queryType": "semantic",
        "semanticConfiguration": "sem-config",
        "select": "title,chunk",
        "top": top,
    }
    r = requests.post(
        f"{SEARCH_ENDPOINT}/indexes/{index}/docs/search?api-version={SEARCH_API_VERSION}",
        headers={"api-key": search_key, "Content-Type": "application/json"},
        json=body,
        timeout=60,
    )
    r.raise_for_status()
    docs = r.json().get("value", [])
    return sorted(docs, key=lambda d: d.get("@search.rerankerScore", 0), reverse=True)


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
    for a in annexes:
        score = a.get("@search.rerankerScore", 0)
        parts.append(
            f"[SOURCE ANNEXE — {a.get('title', '')} — score={score:.2f}]\n"
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


def answer_query(client_id: str, query: str) -> dict:
    cfg = load_engine_config(client_id)
    index = cfg["knowledge"]["index"]
    primary_count = cfg["retrieval"]["primaryCount"]
    annex_count = cfg["retrieval"]["annexCount"]
    gen = cfg["generation"]

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

    docs = retrieve(query, index, top=primary_count + annex_count, search_key=search_key)
    primary, annexes = split_hierarchy(docs, primary_count, annex_count)
    context = format_context(primary, annexes)

    result = generate(query, context, aoai_client, gen["model"], gen["temperature"], gen["seed"])

    # Traceability (axiom A5): attach which source was primary/annex and its
    # real reranker score. Deliberately NOT asking the model to self-report
    # Groundedness/Relevance/Retrieval -- a model grading its own answer is a
    # hallucination risk. Those scores stay the job of the batch Azure AI
    # Foundry Evaluators pipeline (eval/), run against a golden dataset.
    result["_trace"] = {
        "client": client_id,
        "index": index,
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
