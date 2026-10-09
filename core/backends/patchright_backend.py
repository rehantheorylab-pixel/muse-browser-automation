"""core/backends/patchright_backend.py — patchright stealth backend.

patchright (Apache-2.0, https://github.com/Kaliiiiiiiiii-Vinyzu/patchright)
is a compile-time AST-patched Playwright: it removes CDP-level automation
tells from the driver itself rather than injecting JS on top:

  - never calls CDP Runtime.enable (uses isolated execution contexts)
  - Console.enable leak removed
  - --disable-blink-features=AutomationControlled added;
    --enable-automation, --disable-popup-blocking, --disable-component-update,
    --disable-default-apps, --disable-extensions removed
  - automation globals renamed (no __playwright__binding__ etc. signatures)
  - init scripts injected via Playwright Routes, not addInitScript

It is a drop-in Playwright replacement, so this backend subclasses
PlaywrightBackend and only swaps the import + launch call. When
patchright is not installed, is_available() is False and the router
falls through to the next engine.
"""

from __future__ import annotations

from typing import Optional

from core.backends.playwright_backend import PlaywrightBackend
from core.types import BrowserBackendType


class PatchrightBackend(PlaywrightBackend):
    """Stealth backend using patchright instead of stock Playwright."""

    @property
    def backend_type(self) -> BrowserBackendType:
        return BrowserBackendType.PATCHRIGHT

    @staticmethod
    def _patchright_importable() -> bool:
        try:
            import patchright  # noqa: F401
            return True
        except ImportError:
            return False

    async def is_available(self) -> bool:
        return self._patchright_importable()

    async def start(self) -> None:
        if self._browser:
            return
        # Import here: patchright is optional.
        from patchright.async_api import async_playwright

        self._pw = await async_playwright().start()
        # Prefer the real installed Chrome over bundled Chromium for a
        # genuine GPU/plugin/font stack (research recommendation #8).
        try:
            self._browser = await self._pw.chromium.launch(
                channel="chrome", headless=self.headless
            )
        except Exception:
            self._browser = await self._pw.chromium.launch(headless=self.headless)
        self._context = await self._browser.new_context(
            viewport={"width": 1280, "height": 800},
            ignore_https_errors=True,
        )
        page = await self._context.new_page()
        self._page_counter += 1
        tid = f"pr_{self._page_counter}"
        self._pages[tid] = page
        self._active_tab_id = tid

    async def create_tab(self, url: str = "about:blank") -> str:
        if not self._context:
            await self.start()
        page = await self._context.new_page()
        if url and url != "about:blank":
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=90000)
            except Exception:
                pass
        self._page_counter += 1
        tid = f"pr_{self._page_counter}"
        self._pages[tid] = page
        self._active_tab_id = tid
        return tid
