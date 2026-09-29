$ErrorActionPreference = 'Stop'
New-Item -ItemType Directory -Force (Join-Path $PSScriptRoot 'data') | Out-Null
Set-Content -LiteralPath (Join-Path $PSScriptRoot 'data\stop.request') -Value 'User requested stop'
& (Join-Path $PSScriptRoot '.venv\Scripts\python.exe') (Join-Path $PSScriptRoot 'scripts\stop_danmu.py')
& (Join-Path $PSScriptRoot '.venv\Scripts\python.exe') (Join-Path $PSScriptRoot 'scripts\stop_https.py')
Write-Output 'AnimeService will stop within a few seconds. Downloads and media files are untouched.'
