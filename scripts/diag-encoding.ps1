# Diagnostic v5 : liste simple (comme dans le script principal), filtre
# cote PowerShell (pas cote az) pour eviter toute incertitude sur la
# syntaxe --query, puis verifie le vrai code de caractere en memoire.

[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"

$allNames = az storage blob list `
    --account-name stknowledgeengine3v9 `
    --container-name audio-raw-client-s `
    --auth-mode login `
    --query "[].name" `
    -o tsv

Write-Host "Exit code apres list : $LASTEXITCODE"
Write-Host "Nombre de lignes recues : $($allNames.Count)"
Write-Host ""

$match = $allNames | Where-Object { $_ -like "*131d073d*" }
Write-Host "Ligne trouvee : $match"

if ($null -eq $match) {
    Write-Host "AUCUN MATCH TROUVE" -ForegroundColor Red
} else {
    Write-Host "Longueur (caracteres) : $($match.Length)"
    Write-Host ""
    Write-Host "Codes de caracteres (hex UTF-16) :"
    $match.ToCharArray() | ForEach-Object { Write-Host ('{0:X4} ' -f [int]$_) -NoNewline }
    Write-Host ""
    Write-Host ""
    Write-Host "Contient U+00B0 (degree sign correct) : $($match.Contains([char]0x00B0))"
    Write-Host "Contient U+FFFD (replacement char = corrompu) : $($match.Contains([char]0xFFFD))"
}
