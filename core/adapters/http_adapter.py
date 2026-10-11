"""core/adapters/http_adapter.py — Tier 0 Fast Static Fetcher (<20ms latency).

Direct-HTTP requests that bypass the browser go through
``core.stealth.tls_client.make_api_client`` (curl_cffi Chrome
impersonation) whenever curl_cffi is installed — a Chrome UA over
Python's stdlib TLS is a cross-checkable bot signal. Falls back to
urllib when curl_cffi is unavailable.
"""

from __future__ import annotations

import asyncio
import logging
import time
import urllib.request
from typing import Any, Dict, Optional, Tuple

from core.adapters.base import BaseToolAdapter
from core.fetch.extractor import SimpleContentExtractor
from core.fetch.normalizer import FetchResult
from core.stealth.tls_client import (
    CHROME_UA,
    is_curl_cffi_available,
    make_api_client,
)

logger = logging.getLogger("muse.adapters.http")


class HttpStaticAdapter(BaseToolAdapter):
    """Ultra-fast, zero-overhead static HTTP fetcher."""

    name = "http_static"

    async def fetch(self, url: str, options: Optional[Dict[str, Any]] = None) -> FetchResult:
        opts = options or {}
        timeout = float(opts.get("timeout", 10.0))
        # Pin the UA to the impersonation profile's Chrome UA unless the
        # caller overrides it (UA/TLS consistency rule in tls_client).
        user_agent = opts.get("user_agent") or CHROME_UA
        headers = {
            "User-Agent": user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        }

        t0 = time.perf_counter()
        try:
            if is_curl_cffi_available():
                raw, status_code, encoding = await self._fetch_stealth(
                    url, headers, timeout
                )
            else:
                raw, status_code, encoding = await asyncio.to_thread(
                    self._fetch_urllib, url, headers, timeout
                )
            html = raw.decode(encoding, errors="replace")

            t_fetch = (time.perf_counter() - t0) * 1000.0

            # Extract structured text and links
            extracted = SimpleContentExtractor.extract(html, base_url=url)
            t_total = (time.perf_counter() - t0) * 1000.0

            return FetchResult(
                success=True,
                tool=self.name,
                url=url,
                title=extracted["title"],
                content=extracted["text"],
                text=extracted["text"],
                html=html if opts.get("return_html") else None,
                links=extracted["links"],
                images=extracted["images"],
                timing={"total_ms": round(t_total, 2), "fetch_ms": round(t_fetch, 2)},
                status_code=status_code,
            )
        except Exception as exc:
            t_total = (time.perf_counter() - t0) * 1000.0
            return FetchResult(
                success=False,
                tool=self.name,
                url=url,
                error=str(exc),
                timing={"total_ms": round(t_total, 2)},
            )

    async def _fetch_stealth(
        self, url: str, headers: Dict[str, str], timeout: float
    ) -> Tuple[bytes, int, str]:
        """Fetch with Chrome TLS/JA4 + header ordering via curl_cffi."""
        session = make_api_client(user_agent=headers["User-Agent"])

        def _get() -> Tuple[bytes, int, str]:
            resp = session.get(url, headers=headers, timeout=timeout)
            raw = resp.content
            status_code = resp.status_code
            ctype = resp.headers.get("content-type", "")
            encoding = "utf-8"
            if "charset=" in ctype:
                encoding = ctype.split("charset=")[-1].split(";")[0].strip() or "utf-8"
            return raw, status_code, encoding

        # curl_cffi is blocking: keep it off the event loop.
        return await asyncio.to_thread(_get)

    @staticmethod
    def _fetch_urllib(
        url: str, headers: Dict[str, str], timeout: float
    ) -> Tuple[bytes, int, str]:
        """Plain urllib fallback (no TLS impersonation)."""
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            status_code = resp.status
            encoding = resp.headers.get_content_charset() or "utf-8"
        return raw, status_code, encoding
