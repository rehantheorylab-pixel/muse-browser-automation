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
| Cloudflare / WAF challenge | patchright → undetected → Obscura/Camoufox | 2026 lead stealth engine first; FlareSolverr solves separately |
| Max stealth, anti-detect | patchright → Obscura → Camoufox → undetected | Fingerprint evasion at different layers |
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

### 6. patchright — LEAD stealth engine (`core/backends/patchright_backend.py`)
- **Role**: Compile-time AST-patched Playwright (Apache-2.0) — the 2026-recommended lead engine. Removes CDP-level automation tells from the driver itself instead of injecting JS on top.
- **Pros**: Never calls CDP `Runtime.enable` (the single most-documented automation signal — executes in isolated contexts instead); `Console.enable` leak removed; command-line flag hygiene (`--disable-blink-features=AutomationControlled` added; `--enable-automation` and friends removed); automation globals renamed (no `__playwright__binding__` signatures); init scripts via Playwright Routes; drop-in Playwright replacement; actively maintained, auto-tracks Playwright releases; prefers real installed Chrome over bundled Chromium (genuine GPU/plugin/font stack).
- **Cons**: Optional dependency (`pip install patchright`); Console API dead; TLS is stock Chromium (browser-consistent — fine).
- **Use when**: `stealth_required=True` or `cloudflare_required=True` — the router tries patchright first. Anywhere stock Playwright would be flagged.

### 7. undetected-chromedriver (`core/backends/undetected_backend.py`) — legacy fallback
- **Role**: Stealth Chromium via binary-level patching (Rehan's Tier 2).
- **Pros**: Real Chrome rendering (unlike Obscura/Moli); strips `navigator.webdriver` and CDP signatures; **off-screen rendering** (`--window-position=-32000,-32000`) instead of `--headless` so WebGL/Canvas/plugin fingerprints stay genuine; random debug port per run (no 9222 scan signature); disposable UUID profiles (no state bleed, auto-cleaned).
- **Cons**: Upstream effectively unmaintained in 2026 (author moved to nodriver); 2026 benchmarks show 0% success against strict Cloudflare configs — CDP-protocol/binary-signature detection bypasses its patches entirely. Kept as a fallback for basic-Selenium-detection sites only; the router now prefers patchright.
- **Use when**: patchright/Obscura/Camoufox unavailable; legacy flows. Now also applies the `core.stealth` fingerprint bundle + CDP Emulation overrides on start, plus `--disable-blink-features=AutomationControlled` and WebRTC IP-handling policy flags.

### 8. Agent-Browser CLI (`core/backends/agent_browser_backend.py`)
- **Role**: Command-line browser wrapper for AI-agent interaction.
- **Pros**: No persistent browser process; fast single-command page interaction and semantic snapshots; good fallback.
- **Cons**: Less control than a live CDP session; not for multi-step interactive flows.
- **Use when**: one-shot page reads/snapshots where spinning up a full browser is overkill.

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

## Anti-Detection Deep Layer (v2.2) — `core/stealth/`

Beyond per-engine patches, v2.2 adds a shared stealth layer applied to the
Chromium-family backends. Research synthesis (Oct 2026): Camoufox, Chameleon,
Brave, invisible_playwright, patchright, rebrowser.

### Fingerprint protection (`core/stealth/fingerprint.py`)
- **Seeded per-profile identity** (`FingerprintProfile`): one seed derives a
  coherent UA/platform/WebGL/GPU/fonts/timezone/locale/screen. Stable across
  sessions of the same profile, distinct across profiles. Never per-call
  randomness (fails a 4-line double-read check and reads as tampering).
- **JS injection bundle** via `Page.addScriptToEvaluateOnNewDocument`
  (before any page script), with Worker re-injection for blob workers:
  - **Canvas**: position-derived seeded LSB noise on `getImageData`;
    skips small buffers and few-color images (reference-probe guards).
  - **WebGL**: vendor/renderer spoof (e.g. `ANGLE (NVIDIA, ... RTX 3060 ...)`)
    via numeric `getParameter` constants; `WEBGL_debug_renderer_info`
    hidden; every wrapper keeps `[native code]` toString camouflage
    (Cloudflare Turnstile checks this literally).
  - **WebRTC**: `--force-webrtc-ip-handling-policy=disable_non_proxied_udp`
    launch flag + JS suppression of host/srflx ICE candidates (relay kept).
  - **Navigator**: coherent getters (`webdriver:false`, platform, languages,
    hardwareConcurrency, deviceMemory, maxTouchPoints, realistic plugins).
  - **WebGPU**: adapter descriptors hidden (1,095 measured signatures otherwise).
  - **Fonts**: bounded ±0.1px `measureText` offset; Local Font Access denied.
  - **Audio**: scalar spoofing only (sampleRate 48000, pinned baseLatency) —
    blanket buffer noise is OFF by default (measured 10x tampering increase).
- **CDP Emulation overrides** (below-JS, nothing to toString-check): UA +
  full `userAgentMetadata`, timezone, device metrics — from the SAME
  profile object, so values cohere.
- **Self-test harness**: `self_test_checks()` lists the gates a profile must
  pass (double-read, solid-fill, silence, tostring, worker-diff).

### Behavioral evasion (`core/stealth/behavior.py`)
All events go through CDP `Input.*` (`isTrusted:true`); the *fields* and
*timing* are humanized (seeded RNG, reproducible):
- **Mouse**: cubic Bezier with jittered control points, Fitts's-law duration,
  bell velocity profile, overshoot-and-settle on long moves, 60–300ms
  pre-click hesitation, off-center click point, correct `pressure`/`buttons`/
  `movementX/Y` fields, irregular timestamps.
- **Scroll**: notch bursts (8–120ms gaps), flick + momentum tail, lognormal
  reading pauses (0.8–4s), occasional reverse micro-scrolls — via CDP
  `mouseWheel`, never `window.scrollBy` loops.
- **Typing**: lognormal flight times (40–150ms), digraph speedup, 2–5%
  typo + backspace corrections, ~28ms per-key floor, via
  `Input.dispatchKeyEvent` (never `insertText`).
- **Cadence**: Gaussian gaps between actions (never fixed intervals).

### TLS / network layer (`core/stealth/tls_client.py`)
- Tier-1 direct HTTP goes through **curl_cffi** (MIT, `impersonate="chrome"`
  rolling alias): genuine Chrome JA4, HTTP/2 SETTINGS/pseudo-header order,
  per-request-type header ordering, per-cookie `cookie` headers.
- Browser-driven (CDP) traffic already has genuine TLS — untouched.
- Rules: keep UA/TLS consistent; pin `impersonate_os="windows"` to match the
  real egress stack; refresh aliases regularly (a ~6-month-stale pinned
  fingerprint gets blocked identically across libraries).
- **Residuals** (documented, not fixable from JS/CDP): JA4/TLS for non-browser
  paths without curl_cffi, TCP/IP OS fingerprinting (kernel-level), real-GPU
  rasterization differences (need C++ engine patches).

### What changed vs v2.1 (why this is "very, very good" now)
| Before (v2.1) | After (v2.2) |
|---|---|
| undetected-chromedriver = lead engine (0% vs strict CF in 2026) | **patchright** = lead engine (AST-patched driver, active) |
| Fingerprint = binary patching only | Seeded JS bundle + CDP Emulation + probe guards + toString masking |
| Mouse = Playwright click (teleport) | Bezier + Fitts + hesitation via CDP trusted events |
| Typing = `insertText` | Keystroke dynamics via `dispatchKeyEvent` |
| Scroll = fixed-step loops | Notch bursts + momentum + reading pauses |
| TLS = documented caveat | curl_cffi Chrome impersonation for Tier-1 |
| FlareSolverr = primary CF path | Demoted (documented Turnstile timeouts); patchright leads |

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
