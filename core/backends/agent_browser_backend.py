"""core/backends/agent_browser_backend.py — agent-browser CLI backend.

Drives the browser through the real ``agent-browser`` CLI
(https://github.com/vercel-labs/agent-browser, ``npm i -g agent-browser``)
as a CLI-subcommand adapter. agent-browser runs its own daemon + managed
browser; every backend method maps to documented CLI subcommands:

  tabs        ``agent-browser tab`` / ``tab new [url]`` / ``tab <id>`` /
              ``tab close [id]``
  input       ``mouse move x y --human --seed N`` + ``mouse down/up``,
              ``keyboard type <text>``, ``press <key>``, ``mouse wheel dy dx``
  navigation  ``open`` (launch) / ``open <url>`` (launch + navigate)
  reading     ``eval <js>``, ``screenshot <path>``, ``cookies``

The browser is agent-browser's own managed Chromium (or the ``--engine``
it was configured with: chrome / lightpanda / obscura) — this backend
never launches stock Playwright Chromium. ``start()`` raises when the
``agent-browser`` binary is missing; there is no silent fallback to
another engine (the router raises for explicit preferences).

Each backend instance gets an isolated ``--session`` id so parallel
backends do not share the daemon's active tab.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import shutil
import subprocess
import tempfile
import uuid
from typing import Any, Dict, List, Optional

from core.interfaces import BaseBrowserBackend
from core.types import BrowserBackendType, PageModel

logger = logging.getLogger(__name__)

_START_TIMEOUT_S = 120.0
_CMD_TIMEOUT_S = 60.0


class AgentBrowserBackend(BaseBrowserBackend):
    """Adapter driving agent-browser's managed browser via its CLI."""

    def __init__(self, headless: bool = True):
        self.headless = headless
        self._session_id = f"muse-{uuid.uuid4().hex[:12]}"
        self._namespace = f"muse-{uuid.uuid4().hex[:8]}"
        self._rng = random.Random()
        self._started = False
        self._current_tab: Optional[str] = None
        self._known_tabs: Dict[str, Dict[str, Any]] = {}

    # ------------------------------------------------------------------
    # Engine identity
    # ------------------------------------------------------------------

    @property
    def backend_type(self) -> BrowserBackendType:
        return BrowserBackendType.AGENT_BROWSER

    @staticmethod
    def _binary() -> Optional[str]:
        """Locate the real agent-browser CLI (matches tools/installer.py)."""
        explicit = os.environ.get("AGENT_BROWSER_BIN")
        if explicit and os.path.isfile(explicit):
            return explicit
        return shutil.which("agent-browser")

    async def is_available(self) -> bool:
        # Detection only — never installs anything.
        return self._binary() is not None

    async def is_connected(self) -> bool:
        return self._started

    # ------------------------------------------------------------------
    # CLI plumbing
    # ------------------------------------------------------------------

    def _base_cmd(self, as_json: bool = True) -> List[str]:
        binary = self._binary()
        if not binary:
            raise RuntimeError(
                "agent-browser CLI not found. Install it with "
                "`npm install -g agent-browser` (see tools/installer.py)."
            )
        cmd = [
            binary,
            "--session", self._session_id,
            "--namespace", self._namespace,
        ]
        if as_json:
            cmd.append("--json")
        return cmd

    def _run_sync(
        self,
        *args: str,
        timeout: float = _CMD_TIMEOUT_S,
        as_json: bool = True,
    ) -> Any:
        cmd = self._base_cmd(as_json=as_json) + list(args)
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=timeout
            )
        except FileNotFoundError as exc:
            raise RuntimeError("agent-browser CLI disappeared from PATH") from exc
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(
                f"agent-browser command timed out: {' '.join(args)}"
            ) from exc
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or "").strip()[:500]
            raise RuntimeError(
                f"agent-browser {' '.join(args)} failed: {err}"
            )
        out = (proc.stdout or "").strip()
        if as_json and out:
            try:
                return json.loads(out)
            except json.JSONDecodeError:
                logger.debug("agent-browser non-JSON output: %r", out[:200])
                return out
        return out

    async def _cli(self, *args: str, timeout: float = _CMD_TIMEOUT_S,
                   as_json: bool = True) -> Any:
        return await asyncio.to_thread(
            self._run_sync, *args, timeout=timeout, as_json=as_json
        )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Launch agent-browser's managed browser (raises if CLI missing)."""
        if self._started:
            return
        if not await self.is_available():
            raise RuntimeError(
                "agent-browser CLI is not installed "
                "(`npm install -g agent-browser`). No fallback engine is used."
            )
        args = ["open"]
        if not self.headless:
            args.append("--headed")
        await self._cli(*args, timeout=_START_TIMEOUT_S)
        self._started = True
        # Prime tab bookkeeping.
        try:
            await self.list_tabs()
        except Exception as exc:
            logger.debug("agent-browser tab listing after start failed: %s", exc)

    async def stop(self) -> None:
        if not self._started:
            return
        try:
            await self._cli("close", as_json=False)
        except Exception as exc:
            logger.debug("agent-browser close failed: %s", exc)
        finally:
            self._started = False
            self._current_tab = None
            self._known_tabs.clear()

    # ------------------------------------------------------------------
    # Tabs (agent-browser acts on the session's active tab)
    # ------------------------------------------------------------------

    @staticmethod
    def _normalize_tabs(raw: Any) -> List[Dict[str, Any]]:
        if isinstance(raw, dict):
            raw = raw.get("tabs", raw.get("data", []))
        if isinstance(raw, str):
            return []
        tabs: List[Dict[str, Any]] = []
        for t in raw or []:
            if not isinstance(t, dict):
                continue
            tid = str(t.get("tabId") or t.get("id") or "")
            if not tid:
                continue
            tabs.append({
                "id": tid,
                "tabId": tid,
                "title": t.get("title", ""),
                "url": t.get("url", ""),
                "active": bool(t.get("active", False)),
            })
        return tabs

    async def list_tabs(self) -> List[Dict[str, Any]]:
        raw = await self._cli("tab")
        tabs = self._normalize_tabs(raw)
        self._known_tabs = {t["tabId"]: t for t in tabs}
        active = next((t for t in tabs if t["active"]), None)
        if active:
            self._current_tab = active["tabId"]
        elif tabs and self._current_tab not in self._known_tabs:
            self._current_tab = tabs[0]["tabId"]
        return tabs

    async def _select(self, tab_id: str) -> None:
        """Make tab_id the session's active tab (no-op if already active)."""
        if tab_id and tab_id != self._current_tab:
            await self._cli("tab", tab_id, as_json=False)
            self._current_tab = tab_id

    async def create_tab(self, url: str = "about:blank") -> str:
        args = ["tab", "new"]
        if url and url != "about:blank":
            args.append(url)
        raw = await self._cli(*args)
        tid: Optional[str] = None
        if isinstance(raw, dict):
            tid = raw.get("tabId") or raw.get("id")
            if isinstance(tid, dict):
                tid = None
        if not tid:
            # Fall back to re-listing; newest tab wins.
            tabs = await self.list_tabs()
            tid = tabs[-1]["tabId"] if tabs else None
        if not tid:
            raise RuntimeError(f"agent-browser tab new returned no tab id: {raw!r}")
        tid = str(tid)
        self._current_tab = tid
        return tid

    async def switch_tab(self, tab_id: str, focus: bool = False) -> None:
        await self._select(tab_id)

    async def close_tab(self, tab_id: str) -> None:
        await self._cli("tab", "close", tab_id, as_json=False)
        self._known_tabs.pop(tab_id, None)
        if self._current_tab == tab_id:
            self._current_tab = None

    # ------------------------------------------------------------------
    # Navigation & page state
    # ------------------------------------------------------------------

    async def navigate(self, tab_id: str, url: str, wait_until: str = "load") -> bool:
        await self._select(tab_id)
        await self._cli("open", url)
        return True

    @staticmethod
    def _unwrap_eval(raw: Any) -> Any:
        if isinstance(raw, dict):
            for key in ("result", "value", "data"):
                if key in raw:
                    return raw[key]
        return raw

    async def get_title(self, tab_id: str) -> str:
        await self._select(tab_id)
        return str(self._unwrap_eval(await self._cli("eval", "document.title")) or "")

    async def get_url(self, tab_id: str) -> str:
        await self._select(tab_id)
        return str(self._unwrap_eval(await self._cli("eval", "location.href")) or "")

    async def evaluate(self, tab_id: str, expression: str) -> Any:
        await self._select(tab_id)
        return self._unwrap_eval(await self._cli("eval", expression))

    # ------------------------------------------------------------------
    # Input (coordinate mouse + real keystrokes through the CLI)
    # ------------------------------------------------------------------

    async def click(self, tab_id: str, x: int, y: int) -> bool:
        await self._select(tab_id)
        seed = self._rng.randint(0, 2**31 - 1)
        await self._cli(
            "mouse", "move", str(int(x)), str(int(y)),
            "--human", "--seed", str(seed), as_json=False,
        )
        await self._cli("mouse", "down", "left", as_json=False)
        await self._cli("mouse", "up", "left", as_json=False)
        return True

    async def type_text(self, tab_id: str, text: str) -> bool:
        await self._select(tab_id)
        await self._cli("keyboard", "type", text, as_json=False)
        return True

    async def press_key(self, tab_id: str, key: str) -> bool:
        await self._select(tab_id)
        await self._cli("press", key, as_json=False)
        return True

    async def scroll(self, tab_id: str, delta_x: int = 0, delta_y: int = 400) -> bool:
        await self._select(tab_id)
        args = ["mouse", "wheel", str(int(delta_y))]
        if delta_x:
            args.append(str(int(delta_x)))
        await self._cli(*args, as_json=False)
        return True

    # ------------------------------------------------------------------
    # Observation
    # ------------------------------------------------------------------

    async def screenshot(self, tab_id: str, full_page: bool = False) -> bytes:
        await self._select(tab_id)
        fd, path = tempfile.mkstemp(suffix=".png", prefix="agent_browser_")
        os.close(fd)
        try:
            args = ["screenshot", path]
            if full_page:
                args.append("--full")
            await self._cli(*args, as_json=False)
            with open(path, "rb") as f:
                return f.read()
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass

    async def build_page_model(self, tab_id: str) -> PageModel:
        # Primary: the shared PageModeler over evaluate(); snapshot text
        # is kept as a summary fallback when the modeler fails.
        try:
            from core.page_model import PageModeler
            return await PageModeler.build(self, tab_id)
        except Exception:
            pass
        snap = await self._select_and_snapshot(tab_id)
        url = await self.get_url(tab_id)
        title = await self.get_title(tab_id)
        return PageModel(
            url=url,
            title=title,
            interactive_elements=[],
            buttons=[],
            inputs=[],
            links=[],
            summary=f"agent-browser snapshot ({len(snap)} chars)",
        )

    async def _select_and_snapshot(self, tab_id: str) -> str:
        await self._select(tab_id)
        raw = await self._cli("snapshot", as_json=False)
        return raw if isinstance(raw, str) else json.dumps(raw)

    # ------------------------------------------------------------------
    # Cookies
    # ------------------------------------------------------------------

    async def get_cookies(self, tab_id: str) -> List[Dict[str, Any]]:
        await self._select(tab_id)
        raw = await self._cli("cookies")
        if isinstance(raw, dict):
            raw = raw.get("cookies", raw.get("data", []))
        return list(raw) if isinstance(raw, list) else []

    async def set_cookies(self, tab_id: str, cookies: List[Dict[str, Any]]) -> bool:
        await self._select(tab_id)
        ok = True
        for c in cookies:
            name, value = c.get("name"), c.get("value")
            if not name:
                ok = False
                continue
            try:
                # CLI supports name/value (+ optional domain via --domain);
                # path/secure/httpOnly extras are not settable this way.
                args = ["cookies", "set", str(name), str(value or "")]
                if c.get("domain"):
                    args += ["--domain", str(c["domain"])]
                await self._cli(*args, as_json=False)
            except RuntimeError as exc:
                logger.warning("agent-browser cookies set failed for %r: %s", name, exc)
                ok = False
        return ok
