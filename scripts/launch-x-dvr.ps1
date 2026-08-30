param(
    [switch]$NoOpen,
    [ValidateRange(10, 120)][int]$StartupTimeoutSeconds = 30
)

# Starts the standalone X DVR GUI (core/dvr_service.py) -- a thin,
# Owner-auth-gated browser front end over MediaMTX's own APIs. It owns no
# recorder and no camera connection of its own: continuous recording is
# MediaMTX's job (scripts/launch-mediamtx.ps1), independent of this
# process, of X Omni Core, and of whether anyone has this GUI open at all.
#
#   cd "X:\X Omni"
#   .\scripts\launch-x-dvr.ps1

$ErrorActionPreference = 'Stop'
$root = [IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))
$venvPython = Join-Path $root '.venv\Scripts\python.exe'
$logDirectory = Join-Path $root 'logs\launcher'
$dvrOrigin = 'http://127.0.0.1:8300'
$mutex = New-Object System.Threading.Mutex($false, 'Local\XOmniDvrGuiLauncher')
$hasMutex = $false

function Write-LauncherLog {
    param([Parameter(Mandatory)][string]$Message)
    $stamp = Get-Date -Format 'yyyy-MM-dd HH:mm:ss.fff'
    Add-Content -LiteralPath $script:launcherLog -Value "[$stamp] $Message" -Encoding UTF8
}

function Test-XDvrGuiProcess {
    param([AllowNull()]$Process)
    if (-not $Process) { return $false }
    $expectedPython = [IO.Path]::GetFullPath($venvPython)
    $commandLine = [string]$Process.CommandLine
    return (
        $Process.Name -ieq 'python.exe' -and
        $commandLine.IndexOf($expectedPython, [StringComparison]::OrdinalIgnoreCase) -ge 0 -and
        $commandLine -match '(?i)(?:^|\s)-m\s+core\.dvr_service(?:\s|$)'
    )
}

function Get-DvrGuiHealth {
    try {
        $response = Invoke-WebRequest -Uri "$dvrOrigin/healthz" -TimeoutSec 4 -UseBasicParsing
        return [pscustomobject]@{ StatusCode = [int]$response.StatusCode }
    } catch {
        return $null
    }
}

try {
    $hasMutex = $mutex.WaitOne(0)
    if (-not $hasMutex) { return }

    if (-not (Test-Path -LiteralPath $venvPython)) {
        throw "X Omni's isolated Python runtime is missing. Run setup before launching the DVR GUI."
    }

    New-Item -ItemType Directory -Path $logDirectory -Force | Out-Null
    $script:launcherLog = Join-Path $logDirectory 'x-dvr-launcher.log'

    $existing = Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
        Where-Object { Test-XDvrGuiProcess -Process $_ }
    foreach ($process in $existing) {
        Write-LauncherLog "Stopping existing verified X DVR GUI PID $($process.ProcessId) for a fresh restart."
        Stop-Process -Id ([int]$process.ProcessId) -Force -ErrorAction SilentlyContinue
    }
    if ($existing) {
        $deadline = [DateTime]::UtcNow.AddSeconds(10)
        do {
            Start-Sleep -Milliseconds 200
            $remaining = Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
                Where-Object { Test-XDvrGuiProcess -Process $_ }
        } while ($remaining -and [DateTime]::UtcNow -lt $deadline)
    }

    $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
    $stdout = Join-Path $logDirectory "x-dvr-$stamp.out.log"
    $stderr = Join-Path $logDirectory "x-dvr-$stamp.err.log"
    $process = Start-Process -FilePath $venvPython -ArgumentList '-m', 'core.dvr_service' `
        -WorkingDirectory $root -WindowStyle Hidden `
        -RedirectStandardOutput $stdout -RedirectStandardError $stderr -PassThru
    Write-LauncherLog "Started hidden X DVR GUI PID $($process.Id)."

    $deadline = [DateTime]::UtcNow.AddSeconds($StartupTimeoutSeconds)
    do {
        Start-Sleep -Milliseconds 500
        $health = Get-DvrGuiHealth
        if ($health -and $health.StatusCode -eq 200) {
            Write-LauncherLog 'X DVR GUI ready.'
            if (-not $NoOpen) { Start-Process "$dvrOrigin/dvr" }
            return
        }
        if ($process.HasExited) { break }
    } while ([DateTime]::UtcNow -lt $deadline)

    throw "X DVR GUI started, but did not answer $dvrOrigin/healthz within $StartupTimeoutSeconds seconds. See $stderr."
} catch {
    if (-not $script:launcherLog) {
        New-Item -ItemType Directory -Path $logDirectory -Force -ErrorAction SilentlyContinue | Out-Null
        $script:launcherLog = Join-Path $logDirectory 'x-dvr-launcher.log'
    }
    Write-LauncherLog "LAUNCH FAILED: $($_.Exception.Message)"
    Write-Host "X DVR GUI could not start: $($_.Exception.Message)" -ForegroundColor Red
    exit 1
} finally {
    if ($hasMutex) { [void]$mutex.ReleaseMutex() }
    $mutex.Dispose()
}
