"""_auth.py — shared bearer-token auth for the local muse-browser daemons.

The HTTP daemons (simpled.py, mused.py) expose powerful browser-control tools
on 127.0.0.1:18010, including full cookie-jar export. Loopback-only is not
enough: any local process could drive them. So POST /tool requires a bearer
token.

The token is auto-generated on first daemon start and stored at
~/.muse-browser-mcp/daemon_token (user-private). Local clients read the same
file and send it as an Authorization header. No configuration needed.
"""
import hmac
import os
import secrets

TOKEN_DIR = os.path.join(os.path.expanduser("~"), "muse-browser-mcp")
TOKEN_PATH = os.path.join(TOKEN_DIR, "daemon_token")


def get_or_create_token():
    """Return the daemon bearer token, creating it (0600) on first use."""
    try:
        with open(TOKEN_PATH, "r", encoding="utf-8") as f:
            tok = f.read().strip()
            if tok:
                return tok
    except OSError:
        pass
    os.makedirs(TOKEN_DIR, exist_ok=True)
    tok = secrets.token_urlsafe(32)
    try:
        fd = os.open(TOKEN_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(tok)
    except OSError:
        # Fallback (e.g. Windows ACL quirks): plain write. The profile dir
        # itself is user-private on a standard setup.
        with open(TOKEN_PATH, "w", encoding="utf-8") as f:
            f.write(tok)
    return tok


def is_authorized(headers):
    """Check an Authorization header against the daemon token.

    `headers` may be an http.server headers mapping or a plain dict.
    Uses constant-time comparison.
    """
    auth = headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return False
    try:
        expected = get_or_create_token()
    except OSError:
        return False
    return hmac.compare_digest(auth[7:].strip(), expected)
