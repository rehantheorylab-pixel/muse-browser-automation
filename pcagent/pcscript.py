#!/usr/bin/env python3
"""PCScript v2 — our own language for Windows PC control.

A REAL program. No AI, no agents. Reads a script, follows the rules.

Usage: python pcscript.py <script.pcs>

AI (Muse) writes the scripts from screenshots. This program runs them fast.

== APPS (any program) ==
  OPEN <name|path>        open by registry name, full path, or Start Menu search
  CLOSE <app>             close app
  FOCUS <app>             bring to front
  MINIMIZE <app>          minimize window
  MAXIMIZE <app>          maximize window
  RESTORE <app>           restore window
  RESIZE <app> <w> <h>    resize window
  MOVEWIN <app> <x> <y>   move window
  FINDAPP "<query>"       find installed program, print path

== MOUSE (every kind) ==
  MOVE <x> <y>            move mouse
  HOVER <x> <y>           move (for tooltips/menus)
  CLICK <x> <y>          left click
  DOUBLECLICK <x> <y>     double click
  RIGHTCLICK <x> <y>      right click
  MIDDLECLICK <x> <y>     middle click
  DOWN [button]           press mouse button down
  UP [button]             release
  DRAG <x1> <y1> TO <x2> <y2>
  SCROLL <n>              wheel (+up/-down)

== KEYBOARD (every kind) ==
  TYPE "text"             type text
  PRESS <key>             press key (enter, tab, f1..f24, volumeup, ...)
  HOLD <key>              hold key down (shift, ctrl, alt)
  RELEASE <key>           release held key
  SHORTCUT a+b+c          hotkey combo

== UI CONTROLS (by name, no coordinates) ==
  UICLICK "<name>"        click button/control by name in foreground window
  UISET "<name>" "text"   set text in a field by name
  UICHECK "<name>"        check a checkbox
  UIUNCHECK "<name>"      uncheck

== DRAWING ==
  DRAW CIRCLE AT <x> <y> RADIUS <r>
  DRAW SQUARE AT <x> <y> SIZE <s>
  DRAW TRIANGLE AT <x> <y> SIZE <s>
  DRAW LINE FROM <x1> <y1> TO <x2> <y2>
  TOOL "<name>"           select toolbar tool by name
  COLOR <r> <g> <b>       set Paint color
  FILL AT <x> <y>         fill bucket

== SYSTEM ==
  VOLUME <0-100>          set volume (0=mute)
  CLIPBOARD "text"        copy text to clipboard
  PASTE                   ctrl+v
  SCREENSHOT [SAVE <p>]   screenshot

== FLOW ==
  TAKEOVER               show control pill (no input blocking — user keeps mouse)
  RELEASE                hide pill
  CONFIRM "message"      pop permission dialog on PC, wait for Approve/Deny
  ASK "question" -> $var ask user a question via PC dialog, save to $var
  CHATASK "q" -> $var    ask via Muse chat: minimize app, focus browser,
                         Muse asks in chat, restore app on answer (indefinite)
  REQUIRE_YES $var       kill script unless $var is yes-like (for confirm-submit)
  SET $var "value"        set a variable (use $var in later commands)
  WAIT <sec>              pause
  VERIFY WINDOW <app>     fail if window not found
  VERIFY PIXEL <x> <y> COLOR <r> <g> <b> [TOL <n>]
  VERIFY FILE "<path>"    fail if file missing
  RUN <command...>        run PowerShell command

== SENSITIVE PATTERN (login/bank: fill, DON'T submit, confirm in chat) ==
  TYPE "user"             fill username
  TYPE "pass"             fill password (do NOT press enter yet)
  CHATASK "Filled login, NOT submitted. Reply YES to submit." -> $ok
  REQUIRE_YES $ok
  PRESS enter             submit only after user confirmed
"""
import ctypes
import json
import math
import os
import shlex
import subprocess
import sys
import time

import pyautogui

pyautogui.FAILSAFE = False
pyautogui.PAUSE = 0

BASE = os.path.join(os.path.expanduser("~"), "muse-browser-mcp")
APPS = json.load(open(os.path.join(BASE, "apps.json")))

OVERLAY = "http://127.0.0.1:18012"

def overlay_api(method, path, data=None, timeout=10):
    import urllib.request
    url = OVERLAY + path
    body = json.dumps(data or {}).encode()
    req = urllib.request.Request(url, data=body if method == "POST" else None,
                                 headers={"Content-Type": "application/json"},
                                 method=method)
    try:
        return json.loads(urllib.request.urlopen(req, timeout=timeout).read())
    except Exception:
        return {}

def check_stop():
    """Returns True if user clicked STOP. Call before every line in takeover."""
    st = overlay_api("GET", "/status")
    return bool(st.get("stop_requested"))

def click_maybe_under_stop(x, y, button="left"):
    """v7: plain click. No control bar to hide (pill is small, bottom-center)."""
    pyautogui.click(int(x), int(y), button=button)

def wait_for_resume():
    """Called when paused. Waits for resume, then handles AI re-analysis."""
    print("  [PAUSE] user paused — waiting for resume...")
    # wait until state leaves 'paused'
    while True:
        st = overlay_api("GET", "/status")
        s = st.get("state")
        if s == "idle" or st.get("stop_requested"):
            return "stopped"
        if s != "paused":
            break
        time.sleep(0.5)
    # state is now 'awaiting_ai' — AI must analyze before continuing
    st = overlay_api("GET", "/status")
    if st.get("state") == "awaiting_ai":
        print("  [RESUME] AI analyzing screen to get back on track...")
        # take fresh screenshot (controls hidden)
        r = overlay_api("POST", "/screenshot",
                        {"path": os.path.join(BASE, "resume_shot.png")})
        print("  [RESUME] screenshot: %s — waiting for AI decision" % r.get("path"))
        # wait for AI to call /ai_continue (or stop)
        while True:
            st = overlay_api("GET", "/status")
            if st.get("state") == "idle" or st.get("stop_requested"):
                return "stopped"
            if st.get("state") == "running":
                break
            time.sleep(0.5)
        print("  [RESUME] AI continuing from line %d" % st.get("paused_at_line", 0))
    return "resumed"

# ---------- UIA ----------
_uia = None
def uia():
    global _uia
    if _uia is None:
        from pywinauto import Desktop
        _uia = Desktop(backend="uia")
    return _uia

def find_window(spec_or_name):
    if isinstance(spec_or_name, str):
        name = spec_or_name.lower()
        spec = APPS.get(name, {})
        wcls = spec.get("window_class")
        tsub = spec.get("window_title_contains") or name
    else:
        wcls = spec_or_name.get("window_class")
        tsub = spec_or_name.get("window_title_contains")
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

def foreground_window():
    hwnd = ctypes.windll.user32.GetForegroundWindow()
    for w in uia().windows():
        try:
            if w.handle == hwnd:
                return w
        except Exception:
            pass
    return None

def find_control(win, name):
    nl = name.lower()
    for el in win.descendants():
        try:
            if (el.element_info.name or "").strip().lower() == nl:
                return el
        except Exception:
            pass
    return None

def find_button(win, name):
    nl = name.lower()
    for el in win.descendants(control_type="Button"):
        try:
            if (el.element_info.name or "").strip().lower() == nl:
                return el
        except Exception:
            pass
    return None

# ---------- app launching (any program) ----------
def resolve_app(name):
    """Return a launch command for any program."""
    nl = name.lower()
    # 1. registry
    if nl in APPS:
        return APPS[nl]["launch"], APPS[nl]
    # 2. direct path or command
    if os.path.exists(name) or name.endswith(".exe"):
        return name, {}
    # 3. Start Menu search
    found = search_start_menu(nl)
    if found:
        return found, {}
    # 4. try as-is (PATH lookup)
    return name, {}

def search_start_menu(query):
    roots = [
        os.path.join(os.path.expanduser("~"),
                     r"AppData\Roaming\Microsoft\Windows\Start Menu\Programs"),
        r"C:\ProgramData\Microsoft\Windows\Start Menu\Programs",
    ]
    ql = query.lower()
    for root in roots:
        for dp, _, fns in os.walk(root):
            for fn in fns:
                if fn.lower().endswith(".lnk") and ql in fn.lower():
                    return os.path.join(dp, fn)
    return None

# ---------- mouse ----------
def _drag(pts, button="left", delay_ms=2):
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

def circle_pts(cx, cy, r, steps=72):
    return [(cx + r * math.cos(2 * math.pi * i / steps),
             cy + r * math.sin(2 * math.pi * i / steps))
            for i in range(steps + 1)]

# ---------- Paint ----------
def paint_window():
    for w in uia().windows():
        try:
            if w.element_info.class_name == "MSPaintApp":
                return w
        except Exception:
            pass
    return None

def paint_set_color(r, g, b):
    win = paint_window()
    if win is None:
        raise RuntimeError("Paint not open")
    win.set_focus()
    time.sleep(0.6)
    btn = find_button(win, "Edit colors")
    if btn is None:
        raise RuntimeError("Edit colors not found")
    btn.click_input()
    time.sleep(1.2)
    dlg = None
    for el in win.descendants(control_type="Window"):
        try:
            if "Edit Colors" in (el.element_info.name or ""):
                dlg = el
                break
        except Exception:
            pass
    if dlg is None:
        raise RuntimeError("Edit Colors dialog not found")
    edits = []
    for el in dlg.descendants(control_type="Edit"):
        try:
            rc = el.rectangle()
            edits.append((rc.left, rc.top, el))
        except Exception:
            pass
    right = sorted([e for e in edits if e[0] > 940], key=lambda x: x[1])
    if len(right) < 3:
        raise RuntimeError("RGB boxes not found")
    for (_, _, el), val in zip(right[:3], [r, g, b]):
        el.iface_value.SetValue(str(val))
    time.sleep(0.6)
    ok = find_button(dlg, "OK")
    if ok:
        ok.click_input()
    time.sleep(0.8)

def paint_fill(x, y):
    win = paint_window()
    if win is None:
        raise RuntimeError("Paint not open")
    win.set_focus()
    time.sleep(0.6)
    btn = find_button(win, "Fill with color")
    if btn is None:
        raise RuntimeError("Fill tool not found")
    btn.click_input()
    time.sleep(0.6)
    pyautogui.click(int(x), int(y))
    time.sleep(1.0)

# ---------- volume ----------
def set_volume(pct):
    # use Windows key presses (0-100 -> 50 steps of 2%)
    pyautogui.press("volumute")  # ensure known state: start from mute
    time.sleep(0.2)
    pyautogui.press("volumute")  # unmute
    if pct == 0:
        pyautogui.press("volumute")
        return
    steps = int(pct / 2)
    for _ in range(50):
        pyautogui.press("volumedown")
    time.sleep(0.2)
    for _ in range(steps):
        pyautogui.press("volumeup")

# ---------- commands ----------
def cmd_open(args):
    name = " ".join(args)
    launch, spec = resolve_app(name)
    if spec:
        win = find_window(spec)
        if win is not None:
            win.set_focus()
            return "already open, focused"
    if launch.startswith("shell:") or launch.startswith("ms-settings:"):
        subprocess.Popen(["explorer.exe", launch])
    elif launch.lower().endswith(".lnk"):
        os.startfile(launch)
    else:
        subprocess.Popen(launch, shell=True)
    # wait for *any* new window if we don't have a spec
    for _ in range(40):
        time.sleep(0.25)
        if spec and find_window(spec) is not None:
            break
        if not spec:
            break
    else:
        raise RuntimeError("window did not appear")
    if spec:
        w = find_window(spec)
        if w is not None:
            w.set_focus()
            time.sleep(0.5)
    return "opened: %s" % launch

def _win_op(args, op):
    name = " ".join(args).lower()
    spec = APPS.get(name, {})
    if not spec:
        # try by title substring
        w = find_window(name)
    else:
        w = find_window(spec)
    if w is None:
        raise RuntimeError("window not found: %s" % name)
    if op == "minimize":
        w.minimize()
    elif op == "maximize":
        w.maximize()
    elif op == "restore":
        w.restore()
    elif op == "close":
        w.close()
    return op + "d"

def cmd_draw(args):
    shape = args[0].lower()
    u = [a.upper() for a in args]
    if shape == "circle":
        x = int(args[u.index("AT") + 1]); y = int(args[u.index("AT") + 2])
        r = int(args[u.index("RADIUS") + 1])
        _drag(circle_pts(x, y, r))
    elif shape == "square":
        x = int(args[u.index("AT") + 1]); y = int(args[u.index("AT") + 2])
        s = int(args[u.index("SIZE") + 1]); h = s // 2
        _drag([(x-h,y-h),(x+h,y-h),(x+h,y+h),(x-h,y+h),(x-h,y-h)])
    elif shape == "triangle":
        x = int(args[u.index("AT") + 1]); y = int(args[u.index("AT") + 2])
        s = int(args[u.index("SIZE") + 1]); h = s // 2
        _drag([(x,y-h),(x+h,y+h),(x-h,y+h),(x,y-h)])
    elif shape == "line":
        x1 = int(args[u.index("FROM") + 1]); y1 = int(args[u.index("FROM") + 2])
        x2 = int(args[u.index("TO") + 1]); y2 = int(args[u.index("TO") + 2])
        _drag([(x1,y1),(x2,y2)])
    else:
        raise RuntimeError("unknown shape")
    return "drew %s" % shape

# ---------- parser & runner ----------
def parse_line(line):
    out, in_q = [], False
    for ch in line:
        if ch == '"':
            in_q = not in_q
        if ch == "#" and not in_q:
            break
        out.append(ch)
    line = "".join(out).strip()
    if not line:
        return None, []
    parts = shlex.split(line)
    return parts[0].upper(), parts[1:]

VARS = {}

def subst(args):
    """Replace $var with variable values."""
    out = []
    for a in args:
        if a.startswith("$"):
            out.append(VARS.get(a[1:], a))
        else:
            # inline $var within a string
            s = a
            for k, v in VARS.items():
                s = s.replace("$" + k, v)
            out.append(s)
    return out

def run_script(path):
    with open(path) as f:
        lines = f.readlines()
    ok_count, fail_count = 0, 0
    t_start = time.time()
    takeover = False
    for i, raw in enumerate(lines, 1):
        cmd, args = parse_line(raw)
        if cmd is None:
            continue
        # user STOP / PAUSE check (only in takeover mode)
        if takeover:
            st = overlay_api("GET", "/status")
            if st.get("stop_requested") or st.get("state") == "idle":
                print("  [STOP] user clicked STOP — aborting")
                overlay_api("POST", "/hide")
                print("done: %d ok, stopped by user" % ok_count)
                return False
            if st.get("state") == "paused":
                print("  [PAUSE] user paused — waiting for resume…")
                while True:
                    st2 = overlay_api("GET", "/status")
                    if st2.get("state") == "idle" or st2.get("stop_requested"):
                        print("  [STOP] user stopped during pause")
                        overlay_api("POST", "/hide")
                        print("done: %d ok, stopped by user" % ok_count)
                        return False
                    if st2.get("state") != "paused":
                        break
                    time.sleep(0.5)
                print("  [RESUME] continuing…")
            if st.get("state") == "takeover":
                print("  [TAKEOVER] user took control — waiting for release…")
                while True:
                    st2 = overlay_api("GET", "/status")
                    if st2.get("state") == "idle" or st2.get("stop_requested"):
                        print("  [STOP] user stopped during takeover")
                        overlay_api("POST", "/hide")
                        print("done: %d ok, stopped by user" % ok_count)
                        return False
                    if st2.get("state") != "takeover":
                        break
                    time.sleep(0.5)
                print("  [RELEASE] user released control — continuing…")
        t0 = time.time()
        try:
            # substitute $vars, except for ASK/SET which handle it themselves
            if cmd not in ("ASK", "SET"):
                args = subst(args)
            if cmd == "TAKEOVER":
                # v7: show the control pill. NO input blocking (that froze the
                # mouse). The user pauses/takes over via the pill; the script
                # checks stop/pause before every line.
                r = overlay_api("POST", "/show", {"state": "running",
                    "narration": "Starting…"})
                takeover = True
                res = "pill shown" if r.get("ok") else "overlay failed"
            elif cmd == "RELEASE":
                overlay_api("POST", "/hide")
                takeover = False
                res = "released"
            elif cmd == "CONFIRM":
                # Ask user permission via PC dialog. NO TIMEOUT — waits indefinitely.
                # If user denies/closes, kill the script.
                msg = " ".join(args)
                print("  [CONFIRM] asking user (waiting indefinitely): %s" % msg)
                r = overlay_api("POST", "/confirm", {"message": msg}, timeout=86400)
                if not r.get("approved"):
                    raise RuntimeError("user denied permission — killing script")
                res = "user approved"
            elif cmd == "ASK":
                # Ask user a question via PC dialog. Save answer to $var.
                # ASK "What is your name?" -> $name
                u = [a.upper() for a in args]
                if "->" in args:
                    ai = args.index("->")
                    question = " ".join(args[:ai])
                    varname = args[ai + 1].lstrip("$")
                else:
                    question = " ".join(args)
                    varname = "answer"
                question = subst([question])[0]
                print("  [ASK] asking user (waiting indefinitely): %s" % question)
                r = overlay_api("POST", "/ask", {"question": question}, timeout=86400)
                ans = r.get("answer")
                if ans is None:
                    raise RuntimeError("user cancelled — killing script")
                VARS[varname] = ans
                # save to file so the AI can read it
                with open(os.path.join(BASE, "ask_answer.txt"), "w") as f:
                    f.write(ans)
                res = "user answered: %s" % ans
            elif cmd == "CHATASK":
                # Ask the user via the Muse chat (browser), not a PC dialog.
                # Flow: minimize current app -> focus browser -> Muse asks in chat
                # -> user answers -> restore app -> continue.
                # CHATASK "Your question?" -> $var
                u = [a.upper() for a in args]
                if "->" in args:
                    ai = args.index("->")
                    question = " ".join(args[:ai])
                    varname = args[ai + 1].lstrip("$")
                else:
                    question = " ".join(args)
                    varname = "answer"
                question = subst([question])[0]
                print("  [CHATASK] switching to chat: %s" % question)
                import ctypes
                from ctypes import wintypes
                _u = ctypes.windll.user32
                # 1. minimize foreground window, remember it
                _fg = _u.GetForegroundWindow()
                _fg_title = ""
                if _fg:
                    n = _u.GetWindowTextLengthW(_fg)
                    _buf = ctypes.create_unicode_buffer(n + 1)
                    _u.GetWindowTextW(_fg, _buf, n + 1)
                    _fg_title = _buf.value
                    _u.ShowWindow(_fg, 6)
                # 2. focus browser (Chrome) so Rehan sees the chat
                @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
                def _cb(hwnd, _):
                    if _u.IsWindowVisible(hwnd):
                        nn = _u.GetWindowTextLengthW(hwnd)
                        if nn:
                            bb = ctypes.create_unicode_buffer(nn + 1)
                            _u.GetWindowTextW(hwnd, bb, nn + 1)
                            t = bb.value.lower()
                            if "chrome" in t and ("muse" in t or "chat" in t):
                                _cb.found = (hwnd, bb.value)
                                return False
                    return True
                _cb.found = None
                _u.EnumWindows(_cb, 0)
                if _cb.found:
                    _u.ShowWindow(_cb.found[0], 9)
                    _u.SetForegroundWindow(_cb.found[0])
                else:
                    # fallback: focus any Chrome window
                    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
                    def _cb2(hwnd, _):
                        if _u.IsWindowVisible(hwnd):
                            nn = _u.GetWindowTextLengthW(hwnd)
                            if nn:
                                bb = ctypes.create_unicode_buffer(nn + 1)
                                _u.GetWindowTextW(hwnd, bb, nn + 1)
                                if "chrome" in bb.value.lower():
                                    _cb2.found = (hwnd, bb.value)
                                    return False
                        return True
                    _cb2.found = None
                    _u.EnumWindows(_cb2, 0)
                    if _cb2.found:
                        _u.ShowWindow(_cb2.found[0], 9)
                        _u.SetForegroundWindow(_cb2.found[0])
                # 3. tell overlay we're waiting; Muse (me) will ask in chat
                #    and POST the answer to /answer when Rehan replies.
                overlay_api("POST", "/ask", {"question": question})
                # save question where Muse can read it
                with open(os.path.join(BASE, "chat_question.txt"), "w",
                          encoding="utf-8") as f:
                    f.write(question)
                print("  [CHATASK] waiting for answer in chat (indefinite)…")
                r = overlay_api("GET", "/wait_input", timeout=320)
                ans = r.get("answer")
                if ans is None:
                    # user stopped instead of answering
                    if r.get("stop_requested"):
                        raise RuntimeError("user stopped — killing script")
                    raise RuntimeError("no answer — killing script")
                VARS[varname] = ans
                with open(os.path.join(BASE, "ask_answer.txt"), "w",
                          encoding="utf-8") as f:
                    f.write(ans)
                # 4. restore the app window
                if _fg and _u.IsWindow(_fg):
                    _u.ShowWindow(_fg, 9)
                    _u.SetForegroundWindow(_fg)
                elif _fg_title:
                    overlay_api("POST", "/narrate",
                                {"text": "Restoring " + _fg_title[:40]})
                res = "user answered in chat: %s" % ans
            elif cmd == "REQUIRE_YES":
                # Kill the script unless $var is yes-like.
                # Pattern for sensitive actions:
                #   TYPE "user" / TYPE "pass"      (fill, do NOT submit)
                #   CHATASK "Filled login, not submitted. Reply YES to submit." -> $ok
                #   REQUIRE_YES $ok
                #   PRESS enter                     (submit only after confirmation)
                varname = args[0].lstrip("$")
                val = VARS.get(varname, "").strip().lower()
                if val not in ("yes", "y", "ok", "confirm", "confirmed",
                               "submit", "go", "1", "true"):
                    raise RuntimeError(
                        "user did not confirm ($%s=%r) — killing script" %
                        (varname, VARS.get(varname, "")))
                res = "user confirmed"
            elif cmd == "SET":
                # SET $var "value"
                varname = args[0].lstrip("$")
                VARS[varname] = " ".join(subst(args[1:]))
                res = "set $%s" % varname
            elif cmd == "OPEN":
                res = cmd_open(args)
            elif cmd == "CLOSE":
                try:
                    res = _win_op(args, "close")
                except RuntimeError:
                    # no window (tray app?) — kill the process instead
                    name = " ".join(args).lower()
                    spec = APPS.get(name, {})
                    pname = spec.get("process_name", "")
                    if pname:
                        subprocess.run(["taskkill", "/F", "/IM", pname + ".exe"],
                                       capture_output=True, timeout=15)
                        res = "process killed (tray app)"
                    else:
                        raise
            elif cmd == "FOCUS":
                name = " ".join(args).lower()
                spec = APPS.get(name, {})
                w = find_window(spec) if spec else find_window(name)
                if w is None:
                    raise RuntimeError("not open")
                w.set_focus(); res = "focused"
            elif cmd == "MINIMIZE":
                res = _win_op(args, "minimize")
            elif cmd == "MAXIMIZE":
                res = _win_op(args, "maximize")
            elif cmd == "RESTORE":
                res = _win_op(args, "restore")
            elif cmd == "RESIZE":
                name = args[0].lower()
                w = find_window(APPS.get(name, {}) or name)
                if w is None:
                    raise RuntimeError("not open")
                rc = w.rectangle()
                w.move_window(rc.left, rc.top, int(args[1]), int(args[2]))
                res = "resized"
            elif cmd == "MOVEWIN":
                name = args[0].lower()
                w = find_window(APPS.get(name, {}) or name)
                if w is None:
                    raise RuntimeError("not open")
                rc = w.rectangle()
                w.move_window(int(args[1]), int(args[2]),
                              rc.width(), rc.height())
                res = "moved"
            elif cmd == "FINDAPP":
                p = search_start_menu(" ".join(args).lower())
                res = p or "not found"
                if not p:
                    raise RuntimeError("not found")
            # mouse
            elif cmd == "MOVE" or cmd == "HOVER":
                pyautogui.moveTo(int(args[0]), int(args[1])); res = "moved"
            elif cmd == "CLICK":
                if takeover:
                    click_maybe_under_stop(int(args[0]), int(args[1]))
                else:
                    pyautogui.click(int(args[0]), int(args[1]))
                res = "clicked"
            elif cmd == "DOUBLECLICK":
                if takeover:
                    click_maybe_under_stop(int(args[0]), int(args[1]))
                    time.sleep(0.1)
                    click_maybe_under_stop(int(args[0]), int(args[1]))
                else:
                    pyautogui.doubleClick(int(args[0]), int(args[1]))
                res = "double-clicked"
            elif cmd == "RIGHTCLICK":
                if takeover:
                    click_maybe_under_stop(int(args[0]), int(args[1]), "right")
                else:
                    pyautogui.click(int(args[0]), int(args[1]), button="right")
                res = "right-clicked"
            elif cmd == "MIDDLECLICK":
                pyautogui.click(int(args[0]), int(args[1]), button="middle"); res = "middle-clicked"
            elif cmd == "DOWN":
                pyautogui.mouseDown(button=args[0] if args else "left"); res = "down"
            elif cmd == "UP":
                pyautogui.mouseUp(button=args[0] if args else "left"); res = "up"
            elif cmd == "DRAG":
                u = [a.upper() for a in args]
                x1, y1 = int(args[0]), int(args[1])
                x2, y2 = int(args[u.index("TO")+1]), int(args[u.index("TO")+2])
                _drag([(x1,y1),(x2,y2)], delay_ms=5); res = "dragged"
            elif cmd == "SCROLL":
                pyautogui.scroll(int(args[0])); res = "scrolled"
            # keyboard
            elif cmd == "TYPE":
                txt = " ".join(args)
                if len(txt) > 20:
                    # fast path: clipboard + paste (instant for long text)
                    subprocess.run(["powershell", "-NoProfile", "-Command",
                                    "Set-Clipboard -Value '%s'" % txt.replace("'", "''")],
                                   capture_output=True, timeout=15)
                    pyautogui.hotkey("ctrl", "v")
                    res = "typed via clipboard"
                else:
                    pyautogui.typewrite(txt, interval=0); res = "typed"
            elif cmd == "PRESS":
                pyautogui.press(args[0].lower()); res = "pressed"
            elif cmd == "HOLD":
                pyautogui.keyDown(args[0].lower()); res = "holding"
            elif cmd == "RELEASE":
                pyautogui.keyUp(args[0].lower()); res = "released"
            elif cmd == "SHORTCUT":
                pyautogui.hotkey(*args[0].lower().split("+")); res = "shortcut"
            # UI controls
            elif cmd == "UICLICK":
                w = foreground_window()
                if w is None:
                    raise RuntimeError("no foreground window")
                el = find_control(w, " ".join(args))
                if el is None:
                    raise RuntimeError("control not found")
                el.click_input(); time.sleep(0.4); res = "clicked control"
            elif cmd == "UISET":
                w = foreground_window()
                if w is None:
                    raise RuntimeError("no foreground window")
                el = find_control(w, args[0])
                if el is None:
                    raise RuntimeError("control not found")
                el.iface_value.SetValue(" ".join(args[1:]))
                res = "text set"
            elif cmd == "UICHECK" or cmd == "UIUNCHECK":
                w = foreground_window()
                if w is None:
                    raise RuntimeError("no foreground window")
                el = find_control(w, " ".join(args))
                if el is None:
                    raise RuntimeError("control not found")
                try:
                    state = el.iface_toggle.ToggleState
                except Exception:
                    raise RuntimeError("not a checkbox")
                want = 1 if cmd == "UICHECK" else 0
                if state != want:
                    el.click_input(); time.sleep(0.3)
                res = "checkbox set"
            # drawing
            elif cmd == "DRAW":
                res = cmd_draw(args)
            elif cmd == "TOOL":
                w = foreground_window()
                if w is None:
                    raise RuntimeError("no foreground window")
                btn = find_button(w, " ".join(args))
                if btn is None:
                    raise RuntimeError("tool not found")
                btn.click_input(); time.sleep(0.6); res = "tool selected"
            elif cmd == "COLOR":
                paint_set_color(int(args[0]), int(args[1]), int(args[2]))
                res = "color set"
            elif cmd == "FILL":
                u = [a.upper() for a in args]
                paint_fill(int(args[u.index("AT")+1]), int(args[u.index("AT")+2]))
                res = "filled"
            # system
            elif cmd == "VOLUME":
                set_volume(int(args[0])); res = "volume set"
            elif cmd == "CLIPBOARD":
                subprocess.run(["powershell", "-NoProfile", "-Command",
                                "Set-Clipboard -Value '%s'" % " ".join(args).replace("'", "''")],
                               capture_output=True, timeout=15)
                res = "copied"
            elif cmd == "PASTE":
                pyautogui.hotkey("ctrl", "v"); res = "pasted"
            elif cmd == "SCREENSHOT":
                p = args[1] if len(args) > 1 and args[0].upper() == "SAVE" else os.path.join(BASE, "shot.png")
                if takeover:
                    # overlay hides the Stop button so AI never sees it
                    r = overlay_api("POST", "/screenshot", {"path": p})
                    res = "saved to %s" % r.get("path", p)
                else:
                    pyautogui.screenshot(p); res = "saved to %s" % p
            # flow
            elif cmd == "WAIT":
                time.sleep(float(args[0])); res = "waited"
            elif cmd == "VERIFY":
                sub = args[0].upper()
                if sub == "WINDOW":
                    name = args[1].lower()
                    w = find_window(APPS.get(name, {}) or name)
                    if w is None:
                        raise RuntimeError("window not found")
                    res = "window verified"
                elif sub == "PIXEL":
                    u = [a.upper() for a in args]
                    x, y = int(args[1]), int(args[2])
                    er = int(args[u.index("COLOR")+1]); eg = int(args[u.index("COLOR")+2]); eb = int(args[u.index("COLOR")+3])
                    tol = int(args[u.index("TOL")+1]) if "TOL" in u else 10
                    pr, pg, pb = pyautogui.pixel(x, y)
                    if abs(pr-er) > tol or abs(pg-eg) > tol or abs(pb-eb) > tol:
                        raise RuntimeError("pixel (%d,%d,%d)" % (pr,pg,pb))
                    res = "pixel verified"
                elif sub == "FILE":
                    if not os.path.exists(" ".join(args[1:])):
                        raise RuntimeError("file missing")
                    res = "file verified"
                else:
                    raise RuntimeError("unknown VERIFY")
            elif cmd == "RUN":
                r = subprocess.run(" ".join(args), shell=True,
                                   capture_output=True, text=True, timeout=120)
                if r.returncode != 0:
                    raise RuntimeError("exit %d" % r.returncode)
                res = "exit 0"
            else:
                raise RuntimeError("unknown command '%s'" % cmd)
            dt = (time.time() - t0) * 1000
            print("  [OK] L%d %s (%.0fms) -> %s" % (i, cmd, dt, res))
            ok_count += 1
        except Exception as e:
            dt = (time.time() - t0) * 1000
            print("  [FAIL] L%d %s (%.0fms) -> %s" % (i, cmd, dt, e))
            fail_count += 1
            if takeover:
                overlay_api("POST", "/hide")
                takeover = False
            break
    total = (time.time() - t_start)
    if takeover:
        overlay_api("POST", "/hide")
    print("done: %d ok, %d failed, %.1fs total" % (ok_count, fail_count, total))
    return fail_count == 0

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: python pcscript.py <script.pcs>")
        sys.exit(2)
    sys.exit(0 if run_script(sys.argv[1]) else 1)
