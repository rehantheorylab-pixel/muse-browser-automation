"""core/backends/csi_backend.py — CSI (ximing/csi) dedicated backend for Muse 3.0.

Drives the user's REAL Chrome through the ximing/csi local daemon instead of a
browser the agent launches itself. Useful when the task needs the user's live
sessions, cookies, and extensions.

Endpoint contract (grounded in the upstream ximing/csi skill docs —
https://github.com/ximing/csi, skills/csi/references/*.md and README.en.md,
reviewed 2026-10-09):

  - The daemon is a local Go HTTP/WebSocket service, default ``127.0.0.1:10088``.
  - Every tool call is ``POST /command`` with a JSON body of the form::

        {"action": "<tool>", "args": {...}, "session": "<session>"}

    ``session`` is a TOP-LEVEL field (not inside ``args``); tools act on the
    session's current tab — the daemon refuses bare Chrome tabIds.
  - Auth: an ``Authorization`` header is required when the daemon has API-key
    auth enabled (Rehan's daemon runs with auth on; loopback-only by design).
  - Tool set used here comes from the upstream 21-tool index: ``list_tabs``,
    ``find_tab``, ``close_tab``, ``navigate``, ``snapshot``, ``evaluate``,
    ``click``, ``mouse_click``, ``key_type``, ``send_keys``, ``scroll``,
    ``screenshot``, ``cdp``.

ASSUMPTIONS (marked A1..A6 — verify against the live daemon and correct if
behaviour differs; nothing below is invented silently):

  A1. ``mouse_click`` takes ``{"x": int, "y": int}`` coordinates. The upstream
      interaction doc only confirms it is coordinate-level
      (``Input.dispatchMouseEvent``); the exact arg names are assumed.
  A2. ``screenshot`` returns base64 PNG under one of the keys ``data`` /
      ``image`` / ``screenshot`` / ``png``. Parsed robustly across all four.
  A3. Cookies go through the ``cdp`` passthrough tool: ``Network.getAllCookies``
      -> ``{"cookies": [...]}`` and one ``Network.setCookie`` per cookie. CSI
      documents ``cdp`` as raw CDP passthrough returning a JSON object;
      ``Network.getAllCookies``/``Network.setCookie`` are standard CDP.
  A4. Authorization header scheme defaults to ``Bearer`` (override with
      ``CSI_AUTH_SCHEME``). CSI_API_KEY supplies the key.
  A5. ``switch_tab`` is emulated via ``find_tab`` by the tab's URL (taken from
      ``list_tabs``), because CSI exposes no direct "target this tabId" tool.
      Tabs sharing an identical URL in one session may resolve ambiguously.
  A6. ``is_available()`` is a plain TCP connect probe (no auth needed).
      ``start()`` then performs a real ``list_tabs`` round trip so auth and
      extension-connection problems surface as actionable errors.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import socket
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

from core.interfaces import BaseBrowserBackend
from core.types import BrowserBackendType, PageModel

logger = logging.getLogger(__name__)

DEFAULT_CSI_HOST = "127.0.0.1"
DEFAULT_CSI_PORT = 10088
DEFAULT_CSI_SESSION = "muse-v2"
DEFAULT_AUTH_SCHEME = "Bearer"

# Keys that may carry the base64 PNG in a CSI screenshot response (A2).
_SCREENSHOT_KEYS = ("data", "image", "screenshot", "png")


class CSIBackend(BaseBrowserBackend):
    """Adapter driving real Chrome via the externally managed CSI daemon.

    The daemon is never launched or stopped by this backend: ``start()``
    verifies connectivity (raising an actionable error when CSI is down,
    auth fails, or the Chrome extension is disconnected) and ``stop()`` is a
    deliberate no-op.
    """

    def __init__(
        self,
        host: Optional[str] = None,
        port: Optional[int] = None,
        api_key: Optional[str] = None,
        session: Optional[str] = None,
        auth_scheme: Optional[str] = None,
        timeout_s: float = 60.0,
    ) -> None:
        self.host = host or os.environ.get("CSI_HOST", DEFAULT_CSI_HOST)
        self.port = int(port or os.environ.get("CSI_PORT", DEFAULT_CSI_PORT))
        self.api_key = api_key if api_key is not None else os.environ.get("CSI_API_KEY")
        self.session = session or os.environ.get("CSI_SESSION", DEFAULT_CSI_SESSION)
        self.auth_scheme = auth_scheme or os.environ.get("CSI_AUTH_SCHEME", DEFAULT_AUTH_SCHEME)
        self.timeout_s = timeout_s
        self._tabs: Dict[str, Dict[str, Any]] = {}
        self._current_tab_id: Optional[str] = None
        self._connected = False

    @property
    def backend_type(self) -> BrowserBackendType:
        return BrowserBackendType.CSI

    @property
    def command_url(self) -> str:
        return f"http://{self.host}:{self.port}/command"

    # ------------------------------------------------------------------
    # Transport
    # ------------------------------------------------------------------
    def _build_request(self, action: str, args: Optional[Dict[str, Any]]) -> urllib.request.Request:
        body = {"action": action, "args": args or {}, "session": self.session}
        req = urllib.request.Request(
            self.command_url,
            data=json.dumps(body).encode("utf-8"),
            method="POST",
        )
        req.add_header("Content-Type", "application/json")
        if self.api_key:
            req.add_header("Authorization", f"{self.auth_scheme} {self.api_key}")  # A4
        return req

    def _post_sync(
        self,
        action: str,
        args: Optional[Dict[str, Any]] = None,
        timeout: Optional[float] = None,
    ) -> Any:
        req = self._build_request(action, args)
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout_s) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
            if exc.code in (401, 403):
                raise RuntimeError(
                    f"CSI daemon rejected authentication (HTTP {exc.code}). "
                    "Set CSI_API_KEY to the key stored in the CSI config "
                    "(~/.csi/config.json on the machine running Chrome)."
                ) from exc
            raise RuntimeError(f"CSI daemon HTTP {exc.code} on '{action}': {detail}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(
                f"CSI daemon unreachable at {self.host}:{self.port}: {exc.reason}"
            ) from exc
        except OSError as exc:
            raise RuntimeError(
                f"CSI daemon unreachable at {self.host}:{self.port}: {exc}"
            ) from exc
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise RuntimeError(f"CSI daemon returned non-JSON on '{action}': {raw[:200]!r}") from exc
        if isinstance(payload, dict) and payload.get("success") is False:
            code = payload.get("code", "")
            msg = payload.get("error") or payload.get("message") or json.dumps(payload)[:500]
            raise RuntimeError(f"CSI action '{action}' failed [{code}]: {msg}")
        return payload

    async def _post(
        self,
        action: str,
        args: Optional[Dict[str, Any]] = None,
        timeout: Optional[float] = None,
    ) -> Any:
        return await asyncio.to_thread(self._post_sync, action, args, timeout)

    def _tcp_probe(self) -> bool:
        """A6: availability is a short TCP connect probe — no auth required."""
        try:
            with socket.create_connection((self.host, self.port), timeout=1.0):
                return True
        except OSError:
            return False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    async def is_available(self) -> bool:
        return await asyncio.to_thread(self._tcp_probe)

    async def is_connected(self) -> bool:
        return self._connected and await asyncio.to_thread(self._tcp_probe)

    async def start(self) -> None:
        """Verify the externally managed CSI daemon; never launches it."""
        if self._connected and await asyncio.to_thread(self._tcp_probe):
            return
        if not await asyncio.to_thread(self._tcp_probe):
            raise RuntimeError(
                f"CSI daemon is not running on {self.host}:{self.port}. "
                "CSI is externally managed: start it first (e.g. `csi start` on the "
                "machine running Chrome), then retry. This backend never launches "
                "CSI itself."
            )
        try:
            await self._refresh_tabs()
        except RuntimeError as exc:
            msg = str(exc).lower()
            if "not connected" in msg or "extension" in msg:
                raise RuntimeError(
                    "CSI daemon is up but its Chrome extension is not connected. "
                    "Open Chrome with the CSI extension installed and connected, "
                    "then retry."
                ) from exc
            raise
        self._connected = True
        logger.info("CSIBackend connected to CSI daemon at %s:%s", self.host, self.port)

    async def stop(self) -> None:
        # Deliberate no-op: CSI is externally managed by the user.
        logger.info("CSIBackend.stop(): CSI daemon is externally managed; leaving it running.")
        self._connected = False

    # ------------------------------------------------------------------
    # Tab bookkeeping (CSI targets a session's "current tab"; A5)
    # ------------------------------------------------------------------
    async def _refresh_tabs(self) -> None:
        result = await self._post("list_tabs", {})
        tabs = result.get("tabs", []) if isinstance(result, dict) else []
        self._tabs = {
            t["tabId"]: {
                "url": t.get("url", ""),
                "title": t.get("title", ""),
                "active": bool(t.get("active")),
            }
            for t in tabs
            if isinstance(t, dict) and t.get("tabId")
        }
        current: Optional[str] = None
        if isinstance(result, dict):
            target = result.get("currentTarget")
            if isinstance(target, dict) and target.get("tabId") in self._tabs:
                current = target["tabId"]
        if current is None:
            active = [tid for tid, t in self._tabs.items() if t.get("active")]
            current = active[0] if active else next(iter(self._tabs), None)
        self._current_tab_id = current

    async def _ensure_current(self, tab_id: str) -> None:
        """Make CSI's session target the given tab (A5: via find_tab by URL)."""
        if tab_id == self._current_tab_id and tab_id in self._tabs:
            return
        await self._refresh_tabs()
        info = self._tabs.get(tab_id)
        if info is None:
            raise RuntimeError(f"Unknown CSI tab '{tab_id}'; call list_tabs() for live ids.")
        if tab_id == self._current_tab_id:
            return
        url = info.get("url")
        if not url:
            raise RuntimeError(f"CSI tab '{tab_id}' has no known URL to re-target.")
        result = await self._post("find_tab", {"url": url})
        if isinstance(result, dict) and result.get("success") is False:
            raise RuntimeError(f"CSI could not re-target tab '{tab_id}': {result!r}")
        self._current_tab_id = tab_id

    # ------------------------------------------------------------------
    # Tabs
    # ------------------------------------------------------------------
    async def list_tabs(self) -> List[Dict[str, Any]]:
        await self._refresh_tabs()
        return [
            {
                "id": tid,
                "tabId": tid,
                "title": t.get("title", ""),
                "url": t.get("url", ""),
                "active": tid == self._current_tab_id,
            }
            for tid, t in self._tabs.items()
        ]

    async def create_tab(self, url: str = "about:blank") -> str:
        result = await self._post("navigate", {"url": url, "newTab": True})
        tab_id = result.get("tabId") if isinstance(result, dict) else None
        if not tab_id:
            raise RuntimeError(f"CSI navigate did not return a tabId: {result!r}")
        self._tabs[tab_id] = {"url": url, "title": "", "active": True}
        self._current_tab_id = tab_id
        return tab_id

    async def switch_tab(self, tab_id: str, focus: bool = False) -> None:
        if focus:
            logger.debug("CSIBackend.switch_tab(focus=True): CSI cannot bring windows "
                         "to the foreground; focus is best-effort and ignored.")
        await self._ensure_current(tab_id)

    async def close_tab(self, tab_id: str) -> None:
        await self._ensure_current(tab_id)
        await self._post("close_tab", {})
        self._tabs.pop(tab_id, None)
        self._current_tab_id = None
        try:
            await self._refresh_tabs()
        except RuntimeError:
            self._tabs = {}
            self._current_tab_id = None

    # ------------------------------------------------------------------
    # Navigation & page state
    # ------------------------------------------------------------------
    async def navigate(self, tab_id: str, url: str, wait_until: str = "load") -> bool:
        await self._ensure_current(tab_id)
        # CSI navigate waits for page load itself (30 s daemon timeout).
        result = await self._post("navigate", {"url": url})
        if tab_id in self._tabs:
            self._tabs[tab_id]["url"] = url
        return bool(result.get("success", True)) if isinstance(result, dict) else True

    async def get_title(self, tab_id: str) -> str:
        return str(await self.evaluate(tab_id, "document.title"))

    async def get_url(self, tab_id: str) -> str:
        return str(await self.evaluate(tab_id, "window.location.href"))

    async def evaluate(self, tab_id: str, expression: str) -> Any:
        await self._ensure_current(tab_id)
        result = await self._post("evaluate", {"code": expression})
        if isinstance(result, dict) and "value" in result:
            return result["value"]
        return result

    # ------------------------------------------------------------------
    # Input
    # ------------------------------------------------------------------
    async def click(self, tab_id: str, x: int, y: int) -> bool:
        await self._ensure_current(tab_id)
        # A1: coordinate-level click passes isTrusted checks.
        await self._post("mouse_click", {"x": int(x), "y": int(y)})
        return True

    async def type_text(self, tab_id: str, text: str) -> bool:
        await self._ensure_current(tab_id)
        await self._post("key_type", {"text": text})
        return True

    async def press_key(self, tab_id: str, key: str) -> bool:
        await self._ensure_current(tab_id)
        # A1-adjacent: CSI send_keys uses names like "Enter", "Escape", "Tab",
        # "Backspace", arrows, single letters/digits (per upstream interaction doc).
        await self._post("send_keys", {"keys": key})
        return True

    async def scroll(self, tab_id: str, delta_x: int = 0, delta_y: int = 400) -> bool:
        await self._ensure_current(tab_id)
        # CSI scroll takes direction/amount; map the dominant axis (A1-adjacent).
        if abs(delta_y) >= abs(delta_x):
            args = {"direction": "down" if delta_y > 0 else "up", "amount": abs(int(delta_y))}
        else:
            args = {"direction": "right" if delta_x > 0 else "left", "amount": abs(int(delta_x))}
        await self._post("scroll", args)
        return True

    # ------------------------------------------------------------------
    # Observation & extraction
    # ------------------------------------------------------------------
    async def screenshot(self, tab_id: str, full_page: bool = False) -> bytes:
        await self._ensure_current(tab_id)
        if full_page:
            logger.debug("CSIBackend.screenshot(full_page=True): CSI captures the "
                         "viewport only; full_page is best-effort and ignored.")
        result = await self._post("screenshot", {})
        b64: Optional[str] = None
        if isinstance(result, dict):
            for key in _SCREENSHOT_KEYS:  # A2
                val = result.get(key)
                if isinstance(val, str) and val:
                    b64 = val
                    break
        if not b64:
            raise RuntimeError(f"CSI screenshot returned no base64 image: {result!r}")
        return base64.b64decode(b64)

    async def build_page_model(self, tab_id: str) -> PageModel:
        from core.page_model import PageModeler
        return await PageModeler.build(self, tab_id)

    # ------------------------------------------------------------------
    # Session & state (via raw CDP passthrough; A3)
    # ------------------------------------------------------------------
    async def get_cookies(self, tab_id: str) -> List[Dict[str, Any]]:
        await self._ensure_current(tab_id)
        result = await self._post("cdp", {"method": "Network.getAllCookies", "params": {}})
        data = result.get("data") if isinstance(result, dict) else None
        cookies = data.get("cookies") if isinstance(data, dict) else None
        return list(cookies) if isinstance(cookies, list) else []

    async def set_cookies(self, tab_id: str, cookies: List[Dict[str, Any]]) -> bool:
        await self._ensure_current(tab_id)
        ok = True
        for cookie in cookies:
            params: Dict[str, Any] = {
                "name": cookie.get("name"),
                "value": cookie.get("value"),
            }
            for key in ("url", "domain", "path", "secure", "httpOnly", "sameSite", "expires"):
                if cookie.get(key) is not None:
                    params[key] = cookie[key]
            try:
                await self._post("cdp", {"method": "Network.setCookie", "params": params})
            except RuntimeError as exc:
                logger.warning("CSIBackend.set_cookies: failed for %r: %s", cookie.get("name"), exc)
                ok = False
        return ok
