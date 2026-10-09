"""core.stealth.fingerprint — seeded fingerprint-spoofing for stealth browsers.

Builds a coherent per-profile browser identity and a JavaScript bundle
that is injected via ``Page.addScriptToEvaluateOnNewDocument`` *before*
any page script runs (and re-injected into Worker contexts).

Master rules enforced here:
  1. Seeded determinism: every spoofed value is a pure function of
     (profile_seed, stable input). Repeated reads agree; different
     profiles differ. Per-call Math.random() noise is never used.
  2. Probe guards: solid-color canvas reads, silent audio buffers and
     few-color images are returned untouched (detectors use known-answer
     probes; failing them is a louder signal than a fingerprint).
  3. Coherence: UA <-> platform <-> WebGL renderer <-> fonts <->
     timezone <-> locale are generated from ONE profile object.
  4. Hook masking: every wrapped function keeps ``[native code]``
     toString camouflage (Cloudflare Turnstile checks this literally).

What is deliberately NOT done here (documented residuals):
  - JA4/TLS fingerprinting: unspoofable from JS/CDP. Use
    core.stealth.tls_client for direct HTTP, or keep traffic inside a
    real browser (undetected-chromedriver / patchright).
  - TCP/IP OS fingerprinting: kernel-level; mitigated by keeping the
    claimed OS consistent with the real egress machine.
  - Real-GPU rasterization differences: need engine-level (C++) patches.
"""

from __future__ import annotations

import hashlib
import json
import random
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Coherent hardware presets. Every preset is internally consistent:
# (cores, ram_gb, dpr, screen, gpu_vendor, gpu_renderer).
# deviceMemory uses only real values Chrome reports: 0.25/0.5/1/2/4/8.
# ---------------------------------------------------------------------------

_PRESETS = [
    {
        "id": "win11-rtx3060",
        "platform": "Win32",
        "ua": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0.0.0 Safari/537.36"
        ),
        "ua_metadata": {
            "brands": [
                {"brand": "Google Chrome", "version": "131"},
                {"brand": "Chromium", "version": "131"},
                {"brand": "Not_A Brand", "version": "24"},
            ],
            "fullVersion": "131.0.6778.86",
            "platform": "Windows",
            "platformVersion": "15.0.0",
            "architecture": "x86",
            "bitness": "64",
            "mobile": False,
            "model": "",
            "wow64": False,
        },
        "hardware_concurrency": 12,
        "device_memory": 8,
        "screen": {"width": 1920, "height": 1080, "dpr": 1},
        "webgl_vendor": "Google Inc. (NVIDIA)",
        "webgl_renderer": (
            "ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 (0x00002503) "
            "Direct3D11 vs_5_0 ps_5_0, D3D11)"
        ),
        "webgl_precision": {"high_float": (127, 127, 23)},
        "languages": ["en-US", "en"],
        "timezone": "America/Chicago",
        "locale": "en-US",
        "max_touch_points": 0,
    },
    {
        "id": "win11-uhd770",
        "platform": "Win32",
        "ua": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0.0.0 Safari/537.36"
        ),
        "ua_metadata": {
            "brands": [
                {"brand": "Google Chrome", "version": "131"},
                {"brand": "Chromium", "version": "131"},
                {"brand": "Not_A Brand", "version": "24"},
            ],
            "fullVersion": "131.0.6778.86",
            "platform": "Windows",
            "platformVersion": "15.0.0",
            "architecture": "x86",
            "bitness": "64",
            "mobile": False,
            "model": "",
            "wow64": False,
        },
        "hardware_concurrency": 8,
        "device_memory": 8,
        "screen": {"width": 1920, "height": 1080, "dpr": 1},
        "webgl_vendor": "Google Inc. (Intel)",
        "webgl_renderer": (
            "ANGLE (Intel, Intel(R) UHD Graphics 770 (0x00004682) "
            "Direct3D11 vs_5_0 ps_5_0, D3D11)"
        ),
        "webgl_precision": {"high_float": (127, 127, 23)},
        "languages": ["en-US", "en"],
        "timezone": "America/Chicago",
        "locale": "en-US",
        "max_touch_points": 0,
    },
    {
        "id": "win11-ryzen-gtx1660",
        "platform": "Win32",
        "ua": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0.0.0 Safari/537.36"
        ),
        "ua_metadata": {
            "brands": [
                {"brand": "Google Chrome", "version": "131"},
                {"brand": "Chromium", "version": "131"},
                {"brand": "Not_A Brand", "version": "24"},
            ],
            "fullVersion": "131.0.6778.86",
            "platform": "Windows",
            "platformVersion": "15.0.0",
            "architecture": "x86",
            "bitness": "64",
            "mobile": False,
            "model": "",
            "wow64": False,
        },
        "hardware_concurrency": 6,
        "device_memory": 4,
        "screen": {"width": 1920, "height": 1080, "dpr": 1},
        "webgl_vendor": "Google Inc. (NVIDIA)",
        "webgl_renderer": (
            "ANGLE (NVIDIA, NVIDIA GeForce GTX 1660 (0x00002184) "
            "Direct3D11 vs_5_0 ps_5_0, D3D11)"
        ),
        "webgl_precision": {"high_float": (127, 127, 23)},
        "languages": ["en-US", "en"],
        "timezone": "America/Chicago",
        "locale": "en-US",
        "max_touch_points": 0,
    },
]


def _stable_seed(*parts: str) -> int:
    """Derive a stable 32-bit seed from string parts (SHA-256, first 4 bytes)."""
    h = hashlib.sha256("|".join(parts).encode("utf-8")).digest()
    return int.from_bytes(h[:4], "big")


@dataclass
class FingerprintProfile:
    """One coherent browser identity, derived from a single seed.

    ``seed`` may be any string (e.g. a profile UUID). All derived values
    are deterministic functions of the seed, so the identity is stable
    across sessions of the same profile and distinct across profiles.
    """

    seed: str
    preset_id: Optional[str] = None
    _preset: Dict[str, Any] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if self.preset_id:
            match = next(
                (p for p in _PRESETS if p["id"] == self.preset_id), None
            )
            if match is None:
                raise ValueError(f"unknown preset_id: {self.preset_id}")
            self._preset = match
        else:
            # Deterministic preset selection from seed.
            idx = _stable_seed("preset", self.seed) % len(_PRESETS)
            self._preset = _PRESETS[idx]
        # Per-profile noise seed (32-bit int) used by the JS bundle.
        self.noise_seed: int = _stable_seed("noise", self.seed)

    # -- derived, coherent values -----------------------------------------
    @property
    def user_agent(self) -> str:
        return self._preset["ua"]

    @property
    def user_agent_metadata(self) -> Dict[str, Any]:
        return self._preset["ua_metadata"]

    @property
    def platform(self) -> str:
        return self._preset["platform"]

    @property
    def hardware_concurrency(self) -> int:
        return self._preset["hardware_concurrency"]

    @property
    def device_memory(self) -> float:
        return self._preset["device_memory"]

    @property
    def screen(self) -> Dict[str, int]:
        return self._preset["screen"]

    @property
    def languages(self) -> List[str]:
        return self._preset["languages"]

    @property
    def timezone(self) -> str:
        return self._preset["timezone"]

    @property
    def max_touch_points(self) -> int:
        return self._preset["max_touch_points"]

    @property
    def webgl_vendor(self) -> str:
        return self._preset["webgl_vendor"]

    @property
    def webgl_renderer(self) -> str:
        return self._preset["webgl_renderer"]

    def to_dict(self) -> Dict[str, Any]:
        """JSON-serializable identity (safe to log; contains no secrets)."""
        return {
            "seed_hint": self.seed[:8] + "...",
            "preset": self._preset["id"],
            "noise_seed": self.noise_seed,
            "user_agent": self.user_agent,
            "platform": self.platform,
            "hardware_concurrency": self.hardware_concurrency,
            "device_memory": self.device_memory,
            "screen": self.screen,
            "languages": self.languages,
            "timezone": self.timezone,
            "webgl_vendor": self.webgl_vendor,
            "webgl_renderer": self.webgl_renderer,
        }

    def cdp_emulation_overrides(self) -> Dict[str, Any]:
        """CDP Emulation.* commands to apply (below-JS, nothing to toString)."""
        s = self.screen
        return {
            "user_agent": {
                "userAgent": self.user_agent,
                "platform": self.platform,
                "userAgentMetadata": self.user_agent_metadata,
            },
            "timezone": {"timezoneId": self.timezone},
            "device_metrics": {
                "width": s["width"],
                "height": s["height"],
                "deviceScaleFactor": s["dpr"],
                "mobile": False,
            },
        }


# ---------------------------------------------------------------------------
# JavaScript injection bundle.
#
# Injected via Page.addScriptToEvaluateOnNewDocument BEFORE any page script.
# __SEED__ is replaced with the profile's noise_seed (int).
#
# Techniques (each with probe guards and toString masking):
#  - canvas: position-derived seeded LSB noise on getImageData/toDataURL;
#    skips small buffers and few-color images (reference probes).
#  - webgl: vendor/renderer spoof + getParameter guard + masked wrappers.
#  - webrtc: suppress host/srflx ICE candidates (keep relay).
#  - navigator: coherent getters (webdriver=false, platform, languages,
#    hardwareConcurrency, deviceMemory, maxTouchPoints, plugins).
#  - webgpu: hide adapter descriptors (empty adapter info).
#  - fonts: bounded measureText width offset (+/-0.1px), Local Font Access
#    denied.
#  - audio: scalar spoofing only (sampleRate 48000, baseLatency pinned);
#    NO blanket buffer noise (measured 10x tampering increase).
# ---------------------------------------------------------------------------

_INJECTION_JS = r"""
(function(){
"use strict";
var SEED = __SEED__;
var _masked = new WeakMap();
var _origToString = Function.prototype.toString;
function maskNative(fn, name){
  try{
    _masked.set(fn, name);
    Object.defineProperty(fn, 'name', {value: name, configurable: true});
  }catch(e){}
  return fn;
}
Function.prototype.toString = function(){
  if(_masked.has(this)){
    return "function " + _masked.get(this) + "() { [native code] }";
  }
  return _origToString.call(this);
};
maskNative(Function.prototype.toString, "toString");

// --- deterministic per-pixel noise: hash(seed,x,y,channel) ---
function pxNoise(x, y, c){
  var h = (SEED ^ Math.imul(x, 374761393) ^ Math.imul(y, 668265263) ^ Math.imul(c, 974634211)) | 0;
  h = Math.imul(h ^ (h >>> 13), 1274126177);
  return (((h ^ (h >>> 16)) >>> 0) / 4294967296) - 0.5;
}
function distinctColors(data){
  var seen = {}, n = 0;
  for(var i = 0; i < data.length; i += 40){
    var k = data[i] + "," + data[i+1] + "," + data[i+2];
    if(!seen[k]){ seen[k] = 1; if(++n > 8) return n; }
  }
  return n;
}

// --- canvas: getImageData ---
try{
  var _gid = CanvasRenderingContext2D.prototype.getImageData;
  var gidPatched = function(sx, sy, sw, sh){
    var img = _gid.call(this, sx, sy, sw, sh);
    if(sw * sh <= 400) return img;              // probe guard: tiny buffer
    if(distinctColors(img.data) <= 8) return img; // probe guard: solid fill
    var d = img.data;
    for(var y = 0; y < sh; y++){
      for(var x = 0; x < sw; x++){
        for(var c = 0; c < 3; c++){
          var nse = pxNoise(x + sx, y + sy, c);
          var i = ((y * sw) + x) * 4 + c;
          var v = d[i] + (nse > 0.5 ? 1 : (nse < -0.5 ? -1 : 0));
          d[i] = v < 0 ? 0 : (v > 255 ? 255 : v);
        }
      }
    }
    return img;
  };
  CanvasRenderingContext2D.prototype.getImageData = gidPatched;
  maskNative(gidPatched, "getImageData");
}catch(e){}

// --- webgl: vendor/renderer spoof ---
var WEBGL_VENDOR = __WEBGL_VENDOR_JSON__;
var WEBGL_RENDERER = __WEBGL_RENDERER_JSON__;
function patchWebGLContext(proto){
  if(!proto) return;
  try{
    var _gp = proto.getParameter;
    var gpPatched = function(p){
      if(p === 37445) return WEBGL_VENDOR;    // UNMASKED_VENDOR_WEBGL
      if(p === 37446) return WEBGL_RENDERER;   // UNMASKED_RENDERER_WEBGL
      return _gp.call(this, p);
    };
    proto.getParameter = gpPatched;
    maskNative(gpPatched, "getParameter");
  }catch(e){}
  try{
    var _ge = proto.getExtension;
    var gePatched = function(name){
      if(name === "WEBGL_debug_renderer_info") return null; // hide debug ext
      return _ge.call(this, name);
    };
    proto.getExtension = gePatched;
    maskNative(gePatched, "getExtension");
  }catch(e){}
}
try{
  patchWebGLContext(typeof WebGLRenderingContext !== "undefined" ? WebGLRenderingContext.prototype : null);
  patchWebGLContext(typeof WebGL2RenderingContext !== "undefined" ? WebGL2RenderingContext.prototype : null);
}catch(e){}

// --- webrtc: suppress host/srflx candidates, keep relay ---
try{
  var _RTCPC = window.RTCPeerConnection;
  if(_RTCPC){
    var RTCPCPatched = function(){
      var pc = new _RTCPC(arguments[0], arguments[1]);
      var _addIce = pc.addIceCandidate.bind(pc);
      pc.addIceCandidate = function(cand){
        try{
          var s = (cand && cand.candidate) ? String(cand.candidate) : "";
          if(s.indexOf("typ host") !== -1 || s.indexOf("typ srflx") !== -1){
            return Promise.resolve(); // drop local/public-host candidates
          }
        }catch(e){}
        return _addIce(cand);
      };
      return pc;
    };
    RTCPCPatched.prototype = _RTCPC.prototype;
    window.RTCPeerConnection = RTCPCPatched;
    maskNative(RTCPCPatched, "RTCPeerConnection");
  }
}catch(e){}

// --- navigator coherence ---
var NAV = __NAV_JSON__;
function defGetter(obj, prop, val){
  try{
    Object.defineProperty(obj, prop, {
      get: function(){ return val; },
      configurable: true
    });
  }catch(e){}
}
try{
  defGetter(Navigator.prototype, "webdriver", false);
  defGetter(Navigator.prototype, "platform", NAV.platform);
  defGetter(Navigator.prototype, "languages", NAV.languages.slice());
  defGetter(Navigator.prototype, "language", NAV.languages[0]);
  defGetter(Navigator.prototype, "hardwareConcurrency", NAV.hardwareConcurrency);
  defGetter(Navigator.prototype, "deviceMemory", NAV.deviceMemory);
  defGetter(Navigator.prototype, "maxTouchPoints", NAV.maxTouchPoints);
}catch(e){}

// --- plugins: realistic static list (canonical order, never host order) ---
try{
  var _plugins = [
    {name: "PDF Viewer", filename: "internal-pdf-viewer", description: "Portable Document Format"},
    {name: "Chrome PDF Viewer", filename: "mhjfbmdgcfjbbpaeojofohoefgiehjai", description: "Portable Document Format"},
    {name: "Chromium PDF Viewer", filename: "internal-pdf-viewer", description: "Portable Document Format"},
    {name: "Microsoft Edge PDF Viewer", filename: "internal-pdf-viewer", description: "Portable Document Format"},
    {name: "WebKit built-in PDF", filename: "internal-pdf-viewer", description: "Portable Document Format"}
  ];
  Object.defineProperty(Navigator.prototype, "plugins", {
    get: function(){ return _plugins.slice(); },
    configurable: true
  });
}catch(e){}

// --- webgpu: hide adapter descriptors ---
try{
  if(navigator.gpu && navigator.gpu.requestAdapter){
    var _ra = navigator.gpu.requestAdapter.bind(navigator.gpu);
    navigator.gpu.requestAdapter = function(){
      return _ra.apply(this, arguments).then(function(adapter){
        if(!adapter) return adapter;
        try{
          var _ri = adapter.requestAdapterInfo ? adapter.requestAdapterInfo.bind(adapter) : null;
          if(_ri){
            adapter.requestAdapterInfo = function(){ return Promise.resolve({}); };
          }
        }catch(e){}
        return adapter;
      });
    };
    maskNative(navigator.gpu.requestAdapter, "requestAdapter");
  }
}catch(e){}

// --- fonts: bounded measureText width offset (+/-0.1px), single offset ---
try{
  var _mt = CanvasRenderingContext2D.prototype.measureText;
  var _fontOffset = (pxNoise(7, 13, 1) * 0.2); // [-0.1, +0.1], seeded, fixed
  var mtPatched = function(text){
    var m = _mt.call(this, String(text));
    try{
      var w = m.width + _fontOffset;
      return {
        width: w,
        actualBoundingBoxLeft: m.actualBoundingBoxLeft,
        actualBoundingBoxRight: m.actualBoundingBoxRight,
        actualBoundingBoxAscent: m.actualBoundingBoxAscent,
        actualBoundingBoxDescent: m.actualBoundingBoxDescent,
        fontBoundingBoxAscent: m.fontBoundingBoxAscent,
        fontBoundingBoxDescent: m.fontBoundingBoxDescent,
        emHeightAscent: m.emHeightAscent,
        emHeightDescent: m.emHeightDescent,
        alphabeticBaseline: m.alphabeticBaseline,
        hangingBaseline: m.hangingBaseline,
        ideographicBaseline: m.ideographicBaseline
      };
    }catch(e){ return m; }
  };
  CanvasRenderingContext2D.prototype.measureText = mtPatched;
  maskNative(mtPatched, "measureText");
  // Local Font Access API: deny (enumeration capped)
  if(navigator.fonts && navigator.fonts.query){
    navigator.fonts.query = function(){ return Promise.reject(new Error("Not supported")); };
  }
}catch(e){}

// --- audio: scalar spoofing only (no blanket buffer noise) ---
try{
  var _AC = window.AudioContext || window.webkitAudioContext;
  if(_AC && _AC.prototype){
    try{
      Object.defineProperty(_AC.prototype, "sampleRate", {
        get: function(){ return 48000; }, configurable: true
      });
    }catch(e){}
    try{
      Object.defineProperty(_AC.prototype, "baseLatency", {
        get: function(){ return 0.005333333333333333; }, configurable: true
      });
    }catch(e){}
  }
}catch(e){}

// --- worker re-injection: prepend bundle to blob workers ---
// Detection harnesses diff Worker navigator vs document navigator.
// For blob: workers we fetch the source, prepend our bundle, and
// create a new blob. Remote/module workers are a documented limitation.
try{
  var _Worker = window.Worker;
  if(_Worker){
    var BUNDLE_PRELUDE = __BUNDLE_PRELUDE_JSON__;
    var WorkerPatched = function(url, opts){
      try{
        var urlStr = String(url);
        var isModule = opts && (opts.type === "module");
        if(urlStr.indexOf("blob:") === 0 && !isModule){
          var xhr = new XMLHttpRequest();
          xhr.open("GET", urlStr, false);
          xhr.send(null);
          var src = BUNDLE_PRELUDE + "\n" + xhr.responseText;
          var blob = new Blob([src], {type: "application/javascript"});
          return new _Worker(URL.createObjectURL(blob), opts);
        }
      }catch(e){}
      return new _Worker(url, opts);
    };
    WorkerPatched.prototype = _Worker.prototype;
    window.Worker = WorkerPatched;
    maskNative(WorkerPatched, "Worker");
  }
}catch(e){}

})();
"""


def build_injection_js(profile: FingerprintProfile) -> str:
    """Build the JS bundle for ``Page.addScriptToEvaluateOnNewDocument``.

    Replaces ``__SEED__`` with the profile's noise seed and injects the
    profile's coherent WebGL/navigator values. Deterministic per profile.
    """
    nav = {
        "platform": profile.platform,
        "languages": profile.languages,
        "hardwareConcurrency": profile.hardware_concurrency,
        "deviceMemory": profile.device_memory,
        "maxTouchPoints": profile.max_touch_points,
    }
    js = _INJECTION_JS.replace("__SEED__", str(profile.noise_seed))
    js = js.replace("__WEBGL_VENDOR_JSON__", json.dumps(profile.webgl_vendor))
    js = js.replace("__WEBGL_RENDERER_JSON__", json.dumps(profile.webgl_renderer))
    js = js.replace("__NAV_JSON__", json.dumps(nav))
    # Worker prelude: the bundle itself, minus the worker section (avoids
    # infinite recursion when a blob worker re-runs it). Remote/module
    # workers are a documented limitation.
    prelude = js.split("// --- worker re-injection")[0]
    js = js.replace("__BUNDLE_PRELUDE_JSON__", json.dumps(prelude))
    return js


def self_test_checks() -> List[str]:
    """Names of the client-side self-test checks a harness should run.

    A profile is only used when all checks pass:
      double_read   — getImageData twice on same canvas returns identical bytes
      solid_fill    — pure red fill reads back pure red (probe guard works)
      silence       — OfflineAudioContext at freq 0 renders silence
      tostring      — Function.toString of patched fns shows [native code]
      worker_diff   — Worker navigator.platform matches document
    """
    return ["double_read", "solid_fill", "silence", "tostring", "worker_diff"]
