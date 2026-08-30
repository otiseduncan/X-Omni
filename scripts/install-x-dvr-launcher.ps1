$ErrorActionPreference = 'Stop'
$root = [IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))
$launcher = Join-Path $PSScriptRoot 'launch-x-dvr.ps1'
$icon = Join-Path $root 'assets\launcher\x-omni.ico'
$desktop = [Environment]::GetFolderPath('Desktop')
$shortcutPath = Join-Path $desktop 'X DVR.lnk'
$powershell = "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"

foreach ($required in @($launcher, $icon, $powershell)) {
    if (-not (Test-Path -LiteralPath $required)) {
        throw "Required launcher file is missing: $required"
    }
}

$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($shortcutPath)
$shortcut.TargetPath = $powershell
$shortcut.Arguments = "-NoLogo -NoProfile -WindowStyle Hidden -File `"$launcher`""
$shortcut.WorkingDirectory = $root
$shortcut.IconLocation = "$icon,0"
$shortcut.Description = 'Open the standalone X DVR -- independent of X Omni'
$shortcut.Save()

Write-Host "Installed X DVR launcher: $shortcutPath" -ForegroundColor Green
