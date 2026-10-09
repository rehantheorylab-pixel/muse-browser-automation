# `core/cloudflare/` — Challenge-solving integrations

Tier 3 of the browser-automation fetch strategy. Tiers 1–2 stay fully
inside this repo; tier 3 delegates challenge solving to an external
FlareSolverr instance and transports the solved session back.

## 3-tier architecture

| Tier | What | When |
|------|------|------|
| 1 — Plain HTTP | `requests`/`httpx` direct fetch | Default; cheapest and fastest. |
| 2 — Stealth browser | `UndetectedBackend` (undetected-chromedriver, off-screen, disposable profile) driven over CDP | Tier 1 blocked, fingerprint/JS checks present. |
| 3 — FlareSolverr | `FlareSolverrClient` → external FlareSolverr instance solves the challenge in *its* browser; solved cookies + user-agent returned | Tiers 1–2 hit a Cloudflare challenge page. |

Escalation is one-way and explicit: a tier is only used when the lower
tier demonstrably fails. Never pre-emptively solve.

## FlareSolverr setup

Run the official image (Docker required):

```bash
docker run -d \
  --name flaresolverr \
  -p 8191:8191 \
  -e LOG_LEVEL=info \
  --restart unless-stopped \
  ghcr.io/flaresolverr/flaresolverr:latest
```

Point the client at it (default is already `http://localhost:8191/v1`):

```python
from core.cloudflare import FlareSolverrClient

client = FlareSolverrClient()  # or FlareSolverrClient("http://host:8191/v1")
if client.is_available():
    cookies, user_agent = client.solve("https://example.com/", max_timeout=60000)
```

## Session-alignment rules

Solved cookies are bound to the session that solved them. Replaying them
any other way gets them invalidated:

1. **Always pair cookies with the returned user-agent.** Use
   `FlareSolverrClient.apply_to_session(cookies, user_agent)` to build the
   bundle — never mix solved cookies with a different UA string.
2. **Prefer the same egress IP** FlareSolverr used, when the target binds
   sessions to IP (many Cloudflare configurations do).
3. **Solve fresh per session.** `cf_clearance` tokens expire; do not cache
   them across runs.
4. **One site, one solve.** Do not reuse a solution across different
   domains — cookies are domain-scoped.

## TLS/JA3 caveat

FlareSolverr solves the challenge inside its own Chromium, so the solved
cookies carry that browser's TLS/JA3 fingerprint. Replaying those cookies
from a plain `requests`/`httpx` client presents a *different* JA3
fingerprint, and endpoints that enforce JA3 consistency will flag or
re-challenge the session.

Rule of thumb:

- Site does **not** enforce JA3 → exporting cookies via `apply_to_session`
  into `requests`/`httpx` is fine.
- Site **does** enforce JA3 (re-challenge on cookie replay) → do not
  export. Stay inside a browser instead: drive the page with
  `UndetectedBackend` (tier 2) and keep the whole session in the browser.

## Notes

- This package is integration glue only: it transports solutions, it does
  not implement challenge solving itself.
- `FlareSolverrClient` uses stdlib `urllib` only — no extra dependencies.
- The client never stores credentials; solved cookies live only in the
  returned dict and are the caller's responsibility.
