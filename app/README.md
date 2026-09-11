# app/ — Web interface (Jalon 4)

Thin Flask front end over `orchestration/answer.py`'s `answer_query_core_keyless()` —
same retrieval + A3 hierarchy split + Structured Outputs generation as the CLI
(Jalon 3), unchanged. This layer adds a form and an HTTP entry point, nothing to the
RAG pipeline itself.

## Local run

```bash
pip install -r requirements.txt
az login   # DefaultAzureCredential falls back to this locally
export ALLOWED_CLIENTS=clienta,clientb,clientc   # optional, defaults to all 5 known clients
python app.py       # http://localhost:8000
```

Local dev needs the same two RBAC roles as the deployed App Service (see below) —
granted to *your own* Azure AD user this time, not the App Service's managed identity.
Without them you'll get a 403 from Search or Azure OpenAI.

## Credentials — no secrets

No admin key, no Key Vault entry, nothing in an app setting. Auth is
`DefaultAzureCredential`: the App Service's system-assigned managed identity in Azure,
an `az login` session locally. Required roles (`infra/modules/roles.bicep`):

- `Search Index Data Reader` on the Search service
- `Cognitive Services OpenAI User` on the Foundry account (same role Search's own
  identity already holds there, for embeddings)

## Client resolution — stopgap before Jalon 5

`ALLOWED_CLIENTS` (comma-separated, app setting in Azure) is the only gate on which
clients this instance can query. There is no per-user auth yet — anyone who can reach
the app's URL can pick any client in that list and query it. See project memory
`jalon4-app-interface.md` for why `clienta/b/c` and `client-v/s` are treated the same
way here (all data lives in Yassine's own sandbox tenant; there's no separate
DXC-tenant boundary to enforce today). Jalon 5 replaces this env var with a real
Entra ID group → clientId mapping (`access.entraGroup` in each `engine.<client>.yaml`
is already reserved for that).

## Deployment

Provisioned by `infra/modules/webapp.bicep` (Linux App Service, Python), wired into
`infra/main.bicep`. Code deploys via `SCM_DO_BUILD_DURING_DEPLOYMENT=true` (Oryx
builds from `requirements.txt` on push/zip-deploy) — no separate CI pipeline yet.

## Easy Auth — coarse gate (added 2026-09-11)

A "signed in to Yassine's sandbox tenant = in" gate in front of the whole app —
**not** the real per-client auth of Jalon 5 (no group → client mapping, no per-user
resolution). Added because deploying the app with zero protection, even as a
stopgap, was judged too exposed. See project memory `jalon4-app-interface.md`.

### 1. Create the App Registration (one-time, run yourself — `az` isn't available where Claude ran the rest of this milestone)

```bash
az ad app create --display-name "KnowledgeEngineV9-WebApp-Auth" \
  --sign-in-audience AzureADMyOrg \
  --web-redirect-uris "https://app-knowledgeengine2-v9.azurewebsites.net/.auth/login/aad/callback"
```

Note the returned `appId` — that's `easyAuthClientId` below.

### 2. Create a client secret for it

```bash
az ad app credential reset --id <appId> --display-name "easyauth" --years 1
```

Note the returned `password` — that's `easyAuthClientSecret` below. **Never paste it
in a Claude conversation, never commit it** (same rule as the ingestion secret — see
project memory `dxc-internal-track.md`). Keep it only in your shell for the deploy
step (e.g. an env var), or in a password manager.

### 3. Tenant ID

Project memory (`dxc-internal-track.md`) already has this sandbox tenant's ID:
`6cdc0a5a-779d-4912-a0b3-455e5fc4d9c6` — confirm it's still right with
`az account show --query tenantId -o tsv` before using it (getting this wrong breaks
login silently).

### 4. Deploy with these three values as secure parameters

```bash
az deployment group create -g rg-knowledgeengine-v9 -f infra/main.bicep \
  --parameters easyAuthClientId=<appId> \
               easyAuthTenantId=<tenantId> \
               easyAuthClientSecret=<the secret value, e.g. from an env var>
```

`enableEasyAuth` defaults to `true` — omit it. To redeploy without the gate (e.g. for
quick local-network testing), pass `enableEasyAuth=false` and you can skip the other
three parameters entirely.

### What this does and doesn't do

- Does: redirect any unauthenticated request to Entra ID login; only accounts in the
  tenant above (or guests invited into it) can complete sign-in and reach the app.
- Does not: restrict *which* client a signed-in user can query — `ALLOWED_CLIENTS`
  still controls that, uniformly for everyone who gets past the gate. Real per-user →
  per-client resolution is Jalon 5.
