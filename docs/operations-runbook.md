# Operations runbook — clone, deploy, onboard a client, troubleshoot

Copy-paste-ready commands for running this pipeline in production: cloning
and standing the whole platform up from scratch, onboarding a client end to
end (SharePoint access, ingestion, search, auth/isolation, web app), and
diagnosing/fixing a stuck or timed-out Azure AI Search indexer. Everything
below was exercised against a real deployment; `<client>`, `<resource-group>`,
`<search-service>`, `<storage-account>`, `<key-vault>`, `<ai-services-account>`
are placeholders for values fixed once per deployment (see
`infra/main.bicepparam` and `infra/main.bicep` outputs).

**Every session working in this repo is expected to keep this file current**
— see `CLAUDE.md` at the repo root for the rule and where a given entry
belongs. A step marked "not yet captured" below is a known gap, not a
placeholder to leave forever: close it the next time you actually run that
step.

Run Azure CLI / REST commands from Azure Cloud Shell (bash) or any shell
with `az` logged in. Cloud Shell persists files under `$HOME` across
sessions, but **not shell variables** (`$KEY`) — regenerate those every new
session.

## Contents

- §-1 Clone this repository
- §0 Platform foundation — one-time setup (before any client)
- §0bis Two App Registrations required once per environment
- §1 One-time-per-client prerequisite — SharePoint `Sites.Selected` + admin consent
- §2 Onboard a client — ingestion (SharePoint → Blob)
- §3 Onboard a client — Azure AI Search pipeline (Blob → Index)
- §4 Re-run an existing indexer
- §5 Troubleshooting — stuck or timed-out indexer
- §6 Onboard a client — auth & tenant isolation (Jalon 5)
- §7 Web app deployment (zip-deploy) — code/config changes
- §8 Reminders
- §9 Audio pipeline (Jalon 7) — metadata repair & PII/summary backfill
- §10 Video pipeline (Jalon 8) — Video Indexer account setup & API auth chain
- §11 ITSM action module (Jalon 10) — demo identities, Logic Apps, demo replay
- §12 Rebuild in a new tenant — `scripts/bootstrap-new-tenant.ps1`
- §13 Diagnostic engine (deterministic agentic RAG) — Diagnostic tab, ServiceNow webhook, display fixes
- §14 Repository renamed to `deterministic-itsm-engine` (2026-10-05)
- §15 V10 on Azure — slice 1: `fn-kecore` Function App, per-client containers (2026-10-06)
- §16 V10 on Azure — slice 2: kecore decomposition in the Function, parity test (2026-10-06)

---

## §-1. Clone this repository

```bash
git clone https://github.com/yassinebabakhouya2-bit/deterministic-itsm-engine.git
cd deterministic-itsm-engine  # renamed 2026-10-05 from knowledgeengine-rag-platform; existing local clones keep working via `git remote set-url origin ...` (same history).
```

Private repo — needs an authenticated GitHub account with access. `git
push`/`git fetch` need an interactive credential prompt (Windows Git
Credential Manager) — that only works from Yassine's own terminal, never
from a non-interactive shell (see §8).

What a fresh clone does **not** give you, by design:
- `clients-local/` — real client parameter files (`siteId`, tenant/app IDs)
  and any non-demo `engine.<client>.yaml`. Git-ignored on purpose (isolation
  rule, see `clients-local/README.md`). Recreate per client as you onboard
  it (§1-§3, §6).
- Any deployed Azure resource. §0 onwards builds those from scratch.
- Azure secrets (App Registration client secret, Search admin key, Speech
  key). Fetched or generated at deploy time, stored in Key Vault, never
  committed (see §8).

Local dev / running scripts against a deployed environment needs `az` CLI
logged in (`az login`) and, per component, its own `requirements.txt`
(`app/`, `orchestration/`, `eval/`, `ingestion/` as applicable) —
`pip install -r <component>/requirements.txt`.

---

## §0. Platform foundation — one-time setup (before any client)

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
   SharePoint (see §0bis), and store its secret:
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
     **Storage Blob Data Reader** on the storage account, **Cognitive
     Services OpenAI User** on the Foundry account (integrated
     vectorization with zero stored keys), and **Cognitive Services User**
     on the Foundry account for Document Intelligence Layout extraction
     (see §3) — plus, once the web app exists (§7), **Storage Blob Data
     Reader** for its managed identity too (needed by the `/transcript`
     route, §9).

4. Verify:
   ```bash
   az deployment group show --resource-group <resource-group> --name main \
     --query "properties.provisioningState" -o tsv
   ```
   Expect `Succeeded`. The outputs (`storageAccountName`, `searchServiceName`,
   `foundryName`) confirm the exact resource names created, for use in every
   command in the rest of this runbook.

Everything from here on (per-client ingestion, per-client Search pipeline,
indexer troubleshooting, auth, web app) runs against this same foundation —
no change to these resources is needed to onboard a new client.

---

## §0bis. Two App Registrations required once per environment

Both are created once, never per client. Confusing the two, or skipping
one, is the most common source of "it works for clienta but not for a new
client" bugs.

1. **Ingestion app** (`knowledgeengine-sharepoint-ingestion`) — used by
   every client's Logic App to read that client's SharePoint via Graph.
   - Must be **multi-tenant** (`signInAudience: AzureADMultipleOrgs`) —
     required so it can be granted `Sites.Selected` and consented on a
     client's *own* tenant, external or not.
   - Must have **at least one redirect URI registered**, even though it is
     never actually used in practice — otherwise the admin-consent step in
     §1 fails with `AADSTS500113: No reply address is registered for the
     application`. Set once, in the tenant that hosts the app:
     Portal → **Microsoft Entra ID** → **App registrations** →
     `knowledgeengine-sharepoint-ingestion` → **Authentication** → add a
     **Web** redirect URI:
     ```
     https://login.microsoftonline.com/common/oauth2/nativeclient
     ```
   - Its client secret lives in Key Vault (§0 step 2), never committed.
   - Client ID is referenced by every `<client>.parameters.json` (§2) as
     `appClientId`.

2. **Web app auth** (`KnowledgeEngineV9-WebApp-Auth`) — used by Easy Auth on
   the App Service for end-user SSO across every client's users, whatever
   tenant they come from. Required for §6 (auth & tenant isolation). See §6
   for its exact settings (`signInAudience`, `groupMembershipClaims`,
   implicit ID token grant).

---

## §1. One-time-per-client prerequisite — SharePoint `Sites.Selected` + admin consent

Two distinct steps, both required, both outside Bicep (Graph/Entra
permissions, not Azure resources). **Skipping the second is the single most
common onboarding failure** — the Logic App's `Get_secret` action succeeds
(Key Vault works) but `Get_token` fails `Unauthorized`, because
`Sites.Selected` on one site does not by itself grant the app *any*
application-level consent on the tenant.

Get the client's `siteId` first (format `hostname,siteCollectionId,webId`).
If Graph Explorer isn't convenient, use the SharePoint REST API directly —
run these in the browser address bar while signed in to the site:
```
https://<tenant>.sharepoint.com/sites/<SiteName>/_api/site?$select=Id
https://<tenant>.sharepoint.com/sites/<SiteName>/_api/web?$select=Id
```
Full site ID = `<tenant>.sharepoint.com,<_api/site Id>,<_api/web Id>`.

### 1.1 Grant `Sites.Selected` on the site (Azure Cloud Shell)

**Do not use Graph Explorer's "Modify Permissions" panel for this** — a
recurring, account/tenant-independent bug makes it show "Permissions for
the query are missing on this tab" and never load, no matter how many
retries. Go through Azure Cloud Shell instead:

```powershell
# portal.azure.com → Cloud Shell (>_ icon) → PowerShell.
# "No storage account required" is fine for this one-off action.

Connect-MgGraph -Scopes "Sites.FullControl.All"
# → prints a URL (https://login.microsoft.com/device) + a 9-character code.
# The code expires in 120s — open the URL and enter it immediately in a new
# tab, or you'll get "Authentication timed out after 120 seconds due to
# inactivity" and have to rerun the command for a fresh code.
```

```powershell
# New-MgSitePermission can be "not recognized" in an ephemeral Cloud Shell
# (Microsoft.Graph.Sites module not preinstalled). Call the Graph API
# directly with Invoke-MgGraphRequest instead — part of
# Microsoft.Graph.Authentication, already loaded by Connect-MgGraph, no
# install needed.

$body = @{
  roles = @("read")   # "write" only if ingestion must also write to the site (rare)
  grantedToIdentities = @(
    @{
      application = @{
        id = "<INGESTION_APP_CLIENT_ID>"
        displayName = "knowledgeengine-sharepoint-ingestion"
      }
    }
  )
} | ConvertTo-Json -Depth 5

Invoke-MgGraphRequest -Method POST `
  -Uri "https://graph.microsoft.com/v1.0/sites/<SITE_ID>/permissions" `
  -Body $body -ContentType "application/json"
```

Success: JSON response with `roles: {read}` and a generated permission `id`
(worth keeping for reference, not required afterwards).

### 1.2 Admin consent (often skipped, distinct from 1.1)

Visit, **signed in as an admin of the client's own tenant**:
```
https://login.microsoftonline.com/<CLIENT_TENANT_ID>/adminconsent?client_id=<INGESTION_APP_CLIENT_ID>&redirect_uri=https://login.microsoftonline.com/common/oauth2/nativeclient
```

- `AADSTS500113: No reply address is registered for the application` → the
  app has no redirect URI. Fix once, at the app level — see §0bis.1.
- `login.microsoftonline.com/common/wrongplace` → mixed browser sessions
  (several Microsoft accounts signed in in the same tabs). Fix: open the
  consent URL in a **private/incognito window**, sign in with *only* the
  target tenant's admin account.
- The landing page shows a phishing warning ("This page isn't normally
  shown…") — expected, that technical `nativeclient` page isn't meant for
  human eyes. **Read success from the final URL**, not the page content: it
  must contain `?admin_consent=True&tenant=<TENANT_ID>`.

### Verifying the fix, if `Get_token: Unauthorized` shows up anyway

```bash
az rest --method get \
  --url "https://management.azure.com/subscriptions/<SUB_ID>/resourceGroups/<resource-group>/providers/Microsoft.Logic/workflows/logic-ingest-<client>/runs/<RUN_ID>/actions?api-version=2019-05-01" \
  --query "value[].{name:name, status:properties.status, code:properties.code}"
```
If `Get_secret` succeeded and `Get_token` shows `Unauthorized`: redo 1.2,
then retrigger the workflow.

### Creating a brand-new external tenant for a client (if they don't have one)

Not always needed — most clients bring their own tenant. If you must
provision one:

- `portal.azure.com/#create/Microsoft.AzureActiveDirectory` (portal tenant
  creation) hits a confirmed, unresolved Microsoft CAPTCHA-loop bug —
  independent of browser/network/device (reproduced on a corporate PC with
  Zscaler off, and on a phone on 4G). No CLI/PowerShell/REST alternative
  exists to create an Entra tenant.
- **Workaround that worked**: a Microsoft 365 Business Standard free trial
  (`microsoft.com/microsoft-365/business/microsoft-365-business-standard-one-month-trial`)
  verifies by phone/SMS instead of CAPTCHA, and provisions a new Entra
  tenant as a side effect. (The Microsoft 365 Developer Program was tried
  first and ruled out — it needs a Visual Studio subscription / ISV partner
  / MAICPP status / Premier Support contract.)
- That trial auto-bills after its trial period unless cancelled — note the
  date and tell whoever owns the subscription.

---

## §2. Onboard a client — ingestion (SharePoint → Blob)

1. Create `<client>.parameters.json` in `clients-local/` (git-ignored — copy
   `ingestion/example.parameters.json`, fill in `clientCode`, `siteId`,
   `containerName`, `tenantId`, `appClientId`). Leave `listQuery: ""` for a
   full ingestion, or `"?$top=1"` to smoke-test a single file first — note
   this only caps the Graph *page* size, not the total number of files
   ingested (the `Until` loop follows `@odata.nextLink` to the end
   regardless).

2. Preview:
   ```bash
   az deployment group what-if \
     --resource-group <resource-group> \
     --template-file ingestion/main.bicep \
     --parameters clients-local/<client>.parameters.json
   ```

3. Deploy:
   ```bash
   az deployment group create \
     --resource-group <resource-group> \
     --template-file ingestion/main.bicep \
     --parameters clients-local/<client>.parameters.json
   ```
   Confirm `"provisioningState": "Succeeded"` in the output.

4. The Logic App fires automatically on its recurrence trigger. If the very
   first run fails with `Forbidden` on `Get_secret`: wait 2–5 minutes (RBAC
   propagation) and re-run manually (Portal → Logic app → Overview → Run
   Trigger → Run), or trigger via REST:
   ```bash
   az rest --method post \
     --url "https://management.azure.com/subscriptions/<SUB_ID>/resourceGroups/<resource-group>/providers/Microsoft.Logic/workflows/logic-ingest-<client>/triggers/Recurrence/run?api-version=2019-05-01"
   # 202 Accepted, silent on success
   ```

5. Verify the Blob container has **every** expected document — compare
   against the real known count on the SharePoint side. A "Succeeded" run
   status only means no error occurred on what it processed, not that
   everything was processed. Listing:
   ```bash
   az storage blob list --account-name <storage-account> \
     --container-name kb-<client> --auth-mode key \
     --query "length([])"
   ```

---

## §3. Onboard a client — Azure AI Search pipeline (Blob → Index)

Once the Blob container is confirmed complete:

```powershell
cd search
./deploy.ps1 -ClientId <client>
```

Creates, per client: one datasource (`ds-<client>`) and one index
(`idx-<client>`), but **two skillsets and two indexers** projecting into
that same index (Document Intelligence migration, closed 2026-09-13 —
applies to every client, not opt-in):
- `ss-<client>-di` / `ix-<client>-di` — `DocumentIntelligenceLayoutSkill`
  for `.pdf/.docx/.xlsx/.pptx/.html/.htm/.jpg/.jpeg/.png/.bmp/.tiff/.tif`
  (keyless auth against the Foundry account). Handles tables/complex
  layouts that native cracking doesn't.
- `ss-<client>-text` / `ix-<client>-text` — the original native-cracking
  pipeline, now scoped to everything DI doesn't support: `.md/.txt/.csv/
  .json` and anything unlisted, via `excludedFileNameExtensions` mirroring
  the DI indexer's `indexedFileNameExtensions` (nothing falls between the
  two).

Both start automatically on first creation; redeploying onto an existing
indexer does **not** restart it by itself (§4).

**Migrating a client that was onboarded before this split existed** (single
`ss-<client>`/`ix-<client>`, no suffix): delete the old objects first, or
the old native-cracked content and the new DI content coexist as duplicates
in the index:
```bash
az rest --method delete --url "https://<search-service>.search.windows.net/indexes/idx-<client>?api-version=2024-07-01" --headers "api-key=$KEY"
az rest --method delete --url "https://<search-service>.search.windows.net/indexers/ix-<client>?api-version=2024-07-01" --headers "api-key=$KEY"
az rest --method delete --url "https://<search-service>.search.windows.net/skillsets/ss-<client>?api-version=2024-07-01" --headers "api-key=$KEY"
```
then run `./deploy.ps1 -ClientId <client>` as normal.

Check the result:

```bash
KEY=$(az search admin-key show --service-name <search-service> --resource-group <resource-group> --query primaryKey -o tsv)

az rest --method get \
  --url "https://<search-service>.search.windows.net/indexers/ix-<client>-di/status?api-version=2024-07-01" \
  --headers "api-key=$KEY" \
  --query "{status:status, lastStatus:lastResult.status, errorMessage:lastResult.errorMessage, itemsProcessed:lastResult.itemsProcessed, itemsFailed:lastResult.itemsFailed}"
# repeat with -text in place of -di
```

Target: `"status": "success"` on both, `itemsProcessed` summing to the
expected total across the two.

---

## §4. Re-run an existing indexer (e.g. after a smoke test, before a full run)

A `PUT` on an indexer that already exists (a plain `./deploy.ps1` redeploy)
does **not** restart execution by itself. Trigger it explicitly:

```bash
curl -s -X POST "https://<search-service>.search.windows.net/indexers/ix-<client>-text/run?api-version=2024-07-01" \
  -H "api-key: $KEY" -d "" -w "\nHTTP %{http_code}\n"
# repeat with -di for the other pipeline
```

`-d ""` is required — without it, `HTTP 411 Length Required`.
`HTTP 202` = accepted. `HTTP 409` = a run is already in progress, wait.

---

## §5. Troubleshooting — stuck or timed-out indexer

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
every future client) — already applied in `search/indexer.template.json`
and `search/indexer-di.template.json`:

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
either skillset template) — that property doesn't exist on the stable API
`2024-07-01` and returns `HTTP 400`. Only the **indexer**-level `batchSize`
works.

Redeploy with the updated config:

```powershell
./deploy.ps1 -ClientId <client>
```

**Unstick a frozen run** — there's no reliable REST cancel (`search.cancel`
returns `404` on this API, stable and preview alike):

1. Azure Portal → `<search-service>` → Indexers → click the indexer's name
   (not a history row).
2. **Reset** button at the top of the page.
3. **Run** button right after.

Confirm it's actually progressing this time (status + climbing
`itemsProcessed`), refreshing every 2–3 minutes, or via the command in §3.

---

## §6. Onboard a client — auth & tenant isolation (Jalon 5)

Every end user must resolve to exactly the client(s) they're allowed to see,
whether they come from the same Entra tenant as other clients (isolated by
security group) or their own external tenant (isolated by tenant, no group
needed). Both are the same mechanism: tenant → optionally group → client.

### 6.1 `engine.<client>.yaml` — the `access:` block

```yaml
access:
  entraTenantId: "..."   # required — the Entra tenant this client's users sign in from
  entraGroup: "..."      # present only if that tenant hosts more than one client;
                          # absent = the whole tenant has access (single-client tenant)
```

Resolution happens in `app/auth.py`, in two steps, from the
`X-MS-CLIENT-PRINCIPAL` header Easy Auth injects on every authenticated
request:
1. **Tenant** (`tid`) checked against every client's `entraTenantId` —
   unknown tenant → explicit refusal.
2. **Group** (`groups`) checked only when that client's config has
   `entraGroup` set.

Defense in depth: `orchestration/answer.py`'s `_search_request()` also adds
`filter: clientId eq '...'` to the Search query, on top of the physical
per-client index isolation (§2-§3).

### 6.2 App Registration settings (once per environment, see §0bis.2)

`KnowledgeEngineV9-WebApp-Auth`: `signInAudience=AzureADMultipleOrgs`,
`groupMembershipClaims=SecurityGroup`, and — if login fails with
`AADSTS700054: response_type 'id_token' is not enabled for the
application` — **Authentication → Implicit grant → check "ID tokens"**
(an App Registration property, not something Bicep sets).

### 6.3 Bicep settings (`infra/main.bicep` / `infra/modules/webapp.bicep`)

`easyAuthMultiTenant=true`, `easyAuthAllowedTenantIds=[...]`,
`WEBSITE_AUTH_AAD_ALLOWED_TENANTS` populated from it. The default
`openIdIssuer` (`/organizations/v2.0`) works fine with Easy Auth for
external tenants too — no need to switch to `/common/v2.0`.
Since 2026-09-30, `scripts/bootstrap-new-tenant.ps1` derives both values
(and the App Registration's `signInAudience`) from the client configs, and
`scripts/attach-external-tenant.ps1` applies them to a running deployment —
see §6.6.

⚠️ **PowerShell/Azure CLI quoting bug**: passing
`easyAuthAllowedTenantIds='["a","b"]'` as an inline CLI argument gets
mangled (quotes stripped by PowerShell → `Failed to parse string as JSON`).
Fix: use a dedicated ARM parameters file (see
`infra/deploy-tenants.local.json` for the pattern) passed via
`--parameters infra/deploy-tenants.local.json`, instead of an inline value.
Not sensitive (only tenant IDs) — doesn't need to be git-ignored.

⚠️ **Bicep alone is not enough**: `az deployment group create -f
infra/main.bicep` updates infra/app settings but does **not** redeploy
application code or `config/*.yaml` / `clients-local/*.yaml`. After editing
an `engine.<client>.yaml`, a Bicep-only deploy leaves `/healthz` reporting
the old client count. **Any change to `engine.<client>.yaml` or `app/*.py`
needs a zip-deploy (§7) in addition to any Bicep deploy the infra change
also needs.**

### 6.4 Creating a test user

```powershell
az ad user create --display-name "..." --user-principal-name <upn> --password "..." --force-change-password-next-sign-in true
# only if they need access to specific client(s):
az ad group member add --group <group-object-id> --member-id <user-object-id>
```
No code/config change needed for this — Entra administration only.

### 6.5 Verifying isolation end to end

Expected behavior to check per scenario, in a real signed-in browser
session (no CLI equivalent for the RAG-answer check):
- Multi-entity tenant, user in one group → login shows only that group's
  client(s) in the dropdown.
- External tenant, no group configured → login resolves automatically to
  that tenant's single client, no dropdown.
- User in multiple groups → dropdown shows exactly the union of their
  groups' clients.
- User in **no** group, on an allowed tenant → explicit "no client
  associated with your account" screen — not silent denial, not default
  access. If this instead grants access, `app/auth.py`'s group check is the
  first place to look.
- A guest account from an unrelated tenant (e.g. a B2B guest) may be
  blocked by *that tenant's own* Conditional Access policy — a red herring
  unrelated to `easyAuthAllowedTenantIds` or `app/auth.py`. Always test with
  an account native to the tenant under test.

### 6.6 Attach another organization's tenant to a client — `scripts/attach-external-tenant.ps1` (2026-09-30)

**Symptom** (new tenant, 2026-09-30): signing in to the app as
`YassineBABAKHOUYA@ClientForDemo.onmicrosoft.com` (another organization,
tenant `7bb131d8-9785-4b78-9f70-92d80b535162`) stops on Microsoft's sign-in
page with `AADSTS50020: User account '...' from identity provider
'https://sts.windows.net/7bb131d8-.../' does not exist in tenant
'KnowledgeEngineV9' and cannot access the application
'8f7f2a31-458f-48ea-a8d5-05e5e623527a'(KnowledgeEngineV9-WebApp-Auth) in that
tenant`.

**Root cause**: `bootstrap-new-tenant.ps1` (§12) deployed the app
single-tenant — App Registration `signInAudience=AzureADMyOrg`, Easy Auth
issuer `https://sts.windows.net/<this tenant>/v2.0` (so the sign-in page is
this tenant's own), `WEBSITE_AUTH_AAD_ALLOWED_TENANTS=<this tenant>` — and
every client config pointed to this tenant with a group. The Jalon 5
external-tenant set-up (§6.2-6.3, clientc from ClientForDemo on the first
tenant) was not carried over by the rebuild.

**Decision (2026-09-30)** — the Jalon 5 layout, kept: **client-s stays in
this deployment's tenant** (group `KE-v9-clients`), **clientc goes to the
ClientForDemo tenant** (organization 2: whole tenant = clientc, no group).
First idea was to attach client-s to ClientForDemo; dropped the same night.

**Fix** — one command on the running deployment (az logged in to this
deployment's tenant), then a one-time consent in the other organization:
```powershell
.\scripts\attach-external-tenant.ps1 -ClientId clientc -TenantId 7bb131d8-9785-4b78-9f70-92d80b535162
```
It (1) rewrites `access:` in `config/engine.clientc.yaml`:
`entraTenantId` = the other tenant, `entraGroup` removed (whole tenant =
clientc, §6.1); (2) switches the App Registration to `AzureADMultipleOrgs`;
(3) sets the Easy Auth issuer to
`https://login.microsoftonline.com/organizations/v2.0` (`az rest` GET/PUT of
`config/authsettingsV2`) and `WEBSITE_AUTH_AAD_ALLOWED_TENANTS` to this
tenant + every tenant a shipped client config points to (through a JSON file:
the comma must not cross az.cmd); (4) zip-deploys the app with the client
configs (`deploy-webapp.ps1 -ClientsLocal client-s`, §7 - this is also what
puts client-s, whose config was never shipped by the bootstrap's
`-SkipClientsLocal`, into the UI for its group); (5) prints the consent
step. Idempotent. Refuses this tenant's own ID, an unknown one
(public OpenID metadata check) and a tenant another client config already
points to (one client per whole tenant, `app/auth.py`), before changing
anything.

**Consent, once, in the other organization**: a Global Administrator of that
tenant signs in to the app (private window), ticks *Consent on behalf of your
organization*, Accept — or opens
`https://login.microsoftonline.com/<tenant id>/adminconsent?client_id=<app id>`
(whatever page it lands on afterwards, the consent is recorded). Until then a
non-admin user of that tenant gets "Need admin approval".

**Result**: every user of ClientForDemo gets clientc only (no dropdown,
§6.5); users of this tenant see the clients of their groups — clienta,
clientb, client-s — and clientc is no longer one of them (group
`KE-v9-clientc` stays in Entra, unused).

**Rebuilds keep it**: `bootstrap-new-tenant.ps1` treats a config pointing to
another tenant without a group as external and leaves it so; its `infra`
phase derives the allowed tenants, the multi-tenant issuer and the App
Registration audience from the configs (§12.2).
`-ExternalTenantClients @{ 'clientc' = '<tenant id>' }` sets one up at
rebuild time; `@{ 'clientc' = '' }` brings a client back into this tenant
(its group) on a run from `entra`. The repo's `config/engine.clientc.yaml`
carries the ClientForDemo tenant once committed, so the preflight of any
later run prints `External : clientc <- every user of tenant 7bb131d8-...`:
at a real client's tenant, drop it with `@{ 'clientc' = '' }`.

Status: written and mock-tested 2026-09-30 — config rewrite, idempotent
re-run, the three refusals; `app/auth.py` run against the rewritten configs:
checked for both client-s and clientc as the target (ClientForDemo user →
the external client only; this tenant's user in every group → the others;
unknown tenant → `[]`). Live run (2026-09-30): clientc attached to the ClientForDemo
tenant 7bb131d8-... — config, App Registration audience and Easy Auth issuer applied
on the first run; the zip deploy that follows a settings change failed once with a
Kudu 502, then "Site failed to start within 10 mins" on the re-run, and the site was
running anyway a few minutes later (cause not diagnosed). After any app-settings or
Easy Auth change, wait for the site to answer before redeploying. client-s stays in
this tenant (group KE-v9-clients). ClientForDemo consent + login test: to confirm.

---

## §7. Web app deployment (zip-deploy) — code/config changes

Any change to `app/*.py`, `orchestration/answer.py`, `app/requirements.txt`,
or a `config/*.yaml` / `clients-local/*.yaml` file needs the web app
redeployed via zip-deploy (a Bicep deploy alone does not push application
code — see §6.3). Script `deploy-webapp.ps1` (since 2026-09-24), run from
the repo root:

```powershell
.\deploy-webapp.ps1                                  # app/ orchestration/ config/ requirements.txt README.md + clients-local/engine.client-s.yaml
.\deploy-webapp.ps1 -ClientsLocal client-s,client-x  # other real clients' configs
.\deploy-webapp.ps1 -SkipClientsLocal                # no real-client config at all (demo only)
```

It builds the zip entry by entry (`ZipFileExtensions::CreateEntryFromFile`,
`/` separators — `Compress-Archive` writes `\`, unreadable by Linux App
Service), then runs `az webapp deploy --type zip`. Since 2026-09-30 it takes
from the git-ignored `clients-local/` **only** `engine.<client>.yaml` of
`-ClientsLocal` (default `client-s`) — the only file of that folder the app
reads (`orchestration/answer.py` `CONFIG_DIRS`, `app/auth.py`). Before, the
whole folder went up: `itsm-demo-credentials.csv`, eval data, parameters
files, the abandoned client-v config. The zip path is also made absolute:
.NET resolved the relative name against the process directory (which
`cd`/`Push-Location` do not change) while `az` resolved it against the
current location, so from a PowerShell window started in another folder the
zip was written in one place and looked for in another (seen in a mock run,
2026-09-30; the real runs so far, from a window opened in the repo, were not
affected).

**A "Deployment has completed successfully" message with a broken status
poll is not necessarily a failure**: `az webapp deploy`'s own status polling
can itself fail (`ConnectionResetError`) and report a misleading
`numberOfInstancesSuccessful: 0` even on a real success. Don't trust that
signal alone.

**`/healthz` returning the Azure AD login page is expected, not an
error**: Easy Auth's `globalValidation.requireAuthentication` applies to
the *entire* app, `/healthz` included, before Flask ever sees the request.
It proves nothing about deploy success or failure either way — verify with
a real authenticated browser session instead (§6.5), or a request carrying
a valid Easy Auth session.

---

## §8. Reminders

- `$KEY` does not persist across Cloud Shell sessions — always regenerate:
  ```bash
  KEY=$(az search admin-key show --service-name <search-service> --resource-group <resource-group> --query primaryKey -o tsv)
  ```
- Never paste the App Registration secret or the Search admin key in clear
  text into any committed file, anywhere — always fetched at runtime
  (`az search admin-key show`; Key Vault via the Logic App's HTTP action).
- Real `<client>.parameters.json` and non-demo `engine.<client>.yaml` files
  (real SharePoint `siteId`s, real tenant/group IDs) never go into this
  repository — see `ingestion/README.md` and `clients-local/README.md`
  (git-ignored, local only).
- **`git add`/`commit`/`push` always from Yassine's own terminal** —
  `device_bash` (the Claude device bridge) cannot complete the interactive
  Windows Git Credential Manager prompt, and even a read-only git command
  from that bridge has left a stale `.git/index.lock` on this repo before.
  A Claude session working here reads/edits files directly and hands back
  the exact git commands to run, never runs them itself.

---

## §9. Audio pipeline (Jalon 7) — metadata repair & PII/summary backfill

Context: the audio-transcription ingestion (`ingestion/audio-transcribe`)
gained PII redaction + a Problème/Résolution summary. Two follow-up
operations were needed and will recur for every future client with an
audio pipeline: (a) repairing a `transcribed=true` metadata flag wiped by a
since-fixed ingest bug, and (b) backfilling files already transcribed by the
*old* pipeline so they get PII-redacted before being served. Both are done
by clearing blob metadata via direct REST calls (not `az storage blob`), from
PowerShell 5.1 (Windows), because of the encoding pitfalls below.

### 9.0 Deploying the audio pipeline itself (for a new client)

```powershell
az deployment group create --resource-group <resource-group> \
  --template-file ingestion/audio-transcribe/main.bicep \
  --parameters clientCode=<client> speechEndpoint=<speech-endpoint> createRoleAssignments=true \
  --query "properties.provisioningState" -o tsv
```
`createRoleAssignments=true` is required on first deploy for a client — it
creates the 3 role assignments the Logic App's managed identity needs (Key
Vault Secrets User, Storage Blob Data Contributor, Storage Account
Contributor — the last one specifically for `ListServiceSas`) plus, since
the multimodal follow-up, **Search Service Contributor** on the search
service (so the Logic App can trigger `ix-<client>-text` itself at the end
of each run — no manual reindex step needed afterwards). Idempotent on
redeploy (`createRoleAssignments=false` is fine once the roles exist).

Verify a fix or new action is actually in the deployed definition before
retriggering — a local file edit is not itself a redeploy:
```bash
az resource show --ids <logicAppId> --query "properties.definition.actions.<action-name>"
```

### 9.1 Root-caused Windows PowerShell 5.1 encoding bug (read this first)

`Invoke-RestMethod` (and a direct `[xml]$x = Invoke-RestMethod ...` cast)
against Azure Storage's List Blobs / Get Metadata XML **mis-decodes the
UTF-8 response body** in Windows PowerShell 5.1 (.NET Framework, not
PowerShell 7+): the UTF-8 BOM becomes literal garbage (`ï»¿`), and any
non-ASCII character (e.g. `°`, UTF-8 bytes `C2 B0`) becomes double-mojibake
(`Â°`) as if read as Windows-1252. This is unrelated to console codepage,
`chcp 65001`, `$OutputEncoding`, or `[Console]::OutputEncoding` — those only
affect how the *console* displays text, not how `Invoke-RestMethod` decodes
the HTTP body. Symptom: blob names with accents/°/etc. come back corrupted
in PowerShell variables, and operations addressed by that corrupted name
(e.g. a `PUT ?comp=metadata`) fail with `BlobNotFound` even though the blob
exists.

**Fix** — bypass automatic decoding entirely: fetch with
`Invoke-WebRequest -UseBasicParsing`, read the raw bytes from
`.RawContentStream.ToArray()`, manually strip a leading UTF-8 BOM
(`EF BB BF`) if present, then explicitly decode with
`[System.Text.Encoding]::UTF8.GetString($bytes)` before `[xml]`-parsing the
resulting string:

```powershell
function Invoke-AzureStorageXml {
    param([string]$Uri, [hashtable]$Headers)
    $webResp = Invoke-WebRequest -Uri $Uri -Headers $Headers -Method Get -UseBasicParsing
    $bytes = $webResp.RawContentStream.ToArray()
    if ($bytes.Length -ge 3 -and $bytes[0] -eq 0xEF -and $bytes[1] -eq 0xBB -and $bytes[2] -eq 0xBF) {
        $bytes = $bytes[3..($bytes.Length - 1)]
    }
    $xmlText = [System.Text.Encoding]::UTF8.GetString($bytes)
    [xml]$xmlDoc = $xmlText
    return $xmlDoc
}
```

Use this for every List Blobs / Get Metadata call instead of
`Invoke-RestMethod`. `az storage blob list`/`show` (native `az` CLI, not
`az rest`) showed the *same class* of corruption on non-ASCII blob names in
this environment (confirmed via UTF-16 codepoint dump — U+FFFD replacement
characters present regardless of `chcp`/`$env:PYTHONUTF8`), so prefer the
REST approach above over `az storage blob` commands whenever blob names may
contain non-ASCII characters. `az account get-access-token` is still fine
(pure ASCII output) — use it to get the bearer token, then call storage
REST directly with `Authorization: Bearer <token>` + `x-ms-version` headers.

**Second, related pitfall**: don't put non-ASCII characters (`°`, accents)
literally in a `.ps1` script's own source when that script's file lacks a
UTF-8 BOM — PowerShell 5.1 parses a BOM-less script using the legacy system
codepage, so the same double-mojibake corruption happens to the *script's
own regex/string literals*, silently breaking any comparison against
correctly-decoded data (e.g. a literal `°` in a pattern matches nothing
real). Fix: use a `\uXXXX` regex escape instead of the literal character —
pure ASCII in the source file, immune to this regardless of how the file
gets saved/transferred.

**Blob metadata is always fully replaced, never merged**: a `PUT
?comp=metadata` call overwrites the *entire* metadata set with only the
`x-ms-meta-*` headers sent in that request. Always read-then-write the full
desired metadata (e.g. keep `clientid` when only touching `transcribed`).

### 9.2 Reusable scripts (this repo, `scripts/`)

- `repair-transcribed-flag.ps1 -ClientCode <client> [-WhatIf]` — restores a
  wiped `transcribed=true` flag by cross-checking each audio blob without
  the flag against the KB container for a matching `.txt`; only restores
  when the txt already exists (i.e. it really was transcribed).
- `clear-transcribed-flag.ps1 -ClientCode <client> -BlobName <name>
  [-WhatIf]` — single-file: drops the `transcribed` flag (keeps `clientid`)
  to force one specific file to be reprocessed on the next trigger.
- `backfill-pii-pipeline.ps1 -ClientCode <client> -Exclude <name>, <name>...
  [-WhatIf]` — bulk version: drops `transcribed` on **every** blob currently
  `transcribed=true` (minus `-Exclude`), to force a full reprocess with the
  current pipeline. Always target *all* `transcribed=true` blobs, not a
  filename-pattern heuristic — the "N°"-style naming convention turned out
  to cover only some evaluation batches (some older folders use bare UUID
  filenames with no call number), so a name-based filter silently missed
  ~35% of already-transcribed files. Verify completeness independently
  before trusting any filter: e.g. compare "matches my filter" against "all
  `transcribed=true`" counts and inspect the diff.

All three: `-WhatIf` first, always — confirm the printed file list/count
against your own expectation before the real run. None of these three
scripts trigger the Logic App themselves; clearing the flag only makes the
files eligible for reprocessing on the *next* trigger (Portal → Logic app →
Overview → Run Trigger → Run, same trigger method as §2.4).

### 9.3 Runtime planning for a bulk backfill

`ingestion/audio-transcribe/workflow-definition.json`'s `For_each` runs at
`"runtimeConfiguration": { "concurrency": { "repetitions": 1 } }` —
**strictly sequential**, one file at a time, deliberately (not a bug).
Observed real-world pace: ~1.5–2 minutes/file. Before triggering a bulk
backfill, multiply the `-WhatIf` file count by ~2 min to set expectations
(e.g. 131 files ≈ 3h30–4h30) and prefer triggering it when it won't block
other storage/Logic App work — the run is entirely server-side in Azure
(Consumption plan), so it keeps running regardless of whether the operator's
machine stays on or connected.

While it runs, live progress isn't reliably shown by the classic Logic Apps
designer's "For each" pager (`X of Y` shows the *total* item count, not
necessarily the current position, and doesn't always live-update while a
run is in progress). To check real progress: open the audio container in
Storage Browser, navigate into the batch's subfolder, and sort by "Last
modified" — files touched after the trigger time have been processed.

### 9.4 Known unresolved issue — do not include in a bulk backfill

Files with an identical name/GUID appearing twice (e.g. the same
`...wav`-style name duplicated across two different evaluation folders) can
get stuck in the Logic App's channel-merge steps — not fixed as of Jalon 7.
Since the `For_each` loop is sequential, one stuck file blocks everything
queued behind it. Identify duplicates by GUID before a bulk run (list all
target blob names, check for repeats ignoring the folder prefix) and pass
them to `-Exclude` until this is root-caused.

---

## §10. Video pipeline (Jalon 8) — Video Indexer account setup & API auth chain

Context: Jalon 8 scoped 2026-09-20 for client-s (transcript + OCR + topics,
new SharePoint folder on the client-s site). Research done before writing any
Bicep/Logic App — captured here per CLAUDE.md so the next session doesn't
redo it. Nothing deployed yet; this is the auth/API chain to build against.

### 10.1 Account creation — ARM-based, no Media Services needed

`Microsoft.VideoIndexer/accounts@2024-01-01`, system-assigned managed
identity, linked to the platform's existing storage account
(`storageServices.resourceId`) — must be StorageV2 general-purpose v2
(already true here). Module written: `infra/modules/videoindexer.bicep`
(self-contained: account + its own RBAC role assignment, **Storage Blob
Data Contributor** — role id `ba92f5b4-2d11-453d-a403-e96b0029c9fe` — on
the storage account for VI's managed identity).

**✅ Verified 2026-09-20** (Portal → Create a resource → "Azure AI Video
Indexer" → Région dropdown): **France Central IS supported** — full list
seen: Australia East, Brazil South, Canada Central, Central India, Central
US, East Asia, East US, East US 2, France Central, Germany West Central,
Japan East, Japan West, Korea Central, North Europe, South Central US,
Southeast Asia, Sweden Central, Switzerland North, Switzerland West, UK
South, West Central US, West Europe, West US, West US 2, West US 3. No
region/sovereignty blocker — deploy in `francecentral` like every other
resource on this platform.

Deploy standalone (same pattern as the Jalon 7 `infra/modules/roles.bicep`
direct-deploy, to avoid re-supplying Easy Auth secure params by going
through the whole `infra/main.bicep`) — `location` defaults to
`francecentral`, no need to pass it:
```powershell
az deployment group create --resource-group <resource-group> \
  --template-file infra/modules/videoindexer.bicep \
  --parameters videoIndexerAccountName=vi-knowledgeengine2-v9 storageAccountName=<storage-account> \
  --query "properties.provisioningState" -o tsv
```

### 10.2 Auth chain — ARM-created accounts use a DIFFERENT flow than classic/trial accounts

Do not follow the classic Video Indexer quickstart docs verbatim (they
describe the trial/API-key flow, `X-Subscription-Key` against
`api-portal.videoindexer.ai`) — **that flow returns
`ARM_ACCOUNT_MUST_BE_MANAGED_BY_ARM` (HTTP 400) on an ARM-created
account.** The correct chain for an ARM account:

1. **Get an ARM bearer token** (management.azure.com audience) — from the
   Logic App's managed identity (built-in HTTP action, auth type
   "Managed Identity", audience `https://management.azure.com`), same
   pattern already used for other ARM calls in this repo.
2. **Generate a Video Indexer access token** — ARM management-plane call,
   using that bearer token:
   ```
   POST https://management.azure.com/subscriptions/<sub>/resourceGroups/<rg>/providers/Microsoft.VideoIndexer/accounts/<account>/generateAccessToken?api-version=2024-01-01
   Body: { "permissionType": "Contributor", "scope": "Account" }
   → { "accessToken": "<JWT>" }
   ```
3. **Use that JWT against the data-plane API** (`api.videoindexer.ai`, a
   *different* base URL from the ARM calls above) — pass it as the
   `accessToken` query parameter, not a header, on every call below.
   - Upload: `POST https://api.videoindexer.ai/<location>/Accounts/<accountId>/Videos` —
     `videoUrl` (SAS URL to the source blob — same `ListServiceSas`
     pattern already used for Speech in `audio-transcribe`), `name`,
     `privacy`, `indexingPreset`, `language`.
   - Poll: `GET .../Videos/<videoId>/Index` until `state == "Processed"`.
   - Fetch insights: same endpoint, full JSON (transcript, ocr, topics,
     …).
   - **`<accountId>` here is the VI account's internal GUID
     (`properties.accountId` on the ARM resource), not the ARM resource
     name** — read it back from the deployment output/`az resource show`
     before hardcoding it anywhere.
   - **`<location>` in the data-plane URL is the display-name form of the
     region** (e.g. `East US`, not `eastus`) per Video Indexer's own
     convention — confirm the exact string for whichever region §10.1
     lands on.

### 10.3 Indexing preset — CORRECTED 2026-09-25 against the real API spec

The literal `indexingPreset` enum (confirmed on the live Swagger at
api-portal.videoindexer.ai, Upload Video operation) is: `Default`,
`AudioOnly`, `VideoOnly`, `Basic`, `BasicAudio`, `BasicVideo`, `Advanced`,
`AdvancedAudio`, `AdvancedVideo`. **There is no literal "Standard" value**
— the earlier note in this section (from a summarized/indirect doc fetch)
was wrong on that point. `Default` already indexes both audio AND video
(moderate depth); `Advanced` does both at greater depth. Since Topics is
inferred from transcript + OCR + faces combined (needs both modalities
analyzed, not just one), use **`Default`** for the first real test
(cheapest) and fall back to **`Advanced`** only if Topics/OCR are missing
or too sparse in the result. Individual insights can be dropped with
`excludedAI=<name>` (e.g. `excludedAI=Faces`) — full list of excludable
AIs and of `includedInsights`/`excludedInsights` (for the *retrieval*
side, `Get Video Index`) confirmed on the same Swagger.

### 10.3bis No SAS needed — `useManagedIdentityToDownloadVideo=true`

Confirmed 2026-09-25 (Upload Video parameter spec): pass the **plain**
blob URL (no SAS) as `videoUrl` plus `useManagedIdentityToDownloadVideo=
true`, and Video Indexer's own managed identity reads the blob directly
— no `ListServiceSas` action needed in the Logic App, unlike the audio
pipeline. Requires **Storage Blob Data Owner** (not just Contributor) on
the storage account for VI's identity — `infra/modules/videoindexer.bicep`
updated accordingly (was Contributor, now Owner); the already-deployed
account needs a redeploy of that same module to pick up the new role (see
10.5bis). If it fails with `STORAGE_ACCESS_DENIED` / `MANAGED_IDENTITY_MISSING`
anyway, fall back to the audio pipeline's `ListServiceSas` pattern for
`videoUrl` instead.

### 10.3ter Full auth + call sequence — confirmed against the live Swagger (2026-09-25)

1. ARM bearer token (managed identity, audience `https://management.azure.com`).
2. `POST https://management.azure.com/subscriptions/<sub>/resourceGroups/<rg>/providers/Microsoft.VideoIndexer/accounts/<account>/generateAccessToken?api-version=2024-01-01`
   body `{"permissionType":"Contributor","scope":"Account"}` → `{"accessToken":"<JWT>"}`.
3. Upload: `POST https://api.videoindexer.ai/{location}/Accounts/{accountId}/Videos?name=<name>&privacy=Private&indexingPreset=Default&language=fr-FR&videoUrl=<plain blob url>&useManagedIdentityToDownloadVideo=true&accessToken=<JWT>`
   — `{location}` is the **lowercase ARM region code** (`francecentral`,
   confirmed — not a display-name form, correcting an earlier wrong note
   in this section), `{accountId}` is the internal GUID from §10.5
   (`4175d377-f3f5-4278-a894-6038d4ce7470`), not the ARM resource name.
   Response 200 gives `id` = videoId, `state` starts at `Uploaded`.
4. Poll: `GET https://api.videoindexer.ai/{location}/Accounts/{accountId}/Videos/{videoId}/Index?accessToken=<JWT>` until `videos[0].state == "Processed"` (`Uploaded`→`Processing`→`Processed`/`Failed`).
5. Insights are already in that same poll response once `Processed`:
   `videos[0].insights.transcript[]` (`text`, `instances[].start/end`),
   `.ocr[]` (`text`, `left/top/width/height`, `instances[]`), `.topics[]`
   (`name`, `referenceType`: `VideoIndexer`/`Wikipedia`, `confidence`,
   `instances[]`). No separate "fetch insights" call needed — Upload →
   poll the same Index endpoint → done.
6. For a **private** video (our case, `privacy=Private`), the same
   `accessToken` also gates step 4/5 (scope Account/Video, permission
   Reader is enough for read-only polling — Contributor from step 2 works
   too, just broader than needed).

### 10.4 Not yet done (updated 2026-09-25 — see 10.7)
- ~~Redeploy `infra/modules/videoindexer.bicep` (RBAC upgrade Contributor→Owner)~~ done, see 10.5bis.
- ~~Run one real end-to-end test call against the single test video~~ done, see 10.7 — Default preset confirmed, real JSON shape confirmed, UTF-8 read bug found+fixed.
- Next: build `ingestion/video-index/main.bicep` + `workflow-definition.json` (clone `logic-transcribe-client-s`, pure HTTP + managed identity), SharePoint → `video-raw-client-s` ingestion, then test on 2-3 real client-s videos (French) before any bulk run.

### 10.5 Compte Video Indexer déployé (2026-09-20)

`az deployment group create --resource-group rg-knowledgeengine-v9 --template-file infra/modules/videoindexer.bicep --parameters videoIndexerAccountName=vi-knowledgeengine2-v9 storageAccountName=stknowledgeengine2v9` → `Succeeded`, depuis le terminal local de Yassine (`C:\V9\knowledgeengine-rag-platform`), pas Cloud Shell (le repo n'y est pas cloné — piège à noter : Cloud Shell persiste `$HOME` mais ne contient pas ce repo, toujours déployer un `--template-file` depuis un shell où le repo existe réellement).

`accountId` interne confirmé (2026-09-25) : `4175d377-f3f5-4278-a894-6038d4ce7470`. `principalId` : `70d44777-6e35-439b-95d7-8af6a011bb82`. Région : `francecentral`.

### 10.5bis Redéploiement RBAC (2026-09-25) — Owner au lieu de Contributor

Recherche du 2026-09-25 sur le vrai swagger (`api-portal.videoindexer.ai`) a montré que `useManagedIdentityToDownloadVideo=true` (§10.3bis, évite le SAS) exige **Storage Blob Data Owner**, pas seulement Contributor. `infra/modules/videoindexer.bicep` mis à jour ; redéployer le même module ajoute le nouveau role assignment Owner (le rôle Contributor existant restera en place aussi, Owner le rend juste redondant — sans risque, à nettoyer plus tard si besoin avec `az role assignment delete` si Yassine veut du RBAC strictement minimal) :
```powershell
az deployment group create --resource-group rg-knowledgeengine-v9 --template-file infra/modules/videoindexer.bicep --parameters videoIndexerAccountName=vi-knowledgeengine2-v9 storageAccountName=stknowledgeengine2v9 --query "properties.provisioningState" -o tsv
```

### 10.6 Test manuel isolé (2026-09-25) — bug curl.exe / PowerShell sur upload multipart

Étape 2 du plan (test isolé token → upload → poll → insights, upload direct
du fichier local pour éviter de créer un blob/container jetable — la vidéo
de test existe déjà sur SharePoint, cf. retour Yassine). Tokens ARM/VI
obtenus sans problème.

- **Symptôme** : `curl.exe -F "file=@\`"<chemin>\`";type=video/mp4" ...` échoue
  systématiquement avec `curl: (26) Failed to open/read local data from
  file/application`, y compris après avoir vérifié que le chemin local est
  correct et le fichier bien présent (confirmé via `Get-ChildItem`).
- **Cause racine** : le nom de fichier contient une virgule et des
  parenthèses (`... (1080p, h264).mp4`) — curl exige alors de mettre le nom
  entre guillemets dans la valeur du champ `-F` (`file=@"...";type=...`).
  Mais curl interprète le backslash comme caractère d'échappement *à
  l'intérieur* de ces guillemets de champ multipart — et un chemin Windows
  (`C:\Users\...`) est plein de backslashes, qui sont donc corrompus par
  ce parsing avant même que curl essaie d'ouvrir le fichier. Rien à voir
  avec l'échappement PowerShell (backtick) lui-même, qui construisait la
  bonne chaîne — le bug est dans la façon dont curl relit sa propre valeur
  de champ.
- **Fix** : renommer/copier le fichier vers un chemin sans virgule, parenthèse
  ni caractère spécial, pour ne plus avoir besoin de guillemets du tout dans
  `-F` (donc plus aucun backslash à l'intérieur d'une valeur de champ
  entre guillemets).

```powershell
Copy-Item "C:\Users\yassine.baba-kho-ext\Downloads\Microsoft 365 Basics Outlook and Teams Tutorial - Learn Skills Daily (1080p, h264).mp4" "C:\Users\yassine.baba-kho-ext\Downloads\test-video.mp4"
```

Puis `-F "file=@C:\Users\yassine.baba-kho-ext\Downloads\test-video.mp4;type=video/mp4"` sans guillemets internes.

### 10.7 Premier test réel bout en bout (2026-09-25) — succès, indexingPreset=Default confirmé suffisant

Upload direct du fichier local (méthode multipart, cf. 10.6) → poll →
`state: "Processed"` obtenu. Vidéo de test : tutoriel anglais générique
téléchargé (SharePoint), 1h25m55s (~5156s), `indexingPreset=Default`,
`language=fr-FR` (voir caveat langue ci-dessous).

**Confirmé sur données réelles** :
- `videos[0].insights.transcript` : 55 entrées (`text`, `instances[].start/end`).
- `videos[0].insights.ocr` : 583 entrées (texte incrusté, ex. dates d'écran
  `"2/22/2022"`) — très riche, comme attendu pour un tutoriel logiciel.
- `videos[0].insights.topics` : 6 entrées, ex. `Technologie` (referenceType
  `VideoIndexer`), `Logiciels`/`Entreprises`/`Marques`/`Tablettes` (Wikipedia).
  **Confirme définitivement que `indexingPreset=Default` suffit pour
  Transcript+OCR+Topics** — pas besoin d'Advanced (cohérent avec 10.3, qui
  avait déjà corrigé la fausse piste "Standard/Advanced requis").
- `videos[0].insights.keywords` : 0 (vide sur cette vidéo — pas bloquant,
  pas dans le scope Jalon 8 de toute façon).

**Bug rencontré — encodage UTF-8 corrompu à la lecture PowerShell** (même
famille que le bug UTF-8 audio, §9.1) : `Get-Content -Raw | ConvertFrom-Json`
en PowerShell 5.1 lit le fichier JSON (pourtant bien en UTF-8, écrit par
`curl.exe -o`) avec l'encodage ANSI/CP1252 par défaut faute de BOM →
mojibake sur les accents (`"Ã‰lÃ©ments de contrÃ´le graphiques"` au lieu de
`"Éléments de contrôle graphiques"`). **Fix** : lire explicitement en UTF-8
avec `[System.IO.File]::ReadAllText($path, [System.Text.Encoding]::UTF8)`
au lieu de `Get-Content -Raw`. **À appliquer aussi dans la Logic App**
(l'action HTTP d'écriture du `.txt` de sortie doit forcer l'encodage UTF-8
explicitement, ne jamais compter sur un défaut).

**Caveat langue** : vidéo de test en anglais mais indexée avec
`language=fr-FR` (choix volontaire, car les vraies vidéos client-s seront
en français) → transcript de mauvaise qualité sur ce test précis (le
modèle français transcrit phonétiquement de l'anglais, résultat
incohérent : `"The exercise files fratellis courser a looked in the."`).
**Pas un bug du pipeline** — juste un mismatch langue/contenu propre à cet
asset de test générique. À revalider avec une vraie vidéo française
client-s avant la bascule en production (mais le format JSON, le preset et
le chemin d'auth sont eux définitivement validés).

**Autre point noté** : `videos[0].durationInSeconds` et `videos[0].created`
reviennent vides dans la réponse `Index` une fois `Processed` (alors que
`created` était rempli à l'upload) — pas creusé, pas bloquant (durée
déductible de `scenes`/`videosRanges` : ici `0:00:00` → `1:25:55.350884`).

### 10.8 Logic Apps écrites (2026-09-25) — PAS ENCORE DÉPLOYÉES

**`ingestion/main.bicep` + `ingestion/workflow-definition.json` (racine)
modifiés** : ajout `videoContainerName` (défaut `video-raw-${clientCode}`)
et `videoExtensions` (mp4/mov/avi/wmv/mkv/webm/m4v). `Compose_destContainer`
route maintenant : audio → `audioContainerName`, vidéo → `videoContainerName`,
sinon → `containerName` (kb-<client>) — même pattern que l'audio en Jalon 7,
jamais de vidéo écrite directement dans kb-<client>.

**`ingestion/video-index/main.bicep` + `workflow-definition.json` (nouveau,
clone de `audio-transcribe/`) créés.** Différences volontaires vs audio :
- **Aucun secret Key Vault** : Video Indexer utilise le chaînage ARM decrit
  en 10.3ter (MSI native sur l'action HTTP, audience
  `https://management.azure.com/`, puis `generateAccessToken`), pas une clé
  d'abonnement stockée.
- Le Logic App ne télécharge JAMAIS la vidéo lui-même — pas de `Get_sas`/
  `ListServiceSas` comme pour l'audio. Il passe juste l'URL blob nue en
  `videoUrl` + `useManagedIdentityToDownloadVideo=true` ; c'est l'identité
  managée du compte Video Indexer (déjà Storage Blob Data Owner, §10.5bis)
  qui lit le blob.
- **Le JWT data-plane est régénéré à CHAQUE itération du poll**
  (`Generate_vi_token_poll`), pas une seule fois avant la boucle — confirmé
  en live (§10.6-10.7) qu'il expire pendant un traitement long. Après la
  boucle, un dernier `Generate_vi_token_final` + `Get_final_index` récupère
  les insights (transcript/ocr/topics), plutôt que de référencer la sortie
  d'une action à l'intérieur d'un `Until` déjà terminé.
- PII redaction (`ConversationalPIITask`, même ressource Foundry/Language
  que l'audio) sur le **transcript parlé uniquement** — pas de tâche
  "resolution" (spécifique aux appels support). **OCR et Topics ne sont PAS
  redigés en v1** (jugement : texte UI d'un tuto logiciel générique, risque
  jugé plus faible que la parole ; pas de dédup non plus sur l'OCR, quitte à
  avoir des lignes répétées tant qu'un élément reste affiché à l'écran) — à
  revisiter avec Yassine si besoin.
- Flag anti-re-traitement : métadonnée `x-ms-meta-videoindexed: true` sur le
  blob source, écrite seulement si tout a réussi (fail-closed, comme
  l'audio : un échec PII ne laisse jamais rien écrire).
- `runtimeConfiguration.concurrency.repetitions: 1` et `Run_search_indexer`
  toujours exécuté après la boucle (même échecs) dès la v1.
- RBAC de son identité managée : Storage Blob Data Contributor, Search
  Service Contributor, Cognitive Services User (Foundry), et **Contributor
  scopé au compte Video Indexer uniquement** (pas de rôle built-in plus fin
  pour l'action `generateAccessToken`).

**Déploiement — pas encore fait, dans l'ordre :**
```powershell
az storage container create --name video-raw-client-s --account-name stknowledgeengine2v9 --auth-mode login

az deployment group create --resource-group rg-knowledgeengine-v9 --template-file ingestion/main.bicep --parameters clients-local/client-s.parameters.json --query "properties.provisioningState" -o tsv

az deployment group create --resource-group rg-knowledgeengine-v9 --template-file ingestion/video-index/main.bicep --parameters clientCode=client-s videoIndexerAccountId=4175d377-f3f5-4278-a894-6038d4ce7470 --query "properties.provisioningState" -o tsv
```

### 10.9 Déploiement fait (2026-09-26) — bug préexistant découvert sur `logic-ingest-client-s`

Les 3 commandes de 10.8 → **Succeeded** (conteneur `video-raw-client-s` créé, `ingestion/main.bicep` redeployé avec le routage vidéo, `logic-video-index-client-s` déployé).

Déclenchement manuel de `logic-ingest-client-s` (pour faire remonter la vidéo de test vers `video-raw-client-s`) :
```powershell
az rest --method post --url "https://management.azure.com/subscriptions/<sub>/resourceGroups/rg-knowledgeengine-v9/providers/Microsoft.Logic/workflows/logic-ingest-client-s/triggers/Recurrence/run?api-version=2019-05-01"
```
→ succès silencieux (pas de corps de réponse), mais `video-raw-client-s` reste **vide** ensuite.

**Bug découvert, sans rapport avec les changements vidéo d'aujourd'hui** : `logic-ingest-client-s` échoue sur **tous ses runs quotidiens depuis le 2026-09-21**. Erreur run-level constante : `An action failed. No dependent actions succeeded.` Action-level : `Check_blob_exists` = Failed à l'intérieur du `For_each` (~250+ fichiers SharePoint), en cascade `Download_content`=Failed, `Write_blob`=Skipped.

Diagnostic en cours pour trouver le fichier exact : la pagination de `scopeRepetitions` (exécutions de la boucle `For_each`) via `az rest --url $url` échoue dès que l'URL contient un `nextLink` avec `%24skiptoken=...` — `'%24skiptoken' n'est pas reconnu...` — reproduit même depuis un script `.ps1` sauvegardé (`find_failure.ps1`), pas seulement en collant dans la console. Une fois toutes les pages malgré l'erreur affichée à chaque tour, résultat final `Failures found: 1` mais avec nom/statut vides — objet probablement mal reconstruit par la corruption de parsing. Cause probable : `az rest`/PowerShell 5.1 gère mal les URL contenant des `%24` littéraux dans certains contextes d'appel — non résolu.

**Conséquence** : l'étape 5 du plan Jalon 8 (tester 2-3 vidéos) est bloquée — aucune vidéo n'est encore arrivée dans `video-raw-client-s`, donc `logic-video-index-client-s` n'a encore rien à traiter.

**Pivot** : `find_failure2.ps1` remplace chaque appel `az rest` par un `Invoke-RestMethod` natif PowerShell avec un jeton bearer obtenu une seule fois (`az account get-access-token`), pour contourner le parsing d'URL problématique de `az rest`/CLI. Pas encore exécuté à l'heure de cette entrée.

### 10.10 CAUSE RACINE trouvee (2026-09-26) — abonnement Azure DESACTIVE pour non-paiement

> **Correction (2026-09-26, later the same day):** the banner on the
> subscription's Overview page reads *"Nous avons identifié une activité
> suspecte dans cet abonnement. Pour protéger votre compte, nous avons désactivé
> l'abonnement. Contactez le support Azure…"* — a suspicious-activity hold, not
> a non-payment disable; there is no unpaid invoice, so the "régler la facture"
> action below does not apply. Way forward: §12.

Le diagnostic `find_failure2.ps1` a bien fonctionne (contournement du bug `az rest`/`%24skiptoken` via `Invoke-RestMethod` + jeton bearer) et a isole l'iteration en echec du `For_each` : **itemIndex 385** (`scopeRepetitions/000385`), erreur `ActionFailed : An action failed. No dependent actions succeeded.` — mais en creusant plus loin (portail Azure), la vraie cause est apparue : l'**abonnement Azure `Azure subscription` (5e2f9708-6da3-4247-b96a-efa9cc50e848) est en etat `Desactive`**, et la web app (`app-knowledgeengine2-v9`) renvoie `Error 403 - This web app is stopped`.

**Ceci reexplique tout depuis le 2026-09-21** : `logic-ingest-client-s` echoue sur `Check_blob_exists` depuis cette date non pas a cause d'un bug de workflow, mais parce que l'abonnement etait deja desactive (non-paiement) — toutes les ressources (Storage, Logic Apps, Web App) sont progressivement tombees en panne d'auth/acces. La piste `%24skiptoken`/`az rest` etait une vraie limitation PowerShell/CLI mais **secondaire** face a ce blocage de fond.

**Action en cours (Yassine)** : regler la facture impayee / mettre a jour le moyen de paiement dans Portail Azure -> Couts + Facturation, pour reactiver l'abonnement. Rien d'autre (video, ingestion, ITSM) ne peut etre teste ou verifie tant que l'abonnement n'est pas reactive. A reprendre une fois la reactivation confirmee : reverifier `logic-ingest-client-s`, `logic-video-index-client-s` et l'app web depuis zero (le passage par un etat desactive peut avoir des effets de bord sur l'etat des ressources).

## §11. ITSM action module (Jalon 10) — demo identities

### 11.1 Seeding the fictional demo identities (2026-09-25 — script written, NOT yet run)

`scripts/itsm/seed-demo-identities.ps1` + `scripts/itsm/demo-identities.json` create, idempotently:
- **Entra ID (target tenant)**: 7 fictional users (`claire.dubois` manager, `amine.elidrissi` MFA reset, `sophie.martin` password reset, `karim.benali` access request, `julie.bernard` licence request, `thomas.leroy` departure, `nadia.admin` = User Administrator, the guardrail persona whose reset must be refused), 3 security groups (`SG-SP-Projets`, `SG-VPN-Users`, `SG-App-Planning`), manager links, memberships, and the directory role.
- **ServiceNow dev instance**: assignment group `KE-Automation` and matching `sys_user` records. Link key between the two systems: ServiceNow `email` = Entra `userPrincipalName`.

Run from the repo root, after `az login --tenant <target tenant>` (the script shows the target tenant, domain and ID, and asks for an explicit `YES` before any change):

```powershell
.\scripts\itsm\seed-demo-identities.ps1 -SnInstance dev123456
```

Initial passwords of newly created users go to `clients-local/itsm-demo-credentials.csv` (git-ignored). Script source is ASCII-only (see §9.1).

Not covered yet: seeding the demo tickets themselves (incidents via Table API, RITMs via the Service Catalog `order_now` API) — next step.

### 11.2 First run (2026-09-25) and 401 fix

- `az login` pitfalls on this machine: the WAM account picker opens *behind* other windows (Ctrl+C cancels it), and `--use-device-code` is **blocked** on the KnowledgeEngineV9 tenant ("Accès impossible" — device code flow blocked by Conditional Access). Fix: `az config set core.enable_broker_on_windows=false` then `az login --tenant KnowledgeEngineV9.onmicrosoft.com` (browser auth-code flow) → OK.
- Entra part: all 3 groups, 7 users, 6 memberships, 1 role assignment created without warning.
- ServiceNow part: **symptom** `(401) Non autorisé` on the first Table API call → **root cause** (most likely) the PS 5.1 `Get-Credential` dialog returns the user name as `\admin` (empty domain prefix) → **fix** the script now prompts in the console (`Read-Host`, `-AsSecureString`), strips a leading `\`, and checks auth once before writing anything. Re-running is safe (Entra objects already present are skipped).
- Second run: **symptom** re-run stops on `az.cmd : ERROR: Bad Request ... One or more added object references already exist ... 'members'` (NativeCommandError) → **root cause** in Windows PowerShell 5.1, with `$ErrorActionPreference = 'Stop'`, native stderr captured via `2>&1` is turned into a terminating error before the script can inspect `$LASTEXITCODE`, so the intended "already exists → skip" handling never ran → **fix** `Invoke-Graph` sets `ErrorActionPreference = 'Continue'` only around the `az` call and decides on the exit code.
- Third blocker: **symptom** 401 on the ServiceNow Table API as `admin` although the same password works in the browser; syslog (`syslog_list.do?sysparm_query=messageLIKEbasic`) shows `SNCRestrictBasicAuthUserAuthenticationGate: denied basic-auth API call for interactive-login user [admin] under enforce=true` → **root cause** Zurich PDIs enforce a basic-auth restriction for *interactive-login* users on API calls (`glide.authenticate.basic_auth.restriction...` enforcement = true). Allowed exceptions: users holding a role listed in `glide.authenticate.basic_auth.allowed_roles` (default `snc_basic_auth_api_access`), and web-service-access-only users (`glide.authenticate.basic_auth.allow_wsao = true`) → **fix** grant `snc_basic_auth_api_access` to `admin` **temporarily** for seeding, remove it afterwards. Do NOT disable the enforcement property. The module's own account `svc_ke_itsm` (Internal Integration User) is allowed by `allow_wsao`.
- Also check the admin password on the developer portal right before running: it can change (portal reset) and the masked field length on the portal is not the real length. The script prints the received password length to catch truncated pastes.
- Result (2026-09-25 02:37): Entra 3 groups / 7 users / memberships / role OK; ServiceNow `dev374242` group `KE-Automation` + 7 `sys_user` (email = UPN) created.
- Open issue: `Set Password` does not open on `svc_ke_itsm` (Identity type = Machine). Not solved yet.

### 11.3 Demo tickets

`scripts/itsm/seed-demo-tickets.ps1` + `scripts/itsm/demo-tickets.json`: 4 incidents (MFA reset, password reset, and two guardrail cases that must be REFUSED: privileged target `nadia.admin`, third-party request karim → claire) + 3 requests (SharePoint access, Visio licence, offboarding opened by the manager). Every ticket has `correlation_id = KE-DEMO-...` (idempotent re-runs; `-Reset` deletes them all before re-seeding — use before each rehearsal). The `expected_action` / `expected_outcome` fields in the JSON are the golden set for evaluating the module.

Requests are created as `sc_request` + `sc_req_item` directly via the Table API, **without a catalog item**: no catalog flow is attached, so closing the RITM via the Table API is not overridden. Trade-off: less realistic than an `order_now` on a real catalog item; to revisit if the demo has to show a real catalog flow.

```powershell
.\scripts\itsm\seed-demo-tickets.ps1 -SnInstance dev374242          # admin must hold snc_basic_auth_api_access
.\scripts\itsm\seed-demo-tickets.ps1 -SnInstance dev374242 -Reset   # clean queue + re-seed
```
- Bug hit on first run of `seed-demo-tickets.ps1`: **symptom** `ServiceNow user claire.dubois not found` although the user exists → **root cause** Windows PowerShell 5.1: a function returning a one-element array is unrolled to a single `[pscustomobject]`, and `[pscustomobject]` has no `.Count` in 5.1 (`$null -gt 0` is false) → **fix** wrap the call: `$r = @(Get-SnAll ...)`.
- Second blocker: **symptom** `POST incident` → 500 `Transaction cancelled: maximum execution time exceeded` → **root cause** freshly woken PDI, incident insert business rules slow on first calls; the insert was actually committed despite the 500 (INC0010002 existed on the next run) → **fix** just re-run: `correlation_id` idempotency prevents duplicates. `Invoke-Sn` now prints ServiceNow's JSON error body instead of PS 5.1's generic "(500) Erreur interne".
- Result (2026-09-25 02:49): INC0010001 (01-MFA), INC0010002 (02-PWD), INC0010003 (06-GUARD-PRIV), INC0010004 (07-GUARD-THIRDPARTY), RITM0010001 (03-ACCESS), RITM0010002 (04-LICENSE), RITM0010003 (05-OFFBOARD), all assigned to `KE-Automation`.

### 11.4 Ticket polling Logic App (step 10.1) — `itsm/poll/`

Design: Logic App Consumption, zero-connector (same pattern as `ingestion/audio-transcribe/`), every 5 min: Key Vault secret (MI) → ServiceNow Table API `incident` + `sc_req_item` (active, `assignment_group.name=KE-Automation`, basic auth as `svc_ke_itsm`) → Azure Table `itsmtickets` upsert (MI, MERGE over POST). Read-only towards ServiceNow. Chosen over an Azure Function to respect "managed services only, no custom code in the pipeline".

Prerequisite checked 2026-09-25: `svc_ke_itsm` (Identity type Machine, Internal Integration User, role `itil`) reads the `KE-Automation` queue with basic auth (curl → 7 tickets). Not affected by the Zurich interactive-user basic-auth block (`allow_wsao = true`).

1. Store the password in Key Vault without it landing in shell history:
```powershell
$s = Read-Host "svc_ke_itsm password" -AsSecureString
$p = [Runtime.InteropServices.Marshal]::PtrToStringBSTR([Runtime.InteropServices.Marshal]::SecureStringToBSTR($s))
$f = New-TemporaryFile; [IO.File]::WriteAllText($f.FullName, $p); $p = $null
az keyvault secret set --vault-name kv-knowledgeengine-v9 --name servicenow-svc-ke-itsm-password --file $f.FullName --query id -o tsv
Remove-Item $f
```
2. Deploy:
```powershell
az deployment group create --resource-group rg-knowledgeengine-v9 --template-file itsm/poll/main.bicep --parameters snInstance=dev374242
```
3. Verify: Portal → `logic-itsm-poll-itsm-demo` → Run trigger → check each action; then the table `itsmtickets` should hold 7 rows (4 INC + 3 RITM).
- 2026-09-25 03:04: secret `servicenow-svc-ke-itsm-password` created (version c10aea59…, length 100 — to confirm it matches the real svc_ke_itsm password: a 401 in `Get_incidents` would mean a bad paste). Pitfall: `az ... --query "length(value)"` breaks in PowerShell (the parentheses are mangled on the way to az.cmd) → use `$v = az ... --query value -o tsv; $v.Length; $v = $null`.
- 2026-09-25 03:04: `az deployment group create ... itsm/poll/main.bicep --parameters snInstance=dev374242` → Succeeded (table `itsmtickets`, Logic App `logic-itsm-poll-itsm-demo`, MI principal 1f214672-…, 2 role assignments). Note: deployment record name defaulted to `main` (same as infra/main.bicep) — pass `--name itsm-poll` next time to keep deployment history readable.
- 2026-09-25 03:04 first run → `Get_sn_secret` **Forbidden**: the run fired the very second the deployment finished, before the new Key Vault Secrets User assignment had propagated (expected, not a config error). 03:09 run → **Succeeded** (2.6 s).
- Verified in Storage browser: `itsmtickets` holds 7 rows (PartitionKey `itsm-demo`, RowKey INC0010001…RITM0010003); both points flagged "verify on first run" are confirmed: MERGE-over-POST upsert with OAuth works, and dot-walked fields come back as flat keys (`subjectEmail` / `openedByEmail` = `amine.elidrissi@KnowledgeEngineV9.onmicrosoft.com` on INC0010001). The 100-char Key Vault secret is correct (ServiceNow-generated password).
- Cost note: Consumption plan, 288 runs/day × ~20 actions — disable the Logic App (`az logic workflow update ... --state Disabled` or Portal → Disable) between demo periods.

### 11.5 Proposal Logic App (step 10.2) — `itsm/propose/`

Design: `logic-itsm-propose-itsm-demo`, every 5 min, picks `itsmtickets` rows without `proposalStatus`; per ticket: Graph facts (subject user, directory roles, groups, manager, licences, tenant SKU seats — read-only, managed identity) → GPT-4o (`gpt-4o` deployment, temperature 0, strict `json_schema`, closed action enum) → deterministic guards (Logic App expressions) → MERGE `proposalStatus` (`pending_review` / `refused` / `needs_human`), `proposalAction`, `proposalJson`, `proposalRationale`, `guardReasons`, `subjectPrivilegedRoles`, `managerEmail`… into the same row. Nothing is executed on Entra/ServiceNow. Guard list and statuses are documented at the top of `itsm/propose/main.bicep`. The workflow JSON is generated: edit `itsm/propose/gen_workflow.py`, run `python itsm/propose/gen_workflow.py`, redeploy.

1. Deploy (the Logic App is created **Disabled** on purpose):
```powershell
az deployment group create --name itsm-propose --resource-group rg-knowledgeengine-v9 --template-file itsm/propose/main.bicep --query properties.outputs.managedIdentityPrincipalId.value -o tsv
```
2. Grant Graph read permission to its managed identity (not possible from Bicep):
```powershell
.\scripts\itsm\grant-graph-app-roles.ps1 -PrincipalId <principalId from step 1> -Roles Directory.Read.All
```
3. Wait ~5 min (RBAC + Graph token cache), then enable: Portal → `logic-itsm-propose-itsm-demo` → Enable (or Run trigger).
4. Expected on the 7 demo tickets: INC0010001 `mfa_reset`/pending_review; INC0010002 `password_reset`/pending_review; INC0010003 refused (`target_privileged_role`); INC0010004 refused (`target_is_not_requester`, `secret_requested_for_third_party`); RITM0010001 `group_add` SG-SP-Projets/pending_review; RITM0010002 needs_human (`no_free_license_seat`, unless the tenant has a free Visio seat); RITM0010003 `offboarding`/pending_review (opened by the manager claire.dubois).
5. To re-run a proposal on a row: delete its `proposalStatus` property in Storage browser (or all proposal fields), the next run picks it up again.
- 2026-09-25 03:34: `itsm-propose` deployment Succeeded (MI principal `b5043764-193c-49f0-a7c8-6baa10968226`); 03:35 `grant-graph-app-roles.ps1 ... -Roles Directory.Read.All` → granted. Pitfall: the runbook placeholder `<principalId ...>` must be replaced — PowerShell parses `<` as a redirection operator.
- 2026-09-25 03:37 first runs (Enable + manual Run → **two overlapping runs**): Graph lookups, GPT-4o call and JSON parsing all OK; **failed** at `Compose_reasons` → `InvalidTemplate: 'createArray' expects a comma separated list of parameters. The function was invoked with no parameters` → **root cause** `createArray()` with no argument is not valid in Logic Apps expressions → **fix** use `json('[]')` for an empty array (gen_workflow.py). Also added trigger concurrency = 1 (no overlapping runs) and a `startEnabled` Bicep param so a redeploy does not disable the Logic App again. Rows were not modified by the failed runs (the MERGE never ran), so they are picked up again automatically.
- Redeploy after a workflow change (roles already granted):
```powershell
az deployment group create --name itsm-propose --resource-group rg-knowledgeengine-v9 --template-file itsm/propose/main.bicep --parameters startEnabled=true -o none
```
- 2026-09-25 03:43 run after the fix → **Succeeded** (14.8 s, 7 tickets). Result = **7/7 match the expected golden set** (`scripts/itsm/demo-tickets.json`):
```
RowKey       GuardReasons                                              ProposalAction    ProposalStatus
INC0010001                                                             mfa_reset         pending_review
INC0010002                                                             password_reset    pending_review
INC0010003   target_privileged_role                                    password_reset    refused
INC0010004   target_is_not_requester,secret_requested_for_third_party  password_reset    refused
RITM0010001                                                            group_add         pending_review
RITM0010002  no_free_license_seat                                      license_assign    needs_human
RITM0010003                                                            offboarding       pending_review
```
Quick check command: `az storage entity query --account-name stknowledgeengine2v9 --table-name itsmtickets --select RowKey proposalAction proposalStatus guardReasons --query items -o table`

### 11.6 Agent review tab (step 10.3) — `app/itsm.py`, `config/itsm.yaml`

Flask blueprint registered in `app/app.py` (`/itsm` queue, `/itsm/t/<number>` detail, `POST /itsm/t/<number>/decision`). Records the agent's decision in the `itsmtickets` row (`reviewStatus` = validated / rejected / handled_manually, `approvedParamsJson`, `reviewedById/Name`, `reviewedAtUtc`, `reviewComment`). **Executes nothing** — the Web App identity gets no new permission (it already has Storage Table Data Contributor). Server-side rules: access = tenant + Entra group from `config/itsm.yaml` (deny-by-default, `TODO-` placeholder matches nobody); only `pending_review` rows can be validated, `refused` rows never; edited params re-validated (group allowlist, offboarding steps can only be removed); ETag If-Match (no double decision); same-origin check on POST (CSRF). Local smoke test (fake table, 2026-09-25): 403 without the group, HTML in ticket text escaped, forged "validate" on a refused row ignored, POST without/with foreign Origin → 403, non-allowlisted group refused, stale ETag → second decision ignored, offboarding cannot add a non-proposed step — all OK.

1. Create the agents group and add yourself (target tenant):
```powershell
$gid = az ad group create --display-name KE-v9-itsm-agents --mail-nickname KE-v9-itsm-agents --query id -o tsv
az ad group member add --group $gid --member-id (az ad signed-in-user show --query id -o tsv)
$gid
```
2. Put that id in `config/itsm.yaml` → `access.entraGroup`.
3. Deploy the web app: `.\deploy-webapp.ps1`
4. Sign out / sign in on the web app (the `groups` claim is only refreshed at sign-in), then open `/itsm`.
- 2026-09-25 03:54: group `KE-v9-itsm-agents` created (`1bcf7b37-d2fd-4f5c-968c-906ddafac988`), Yassine added, id written to `config/itsm.yaml`.
- 2026-09-25 03:55 `deploy-webapp.ps1` → **site failed to start** (10 min timeout). `az webapp log startup show -n app-knowledgeengine2-v9 -g rg-knowledgeengine-v9` (the fastest way to get the real traceback) → `NameError: name '_clean_excerpt' is not defined` at `app/app.py` import: `app.jinja_env.filters["clean_excerpt"] = _clean_excerpt` was registered BEFORE the function definition. This line came from **pre-existing uncommitted edits in the working tree** (app.py was already "MM" in `git status` before Jalon 10 touched it), not from the ITSM change — but any deploy of the working tree would have hit it. **Fix**: registration moved right after `def _clean_excerpt`. Verified by importing the REAL `app/app.py` locally with Azure clients stubbed (import OK, `/healthz` 200, `/` renders, ITSM link only shown when allowed). Lesson: before `deploy-webapp.ps1`, run an import smoke test of `app/app.py`, not only unit tests of the new module.
- 2026-09-25 04:12 redeploy → startup probe OK (the CLI kept polling "Starting the site" while the Easy Auth container started — ignore it and check `az webapp log startup show` instead). After sign-out/sign-in, `/itsm` works live for a member of `KE-v9-itsm-agents`: queues À valider 4 / Traitement humain 1 / Refusés 2, matching the proposal golden set.
- 2026-09-25 04:24 live test "Valider" on RITM0010001 → **"Le ticket a été modifié entre-temps"** every time → **root cause** the form carried the row ETag, but `itsm/poll` MERGEs every row every 5 min (`lastSeenUtc`), so the ETag changes constantly and any page open for a few minutes is "stale" → **fix** (`app/itsm.py`): the form now carries the proposal version (`proposedAtUtc`); the POST re-reads the row, checks the proposal is unchanged and undecided, and updates with If-Match on the ETag read in that same request (retried up to 3× if a poller MERGE lands in between). Double decisions stay impossible. Smoke test updated (poller churn simulated, stale proposal refused). Also renamed "Concerné" → "Appelant / Bénéficiaire (ServiceNow)": it is the ServiceNow caller, not necessarily the targeted account (INC0010004: caller Karim, target Claire). Requires a web app redeploy.
- 2026-09-25 04:33 redeploy OK (`RuntimeSuccessful`); live test: RITM0010001 validated → "Validé — en attente d'exécution" by Yassine, approved params `group_add` / `SG-SP-Projets`. **Step 10.3 validated live.** Cosmetic follow-up (next deploy): status line shows the review status once decided, approved params rendered in French instead of a dict.

### 11.7 Executor Logic App (step 10.4a) — `itsm/execute/`

Design: `logic-itsm-execute-itsm-demo`, every 2 min, trigger concurrency 1. Picks rows `reviewStatus = validated` without `executionStatus`; claims each row (MERGE with If-Match), **re-checks the guards against Entra ID at execution time**, executes only `approvedParamsJson` (the agent's decision, never raw LLM output): `group_add` (allowlisted group), `offboarding` (disable / revoke sessions / remove static groups — dynamic groups skipped). ServiceNow: RITM closed (`state=3`) + work note on full success, work note only otherwise. Writes `executionStatus` (success / partial / blocked / dry_run / error) + `executionLog`. **password_reset / mfa_reset are NOT executed yet (10.4b: secret delivery to the validating agent) → `blocked: action_not_implemented`.** Separate managed identity: the only component with Graph write permissions. `dryRun=true` by default (no write to Entra/ServiceNow, plan logged). Workflow JSON generated by `itsm/execute/gen_workflow.py`. The web app tab shows the execution result.

1. Deploy (Disabled, dryRun):
```powershell
az deployment group create --name itsm-execute --resource-group rg-knowledgeengine-v9 --template-file itsm/execute/main.bicep --query properties.outputs.managedIdentityPrincipalId.value -o tsv
```
2. Graph application permissions for its identity:
```powershell
.\scripts\itsm\grant-graph-app-roles.ps1 -PrincipalId <id> -Roles Directory.Read.All,GroupMember.ReadWrite.All,User.EnableDisableAccount.All,User.RevokeSessions.All
```
3. Redeploy the web app (execution status display): `.\deploy-webapp.ps1`
4. Enable + Run → validated rows get `dry_run` with the planned actions. Check in `/itsm` (Décidés).
5. Real run: rows left in `dry_run` are picked up again automatically once `dryRun=false` (no manual reset), so just redeploy:
```powershell
az deployment group create --name itsm-execute --resource-group rg-knowledgeengine-v9 --template-file itsm/execute/main.bicep --parameters dryRun=false startEnabled=true createRoleAssignments=false -o none
```
6. A row ended `blocked` (e.g. action_not_implemented) is never retried automatically: clear its `executionStatus` to replay it once the cause is fixed.
- 2026-09-25 04:45 first `itsm-execute` deployment → **InvalidTemplate** `Unable to parse template language expression 'odata.id'` in `Add_member` → **root cause** in a Logic App action body, a JSON **key** starting with `@` is evaluated as an expression → **fix** `"@@odata.id"` (escaped `@`) in gen_workflow.py. (The poll/propose workflows only had `@odata.type` inside expression strings, which is fine.)
- 2026-09-25 04:46 `itsm-execute` deployment Succeeded — executor MI principal `4c2b9713-e0e4-4ac3-bed5-e5ff67b24c7e` (Disabled, dryRun=true).
- 2026-09-25 04:47: Graph app roles granted to the executor MI (Directory.Read.All, GroupMember.ReadWrite.All, User.EnableDisableAccount.All, User.RevokeSessions.All); web app redeployed (execution status display) → `Site started successfully` in 125 s.
- 2026-09-25 04:53 first executor run (dryRun) → Succeeded; RITM0010001 → `dry_run` shown in `/itsm` (claim + Graph re-check OK). Executor changed so `dry_run` rows are re-executed for real when `dryRun=false`.
- 2026-09-25 04:57 **first REAL execution, end to end — Step 10.4a validated live** (dryRun=false): RITM0010001 → `success`, `✓ group_add SG-SP-Projets (HTTP 204)`; Entra: Karim Benali is now a member of `SG-SP-Projets`; ServiceNow: RITM0010001 `Closed Complete` (was Open) by *KnowledgeEngine ITSM Module* (svc_ke_itsm) with work note `[KnowledgeEngine] success - action group_add validee par Yassine BABAKHOUYA (...). group_add OK SG-SP-Projets (HTTP 204)`. Known cosmetic gaps: RITM `Stage` stays "Assess or Scope Task" and parent REQ stays open (no catalog flow on Table-API-created requests); web app status line now shows the execution status once executed (next deploy).
- 2026-09-25 05:07 **gap found before the offboarding test** (RITM0010003 validated with steps disable_account, remove_groups, remove_licenses): (1) `remove_licenses` could be proposed and approved but the executor had no branch for it — it would have been silently skipped while the run reported `success`; (2) `revoke_sessions` was not proposed (the ticket did not spell it out), so a disabled account would have kept its already-issued tokens. **Fix** (gen_workflow.py): `remove_licenses` implemented (`GET licenseDetails` → `POST assignLicense removeLicenses`, "aucune licence" logged when none); `disable_account` now always also revokes sessions. Needs the extra Graph app role `LicenseAssignment.ReadWrite.All` on the executor identity + executor redeploy.
- 2026-09-25 05:07 RITM0010003 (offboarding thomas.leroy, requested by his manager claire.dubois) executed live by the executor version deployed BEFORE the fix above: `disable_account` OK (204), removed from KnowledgeEngineV9 (M365 group), SG-SP-Projets, SG-VPN-Users, SG-App-Planning (204 each), RITM `Closed Complete` + work note. As predicted: no `revoke_sessions` step and `remove_licenses` silently skipped although reported `success` — confirms the gap; harmless here (demo user never signed in, no licence), fixed for the next runs.
- 2026-09-25 05:10: `LicenseAssignment.ReadWrite.All` granted to the executor MI; executor redeployed with the offboarding fix (dryRun=false, Enabled).

### 11.8 Replaying the demo — `scripts/itsm/reset-demo.ps1`

Re-enables the demo users, restores the demo group memberships from `demo-identities.json` (only demo users / demo groups are touched), reopens the 7 ServiceNow demo tickets **with `svc_ke_itsm`** (password read from Key Vault → no admin basic-auth exception needed; same ticket numbers, a work note records the reset), and empties the `itsm-demo` partition of `itsmtickets`. The poll then propose Logic Apps rebuild the queue within ~10 min. The M365 group `KnowledgeEngineV9` removed by an offboarding run is not a demo group — re-add manually if wanted. To verify on first use: a reopened RITM (`state=1`) comes back `active=true` (the poller only imports active tickets).
```powershell
.\scripts\itsm\reset-demo.ps1
```
- 2026-09-25 05:16 first run of `reset-demo.ps1` → OK: thomas.leroy re-enabled + 3 demo groups restored, karim.benali removed from SG-SP-Projets, RITM0010001/0010003 reopened (state 3 → 1), 7 table rows deleted. Verified: the reopened RITMs come back **active** (poller re-imported all 7 tickets, visible in /itsm and reopened in ServiceNow). Only thomas.leroy's membership of the M365 group `KnowledgeEngineV9` is not restored (not a demo group).
- 2026-09-25 05:23 replay after reset, **offboarding with the fixed executor validated live**: RITM0010003 → `success`, work note `disable_account OK 204 | revoke_sessions OK 200 | remove_group OK ×4 (204) | remove_licenses OK aucune licence attribuee`; Entra: Thomas Leroy disabled, "N'est pas membre d'un groupe"; ServiceNow: Closed Complete. Full demo cycle (reset → poll → propose → agent validation → execution → ServiceNow closure) proven replayable.

### 11.9 Step 10.4b — password_reset / mfa_reset with one-time secret delivery

Design: the executor (`itsm/execute/gen_workflow.py`) now implements
- `password_reset`: a temporary password is generated **directly into** the dedicated delivery vault `kv-ke2-itsm-delivery` (PUT, secure inputs), read back (secure outputs), applied with `PATCH /users/{id}` `passwordProfile` + `forceChangePasswordNextSignIn`, then sessions revoked;
- `mfa_reset`: Microsoft Authenticator registrations deleted, one-time **Temporary Access Pass** (60 min) created (secure outputs) and stored in the delivery vault (secure inputs), sessions revoked;
- incidents are **resolved** in ServiceNow (`state=6`, `close_code` = param `incidentCloseCode` default "Solution provided" — verify it is a valid Zurich choice on first run, `close_notes`, work note). No secret ever goes to ServiceNow, the table, run history or logs; the row only gets `secretRef` (vault secret name) + `secretKind`.
- Web app (`POST /itsm/t/<n>/reveal`): only the agent who validated the ticket (`reviewedById` = own `oid`) sees a one-time button; the row is claimed first (If-Match), the secret is read from the delivery vault, **deleted**, and shown once on a `Cache-Control: no-store` page telling the agent to hand it over through a verified channel. The delivery vault holds nothing else → the web app identity (Key Vault Secrets Officer on THAT vault only) never reaches the main vault. Local smoke test OK: button only for the validator, other agent → refused, no Origin → 403, reveal → value shown + GET/DELETE on the vault + no-store, second reveal refused, secret never written to the row.
- Guardrails for these actions: our precheck (no reset on an account holding any Entra directory role) **plus** the executor identity only gets the **Helpdesk Administrator** directory role, which cannot reset administrators anyway.

Deploy / setup:
```powershell
az deployment group create --name itsm-execute --resource-group rg-knowledgeengine-v9 --template-file itsm/execute/main.bicep --parameters dryRun=false startEnabled=true createRoleAssignments=false -o none
.\scripts\itsm\setup-secret-actions.ps1 -PrincipalId 4c2b9713-e0e4-4ac3-bed5-e5ff67b24c7e
.\deploy-webapp.ps1
```
`setup-secret-actions.ps1`: Graph app roles `UserAuthenticationMethod.ReadWrite.All` + `User-PasswordProfile.ReadWrite.All`, directory role Helpdesk Administrator on the executor identity, Temporary Access Pass policy enabled for all users. Rows previously ended `blocked: action_not_implemented` must be reset (`reset-demo.ps1`) to be replayed.
- 2026-09-25 05:32: executor redeployed (delivery vault + roles); `setup-secret-actions.ps1` → app roles UserAuthenticationMethod.ReadWrite.All + User-PasswordProfile.ReadWrite.All granted, Helpdesk Administrator assigned; **TAP policy step → Forbidden**: the az CLI delegated token has no `Policy.ReadWrite.AuthenticationMethod` → **fix** enable it in the portal (Entra admin center → Protection → Authentication methods → Policies → Temporary Access Pass → Enable, target All users → Save); the script now warns instead of failing. Pitfall: the `throw` aborted the whole pasted multi-line block, so `deploy-webapp.ps1` (third line) did not run.
- 2026-09-25 05:41 **password_reset validated live** (INC0010002, sophie.martin): executor `success` — `password_reset ... HTTP 204`, sessions revoked; one-time reveal used by the validating agent at 03:42 UTC, row shows "Mot de passe temporaire remis par Yassine BABAKHOUYA ... Supprimé du coffre". Helpdesk Administrator + User-PasswordProfile.ReadWrite.All effective within ~10 min of granting. To confirm in ServiceNow: INC0010002 Resolved with close code accepted.
- 2026-09-25 05:45 **bug on the first live reveal** (INC0010002): the row was marked "remis" but no password was displayed → **root cause** the reveal route claimed the row (secretRevealedAtUtc) BEFORE reading the vault, so a vault read failure burnt the one-time reveal silently (the page only showed "remis ... Supprimé du coffre"). **Fix** (`app/itsm.py`): read the vault first, then claim (If-Match), then delete + show; a failure before the claim now shows the HTTP status and stays retryable. Smoke test added (vault 403 → nothing burnt, no delete). Underlying vault read failure still to identify from the new message after redeploy. Recovery for a burnt row: Storage browser → edit the row → delete `secretRevealedAtUtc` and `secretRevealedByName` (the secret stays in the delivery vault until its 1 h expiry).
- 2026-09-25 05:52 retry of the INC0010002 reveal after the fix → "Code introuvable (HTTP 404)": the secret was already gone from the delivery vault, i.e. the first click (old code) had read AND deleted it — the one-time page was consumed without the agent seeing the value. Decision: replay via `reset-demo.ps1`. Executor hardened before the replay: `mfa_reset` now deletes any existing Temporary Access Pass before creating a new one (only one TAP per user — a replay within the hour would otherwise fail).
- 2026-09-25 06:00 replay after executor redeploy + `reset-demo.ps1` → **step 10.4b validated live** (per Yassine: "c'est tout bon"): INC0010001 `mfa_reset` (old TAP cleared, one-time TAP 60 min) and INC0010002 `password_reset` executed, one-time reveal displayed to the validating agent and deleted from the delivery vault. **Jalon 10 functional scope complete**: poll → GPT-4o proposal + deterministic guards → agent review → execution (group_add, offboarding, password_reset, mfa_reset) → ServiceNow closure/resolution, demo replayable with `reset-demo.ps1`.

### 11.10 Evaluation runs logged to Azure AI Foundry (2026-09-25)

`eval/evaluate_rag.py` now passes `azure_ai_project` (project endpoint `https://aif-knowledgeengine2-v9.services.ai.azure.com/api/projects/proj-knowledgeengine2-v9`) + `evaluation_name` to `evaluate()`, so each run of a **synthetic** client (clienta/b/c) appears in the Foundry portal, Evaluation tab, with per-question scores. Real clients (client-s) are never uploaded — their results stay in `clients-local/` only. Auth = the signed-in `az` identity (DefaultAzureCredential), no key. New flags: `--client <id>` (repeatable, run a subset — fewer gpt-4o calls, less 429 risk) and `--no-upload` (local only). `eval_summary.json` is now merged per client instead of overwritten, so a `--client` run keeps the other clients' scores.

```powershell
py -X utf8 eval\evaluate_rag.py --client clienta
```
Not yet run live at time of writing — if the upload fails with 403, the signed-in user needs the **Azure AI User** role on the Foundry project.

### 10.5ter RBAC Owner redéployé (2026-09-25) — confirmé

Redéploiement de `infra/modules/videoindexer.bicep` (rôle Storage Blob Data Owner) → `Succeeded`. Prochaine étape : test manuel réel (upload → poll) sur la vidéo de test, via un blob direct plutôt que SharePoint (plus simple pour un test isolé) — voir 10.6.

---

## §12. Rebuild in a new tenant — `scripts/bootstrap-new-tenant.ps1`

**Context (2026-09-26):** the Azure subscription of tenant KnowledgeEngineV9 was
disabled (suspicious-activity hold, see the §10.10 correction). **Update
(2026-09-26, same day):** the whole tenant will be deactivated on 2026-09-30
(no further payment). Decision: move to a brand-new tenant + subscription
entirely. Nothing is copied from the old subscription: indexes, blobs, tables,
Logic Apps and the web app are all regenerated from this repo.

**Scope: four clients — clienta, clientb, clientc (synthetic demo) and
client-s (the real [CLIENT-PROD]/DXC client). client-v ([CLIENT-PARENT]) is abandoned and is
never referenced by the script or this section.**

**First real run: 2026-09-26, tenant KnowledgeEngineV9655.onmicrosoft.com.**
Foundation, the 4 search pipelines (8 indexers, 0 failures, synonym map
regenerated) and the web app are deployed. Four problems were hit and fixed on
the way, all now in the repo (12.4): the Foundry write race and the anti-abuse
block it triggered, the cold SCM site of the new Function App, Search's missing
Cognitive Services User role, the missing `kb-client-s` container. client-s
runs with an empty index until its data is loaded (12.3).

### 12.1 What changed in the repo for this

- Global-name prefix `knowledgeengine2` → `knowledgeengine3` in the 27 files
  that hard-code it (the old names stay reserved while the old subscription
  exists; Key Vault names longer, through soft delete). Key Vaults:
  `kv-knowledgeengine3-v9`, `kv-ke3-itsm-delivery`. Resource group name
  unchanged. This runbook's historical entries keep the old names on purpose.
- `infra/main.bicep` exposes `generationCapacity` / `enrichCapacity` /
  `embeddingCapacity` so a new subscription's model quota can be matched
  instead of failing the deploy.
- `deploy-webapp.ps1 -SkipClientsLocal` leaves `clients-local/` (real client
  configs) out of the package. Since 2026-09-30 the package only ever takes
  `clients-local/engine.<client>.yaml` of `-ClientsLocal` from that folder
  (§7), and the bootstrap's `webapp` phase ships the configs of its real
  clients (client-s) instead of none.
- 2026-09-30: external-tenant clients (§6.6) — `-ExternalTenantClients`, kept
  on later runs; Easy Auth's allowed tenants / multi-tenant issuer and the App
  Registration audience are derived from the client configs.
- `scripts/bootstrap-new-tenant.ps1`, below — now covers 4 clients and has
  opt-in phases for audio / video / SharePoint ingestion.
- `infra/main.bicep` has `deployFoundry` (default true): the script passes
  false once the Foundry account and its 3 model deployments exist and are
  Succeeded, so re-runs never write to the Cognitive Services account again
  (`-ForceFoundry` overrides). `modules/foundry.bicep`: `gpt-4o` now
  `dependsOn` the project (see 12.4).

### 12.2 Run it — fast path (foundation + all 4 clients, synthetic-style upload)

Prerequisites: a new tenant with an Azure subscription, `az` logged in with a
Global Administrator of that tenant who is Owner of the subscription, and — if
you have one — a local export of client-s's real KB dropped at `kb/client-s/`
(same shape as `kb/clienta`, etc.). Without that export, client-s still gets
its group, its Entra config and its (empty) search index; content is added
later with `-From ingestion` or by dropping files into `kb/client-s` and
re-running `-From data`.

```powershell
az login --tenant <new-tenant-id>
az account set --subscription <subscription-id>
cd C:\V9\knowledgeengine-rag-platform
.\scripts\bootstrap-new-tenant.ps1
```

It prints the tenant/subscription and asks for confirmation, then runs the
phases below; after fixing a failure, resume with `-From <phase>`:

| Phase | What it does |
|---|---|
| `prereqs` | Resource providers, RG, your data-plane roles (Storage Blob/Table Data Contributor, Search Index Data Contributor, Cognitive Services OpenAI User, Key Vault Secrets Officer; Azure AI User optional). |
| `entra` | App Registration `KnowledgeEngineV9-WebApp-Auth`; native demo user `ke-demo@<initial domain>`; groups `KE-v9-clienta/b/c` (one each) and `KE-v9-clients` (shared, client-s) with the demo user and you as members; rewrites `access:` in `config/engine.client{a,b,c,-s}.yaml`. An external-tenant client (§6.6: `-ExternalTenantClients`, or a config already pointing to another tenant without a group) gets that tenant and no group instead — no KE-v9-* group for it. |
| `infra` | Fits model capacities to quota, new Easy Auth secret, deploys `infra/main.bicep`, aligns the redirect URI. Easy Auth sign-in tenants = this tenant + every tenant a shipped client config points to; more than one = multi-tenant (`/organizations` issuer, App Registration `AzureADMultipleOrgs`), else single-tenant. |
| `function` | Deploys the enrichment Function (vendored zip if it matches `enrichment/` byte for byte, else source + remote build). |
| `data` | Uploads `kb/client{a,b,c}` (+ `kb/clientc-multiformat`) and, if present, `kb/client-s` — all with metadata `clientid`. No `kb/client-s`: skipped with a warning, not a failure. |
| `search` | `search/deploy.ps1` per client (now 4, so 8 indexers), then `search/generate-synonyms.ps1`. |
| `ingestion` | Key Vault `kv-knowledgeengine3-v9`; app `knowledgeengine-sharepoint-ingestion` (multi-tenant, nativeclient redirect URI, Graph `Sites.Selected` consented, secret in the vault as `ingestion-secret-v2`); a **new SharePoint site per client of `-SharePointClients`** (default client-s: team site of the private Microsoft 365 group `KE <client> KB`); read access for the app on it (a temporary app holding `Sites.FullControl.All` does the grant, then is deleted); containers `kb-`/`audio-raw-`/`video-raw-<client>`; `logic-ingest-<client>`; updates `clients-local/<client>.parameters.json`. |
| `audio` | `speech-key` = key1 of the Foundry account (Speech runs on it, as on the first tenant - no new Cognitive Services account); `logic-transcribe-<client>` for `-MediaClients`. |
| `video` | Registers `Microsoft.VideoIndexer`, deploys `infra/modules/videoindexer.bicep`, reads the account's internal GUID (`properties.accountId`), `logic-video-index-<client>`. |
| `itsm` | Asks the `svc_ke_itsm` password once (checked against ServiceNow, stored in the vault); `seed-demo-identities.ps1` on this tenant's domain (existing ServiceNow callers get their email re-pointed to the new UPN); `logic-itsm-poll/propose/execute-itsm-demo` (executor Enabled, `dryRun=false`) + their Graph permissions (`grant-graph-app-roles.ps1`, `setup-secret-actions.ps1`); group `KE-v9-itsm-agents` + `config/itsm.yaml`; `reset-demo.ps1` (reopens the demo tickets). Interactive: ServiceNow admin credentials, two `YES` confirmations. |
| `webapp` | `deploy-webapp.ps1 -ClientsLocal <real clients of -Clients>` (client-s): only their `clients-local/engine.<client>.yaml` goes up with the code (§7); `-SkipClientsLocal` when there is none. The final summary prints the admin consent link of each external tenant. |

Output: app URL, demo user, its password printed once at the end (kept out of
the transcript log, git-ignored). Commit the rewritten
`config/engine.client*.yaml` afterwards. The end-of-run summary lists exactly
what was skipped (client-s data source, audio, video, ITSM).

On a real client's tenant, add `-SkipItsm`: the `itsm` phase seeds the 7
fictional demo users and groups into the directory. Its prompts name the
target tenant (domain and ID) and need `YES`; any other answer stops the run
before `webapp`.

### 12.3 client-s data — new SharePoint site (decided 2026-09-26)

The first tenant's SharePoint sites are abandoned. client-s now lives on the
site Yassine created on this tenant, `https://knowledgeenginev9655.sharepoint.com/sites/ClientS`
(Documents library: `Kbs/`, `Audio/2025/...`, `Video/`):
```powershell
.\scripts\bootstrap-new-tenant.ps1 -From ingestion -SharePointSiteUrls @{ 'client-s' = 'https://knowledgeenginev9655.sharepoint.com/sites/ClientS' }
```
Without `-SharePointSiteUrls`, the `ingestion` phase creates a site itself (team
site of a private Microsoft 365 group `KE <client> KB`). Either way it prints the
site URL at the end of the run. Then:

1. Put client-s's documents, audio (`.wav`) and video files in the site's
   **Documents** library (sub-folders are fine; the Logic App keeps the relative
   path in the blob name and routes audio/video to their raw containers).
2. Sync now instead of waiting for the daily run:
   ```powershell
   .\scripts\sync-client.ps1 -ClientId client-s            # SharePoint -> Blob, then both indexers
   .\scripts\sync-client.ps1 -ClientId client-s -WithMedia # + starts the audio / video pipelines
   ```
   `sync-client.ps1` fires `logic-ingest-<client>`, waits for that run, re-runs
   `ix-<client>-di` / `ix-<client>-text` and prints their counts. The audio and
   video Logic Apps re-index the client themselves when they finish.

A local export dropped in `kb/client-s/` (then `-From data`) still works for a
one-off load without SharePoint.

### 12.4 Known risks on a fresh subscription

- **Foundry RequestConflict, then Azure anti-abuse block (hit 2026-09-26 on
  the first real run).** `modules/foundry.bicep` created the project and the
  `gpt-4o` deployment in parallel on the same account; the account accepts one
  write at a time, so every run failed with `RequestConflict` ("Another
  operation is in progress on the resource .../accounts/aif-...") — not
  transient. The models were created anyway; the project was not. Six re-runs
  in 30 min, each re-PUTting the account, then got the preflight error
  `InvalidTemplateDeployment` / `715-123420` ("unusual activity for your
  account") from Microsoft.CognitiveServices. The subscription itself stayed
  Active. Fixed by serializing (`gpt-4o` dependsOn project) and by
  `deployFoundry=false` on re-runs (12.1). If 715-123420 shows up with the
  account not yet complete: do not loop — wait hours, retry once, then open an
  Azure support ticket. The missing project only affects
  `eval/evaluate_rag.py` (eval runs logged to Foundry); add it later with
  `-From infra -ForceFoundry`.
- **Search skillset: "Unable to connect to AI Services using managed
  identity" (hit 2026-09-26).** `ss-<client>-di` bills Document Intelligence
  Layout to the Foundry account by identity (`AIServicesByIdentity`), which
  needs Search's identity to hold **Cognitive Services User** on the account.
  §0 always said `roles.bicep` granted it, but the file only had Cognitive
  Services OpenAI User: on the first tenant it had been granted by hand. Fixed
  in `roles.bicep` (`searchToFoundryCsUser`); `search/deploy.ps1` retries that
  exact error for up to 10 min, since a new role takes minutes to reach Search.
- **Model version**: `gpt-4o` 2024-11-20 is at the *Legacy* stage (retirement
  2027-04-14, replacement gpt-5.1). A new subscription can still deploy it, but
  not once it moves to *Deprecated* — "existing customer" is decided per
  subscription. Deploy early (phase `infra`).
- **Quotas**: gpt-4o and embedding capacities are lowered automatically to the
  free quota. The App Service `B1` plan can fail on some new subscriptions with
  `Current Limit (Basic VMs): 0` — request quota or change region.
- **Web app first start**: the Oryx build takes several minutes, and
  `az webapp deploy` can report a failed status poll on a real success (§7).
- **Video Indexer account id**: the `video` phase reads the internal GUID
  (`properties.accountId`) with `az resource show --resource-type
  Microsoft.VideoIndexer/accounts`, not the ARM id. Verified on the first real
  run (2026-09-26).
- **First run of `logic-ingest-<client>` failed, and nothing re-ran the
  document indexer (hit 2026-09-26, fixed 2026-09-29).** A Recurrence trigger
  without start time fires as soon as the Logic App is created, before its Key
  Vault Secrets User role is active: `Get_secret` 403 "Caller is not
  authorized", nothing copied, next run 24 h later. And `ix-<client>-di` was
  started by no Logic App at all (only `ix-<client>-text` was, at the end of
  the audio / video runs). Now: the three `main.bicep` give the trigger an
  explicit first run (`firstRunUtc`: ingestion 1 h after its deployment,
  audio / video 1 h 30, so they find the copied files), then daily at that
  time; both indexers run every hour (`schedule` in the indexer templates),
  which also resumes a Document Intelligence run stopped by the 2 h limit, so
  the audio / video Logic Apps no longer end with `Run_search_indexer`. To do a
  cycle right away: `.\scripts\sync-client.ps1 -ClientId <client> -WithMedia`.
  Existing indexers get the schedule by re-running `search/deploy.ps1` per
  client.
- **Files over 100 MB (hit 2026-09-27, fixed: the 1h25 test video was copied on the next run).** The Logic App HTTP action
  buffers at most 104857600 bytes, so `Download_content` failed on the 1h25
  test video (`Cannot write more bytes to the buffer than the configured
  maximum buffer size`). `ingestion/workflow-definition.json` now copies files
  over 100 MB server side by 100 MiB blocks (`Put Block From URL` from the
  pre-authenticated SharePoint download URL, then `Put Block List`); nothing
  transits through the Logic App. Smaller files keep the download + write path.
  Redeploy one client's ingestion Logic App only:
  `az deployment group create --resource-group rg-knowledgeengine-v9 --name
  ingestion-<client> --template-file ingestion/main.bicep --parameters
  "@clients-local/<client>.parameters.json" -o none`.
- **Every ingestion run re-copied every file (found 2026-09-27, fixed).**
  `Check_blob_exists` (HEAD on the destination blob) had no `x-ms-version`
  header; Storage refuses a managed identity token without it, so the HEAD
  never answered 200, every file looked missing and was downloaded and
  rewritten on every run. Rewriting a blob drops its metadata, so the
  `transcribed` / `videoindexed` flags were lost (all audio re-transcribed)
  and every document got a new timestamp (all re-indexed by DI + enrichment):
  paid work redone every day. Seen after a re-run: `audio-raw` went from 118
  to 0 transcribed and `ix-<client>-di` re-processed all 286 documents. Fixed
  by the header; redeploy with the command above.
- **Video / audio pipeline silently skipping everything with 0 or 1 file
  (found 2026-09-27, fixed).** `List_blobs_loop` turns the Blob XML listing
  into JSON: `Blobs.Blob` is an array only for 2+ blobs (one blob gives an
  object, none gives null), so `Select_blobInfo` failed with one video. The
  run still showed *Succeeded*: `Run_search_indexer` always runs last and sets
  the run status. `Compose_blobsJson` now normalizes to an array (both
  `ingestion/video-index` and `ingestion/audio-transcribe`), and
  `scripts/sync-client.ps1` reports failed steps under *Succeeded* runs too
  (`-Trace ingest|audio|video` walks a run step by step). Redeploy:
  `az deployment group create --resource-group rg-knowledgeengine-v9 --name
  video-<client> --template-file ingestion/video-index/main.bicep --parameters
  clientCode=<client> videoIndexerAccountId=<accountId> videoIndexerAccountName=vi-knowledgeengine3-v9
  videoIndexerLocation=francecentral namePrefix=knowledgeengine3 createRoleAssignments=true -o none`
  (audio: `--name audio-<client> --template-file ingestion/audio-transcribe/main.bicep
  --parameters clientCode=<client> speechEndpoint=https://aif-knowledgeengine3-v9.cognitiveservices.azure.com
  namePrefix=knowledgeengine3 keyVaultName=kv-knowledgeengine3-v9 createRoleAssignments=true`).
- **Video post-processing had never run end to end; audio choked on empty
  phrases (found 2026-09-29, fixed).** Video: `Compose_transcript` was written
  `"@{coalesce(...)}"`, which turns the array into a string, so
  `For_each_transcriptLine` failed right after Video Indexer had finished. The
  video was therefore never flagged `videoindexed` and **each daily run
  uploaded it again and paid a full Video Indexer pass** (1h25 of video, three
  times from 2026-09-27 to 09-29). Now: a video already *Processed* under the
  same name in the VI account is reused (`List_vi_videos` /
  `Condition_already_indexed`, no new upload); transcript lines without text are
  dropped; `instances[0]` / index lookups go through `first()` / `skip()`.
  Audio: Speech returns some phrases with an empty `nBest`, and the channel
  merge read `nBest[0]` (2 files of client-s failed every day);
  `Filter_channel0/1` now drop them; a recording left with no phrase at all
  (the same `cb6131cb-...wav` in two folders) is flagged `transcribed` +
  `nospeech` instead of failing in `Merge_channels_loop` (an Until loop runs
  once even with nothing to merge) and being re-submitted to Speech every day.
  Both: the loops that append lines to a
  variable ran 20 in parallel, so lines could land out of order (and video
  timestamps next to the wrong line); they now run one at a time. Transcripts
  written before this fix may have lines out of order.

---

## §13. Diagnostic engine (deterministic agentic RAG) — 2026-09-30

Design: the architecture document "Architecture - Agentic RAG diagnostic ITSM" (Docs).
Code: `orchestration/diagnostic/` (contracts, pure FSM, ports, prompts, service) and
`app/diag_tab.py` (tab + signed endpoints). Tests: `python -m pytest tests` (needs
`pydantic`, `pytest`, `flask`; 42 tests, no network).

### 13.1 What it is

A state machine `INIT_TRIAGE -> NEED_DIAGNOSTIC_DATA / OCR_PROCESSING -> KB_MATCHED ->
ACTION_PROPOSED`, terminal `HUMAN_ESCALATION`. The model extracts facts, reads screenshots and
drafts the plan; **code** decides every transition. Loop termination: 4 questions max, 24 h
deadline, stagnation check, never the same question twice, 2 unreadable screenshots, 2 rejected
plans, 8-transition guard per event. A plan is accepted only if every step cites a chunk of the
selected document, every `verbatim_from_kb` step is a substring of it, and its sha256 matches.
Risk topics (MFA reset, privileged access, data deletion, security incident) escalate at once.
A technical failure (search/model down) becomes an escalation, never an improvised answer.

### 13.2 Web app

Tab **Diagnostic** (`/diag`) between Assistant and Tickets ITSM: paste text and/or screenshots
(PNG/JPEG/WebP, 10 MB, 3 max, kept in memory only), answer the question, get the plan or the
escalation file. Sessions are rows of the `diagsessions` table (created at start-up); a session
is visible to its author, and ServiceNow sessions to users with ITSM access. Deploy:
`.\deploy-webapp.ps1 -ClientsLocal client-s` (new Python dependency `pydantic`, installed by Oryx).

### 13.3 ServiceNow webhook (optional)

`.\scripts\enable-diagnostic-webhook.ps1 -ClientId client-s` sets `DIAG_WEBHOOK_SECRET` /
`DIAG_WEBHOOK_CLIENT`, excludes exactly `/api/servicenow/webhook` and `/diag/internal/sweep` from
Easy Auth, and writes the secret to `clients-local\diag-webhook-secret.txt` (never printed).
`POST /api/servicenow/webhook` with `X-KE-Timestamp` and `X-KE-Signature: sha256=HMAC(secret,
"<timestamp>.<body>")` (5 minute window), JSON `{event_id, ticket_number, short_description,
description, comment}`. The response carries `state`, `outbox` (question / plan / escalation)
and `plan` for the flow to post into the ticket; the same `event_id` twice changes nothing.
`POST /diag/internal/sweep` (same signature) escalates waiting sessions past their deadline:
call it hourly from a Logic App. Without the secret both endpoints answer 404.
**Not done:** posting back into ServiceNow from the app (the flow does it from the response).

### 13.4 To calibrate before trusting the thresholds

Confidence = 0.5 x reranker/`reranker_full` + 0.3 x margin/`margin_full` + 0.2 x variable
coverage; KB_MATCHED needs >= 0.95 and a margin >= `margin_min`. Starting values (NOT yet measured):
`reranker_full` 3.0, `margin_full` 0.8, `margin_min` 0.5 - a top score of about 2.8 with a 0.8 margin
and the required variables present reaches 0.95. Calibrate on `eval/` (goal: zero wrong fiche at
>= 0.95) and override per client in `engine.<client>.yaml`:

    diagnostic:
      conf_threshold: 0.95
      margin_min: 0.5
      reranker_full: 3.0
      margin_full: 0.8

Other limits of this first version: the index has no application/OS fields, so the variables
steer the query text and the question choice, not OData filters; screenshots are read by the
chat model (no Vision/Document Intelligence service deployed), so `bbox` is empty and a code is
"verified" only when it matches a known pattern with confidence >= 0.85; `source_system` is
always `servicenow_kb` (no SharePoint-tagged documents in the index yet).

### 13.5 Display fixes on the Assistant (2026-09-30)

- No irrelevant KB fiche as "source principale": when no fiche covers the question, the best
  audio/video source actually used is promoted (label "Aucun document KB pertinent"), else none.
  The fiche stays in `_trace`. Shared engine (`attach_sources`): re-run the golden eval.
- Audio/video sources show a redacted summary or excerpt instead of "contenu non affiche": only
  sentences that look like a spelled password are dropped, digit runs are masked; one card per
  source document.

### 13.6 First live session and fixes (2026-09-30 22h)

A screenshot-only start ("Connexion Bureau a distance" loading dialog) then the answer
"burau a distance" ended in `stagnation` at 29 %. Causes, fixed (45 tests): the text read from
a screenshot was neither used in the search query nor counted as evidence; a short answer to a
targeted question was not stored as that variable; a second screenshot was requested after one had
already been read; a screenshot-only start put a placeholder sentence in the query.

Second live session ("Outlook en ligne : correcteur ..." at 63 %): a screenshot was requested for a
how-to question, and one empty reply ("Ca ne marche pas") escalated at turn 1/4. Now: close
candidates are asked about first (choose between the fiches), a screenshot is requested after that;
escalation for stagnation needs two consecutive replies with nothing new; the escalation card lists
the closest fiches and points to the Assistant.

### 13.7 One tab: the Assistant and the Diagnostic merged (2026-09-30 22h)

`/` now redirects to `/diag`, the single "Assistant" tab: the closest KB fiche (title, match strength,
excerpt, other leads) is shown as soon as the problem is described and stays on top while the guided
diagnostic asks its questions; once a plan is validated the plan replaces it. The previous single-shot
assistant is kept at `/classic` (link "Assistant classique", keeps its saved conversations), so a
rollback is one redirect line in `app/app.py` (`root`). Not yet in the unified tab: audio/video
secondary sources and the Assistant's conversation history sidebar.

### 13.8 Corrections session reset mot de passe (2026-09-30)
- Fiche choisie par l'utilisateur (bouton) = confirmation humaine: passage direct a KB_MATCHED puis plan; la question "Laquelle de ces situations" n'est plus reposee.
- `required_variables` vaut [] par defaut; la question "application" n'est posee que si requise ou sans candidat.
- Extrait de fiche lisible (filtre `kb_text`, balisage DI retire).
- Session terminee (escalade/plan): les reponses ulterieures sont ignorees (plus de messages ajoutes).
- Redeploiement: `.\deploy-webapp.ps1 -ClientsLocal client-s`.

### 13.9 Refonte : moteur de resolution guidee (2026-09-30)
Remplace le paquet `orchestration/diagnostic/` (supprime) par `orchestration/guide/`. Plus aucune escalade.
- Parcours : (1) fiche exacte -> (2) resume des etapes -> (3) accompagnement etape par etape jusqu'a "resolu".
- Phases : LOCATE, GUIDING, SOLVED (seul etat final), STUCK (attente d'une nouvelle description, non final).
- Fiche exacte : score reranker >= `exact_score` (2.0) et ecart >= `margin_min` (0.5), recoupe par un juge LLM (temp 0, schema strict) ; en cas d'ambiguite l'utilisateur choisit parmi 3 fiches (boutons) ; apres `max_rounds` (2) clarifications sans choix, la meilleure fiche est prise ("la plus proche"). Seuils NON calibres : `diagnostic:` dans `engine.<client>.yaml`.
- Guide : resume + etapes (titre court, consigne fidele a la fiche) ; validation en code (chaque etape cite un chunk existant, `verbatim` degrade si faux) ; repli deterministe sur les lignes numerotees de la fiche.
- Etapes : boutons Fait / Ca ne marche pas / Expliquer / Precedente ; aide basee sur la fiche uniquement (sinon rappel de la consigne) ; apres 2 echecs proposition d'une autre fiche ; "Ce n'est pas la bonne fiche" et "toujours pas resolu" proposent les fiches suivantes ; plus de fiche -> STUCK.
- Sujets sensibles (MFA, admin, suppression, incident) : bandeau d'alerte, pas d'escalade.
- Actions = liste fermee (`pick:1-3, done, blocked, explain, back, wrong_fiche, solved_yes, solved_no, none`), validee cote serveur.
- Webhook ServiceNow : meme route, reponse `state/guide/current_step/outbox` ; `/diag/internal/sweep` est un no-op.
- Tests : `tests/test_guide_fsm.py`, `tests/test_guide_app.py` (24 passes).
- Deploiement : `.\deploy-webapp.ps1 -ClientsLocal client-s`.


## §14. Repository renamed to `deterministic-itsm-engine` (2026-10-05)

- GitHub repo `yassinebabakhouya2-bit/knowledgeengine-rag-platform` renamed to
  `yassinebabakhouya2-bit/deterministic-itsm-engine` from the repo's GitHub
  Settings page. GitHub redirects the old URL automatically; existing local
  clones keep working without any change, but the remote URL was also
  updated for consistency:
  ```powershell
  git remote set-url origin https://github.com/yassinebabakhouya2-bit/deterministic-itsm-engine.git
  ```
- Reason: the repo name `knowledgeengine-rag-platform` undersold what the
  repo actually demonstrates — a deterministic *decision* layer (code
  decides, never the LLM) applied to two problems: which KB fiche/step to
  show (diagnostic engine) and which ITSM action to take (Jalon 10 action
  engine, already closed and validated live). `deterministic-itsm-engine`
  names the differentiator; both modules stay in this one repo, not split
  across repos — the earlier plan to split the V10 diagnostic track into its
  own repo (`deterministic-diagnostic-engine`, created 2026-10-04) was
  reversed the next day (see `jalon10`/`diagnostic-engine` project memory):
  V10 continues directly on `main` of this same repo.
- README.md, CLAUDE.md, `docs/architecture.md` and `docs/v10-deterministic-engine.md`
  updated the same day to reflect the new name and the unified framing
  (diagnostic engine + ITSM action engine = two applications of one
  deterministic core). This runbook's own history above (§0–§13) is left
  untouched — it is a log of what was actually run, not a document to
  rewrite after the fact.
- Local folder name on disk (`C:\V9\knowledgeengine-rag-platform`) was not
  renamed — purely cosmetic, left as is to avoid re-pointing every open
  terminal/IDE session; only the GitHub remote name changed.


## §15. V10 on Azure — slice 1: `fn-kecore` Function App, per-client containers (2026-10-06)

Context: on 2026-10-05 the V10 track (kecore / kefind / scoreboard) stopped
running as local CLIs; everything moves to Azure in Bicep (plan and slices in
`docs/v10-deterministic-engine.md`, "Azure-native migration"). Slice 1 is
infrastructure only: `infra/modules/kecore.bicep`, also wired into
`infra/main.bicep` so a fresh `bootstrap-new-tenant.ps1` creates it.

What it creates (no application code yet):
- Function App `fn-kecore-knowledgeengine3-v9` on the existing B1 plan
  `plan-knowledgeengine3-v9`, Python 3.11, system identity, identity-based host
  storage, alwaysOn; Log Analytics `log-fn-kecore-knowledgeengine3-v9` +
  Application Insights `appi-fn-kecore-knowledgeengine3-v9`.
- Containers `kecore-<client>` and `tickets-<client>` for clienta, clientb,
  clientc, client-s (private).
- Roles of the Function's identity: Storage Blob Data Owner, Storage Queue Data
  Contributor, Storage Table Data Contributor (storage account), Cognitive
  Services OpenAI User (Foundry account).

### 15.1 Deploy (module alone — does not touch the other resources)

Validated beforehand with `bicep build` and `bicep lint` (0 warning in the
module). Deploying the module on its own, rather than `main.bicep`, avoids
re-applying the Web App's Easy Auth settings, whose secret is only passed at
deploy time. Every default of the module matches the live naming.

```powershell
cd C:\V9\knowledgeengine-rag-platform
az account show --query "{tenant:tenantId, subscription:name}" -o table   # must be the platform's subscription
az deployment group create --resource-group rg-knowledgeengine-v9 --name kecore-slice1 `
  --template-file infra/modules/kecore.bicep -o table
```

### 15.2 Verify

```powershell
az functionapp show -g rg-knowledgeengine-v9 -n fn-kecore-knowledgeengine3-v9 --query "{state:state, principal:identity.principalId}" -o table
az storage container list --account-name stknowledgeengine3v9 --auth-mode login --query "[].name" -o tsv |
  Select-String "^(kecore|tickets)-"   # PowerShell filter: no '||' inside a JMESPath passed through az.cmd
$p = az functionapp show -g rg-knowledgeengine-v9 -n fn-kecore-knowledgeengine3-v9 --query identity.principalId -o tsv
az role assignment list --assignee $p --all --query "[].roleDefinitionName" -o tsv
```

Expected: `Running`; 8 containers; the 4 roles above. The Function App answers
its default page until slice 2 deploys code — that is normal.

### 15.3 Drop a client's ITSM export (client-s: EasyVista, no API)

The raw export goes to `tickets-<client>/raw/`, uncompressed (CSV, XLSX, JSON or
JSONL — what `scoreboard/importers.py` reads; `.gz` is not read). It holds
personal data (names, e-mails) until slice 4's Function scrubs it into a Table
and deletes the raw blob: do not copy it anywhere else.

```powershell
az storage blob upload --account-name stknowledgeengine3v9 --auth-mode login `
  --container-name tickets-client-s --name "raw/<export file name>" --file "<path to the export>"
```

A 403 `AuthorizationPermissionMismatch` means the signed-in user lacks
Storage Blob Data Contributor on `stknowledgeengine3v9` (the bootstrap's
`prereqs` phase normally grants it).

**Status 2026-10-06: deployed (`kecore-slice1` Succeeded).**


## §16. V10 on Azure — slice 2: kecore decomposition in the Function, parity test (2026-10-06)

Prerequisite: §15 deployed (Function App, containers, roles).

What changed in the repo:
- `kecore/fiches.py`: documents are read from bytes (`read_document_bytes`) and in an
  OS-independent order (`document_sort_key`: case-insensitive, separator-agnostic — the order
  Windows gave the reference runs). `load_folder` and the Function share
  `fiches_from_documents`. Order matters: duplicate ids keep the first file, and the profile
  keeps the first spelling it meets for each heading, which is part of an LLM record key.
- `kecore/llm.py`: `RecordingLLM` takes a `store` (folder by default, blob container in Azure),
  same layout and keys.
- `kecore_func/`: the Function (see its README). `kecore_pipeline.py` holds the steps and is
  tested in memory, including "same result as a local run" and "replay calls nothing".
- `scripts/deploy-kecore-function.ps1`.

**Parity proven before deploying (2026-10-06):** the pipeline code, run in the Cowork sandbox
on the local copy of `kb-client-s/Kbs` and the local record, in replay mode, gave exactly the
2026-10-01 reference (`client-s-v2`): 243 documents, 242 fiches (same `KB0068` unreadable
warning), 163 guided / 22 citable / 57 info_only, 2228 / 2228 steps verified, mean agreement
0.919, **0 model calls, 462 answers from the record, 0 errors**. python-docx 1.2.0 and pypdf
6.18.0 give that same text; they are pinned in `kecore_func/requirements.txt`. The only replay
miss is the dictionary clustering call (`with_dictionary`), which postdates the reference: it
falls back to the deterministic dictionary and changes no status.

### 16.1 Upload the LLM record (once)

455 files, about 2.5 MB, from the reference runs. After this, `clients-local/kecore/` is no
longer needed by anything (slice 7 removes it).

```powershell
cd C:\V9\knowledgeengine-rag-platform
az storage blob upload-batch --account-name stknowledgeengine3v9 --auth-mode login `
  --destination kecore-client-s --destination-path llm-cache `
  --source clients-local\kecore\llm-cache --overwrite false -o none
```

### 16.2 Deploy the code

```powershell
.\scripts\deploy-kecore-function.ps1
```

Expected last lines: the function names `kecore_start`, `kecore_run`, `kecore_extract`,
`kecore_profile`, `kecore_decompose`, `kecore_report`.

### 16.3 Parity run on Azure (replay: no model call, no cost)

```powershell
$fn = 'fn-kecore-knowledgeengine3-v9'
$key = az functionapp keys list --resource-group rg-knowledgeengine-v9 --name $fn --query functionKeys.default -o tsv
$body = @{ client = 'client-s'; source_prefix = 'Kbs/'; mode = 'replay' } | ConvertTo-Json
$run = Invoke-RestMethod -Method Post -Uri "https://$fn.azurewebsites.net/api/kecore/runs?code=$key" -Body $body -ContentType 'application/json'
do { Start-Sleep -Seconds 30; $s = Invoke-RestMethod $run.statusQueryGetUri; $s.runtimeStatus } while ($s.runtimeStatus -in 'Pending', 'Running')
$s.output | ConvertTo-Json -Depth 5
```

Expected output: `fiches 242, guided 163, citable 22, info_only 57, steps 2228,
steps_verified 2228, mean_agreement 0.919`, and under `llm`: `calls 0, cached 462, errors 0`.

If it differs:
- `fiches` is not 242: the content of `kb-client-s/Kbs/` changed since 2026-10-01 (daily
  SharePoint ingestion). Compare the blob listing with the reference before anything else.
- `errors` above 0: record keys missed. Check that the app setting `KECORE_AOAI_ENDPOINT` has
  the host `aif-knowledgeengine3-v9.cognitiveservices.azure.com` (it is part of every key)
  and that the build installed the pinned python-docx / pypdf.
- The run's files are under `kecore-client-s/runs/<run_id>/`; `report.md` is the readable one.

### 16.4 Normal runs

Same request with `"mode": "record"`: answers already in the record are reused, only new or
changed fiches call the model. `limit` (first N fiches) is useful for a quick check on a new
client. Each run keeps its own folder; `latest.json` points to the last completed one.

### 16.5 No function loaded after a deployment (2026-10-06, first deployment)

Symptom: right after the first deployment, `az functionapp function list` printed nothing and
`POST /api/kecore/runs` answered 404; still nothing after a restart.

Root cause, two things, read from `FunctionAppLogs` and the host itself:
- the roles of §15 had been created a minute before: the host's health check reported
  `Unable to access AzureWebJobsStorage ... AuthorizationPermissionMismatch` from 19:40 to 19:41
  UTC (identity-based host storage, RBAC propagation), then recovered on its own;
- the function list Azure Resource Manager keeps stayed empty because the triggers had not been
  synced once the host was up. The host itself was `Running` and served the 6 functions.

The code was never at fault (it imports and registers the 6 functions in a clean environment).

Fix: `deploy-kecore-function.ps1` now syncs the triggers and asks the host directly, through
Azure Resource Manager (management.azure.com, reachable through Zscaler), until it lists the
functions. The same check by hand:

```powershell
$rg = 'rg-knowledgeengine-v9'; $fn = 'fn-kecore-knowledgeengine3-v9'
$sub = az account show --query id -o tsv
$base = "https://management.azure.com/subscriptions/$sub/resourceGroups/$rg/providers/Microsoft.Web/sites/$fn"
az rest --method post --url "$base/syncfunctiontriggers?api-version=2022-03-01"
az rest --method get --url "$base/hostruntime/admin/host/status?api-version=2022-03-01"
az rest --method get --url "$base/hostruntime/admin/functions?api-version=2022-03-01" --query "[].name" -o tsv
```

Host errors are in `FunctionAppLogs` (workspace `log-fn-kecore-knowledgeengine3-v9`). From this
machine the query only works with Zscaler off: Zscaler intercepts TLS to the Log Analytics API
(`CERTIFICATE_VERIFY_FAILED`); the portal's Logs blade works either way.

```powershell
$ws = az monitor log-analytics workspace show -g $rg -n "log-$fn" --query customerId -o tsv
az monitor log-analytics query -w $ws --analytics-query "FunctionAppLogs | where TimeGenerated > ago(2h) and Level in ('Error','Warning') | project TimeGenerated, Message, ExceptionMessage | order by TimeGenerated desc | take 25" -o table
```

### 16.6 Parity run on Azure — PASS (2026-10-06 20:07 UTC)

Run `20261006T200741Z-b61cb7`, mode `replay`, completed in under 2 minutes:

| | Reference 2026-10-01 (local) | Azure 2026-10-06 |
| --- | --- | --- |
| Fiches | 242 | 242 |
| Guided / citable / info_only | 163 / 22 / 57 | 163 / 22 / 57 |
| Steps verified | 2228 / 2228 | 2228 / 2228 |
| Mean agreement | 0.919 | 0.919 |
| Model calls / from the record / errors | 215 / 247 / 0 (record mode) | 0 / 462 / 0 |

The decomposition of the real client-s KB now runs in Azure with the same result, at no model
cost. The versioned map of the KB is in `kecore-client-s/runs/20261006T200741Z-b61cb7/`
(`profile.json`, `decomposed/`, `fiches.decomposed.jsonl`, `report.md`, `summary.json`);
`latest.json` points to it.

**Status 2026-10-06: slices 1 and 2 deployed and verified on Azure.**

## 17. Slice 3: finding the fiche by entities and the graph (2026-10-07)

`kefind.funnel.find` — entities first, the client's graph next, BM25F text only to break ties;
the code decides at every step, the LLM only proposes (dictionary candidates, ticket search
terms). New in the repo: `kefind/graph.py`, `kefind/funnel.py`, `kefind/interpret.py`,
`kefind/funnel_engine.py`; `kecore/profile.py`'s dictionary now also mines product names from
document names (LLM-classified, corpus-verified, permanently human-rejectable); the report adds
a `graph` to the run's summary; the profile step reads `dictionary-decisions.json`;
`kefind_service.py` and the `kecore_find` route (interpretations recorded in
`kecore-<client>/find-cache/`). `scripts/deploy-kecore-function.ps1` ships `kefind/` with
`kecore/`.

### 17.1 The client-s dictionary: common words in, products out (found 2026-10-06)

The deterministic dictionary (trigger words only) held `desk` and `request` as "applications"
(from "the service Desk", "service Request") and missed every real product (VEEAM, Unity,
AutoCAD, LogMeIn, Palo Alto...), because client-s names its products in document names without
any trigger word ("How to Install AutoCad", "VEEAM-Appel_Support"). Fix, in
`kecore.profile.build_dictionary`:
- a candidate must be written as a name: at least 80% of its occurrences capitalized, links and
  paths aside ("Desk" 54%, "Request" 32%, against AutoCAD 100%, VEEAM 98%, Unity 94%);
- the words written like names in the document names are candidates too (71 on client-s, in 2
  fiches or more but not in most of them, which code alone cannot tell from a product);
- one model call per run keeps the products among the candidates and groups their spellings
  under a canonical name (`kecore.llm_segment.llm_dictionary_products`);
- the code verifies: a proposed alias is kept only if it shares a word with an actual candidate
  and occurs in 2+ fiches of the corpus — the model cannot invent anything;
- an entry a person rejects in `kecore-<client>/dictionary-decisions.json` never comes back.

Tests in `kecore/tests/test_profile.py`.

### 17.2 Deploy the code

```powershell
cd C:\V9\knowledgeengine-rag-platform
.\scripts\deploy-kecore-function.ps1
$fn = 'fn-kecore-knowledgeengine3-v9'
$key = az functionapp keys list --resource-group rg-knowledgeengine-v9 --name $fn --query functionKeys.default -o tsv
```

### 17.3 New map of client-s (record: one model call, for the dictionary)

```powershell
$body = @{ client = 'client-s'; source_prefix = 'Kbs/'; mode = 'record' } | ConvertTo-Json
$run = Invoke-RestMethod -Method Post -Uri "https://$fn.azurewebsites.net/api/kecore/runs?code=$key" -Body $body -ContentType 'application/json'
do { Start-Sleep -Seconds 30; $s = Invoke-RestMethod $run.statusQueryGetUri; $s.runtimeStatus } while ($s.runtimeStatus -in 'Pending', 'Running')
$s.output | ConvertTo-Json -Depth 5
(az rest --method get --url "https://stknowledgeengine3v9.blob.core.windows.net/kecore-client-s/runs/$($s.output.run_id)/profile.json" --resource https://storage.azure.com --headers "x-ms-version=2021-08-06" | ConvertFrom-Json).dictionary
```

Expected (2026-10-07, real run): 242 fiches, 163/22/57, 2228/2228 verified, mean agreement
0.919, one live model call (the dictionary), `graph` with 242 fiches / 231 with a number / 12
references / 11 duplicate groups / 2 number conflicts / 0 superseded.

### 17.4 Reject what is not a product

The ids of the entries to drop go in `kecore-client-s/dictionary-decisions.json`, then the run
of 17.3 is made again: the dictionary's answer is already in the record (same candidates, same
request), so it costs no call; the code applies the rejection. The JSON goes through a temporary
file, deleted right after: PowerShell strips the quotes of a JSON argument given to `az`.

```powershell
$tmp = New-TemporaryFile
'{"rejected": ["<entry id>"]}' | Set-Content -Path $tmp -Encoding ascii
az storage blob upload --account-name stknowledgeengine3v9 --auth-mode login --container-name kecore-client-s `
  --name dictionary-decisions.json --file $tmp --overwrite -o none
Remove-Item $tmp
```

### 17.5 Ask for a fiche

```powershell
function Find-Fiche([string]$Text, [bool]$Interpret = $true, [string[]]$Answers = @()) {
    $b = @{ client = 'client-s'; text = $Text; answers = $Answers; interpret = $Interpret } | ConvertTo-Json
    $r = Invoke-RestMethod -Method Post -Uri "https://$fn.azurewebsites.net/api/kecore/find?code=$key" `
        -Body ([Text.Encoding]::UTF8.GetBytes($b)) -ContentType 'application/json; charset=utf-8'
    $shown = if ($r.fiche) { $r.fiche.label } else { $r.decision.question }
    '{0} [{1}] {2}' -f $r.decision.kind, $r.decision.reason, $shown
}
# code only
Find-Fiche 'Comment vider le cache Teams ?' $false
```

### 17.6 What slice 3 does not do yet

- A fiche kecore marks `info_only` (no resolution step) is never returned.
- Two fiches that are really close still give a question.
- The thresholds (`kefind.funnel.FunnelConfig`) are starting values; real tickets measure and
  calibrate them in slice 4.
- Raw ITSM ticket exports hold personal data (names, e-mails). They go to
  `tickets-<client>/raw/` in Azure, never to the client's SharePoint library: the daily
  ingestion Logic App copies that whole library into `kb-<client>` and the hourly text indexer
  does not exclude `.csv`, so a raw export dropped there gets indexed and searchable alongside
  the KB. If one is uploaded there by mistake, move it to `tickets-<client>/raw/`, delete it from
  SharePoint before the next daily ingestion run, and never commit it to git (`.gitignore` now
  excludes `*.csv` / `*.csv.gz` repo-wide).

### 17.7 First real run on Azure (2026-10-07): confirmed and one fix

Deployed and run on client-s's real 242 fiches (`run_id 20261007T195803Z-aeb2ac`). Confirmed
against the sandbox prediction, exactly: 242/163/22/57 fiches, 2228/2228 steps verified, mean
agreement 0.919; `graph` stats (242 fiches, 231 with a number, 12 references, 11 duplicate
groups, 2 number conflicts); the real 20-entry dictionary (GPT-4o) matches the hand-simulated
one closely. Three of four `Find-Fiche` calls matched the prediction exactly (Teams cache,
AutoCAD license by title match, English "account locked" ticket with no interpretation) — the
funnel, the graph and the title-match mechanism are confirmed working in production.

**Bug found: a French ticket with interpretation missed its own fiche.**
`Find-Fiche "Mon compte est bloqué, je n'arrive plus à me connecter à Windows"` (interpret=true)
returned `question [text_only_close_choice]` with three unrelated fiches, instead of
`KB0120- LOCKED ACCOUNT`.

Root cause, from the call's full trace (`$r | ConvertTo-Json -Depth 8`):
- the `entities` step found nothing — the ticket has no error code, no application, nothing
  technical; its only OS mention ("Windows", no version) is not in
  `kecore.entities.OPERATING_SYSTEMS` (only `windows 10`/`windows 11`/`windows server`), and by
  design the OS never filters anyway (`kefind/funnel.py`'s own docstring: "Le système
  d'exploitation ne filtre jamais"). So the funnel fell to `text_only` mode (pure BM25F over all
  175 candidates) — expected behavior for a ticket with no identifying entity, not a bug by
  itself.
- the real bug is in `kefind/interpret.py`'s prompt: it told the model to give "the usual fix
  when it is well known (for example 'unlock account', ...)" — only the resolution action, never
  the problem's state. The model dutifully returned `"unlock account"`, never `"account
  locked"`/`"locked account"`. Fiche titles name the problem's state ("LOCKED ACCOUNT"), not the
  fix, so `unlock` never lexically meets `locked` and KB0120's title-match bonus (`named()`,
  needs 2 matched title words) never triggered. The ticket's own French words ("compte",
  "bloque") did get tokenized and used — confirmed by the trace — so the filter and ranking code
  worked exactly as designed; only the terms the model chose to propose were one-sided.

Fix (`kefind/interpret.py`, prompt only, same validation/record-and-replay mechanism, no logic
change): the system prompt now explicitly asks for both the problem's state phrasing
("account locked", "compte bloqué") and the fix ("unlock account"), explaining that knowledge-base
titles name the state, not the action. `RecordingLLM`'s cache key hashes the system prompt, so
this invalidates the existing cached answer for this ticket and forces one fresh live call.

**Dictionary: one rejection.** Of the real run's 20 entries, one entry is the client's own parent
company name, not a software product — rejected via 17.4 (the exact spelling is not reproduced
here; see the real `dictionary-decisions.json` in Azure, never in git). `unity` (possibly an
internal PC-migration program name rather than the game engine) and `miracast` (a display
standard, not a vendor product) are kept: both still work as useful entities even if the
classification label is imprecise.

**Status 2026-10-07: confirmed on Azure (run_id 20261007T204049Z-215423). `dictionary-decisions.json`
holds the client's parent-company name as its one rejected entry; dictionary now 19 entries. The
retest of the failing ticket now returns `fiche [text_only_close_title_match] KB0120- LOCKED
ACCOUNT` directly. Slice 3 closed pending slice 4 (tickets, calibration).**

### 17.8 Never the real client names in git (2026-10-07)

The repo's git history (32 of 49 commits, 2 commit messages) held the client's real parent-company
and production-client names in several older commits. Scrubbed with `git filter-repo
--replace-text` (regex, case-insensitive, whole word) and force-pushed on all branches. Anyone
with another clone or fork of this repo must delete it and re-clone — the old commit SHAs no
longer exist upstream. If the repo was ever public, GitHub's cached views of already-merged PRs
can still show the old names; only GitHub support can purge that cache.

**Caution for next time**: `git filter-repo --force` resets the working tree to the rewritten
HEAD, discarding any uncommitted changes on already-tracked files (new, untracked files are
unaffected). Always commit (or at minimum stash) pending work before running a history rewrite.
This cost a round of re-recovering the slice 3 code straight from the last deployed package in
Azure (`/api/vfs/data/SitePackages/` on the Function App's Kudu site, since
`WEBSITE_RUN_FROM_PACKAGE` is unset and the live code is a mounted zip, not a `wwwroot` tree).

## 18. Slice 4: real tickets, scrubbed and run blank (2026-10-08)

The client has no KB field on its tickets (confirmed: no ticket's "Résolution" text names a KB
number), and hand-labeling isn't happening yet. So slice 4 starts reduced: get the real export
into a shape `kefind` can read, and run every ticket through the funnel once, unlabeled, to see
the shape of what comes back (fiche / question / abstain, and why). No accuracy number yet —
that needs labels (even 20-30 would do) and is the scoreboard, still ahead.

### 18.1 What's new

- `kecore/tickets.py` -- `scrub(data: bytes) -> (list[Ticket], ScrubReport)`. Parses the real
  EasyVista export (`;`-separated, quoted fields with embedded newlines -- `csv` module, never
  hand-split), drops the width-indicator row that isn't a ticket, drops every column that names a
  person (`Bénéficiaire`, `Demandeur`, `Intervenant en cours`, `Enregistré par`,
  `Résolu par (intervenant)`) entirely rather than masking it, and masks e-mail/phone in the free
  text that's kept (`kecore.text.Scrubber`, the same mechanism KB decomposition already uses).
  `Ticket.to_entity(client)` gives one Azure Table entity, French column names slugged into valid
  Table property names (ASCII, `[A-Za-z_][A-Za-z0-9_]*`).
- `kecore_func/kecore_table.py` -- `TableStorage`, the Function's managed identity against the
  same storage account's Table endpoint (no key). One table (`KECORE_TICKETS_TABLE`, default
  `tickets`), `PartitionKey` = client, `RowKey` = ticket id.
- `kecore_func/tickets_service.py` -- the route logic: `scrub` (reads every
  `tickets-<client>/raw/*.csv`, writes Table rows, deletes the raw export), `run_batch` (one
  Durable-batch slice of a client's scrubbed tickets through `kefind.funnel.find`, interpretation
  included when asked, tallied by `kind`/`reason` only), `merge_runs`.
- `kecore_func/kecore_blob.py` gained `delete(container, name)` (raw export removed once scrubbed).
- `kecore_func/kecore_pipeline.py` gained `Storage.delete(...)` on the protocol and
  `ticket_container(client) -> "tickets-<client>"`.
- Two new routes in `kecore_func/function_app.py`:
  - `POST /api/kecore/tickets/scrub` `{"client": "client-s"}` -- synchronous (one CSV parse +
    Table upserts, fast enough for a plain HTTP route).
  - `POST /api/kecore/tickets/runs` `{"client": "client-s", "run_id": null, "interpret": true,
    "limit": 200}` -- Durable, same shape as `/kecore/runs`: an orchestrator (`tickets_run`) calls
    `tickets_count` then fans out `tickets_run_batch` activities (`RUN_BATCH_SIZE = 25`,
    `kecore_pipeline.batches`) and merges the tallies. `run_id: null` uses the client's latest
    `kecore` run (same as `/kecore/find`'s default).
- `azure-data-tables>=12.5` added to `kecore_func/requirements.txt` (was only in the repo root's).
- Infra: no Bicep change needed -- `infra/modules/kecore.bicep` already provisions the
  `tickets-<client>` containers and already grants the Function's managed identity
  `storageTableDataContributor` (slice-1 anticipated this).

Tests: `kecore/tests/test_tickets.py` (10), `kecore_func/tests/test_tickets_service.py` (23,
in-memory storage/table stubs, no Azure needed). Full suite: 93 (kecore) + 94 (kefind) + 23
(kecore_func) = 210 passing.

### 18.2 Raw export: where it goes

The raw CSV goes to `tickets-<client>/raw/<file>.csv` in Blob storage -- **never** the client's
SharePoint (§17.6) and never git (`.gitignore`'s `*.csv` rule). `/tickets/scrub` deletes it once
it's in the Table, so the raw export never lingers.

### 18.3 Deploy + scrub + run (PowerShell)

```powershell
cd C:\V9\knowledgeengine-rag-platform
.\scripts\deploy-kecore-function.ps1

$key = az functionapp keys list --name fn-kecore-knowledgeengine3-v9 --resource-group <rg> --query "functionKeys.default" -o tsv
$base = "https://fn-kecore-knowledgeengine3-v9.azurewebsites.net/api"

# upload the raw export to tickets-client-s/raw/ first (az storage blob upload or the portal), then:
Invoke-RestMethod -Method Post -Uri "$base/kecore/tickets/scrub?code=$key" `
  -ContentType "application/json" -Body '{"client":"client-s"}'

$run = Invoke-RestMethod -Method Post -Uri "$base/kecore/tickets/runs?code=$key" `
  -ContentType "application/json" -Body '{"client":"client-s","limit":500}'
Start-Sleep -Seconds 5
Invoke-RestMethod -Uri $run.statusQueryGetUri
```

The run's final output is `{"client", "run_id", "tickets", "empty", "kinds": {...}, "reasons":
{...}}` -- a distribution, not a score. `kinds` splits fiche/question/abstain; `reasons` is
`kefind.funnel`'s own reason codes for each. Nothing here says whether a shown fiche was the
*right* one -- that's the scoreboard, once tickets are labeled.

### 18.4 Bug: a network timeout crashed the whole ticket run (2026-10-07)

**Symptom.** The first real `/kecore/tickets/runs` call (370 scrubbed tickets, `interpret: true`)
failed after ~8 minutes: `tickets_run_batch` raised `AzureError: cannot reach
.../gpt-4o/chat/completions: timed out`, and Durable Functions' `task_all` failed the whole
orchestration on that one batch — no partial tally from the other batches that had already
finished.

**Root cause.** `RestClient.request` (`kecore/azure.py`) retries on HTTP 429/500/502/503/504, but
a genuine transport-level failure (DNS, connect, read timeout) is raised by `urllib_transport`
*before* any status code exists, as a hardcoded `AzureError` -- bypassing retry entirely, and
ignoring the `error_class=LLMError` `AzureOpenAIChat` configures. `kefind.interpret.interpret`
only catches `LLMError` by design (a failed interpretation should degrade to "no terms", never
crash the batch) -- so the raw `AzureError` fell straight through.

**Fix.** `RestClient.request` now catches a transport-level `AzureError`, retries it exactly like
a 429/5xx (same backoff), and on final failure raises through the client's own `error_class`
instead of the hardcoded `AzureError`. `kecore/tests/test_llm.py::TransportRetryTest` covers both
paths (retried-then-succeeds, retried-then-degrades-to-LLMError). No behavior change for a
request that never times out. Full suite: 95 (kecore) + 94 (kefind) + 23 (kecore_func) = 212
passing.

### 18.5 Bug: only the ticket text was scrubbed, not the rest of the row (2026-10-08)

**Symptom.** Found while building the labeling tab (§18.6), before any human saw the data: the
stored row of a scrubbed ticket kept `Résolution`, `Cause réelle`, `Sujet complet`, `Référence
externe`... exactly as exported, e-mail addresses and phone numbers included. The 896 e-mails and
186 phones reported by the first `/tickets/scrub` were masked in Titre / Sujet / Description only.

**Root cause.** `kecore.tickets.scrub` passed only `TEXT_COLUMNS` (the three columns kefind reads)
through the `Scrubber`; every other kept column was copied as is. The module docstring said the
resolution was scrubbed too, and the test meant to check it
(`test_resolution_text_is_kept_scrubbed_not_dropped`) only checked that the column was kept. Two
cleanings of the local pipeline of 2026-10-01 were also missing: cutting the e-mail signature and
masking @mentions.

**Fix.** Every kept column except the structured ones (dates, priority, status, SLA, reference
numbers) is cleaned (signature, greeting name, forwarded headers, e-mail, phone, mentions, person
names, 30,000-character cut); the rules as they stand after two review passes are in 18.8 and in
the docstring of `kecore/tickets.py`. `POST /api/kecore/tickets/rescrub` re-applies it in place to the rows already stored: the
raw export is deleted after scrubbing, so in place is the only way. 13 new tests in
`kecore/tests/test_tickets.py`, one in `kecore_func/tests`. Known limit, unchanged: a first name
alone in running text, without a marker around it, is not detected.

### 18.6 Labeling tab, scoreboard on Azure, calibrated floor (2026-10-08)

Yassine: "tout le reste de la solution, décide toi-même". Decisions taken, each reversible:

- **Labels live apart** (`ticketlabels`, written only by the Web App): human work is never
  overwritten by a re-scrub or a new run. Every table write MERGEs (`kecore_table.TableStorage`),
  so a re-scrub also keeps the engine's findings on a ticket's row.
- **Labelers = the ITSM agents** (`KE-v9-itsm-agents`, from `config/itsm.yaml`); a dedicated group
  can be set in `config/labels.yaml`. The user must also be allowed to see the client (`app/auth.py`).
- **Against a biased ground truth**: the engine's proposal is shown, never pre-checked; the next
  ticket is drawn from the engine outcome (fiche / question / abstain) whose labels are fewest; "Je
  ne sais pas" skips without inventing a label. A label also accepts what the KB graph maps the
  labeled fiche to (its canonical twin, the fiche replacing it).
- **Calibrated floor** (`FunnelConfig.min_show`, kefind funnel v2): under it a fiche is offered
  first in a choice instead of shown alone; a fiche the ticket designates itself (cited number,
  the technician's own answer) is always shown. Chosen by `scoreboard.metrics.calibrate` on half the
  labels (split by a hash of the ticket id, stable), confirmed on the other half, and applied only
  by an explicit `POST /api/kecore/funnel-config/apply`, refused (409) when not confirmed. Never
  automatic. Default ceiling: 5% wrong fiches shown, judged on the top of the 95% interval.
- **How many labels**: a first score as soon as there are a few dozen (wide margins). To prove a
  ceiling with zero wrong fiche: 5% needs 73 tickets per half (146 labeled), 10% needs 35 per half
  (70). With some wrong fiches, more.
- **`apply` refuses** a floor that was not confirmed on half B, measured on another KB map than
  `latest.json`, measured without the model's interpretation (which `/find` uses), measured while the
  model failed transiently (throttling, server, network), or chosen for a ceiling looser than 10%.
- **A ticket that fails in a run is counted, never fatal** (`errors`, reason `error:<type>`).

New: routes `tickets/rescrub`, `scoreboard/runs`, `scoreboard/latest`, `funnel-config/apply`
(`kecore_func/README.md`); `tickets/runs` now refreshes the catalog (`kefindfiches`) and keeps each
finding on its row (`kefind_*`); `/find` reads `kecore-<client>/funnel-config.json` (cached 60 s);
the `scoreboard` package is deployed with the Function; Web App tab `/labels` (`app/labels.py`,
`config/labels.yaml`), linked from the Assistant and ITSM headers for labelers. Tests: kecore 108,
kefind 99, kecore_func 60, scoreboard 71, app 40 after the review fixes of 18.8 (kecore 123) — 393
passing. No infrastructure change: both
identities already hold Storage Table Data Contributor on the account.

### 18.7 Deploy and use (lot 1)

```powershell
cd C:\V9\knowledgeengine-rag-platform
.\scripts\deploy-kecore-function.ps1
.\deploy-webapp.ps1

$key  = az functionapp keys list --name fn-kecore-knowledgeengine3-v9 --resource-group rg-knowledgeengine-v9 --query "functionKeys.default" -o tsv
$base = "https://fn-kecore-knowledgeengine3-v9.azurewebsites.net/api"

# 0. read-only check: did a copy of the raw export reach the KB container (and its index)? (18.8)
az storage blob list --account-name stknowledgeengine3v9 --container-name kb-client-s --auth-mode login --query "[?ends_with(name, '.csv')].name" -o tsv

# 1. privacy fix on the 370 rows already stored (18.5, 18.8)
Invoke-RestMethod -Method Post -Uri "$base/kecore/tickets/rescrub?code=$key" -ContentType "application/json" -Body '{"client":"client-s"}'

# 2. blank run: catalog + each ticket's finding kept on its row (feeds the labeling tab)
$run = Invoke-RestMethod -Method Post -Uri "$base/kecore/tickets/runs?code=$key" -ContentType "application/json" -Body '{"client":"client-s","limit":500}'
do { Start-Sleep -Seconds 30; $s = Invoke-RestMethod -Uri $run.statusQueryGetUri; $s.runtimeStatus } while ($s.runtimeStatus -in 'Pending','Running')
$s.output | ConvertTo-Json -Depth 5
```

Then label at `https://app-knowledgeengine3-v9.azurewebsites.net/labels`. Score whenever wanted:

```powershell
$sb = Invoke-RestMethod -Method Post -Uri "$base/kecore/scoreboard/runs?code=$key" -ContentType "application/json" -Body '{"client":"client-s"}'
do { Start-Sleep -Seconds 20; $s = Invoke-RestMethod -Uri $sb.statusQueryGetUri; $s.runtimeStatus } while ($s.runtimeStatus -in 'Pending','Running')
$s.output | ConvertTo-Json -Depth 5
(Invoke-RestMethod -Uri "$base/kecore/scoreboard/latest?client=client-s&code=$key").report_md
# only when the output says "confirmed": true
Invoke-RestMethod -Method Post -Uri "$base/kecore/funnel-config/apply?code=$key" -ContentType "application/json" -Body ('{"client":"client-s","scoreboard_id":"' + $s.output.sb_id + '"}')
```

Rollback of a floor: `-Body '{"client":"client-s","reset":true}'` (the previous file stays in
`kecore-client-s/funnel-config.history/`).

### 18.8 Two independent reviews of the slice 4 code (2026-10-08)

Before anything was deployed, a separate agent that had not seen the code written reviewed it
twice, running it on concrete inputs. Every finding below is fixed and has a test
(`kecore/tests/test_tickets.py` ReviewCasesTest / SecondReviewTest, `kecore_func/tests`,
`tests/test_labels_app.py`).

| Finding (symptom) | Root cause | Fix |
| --- | --- | --- |
| "Cordialement, Jean Dupont" kept whole; "Cdt" signatures kept | the formula had to be alone on its line; "cdt" unknown | a formula ending its line, alone or followed by up to 3 capitalised words (the signer); abbreviations only at the start of a line |
| first fix over-cut: "Le CDT du chantier…" → "Le", "je vous remercie cordialement de votre aide : …" cut | formula matched anywhere | same tail rule: nothing but punctuation and a name may follow the formula on its line |
| "Bonjour Outlook plante au démarrage" → "Bonjour [nom] au démarrage" | the inline `(?i)` also made the name's capital-letter class case-insensitive | case-sensitive name, `(?i:…)` scoped to the greeting word and title; the name must end the greeting (`,` `!` `.` `:` or end of line); "Bonjour M. Dupont,", "Bonjour Jean et Paul," handled |
| "De : Jean Dupont <…>" kept the name | forwarded headers not handled | header lines (De, From, À, To, Cc, De la part de…; "A :" only with an address) → `[masqué]` |
| "01.10.2026 18:16" → "[phone]:16" | phone pattern accepted a date | ticket-only phone pattern: same formats as `kecore.text.PHONE_RE`, never starting on a dd.mm.yyyy / dd-mm-yyyy / dd mm yyyy date. A first fix (one consistent separator) missed "0661-234567": replaced. `kecore.text.PHONE_RE` is untouched on purpose: the KB decomposition uses it and its LLM record is keyed on exact text |
| "[email]", "[phone]", "[nom]" searched as the words email / phone / nom (in 55 / 34 / 21 of the 175 ranked client-s fiches) | placeholders left in the text kefind reads | `ticket_text` and `Ticket.text` remove them |
| a header "Bénéficiaire " (trailing space) or a renamed person column stored in clear | blocklist of exact header names | allowlist (`KEPT_COLUMNS`), headers compared trimmed and case-folded; unknown columns dropped and reported (`unknown_columns`) |
| names from the person columns not masked in the text | — | at scrub time: first-name/surname pairs in any case; a single name only written as one ("Dupont", never "DUPONT", which could be a word of an upper-case title: "ECRAN BLANC" with a beneficiary "Blanc" stays); a single upper-case value ("ADMINISTRATEUR") is an account |
| one U+2028 in a labeled ticket broke every scoreboard batch | `read_jsonl` used `splitlines()` | split on "\n" only |
| a 130k-character field failed the whole scrub; `[\w.+-]+@` took seconds on long runs | csv module field limit; unbounded e-mail local part | field limit raised, value cut to 30,000 characters before cleaning, ticket-only e-mail pattern with a bounded local part |
| `apply` accepted a floor measured on another map, without interpretation, with model failures, or for a 45% ceiling | only `confirmed` was checked | all refused (18.6); only transient model failures block (a content filter refusal repeats at the desk too: measured as it really goes, reported) |
| two labelers could overwrite each other | one shared order, unconditional write | an order per labeler; a first label is an insert, a correction an If-Match update of the row read; refused with "changed" otherwise |
| `/t/I1%0A` reached Azure and gave a 500; free text in `?msg=` shown on the page | `re.match` with `$`; message passed as text | `fullmatch` for every id from a request; messages are codes |

**Known limits, kept:** a first name alone in running text, an upper-case surname alone, a
lower-case surname after a lower-case mention ("@jean dupont"). The 370 rows stored on 2026-10-07
cannot get the person-column name masking (those columns were never stored): only a fresh export
through `/tickets/scrub` gives it. The raw export deleted from `tickets-client-s/raw/` stays
recoverable for 7 days (blob soft delete of the account), then is purged.

**Check (read-only, 18.7 step 0):** the daily ingestion copies every file of the client's SharePoint
site into `kb-client-s` and the text indexer indexes `.csv`; neither propagates a deletion. If the
raw export sat in SharePoint through one ingestion run, a copy may still be in `kb-client-s` and in
`idx-client-s`. If the check lists a file, remove the blob and its chunks from the index before
anything else.

### 18.9 Lot 1 deployed and run on the real data (2026-10-08)

Commit `1211d9a`; fn-kecore and the Web App deployed (18.7).

- Step 0 (read-only check): no `.csv` in `kb-client-s`, so the raw export never reached the index.
- `rescrub`: 370 rows, 67 changed (68 `[mention]`, 57 `[signature]`, 3 `[nom]`, 1 `[masqué]`).
- Blank run on the KB map `20261007T204049Z-215423` (catalog: 242 fiches): 370 tickets, fiche shown
  88, question 282, abstain 0, errors 0, interpretation failures 7 (transient, counted, not fatal).
  Reasons: `text_only_close_choice` 207, `text_only_close_title_match` 75, `entities_close_choice` 52,
  `entities_close_title_match` 13, `text_only_no_title_match` 10, `entities_close_entity` 8,
  `entities_no_title_match` 5.
- Observation, not acted on: the engine never abstained on 370 real tickets, and three answers in
  four are a question, most from the text alone. Whether those questions offer the right fiche, or
  should have been abstentions, is exactly what the labels measure (scoreboard). No threshold is
  changed before that: it would be tuning blind.

## 19. Slices 5 and 6: the engine in the live Diagnostic, the dictionary review, the write-back (2026-10-08)

Lots 2 and 3 of the plan agreed with Yassine ("tout le reste de la solution, décide toi-même").
Built and reviewed on 2026-10-08, deployed with 19.6.

### 19.1 The engine finds the fiche in the Diagnostic (slice 5)

`app/diag_tab.py` puts the engine in front of the search index (`orchestration/guide/kefind_ports.py`)
when the Web App is linked to fn-kecore (`app/kecore_client.py`, settings `KECORE_FUNCTION_URL` and
`KECORE_FUNCTION_KEY`):

- the engine shows a fiche: the session is guided with the fiche's verified steps, word for word
  (badge "Texte exact de la fiche"; the card says "Fiche identifiée par le moteur déterministe");
- the engine asks: its fiches are the choices, one per branch of its question in turn, never "strong";
- the engine abstains, has no map for the client, or cannot be reached: the search index answers,
  exactly as before. Without the two settings the Diagnostic is unchanged.

A candidate of the engine is `kefind:<run id>:<fiche id>`: the KB map that decided is pinned in the
session, so a pick or a help request after a new kecore run still reads the same fiche.

New routes of fn-kecore (`kecore_func/README.md`): `GET /kecore/fiche` (the fiche's steps and full
text, for a given run); `/kecore/find` accepts `observe` (19.2).

Link Web App → Function (`infra/modules/kecore-link.bicep`): the Function's default host key is
copied into Key Vault (`kecore-function-key`); the Web App's identity gets Key Vault Secrets User on
that secret only; the two settings are merged into the existing app settings, the key as a Key
Vault reference. The key is never in code, a file or a plain setting.

### 19.2 The dictionary's online loop and its review tab (slice 5, pilier 2)

- A live question is observed only when `/kecore/find` gets an `observe` key: the Diagnostic sends a
  hash of the session (sha256 of client and session id, 32 hex characters), never the id. A
  candidate is a product-like name after a trigger word ("l'application X", "le logiciel Y"), 3
  words and 40 characters at most (a longer run of capitalized words is a sentence, a signature or
  people's names: dropped), unknown to the dictionary. It counts once per distinct session.
- Personal data: the question is never stored. Below 3 distinct sessions a candidate is only a hash,
  a count and hashes of sessions (table `kefindpending`); its spelling is stored at the threshold.
- Review tab `/dictionary` (same access as `/labels`): accept as new software, accept as another
  spelling of an entry, reject; reject an entry of the current dictionary.
- Decisions are rows of `kefindpending`, set once with If-Match. The next kecore run reads them,
  with the optional hand-written `kecore-<client>/dictionary-decisions.json` (17.4). Routes
  `GET /kecore/dictionary`, `POST /kecore/dictionary/decision` (404: unknown or not ready yet; 409:
  already decided).

### 19.3 Write-back of a work note into the ServiceNow incident (slice 6)

- An ITSM agent may give an incident number (INC followed by 7 to 10 digits) when starting a
  diagnostic; sessions from the ServiceNow webhook carry theirs.
- Session page, for ITSM agents and the clients of `config/diag-writeback.yaml`: panel
  "Ticket INC… : note de résolution" with the note, "Valider : ajouter la note au ticket", and
  "Transférer au module ITSM (<action>)" when the fiche's title matches a handover rule
  (`password_reset`, `mfa_reset`, `group_add`).
- The note (`orchestration/guide/writeback.py`): fiche title and reference, outcome, the fiche's
  steps, "Validé par <agent> le …". A step the assistant reformulated (written by a model, which sees
  the user's text) is replaced by a pointer to the fiche: no user text and no model wording reach
  ServiceNow.
- A validation is a row of `diagwriteback` (`status` validated, `executionStatus` empty). The Web App
  never calls ServiceNow and holds no ServiceNow credential.
- Executor `logic-diag-writeback-client-s` (`itsm/writeback/`, its own identity): every 2 minutes,
  the rows of its client not executed yet (filtered in the query, 50 per run); claims a row with
  If-Match; looks the incident up (an inactive incident is left alone); adds the note as an internal
  work note, or also assigns the incident to KE-Automation for a handover (the group must exist);
  writes `executionStatus` back: success, not_found, inactive, dry_run or error, shown on the page.
- An agent may validate again after error, not_found or inactive, or when a row stayed "running" for
  15 minutes; never after success or dry_run.
- `dryRun=true` by default (the incident is looked up, nothing written). Deployed again with
  `dryRun=false`, the dry_run rows validated in the last 2 days are written for real.
- Identity: Storage Table Data Contributor on the `diagwriteback` table only (not the account:
  `diagsessions` holds what users typed), Key Vault Secrets User on the ServiceNow password secret only.

### 19.4 Decisions (each reversible)

- The engine first, the search index as fallback: never a regression when the engine is down,
  abstains or has no map.
- The Web App calls fn-kecore with the Function's default host key, kept in Key Vault and read
  through a Key Vault reference. Accepted for now: that key also opens the administration routes
  (runs, scrub, apply). Planned hardening (lot 4): a function-level key on the four routes the
  Web App calls (`find`, `fiche`, `dictionary`, `dictionary/decision`).
- Write-back for incidents only: the executor reads and writes the incident table. Requested items
  (RITM) stay with the ITSM module.
- One executor per client and ServiceNow instance (`clientId` of the module, `clients` of
  `config/diag-writeback.yaml`): a client is listed only once its executor is deployed.
- Dictionary decisions are table rows, the hand-written file stays a second source.

### 19.5 Independent review before deployment (2026-10-08)

A separate agent that had not seen the code reviewed lots 2 and 3, running it on concrete inputs.
Every finding is fixed and tested (`kecore_func/tests/test_dictionary_service.py`,
`test_function_app.py` Slice5RoutesTest, `tests/test_kefind_ports.py`, `tests/test_writeback.py`,
`tests/test_guide_app.py`).

| Finding (symptom) | Root cause | Fix |
| --- | --- | --- |
| `"l'"*22+"x"` took 2.7 s in the dictionary scan, 61 characters about 12 minutes: any Diagnostic user could freeze fn-kecore | the optional article group before the trigger word, with `l'` matching two ways, backtracked exponentially | group removed (it changed no captured name: kecore tests unchanged), the scan is linear (20 KB of hostile text in about a millisecond); live text capped at 4,000 characters |
| a whole capitalized run ("Mon Compte Pour Marie Curie Bureau B204 et Paul Martin") stored at its first sight | no length cap; spelling stored from the first observation | 3 words / 40 characters at most, longer runs dropped; hash only until 3 distinct sessions |
| one session made a candidate "ready" by itself (every LOCATE event called `/find`) | no notion of session | `observe` key = hash of the session, the 8 latest kept per candidate; point reads instead of listing the partition |
| a decision could be lost (decisions file read-modify-write, row marked before the file write) or undone by a concurrent observation | file as the store, unconditional writes | decisions are table rows set once with If-Match, read by the next run; an observation never writes the status |
| the executor read every client's rows into one ServiceNow instance | no partition filter | `clientId` parameter, `clients` list in `config/diag-writeback.yaml` |
| processed rows read again forever, first page only: past about 1,000 rows new validations would never run | status filtered after the query | filter in the query (`executionStatus eq ''`), 50 rows per run |
| a RITM number accepted but never writable | the executor writes incidents only | incident numbers only |
| going live would replay every dry-run row, closed incidents included | no age or state check | dry_run rows of the last 2 days only; an inactive incident ends `inactive`, nothing written |
| a handover reported success when the group did not exist | a PATCH with an empty group still answers 200 | the group lookup must return a row, else `error` |
| a pick or a help request after a new kecore run read another map's fiche | the run id was lost between requests | the run is pinned in the candidate id |
| the engine's "which application?" question offered only the first application's fiches | options flattened in order, 3 shown | one fiche per branch in turn |
| a step reformulated by the model (which sees the user's text) went into the note | every step copied | only the fiche's own steps; a reformulated one becomes a pointer to the fiche |
| ServiceNow answers (the whole incident) in the run history; roles on the whole account and vault | defaults | `sysparm_fields`, secure inputs and outputs; roles on the table and on the secret |
| an invalid decisions file answered 409 "already decided" | its ValueError was taken for a conflict | a decision no longer reads the file; the review answers 500 with the reason |

Tests after the fixes: kecore 123, kefind 99, kecore_func 86, scoreboard 71, app 86 (465 passing).
Both Bicep modules build without warning (bicep 0.48.1); the workflow's actions, references and
parameters were checked by script and its `$filter` rendered for both modes.

### 19.6 Deploy (lots 2 and 3)

After the commit (files listed explicitly, as always). First deployment on 2026-10-08: see 19.8.

```powershell
cd C:\V9\knowledgeengine-rag-platform
.\scripts\deploy-kecore-function.ps1
.\deploy-webapp.ps1
az deployment group create -g rg-knowledgeengine-v9 --name kecore-link --template-file infra/modules/kecore-link.bicep --query properties.provisioningState -o tsv
az deployment group create -g rg-knowledgeengine-v9 --name diag-writeback --template-file itsm/writeback/main.bicep --parameters clientId=client-s dryRun=true --query properties.provisioningState -o tsv
Start-Sleep -Seconds 120   # the role on the secret must propagate before App Service resolves the reference

$app = az webapp show --name app-knowledgeengine3-v9 --resource-group rg-knowledgeengine-v9 --query id -o tsv
foreach ($i in 1..4) {
    az webapp restart --name app-knowledgeengine3-v9 --resource-group rg-knowledgeengine-v9
    Start-Sleep -Seconds 60
    $kv = az rest --method get --uri "https://management.azure.com$app/config/configreferences/appsettings/KECORE_FUNCTION_KEY?api-version=2022-03-01" --query properties.status -o tsv 2>$null
    "Key Vault reference: $kv"
    if ($kv -eq "Resolved") { break }
}
$key  = az functionapp keys list --name fn-kecore-knowledgeengine3-v9 --resource-group rg-knowledgeengine-v9 --query "functionKeys.default" -o tsv
$base = "https://fn-kecore-knowledgeengine3-v9.azurewebsites.net/api"
foreach ($t in @("Mon compte est bloqué, je n'arrive plus à me connecter à Windows", "Teams affiche un écran blanc")) {
    $q = @{ client = "client-s"; text = $t } | ConvertTo-Json
    $a = Invoke-RestMethod -Method Post -Uri "$base/kecore/find?code=$key" -ContentType "application/json; charset=utf-8" -Body ([Text.Encoding]::UTF8.GetBytes($q))
    $id = if ($a.decision.fiche_id) { $a.decision.fiche_id } else { @($a.candidates | ForEach-Object { $_.fiche_id })[0] }
    "find: {0} ({1}), interpreted {2} -> {3}" -f $a.decision.kind, $a.decision.reason, $a.interpreted, $id
    if ($id) {
        $f = Invoke-RestMethod -Uri ("$base/kecore/fiche?client=client-s&fiche_id=" + [uri]::EscapeDataString($id) + "&code=$key")
        "   fiche: {0} steps, run {1}" -f @($f.steps).Count, $f.run_id
    }
}
```

Expected: `Succeeded` twice, `Key Vault reference: Resolved`, and for each question the fiche the
engine decides (or offers first), interpreted `True`, with its steps. The question is sent as the
Diagnostic sends it: interpreted (one model call, recorded under the request's hash). Without the
interpretation a French question and English fiches share no word (19.8). The host's function list
printed by the deploy script may lag (16.5); the calls above are the proof. Then in `/diag`: the
first question gives "Fiche identifiée par le moteur déterministe" and steps marked "Texte exact de
la fiche".

### 19.7 Test the write-back on the PDI (dry run, then live)

1. Wake the PDI `dev374242` (developer.servicenow.com) and note the number of an active incident.
2. In `/diag`, start a diagnostic with that number, reach a fiche, then "Valider : ajouter la note au
   ticket". Within 2 minutes the page says "simulé (aucune écriture)": the incident was found.
3. Go live: `az deployment group create -g rg-knowledgeengine-v9 --name diag-writeback --template-file
   itsm/writeback/main.bicep --parameters clientId=client-s dryRun=false`. Within 2 minutes the
   page says "écrit dans le ticket" and the work note is on the incident in the PDI.

### 19.8 First deployment of lots 2 and 3 (2026-10-08)

Commit `38826f6`. fn-kecore and the Web App deployed; `diag-writeback`: Succeeded, Logic App
`logic-diag-writeback-client-s` Enabled (dry run).

| Symptom | Root cause | Fix |
| --- | --- | --- |
| `kecore-link`: `InvalidTemplate`, "Circular dependency detected on resource .../config/appsettings"; nothing created (validation failure) | a template may not read (`list()`) the app settings of the resource it writes; `bicep build` does not see it, ARM does | `infra/modules/appsettings-merge.bicep`: kecore-link reads the current settings and passes them to the module, which writes them back with the two new ones; the settings parameter is `@secure()` (it holds the Easy Auth secret: never in the deployment history) |
| `Key Vault reference: ` empty (NotFound) | consequence of the line above | redeployed with the fix |
| `/kecore/fiche` 404 for the id the previous call returned ("KB0163 – Unblock URL on PALO ALTO", printed with an "â") | the Python worker answers a bare `application/json`; Windows PowerShell 5.1 decodes it as ISO-8859-1, so the en dash came back as mojibake and was sent back as such | every JSON answer of fn-kecore is ASCII (non-ASCII characters escaped) and says `charset=utf-8`; the Web App was not affected (`requests` reads JSON as UTF-8) |
| the check question gave a choice of unrelated fiches ("Unblock URL on PALO ALTO", shared mailbox creation...) | the check sent it with `"interpret": false`: a French question and English fiches share no word, and the closest fiches by "compte", "bloqué", "connecter" are noise. The Diagnostic always asks for the interpretation | the check now sends it as the Diagnostic does. Simulated locally on the 1 October map of the same 242 fiches, with terms of the kind the interpretation gives ("account locked", "password expired", "clear Teams cache"), the same questions find KB0120 LOCKED ACCOUNT, the SSPR password reset fiche and "How to clear the TEAMS cache"; the check above confirms it on Azure |
| (risk seen in the same trace) the interpretation fails (7 of 370 tickets in the blank run, transient model errors): the engine's text-only decision then rests on the question's own words, noise for a French question | the engine had no way to tell the Diagnostic | `/kecore/find` answers `interpreted` (null: not asked; false: asked, failed); the Diagnostic does not use a `text_only` decision whose interpretation failed: the search index answers (`kefind_ports.py`). A decision backed by an entity of the ticket stands |
| the deploy script listed 21 functions, 24 are deployed | the host's listing lags one deployment (16.5) | none needed: the route calls are the proof |

Tests after the fixes: kecore 123, kefind 99, kecore_func 88, scoreboard 71, app 87.

Second deployment (commit `471fbde`): `kecore-link` Succeeded, `Key Vault reference: Resolved` at the
first check, the host lists its 24 functions. Asked as the Diagnostic asks (interpreted):

- "Mon compte est bloqué, je n'arrive plus à me connecter à Windows": fiche shown, KB0120- LOCKED
  ACCOUNT (`text_only_close_title_match`), 17 verified steps;
- "Teams affiche un écran blanc": a question (`entities_close_choice`) between the Teams fiches, led
  by "KB0250 - How to reconnect the Teams phone": the person picks. Whether "How to clear the TEAMS
  cache" should lead is for the labels to say (no prompt or threshold tuned on one example).

### 19.9 UX fixes after the first live write-back test (2026-10-08)

Yassine ran KB0120 (17 steps) end to end and validated a work note on `INC0010005`.

- The page said "validé, en attente de l'exécuteur" and nothing more: the executor polls every
  2 minutes (19.3), so a wait is normal, but the page did not say so. Now, while a validated row has
  no `executionStatus` yet, the panel adds "L'exécuteur ServiceNow passe toutes les 2 minutes :
  rafraîchissez la page après ce délai pour voir le résultat."
- Each "C'est fait." / "Ça ne marche pas." click was logged as a chat line ("Vous : C'est fait."):
  on a 17-step fiche the person scrolled past 17 of them to reach the current step on every reload.
  The step list already marks each one done with a check, so the button clicks are no longer shown
  as chat; what the person actually types still is.
- A POST back to the session page (`/reply`, `/writeback`) now redirects with a URL fragment
  (`#focus` on the current step or choice card, `#writeback` on the write-back panel): the browser
  lands there on load, no custom script needed, instead of the top of a session that has grown long.

Tests: `tests/test_guide_app.py` (90 app tests total).

### 19.10 Fusion de l'assistant « classique » dans le Diagnostic guidé (2026-10-08)

Les deux assistants (RAG classique en page d'accueil, Diagnostic guidé pas-à-pas) répondaient à la
même question de deux façons séparées. Décision : garder `kefind` comme seul décideur de fiche, et
n'utiliser le moteur de réponse libre de l'assistant classique que lorsque `kefind` et l'index de
recherche n'ont rien trouvé -- jamais l'inverse, et sans dupliquer son code RAG.

- Nouvelle phase `Phase.OPEN` (`orchestration/guide/contracts.py`, `fsm.py`) : atteinte quand
  `_locate()` épuise tous les candidats déterministes et indexés sans rien trouver. Un nouveau port
  `open_answer` (`orchestration/guide/ports.py::make_open_answer`) appelle directement
  `orchestration/answer.py::diagnostic_query_core_keyless` -- la même fonction que l'UI classique
  (`app/app.py`) appelle à chaque tour -- pour une réponse libre ancrée sur le corpus du client.
  `KefindPorts.open_answer` (`kefind_ports.py`) délègue toujours tout de suite au fallback : le
  moteur déterministe ne participe jamais à une réponse ouverte, il ne décide que des fiches.
- Depuis `OPEN`, tout nouveau message de l'utilisateur (hors "C'est fait." / "Ça ne marche pas.")
  relance `_locate()` avec le contexte enrichi avant de retenter une réponse ouverte si toujours
  rien trouvé -- `kefind` garde toujours la première chance. "C'est fait." referme la session
  (`Phase.SOLVED`, titre pris sur la source principale de la réponse ouverte) ; "Ça ne marche pas."
  redemande une précision sans nouvel appel au modèle.
- `GuideState.open_turns` (8 au plus) donne à chaque nouvel appel l'historique des échanges ouverts
  précédents (`prior_turns`), pour que le fil de la conversation reste cohérent.
- Si le port `open_answer` lève une exception (modèle ou réseau), repli sur `Phase.STUCK` comme
  filet de sécurité -- cette phase ne sert plus que ce cas, le "rien trouvé" général va désormais en
  `OPEN`.
- Page de session (`app/diag_tab.py`) : étiquette "Diagnostic ouvert", rendu du message `answer`
  (badge de confiance, source principale, sources liées, prochaine vérification), carte "Est-ce
  résolu ?" avec les deux boutons en phase `OPEN`.

Tests: `tests/` 99 (était 90) -- `test_guide_fsm.py` (nouvelle phase, réponse ouverte, repli sur
STUCK), `test_kefind_ports.py` (délégation systématique au fallback), `test_guide_app.py` (bout en
bout avec `monkeypatch.setattr(guide.ports, "diagnostic_query_core_keyless", ...)`, puisque
`diagnostic_query_core_keyless` appelle Azure Search directement et ne passe pas par le
`FakeAoai` des autres tests du fichier).

### 19.11 La décision par le sens : index sémantique figé, calibré sur la KB (2026-10-09)

**Pourquoi.** Sur les 370 vrais tickets (19.8), le moteur posait une question 76 % du temps, et 259
de ces 282 questions venaient d'égalités de score (`*_close_choice`) que le comptage de mots (BM25F)
ne peut pas trancher : « ligne » (question en français) et « line » (fiche en anglais) sont deux mots
sans rapport pour lui. Mesure hors-ligne sur les 175 fiches classées de client-s, sans ticket : avec
la 1re phrase de description d'une fiche comme question, la bonne fiche sortait 1re dans 96 % des cas
mais n'était montrée que dans 38 % (la règle « 2 mots du titre »), et 3 % des fiches montrées étaient
fausses. Une revue indépendante a conclu : garder le squelette (entités, graphe, le code décide,
étapes verbatim, enregistrement), remplacer le signal de pertinence et la source de calibration.

**Ce qui change.** Un run kecore construit maintenant, entre `report` et la publication de
`latest.json`, le dossier `kecore-<client>/runs/<run>/semantic/` (`kecore_func/semantic_service.py`) :

| Étape | Ce qu'elle fait | Écrit |
| --- | --- | --- |
| `cards` | par fiche, un appel gpt-4o écrit ce qu'elle résout (FR + EN) et ~10 questions qu'on pose quand on en a besoin (6 FR, 4 EN) ; le code garde ce qui passe : 2 à 40 mots, ni e-mail ni téléphone, aucune entité technique, numéro de fiche, application ni système que la fiche ne nomme pas (`kefind/cards.py`) | `cards/<i>.json` |
| `heldout` | par fiche, un appel indépendant (autre consigne, autre persona, température 0.9, seed fixe) écrit 4 messages d'un employé qui ne connaît pas la fiche, sans les mots du titre ; bruit déterministe (accents, lettres inversées) ; **jamais indexés** : c'est l'examen (`kefind/calibrate.py`) | `heldout/<i>.json` |
| `index` | chaque entrée (nom de la fiche, ce qu'elle résout, ses questions) vectorisée une fois (text-embedding-3-large, 1024 dimensions) ; toute ligne écrite par le modèle (ce qu'elle résout, ses questions) plus proche du texte propre d'une autre fiche que du sien est retirée -- l'étalon est le nom de chaque fiche et son propre texte (description, étapes vérifiées), jamais le modèle : une carte fausse ne peut pas se valider elle-même (`kefind/semantic.py`) | `vectors.f32`, `built.json`, puis `index.json` |
| `calibrate` | seuils `floor`, `margin`, `offer` choisis sur la moitié « calibration » de l'examen (fiches réparties par hash), mesurés sur l'autre moitié | `calibration.json` |
| `publish` | `latest.json`, en dernier : un run publié ne change plus jamais | `latest.json` |

Si une étape sémantique échoue, le run est quand même publié ; `latest.json` dit pourquoi
(`summary.semantic.error`) et `/find` décide par les mots comme avant. Un index n'est jamais utilisé à
moitié : `vectors.f32` est écrit avant `index.json`, et la carte ne charge un index que si son
`calibration.json` existe et que les sha256 concordent ; sinon elle décide par les mots et la réponse dit
pourquoi (`semantic_error`), sans jamais faire échouer `/find`. La Function ne garde en mémoire qu'un run
publié (`latest.json` le nomme, ou son dossier a `published.json`) : un run encore en construction est
relu à chaque appel, aucune instance ne fige une carte à moitié faite.

**La décision** (`kefind.funnel._find_semantic`, `kefind.semantic.decide`) : la question est vectorisée
une fois ; le score d'une fiche est la meilleure similarité cosinus entre la question et une de ses
entrées (calcul en Python pur, float64, ordre fixe : aucune différence d'une machine à l'autre). Seules
les entités fortes filtrent encore (réponse du technicien, numéro de fiche cité, code d'erreur ou
d'événement, mise à jour) ; une application citée en passant ne filtre plus. Puis trois comparaisons :
fiche montrée si son score atteint `floor` ET devance strictement la suivante d'au moins `margin` (une
égalité exacte n'est jamais tranchée par le nom d'une fiche) ; sinon les fiches au-dessus de `offer` sont
proposées (question) ; sinon abstention. Sans calibration réussie, ou seuils retenus par le verrou,
`floor` vaut 1.01 : aucune fiche n'est montrée seule sur un score, même aidée par un code d'erreur --
seule une fiche que le ticket nomme lui-même, seule candidate, l'est. Les règles « 2 mots du titre » ne
s'appliquent plus en mode sémantique. Les sommes sont exactes (`math.fsum`) : même résultat sur toute
machine et toute version de Python (le `sum` de Python a changé d'algorithme en 3.12). Les identifiants
du ticket se lisent quelle que soit leur casse (`kb893803` = `KB893803`, `0X8007…` = `0x8007…`) : deux
casses partagent un vecteur, elles partagent aussi la lecture des entités.

**Calibration sans ticket.** Sur la moitié « calibration », le code essaie chaque couple (floor, margin) --
marges strictement positives -- et garde celui qui montre le plus souvent la bonne fiche tant que (1) la
mauvaise fiche montrée reste sous 3 % en haut de son intervalle de Wilson à 95 % (la moitié test tolère
4,1 % : cette marge évite qu'un simple retour à la moyenne déclenche le verrou), et (2) quand la bonne
fiche est retirée de l'index
(la question n'a plus de bonne réponse), une fiche est montrée au plus 10 % du temps. `offer` garde 95 %
des questions dont la fiche est parmi les 3 plus proches. Cibles fixées avant toute mesure, vérifiées sur
l'autre moitié : mauvaise fiche montrée ≤ 2 % (borne haute ≤ 4,1 %), bonne fiche montrée ≥ 70 %,
questions ≤ 25 %, bonne fiche parmi les proposées ≥ 95 %, fiche montrée sans bonne réponse ≤ 10 %.
`calibration.json` → `acceptance.passed`. **Verrou de sécurité** : si la moitié test échoue un contrôle de
sécurité (mauvaise fiche montrée, borne haute ; fiche montrée sans bonne réponse), les seuils choisis sont
retenus (`withheld: true`, gardés sous `chosen`) : aucune fiche n'est montrée seule, le moteur ne fait que
proposer. Une cible d'utilité ratée (bonne fiche montrée, part de questions) ne bloque rien : elle est
rapportée (`test_effective` donne alors les mesures avec les seuils réellement utilisés). Sans aucune
question d'examen (tous les appels au modèle ont échoué), l'index propose comme un index non calibré
(`offer` 0,35) au lieu de s'abstenir sur tout. Les questions d'une même fiche ne sont pas indépendantes :
les intervalles sont plus étroits que la réalité, à lire comme un minimum de prudence. Ces chiffres
mesurent les questions écrites depuis la KB, pas
de vrais tickets : les vrais tickets se mesurent avec le run de tickets (`modes`, `reasons`) et 30 tickets
vérifiés à la main.

**Ce qui est garanti, et ce qui ne l'est pas.** Même texte de question (après normalisation : casse,
espaces, formes Unicode) + même run = même vecteur, mêmes scores, même décision, octet pour octet. Chaque
réponse du modèle (cartes, examen) est enregistrée dans `llm-cache/`, chaque vecteur dans `embed-cache/`
(construction) ou `find-cache/` (questions) ; le premier vecteur enregistré pour un texte gagne
(`write_if_absent`). Un run rejoué en mode `replay` réécrit le dossier sémantique à l'identique sans un
appel (testé). Les étapes montrées restent le texte exact de la fiche. Pas garanti : 100 % juste sur de
vrais tickets ; la même fiche pour deux formulations différentes (elles ont deux vecteurs) ; une question
jamais vue pendant une panne du service d'embeddings (décidée par les mots, réponse `mode: "degraded"`,
rien n'est enregistré). Deux fiches au texte quasi identique sont déjà fusionnées par le graphe (11
groupes sur client-s). Deux fiches au même nom mais au contenu différent (une version courte et une
longue, une version française et une anglaise) donnent une question : c'est le bon comportement, la KB
n'est jamais modifiée ; elles sont listées dans `built.json` (`same_label`) et comptées dans le résumé du
run (`semantic.index.same_label_groups`) pour qu'une personne les voie.

**Données.** Les cartes et l'examen sont dérivés des fiches et restent dans `kecore-<client>`. Pour une
question, `find-cache/` garde son vecteur (pas son texte), sous l'empreinte de son texte ; e-mails et
numéros de téléphone sont retirés avant vectorisation, pas les noms de personnes (aucun repérage fiable
dans un texte libre). Un vecteur peut être partiellement inversé : le traiter comme la table des tickets.
Il est gardé sans limite de durée -- c'est ce qui garantit la même décision pour la même question ; une
règle de rétention échangerait ce déterminisme contre de la confidentialité, c'est un choix à faire
explicitement.

**Réponse de `/kecore/find`.** Nouveaux champs `mode` (`semantic`, `words`, `degraded`) et
`semantic_error` ; `decision.reason` en `semantic_clear_lead`, `semantic_single`, `semantic_designated`,
`semantic_close_choice`, `semantic_close_entity`, `semantic_below_floor`, `semantic_nothing_close` ;
`decision.degraded`. La trace a une étape `semantic` : modèle, empreinte de l'index, seuils, et pour les
5 premières fiches leur score, le type d'entrée qui a gagné et son texte (pour comprendre une décision).
Dans le Diagnostic (`orchestration/guide/kefind_ports.py`), une fiche montrée est guidée et une question
propose les fiches, comme avant. Deux changements : quand le moteur a décidé par le sens (`mode:
"semantic"`) que rien n'est proche, ou que toutes ses fiches ont été refusées, c'est la réponse --
l'index de recherche et son juge LLM ne choisissent plus une fiche à sa place (la réponse libre OPEN
suit, présentée comme telle) ; et une fiche décidée par les mots pendant une panne des embeddings
(`mode: "degraded"`) est proposée, jamais montrée d'office.

**Infra.** Aucune nouvelle ressource : le déploiement `text-embedding-3-large` existe déjà (foundry.bicep),
le rôle Cognitive Services OpenAI User de la Function couvre tout le compte. Nouveau réglage
`KECORE_AOAI_EMBEDDING_DEPLOYMENT` (kecore.bicep). Coût d'un run complet : ~350 appels gpt-4o et
~3 000 textes vectorisés (quelques centimes) ; un rejeu ne coûte rien.

**Déployer, construire, vérifier** (bloc unique, après le commit) :

```powershell
cd C:\V9\knowledgeengine-rag-platform
az deployment group create --resource-group rg-knowledgeengine-v9 --name kecore-semantic --template-file infra/modules/kecore.bicep -o table
.\scripts\deploy-kecore-function.ps1
$key  = az functionapp keys list --name fn-kecore-knowledgeengine3-v9 --resource-group rg-knowledgeengine-v9 --query "functionKeys.default" -o tsv
$base = "https://fn-kecore-knowledgeengine3-v9.azurewebsites.net/api"
$body = @{ client = 'client-s'; source_prefix = 'Kbs/'; mode = 'record' } | ConvertTo-Json
$run  = Invoke-RestMethod -Method Post -Uri "$base/kecore/runs?code=$key" -Body $body -ContentType 'application/json'
do { Start-Sleep -Seconds 30; $s = Invoke-RestMethod $run.statusQueryGetUri; $s.runtimeStatus } while ($s.runtimeStatus -in 'Pending', 'Running')
$s.output.semantic | ConvertTo-Json -Depth 6
```

Attendu : `semantic.index.sha256`, `semantic.calibration.feasible`, `withheld` (false) et
`acceptance.passed`. Puis
`POST /kecore/find` avec « comment attribuer une ligne teams » : `mode` = `semantic`.

**Revue indépendante avant déploiement (2026-10-09).** Un agent qui n'avait pas vu le code l'a relu et
exécuté sur des entrées concrètes. Tout est corrigé et testé.

| Constat (symptôme) | Cause | Correction |
| --- | --- | --- |
| calibration échouée après l'index : `/find` en `mode: "semantic"` avec les seuils non calibrés ; index à moitié écrit : `/find` et `/fiche` en 500 | la carte chargeait tout ce qui était dans `semantic/` | vecteurs avant `index.json` ; index chargé seulement avec son `calibration.json` ; toute erreur → décision par les mots + `semantic_error` |
| verrou contourné : seuils retenus ou non calibrés, mais un ticket avec un code d'erreur montrait une fiche à 0,35 | une entité forte abaissait le plancher au niveau `offer` | plancher > 1 : rien n'est montré sur un score ; seule une fiche nommée, seule candidate |
| une marge 0 pouvait être choisie : égalité exacte tranchée par l'ordre alphabétique | marges à partir de 0, test `>=` | marges ≥ 0,005 et avance strictement positive |
| aucun examen → abstention sur tout, toujours en `mode: "semantic"` | `offer` mis à 1,0 | `offer` d'un index non calibré (0,35) |
| `kb893803` et `KB893803` : même vecteur, entités différentes | motifs kecore sensibles à la casse | identifiants du ticket lus sans casse (`canonical_identifiers`) |
| un run nommé pendant sa construction restait en mémoire sans index | cache de toute carte demandée | seuls les runs publiés (`published.json`, `latest.json`) restent en mémoire |
| une question citant une autre application passait (« … dans acrobat » pour une fiche Teams) | le contrôle ignorait applications et systèmes ; l'étalon incluait les lignes du modèle | applications/systèmes absents de la fiche refusés ; étalon = texte propre des fiches |
| le choix des seuils poussait au bord des 4 % : la moitié test échouait souvent par retour à la moyenne | pas de marge entre choix et contrôle | choix sous 3 %, contrôle à 4,1 % ; corrélation des questions d'une fiche documentée |
| décisions différentes possibles entre Python 3.11 et 3.12 | `sum` a changé d'algorithme en 3.12 | `math.fsum` |
| deux exécutions d'un même lot (température 0,9) : le rejeu n'était plus identique | `RecordingLLM` : le dernier écrivain gagnait | le premier enregistrement gagne, l'autre appel le relit |
| panne d'embeddings : fiche décidée par les mots montrée et guidée d'office | le Diagnostic ignorait `mode` | proposée, jamais montrée ; une abstention par le sens ne repasse plus par l'index et son juge LLM |
| plancher `min_show` du scoreboard calculé sur des cosinus, appliqué aux scores BM25 | deux échelles mélangées | `funnel-config/apply` refuse quand la carte décide par le sens |

Restent connus, sans effet sur la décision servie : si une activité `cards`/`heldout` échoue, ses
voisines encore en cours peuvent écrire dans le dossier après la publication (les cartes ne sont pas
lues à la décision) ; des fiches remplacées (aucune sur client-s) reçoivent des cartes mais sont écartées
à la décision, ce qui rend la calibration un peu plus prudente.

Tests : kecore 132, kefind 139, kecore_func 102, scoreboard 71, app 102 (546).
