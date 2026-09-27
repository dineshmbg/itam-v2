<#
.SYNOPSIS
  Prepares the PostgreSQL that is installed on THIS (physical) server for the ITAM Portal, and loads the inventory into it.

.DESCRIPTION
  Run on the physical server, in PowerShell (as Administrator when using -ConfigureNetwork).
  1. connects as the PostgreSQL superuser (asks for its password) and checks the server
  2. creates the application role (default ank_app - an ordinary role, not a superuser) with a password, and the database (default ongc_ank) owned by it
  3. restores the backup file into the database - ONLY if the database is still empty; existing data is never overwritten
  4. with -ConfigureNetwork: lets the Ubuntu VM (and only that address) connect - listen_addresses, one pg_hba.conf line, one firewall rule -
     and sets the server time zone to Asia/Kolkata (the portal shows times in the server's time zone)
  Everything is idempotent: running it again changes nothing that is already right. Config files are backed up before they are edited.

.EXAMPLE
  .\Prepare-Database.ps1 -DumpFile D:\itam\ongc_ank_20260923_231351_manual.dump -VmAddress 192.168.1.50 -ConfigureNetwork

.PARAMETER VmAddress
  The address the DATABASE will see the VM connecting from (the VM's IP address; a CIDR such as 192.168.1.0/24 also works).
#>
[CmdletBinding()]
param(
  [string]$DumpFile,
  [string]$VmAddress,
  [switch]$ConfigureNetwork,
  [string]$PgBin,
  [string]$PgHost = 'localhost',
  [int]$Port = 5432,
  [string]$AdminUser = 'postgres',
  [string]$DbName = 'ongc_ank',
  [string]$AppUser = 'ank_app',
  [securestring]$AppPassword,
  [string]$ServiceName,
  [string]$ConfigFile,
  [string]$HbaFile,
  [switch]$NoRestart,
  [switch]$NoFirewall
)
$ErrorActionPreference = 'Stop'
function Say($m)  { Write-Host ""; Write-Host "==> $m" -ForegroundColor Cyan }
function Ok($m)   { Write-Host "    $m" -ForegroundColor Green }
function Warn($m) { Write-Host "    WARNING: $m" -ForegroundColor Yellow }
function Plain([securestring]$s) { [Runtime.InteropServices.Marshal]::PtrToStringBSTR([Runtime.InteropServices.Marshal]::SecureStringToBSTR($s)) }

# ---- tools
if (-not $PgBin) {
  $found = Get-ChildItem 'C:\Program Files\PostgreSQL' -Directory -ErrorAction SilentlyContinue | Sort-Object { [int]($_.Name -replace '\D', '') } -Descending |
    Where-Object { Test-Path (Join-Path $_.FullName 'bin\psql.exe') } | Select-Object -First 1
  if ($found) { $PgBin = Join-Path $found.FullName 'bin' } else { $g = Get-Command psql.exe -ErrorAction SilentlyContinue; if ($g) { $PgBin = Split-Path $g.Source } }
}
if (-not $PgBin -or -not (Test-Path (Join-Path $PgBin 'psql.exe'))) { throw "psql.exe was not found. Give the folder with -PgBin, e.g. -PgBin 'C:\Program Files\PostgreSQL\18\bin'" }
$psql = Join-Path $PgBin 'psql.exe'; $pgrestore = Join-Path $PgBin 'pg_restore.exe'
if ($DumpFile -and -not (Test-Path $DumpFile)) { throw "Backup file not found: $DumpFile" }
if ($ConfigureNetwork -and -not $VmAddress) { throw "-ConfigureNetwork needs -VmAddress (the VM's IP address)" }

# ---- credentials (never written to disk; passed to the tools through environment variables only)
if (-not $env:PGPASSWORD) { $env:PGPASSWORD = Plain (Read-Host -AsSecureString "Password of the PostgreSQL user '$AdminUser'") }
$generated = $false; $appPw = $null
if ($AppPassword) { $appPw = Plain $AppPassword }

function Native([string]$Exe, [string[]]$Arguments) {
  $prev = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
  try { $out = & $Exe @Arguments 2>&1 } finally { $ErrorActionPreference = $prev }
  return @{ Code = $LASTEXITCODE; Text = (($out | ForEach-Object { "$_" }) -join "`n").Trim() }
}
function Psql([string]$Db, [string]$Sql, [string[]]$Vars = @()) {
  $args_ = @('-X', '-q', '-t', '-A', '-v', 'ON_ERROR_STOP=1', '-h', $PgHost, '-p', "$Port", '-U', $AdminUser, '-d', $Db) + $Vars + @('-c', $Sql)
  $r = Native $psql $args_
  if ($r.Code -ne 0) { throw "psql failed: $($r.Text)" }
  return $r.Text
}
function PsqlFile([string]$Db, [string]$Text, [string[]]$Vars = @()) {
  $tmp = [IO.Path]::GetTempFileName()
  try {
    [IO.File]::WriteAllText($tmp, $Text)
    $args_ = @('-X', '-q', '-v', 'ON_ERROR_STOP=1', '-h', $PgHost, '-p', "$Port", '-U', $AdminUser, '-d', $Db) + $Vars + @('-f', $tmp)
    $r = Native $psql $args_
    if ($r.Code -ne 0) { throw "psql failed: $($r.Text)" }
  } finally { Remove-Item $tmp -Force -ErrorAction SilentlyContinue }
}

# ---- 1. the server
Say "Checking PostgreSQL at ${PgHost}:$Port"
$ver = Psql 'postgres' 'SHOW server_version'
Ok "PostgreSQL $ver"
if (-not $ver.StartsWith('18')) { Warn "the backup was made with PostgreSQL 18; restoring into version $ver may fail" }
$tz = Psql 'postgres' 'SHOW TimeZone'
$collate = Psql 'postgres' "SELECT datcollate FROM pg_database WHERE datname = 'template1'"
Ok "time zone $tz, collation $collate, encoding $(Psql 'postgres' 'SHOW server_encoding')"
if ($collate -ne 'English_India.1252') { Warn "the original database uses collation English_India.1252 (Windows default here); this server uses '$collate' - sort order of text can differ slightly" }
$tzOk = ($tz -eq 'Asia/Kolkata' -or $tz -eq 'Asia/Calcutta')
if (-not $tzOk -and -not $ConfigureNetwork) { Warn "time zone is '$tz', the portal expects Asia/Kolkata (run again with -ConfigureNetwork to set it)" }

# ---- 2. role and database
Say "Application role '$AppUser' and database '$DbName'"
$roleExists = (Psql 'postgres' "SELECT count(*) FROM pg_roles WHERE rolname = '$AppUser'") -eq '1'
if ($roleExists -and -not $appPw) {
  # running again must never change the password the portal already uses - give -AppPassword to set a new one on purpose
  Psql 'postgres' "ALTER ROLE `"$AppUser`" LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE" | Out-Null
  Write-Host "    role '$AppUser' already exists - its password was left unchanged"
} else {
  if (-not $appPw) {
    $bytes = New-Object byte[] 32; [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    $appPw = -join ([Convert]::ToBase64String($bytes).ToCharArray() | Where-Object { $_ -match '[A-Za-z0-9]' } | Select-Object -First 26); $generated = $true
  }
  $env:ITAM_APP_PW = $appPw
  $roleSql = @'
\getenv pw ITAM_APP_PW
SELECT format('CREATE ROLE %I LOGIN PASSWORD %L', :'u', :'pw') WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'u') \gexec
SELECT format('ALTER ROLE %I LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD %L', :'u', :'pw') \gexec
'@
  PsqlFile 'postgres' $roleSql @('-v', "u=$AppUser")
}
$dbSql = @'
SELECT format('CREATE DATABASE %I OWNER %I ENCODING ''UTF8''', :'d', :'u') WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = :'d') \gexec
'@
PsqlFile 'postgres' $dbSql @('-v', "u=$AppUser", '-v', "d=$DbName")
Psql $DbName "ALTER SCHEMA public OWNER TO `"$AppUser`"" | Out-Null
Ok "role '$AppUser' (login, not a superuser) and database '$DbName' owned by it are ready"

# ---- 3. the data
Say "Inventory data"
$has = Psql $DbName "SELECT coalesce(to_regclass('public.asset')::text, '')"
if ($has) {
  $n = Psql $DbName "SELECT count(*) FROM asset WHERE is_current = 1"
  Ok "the database already holds the inventory ($n current assets) - restore skipped, nothing was overwritten"
} elseif ($DumpFile) {
  Write-Host "    restoring $DumpFile ... (a minute or two)"
  $r = Native $pgrestore @('-h', $PgHost, '-p', "$Port", '-U', $AdminUser, '-d', $DbName, '--no-owner', "--role=$AppUser", '--exit-on-error', $DumpFile)
  if ($r.Code -ne 0) { throw "pg_restore failed: $($r.Text)" }
  $tables = Psql $DbName "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public'"
  $n = Psql $DbName "SELECT count(*) FROM asset WHERE is_current = 1"
  Ok "restored: $tables tables, $n current assets"
} else {
  Warn "the database is empty and no -DumpFile was given - the portal will wait until a backup is restored"
}

# ---- 4. network access for the VM
if ($ConfigureNetwork) {
  Say "Letting the VM ($VmAddress) connect - and nobody else"
  if (-not $ConfigFile) { $ConfigFile = Psql 'postgres' 'SHOW config_file' }
  if (-not $HbaFile)    { $HbaFile    = Psql 'postgres' 'SHOW hba_file' }
  $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
  $restartNeeded = $false

  # postgresql.conf: listen_addresses
  $conf = Get-Content $ConfigFile
  $cur = Psql 'postgres' 'SHOW listen_addresses'
  if ($cur -eq '*' -or ($cur -split ',' | ForEach-Object { $_.Trim() }) -contains $VmAddress) { Ok "listen_addresses is already '$cur'" }
  elseif ($conf | Where-Object { $_ -match "^\s*listen_addresses\s*=\s*'\*'" }) {
    Warn "postgresql.conf already says listen_addresses = '*' but the running server still uses '$cur' - it has not been restarted since"; $restartNeeded = $true
  }
  else {
    Copy-Item $ConfigFile "$ConfigFile.bak-$stamp"
    $idx = -1; for ($i = 0; $i -lt $conf.Count; $i++) { if ($conf[$i] -match '^\s*listen_addresses\s*=') { $idx = $i } }
    $line = "listen_addresses = '*'          # ITAM Portal: the VM connects over the network; pg_hba.conf and the firewall restrict who may"
    if ($idx -ge 0) { $conf[$idx] = $line } else { $conf += $line }
    Set-Content -Path $ConfigFile -Value $conf -Encoding ASCII
    Ok "listen_addresses set to '*' (backup: $ConfigFile.bak-$stamp)"; $restartNeeded = $true
  }

  # pg_hba.conf: one line for the VM, this database, this role
  $cidr = if ($VmAddress.Contains('/')) { $VmAddress } else { "$VmAddress/32" }
  $hba = Get-Content $HbaFile
  $rule = "host    $DbName    $AppUser    $cidr    scram-sha-256    # ITAM Portal (Ubuntu VM)"
  if ($hba | Where-Object { $_ -match [regex]::Escape($DbName) -and $_ -match [regex]::Escape($AppUser) -and $_ -match [regex]::Escape($cidr) }) { Ok "pg_hba.conf already allows $AppUser@$DbName from $cidr" }
  else {
    Copy-Item $HbaFile "$HbaFile.bak-$stamp"
    Add-Content -Path $HbaFile -Value $rule -Encoding ASCII
    Ok "pg_hba.conf: added  $rule   (backup: $HbaFile.bak-$stamp)"
  }

  # time zone
  if (-not $tzOk) { Psql 'postgres' "ALTER SYSTEM SET TimeZone = 'Asia/Kolkata'" | Out-Null; Psql 'postgres' "ALTER SYSTEM SET log_timezone = 'Asia/Kolkata'" | Out-Null; Ok "time zone set to Asia/Kolkata" }

  # apply
  if ($restartNeeded -and -not $NoRestart) {
    if (-not $ServiceName) { $svc = Get-Service | Where-Object { $_.Name -like 'postgresql*' } | Select-Object -First 1; if ($svc) { $ServiceName = $svc.Name } }
    if (-not $ServiceName) { Warn "could not find the PostgreSQL Windows service - restart it yourself (listen_addresses needs a restart)" }
    else { Write-Host "    restarting service $ServiceName ..."; Restart-Service $ServiceName -Force; Start-Sleep 5; Ok "restarted" }
  } elseif ($restartNeeded) { Warn "restart PostgreSQL for listen_addresses to take effect" }
  else { Psql 'postgres' 'SELECT pg_reload_conf()' | Out-Null; Ok "configuration reloaded" }

  # firewall: only the VM may reach the port
  if (-not $NoFirewall) {
    $rn = "ITAM Portal PostgreSQL (from $VmAddress)"
    if (Get-NetFirewallRule -DisplayName $rn -ErrorAction SilentlyContinue) { Ok "firewall rule already exists" }
    else { New-NetFirewallRule -DisplayName $rn -Direction Inbound -Protocol TCP -LocalPort $Port -RemoteAddress $VmAddress -Action Allow -Profile Any | Out-Null; Ok "firewall: TCP $Port allowed from $VmAddress only" }
  }
}

# ---- done
Say "Done"
$addrs = @(Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue | Where-Object { $_.IPAddress -notlike '127.*' -and $_.IPAddress -notlike '169.254.*' } | ForEach-Object { $_.IPAddress })
Write-Host "    This server's addresses: $($addrs -join ', ')   (use the one the VM can reach as DB_HOST)"
Write-Host "    Database: $DbName    role: $AppUser    port: $Port"
if ($generated) {
  Write-Host ""
  Write-Host "    Password for role '$AppUser' (shown once - the VM installer asks for it):" -ForegroundColor Yellow
  Write-Host "        $appPw" -ForegroundColor Yellow
}
Write-Host ""
Write-Host "    On the Ubuntu VM:   sudo DB_HOST=<one of the addresses above> ./vm-install.sh"
$env:ITAM_APP_PW = $null
