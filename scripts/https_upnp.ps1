# Keep one UPnP TCP mapping for the HTTPS edge; the LAN address and port live in data/https.json:
#   "upnp": {"internal_client": "192.0.2.10", "port": 38443, "description": "AnimeService-HTTPS"}
# A mapping on the same external port that belongs to something else is preserved, never replaced.
$ErrorActionPreference = 'Stop'
$configPath = Join-Path (Split-Path -Parent $PSScriptRoot) 'data\https.json'
$config = (Get-Content -LiteralPath $configPath -Raw -Encoding UTF8 | ConvertFrom-Json).upnp
if (-not $config -or -not $config.internal_client) { throw 'data/https.json needs upnp.internal_client' }
$port = if ($config.port) { [int]$config.port } else { 38443 }
$description = if ($config.description) { [string]$config.description } else { 'AnimeService-HTTPS' }
$nat = New-Object -ComObject HNetCfg.NATUPnP
$mappings = $nat.StaticPortMappingCollection
if ($null -eq $mappings) { throw 'UPnP unavailable' }
$existing = @($mappings | Where-Object { $_.ExternalPort -eq $port -and $_.Protocol -eq 'TCP' })
if ($existing.Count) {
    if ($existing[0].InternalClient -ne $config.internal_client -or $existing[0].InternalPort -ne $port -or $existing[0].Description -ne $description) {
        Write-Output "Port $port belongs to another mapping; preserved"
        exit 0
    }
} else {
    $null = $mappings.Add($port, 'TCP', $port, $config.internal_client, $true, $description)
}
Write-Output "TCP $port -> $port ready"
