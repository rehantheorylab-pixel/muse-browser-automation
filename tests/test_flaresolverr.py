"""Tests for core/cloudflare/flaresolverr.py — mocked HTTP only.

No live FlareSolverr, no live sites, no Cloudflare challenges.
urllib.request.urlopen is stubbed to return canned JSON payloads.
"""
import json
import urllib.error
from unittest import mock

import pytest

from core.cloudflare import FlareSolverrClient
from core.cloudflare.flaresolverr import FlareSolverrClient as DirectClient

ENDPOINT = "http://localhost:8191/v1"
TEST_URL = "https://example.com/"
TEST_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"

CANNED_OK = {
    "status": "ok",
    "message": "Challenge solved!",
    "solution": {
        "url": TEST_URL,
        "status": 200,
        "cookies": [
            {"name": "cf_clearance", "value": "abc123"},
            {"name": "session", "value": "xyz789"},
        ],
        "userAgent": TEST_UA,
    },
}


def _canned_response(payload):
    """A urlopen() stub returning a context manager with canned JSON bytes."""
    resp = mock.MagicMock()
    resp.read.return_value = json.dumps(payload).encode("utf-8")
    cm = mock.MagicMock()
    cm.__enter__.return_value = resp
    return cm


def _patch_urlopen(return_value=None, side_effect=None):
    return mock.patch(
        "core.cloudflare.flaresolverr.urllib.request.urlopen",
        return_value=return_value,
        side_effect=side_effect,
    )


# ── solve() ──────────────────────────────────────────────────────────────

def test_solve_parses_canned_response():
    client = FlareSolverrClient(ENDPOINT)
    with _patch_urlopen(return_value=_canned_response(CANNED_OK)) as m_urlopen:
        cookies, user_agent = client.solve(TEST_URL, max_timeout=60000)

    assert cookies == {"cf_clearance": "abc123", "session": "xyz789"}
    assert user_agent == TEST_UA

    # Verify the exact request payload per Rehan's spec.
    (req,), kwargs = m_urlopen.call_args
    assert req.full_url == ENDPOINT
    assert json.loads(req.data.decode("utf-8")) == {
        "cmd": "request.get",
        "url": TEST_URL,
        "maxTimeout": 60000,
    }


def test_solve_failure_status_raises():
    client = FlareSolverrClient(ENDPOINT)
    payload = {"status": "error", "message": "Cloudflare challenge not solved"}
    with _patch_urlopen(return_value=_canned_response(payload)):
        with pytest.raises(RuntimeError, match="failed to solve"):
            client.solve(TEST_URL)


def test_solve_incomplete_solution_raises():
    client = FlareSolverrClient(ENDPOINT)
    payload = {"status": "ok", "solution": {"cookies": [], "userAgent": ""}}
    with _patch_urlopen(return_value=_canned_response(payload)):
        with pytest.raises(RuntimeError, match="incomplete solution"):
            client.solve(TEST_URL)


def test_solve_connection_error_raises():
    client = FlareSolverrClient(ENDPOINT)
    with _patch_urlopen(
        side_effect=urllib.error.URLError("connection refused")
    ):
        with pytest.raises(RuntimeError, match="FlareSolverr request failed"):
            client.solve(TEST_URL)


def test_solve_skips_malformed_cookie_entries():
    client = FlareSolverrClient(ENDPOINT)
    payload = {
        "status": "ok",
        "solution": {
            "cookies": [
                {"name": "good", "value": "1"},
                {"value": "no-name"},  # malformed: no name key
                "not-a-dict",
            ],
            "userAgent": TEST_UA,
        },
    }
    with _patch_urlopen(return_value=_canned_response(payload)):
        cookies, _ = client.solve(TEST_URL)
    assert cookies == {"good": "1"}


# ── is_available() ───────────────────────────────────────────────────────

def test_is_available_true_when_endpoint_responds():
    client = FlareSolverrClient(ENDPOINT)
    with _patch_urlopen(return_value=_canned_response({})):
        assert client.is_available() is True


def test_is_available_true_on_http_error_status():
    # A bare GET is rejected by the API, but any HTTP response proves it is up.
    client = FlareSolverrClient(ENDPOINT)
    err = urllib.error.HTTPError(ENDPOINT, 404, "Not Found", {}, None)
    with _patch_urlopen(side_effect=err):
        assert client.is_available() is True


def test_is_available_false_on_connection_refused():
    client = FlareSolverrClient(ENDPOINT)
    with _patch_urlopen(
        side_effect=urllib.error.URLError("[Errno 111] Connection refused")
    ):
        assert client.is_available() is False


# ── apply_to_session() ───────────────────────────────────────────────────

def test_apply_to_session_returns_aligned_bundle():
    cookies = {"cf_clearance": "abc123"}
    bundle = FlareSolverrClient.apply_to_session(cookies, TEST_UA)
    assert bundle == {
        "cookies": {"cf_clearance": "abc123"},
        "headers": {"User-Agent": TEST_UA},
    }


def test_apply_to_session_copies_cookies():
    cookies = {"a": "1"}
    bundle = FlareSolverrClient.apply_to_session(cookies, TEST_UA)
    bundle["cookies"]["a"] = "mutated"
    assert cookies == {"a": "1"}  # caller's dict untouched


def test_apply_to_session_warns_about_tls_ja3():
    doc = FlareSolverrClient.apply_to_session.__doc__
    assert "JA3" in doc
    assert "stay inside" in doc or "browser" in doc.lower()


# ── constructor ──────────────────────────────────────────────────────────

def test_default_endpoint():
    assert FlareSolverrClient().endpoint == "http://localhost:8191/v1"


def test_endpoint_trailing_slash_stripped():
    assert FlareSolverrClient("http://localhost:8191/v1/").endpoint == ENDPOINT


def test_package_exports_client():
    assert DirectClient is FlareSolverrClient
