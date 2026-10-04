#!/usr/bin/env python3
"""Muse PC Agent v2 — fast local PC control with semantic commands.

Runs on Rehan's PC, loopback-only (127.0.0.1:18011).
Muse sends high-level intents; the agent executes locally at full speed.

Semantic layer:
  {"action": "open", "app": "paint"}        -> launch Paint by name, wait, focus
  {"action": "draw", "shape": "circle", "cx":..,"cy":..,"r":..} -> draw shape
  {"action": "type", "text": "hello"}       -> type text
  {"action": "apps"}                        -> list registered apps

Low-level (for fine control):
  move, click, down, up, path, circle, key, hotkey

No credentials, no network exposure. Loopback only.
"""
import json
import math
import os
import subprocess
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pyautogui

pyautogui.FAILSAFE = False
pyautogui.PAUSE = 0

HOST = "127.0.0.1"
PORT = 18011
BASE = os.path.join(os.path.expanduser("~"), "muse-browser-mcp")

# ---- app registry ----
def load_apps():
    try:
        with open(os.path.join(BASE, "apps.json")) as f:
            return json.load(f)
    except Exception:
        return {}

APPS = load_apps()

# ---- window helpers (pywinauto, lazy import) ----
_uia = None
def uia():
    global _uia
    if _uia is None:
        from pywinauto import Desktop
        _uia = Desktop(backend="uia")
    return _uia

def find_window(spec):
    """Find a window by class and/or title substring."""
    wcls = spec.get("window_class")
    tsub = spec.get("window_title_contains")
    for w in uia().windows():
        try:
            t = w.window_text() or ""
            c = w.element_info.class_name
        except Exception:
            continue
        if wcls and c != wcls:
            continue
        if tsub and tsub.lower() not in t.lower():
            continue
        if wcls or tsub:
            return w
    return None

def find_process(spec):
    """Check if the app's process is running (for tray apps with no window)."""
    import subprocess
    pname = spec.get("process_name", "").lower()
    if not pname:
        # derive from launch path basename
        launch = spec.get("launch", "")
        base = launch.replace("/", "\\").split("\\")[-1]
        if "." in base:
            pname = base[:base.rfind(".")].lower()
    if not pname:
        return False
    try:
        r = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq %s*" % pname],
            capture_output=True, text=True, timeout=10)
        return pname in r.stdout.lower()
    except Exception:
        return False

# ================= low-level mouse =================
def do_move(p):
    pyautogui.moveTo(int(p["x"]), int(p["y"]))
    return {"ok": True}

def do_click(p):
    pyautogui.click(int(p.get("x", 0)), int(p.get("y", 0)),
                    button=p.get("button", "left"))
    return {"ok": True}

def do_down(p):
    if p.get("x") is not None:
        pyautogui.moveTo(int(p["x"]), int(p["y"]))
    pyautogui.mouseDown(button=p.get("button", "left"))
    return {"ok": True}

def do_up(p):
    pyautogui.mouseUp(button=p.get("button", "left"))
    return {"ok": True}

def _drag_points(pts, button="left", delay_ms=2):
    if not pts:
        return
    d = delay_ms / 1000.0
    pyautogui.moveTo(int(pts[0][0]), int(pts[0][1]))
    time.sleep(0.05)
    pyautogui.mouseDown(button=button)
    time.sleep(0.02)
    for x, y in pts[1:]:
        pyautogui.moveTo(int(x), int(y))
        if d > 0:
            time.sleep(d)
    pyautogui.mouseUp(button=button)

def do_path(p):
    pts = p["points"]
    t0 = time.time()
    _drag_points(pts, p.get("button", "left"), p.get("delay_ms", 2))
    return {"ok": True, "points": len(pts), "seconds": round(time.time() - t0, 3)}

def circle_points(cx, cy, r, steps=72):
    pts = []
    for i in range(steps + 1):
        a = 2 * math.pi * i / steps
        pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
    return pts

def do_circle(p):
    pts = circle_points(int(p["cx"]), int(p["cy"]), int(p["r"]),
                        int(p.get("steps", 72)))
    t0 = time.time()
    _drag_points(pts, p.get("button", "left"), p.get("delay_ms", 2))
    return {"ok": True, "seconds": round(time.time() - t0, 3)}

def do_type(p):
    pyautogui.typewrite(p["text"], interval=0)
    return {"ok": True}

def do_key(p):
    pyautogui.press(p["key"])
    return {"ok": True}

def do_hotkey(p):
    pyautogui.hotkey(*p["keys"])
    return {"ok": True}

# ================= semantic: apps =================
def do_apps(p):
    return {"ok": True, "apps": {k: v.get("desc", k) for k, v in load_apps().items()}}

def do_open(p):
    name = (p.get("app") or "").lower()
    spec = load_apps().get(name)
    if not spec:
        return {"ok": False, "error": "unknown app '%s'" % name,
                "known": sorted(APPS.keys())}
    # already running? just focus it (window) or report running (tray app)
    win = find_window(spec)
    if win is not None:
        try:
            win.set_focus()
        except Exception:
            pass
        return {"ok": True, "app": name, "already_open": True}
    if find_process(spec):
        return {"ok": True, "app": name, "already_open": True,
                "note": "process running (tray app, no window)"}
    # launch
    launch = spec["launch"]
    if launch.startswith("shell:") or launch.startswith("ms-settings:"):
        subprocess.Popen(["explorer.exe", launch])
    elif launch.lower().endswith(".lnk"):
        os.startfile(launch)  # shortcuts need ShellExecute, not Popen
    else:
        subprocess.Popen(launch)
    # wait for window (or process, for tray apps with no window)
    win = None
    for _ in range(40):  # ~10s
        time.sleep(0.25)
        win = find_window(spec)
        if win is not None:
            break
        if find_process(spec):
            return {"ok": True, "app": name, "already_open": False,
                    "note": "process started (tray app, no window)"}
    if win is None:
        return {"ok": False, "error": "launched but window not found"}
    try:
        win.set_focus()
        time.sleep(0.5)
    except Exception:
        pass
    return {"ok": True, "app": name, "already_open": False}

def do_close(p):
    name = (p.get("app") or "").lower()
    spec = load_apps().get(name)
    if not spec:
        return {"ok": False, "error": "unknown app"}
    win = find_window(spec)
    if win is None:
        # no window — try killing the process (tray apps)
        if find_process(spec):
            import subprocess
            pname = spec.get("process_name", "")
            try:
                subprocess.run(["taskkill", "/F", "/IM", pname + ".exe"],
                               capture_output=True, timeout=15)
                time.sleep(1)
                if not find_process(spec):
                    return {"ok": True, "note": "process killed (tray app)"}
                return {"ok": False, "error": "process still running"}
            except Exception as e:
                return {"ok": False, "error": str(e)[:100]}
        return {"ok": True, "already_closed": True}
    try:
        win.close()
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}

def do_focus(p):
    name = (p.get("app") or "").lower()
    spec = load_apps().get(name)
    if not spec:
        return {"ok": False, "error": "unknown app"}
    win = find_window(spec)
    if win is None:
        return {"ok": False, "error": "not open"}
    win.set_focus()
    return {"ok": True}

# ================= semantic: draw =================
def do_draw(p):
    """Draw a shape. {"shape":"circle"|"square"|"triangle"|"line", cx,cy, r or size}"""
    shape = (p.get("shape") or "circle").lower()
    cx, cy = int(p["cx"]), int(p["cy"])
    button = p.get("button", "left")
    delay_ms = p.get("delay_ms", 2)
    t0 = time.time()
    if shape == "circle":
        r = int(p.get("r", 100))
        pts = circle_points(cx, cy, r, int(p.get("steps", 72)))
    elif shape == "square":
        s = int(p.get("size", 200))
        h = s // 2
        pts = [(cx - h, cy - h), (cx + h, cy - h),
               (cx + h, cy + h), (cx - h, cy + h), (cx - h, cy - h)]
    elif shape == "triangle":
        s = int(p.get("size", 200))
        h = s // 2
        pts = [(cx, cy - h), (cx + h, cy + h), (cx - h, cy + h), (cx, cy - h)]
    elif shape == "line":
        x2, y2 = int(p["x2"]), int(p["y2"])
        pts = [(cx, cy), (x2, y2)]
    else:
        return {"ok": False, "error": "unknown shape"}
    _drag_points(pts, button, delay_ms)
    return {"ok": True, "shape": shape,
            "seconds": round(time.time() - t0, 3)}

def _win32():
    import ctypes
    from ctypes import wintypes
    u = ctypes.windll.user32
    return u

def _find_window_by_title(substr):
    import ctypes
    from ctypes import wintypes
    u = ctypes.windll.user32
    found = []
    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd, _):
        if u.IsWindowVisible(hwnd):
            n = u.GetWindowTextLengthW(hwnd)
            if n:
                buf = ctypes.create_unicode_buffer(n + 1)
                u.GetWindowTextW(hwnd, buf, n + 1)
                if substr.lower() in buf.value.lower():
                    found.append((hwnd, buf.value))
        return True
    u.EnumWindows(cb, 0)
    return found

def do_win_minimize(p):
    u = _win32()
    hwnd = u.GetForegroundWindow()
    if hwnd:
        u.ShowWindow(hwnd, 6)  # SW_MINIMIZE
        import ctypes
        n = u.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(n + 1)
        u.GetWindowTextW(hwnd, buf, n + 1)
        do_win_minimize._last = (hwnd, buf.value)
        return {"ok": True, "minimized": buf.value[:60]}
    return {"ok": False, "error": "no foreground window"}
do_win_minimize._last = (None, "")

def do_win_focus(p):
    title = p.get("title", "")
    wins = _find_window_by_title(title)
    if not wins:
        return {"ok": False, "error": "no window matching: " + title[:40]}
    u = _win32()
    hwnd, name = wins[0]
    u.ShowWindow(hwnd, 9)  # SW_RESTORE
    u.SetForegroundWindow(hwnd)
    return {"ok": True, "focused": name[:60]}

def do_win_restore(p):
    # restore the window we minimized (or find by title)
    hwnd, name = do_win_minimize._last
    u = _win32()
    import ctypes
    if hwnd and u.IsWindow(hwnd):
        u.ShowWindow(hwnd, 9)
        u.SetForegroundWindow(hwnd)
        return {"ok": True, "restored": name[:60]}
    title = p.get("title", name)
    return do_win_focus({"title": title})

def do_dictate(p):
    """Trigger Wispr Flow push-to-talk (Ctrl+Win per user config)."""
    import ctypes
    # VK_LCONTROL=162, VK_LWIN=91
    for vk, up in [(162, False), (91, False), (91, True), (162, True)]:
        ctypes.windll.user32.keybd_event(vk, 0, 2 if up else 0, 0)
        time.sleep(0.05)
    return {"ok": True, "note": "push-to-talk triggered"}

ACTIONS = {
    # semantic
    "open": do_open, "close": do_close, "focus": do_focus, "apps": do_apps,
    "draw": do_draw, "dictate": do_dictate,
    "win_minimize": do_win_minimize, "win_focus": do_win_focus,
    "win_restore": do_win_restore,
    # low-level
    "move": do_move, "click": do_click, "down": do_down, "up": do_up,
    "path": do_path, "circle": do_circle,
    "type": do_type, "key": do_key, "hotkey": do_hotkey,
}

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            self._json({"ok": True, "agent": "pc-agent", "v": 2,
                        "apps": len(APPS)})
        else:
            self._json({"ok": False}, 404)

    def do_POST(self):
        if self.path != "/cmd":
            self._json({"ok": False}, 404)
            return
        try:
            n = int(self.headers.get("Content-Length", 0))
            req = json.loads(self.rfile.read(n) or b"{}")
        except Exception as e:
            self._json({"ok": False, "error": "bad json"}, 400)
            return
        fn = ACTIONS.get(req.get("action"))
        if not fn:
            self._json({"ok": False, "error": "unknown action",
                        "known": sorted(ACTIONS.keys())})
            return
        try:
            self._json(fn(req))
        except Exception as e:
            self._json({"ok": False, "error": str(e)[:200]})

def main():
    HTTPServer((HOST, PORT), Handler).serve_forever()

if __name__ == "__main__":
    main()
