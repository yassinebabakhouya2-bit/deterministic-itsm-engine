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

---

## §-1. Clone this repository

```bash
git clone https://github.com/yassinebabakhouya2-bit/knowledgeengine-rag-platform.git
cd knowledgeengine-rag-platform
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

---

## §7. Web app deployment (zip-deploy) — code/config changes

Any change to `app/*.py`, `orchestration/answer.py`, `app/requirements.txt`,
or a `config/*.yaml` / `clients-local/*.yaml` file needs the web app
redeployed via zip-deploy (a Bicep deploy alone does not push application
code — see §6.3). The general shape:

```powershell
# build deploy.zip from the repo's app/orchestration/config files, then:
az webapp deploy --resource-group <resource-group> --name <webapp-name> \
  --src-path <zip> --type zip
```

**Gap — not yet fully captured here**: the exact, verified zip-build step
has been done at least twice (entry-by-entry via .NET's
`ZipFileExtensions::CreateEntryFromFile`, converting `\` path separators to
`/`, per prior sessions), but the literal script wasn't committed to this
runbook at the time. Capture it here verbatim (the actual `.ps1`/command,
not a paraphrase) the next time a zip-deploy is performed, per the rule in
`CLAUDE.md`.

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
new SharePoint folder on the [CLIENT-PROD] site). Research done before writing any
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

### 10.3 Indexing preset — must be "Standard" or "Advanced" tier for Topics, not "Basic"

Per the indexing configuration guide: OCR is available at every tier
(Basic/Standard/Advanced) once video-visual analysis is requested at all,
but **Topics requires Standard or Advanced** — Basic alone won't produce
it. Use an "audio and video" preset at Standard or Advanced tier to get
transcript + OCR + topics in a single indexing call. **Exact literal
`indexingPreset` enum string not yet confirmed against a live call** —
verify with the actual API response on the first real test video before
hardcoding it into the Logic App.

### 10.4 Not yet done
- Deploy `infra/modules/videoindexer.bicep` (region no longer a blocker).
- Run one real end-to-end test call (generate token → upload → poll →
  fetch insights) against the single test video Yassine is uploading to
  SharePoint (`Documents/Microsoft 365 Basics Outlook and Teams Tutorial…mp4`)
  to confirm the exact `indexingPreset` string, the real shape of the OCR
  section, and the account's internal `accountId`/location-string before
  writing the actual Logic App.

### 10.5 Compte Video Indexer déployé (2026-09-20)

`az deployment group create --resource-group rg-knowledgeengine-v9 --template-file infra/modules/videoindexer.bicep --parameters videoIndexerAccountName=vi-knowledgeengine2-v9 storageAccountName=stknowledgeengine2v9` → `Succeeded`, depuis le terminal local de Yassine (`C:\V9\knowledgeengine-rag-platform`), pas Cloud Shell (le repo n'y est pas cloné — piège à noter : Cloud Shell persiste `$HOME` mais ne contient pas ce repo, toujours déployer un `--template-file` depuis un shell où le repo existe réellement).

Reste à récupérer avant le test API (§10.2) : le `principalId` de l'identité managée (sortie Bicep `videoIndexerPrincipalId`) et surtout le **`accountId` interne** du compte (GUID `properties.accountId` sur la ressource ARM — différent du nom `vi-knowledgeengine2-v9`, c'est CET id qui sert dans les URLs `api.videoindexer.ai`), voir commandes ci-dessous.

## §11. ITSM action module (Jalon 10) — demo identities

### 11.1 Seeding the fictional demo identities (2026-09-25 — script written, NOT yet run)

`scripts/itsm/seed-demo-identities.ps1` + `scripts/itsm/demo-identities.json` create, idempotently:
- **Entra ID (personal tenant only)**: 7 fictional users (`claire.dubois` manager, `amine.elidrissi` MFA reset, `sophie.martin` password reset, `karim.benali` access request, `julie.bernard` licence request, `thomas.leroy` departure, `nadia.admin` = User Administrator, the guardrail persona whose reset must be refused), 3 security groups (`SG-SP-Projets`, `SG-VPN-Users`, `SG-App-Planning`), manager links, memberships, and the directory role.
- **ServiceNow dev instance**: assignment group `KE-Automation` and matching `sys_user` records. Link key between the two systems: ServiceNow `email` = Entra `userPrincipalName`.

Run from the repo root, after `az login --tenant <personal tenant>` (the script asks for an explicit `YES` after showing the signed-in tenant, to avoid hitting a DXC/[CLIENT-PARENT] tenant):

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
