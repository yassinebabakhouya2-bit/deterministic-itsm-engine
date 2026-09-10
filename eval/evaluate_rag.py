# =====================================================================
# RAG Evaluation — KnowledgeEngine v9 (quantified proof of reliability)
# 1) For each client (A/B/C, dedicated index): orchestration/answer.py's
#    answer_query_core() -- hybrid retrieval + A3 hierarchy split (rank on
#    @search.rerankerScore) + GPT-4o Structured Outputs answer (temp=0,
#    seed) -- reused here, not duplicated (axiom A4; see
#    orchestration/README.md for why direct Chat Completions calls are
#    used instead of Foundry Agent Service/Prompt Flow/Workflows).
# 2) Azure AI Foundry evaluators: Groundedness, Relevance, Retrieval
# Keys retrieved at runtime via az (never stored). Requires az login.
# Usage: python evaluate_rag.py
#
# Note (2026-09-10): context fed to the evaluators is now the primary +
# annex sources only (per client's engine.yaml primaryCount/annexCount --
# axiom A3), not a flat top-5 as before. Narrower, hierarchy-aware context
# changes what gets scored, so these results are a NEW baseline, not
# directly comparable to the Jalon 1 scores recorded in project memory
# jalon1.md (those were measured before orchestration/ existed).
# =====================================================================
import json
import sys
from pathlib import Path

from azure.ai.evaluation import (
    GroundednessEvaluator,
    RelevanceEvaluator,
    RetrievalEvaluator,
    evaluate,
)
from openai import AzureOpenAI

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "orchestration"))
from answer import (  # noqa: E402 -- reuse orchestration logic (axiom A4), not a duplicate
    AOAI_ACCOUNT,
    AOAI_API_VERSION,
    AOAI_ENDPOINT,
    RG,
    SEARCH_SERVICE,
    answer_query_core,
    az,
)

CLIENTS = ["clienta", "clientb", "clientc"]  # one DEDICATED index per client (isolation M2)

print("Retrieving keys (runtime, not stored)...")
SEARCH_KEY = az(
    f"az search admin-key show --service-name {SEARCH_SERVICE} "
    f"--resource-group {RG} --query primaryKey -o tsv"
)
AOAI_KEY = az(
    f"az cognitiveservices account keys list --name {AOAI_ACCOUNT} "
    f"--resource-group {RG} --query key1 -o tsv"
)
AOAI_CLIENT = AzureOpenAI(
    azure_endpoint=AOAI_ENDPOINT, api_key=AOAI_KEY, api_version=AOAI_API_VERSION
)

model_config = {
    "azure_endpoint": AOAI_ENDPOINT,
    "api_key": AOAI_KEY,
    "azure_deployment": "gpt-4o",
    "api_version": AOAI_API_VERSION,
}

all_metrics = {}

for CLIENT_ID in CLIENTS:
    print(f"\n=== Client {CLIENT_ID} ===")

    # ---- Phase 1: generate RAG answers via orchestration/answer.py ----
    print("Generating RAG answers (retrieval + A3 split + Structured Outputs)...")
    rows = []
    with open(f"golden_{CLIENT_ID}.jsonl", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            q = json.loads(line)["query"]
            result = answer_query_core(CLIENT_ID, q, SEARCH_KEY, AOAI_CLIENT)
            rows.append(
                {
                    "query": q,
                    "context": result["_trace"]["context"],
                    "response": result["answer"],
                }
            )
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
