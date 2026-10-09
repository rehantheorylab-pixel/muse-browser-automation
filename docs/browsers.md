# Browser Engines & Runtime Architecture

## Overview

Muse Browser Automation v2 integrates multiple specialized browser engines into a
unified lifecycle pool behind one smart router (`core/router.py`). Different tasks
require different trade-offs: speed, stealth, JavaScript execution depth, or
authenticated user sessions. The router picks the best engine per task — each
tool keeps its pros, and the router avoids their cons.

**Routing cheat sheet** (see `core/router.py::resolve_backend`):

| Task need | Router picks | Why |
|---|---|---|
| Logged-in personal session | Chrome (personal profile) | Real cookies, extensions, SSO state |
| Cloudflare / WAF challenge | FlareSolverr → undetected-chromedriver | Challenge solver + patched stealth driver |
| Max stealth, anti-detect | Obscura / Camoufox / undetected | Fingerprint evasion at different layers |
| Raw speed, isolated pages | Playwright / Moli | Fast startup, headless, disposable |
| Ultra-lightweight fetch | Lightpanda | ~10x lower memory than Chromium |
| CLI-style single shots | Agent-Browser | No persistent browser needed |
| Existing CSI workflows | CSI | Reuses the ximing/csi bridge the operator already runs |

---

## Supported Browser Runtimes

### 1. Google Chrome — Personal Profile (`core/backends/chrome_backend.py`)
- **Role**: Authenticated live browser using the operator's real Chrome profile.
- **Pros**: Real logged-in sessions (Gmail, SaaS, social); extensions available; zero login friction; the "Muse Automation" tab group keeps automation tabs visible and separate.
- **Cons**: Tied to the operator's machine and profile; automation is visible in the tab strip; a Chrome restart is needed to hot-load extension updates (service workers don't hot-reload).
- **Use when**: `session_required=True`, or the task must act as the operator (posting, authenticated portals, Google Workspace).
- **Avoid when**: the operator is sensitive about tab clutter, or the task needs a clean isolated session.

### 2. Playwright Chromium (`core/backends/playwright_backend.py`)
- **Role**: General-purpose isolated automation engine; the default fast path.
- **Pros**: Fast startup (~800ms); robust DOM APIs; full CDP event streaming; disposable contexts; good screenshots.
- **Cons**: Stock Chromium fingerprint is detectable by aggressive bot defenses; not for Cloudflare-challenged targets.
- **Use when**: speed matters and the target has no serious bot protection (scraping, screenshots, form automation, test verification).

### 3. Moli (`core/backends/moli_backend.py`)
- **Role**: Rust browser engine (CDP on :9226), Playwright-driven.
- **Pros**: Very fast, low overhead; independent engine (not Chromium) so Chromium-specific fingerprints don't apply.
- **Cons**: From-scratch engine — heavy web apps (Sheets, complex SPAs) may render differently than real Chrome; smaller ecosystem.
- **Use when**: raw speed on simple/medium pages; a non-Chromium fingerprint is useful.

### 4. Obscura (`core/backends/obscura_backend.py`)
- **Role**: Stealth browser for bot-protected targets.
- **Pros**: Native fingerprint randomization; TLS fingerprint masking; purpose-built for anti-bot evasion.
- **Cons**: From-scratch Rust engine (not Chromium) — pixel-perfect real-Chrome work still belongs to Chrome; single maintainer upstream; no auto-start (operator starts it manually).
- **Use when**: `stealth_required=True` — Cloudflare/Akamai/DataDome-protected targets where Playwright gets flagged.

### 5. Camoufox (`core/backends/camoufox_backend.py`)
- **Role**: Stealth Firefox-based browser with fingerprint evasion at the C++ level.
- **Pros**: Evasion below the JavaScript layer (harder to detect than JS patches); real Gecko rendering for Firefox-targeted testing.
- **Cons**: Heavier download (fetches its own browser build); Firefox-only rendering quirks.
- **Use when**: maximum stealth and the JS-layer patches of undetected-chromedriver aren't enough; cross-engine verification.

### 6. undetected-chromedriver (`core/backends/undetected_backend.py`)
- **Role**: Stealth Chromium via binary-level patching (Rehan's Tier 2).
- **Pros**: Real Chrome rendering (unlike Obscura/Moli); strips `navigator.webdriver` and CDP signatures; **off-screen rendering** (`--window-position=-32000,-32000`) instead of `--headless` so WebGL/Canvas/plugin fingerprints stay genuine; random debug port per run (no 9222 scan signature); disposable UUID profiles (no state bleed, auto-cleaned).
- **Cons**: Still Chromium under the hood — determined fingerprinting can adapt; needs `undetected_chromedriver` + chromedriver binary installed.
- **Use when**: Cloudflare Turnstile / WAF targets where genuine Chrome rendering is required but the stock driver signature would be flagged. Pairs with FlareSolverr (Tier 3) for full challenge flows.

### 7. Agent-Browser CLI (`core/backends/agent_browser_backend.py`)
- **Role**: Command-line browser wrapper for AI-agent interaction.
- **Pros**: No persistent browser process; fast single-command page interaction and semantic snapshots; good fallback.
- **Cons**: Less control than a live CDP session; not for multi-step interactive flows.
- **Use when**: one-shot page reads/snapshots where spinning up a full browser is overkill.

### 8. CSI (`core/backends/csi_backend.py`)
- **Role**: ximing/csi bridge the operator already runs — reuse existing workflows.
- **Pros**: Zero new setup if CSI is already running; familiar tool surface.
- **Cons**: Externally managed (the router can't start it); feature surface limited to what CSI exposes.
- **Use when**: the operator's existing CSI flows; `preference="csi"`.

### 9. Lightpanda (`core/backends/lightpanda_backend.py`)
- **Role**: Ultra-lightweight headless JavaScript browser (Zig), CDP on :9223.
- **Pros**: ~10x lower memory than Chromium; near-instant startup; exposes a Playwright/Puppeteer-compatible CDP server — full backend implemented, detection-only (no downloads).
- **Cons**: Limited rendering fidelity vs full Chromium; AGPL-3.0 licensed (per the standing licensing rule: keep any modified distribution open with a notice crediting the original).
- **Use when**: massive parallel lightweight fetches where Chromium's memory cost dominates.

### 10. CSI (`core/backends/csi_backend.py`)
- **Role**: ximing/csi bridge — reuse the operator's existing CSI daemon (default `127.0.0.1:10088`).
- **Pros**: Zero new setup if CSI already runs; 21-tool surface (tabs, CDP passthrough, cookies); stdlib-only client.
- **Cons**: Externally managed (router can't start it); tab targeting emulated via URL lookup; auth via `CSI_API_KEY` env.
- **Use when**: existing CSI workflows; `preference="csi"`.

---

## Challenge & Session Layer

### FlareSolverr (`core/cloudflare/`)
- **Role**: Tier 3 challenge solver — isolated microservice (`http://localhost:8191/v1`) that navigates Cloudflare's "Checking your browser…" shield and extracts `cf_clearance` + the solving User-Agent.
- **Session alignment rule**: when moving solved cookies into `requests`/`httpx`, the `User-Agent` header **must exactly match** the solver's UA. If the endpoint enforces TLS/JA3 fingerprint matching, pure-Python HTTP will fail despite valid cookies — keep all requests inside the stealth browser instead.
- **Setup**: `docker run -d -p 8191:8191 ghcr.io/flaresolverr/flaresolverr:latest` (see `core/cloudflare/README.md`).
- **Note**: integration code only — no live challenge testing is performed by this repo's test suite.

### Cookie Sync (Chrome → Obscura)
- `tools/export_cookies.py` exports the Chrome jar via the extension's self-contained `cookies.export_cdp` (one throwaway background tab, closed immediately — no profile spam).
- `tools/sync_cookies_to_obscura.py` merges into Obscura's `cookies.json`.
- Scheduled daily at 04:00 via the `MuseCookieExport` task (created by the installer).

---

## Connection & Pool Management

- **Connection Reuse**: the runtime pool reuses running browser instances for sequential tasks within the same profile.
- **Graceful Cleanup**: timeout + process lifecycle management terminates orphan browsers; undetected-chromedriver's disposable profiles are deleted on `stop()`.
- **Headless vs Headed**: stealth backends default to headed-but-hidden strategies (off-screen window) rather than `--headless` to preserve genuine fingerprints.
- **Single-port architecture**: all external/AI-agent traffic flows through `127.0.0.1:18010`; remote access via ngrok (see installer step 10).
