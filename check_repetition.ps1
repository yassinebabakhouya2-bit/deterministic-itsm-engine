$token = az account get-access-token --resource https://management.azure.com --query accessToken -o tsv
$headers = @{ Authorization = "Bearer $token" }
$sub = az account show --query id -o tsv
$runId = "08584112342760430379361095059CU17"
$url = "https://management.azure.com/subscriptions/$sub/resourceGroups/rg-knowledgeengine-v9/providers/Microsoft.Logic/workflows/logic-ingest-client-s/runs/$runId/actions/For_each/scopeRepetitions/000385/actions?api-version=2019-05-01"
$resp = Invoke-RestMethod -Method Get -Uri $url -Headers $headers
$resp | ConvertTo-Json -Depth 10
