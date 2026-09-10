# Azure AI Search index pipeline — multi-tenant, dedicated index per client

**Versioned** and **per-client parameterized** definition of the search chain, deployed via code (REST) — independent of the portal.

**Architecture (M2): one DEDICATED index per client** = physical isolation. A user only ever reaches their own client's index. The `clientId` field is additionally projected into each document (defense in depth / security trimming).

| File | Role |
|---|---|
| `index.template.json` | Schema of the `idx-__CLIENTID__` index: `chunk`, `title`, `parent_id`, **`clientId` (filterable)**, `text_vector` (3072 dims), HNSW vector profile + Azure OpenAI vectorizer, semantic config. |
| `datasource.template.json` | Blob source `kb-__CLIENTID__`, connection via **managed identity** (ResourceId, no key). |
| `skillset.template.json` | Chunking (SplitSkill, pages ~2000 / overlap 500) + **integrated embedding** (`text-embedding-3-large`) + index projections (one document per chunk) + **`clientId` injection** (blob metadata). |
| `indexer.template.json` | Links source → skillset → dedicated index. |
| `deploy.ps1` | Deploys the 4 objects **for one client** (`-ClientId`), admin key retrieved at runtime (never committed). |

The templates use the `__CLIENTID__` and `__STORAGE_RESOURCE_ID__` tokens, substituted at runtime.

## Deployment (per client)

```powershell
az login                                   # if not already done
cd search
./deploy.ps1 -ClientId clienta
./deploy.ps1 -ClientId clientb
./deploy.ps1 -ClientId clientc
```

Each call creates the dedicated `ds/idx/ss/ix-<clientId>` pipeline and starts the indexer.
**Adding a client = one `engine.<client>.yaml` + one `kb-<client>` container + one `deploy.ps1 -ClientId <client>` call. Zero changes to the engine** (axiom A2).

> Determinism (A1): schema, chunking, and vectorizer are pinned here. Embedding pinned to NoAutoUpgrade.
> Isolation (M2): dedicated index + `clientId` as defense in depth.
