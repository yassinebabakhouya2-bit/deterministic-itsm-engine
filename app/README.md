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
