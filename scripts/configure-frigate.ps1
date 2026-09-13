<#
.SYNOPSIS
    Register X Omni's Frigate account password on this machine, sealed with Windows DPAPI.

.DESCRIPTION
    X talks to Frigate's authenticated API on port 8971 using a dedicated
    Frigate account. That account's password is never stored in git, in .env,
    in a log, or in an API response: this script seals it with Windows DPAPI
    for the current Windows user, exactly the way the exterior camera's
    credential used to be stored.

    The password is read as a SecureString, so it is never typed on a command
    line, never appears in the PowerShell history, and never lands in a
    process-listing argument.

    Set FRIGATE_BASE_URL separately (config/.env.local), for example:

        FRIGATE_BASE_URL=https://192.168.1.201:8971
        FRIGATE_CAMERA=exterior

    Use the LAN address or mDNS name while both machines are on the same
    network. A Tailscale name works here too, with no code change -- it is
    just a different value for the same setting.

.PARAMETER Username
    The Frigate account X should sign in as. Give it the least privilege that
    works -- viewer, or a custom role limited to the exterior camera.

.PARAMETER Clear
    Remove the stored credential instead of registering one.

.PARAMETER BaseUrl
    Authenticated Frigate origin. Defaults to FRIGATE_BASE_URL.

.PARAMETER Camera
    Logical Frigate camera name. Defaults to FRIGATE_CAMERA (exterior).

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\scripts\configure-frigate.ps1 -Username x-omni

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\scripts\configure-frigate.ps1 -Clear
#>
[CmdletBinding()]
param(
    [string]$Username,
    [string]$BaseUrl,
    [string]$Camera,
    [switch]$Clear
)

$ErrorActionPreference = 'Stop'

$repoRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $repoRoot '.venv\Scripts\python.exe'
$helper = Join-Path $PSScriptRoot 'configure_frigate.py'
if (-not (Test-Path -LiteralPath $python)) {
    throw "X Omni's virtual environment was not found at $python"
}

if ($Clear) {
    & $python $helper --clear
    exit $LASTEXITCODE
}

if (-not $Username) {
    $Username = Read-Host 'Frigate username for X Omni'
}
$Username = $Username.Trim()
if (-not $Username) { throw 'A Frigate username is required.' }

$secure = Read-Host "Frigate password for '$Username'" -AsSecureString
$bstr = [IntPtr]::Zero
$plain = $null

try {
    $bstr = [System.Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    $plain = [System.Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
    if (-not $plain) { throw 'A Frigate password is required.' }
    $arguments = @($helper, '--username', $Username)
    if ($BaseUrl) { $arguments += @('--base-url', $BaseUrl) }
    if ($Camera) { $arguments += @('--camera', $Camera) }
    Push-Location $repoRoot
    try {
        # Password reaches the helper only on stdin, never in an argument,
        # process listing, environment variable, log line, or shell history.
        $plain | & $python @arguments
        $exitCode = $LASTEXITCODE
    } finally {
        Pop-Location
    }
    exit $exitCode
} finally {
    if ($bstr -ne [IntPtr]::Zero) {
        [System.Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
    }
    $plain = $null
    $secure = $null
    [System.GC]::Collect()
}
