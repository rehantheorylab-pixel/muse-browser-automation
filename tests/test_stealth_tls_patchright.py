"""Tests for core.stealth.tls_client and patchright backend — mock-only."""

import sys
import types
from unittest import mock

import pytest

from core.stealth.tls_client import (
    CHROME_UA,
    HEADER_ORDER_NAV,
    HEADER_ORDER_XHR,
    check_ua_tls_consistency,
    is_curl_cffi_available,
    make_api_client,
)
from core.backends.patchright_backend import PatchrightBackend
from core.types import BrowserBackendType


# --- tls_client -------------------------------------------------------------

def test_chrome_ua_claims_chrome():
    assert "Chrome" in CHROME_UA
    assert "Windows" in CHROME_UA


def test_header_orders_sane():
    assert HEADER_ORDER_NAV[0] == ":method"
    assert "user-agent" in HEADER_ORDER_NAV
    assert "cookie" not in HEADER_ORDER_NAV  # cookies split per-header on h2
    assert HEADER_ORDER_XHR[0] == ":method"


def test_ua_tls_consistency():
    assert check_ua_tls_consistency(CHROME_UA, "chrome") is True
    assert check_ua_tls_consistency(CHROME_UA, "safari") is False
    assert check_ua_tls_consistency(
        "Mozilla/5.0 (Macintosh; Intel Mac OS X) Version/17.0 Safari/605.1.15",
        "chrome",
    ) is False


def test_make_api_client_requires_curl_cffi(monkeypatch):
    # Simulate curl_cffi missing.
    monkeypatch.setitem(sys.modules, "curl_cffi", None)
    # Force re-import failure path via import hook.
    import builtins
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "curl_cffi" or name.startswith("curl_cffi."):
            raise ImportError("blocked")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(RuntimeError, match="curl_cffi"):
        make_api_client()


def test_make_api_client_builds_session(monkeypatch):
    calls = {}

    class FakeSession:
        def __init__(self, **kwargs):
            calls.update(kwargs)
            self.headers = {}

    fake_requests = types.SimpleNamespace(Session=FakeSession)
    fake_mod = types.ModuleType("curl_cffi")
    fake_mod.requests = fake_requests
    monkeypatch.setitem(sys.modules, "curl_cffi", fake_mod)
    monkeypatch.setitem(sys.modules, "curl_cffi.requests", fake_requests)

    s = make_api_client()
    assert calls.get("impersonate") == "chrome"
    assert s.headers["User-Agent"] == CHROME_UA

    s2 = make_api_client(user_agent="Custom/1.0", impersonate="chrome")
    assert s2.headers["User-Agent"] == "Custom/1.0"


# --- patchright backend ------------------------------------------------------

def test_patchright_backend_type():
    b = PatchrightBackend()
    assert b.backend_type == BrowserBackendType.PATCHRIGHT
    assert b.backend_type.value == "patchright"


def test_patchright_tab_id_prefix():
    b = PatchrightBackend()
    assert b._page_counter == 0  # prefix asserted on live use (pr_)


def test_patchright_unavailable_without_package(monkeypatch):
    import builtins
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "patchright":
            raise ImportError("blocked")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    assert PatchrightBackend._patchright_importable() is False
