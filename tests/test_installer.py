"""Installer tests — no pwsh on this machine, so structural validation.

NOTE on the file name: the task brief said `install-all.ps1`, but the
merged repo ships the installer as `install/install.ps1` (plus
`install/StartMuseMCP.vbs`). These tests target the real file.
If `pwsh`/`powershell` is present, the tests additionally run the real
PowerShell parser against the script and require zero parse errors.
"""
import json
import os
import re
import shutil
import subprocess

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INSTALL_PS1 = os.path.join(REPO_ROOT, "install", "install.ps1")


def _read():
    with open(INSTALL_PS1, encoding="utf-8-sig") as f:
        return f.read()


def _strip_ps_strings_and_comments(src):
    """Remove block comments, line comments, and quoted strings so a
    brace/paren balance check isn't fooled by their contents."""
    src = re.sub(r"<#.*?#>", "", src, flags=re.DOTALL)  # block comments
    out = []
    i, n = 0, len(src)
    while i < n:
        ch = src[i]
        if ch in ("'", '"'):
            quote = ch
            i += 1
            while i < n:
                if src[i] == "`" and i + 1 < n:  # backtick escape
                    i += 2
                    continue
                if src[i] == quote:
                    if quote == "'" and i + 1 < n and src[i + 1] == "'":
                        i += 2  # '' inside single-quoted string
                        continue
                    i += 1
                    break
                i += 1
            out.append(" ")
        elif ch == "#":
            while i < n and src[i] != "\n":
                i += 1
        else:
            out.append(ch)
            i += 1
    return "".join(out)


# ── existence ───────────────────────────────────────────────────────────

def test_installer_exists_and_nonempty():
    assert os.path.isfile(INSTALL_PS1), INSTALL_PS1
    assert os.path.getsize(INSTALL_PS1) > 500


def test_installer_referenced_artifacts_exist():
    for rel in [
        os.path.join("install", "StartMuseMCP.vbs"),
        os.path.join("daemon", "simpled.py"),
        os.path.join("tools", "export_cookies.py"),
        os.path.join("extension", "manifest.json"),
    ]:
        assert os.path.isfile(os.path.join(REPO_ROOT, rel)), rel


# ── structure ───────────────────────────────────────────────────────────

def test_has_comment_based_help():
    src = _read()
    assert src.lstrip().startswith("<#")
    assert ".SYNOPSIS" in src
    assert "#>" in src


def test_stops_on_error():
    src = _read()
    assert re.search(r'\$ErrorActionPreference\s*=\s*["\']Stop["\']', src)


def test_braces_and_parens_balanced():
    code = _strip_ps_strings_and_comments(_read())
    assert code.count("{") == code.count("}"), "unbalanced { }"
    assert code.count("(") == code.count(")"), "unbalanced ( )"
    assert code.count("{") > 5  # sanity: the check actually saw code


def test_no_trailing_whitespace_after_line_continuation():
    # In PowerShell a backtick followed by whitespace is NOT a line
    # continuation — a classic silent installer bug.
    bad = [ln for ln in _read().splitlines() if re.search(r"`[ \t]+$", ln)]
    assert not bad, f"trailing whitespace after backtick: {bad!r}"


# ── expected installer steps ────────────────────────────────────────────

def test_checks_python_version():
    src = _read()
    assert "python --version" in src
    assert "3.10" in src


def test_installs_websockets_dependency():
    src = _read()
    assert re.search(r"pip install.*websockets", src)


def test_copies_extension_daemon_tools():
    src = _read()
    for d in ("extension", "daemon", "tools"):
        assert f'"{d}"' in src, f"installer should copy {d}/"


def test_registers_cookie_export_scheduled_task():
    src = _read()
    assert "MuseCookieExport" in src
    assert "New-ScheduledTaskAction" in src
    assert "Register-ScheduledTask" in src


def test_registers_daemon_autostart():
    src = _read()
    assert "StartMuseMCP.vbs" in src
    assert "Startup" in src


def test_prints_manual_chrome_steps():
    src = _read()
    assert "chrome://extensions" in src
    assert "Load unpacked" in src


def test_daemon_port_consistent():
    src = _read()
    ports = set(re.findall(r"127\.0\.0\.1:(\d+)", src))
    assert ports == {"18010"}, f"daemon port references diverged: {ports}"


# ── artifacts the installer deploys ─────────────────────────────────────

def test_extension_manifest_is_valid_json():
    path = os.path.join(REPO_ROOT, "extension", "manifest.json")
    with open(path, encoding="utf-8") as f:
        manifest = json.load(f)
    assert manifest["manifest_version"] in (2, 3)
    assert "name" in manifest


# ── real parser when pwsh exists ────────────────────────────────────────

def _find_pwsh():
    return shutil.which("pwsh") or shutil.which("powershell")


def test_pwsh_parser_reports_no_errors():
    pwsh = _find_pwsh()
    if not pwsh:
        pytest.skip("pwsh/powershell not installed; structure checks above apply")
    ps = (
        "$errs = $null; $toks = $null; "
        "[void][System.Management.Automation.Language.Parser]::ParseFile("
        f"'{INSTALL_PS1}', [ref]$toks, [ref]$errs); "
        "$errs.Count"
    )
    out = subprocess.run(
        [pwsh, "-NoProfile", "-NonInteractive", "-Command", ps],
        capture_output=True, text=True, timeout=60,
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "0", f"parse errors: {out.stdout}"
