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

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\scripts\configure-frigate.ps1 -Username x-omni

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\scripts\configure-frigate.ps1 -Clear
#>
[CmdletBinding()]
param(
    [string]$Username,
    [switch]$Clear
)

$ErrorActionPreference = 'Stop'

$repoRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $repoRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) {
    throw "X Omni's virtual environment was not found at $python"
}

if ($Clear) {
    & $python -c @'
import sys
sys.path.insert(0, r".")
from core.config import Settings
from core.services.frigate_client import credential_store
store = credential_store(Settings.load().frigate_credential_path)
print("removed" if store.clear() else "nothing was registered")
'@
    exit $LASTEXITCODE
}

if (-not $Username) {
    $Username = Read-Host 'Frigate username for X Omni'
}
$Username = $Username.Trim()
if (-not $Username) { throw 'A Frigate username is required.' }

$secure = Read-Host "Frigate password for '$Username'" -AsSecureString
$plain = [System.Runtime.InteropServices.Marshal]::PtrToStringBSTR(
    [System.Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
)
if (-not $plain) { throw 'A Frigate password is required.' }

try {
    # The password reaches Python on stdin, never as an argument.
    $script = @'
import sys
sys.path.insert(0, r".")
from core.config import Settings
from core.services.frigate_client import credential_store

username = sys.stdin.readline().rstrip("\n")
password = sys.stdin.readline().rstrip("\n")
settings = Settings.load()
store = credential_store(settings.frigate_credential_path)
store.save({"username": username, "password": password})
print(f"Sealed the Frigate credential for {username!r} at {store.path}")
print("Base URL:", settings.frigate_base_url or "(FRIGATE_BASE_URL is not set yet)")
print("Camera:", settings.frigate_camera)
'@
    Push-Location $repoRoot
    try {
        "$Username`n$plain" | & $python -c $script
    } finally {
        Pop-Location
    }
} finally {
    $plain = $null
    [System.GC]::Collect()
}
