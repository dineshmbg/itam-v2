<#
.SYNOPSIS
  Builds a new portal version on the development PC and packs it as ONE signed release file for the Software update page.

.DESCRIPTION
  Refuses to build while the working folder has uncommitted changes (override with -AllowDirty); the commit id is recorded in the signed notes.
  Steps: rebuild the front end (skip with -SkipFrontend), run the tests (skip with -SkipTests; a failing test stops the release), build the container image
  with Podman or Docker, then create deploy\releases\itam-release-<version>.itamrel = manifest.json + manifest.sig + itam-portal.tar, where the signature is made
  with your private release key (make-release-key.ps1, once). Upload that file on the portal's  Data tools > Software update  page - or copy it to the VM and run
  sudo ./update-portal.sh <file>. The plain image is also written to deploy\images\itam-portal.tar (only needed for the very first install with vm-install.sh).

  Database changes need nothing extra: schema changes live in portal\db\setup.py (idempotent) and are applied automatically when the new version starts.

.EXAMPLE
  .\deploy\build-release.ps1 -Notes "PM dashboard fix"
#>
[CmdletBinding()]
param(
  [string]$Version = (Get-Date -Format 'yyyyMMdd-HHmm'),
  [string]$Notes = '',
  [string]$KeyFile = (Join-Path $env:USERPROFILE '.itam-release\release-private.pem'),
  [ValidateSet('auto', 'podman', 'docker')][string]$Engine = 'auto',
  [switch]$SkipFrontend,
  [switch]$SkipTests,
  [switch]$AllowDirty
)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
function Say($m) { Write-Host ""; Write-Host "==> $m" -ForegroundColor Cyan }
function Find-OpenSsl {
  $c = Get-Command openssl -ErrorAction SilentlyContinue
  if ($c) { return $c.Source }
  foreach ($p in 'C:\Program Files\Git\mingw64\bin\openssl.exe', 'C:\Program Files\Git\usr\bin\openssl.exe', 'C:\Program Files\OpenSSL-Win64\bin\openssl.exe') { if (Test-Path $p) { return $p } }
  throw "openssl was not found (it comes with Git for Windows)."
}

# A release must be reproducible from a commit: refuse to package work that exists only in the working folder (that is how an
# uncommitted feature once rode along in a release). AGENTS.md is a copy of CLAUDE.md written by another tool and is ignored here.
$commit = (& git rev-parse --short HEAD 2>$null)
if ($commit) {
  $dirty = @(& git status --porcelain 2>$null | Where-Object { $_ -and $_ -notmatch '^\?\? AGENTS\.md$' })
  if ($dirty.Count -gt 0 -and -not $AllowDirty) {
    Write-Host "The working folder has changes that are not committed:" -ForegroundColor Yellow
    $dirty | Select-Object -First 15 | ForEach-Object { Write-Host "    $_" }
    if ($dirty.Count -gt 15) { Write-Host "    ... and $($dirty.Count - 15) more" }
    throw "Not building: commit (or set aside) those changes first so the release matches a commit - or pass -AllowDirty to build anyway."
  }
  $Notes = ("$Notes [commit $commit" + $(if ($dirty.Count -gt 0) { ", UNCOMMITTED CHANGES INCLUDED" } else { "" }) + "]").Trim()
  Write-Host "Building from commit $commit$(if ($dirty.Count -gt 0) { ' (plus uncommitted changes, -AllowDirty)' })" -ForegroundColor Cyan
}

if (-not (Test-Path $KeyFile)) { throw "Release signing key not found: $KeyFile`nCreate it once with  .\deploy\make-release-key.ps1  (and back it up)." }
$openssl = Find-OpenSsl
if ($Engine -eq 'auto') {
  if (Get-Command podman -ErrorAction SilentlyContinue) { $Engine = 'podman' }
  elseif (Get-Command docker -ErrorAction SilentlyContinue) { $Engine = 'docker' }
  else { throw "Neither podman nor docker was found." }
}
& $Engine info *> $null
if ($LASTEXITCODE -ne 0) { throw "$Engine is installed but not running (start Docker Desktop / the Podman machine first)." }

if (-not $SkipFrontend) {
  Say "Front end"
  Push-Location (Join-Path $root 'portal\frontend'); try { npm run build; if ($LASTEXITCODE -ne 0) { throw "front-end build failed" } } finally { Pop-Location }
}
if (-not $SkipTests) {
  Say "Tests (they run inside rolled-back transactions and never change data)"
  $py = Join-Path $root 'portal\.venv\Scripts\python.exe'
  Push-Location (Join-Path $root 'portal'); try { & $py -m pytest -q; if ($LASTEXITCODE -ne 0) { throw "tests failed - not building a release" } } finally { Pop-Location }
}

Say "Building image $Version with $Engine"
& $Engine build -f deploy/Containerfile --build-arg "ITAM_VERSION=$Version" -t localhost/itam-portal:latest -t "localhost/itam-portal:$Version" .
if ($LASTEXITCODE -ne 0) { throw "image build failed" }

Say "Saving the image"
$imgDir = Join-Path $root 'deploy\images'; New-Item -ItemType Directory -Force $imgDir | Out-Null
$tar = Join-Path $imgDir 'itam-portal.tar'; Remove-Item $tar -Force -ErrorAction SilentlyContinue
& $Engine save -o $tar localhost/itam-portal:latest "localhost/itam-portal:$Version"
if ($LASTEXITCODE -ne 0) { throw "image save failed" }
$hash = (Get-FileHash $tar -Algorithm SHA256).Hash.ToLower()
[IO.File]::WriteAllText((Join-Path $imgDir 'SHA256SUMS.txt'), "$hash *itam-portal.tar`n")
[IO.File]::WriteAllText((Join-Path $imgDir 'VERSION.txt'), "$Version`n")

Say "Signing and packing"
$work = Join-Path ([IO.Path]::GetTempPath()) ("itam-release-" + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory $work | Out-Null
try {
  $manifest = [ordered]@{ product = 'itam-portal'; version = $Version; built_at = (Get-Date -Format 's'); image = 'itam-portal.tar'; size = (Get-Item $tar).Length; sha256 = $hash; notes = $Notes }
  $mf = Join-Path $work 'manifest.json'; $sf = Join-Path $work 'manifest.sig'
  [IO.File]::WriteAllText($mf, (($manifest | ConvertTo-Json) -replace "`r`n", "`n") + "`n", (New-Object Text.UTF8Encoding($false)))
  $ErrorActionPreference = 'Continue'; & $openssl dgst -sha256 -sign $KeyFile -out $sf $mf *> $null; $code = $LASTEXITCODE; $ErrorActionPreference = 'Stop'
  if ($code -ne 0) { throw "signing failed" }
  $pubKey = Join-Path $root 'deploy\release-public.pem'
  if (Test-Path $pubKey) {
    $ErrorActionPreference = 'Continue'; & $openssl dgst -sha256 -verify $pubKey -signature $sf $mf *> $null; $code = $LASTEXITCODE; $ErrorActionPreference = 'Stop'
    if ($code -ne 0) { throw "the new signature does NOT verify against deploy\release-public.pem - the private key and the public key do not belong together" }
  }
  $relDir = Join-Path $root 'deploy\releases'; New-Item -ItemType Directory -Force $relDir | Out-Null
  $pkg = Join-Path $relDir "itam-release-$Version.itamrel"; Remove-Item $pkg -Force -ErrorAction SilentlyContinue
  Add-Type -AssemblyName System.IO.Compression.FileSystem
  $zip = [IO.Compression.ZipFile]::Open($pkg, 'Create')
  try {
    foreach ($pair in @(@($mf, 'manifest.json'), @($sf, 'manifest.sig'), @($tar, 'itam-portal.tar'))) {
      [void][IO.Compression.ZipFileExtensions]::CreateEntryFromFile($zip, $pair[0], $pair[1], [IO.Compression.CompressionLevel]::NoCompression)
    }
  } finally { $zip.Dispose() }
} finally { Remove-Item $work -Recurse -Force -ErrorAction SilentlyContinue }

Say "Ready: version $Version"
Write-Host ("    {0}  ({1} MB)" -f $pkg, [math]::Round((Get-Item $pkg).Length / 1MB))
Write-Host "    Upload it on the portal:  Data tools > Software update   - or copy it to the VM and run:  sudo ./update-portal.sh releases/itam-release-$Version.itamrel"
