# Reads .env and pushes the needed values to Fly as secrets (one deploy).
# DATABASE_PATH is intentionally excluded - fly.toml sets it to the volume path.
# Run this AFTER `fly launch --no-deploy` (so the app exists), then `fly deploy`.

$ErrorActionPreference = "Stop"
$env:PATH = "$HOME\.fly\bin;$env:PATH"

$exclude = @("DATABASE_PATH", "OPENAI_BASE_URL")
$pairs = @()
foreach ($line in Get-Content ".env") {
    $t = $line.Trim()
    if ($t -eq "" -or $t.StartsWith("#")) { continue }
    $idx = $t.IndexOf("=")
    if ($idx -lt 1) { continue }
    $key = $t.Substring(0, $idx).Trim()
    $val = $t.Substring($idx + 1).Trim()
    if ($exclude -contains $key -or $val -eq "") { continue }
    $pairs += "$key=$val"
}

if ($pairs.Count -eq 0) { throw "No secrets found in .env" }
Write-Host "Setting $($pairs.Count) Fly secrets:" ($pairs | ForEach-Object { ($_ -split "=")[0] })
fly secrets set @pairs
