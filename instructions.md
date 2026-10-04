# Complete Automation Setup Instructions

**For: Muse AI (or any AI assistant) setting up on a Windows PC**

This guide sets up three automation tools. Follow it exactly, in order. If any step fails, fix it before continuing — do not skip steps.

---

## The Three Tools

| # | Tool | What it does | Port |
|---|------|--------------|------|
| 1 | **Chrome Automation** | Controls the user's personal Chrome via custom extension (background tabs, no focus steal) | 18010 |
| 2 | **Obscura Automation** | Controls Obscura browser (separate browser, default for agent tasks) | 9222+ |
| 3 | **PC Agent** | Windows-level mouse/keyboard control (undetectable, fastest) | 18011 |

**CRITICAL RULE:** For ALL browser automation, use Tool 1 (Chrome extension) or Tool 2 (Obscura). NEVER use CSI/CDP directly — Google detects it as a bot. For PC control, ALWAYS use Tool 3 (pc_agent) — it uses Windows SendInput API, indistinguishable from human input.

---

## Prerequisites

- Windows 10/11 PC
- Python 3.11+ installed
- Google Chrome installed
- PowerShell

---

## Tool 1: Chrome Automation Setup

### Files
- `daemon/simpled.py` — HTTP daemon (127.0.0.1:18010)
- `daemon/_auth.py` — Bearer token auth
- `daemon/mcp_server.py` — MCP server
- `extension/` — Chrome MV3 extension

### Setup Steps

1. **Copy files:**
   ```powershell
   mkdir $env:USERPROFILE\muse-browser-mcp -Force
   Copy-Item daemon\* $env:USERPROFILE\muse-browser-mcp\ -Force
   Copy-Item extension $env:USERPROFILE\muse-browser-mcp\extension -Recurse -Force
   ```

2. **Install Python deps:**
   ```powershell
   pip install websockets
   ```

3. **Start the daemon:**
   ```powershell
   Start-Process pythonw -ArgumentList "$env:USERPROFILE\muse-browser-mcp\simpled.py" -WindowStyle Hidden
   ```

4. **Load extension in Chrome** (USER MUST DO THIS):
   - Open `chrome://extensions`
   - Enable "Developer mode" (top right)
   - Click "Load unpacked"
   - Select `%USERPROFILE%\muse-browser-mcp\extension`
   - User says "done" when complete

5. **Verify:**
   ```powershell
   $token = Get-Content $env:USERPROFILE\muse-browser-mcp\daemon_token
   $headers = @{Authorization = "Bearer $token"; "Content-Type" = "application/json"}
   $body = '{"method": "tabs.list", "params": {}}'
   Invoke-RestMethod -Uri "http://127.0.0.1:18010/tool" -Method Post -Headers $headers -Body $body
   ```
   Should return list of open tabs.

### Commands Reference — Chrome Automation

**Base URL:** `http://127.0.0.1:18010/tool`
**Auth:** `Authorization: Bearer <token from ~/muse-browser-mcp/daemon_token>`
**Method:** POST with JSON `{"method": "<dotted.name>", "params": {...}}`

| Command | Method | Params | What it does |
|---------|--------|--------|--------------|
| List tabs | `tabs.list` | `{}` | Returns all open tabs |
| Navigate | `page.navigate` | `{"url": "...", "tabId": N}` | Navigate tab to URL |
| Screenshot | `page.screenshot` | `{"tabId": N}` | Capture tab screenshot |
| Snapshot | `page.snapshot` | `{"tabId": N}` | Get accessibility tree |
| Click | `page.click` | `{"tabId": N, "selector": "@e1"}` | Click element by ref |
| Fill | `page.fill` | `{"tabId": N, "selector": "@e1", "text": "..."}` | Fill input field |
| Evaluate | `page.evaluate` | `{"tabId": N, "code": "JS..."}` | Run JavaScript in tab |
| Get cookies | `cookies.export_cdp` | `{"tabId": N}` | Export cookies via CDP |

**Example — take a screenshot:**
```python
import json, urllib.request
tok = open(r"%USERPROFILE%/muse-browser-mcp/daemon_token").read().strip()
data = json.dumps({"method": "page.screenshot", "params": {"tabId": 123}}).encode()
req = urllib.request.Request("http://127.0.0.1:18010/tool", data=data,
    headers={"Content-Type": "application/json", "Authorization": "Bearer " + tok})
result = json.loads(urllib.request.urlopen(req).read())
```

---

## Tool 2: Obscura Automation Setup

### Files
- `obscura/obscura_helper.py` — Main helper (CDP on 127.0.0.1:9222)
- `obscura/profiles.py` — Profile manager
- `obscura/README.md` — Detailed docs

### Setup Steps

1. **Download Obscura:**
   - Go to https://github.com/h4ckf0r0day/obscura/releases
   - Download the **-stealth** build (not the regular one)
   - Extract to `%USERPROFILE%\obscura\`

2. **Copy helper files:**
   ```powershell
   Copy-Item obscura\*.py $env:USERPROFILE\muse-browser-mcp\ -Force
   ```

3. **Create main profile:**
   ```powershell
   python $env:USERPROFILE\muse-browser-mcp\obscura_profiles.py main
   ```

4. **Sync Chrome cookies** (optional):
   ```powershell
   python $env:USERPROFILE\muse-browser-mcp\obscura_profiles.py sync
   ```

### Commands Reference — Obscura

| Command | What it does |
|---------|--------------|
| `python obscura_profiles.py main` | Start main profile (port 9222, persistent) |
| `python obscura_profiles.py temp` | Start temp profile (auto-deletes) |
| `python obscura_profiles.py empty` | Start blank profile |
| `python obscura_profiles.py sync` | Sync Chrome cookies to main |
| `python obscura_profiles.py list` | List running profiles |
| `python obscura_profiles.py cleanup` | Delete stopped temp profiles |

**CDP via obscura_helper.py:**
```python
from obscura_helper import ObscuraHelper
h = ObscuraHelper(port=9222)
h.navigate("https://example.com")
h.screenshot("shot.png")
h.evaluate("() => document.title")
```

**IMPORTANT:** Obscura is NOT Chromium — it's a from-scratch Rust engine. Heavy web apps may render differently. For pixel-perfect Chrome work, use Tool 1.

---

## Tool 3: PC Agent Setup (Computer Use)

### Files
- `pcagent/pc_agent.py` — Main daemon (127.0.0.1:18011)
- `pcagent/apps.json` — Semantic app registry (edit to add apps)
- `pcagent/pcscript.py` — PCScript interpreter
- `pcagent/kill_agent.py` — Safe restart helper

### Setup Steps

1. **Copy files:**
   ```powershell
   Copy-Item pcagent\* $env:USERPROFILE\muse-browser-mcp\ -Force
   ```

2. **Install Python deps:**
   ```powershell
   pip install pyautogui pillow
   ```

3. **Start the daemon:**
   ```powershell
   Start-Process pythonw -ArgumentList "$env:USERPROFILE\muse-browser-mcp\pc_agent.py" -WindowStyle Hidden
   ```

4. **Verify:**
   ```powershell
   Invoke-RestMethod -Uri "http://127.0.0.1:18011/health"
   ```
   Should return `{"ok": true, ...}`

5. **Auto-start on login:** Create `%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\StartMusePCAgent.vbs`:
   ```vbs
   CreateObject("Wscript.Shell").Run "pythonw %USERPROFILE%\muse-browser-mcp\pc_agent.py", 0, False
   ```

### Commands Reference — PC Agent

**Base URL:** `http://127.0.0.1:18011`
**Method:** POST with JSON

| Command | Endpoint | Params | What it does |
|---------|----------|--------|--------------|
| Click | `/click` | `{"x": 500, "y": 300}` | Left click at coordinates |
| Right click | `/rightclick` | `{"x": 500, "y": 300}` | Right click |
| Double click | `/doubleclick` | `{"x": 500, "y": 300}` | Double click |
| Move | `/move` | `{"x": 500, "y": 300}` | Move mouse (no click) |
| Type | `/type` | `{"text": "hello"}` | Type text (uses clipboard for >20 chars) |
| Press key | `/press` | `{"key": "enter"}` | Press a key |
| Hotkey | `/hotkey` | `{"keys": ["ctrl", "c"]}` | Press key combination |
| Screenshot | `/screenshot` | `{}` | Capture full screen |
| Open app | `/open` | `{"app": "chrome"}` | Open app by name (see apps.json) |
| Close app | `/close` | `{"app": "chrome"}` | Close app |
| Scroll | `/scroll` | `{"amount": -500}` | Scroll (negative = down) |
| Drag | `/drag` | `{"x1": 100, "y1": 100, "x2": 500, "y2": 500}` | Drag mouse |

**Semantic app control** (via apps.json):
```python
# Open Chrome (uses registry, no coordinates needed)
POST /open {"app": "chrome"}

# apps.json format:
{
  "chrome": {"path": "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe"},
  "notepad": {"path": "notepad.exe"}
}
```

**PCScript** (deterministic automation language):
```python
# Run a .pcs script
python pcscript.py script.pcs

# .pcs format:
# CLICK 500 300
# TYPE "hello world"
# PRESS enter
# WAIT 1000
# SCREENSHOT
```

### Restarting PC Agent (SAFE method)
```powershell
# Kill ONLY the agent (not all pythonw!)
python $env:USERPROFILE\muse-browser-mcp\kill_agent.py
Start-Process pythonw -ArgumentList "$env:USERPROFILE\muse-browser-mcp\pc_agent.py" -WindowStyle Hidden
```
**NEVER** kill all pythonw processes — that kills the bridge and other daemons!

---

## Screenshot & Coordinate Tools

### Fast Screenshot (via PC Agent)
```python
import urllib.request, json, base64
req = urllib.request.Request("http://127.0.0.1:18011/screenshot",
    data=json.dumps({}).encode(), headers={"Content-Type": "application/json"})
data = json.loads(urllib.request.urlopen(req).read())
img = base64.b64decode(data["image"])  # PNG bytes
```

### Coordinate Finding Workflow
1. Take screenshot via PC Agent
2. Display to user or analyze with vision model
3. Get (x, y) coordinates
4. Click via PC Agent `/click`

**Speed:** PC Agent executes clicks locally at full speed (~50ms). No network latency.

---

## Verification Checklist

Run these to confirm everything works:

- [ ] Chrome daemon: `tabs.list` returns tabs
- [ ] Chrome extension: navigate + screenshot works
- [ ] Obscura: `obscura_profiles.py main` starts, CDP connects
- [ ] PC Agent: `/health` returns ok
- [ ] PC Agent: `/screenshot` returns image
- [ ] PC Agent: `/click` moves mouse (verify visually)

When all pass, say **"AUTOMATION READY"**.

---

## Troubleshooting

| Problem | Fix |
|---------|-----|
| Port 18010 in use | Daemon already running — check `tabs.list` first |
| Port 18011 in use | PC Agent already running — check `/health` first |
| Extension not connecting | Reload extension in chrome://extensions |
| Obscura won't start | Check if port 9222 is free; kill old process |
| PC Agent slow | Normal — first call loads pyautogui (~2s), then fast |
| Google detects automation | You're using CSI/CDP — switch to PC Agent (Windows-level) |

---

## Speed Notes

- **PC Agent clicks:** ~50ms (local, no network)
- **Chrome extension:** ~2s/op via tunnel, ~100ms local
- **Obscura CDP:** ~1s/op
- **Always prefer PC Agent** for speed and undetectability.

---

## Security Notes

- All tools bind to `127.0.0.1` only (loopback, not network-accessible)
- Never expose these ports publicly without authentication
- The daemon token is in `~/muse-browser-mcp/daemon_token` (mode 0600)
- Audit for unauthenticated routes before any public exposure
