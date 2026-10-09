"""Router tests — pure, no network, no proxy, no live browsers.

Exercises BrowserRouter.resolve_backend() with fully mocked backends.
Async entry points are driven with asyncio.run() so no extra test
dependency (e.g. pytest-asyncio) is required.
"""
import asyncio
from unittest import mock

import pytest

from core.backends.agent_browser_backend import AgentBrowserBackend
from core.backends.camoufox_backend import CamoufoxBackend
from core.backends.moli_backend import MoliBackend
from core.interfaces import BaseBrowserBackend
from core.router import BrowserRouter
from core.types import BrowserBackendType


class FakeBackend(BaseBrowserBackend):
    """Scriptable stand-in for a real backend. Never touches the network."""

    def __init__(self, btype, available=True, connected=False):
        self._btype = btype
        self._available = available
        self._connected = connected
        self.calls = []

    @property
    def backend_type(self):
        return self._btype

    async def is_available(self):
        return self._available

    async def is_connected(self):
        return self._connected

    async def start(self):
        self.calls.append("start")

    async def stop(self):
        self.calls.append("stop")

    async def list_tabs(self):
        return [{"id": "t1", "title": "fake"}]

    async def create_tab(self, url="about:blank"):
        return "t-new"

    async def switch_tab(self, tab_id, focus=False):
        self.calls.append(("switch_tab", tab_id, focus))

    async def close_tab(self, tab_id):
        self.calls.append(("close_tab", tab_id))

    async def navigate(self, tab_id, url, wait_until="load"):
        return True

    async def get_title(self, tab_id):
        return "fake title"

    async def get_url(self, tab_id):
        return "https://example.test/"

    async def evaluate(self, tab_id, expression):
        return {"expr": expression}

    async def click(self, tab_id, x, y):
        self.calls.append(("click", tab_id, x, y))
        return True

    async def type_text(self, tab_id, text):
        return True

    async def press_key(self, tab_id, key):
        return True

    async def scroll(self, tab_id, delta_x=0, delta_y=400):
        return True

    async def screenshot(self, tab_id, full_page=False):
        return b"\x89PNG-fake"

    async def build_page_model(self, tab_id):
        return None

    async def get_cookies(self, tab_id):
        return []

    async def set_cookies(self, tab_id, cookies):
        return True


def _router(chrome=None, obscura=None, playwright=None):
    return BrowserRouter(
        chrome=chrome or FakeBackend(BrowserBackendType.CHROME),
        obscura=obscura or FakeBackend(BrowserBackendType.OBSCURA),
        playwright=playwright or FakeBackend(BrowserBackendType.PLAYWRIGHT),
    )


def run(coro):
    return asyncio.run(coro)


# ── intent-based auto routing ────────────────────────────────────────────

def test_default_auto_routes_to_playwright():
    r = _router()
    b = run(r.resolve_backend())
    assert b.backend_type == BrowserBackendType.PLAYWRIGHT
    assert r._active_backend is b


def test_session_required_routes_to_chrome():
    chrome = FakeBackend(BrowserBackendType.CHROME, available=True, connected=True)
    r = _router(chrome=chrome)
    b = run(r.resolve_backend(session_required=True))
    assert b is chrome


def test_session_required_falls_back_when_chrome_not_connected():
    chrome = FakeBackend(BrowserBackendType.CHROME, available=True, connected=False)
    r = _router(chrome=chrome)
    b = run(r.resolve_backend(session_required=True))
    # Chrome isn't connected -> default fast path (Playwright).
    assert b.backend_type == BrowserBackendType.PLAYWRIGHT


def test_stealth_required_routes_to_obscura():
    obscura = FakeBackend(BrowserBackendType.OBSCURA, available=True, connected=True)
    r = _router(obscura=obscura)
    b = run(r.resolve_backend(stealth_required=True))
    assert b is obscura


def test_stealth_required_falls_back_when_obscura_not_connected():
    obscura = FakeBackend(BrowserBackendType.OBSCURA, available=True, connected=False)
    r = _router(obscura=obscura)
    b = run(r.resolve_backend(stealth_required=True))
    assert b.backend_type == BrowserBackendType.PLAYWRIGHT


def test_intent_string_alone_does_not_change_routing():
    # `intent` is accepted by the signature but routing keys off the
    # session_required / stealth_required flags (documents current behavior).
    r = _router()
    b = run(r.resolve_backend(intent="research"))
    assert b.backend_type == BrowserBackendType.PLAYWRIGHT


# ── fallback chain ─────────────────────────────────────────────────────

def test_fallback_playwright_down_goes_to_chrome():
    pw = FakeBackend(BrowserBackendType.PLAYWRIGHT, available=False)
    chrome = FakeBackend(BrowserBackendType.CHROME, available=True)
    r = _router(chrome=chrome, playwright=pw)
    b = run(r.resolve_backend())
    assert b is chrome


def test_fallback_chrome_down_goes_to_obscura():
    pw = FakeBackend(BrowserBackendType.PLAYWRIGHT, available=False)
    chrome = FakeBackend(BrowserBackendType.CHROME, available=False)
    obscura = FakeBackend(BrowserBackendType.OBSCURA, available=True)
    r = _router(chrome=chrome, obscura=obscura, playwright=pw)
    b = run(r.resolve_backend())
    assert b is obscura


def test_fallback_all_down_raises():
    r = _router(
        chrome=FakeBackend(BrowserBackendType.CHROME, available=False),
        obscura=FakeBackend(BrowserBackendType.OBSCURA, available=False),
        playwright=FakeBackend(BrowserBackendType.PLAYWRIGHT, available=False),
    )
    with pytest.raises(RuntimeError, match="No browser backend"):
        run(r.resolve_backend())


# ── explicit preference ────────────────────────────────────────────────

def test_explicit_chrome_preference():
    r = _router()
    b = run(r.resolve_backend(preference="chrome"))
    assert b.backend_type == BrowserBackendType.CHROME


def test_explicit_csi_preference_uses_dedicated_backend():
    # "csi" now resolves to the dedicated CSIBackend (lazy import), not Chrome.
    from core.backends.csi_backend import CSIBackend
    r = _router()
    with mock.patch.object(CSIBackend, "is_available", return_value=True):
        b = run(r.resolve_backend(preference="csi"))
    assert b.backend_type == BrowserBackendType.CSI


def test_explicit_undetected_preference():
    from core.backends.undetected_backend import UndetectedBackend
    r = _router()
    with mock.patch.object(UndetectedBackend, "is_available", return_value=True):
        b = run(r.resolve_backend(preference="undetected"))
    assert b.backend_type == BrowserBackendType.UNDETECTED


def test_explicit_lightpanda_preference():
    from core.backends.lightpanda_backend import LightpandaBackend
    r = _router()
    with mock.patch.object(LightpandaBackend, "is_available", return_value=True):
        b = run(r.resolve_backend(preference="lightpanda"))
    assert b.backend_type == BrowserBackendType.LIGHTPANDA


def test_cloudflare_required_routes_to_undetected_first():
    from core.backends.undetected_backend import UndetectedBackend
    r = _router()
    with mock.patch.object(UndetectedBackend, "is_available", return_value=True):
        b = run(r.resolve_backend(cloudflare_required=True))
    assert b.backend_type == BrowserBackendType.UNDETECTED


def test_cloudflare_required_falls_back_to_obscura():
    from core.backends.undetected_backend import UndetectedBackend
    r = _router(
        obscura=FakeBackend(BrowserBackendType.OBSCURA, available=True, connected=True),
    )
    with mock.patch.object(UndetectedBackend, "is_available", return_value=False):
        b = run(r.resolve_backend(cloudflare_required=True))
    assert b.backend_type == BrowserBackendType.OBSCURA


def test_cloudflare_required_all_down_raises():
    from core.backends.undetected_backend import UndetectedBackend
    from core.backends.camoufox_backend import CamoufoxBackend
    r = _router(
        obscura=FakeBackend(BrowserBackendType.OBSCURA, available=False),
    )
    with mock.patch.object(UndetectedBackend, "is_available", return_value=False), \
         mock.patch.object(CamoufoxBackend, "is_available", return_value=False):
        with pytest.raises(RuntimeError, match="[Cc]loudflare"):
            run(r.resolve_backend(cloudflare_required=True))


def test_stealth_required_falls_back_to_camoufox_then_undetected():
    from core.backends.camoufox_backend import CamoufoxBackend
    from core.backends.undetected_backend import UndetectedBackend
    r = _router(
        obscura=FakeBackend(BrowserBackendType.OBSCURA, available=True, connected=False),
    )
    with mock.patch.object(CamoufoxBackend, "is_available", return_value=True):
        b = run(r.resolve_backend(stealth_required=True))
    assert b.backend_type == BrowserBackendType.CAMOUFOX


def test_flaresolverr_available_helper():
    r = _router()
    with mock.patch("core.cloudflare.FlareSolverrClient") as cls:
        cls.return_value.is_available.return_value = True
        assert r.flaresolverr_available() is True
        cls.return_value.is_available.return_value = False
        assert r.flaresolverr_available() is False


def test_explicit_preference_unavailable_raises():
    r = _router(chrome=FakeBackend(BrowserBackendType.CHROME, available=False))
    with pytest.raises(RuntimeError, match="[Cc]hrome"):
        run(r.resolve_backend(preference="chrome"))


def test_explicit_obscura_preference():
    r = _router()
    b = run(r.resolve_backend(preference="obscura"))
    assert b.backend_type == BrowserBackendType.OBSCURA


def test_explicit_playwright_preference():
    r = _router()
    b = run(r.resolve_backend(preference="playwright"))
    assert b.backend_type == BrowserBackendType.PLAYWRIGHT


def test_moli_preference_registered_backend():
    moli = FakeBackend(BrowserBackendType.MOLI, available=True)
    r = _router()
    r.register_backend(BrowserBackendType.MOLI, moli)
    b = run(r.resolve_backend(preference="moli"))
    assert b is moli


def test_moli_preference_unavailable_raises():
    # Lazily instantiates the real MoliBackend; force its availability
    # check off so the test is deterministic on any machine.
    with mock.patch.object(
        MoliBackend, "is_available", new=mock.AsyncMock(return_value=False)
    ):
        r = _router()
        with pytest.raises(RuntimeError, match="Moli not available"):
            run(r.resolve_backend(preference="moli"))


def test_camoufox_preference_unavailable_raises():
    with mock.patch.object(
        CamoufoxBackend, "is_available", new=mock.AsyncMock(return_value=False)
    ):
        r = _router()
        with pytest.raises(RuntimeError, match="Camoufox not available"):
            run(r.resolve_backend(preference="camoufox"))


def test_agent_browser_preference_unavailable_raises():
    with mock.patch.object(
        AgentBrowserBackend, "is_available", new=mock.AsyncMock(return_value=False)
    ):
        r = _router()
        with pytest.raises(RuntimeError, match="Agent-Browser not available"):
            run(r.resolve_backend(preference="agent-browser"))


# ── router bookkeeping ─────────────────────────────────────────────────

def test_get_available_backends_lists_only_available():
    r = _router(
        chrome=FakeBackend(BrowserBackendType.CHROME, available=True),
        obscura=FakeBackend(BrowserBackendType.OBSCURA, available=False),
        playwright=FakeBackend(BrowserBackendType.PLAYWRIGHT, available=True),
    )
    avail = run(r.get_available_backends())
    assert set(avail) == {BrowserBackendType.CHROME, BrowserBackendType.PLAYWRIGHT}


def test_health_check_reports_availability_and_active():
    r = _router()
    run(r.resolve_backend())  # sets active backend
    status = run(r.health_check())
    assert status["chrome"]["available"] is True
    assert status["obscura"]["available"] is True
    assert status["playwright"]["available"] is True
    assert status["active_backend"] == BrowserBackendType.PLAYWRIGHT.value


def test_register_backend_replaces_entry():
    r = _router()
    new_pw = FakeBackend(BrowserBackendType.PLAYWRIGHT, available=True)
    r.register_backend(BrowserBackendType.PLAYWRIGHT, new_pw)
    assert r.backends[BrowserBackendType.PLAYWRIGHT] is new_pw


def test_unified_proxy_method_delegates_to_active_backend():
    chrome = FakeBackend(BrowserBackendType.CHROME, available=True, connected=True)
    r = _router(chrome=chrome)
    run(r.resolve_backend(session_required=True))
    tabs = run(r.list_tabs())
    assert tabs == [{"id": "t1", "title": "fake"}]
    title = run(r.get_title("t1"))
    assert title == "fake title"


def test_unified_proxy_resolves_backend_when_none_active():
    r = _router()
    assert r._active_backend is None
    tabs = run(r.list_tabs())  # triggers resolve_backend() internally
    assert tabs == [{"id": "t1", "title": "fake"}]
    assert r._active_backend is not None


def test_execute_action_click_verify_off():
    pw = FakeBackend(BrowserBackendType.PLAYWRIGHT, available=True)
    r = _router(playwright=pw)
    res = run(
        r.execute_action("t1", "click", {"x": 10, "y": 20}, verify=False,
                         backend=pw)
    )
    assert res.ok is True
    assert res.action == "click"
    assert res.error is None
    assert ("click", "t1", 10, 20) in pw.calls


def test_execute_action_unknown_action_reports_error():
    pw = FakeBackend(BrowserBackendType.PLAYWRIGHT, available=True)
    r = _router(playwright=pw)
    res = run(
        r.execute_action("t1", "teleport", {}, verify=False, backend=pw)
    )
    assert res.ok is False
    assert "Unknown action" in (res.error or "")


# ── patchright lead-engine routing (v2.2) ────────────────────────────────

def test_patchright_preference_unavailable_raises():
    from core.backends.patchright_backend import PatchrightBackend
    with mock.patch.object(
        PatchrightBackend, "is_available", new=mock.AsyncMock(return_value=False)
    ):
        r = _router()
        with pytest.raises(RuntimeError, match="[Pp]atchright"):
            run(r.resolve_backend(preference="patchright"))


def test_patchright_preference_available():
    from core.backends.patchright_backend import PatchrightBackend
    with mock.patch.object(
        PatchrightBackend, "is_available", new=mock.AsyncMock(return_value=True)
    ):
        r = _router()
        b = run(r.resolve_backend(preference="patchright"))
        assert b.backend_type == BrowserBackendType.PATCHRIGHT


def test_stealth_chain_prefers_patchright():
    from core.backends.patchright_backend import PatchrightBackend
    with mock.patch.object(
        PatchrightBackend, "is_available", new=mock.AsyncMock(return_value=True)
    ):
        r = _router()
        b = run(r.resolve_backend(stealth_required=True))
        assert b.backend_type == BrowserBackendType.PATCHRIGHT


def test_stealth_chain_falls_back_when_patchright_missing():
    from core.backends.patchright_backend import PatchrightBackend
    obscura = FakeBackend(BrowserBackendType.OBSCURA, available=True, connected=True)
    with mock.patch.object(
        PatchrightBackend, "is_available", new=mock.AsyncMock(return_value=False)
    ):
        r = _router(obscura=obscura)
        b = run(r.resolve_backend(stealth_required=True))
        assert b is obscura


def test_cloudflare_chain_prefers_patchright():
    from core.backends.patchright_backend import PatchrightBackend
    with mock.patch.object(
        PatchrightBackend, "is_available", new=mock.AsyncMock(return_value=True)
    ):
        r = _router()
        b = run(r.resolve_backend(cloudflare_required=True))
        assert b.backend_type == BrowserBackendType.PATCHRIGHT
