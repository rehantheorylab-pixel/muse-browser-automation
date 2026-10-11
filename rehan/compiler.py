"""Deterministic task compiler for s3rehan.

Parses narrow imperative task descriptions directly into agent.* action
sequences using regex — microseconds, zero LLM calls, zero tokens.

Covered patterns (each task part split on "then" / "," / "and then" / ";"):
- "open X"                       -> agent.open("X")
- "click/select the Y"            -> agent.click("the Y")
- "double-click the Y"            -> agent.click("the Y", num_clicks=2)
- "right-click the Y"             -> agent.click("the Y", button_type="right")
- "type 'hello'" / "type hello"   -> agent.type(text='hello')
- "press Enter" / "hit Ctrl+S"    -> agent.hotkey(['enter']) / (['ctrl','s'])
- "wait 2" / "wait 2 seconds"     -> agent.wait(2.0)
- "draw a circle ... drag ... on the canvas"
                                  -> agent.drag_across("the ... canvas")
                                  (+ shape-tool clicks when mentioned)

Returns a list of agent.* statements ending with agent.done(), or None when
any part is not parseable (caller falls back to the LLM planner).
"""

import re as _re


def compile_task(task_text):
    """Try to compile task to [agent.* statements]. None if not parseable."""
    t = task_text.strip()
    # Remove leading "--task" if present (from start /B quoting)
    t = _re.sub(r"^--task\s+", "", t)
    actions = []

    # Split on "then", ",", "and then", ";" — each part is an action.
    parts = _re.split(r"\s+then\s+|\s*,\s*|\s+and then\s+|;\s*", t, flags=_re.I)
    for part in parts:
        part = part.strip()
        # After a comma-split the next part can start with "then".
        part = _re.sub(r"^then\s+", "", part, flags=_re.I)
        if not part:
            continue
        # "Open X" -> agent.open("X"). Stops at " and " so
        # "open notepad and type hello" compiles the rest too.
        m = _re.match(r"open\s+(.+?)(?:\s+and\s+|\s*$)", part, _re.I)
        if m:
            app = m.group(1).strip().rstrip(".")
            # Clean up: "Paint 3D" not "Paint 3D, click the Create new button"
            app = _re.split(r",\s*", app)[0].strip()
            if app:
                actions.append(f'agent.open("{app}")')
                # Check if there's a "click Y" after the open in the same part
                rest = part[m.end():].strip()
                if rest:
                    part = rest
                else:
                    continue
        # "type 'hello'" / 'type "hello"' / "type hello" -> agent.type(text=...)
        # (before the click pattern: "press" is claimed by hotkey below)
        m = _re.match(r"""type\s+["'](.+?)["']\s*$""", part, _re.I)
        if not m:
            m = _re.match(r"type\s+([a-zA-Z0-9][\w\s\-\.\,]{0,80}?)\s*$", part, _re.I)
        if m:
            text = m.group(1).strip().rstrip(".")
            if text:
                actions.append(f"agent.type(text={text!r})")
                continue
        # "press Enter" / "hit Ctrl+S" -> agent.hotkey([...])
        m = _re.match(r"(?:press|hit)\s+([a-zA-Z0-9\+\s]+?)\s*$", part, _re.I)
        if m and not _re.search(r"\bthe\b", m.group(1), _re.I):
            keys = [k.strip().lower() for k in _re.split(r"\+|\s+", m.group(1)) if k.strip()]
            # Normalize common names.
            _aliases = {"escape": "esc", "del": "delete", "return": "enter", "windows": "win"}
            keys = [_aliases.get(k, k) for k in keys]
            if keys and all(_re.fullmatch(r"[a-z0-9]+", k) for k in keys):
                actions.append(f"agent.hotkey({keys!r})")
                continue
        # "wait 2" / "wait 2 seconds" -> agent.wait(2.0)
        m = _re.match(r"wait\s+(\d+(?:\.\d+)?)\s*(?:seconds?|secs?|s)?\s*$", part, _re.I)
        if m:
            actions.append(f"agent.wait({float(m.group(1))})")
            continue
        # "scroll down" / "scroll up 3 times" -> agent.scroll(...)
        m = _re.match(r"scroll\s+(up|down)(?:\s+(\d+)\s*(?:times?)?)?\s*$", part, _re.I)
        if m:
            direction = m.group(1).lower()
            clicks = int(m.group(2)) if m.group(2) else 3
            # Upstream scroll(): positive clicks = up, negative = down.
            clicks = clicks if direction == "up" else -clicks
            actions.append(f'agent.scroll("the screen", {clicks})')
            continue
        # "close the window" / "close notepad" -> Alt+F4
        # "close the tab" -> Ctrl+W (more precise than Alt+F4)
        m = _re.match(r"close\s+(?:the\s+)?(.+?)\s*$", part, _re.I)
        if m:
            target = m.group(1).lower()
            if "tab" in target:
                actions.append("agent.hotkey(['ctrl', 'w'])")
            else:
                actions.append("agent.hotkey(['alt', 'f4'])")
            continue
        # "select all" -> Ctrl+A (before the generic click/select pattern)
        if _re.match(r"select\s+all\s*$", part, _re.I):
            actions.append("agent.hotkey(['ctrl', 'a'])")
            continue
        # Common clipboard/edit hotkeys -> Ctrl+<key>
        _hotkey_words = {
            "copy": "c",
            "paste": "v",
            "cut": "x",
            "undo": "z",
            "redo": "y",
            "save": "s",
            "find": "f",
        }
        m = _re.match(r"(copy|paste|cut|undo|redo|save|find)\s*$", part, _re.I)
        if m:
            actions.append(f"agent.hotkey(['ctrl', '{_hotkey_words[m.group(1).lower()]}'])")
            continue
        # "double-click the X" / "right-click the X"
        m = _re.match(r"double[\-\s]?click\s+(?:the\s+)?(.+?)\s*$", part, _re.I)
        if m:
            target = m.group(1).strip().rstrip(".")
            if not target.lower().startswith("the "):
                target = "the " + target
            actions.append(f'agent.click("{target}", num_clicks=2)')
            continue
        m = _re.match(r"right[\-\s]?click\s+(?:the\s+)?(.+?)\s*$", part, _re.I)
        if m:
            target = m.group(1).strip().rstrip(".")
            if not target.lower().startswith("the "):
                target = "the " + target
            actions.append(f'agent.click("{target}", button_type="right")')
            continue
        # "click the Y" / "click Y" -> agent.click("the Y")
        # NOTE: "press" deliberately NOT here (hotkey above owns it).
        # The full description is kept ("the Create new button", not
        # "the Create new") — more precise for the grounding model.
        m = _re.match(r"(?:click|select)\s+(?:the\s+)?(.+?)\s*$", part, _re.I)
        if m:
            target = m.group(1).strip().rstrip(".")
            if not target.lower().startswith("the "):
                target = "the " + target
            actions.append(f'agent.click("{target}")')
            continue
        # "draw a circle" -> agent.drag_across("the canvas"). The word
        # "drag" is NOT required: "draw a circle" already means a drag
        # gesture on the canvas (run 20 regressed on this).
        if _re.search(r"draw\s+(?:a\s+)?circle", part, _re.I):
            # Find the canvas/target ("on the X" or "across the X").
            m2 = _re.search(r"(?:on|across)\s+(?:the\s+)?(.+?)(?:\s+area|\s*$|\.)", part, _re.I)
            canvas = "the canvas"
            if m2:
                c = m2.group(1).strip()
                if "canvas" in c.lower() or "paint" in c.lower():
                    canvas = "the main white Paint 3D drawing canvas"
            # Need to select the shape tool first if mentioned. Skip
            # duplicates: the tab may already have its own task part.
            if _re.search(r"circle\s+(?:shape\s+)?tool", part, _re.I):
                # Check if 2D shapes was already clicked (any variant)
                has_2d = any("2d shapes" in a.lower() for a in actions)
                for _stmt in (
                    'agent.click("the 2D shapes tab")',
                    'agent.click("the Circle shape tool")',
                ):
                    if "2d shapes" in _stmt.lower() and has_2d:
                        continue
                    if not actions or actions[-1] != _stmt:
                        actions.append(_stmt)
            actions.append(f'agent.drag_across("{canvas}")')
            continue
        # If we can't parse this part, bail out (fall back to planner)
        return None

    if not actions:
        return None
    # Drop consecutive duplicates (a tab clicked in its own part and again
    # by the draw handler is one click, not two).
    deduped = []
    for _a in actions:
        if not deduped or deduped[-1] != _a:
            deduped.append(_a)
    actions = deduped
    actions.append("agent.done()")
    return actions
