[CmdletBinding()]
param(
    [string]$XOmniRoot = 'X:\X Omni',
    [string]$CalibrationIQRoot = 'X:\Calibration IQ',
    [string]$ScrapeXRoot = 'X:\ScrapeX',
    [switch]$Open
)

$ErrorActionPreference = 'Stop'

function Write-Step {
    param([Parameter(Mandatory)][string]$Text)
    Write-Host ""
    Write-Host "=== $Text ===" -ForegroundColor Cyan
}

function Assert-Command {
    param([Parameter(Mandatory)][string]$Name)
    if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) {
        throw "Required command '$Name' is not available on PATH."
    }
}

function Assert-Repository {
    param([Parameter(Mandatory)][string]$Path, [Parameter(Mandatory)][string]$Name)
    if (-not (Test-Path -LiteralPath $Path)) {
        throw "$Name repository was not found at $Path."
    }
    $inside = & git -C $Path rev-parse --is-inside-work-tree 2>$null
    if ($LASTEXITCODE -ne 0 -or ([string]$inside).Trim() -ne 'true') {
        throw "$Name path is not a Git working tree: $Path"
    }
}

function Get-Revision {
    param([Parameter(Mandatory)][string]$Path)
    $output = & git -C $Path rev-parse HEAD 2>$null
    if ($LASTEXITCODE -ne 0) { return $null }
    $revision = ([string]($output | Select-Object -First 1)).Trim().ToLowerInvariant()
    if ($revision -match '^[0-9a-f]{40}$') { return $revision }
    return $null
}

function Pull-Main {
    param([Parameter(Mandatory)][string]$Path, [Parameter(Mandatory)][string]$Name)
    Write-Step "Pulling $Name"
    & git -C $Path pull --ff-only origin main
    if ($LASTEXITCODE -ne 0) {
        throw "$Name git pull failed."
    }
    $revision = Get-Revision -Path $Path
    Write-Host "$Name revision: $revision" -ForegroundColor Green
    return $revision
}

function Get-XCorePort {
    param([Parameter(Mandatory)][string]$Root)
    $port = 8100
    $envFile = Join-Path $Root 'config\.env.local'
    if (Test-Path -LiteralPath $envFile) {
        $line = Get-Content -LiteralPath $envFile |
            Where-Object { $_ -match '^\s*XOMNI_PORT\s*=\s*(\d+)\s*$' } |
            Select-Object -Last 1
        if ($line -and $line -match '=\s*(\d+)\s*$') {
            $port = [int]$matches[1]
        }
    }
    if ($env:XOMNI_PORT -match '^\d+$') {
        $port = [int]$env:XOMNI_PORT
    }
    return $port
}

function Invoke-HealthJson {
    param([Parameter(Mandatory)][string]$Url)
    $response = Invoke-WebRequest -UseBasicParsing -Uri $Url -TimeoutSec 8
    if ([int]$response.StatusCode -ne 200) {
        throw "Health check returned HTTP $($response.StatusCode): $Url"
    }
    return ($response.Content | ConvertFrom-Json)
}

Assert-Command -Name 'git'
Assert-Command -Name 'powershell.exe'
Assert-Repository -Path $XOmniRoot -Name 'X Omni'
Assert-Repository -Path $CalibrationIQRoot -Name 'Calibration IQ'
Assert-Repository -Path $ScrapeXRoot -Name 'ScrapeX'

# Pull all three first so every service is deployed from one coherent checkout set.
$ciqRevision = Pull-Main -Path $CalibrationIQRoot -Name 'Calibration IQ'
$scrapexRevision = Pull-Main -Path $ScrapeXRoot -Name 'ScrapeX'
$xRevision = Pull-Main -Path $XOmniRoot -Name 'X Omni'

Write-Step 'Refreshing ScrapeX standalone runtime'
& (Join-Path $ScrapeXRoot 'scripts\install.ps1')
if ($LASTEXITCODE -ne 0) {
    throw 'ScrapeX install/validation failed.'
}

Write-Step 'Deploying Calibration IQ native Production'
& (Join-Path $CalibrationIQRoot 'native\scripts\Start-Native.ps1') -Profile Production -NoBrowser
if ($LASTEXITCODE -ne 0) {
    throw 'Calibration IQ native deployment failed.'
}

Write-Step 'Refreshing X Omni dependencies and UI'
& (Join-Path $XOmniRoot 'scripts\setup.ps1')
if ($LASTEXITCODE -ne 0) {
    throw 'X Omni setup/build failed.'
}

Write-Step 'Restarting X Omni from current source'
& (Join-Path $XOmniRoot 'scripts\launch-x-omni.ps1') -NoOpen
if ($LASTEXITCODE -ne 0) {
    throw 'X Omni launch failed.'
}

Write-Step 'Synchronizing ScrapeX running process to current source'
$xPython = Join-Path $XOmniRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $xPython)) {
    throw "X Omni Python runtime is missing: $xPython"
}
$scrapexBootstrap = @'
import asyncio
import json
from core.config import Settings
from core.services import scrapex

result = asyncio.run(scrapex.start_native(Settings.load()))
print(json.dumps(result, sort_keys=True))
raise SystemExit(0 if result.get("success") is True else 1)
'@
Push-Location $XOmniRoot
try {
    & $xPython -c $scrapexBootstrap
    if ($LASTEXITCODE -ne 0) {
        throw 'X Omni could not synchronize ScrapeX to its current checkout.'
    }
} finally {
    Pop-Location
}

Write-Step 'Verifying deployed revisions'
$xPort = Get-XCorePort -Root $XOmniRoot
$xHealth = Invoke-HealthJson -Url "http://127.0.0.1:$xPort/healthz"
$runningXRevision = [string]$xHealth.source_revision
if ($xRevision -and -not $runningXRevision.Equals($xRevision, [StringComparison]::OrdinalIgnoreCase)) {
    throw "X Omni is healthy but running revision '$runningXRevision' instead of '$xRevision'."
}

$ciqHealth = Invoke-HealthJson -Url 'http://127.0.0.1:8084/api/v1/health/ready'
$scrapexHealth = Invoke-HealthJson -Url 'http://127.0.0.1:8125/api/health'
$runningScrapeXRevision = [string]$scrapexHealth.runtime_revision
if ($scrapexRevision -and -not $runningScrapeXRevision.Equals($scrapexRevision, [StringComparison]::OrdinalIgnoreCase)) {
    throw "ScrapeX is healthy but running revision '$runningScrapeXRevision' instead of '$scrapexRevision'."
}

Write-Host ""
Write-Host 'LOCAL DEPLOYMENT VERIFIED' -ForegroundColor Green
Write-Host "X Omni        : http://127.0.0.1:$xPort  $runningXRevision"
Write-Host "Calibration IQ: http://127.0.0.1:8084   $ciqRevision"
Write-Host "ScrapeX       : http://127.0.0.1:8125   $runningScrapeXRevision"

if ($Open) {
    Start-Process "http://127.0.0.1:$xPort/"
    Start-Process 'http://127.0.0.1:8084/'
}
