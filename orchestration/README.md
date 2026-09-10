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
this module calls the `aif-knowledgeengine2-v9` deployment directly instead.

Prompt Flow and Foundry's visual Workflows were also ruled out: both are being retired
(Prompt Flow 2027-04-20, Workflows 2026-12-01) — see project memory
`jalon3-orchestration.md` for sources and the full trade-off discussion.

## Usage

```bash
pip install -r requirements.txt
az login   # keys are retrieved at runtime via az, never stored (same convention as eval/)
python answer.py --client clienta --query "..."
python answer.py --client client-v --query "..."   # real client — config read from clients-local/
```

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

## Not yet done

`eval/evaluate_rag.py` still has its own, separate `retrieve()`/`answer()` (predates
this module, produced the validated Jalon 1 scores). It has **not** been refactored to
call this module yet — deliberately, to avoid touching a validated harness without
being able to re-run it end-to-end from this session. Refactor it once `answer.py` has
been validated live (via Azure Cloud Shell, same as the rest of the project).
