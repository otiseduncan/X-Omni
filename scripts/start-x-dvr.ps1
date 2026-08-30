# X DVR -- start the independent recording service.
#
#   cd "X:\X Omni"
#   .\scripts\start-x-dvr.ps1
#
# This owns continuous RTSP recording to E:\XOmni-DVR and the standalone DVR
# GUI/API. It is deliberately independent of X Omni Core: this script never
# touches Core, and Core's own launcher never touches this process. Run in a
# window you can watch -- output stays in the foreground on purpose.

& {
    $ErrorActionPreference = "Stop"
    $root = Split-Path -Parent $PSScriptRoot
    Set-Location $root

    Write-Host "=== X DVR ===" -ForegroundColor Cyan

    $venvPython = Join-Path $root ".venv\Scripts\python.exe"
    if (-not (Test-Path -LiteralPath $venvPython)) {
        throw "The isolated Python runtime is missing. Run .\scripts\setup.ps1 first."
    }

    $dvrPort = 8300
    $envFile = Join-Path $root "config\.env.local"
    if (Test-Path -LiteralPath $envFile) {
        $configuredPort = Get-Content -LiteralPath $envFile |
            Where-Object { $_ -match '^\s*XOMNI_DVR_PORT\s*=\s*(\d+)\s*$' } |
            Select-Object -Last 1
        if ($configuredPort -and $configuredPort -match '=\s*(\d+)\s*$') {
            $dvrPort = [int]$matches[1]
        }
    }
    if ($env:XOMNI_DVR_PORT -match '^\d+$') {
        $dvrPort = [int]$env:XOMNI_DVR_PORT
    }

    # Refuse to create a second recorder against the same E:\XOmni-DVR archive.
    $dvrListener = Get-NetTCPConnection -LocalAddress 127.0.0.1 -LocalPort $dvrPort `
        -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($dvrListener) {
        $listenerProcessId = [int]$dvrListener.OwningProcess
        $processInfo = Get-CimInstance Win32_Process -Filter "ProcessId=$listenerProcessId"
        $expectedPythonToken = [IO.Path]::GetFullPath($venvPython)
        $commandLine = [string]$processInfo.CommandLine
        $identityMatches =
            $processInfo.Name -ieq "python.exe" -and
            $commandLine.IndexOf($expectedPythonToken, [StringComparison]::OrdinalIgnoreCase) -ge 0 -and
            $commandLine -match '(?i)(?:^|\s)-m\s+core\.dvr_service(?:\s|$)'
        $probeMatches = $false
        if ($identityMatches) {
            for ($attempt = 1; $attempt -le 4 -and -not $probeMatches; $attempt++) {
                try {
                    $probe = Invoke-WebRequest -Uri "http://127.0.0.1:$dvrPort/healthz" `
                        -TimeoutSec 5 -SkipHttpErrorCheck
                    $payload = $probe.Content | ConvertFrom-Json
                    $probeMatches = $payload.service -eq "dvr"
                } catch {
                    $probeMatches = $false
                }
                if (-not $probeMatches -and $attempt -lt 4) { Start-Sleep -Seconds 2 }
            }
        }
        if ($identityMatches -and $probeMatches) {
            Write-Host "X DVR is already running at http://127.0.0.1:$dvrPort/dvr" -ForegroundColor Green
            Write-Host "Reusing verified DVR pid $listenerProcessId; no process was restarted."
            return
        }
        if (-not $identityMatches) {
            throw "Port $dvrPort is held by pid $listenerProcessId, which is not X DVR's own process (command line does not match). X DVR will not replace it."
        }
        throw "Port $dvrPort is held by X DVR's own process (pid $listenerProcessId), but it did not answer a healthy /healthz after several attempts."
    }

    Write-Host "X DVR  : http://127.0.0.1:$dvrPort/dvr"
    Write-Host "Stop with Ctrl+C (continuous recording stops with this process only)."
    Write-Host ""

    & $venvPython -m core.dvr_service
}
