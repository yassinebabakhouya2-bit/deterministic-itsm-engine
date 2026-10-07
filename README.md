# Deterministic ITSM Engine — Azure-native AI platform for enterprise IT support

*(GitHub repo: `deterministic-itsm-engine`, formerly `knowledgeengine-rag-platform`.)*

One deterministic core, two applications. Everywhere this platform lets an LLM touch something real — which KB fiche to show, which ITSM action to take — **the code decides, never the model**: the LLM is restricted to understanding input and drafting already-chosen content, and every decision is filtered through closed enums, verbatim verification, or explicit guardrails before it reaches a human or a system of record.

- **Diagnostic engine** — ingests multimodal knowledge (documents, screenshots, support-call audio, video) from multiple client organizations in a strictly isolated multi-tenant architecture, and guides a support agent or end user step by step to resolution. Every answer is traceable, measured, and never leaked across clients. A newer, harder track (`kecore/` · `kefind/` · `scoreboard/`) pushes determinism further: the engine finds the exact fiche and every cited step is checked verbatim against the source — see [`docs/v10-deterministic-engine.md`](docs/v10-deterministic-engine.md).
- **ITSM action engine** — reads ServiceNow tickets, lets GPT-4o propose one action from a closed list, filters it through deterministic guardrails, waits for a human agent's approval, then executes it in Entra ID (group access, offboarding, password reset, MFA reset) and closes the ticket. This module is fully built and validated live end to end (Jalon 10).

Both modules share the same five axioms and the same non-negotiable principle: **determinism is enforced in code, not asked of the model.** This repository is the technical showcase: the reusable engine, the infrastructure-as-code, and three synthetic demo clients (Client A/B/C — no real client data). Fully cloud-native: extraction, indexing, orchestration, integration and evaluation all run on managed Azure services — no custom OCR, no self-hosted index, no always-on custom integration code.

## The 5 axioms

| Axiom | Principle |
|---|---|
| **A1 — Determinism** | Reproducible outputs: pinned model versions, temperature=0 + fixed seed, strict `json_schema` Structured Outputs, closed action enums, rules enforced in code rather than in the prompt. |
| **A2 — Client-agnosticism** | Zero business logic in the engine; all client specifics live in an `engine.<client>.yaml`. Onboarding a client is configuration, not development. |
| **A3 — Hierarchy** | One primary text record + at most two annexes, selected by rank before the LLM call. Audio and video are never the primary source. |
| **A4 — Separation of responsibilities** | Extraction → indexing → orchestration → presentation as separate services; in ITSM, *propose*, *approve* and *execute* are three distinct identities. |
| **A5 — Traceability** | Every answer cites its sources and is measured (Azure AI Foundry evaluators: Groundedness, Relevance, Retrieval); every ITSM action is logged in the table and the ticket. |

## Architecture at a glance

```mermaid
flowchart LR
  subgraph ING["Ingestion (background)"]
    direction LR
    SP["SharePoint<br/>docs · audio"] --> LA["Logic Apps<br/>ingest · transcribe"] --> BL["Blob Storage<br/>one container per client"] --> SK["Indexer + skillset<br/>Document Intelligence · Speech<br/>enrichment · embeddings"] --> IX["AI Search<br/>one index per client"]
  end
  subgraph ASK["Diagnostic engine (per question)"]
    direction LR
    AG["Support agent / user"] --> EN["Entra ID<br/>tenant + group → client"] --> WA["Web App"] --> SR["AI Search<br/>hybrid + reranker"] --> G4["GPT-4o<br/>primary + annexes, code decides"]
  end
  subgraph ITSM["ITSM action engine (every 2–5 min)"]
    direction LR
    SN["ServiceNow"] --> PP["Logic Apps<br/>poll · propose"] --> TB["Table<br/>itsmtickets"] --> UI["Web App /itsm<br/>human approval"] --> EX["Logic App execute<br/>Graph + ServiceNow"]
  end
  IX -.same index.-> SR
```

Each client is isolated by a **dedicated Azure AI Search index** (physical isolation, not just a filter), with the `clientId` field projected onto every document and enforced on every query as defense in depth. Users are resolved to a client from their Entra ID tenant and security group; an unknown tenant or group sees nothing (deny-by-default).

See [`docs/architecture.md`](docs/architecture.md) for the full technical blueprint, [`docs/ingestion-pipeline.md`](docs/ingestion-pipeline.md) for the SharePoint → Blob → Search flow, [`docs/operations-runbook.md`](docs/operations-runbook.md) for deployment and operations (jalon by jalon), and [`docs/v10-deterministic-engine.md`](docs/v10-deterministic-engine.md) for the deterministic diagnostic track.

## Azure stack

- **Models**: Azure AI Foundry — GPT-4o (answers, ITSM proposals, screenshot vision), a separate GPT-4o deployment for enrichment, `text-embedding-3-large`.
- **Indexing**: hybrid Azure AI Search (BM25 + vectors + semantic reranker), synonym maps and entity boosting, per-client indexers and skillsets.
- **Extraction**: Document Intelligence (layout → markdown chunking), AI Speech (batch transcription with PII redaction), Video Indexer.
- **Enrichment**: one Azure Function called by the skillset (entities, aliases, summaries), with a Table Storage cache.
- **Integration**: Logic Apps with plain HTTP actions and managed identities — zero managed connectors — for SharePoint ingestion, call transcription and the three ITSM workflows.
- **App**: App Service Web App (Flask) behind Easy Auth (Entra ID), multi-turn conversations stored in Table Storage.
- **Security**: managed identities and RBAC; remaining secrets only in Key Vault; ITSM executor limited to Helpdesk Administrator; one-time delivery of temporary passwords / Temporary Access Passes through a dedicated Key Vault.
- **Evaluation**: Azure AI Evaluation SDK (Groundedness, Relevance, Retrieval) against a golden dataset per client, runs logged to the Azure AI Foundry project.

## Azure managed services used

Every runtime component is an Azure managed service: Azure runs the servers, patching, scaling and retries; the repository only holds configuration (Bicep, workflow JSON, index and skillset templates, per-client YAML).

| Managed service | Resources | Role in the platform |
|---|---|---|
| **Azure AI Foundry** (AI Services) | 1 account + 1 project | Hosts the model deployments (GPT-4o, GPT-4o for enrichment, `text-embedding-3-large`) and the cognitive services below; the project receives evaluation runs. |
| **Azure OpenAI in Foundry** | 3 deployments | Answer generation, ITSM action proposals, screenshot reading (vision), evaluation judge; embeddings for vector search. |
| **Azure AI Document Intelligence** | via Foundry | Extracts text, tables and heading structure from PDF, Word, PowerPoint and images during indexing. |
| **Azure AI Speech** | via Foundry | Batch transcription of support calls, with speaker separation. |
| **Azure AI Video Indexer** | 1 account | Transcript, on-screen text and topics from tutorial videos. |
| **Azure AI Search** | 1 service, 1 index per client | Hybrid search (BM25 + vectors + semantic reranker), indexers and skillsets that turn blobs into indexed chunks, synonym maps. |
| **Azure Logic Apps** (Consumption) | ingestion, transcription, 3 ITSM workflows | Scheduled workflows with plain HTTP actions and managed identities: SharePoint → Blob, audio → Speech, ServiceNow poll / propose / execute. |
| **Azure Functions** | 1 app | Enrichment skill called by the skillsets (entities, aliases, summaries). |
| **Azure App Service** | 1 plan (Linux) + 1 Web App | Hosts the diagnostic engine and the `/itsm` approval tab, behind Easy Auth. |
| **Azure Storage** | 1 account | Blob: raw files per client container. Table: conversations, ITSM tickets, enrichment cache. |
| **Azure Key Vault** | 2 vaults | Integration secrets (SharePoint, Speech, ServiceNow) and one-time delivery of temporary passwords / Temporary Access Passes. |
| **Microsoft Entra ID** | tenant, groups, app registrations | Sign-in, user → client resolution by tenant and group, managed identities, target of ITSM actions through Microsoft Graph. |
| **Azure Monitor** | Log Analytics + Application Insights | Logs, performance and failures of the enrichment Function; smart-detection alerts. |

External, non-Azure: **ServiceNow** (ticket source, REST Table API) and **SharePoint Online** (client knowledge source, read through Microsoft Graph).

## Repository structure

```
infra/          Platform Infrastructure as Code (Bicep) — storage, search, Foundry, Function, Video Indexer, Web App, RBAC
ingestion/      SharePoint → Blob ingestion and audio transcription Logic Apps (Bicep, zero connectors)
search/         Azure AI Search templates (datasource, skillsets, indexers, index, synonym map) + deploy.ps1
enrichment/     Azure Function called by the skillsets (entities, aliases, summaries)
orchestration/  guide/ — deterministic state machine: exact fiche -> step-by-step guidance -> solved (no escalation)
app/            Web App: diagnostic engine + /itsm approval tab (Entra ID auth, client resolution)
itsm/           ITSM action engine Logic Apps: poll (ServiceNow → table), propose (GPT-4o + guardrails), execute (Graph + ServiceNow)
scripts/itsm/   Operator scripts: demo identities and tickets, Graph app roles, demo reset
config/         Per-client configuration (engine.<client>.yaml) and ITSM configuration (itsm.yaml)
eval/           Golden datasets and evaluation script (Client A/B/C, synthetic)
kb/             Synthetic knowledge base content for the three demo clients
demo/           Static demo build
docs/           Architecture blueprint, ingestion pipeline, operations runbook (jalon by jalon), deterministic engine track
scoreboard/     Measurement harness — replays labeled tickets, reports metrics with confidence intervals
kecore/         KB fiche decomposition into verified steps (verbatim checks, per-client writing profile + dynamic dictionary, self-consistency)
kefind/         Deterministic 5-step RAG built on kecore (understand · search · filter · decide · compose)
kecore_func/    Azure Function running the kecore decomposition (Durable Functions, blob storage, managed identity)
```

## Deployment

```bash
az login
az account set --subscription "<your subscription>"

# 1. Platform foundation (once)
az deployment group create --resource-group <rg-name> \
  --template-file infra/main.bicep --parameters infra/main.bicepparam

# 2. Per-client Search pipeline
cd search && ./deploy.ps1 -ClientId clienta

# 3. Per-client ingestion (optional, if sourcing from SharePoint)
az deployment group create --resource-group <rg-name> \
  --template-file ingestion/main.bicep --parameters ingestion/example.parameters.json

# 4. ITSM action engine (optional) — see docs/operations-runbook.md §11
az deployment group create --resource-group <rg-name> --template-file itsm/poll/main.bicep ...
```

Onboarding a new client never touches the engine: a new `config/engine.<client>.yaml`, a new `kb-<client>` container, and one `search/deploy.ps1 -ClientId <client>` call.

## Roadmap — jalons 1 through 10 (closed), v10 in progress

| # | Jalon | Status |
|---|---|---|
| 1 | Socle + evaluation (Foundry, AI Search, golden datasets) | ✅ Closed |
| 2 | Real multi-client ingestion (SharePoint → Blob → Search) | ✅ Closed |
| 3 | Orchestration (retrieval, A3 primary/annex, structured generation) | ✅ Closed |
| 4 | Web App / demo interface | ✅ Closed |
| 5 | Auth & tenant isolation (Entra ID SSO, multi-tenant, groups, security trimming) | ✅ Closed |
| 6 / 6bis | External multi-format ingestion + Document Intelligence migration (all clients) | ✅ Closed |
| 7 | Audio (Azure AI Speech, PII redaction, call summaries) | ✅ Closed |
| 8 | Video (Azure AI Video Indexer) | 🟡 Partial — service deployed, scheduled pipeline pending |
| 9 | GraphRAG multimodal + iterative diagnostic (deterministic state machine, no escalation) | ✅ Closed — see `orchestration/guide/` |
| 10 | **ITSM action engine** (ServiceNow + Entra ID, GPT-4o proposal + deterministic guardrails, human approval, execution, one-time secret delivery) | ✅ **Closed, validated live end to end** — replayable demo (`scripts/itsm/reset-demo.ps1`) |
| V10 | Deterministic diagnostic track (`scoreboard`, `kecore`, `kefind`) — measurement harness, KB decomposition with self-consistency, dynamic per-client dictionary, entity + graph fiche finder | 🟡 In progress — moving to Azure-native (Bicep): slices 1–3 of 7 deployed (KB decomposition and the entity/graph fiche finder both run in Azure, confirmed on real data) — see [`docs/v10-deterministic-engine.md`](docs/v10-deterministic-engine.md) |
| — | Copilot Studio agent | 🟢 Planned |

Convention: one jalon = one dedicated conversation, commit/push at every significant step, operations logged live in `docs/operations-runbook.md` (see `CLAUDE.md`).

---

*Designed and developed by Yassine Baba Khouya. Property of the author.*
