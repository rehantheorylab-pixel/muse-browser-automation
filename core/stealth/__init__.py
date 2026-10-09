"""core.stealth — anti-detection layer for the browser automation stack.

Modules:
  fingerprint — seeded fingerprint-spoofing JS bundle (canvas, WebGL,
                audio, fonts, WebRTC, navigator, WebGPU) injected via
                Page.addScriptToEvaluateOnNewDocument. Also builds the
                coherent per-profile identity used by CDP Emulation.
  behavior    — human-like input: CDP mouse (Bezier + Fitts), scroll
                (notch bursts + momentum), typing (keystroke dynamics).
  tls_client  — curl_cffi API client with Chrome TLS/JA4 + header ordering
                for Tier-1 direct HTTP requests that bypass the browser.

Master principles (synthesized from Camoufox, Chameleon, Brave,
invisible_playwright research, Oct 2026):
  1. Seeded-per-profile determinism: noise = f(profile_seed, input).
     Never per-call randomness.
  2. Guard reference probes: skip noise on solid fills / silent audio.
  3. Coherence over uniqueness: UA/platform/GPU/fonts/timezone must agree.
  4. Inject before page scripts, including Worker contexts.
  5. Mask every hook (Function.prototype.toString camouflage).
"""

from core.stealth.fingerprint import FingerprintProfile, build_injection_js
from core.stealth.behavior import (
    bezier_mouse_path,
    fitts_duration_ms,
    human_type_plan,
)
from core.stealth.tls_client import make_api_client, is_curl_cffi_available

__all__ = [
    "FingerprintProfile",
    "build_injection_js",
    "bezier_mouse_path",
    "fitts_duration_ms",
    "human_type_plan",
    "make_api_client",
    "is_curl_cffi_available",
]
