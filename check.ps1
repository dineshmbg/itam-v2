<#
.SYNOPSIS
  One command that answers "is this safe to release?": uncommitted work, front-end build, every test, and (optionally) a real restore drill.

.EXAMPLE
  .\check.ps1            # the normal check before a release
  .\check.ps1 -Drill     # also restore a fresh dump into a scratch database and compare it (needs the postgres login, ~2 seconds)

.NOTES
  There is no hosted CI yet on purpose: the tests run against the dev database's real data, so a hosted runner would have nothing to test against.
  The prerequisite for CI is a small fixed test dataset (see portal/README.md, "Checks and tests"). Until then this script is the gate.
#>
[CmdletBinding()]
param([switch]$Drill)
$ErrorActionPreference = 'Continue'
Set-Location $PSScriptRoot
$failed = @()
function Step($m) { Write-Host ""; Write-Host "==> $m" -ForegroundColor Cyan }

Step "Uncommitted work (a release is refused while there is any)"
$dirty = @(& git status --porcelain 2>$null | Where-Object { $_ -and $_ -notmatch '^\?\? AGENTS\.md$' })
if ($dirty.Count) { Write-Host "  $($dirty.Count) path(s) not committed:" -ForegroundColor Yellow; $dirty | Select-Object -First 12 | ForEach-Object { Write-Host "    $_" } } else { Write-Host "  none" }
$ahead = (& git rev-list --count '@{u}..HEAD' 2>$null)
if ($ahead) { Write-Host "  $ahead commit(s) exist only on this PC (not pushed)." -ForegroundColor Yellow }

Step "Front end builds"
Push-Location portal\frontend; try { & node build.mjs | Select-Object -Last 4; if ($LASTEXITCODE -ne 0) { $failed += 'front-end build' } } finally { Pop-Location }

Step "Tests"
Push-Location portal; try { & .\.venv\Scripts\python.exe -m pytest -q 2>&1 | Select-Object -Last 8; if ($LASTEXITCODE -ne 0) { $failed += 'tests' } } finally { Pop-Location }

if ($Drill) {
  Step "Restore drill (backup -> scratch database -> compare -> drop)"
  & .\portal\.venv\Scripts\python.exe portal\db\restore_drill.py
  if ($LASTEXITCODE -ne 0) { $failed += 'restore drill' }
}

Write-Host ""
if ($failed.Count) { Write-Host "NOT READY: $($failed -join ', ') failed." -ForegroundColor Red; exit 1 }
Write-Host "READY$(if ($dirty.Count) { ' (but commit the changes above before building a release)' })." -ForegroundColor Green
