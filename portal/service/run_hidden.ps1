# Runs the ITAM Portal in the background. Launched by the "ITAM Portal" Windows Scheduled Task at logon
# (see service/install.ps1), so the portal comes up automatically and keeps running independently of any
# terminal, IDE, or Claude Code session. Task Scheduler restarts this script automatically if it exits.
#
# Logs to service\logs\portal.log (rotated to portal.log.old once it passes 10 MB).
$ErrorActionPreference = 'Stop'
$serviceDir = $PSScriptRoot
$root = Split-Path -Parent $serviceDir            # portal/
Set-Location $root

$logDir = Join-Path $serviceDir 'logs'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$log = Join-Path $logDir 'portal.log'
if ((Test-Path $log) -and (Get-Item $log).Length -gt 10MB) {
  Move-Item -Force $log (Join-Path $logDir 'portal.log.old')
}
function Log($msg) { "$(Get-Date -Format o)  $msg" | Add-Content -Path $log }

$py = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path $py)) {
  Log '.venv is missing - run run_portal.ps1 once manually first to build it from vendor\wheels.'
  exit 1
}

# PostgreSQL (its own Windows service) may still be starting right after logon/reboot - wait up to ~30s for it.
function Wait-ForPostgres {
  for ($i = 0; $i -lt 10; $i++) {
    try { (New-Object Net.Sockets.TcpClient('localhost', 5432)).Close(); return $true } catch { Start-Sleep -Seconds 3 }
  }
  return $false
}
Log 'starting - waiting for PostgreSQL...'
if (-not (Wait-ForPostgres)) { Log 'PostgreSQL did not become reachable in time - continuing anyway (setup/serve will report the real error).' }

# PYTHONIOENCODING + piping through Out-File (rather than *>>) keeps the log plain UTF-8 - without this,
# Python's Windows console auto-detection can write UTF-16 and the log becomes unreadable ("P o r t a l").
$env:PYTHONIOENCODING = 'utf-8'
& $py 'db\setup.py' 2>&1 | Out-File -FilePath $log -Append -Encoding utf8

# Safety net: if the previous instance's process was not cleanly reaped (observed once: Stop-ScheduledTask did
# not kill the python.exe child), free the port before starting - this script is the only intended owner of it.
Get-NetTCPConnection -LocalPort 8420 -State Listen -ErrorAction SilentlyContinue | ForEach-Object {
  try { Stop-Process -Id $_.OwningProcess -Force -ErrorAction Stop; Log "stopped a leftover process (PID $($_.OwningProcess)) still holding port 8420" } catch {}
}

Log 'launching serve.py'
# Listen on every network address so PCs on the LAN can reach the portal - and so the activity log records each PC's own address
# (bound to 127.0.0.1 every connection would look like it came from the server itself). Set PORTAL_HOST=127.0.0.1 to keep it local-only.
# Every backup is also copied to a second physical disk, so one failed disk cannot take the database and its backups together.
# Override with the PORTAL_BACKUP_COPY_DIR environment variable; set it to an empty value to switch the copy off.
if ($null -eq $env:PORTAL_BACKUP_COPY_DIR -and (Test-Path 'D:')) { $env:PORTAL_BACKUP_COPY_DIR = 'D:\itam-v2-backup' }
$bind = if ($env:PORTAL_HOST) { $env:PORTAL_HOST } else { '0.0.0.0' }
& $py 'serve.py' --host $bind --port 8420 2>&1 | Out-File -FilePath $log -Append -Encoding utf8
Log "serve.py exited with code $LASTEXITCODE"
exit $LASTEXITCODE
