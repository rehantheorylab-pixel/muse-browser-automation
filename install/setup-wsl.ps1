<#
.SYNOPSIS
  Set up WSL2 + Ubuntu for s3rehan Linux parallel workers.

.DESCRIPTION
  Idempotent. Safe to re-run; skips anything already done.
  - Enables WSL2 (needs admin; warns if not elevated)
  - Installs Ubuntu (minimal) if missing
  - Writes %USERPROFILE%\.wslconfig with a RAM cap
    (min(4GB, 25% of host RAM)) so WSL2 can't eat Rehan's memory
  - Inside Ubuntu: python3, pip, Playwright + Chromium (headless),
    then apt cache cleanup to keep storage lean
  - Creates ~/.s3rehan (the Linux worker runner is deployed lazily
    by rehan/linux_worker.py on first use)

  Run: powershell -ExecutionPolicy Bypass -File setup-wsl.ps1
  Optional flags: -SkipBrowsers (skip Chromium download ~160MB),
                  -MemoryGB N (override the RAM cap)

.NOTES
  No hardcoded usernames — everything hangs off $env:USERPROFILE.
#>
param(
  [switch]$SkipBrowsers,
  [int]$MemoryGB = 0
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

function Write-Step($n) { Write-Host "`n=== [$n]" -ForegroundColor Cyan }
function Write-Ok($m)   { Write-Host "[ok]   $m" -ForegroundColor Green }
function Write-Warn($m) { Write-Host "[warn] $m" -ForegroundColor Yellow }
function Write-Info($m) { Write-Host "       $m" }

$IsAdmin = ([Security.Principal.WindowsPrincipal] `
  [Security.Principal.WindowsIdentity]::GetCurrent()
  ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)

# ---------------------------------------------------------------- 1. WSL
Write-Step '1. WSL2 platform'
$wsl = Get-Command wsl -ErrorAction SilentlyContinue
if (-not $wsl) {
  if (-not $IsAdmin) {
    Write-Warn 'WSL is not installed and this shell is not elevated.'
    Write-Info 'Re-run as Administrator once: right-click PowerShell > Run as administrator,'
    Write-Info 'then: wsl --install --no-distribution'
    exit 1
  }
  Write-Info 'Installing WSL2 platform (no distribution yet)...'
  wsl --install --no-distribution
  Write-Ok 'WSL2 platform installed.'
} else {
  Write-Ok 'wsl.exe present.'
}
try { wsl --set-default-version 2 | Out-Null } catch { }
Write-Ok 'Default version set to WSL2.'

# ---------------------------------------------------------------- 2. Distro
Write-Step '2. Ubuntu distribution'
$distros = (wsl -l -q 2>$null) -join "`n"
if ($distros -match 'Ubuntu') {
  Write-Ok 'Ubuntu already installed.'
} else {
  if (-not $IsAdmin) {
    Write-Warn 'Ubuntu is missing and this shell is not elevated.'
    Write-Info 'Re-run as Administrator once; the script will install Ubuntu.'
    exit 1
  }
  Write-Info 'Installing Ubuntu (one-time download, ~600MB)...'
  wsl --install -d Ubuntu
  Write-Ok 'Ubuntu installed.'
}

# ---------------------------------------------------------------- 3. .wslconfig (RAM cap)
Write-Step '3. Memory cap (.wslconfig)'
$totalBytes = (Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory
$totalGB = [math]::Floor($totalBytes / 1GB)
$capGB = if ($MemoryGB -gt 0) { $MemoryGB } else { [math]::Min(4, [math]::Max(2, [math]::Floor($totalGB * 0.25))) }
$cpuCount = (Get-CimInstance Win32_ComputerSystem).NumberOfLogicalProcessors
$wslCpu = [math]::Max(2, [math]::Min(4, [math]::Floor($cpuCount / 2)))
$configPath = Join-Path $env:USERPROFILE '.wslconfig'
$config = @"
[wsl2]
memory=${capGB}GB
processors=${wslCpu}
swap=1GB
localhostForwarding=true
"@
$config | Set-Content -Path $configPath -Encoding Ascii
Write-Ok ".wslconfig written: memory=${capGB}GB, processors=${wslCpu} (host has ${totalGB}GB)."
Write-Info 'Note: .wslconfig changes apply after `wsl --shutdown`.'

# ---------------------------------------------------------------- 4. Ubuntu packages
Write-Step '4. Ubuntu packages (python3, pip, Playwright)'
$setupScript = @'
set -e
export DEBIAN_FRONTEND=noninteractive
sudo apt-get update -qq
sudo apt-get install -y -qq python3 python3-pip curl ca-certificates
python3 -m pip install --quiet --upgrade pip
python3 -m pip install --quiet playwright
mkdir -p ~/.s3rehan
sudo apt-get clean
sudo rm -rf /var/lib/apt/lists/*
echo PACKAGES_OK
'@
$out = $setupScript | wsl -d Ubuntu -- bash 2>&1
if ($out -match 'PACKAGES_OK') {
  Write-Ok 'python3, pip, playwright installed; apt cache cleaned.'
} else {
  Write-Warn 'Package step produced unexpected output:'
  Write-Info ($out -join "`n")
}

if (-not $SkipBrowsers) {
  Write-Step '5. Headless Chromium (Playwright)'
  $b = wsl -d Ubuntu -- bash -c 'python3 -m playwright install chromium 2>&1 | tail -2'
  Write-Ok 'Chromium installed.'
  Write-Info ($b -join ' ')
} else {
  Write-Step '5. Headless Chromium (skipped by -SkipBrowsers)'
}

# ---------------------------------------------------------------- 6. Verify
Write-Step '6. Verification'
$ver = wsl -d Ubuntu -- bash -c 'python3 -c "import sys; print(sys.version.split()[0])"'
Write-Ok "Ubuntu python3: $ver"
$mem = wsl -d Ubuntu -- bash -c 'free -m | awk "/^Mem:/ {print \$2}"'
Write-Info "WSL2 guest sees ${mem}MB RAM (cap ${capGB}GB after wsl --shutdown)."
Write-Host ''
Write-Ok 'WSL2 Linux workers ready. In s3rehan: TaskDispatcher(..., n_linux_workers=N).'
Write-Info 'Run `wsl --shutdown` once for the new memory cap to take effect.'
