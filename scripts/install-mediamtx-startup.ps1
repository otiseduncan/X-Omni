# Registers MediaMTX to start automatically, silently, at every Windows
# logon -- independent of X Omni Core or the X DVR GUI's own startup.
#
#   cd "X:\X Omni"
#   .\scripts\install-mediamtx-startup.ps1
#
# To remove: delete "MediaMTX.lnk" from shell:startup (Win+R -> shell:startup).

$ErrorActionPreference = 'Stop'
$root = [IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))
$launcher = Join-Path $PSScriptRoot 'launch-mediamtx.ps1'
$startupFolder = [Environment]::GetFolderPath('Startup')
$shortcutPath = Join-Path $startupFolder 'MediaMTX.lnk'
$powershell = "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"

foreach ($required in @($launcher, $powershell)) {
    if (-not (Test-Path -LiteralPath $required)) {
        throw "Required launcher file is missing: $required"
    }
}

$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($shortcutPath)
$shortcut.TargetPath = $powershell
# -NoOpen: logon startup should not pop a browser tab every login.
$shortcut.Arguments = "-NoLogo -NoProfile -WindowStyle Hidden -File `"$launcher`" -NoOpen"
$shortcut.WorkingDirectory = $root
$shortcut.Description = 'Start MediaMTX (exterior camera recording/streaming) at logon'
$shortcut.Save()

Write-Host "MediaMTX will now start automatically at logon: $shortcutPath" -ForegroundColor Green
