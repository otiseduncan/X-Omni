param(
    [switch]$NoOpen,
    [ValidateRange(30, 180)][int]$StartupTimeoutSeconds = 120
)

$ErrorActionPreference = 'Stop'
$root = [IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))
$startScript = Join-Path $PSScriptRoot 'start.ps1'
$venvPython = Join-Path $root '.venv\Scripts\python.exe'
$logDirectory = Join-Path $root 'logs\launcher'
$dvrRecordingsRoot = [IO.Path]::GetFullPath('E:\XOmni-DVR\recordings')
$localOrigin = 'http://127.0.0.1'
$mutex = New-Object System.Threading.Mutex($false, 'Local\XOmniWindowsLauncher')
$hasMutex = $false

function Show-XOmniMessage {
    param(
        [Parameter(Mandatory)][string]$Text,
        [string]$Title = 'X Omni',
        [ValidateSet('Information', 'Warning', 'Error')][string]$Kind = 'Information'
    )
    try {
        Add-Type -AssemblyName System.Windows.Forms -ErrorAction Stop
        $icon = [System.Windows.Forms.MessageBoxIcon]::$Kind
        [void][System.Windows.Forms.MessageBox]::Show(
            $Text,
            $Title,
            [System.Windows.Forms.MessageBoxButtons]::OK,
            $icon
        )
    } catch {
        Write-Host "$Title`: $Text"
    }
}

function Write-LauncherLog {
    param([Parameter(Mandatory)][string]$Message)
    $stamp = Get-Date -Format 'yyyy-MM-dd HH:mm:ss.fff'
    Add-Content -LiteralPath $script:launcherLog -Value "[$stamp] $Message" -Encoding UTF8
}

function Get-CorePort {
    $port = 8100
    $envFile = Join-Path $root 'config\.env.local'
    if (Test-Path -LiteralPath $envFile) {
        $configured = Get-Content -LiteralPath $envFile |
            Where-Object { $_ -match '^\s*XOMNI_PORT\s*=\s*(\d+)\s*$' } |
            Select-Object -Last 1
        if ($configured -and $configured -match '=\s*(\d+)\s*$') {
            $port = [int]$matches[1]
        }
    }
    if ($env:XOMNI_PORT -match '^\d+$') {
        $port = [int]$env:XOMNI_PORT
    }
    return $port
}

function Get-PortOwner {
    param([Parameter(Mandatory)][int]$Port)
    $listener = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if (-not $listener) { return $null }
    return Get-CimInstance Win32_Process -Filter "ProcessId=$([int]$listener.OwningProcess)" -ErrorAction SilentlyContinue
}

function Test-XOmniCoreProcess {
    param([AllowNull()]$Process)
    if (-not $Process) { return $false }
    $expectedPython = [IO.Path]::GetFullPath($venvPython)
    $commandLine = [string]$Process.CommandLine
    return (
        $Process.Name -ieq 'python.exe' -and
        $commandLine.IndexOf($expectedPython, [StringComparison]::OrdinalIgnoreCase) -ge 0 -and
        $commandLine -match '(?i)(?:^|\s)-m\s+core\.main(?:\s|$)'
    )
}

function ConvertFrom-WindowsCommandLine {
    param([Parameter(Mandatory)][string]$CommandLine)

    try {
        if (-not ('XOmniLauncher.NativeCommandLine' -as [type])) {
            Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;

namespace XOmniLauncher {
    public static class NativeCommandLine {
        [DllImport("shell32.dll", SetLastError = true)]
        public static extern IntPtr CommandLineToArgvW(
            [MarshalAs(UnmanagedType.LPWStr)] string commandLine,
            out int argumentCount
        );

        [DllImport("kernel32.dll")]
        public static extern IntPtr LocalFree(IntPtr memory);
    }
}
'@ -ErrorAction Stop
        }

        $argumentCount = 0
        $argumentVector = [XOmniLauncher.NativeCommandLine]::CommandLineToArgvW(
            $CommandLine,
            [ref]$argumentCount
        )
        if ($argumentVector -eq [IntPtr]::Zero -or $argumentCount -lt 1) {
            return $null
        }
        try {
            $arguments = @()
            for ($index = 0; $index -lt $argumentCount; $index++) {
                $slot = [IntPtr]::Add($argumentVector, $index * [IntPtr]::Size)
                $value = [Runtime.InteropServices.Marshal]::ReadIntPtr($slot)
                $arguments += [Runtime.InteropServices.Marshal]::PtrToStringUni($value)
            }
            return ,$arguments
        } finally {
            [void][XOmniLauncher.NativeCommandLine]::LocalFree($argumentVector)
        }
    } catch {
        return $null
    }
}

function Test-XOmniDvrRecorderArguments {
    param(
        [Parameter(Mandatory)][string]$ExecutablePath,
        [Parameter(Mandatory)][string[]]$Arguments
    )

    try {
        $actualExecutable = [IO.Path]::GetFullPath($ExecutablePath)
        if ([IO.Path]::GetFileName($actualExecutable) -ine 'ffmpeg.exe') {
            return $false
        }
        if (-not $Arguments -or $Arguments.Count -lt 2) {
            return $false
        }
        $argvExecutable = [IO.Path]::GetFullPath([string]$Arguments[0])
        if (-not [string]::Equals(
            $actualExecutable,
            $argvExecutable,
            [StringComparison]::OrdinalIgnoreCase
        )) {
            return $false
        }

        # This is the complete, credential-free argv emitted by
        # CameraDVR._record_args(). Any extra, missing, reordered, or changed
        # argument means the process is not ours and must be left untouched.
        $expected = @(
            '-hide_banner',
            '-loglevel', 'error',
            '-f', 'concat',
            '-safe', '0',
            '-protocol_whitelist', 'file,pipe,tcp,rtsp',
            '-i', 'pipe:0',
            '-map', '0:v:0',
            '-an',
            '-c:v', 'copy',
            '-f', 'segment',
            '-segment_format', 'matroska',
            '-segment_time', '300',
            '-reset_timestamps', '1'
        )
        if ($Arguments.Count -ne ($expected.Count + 2)) {
            return $false
        }
        for ($index = 0; $index -lt $expected.Count; $index++) {
            if (-not [string]::Equals(
                [string]$Arguments[$index + 1],
                [string]$expected[$index],
                [StringComparison]::Ordinal
            )) {
                return $false
            }
        }

        $output = [IO.Path]::GetFullPath([string]$Arguments[$Arguments.Count - 1])
        $outputDirectory = [IO.Path]::GetDirectoryName($output)
        $outputName = [IO.Path]::GetFileName($output)
        if (-not [string]::Equals(
            $outputDirectory,
            $script:dvrRecordingsRoot,
            [StringComparison]::OrdinalIgnoreCase
        )) {
            return $false
        }
        return $outputName -match '^[0-9]{8}T[0-9]{12}Z-%06d[.]mkv$'
    } catch {
        return $false
    }
}

function Test-XOmniDvrRecorderProcess {
    param([AllowNull()]$Process)

    if (-not $Process -or $Process.Name -ine 'ffmpeg.exe') {
        return $false
    }
    $executablePath = [string]$Process.ExecutablePath
    $commandLine = [string]$Process.CommandLine
    if (-not $executablePath -or -not $commandLine) {
        return $false
    }
    $arguments = ConvertFrom-WindowsCommandLine -CommandLine $commandLine
    if (-not $arguments) {
        return $false
    }
    return Test-XOmniDvrRecorderArguments `
        -ExecutablePath $executablePath `
        -Arguments $arguments
}

function Stop-VerifiedDvrRecorders {
    # Name narrows discovery only. Every stop is by PID after executable and
    # complete argv verification; preview, playback, and foreign FFmpeg jobs
    # deliberately fail Test-XOmniDvrRecorderProcess and remain untouched.
    $candidates = @(
        Get-CimInstance Win32_Process -Filter "Name='ffmpeg.exe'" -ErrorAction SilentlyContinue
    )
    foreach ($candidate in $candidates) {
        if (-not (Test-XOmniDvrRecorderProcess -Process $candidate)) {
            continue
        }
        $processId = [int]$candidate.ProcessId
        # Re-read immediately before the destructive action so PID reuse or a
        # process exit cannot turn the earlier verification into authority.
        $current = Get-CimInstance Win32_Process -Filter "ProcessId=$processId" -ErrorAction SilentlyContinue
        if (-not (Test-XOmniDvrRecorderProcess -Process $current)) {
            continue
        }

        Write-LauncherLog "Stopping exact X Omni DVR recorder PID $processId before Core restart."
        try {
            Stop-Process -Id $processId -Force -ErrorAction Stop
        } catch {
            $remaining = Get-CimInstance Win32_Process -Filter "ProcessId=$processId" -ErrorAction SilentlyContinue
            if ($remaining) {
                throw "Verified X Omni DVR recorder PID $processId could not be stopped."
            }
            continue
        }
        try {
            Wait-Process -Id $processId -Timeout 15 -ErrorAction SilentlyContinue
        } catch {}
        $remaining = Get-CimInstance Win32_Process -Filter "ProcessId=$processId" -ErrorAction SilentlyContinue
        if ($remaining) {
            throw "Verified X Omni DVR recorder PID $processId did not exit."
        }
    }
}

function Get-CoreHealth {
    param([Parameter(Mandatory)][int]$Port)
    try {
        $response = Invoke-WebRequest -Uri "$localOrigin`:$Port/healthz" -TimeoutSec 4 -UseBasicParsing
        $payload = $response.Content | ConvertFrom-Json
        return [pscustomobject]@{
            StatusCode = [int]$response.StatusCode
            Payload = $payload
        }
    } catch {
        return $null
    }
}

function Stop-VerifiedCore {
    param([Parameter(Mandatory)][int]$Port)
    $owner = Get-PortOwner -Port $Port
    if (-not $owner) { return }
    if (-not (Test-XOmniCoreProcess -Process $owner)) {
        throw "Port $Port is held by unverified PID $($owner.ProcessId). X Omni will not replace it."
    }

    Write-LauncherLog "Stopping existing verified X Omni Core PID $($owner.ProcessId) for a fresh restart."
    Stop-Process -Id ([int]$owner.ProcessId) -Force -ErrorAction Stop
    $deadline = [DateTime]::UtcNow.AddSeconds(15)
    do {
        Start-Sleep -Milliseconds 250
        $remaining = Get-PortOwner -Port $Port
    } while ($remaining -and [DateTime]::UtcNow -lt $deadline)
    if ($remaining) {
        throw "Verified X Omni Core PID $($remaining.ProcessId) did not release port $Port."
    }

    # A Windows venv launcher can remain briefly after its base-Python child
    # exits. Stop only exact X Omni core command lines; never name-match Python.
    $expectedPython = [IO.Path]::GetFullPath($venvPython)
    $stragglers = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
        $_.Name -ieq 'python.exe' -and
        ([string]$_.CommandLine).IndexOf($expectedPython, [StringComparison]::OrdinalIgnoreCase) -ge 0 -and
        ([string]$_.CommandLine) -match '(?i)(?:^|\s)-m\s+core\.main(?:\s|$)'
    })
    foreach ($process in $stragglers) {
        Write-LauncherLog "Stopping verified X Omni Core straggler PID $($process.ProcessId)."
        Stop-Process -Id ([int]$process.ProcessId) -Force -ErrorAction SilentlyContinue
    }
}

function Stop-VerifiedLegacyModel {
    $modelScript = 'X:\XV12\scripts\xv12-model.ps1'
    if (-not (Test-Path -LiteralPath $modelScript)) { return }

    $statusText = & $modelScript -Action Status 2>$null | Out-String
    $statusSucceeded = $?
    if (-not $statusSucceeded -or -not $statusText.Trim()) { return }
    try {
        $status = $statusText | ConvertFrom-Json
    } catch {
        Write-LauncherLog 'XV12 model status was not valid JSON; it was left untouched.'
        return
    }

    if ($status.status -eq 'healthy' -and $status.owned_process -eq $true) {
        Write-LauncherLog "Stopping verified XV12-owned legacy model PID $($status.pid) to release the GPUs."
        & $modelScript -Action Stop | ForEach-Object { Write-LauncherLog ([string]$_) }
        if (-not $?) {
            throw 'The verified XV12 model could not be stopped cleanly.'
        }
        return
    }

    if ($status.pid) {
        throw "GPU model port $($status.port) is owned by an unverified process (PID $($status.pid)). X Omni will not stop it."
    }
}

function Stop-VerifiedLegacyComfyUI {
    $comfyScript = 'X:\XV12\scripts\xv12-comfyui.ps1'
    $stateFile = 'X:\XV12\runtime\state\comfyui.json'
    if (-not (Test-Path -LiteralPath $comfyScript)) { return }

    $status = & $comfyScript -Action Status 2>$null
    $statusSucceeded = $?
    if (-not $statusSucceeded -or -not $status) { return }

    $verified = $false
    $state = $null
    $process = $null
    if ($status.healthy -eq $true -and $status.pid -and (Test-Path -LiteralPath $stateFile)) {
        try {
            $state = Get-Content -LiteralPath $stateFile -Raw | ConvertFrom-Json
            $process = Get-CimInstance Win32_Process -Filter "ProcessId=$([int]$status.pid)" -ErrorAction Stop
            $nativeProcess = Get-Process -Id ([int]$status.pid) -ErrorAction Stop
            $expectedExecutable = [IO.Path]::GetFullPath((Join-Path ([string]$state.runtime_root) 'python_embeded\python.exe'))
            $expectedMain = [IO.Path]::GetFullPath((Join-Path ([string]$state.runtime_root) 'ComfyUI\main.py'))
            $actualExecutable = [IO.Path]::GetFullPath([string]$process.ExecutablePath)
            $commandLine = [string]$process.CommandLine
            $recordedStart = if ($state.process_started_at -is [datetime]) {
                $state.process_started_at.ToUniversalTime()
            } else {
                [DateTimeOffset]::Parse([string]$state.process_started_at).UtcDateTime
            }
            $actualStart = $nativeProcess.StartTime.ToUniversalTime()
            $verified = (
                [string]$state.managed_by -eq 'XV12' -and
                [IO.Path]::GetFullPath([string]$state.root) -eq [IO.Path]::GetFullPath('X:\XV12') -and
                [int]$state.pid -eq [int]$status.pid -and
                [int]$state.port -eq [int]$status.port -and
                [math]::Abs(($actualStart - $recordedStart).TotalSeconds) -lt 4 -and
                $actualExecutable -ieq $expectedExecutable -and
                $commandLine.IndexOf($expectedMain, [StringComparison]::OrdinalIgnoreCase) -ge 0 -and
                $commandLine -match "(?i)--listen\s+127\.0\.0\.1(?:\s|$)" -and
                $commandLine -match "(?i)--port\s+$([int]$status.port)(?:\s|$)"
            )
        } catch {
            $verified = $false
        }
    }

    if ($verified) {
        Write-LauncherLog "Stopping exact XV12-owned legacy ComfyUI PID $($status.pid) to release port $($status.port) and the GPUs."
        Stop-Process -Id ([int]$status.pid) -Force -ErrorAction Stop
        try { Wait-Process -Id ([int]$status.pid) -Timeout 20 -ErrorAction SilentlyContinue } catch {}
        & $comfyScript -Action Stop | ForEach-Object { Write-LauncherLog ([string]$_) }
        return
    }

    if ($status.pid) {
        throw "ComfyUI port $($status.port) is owned by an unverified process (PID $($status.pid)). X Omni will not stop it."
    }
}

function Open-XOmni {
    param([Parameter(Mandatory)][int]$Port)
    if (-not $NoOpen) {
        Start-Process "$localOrigin`:$Port/"
    }
}

function Invoke-UiRebuild {
    $npmCmd = Get-Command npm.cmd -ErrorAction SilentlyContinue
    if (-not $npmCmd) { $npmCmd = Get-Command npm -ErrorAction SilentlyContinue }
    if (-not $npmCmd) {
        Write-LauncherLog 'npm was not found on PATH; launching with the interface already on disk instead of rebuilding.'
        return
    }
    Write-LauncherLog 'Rebuilding the UI so this launch always serves the current source.'
    Push-Location (Join-Path $root 'ui')
    try {
        & $npmCmd.Source run build 2>&1 | ForEach-Object { Write-LauncherLog "[ui build] $_" }
        if ($LASTEXITCODE -ne 0) {
            throw "UI build failed with exit code $LASTEXITCODE. See $($script:launcherLog) for the build log."
        }
    } finally {
        Pop-Location
    }
}

try {
    $hasMutex = $mutex.WaitOne(0)
    if (-not $hasMutex) { return }

    if (-not (Test-Path -LiteralPath $venvPython)) {
        throw "X Omni's isolated Python runtime is missing. Run setup before launching."
    }
    if (-not (Test-Path -LiteralPath $startScript)) {
        throw "X Omni's start script is missing: $startScript"
    }
    if (-not (Test-Path -LiteralPath (Join-Path $root 'ui\dist\index.html'))) {
        throw "X Omni's built interface is missing. Run setup before launching."
    }

    New-Item -ItemType Directory -Path $logDirectory -Force | Out-Null
    $script:launcherLog = Join-Path $logDirectory 'x-omni-launcher.log'
    $corePort = Get-CorePort
    Write-LauncherLog "Launch requested for $localOrigin`:$corePort."

    # Double-clicking the launcher means "give me a known-good, current X
    # Omni" -- not "tell me whether the old one still happens to be alive."
    # Every launch stops whatever is running, rebuilds the UI from source,
    # and starts clean, so there is exactly one behavior to reason about
    # instead of a reuse path and a restart path that can drift apart.
    $owner = Get-PortOwner -Port $corePort
    if ($owner -and -not (Test-XOmniCoreProcess -Process $owner)) {
        throw "Port $corePort is held by unverified PID $($owner.ProcessId). X Omni will not replace it."
    }

    Stop-VerifiedLegacyModel
    Stop-VerifiedLegacyComfyUI
    if ($owner) {
        Stop-VerifiedCore -Port $corePort
    }
    Stop-VerifiedDvrRecorders

    Invoke-UiRebuild

    $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
    $stdout = Join-Path $logDirectory "core-$stamp.out.log"
    $stderr = Join-Path $logDirectory "core-$stamp.err.log"
    $hostExe = "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"
    if (-not (Test-Path -LiteralPath $hostExe)) {
        throw "Windows PowerShell was not found at $hostExe"
    }

    # The launcher is already the trust boundary and start.ps1 is a fixed,
    # repository-owned path. Allow that one child script to run even when the
    # interactive Windows PowerShell policy blocks all .ps1 files.
    $arguments = "-NoLogo -NoProfile -ExecutionPolicy Bypass -File `"$startScript`""
    $process = Start-Process -FilePath $hostExe -ArgumentList $arguments `
        -WorkingDirectory $root -WindowStyle Hidden `
        -RedirectStandardOutput $stdout -RedirectStandardError $stderr -PassThru
    Write-LauncherLog "Started hidden Core host PID $($process.Id); stdout=$stdout stderr=$stderr"

    $deadline = [DateTime]::UtcNow.AddSeconds($StartupTimeoutSeconds)
    $lastHealth = $null
    do {
        Start-Sleep -Milliseconds 750
        $lastHealth = Get-CoreHealth -Port $corePort
        if ($lastHealth -and $lastHealth.StatusCode -eq 200 -and $lastHealth.Payload.ok -eq $true) {
            Write-LauncherLog "X Omni ready with worker '$($lastHealth.Payload.worker)'."
            Open-XOmni -Port $corePort
            return
        }
        if ($process.HasExited -and -not (Get-PortOwner -Port $corePort)) { break }
    } while ([DateTime]::UtcNow -lt $deadline)

    $details = @()
    if (Test-Path -LiteralPath $stderr) {
        $details += @(Get-Content -LiteralPath $stderr -Tail 12 -ErrorAction SilentlyContinue)
    }
    if (-not $details -and (Test-Path -LiteralPath $stdout)) {
        $details += @(Get-Content -LiteralPath $stdout -Tail 12 -ErrorAction SilentlyContinue)
    }
    $detailText = ($details -join [Environment]::NewLine).Trim()
    if (-not $detailText) { $detailText = 'No additional startup detail was recorded.' }
    throw "X Omni Core started, but the model did not become ready within $StartupTimeoutSeconds seconds.`n`n$detailText`n`nLogs: $logDirectory"
} catch {
    if (-not $script:launcherLog) {
        New-Item -ItemType Directory -Path $logDirectory -Force -ErrorAction SilentlyContinue | Out-Null
        $script:launcherLog = Join-Path $logDirectory 'x-omni-launcher.log'
    }
    Write-LauncherLog "LAUNCH FAILED: $($_.Exception.Message)"
    Show-XOmniMessage -Title 'X Omni could not start' -Kind Error -Text $_.Exception.Message
    exit 1
} finally {
    if ($hasMutex) { [void]$mutex.ReleaseMutex() }
    $mutex.Dispose()
}
