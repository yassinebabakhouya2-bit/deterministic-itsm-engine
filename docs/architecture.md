# Architecture — technical blueprint

### Azure-native · Multimodal · Deterministic · Multi-tenant

## Executive summary

AI assistance platform for enterprise IT support, built entirely on Azure, leveraging multimodal knowledge (documents, screenshots, audio, video) from multiple client organizations within a strictly isolated multi-tenant architecture. Deterministic, client-agnostic design: reproducible, traceable answers, no business logic hardcoded into the engine. Extraction, indexing, orchestration, and evaluation all run on managed Azure services — no custom code.

## Design principles carried through the architecture

- The 5 axioms (below)
- `engine.yaml` per client — agnosticism
- Primary / related-annex response logic
- A structured contract between orchestrator and presentation layer
- Traceability (a `score_report` per answer)
- Managed services over custom code:

| Concern | Approach |
|---|---|
| Extraction (OCR, layout) | Azure AI Document Intelligence |
| Indexing | Azure AI Search (hybrid) |
| Interface | Azure Web App |

## The 5 axioms, reinforced by Azure

| Axiom | Definition | How Azure reinforces it |
|---|---|---|
| **A1 — Determinism** | Reproducible answers (temperature=0, stable tie-breaking) | Azure OpenAI temperature=0 + seed; stable sort in Azure AI Search |
| **A2 — Client-agnosticism** | Zero business logic in the engine; per-client configuration | `engine.yaml` + index/filter by `clientId`; no service contains business rules |
| **A3 — Hierarchy** | ONE primary record + annexes before the LLM | Azure AI Search semantic reranker + retained selection logic |
| **A4 — Separation** | Each layer, one responsibility | extraction → indexing → orchestration → presentation, cleanly separated |
| **A5 — Traceability** | Every decision audited | `score_report` + Azure AI Foundry evaluators (Faithfulness, Groundedness, Relevance) |

## Target architecture — layer by layer

```
SOURCES LAYER (per client, declared in engine.yaml)
  Documents · Screenshots/images · Audio (calls) · Video (tutorials)
        |
EXTRACTION LAYER — 100% Azure services, zero custom OCR
  • Azure AI Document Intelligence  -> text + OCR + layout
  • Azure AI Speech                 -> audio transcription + diarization
  • Azure AI Video Indexer          -> transcript + OCR + topics
        |
INDEXING LAYER — Azure AI Search
  • Integrated vectorization (auto chunking + embeddings)
  • Hybrid: keyword + vector search, merged
  • Semantic reranker (cross-encoder)
  • clientId field on every doc -> security trimming
  • Embeddings: Azure OpenAI text-embedding-3-large
  • Multimodal: image verbalization + visual embeddings
        |
ORCHESTRATION LAYER — Azure AI Foundry + Azure OpenAI
  • Primary / related selection (axiom A3)
  • GPT-4o synthesis (temperature=0, axiom A1)
  • Multi-turn history retained
        |
EVALUATION LAYER — Azure AI Foundry Evaluators
  • Faithfulness · Groundedness · Relevance
  • Golden dataset per client, periodic run
        |
ACCESS & SECURITY LAYER
  • Azure Entra ID (SSO) -> user resolution -> client
  • Azure Web App — main interface
  • Copilot agent (Copilot Studio) — conversational access
```

### Sources (agnostic, per client)

Each client declares its sources in its own `engine.yaml`. No source is hardcoded. Supported types: documents (PDF, DOCX, MD, HTML), screenshots/images, audio (support calls), video (tutorials, procedure recordings).

### Extraction — native Azure services only

Non-negotiable design principle: no Python OCR/text-extraction code. Everything goes through managed services.

- **Azure AI Document Intelligence** — text extraction, OCR, structure (tables, layout) for documents and screenshots.
- **Azure AI Speech** (batch + diarization) — support-call transcription with speaker separation and timestamps.
- **Azure AI Video Indexer** — transcription, on-screen OCR, topic detection, and timestamps for videos.

Multimodal indexing follows two paths: **verbalization** (diagrams/schematics described in text for grounding) and **direct embeddings** (screenshots/photos for visual similarity).

### Indexing — hybrid Azure AI Search

- **Integrated vectorization**: Azure AI Search automatically chunks and vectorizes at indexing and query time.
- **Hybrid search**: keyword and vector search run in parallel, results merged and reranked — maximizes recall.
- **Semantic reranker**: final neural reranking, serving axiom A3 (hierarchy).
- **Embeddings**: Azure OpenAI `text-embedding-3-large`.

### Orchestration — Azure AI Foundry + Azure OpenAI

The response business logic (primary/related, format, multi-turn) lives in Foundry orchestration. GPT-4o at temperature=0 for determinism. The structured contract (primary, related, ambiguous, score_report) is retained end to end.

### Evaluation — Azure AI Foundry Evaluators

Three native Azure evaluators score every response against a *golden dataset* per client (typical questions + expected answers):

- **Faithfulness / Groundedness** — is the answer grounded in the sources, not hallucinated?
- **Relevance** — does the answer actually address the question?

A periodic evaluation run produces a quantified reliability score per client — evaluation turns reliability from an assumption into a measured, reproducible property of the system.

### Access & security — Entra ID + multi-tenant isolation (Jalon 5)

- **Azure Entra ID (SSO)** authenticates the user via their own organization's tenant (App Registration is multi-tenant — any Entra tenant can complete sign-in once its admin consents, so tenant restriction is enforced explicitly, not left to Entra ID itself).
- **User → client resolution, two levels**: (1) the token's tenant ID resolves the user's organization against every client's declared `entraTenantId` — an unrecognized tenant is denied outright; (2) within a tenant that hosts several clients (Yassine's own sandbox today, or an external organization with several of its own entities), the token's Entra group resolves the specific client. A tenant with no `entraGroup` declared for any client is treated as "whole tenant = one client" — the default for a newly onboarded external organization.
- **Isolation via dedicated index + security trimming**: each client has its own Azure AI Search index; a user is only ever routed to their own organization's index, and the query itself additionally carries an explicit `clientId eq '...'` filter (defense in depth, on top of — not instead of — the physical per-index separation).

Three possible isolation levels, in increasing order of strength:

1. **Shared index + `clientId` filter** — simple, economical, logical isolation.
2. **Dedicated index per client** — total physical isolation (the model used here).
3. **Hybrid** — shared for small clients, dedicated for large or sensitive ones.

### Conversational access — Copilot agent

A **Copilot Studio** agent provides a conversational entry point integrated into the client's Microsoft 365 ecosystem, alongside the Web App — querying the organization's KB directly from Teams/Copilot, respecting multi-tenant isolation.

## Multi-tenant model — agnosticism in practice

Adding a client means declaring an `engine.yaml`, provisioning its `kb-<client>` container, and deploying its dedicated `idx-<client>` index. **Zero engine modification.** The demo tenants in this repository are 100% synthetic: Client A, Client B, Client C.

## Positioning against 2026 industry standards

| Industry expectation | Requirement | This platform's response |
|---|---|---|
| Getting past the pilot stage | Production-grade, not a fragile prototype | Managed Azure services end to end, no custom extraction/indexing code |
| The agentic shift | AI acts, under guardrails | Copilot + Foundry orchestration + the 5 axioms |
| Human-in-the-loop | Human validation on ambiguous cases | A mode that surfaces ambiguity to the user instead of guessing |
| Quality as a reliability discipline | Continuous evaluation | Azure AI Foundry Evaluators + a golden dataset per client |
| Fresh data | Up-to-date index | Incremental Azure AI Search indexing |
| Governance & access control | Real access control, not display-level filtering | Entra ID + per-client security trimming, dedicated index |
| Avoiding vendor/client lock-in | Engine neutrality | `engine.yaml` agnosticism — clients are interchangeable configuration |
| Multimodal by default | Text + audio + video | Document Intelligence + Speech + Video Indexer (roadmap) |

## Cost profile

Paid services involved as the roadmap is built out: Document Intelligence, AI Search (Basic tier or above), Speech, Video Indexer, Azure OpenAI, App Service. For a proof-of-concept scope, only the services needed for the current step should stay active — the heaviest line items are typically Video Indexer and AI Search. A controlled POC, activated incrementally, runs from a few tens to roughly €150/month depending on which layers are live.

See [`ingestion-pipeline.md`](ingestion-pipeline.md) for the ingestion flow that feeds the indexing layer described above.
