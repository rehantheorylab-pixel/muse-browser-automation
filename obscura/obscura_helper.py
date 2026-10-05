"""obscura_helper.py — clean CDP wrapper for the Obscura stealth browser.

Ports the proven pieces of the muse-browser-mcp Chrome extension into
Obscura's world:
  - the extension's SNAPSHOT_JS element-extraction (v1.1.4, return-wrapped
    form that proved reliable after the page.snapshot null saga)
  - the extension's KEYMAP for press_key
  - the 8-step CDP flow (browser endpoint -> createTarget -> attachToTarget
    flatten=True -> Page.enable -> sessionId on EVERY call)

Usage:
    import asyncio
    from obscura_helper import ObscuraPage

    async def main():
        async with ObscuraPage() as page:
            await page.navigate("https://example.com")
            snap = await page.snapshot()
            print(snap["title"], snap["count"])
            await page.screenshot("shot.png")

    asyncio.run(main())

Notes:
  - Expects Obscura already serving on 127.0.0.1:9222 (--stealth).
    This helper never auto-starts the daemon; call is_running() to check.
  - Obscura is NOT Chromium (independent Rust engine): heavy web apps may
    render/behave differently from real Chrome. Snapshots degrade gracefully
    (checkVisibility is feature-detected).
"""

import asyncio
import base64
import json
import urllib.request

import websockets

CDP_HOST = "127.0.0.1"
CDP_PORT = 9222

# Ported verbatim from muse-browser-mcp extension/background.js (v1.1.4).
# Fast on purpose: checkVisibility (native) instead of getComputedStyle per
# element, textContent (no layout) instead of innerText (forces reflow).
SNAPSHOT_JS = r'''function () {
  try {
    const out = [];
    const sel = 'a, button, input, select, textarea, [role="button"], [role="link"], [role="textbox"], [role="checkbox"], [role="switch"], [onclick], [tabindex]:not([tabindex="-1"])';
    let i = 0;
    for (const el of document.querySelectorAll(sel)) {
      try {
        const r = el.getBoundingClientRect();
        if (!r || r.width < 1 || r.height < 1) continue;
        if (r.bottom < 0 || r.top > window.innerHeight || r.right < 0 || r.left > window.innerWidth) continue;
        if (typeof el.checkVisibility === 'function') {
          if (!el.checkVisibility({ checkOpacity: true, checkVisibilityCSS: true })) continue;
        }
        const name = ((el.textContent || '') + ' ' + (el.value || '') + ' ' + (el.getAttribute('aria-label') || '') + ' ' + (el.title || '') + ' ' + (el.placeholder || '')).replace(/\s+/g, ' ').trim().slice(0, 140);
        out.push({ ref: 'e' + (i++), tag: el.tagName.toLowerCase(), type: el.type || '', role: el.getAttribute('role') || '', name, x: Math.round(r.x + r.width / 2), y: Math.round(r.y + r.height / 2), w: Math.round(r.width), h: Math.round(r.height) });
        if (out.length >= 400) break;
      } catch (_) {}
    }
    return { url: location.href, title: document.title, count: out.length, elements: out };
  } catch (e) { return { url: location.href, title: document.title, count: 0, elements: [], error: String(e) }; }
}'''

# Ported from the extension's KEYMAP.
KEYMAP = {
    "Enter": 13, "Tab": 9, "Escape": 27, "Backspace": 8, "Delete": 46,
    "ArrowLeft": 37, "ArrowUp": 38, "ArrowRight": 39, "ArrowDown": 40,
    "Home": 36, "End": 35, "PageUp": 33, "PageDown": 34,
}

# Indexed action snapshot (jev-blueprint §6.5): our own script emitting
# jev-style action rows {id, role, label, kind, value, rect, node} — the
# identical schema as the extension's page.snapshot_indexed, so the same
# shared decision core drives all three tools. Ported from the extension
# version; invoked via the same return-wrapped arrow-IIFE pattern as
# snapshot() because Obscura's Runtime.evaluate rejects bare
# anonymous function(){} statements.
INDEXED_SNAPSHOT_JS = r'''function () {
  try {
    const cache = window.__museIdx || (window.__museIdx = { ids: [], nodes: new Map(), next: 1 });
    const nodeId = (e) => {
      let id = cache.ids.indexOf(e);
      if (id < 0) { id = cache.next++; cache.ids.push(e); }
      cache.nodes.set(id, e); return id;
    };
    const ROLE_OF = (el) => {
      const t = (el.tagName || '').toLowerCase();
      const ty = (el.type || '').toLowerCase();
      const r = (el.getAttribute('role') || '').toLowerCase();
      if (r) return r;
      if (t === 'a') return 'link';
      if (t === 'button') return 'button';
      if (t === 'select') return 'combobox';
      if (t === 'textarea') return 'textbox';
      if (t === 'input') {
        if (ty === 'checkbox') return 'checkbox';
        if (ty === 'radio') return 'radiobutton';
        if (ty === 'submit' || ty === 'button') return 'button';
        if (ty === 'search') return 'searchbox';
        if (ty === 'number') return 'spinbutton';
        return 'textbox';
      }
      return 'element';
    };
    const NAME_OF = (el) => {
      const parts = [el.getAttribute('aria-label'), el.title, el.placeholder,
        (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA') ? '' : (el.textContent || ''),
        el.value || '', el.getAttribute('alt') || ''];
      return parts.filter(Boolean).join(' ').replace(/\s+/g, ' ').trim().slice(0, 80);
    };
    const rows = [];
    const sel = 'a, button, input, select, textarea, [role="button"], [role="link"], [role="textbox"], [role="checkbox"], [role="combobox"], [role="switch"], [onclick], [tabindex]:not([tabindex="-1"])';
    let n = 0;
    for (const el of document.querySelectorAll(sel)) {
      try {
        const r = el.getBoundingClientRect();
        if (!r || r.width < 1 || r.height < 1) continue;
        if (r.bottom < 0 || r.top > window.innerHeight || r.right < 0 || r.left > window.innerWidth) continue;
        if (typeof el.checkVisibility === 'function') {
          if (!el.checkVisibility({ checkVisibilityCSS: true })) continue;
        }
        const role = ROLE_OF(el);
        const label = NAME_OF(el) || role;
        const rect = { x: Math.round(r.x + r.width / 2), y: Math.round(r.y + r.height / 2), w: Math.round(r.width), h: Math.round(r.height) };
        const node = nodeId(el);
        const value = (el.value !== undefined && el.value !== null) ? String(el.value).slice(0, 120) : '';
        const isFill = ['textbox', 'searchbox', 'spinbutton'].indexOf(role) >= 0;
        const isSelect = role === 'combobox';
        if (isFill) {
          rows.push({ id: ++n, role, label, kind: 'fill', value, rect, node });
          rows.push({ id: ++n, role, label: 'Open ' + label, kind: 'click', value: '', rect, node });
        } else if (isSelect) {
          rows.push({ id: ++n, role, label, kind: 'fill', value, rect, node });
          const opts = Array.from(el.options || []).map(function(o){ return (o.text || '').trim(); }).filter(Boolean).slice(0, 40);
          for (const o of opts) rows.push({ id: ++n, role: 'option', label: label + ' -> ' + o, kind: 'select', value: o, rect, node });
          if (!opts.length) rows.push({ id: ++n, role, label: 'Open ' + label, kind: 'click', value: '', rect, node });
        } else {
          rows.push({ id: ++n, role, label, kind: 'click', value, rect, node });
        }
        if (n >= 250) break;
      } catch (_) {}
    }
    let text = '';
    try {
      const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
      const chunks = []; let total = 0; let tnode;
      while ((tnode = walker.nextNode()) && total < 6000) {
        const t = (tnode.nodeValue || '').replace(/\s+/g, ' ').trim();
        if (!t) continue;
        const pel = tnode.parentElement;
        if (!pel) continue;
        try { const pr = pel.getBoundingClientRect(); if (pr.bottom < 0 || pr.top > window.innerHeight) continue; } catch (_) { continue; }
        chunks.push(t); total += t.length + 1;
      }
      text = chunks.join(' ').slice(0, 6000);
    } catch (_) {}
    return { url: location.href, title: document.title, count: n, rows, text, worker: 'obscura-1.0' };
  } catch (e) { return { url: location.href, title: document.title, count: 0, rows: [], text: '', error: String(e) }; }
}'''


class ObscuraError(Exception):
    pass


def is_running(timeout=3):
    """True if Obscura's CDP endpoint answers on 127.0.0.1:9222."""
    try:
        urllib.request.urlopen(
            "http://%s:%d/json/version" % (CDP_HOST, CDP_PORT), timeout=timeout
        )
        return True
    except Exception:
        return False


def version_info(timeout=5):
    """ /json/version payload (browser, protocol, user agent)."""
    raw = urllib.request.urlopen(
        "http://%s:%d/json/version" % (CDP_HOST, CDP_PORT), timeout=timeout
    ).read().decode()
    return json.loads(raw)


class ObscuraPage:
    """One Obscura target, driven via CDP. Use as an async context manager."""

    def __init__(self, url="about:blank"):
        self._start_url = url
        self._ws = None
        self._msg_id = 0
        self._target_id = None
        self._session_id = None
        self._own_target = False

    def _find_page_target(self, timeout=5):
        """Return an existing page target dict from /json/list, or None.

        Connecting straight to a page's webSocketDebuggerUrl avoids
        Target.createTarget/attachToTarget entirely — more reliable on
        Obscura's partial CDP implementation.
        """
        try:
            raw = urllib.request.urlopen(
                "http://%s:%d/json/list" % (CDP_HOST, CDP_PORT),
                timeout=timeout,
            ).read().decode()
            targets = json.loads(raw)
        except Exception:
            return None
        if not isinstance(targets, list):
            return None
        for t in targets:
            if (isinstance(t, dict) and t.get("type") == "page"
                    and t.get("webSocketDebuggerUrl")):
                return t
        return None

    async def __aenter__(self):
        if not is_running():
            raise ObscuraError(
                "Obscura is not serving on %s:%d. Start it first, e.g. "
                'obscura.exe serve --host 127.0.0.1 --port 9222 --stealth '
                '--storage-dir <profile>' % (CDP_HOST, CDP_PORT)
            )
        last_err = None
        for attempt in range(3):
            try:
                # Path 1 (preferred): attach to an existing page target.
                target = self._find_page_target()
                if target:
                    self._ws = await websockets.connect(
                        target["webSocketDebuggerUrl"])
                    self._target_id = target.get("id")
                    self._session_id = None  # page socket: no session needed
                    self._own_target = False
                else:
                    # Path 2: browser endpoint + create/attach (classic flow).
                    self._ws = await websockets.connect(
                        "ws://%s:%d/devtools/browser" % (CDP_HOST, CDP_PORT)
                    )
                    r = await self._cdp("Target.createTarget",
                                        url=self._start_url)
                    self._target_id = r["result"]["targetId"]
                    # flatten=True is REQUIRED — without it there is no
                    # sessionId and every later call fails.
                    r = await self._cdp("Target.attachToTarget",
                                        targetId=self._target_id, flatten=True)
                    self._session_id = r["result"]["sessionId"]
                    self._own_target = True
                await self._cdp("Page.enable")
                await self._cdp("Runtime.enable")
                return self
            except Exception as e:
                last_err = e
                try:
                    if self._ws:
                        await self._ws.close()
                except Exception:
                    pass
                self._ws = None
                self._target_id = None
                self._session_id = None
                await asyncio.sleep(1 + attempt)
        raise ObscuraError(
            "could not establish Obscura CDP session after 3 attempts: %r"
            % (last_err,))

    async def __aexit__(self, *exc):
        try:
            # Only close targets we created — never kill a page the user
            # (or another tool) already had open.
            if self._target_id and self._own_target:
                await self._cdp("Target.closeTarget", targetId=self._target_id)
        except Exception:
            pass
        try:
            if self._ws:
                await self._ws.close()
        except Exception:
            pass
        self._ws = None
        self._own_target = False

    async def _cdp(self, method, **params):
        if self._ws is None:
            # This was the "'NoneType' object has no attribute 'send'"
            # failure: methods called without 'async with', or after a
            # failed __aenter__. Say so plainly instead of AttributeError.
            raise ObscuraError(
                "not connected to Obscura — use 'async with ObscuraPage():' "
                "before calling CDP methods")
        self._msg_id += 1
        req = {"id": self._msg_id, "method": method, "params": params}
        if self._session_id:
            req["sessionId"] = self._session_id
        await self._ws.send(json.dumps(req))
        while True:
            raw = await asyncio.wait_for(self._ws.recv(), timeout=30)
            resp = json.loads(raw)
            if "id" in resp and resp["id"] == self._msg_id:
                if "error" in resp:
                    raise ObscuraError("%s: %s" % (method, resp["error"]))
                return resp

    async def navigate(self, url, wait=3):
        """Navigate and wait a beat for the page to settle."""
        await self._cdp("Page.navigate", url=url)
        await asyncio.sleep(wait)
        return url

    async def evaluate(self, js, await_promise=True):
        """Run JS in the page. Wrapped as an arrow-function IIFE because
        Obscura's engine rejects anonymous `function(){}` statements."""
        wrapped = "(async()=>{%s})()" % js
        r = await self._cdp(
            "Runtime.evaluate",
            expression=wrapped,
            returnByValue=True,
            awaitPromise=await_promise,
        )
        res = r["result"]["result"]
        if res.get("type") == "object" and res.get("subtype") == "error":
            raise ObscuraError("JS error: %s" % res.get("description"))
        return res.get("value")

    async def snapshot(self):
        """Structured element list (ported SNAPSHOT_JS). Never returns null:
        on total failure it returns {count: 0, elements: [], error}."""
        wrapped = "(async()=>{return (%s)()})()" % SNAPSHOT_JS
        r = await self._cdp(
            "Runtime.evaluate",
            expression=wrapped,
            returnByValue=True,
            awaitPromise=True,
        )
        val = r["result"]["result"].get("value")
        if val is None:
            return {"url": "", "title": "", "count": 0, "elements": [],
                    "error": "snapshot evaluated to null"}
        return val

    async def snapshot_indexed(self):
        """jev-style indexed action rows (blueprint §6.5).

        Same schema as the Chrome extension's page.snapshot_indexed:
        {url, title, count, rows: [{id, role, label, kind, value, rect,
        node}], text}. The shared decision core in rehan/jev.py consumes
        all three tools' rows identically ("one brain, three hands").
        """
        wrapped = "(async()=>{return (%s)()})()" % INDEXED_SNAPSHOT_JS
        r = await self._cdp(
            "Runtime.evaluate",
            expression=wrapped,
            returnByValue=True,
            awaitPromise=True,
        )
        val = r["result"]["result"].get("value")
        if val is None:
            return {"url": "", "title": "", "count": 0, "rows": [],
                    "text": "", "error": "snapshot_indexed evaluated to null"}
        return val

    async def title(self):
        return await self.evaluate("return document.title")

    async def screenshot(self, path=None, full_page=False):
        """PNG bytes; optionally saved to path."""
        params = {"format": "png"}
        if full_page:
            params["captureBeyondViewport"] = True
        r = await self._cdp("Page.captureScreenshot", **params)
        data = base64.b64decode(r["result"]["data"])
        if path:
            with open(path, "wb") as f:
                f.write(data)
        return data

    async def click(self, x, y):
        await self._cdp("Input.dispatchMouseEvent",
                        type="mouseMoved", x=x, y=y)
        await self._cdp("Input.dispatchMouseEvent", type="mousePressed",
                        x=x, y=y, button="left", clickCount=1)
        await self._cdp("Input.dispatchMouseEvent", type="mouseReleased",
                        x=x, y=y, button="left", clickCount=1)
        return {"clicked": {"x": x, "y": y}}

    async def type_text(self, text):
        """Best-effort typing via keyDown text events (Obscura's CDP does not
        document Input.insertText, so we don't rely on it)."""
        for ch in text:
            await self._cdp("Input.dispatchKeyEvent", type="keyDown", text=ch)
        return {"typed": len(text)}

    async def press_key(self, key):
        """Named key (Enter, Tab, Escape, arrows...) or a single character."""
        if len(key) == 1:
            return await self.type_text(key)
        code = KEYMAP.get(key)
        if code is None:
            raise ObscuraError("unknown key: %r" % key)
        for t in ("keyDown", "keyUp"):
            await self._cdp("Input.dispatchKeyEvent", type=t,
                            windowsVirtualKeyCode=code)
        return {"pressed": key}

    async def scroll(self, x=0, y=0, dx=0, dy=300):
        await self._cdp("Input.dispatchMouseEvent",
                        type="mouseWheel", x=x, y=y, deltaX=dx, deltaY=dy)
        return {"scrolled": {"dx": dx, "dy": dy}}

    async def get_cookies(self):
        r = await self._cdp("Storage.getCookies")
        return r["result"]["cookies"]

    async def set_cookies(self, cookies):
        """cookies: list of {name, value, domain, path?, secure?, httpOnly?}."""
        for c in cookies:
            await self._cdp("Storage.setCookies", cookies=[c])
        return {"set": len(cookies)}

    async def clear_cookies(self):
        await self._cdp("Storage.clearCookies")
        return {"cleared": True}


async def quick_shot(url, out_path, wait=4):
    """One-shot: navigate, screenshot, close. Returns the page title."""
    async with ObscuraPage() as page:
        await page.navigate(url, wait=wait)
        title = await page.title()
        await page.screenshot(out_path)
        return title


if __name__ == "__main__":
    async def _demo():
        print("Obscura running:", is_running())
        print("Version:", version_info().get("Browser"))
        async with ObscuraPage() as page:
            await page.navigate("https://example.com", wait=3)
            snap = await page.snapshot()
            print("title:", snap.get("title"), "| elements:", snap.get("count"))
            for el in snap.get("elements", [])[:5]:
                print("  %(ref)s %(tag)s '%(name)s' @(%(x)d,%(y)d)" % el)
            await page.screenshot("obscura_helper_demo.png")
            print("screenshot saved: obscura_helper_demo.png")

    asyncio.run(_demo())
