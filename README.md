# Muse Browser Automation v2

## Install — one command

```powershell
# Copy-paste this into PowerShell:
irm https://raw.githubusercontent.com/rehantheorylab-pixel/muse-browser-automation/main/install/install-all.ps1 | iex
```

Paste → Enter → wait → copy the output → paste it into Muse.

That's it. The installer downloads everything, sets up all engines, and prints a summary you hand back to the AI so it knows what's live.

---

## What this is

The merged "best of both" repo: the multi-engine browser framework (a friend's design) combined with Rehan's research stack (custom Chrome extension, cookie sync, PC Agent, anti-detection architecture). One smart router picks the best browser engine per task — you just say what to do.

## The engines

| Engine | Best for | Port |
|---|---|---|
| Chrome (personal) | Your logged-in sessions — real Chrome, your profile, background tabs | 18010 |
| Playwright | Fast headless scraping, tests, DOM work | — |
| Moli | Speed + AI-optimized control lane | 9226 |
| Obscura | Stealth — bot-defended sites (Cloudflare, Datadome, Akamai) | 9222 |
| Camoufox | Anti-detect — fingerprint evasion at the C++ level | — |
| undetected-chromedriver | Stealth Chrome — patched driver, off-screen rendering, random debug port, disposable profiles | dynamic |
| Agent-Browser | Quick CLI-style page checks, fallback snapshots | — |
| CSI | Existing ximing/csi workflows (externally managed daemon) | 10088 |
| Lightpanda | Ultra-lightweight JS pages, low memory | 9223 |

Plus **PC Agent** (port 18011): Windows mouse/keyboard via SendInput — for anything outside the browser.
Plus **FlareSolverr** (port 8191): Cloudflare challenge solver — extracts `cf_clearance` + User-Agent (see `core/cloudflare/`).

## Smart router

`core/router.py` picks the engine automatically (Rehan's 3-tier architecture):

1. **Cloudflare/WAF challenge?** (`cloudflare_required=True`) → undetected-chromedriver → Obscura → Camoufox. (FlareSolverr solves separately via `core/cloudflare`.)
2. **Logged-in session needed?** (`session_required=True`) → Chrome (personal profile, your cookies).
3. **Stealth / anti-detect?** (`stealth_required=True`) → Obscura → Camoufox → undetected-chromedriver.
4. **Raw speed?** → Playwright → Moli → Chrome → Obscura.
5. **Need a JSON decision fast?** → jev ultrafast lane.
6. **Fallback:** Agent-Browser for simple snapshot reads.

Override any pick with `--engine <name>` (`chrome`, `playwright`, `moli`, `obscura`, `camoufox`, `undetected`, `agent-browser`, `csi`, `lightpanda`). Unavailable engine → router raises immediately, no silent fallback.

## Rehan's research

- **jev ultrafast lane** (`rehan/`) — JSON decision lane: screenshot + page map in, action JSON out. The fast path for agent step loops.
- **Cookie sync** (`tools/export_cookies.py`, `tools/sync_cookies_to_obscura.py`) — daily export of Chrome cookies into Obscura's profile, so stealth browsing stays logged in.
- **"Muse Automation" tab group rule** — automation tabs live in a dedicated Chrome tab group; nothing steals focus, nothing touches your working tabs.
- **PC Agent** (`pcagent/`) — deterministic Windows control program: mouse, keyboard, app launching via a small script language (`.pcs`), plus kill switches.
- **Daemon stability fixes** (`daemon/`) — crash supervisor with 5s auto-restart, per-request bearer auth, no-fail silent defaults.

Details: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md), [docs/CURRENT_ARCHITECTURE.md](docs/CURRENT_ARCHITECTURE.md), [docs/browsers.md](docs/browsers.md), [docs/troubleshooting.md](docs/troubleshooting.md).

## Manual setup (if the installer doesn't work)

1. Clone this repo.
2. Run `install/install.ps1` in PowerShell — same steps the one-liner runs, but visible.
3. Start the daemons (Chrome :18010, Obscura :9222, Moli :9226, PC Agent :18011).
4. Load `extension/` as an unpacked Chrome extension (Developer mode). Chrome automation needs this — see [instructions.md](instructions.md).
5. Full steps: [instructions.md](instructions.md).

## For AI agents

**Read [instructions.md](instructions.md) first.** It covers all three tools, their ports, setup order, and the hard rules (which engine for which job, never drive Chrome via CDP directly — use the extension).

---
MIT License. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
