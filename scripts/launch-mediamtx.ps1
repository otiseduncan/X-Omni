param(
    [switch]$NoOpen,
    [ValidateRange(10, 120)][int]$StartupTimeoutSeconds = 20
)

# Starts MediaMTX -- the exterior camera's media transport layer (RTSP
# connection, continuous recording to E:\MediaMTX\recordings, HLS/WebRTC
# live, and recorded playback). Independent of X Omni Core and of the
# standalone X DVR GUI process: neither starting nor stopping either of
# those touches this process, and vice versa.
#
#   cd "X:\X Omni"
#   .\scripts\launch-mediamtx.ps1

$ErrorActionPreference = 'Stop'
$root = [IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))
$mediamtxRoot = 'X:\MediaMTX'
$mediamtxExe = Join-Path $mediamtxRoot 'mediamtx.exe'
$mediamtxYaml = Join-Path $mediamtxRoot 'mediamtx.yml'
$venvPython = Join-Path $root '.venv\Scripts\python.exe'
$syncScript = Join-Path $root 'scripts\sync-mediamtx-config.py'
$logDirectory = Join-Path $root 'logs\launcher'
$controlOrigin = 'http://127.0.0.1:9997'
$playerOrigin = 'http://127.0.0.1:8889'
$mutex = New-Object System.Threading.Mutex($false, 'Local\XOmniMediaMTXLauncher')
$hasMutex = $false

function Write-LauncherLog {
    param([Parameter(Mandatory)][string]$Message)
    $stamp = Get-Date -Format 'yyyy-MM-dd HH:mm:ss.fff'
    Add-Content -LiteralPath $script:launcherLog -Value "[$stamp] $Message" -Encoding UTF8
}

function Test-MediaMTXProcess {
    param([AllowNull()]$Process)
    if (-not $Process) { return $false }
    $expectedExe = [IO.Path]::GetFullPath($mediamtxExe)
    $actualExe = [string]$Process.ExecutablePath
    return (
        $Process.Name -ieq 'mediamtx.exe' -and
        $actualExe -and
        [IO.Path]::GetFullPath($actualExe) -ieq $expectedExe
    )
}

function Get-MediaMTXHealth {
    try {
        $response = Invoke-WebRequest -Uri "$controlOrigin/v3/info" -TimeoutSec 4 -UseBasicParsing
        return [pscustomobject]@{ StatusCode = [int]$response.StatusCode }
    } catch {
        return $null
    }
}

function Get-MediaMTXConfiguredPaths {
    # Read the just-synced YAML directly rather than hardcoding camera
    # names -- sync-mediamtx-config.py regenerates this file from X Omni's
    # own camera credential store, so the path list must stay in step with
    # whatever cameras are actually configured, not a fixed pair.
    param([Parameter(Mandatory)][string]$YamlPath)
    $inPaths = $false
    $names = [System.Collections.Generic.List[string]]::new()
    foreach ($line in Get-Content -LiteralPath $YamlPath) {
        if ($line -match '^paths:\s*$') { $inPaths = $true; continue }
        if (-not $inPaths) { continue }
        if ($line -match '^\S') { break }
        if ($line -match '^  ([A-Za-z0-9_]+):\s*$' -and $Matches[1] -ne 'all_others') {
            $names.Add($Matches[1])
        }
    }
    return $names
}

function Test-MediaMTXPathsReady {
    # The control API answering only proves MediaMTX itself started -- it
    # says nothing about whether any actual camera connected. A launcher
    # that declares success here while every camera silently failed (bad
    # credentials, camera offline, network issue) isn't actually verifying
    # the thing it exists to start.
    param([Parameter(Mandatory)][AllowEmptyCollection()][string[]]$ExpectedPaths)
    if ($ExpectedPaths.Count -eq 0) { return $true }
    try {
        $response = Invoke-RestMethod -Uri "$controlOrigin/v3/paths/list" -TimeoutSec 4
    } catch {
        return $false
    }
    $byName = @{}
    foreach ($item in $response.items) { $byName[[string]$item.name] = $item }
    foreach ($name in $ExpectedPaths) {
        if (-not $byName.ContainsKey($name) -or -not $byName[$name].ready) { return $false }
    }
    return $true
}

try {
    $hasMutex = $mutex.WaitOne(0)
    if (-not $hasMutex) { return }

    if (-not (Test-Path -LiteralPath $mediamtxExe)) {
        throw "MediaMTX executable is missing: $mediamtxExe"
    }
    if (-not (Test-Path -LiteralPath $venvPython)) {
        throw "X Omni's isolated Python runtime is missing. Run setup before launching MediaMTX."
    }

    New-Item -ItemType Directory -Path $logDirectory -Force | Out-Null
    $script:launcherLog = Join-Path $logDirectory 'mediamtx-launcher.log'
    Write-LauncherLog 'Syncing MediaMTX camera paths from X Omni camera credentials.'

    # Never let a stale/missing camera config start MediaMTX pointed at the
    # wrong (or no) source -- always regenerate paths from the exact same
    # credential store X Omni itself uses before every launch.
    & $venvPython $syncScript $mediamtxYaml
    if ($LASTEXITCODE -ne 0) {
        throw "MediaMTX camera path sync failed (exit $LASTEXITCODE). See $syncScript output above."
    }
    $expectedPaths = Get-MediaMTXConfiguredPaths -YamlPath $mediamtxYaml

    # Get-Process's Name/Path shape doesn't match Test-MediaMTXProcess's
    # Win32_Process-style check (Name lacks ".exe", there is no
    # ExecutablePath) -- that mismatch previously made verification always
    # fail whenever MediaMTX was already running, rejecting even the exact
    # correct process. Win32_Process throughout keeps the shapes consistent.
    $existing = Get-CimInstance Win32_Process -Filter "Name='mediamtx.exe'" -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($existing -and -not (Test-MediaMTXProcess -Process $existing)) {
        throw "A process named mediamtx.exe is already running from an unexpected location. X Omni will not replace it."
    }
    if ($existing) {
        Write-LauncherLog "Stopping existing verified MediaMTX PID $($existing.ProcessId) for a fresh restart."
        Stop-Process -Id ([int]$existing.ProcessId) -Force -ErrorAction Stop
        $deadline = [DateTime]::UtcNow.AddSeconds(10)
        do {
            Start-Sleep -Milliseconds 200
            $remaining = Get-CimInstance Win32_Process -Filter "Name='mediamtx.exe'" -ErrorAction SilentlyContinue |
                Select-Object -First 1
        } while ($remaining -and [DateTime]::UtcNow -lt $deadline)
        if ($remaining) { throw 'MediaMTX did not stop within the expected time.' }
    }

    $process = Start-Process -FilePath $mediamtxExe -ArgumentList "`"$mediamtxYaml`"" `
        -WorkingDirectory $mediamtxRoot -WindowStyle Hidden -PassThru
    Write-LauncherLog "Started hidden MediaMTX PID $($process.Id)."

    $deadline = [DateTime]::UtcNow.AddSeconds($StartupTimeoutSeconds)
    $apiUp = $false
    do {
        Start-Sleep -Milliseconds 500
        $health = Get-MediaMTXHealth
        if ($health -and $health.StatusCode -eq 200) {
            $apiUp = $true
            if (Test-MediaMTXPathsReady -ExpectedPaths $expectedPaths) {
                Write-LauncherLog "MediaMTX ready; $($expectedPaths.Count) configured path(s) online: $($expectedPaths -join ', ')."
                if (-not $NoOpen -and $expectedPaths.Count -gt 0) {
                    # MediaMTX serves an actual interactive WebRTC viewer page
                    # at <path>/ on its WebRTC port -- not the raw JSON at
                    # /v3/paths/list, which is an API response, not a player.
                    Start-Process "$playerOrigin/$($expectedPaths[0])/"
                }
                return
            }
        }
        if ($process.HasExited) { break }
    } while ([DateTime]::UtcNow -lt $deadline)

    if (-not $apiUp) {
        throw "MediaMTX started, but did not answer $controlOrigin/v3/info within $StartupTimeoutSeconds seconds. Check $mediamtxRoot\mediamtx.log."
    }
    throw "MediaMTX's control API came up, but not every configured camera path came online within $StartupTimeoutSeconds seconds ($($expectedPaths -join ', ')). Check $mediamtxRoot\mediamtx.log and the camera's own connection."
} catch {
    if (-not $script:launcherLog) {
        New-Item -ItemType Directory -Path $logDirectory -Force -ErrorAction SilentlyContinue | Out-Null
        $script:launcherLog = Join-Path $logDirectory 'mediamtx-launcher.log'
    }
    Write-LauncherLog "LAUNCH FAILED: $($_.Exception.Message)"
    Write-Host "MediaMTX could not start: $($_.Exception.Message)" -ForegroundColor Red
    exit 1
} finally {
    if ($hasMutex) { [void]$mutex.ReleaseMutex() }
    $mutex.Dispose()
}
