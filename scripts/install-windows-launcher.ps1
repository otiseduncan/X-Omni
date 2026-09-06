$ErrorActionPreference = 'Stop'
$root = [IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))
$launcher = Join-Path $PSScriptRoot 'launch-x-omni.ps1'
$icon = Join-Path $root 'assets\launcher\x-omni.ico'
$desktop = [Environment]::GetFolderPath('Desktop')
$shortcutPath = Join-Path $desktop 'X Omni.lnk'
$powershell = "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"

foreach ($required in @($launcher, $icon, $powershell)) {
    if (-not (Test-Path -LiteralPath $required)) {
        throw "Required launcher file is missing: $required"
    }
}

$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($shortcutPath)
$shortcut.TargetPath = $powershell
$shortcut.Arguments = "-NoLogo -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$launcher`""
$shortcut.WorkingDirectory = $root
$shortcut.IconLocation = "$icon,0"
$shortcut.Description = 'Launch X Omni and wait for the local model'
$shortcut.Save()

Write-Host "Installed X Omni launcher: $shortcutPath" -ForegroundColor Green
