<#
.SYNOPSIS
  One-time: creates the key pair that signs software-update packages. Only packages signed with YOUR private key are ever installed on the servers.

.DESCRIPTION
  The PRIVATE key stays on this PC (default %USERPROFILE%\.itam-release\release-private.pem, readable only by you) - never copy it to a server or into the project.
  The PUBLIC key is copied to deploy\release-public.pem; vm-install.sh puts it on the VM, and the update service there uses it to verify every package.
  BACK UP THE PRIVATE KEY (USB stick in a safe place): if it is lost, no new package can be signed until a new key pair is created and installed on the VM again.
  The script refuses to overwrite an existing key.
#>
[CmdletBinding()]
param([string]$KeyDir = (Join-Path $env:USERPROFILE '.itam-release'))
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot

function Find-OpenSsl {
  $c = Get-Command openssl -ErrorAction SilentlyContinue
  if ($c) { return $c.Source }
  foreach ($p in 'C:\Program Files\Git\mingw64\bin\openssl.exe', 'C:\Program Files\Git\usr\bin\openssl.exe', 'C:\Program Files\OpenSSL-Win64\bin\openssl.exe') { if (Test-Path $p) { return $p } }
  throw "openssl was not found (it comes with Git for Windows). Install Git for Windows or OpenSSL and try again."
}
$openssl = Find-OpenSsl
$priv = Join-Path $KeyDir 'release-private.pem'
$pub = Join-Path $KeyDir 'release-public.pem'
if (Test-Path $priv) { throw "A release key already exists at $priv - not overwriting it. (A new key would have to be installed on the VM again.)" }

New-Item -ItemType Directory -Force $KeyDir | Out-Null
# openssl writes progress messages to stderr, which Windows PowerShell 5.1 would treat as errors - only the exit code counts here
$ErrorActionPreference = 'Continue'
& $openssl genrsa -out $priv 3072 *> $null
if ($LASTEXITCODE -ne 0) { throw "could not create the private key" }
& $openssl rsa -in $priv -pubout -out $pub *> $null
if ($LASTEXITCODE -ne 0) { throw "could not create the public key" }
$ErrorActionPreference = 'Stop'
Copy-Item $pub (Join-Path $root 'deploy\release-public.pem') -Force

# only the current Windows user may read the private key
icacls $KeyDir /inheritance:r /grant:r "$($env:USERNAME):(OI)(CI)F" | Out-Null

Write-Host ""
Write-Host "Release key created." -ForegroundColor Green
Write-Host "  PRIVATE key : $priv   <- keep it here, and BACK IT UP (USB stick, safe place). Never copy it to a server."
Write-Host "  public key  : deploy\release-public.pem   <- goes to the VM (vm-install.sh does that)"
