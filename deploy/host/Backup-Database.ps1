<#
.SYNOPSIS
  Backs up the portal database on the physical server (custom-format pg_dump - the same kind of file Prepare-Database.ps1 restores).

.DESCRIPTION
  Run it before an update, and schedule it (Task Scheduler, daily) as the server-side safety net: the portal's own backups are stored inside the
  VM, so a lost VM must not mean lost data. Old files beyond -Keep are removed from the target folder (only files this script made).

.EXAMPLE
  .\Backup-Database.ps1 -To D:\itam-db-backups -Keep 30
  # unattended:  set the password once for the account that runs the task:   [Environment]::SetEnvironmentVariable('PGPASSWORD','...','User')
#>
[CmdletBinding()]
param(
  [Parameter(Mandatory)][string]$To,
  [string]$PgBin,
  [string]$PgHost = 'localhost',
  [int]$Port = 5432,
  [string]$User = 'postgres',
  [string]$DbName = 'ongc_ank',
  [int]$Keep = 30
)
$ErrorActionPreference = 'Stop'
if (-not $PgBin) {
  $f = Get-ChildItem 'C:\Program Files\PostgreSQL' -Directory -ErrorAction SilentlyContinue | Sort-Object { [int]($_.Name -replace '\D', '') } -Descending |
    Where-Object { Test-Path (Join-Path $_.FullName 'bin\pg_dump.exe') } | Select-Object -First 1
  if ($f) { $PgBin = Join-Path $f.FullName 'bin' }
}
if (-not $PgBin -or -not (Test-Path (Join-Path $PgBin 'pg_dump.exe'))) { throw "pg_dump.exe not found - give the folder with -PgBin" }
if (-not $env:PGPASSWORD -and -not $env:PGPASSFILE) {
  $sec = Read-Host -AsSecureString "Password of PostgreSQL user '$User'"
  $env:PGPASSWORD = [Runtime.InteropServices.Marshal]::PtrToStringBSTR([Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec))
}
New-Item -ItemType Directory -Force $To | Out-Null
$file = Join-Path $To ("{0}_{1:yyyyMMdd_HHmmss}_server.dump" -f $DbName, (Get-Date))
$prev = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
$out = & (Join-Path $PgBin 'pg_dump.exe') -h $PgHost -p "$Port" -U $User -d $DbName -Fc -Z 6 --no-owner -f $file 2>&1
$code = $LASTEXITCODE; $ErrorActionPreference = $prev
if ($code -ne 0 -or -not (Test-Path $file) -or (Get-Item $file).Length -eq 0) { Remove-Item $file -Force -ErrorAction SilentlyContinue; throw "pg_dump failed: $(($out | ForEach-Object { "$_" }) -join ' ')" }
$hash = (Get-FileHash $file -Algorithm SHA256).Hash.ToLower()
"$hash *$(Split-Path $file -Leaf)" | Add-Content (Join-Path $To 'SHA256SUMS.txt') -Encoding ASCII
Write-Host ("Backup written: {0}  ({1:N1} MB)" -f $file, ((Get-Item $file).Length / 1MB)) -ForegroundColor Green
$old = Get-ChildItem $To -Filter "${DbName}_*_server.dump" | Sort-Object LastWriteTime -Descending | Select-Object -Skip $Keep
foreach ($o in $old) { Remove-Item $o.FullName -Force; Write-Host "  removed old backup $($o.Name)" }
