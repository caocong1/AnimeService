$ErrorActionPreference = 'Stop'
$projectRoot = $PSScriptRoot
$runtime = Join-Path $projectRoot '.venv\Scripts\pythonw.exe'
if (-not (Test-Path -LiteralPath $runtime)) { throw 'Missing project runtime. Run Install-AnimeService.ps1 first.' }
Start-Process -FilePath $runtime -ArgumentList ('"' + (Join-Path $projectRoot 'scripts\supervisor.py') + '"') -WorkingDirectory $projectRoot -WindowStyle Hidden
