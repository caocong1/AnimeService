@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "%~dp0Start-AnimeService.ps1"
start "" "http://127.0.0.1:4871"
