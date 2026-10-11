"""Input-method ladder for s3rehan.

Research (DEEP_RESEARCH_ROUND2.md §2, UiPath internals): input methods
form a speed ladder. Fastest methods hook the app directly and work in
the background; slowest need a visible window and steal focus.

Ladder (fastest -> slowest):
  1. INVOKE   UIA InvokePattern.Invoke() — background, no focus change.
  2. SETVALUE UIA ValuePattern.SetValue() — background, for text fields.
  3. MESSAGE  SendMessage/PostMessage to the window — background-ish.
  4. CDP      Chrome DevTools Input.dispatchMouseEvent — background tab OK.
  5. HARDWARE pyautogui / SendInput — needs the VISIBLE desktop.

Rules:
  - Workers on hidden virtual desktops (background_only=True) may use
    only 1-4. HARDWARE is refused — it would click on the USER's screen.
  - Per-(app, action) memory: remember which method worked last; try it
    first next time (apps differ — some ignore InvokePattern).
  - Every attempt is timed; the ladder records what worked for the
    benchmark and for Rehan's "which method is fastest" questions.

This module is pure logic + Windows calls isolated in small functions
so unit tests run anywhere (Windows-only parts degrade gracefully).
"""

import os
import threading
import time
from typing import Callable, Dict, List, Optional, Tuple

# Method names in ladder order (fastest first).
INVOKE = "invoke"
SETVALUE = "setvalue"
MESSAGE = "message"
CDP = "cdp"
HARDWARE = "hardware"

LADDER: List[str] = [INVOKE, SETVALUE, MESSAGE, CDP, HARDWARE]

# Methods allowed when background_only=True (hidden virtual desktop).
BACKGROUND_METHODS = {INVOKE, SETVALUE, MESSAGE, CDP}

# Which methods apply to which action kinds, in LADDER (fastest-first)
# order: MESSAGE (window messages) ranks ahead of CDP (DevTools
# round-trip) per the module docstring's speed research.
METHOD_FOR_ACTION: Dict[str, List[str]] = {
    "click": [INVOKE, MESSAGE, CDP, HARDWARE],
    "type": [SETVALUE, MESSAGE, CDP, HARDWARE],
    "press": [MESSAGE, CDP, HARDWARE],  # hotkeys have no InvokePattern
}


class InputLadder:
    """Try input methods fastest-first; remember what works per app.

    Usage:
        ladder = InputLadder()
        ok, method, ms = ladder.click(app="notepad", x=100, y=200,
                                      el=uia_element_or_None,
                                      background_only=True)
    The caller supplies the actual per-method callables at construction
    (so tests inject fakes and production wires real UIA/CDP/pyautogui).
    """

    def __init__(
        self,
        do_invoke: Optional[Callable] = None,
        do_setvalue: Optional[Callable] = None,
        do_message: Optional[Callable] = None,
        do_cdp: Optional[Callable] = None,
        do_hardware: Optional[Callable] = None,
    ):
        self._impl: Dict[str, Optional[Callable]] = {
            INVOKE: do_invoke,
            SETVALUE: do_setvalue,
            MESSAGE: do_message,
            CDP: do_cdp,
            HARDWARE: do_hardware,
        }
        # (app_norm, action) -> method that worked last time.
        self._memory: Dict[Tuple[str, str], str] = {}
        self._lock = threading.Lock()
        # Timing log for benchmarks: list of dicts.
        self.attempt_log: List[Dict] = []

    # -- public API ---------------------------------------------------

    def act(
        self,
        action: str,
        app: Optional[str] = None,
        background_only: bool = False,
        **kwargs,
    ) -> Tuple[bool, str, float]:
        """Run the ladder for one action.

        Returns (ok, method_used, ms_total). method_used is "none" when
        every applicable method failed or was unavailable.
        """
        t0 = time.perf_counter()
        methods = self._order(action, app, background_only)
        last_err: Optional[str] = None
        for method in methods:
            fn = self._impl.get(method)
            if fn is None:
                continue
            try:
                ok = bool(fn(action=action, **kwargs))
            except Exception as e:  # noqa: BLE001
                ok, last_err = False, f"{type(e).__name__}: {e}"
            ms = (time.perf_counter() - t0) * 1000.0
            self._log(action, app, method, ok, ms, background_only)
            if ok:
                self._remember(app, action, method)
                return True, method, ms
        ms = (time.perf_counter() - t0) * 1000.0
        self._log(action, app, "none", False, ms, background_only,
                  note=last_err)
        return False, "none", ms

    def click(self, app=None, background_only=False, **kw):
        return self.act("click", app=app, background_only=background_only,
                        **kw)

    def type(self, app=None, background_only=False, **kw):
        return self.act("type", app=app, background_only=background_only,
                        **kw)

    def press(self, app=None, background_only=False, **kw):
        return self.act("press", app=app, background_only=background_only,
                        **kw)

    # -- ordering -----------------------------------------------------

    def _order(self, action: str, app: Optional[str],
               background_only: bool) -> List[str]:
        cands = list(METHOD_FOR_ACTION.get(action, LADDER))
        if background_only:
            cands = [m for m in cands if m in BACKGROUND_METHODS]
        # Remembered winner first (per app), rest in ladder order.
        key = ((app or "").strip().lower(), action)
        with self._lock:
            winner = self._memory.get(key)
        if winner in cands:
            cands.remove(winner)
            cands.insert(0, winner)
        return cands

    def _remember(self, app: Optional[str], action: str, method: str) -> None:
        with self._lock:
            self._memory[((app or "").strip().lower(), action)] = method

    def _log(self, action, app, method, ok, ms, background_only,
             note=None) -> None:
        rec = {"action": action, "app": app, "method": method, "ok": ok,
               "ms": round(ms, 1), "background_only": background_only,
               "ts": time.time()}
        if note:
            rec["note"] = str(note)[:200]
        with self._lock:
            self.attempt_log.append(rec)
            if len(self.attempt_log) > 2000:
                del self.attempt_log[:1000]

    # -- Windows implementations (wired by production, not tests) -----

    @staticmethod
    def windows_impls():
        """Build an InputLadder with real Windows callables.

        Returns None on non-Windows (caller falls back to fakes).
        """
        if os.name != "nt":
            return None
        return InputLadder(
            do_invoke=_uia_invoke,
            do_setvalue=_uia_setvalue,
            do_message=_win_message,
            do_cdp=None,  # wired by the browser layer per tab
            do_hardware=_hw_click_type,
        )


def _uia_invoke(action: str, el=None, **kw) -> bool:
    """UIA InvokePattern — background click, no focus change."""
    if el is None or action != "click":
        return False
    try:
        pat = el.GetInvokePattern()
        if pat is None:
            return False
        pat.Invoke()
        return True
    except Exception:
        return False


def _uia_setvalue(action: str, el=None, text: str = "", **kw) -> bool:
    """UIA ValuePattern — background text entry."""
    if el is None or action != "type":
        return False
    try:
        pat = el.GetValuePattern()
        if pat is None:
            return False
        pat.SetValue(text or "")
        return True
    except Exception:
        return False


def _win_message(action: str, hwnd: int = 0, **kw) -> bool:
    """SendMessage/PostMessage — background-ish window messages."""
    if not hwnd or os.name != "nt":
        return False
    try:
        import ctypes

        user32 = ctypes.windll.user32
        WM_LBUTTONDOWN, WM_LBUTTONUP = 0x0201, 0x0202
        if action == "click":
            x, y = int(kw.get("x", 0)), int(kw.get("y", 0))
            lparam = (y << 16) | (x & 0xFFFF)
            user32.PostMessageW(hwnd, WM_LBUTTONDOWN, 1, lparam)
            user32.PostMessageW(hwnd, WM_LBUTTONUP, 0, lparam)
            return True
        return False
    except Exception:
        return False


def _hw_click_type(action: str, x: int = 0, y: int = 0, text: str = "",
                   keys: str = "", **kw) -> bool:
    """Hardware input via pyautogui — VISIBLE desktop only."""
    try:
        import pyautogui

        if action == "click":
            pyautogui.click(x, y)
        elif action == "type":
            if text:
                pyautogui.write(text)
        elif action == "press" and keys:
            pyautogui.hotkey(*keys.split("+"))
        else:
            return False
        return True
    except Exception:
        return False
