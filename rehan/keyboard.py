"""Keyboard-first action shortcuts.

Research (DEEP_RESEARCH_ROUND2.md): hunting a Save button with vision
costs 1-3s; pressing Ctrl+S costs ~50ms and never misses. This module
maps common intent -> keyboard shortcut, tried BEFORE any visual hunt.

The shortcuts are standard Windows conventions; per-app overrides can
be registered for apps that differ.
"""

from typing import Dict, List, Optional, Tuple

# intent -> list of (keys, description), tried in order.
# Keys use "+"-joined form for pyautogui.hotkey / CDP press.
SHORTCUTS: Dict[str, List[Tuple[str, str]]] = {
    "save": [("ctrl+s", "Save")],
    "save_as": [("ctrl+shift+s", "Save As")],
    "close": [("alt+f4", "Close window"), ("ctrl+w", "Close tab")],
    "copy": [("ctrl+c", "Copy")],
    "paste": [("ctrl+v", "Paste")],
    "cut": [("ctrl+x", "Cut")],
    "undo": [("ctrl+z", "Undo")],
    "redo": [("ctrl+y", "Redo")],
    "select_all": [("ctrl+a", "Select all")],
    "find": [("ctrl+f", "Find")],
    "new": [("ctrl+n", "New")],
    "open": [("ctrl+o", "Open")],
    "print": [("ctrl+p", "Print")],
    "refresh": [("f5", "Refresh"), ("ctrl+r", "Refresh")],
    "back": [("alt+left", "Back")],
    "forward": [("alt+right", "Forward")],
    "tab_new": [("ctrl+t", "New tab")],
    "tab_close": [("ctrl+w", "Close tab")],
    "tab_next": [("ctrl+tab", "Next tab")],
    "tab_prev": [("ctrl+shift+tab", "Previous tab")],
    "zoom_in": [("ctrl+plus", "Zoom in")],
    "zoom_out": [("ctrl+minus", "Zoom out")],
    "zoom_reset": [("ctrl+0", "Zoom reset")],
    "fullscreen": [("f11", "Fullscreen")],
    "escape": [("esc", "Escape / cancel")],
    "enter": [("enter", "Confirm")],
    "delete": [("del", "Delete")],
}

# Per-app overrides: app_substring -> {intent: [(keys, desc), ...]}.
# Registered at runtime; empty by default.
_APP_OVERRIDES: Dict[str, Dict[str, List[Tuple[str, str]]]] = {}


def register_app_shortcuts(app_substring: str,
                           mapping: Dict[str, List[Tuple[str, str]]]) -> None:
    """Register per-app shortcut overrides (e.g. Photoshop's Ctrl+Shift+S)."""
    _APP_OVERRIDES[app_substring.strip().lower()] = mapping


def shortcuts_for(intent: str, app: Optional[str] = None) -> List[Tuple[str, str]]:
    """Shortcut candidates for an intent, app overrides first."""
    out: List[Tuple[str, str]] = []
    if app:
        low = app.strip().lower()
        for sub, mapping in _APP_OVERRIDES.items():
            if sub in low and intent in mapping:
                out.extend(mapping[intent])
    out.extend(SHORTCUTS.get(intent, []))
    return out


# Intent detection: does this task look like a keyboard-shortcut action?
# Returns the intent name or None.
_INTENT_PATTERNS: List[Tuple[str, List[str]]] = [
    ("save", ["save the", "save file", "click save", "press save", "save document"]),
    ("close", ["close the window", "close window", "close the app"]),
    ("copy", ["copy the", "copy text"]),
    ("paste", ["paste the", "paste text"]),
    ("undo", ["undo the", "undo last"]),
    ("select_all", ["select all"]),
    ("find", ["find in page", "search in page", "open find"]),
    ("refresh", ["refresh the page", "reload the page"]),
    ("new", ["new file", "new document"]),
    ("print", ["print the", "print document"]),
    ("escape", ["press escape", "hit escape", "cancel the dialog"]),
]


def detect_intent(task_text: str) -> Optional[str]:
    """Map a natural-language task to a keyboard shortcut intent."""
    low = (task_text or "").lower()
    for intent, patterns in _INTENT_PATTERNS:
        if any(p in low for p in patterns):
            return intent
    return None
