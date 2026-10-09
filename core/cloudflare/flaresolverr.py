"""core/cloudflare/flaresolverr.py — HTTP client for a FlareSolverr instance.

The challenge is solved inside FlareSolverr's own browser; this class only
transports the solved cookies and user-agent back to the caller. Stdlib
only (urllib), no extra dependencies.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Dict, Tuple


class FlareSolverrClient:
    """Thin client for FlareSolverr's ``/v1`` API.

    Parameters
    ----------
    endpoint:
        Base URL of the FlareSolverr API, e.g. ``http://localhost:8191/v1``.
    """

    def __init__(self, endpoint: str = "http://localhost:8191/v1"):
        self.endpoint = endpoint.rstrip("/")

    def _post(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            self.endpoint,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.URLError as e:
            raise RuntimeError(f"FlareSolverr request failed: {e}") from e

    def is_available(self) -> bool:
        """True when the FlareSolverr endpoint answers at all.

        A bare GET is rejected by the API (it only accepts POST), but any
        HTTP response — even an error status — proves the server is up.
        Connection refused / timeout / DNS failure -> False.
        """
        try:
            req = urllib.request.Request(self.endpoint, method="GET")
            with urllib.request.urlopen(req, timeout=5):
                return True
        except urllib.error.HTTPError:
            # Server answered (e.g. 404 on a bare GET): it is up.
            return True
        except Exception:
            return False

    def solve(self, url: str, max_timeout: int = 60000) -> Tuple[Dict[str, str], str]:
        """Solve the challenge for ``url`` via FlareSolverr.

        Sends ``{"cmd": "request.get", "url": url, "maxTimeout": max_timeout}``
        and returns ``(cookies_dict, user_agent)`` from the solution.

        Raises
        ------
        RuntimeError
            If FlareSolverr is unreachable, returns a non-ok status, or the
            solution has no usable cookies/user-agent.
        """
        payload = {"cmd": "request.get", "url": url, "maxTimeout": max_timeout}
        result = self._post(payload)
        if result.get("status") != "ok":
            raise RuntimeError(
                f"FlareSolverr failed to solve {url!r}: "
                f"{result.get('message', result)}"
            )
        solution = result.get("solution") or {}
        cookies = {
            c["name"]: c["value"]
            for c in solution.get("cookies", [])
            if isinstance(c, dict) and "name" in c
        }
        user_agent = solution.get("userAgent", "")
        if not cookies or not user_agent:
            raise RuntimeError(
                f"FlareSolverr returned an incomplete solution for {url!r}"
            )
        return cookies, user_agent

    @staticmethod
    def apply_to_session(
        cookies: Dict[str, str], user_agent: str
    ) -> Dict[str, Any]:
        """Build a requests/httpx-compatible session-alignment bundle.

        Returns ``{"cookies": {...}, "headers": {"User-Agent": user_agent}}``,
        ready to pass as ``cookies=`` / ``headers=`` to requests or httpx.

        .. warning::
            **Session-alignment rule** — the solved cookies belong to the
            browser session (user-agent, egress IP, TLS fingerprint) that
            FlareSolverr used to solve the challenge. Replay them only with
            the matching ``User-Agent`` (and the same egress IP when the
            target binds sessions to it), or the site may invalidate them.

        .. warning::
            **TLS/JA3 fingerprint mismatch** — cookies solved by
            FlareSolverr's Chromium carry that browser's TLS/JA3 fingerprint.
            Replaying them from a plain ``requests``/``httpx`` client
            presents a *different* JA3 fingerprint, and sites that enforce
            JA3 consistency will flag or re-challenge the session. If the
            endpoint enforces JA3, do NOT export cookies — stay inside a
            browser (e.g. :class:`UndetectedBackend`) and continue the
            session there instead.
        """
        return {
            "cookies": dict(cookies),
            "headers": {"User-Agent": user_agent},
        }
