<#
.SYNOPSIS
  One-time data fix, run on the physical database server: fills asset.hostname with the asset's own CI number wherever
  hostname is blank (matches the fix already applied on the dev database). Backs the database up first.

.DESCRIPTION
  Steps: (1) pg_dump the whole database via Backup-Database.ps1, next to this script, (2) count how many asset rows currently
  have no hostname, (3) ask you to type YES before writing anything (skip with -Force), (4) run the update, (5) verify the
  blank count is now 0. Safe to run more than once - it only ever touches rows that are still blank, so a second run just
  finds nothing left to do.

.EXAMPLE
  .\Backfill-Hostname.ps1 -BackupTo D:\itam-db-backups
#>
[CmdletBinding()]
param(
  [Parameter(Mandatory)][string]$BackupTo,
  [string]$PgBin,
  [string]$PgHost = 'localhost',
  [int]$Port = 5432,
  [string]$User = 'postgres',
  [string]$DbName = 'ongc_ank',
  [switch]$Force
)
$ErrorActionPreference = 'Stop'

if (-not $PgBin) {
  $f = Get-ChildItem 'C:\Program Files\PostgreSQL' -Directory -ErrorAction SilentlyContinue | Sort-Object { [int]($_.Name -replace '\D', '') } -Descending |
    Where-Object { Test-Path (Join-Path $_.FullName 'bin\psql.exe') } | Select-Object -First 1
  if ($f) { $PgBin = Join-Path $f.FullName 'bin' }
}
if (-not $PgBin -or -not (Test-Path (Join-Path $PgBin 'psql.exe'))) { throw "psql.exe not found - give the folder with -PgBin" }
$psql = Join-Path $PgBin 'psql.exe'

if (-not $env:PGPASSWORD -and -not $env:PGPASSFILE) {
  $sec = Read-Host -AsSecureString "Password of PostgreSQL user '$User'"
  $env:PGPASSWORD = [Runtime.InteropServices.Marshal]::PtrToStringBSTR([Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec))
}

function Invoke-Psql([string]$Sql) {
  $prev = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
  $out = & $psql -h $PgHost -p "$Port" -U $User -d $DbName -At -c $Sql 2>&1
  $code = $LASTEXITCODE; $ErrorActionPreference = $prev
  if ($code -ne 0) { throw "psql failed: $(($out | ForEach-Object { "$_" }) -join ' ')" }
  return $out
}

Write-Host "==> Backing up $DbName first" -ForegroundColor Cyan
$backupScript = Join-Path $PSScriptRoot 'Backup-Database.ps1'
if (-not (Test-Path $backupScript)) { throw "Backup-Database.ps1 not found next to this script - run this from deploy\host\" }
& $backupScript -To $BackupTo -PgBin $PgBin -PgHost $PgHost -Port $Port -User $User -DbName $DbName

Write-Host "`n==> Checking how many asset rows have a blank hostname" -ForegroundColor Cyan
$countSql = "SELECT count(*) FROM asset WHERE record_level='ASSET' AND (hostname IS NULL OR hostname='');"
$before = (Invoke-Psql $countSql).Trim()
Write-Host "    $before row(s) currently have no hostname recorded."

if ([int]$before -eq 0) {
  Write-Host "Nothing to do - every asset already has a hostname." -ForegroundColor Green
  exit 0
}

if (-not $Force) {
  $answer = Read-Host "Type YES to set hostname = Asset (CI) for these $before row(s)"
  if ($answer -ne 'YES') { Write-Host "Cancelled - nothing was changed." -ForegroundColor Yellow; exit 1 }
}

Write-Host "`n==> Updating" -ForegroundColor Cyan
Invoke-Psql "UPDATE asset SET hostname = asset_key WHERE record_level='ASSET' AND (hostname IS NULL OR hostname='');" | Out-Null

$after = (Invoke-Psql $countSql).Trim()
Write-Host "`n==> Done. Blank hostnames: $before -> $after" -ForegroundColor Green
if ([int]$after -ne 0) { Write-Host "Still $after row(s) blank - worth a look before assuming this is complete." -ForegroundColor Yellow }
