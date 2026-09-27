# Removes the "ITAM Portal" Windows Scheduled Task. Stops the running instance first if there is one.
# The portal's files, database and data are untouched - this only removes the auto-start/auto-restart task.
$ErrorActionPreference = 'Stop'
$name = 'ITAM Portal'
if (Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue) {
  Stop-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
  Unregister-ScheduledTask -TaskName $name -Confirm:$false
  Write-Host "Removed scheduled task '$name'."
} else {
  Write-Host "No scheduled task named '$name' was found."
}
