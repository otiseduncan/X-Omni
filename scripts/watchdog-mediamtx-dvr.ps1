param(
    [int]$MediaMTXTimeoutSeconds = 4,
    [int]$DvrTimeoutSeconds = 4
)

# Runs on a schedule (see install-watchdog-task.ps1), independent of any
# interactive session that launched MediaMTX/the DVR GUI -- this is what
# actually recovers from a silent process death, unlike the Startup-folder
# shortcuts, which only fire once at logon.
#
# Only ever relaunches a target that fails its own health check first.
# launch-mediamtx.ps1 unconditionally stops-and-restarts whatever it finds,
# so calling it when the service is already healthy would itself introduce
# a needless recording gap every time this task ticks.

$root = "X:\X Omni"
$logDir = Join-Path $root "logs\launcher"
$logPath = Join-Path $logDir "watchdog.log"
New-Item -ItemType Directory -Path $logDir -Force | Out-Null

function Write-WatchdogLog {
    param([string]$Message)
    $stamp = Get-Date -Format 'yyyy-MM-dd HH:mm:ss.fff'
    Add-Content -LiteralPath $logPath -Value "[$stamp] $Message" -Encoding UTF8
}

function Test-Healthy {
    param([string]$Uri, [int]$TimeoutSeconds)
    try {
        $response = Invoke-WebRequest -Uri $Uri -TimeoutSec $TimeoutSeconds -UseBasicParsing
        return $response.StatusCode -eq 200
    } catch {
        return $false
    }
}

$mediamtxHealthy = Test-Healthy -Uri 'http://127.0.0.1:9997/v3/info' -TimeoutSeconds $MediaMTXTimeoutSeconds
if (-not $mediamtxHealthy) {
    Write-WatchdogLog 'MediaMTX unhealthy or unreachable -- relaunching.'
    try {
        & (Join-Path $root 'scripts\launch-mediamtx.ps1') -NoOpen
        Write-WatchdogLog 'MediaMTX relaunch invoked.'
    } catch {
        Write-WatchdogLog "MediaMTX relaunch FAILED: $($_.Exception.Message)"
    }
}

$dvrHealthy = Test-Healthy -Uri 'http://127.0.0.1:8300/healthz' -TimeoutSeconds $DvrTimeoutSeconds
if (-not $dvrHealthy) {
    Write-WatchdogLog 'X DVR GUI unhealthy or unreachable -- relaunching.'
    try {
        & (Join-Path $root 'scripts\launch-x-dvr.ps1') -NoOpen
        Write-WatchdogLog 'X DVR GUI relaunch invoked.'
    } catch {
        Write-WatchdogLog "X DVR GUI relaunch FAILED: $($_.Exception.Message)"
    }
}

Write-WatchdogLog "check complete: mediamtx=$(if ($mediamtxHealthy) { 'ok' } else { 'DOWN -> relaunched' }) dvr=$(if ($dvrHealthy) { 'ok' } else { 'DOWN -> relaunched' })"
