"""core.stealth.tls_client — Chrome-impersonating HTTP client for Tier-1 API calls.

Problem: Python's stdlib ssl/OpenSSL produces a distinct TLS ClientHello
(no GREASE, no ALPS, different extension order). ``requests``/``httpx``
therefore hash to a Python JA4 (``t13d1713h1_...``) while real Chrome
hashes to ``t13d1516h2_...``. Vendors cross-check UA vs TLS fingerprint,
so "Chrome UA + Python TLS" is a higher-confidence bot signal than a
consistent Python/Python identity.

Solution: route direct HTTP (Tier-1) through ``curl_cffi`` (MIT), a
Python binding to curl-impersonate (patched BoringSSL in patched
libcurl). ``impersonate="chrome"`` uses a rolling alias tracking the
newest Chrome. This also fixes HTTP/2 fingerprinting (SETTINGS frames,
pseudo-header order, per-cookie headers) and header ordering from the
same profile.

Rules:
  - Browser-driven traffic (CDP) already has genuine TLS: untouched.
  - Only Tier-1 direct-HTTP requests go through this client.
  - Keep UA/TLS consistent: the session UA must match the impersonation.
  - Use rolling aliases; a ~6-month-stale pinned fingerprint gets blocked
    identically across libraries (freshness beats library choice).
  - Pin impersonate_os="windows" so TLS/headers don't contradict the
    real Windows egress stack.

curl_cffi is an optional dependency: pip install curl-cffi
"""

from __future__ import annotations

from typing import Any, Dict, Optional

# Rolling Chrome UA kept in sync with the fingerprint presets.
CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

# Header order captured from real Chrome navigation requests.
# NOTE: order varies by request type (nav vs XHR vs subresource) and
# Chrome 148+ reportedly moved sec-ch-ua* after sec-fetch-*. Re-capture
# quarterly; prefer curl_cffi's profile ordering when available.
HEADER_ORDER_NAV = [
    ":method", ":authority", ":scheme", ":path",
    "sec-ch-ua", "sec-ch-ua-mobile", "sec-ch-ua-platform",
    "upgrade-insecure-requests",
    "user-agent",
    "accept",
    "sec-fetch-site", "sec-fetch-mode", "sec-fetch-user", "sec-fetch-dest",
    "accept-encoding", "accept-language",
    "priority",
]

HEADER_ORDER_XHR = [
    ":method", ":authority", ":scheme", ":path",
    "sec-ch-ua", "sec-ch-ua-mobile", "sec-ch-ua-platform",
    "user-agent",
    "accept",
    "sec-fetch-site", "sec-fetch-mode", "sec-fetch-dest",
    "accept-encoding", "accept-language",
]


def is_curl_cffi_available() -> bool:
    try:
        import curl_cffi  # noqa: F401
        return True
    except ImportError:
        return False


def make_api_client(
    user_agent: Optional[str] = None,
    impersonate: str = "chrome",
    impersonate_os: str = "windows",
    timeout: float = 30.0,
) -> Any:
    """Build a Chrome-impersonating session for Tier-1 direct HTTP.

    Returns a ``curl_cffi.requests.Session`` with:
      - TLS/JA4 = real Chrome (via impersonate profile)
      - HTTP/2 SETTINGS/pseudo-header order = Chrome
      - UA header pinned to ``user_agent`` (defaults to CHROME_UA)

    Raises RuntimeError if curl_cffi is not installed.
    """
    try:
        from curl_cffi import requests as creq
    except ImportError as e:
        raise RuntimeError(
            "curl_cffi is not installed (pip install curl-cffi). "
            "Tier-1 stealth HTTP requires it."
        ) from e

    kwargs: Dict[str, Any] = {"impersonate": impersonate, "timeout": timeout}
    session = creq.Session(**kwargs)
    session.headers.update({"User-Agent": user_agent or CHROME_UA})
    return session


def check_ua_tls_consistency(user_agent: str, impersonate: str = "chrome") -> bool:
    """Sanity check: UA claims Chrome while impersonation is Chrome-family.

    A mismatch here ("Chrome UA + safari impersonation") is exactly the
    cross-check vendors run. Returns True when consistent.
    """
    ua = user_agent.lower()
    claims_chrome = "chrome" in ua and "edg" not in ua and "opr" not in ua
    imp_is_chrome = impersonate.lower().startswith("chrome")
    return claims_chrome == imp_is_chrome
