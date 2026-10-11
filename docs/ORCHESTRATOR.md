# Agent Display Orchestrator

How Rehan's per-agent background workspaces work.

## PRINCIPLE (2026-10-11): Virtual MONITORS are primary.

One agent = one virtual MONITOR (IddCx driver) = one "separate computer".
Zero flicker, zero switching, true background. DWM composites all monitors
24/7; each captured pixel-perfectly via DXGI with no disturbance to the user.

Virtual DESKTOPS (below) are the FALLBACK when VDD is not installed.
They work (proven 2026-10-10) but require visible switching for GUI work.

## Primary: Virtual Monitors (true background)

```
Install (one-time, admin):
  install/setup-all.ps1  →  VDD driver + N virtual monitors

Per agent:
  pool = MonitorPool.ensure(1)
  screen = pool.claim("agent-t8")   # agent's own "separate computer"
  img = screen.grab()               # pixel-perfect, zero flicker
  pool.release("agent-t8")
```

## Fallback: Virtual Desktops (working now, visible switching for GUI)

One agent = one Windows virtual desktop + one WSL2 instance. Rehan uses
Desktop 1 normally; agents work on their own desktops. Same session, shared
filesystem — agent edits appear on his PC instantly.

**WARNING:** Virtual desktops require VISIBLE SWITCHING for pixel-perfect
GUI capture. This violates the true-background principle. Install VDD
for the primary path.

## Layout on his PC

```
C:\AgentDesktops\
  VirtualDesktop.exe      (MScholtes v1.21 — Win10 virtual desktop CLI)
  New-AgentDesktop.ps1    (create desktop for agent)
  Remove-AgentDesktop.ps1 (remove agent desktop)
  New-AgentWSL.ps1        (clone WSL2 instance for agent)
  Remove-AgentWSL.ps1     (delete agent WSL2 instance)
  wsl-base\ubuntu-base.tar        (shared base image, created once)
  wsl-instances\<agent>\          (per-agent instance disks)
```

## Assigning an agent (example: agent "t8")

```powershell
# 1. Background Windows desktop (invisible to Rehan, never steals focus)
powershell -ExecutionPolicy Bypass -File C:\AgentDesktops\New-AgentDesktop.ps1 -AgentName "t8"
# -> AGENT_DESKTOP_NUM=2   (Desktop 1 stays Rehan's)

# 2. Private WSL2 Linux (mounts /mnt/c and /mnt/d — same files as Windows)
powershell -ExecutionPolicy Bypass -File C:\AgentDesktops\New-AgentWSL.ps1 -AgentName "t8"
# -> AGENT_WSL_NAME=Agent-t8
```

Launch the agent's windows ON its desktop (never on Desktop 1):

```powershell
# Start a process, then move its window to the agent desktop
$p = Start-Process notepad -PassThru; Start-Sleep 1
C:\AgentDesktops\VirtualDesktop.exe /GetDesktop:2 /MoveWindowHandle:$($p.MainWindowHandle)
```

Or inside WSL:

```powershell
wsl -d Agent-t8 -- bash -c "echo hello > /mnt/c/Temp/from-agent-t8.txt"
# -> instantly visible at C:\Temp\from-agent-t8.txt on Rehan's PC
```

## Teardown (agent done)

```powershell
powershell -ExecutionPolicy Bypass -File C:\AgentDesktops\Remove-AgentDesktop.ps1 -AgentName "t8"
powershell -ExecutionPolicy Bypass -File C:\AgentDesktops\Remove-AgentWSL.ps1 -AgentName "t8"
```

## Rules (Rehan's standing orders)

- NEVER touch his personal Chrome. Target only `rb_prof` harvester processes.
- NEVER switch to an agent desktop while he is using the PC.
- Close test windows when done.
- Desktop 0 ("Desktop 1") is his — scripts refuse to remove it.
- Agent edits on C:/D: are visible to him instantly — that is the design.

## One-time setup (already done 2026-10-10)

1. Downloaded VirtualDesktop.exe v1.21 (Win10 build) to C:\AgentDesktops\
2. Installed Ubuntu via `wsl --install -d Ubuntu --no-launch`
3. Exported base: `wsl --export Ubuntu C:\AgentDesktops\wsl-base\ubuntu-base.tar`

## Limitations

- VirtualDesktop.exe uses undocumented COM APIs; a major Windows update
  could break it (MScholtes ships updated builds; check GitHub releases).
- WSL2 instances share the same kernel but have separate filesystems
  except /mnt — perfect for this use case.
- GUI Linux apps need WSLg (built into Win10 22H2+); terminal work needs nothing.
- Each WSL2 instance uses ~1-2 GB disk + ~200-500 MB RAM when running.
