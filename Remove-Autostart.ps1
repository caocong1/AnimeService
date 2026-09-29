$startupLink = Join-Path ([Environment]::GetFolderPath('Startup')) 'AnimeService.lnk'
if (Test-Path -LiteralPath $startupLink) {
    $shortcut = (New-Object -ComObject WScript.Shell).CreateShortcut($startupLink)
    if ($shortcut.TargetPath -eq (Join-Path $PSScriptRoot '.venv\Scripts\pythonw.exe')) { Remove-Item -LiteralPath $startupLink }
}
Write-Output 'Removed only this project login startup. Program, data, downloads and desktop entry remain.'
