"""Sync Chrome cookies to Obscura (one-shot, safe).

Reads Downloads/cookies-export.txt (Netscape format, from export_cookies.py),
converts to Obscura's cookies.json format, and merges into the profile.
Expires must be INT (Obscura requirement).

Usage: python sync_cookies_to_obscura.py
"""
import json
import os

def parse_netscape(path):
    cookies = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) < 7:
                continue
            domain, flag, cpath, secure, expires, name, value = parts[:7]
            # Skip empty names (malformed lines)
            if not name:
                continue
            try:
                exp = int(expires)
            except (ValueError, TypeError):
                exp = 0
            cookies.append({
                "name": name,
                "value": value,
                "domain": domain,
                "path": cpath or "/",
                "secure": secure.upper() == "TRUE",
                "httpOnly": False,  # Netscape format doesn't carry httpOnly
                "sameSite": "Lax",
                "expires": exp,  # must be INT for Obscura
                "hostOnly": flag.upper() != "TRUE",
            })
    return cookies


def main():
    home = os.path.expanduser("~")
    src = os.path.join(home, "Downloads", "cookies-export.txt")
    dst = os.path.join(home, "obscura", "profile", "cookies.json")

    if not os.path.isfile(src):
        raise RuntimeError(f"Export file not found: {src} (run export_cookies.py first)")

    new_cookies = parse_netscape(src)
    print(f"Parsed {len(new_cookies)} cookies from export")

    # Load existing, merge by (domain, path, name) — new wins
    existing = []
    if os.path.isfile(dst):
        try:
            with open(dst, encoding="utf-8") as f:
                existing = json.load(f)
        except (json.JSONDecodeError, OSError):
            existing = []

    seen = {(c.get("domain"), c.get("path"), c.get("name")): c for c in existing}
    for c in new_cookies:
        seen[(c["domain"], c["path"], c["name"])] = c

    merged = list(seen.values())
    # Backup
    bak = dst + ".bak"
    try:
        if os.path.isfile(dst):
            with open(dst, "rb") as fsrc, open(bak, "wb") as fdst:
                fdst.write(fsrc.read())
    except OSError:
        pass

    with open(dst, "w", encoding="utf-8") as f:
        json.dump(merged, f, indent=2)

    print(f"Merged {len(merged)} total cookies -> {dst}")
    print("NOTE: Restart Obscura for it to pick up the new cookies.")


if __name__ == "__main__":
    main()
