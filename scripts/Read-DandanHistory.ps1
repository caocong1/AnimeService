param([Parameter(Mandatory=$true)][string]$Database, [Parameter(Mandatory=$true)][string]$Library)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
Add-Type -Path $Library
$cs = [LiteDB.ConnectionString]::new()
$cs.Filename = $Database
$cs.ReadOnly = $true
$db = [LiteDB.LiteDatabase]::new($cs, $null)
function IsoDate($value) {
    if ($value.IsDateTime -and $value.AsDateTime.Year -ge 2000) { return $value.AsDateTime.ToUniversalTime().ToString('o') }
    return $null
}
try {
    $names = @($db.GetCollectionNames())
    foreach ($required in @('recentplays','libraryfiles','bangumiepisodehistories')) {
        if ($required -notin $names) { throw 'Unsupported history schema' }
    }
    $recent = @(foreach ($r in $db.GetCollection('recentplays').FindAll()) {
        [ordered]@{path=$r['FilePath'].AsString; played=IsoDate $r['LastPlay']; seconds=$r['ExitTime'].RawValue; fraction=$r['Position'].RawValue}
    })
    $files = @(foreach ($r in $db.GetCollection('libraryfiles').FindAll()) {
        [ordered]@{path=$r['Path'].AsString; size=$r['Size'].RawValue; duration=$r['Duration'].RawValue; episode_id=[string]$r['EpisodeId'].RawValue}
    })
    $histories = @($db.GetCollection('bangumiepisodehistories').FindAll())
    $accounts = @($histories | ForEach-Object { [string]$_['UserId'].RawValue } | Where-Object { $_ -and $_ -ne '0' } | Sort-Object -Unique)
    $accountScope = 'anonymous'
    $chosen = '0'
    if ($accounts.Count -eq 1) { $chosen=$accounts[0]; $accountScope='single_account' }
    if ($accounts.Count -gt 1) { $chosen=$null; $accountScope='ambiguous' }
    $seen = @(foreach ($r in $histories) {
        # Never combine watched flags belonging to different cached accounts.
        if ($null -eq $chosen -or [string]$r['UserId'].RawValue -ne $chosen) { continue }
        foreach ($ep in $r['Episodes'].AsArray) {
            $watched = IsoDate $ep['LastWatched']
            if ($watched) { [ordered]@{episode_id=[string]$ep['EpisodeId'].RawValue; watched=$watched} }
        }
    })
    [ordered]@{schema=1; account_scope=$accountScope; recent=$recent; files=$files; seen=$seen} | ConvertTo-Json -Depth 6 -Compress
} finally {
    # LiteDB 5.0.17 attempts a checkpoint on close, even with ReadOnly=true.
    # The database is an isolated copy; keep the source and copy read-only.
    try { $db.Dispose() } catch {
        if ($_.Exception.GetBaseException() -isnot [System.NotSupportedException]) { throw }
    }
}
