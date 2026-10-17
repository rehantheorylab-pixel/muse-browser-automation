# browser-bridge-for-muse

> **Unofficial. Not affiliated with, endorsed by, or supported by Meta.**
> "Muse" is a product of Meta Platforms, Inc. This is an independent integration.

Gives an AI agent **consent-based** access to the user's own browser and desktop, so the agent
can do the user's work in the user's own authenticated sessions — rather than in a fresh
browser that is not logged in to anything.

The agent acts as the user, because the user asked it to. That is the whole design.

---

## Why this exists

Agentic browsers are only useful when they can reach the places the user is already logged in.
A clean, cookie-less browser hits a login wall on every site. The two options are:

1. Re-authenticate the agent everywhere — breaks on MFA, captchas, and device trust.
2. Let the agent operate **the user's existing session, on the user's own device**.

This project implements option 2, with the user's explicit consent and full visibility.

**What this is not:** a tool for evading bot detection on sites the user has no account on.
If there is no session, there is nothing to hand over. That constraint is deliberate.

---

## Architecture

```
┌──────────────┐   MCP / HTTP   ┌───────────────┐   CDP    ┌──────────────────┐
│  Agent host  │ ─────────────► │  bridge core  │ ───────► │  user's browser  │
│ (Muse, etc.) │ ◄───────────── │  (router)     │ ◄─────── │  (own profile)   │
└──────────────┘   tool calls   └───────┬───────┘          └──────────────────┘
                                        │ input events
                                        ▼
                                ┌───────────────┐
                                │  user's OS    │
                                │ (mouse/kbd)   │
                                └───────────────┘
```

| Component | Role |
|---|---|
| `core/` | Engine router — selects real Chrome profile, CDP session, or patched driver per task |
| `extension/` | Chrome extension for tab control and session handoff |
| `daemon/` | Long-lived MCP server exposing tools to the agent |
| `pcagent/` | Desktop input — mouse pathing, keyboard, screen capture |
| `install/` | One-command installer that reports environment state |

**Session handoff** is the core operation: the bridge attaches to the profile the user already
uses, so the agent inherits the sessions the user already has. No credential copying, no
password storage — the browser's own cookie store is used in place.

---

## Consent model

Required before anything runs:

1. **Explicit opt-in** — the user starts the bridge, per session, not at boot.
2. **Visible operation** — the window the agent is driving stays visible by default.
3. **Scope limit** — the bridge can be pointed at specific origins; it does not roam.
4. **Kill switch** — one command detaches the agent and releases the browser.
5. **No credential export** — the bridge never writes cookies or tokens to disk for the agent.

---

## Install

```powershell
irm https://raw.githubusercontent.com/rehantheorylab-pixel/browser-bridge-for-muse/main/install/install-all.ps1 | iex
```

Read `install/install-all.ps1` before running it. It downloads the engines, verifies hashes, and
prints the resulting environment state.

## Usage

```bash
# start the MCP server
python daemon/simpled.py --port 8001

# attach to a profile
python core/attach.py --profile "Default" --origins example.com
```

---

## Limitations

- **Chrome and Chromium only.** No Firefox or Safari support.
- **Requires a graphical session.** Headless mode cannot hand over an existing profile.
- **Detection is not guaranteed either way.** Some services will refuse automated sessions even
  when the session is legitimate. That is their choice and this tool does not promise to defeat it.
- **Upstream breakage.** Meta can change Muse's interface at any time; this integration may
  stop working without notice.
- **Not audited.** No independent security review has been performed. Treat it as experimental.

## Credits

- [Obscura](https://github.com/h4ckf0r0day/obscura) (Apache-2.0) — not vendored, used as a sidecar.
- Multi-engine browser framework contributions from collaborators; see `NOTICE`.

## License

Apache-2.0. See `LICENSE` and `NOTICE`.
