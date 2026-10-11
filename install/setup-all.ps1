#Requires -RunAsAdministrator
<#
.SYNOPSIS
    One-command setup for the unified s3rehan agent stack.
.DESCRIPTION
    Installs and configures EVERYTHING background agents need:
      [1] Virtual monitors (IddCx Virtual Display Driver) — each agent's
          own real screen, zero flicker on the user's display.
      [2] WSL2 + Ubuntu — Linux workers for non-Windows-UI tasks.
      [3] Python deps — dxcam, pillow (monitor capture), pyvda
          (virtual-desktop fallback).
      [4] Verification — monitor count, WSL status, import smoke test.
    Idempotent: safe to run twice. Storage-light: no VM disk images;
    WSL2 is ~1GB and returns RAM when idle.
.PARAMETER Monitors
    Number of virtual monitors (agents). Default 2.
.PARAMETER MemoryGB
    WSL2 memory cap in GB (default: min(4, 25% of host RAM)).
.PARAMETER SkipVDD
    Skip the virtual-display driver (virtual-desktop fallback only).
.PARAMETER SkipWSL
    Skip WSL2 setup.
.EXAMPLE
    .\setup-all.ps1
    .\setup-all.ps1 -Monitors 4 -MemoryGB 6
#>
param(
    [int]$Monitors = 2,
    [int]$MemoryGB = 0,
    [switch]$SkipVDD,
    [switch]$SkipWSL
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path

Write-Host "=== s3rehan unified setup ===" -ForegroundColor Cyan

# ---------------------------------------------------------------- [1] VDD
if (-not $SkipVDD) {
    Write-Host "`n[1/4] Virtual monitors (IddCx driver)..." -ForegroundColor Yellow
    $vddScript = Join-Path $ScriptDir "setup-virtual-monitors.ps1"
    if (Test-Path $vddScript) {
        & $vddScript -Count $Monitors
    } else {
        Write-Host "  Installing Virtual Display Driver via winget..." -ForegroundColor Yellow
        winget install --id VirtualDrivers.Virtual-Display-Driver -e --silent `
            --accept-package-agreements --accept-source-agreements
        $mons = (0..($Monitors - 1) | ForEach-Object {
            "  <monitor><id>$_</id>`n    <resolution><width>1920</width><height>1080</height><refresh>60</refresh></resolution>`n  </monitor>"
        }) -join "`n"
        $xml = "<?xml version=`"1.0`" encoding=`"utf-8`"?>`n<settings>`n$mons`n</settings>`n"
        foreach ($p in @("C:\IddSampleDriver\vdd_settings.xml", "C:\VirtualDisplayDriver\vdd_settings.xml")) {
            try {
                $dir = Split-Path $p
                if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
                Set-Content -Path $p -Value $xml -Encoding UTF8
                Write-Host "  wrote $p"
                break
            } catch { }
        }
    }
} else {
    Write-Host "`n[1/4] Skipped (virtual-desktop fallback will be used)." -ForegroundColor DarkYellow
}

# ---------------------------------------------------------------- [2] WSL2
if (-not $SkipWSL) {
    Write-Host "`n[2/4] WSL2 + Ubuntu..." -ForegroundColor Yellow
    $wsl = Get-Command wsl -ErrorAction SilentlyContinue
    if (-not $wsl) {
        Write-Host "  Enabling WSL..."
        wsl --install --no-distribution
        Write-Host "  NOTE: a reboot may be required before Ubuntu installs." -ForegroundColor DarkYellow
    }
    $distros = (wsl --list --quiet 2>$null) -join "`n"
    if ($distros -notmatch "Ubuntu") {
        Write-Host "  Installing Ubuntu (this takes a few minutes)..."
        wsl --install -d Ubuntu --no-launch
    } else {
        Write-Host "  Ubuntu already installed."
    }
    wsl --set-default-version 2 2>$null | Out-Null

    # Memory cap: min(4GB, 25% of host RAM) unless overridden.
    if ($MemoryGB -le 0) {
        $totalGB = [math]::Round((Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory / 1GB)
        $MemoryGB = [math]::Min(4, [math]::Max(2, [math]::Floor($totalGB * 0.25)))
    }
    $wslconfig = "$env:USERPROFILE\.wslconfig"
    $cfg = "[wsl2]`nmemory=${MemoryGB}GB`nprocessors=4`nswap=0`n"
    Set-Content -Path $wslconfig -Value $cfg -Encoding ASCII
    Write-Host "  .wslconfig: memory=${MemoryGB}GB (WSL2 returns RAM when idle)"

    # Base packages inside Ubuntu (idempotent).
    Write-Host "  Installing python3/pip in Ubuntu..."
    wsl --distribution Ubuntu -- bash -lc "sudo apt-get update -qq && sudo apt-get install -y -qq python3 python3-pip > /dev/null 2>&1 && sudo apt-get clean" 2>$null
    Write-Host "  (Run browser-automation's setup-wsl.ps1 for Playwright/Chromium inside WSL.)"
} else {
    Write-Host "`n[2/4] Skipped." -ForegroundColor DarkYellow
}

# ---------------------------------------------------------------- [3] pip
Write-Host "`n[3/4] Python deps (dxcam, pillow, pyvda)..." -ForegroundColor Yellow
python -m pip install --upgrade --quiet dxcam pillow pyvda
if ($LASTEXITCODE -ne 0) { Write-Warning "pip install had issues — install dxcam/pillow/pyvda manually." }

# ------------------------------------------------------------ [4] verify
Write-Host "`n[4/4] Verifying..." -ForegroundColor Yellow
$ok = $true
try {
    $n = python -c "from rehan.virtual_monitor import virtual_monitors; print(len(virtual_monitors()))" 2>$null
    Write-Host "  virtual monitors: $n"
    if ([int]$n -lt $Monitors -and -not $SkipVDD) {
        Write-Host "  (fewer than requested — open the VDD control app once to apply settings)" -ForegroundColor DarkYellow
    }
} catch { Write-Host "  virtual monitors: check failed ($_)"; $ok = $false }
try {
    $st = wsl --status 2>$null
    if ($LASTEXITCODE -eq 0) { Write-Host "  WSL: available" } else { Write-Host "  WSL: not ready (reboot may be needed)" -ForegroundColor DarkYellow }
} catch { Write-Host "  WSL: not installed (skipped or pending reboot)" -ForegroundColor DarkYellow }
try {
    python -c "import rehan.workers, rehan.agent_hierarchy, rehan.lanerouter; print('  python imports: ok')" 2>$null
    if ($LASTEXITCODE -ne 0) { Write-Host "  python imports: run from the s3rehan directory" -ForegroundColor DarkYellow }
} catch { }

Write-Host ""
if ($ok) { Write-Host "Done. Each agent now gets its own virtual monitor (own screen)." -ForegroundColor Green }
Write-Host "Isolation priority: virtual monitor > virtual desktop > shim."
Write-Host "Disable monitors anytime: set S3REHAN_NO_VMONITOR=1"
