# orchestration/ — Orchestration layer (Jalon 3)

Retrieval (Azure AI Search, hybrid + semantic reranker) → hierarchy split (axiom A3) →
generation (Azure OpenAI Chat Completions, Structured Outputs, temperature=0 + seed,
axiom A1) → typed, schema-enforced answer.

## Why not Foundry Agent Service?

Foundry Agent Service (`PromptAgentDefinition` + `AzureAISearchTool`) was the first
option considered — fully managed, deployable via SDK/Bicep. It was dropped because,
as of 2026-09-10, it does not support Structured Outputs (`response_format:
json_schema`) — only direct Chat Completions / Responses API calls on an Azure OpenAI
deployment do. A reliable, schema-enforced answer (no hallucinated shape, an explicit
`ambiguous` flag instead of guessing) was the explicit priority for this milestone, so
this module calls the `aif-knowledgeengine3-v9` deployment directly instead.

Prompt Flow and Foundry's visual Workflows were also ruled out: both are being retired
(Prompt Flow 2027-04-20, Workflows 2026-12-01) — see project memory
`jalon3-orchestration.md` for sources and the full trade-off discussion.

## Usage — CLI (admin keys via `az`)

```bash
pip install -r requirements.txt
az login   # keys are retrieved at runtime via az, never stored (same convention as eval/)
python answer.py --client clienta --query "..."
python answer.py --client client-v --query "..."   # real client — config read from clients-local/
```

## Usage — keyless (RBAC), for app/app.py (Jalon 4)

`answer_query_core_keyless()` + `build_aoai_client_keyless()` use
`DefaultAzureCredential` instead of admin keys — no `az` subprocess call, no key in an
app setting. Used by the Azure Web App (`app/`), whose system-assigned managed
identity needs two roles (granted in `infra/modules/roles.bicep`):

- `Search Index Data Reader` on the Search service
- `Cognitive Services OpenAI User` on the Foundry account (same role Search's own
  identity already holds there, for embeddings)

`DefaultAzureCredential` also resolves through an interactive `az login` session
locally, so the keyless path works for local dev too — it's just not what
`answer_query()` (CLI, above) or `eval/evaluate_rag.py` use; those keep the admin-key
path they were validated with in Jalon 3, untouched.

## Design notes

- **No model-retyped titles**: the model never writes source titles — they're already known deterministically from retrieval. It only returns `primary_source_used`/`related_sources_used` (booleans); the script fills in the real titles afterwards. Added after the first live test (2026-09-10) showed the model paraphrasing "KB-A-001.md" as "KB-A-001" — an avoidable divergence.

- **Axiom A2 (agnosticism)**: all client specifics come from `engine.<client>.yaml` —
  `config/` for tracked synthetic clients, `clients-local/` for real ones (git-ignored).
  This module never hardcodes a client.
- **Axiom A3 (hierarchy)**: primary/annex is a **rank-based** split on
  `@search.rerankerScore` (already computed by the semantic reranker) — Microsoft
  explicitly advises against fine-grained thresholds on that score, so rank is used
  instead of a cutoff value.
- **Axiom A5 (traceability)**: every answer carries a lightweight `_trace` block
  (which source was primary/annex, its reranker score). It does **not** ask the model
  to self-report Groundedness/Relevance/Retrieval — a model grading its own answer is
  itself a hallucination risk. Those scores stay the job of the batch Azure AI Foundry
  Evaluators pipeline (`eval/`), run against a golden dataset, not a per-call estimate.

## Status

`eval/evaluate_rag.py` has been refactored (Jalon 3, commit `eb62091`) to call
`answer_query_core()` from this module instead of duplicating retrieval/generation
(axiom A4) — it fetches admin keys once and reuses this module, it does not have its
own `retrieve()`/`answer()` anymore.

## Diagnostic engine (orchestration/diagnostic/)

Deterministic diagnostic state machine (brick 1, no LLM, not yet wired to the app).
`contracts.py` = Pydantic contracts, `fsm.py` = pure transitions with injected ports
(extraction, risks, retrieval, OCR, question, chunk loading, plan drafting).
Tests: `python -m pytest tests/test_diagnostic_fsm.py` (needs `pydantic`, `pytest`).
Design: see the architecture document "Architecture - Agentic RAG diagnostic ITSM".
