# Start the ITAM Portal. Works fully offline: Python packages are installed from vendor\wheels, the front end is pre-built.
#   .\run_portal.ps1            start on http://127.0.0.1:8420 and open the browser
#   .\run_portal.ps1 -NoBrowser -Port 9000
param([int]$Port = 8420, [switch]$NoBrowser, [switch]$SkipSetup)
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

if (-not (Test-Path '.venv\Scripts\python.exe')) {
  Write-Host 'Creating virtual environment from the bundled wheels (no internet needed)...'
  python -m venv .venv
  & .\.venv\Scripts\python.exe -m pip install --quiet --no-index --find-links vendor\wheels -r requirements.txt
}
$py = '.\.venv\Scripts\python.exe'

if (-not $SkipSetup) { & $py db\setup.py }   # idempotent: indexes, live-update triggers, engineer register

$serveArgs = @('serve.py', '--port', $Port)
if (-not $NoBrowser) { $serveArgs += '--open' }
& $py @serveArgs
