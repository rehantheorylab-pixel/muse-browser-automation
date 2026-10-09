"""Tests for core.stealth.fingerprint — mock-only, no browser, no network."""

import json

import pytest

from core.stealth.fingerprint import (
    FingerprintProfile,
    _PRESETS,
    _stable_seed,
    build_injection_js,
    self_test_checks,
)


def test_stable_seed_deterministic():
    assert _stable_seed("a", "b") == _stable_seed("a", "b")
    assert _stable_seed("a", "b") != _stable_seed("a", "c")


def test_profile_preset_selection_deterministic():
    p1 = FingerprintProfile(seed="profile-1")
    p2 = FingerprintProfile(seed="profile-1")
    assert p1.to_dict()["preset"] == p2.to_dict()["preset"]
    assert p1.noise_seed == p2.noise_seed


def test_profile_different_seeds_differ():
    seeds = {FingerprintProfile(seed=f"s-{i}").noise_seed for i in range(20)}
    assert len(seeds) == 20  # all distinct noise seeds


def test_profile_explicit_preset():
    p = FingerprintProfile(seed="x", preset_id="win11-rtx3060")
    assert p.to_dict()["preset"] == "win11-rtx3060"
    assert "RTX 3060" in p.webgl_renderer


def test_profile_unknown_preset_raises():
    with pytest.raises(ValueError):
        FingerprintProfile(seed="x", preset_id="nope")


def test_profile_coherence():
    # UA <-> platform <-> WebGL vendor must agree (Windows/NVIDIA here).
    for preset in _PRESETS:
        p = FingerprintProfile(seed="s", preset_id=preset["id"])
        assert "Windows" in p.user_agent
        assert p.platform == "Win32"
        assert "NVIDIA" in p.webgl_vendor or "Intel" in p.webgl_vendor
        # deviceMemory uses only real Chrome values.
        assert p.device_memory in (0.25, 0.5, 1, 2, 4, 8)
        # hardwareConcurrency is a plausible desktop value.
        assert p.hardware_concurrency in (4, 6, 8, 12, 16)
        # DPR coherent with 1080p.
        assert p.screen["dpr"] == 1


def test_cdp_emulation_overrides_shape():
    p = FingerprintProfile(seed="s")
    ov = p.cdp_emulation_overrides()
    assert ov["user_agent"]["userAgent"] == p.user_agent
    assert "userAgentMetadata" in ov["user_agent"]
    assert ov["timezone"]["timezoneId"] == p.timezone
    assert ov["device_metrics"]["width"] == 1920


def test_injection_js_placeholders_replaced():
    p = FingerprintProfile(seed="seed-abc")
    js = build_injection_js(p)
    for token in ("__SEED__", "__WEBGL_VENDOR_JSON__", "__WEBGL_RENDERER_JSON__",
                  "__NAV_JSON__", "__BUNDLE_PRELUDE_JSON__"):
        assert token not in js, token
    assert str(p.noise_seed) in js
    assert json.dumps(p.webgl_renderer) in js


def test_injection_js_has_probe_guards():
    js = build_injection_js(FingerprintProfile(seed="s"))
    # Canvas probe guards: small buffer + few-color skip.
    assert "sw * sh <= 400" in js
    assert "distinctColors" in js
    # toString masking for every hook (Turnstile checks [native code]).
    assert "[native code]" in js


def test_injection_js_covers_all_vectors():
    js = build_injection_js(FingerprintProfile(seed="s"))
    assert "getImageData" in js          # canvas
    # WebGL uses numeric constants (37445/37446), never the string literal
    # as a lookup key — comments may mention the names.
    assert '"UNMASKED_VENDOR_WEBGL"' not in js
    assert "37445" in js and "37446" in js
    assert "RTCPeerConnection" in js     # webrtc
    assert "webdriver" in js             # navigator
    assert "requestAdapter" in js        # webgpu
    assert "measureText" in js           # fonts
    assert "sampleRate" in js            # audio scalars
    assert "Worker" in js                # worker re-injection


def test_injection_js_no_per_call_random():
    # Seeded determinism rule: the bundle must not call Math.random().
    js = build_injection_js(FingerprintProfile(seed="s"))
    assert "Math.random()" not in js


def test_injection_js_deterministic_per_profile():
    a = build_injection_js(FingerprintProfile(seed="same"))
    b = build_injection_js(FingerprintProfile(seed="same"))
    assert a == b
    c = build_injection_js(FingerprintProfile(seed="different"))
    assert a != c


def test_self_test_checks():
    checks = self_test_checks()
    assert set(checks) == {
        "double_read", "solid_fill", "silence", "tostring", "worker_diff"
    }
