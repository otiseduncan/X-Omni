# Registers a per-user scheduled task that runs watchdog-mediamtx-dvr.ps1
# every 5 minutes, starting at logon, for as long as this user is logged in.
# This is what actually recovers from a silent MediaMTX/DVR GUI death --
# unlike the Startup-folder shortcuts, which only fire once at logon and
# never notice if the process dies mid-session.
#
#   cd "X:\X Omni"
#   .\scripts\install-watchdog-task.ps1
#
# To remove: Unregister-ScheduledTask -TaskName "X Omni MediaMTX+DVR Watchdog"

$ErrorActionPreference = 'Stop'
$root = [IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))
$script = Join-Path $PSScriptRoot 'watchdog-mediamtx-dvr.ps1'
$taskName = 'X Omni MediaMTX+DVR Watchdog'
$powershell = "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"

if (-not (Test-Path -LiteralPath $script)) {
    throw "Watchdog script is missing: $script"
}

$action = New-ScheduledTaskAction -Execute $powershell `
    -Argument "-NoLogo -NoProfile -WindowStyle Hidden -File `"$script`"" `
    -WorkingDirectory $root

# Base the repeating trigger on a one-time trigger's Repetition settings --
# New-ScheduledTaskTrigger has no direct -RepetitionInterval on -AtLogOn.
$repeatingTemplate = New-ScheduledTaskTrigger -Once -At (Get-Date) `
    -RepetitionInterval (New-TimeSpan -Minutes 5) `
    -RepetitionDuration (New-TimeSpan -Days 3650)
$trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
$trigger.Repetition = $repeatingTemplate.Repetition

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 2)

$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType Interactive -RunLevel Limited

if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
}

Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
    -Settings $settings -Principal $principal `
    -Description 'Every 5 minutes, checks MediaMTX (recording) and the X DVR GUI, relaunching either if unhealthy.' `
    | Out-Null

Write-Host "Installed scheduled task: $taskName (every 5 min while logged on)" -ForegroundColor Green
