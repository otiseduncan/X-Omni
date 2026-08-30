# Registers the X DVR GUI to start automatically, silently, at every Windows
# logon -- independent of X Omni Core and of MediaMTX's own startup.
#
#   cd "X:\X Omni"
#   .\scripts\install-x-dvr-startup.ps1
#
# To remove: delete "X DVR.lnk" from shell:startup (Win+R -> shell:startup).

$ErrorActionPreference = 'Stop'
$root = [IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))
$launcher = Join-Path $PSScriptRoot 'launch-x-dvr.ps1'
$startupFolder = [Environment]::GetFolderPath('Startup')
$shortcutPath = Join-Path $startupFolder 'X DVR.lnk'
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
$shortcut.Description = 'Start the X DVR GUI at logon (MediaMTX owns recording independently)'
$shortcut.Save()

Write-Host "X DVR GUI will now start automatically at logon: $shortcutPath" -ForegroundColor Green
