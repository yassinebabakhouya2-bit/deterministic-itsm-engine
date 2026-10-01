$sub = az account show --query id -o tsv
$runId = "08584112342760430379361095059CU17"
$url = "https://management.azure.com/subscriptions/$sub/resourceGroups/rg-knowledgeengine-v9/providers/Microsoft.Logic/workflows/logic-ingest-client-s/runs/$runId/actions/For_each/scopeRepetitions?api-version=2019-05-01"
$failures = @()
while ($url) {
    $resp = az rest --method get --url $url | ConvertFrom-Json
    $failures += $resp.value | Where-Object { $_.properties.status -ne "Succeeded" }
    $url = $resp.nextLink
}
Write-Output "Failures found: $($failures.Count)"
$failures | ForEach-Object { "$($_.name) : $($_.properties.status)" }
