# Azure AI Search index pipeline — multi-tenant, dedicated index per client

**Versioned** and **per-client parameterized** definition of the search chain, deployed via code (REST) — independent of the portal.

**Architecture (M2): one DEDICATED index per client** = physical isolation. A user only ever reaches their own client's index. The `clientId` field is additionally projected into each document (defense in depth / security trimming).

**Ingestion (since 2026-09-13): dual pipeline per client**, both projecting into the same dedicated index — Document Intelligence Layout does not support plain-text formats (`.md`/`.txt`), so extraction is split by file type instead of forced through a single skillset:

| File | Role |
|---|---|
| `index.template.json` | Schema of the `idx-__CLIENTID__` index: `chunk`, `title`, `parent_id`, **`clientId` (filterable)**, `text_vector` (3072 dims), HNSW vector profile + Azure OpenAI vectorizer, semantic config. Shared by both pipelines. |
| `datasource.template.json` | Blob source `kb-__CLIENTID__`, connection via **managed identity** (ResourceId, no key). Shared by both pipelines. |
| `skillset-di.template.json` | **DI pipeline** — `DocumentIntelligenceLayoutSkill` (text + tables, built-in chunking) + integrated embedding. Keyless auth (`AIServicesByIdentity`) on the Foundry resource. Deployed as `ss-__CLIENTID__-di`. |
| `indexer-di.template.json` | Feeds `ss-__CLIENTID__-di`. `indexedFileNameExtensions`: `.pdf,.docx,.xlsx,.pptx,.html,.htm,.jpg,.jpeg,.png,.bmp,.tiff,.tif` (the formats DI actually supports) + `allowSkillsetToReadFileData: true`. Deployed as `ix-__CLIENTID__-di`. |
| `skillset.template.json` | **Native text pipeline** — `SplitSkill` (pages ~2000 / overlap 500) + integrated embedding, unchanged from the original single-pipeline design. Deployed as `ss-__CLIENTID__-text`. |
| `indexer.template.json` | Feeds `ss-__CLIENTID__-text`. `excludedFileNameExtensions` = the same list as the DI indexer's allowlist, so it catches everything DI doesn't handle (`.md`, `.txt`, `.csv`, `.json`, and any future plain-text format) — no file silently falls through either pipeline. Deployed as `ix-__CLIENTID__-text`. |
| `deploy.ps1` | Deploys all 6 objects **for one client** (`-ClientId`): 1 datasource + 1 index + 2 skillsets + 2 indexers. Admin key retrieved at runtime (never committed). Api-version `2026-04-01` (required for `DocumentIntelligenceLayoutSkill`). |

The templates use the `__CLIENTID__` and `__STORAGE_RESOURCE_ID__` tokens, substituted at runtime.

## Deployment (per client)

```powershell
az login                                   # if not already done
cd search
./deploy.ps1 -ClientId clienta
./deploy.ps1 -ClientId clientb
./deploy.ps1 -ClientId clientc
```

Each call creates/updates the dedicated `ds/idx-<clientId>` pair plus the two skillset+indexer pairs (`-di` and `-text`), and starts both indexers.
**Adding a client = one `engine.<client>.yaml` + one `kb-<client>` container + one `deploy.ps1 -ClientId <client>` call. Zero changes to the engine** (axiom A2).

> Determinism (A1): schema, chunking, and vectorizer are pinned here. Embedding pinned to NoAutoUpgrade.
> Isolation (M2): dedicated index + `clientId` as defense in depth.

### Migrating a client that still has the old single-pipeline objects

If a client's index was populated before 2026-09-13 (single `ss-<client>`/`ix-<client>`, no `-di`/`-text` suffix), deploying the new templates creates the two new pipelines **alongside** the old ones — the old indexer is never re-run automatically, but its already-indexed chunks stay in the index and duplicate the new ones. Delete the old objects (and the index, to purge any stale content) before redeploying:

```powershell
Invoke-RestMethod -Method Delete -Uri "https://<service>.search.windows.net/indexes/idx-<client>?api-version=2026-04-01" -Headers $headers
Invoke-RestMethod -Method Delete -Uri "https://<service>.search.windows.net/indexers/ix-<client>?api-version=2026-04-01" -Headers $headers
Invoke-RestMethod -Method Delete -Uri "https://<service>.search.windows.net/skillsets/ss-<client>?api-version=2026-04-01" -Headers $headers
./deploy.ps1 -ClientId <client>
```

See project memory `document-intelligence-migration.md` for the full rollout history and per-client validation results.
