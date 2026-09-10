# Ingestion — SharePoint → Blob (per client)

A single Logic App template (Bicep), deployed once per client. Onboarding a
new client means a new parameters file — never a template change (axiom
A2, client-agnosticism).

## Design

Zero connectors: every step is a native `Http` action authenticated via the
Logic App's system-assigned managed identity (`ManagedServiceIdentity`
audience `https://vault.azure.net` for the Key Vault read, `https://storage.azure.com`
for the Blob write). No `Microsoft.Web/connections` resource, no secret
outside Key Vault, no custom code — consistent with the project's "zero
in-house code" principle for anything Azure already does natively.

## What it does

1. Reads the ingestion App Registration's client secret from Key Vault.
2. Acquires a Microsoft Graph token (client-credentials flow).
3. Lists every file at the client's SharePoint site — a bounded `Until`
   loop follows Graph's `@odata.nextLink` so libraries over the default
   200-item page size are still ingested in full.
4. Downloads each file in two steps: `GET .../drive/items/{id}?$select=@microsoft.graph.downloadUrl`
   (authenticated) then a plain `GET` on the returned URL (unauthenticated).
   Graph's `/content` endpoint 302-redirects larger files to a
   pre-authenticated download URL off `graph.microsoft.com`, and Logic Apps
   won't forward a Bearer token across that redirect — this two-step
   download is the fix.
5. Writes each file to the client's Blob container (`kb-<clientCode>`),
   tagging it with an `x-ms-meta-clientid` header — read downstream by the
   Azure AI Search skillset to project the index's `clientId` field
   (defense in depth alongside the dedicated per-client index).

## Prerequisite (one-time, per client, not IaC)

`Sites.Selected` Graph permission granted to the App Registration on the
client's SharePoint site — a Graph API call, not an Azure resource, so it
lives outside Bicep:

```
POST https://graph.microsoft.com/v1.0/sites/{siteId}/permissions
```

## Deploy

```bash
az deployment group what-if \
  --resource-group rg-knowledgeengine-v9 \
  --template-file main.bicep \
  --parameters your-client.parameters.json

az deployment group create \
  --resource-group rg-knowledgeengine-v9 \
  --template-file main.bicep \
  --parameters your-client.parameters.json
```

`example.parameters.json` shows the shape (`?$top=1` in `listQuery` as a
safe single-file smoke test before a full run — set it to `""` for
production). Real per-client parameter files — with real site IDs — are
never committed here; keep them in your own local/private location.

## Troubleshooting

A stuck or timed-out Azure AI Search indexer on the resulting Blob content
is a downstream concern, not an ingestion one — see `search/README.md`.
