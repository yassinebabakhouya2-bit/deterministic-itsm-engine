# app/ — Web interface (Jalon 4, auth hardened Jalon 5)

Thin Flask front end over `orchestration/answer.py`'s `answer_query_core_keyless()` —
same retrieval + A3 hierarchy split + Structured Outputs generation as the CLI
(Jalon 3), unchanged. This layer adds a form and an HTTP entry point, nothing to the
RAG pipeline itself.

## Local run

```bash
pip install -r requirements.txt
az login   # DefaultAzureCredential falls back to this locally
export LOCAL_DEV_CLIENTS=clienta,clientb   # see "Client resolution" below -- only
                                            # used when Easy Auth isn't in front of
                                            # this process at all (true local dev)
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

## Client resolution (Jalon 5)

`app/auth.py` resolves which client(s) a request may query from the
`X-MS-CLIENT-PRINCIPAL` claims Easy Auth itself attaches to every authenticated
request — **never** from anything the browser posted. Two levels, in order:

1. **Tenant** (`tid` claim) — checked against every `entraTenantId` declared across
   `engine.<client>.yaml` (`config/` + `clients-local/`). A tenant that doesn't match
   any of them is denied outright, whatever groups the user has.
2. **Group** (`groups` claim) — only consulted when the matched tenant hosts several
   clients (a config with `access.entraGroup` set). A config *without* `entraGroup`
   means "this whole tenant = this client" (an external organization onboarded as a
   single client — the default for a new external tenant).

Both patterns can coexist for the same tenant: an external organization that itself
has several entities it wants isolated from each other declares one
`engine.<entity>.yaml` per entity, all sharing that organization's `entraTenantId` but
each with its own `entraGroup` (a security group *they* create in *their own* tenant)
— exactly the same mechanism used for `clienta`/`clientb`/`clientc`/`client-v`/
`client-s` sharing Yassine's own sandbox tenant today.

**Deny-by-default**: an unrecognized tenant, or a recognized tenant with no matching
group, resolves to *zero* clients — the app shows "access denied", never a fallback
client. `ALLOWED_CLIENTS` (the Jalon 4 stopgap env var) is gone.

**Onboarding a new client — the whole procedure**:
- *A new entity inside a tenant that already hosts other clients* (typically Yassine's
  own sandbox): create an Entra ID security group for it (Entra admin center → Groups
  → New group), add the intended users, note its Object ID, then set
  `access.entraGroup` in that client's `engine.<client>.yaml` to that Object ID
  (replacing the `TODO-...` placeholder) and redeploy.
- *A brand-new external organization* (their own tenant, first client from there): get
  their Tenant ID from them (Azure portal → Microsoft Entra ID → Overview, or
  `az account show --query tenantId`), create `engine.<their-client-id>.yaml` with
  that `entraTenantId` and no `entraGroup`, add that tenant ID to
  `easyAuthAllowedTenantIds` at deploy time (see below), redeploy, and have their
  Global Admin complete the one-time consent screen on first sign-in (standard
  multi-tenant app behavior — nothing custom to build for this).
- *That same external organization later wants several of their own entities
  isolated*: same as the first bullet, but in effect, the entities exist in *their*
  tenant, not yours — declare one `engine.<entity>.yaml` per entity, all with their
  `entraTenantId`, each with its own `entraGroup` (a group *they* create and hand you
  the Object ID for).

**Known limitation, not handled yet**: Entra only emits the `groups` claim inline for
users in fewer than ~200 groups; beyond that it's a "groups overage" claim requiring a
Microsoft Graph call instead. Unlikely on a sandbox/demo tenant; not implemented here.

## Local-dev fallback (no Easy Auth in front)

`LOCAL_DEV_CLIENTS` (comma-separated) is used **only** when the
`X-MS-CLIENT-PRINCIPAL` header is entirely absent — i.e. Easy Auth isn't sitting in
front of this process at all (`python app.py` locally, no App Service). This never
happens once deployed with `enableEasyAuth=true` and
`globalValidation.requireAuthentication=true`: an unauthenticated request never
reaches Flask there, so the header is always present, even when its claims resolve to
zero clients (a real "access denied", never treated as this fallback). Empty by
default — must be set explicitly to develop locally without Easy Auth.

## Deployment

Provisioned by `infra/modules/webapp.bicep` (Linux App Service, Python), wired into
`infra/main.bicep`. Code deploys via `SCM_DO_BUILD_DURING_DEPLOYMENT=true` (Oryx
builds from `requirements.txt` on push/zip-deploy) — no separate CI pipeline yet.

## Easy Auth — sign-in gate (added Jalon 4, hardened Jalon 5)

Easy Auth (App Service Authentication V2) gates the whole app on a valid Entra ID
sign-in. Two real defenses stack on top of that gate:

1. **`WEBSITE_AUTH_AAD_ALLOWED_TENANTS`** (platform-level) — a Microsoft-documented
   App Service setting restricting which Entra tenants may even complete sign-in,
   checked against the `tid` claim *before* the request reaches Flask at all. Set from
   `easyAuthAllowedTenantIds` at deploy time (max 10 tenant IDs — a Microsoft-imposed
   cap). **Required** as soon as `easyAuthMultiTenant` is true: Microsoft's own docs
   are explicit that a multi-tenant Easy Auth app "doesn't validate which tenant the
   request comes from" on its own — leaving this empty with multi-tenant on means any
   tenant that completes admin consent could sign in.
2. **`app/auth.py`** (code-level, see above) — resolves tenant+group to a specific
   `client_id`. Still needed even with (1): (1) only says *which tenants* may sign in
   at all, not which client each one may query, and a tenant hosting several clients
   still needs the group-level split.

### 1. Create the App Registration (one-time, run yourself — `az` isn't available where Claude ran the rest of this milestone)

```bash
az ad app create --display-name "KnowledgeEngineV9-WebApp-Auth" \
  --sign-in-audience AzureADMyOrg \
  --web-redirect-uris "https://app-knowledgeengine2-v9.azurewebsites.net/.auth/login/aad/callback"
```

Note the returned `appId` — that's `easyAuthClientId` below. (Already done — this
App Registration exists: appId `afdbc8c4-b70b-44b3-b677-8c6e39645e53`.)

### 2. Create a client secret for it

```bash
az ad app credential reset --id <appId> --display-name "easyauth" --years 1
```

Note the returned `password` — that's `easyAuthClientSecret` below. **Never paste it
in a Claude conversation, never commit it** (same rule as the ingestion secret — see
project memory `dxc-internal-track.md`). Keep it only in your shell for the deploy
step (e.g. an env var), or in a password manager.

### 3. Tenant ID

Yassine's sandbox tenant: `6cdc0a5a-779d-4912-a0b3-455e5fc4d9c6` — confirm it's still
right with `az account show --query tenantId -o tsv` before using it (getting this
wrong breaks login silently).

### 4. Jalon 5 additions — run when ready to accept external tenants

**Emit the `groups` claim** (required for the group-level resolution to work at all —
without this, `groups` is simply never present in the token, regardless of what
`app/auth.py` does with it):

```bash
az ad app update --id afdbc8c4-b70b-44b3-b677-8c6e39645e53 \
  --set groupMembershipClaims=SecurityGroup
```

`SecurityGroup` (not `All`) — keeps out Microsoft 365/distribution groups noise,
security groups only, which is what you'd create per client/entity anyway.

**Switch to multi-tenant** (only needed once you're ready to test/demo an external
tenant — leave as `AzureADMyOrg` otherwise, no change needed for the sandbox-only
scenario):

```bash
az ad app update --id afdbc8c4-b70b-44b3-b677-8c6e39645e53 \
  --set signInAudience=AzureADMultipleOrgs
```

⚠️ **Unverified live**: Microsoft's docs give `https://login.microsoftonline.com/organizations/v2.0`
as the default issuer for an "Any Microsoft Entra directory - Multitenant" app
registration, which is what `webapp.bicep` uses when `easyAuthMultiTenant=true`.
Community reports are mixed on whether Easy Auth accepts `/organizations/v2.0`
specifically or only `/common/v2.0` for multi-tenant. **Test this live** the same way
the Jalon 4 Easy Auth rollout was debugged (this environment has no live Azure access
to verify it directly): if login breaks with an issuer-validation error after
deploying with `easyAuthMultiTenant=true`, open `infra/modules/webapp.bicep` and
change the `/organizations/v2.0` issuer to `https://login.microsoftonline.com/common/v2.0`,
then redeploy. `WEBSITE_AUTH_AAD_ALLOWED_TENANTS` still blocks anyone outside
`easyAuthAllowedTenantIds` either way, so switching to `/common/v2.0` (which would
also technically accept personal Microsoft accounts) doesn't weaken the real
boundary.

### 5. Deploy

```bash
az deployment group create -g rg-knowledgeengine-v9 -f infra/main.bicep \
  --parameters easyAuthClientId=afdbc8c4-b70b-44b3-b677-8c6e39645e53 \
               easyAuthTenantId=6cdc0a5a-779d-4912-a0b3-455e5fc4d9c6 \
               easyAuthClientSecret=<the secret value, e.g. from an env var> \
               easyAuthMultiTenant=true \
               easyAuthAllowedTenantIds='["6cdc0a5a-779d-4912-a0b3-455e5fc4d9c6","<external-tenant-id>"]'
```

Leave `easyAuthMultiTenant` and `easyAuthAllowedTenantIds` out entirely (they default
to `false` / `[]`) to keep today's sandbox-only behavior byte-for-byte unchanged.
`enableEasyAuth` defaults to `true` — omit it. To redeploy without the gate (e.g. for
quick local-network testing), pass `enableEasyAuth=false` and you can skip the auth
parameters entirely.

### What this does and doesn't do

- Does: redirect any unauthenticated request to Entra ID login; only tenants in
  `easyAuthAllowedTenantIds` (or, if that's left empty, any tenant that completes
  admin consent) can complete sign-in; only reach a client whose tenant+group
  actually resolves in `app/auth.py`.
- Does not: let a signed-in user pick a client freely — `app/auth.py` decides that
  from claims, never from what the browser posts.
