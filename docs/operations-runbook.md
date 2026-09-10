# Operations runbook — onboarding & indexer troubleshooting

Copy-paste-ready commands for running this pipeline in production: onboarding
a client end to end, and diagnosing/fixing a stuck or timed-out Azure AI
Search indexer. Everything below was exercised against a real deployment;
`<client>`, `<resource-group>`, `<search-service>`, `<storage-account>`,
`<key-vault>`, `<ai-services-account>` are placeholders for values fixed once
per deployment (see `infra/main.bicepparam` and `infra/main.bicep` outputs).

Run from Azure Cloud Shell (bash) or any shell with `az` logged in.
Cloud Shell persists files under `$HOME` across sessions, but **not shell
variables** (`$KEY`) — regenerate those every new session.

---

## 0. Platform foundation — one-time setup (before any client)

Everything below creates the shared AI backend that every client's pipeline
plugs into: Blob storage, Azure AI Search, and Azure AI Foundry with its
model deployments. It's provisioned once per environment, not per client.

1. **Resource group** (Bicep in this repo deploys *into* it, not the group
   itself):
   ```bash
   az group create --name <resource-group> --location francecentral
   ```

2. **Key Vault** — holds the ingestion App Registration's client secret.
   Not created by `infra/main.bicep` (it's referenced as an `existing`
   resource from `ingestion/main.bicep`), so create it once, separately:
   ```bash
   az keyvault create --name <key-vault> --resource-group <resource-group> \
     --location francecentral --enable-rbac-authorization true
   ```
   Then create the Entra ID App Registration used for Graph access to
   SharePoint (`az ad app create --display-name <app-name>` + a client
   secret, plus the `Sites.Selected` Graph API permission — see §1 below),
   and store its secret:
   ```bash
   az keyvault secret set --vault-name <key-vault> --name <secret-name> \
     --value "<app-registration-client-secret>"
   ```
   Grant the Logic Apps' managed identities the **Key Vault Secrets User**
   role on this vault once they exist (handled automatically per client by
   `ingestion/main.bicep`'s `createRoleAssignments` parameter).

3. **Deploy the foundation** (`infra/main.bicep` — Storage, AI Search, AI
   Foundry + model deployments, and the RBAC linking them together, all in
   one deployment):
   ```bash
   az deployment group what-if \
     --resource-group <resource-group> \
     --template-file infra/main.bicep --parameters infra/main.bicepparam

   az deployment group create \
     --resource-group <resource-group> \
     --template-file infra/main.bicep --parameters infra/main.bicepparam
   ```
   This provisions, in dependency order:
   - **Storage account** (`modules/storage.bicep`) — Standard_LRS, TLS 1.2
     minimum, no public blob access, 7-day soft delete, one private
     container per demo client (`kb-clienta/b/c` — add real clients'
     containers the same way, or let `ingestion/main.bicep` create them
     implicitly via its own container parameter).
   - **Azure AI Search** (`modules/search.bicep`) — Basic tier, system-assigned
     managed identity, semantic search enabled (required for the semantic
     reranker), AAD-or-key auth.
   - **Azure AI Foundry** (`modules/foundry.bicep`) — an `AIServices`
     account + default project, with two **regional, version-pinned**
     model deployments (`versionUpgradeOption: NoAutoUpgrade`, axiom A1):
     `gpt-4o` (2024-11-20, Standard, 50K TPM) and `text-embedding-3-large`
     (Standard, 120K TPM) — deployed sequentially (`dependsOn`) since two
     OpenAI deployments on the same account can't be created concurrently.
   - **RBAC** (`modules/roles.bicep`) — grants Search's managed identity
     **Storage Blob Data Reader** on the storage account and **Cognitive
     Services OpenAI User** on the Foundry account, so integrated
     vectorization works with zero stored keys.

4. Verify:
   ```bash
   az deployment group show --resource-group <resource-group> --name main \
     --query "properties.provisioningState" -o tsv
   ```
   Expect `Succeeded`. The outputs (`storageAccountName`, `searchServiceName`,
   `foundryName`) confirm the exact resource names created, for use in every
   command in the rest of this runbook.

Everything from here on (per-client ingestion, per-client Search pipeline,
indexer troubleshooting) runs against this same foundation — no change to
these resources is needed to onboard a new client.

---

## 1. One-time prerequisite per client (outside Bicep)

Grant `Sites.Selected` on the client's SharePoint site to the ingestion App
Registration, via Graph — this is a Microsoft Graph permission, not an Azure
resource, so it isn't IaC:

```
POST https://graph.microsoft.com/v1.0/sites/{siteId}/permissions
```

Get the client's `siteId` (format `hostname,siteCollectionId,webId`) first.

---

## 2. Onboard a client — ingestion (SharePoint → Blob)

1. Create `<client>.parameters.json` (copy `ingestion/example.parameters.json`,
   fill in `clientCode`, `siteId`, `containerName`, `tenantId`, `appClientId`).
   Leave `listQuery: ""` for a full ingestion, or `"?$top=1"` to smoke-test a
   single file first.

2. Preview:
   ```bash
   az deployment group what-if \
     --resource-group <resource-group> \
     --template-file ingestion/main.bicep \
     --parameters <client>.parameters.json
   ```

3. Deploy:
   ```bash
   az deployment group create \
     --resource-group <resource-group> \
     --template-file ingestion/main.bicep \
     --parameters <client>.parameters.json
   ```
   Confirm `"provisioningState": "Succeeded"` in the output.

4. The Logic App fires automatically on its recurrence trigger. If the very
   first run fails with `Forbidden` on `Get_secret`: wait 2–5 minutes (RBAC
   propagation) and re-run manually (Portal → Logic app → Overview → Run
   Trigger → Run).

5. Verify the Blob container has **every** expected document — compare
   against the real known count on the SharePoint side. A "Succeeded" run
   status only means no error occurred on what it processed, not that
   everything was processed.

---

## 3. Onboard a client — Azure AI Search pipeline (Blob → Index)

Once the Blob container is confirmed complete:

```powershell
cd search
./deploy.ps1 -ClientId <client>
```

Creates `ds-<client>`, `idx-<client>`, `ss-<client>`, `ix-<client>` and
starts the indexer automatically — first creation only; redeploying onto an
existing indexer does not restart it by itself (see §3).

Check the result:

```bash
KEY=$(az search admin-key show --service-name <search-service> --resource-group <resource-group> --query primaryKey -o tsv)

az rest --method get \
  --url "https://<search-service>.search.windows.net/indexers/ix-<client>/status?api-version=2024-07-01" \
  --headers "api-key=$KEY" \
  --query "{status:status, lastStatus:lastResult.status, errorMessage:lastResult.errorMessage, itemsProcessed:lastResult.itemsProcessed, itemsFailed:lastResult.itemsFailed}"
```

Target: `"status": "success"`, `itemsProcessed` = expected total.

---

## 4. Re-run an existing indexer (e.g. after a smoke test, before a full run)

A `PUT` on an indexer that already exists (a plain `./deploy.ps1` redeploy)
does **not** restart execution by itself. Trigger it explicitly:

```bash
curl -s -X POST "https://<search-service>.search.windows.net/indexers/ix-<client>/run?api-version=2024-07-01" \
  -H "api-key: $KEY" -d "" -w "\nHTTP %{http_code}\n"
```

`-d ""` is required — without it, `HTTP 411 Length Required`.
`HTTP 202` = accepted. `HTTP 409` = a run is already in progress, wait.

---

## 5. Troubleshooting — stuck or timed-out indexer

**Symptom A — timeout failure**: `"The request was canceled due to the
configured HttpClient.Timeout of 100 seconds elapsing"`. Not critical by
itself, but signals batches too heavy for the service (large documents,
many chunks to vectorize at once).

**Symptom B — frozen run**: `status: running` but `itemsProcessed` hasn't
moved in several minutes (compare to the previous run's pace — if the last
run processed 90 docs in 10 minutes and the new one is at 0 after 15, that's
a stall, not just slowness).

**Diagnose first — rule out Azure OpenAI throttling**:

```bash
AOAI_ID=$(az cognitiveservices account show --name <ai-services-account> --resource-group <resource-group> --query id -o tsv)

az monitor metrics list --resource "$AOAI_ID" \
  --metric "ClientErrors" \
  --interval PT1M \
  --start-time $(date -u -d '-20 minutes' +%Y-%m-%dT%H:%M:%SZ) \
  --aggregation Total \
  --query "value[0].timeseries[0].data[?total!=null].{time:timeStamp, errors:total}" \
  -o table
```

Non-zero errors in the stuck run's window → throttling confirmed (slow down
the call rate, or wait). All zero → not throttling, move to the fix below.

**Fix — a more robust indexer configuration** (one-time, then reusable for
every future client) — already applied in `search/indexer.template.json`:

```json
"parameters": {
  "batchSize": 1,
  "maxFailedItems": -1,
  "maxFailedItemsPerBatch": -1,
  "configuration": {
    "dataToExtract": "contentAndMetadata",
    "parsingMode": "default",
    "indexStorageMetadataOnlyForOversizedDocuments": true
  }
}
```

`batchSize: 1` processes one document at a time (a checkpoint after each
document — visible progress, a stall becomes far less likely).
`maxFailedItems`/`maxFailedItemsPerBatch: -1` stops one bad document from
failing the whole run. `indexStorageMetadataOnlyForOversizedDocuments` sends
files over the tier's size limit (16MB on Basic) to metadata-only instead of
failing extraction outright.

⚠️ Do **not** put `batchSize` on the `AzureOpenAIEmbeddingSkill` itself (in
`skillset.template.json`) — that property doesn't exist on the stable API
`2024-07-01` and returns `HTTP 400`. Only the **indexer**-level `batchSize`
works.

Redeploy with the updated config:

```powershell
./deploy.ps1 -ClientId <client>
```

**Unstick a frozen run** — there's no reliable REST cancel (`search.cancel`
returns `404` on this API, stable and preview alike):

1. Azure Portal → `<search-service>` → Indexers → click `ix-<client>` (the
   name, not a history row).
2. **Reset** button at the top of the page.
3. **Run** button right after.

Confirm it's actually progressing this time (status + climbing
`itemsProcessed`), refreshing every 2–3 minutes, or via the command in §2.

---

## 6. Reminders

- `$KEY` does not persist across Cloud Shell sessions — always regenerate:
  ```bash
  KEY=$(az search admin-key show --service-name <search-service> --resource-group <resource-group> --query primaryKey -o tsv)
  ```
- Never paste the App Registration secret or the Search admin key in clear
  text into any committed file, anywhere — always fetched at runtime
  (`az search admin-key show`; Key Vault via the Logic App's HTTP action).
- Real `<client>.parameters.json` files (real SharePoint `siteId`s) never go
  into this repository — see `ingestion/README.md` and `clients-local/`
  (git-ignored, local only) in this project.
