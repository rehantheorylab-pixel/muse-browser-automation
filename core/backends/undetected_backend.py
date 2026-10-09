"""core/backends/undetected_backend.py — Stealth Chromium backend.

Launches Chrome through ``undetected_chromedriver`` with Rehan's stealth
recipe (off-screen window, NOT headless; random remote-debugging port;
disposable UUID profile directory), then attaches to the live browser via
Playwright ``connect_over_cdp`` — the same attach pattern MoliBackend uses.

The disposable profile directory is deleted on :meth:`stop` so repeated
runs do not bloat the disk.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import socket
import urllib.request
import uuid
from typing import Any, Dict, List, Optional

from core.backends.playwright_backend import PlaywrightBackend
from core.types import BrowserBackendType

logger = logging.getLogger(__name__)

_DEBUG_READY_RETRIES = 60
_DEBUG_READY_INTERVAL_S = 0.5


def get_random_free_port() -> int:
    """Return a currently-free loopback TCP port (bind port 0, read it back)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def default_stealth_profile_root() -> str:
    """Base directory for disposable stealth profiles.

    ``%USERPROFILE%/muse-browser-mcp/stealth_profiles`` on Windows,
    ``/tmp/muse-browser-mcp/stealth_profiles`` everywhere else.
    """
    if os.name == "nt":
        base = os.environ.get("USERPROFILE") or os.path.expanduser("~")
        return os.path.join(base, "muse-browser-mcp", "stealth_profiles")
    return os.path.join("/tmp", "muse-browser-mcp", "stealth_profiles")


def _build_stealth_driver_with_port(
    base_profile_dir: str,
) -> tuple[Any, str, int]:
    """Build the stealth driver and also report the DevTools port.

    The driver construction below is exactly Rehan's spec (off-screen,
    NOT headless; disposable ``prof_<uuid>`` profile dir) plus 2026
    flag hygiene from the stealth research:
      - --disable-blink-features=AutomationControlled (hides the
        AutomationControlled blink feature)
      - --force-webrtc-ip-handling-policy=disable_non_proxied_udp
        (WebRTC IP leak prevention; mDNS obfuscation stays default-on)
    uc itself strips navigator.webdriver and the $cdc_ CDP signatures.
    """
    import undetected_chromedriver as uc

    profile_path = os.path.join(base_profile_dir, f"prof_{uuid.uuid4().hex[:8]}")
    os.makedirs(profile_path, exist_ok=True)
    debug_port = get_random_free_port()
    options = uc.ChromeOptions()
    options.add_argument(f"--user-data-dir={profile_path}")
    options.add_argument(f"--remote-debugging-port={debug_port}")
    options.add_argument("--window-position=-32000,-32000")  # off-screen, NOT headless
    options.add_argument("--window-size=1280,800")
    options.add_argument("--password-store=basic")
    # 2026 flag hygiene: hide AutomationControlled blink feature, and
    # stop WebRTC from leaking non-proxied UDP (host/srflx candidates).
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument(
        "--force-webrtc-ip-handling-policy=disable_non_proxied_udp"
    )
    driver = uc.Chrome(options=options)
    return driver, profile_path, debug_port


def build_stealth_driver(base_profile_dir: str) -> tuple[Any, str]:
    """Build a stealth undetected-chromedriver Chrome in a disposable profile.

    Returns ``(driver, profile_path)``. The profile directory is a fresh
    ``prof_<uuid8>`` folder under ``base_profile_dir``; the caller owns it
    and should delete it after ``driver.quit()`` to avoid disk bloat.
    """
    driver, profile_path, _debug_port = _build_stealth_driver_with_port(
        base_profile_dir
    )
    return driver, profile_path


class UndetectedBackend(PlaywrightBackend):
    """Stealth backend: undetected-chromedriver launches Chrome off-screen,
    Playwright attaches over CDP and drives it.

    All tab/page/cookie methods are inherited from :class:`PlaywrightBackend`
    and operate on the attached Playwright page objects.

    Anti-detection layers applied on ``start()`` (2026 research):
      1. Fingerprint JS bundle (``core.stealth.fingerprint``) injected via
         ``context.add_init_script`` — seeded canvas noise with probe
         guards, WebGL spoof, WebRTC candidate filtering, navigator
         coherence, WebGPU hiding, font/audio scalar spoofing, Worker
         re-injection. Every hook keeps [native code] toString camouflage.
      2. CDP Emulation overrides (below-JS): UA + metadata, timezone,
         device metrics — from the same coherent FingerprintProfile.
    """

    def __init__(
        self,
        base_profile_dir: Optional[str] = None,
        stealth_seed: Optional[str] = None,
        fingerprint: bool = True,
    ):
        # headless=False: uc Chrome runs headed but off-screen by design.
        super().__init__(headless=False)
        self._base_profile_dir = base_profile_dir or default_stealth_profile_root()
        self._driver: Optional[Any] = None
        self._profile_path: Optional[str] = None
        self._debug_port: Optional[int] = None
        self._stealth_seed = stealth_seed or uuid.uuid4().hex
        self._fingerprint_enabled = fingerprint
        self._fingerprint_profile: Optional[Any] = None

    @property
    def backend_type(self) -> BrowserBackendType:
        return BrowserBackendType.UNDETECTED

    @staticmethod
    def _uc_importable() -> bool:
        try:
            import undetected_chromedriver  # noqa: F401
            return True
        except ImportError:
            return False

    @staticmethod
    def _chromedriver_present() -> bool:
        return (
            shutil.which("chromedriver") is not None
            or shutil.which("chromedriver.exe") is not None
        )

    async def is_available(self) -> bool:
        """Available when undetected_chromedriver is importable AND a
        chromedriver binary is on PATH."""
        return self._uc_importable() and self._chromedriver_present()

    async def is_connected(self) -> bool:
        if self._driver is None:
            return False
        return await super().is_connected()

    async def _is_debug_ready(self) -> bool:
        """Poll the Chrome DevTools HTTP endpoint until it answers."""
        if not self._debug_port:
            return False
        try:
            req = urllib.request.Request(
                f"http://127.0.0.1:{self._debug_port}/json/version"
            )
            with urllib.request.urlopen(req, timeout=1.0) as resp:
                data = json.loads(resp.read().decode())
                return "webSocketDebuggerUrl" in data or "Browser" in data
        except Exception:
            return False

    async def _debug_ws_url(self) -> str:
        """Read the browser-level DevTools websocket URL from /json/version."""
        assert self._debug_port
        try:
            req = urllib.request.Request(
                f"http://127.0.0.1:{self._debug_port}/json/version"
            )
            with urllib.request.urlopen(req, timeout=1.0) as resp:
                data = json.loads(resp.read().decode())
                ws_url = data.get("webSocketDebuggerUrl")
                if ws_url:
                    return ws_url
        except Exception as e:
            logger.warning("Could not read /json/version: %s", e)
        return f"ws://127.0.0.1:{self._debug_port}/devtools/browser"

    async def start(self) -> None:
        """Launch the stealth driver off-screen and attach Playwright over CDP."""
        if await self.is_connected():
            return
        if not self._uc_importable():
            raise RuntimeError(
                "undetected_chromedriver is not installed "
                "(pip install undetected-chromedriver)"
            )

        # uc.Chrome() blocks — keep it off the event loop.
        self._driver, self._profile_path, self._debug_port = await asyncio.to_thread(
            _build_stealth_driver_with_port, self._base_profile_dir
        )

        for _ in range(_DEBUG_READY_RETRIES):
            if await self._is_debug_ready():
                break
            await asyncio.sleep(_DEBUG_READY_INTERVAL_S)
        else:
            port = self._debug_port
            await self._teardown_driver_only()
            raise RuntimeError(
                f"Chrome DevTools endpoint did not respond on port {port}"
            )

        from playwright.async_api import async_playwright

        self._pw = await async_playwright().start()
        try:
            self._browser = await self._pw.chromium.connect_over_cdp(
                await self._debug_ws_url()
            )
        except Exception as e:
            await self.stop()
            raise RuntimeError(f"Playwright CDP attach failed: {e}") from e

        self._context = (
            self._browser.contexts[0]
            if self._browser.contexts
            else await self._browser.new_context()
        )

        # Anti-detection layer: coherent fingerprint profile + injection.
        if self._fingerprint_enabled:
            await self._apply_stealth_profile()

        pages = self._context.pages
        page = pages[0] if pages else await self._context.new_page()

        self._page_counter += 1
        tid = f"uc_{self._page_counter}"
        self._pages[tid] = page
        self._active_tab_id = tid

    async def _apply_stealth_profile(self) -> None:
        """Build a FingerprintProfile and apply it via CDP + init script.

        CDP Emulation overrides run below-JS (no toString surface);
        the JS bundle covers canvas/WebGL/WebRTC/fonts/audio/navigator.
        Both derive from the SAME profile object, so values cohere.
        Failures are logged, never fatal (graceful degradation).
        """
        try:
            from core.stealth.fingerprint import (
                FingerprintProfile,
                build_injection_js,
            )
        except Exception as e:
            logger.warning("stealth fingerprint module unavailable: %s", e)
            return

        try:
            profile = FingerprintProfile(seed=self._stealth_seed)
            self._fingerprint_profile = profile

            # 1. Init script for every new document (runs before page JS).
            js = build_injection_js(profile)
            await self._context.add_init_script(js)

            # 2. CDP Emulation overrides (below-JS).
            pages = self._context.pages
            if pages:
                cdp = await self._context.new_cdp_session(pages[0])
                try:
                    ov = profile.cdp_emulation_overrides()
                    await cdp.send(
                        "Emulation.setUserAgentOverride", ov["user_agent"]
                    )
                    await cdp.send(
                        "Emulation.setTimezoneOverride", ov["timezone"]
                    )
                    await cdp.send(
                        "Emulation.setDeviceMetricsOverride",
                        ov["device_metrics"],
                    )
                finally:
                    try:
                        await cdp.detach()
                    except Exception:
                        pass
            logger.info(
                "stealth profile applied: preset=%s noise_seed=%d",
                profile.to_dict()["preset"],
                profile.noise_seed,
            )
        except Exception as e:
            logger.warning("applying stealth profile failed: %s", e)

    async def _teardown_driver_only(self) -> None:
        """Quit the uc driver and delete the disposable profile (no PW state)."""
        if self._driver is not None:
            try:
                self._driver.quit()
            except Exception as e:
                logger.warning("undetected driver quit failed: %s", e)
            self._driver = None
        if self._profile_path:
            shutil.rmtree(self._profile_path, ignore_errors=True)
            self._profile_path = None
        self._debug_port = None

    async def stop(self) -> None:
        """Detach Playwright, quit the driver, and delete the disposable profile."""
        if self._context:
            try:
                await self._context.close()
            except Exception:
                pass
            self._context = None
        if self._browser:
            try:
                await self._browser.close()
            except Exception:
                pass
            self._browser = None
        if self._pw:
            try:
                await self._pw.stop()
            except Exception:
                pass
            self._pw = None
        self._pages.clear()
        self._active_tab_id = None
        await self._teardown_driver_only()

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
        tid = f"uc_{self._page_counter}"
        self._pages[tid] = page
        self._active_tab_id = tid
        return tid

    async def get_cookies(self, tab_id: str) -> List[Dict[str, Any]]:
        """Retrieve the full cookie jar via the Playwright context."""
        return await super().get_cookies(tab_id)

    async def set_cookies(self, tab_id: str, cookies: List[Dict[str, Any]]) -> bool:
        """Inject cookies into the session via the Playwright context."""
        return await super().set_cookies(tab_id, cookies)
