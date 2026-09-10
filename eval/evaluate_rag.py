# =====================================================================
# RAG Evaluation — KnowledgeEngine v9 (quantified proof of reliability)
# 1) For each client (A/B/C, dedicated index): hybrid retrieval + GPT-4o answer (temp=0)
# 2) Azure AI Foundry evaluators: Groundedness, Relevance, Retrieval
# Keys retrieved at runtime via az (never stored). Requires az login.
# Usage: python evaluate_rag.py
# =====================================================================
import json
import subprocess

import requests
from openai import AzureOpenAI
from azure.ai.evaluation import (
    GroundednessEvaluator,
    RelevanceEvaluator,
    RetrievalEvaluator,
    evaluate,
)

# ---- Infra constants ----
RG = "rg-knowledgeengine-v9"
SEARCH_SERVICE = "srch-knowledgeengine2-v9"
SEARCH_ENDPOINT = f"https://{SEARCH_SERVICE}.search.windows.net"
CLIENTS = ["clienta", "clientb", "clientc"]   # one DEDICATED index per client (isolation M2)
SEARCH_API_VERSION = "2024-07-01"

AOAI_ACCOUNT = "aif-knowledgeengine2-v9"
AOAI_ENDPOINT = f"https://{AOAI_ACCOUNT}.openai.azure.com"
CHAT_DEPLOY = "gpt-4o"
AOAI_API_VERSION = "2024-08-01-preview"

TOP_K = 5


def az(cmd: str) -> str:
    """Run an az command and return stdout (text)."""
    out = subprocess.run(cmd, capture_output=True, text=True, shell=True)
    if out.returncode != 0:
        raise RuntimeError(f"az command failed: {cmd}\n{out.stderr}")
    return out.stdout.strip()


print("Retrieving keys (runtime, not stored)...")
SEARCH_KEY = az(
    f'az search admin-key show --service-name {SEARCH_SERVICE} '
    f'--resource-group {RG} --query primaryKey -o tsv'
)
AOAI_KEY = az(
    f'az cognitiveservices account keys list --name {AOAI_ACCOUNT} '
    f'--resource-group {RG} --query key1 -o tsv'
)

client = AzureOpenAI(
    azure_endpoint=AOAI_ENDPOINT, api_key=AOAI_KEY, api_version=AOAI_API_VERSION
)

SYSTEM_PROMPT = (
    "Tu es l'assistant de support IT du client. Reponds UNIQUEMENT a partir du CONTEXTE fourni. "
    "Si l'information ne s'y trouve pas, dis clairement que tu ne sais pas. Sois precis et concis."
)


def retrieve(query: str, index: str, k: int = TOP_K) -> str:
    """Hybrid search (BM25 + vectors + semantic reranker) -> concatenated context."""
    body = {
        "search": query,
        "vectorQueries": [
            {"kind": "text", "text": query, "fields": "text_vector", "k": k}
        ],
        "queryType": "semantic",
        "semanticConfiguration": "sem-config",
        "select": "title,chunk",
        "top": k,
    }
    r = requests.post(
        f"{SEARCH_ENDPOINT}/indexes/{index}/docs/search?api-version={SEARCH_API_VERSION}",
        headers={"api-key": SEARCH_KEY, "Content-Type": "application/json"},
        json=body,
        timeout=60,
    )
    r.raise_for_status()
    docs = r.json().get("value", [])
    return "\n\n".join(f"[{d.get('title','')}]\n{d.get('chunk','')}" for d in docs)


def answer(query: str, context: str) -> str:
    """Generate a deterministic GPT-4o answer (temperature=0)."""
    resp = client.chat.completions.create(
        model=CHAT_DEPLOY,
        temperature=0,
        seed=42,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"CONTEXTE:\n{context}\n\nQUESTION: {query}"},
        ],
    )
    return resp.choices[0].message.content


model_config = {
    "azure_endpoint": AOAI_ENDPOINT,
    "api_key": AOAI_KEY,
    "azure_deployment": CHAT_DEPLOY,
    "api_version": AOAI_API_VERSION,
}

all_metrics = {}

for CLIENT_ID in CLIENTS:
    INDEX = f"idx-{CLIENT_ID}"
    print(f"\n=== Client {CLIENT_ID} (index {INDEX}) ===")

    # ---- Phase 1: generate RAG answers ----
    print("Generating RAG answers (retrieval + GPT-4o)...")
    rows = []
    with open(f"golden_{CLIENT_ID}.jsonl", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            q = json.loads(line)["query"]
            ctx = retrieve(q, INDEX)
            ans = answer(q, ctx)
            rows.append({"query": q, "context": ctx, "response": ans})
            print(f"  - {q[:60]}...")

    input_path = f"eval_input_{CLIENT_ID}.jsonl"
    with open(input_path, "w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    # ---- Phase 2: Azure AI Foundry evaluation ----
    print("Evaluating (Groundedness, Relevance, Retrieval)...")
    result = evaluate(
        data=input_path,
        evaluators={
            "groundedness": GroundednessEvaluator(model_config),
            "relevance": RelevanceEvaluator(model_config),
            "retrieval": RetrievalEvaluator(model_config),
        },
        evaluator_config={
            "groundedness": {
                "column_mapping": {
                    "query": "${data.query}",
                    "response": "${data.response}",
                    "context": "${data.context}",
                }
            },
            "relevance": {
                "column_mapping": {
                    "query": "${data.query}",
                    "response": "${data.response}",
                }
            },
            "retrieval": {
                "column_mapping": {
                    "query": "${data.query}",
                    "context": "${data.context}",
                }
            },
        },
        output_path=f"eval_results_{CLIENT_ID}.json",
    )

    all_metrics[CLIENT_ID] = result["metrics"]

with open("eval_summary.json", "w", encoding="utf-8") as f:
    json.dump(all_metrics, f, ensure_ascii=False, indent=2)

print("\n===== CONSOLIDATED RELIABILITY SCORE (KnowledgeEngineV9, averages /5) =====")
for CLIENT_ID, metrics in all_metrics.items():
    print(f"\n{CLIENT_ID}:")
    for k, v in metrics.items():
        print(f"  {k}: {round(v, 3)}")

print("\nFull detail: eval_results_<client>.json, summary: eval_summary.json")
