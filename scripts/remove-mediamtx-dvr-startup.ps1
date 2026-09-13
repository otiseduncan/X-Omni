<#
.SYNOPSIS
    Removes the Windows startup artifacts the retired MediaMTX/X DVR stack left behind.

.DESCRIPTION
    Surveillance recording moved to a Frigate NVR on its own machine. Omega no
    longer runs MediaMTX or the standalone X DVR service, so the scheduled task
    and logon shortcuts that used to start and supervise them are now dead
    weight that would relaunch processes this repository no longer contains.

    This removes exactly three named artifacts and nothing else:

      * the scheduled task  "X Omni MediaMTX+DVR Watchdog"
      * the startup shortcut  MediaMTX.lnk
      * the startup shortcut  X DVR.lnk

    It is deliberately narrow and deliberately non-destructive about data:

      * no other scheduled task or startup entry is touched;
      * X:\MediaMTX is NOT deleted -- it is only reported, so its recordings
        can be archived or removed by hand, by a person, later;
      * no recording, clip, or snapshot is deleted anywhere.

    Safe to run more than once. Run it again after a reboot if you want to
    confirm nothing reappeared.

.PARAMETER WhatIf
    Report what would be removed without removing anything.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\scripts\remove-mediamtx-dvr-startup.ps1 -WhatIf

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\scripts\remove-mediamtx-dvr-startup.ps1
#>
[CmdletBinding(SupportsShouldProcess = $true)]
param()

$ErrorActionPreference = 'Stop'

$taskName = 'X Omni MediaMTX+DVR Watchdog'
$shortcutNames = @('MediaMTX.lnk', 'X DVR.lnk')
$legacyMediaMtxRoot = 'X:\MediaMTX'

$removed = [System.Collections.Generic.List[string]]::new()
$absent = [System.Collections.Generic.List[string]]::new()
$failed = [System.Collections.Generic.List[string]]::new()

Write-Host 'Removing retired MediaMTX / X DVR startup artifacts.' -ForegroundColor Cyan
Write-Host ''

# ---------------------------------------------------------------- scheduled task
try {
    $task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    if ($null -eq $task) {
        $absent.Add("Scheduled task '$taskName'")
    } elseif ($PSCmdlet.ShouldProcess("Scheduled task '$taskName'", 'Unregister')) {
        Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
        $removed.Add("Scheduled task '$taskName'")
    }
} catch {
    $failed.Add("Scheduled task '$taskName': $($_.Exception.Message)")
}

# ------------------------------------------------------------ startup shortcuts
# Both the per-user and all-users Startup folders, because either installer
# variant could have written there.
$startupFolders = @(
    [Environment]::GetFolderPath('Startup'),
    [Environment]::GetFolderPath('CommonStartup')
) | Where-Object { $_ -and (Test-Path -LiteralPath $_) } | Select-Object -Unique

foreach ($folder in $startupFolders) {
    foreach ($name in $shortcutNames) {
        $path = Join-Path $folder $name
        if (-not (Test-Path -LiteralPath $path)) {
            $absent.Add("Startup shortcut '$path'")
            continue
        }
        try {
            if ($PSCmdlet.ShouldProcess($path, 'Remove startup shortcut')) {
                Remove-Item -LiteralPath $path -Force
                $removed.Add("Startup shortcut '$path'")
            }
        } catch {
            $failed.Add("Startup shortcut '$path': $($_.Exception.Message)")
        }
    }
}

# ------------------------------------------------------------------- reporting
Write-Host 'Removed:' -ForegroundColor Green
if ($removed.Count -eq 0) { Write-Host '  (nothing -- already clean)' }
else { $removed | ForEach-Object { Write-Host "  $_" } }

Write-Host ''
Write-Host 'Already absent:'
if ($absent.Count -eq 0) { Write-Host '  (none)' }
else { $absent | ForEach-Object { Write-Host "  $_" } }

if ($failed.Count -gt 0) {
    Write-Host ''
    Write-Host 'Could not remove (run an elevated PowerShell and try again):' -ForegroundColor Yellow
    $failed | ForEach-Object { Write-Host "  $_" }
}

if (Test-Path -LiteralPath $legacyMediaMtxRoot) {
    Write-Host ''
    Write-Host "NOTE: $legacyMediaMtxRoot still exists and is no longer used by X Omni." -ForegroundColor Yellow
    Write-Host '      It is left exactly as it is. Archive or delete it yourself once you are'
    Write-Host '      satisfied nothing in it is still wanted -- this script never touches'
    Write-Host '      recordings.'
}

Write-Host ''
Write-Host 'Done. Surveillance recording now belongs to Frigate on its own machine.' -ForegroundColor Cyan
if ($failed.Count -gt 0) { exit 1 }
