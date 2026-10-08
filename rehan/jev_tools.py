"""Shared jev decision core adapters: "one brain, three hands" (blueprint §6.6).

The decision + validation logic lives in rehan/jev.py (decide_json,
validate_decision, format_history). Each tool provides:
  observe() -> rows   (indexed action rows, jev schema)
  execute(row, op, text) -> None  (tool-native input)

This module adapts the three observers' rows into the row shape
rehan/jev.py's executor expects:
  {"label", "role", "marker", "kind", "element", "option"}
and provides tool-native executors. The desktop (UIA) path needs no
adapter — run_jev_steps already speaks element dicts.

Row schema from tools (extension page.snapshot_indexed,
ObscuraPage.snapshot_indexed):
  {"id", "role", "label", "kind", "value", "rect", "node", "text?"}
kind ∈ {"click", "fill", "select"}; rect = {x, y, w, h} center pixels.
"""

from rehan.jev import _row_marker


def rows_from_browser(indexed):
    """Convert a tool's indexed snapshot to executor rows.

    indexed: dict with "rows" (list of {id,role,label,kind,value,rect,node})
             and optional "text" (visible page text for the prompt).
    Returns (rows, page_text). Row "element" is the original tool row;
    the executor reads coordinates from element["rect"].
    """
    out = []
    for r in (indexed or {}).get("rows") or []:
        kind = r.get("kind", "click")
        role = r.get("role", "element")
        out.append({
            "label": r.get("label", "") or role,
            "role": role,
            "marker": _row_marker(role),
            "kind": kind if kind in ("click", "fill", "select") else "click",
            "element": r,  # tool-native row; has rect/node/value
            "option": r.get("value") if kind == "select" else None,
        })
    return out, (indexed or {}).get("text", "")


def browser_element_table(indexed) -> str:
    """Render a tool's indexed snapshot as the decision table text."""
    from rehan.jev import element_table as _et
    rows, page_text = rows_from_browser(indexed)
    # Reuse the table renderer by faking element dicts with names.
    lines = ["[0] screen  (whole screen)"]
    for i, row in enumerate(rows, 1):
        lines.append(f"[{i}] {row['marker']} {row['role']}  {row['label']}")
    if page_text and page_text.strip():
        lines.append("")
        lines.append("Visible page text (context only, not clickable):")
        lines.append(page_text.strip()[:6000])
    return "\n".join(lines)


class BrowserExecutor:
    """Tool-native input for browser rows.

    click_fn(x, y), type_fn(x, y, text), key_fn(key), scroll_fn(x, y, delta)
    are tool-specific callables (extension daemon / Obscura CDP).
    Coordinates come from the row's rect — never from model output.
    """

    def __init__(self, click_fn, type_fn, key_fn=None, scroll_fn=None,
                 settle_click=0.05, settle_combo=0.20):
        self.click_fn = click_fn
        self.type_fn = type_fn
        self.key_fn = key_fn
        self.scroll_fn = scroll_fn
        self.settle_click = settle_click
        self.settle_combo = settle_combo

    def _xy(self, row):
        rect = (row.get("element") or {}).get("rect") or {}
        return int(rect.get("x", 0)), int(rect.get("y", 0))

    def execute(self, op, row, text=None):
        """Execute one decided action. Returns True on success."""
        import time
        x, y = self._xy(row)
        kind = row.get("kind", "click")
        if op == "CLICK":
            self.click_fn(x, y)
            time.sleep(self.settle_click)
        elif op == "TYPE_TEXT":
            if not text:
                raise ValueError("TYPE_TEXT needs text")
            self.type_fn(x, y, text)
            time.sleep(self.settle_click)
        elif op == "SELECT":
            opt = row.get("option")
            self.click_fn(x, y)
            time.sleep(self.settle_combo)
            if opt:
                # Type-to-filter + Enter (most comboboxes/selects).
                self.type_fn(x, y, opt)
                time.sleep(0.1)
                if self.key_fn:
                    self.key_fn("Enter")
                time.sleep(0.2)
        elif op in ("SCROLL_UP", "SCROLL_DOWN"):
            if self.scroll_fn:
                self.scroll_fn(x, y, 300 if op == "SCROLL_UP" else -300)
            else:
                self.click_fn(x, y)
        elif op == "WAIT":
            time.sleep(float(row.get("wait_secs", 1)))
        elif op == "PRESS_KEY" and self.key_fn:
            for k in (text or "").split("+"):
                self.key_fn(k.strip())
        else:
            raise ValueError(f"browser executor cannot do {op}")
        return True
