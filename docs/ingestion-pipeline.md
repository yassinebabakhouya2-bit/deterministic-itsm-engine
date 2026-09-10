# Ingestion pipeline — SharePoint → Blob → Search

The concrete, deployed data path that feeds the indexing layer described in [`architecture.md`](architecture.md), from a client's SharePoint site through to a queryable Azure AI Search index.

```mermaid
flowchart LR
  SP["SharePoint\n(per client)"] -->|Graph API| LA

  subgraph LA["Logic App — logic-ingest-&lt;client&gt;\n(Bicep, zero connectors, managed identity)"]
    direction TB
    A1["Get secret"] --> A2["Get token (OAuth)"]
    A2 --> A3["List files\n(paginated)"]
    A3 --> A4["Download\n(two-step, handles redirects)"]
    A4 --> A5["Write blob\n+ clientId metadata"]
  end

  KV[("Key Vault")]
  A1 -.App Reg. secret.-> KV

  LA --> Blob[("Blob Storage\nkb-&lt;client&gt;")]

  subgraph SRCH["Azure AI Search"]
    direction TB
    DS["Datasource\nds-&lt;client&gt;"]
    SS["Skillset\nSplit + Embed"]
    IX["Indexer\nix-&lt;client&gt;"]
    IDX[("Dedicated index\nidx-&lt;client&gt;")]
    DS --> IX
    SS --> IX
    IX --> IDX
  end

  Blob --> DS
  AOAI["Azure OpenAI\ntext-embedding-3-large"]
  SS -.embeddings.-> AOAI

  IDX -.roadmap.-> AGENT["AI Foundry\nGPT-4o + grounding"]
  AGENT -.roadmap.-> WEB["Web App / Copilot"]

  style AGENT stroke-dasharray: 5 5
  style WEB stroke-dasharray: 5 5
```

## What each piece does

- **Logic App, one per client** (`ingestion/main.bicep`) — a single Bicep template deployed once per client, parameterized by `clientCode`, `siteId`, `containerName`. Reads the ingestion App Registration's secret from Key Vault, acquires a Microsoft Graph token, lists every file at the client's SharePoint site (following `@odata.nextLink` so libraries beyond the default page size are still ingested in full), downloads each file, and writes it to the client's Blob container with a `clientId` metadata tag. Authentication is 100% system-assigned managed identity — no `Microsoft.Web/connections` resource, no secret outside Key Vault, no custom code.
- **Two download quirk it handles**: Microsoft Graph's `/content` endpoint 302-redirects larger files to a pre-authenticated URL, and Logic Apps won't forward a Bearer token across that redirect — so the download happens in two steps (get the download URL authenticated, then a plain unauthenticated `GET` on it).
- **Blob Storage** — one container per client (`kb-<client>`), a durable raw copy of the source documents, independent of the SharePoint source once written.
- **Azure AI Search pipeline** (`search/*.template.json`) — four objects (datasource, skillset, indexer, index) deployed per client by a generic script. The skillset chunks documents and vectorizes them (`text-embedding-3-large`); each client has its own dedicated index (physical isolation, not just a filter — see `architecture.md`).
- **Not yet built**: AI Foundry orchestration (GPT-4o + grounding on the client's index) and the Web App / Copilot front end, per the roadmap in the root `README.md`.

## Deploying a new client

```bash
# 1. One-time prerequisite (Graph, not IaC): grant Sites.Selected on the client's SharePoint site
#    POST https://graph.microsoft.com/v1.0/sites/{siteId}/permissions

# 2. Ingestion
az deployment group create --resource-group <rg-name> \
  --template-file ingestion/main.bicep --parameters ingestion/example.parameters.json

# 3. Search pipeline
cd search && ./deploy.ps1 -ClientId <client>
```

Real per-client parameter files (with real SharePoint site IDs and tenant IDs) are never committed to this repository — see `ingestion/README.md`.
