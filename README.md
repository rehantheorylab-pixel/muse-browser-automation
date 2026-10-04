# muse-browser-automation

A complete automation stack for Windows: browser control (Chrome + Obscura) and full PC control — with **paste-ready setup instructions** so anyone's AI agent can install everything by itself.

## The 30-second version

1. Give your AI agent the repo link + tell it to read `instructions.md`.
2. The agent installs everything through the terminal by itself.
3. You do one 4-click step in Chrome (load the unpacked extension).
4. The agent proves it works and says **AUTOMATION READY**.

## The Three Tools

| # | Tool | What | Port |
|---|------|------|------|
| 1 | **Chrome Automation** | Custom extension drives your Chrome (background tabs, no focus steal) | 18010 |
| 2 | **Obscura Automation** | Separate stealth browser for agent tasks | 9222+ |
| 3 | **PC Agent** | Windows-level mouse/keyboard (undetectable, fastest) | 18011 |

**Full docs:** `instructions.md` — complete setup guide for AI agents.
**Quick ref:** `commands.md` — every command with examples.
**Setup prompt:** `SETUP_PROMPT.md` — paste-ready prompt.

## What's inside

| Path | What it is |
|---|---|
| `extension/` | Chrome MV3 extension — drives background tabs via CDP (never steals focus). |
| `daemon/` | `simpled.py` — HTTP daemon `127.0.0.1:18010`. `mcp_server.py` — MCP front-end. |
| `obscura/` | `obscura_helper.py`, `profiles.py` — Obscura browser automation. |
| `pcagent/` | `pc_agent.py` — Windows-level PC control daemon `127.0.0.1:18011`. `apps.json` — app registry. `pcscript.py` — automation scripts. |
| `tools/` | `export_cookies.py` — daily cookie export. |
| `install/` | `install.ps1` — Windows installer. |
| `docs/` | `ARCHITECTURE.md` — how pieces fit. |

## Two browsers, on purpose

- **Your Chrome** (with the extension) — your real logged-in sessions. The
  agent only touches it when you explicitly ask.
- **Obscura** (separate stealth browser) — the agent's default browser, so
  automation never disturbs your tabs. Cookie-synced from Chrome daily.

## Security

- Everything listens on `127.0.0.1` only. No inbound network exposure.
- No credentials, tokens, or keys are stored in this repo. The installer asks
  for nothing except, optionally, your ngrok authtoken for remote access.
- The extension requests `debugger` + host permissions because that is
  literally its job (driving tabs); the source is right here in
  `extension/background.js` — read it before loading.

## License

Apache 2.0 — see `LICENSE` and `NOTICE`. Obscura is a separate upstream
project ([h4ckf0r0day/obscura](https://github.com/h4ckf0r0day/obscura),
Apache 2.0) and is not included here.
