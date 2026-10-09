"""Tests for core/backends/undetected_backend.py — mocks only.

No live browser, no network, no real undetected_chromedriver install.
Everything external (uc module, chromedriver, Playwright CDP attach,
DevTools HTTP polling) is stubbed.
"""
import asyncio
import importlib.abc
import os
import sys
import types
from unittest import mock

import pytest

from core.backends.undetected_backend import (
    UndetectedBackend,
    build_stealth_driver,
    default_stealth_profile_root,
    get_random_free_port,
)
from core.interfaces import BaseBrowserBackend
from core.types import BrowserBackendType


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


def _install_fake_uc(monkeypatch):
    """Put a fake `undetected_chromedriver` module in sys.modules."""
    created = {}

    class FakeChromeOptions:
        def __init__(self):
            self.args = []

        def add_argument(self, arg):
            self.args.append(arg)

    class FakeChrome:
        def __init__(self, options=None, **kwargs):
            created["options"] = options
            created["kwargs"] = kwargs
            self.quit_called = False

        def quit(self):
            self.quit_called = True

    fake_uc = types.ModuleType("undetected_chromedriver")
    fake_uc.ChromeOptions = FakeChromeOptions
    fake_uc.Chrome = FakeChrome
    monkeypatch.setitem(sys.modules, "undetected_chromedriver", fake_uc)
    return fake_uc, created


def _install_fake_playwright(monkeypatch):
    """Put a fake `playwright.async_api` module in sys.modules.

    Returns (fake_pw, fake_browser, fake_context, pages_list).
    """
    fake_page = mock.AsyncMock()
    fake_page.url = "about:blank"
    fake_page.title = mock.AsyncMock(return_value="Fake Title")

    fake_context = mock.MagicMock()
    fake_context.pages = [fake_page]
    fake_context.new_page = mock.AsyncMock(return_value=fake_page)
    fake_context.close = mock.AsyncMock()
    fake_context.cookies = mock.AsyncMock(return_value=[])
    fake_context.add_cookies = mock.AsyncMock()

    fake_browser = mock.MagicMock()
    fake_browser.contexts = [fake_context]
    fake_browser.new_context = mock.AsyncMock(return_value=fake_context)
    fake_browser.close = mock.AsyncMock()
    fake_browser.is_connected = mock.Mock(return_value=True)

    fake_pw = mock.MagicMock()
    fake_pw.chromium.connect_over_cdp = mock.AsyncMock(return_value=fake_browser)
    fake_pw.stop = mock.AsyncMock()

    pw_factory = mock.MagicMock()
    pw_factory.start = mock.AsyncMock(return_value=fake_pw)

    fake_api = types.ModuleType("playwright.async_api")
    fake_api.async_playwright = lambda: pw_factory
    monkeypatch.setitem(sys.modules, "playwright.async_api", fake_api)
    return fake_pw, fake_browser, fake_context, [fake_page]


def _fake_builder_factory(fake_driver):
    def fake_builder(base_dir):
        profile = os.path.join(base_dir, "prof_test1234")
        os.makedirs(profile, exist_ok=True)
        return fake_driver, profile, 54321

    return fake_builder


def _start_mocked(monkeypatch, backend, fake_driver):
    """Patch uc-import check, builder, DevTools polling and Playwright, then run start()."""
    _install_fake_playwright(monkeypatch)
    patches = [
        mock.patch.object(backend, "_uc_importable", return_value=True),
        mock.patch(
            "core.backends.undetected_backend._build_stealth_driver_with_port",
            side_effect=_fake_builder_factory(fake_driver),
        ),
        mock.patch.object(
            backend, "_is_debug_ready", new=mock.AsyncMock(return_value=True)
        ),
        mock.patch.object(
            backend,
            "_debug_ws_url",
            new=mock.AsyncMock(return_value="ws://127.0.0.1:54321/devtools/browser"),
        ),
    ]
    for p in patches:
        p.start()
    try:
        run(backend.start())
    finally:
        for p in patches:
            p.stop()


# ── port allocation ──────────────────────────────────────────────────────

def test_get_random_free_port_returns_free_port():
    port = get_random_free_port()
    assert isinstance(port, int)
    assert 1024 <= port <= 65535
    # The port must actually be free: binding to it must succeed.
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", port))  # raises if the port were taken


def test_get_random_free_port_varies():
    ports = {get_random_free_port() for _ in range(10)}
    assert len(ports) > 1  # ephemeral allocation should not repeat forever


# ── profile root ─────────────────────────────────────────────────────────

def test_default_stealth_profile_root_posix(monkeypatch):
    monkeypatch.setattr("os.name", "posix")
    root = default_stealth_profile_root()
    assert root == "/tmp/muse-browser-mcp/stealth_profiles"


def test_default_stealth_profile_root_windows(monkeypatch):
    monkeypatch.setattr("os.name", "nt")
    monkeypatch.setenv("USERPROFILE", "C:\\Users\\Test")
    root = default_stealth_profile_root()
    # os.path keeps posix semantics under this test env, so assert structure
    # rather than separators.
    assert root.startswith("C:\\Users\\Test")
    assert "muse-browser-mcp" in root
    assert root.endswith("stealth_profiles")


# ── spec-faithful driver construction ─────────────────────────────────────

def test_build_stealth_driver_spec_arguments(monkeypatch, tmp_path):
    _, created = _install_fake_uc(monkeypatch)
    driver, profile_path = build_stealth_driver(str(tmp_path))

    assert os.path.isdir(profile_path)
    assert os.path.basename(profile_path).startswith("prof_")
    assert len(os.path.basename(profile_path).split("prof_")[1]) == 8

    options = created["options"]
    args = options.args
    assert args[0] == f"--user-data-dir={profile_path}"
    assert args[1].startswith("--remote-debugging-port=")
    int(args[1].split("=", 1)[1])  # port parses as int
    assert args[2] == "--window-position=-32000,-32000"  # off-screen, NOT headless
    assert args[3] == "--window-size=1280,800"
    assert args[4] == "--password-store=basic"
    assert created["kwargs"] == {}
    assert driver is not None


def test_build_stealth_driver_returns_two_tuple(monkeypatch, tmp_path):
    _install_fake_uc(monkeypatch)
    result = build_stealth_driver(str(tmp_path))
    assert isinstance(result, tuple) and len(result) == 2


def test_build_stealth_driver_unique_profiles(monkeypatch, tmp_path):
    _install_fake_uc(monkeypatch)
    _, p1 = build_stealth_driver(str(tmp_path))
    _, p2 = build_stealth_driver(str(tmp_path))
    assert p1 != p2


# ── backend identity / interface ─────────────────────────────────────────

def test_backend_type_is_undetected_enum():
    assert UndetectedBackend().backend_type is BrowserBackendType.UNDETECTED
    assert BrowserBackendType.UNDETECTED.value == "undetected"


def test_implements_base_browser_backend_interface():
    assert issubclass(UndetectedBackend, BaseBrowserBackend)
    assert not getattr(UndetectedBackend, "__abstractmethods__", frozenset())


# ── is_available ─────────────────────────────────────────────────────────

def test_is_available_false_when_uc_missing(monkeypatch):
    monkeypatch.delitem(sys.modules, "undetected_chromedriver", raising=False)
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/chromedriver")
    with _BlockImport("undetected_chromedriver"):
        assert run(UndetectedBackend().is_available()) is False


def test_is_available_false_when_no_chromedriver(monkeypatch):
    _install_fake_uc(monkeypatch)
    monkeypatch.setattr("shutil.which", lambda name: None)
    assert run(UndetectedBackend().is_available()) is False


def test_is_available_true_when_uc_and_driver_present(monkeypatch):
    _install_fake_uc(monkeypatch)
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/chromedriver")
    assert run(UndetectedBackend().is_available()) is True


def test_is_connected_false_before_start():
    assert run(UndetectedBackend().is_connected()) is False


# ── start/stop lifecycle ─────────────────────────────────────────────────

def test_start_raises_when_uc_missing(monkeypatch):
    monkeypatch.delitem(sys.modules, "undetected_chromedriver", raising=False)
    with _BlockImport("undetected_chromedriver"):
        with pytest.raises(RuntimeError, match="undetected_chromedriver"):
            run(UndetectedBackend().start())


def test_start_attaches_and_registers_tab(monkeypatch, tmp_path):
    b = UndetectedBackend(base_profile_dir=str(tmp_path))
    fake_driver = mock.MagicMock()
    _start_mocked(monkeypatch, b, fake_driver)

    assert run(b.is_connected()) is True
    assert b._driver is fake_driver
    assert b._debug_port == 54321
    assert list(b._pages.keys()) == ["uc_1"]
    assert b._active_tab_id == "uc_1"
    run(b.stop())


def test_start_is_idempotent(monkeypatch, tmp_path):
    b = UndetectedBackend(base_profile_dir=str(tmp_path))
    fake_driver = mock.MagicMock()
    builder = mock.Mock(side_effect=_fake_builder_factory(fake_driver))
    _install_fake_playwright(monkeypatch)
    with mock.patch.object(b, "_uc_importable", return_value=True), mock.patch(
        "core.backends.undetected_backend._build_stealth_driver_with_port", builder
    ), mock.patch.object(
        b, "_is_debug_ready", new=mock.AsyncMock(return_value=True)
    ), mock.patch.object(
        b,
        "_debug_ws_url",
        new=mock.AsyncMock(return_value="ws://127.0.0.1:54321/devtools/browser"),
    ):
        run(b.start())
        run(b.start())  # second start must be a no-op
    assert builder.call_count == 1
    run(b.stop())


def test_stop_quits_driver_and_deletes_profile(monkeypatch, tmp_path):
    b = UndetectedBackend(base_profile_dir=str(tmp_path))
    fake_driver = mock.MagicMock()
    _start_mocked(monkeypatch, b, fake_driver)

    profile_path = b._profile_path
    assert profile_path and os.path.isdir(profile_path)

    run(b.stop())

    fake_driver.quit.assert_called_once()
    assert not os.path.exists(profile_path)
    assert b._pages == {}
    assert b._active_tab_id is None
    assert run(b.is_connected()) is False


def test_stop_without_start_is_safe():
    run(UndetectedBackend().stop())  # must not raise


# ── tab CRUD ─────────────────────────────────────────────────────────────

def test_tab_crud_with_mocked_pages(monkeypatch, tmp_path):
    b = UndetectedBackend(base_profile_dir=str(tmp_path))
    _start_mocked(monkeypatch, b, mock.MagicMock())

    tid2 = run(b.create_tab())
    assert tid2 == "uc_2"
    assert tid2.startswith("uc_")

    tabs = run(b.list_tabs())
    assert {t["id"] for t in tabs} == {"uc_1", "uc_2"}
    assert sum(t["active"] for t in tabs) == 1

    run(b.switch_tab("uc_1"))
    assert b._active_tab_id == "uc_1"

    run(b.close_tab("uc_2"))
    assert "uc_2" not in b._pages
    tabs = run(b.list_tabs())
    assert [t["id"] for t in tabs] == ["uc_1"]

    run(b.stop())


def test_get_and_set_cookies_delegate_to_context(monkeypatch, tmp_path):
    b = UndetectedBackend(base_profile_dir=str(tmp_path))
    fake_driver = mock.MagicMock()
    # Install the fake Playwright and keep the context handle for assertions.
    _, _, fake_context, _ = _install_fake_playwright(monkeypatch)
    builder_patch = mock.patch(
        "core.backends.undetected_backend._build_stealth_driver_with_port",
        side_effect=_fake_builder_factory(fake_driver),
    )
    with builder_patch, mock.patch.object(
        b, "_uc_importable", return_value=True
    ), mock.patch.object(
        b, "_is_debug_ready", new=mock.AsyncMock(return_value=True)
    ), mock.patch.object(
        b,
        "_debug_ws_url",
        new=mock.AsyncMock(return_value="ws://127.0.0.1:54321/devtools/browser"),
    ):
        run(b.start())
        fake_context.cookies = mock.AsyncMock(
            return_value=[{"name": "a", "value": "1"}]
        )
        assert run(b.get_cookies("uc_1")) == [{"name": "a", "value": "1"}]
        assert run(b.set_cookies("uc_1", [{"name": "a", "value": "1"}])) is True
        fake_context.add_cookies.assert_called_once_with([{"name": "a", "value": "1"}])
        run(b.stop())
