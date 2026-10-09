"""core/backends/lightpanda_backend.py — Lightpanda headless-browser backend.

Lightpanda (lightpanda-io/browser) is an ultra-lightweight headless browser
written from scratch in Zig (V8 for JS), built for AI agents and scraping —
roughly 9x less memory and 11x faster than headless Chrome in the project's
benchmarks. Findings from research (2026-10-09):

  - Lightpanda exposes the Chrome DevTools Protocol: ``lightpanda serve
    --host 127.0.0.1 --port <port>`` starts a WebSocket-based CDP server, and
    the project documents compatibility with Playwright, Puppeteer, and
    chromedp through that endpoint.
  - Distribution is a single nightly binary (Linux/macOS; Docker available);
    nothing is downloaded or installed by this backend — ``is_available()``
    only detects the binary on PATH (``LIGHTPANDA_BIN`` env override).
  - License: AGPL-3.0. Per the standing repo-licensing rule (contact owner /
    keep-open for copyleft on distribution), note this before shipping any
    product that bundles the Lightpanda binary.

Implementation therefore follows ``moli_backend.py``'s proven pattern:
spawn ``lightpanda serve``, poll ``/json/version``, then
``playwright.chromium.connect_over_cdp`` and drive it through the standard
``PlaywrightBackend`` tab/page machinery.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import urllib.request
from typing import Optional

from core.backends.playwright_backend import PlaywrightBackend
from core.types import BrowserBackendType

DEFAULT_LIGHTPANDA_HOST = "127.0.0.1"
DEFAULT_LIGHTPANDA_PORT = 9223  # 9222 is Chrome's default; avoid collisions.


class LightpandaBackend(PlaywrightBackend):
    """Adapter driving the Lightpanda Zig engine via Playwright connect_over_cdp."""

    def __init__(self, headless: bool = True):
        super().__init__(headless)
        self.port = int(os.environ.get("LIGHTPANDA_PORT", DEFAULT_LIGHTPANDA_PORT))
        self.host = os.environ.get("LIGHTPANDA_HOST", DEFAULT_LIGHTPANDA_HOST)
        self._lp_proc: Optional[subprocess.Popen] = None

    @property
    def backend_type(self) -> BrowserBackendType:
        return BrowserBackendType.LIGHTPANDA

    def _binary(self) -> Optional[str]:
        explicit = os.environ.get("LIGHTPANDA_BIN")
        if explicit and os.path.isfile(explicit):
            return explicit
        return shutil.which("lightpanda")

    async def is_available(self) -> bool:
        # Detection only — never downloads or installs anything.
        return self._binary() is not None

    async def is_connected(self) -> bool:
        return self._browser is not None and self._browser.is_connected()

    async def _is_http_ready(self) -> bool:
        def probe() -> bool:
            try:
                req = urllib.request.Request(f"http://{self.host}:{self.port}/json/version")
                with urllib.request.urlopen(req, timeout=1.0) as resp:
                    data = json.loads(resp.read().decode())
                    return (
                        "webSocketDebuggerUrl" in data
                        or "Browser" in data
                        or "Protocol-Version" in data
                    )
            except Exception:
                return False

        return await asyncio.to_thread(probe)

    async def start(self) -> None:
        if await self.is_connected():
            return

        exe = self._binary()
        if not exe:
            raise RuntimeError(
                "Lightpanda binary not found. Install it (e.g. from the "
                "lightpanda-io/browser nightly releases or `docker pull "
                "lightpanda/browser:nightly`) or set LIGHTPANDA_BIN to its path."
            )

        if not await self._is_http_ready():
            self._lp_proc = subprocess.Popen(
                [exe, "serve", "--host", self.host, "--port", str(self.port)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            for _ in range(30):
                if await self._is_http_ready():
                    break
                await asyncio.sleep(0.5)
            else:
                raise RuntimeError(f"Lightpanda did not start CDP on port {self.port}")

        # Connect Playwright over the CDP WebSocket.
        from playwright.async_api import async_playwright
        self._pw = await async_playwright().start()

        def fetch_ws_url() -> str:
            try:
                req = urllib.request.Request(f"http://{self.host}:{self.port}/json/version")
                with urllib.request.urlopen(req, timeout=1.0) as resp:
                    data = json.loads(resp.read().decode())
                    return data.get("webSocketDebuggerUrl") or f"ws://{self.host}:{self.port}/devtools/browser"
            except Exception:
                return f"ws://{self.host}:{self.port}/devtools/browser"

        ws_url = await asyncio.to_thread(fetch_ws_url)
        self._browser = await self._pw.chromium.connect_over_cdp(ws_url)
        self._context = (
            self._browser.contexts[0] if self._browser.contexts else await self._browser.new_context()
        )

        pages = self._context.pages
        page = pages[0] if pages else await self._context.new_page()

        self._page_counter += 1
        tid = f"lp_{self._page_counter}"
        self._pages[tid] = page
        self._active_tab_id = tid

    async def stop(self) -> None:
        if self._browser:
            await self._browser.close()
            self._browser = None
        if self._pw:
            await self._pw.stop()
            self._pw = None
        if self._lp_proc:
            self._lp_proc.terminate()
            self._lp_proc = None

    async def create_tab(self, url: str = "about:blank") -> str:
        page = await self._context.new_page()
        self._page_counter += 1
        tid = f"lp_{self._page_counter}"
        self._pages[tid] = page
        self._active_tab_id = tid
        if url and url != "about:blank":
            await page.goto(url)
        return tid
