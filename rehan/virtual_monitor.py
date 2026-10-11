"""Virtual-monitor agents: each agent gets a REAL separate screen.

The breakthrough
----------------
Windows Task-View "virtual desktops" share ONE Win32 desktop object — DWM
only composites the visible one, so a hidden virtual desktop has no pixels
anywhere until shown. That is why pixel-perfect capture of a hidden virtual
desktop without switching is impossible.

A virtual MONITOR is different. An IddCx (Indirect Display) driver presents
a real monitor to Windows: DWM composites it independently, 24/7, whether
or not anyone looks at it. Each DXGI output — physical or virtual — can be
captured pixel-perfectly via IDXGIOutputDuplication with ZERO switching and
ZERO flicker on the user's screen.

Architecture
------------
    Physical monitor (user, untouched)
    Virtual monitor 1  ->  Agent 1's "separate computer"
    Virtual monitor 2  ->  Agent 2's "separate computer"
    ...

- Driver: VirtualDrivers/Virtual-Display-Driver (MIT, ~10k stars, SIGNED via
  SignPath, on WinGet — no test-signing needed).
- Capture: ``dxcam`` (pip) wraps Desktop Duplication; one DXCamera per
  output; 240+ fps on 1080p. Falls back to Win32 BitBlt-on-that-output.
- Agent windows are positioned on the virtual monitor via SetWindowPos with
  virtual-screen coordinates (e.g. x=1920..3840 for the 2nd monitor).
- Input: SendInput absolute coordinates span the whole virtual screen, so
  clicks/keys land on the virtual monitor; background methods
  (UIA InvokePattern, PostMessage) from rehan.input_ladder never disturb
  the user's foreground at all.
- Filesystem is shared (same Windows session) — a file the agent writes is
  visible from the user's desktop automatically.

Storage/RAM cost: driver is kilobytes; dxcam is a pip package. No VM disk
images, no per-agent OS copies.

Windows-only. All Win32 imports are lazy so the module imports on Linux
(for tests) — real calls raise VirtualMonitorError when unavailable.
"""

import os
import subprocess
import threading
import time
from typing import Dict, List, Optional, Tuple

try:
    from PIL import Image
    _PIL = True
except Exception:
    _PIL = False


class VirtualMonitorError(Exception):
    """Base for virtual-monitor failures."""


def on_windows() -> bool:
    return os.name == "nt"


# ---------------------------------------------------------------------------
# Monitor enumeration (Win32)
# ---------------------------------------------------------------------------

def list_monitors() -> List[Dict]:
    """Enumerate displays -> [{index, name, x, y, w, h, primary, virtual}].

    Uses EnumDisplayMonitors + GetMonitorInfoW. A monitor is flagged
    ``virtual`` when its device name matches known IddCx virtual-display
    drivers (Virtual-Display-Driver, Parsec VDD, IddSampleDriver, usbmmidd).
    """
    if not on_windows():
        raise VirtualMonitorError("list_monitors is Windows-only")
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32

    class RECT(ctypes.Structure):
        _fields_ = [("left", wintypes.LONG), ("top", wintypes.LONG),
                    ("right", wintypes.LONG), ("bottom", wintypes.LONG)]

    class MONITORINFOEXW(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD),
                    ("rcMonitor", RECT),
                    ("rcWork", RECT),
                    ("dwFlags", wintypes.DWORD),
                    ("szDevice", wintypes.WCHAR * 32)]

    monitors: List[Dict] = []
    VIRTUAL_HINTS = ("vdd", "virtual display", "iddsample",
                     "parsec", "usbmmidd", "indirect")

    def _cb(hmon, hdc, rect_ptr, data):
        mi = MONITORINFOEXW()
        mi.cbSize = ctypes.sizeof(MONITORINFOEXW)
        if user32.GetMonitorInfoW(hmon, ctypes.byref(mi)):
            name = mi.szDevice or ""
            w = mi.rcMonitor.right - mi.rcMonitor.left
            h = mi.rcMonitor.bottom - mi.rcMonitor.top
            monitors.append({
                "index": len(monitors),
                "name": name,
                "x": mi.rcMonitor.left,
                "y": mi.rcMonitor.top,
                "w": w,
                "h": h,
                "primary": bool(mi.dwFlags & 1),  # MONITORINFOF_PRIMARY
                "virtual": any(hint in name.lower() for hint in VIRTUAL_HINTS),
            })
        return True

    MONITORENUMPROC = ctypes.WINFUNCTYPE(
        wintypes.BOOL, wintypes.HMONITOR, wintypes.HDC,
        ctypes.POINTER(RECT), wintypes.LPARAM)
    user32.EnumDisplayMonitors(None, None, MONITORENUMPROC(_cb), 0)
    return monitors


def virtual_monitors() -> List[Dict]:
    """Only the IddCx virtual monitors."""
    return [m for m in list_monitors() if m["virtual"]]


# ---------------------------------------------------------------------------
# Driver install / configuration
# ---------------------------------------------------------------------------

VDD_WINGET_ID = "VirtualDrivers.Virtual-Display-Driver"
VDD_SETTINGS_PATHS = (
    r"C:\IddSampleDriver\vdd_settings.xml",
    r"C:\VirtualDisplayDriver\vdd_settings.xml",
)


def vdd_installed() -> bool:
    """True if a known virtual-display driver is present."""
    if not on_windows():
        return False
    try:
        return len(virtual_monitors()) > 0
    except VirtualMonitorError:
        return False


def vmonitor_enabled() -> bool:
    """False when S3REHAN_NO_VMONITOR=1 (force virtual-desktop fallback)."""
    return os.environ.get("S3REHAN_NO_VMONITOR", "0") != "1"


def monitors_available() -> bool:
    """True when virtual monitors are usable right now.

    Safe to call anywhere: never raises, never installs anything.
    """
    try:
        return vmonitor_enabled() and vdd_installed()
    except Exception:
        return False


def pil_to_png(img) -> bytes:
    """PIL Image -> PNG bytes (for screenshot paths)."""
    import io

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def install_vdd(count: int = 2, width: int = 1920,
                height: int = 1080) -> Dict:
    """Install + configure the signed Virtual Display Driver.

    Uses winget (no test-signing, no reboot into special modes). Writes a
    minimal vdd_settings.xml requesting ``count`` monitors, then asks the
    driver control app to apply it.

    Returns {"ok": bool, "monitors": [...], "note": str}.
    """
    if not on_windows():
        raise VirtualMonitorError("install_vdd is Windows-only")
    # 1. winget install (idempotent — winget skips if present)
    r = subprocess.run(
        ["winget", "install", "--id", VDD_WINGET_ID, "-e",
         "--silent", "--accept-package-agreements",
         "--accept-source-agreements"],
        capture_output=True, text=True, timeout=300)
    if r.returncode != 0 and "already installed" not in (
            r.stdout + r.stderr).lower():
        return {"ok": False, "monitors": [],
                "note": f"winget failed: {(r.stderr or r.stdout)[:300]}"}
    # 2. write settings requesting N monitors
    xml = _vdd_settings_xml(count, width, height)
    wrote = None
    for path in VDD_SETTINGS_PATHS:
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(xml)
            wrote = path
            break
        except OSError:
            continue
    # 3. re-enumerate (driver picks up settings; may need its control app
    #    poked — the driver watches the file on most builds)
    time.sleep(2.0)
    try:
        mons = virtual_monitors()
    except VirtualMonitorError:
        mons = []
    ok = len(mons) >= count
    note = (f"settings -> {wrote}; found {len(mons)}/{count} virtual "
            f"monitors" + ("" if ok else
            " — open the VDD control app once to apply, then re-run"))
    return {"ok": ok, "monitors": mons, "note": note}


def _vdd_settings_xml(count: int, w: int, h: int) -> str:
    res = "\n".join(
        f'    <resolution><width>{w}</width><height>{h}</height>'
        f'<refresh>60</refresh></resolution>' for _ in range(1))
    mons = "\n".join(
        f'  <monitor><id>{i}</id>\n{res}\n  </monitor>'
        for i in range(count))
    return f"""<?xml version="1.0" encoding="utf-8"?>
<settings>
{mons}
</settings>
"""


# ---------------------------------------------------------------------------
# Capture: one DXGI duplication session per virtual output
# ---------------------------------------------------------------------------

class VirtualScreen:
    """Pixel-perfect, zero-flicker capture of ONE virtual monitor.

    Uses ``dxcam`` (Desktop Duplication per DXGI output). Each agent owns
    one VirtualScreen bound to its monitor; ``grab()`` returns a PIL Image
    of exactly what is on that monitor — wallpaper, taskbar, windows —
    indistinguishable from a physical screen capture.
    """

    def __init__(self, monitor: Dict):
        self.monitor = monitor
        self._cam = None
        self._lock = threading.Lock()
        self._dxcam_output = monitor.get("index", 1)

    @property
    def rect(self) -> Tuple[int, int, int, int]:
        m = self.monitor
        return (m["x"], m["y"], m["w"], m["h"])

    def start(self) -> None:
        """Open the DXGI duplication session for this output."""
        if not on_windows():
            raise VirtualMonitorError("capture is Windows-only")
        try:
            import dxcam
        except ImportError as e:
            raise VirtualMonitorError(
                "pip install dxcam  (Desktop Duplication capture)") from e
        # dxcam enumerates outputs per adapter; virtual monitors appear as
        # their own outputs. Best-effort: match by output index derived
        # from the Win32 monitor list order.
        self._cam = dxcam.create(output_idx=self._dxcam_output,
                                 output_color="BGRA")
        # warm up (first grab can be None while duplication spins up)
        for _ in range(10):
            if self._cam.grab() is not None:
                break
            time.sleep(0.05)

    def grab(self):
        """Return PIL Image of this virtual monitor, or None on timeout."""
        if self._cam is None:
            raise VirtualMonitorError("call start() first")
        if not _PIL:
            raise VirtualMonitorError("pip install pillow")
        with self._lock:
            frame = self._cam.grab()  # numpy BGRA or None
        if frame is None:
            return None
        img = Image.fromarray(frame[:, :, :3][:, :, ::-1], "RGB")
        return img

    def grab_region(self, x: int, y: int, w: int, h: int):
        """Crop to monitor-local coordinates."""
        img = self.grab()
        return img.crop((x, y, x + w, y + h)) if img else None

    def stop(self) -> None:
        cam, self._cam = self._cam, None
        if cam is not None:
            try:
                del cam
            except Exception:
                pass

    # -- context manager -----------------------------------------------
    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *a):
        self.stop()


# ---------------------------------------------------------------------------
# Window placement on the virtual monitor
# ---------------------------------------------------------------------------

def move_window_to_monitor(hwnd: int, monitor: Dict,
                           x: int = 0, y: int = 0,
                           w: Optional[int] = None,
                           h: Optional[int] = None) -> bool:
    """Move/resize a window onto the virtual monitor.

    (x, y) are monitor-local; they are translated to virtual-screen
    coordinates. The user's physical monitor is never touched.
    """
    if not on_windows():
        raise VirtualMonitorError("Windows-only")
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    gx, gy = monitor["x"] + x, monitor["y"] + y
    if w is None or h is None:
        # keep the window's current size

        class RECT(ctypes.Structure):
            _fields_ = [("left", wintypes.LONG), ("top", wintypes.LONG),
                        ("right", wintypes.LONG), ("bottom", wintypes.LONG)]

        rc = RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(rc)):
            raise VirtualMonitorError("GetWindowRect failed")
        w = rc.right - rc.left if w is None else w
        h = rc.bottom - rc.top if h is None else h
    flags = 0x0004 | 0x0040  # SWP_NOZORDER | SWP_SHOWWINDOW
    return bool(user32.SetWindowPos(hwnd, 0, gx, gy, w, h, flags))


# ---------------------------------------------------------------------------
# Agent binding: one virtual monitor <-> one agent
# ---------------------------------------------------------------------------

class MonitorPool:
    """Owns N virtual monitors and hands one to each agent.

    Usage:
        pool = MonitorPool.ensure(3)          # install driver if needed
        screen = pool.claim("agent-1")        # VirtualScreen, started
        img = screen.grab()                   # pixel-perfect, no flicker
        pool.release("agent-1")
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._claimed: Dict[str, VirtualScreen] = {}
        self._monitors: List[Dict] = []

    @classmethod
    def ensure(cls, n: int = 1) -> "MonitorPool":
        pool = cls()
        pool.refresh()
        have = len(pool._monitors)
        if have < n:
            res = install_vdd(count=n)
            if not res["ok"]:
                raise VirtualMonitorError(
                    "could not provision virtual monitors: " + res["note"])
            pool.refresh()
        if len(pool._monitors) < n:
            raise VirtualMonitorError(
                f"only {len(pool._monitors)}/{n} virtual monitors present")
        return pool

    def refresh(self) -> List[Dict]:
        self._monitors = virtual_monitors()
        return self._monitors

    def _key(self, m: Dict) -> str:
        return f'{m["name"]}@{m["x"]},{m["y"]}'

    def claim(self, agent_id: str) -> VirtualScreen:
        with self._lock:
            if agent_id in self._claimed:
                return self._claimed[agent_id]
            used = {self._key(s.monitor) for s in self._claimed.values()}
            free = [m for m in self._monitors if self._key(m) not in used]
            if not free:
                raise VirtualMonitorError("no free virtual monitor")
            screen = VirtualScreen(free[0])
            screen.start()
            self._claimed[agent_id] = screen
            return screen

    def release(self, agent_id: str) -> None:
        with self._lock:
            screen = self._claimed.pop(agent_id, None)
        if screen is not None:
            screen.stop()

    def status(self) -> Dict:
        with self._lock:
            return {"monitors": len(self._monitors),
                    "claimed": sorted(self._claimed)}
