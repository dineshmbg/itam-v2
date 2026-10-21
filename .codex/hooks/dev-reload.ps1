# PostToolUse hook (Write|Edit): keep the dev server in step with the code.
#   portal/frontend/src/**  -> rebuild the front end (the server serves static files live, no restart needed)
#   portal/app, portal/db, tools (*.py) -> restart the "ITAM Portal" Scheduled Task
# Silent and always exits 0 so it can never block an edit. Log: %TEMP%\dev-reload.log
$ErrorActionPreference = 'Stop'
$log = Join-Path $env:TEMP 'dev-reload.log'
try {
  $in = [Console]::In.ReadToEnd() | ConvertFrom-Json
  $f = ($in.tool_input.file_path -replace '\\', '/')
  if (-not $f) { exit 0 }
  $root = (Resolve-Path (Join-Path $PSScriptRoot '../..')).Path -replace '\\', '/'
  if ($f.StartsWith($root + '/')) { $f = $f.Substring($root.Length + 1) }
  if ($f -match '^portal/frontend/src/') {
    Push-Location (Join-Path $root 'portal/frontend')
    node build.mjs *> $null
    Pop-Location
    "$(Get-Date -Format s) build   $f" | Add-Content $log
  }
  elseif ($f -match '^(portal/(app|db)|tools)/.+\.py$') {
    Stop-ScheduledTask -TaskName 'ITAM Portal'
    Start-Sleep 2
    Start-ScheduledTask -TaskName 'ITAM Portal'
    "$(Get-Date -Format s) restart $f" | Add-Content $log
  }
} catch { "$(Get-Date -Format s) ERROR $($_.Exception.Message)" | Add-Content $log }
exit 0
