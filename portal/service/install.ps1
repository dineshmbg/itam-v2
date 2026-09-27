# Registers the "ITAM Portal" Windows Scheduled Task: starts the portal automatically when you log in,
# and self-heals if it ever stops unexpectedly. No admin rights are needed - the task runs as your own
# Windows account (same as opening it from a terminal), so it can read ~/.ongc_pgpass and
# ~/.itam_smtp_secret exactly as before.
#
#   .\service\install.ps1          register (or re-register after moving the project folder)
#   .\service\uninstall.ps1        remove the task (the portal itself is untouched)
#
# Crash recovery uses a 1-minute repeating "watchdog" trigger rather than Task Scheduler's own
# RestartOnFailure setting: that setting is unreliable in practice (verified: it did not fire after a
# forced kill of the process). The watchdog fires every minute regardless of outcome, but
# MultipleInstances=IgnoreNew makes it a no-op whenever the portal is already running, so in practice a
# crash is noticed and restarted within about a minute. run_hidden.ps1 also frees port 8420 itself before
# starting, in case a previous instance was not cleanly reaped.
#
# PostgreSQL already runs as its own Windows service (postgresql-x64-18) and starts at boot on its own.
$ErrorActionPreference = 'Stop'
$serviceDir = $PSScriptRoot
$runner = Join-Path $serviceDir 'run_hidden.ps1'
$name = 'ITAM Portal'

$action = New-ScheduledTaskAction -Execute 'powershell.exe' `
  -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$runner`""
$logonTrigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$bootTrigger = New-ScheduledTaskTrigger -AtStartup
$watchdogTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 1) -RepetitionDuration (New-TimeSpan -Days 3650)
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) `
  -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew `
  -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $name -Action $action -Trigger @($logonTrigger, $bootTrigger, $watchdogTrigger) -Settings $settings -Principal $principal `
  -Description 'Starts the ITAM Portal (Starlette + PostgreSQL) at logon, at boot, and self-heals within about a minute if it stops. See portal\service\run_hidden.ps1.' `
  -Force | Out-Null

Write-Host "Registered scheduled task '$name'. It will start automatically at boot / your next logon and check itself every minute."
Write-Host "To start it right now:  Start-ScheduledTask -TaskName '$name'"
Write-Host ""
Write-Host "NOTE on unattended servers: this task's LogonType is 'Interactive', same as before - the AtStartup trigger fires at boot,"
Write-Host "but Windows still needs an interactive session (i.e. someone signed in, or Windows auto-logon configured for $env:USERNAME)"
Write-Host "before an Interactive-logon task can actually run. If this server reboots with nobody signed in and no auto-logon set up,"
Write-Host "enable Windows auto sign-in for this account (Sysinternals Autologon, or the netplwiz / AutoAdminLogon registry value) so"
Write-Host "the portal truly comes back up unattended after a restart."
