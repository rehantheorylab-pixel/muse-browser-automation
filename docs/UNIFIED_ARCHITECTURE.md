# Unified Agent Architecture (2026-10-10)

One model for everything: **each agent gets its own screen**.

## The stack

```
User's physical monitor (untouched, zero flicker)
│
├── Agent 1 ── virtual monitor 1 ── [optional] WSL2 Linux
│     ├── Sub-agent 1a ── virtual monitor 2
│     └── Sub-agent 1b ── virtual monitor 3 ── [optional] WSL2 Linux
│
├── Agent 2 ── virtual monitor 4
│
└── Headless pool ── WSL2 (no screen needed: scraping, APIs, ETL)
```

- **Every agent = one virtual monitor = one "separate computer."**
  The agent screenshots its own monitor (DXGI duplication, 240+ fps),
  pixel-perfect, with **zero switching and zero flicker** on the user's
  display. The user can watch Netflix while 4 agents work — nothing
  moves on their screen.
- **Filesystem is shared** (same Windows session): a file an agent writes
  on its monitor is visible from the user's desktop automatically — no
  sync, exactly like Windows' built-in virtual desktops.
- **Each agent can deploy Linux AND Windows at the same time**: a virtual
  monitor for GUI work plus a WSL2 worker for headless tasks. WSLg GUI
  apps render as Windows windows and are moved onto the agent's monitor.

## Display-isolation priority

| # | Backend | When | Capture |
|---|---------|------|---------|
| 1 | **Virtual monitor** (IddCx VDD) | Driver installed (default) | DXGI per-output, 0ms flicker |
| 2 | **Virtual desktop** (pyvda) | No driver / `S3REHAN_NO_VMONITOR` unset but driver absent | Switch-capture, idle-gated, ~100–200ms flicker budget |
| 3 | **Shim** | Tests / dev / `S3REHAN_NO_VDESKTOP=1` | None |

Every layer degrades automatically — code never branches on "is the
driver installed", it just claims and falls back.

## Components

| Module | Role in the unified architecture |
|--------|----------------------------------|
| `rehan/virtual_monitor.py` | `MonitorPool` (claim/release), `VirtualScreen.grab()`, `install_vdd()`, `move_window_to_monitor()`. Primary isolation. |
| `rehan/agent_hierarchy.py` | `Agent`: claims a monitor from the shared pool on `start()` (children share the parent's pool), falls back to its virtual desktop. `screenshot()` grabs the monitor. |
| `rehan/workers.py` | `TaskDispatcher`: owns one `MonitorPool`, hands one screen per worker. `Worker.screenshot()` → monitor grab → desktop fallback. GUI tasks prefer monitor-backed workers (`needs_gui`). |
| `rehan/linux_worker.py` | `WSLWorker`: headless by default; with `S3REHAN_LINUX_VMONITOR=1` claims a monitor for WSLg GUI apps (`place_gui_on_monitor()` moves `wslg.exe` windows there). |
| `rehan/lanerouter.py` | `route_display()` picks monitor/vdesktop/inline by `needs_gui` + availability. `route_placement()` = platform + display in one call. Action lanes (cache→planner) unchanged. |
| `rehan/desktop_screenshot.py` | Fallback capture (switch/composite) when no driver is installed. |

## Data flow (one GUI action)

```
task(needs_gui=True)
  → lanerouter.route_placement() → {platform: windows, display: monitor}
  → dispatcher._pump() → idle Windows worker WITH a claimed monitor
  → worker.run_task: act via background input (input_ladder)
  → worker.screenshot() → screen.grab() → PIL → PNG (0ms flicker)
  → invalidate, next action
```

## Setup

One admin PowerShell (idempotent):

```powershell
.\install\setup-all.ps1                 # 2 monitors, WSL2, all deps
.\install\setup-all.ps1 -Monitors 4    # 4 agents at once
```

## Knobs

| Env | Effect |
|-----|--------|
| `S3REHAN_NO_VMONITOR=1` | Disable monitors → virtual-desktop fallback everywhere |
| `S3REHAN_NO_VDESKTOP=1` | Disable virtual desktops → shim (tests) |
| `S3REHAN_LINUX_VMONITOR=1` | Linux workers claim monitors (WSLg GUI) |
| `S3REHAN_WORKERS=N` | Windows worker floor (RAM-capped) |
| `S3REHAN_LINUX_WORKERS=N` | Linux worker floor |

## Resource budget (Rehan's constraints)

- VDD driver: kilobytes. dxcam: one pip package. No VM disk images.
- WSL2: ~1GB base, `.wslconfig` caps RAM (default min(4GB, 25% host)),
  returns RAM to Windows when idle.
- Monitor count, not pixels, is the scarce resource — one per GUI agent.
