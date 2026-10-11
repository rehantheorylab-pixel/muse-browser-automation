"""Tests for HttpStaticAdapter TLS wiring — pure, no network.

- When curl_cffi is available, direct HTTP must go through
  core.stealth.tls_client.make_api_client (Chrome TLS impersonation),
  never raw urllib with a Chrome UA.
- When curl_cffi is missing, it falls back to urllib.
"""
import asyncio

from core.adapters import http_adapter
from core.adapters.http_adapter import HttpStaticAdapter


def run(coro):
    return asyncio.run(coro)


class _FakeResp:
    def __init__(self):
        self.content = b"<html><head><title>T</title></head><body>hi</body></html>"
        self.status_code = 200
        self.headers = {"content-type": "text/html; charset=utf-8"}


class _FakeSession:
    def __init__(self):
        self.calls = []
        self.headers = {}

    def get(self, url, headers=None, timeout=None):
        self.calls.append({"url": url, "headers": dict(headers or {}),
                           "timeout": timeout})
        return _FakeResp()


def test_fetch_uses_chrome_impersonation_when_available(monkeypatch):
    """make_api_client() must actually be used — not just documented."""
    session = _FakeSession()
    monkeypatch.setattr(http_adapter, "is_curl_cffi_available", lambda: True)
    monkeypatch.setattr(
        http_adapter, "make_api_client",
        lambda user_agent=None, **kw: session,
    )
    result = run(HttpStaticAdapter().fetch("https://example.com"))
    assert result.success, result.error
    assert result.status_code == 200
    assert len(session.calls) == 1
    call = session.calls[0]
    assert call["url"] == "https://example.com"
    # UA pinned to the impersonation profile's Chrome UA (consistency rule).
    from core.stealth.tls_client import CHROME_UA
    assert call["headers"]["User-Agent"] == CHROME_UA


def test_fetch_falls_back_to_urllib_without_curl_cffi(monkeypatch):
    monkeypatch.setattr(http_adapter, "is_curl_cffi_available", lambda: False)

    html = b"<html><head><title>T</title></head><body>hi</body></html>"

    class _CM:
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def read(self):
            return html
        status = 200
        @property
        def headers(self):
            class _H:
                def get_content_charset(self):
                    return "utf-8"
            return _H()

    monkeypatch.setattr(
        "urllib.request.urlopen", lambda req, timeout=None: _CM()
    )
    result = run(HttpStaticAdapter().fetch("https://example.com"))
    assert result.success, result.error
    assert "hi" in result.text


def test_fetch_error_contract_preserved(monkeypatch):
    monkeypatch.setattr(http_adapter, "is_curl_cffi_available", lambda: False)
    def boom(req, timeout=None):
        raise OSError("no route")
    monkeypatch.setattr("urllib.request.urlopen", boom)
    result = run(HttpStaticAdapter().fetch("https://example.com"))
    assert not result.success
    assert "no route" in result.error
