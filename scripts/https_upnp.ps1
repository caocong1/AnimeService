# Mapping conflicts are preserved and fail; no private values or COM errors are printed.
param(
    [string]$ConfigPath = (Join-Path (Split-Path -Parent $PSScriptRoot) 'data\https.json'),
    [switch]$LibraryOnly
)
$ErrorActionPreference = 'Stop'

function Get-HttpsMappingConfig($Config) {
    $ip = $null
    if (-not $Config -or -not [System.Net.IPAddress]::TryParse([string]$Config.internal_client, [ref]$ip) -or
        $ip.AddressFamily -ne [System.Net.Sockets.AddressFamily]::InterNetwork) { throw 'invalid_mapping_config' }
    $port = 38443
    if ($null -ne $Config.port) {
        if (($Config.port -isnot [int] -and $Config.port -isnot [long]) -or $Config.port -lt 1 -or $Config.port -gt 65535) {
            throw 'invalid_mapping_config'
        }
        $port = [int]$Config.port
    }
    $description = 'AnimeService-HTTPS'
    if ($null -ne $Config.description) {
        if ($Config.description -isnot [string] -or $Config.description.Length -lt 1 -or
            $Config.description.Length -gt 64 -or $Config.description -match '[\p{C}]') { throw 'invalid_mapping_config' }
        $description = $Config.description
    }
    return [pscustomobject]@{client=$ip.ToString(); port=$port; description=$description}
}

function Get-HttpsMappingAction($Mappings, $Config) {
    $existing = @($Mappings | Where-Object { $_.ExternalPort -eq $Config.port -and $_.Protocol -eq 'TCP' })
    if (-not $existing.Count) { return 'add' }
    if ($existing.Count -ne 1 -or $existing[0].InternalClient -ne $Config.client -or
        $existing[0].InternalPort -ne $Config.port -or $existing[0].Description -ne $Config.description -or
        -not $existing[0].Enabled) { return 'conflict' }
    return 'ready'
}

if (-not $LibraryOnly) {
    try {
        $settings = Get-Content -LiteralPath $ConfigPath -Raw -Encoding UTF8 | ConvertFrom-Json
        $config = Get-HttpsMappingConfig $settings.upnp
        $nat = New-Object -ComObject HNetCfg.NATUPnP
        $mappings = $nat.StaticPortMappingCollection
        if ($null -eq $mappings) { throw 'mapping_unavailable' }
        $action = Get-HttpsMappingAction $mappings $config
        if ($action -eq 'conflict') { Write-Output 'mapping_conflict'; exit 3 }
        if ($action -eq 'add') {
            $null = $mappings.Add($config.port, 'TCP', $config.port, $config.client, $true, $config.description)
        }
        Write-Output 'mapping_ready'
    } catch {
        Write-Output 'mapping_setup_failed'
        exit 1
    }
}
