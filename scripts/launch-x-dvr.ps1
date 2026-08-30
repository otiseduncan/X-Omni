param(
    [switch]$NoOpen,
    [ValidateRange(10, 120)][int]$StartupTimeoutSeconds = 30
)

$ErrorActionPreference = 'Stop'
$root = [IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))
$startScript = Join-Path $PSScriptRoot 'start-x-dvr.ps1'
$venvPython = Join-Path $root '.venv\Scripts\python.exe'
$logDirectory = Join-Path $root 'logs\launcher'
$dvrRecordingsRoot = [IO.Path]::GetFullPath('E:\XOmni-DVR\recordings')
$localOrigin = 'http://127.0.0.1'
$mutex = New-Object System.Threading.Mutex($false, 'Local\XOmniDvrWindowsLauncher')
$hasMutex = $false

function Show-XDvrMessage {
    param(
        [Parameter(Mandatory)][string]$Text,
        [string]$Title = 'X DVR',
        [ValidateSet('Information', 'Warning', 'Error')][string]$Kind = 'Information'
    )
    try {
        Add-Type -AssemblyName System.Windows.Forms -ErrorAction Stop
        $icon = [System.Windows.Forms.MessageBoxIcon]::$Kind
        [void][System.Windows.Forms.MessageBox]::Show(
            $Text, $Title, [System.Windows.Forms.MessageBoxButtons]::OK, $icon
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

function Get-DvrPort {
    $port = 8300
    $envFile = Join-Path $root 'config\.env.local'
    if (Test-Path -LiteralPath $envFile) {
        $configured = Get-Content -LiteralPath $envFile |
            Where-Object { $_ -match '^\s*XOMNI_DVR_PORT\s*=\s*(\d+)\s*$' } |
            Select-Object -Last 1
        if ($configured -and $configured -match '=\s*(\d+)\s*$') {
            $port = [int]$matches[1]
        }
    }
    if ($env:XOMNI_DVR_PORT -match '^\d+$') {
        $port = [int]$env:XOMNI_DVR_PORT
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

function Test-XDvrProcess {
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

function ConvertFrom-WindowsCommandLine {
    param([Parameter(Mandatory)][string]$CommandLine)

    try {
        if (-not ('XDvrLauncher.NativeCommandLine' -as [type])) {
            Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;

namespace XDvrLauncher {
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
        $argumentVector = [XDvrLauncher.NativeCommandLine]::CommandLineToArgvW(
            $CommandLine, [ref]$argumentCount
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
            [void][XDvrLauncher.NativeCommandLine]::LocalFree($argumentVector)
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
            $actualExecutable, $argvExecutable, [StringComparison]::OrdinalIgnoreCase
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
                [string]$Arguments[$index + 1], [string]$expected[$index], [StringComparison]::Ordinal
            )) {
                return $false
            }
        }

        $output = [IO.Path]::GetFullPath([string]$Arguments[$Arguments.Count - 1])
        $outputDirectory = [IO.Path]::GetDirectoryName($output)
        $outputName = [IO.Path]::GetFileName($output)
        if (-not [string]::Equals(
            $outputDirectory, $script:dvrRecordingsRoot, [StringComparison]::OrdinalIgnoreCase
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
    return Test-XOmniDvrRecorderArguments -ExecutablePath $executablePath -Arguments $arguments
}

function Stop-VerifiedDvrRecorders {
    # Name narrows discovery only. Every stop is by PID after executable and
    # complete argv verification; preview, playback, and foreign FFmpeg jobs
    # deliberately fail Test-XOmniDvrRecorderProcess and remain untouched.
    # This runs only from X DVR's own launcher now -- restarting X Omni Core
    # never reaches this function.
    $candidates = @(
        Get-CimInstance Win32_Process -Filter "Name='ffmpeg.exe'" -ErrorAction SilentlyContinue
    )
    foreach ($candidate in $candidates) {
        if (-not (Test-XOmniDvrRecorderProcess -Process $candidate)) { continue }
        $processId = [int]$candidate.ProcessId
        $current = Get-CimInstance Win32_Process -Filter "ProcessId=$processId" -ErrorAction SilentlyContinue
        if (-not (Test-XOmniDvrRecorderProcess -Process $current)) { continue }

        Write-LauncherLog "Stopping exact X DVR recorder PID $processId before restart."
        try {
            Stop-Process -Id $processId -Force -ErrorAction Stop
        } catch {
            $remaining = Get-CimInstance Win32_Process -Filter "ProcessId=$processId" -ErrorAction SilentlyContinue
            if ($remaining) {
                throw "Verified X DVR recorder PID $processId could not be stopped."
            }
            continue
        }
        $exited = $false
        $deadline = [DateTime]::UtcNow.AddSeconds(15)
        do {
            $remaining = Get-CimInstance Win32_Process -Filter "ProcessId=$processId" -ErrorAction SilentlyContinue
            if (-not $remaining -or -not (Test-XOmniDvrRecorderProcess -Process $remaining)) {
                $exited = $true
                break
            }
            Start-Sleep -Milliseconds 100
        } while ([DateTime]::UtcNow -lt $deadline)
        if (-not $exited) {
            throw "Verified X DVR recorder PID $processId did not exit."
        }
    }
}

function Get-DvrHealth {
    param([Parameter(Mandatory)][int]$Port)
    try {
        $response = Invoke-WebRequest -Uri "$localOrigin`:$Port/healthz" -TimeoutSec 4 -UseBasicParsing
        return [pscustomobject]@{
            StatusCode = [int]$response.StatusCode
            Payload = $response.Content | ConvertFrom-Json
        }
    } catch {
        return $null
    }
}

function Stop-VerifiedDvrService {
    param([Parameter(Mandatory)][int]$Port)
    $owner = Get-PortOwner -Port $Port
    if (-not $owner) { return }
    if (-not (Test-XDvrProcess -Process $owner)) {
        throw "Port $Port is held by unverified PID $($owner.ProcessId). X DVR will not replace it."
    }

    Write-LauncherLog "Stopping existing verified X DVR PID $($owner.ProcessId) for a fresh restart."
    Stop-Process -Id ([int]$owner.ProcessId) -Force -ErrorAction Stop
    $deadline = [DateTime]::UtcNow.AddSeconds(15)
    do {
        Start-Sleep -Milliseconds 250
        $remaining = Get-PortOwner -Port $Port
    } while ($remaining -and [DateTime]::UtcNow -lt $deadline)
    if ($remaining) {
        throw "Verified X DVR PID $($remaining.ProcessId) did not release port $Port."
    }
    # The recorder subprocess should exit with its parent; verify and clean
    # up any straggler so a fresh DVR service does not fight over the archive.
    Stop-VerifiedDvrRecorders
}

function Open-XDvr {
    param([Parameter(Mandatory)][int]$Port)
    if (-not $NoOpen) {
        Start-Process "$localOrigin`:$Port/dvr"
    }
}

try {
    $hasMutex = $mutex.WaitOne(0)
    if (-not $hasMutex) { return }

    if (-not (Test-Path -LiteralPath $venvPython)) {
        throw "X Omni's isolated Python runtime is missing. Run setup before launching X DVR."
    }
    if (-not (Test-Path -LiteralPath $startScript)) {
        throw "X DVR's start script is missing: $startScript"
    }

    New-Item -ItemType Directory -Path $logDirectory -Force | Out-Null
    $script:launcherLog = Join-Path $logDirectory 'x-dvr-launcher.log'
    $dvrPort = Get-DvrPort
    Write-LauncherLog "Launch requested for $localOrigin`:$dvrPort/dvr."

    $owner = Get-PortOwner -Port $dvrPort
    if ($owner -and -not (Test-XDvrProcess -Process $owner)) {
        throw "Port $dvrPort is held by unverified PID $($owner.ProcessId). X DVR will not replace it."
    }
    if ($owner) {
        Stop-VerifiedDvrService -Port $dvrPort
    } else {
        # No verified DVR service owns the port, but a previous run's
        # recorder could still be alive (e.g. after a crash) -- verify and
        # clear it before starting a second one against the same archive.
        Stop-VerifiedDvrRecorders
    }

    $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
    $stdout = Join-Path $logDirectory "dvr-$stamp.out.log"
    $stderr = Join-Path $logDirectory "dvr-$stamp.err.log"
    $hostExe = "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"
    if (-not (Test-Path -LiteralPath $hostExe)) {
        throw "Windows PowerShell was not found at $hostExe"
    }

    $arguments = "-NoLogo -NoProfile -ExecutionPolicy Bypass -File `"$startScript`""
    $process = Start-Process -FilePath $hostExe -ArgumentList $arguments `
        -WorkingDirectory $root -WindowStyle Hidden `
        -RedirectStandardOutput $stdout -RedirectStandardError $stderr -PassThru
    Write-LauncherLog "Started hidden X DVR host PID $($process.Id); stdout=$stdout stderr=$stderr"

    $deadline = [DateTime]::UtcNow.AddSeconds($StartupTimeoutSeconds)
    $lastHealth = $null
    do {
        Start-Sleep -Milliseconds 500
        $lastHealth = Get-DvrHealth -Port $dvrPort
        if ($lastHealth -and $lastHealth.StatusCode -eq 200 -and $lastHealth.Payload.ok -eq $true) {
            Write-LauncherLog "X DVR ready."
            Open-XDvr -Port $dvrPort
            return
        }
        if ($process.HasExited -and -not (Get-PortOwner -Port $dvrPort)) { break }
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
    throw "X DVR started, but did not become healthy within $StartupTimeoutSeconds seconds.`n`n$detailText`n`nLogs: $logDirectory"
} catch {
    if (-not $script:launcherLog) {
        New-Item -ItemType Directory -Path $logDirectory -Force -ErrorAction SilentlyContinue | Out-Null
        $script:launcherLog = Join-Path $logDirectory 'x-dvr-launcher.log'
    }
    Write-LauncherLog "LAUNCH FAILED: $($_.Exception.Message)"
    Show-XDvrMessage -Title 'X DVR could not start' -Kind Error -Text $_.Exception.Message
    exit 1
} finally {
    if ($hasMutex) { [void]$mutex.ReleaseMutex() }
    $mutex.Dispose()
}
