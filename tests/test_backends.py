"""Backend adapter tests — pure, no network, no proxy, no live browsers.

- Every adapter must satisfy the BaseBrowserBackend interface.
- is_available() is exercised with mocked checks (filesystem / imports),
  never by launching a real browser.
"""
import asyncio
import importlib.abc
import sys
import types
from unittest import mock

import pytest

from core.backends.agent_browser_backend import AgentBrowserBackend
from core.backends.camoufox_backend import CamoufoxBackend
from core.backends.chrome_backend import ChromeBackend
from core.backends.moli_backend import MoliBackend
from core.backends.obscura_backend import ObscuraBackend
from core.backends.playwright_backend import PlaywrightBackend
from core.interfaces import BaseBrowserBackend
from core.types import BrowserBackendType

ALL_BACKENDS = [
    ChromeBackend,
    ObscuraBackend,
    PlaywrightBackend,
    MoliBackend,
    CamoufoxBackend,
    AgentBrowserBackend,
]

# Abstract methods every backend must implement.
REQUIRED_METHODS = [
    "is_available", "is_connected", "start", "stop",
    "list_tabs", "create_tab", "switch_tab", "close_tab",
    "navigate", "get_title", "get_url", "evaluate",
    "click", "type_text", "press_key", "scroll",
    "screenshot", "build_page_model",
    "get_cookies", "set_cookies",
]


def run(coro):
    return asyncio.run(coro)


class _BlockImport(importlib.abc.MetaPathFinder):
    """Meta-path finder that makes an import fail deterministically."""

    def __init__(self, name):
        self.name = name

    def find_spec(self, fullname, path=None, target=None):
        if fullname == self.name or fullname.startswith(self.name + "."):
            raise ImportError(f"blocked import for test: {fullname}")
        return None

    def __enter__(self):
        sys.meta_path.insert(0, self)
        return self

    def __exit__(self, *exc):
        sys.meta_path.remove(self)
        return False


# ── interface compliance ───────────────────────────────────────────────

def test_all_backends_implement_base_interface():
    for cls in ALL_BACKENDS:
        assert issubclass(cls, BaseBrowserBackend), cls.__name__


def test_all_backends_instantiate_with_no_abstracts():
    for cls in ALL_BACKENDS:
        inst = cls()
        assert not getattr(cls, "__abstractmethods__", frozenset()), cls.__name__
        assert isinstance(inst, BaseBrowserBackend)


def test_all_backends_implement_every_required_method():
    for cls in ALL_BACKENDS:
        for name in REQUIRED_METHODS:
            assert callable(getattr(cls, name, None)), f"{cls.__name__}.{name}"


def test_backend_type_is_valid_enum_member():
    expected = {
        ChromeBackend: BrowserBackendType.CHROME,
        ObscuraBackend: BrowserBackendType.OBSCURA,
        PlaywrightBackend: BrowserBackendType.PLAYWRIGHT,
    }
    for cls, want in expected.items():
        assert cls().backend_type == want, cls.__name__
    # Moli/Camoufox/AgentBrowser subclass PlaywrightBackend and inherit
    # its backend_type (PLAYWRIGHT) — documents the current quirk: the
    # router keys them by their registration type instead.
    for cls in (MoliBackend, CamoufoxBackend, AgentBrowserBackend):
        assert cls().backend_type == BrowserBackendType.PLAYWRIGHT, cls.__name__


def test_base_backend_cannot_be_instantiated():
    with pytest.raises(TypeError):
        BaseBrowserBackend()


# ── is_available with mocked checks ─────────────────────────────────────

def test_chrome_is_available_by_design():
    # ChromeBackend.is_available() hardcodes True (daemon model: the
    # connectivity check happens on real calls, not here).
    assert run(ChromeBackend().is_available()) is True


def test_chrome_is_available_mocked_off():
    with mock.patch.object(
        ChromeBackend, "is_available", new=mock.AsyncMock(return_value=False)
    ):
        assert run(ChromeBackend().is_available()) is False


def test_obscura_available_when_exe_present(monkeypatch):
    monkeypatch.setattr("os.path.isfile", lambda p: True)
    assert run(ObscuraBackend().is_available()) is True


def test_obscura_unavailable_when_no_exe_and_not_connected(monkeypatch):
    monkeypatch.setattr("os.path.isfile", lambda p: False)
    assert run(ObscuraBackend().is_available()) is False


def test_obscura_available_when_connected(monkeypatch):
    monkeypatch.setattr("os.path.isfile", lambda p: False)
    b = ObscuraBackend()
    b._ws = mock.Mock()
    b._ws.open = True
    assert run(b.is_available()) is True


def test_obscura_is_connected_false_when_no_socket():
    assert run(ObscuraBackend().is_connected()) is False


def test_playwright_available_when_importable(monkeypatch):
    fake_pkg = types.ModuleType("playwright")
    fake_api = types.ModuleType("playwright.async_api")
    fake_api.async_playwright = object()
    fake_pkg.async_api = fake_api
    monkeypatch.setitem(sys.modules, "playwright", fake_pkg)
    monkeypatch.setitem(sys.modules, "playwright.async_api", fake_api)
    assert run(PlaywrightBackend().is_available()) is True


def test_playwright_unavailable_when_not_importable():
    sys.modules.pop("playwright", None)
    sys.modules.pop("playwright.async_api", None)
    with _BlockImport("playwright"):
        assert run(PlaywrightBackend().is_available()) is False


def test_playwright_is_connected_false_before_start():
    assert run(PlaywrightBackend().is_connected()) is False


def test_moli_available_when_exe_present(monkeypatch):
    monkeypatch.setattr("os.path.isfile", lambda p: True)
    assert run(MoliBackend().is_available()) is True


def test_moli_unavailable_when_exe_missing(monkeypatch):
    monkeypatch.setattr("os.path.isfile", lambda p: False)
    assert run(MoliBackend().is_available()) is False


def test_camoufox_available_when_importable(monkeypatch):
    fake = types.ModuleType("camoufox")
    monkeypatch.setitem(sys.modules, "camoufox", fake)
    assert run(CamoufoxBackend().is_available()) is True


def test_camoufox_unavailable_when_not_importable():
    sys.modules.pop("camoufox", None)
    with _BlockImport("camoufox"):
        assert run(CamoufoxBackend().is_available()) is False


def test_agent_browser_available_when_exe_present(monkeypatch):
    monkeypatch.setattr("os.path.isfile", lambda p: True)
    assert run(AgentBrowserBackend().is_available()) is True


def test_agent_browser_unavailable_when_exe_missing(monkeypatch):
    monkeypatch.setattr("os.path.isfile", lambda p: False)
    assert run(AgentBrowserBackend().is_available()) is False


# ── start/stop contracts (no real browser launched) ─────────────────────

def test_chrome_start_succeeds_when_available():
    run(ChromeBackend().start())  # must not raise


def test_chrome_start_raises_when_unavailable():
    with mock.patch.object(
        ChromeBackend, "is_available", new=mock.AsyncMock(return_value=False)
    ):
        with pytest.raises(RuntimeError, match="not running"):
            run(ChromeBackend().start())


def test_chrome_stop_is_noop():
    run(ChromeBackend().stop())  # must not raise


def test_playwright_stop_without_start_is_safe():
    run(PlaywrightBackend().stop())  # must not raise
