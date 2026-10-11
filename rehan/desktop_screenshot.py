"""Desktop-aware screenshots for virtual-desktop agents.

The requirement
---------------
The agent must see its desktop EXACTLY like a user sees their monitor —
full desktop, wallpaper, taskbar, all windows on THAT desktop, no
artifacts, no stitching. Indistinguishable from a physical machine.

The engineering reality
-----------------------
Windows virtual desktops share a single Win32 desktop object; DWM only
composites the VISIBLE desktop. The pixels of a hidden virtual desktop
do not exist anywhere until that desktop becomes visible — no API
renders it off-screen (not BitBlt, not DWM thumbnails, not Desktop
Duplication, not WGC; per-window capture can never reproduce wallpaper,
taskbar, shadows, or DWM compositing faithfully).

Therefore the ONLY pixel-perfect capture is:

    switch to the agent's desktop -> BitBlt the real DWM frame ->
    switch back                                    ("smart"/"switch" mode)

This is the default. The flicker is bounded by:

- a GLOBAL screenshot mutex (one desktop switch at a time, all agents),
- user-idle gating (don't switch mid-keystroke — keystrokes typed during
  a switch would land in the agent's window),
- fullscreen deferral (never yank the user out of a game/video),
- capture-once-per-action caching (Agent.screenshot fresh=False).

Measured flicker is reported on every capture (info["flicker_ms"]);
budget default 100ms with a warning when exceeded.

Fallbacks (honest tradeoffs)
----------------------------
- "composite": per-window PrintWindow/WGC capture composited in z-order.
  ZERO flicker, but approximate — black canvas instead of wallpaper, no
  taskbar, GPU apps blank without the WGC helper. Good enough for
  window-content grounding; does NOT meet the pixel-perfect bar.
- Browser windows: prefer CDP ``Page.captureScreenshot`` (works hidden).

Window<->desktop membership is tracked explicitly: the worker moves its
windows onto its desktop via ``VirtualDesktop.move_window_here`` (which
registers them here). Owned popups/dialogs (GW_OWNER chain) are included
transitively, since the shell puts them on the owner's desktop.
"""

import io
import os
import subprocess
import threading
import time
from typing import Dict, List, Optional, Set, Tuple

# ---------------------------------------------------------------------------
# platform guard
# ---------------------------------------------------------------------------

def on_windows() -> bool:
    return os.name == "nt"


# ---------------------------------------------------------------------------
# per-window capture
# ---------------------------------------------------------------------------

PW_RENDERFULLCONTENT = 0x00000002


class ScreenshotError(Exception):
    """Base for screenshot failures."""


class WgcUnavailable(ScreenshotError):
    """The WGC helper exe is not built/present."""


def _blank_image(w: int, h: int):
    from PIL import Image

    return Image.new("RGB", (max(1, w), max(1, h)), (0, 0, 0))


def is_blank(img, sample: int = 64, tolerance: int = 8) -> bool:
    """Heuristic: PrintWindow failures come back solid black/white.

    Samples a grid of pixels; blank when every sample is within
    ``tolerance`` of the first. Cheap (~microseconds) and good enough to
    decide "this capture is useless, try the next strategy".
    """
    try:
        w, h = img.size
        px = img.convert("RGB").load()
        r0, g0, b0 = px[0, 0]
        step_x = max(1, w // sample)
        step_y = max(1, h // sample)
        for yy in range(0, h, step_y):
            for xx in range(0, w, step_x):
                r, g, b = px[xx, yy]
                if (abs(r - r0) > tolerance or abs(g - g0) > tolerance
                        or abs(b - b0) > tolerance):
                    return False
        return True
    except Exception:
        return True


def _bitmap_to_pil(hbmp, w: int, h: int):
    """HBITMAP (32bpp) -> PIL Image via GetDIBits. Windows only."""
    import ctypes
    from ctypes import wintypes

    gdi32 = ctypes.windll.gdi32

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [
            ("biSize", wintypes.DWORD),
            ("biWidth", wintypes.LONG),
            ("biHeight", wintypes.LONG),
            ("biPlanes", wintypes.WORD),
            ("biBitCount", wintypes.WORD),
            ("biCompression", wintypes.DWORD),
            ("biSizeImage", wintypes.DWORD),
            ("biXPelsPerMeter", wintypes.LONG),
            ("biYPelsPerMeter", wintypes.LONG),
            ("biClrUsed", wintypes.DWORD),
            ("biClrImportant", wintypes.DWORD),
        ]

    class BITMAPINFO(ctypes.Structure):
        _fields_ = [("bmiHeader", BITMAPINFOHEADER),
                    ("bmiColors", wintypes.DWORD * 3)]

    bmi = BITMAPINFO()
    bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bmi.bmiHeader.biWidth = w
    bmi.bmiHeader.biHeight = -h  # top-down DIB
    bmi.bmiHeader.biPlanes = 1
    bmi.bmiHeader.biBitCount = 32
    bmi.bmiHeader.biCompression = 0  # BI_RGB

    buf = (ctypes.c_ubyte * (w * h * 4))()
    hdc = gdi32.CreateCompatibleDC(None)
    try:
        gdi32.SelectObject(hdc, hbmp)
        lines = gdi32.GetDIBits(hdc, hbmp, 0, h, buf, ctypes.byref(bmi), 0)
    finally:
        gdi32.DeleteDC(hdc)
    if not lines:
        return None
    from PIL import Image

    return Image.frombytes("RGBA", (w, h), bytes(buf), "raw", "BGRA")


def window_rect(hwnd: int) -> Optional[Tuple[int, int, int, int]]:
    """GetWindowRect -> (x, y, w, h). None on failure / non-Windows."""
    if not on_windows():
        return None
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        r = (wintypes.LONG * 4)()
        if not user32.GetWindowRect(int(hwnd), r):
            return None
        x, y = int(r[0]), int(r[1])
        w, h = int(r[2]) - x, int(r[3]) - y
        if w <= 0 or h <= 0:
            return None
        return (x, y, w, h)
    except Exception:
        return None


def capture_window_printwindow(hwnd: int):
    """Capture one window via PrintWindow(PW_RENDERFULLCONTENT).

    Works for Win32/GDI apps even when the window is on a non-visible
    virtual desktop — WM_PRINT is serviced by the application itself and
    does not depend on DWM compositing or on-screen visibility.

    Returns a PIL Image, or None when the window cannot be captured
    (GPU-composited apps like Chrome/WebView2 typically come back blank;
    use :func:`is_blank` to detect that and escalate to WGC).
    """
    if not on_windows():
        return None
    try:
        import ctypes

        user32 = ctypes.windll.user32
        gdi32 = ctypes.windll.gdi32
        rect = window_rect(int(hwnd))
        if not rect:
            return None
        _, _, w, h = rect

        hdc_screen = user32.GetDC(0)
        hdc_mem = gdi32.CreateCompatibleDC(hdc_screen)
        hbmp = gdi32.CreateCompatibleBitmap(hdc_screen, w, h)
        h_old = gdi32.SelectObject(hdc_mem, hbmp)
        try:
            ok = user32.PrintWindow(int(hwnd), hdc_mem, PW_RENDERFULLCONTENT)
            img = _bitmap_to_pil(hbmp, w, h) if ok else None
        finally:
            gdi32.SelectObject(hdc_mem, h_old)
            gdi32.DeleteObject(hbmp)
            gdi32.DeleteDC(hdc_mem)
            user32.ReleaseDC(0, hdc_screen)
        return img
    except Exception:
        return None


def _wgc_helper_path() -> Optional[str]:
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        os.path.join(here, "..", "tools", "wgc_capture", "wgc_capture.exe"),
        os.path.join(here, "tools", "wgc_capture", "wgc_capture.exe"),
    ]
    for cand in candidates:
        cand = os.path.normpath(cand)
        if os.path.isfile(cand):
            return cand
    return None


def capture_window_wgc(hwnd: int, timeout_s: float = 15.0):
    """Capture one window via the Windows.Graphics.Capture helper.

    Handles GPU-composited and occluded windows where PrintWindow returns
    blank. Raises :class:`WgcUnavailable` when the helper exe has not been
    built on the PC (see tools/wgc_capture/). Returns PIL Image or None.
    """
    exe = _wgc_helper_path()
    if not exe:
        raise WgcUnavailable(
            "tools/wgc_capture/wgc_capture.exe not built; "
            "see tools/wgc_capture/BUILD.md (optional upgrade path)")
    try:
        proc = subprocess.run(
            [exe, str(int(hwnd))], capture_output=True, timeout=timeout_s)
        if proc.returncode != 0 or not proc.stdout:
            return None
        from PIL import Image

        return Image.open(io.BytesIO(proc.stdout)).convert("RGB")
    except Exception:
        return None


def capture_window(hwnd: int, strategy: str = "auto"):
    """Capture one window. strategy: 'auto' | 'wgc' | 'printwindow'.

    'auto' tries WGC (when available) then PrintWindow. Returns a PIL
    Image, or None if nothing worked. Raises ScreenshotError on an
    unknown strategy name.
    """
    if strategy not in ("auto", "wgc", "printwindow"):
        raise ScreenshotError(f"unknown strategy: {strategy}")
    strategies = (("wgc", "printwindow") if strategy == "auto"
                  else (strategy,))
    for strat in strategies:
        try:
            if strat == "wgc":
                img = capture_window_wgc(int(hwnd))
            else:  # printwindow
                img = capture_window_printwindow(int(hwnd))
        except WgcUnavailable:
            continue
        except Exception:
            continue
        if img is not None and not is_blank(img):
            return img
        # Blank (e.g. GPU app under PrintWindow) -> try next strategy.
        if img is not None and strat == "printwindow":
            return img  # last resort result even if blank-ish
    return None


# ---------------------------------------------------------------------------
# window <-> virtual-desktop membership (explicit tracking)
# ---------------------------------------------------------------------------
# The worker moves its windows onto its desktop via
# VirtualDesktop.move_window_here(), which registers the hwnd here.
# Owned popups/dialogs are included transitively via the GW_OWNER chain.

_owned: Dict[int, Set[int]] = {}
_owned_lock = threading.Lock()


def _vd_key(vd) -> int:
    return id(vd)


def own_window(vd, hwnd: int) -> None:
    """Register hwnd as belonging to vd's virtual desktop."""
    with _owned_lock:
        _owned.setdefault(_vd_key(vd), set()).add(int(hwnd))


def disown_window(vd, hwnd: int) -> None:
    with _owned_lock:
        s = _owned.get(_vd_key(vd))
        if s is not None:
            s.discard(int(hwnd))


def release_desktop(vd) -> None:
    """Forget all window registrations for vd (called on remove)."""
    with _owned_lock:
        _owned.pop(_vd_key(vd), None)


def owned_hwnds(vd) -> Set[int]:
    with _owned_lock:
        return set(_owned.get(_vd_key(vd), set()))


def _enum_top_windows() -> List[int]:
    """All top-level windows, top-to-bottom z-order. [] off Windows."""
    if not on_windows():
        return []
    hwnds: List[int] = []
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32

        @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
        def _cb(hwnd, _):
            hwnds.append(int(hwnd))
            return True

        user32.EnumWindows(_cb, 0)
    except Exception:
        pass
    return hwnds


def _window_owner(hwnd: int) -> int:
    if not on_windows():
        return 0
    try:
        import ctypes

        return int(ctypes.windll.user32.GetWindow(int(hwnd), 4))  # GW_OWNER=4
    except Exception:
        return 0


def _window_cloaked(hwnd: int) -> bool:
    """DWMWA_CLOAKED != 0 -> skip (suspended UWP etc.)."""
    if not on_windows():
        return False
    try:
        import ctypes
        from ctypes import wintypes

        dwm = ctypes.windll.dwmapi
        cloaked = wintypes.DWORD(0)
        hr = dwm.DwmGetWindowAttribute(int(hwnd), 14,
                                      ctypes.byref(cloaked),
                                      ctypes.sizeof(cloaked))
        return hr == 0 and cloaked.value != 0
    except Exception:
        return False


def _window_visible(hwnd: int) -> bool:
    if not on_windows():
        return False
    try:
        import ctypes

        return bool(ctypes.windll.user32.IsWindowVisible(int(hwnd)))
    except Exception:
        return False


def iter_desktop_windows(vd) -> List[Dict]:
    """Top-level windows on vd's virtual desktop, BOTTOM-to-top z-order.

    Each entry: {"hwnd", "title", "rect"=(x,y,w,h)}. Skips invisible,
    cloaked, and zero-area windows. Ownership = explicitly registered
    hwnds plus their owned popups (GW_OWNER chain).
    """
    owned = owned_hwnds(vd)
    if not owned:
        return []
    try:
        import ctypes

        user32 = ctypes.windll.user32
    except Exception:
        user32 = None  # non-Windows / mocked env: titles unavailable

    result: List[Dict] = []
    for hwnd in _enum_top_windows():  # top-to-bottom z-order
        if hwnd not in owned and _window_owner(hwnd) not in owned:
            continue
        if not _window_visible(hwnd) or _window_cloaked(hwnd):
            continue
        rect = window_rect(hwnd)
        if not rect:
            continue
        title = ""
        if user32 is not None:
            try:
                import ctypes as _ct

                length = user32.GetWindowTextLengthW(hwnd)
                buf = _ct.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, buf, length + 1)
                title = buf.value
            except Exception:
                title = ""
        result.append({"hwnd": hwnd, "title": title, "rect": rect})
    result.reverse()  # paint bottom-to-top
    return result


def screen_size() -> Tuple[int, int]:
    if not on_windows():
        return (1920, 1080)
    try:
        import ctypes

        user32 = ctypes.windll.user32
        return (int(user32.GetSystemMetrics(0)), int(user32.GetSystemMetrics(1)))
    except Exception:
        return (1920, 1080)


# ---------------------------------------------------------------------------
# desktop-level capture
# ---------------------------------------------------------------------------

def capture_desktop_composite(vd, strategy: str = "auto"):
    """Approximate screenshot — ZERO user-visible change.

    Composites per-window captures (bottom-to-top z-order) onto a
    desktop-sized canvas. Windows that fail all strategies are skipped
    (their region stays black) rather than aborting the whole shot.

    Honest limitation: black canvas instead of wallpaper, no taskbar,
    GPU apps blank without the WGC helper. This does NOT meet the
    pixel-perfect bar — it is the zero-flicker fallback. See module
    docstring.
    """
    from PIL import Image

    w, h = screen_size()
    canvas = Image.new("RGB", (w, h), (0, 0, 0))
    for win in iter_desktop_windows(vd):
        hwnd = win["hwnd"]
        x, y, ww, hh = win["rect"]
        try:
            img = capture_window(hwnd, strategy=strategy)
        except Exception:
            img = None
        if img is None:
            continue
        try:
            img = img.convert("RGB")
            if img.size != (ww, hh):
                img = img.resize((ww, hh))
            # Clip to canvas (windows can straddle monitor edges).
            sx0, sy0 = max(0, -x), max(0, -y)
            dx0, dy0 = max(0, x), max(0, y)
            dx1, dy1 = min(w, x + ww), min(h, y + hh)
            if dx1 > dx0 and dy1 > dy0:
                crop = img.crop((sx0, sy0, sx0 + (dx1 - dx0), sy0 + (dy1 - dy0)))
                canvas.paste(crop, (dx0, dy0))
        except Exception:
            continue
    return canvas


def virtual_screen_rect() -> Tuple[int, int, int, int]:
    """Full virtual screen (all monitors): (x, y, w, h)."""
    if not on_windows():
        return (0, 0, 1920, 1080)
    try:
        import ctypes

        user32 = ctypes.windll.user32
        x = int(user32.GetSystemMetrics(76))  # SM_XVIRTUALSCREEN
        y = int(user32.GetSystemMetrics(77))  # SM_YVIRTUALSCREEN
        w = int(user32.GetSystemMetrics(78))  # SM_CXVIRTUALSCREEN
        h = int(user32.GetSystemMetrics(79))  # SM_CYVIRTUALSCREEN
        return (x, y, max(1, w), max(1, h))
    except Exception:
        return (0, 0, 1920, 1080)


_dpi_awareness_done = False


def _ensure_dpi_awareness() -> None:
    """Make BitBlt coordinates match real pixels (idempotent)."""
    global _dpi_awareness_done
    if _dpi_awareness_done or not on_windows():
        _dpi_awareness_done = True
        return
    try:
        import ctypes

        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)  # per-monitor v2
        except Exception:
            ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass
    _dpi_awareness_done = True


def _com_init() -> None:
    """CoInitialize for worker threads calling pyvda (COM). Harmless if
    the thread is already initialized."""
    if not on_windows():
        return
    try:
        import ctypes

        ctypes.windll.ole32.CoInitializeEx(None, 0x2)
    except Exception:
        pass


def _bitblt_virtual_screen():
    """Raw BitBlt of the CURRENTLY VISIBLE virtual screen -> PIL Image.

    Uses CAPTUREBLT so layered windows are included. Caller must have
    already switched to the target desktop.
    """
    import ctypes

    _ensure_dpi_awareness()
    user32 = ctypes.windll.user32
    gdi32 = ctypes.windll.gdi32
    x, y, w, h = virtual_screen_rect()

    hdc_screen = user32.GetDC(0)
    hdc_mem = gdi32.CreateCompatibleDC(hdc_screen)
    hbmp = gdi32.CreateCompatibleBitmap(hdc_screen, w, h)
    h_old = gdi32.SelectObject(hdc_mem, hbmp)
    try:
        SRCCOPY = 0x00CC0020
        CAPTUREBLT = 0x40000000
        ok = gdi32.BitBlt(hdc_mem, 0, 0, w, h, hdc_screen, x, y,
                          SRCCOPY | CAPTUREBLT)
        img = _bitmap_to_pil(hbmp, w, h) if ok else None
    finally:
        gdi32.SelectObject(hdc_mem, h_old)
        gdi32.DeleteObject(hbmp)
        gdi32.DeleteDC(hdc_mem)
        user32.ReleaseDC(0, hdc_screen)
    if img is None:
        raise ScreenshotError("BitBlt failed")
    return img.convert("RGB")


def _vd_go(vd) -> None:
    """Switch to vd's real virtual desktop. Raises ScreenshotError."""
    _com_init()
    target = getattr(vd, "_vd", None)
    if target is None:
        raise ScreenshotError("no real virtual desktop (shim mode?)")
    try:
        target.go()
    except Exception as exc:
        raise ScreenshotError(f"cannot switch to agent desktop: {exc}")


def _vd_current():
    """Current virtual desktop (pyvda object) or None."""
    try:
        from pyvda import VirtualDesktop as _VD

        return _VD.current()
    except Exception:
        return None


def capture_desktop_switch(vd, return_to=None,
                           flicker_budget_ms: float = 100.0
                           ) -> Tuple[object, float]:
    """PIXEL-PERFECT capture: switch to vd, BitBlt, switch back.

    Returns (PIL Image, flicker_ms) — the true DWM-composited frame:
    wallpaper, taskbar, shadows, GPU apps, everything, exactly as a user
    on that desktop would see it.

    The user's visible desktop DOES change for the duration; keep the
    measured flicker in mind (warns when over ``flicker_budget_ms``).
    ``return_to`` may be a pyvda VirtualDesktop to switch back to; when
    None the pre-switch current desktop is used.
    """
    if not on_windows():
        raise ScreenshotError("switch capture needs Windows")
    t0 = time.perf_counter()
    back = return_to if return_to is not None else _vd_current()
    _vd_go(vd)
    try:
        # Let DWM present one frame on the fresh desktop before grabbing.
        time.sleep(0.03)
        img = _bitblt_virtual_screen()
    finally:
        try:
            if back is not None:
                back.go()
        except Exception:
            pass
    flicker_ms = (time.perf_counter() - t0) * 1000.0
    if flicker_ms > flicker_budget_ms:
        import warnings

        warnings.warn(
            f"agent screenshot flicker {flicker_ms:.0f}ms exceeded budget "
            f"{flicker_budget_ms:.0f}ms")
    return img, flicker_ms


# ---------------------------------------------------------------------------
# user-activity safety: when is it OK to switch?
# ---------------------------------------------------------------------------

def user_idle_ms() -> float:
    """ms since last keyboard/mouse input. +inf off-Windows; 0.0 when
    unknown (conservative: assume the user is active)."""
    if not on_windows():
        return float("inf")
    try:
        import ctypes
        from ctypes import wintypes

        class LASTINPUTINFO(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.UINT),
                        ("dwTime", wintypes.DWORD)]

        lii = LASTINPUTINFO()
        lii.cbSize = ctypes.sizeof(lii)
        if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(lii)):
            return 0.0
        tick = ctypes.windll.kernel32.GetTickCount()
        return float((tick - lii.dwTime) & 0xFFFFFFFF)
    except Exception:
        return 0.0


def foreground_is_fullscreen() -> bool:
    """True when the foreground window covers a monitor edge-to-edge.

    Switching virtual desktops at that moment would yank the user out of
    a game / video / presentation — always defer in that case.
    """
    if not on_windows():
        return False
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return False
        rect = window_rect(int(hwnd))
        if not rect:
            return False
        x, y, w, h = rect
        vx, vy, vw, vh = virtual_screen_rect()
        # Fullscreen == covers the virtual screen, or exactly one monitor.
        if (x, y, w, h) == (vx, vy, vw, vh):
            return True
        try:
            n_mon = int(user32.GetSystemMetrics(80))  # SM_CMONITORS
        except Exception:
            n_mon = 1
        if n_mon == 1:
            sw = int(user32.GetSystemMetrics(0))
            sh = int(user32.GetSystemMetrics(1))
            return (x, y, w, h) == (0, 0, sw, sh)
        return False
    except Exception:
        return False


def safe_to_switch(idle_threshold_ms: float = 1500.0) -> Tuple[bool, str]:
    """(ok, reason). Never switch during fullscreen; otherwise require
    the user to have been idle for ``idle_threshold_ms``."""
    if foreground_is_fullscreen():
        return False, "fullscreen"
    idle = user_idle_ms()
    if idle < idle_threshold_ms:
        return False, f"user-active-{idle:.0f}ms"
    return True, "idle"


# One desktop switch at a time across ALL agents — interleaved switches
# would strand captures on the wrong desktop.
_screenshot_lock = threading.Lock()


def capture_desktop_smart(vd,
                          idle_threshold_ms: float = 1500.0,
                          max_wait_ms: float = 4000.0,
                          on_busy: str = "wait-then-switch",
                          flicker_budget_ms: float = 100.0,
                          lock_timeout_s: float = 30.0) -> Tuple[object, dict]:
    """Pixel-perfect screenshot with bounded user disruption.

    Policy (``on_busy``) when the user is active or fullscreen:
      - "wait-then-switch" (default): wait up to ``max_wait_ms`` for an
        idle, non-fullscreen moment; then switch-capture regardless.
        Bounded flicker, minimal keystroke-leak risk.
      - "wait-then-composite": same wait, but fall back to the zero-flicker
        composite when the user never idles. Never flickers; approximate.
      - "switch-now": capture immediately (fastest; brief flicker even
        mid-keystroke).
      - "composite-now": never switch; approximate composite.

    Returns (PIL Image, info) where info holds: mode, flicker_ms (or
    None), waited_ms, idle_ms_at_capture, and the safety reason string.
    Raises ScreenshotError only when every path fails.
    """
    if on_busy not in ("wait-then-switch", "wait-then-composite",
                       "switch-now", "composite-now"):
        raise ScreenshotError(f"unknown on_busy policy: {on_busy}")

    t_start = time.perf_counter()
    info: dict = {"mode": None, "flicker_ms": None, "waited_ms": 0.0,
                  "idle_ms_at_capture": None, "reason": None}

    def _do_switch() -> Tuple[object, dict]:
        if not _screenshot_lock.acquire(timeout=lock_timeout_s):
            raise ScreenshotError("screenshot lock timed out")
        try:
            info["idle_ms_at_capture"] = user_idle_ms()
            img, flicker = capture_desktop_switch(
                vd, flicker_budget_ms=flicker_budget_ms)
            info.update(mode="switch", flicker_ms=flicker)
            return img, info
        finally:
            _screenshot_lock.release()

    def _do_composite(reason: str) -> Tuple[object, dict]:
        info.update(mode="composite", reason=reason)
        return capture_desktop_composite(vd), info

    if on_busy == "switch-now":
        try:
            return _do_switch()
        except ScreenshotError as exc:
            return _do_composite(f"switch-failed:{exc}")
    if on_busy == "composite-now":
        return _do_composite("policy=composite-now")

    # wait-then-*: poll for a safe moment.
    deadline = t_start + max_wait_ms / 1000.0
    reason = "unknown"
    while True:
        ok, reason = safe_to_switch(idle_threshold_ms)
        if ok:
            break
        if time.perf_counter() >= deadline:
            break
        time.sleep(0.1)
    info["waited_ms"] = (time.perf_counter() - t_start) * 1000.0
    info["reason"] = reason
    if reason not in ("idle",):
        # Never became safe in time.
        if on_busy == "wait-then-composite":
            return _do_composite(f"busy-timeout:{reason}")
        # wait-then-switch: proceed anyway — bounded, measured flicker.
    try:
        return _do_switch()
    except ScreenshotError as exc:
        return _do_composite(f"switch-failed:{exc}")


def capture_desktop(vd, mode: str = "smart", **kwargs):
    """Screenshot of vd's virtual desktop as a PIL Image.

    Modes:
      - "smart" (default): pixel-perfect switch capture with idle gating,
        fullscreen deferral and a global switch mutex; falls back to the
        zero-flicker composite per ``on_busy``. Extra kwargs go to
        :func:`capture_desktop_smart`.
      - "switch": immediate switch capture; returns (img, flicker_ms).
        Use when you want the raw measurement.
      - "composite": zero-flicker per-window composite (approximate; never
        touches the user's visible desktop).
    """
    if mode == "smart":
        img, _info = capture_desktop_smart(vd, **kwargs)
        return img
    if mode == "switch":
        img, _flicker = capture_desktop_switch(vd, **kwargs)
        return img
    if mode == "composite":
        return capture_desktop_composite(vd, **kwargs)
    raise ScreenshotError(f"unknown mode: {mode}")


def capture_desktop_info(vd, **kwargs) -> Tuple[object, dict]:
    """Like capture_desktop(mode='smart') but also returns the info dict
    (flicker_ms, waited_ms, mode, reason)."""
    return capture_desktop_smart(vd, **kwargs)


def screenshot_png(vd, **kwargs) -> bytes:
    """PNG bytes of vd's virtual desktop (for the vision pipeline).

    Default is the pixel-perfect smart path; pass mode="composite" for
    the zero-flicker approximate path.
    """
    img = capture_desktop(vd, **kwargs)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()
