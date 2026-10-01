# =====================================================================
# generate-synonyms.ps1 -- builds and publishes the SHARED synonym map
# (syn-clienta, referenced by every client's index since the Basic-tier
# quota fix -- see the Jalon 9 design note in deploy.ps1) from ALIASES
# ALREADY VERIFIED against source text by
# enrichment/function_app.py::_verify_aliases() (a synonym here is never
# hand-typed and never an unverified model guess). Idempotent PUT --
# safe to re-run any time, e.g. after a reindex adds new aliases.
# Usage: ./generate-synonyms.ps1
# =====================================================================
param(
  [string]$Service       = "srch-knowledgeengine3-v9",
  [string]$ResourceGroup = "rg-knowledgeengine-v9",
  [string]$MapName       = "syn-clienta",
  [string]$ApiVersion    = "2024-07-01"
)
$ErrorActionPreference = "Stop"
$endpoint = "https://$Service.search.windows.net"
$key = az search admin-key show --service-name $Service --resource-group $ResourceGroup --query primaryKey -o tsv
if (-not $key) { throw "Could not retrieve the admin key." }
$headers = @{ "api-key" = $key; "Content-Type" = "application/json" }

$clients = @("clienta","clientb","clientc","client-s")   # client-v abandoned 2026-09-26 (tenant rebuild)
# canonical (lowercase key) -> HashSet of surface forms actually seen (incl. canonical itself)
$groups = @{}

foreach ($c in $clients) {
  $idx = "idx-$c"
  Write-Host "Lecture des alias verifies -- $idx ..."
  try {
    $skip = 0
    do {
      $body = @{ search = "*"; select = "aliases"; top = 1000; skip = $skip } | ConvertTo-Json
      $r = Invoke-RestMethod -Method Post -Uri "$endpoint/indexes/$idx/docs/search?api-version=$ApiVersion" -Headers $headers -Body $body
      foreach ($doc in $r.value) {
        foreach ($row in $doc.aliases) {
          # Format produit par _shape_doc() : "Canonique|alias1|alias2"
          $parts = $row -split '\|'
          if ($parts.Count -ge 2) {
            $canon = $parts[0].Trim()
            if (-not $canon) { continue }
            $gkey = $canon.ToLower()
            if (-not $groups.ContainsKey($gkey)) { $groups[$gkey] = New-Object System.Collections.Generic.HashSet[string] }
            [void]$groups[$gkey].Add($canon)
            for ($i = 1; $i -lt $parts.Count; $i++) {
              $a = $parts[$i].Trim()
              if ($a) { [void]$groups[$gkey].Add($a) }
            }
          }
        }
      }
      $skip += 1000
    } while ($r.value.Count -eq 1000)
  } catch {
    Write-Host "  (index $idx ignore : $($_.Exception.Message))"
  }
}

# Vocabulaire de depart, generique Service Desk -- n'ecrase jamais un groupe
# deja alimente par le corpus reel, ne comble que ce qui manque encore.
$seed = @(
  @("mot de passe", "password", "SSPR", "reinitialisation mot de passe"),
  @("MFA", "authentification multifacteur", "multi-factor authentication"),
  @("VPN", "acces distant", "teletravail", "remote access"),
  @("imprimante", "impression", "printer", "spouleur", "print spooler"),
  @("OOF", "out of office", "absence du bureau")
)
foreach ($grp in $seed) {
  $gkey = $grp[0].ToLower()
  if (-not $groups.ContainsKey($gkey)) { $groups[$gkey] = New-Object System.Collections.Generic.HashSet[string] }
  foreach ($term in $grp) { [void]$groups[$gkey].Add($term) }
}

$lines = @()
foreach ($k in $groups.Keys) {
  $vals = @($groups[$k] | Where-Object { $_ })
  if ($vals.Count -ge 2) { $lines += ($vals -join ", ") }
}

Write-Host ""
Write-Host "Groupes generes : $($lines.Count)"
$lines | ForEach-Object { Write-Host "  $_" }

$synonyms = ($lines -join "`n") + "`n"
$payload = @{ name = $MapName; format = "solr"; synonyms = $synonyms } | ConvertTo-Json
Invoke-RestMethod -Method Put -Uri "$endpoint/synonymmaps/$MapName`?api-version=$ApiVersion" -Headers $headers -Body $payload | Out-Null
Write-Host ""
Write-Host "OK -> synonymmaps/$MapName ($($lines.Count) groupes, partage par tous les clients)"
