# =====================================================================
# RAG Evaluation — KnowledgeEngine v9 (quantified proof of reliability)
# 1) For each client (dedicated index): orchestration/answer.py's
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
#
# Design note (2026-09-21, Jalon 7 follow-up -- measure before redesign):
# Yassine reported the deployed engine underperforming a comparable
# production tool on a real client and asked for a "refonte" (redesign)
# for determinism/accuracy. Before any architectural change, this script
# is extended to actually MEASURE that client (axiom A5 -- reliability is
# measured, not promised) instead of only ever scoring the synthetic demo
# clients. Real clients (client-s, and any added later) keep their golden
# dataset + every generated eval artifact entirely under clients-local/ --
# NEVER under eval/ -- per the isolation rule in clients-local/README.md
# ("nothing in this folder is ever git add'ed... or referenced by name in
# any file tracked by git"): eval/.gitignore only excludes GENERATED
# artifacts (eval_input_*.jsonl, eval_results_*.json, eval_summary.json),
# not golden_*.jsonl, so a real client's golden dataset must never be
# placed in eval/ -- it would be tracked by git. clients-local/ is
# git-ignored in full, so it's always safe there. Synthetic demo clients
# (clienta/b/c) are unchanged: golden sets tracked in eval/, 100%
# synthetic per eval/README.md.
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

EVAL_DIR = Path(__file__).resolve().parent
CLIENTS_LOCAL_DIR = EVAL_DIR.parent / "clients-local"

# Synthetic demo clients: golden set tracked in eval/, 100% synthetic (isolation M2).
CLIENTS = ["clienta", "clientb", "clientc"]

# Real clients: golden set + every generated artifact live in clients-local/
# (git-ignored in full) -- never in eval/. Add a client code here once its
# clients-local/golden_<id>.jsonl exists; a missing golden file is skipped,
# not a hard failure, so onboarding a new real client never breaks this run.
REAL_CLIENTS = ["client-s"]


def client_base_dir(client_id: str) -> Path:
    return CLIENTS_LOCAL_DIR if client_id in REAL_CLIENTS else EVAL_DIR


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

for CLIENT_ID in CLIENTS + REAL_CLIENTS:
    base_dir = client_base_dir(CLIENT_ID)
    golden_path = base_dir / f"golden_{CLIENT_ID}.jsonl"
    if not golden_path.exists():
        print(f"\n=== Client {CLIENT_ID} -- skipped (no {golden_path}) ===")
        continue

    print(f"\n=== Client {CLIENT_ID} ===")

    # ---- Phase 1: generate RAG answers via orchestration/answer.py ----
    print("Generating RAG answers (retrieval + A3 split + Structured Outputs)...")
    rows = []
    with open(golden_path, encoding="utf-8") as f:
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

    input_path = base_dir / f"eval_input_{CLIENT_ID}.jsonl"
    with open(input_path, "w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    # ---- Phase 2: Azure AI Foundry evaluation ----
    print("Evaluating (Groundedness, Relevance, Retrieval)...")
    result = evaluate(
        data=str(input_path),
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
        output_path=str(base_dir / f"eval_results_{CLIENT_ID}.json"),
    )

    all_metrics[CLIENT_ID] = result["metrics"]

# Synthetic clients' summary stays in eval/ (tracked dir, but the summary
# itself is git-ignored -- eval/eval_summary.json -- same as before).
# Real clients' summary is kept entirely separate, in clients-local/, so
# no real-client entry is ever written under eval/ (defense-in-depth on
# top of the .gitignore rules, per clients-local/README.md).
synthetic_metrics = {c: m for c, m in all_metrics.items() if c in CLIENTS}
real_metrics = {c: m for c, m in all_metrics.items() if c in REAL_CLIENTS}

with open(EVAL_DIR / "eval_summary.json", "w", encoding="utf-8") as f:
    json.dump(synthetic_metrics, f, ensure_ascii=False, indent=2)

if real_metrics:
    with open(CLIENTS_LOCAL_DIR / "eval_summary.json", "w", encoding="utf-8") as f:
        json.dump(real_metrics, f, ensure_ascii=False, indent=2)

print("\n===== CONSOLIDATED RELIABILITY SCORE (KnowledgeEngineV9, averages /5) =====")
for CLIENT_ID, metrics in all_metrics.items():
    print(f"\n{CLIENT_ID}:")
    for k, v in metrics.items():
        print(f"  {k}: {round(v, 3)}")

print("\nFull detail: eval_results_<client>.json (eval/ for demo clients, "
      "clients-local/ for real clients). Summary: eval/eval_summary.json "
      "(demo) and clients-local/eval_summary.json (real, if any).")
