$ErrorActionPreference = 'Stop'
$projectRoot = $PSScriptRoot
if (-not (Test-Path -LiteralPath (Join-Path $projectRoot '.venv\Scripts\python.exe'))) { python -m venv (Join-Path $projectRoot '.venv') }
& (Join-Path $projectRoot '.venv\Scripts\python.exe') -m pip install -r (Join-Path $projectRoot 'requirements.txt')
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed' }
# Per-user Startup shortcut works without admin, password, or changes to existing service tasks.
$shellObject = New-Object -ComObject WScript.Shell
$startupDirectory = [Environment]::GetFolderPath('Startup')
$shortcut = $shellObject.CreateShortcut((Join-Path $startupDirectory 'AnimeService.lnk'))
$shortcut.TargetPath = Join-Path $projectRoot '.venv\Scripts\pythonw.exe'
$shortcut.Arguments = '"' + (Join-Path $projectRoot 'scripts\supervisor.py') + '"'
$shortcut.WorkingDirectory = $projectRoot
$shortcut.WindowStyle = 7
$shortcut.Description = 'Local AnimeService background and restart supervisor'
$shortcut.Save()
$desktopDirectory = [Environment]::GetFolderPath('Desktop')
$entry = $shellObject.CreateShortcut((Join-Path $desktopDirectory 'AnimeService.lnk'))
$entry.TargetPath = Join-Path $projectRoot 'Open-AnimeService.cmd'
$entry.WorkingDirectory = $projectRoot
$entry.Description = 'Open local anime tracking home'
$entry.Save()
& (Join-Path $projectRoot 'Start-AnimeService.ps1')
Write-Output 'Installed: per-user login startup and desktop shortcut. URL: http://127.0.0.1:4871'
