$token = az account get-access-token --resource https://management.azure.com --query accessToken -o tsv
$headers = @{ Authorization = "Bearer $token" }
$sub = az account show --query id -o tsv
$runId = "08584112342760430379361095059CU17"
$url = "https://management.azure.com/subscriptions/$sub/resourceGroups/rg-knowledgeengine-v9/providers/Microsoft.Logic/workflows/logic-ingest-client-s/runs/$runId/actions/For_each/scopeRepetitions?api-version=2019-05-01"
$failures = @()
$page = 0
while ($url) {
    $page++
    $resp = Invoke-RestMethod -Method Get -Uri $url -Headers $headers
    Write-Output "Page $page : $($resp.value.Count) entrees"
    $failures += $resp.value | Where-Object { $_.properties.status -ne "Succeeded" }
    $url = $resp.nextLink
}
Write-Output "Failures found: $($failures.Count)"
$failures | ConvertTo-Json -Depth 10
