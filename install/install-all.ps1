<#
.SYNOPSIS
  One-command installer for Muse Browser Automation v2 (Windows).

.DESCRIPTION
  Paste ONE command from the README into PowerShell and this script does
  everything: prerequisites, browser engines (Moli, Camoufox, Playwright,
  Obscura, Agent-Browser), the Chrome extension, the loopback daemons,
  ngrok public URL, and the daily cookie-export task. Idempotent — safe
  to re-run; it skips anything already installed or running.

  ONE-LINE INSTALL (fill in the published URL in the README):
    powershell -ExecutionPolicy Bypass -c "irm <INSTALLER-URL> | iex"

  Repo files (extension/, daemon/, pcagent/, tools/, obscura/, install/):
  the script uses them from a local checkout when present (repo root is
  the parent of this script's directory, or the current directory);
  otherwise it downloads $RepoZipUrl (set it when publishing, or pass
  -RepoZipUrl / $env:MUSE_BA_REPOZIP).

.NOTES
  No hardcoded usernames — everything hangs off $env:USERPROFILE.
  Paths with spaces (e.g. "C:\Users\AL Hussain Academy\...") are quoted
  everywhere; Obscura's --storage-dir uses the 8.3 short path.
#>
param(
  [string]$RepoZipUrl = $env:MUSE_BA_REPOZIP
)

$ProgressPreference = 'SilentlyContinue'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

# ---------------------------------------------------------------- constants
$Target      = Join-Path $env:USERPROFILE 'muse-browser-mcp'
$BinDir      = Join-Path $Target 'bin'
$ObscuraDir  = Join-Path $env:USERPROFILE 'obscura'
$ObscuraExe  = Join-Path $ObscuraDir 'obscura.exe'
$ProfileDir  = Join-Path $ObscuraDir 'profile'
$StartupDir  = [IO.Path]::Combine($env:APPDATA, 'Microsoft\Windows\Start Menu\Programs\Startup')
$DaemonPort  = 18010
$AgentPort   = 18011
$ObscuraPort = 9222
$MoliPort    = 9226

$global:Summary = [ordered]@{
  'Extension'          = 'NOT DEPLOYED'
  'Public URL (ngrok)' = 'NOT RUNNING'
}

# ---------------------------------------------------------------- helpers
function Write-Step($name) {
  Write-Host ''
  Write-Host "== $name ==" -ForegroundColor Cyan
}
function Write-Ok($msg)   { Write-Host "[ok]   $msg" -ForegroundColor Green }
function Write-Warn($msg) { Write-Host "[warn] $msg" -ForegroundColor Yellow }
function Write-Info($msg) { Write-Host '       ' $msg }

function Test-HttpOk($url, $timeoutSec = 4) {
  try {
    $r = Invoke-WebRequest -Uri $url -TimeoutSec $timeoutSec -UseBasicParsing
    return ($r.StatusCode -ge 200 -and $r.StatusCode -lt 300)
  } catch { return $false }
}

function Get-ShortPath($path) {
  try {
    $fso = New-Object -ComObject Scripting.FileSystemObject
    return $fso.GetFolder($path).ShortPath
  } catch { return $path }  # 8.3 disabled on this volume -> fall back to quoted long path
}

function Test-DaemonUp {
  # simpled.py: POST /tool {"tool":"ping"} (auth may be required -> retry with token)
  $body = '{"tool":"ping","args":{}}'
  try {
    Invoke-RestMethod -Uri "http://127.0.0.1:$DaemonPort/tool" -Method Post `
      -Body $body -ContentType 'application/json' -TimeoutSec 4 | Out-Null
    return $true
  } catch {
    $tokFile = Join-Path $Target 'daemon_token'
    if (Test-Path $tokFile) {
      try {
        $tok = (Get-Content $tokFile -Raw).Trim()
        Invoke-RestMethod -Uri "http://127.0.0.1:$DaemonPort/tool" -Method Post `
          -Body $body -ContentType 'application/json' `
          -Headers @{ Authorization = "Bearer $tok" } -TimeoutSec 4 | Out-Null
        return $true
      } catch { return $false }
    }
    return $false
  }
}

# ---------------------------------------------------------------- banner
Write-Host ''
Write-Host '============================================================' -ForegroundColor Cyan
Write-Host '  Muse Browser Automation v2 — one-command installer' -ForegroundColor Cyan
Write-Host '============================================================' -ForegroundColor Cyan
Write-Host "Target: $Target"

# ================================================================ 1. prerequisites
Write-Step '1. Prerequisites (Python 3.10+, Chrome, Git)'
try { $pyVer = & python --version 2>&1 } catch { $pyVer = $null }
if (-not $pyVer -or $pyVer -notmatch 'Python (\d+)\.(\d+)') {
  Write-Host 'ERROR: Python 3.10+ is required but was not found on PATH.' -ForegroundColor Red
  Write-Host 'Install it from https://www.python.org/downloads/ (tick "Add python.exe to PATH"),'
  Write-Host 'then re-run this script. Nothing else can proceed without Python.'
  exit 1
}
$major, $minor = [int]$Matches[1], [int]$Matches[2]
if ($major -lt 3 -or ($major -eq 3 -and $minor -lt 10)) {
  Write-Host "ERROR: found $pyVer — need Python 3.10 or newer." -ForegroundColor Red
  exit 1
}
Write-Ok $pyVer

$chromePaths = @(
  (Join-Path $env:ProgramFiles 'Google\Chrome\Application\chrome.exe'),
  (Join-Path ${env:ProgramFiles(x86)} 'Google\Chrome\Application\chrome.exe'),
  (Join-Path $env:LOCALAPPDATA 'Google\Chrome\Application\chrome.exe')
)
if ($chromePaths | Where-Object { Test-Path $_ }) { Write-Ok 'Google Chrome found' }
else { Write-Warn 'Google Chrome not found — the extension steps at the end need it.' ; $global:Summary['Chrome'] = 'NOT FOUND (needed for extension)' }

if (Get-Command git -ErrorAction SilentlyContinue) { Write-Ok 'Git found (optional)' }
else { Write-Info 'Git not found (optional — continuing without it).' }

# ================================================================ 2. repo files
Write-Step '2. Deploying repo files'
function Get-RepoRoot {
  $cands = @()
  if ($PSScriptRoot) { $cands += (Join-Path $PSScriptRoot '..') }
  $cands += (Get-Location).Path
  foreach ($c in $cands) {
    if (Test-Path (Join-Path $c 'extension\manifest.json')) { return (Resolve-Path $c).Path }
  }
  if (-not $RepoZipUrl) {
    throw ('Repo files not found locally and no download URL was given. ' +
           'Re-run with -RepoZipUrl <url> or set $env:MUSE_BA_REPOZIP to the published repo zip.')
  }
  Write-Info "Downloading repo zip from $RepoZipUrl ..."
  $zip  = Join-Path $env:TEMP 'muse-ba-repo.zip'
  $dest = Join-Path $env:TEMP 'muse-ba-repo'
  Invoke-WebRequest -Uri $RepoZipUrl -OutFile $zip
  if (Test-Path $dest) { Remove-Item $dest -Recurse -Force }
  Expand-Archive -Path $zip -DestinationPath $dest -Force
  $found = Get-ChildItem $dest -Recurse -Filter 'manifest.json' |
           Where-Object { $_.Directory.Name -eq 'extension' } | Select-Object -First 1
  if (-not $found) { throw 'Downloaded zip does not contain extension/manifest.json.' }
  return $found.Directory.Parent.FullName
}
try {
  $RepoRoot = Get-RepoRoot
  Write-Info "Repo: $RepoRoot"
  $copyDirs = @('extension', 'daemon', 'tools', 'pcagent', 'obscura', 'rehan')
  foreach ($d in $copyDirs) {
    $src = Join-Path $RepoRoot $d
    if (-not (Test-Path $src)) { Write-Warn "repo dir missing, skipped: $d"; continue }
    $dst = Join-Path $Target $d
    New-Item -ItemType Directory -Force -Path $dst | Out-Null
    Get-ChildItem -Path $src -Recurse -Force |
      Where-Object { $_.FullName -notmatch '__pycache__' } |
      ForEach-Object {
        $rel  = $_.FullName.Substring($src.Length + 1)
        $dest = Join-Path $dst $rel
        if ($_.PSIsContainer) { New-Item -ItemType Directory -Force -Path $dest | Out-Null }
        else { Copy-Item $_.FullName -Destination $dest -Force }
      }
    Write-Ok "copied $d -> $dst"
  }
  # autostart VBS launchers (prefer the repo's own StartMuseMCP.vbs; generate the rest)
  $vbsDaemon = Join-Path $RepoRoot 'install\StartMuseMCP.vbs'
  $daemonVbsText = @'
' Muse Browser MCP autostart (no hardcoded usernames or paths).
Set WshShell = CreateObject("WScript.Shell")
DaemonPath = WshShell.ExpandEnvironmentStrings("%USERPROFILE%") & "\muse-browser-mcp\daemon\simpled.py"
WshShell.Run """pythonw.exe"" """ & DaemonPath & """", 0, False
'@
  $agentVbsText = @'
' Muse PC Agent autostart (no hardcoded usernames or paths).
Set WshShell = CreateObject("WScript.Shell")
AgentPath = WshShell.ExpandEnvironmentStrings("%USERPROFILE%") & "\muse-browser-mcp\pcagent\pc_agent.py"
WshShell.Run """pythonw.exe"" """ & AgentPath & """", 0, False
'@
  if (Test-Path $vbsDaemon) { Copy-Item $vbsDaemon (Join-Path $StartupDir 'StartMuseMCP.vbs') -Force }
  else { $daemonVbsText | Out-File (Join-Path $StartupDir 'StartMuseMCP.vbs') -Encoding ASCII -Force }
  $agentVbsText | Out-File (Join-Path $StartupDir 'StartPCAgent.vbs') -Encoding ASCII -Force
  Write-Ok 'autostart launchers registered (StartMuseMCP.vbs, StartPCAgent.vbs)'
} catch {
  Write-Warn "repo deploy failed: $($_.Exception.Message)"
}

# ================================================================ 3. python deps
Write-Step '3. Python dependencies (websockets, pyautogui, playwright, camoufox)'
foreach ($pkg in @('websockets', 'pyautogui', 'playwright', 'camoufox')) {
  $mod = @{ websockets = 'websockets'; pyautogui = 'pyautogui'; playwright = 'playwright'; camoufox = 'camoufox' }[$pkg]
  $have = $false
  try { & python -c "import $mod" 2>$null; $have = ($LASTEXITCODE -eq 0) } catch { }
  if ($have) { Write-Ok "$pkg already installed"; continue }
  Write-Info "pip installing $pkg ..."
  & python -m pip install --quiet $pkg 2>&1 | Out-Null
  if ($LASTEXITCODE -eq 0) { Write-Ok $pkg } else { Write-Warn "pip install $pkg failed — continuing." }
}
Write-Info 'Installing Playwright Chromium (one-time download, ~170 MB) ...'
try {
  $pwDir = Join-Path $env:USERPROFILE '.cache\ms-playwright'
  if ((Test-Path $pwDir) -and (Get-ChildItem $pwDir -Directory | Where-Object { $_.Name -like 'chromium-*' })) {
    Write-Ok 'Playwright Chromium already downloaded'
  } else {
    & python -m playwright install chromium 2>&1 | Out-Null
    if ($LASTEXITCODE -eq 0) { Write-Ok 'Playwright Chromium installed' }
    else { Write-Warn 'playwright install chromium failed — continuing.' }
  }
} catch { Write-Warn "Playwright browser install failed: $($_.Exception.Message)" }
Write-Info 'Fetching Camoufox browser build (first run only) ...'
try {
  & camoufox fetch 2>&1 | Out-Null
  if ($LASTEXITCODE -eq 0) { Write-Ok 'Camoufox browser fetched' }
  else { Write-Warn 'camoufox fetch failed — it will fetch on first use instead.' }
} catch { Write-Warn 'camoufox fetch failed — it will fetch on first use instead.' }

# ================================================================ 4. Moli
Write-Step '4. Moli (Rust browser engine, CDP)'
$moliExe = $null
try {
  $cmd = Get-Command moli -ErrorAction SilentlyContinue
  if ($cmd) { $moliExe = $cmd.Source }
  elseif (Test-Path (Join-Path $env:LOCALAPPDATA 'Moli\bin\moli.exe')) { $moliExe = Join-Path $env:LOCALAPPDATA 'Moli\bin\moli.exe' }
  if (-not $moliExe) {
    Write-Info 'Installing Moli via the official installer ...'
    powershell -ExecutionPolicy Bypass -c "irm https://github.com/lexmount/moli/releases/latest/download/moli-installer.ps1 | iex" 2>&1 | Out-Null
    $cmd = Get-Command moli -ErrorAction SilentlyContinue
    if ($cmd) { $moliExe = $cmd.Source }
    elseif (Test-Path (Join-Path $env:LOCALAPPDATA 'Moli\bin\moli.exe')) { $moliExe = Join-Path $env:LOCALAPPDATA 'Moli\bin\moli.exe' }
  }
  if ($moliExe) {
    $ver = & $moliExe --version 2>&1
    Write-Ok "Moli $ver"
    if (-not (Test-HttpOk "http://127.0.0.1:$MoliPort/json/version")) {
      # discover how this moli build selects the serve port (don't guess flags)
      $help = & $moliExe serve --help 2>&1 | Out-String
      $portFlag = $null
      foreach ($f in @('--cdp-port', '--port', '-p')) { if ($help -match [regex]::Escape($f)) { $portFlag = $f; break } }
      if ($portFlag) {
        Start-Process -FilePath $moliExe -ArgumentList @('serve', '--layout', $portFlag, "$MoliPort") -WindowStyle Hidden
        Start-Sleep -Seconds 3
        if (Test-HttpOk "http://127.0.0.1:$MoliPort/json/version") { Write-Ok "Moli CDP serving on 127.0.0.1:$MoliPort" }
        else { Write-Warn 'Moli started but did not answer — check manually.' }
      } else {
        Write-Warn 'This moli build exposes no documented port flag; leaving it installed (start with: moli serve).'
        $global:Summary['Moli CDP'] = 'binary installed — start manually: moli serve'
      }
    } else { Write-Ok "Moli already serving on 127.0.0.1:$MoliPort" }
  } else { Write-Warn 'Moli install did not produce a binary — continuing.' }
} catch { Write-Warn "Moli step failed: $($_.Exception.Message)" }

# ================================================================ 5. Obscura
Write-Step '5. Obscura (stealth browser, CDP :9222)'
try {
  if (-not (Test-Path $ObscuraExe)) {
    Write-Info 'Downloading Obscura stealth build (h4ckf0r0day/obscura, latest release) ...'
    $rel = Invoke-RestMethod -Uri 'https://api.github.com/repos/h4ckf0r0day/obscura/releases/latest' `
             -Headers @{ 'User-Agent' = 'Muse-Browser-Automation/2.0' }
    $asset = $rel.assets | Where-Object { $_.name -eq 'obscura-x86_64-windows-stealth.zip' } | Select-Object -First 1
    if ($asset) {
      New-Item -ItemType Directory -Force -Path $ObscuraDir | Out-Null
      $zip = Join-Path $env:TEMP 'obscura-win-stealth.zip'
      Invoke-WebRequest -Uri $asset.browser_download_url -OutFile $zip
      Expand-Archive -Path $zip -DestinationPath $ObscuraDir -Force
      Write-Ok "Obscura downloaded -> $ObscuraDir"
    } else { Write-Warn 'Stealth asset not found in the latest release.' }
  }
  if (-not (Test-Path $ObscuraExe)) {
    $p = Read-Host 'obscura.exe was not found or downloaded. Enter its full path (or press Enter to skip Obscura)'
    if ($p -and (Test-Path $p)) {
      New-Item -ItemType Directory -Force -Path $ObscuraDir | Out-Null
      Copy-Item $p $ObscuraExe -Force
      Write-Ok "Obscura copied -> $ObscuraExe"
    } else { Write-Warn 'Obscura skipped.' }
  } else { Write-Ok "Obscura present: $ObscuraExe" }

  if (Test-Path $ObscuraExe) {
    if (-not (Test-HttpOk "http://127.0.0.1:$ObscuraPort/json/version")) {
      New-Item -ItemType Directory -Force -Path $ProfileDir | Out-Null
      $shortProfile = Get-ShortPath $ProfileDir   # 8.3 short path: handles "AL Hussain Academy"
      Write-Info "storage-dir (8.3): $shortProfile"
      Start-Process -FilePath $ObscuraExe `
        -ArgumentList @('serve', '--host', '127.0.0.1', '--port', "$ObscuraPort", '--stealth', '--storage-dir', $shortProfile) `
        -WindowStyle Hidden
      Start-Sleep -Seconds 4
      if (Test-HttpOk "http://127.0.0.1:$ObscuraPort/json/version") { Write-Ok "Obscura serving hidden on 127.0.0.1:$ObscuraPort" }
      else { Write-Warn 'Obscura did not answer on :9222 — check the binary runs.' }
    } else { Write-Ok "Obscura already serving on 127.0.0.1:$ObscuraPort" }
    # autostart on login (bakes in the 8.3 storage path)
    $shortProfile = Get-ShortPath $ProfileDir
    $obscuraVbs = @"
' Obscura autostart (hidden). Storage path baked in as 8.3 short path.
Set WshShell = CreateObject("WScript.Shell")
WshShell.Run """$ObscuraExe"" serve --host 127.0.0.1 --port $ObscuraPort --stealth --storage-dir ""$shortProfile""", 0, False
"@
    $obscuraVbs | Out-File (Join-Path $StartupDir 'StartObscura.vbs') -Encoding ASCII -Force
    Write-Ok 'Obscura autostart registered (StartObscura.vbs)'
  }
} catch { Write-Warn "Obscura step failed: $($_.Exception.Message)" }

# ================================================================ 6. Agent-Browser
Write-Step '6. Agent-Browser CLI (optional)'
try {
  if (Get-Command agent-browser -ErrorAction SilentlyContinue) { Write-Ok 'agent-browser already on PATH' }
  elseif (Get-Command npm -ErrorAction SilentlyContinue) {
    Write-Info 'Trying npm install -g agent-browser ...'
    & npm install -g agent-browser 2>&1 | Out-Null
    if ((Get-Command agent-browser -ErrorAction SilentlyContinue)) { Write-Ok 'agent-browser installed' }
    else { Write-Warn 'agent-browser not available via npm — skipped.' }
  } else { Write-Info 'agent-browser not found and npm is missing — skipped (optional).' }
} catch { Write-Warn "agent-browser step failed: $($_.Exception.Message)" }

# ================================================================ 7. Chrome extension
Write-Step '7. Chrome extension (Muse Browser Control)'
$extDir = Join-Path $Target 'extension'
if (Test-Path (Join-Path $extDir 'manifest.json')) {
  Write-Ok "extension staged at $extDir"
  Write-Host ''
  Write-Host '  MANUAL STEP (Chrome, ~1 minute — unpacked extensions cannot be scripted):' -ForegroundColor Cyan
  Write-Host '    1. Open chrome://extensions'
  Write-Host "    2. Enable 'Developer mode' (top right)"
  Write-Host '    3. Click "Load unpacked" and select:'
  Write-Host "         $extDir"
  Write-Host '    4. Confirm the "Muse Browser Control" extension is enabled.'
  $global:Summary['Extension'] = "load manually from $extDir (chrome://extensions -> Load unpacked)"
} else {
  Write-Warn 'extension files were not deployed — copy install step 2 output manually.'
  $global:Summary['Extension'] = 'NOT DEPLOYED'
}

# ================================================================ 8. daemon :18010
Write-Step '8. Daemon (simpled.py) on 127.0.0.1:18010'
try {
  $daemonPy = Join-Path $Target 'daemon\simpled.py'
  if (Test-DaemonUp) { Write-Ok 'daemon already running on 127.0.0.1:18010' }
  elseif (Test-Path $daemonPy) {
    Start-Process -FilePath 'pythonw.exe' -ArgumentList "`"$daemonPy`"" -WindowStyle Hidden
    Start-Sleep -Seconds 4
    if (Test-DaemonUp) { Write-Ok 'daemon started on 127.0.0.1:18010' }
    else { Write-Warn 'daemon did not answer yet — it may still be starting; check ~/muse-browser-mcp/simpled.log.' }
  } else { Write-Warn "daemon script missing: $daemonPy" }
} catch { Write-Warn "daemon step failed: $($_.Exception.Message)" }

# ================================================================ 9. PC agent :18011
Write-Step '9. PC Agent (pc_agent.py) on 127.0.0.1:18011'
try {
  $agentPy = Join-Path $Target 'pcagent\pc_agent.py'
  if (Test-HttpOk "http://127.0.0.1:$AgentPort/health") { Write-Ok 'PC Agent already running on 127.0.0.1:18011' }
  elseif (Test-Path $agentPy) {
    Start-Process -FilePath 'pythonw.exe' -ArgumentList "`"$agentPy`"" -WindowStyle Hidden
    Start-Sleep -Seconds 4
    if (Test-HttpOk "http://127.0.0.1:$AgentPort/health") { Write-Ok 'PC Agent started on 127.0.0.1:18011' }
    else { Write-Warn 'PC Agent did not answer yet — it may still be starting.' }
  } else { Write-Warn "PC Agent script missing: $agentPy" }
} catch { Write-Warn "PC Agent step failed: $($_.Exception.Message)" }

# ================================================================ 10. ngrok
Write-Step '10. ngrok (public URL for the daemon)'
$ngrokExe = $null
try {
  $ng = Get-Command ngrok -ErrorAction SilentlyContinue
  if ($ng) { $ngrokExe = $ng.Source } else { $ngrokExe = Join-Path $BinDir 'ngrok.exe' }
  if (-not (Test-Path $ngrokExe)) {
    Write-Info 'Downloading ngrok v3 (windows-amd64) ...'
    New-Item -ItemType Directory -Force -Path $BinDir | Out-Null
    $zip = Join-Path $env:TEMP 'ngrok.zip'
    Invoke-WebRequest -Uri 'https://bin.equinox.io/c/bNyj1mQVY4c/ngrok-v3-stable-windows-amd64.zip' -OutFile $zip
    Expand-Archive -Path $zip -DestinationPath $BinDir -Force
    # add to the user's PATH (idempotent)
    $userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
    if ($userPath -notlike "*$BinDir*") {
      [Environment]::SetEnvironmentVariable('Path', "$userPath;$BinDir", 'User')
      $env:Path += ";$BinDir"
      Write-Ok "added $BinDir to user PATH (new terminals)"
    }
  }
  if (Test-Path $ngrokExe) { Write-Ok "ngrok at $ngrokExe" } else { throw 'ngrok binary missing after download.' }

  # authtoken
  $cfgOk = $false
  try { $chk = & $ngrokExe config check 2>&1 | Out-String; $cfgOk = ($LASTEXITCODE -eq 0) -and ($chk -match 'Valid') } catch { }
  if (-not $cfgOk) {
    Write-Host '  ngrok needs an authtoken (free). Opening your dashboard ...' -ForegroundColor Cyan
    Start-Process 'https://dashboard.ngrok.com/get-started/your-authtoken'
    $sec = Read-Host '  Paste your ngrok authtoken here'
    if ($sec) {
      & $ngrokExe config add-authtoken $sec 2>&1 | Out-Null
      $sec = $null
      Write-Ok 'authtoken saved'
    } else { Write-Warn 'no authtoken given — ngrok tunnel skipped.' }
  } else { Write-Ok 'ngrok authtoken already configured' }

  # start tunnel if none is up
  $tunnels = $null
  try { $tunnels = Invoke-RestMethod -Uri 'http://127.0.0.1:4040/api/tunnels' -TimeoutSec 4 } catch { }
  if (-not $tunnels -or -not $tunnels.tunnels -or $tunnels.tunnels.Count -eq 0) {
    Write-Info "Starting: ngrok http $DaemonPort (hidden) ..."
    Start-Process -FilePath $ngrokExe -ArgumentList @('http', "$DaemonPort") -WindowStyle Hidden
    Start-Sleep -Seconds 5
    try { $tunnels = Invoke-RestMethod -Uri 'http://127.0.0.1:4040/api/tunnels' -TimeoutSec 4 } catch { }
  } else { Write-Ok 'ngrok tunnel already running' }
  $publicUrl = $null
  if ($tunnels -and $tunnels.tunnels -and $tunnels.tunnels.Count -gt 0) {
    $publicUrl = $tunnels.tunnels[0].public_url
    Write-Ok "public URL: $publicUrl"
    # autostart ngrok on login so the URL survives reboots
    $ngrokVbs = @"
' ngrok tunnel autostart (hidden): exposes the Muse daemon on $DaemonPort.
Set WshShell = CreateObject("WScript.Shell")
WshShell.Run """$ngrokExe"" http $DaemonPort", 0, False
"@
    $ngrokVbs | Out-File (Join-Path $StartupDir 'StartNgrok.vbs') -Encoding ASCII -Force
    Write-Ok 'ngrok autostart registered (StartNgrok.vbs)'
  } else {
    Write-Warn 'ngrok tunnel is not up — check the authtoken and retry.'
  }
  $global:Summary['Public URL (ngrok)'] = $(if ($publicUrl) { $publicUrl } else { 'NOT RUNNING' })
} catch { Write-Warn "ngrok step failed: $($_.Exception.Message)"; $global:Summary['Public URL (ngrok)'] = 'FAILED' }

# ================================================================ 11. cookie export task
Write-Step '11. Daily cookie export (04:00)'
try {
  $exporter = Join-Path $Target 'tools\export_cookies.py'
  if (Test-Path $exporter) {
    $action   = New-ScheduledTaskAction -Execute 'pythonw.exe' -Argument "`"$exporter`""
    $trigger  = New-ScheduledTaskTrigger -Daily -At 04:00
    $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable
    Register-ScheduledTask -TaskName 'MuseCookieExport' -Action $action -Trigger $trigger `
      -Settings $settings -Description 'Daily Chrome cookie export (Netscape format) via Muse Browser Control' -Force | Out-Null
    Write-Ok "scheduled task 'MuseCookieExport' daily at 04:00"
  } else { Write-Warn "exporter missing: $exporter — task not created." }
} catch {
  Write-Warn "could not register 'MuseCookieExport': $($_.Exception.Message)"
  Write-Info 'Run this script as Administrator to enable the daily cookie export.'
}

# ================================================================ final verification + summary
Write-Step 'Final verification'
$checks = [ordered]@{
  'Daemon'      = (Test-DaemonUp)
  'PC Agent'    = (Test-HttpOk "http://127.0.0.1:$AgentPort/health")
  'Obscura CDP' = (Test-HttpOk "http://127.0.0.1:$ObscuraPort/json/version")
  'Moli CDP'    = (Test-HttpOk "http://127.0.0.1:$MoliPort/json/version")
}
foreach ($k in $checks.Keys) {
  if ($checks[$k]) { Write-Ok "$k reachable" } else { Write-Warn "$k NOT reachable" }
}

$daemonLine  = "http://127.0.0.1:$DaemonPort"
$agentLine   = "http://127.0.0.1:$AgentPort"
$obscuraLine = "http://127.0.0.1:$ObscuraPort"
$moliLine    = "http://127.0.0.1:$MoliPort"
$ngrokLine   = $global:Summary['Public URL (ngrok)']
$extLine     = $global:Summary['Extension']

Write-Host ''
Write-Host '=====================================================' -ForegroundColor Green
Write-Host '        === MUSE BROWSER AUTOMATION READY ===' -ForegroundColor Green
Write-Host '=====================================================' -ForegroundColor Green
Write-Host "  Daemon:              $daemonLine"
Write-Host "  PC Agent:            $agentLine"
Write-Host "  Obscura CDP:         $obscuraLine"
Write-Host "  Moli CDP:            $moliLine"
Write-Host "  Public URL (ngrok):  $ngrokLine"
Write-Host "  Extension:           $extLine"
Write-Host '=====================================================' -ForegroundColor Green
Write-Host ''
Write-Host 'Copy the block above into Muse. Done.'
