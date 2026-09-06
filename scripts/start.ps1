# X Omni -- start Core.
#
#   cd "X:\X Omni"
#   .\scripts\start.ps1
#
# Core starts the default model worker (Omni) itself. Cold start is
# ~15-20 seconds. Run this in a window you can watch -- output stays in
# the foreground on purpose.

& {
    $ErrorActionPreference = "Stop"
    $root = Split-Path -Parent $PSScriptRoot
    Set-Location $root

    Write-Host "=== X Omni ===" -ForegroundColor Cyan

    $venvPython = Join-Path $root ".venv\Scripts\python.exe"
    if (-not (Test-Path -LiteralPath $venvPython)) {
        throw "The isolated Python runtime is missing. Run .\scripts\setup.ps1 first."
    }

    # Refuse to create a second Core. Uvicorn runs application startup before
    # reporting a bind failure, so relying on port-bind failure alone could let
    # the losing instance adopt the model and then stop it during shutdown.
    $corePort = 8100
    $envFile = Join-Path $root "config\.env.local"
    if (Test-Path -LiteralPath $envFile) {
        $configuredPort = Get-Content -LiteralPath $envFile |
            Where-Object { $_ -match '^\s*XOMNI_PORT\s*=\s*(\d+)\s*$' } |
            Select-Object -Last 1
        if ($configuredPort -and $configuredPort -match '=\s*(\d+)\s*$') {
            $corePort = [int]$matches[1]
        }
    }
    if ($env:XOMNI_PORT -match '^\d+$') {
        $corePort = [int]$env:XOMNI_PORT
    }

    $coreListener = Get-NetTCPConnection -LocalAddress 127.0.0.1 -LocalPort $corePort `
        -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($coreListener) {
        $listenerProcessId = [int]$coreListener.OwningProcess
        $processInfo = Get-CimInstance Win32_Process -Filter "ProcessId=$listenerProcessId"
        $expectedPythonToken = [IO.Path]::GetFullPath($venvPython)
        $commandLine = [string]$processInfo.CommandLine
        $identityMatches =
            $processInfo.Name -ieq "python.exe" -and
            $commandLine.IndexOf($expectedPythonToken, [StringComparison]::OrdinalIgnoreCase) -ge 0 -and
            $commandLine -match '(?i)(?:^|\s)-m\s+core\.main(?:\s|$)'
        # A verified process can still miss one health check under load (e.g.
        # a concurrent build/test run competing for CPU). Retry before
        # concluding anything is actually wrong -- one slow response must
        # never be reported the same way as a genuinely foreign process.
        $probeMatches = $false
        if ($identityMatches) {
            for ($attempt = 1; $attempt -le 4 -and -not $probeMatches; $attempt++) {
                try {
                    $probe = Invoke-WebRequest -Uri "http://127.0.0.1:$corePort/healthz" `
                        -TimeoutSec 5 -SkipHttpErrorCheck
                    $payload = $probe.Content | ConvertFrom-Json
                    $probeMatches = $payload.core -eq "running"
                } catch {
                    $probeMatches = $false
                }
                if (-not $probeMatches -and $attempt -lt 4) { Start-Sleep -Seconds 2 }
            }
        }
        if ($identityMatches -and $probeMatches) {
            Write-Host "X Omni is already running at http://127.0.0.1:$corePort" -ForegroundColor Green
            Write-Host "Reusing verified Core pid $listenerProcessId; no process was restarted."
            return
        }
        if (-not $identityMatches) {
            throw "Port $corePort is held by pid $listenerProcessId, which is not X Omni's own Core process (command line does not match). X Omni will not replace it."
        }
        throw "Port $corePort is held by X Omni's own Core (pid $listenerProcessId), but it did not answer a healthy /healthz after several attempts. It may be mid-swap or stuck -- check logs\launcher before stopping it yourself."
    }

    $workerConfig = Get-Content (Join-Path $root "config\workers.json") -Raw | ConvertFrom-Json
    $defaultWorkerName = $workerConfig.default_worker
    $workerPort = [int]$workerConfig.workers.$defaultWorkerName.port

    # Surface an occupied port before Core performs the authoritative process,
    # command-line, alias, context, and ownership checks.
    $existing = Get-NetTCPConnection -LocalPort $workerPort -State Listen -ErrorAction SilentlyContinue
    if ($existing) {
        $listenerProcessId = $existing.OwningProcess | Select-Object -First 1
        $name = (Get-Process -Id $listenerProcessId -ErrorAction SilentlyContinue).ProcessName
        Write-Host "Note: worker port $workerPort is held by $name (pid $listenerProcessId)." -ForegroundColor Yellow
        Write-Host "      Core will refuse it unless the complete X Omni worker contract matches." -ForegroundColor Yellow
    }

    if (-not (Test-Path (Join-Path $root "ui\dist"))) {
        Write-Host "UI is not built. Run .\scripts\setup.ps1 first." -ForegroundColor Yellow
    }

    $sourceRevision = $null
    try {
        $revisionOutput = & git -C $root rev-parse HEAD 2>$null
        $revisionExitCode = $LASTEXITCODE
        $candidateRevision = ([string]($revisionOutput | Select-Object -First 1)).Trim()
        if ($revisionExitCode -eq 0 -and $candidateRevision -match "^[0-9a-fA-F]{40}$") {
            $sourceRevision = $candidateRevision.ToLowerInvariant()
            $env:XOMNI_SOURCE_REVISION = $sourceRevision
        }
    } catch {
        $sourceRevision = $null
    }
    if (-not $sourceRevision) {
        Remove-Item Env:XOMNI_SOURCE_REVISION -ErrorAction SilentlyContinue
    }

    Write-Host "Core     : http://127.0.0.1:8100"
    Write-Host "Revision : $(if ($sourceRevision) { $sourceRevision } else { 'unknown' })"
    Write-Host "Stop with Ctrl+C (the model worker is stopped too)."
    Write-Host ""

    & $venvPython -m core.main
}
