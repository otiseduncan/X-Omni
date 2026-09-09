$ErrorActionPreference = 'Stop'
$root = [IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))
$launcher = Join-Path $PSScriptRoot 'launch-mediamtx.ps1'
$icon = Join-Path $root 'assets\launcher\x-omni.ico'
$desktop = [Environment]::GetFolderPath('Desktop')
$shortcutPath = Join-Path $desktop 'MediaMTX.lnk'
$powershell = "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"

foreach ($required in @($launcher, $icon, $powershell)) {
    if (-not (Test-Path -LiteralPath $required)) {
        throw "Required launcher file is missing: $required"
    }
}

# The same guard install-windows-launcher.ps1 already applies. A desktop icon
# pointing at a script with a syntax error fails at double-click time, in a
# hidden window, with nothing to read -- so refuse to install the shortcut
# rather than hand over one that cannot work.
$tokens = $null
$parseErrors = $null
[void][System.Management.Automation.Language.Parser]::ParseFile(
    $launcher,
    [ref]$tokens,
    [ref]$parseErrors
)
if ($parseErrors.Count -gt 0) {
    $detail = ($parseErrors | ForEach-Object { $_.ToString() }) -join [Environment]::NewLine
    throw "MediaMTX launcher source does not parse cleanly. Shortcut was not installed.`n$detail"
}

$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($shortcutPath)
$shortcut.TargetPath = $powershell
$shortcut.Arguments = "-NoLogo -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$launcher`""
$shortcut.WorkingDirectory = $root
$shortcut.IconLocation = "$icon,0"
$shortcut.Description = 'Start MediaMTX (exterior camera recording/streaming)'
$shortcut.Save()

Write-Host "Installed MediaMTX launcher: $shortcutPath" -ForegroundColor Green
