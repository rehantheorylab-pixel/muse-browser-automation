"""Coordinate cache, frame hashing, and attempt accounting for s3rehan.

Speed comes from NOT recomputing: a grounded click point is cached under a
key of (app/process, window geometry, screen size, normalized target,
screen content hash). Any popup, resize, scroll, or movement changes the
screen hash -> automatic cache miss -> re-ground on the fresh frame.

Safety rails:
  - frames_match(): hamming distance between perceptual hashes decides
    whether a frame captured before a slow (~20s) grounding is still the
    frame the click is about to land on. Stale -> re-ground, never click.
  - Attempt accounting: at most 3 failed grounding attempts per
    (app, window, target) in a rolling window; beyond that the target is
    declared ungroundable for now (escalate to the planner / stop) instead
    of burning more model calls.
  - log_attempt(): every grounding attempt appends one JSON line with
    stage timings, model calls + latencies, path taken, and outcome —
    reusable as training records.

All file I/O is best-effort: logging must never break grounding.
"""

import hashlib
import io
import json
import os
import threading
import time
from typing import Dict, List, Optional, Tuple

# Rolling window / cap for failed grounding attempts per target.
_ATTEMPT_WINDOW_S = 180.0
_MAX_FAILED_ATTEMPTS = 3


# ---------------------------------------------------------------------------
# Frame hashing
# ---------------------------------------------------------------------------

def screen_hash(png: bytes, size: int = 16) -> str:
    """Perceptual hash of a screenshot: 16x16 grayscale, mean-thresholded.

    Returns 64 hex chars (256 bits). ~3-5ms. Deterministic for identical
    frames; small UI noise (cursor, clock) flips only a few bits.
    """
    import numpy as np
    from PIL import Image

    try:
        img = Image.open(io.BytesIO(png)).convert("L").resize(
            (size, size), Image.BILINEAR)
    except Exception:
        # Unreadable frame -> hash of nothing (never matches a real frame).
        return "0" * (size * size // 4)
    a = np.asarray(img).astype(np.float32)
    bits = (a >= a.mean()).astype(np.uint8).reshape(-1)
    val = 0
    for b in bits:
        val = (val << 1) | int(b)
    return format(val, "064x")


def hash_distance(h1: str, h2: str) -> int:
    """Hamming distance between two hex hashes."""
    try:
        return bin(int(h1, 16) ^ int(h2, 16)).count("1")
    except Exception:
        return 1 << 30


def _as_hash(h_or_png) -> str:
    if isinstance(h_or_png, str) and len(h_or_png) == 64:
        return h_or_png
    if isinstance(h_or_png, (bytes, bytearray)):
        return screen_hash(bytes(h_or_png))
    return "0" * 64


def frames_match(hash_or_png_a, hash_or_png_b,
                 tol_bits: int = 12) -> bool:
    """True when two frames are the same screen (within noise tolerance)."""
    ha, hb = _as_hash(hash_or_png_a), _as_hash(hash_or_png_b)
    if ha == "0" * 64 or hb == "0" * 64:
        return False
    return hash_distance(ha, hb) <= tol_bits


# ---------------------------------------------------------------------------
# Element signatures: re-find the target after a scroll / layout shift
# ---------------------------------------------------------------------------

def element_signature(png: bytes, rect: Dict) -> Dict:
    """Visual signature of the target element itself.

    A 32x32 grayscale thumbnail of the element's rect plus a dhash and its
    size. Unlike the full-screen hash, the signature lets the cache
    re-find the element after scrolling even when the whole frame changed.
    Returns {} when the rect is unreadable.
    """
    import numpy as np
    from PIL import Image

    try:
        img = Image.open(io.BytesIO(png)).convert("L")
        W, H = img.size
        w, h = max(8, int(rect["w"])), max(8, int(rect["h"]))
        x0 = max(0, min(W - w, int(rect["x"] - w / 2)))
        y0 = max(0, min(H - h, int(rect["y"] - h / 2)))
        crop = img.crop((x0, y0, x0 + w, y0 + h))
        # Thumbnail preserves the aspect ratio (width 32) so template
        # matching compares like-for-like shapes, not squashed squares.
        th_ = max(8, round(32 * h / w))
        thumb = crop.resize((32, th_), Image.BILINEAR)
        # dhash on a fixed 9x8 normalization (comparable across sizes).
        norm = np.asarray(crop.resize((9, 8), Image.BILINEAR),
                          dtype=np.int16)
        diff = (norm[:, 1:] > norm[:, :-1])
        val = 0
        for b in diff.reshape(-1):
            val = (val << 1) | int(b)
        buf = io.BytesIO()
        thumb.save(buf, format="PNG")
        return {"dhash": f"{val:016x}", "wh": (w, h),
                "thumb": buf.getvalue(), "at": (x0 + w // 2, y0 + h // 2)}
    except Exception:
        return {}


def _ncc_best(hay, tmpl):
    """Best normalized-cross-correlation match of tmpl in haystack.

    Returns (y, x, score) in haystack pixels, or None for a flat template.
    """
    import numpy as np
    from numpy.lib.stride_tricks import sliding_window_view

    h, w = tmpl.shape
    if hay.shape[0] < h or hay.shape[1] < w:
        return None
    t = tmpl - tmpl.mean()
    tss = float((t * t).sum())
    if tss < 1e-6:
        return None  # flat template matches everything: useless
    win = sliding_window_view(hay, (h, w))
    wmean = win.mean(axis=(2, 3), keepdims=True)
    wc = win - wmean
    num = (wc * t).sum(axis=(2, 3))
    den = np.sqrt((wc * wc).sum(axis=(2, 3)) * tss)
    ncc = np.divide(num, den, out=np.zeros_like(num), where=den > 1e-6)
    i, j = np.unravel_index(int(np.argmax(ncc)), ncc.shape)
    return int(i), int(j), float(ncc[i, j])


def find_signature(png: bytes, sig: Dict, min_score: float = 0.80,
                   x_radius: int = 60) -> Optional[Dict]:
    """Locate an element signature in a new frame (scroll/shift recovery).

    Searches a vertical strip around the signature's last known x (scrolls
    move content vertically) with strided normalized cross-correlation on
    a half-resolution image. Returns {"x","y","score"} in full-resolution
    pixels, or None when nothing matches well enough.
    """
    import numpy as np
    from PIL import Image

    try:
        thumb = np.asarray(
            Image.open(io.BytesIO(sig["thumb"])).convert("L"),
            dtype=np.float32)
        img = Image.open(io.BytesIO(png)).convert("L")
    except Exception:
        return None
    W, H = img.size
    last_x, _ = sig.get("at", (W // 2, H // 2))
    # Half resolution keeps the strided window tensor small. The template
    # is the thumbnail re-scaled to the SEARCH resolution (half of the
    # element's true w/h), preserving aspect ratio.
    img2 = img.resize((W // 2, H // 2), Image.BILINEAR)
    thumb_img = Image.open(io.BytesIO(sig["thumb"])).convert("L")
    tw_full, th_full = sig.get("wh", (32, 32))
    t2 = np.asarray(
        thumb_img.resize((max(8, int(tw_full // 2)),
                          max(8, int(th_full // 2))),
                         Image.BILINEAR), dtype=np.float32)
    th, tw = t2.shape
    hay = np.asarray(img2, dtype=np.float32)
    hh, hw = hay.shape
    x0 = max(0, int(last_x // 2 - x_radius // 2))
    x1 = min(hw, int(last_x // 2 + x_radius // 2 + tw))
    if x1 - x0 < tw:
        return None
    strip = hay[:, x0:x1]
    hit = _ncc_best(strip, t2)
    if hit is None:
        return None
    yy, xx, score = hit
    if score < min_score:
        return None
    # Map back to full resolution (template center).
    return {"x": int((x0 + xx + tw / 2) * 2), "y": int((yy + th / 2) * 2),
            "score": round(score, 3)}


# ---------------------------------------------------------------------------
# Key normalization
# ---------------------------------------------------------------------------

def normalize_ref(ref_expr: str) -> str:
    from rehan import perception

    toks = sorted(set(perception._tokens(ref_expr or "")))
    return " ".join(toks)


def normalize_app(app: Optional[str]) -> str:
    return (app or "").strip().lower()


def normalize_winrect(rect) -> Optional[Tuple[int, int, int, int]]:
    if not rect:
        return None
    try:
        if isinstance(rect, dict):
            return (int(rect.get("x", 0)), int(rect.get("y", 0)),
                    int(rect.get("w", 0)), int(rect.get("h", 0)))
        x, y, w, h = rect
        return (int(x), int(y), int(w), int(h))
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Coordinate cache
# ---------------------------------------------------------------------------

class CoordCache:
    """Process-wide cache of grounded click points.

    Key: (app, window_rect, screen_wh, dpi_scale, normalized_ref,
    screen_hash). The screen hash in the key is the invalidation
    mechanism: any visual change (popup, scroll, resize, animation)
    misses automatically. A secondary signature index re-finds the
    element after scrolling even when the frame hash changed.
    """

    def __init__(self, max_entries: int = 512):
        self._d: Dict[tuple, Dict] = {}
        self._sig: Dict[tuple, Dict] = {}  # (app, ref_norm) -> signature rec
        self._lock = threading.Lock()
        self._max = max_entries
        self.hits = 0
        self.misses = 0

    def _key(self, app, window_rect, screen_wh, dpi_scale, ref_expr,
             scr_hash: str) -> tuple:
        return (normalize_app(app), normalize_winrect(window_rect),
                tuple(screen_wh or (0, 0)), round(float(dpi_scale or 1.0), 2),
                normalize_ref(ref_expr), scr_hash or "")

    def get(self, app, window_rect, screen_wh, dpi_scale, ref_expr,
            scr_hash: str) -> Optional[Dict]:
        k = self._key(app, window_rect, screen_wh, dpi_scale, ref_expr,
                      scr_hash)
        with self._lock:
            v = self._d.get(k)
            if v is None:
                self.misses += 1
                STABILITY.note_miss(app, ref_expr)
                return None
            self.hits += 1
            out = dict(v)
        # Stability: a hit reinforces; expose whether the caller may skip
        # re-verification for this proven-stable target.
        out["stability"] = STABILITY.note_hit(app, ref_expr)
        out["skip_verify"] = out["stability"] >= _STABILITY_SKIP_VERIFY
        return out

    def put(self, app, window_rect, screen_wh, dpi_scale, ref_expr,
            scr_hash: str, x: int, y: int, w: int, h: int,
            confidence: float, path: str,
            signature: Optional[Dict] = None) -> None:
        k = self._key(app, window_rect, screen_wh, dpi_scale, ref_expr,
                      scr_hash)
        with self._lock:
            if len(self._d) >= self._max:
                # Evict the oldest entry (insertion-ordered dict).
                self._d.pop(next(iter(self._d)))
            self._d[k] = {"x": int(x), "y": int(y), "w": int(w), "h": int(h),
                          "confidence": float(confidence), "path": path,
                          "ts": time.time()}
            if signature:
                sk = (normalize_app(app), normalize_ref(ref_expr))
                self._sig[sk] = {"sig": signature, "x": int(x), "y": int(y),
                                 "ts": time.time()}

    def get_signature(self, app, ref_expr,
                      max_age_s: float = 600.0) -> Optional[Dict]:
        """Latest element signature for (app, target), for scroll recovery."""
        sk = (normalize_app(app), normalize_ref(ref_expr))
        with self._lock:
            rec = self._sig.get(sk)
            if rec is None or time.time() - rec["ts"] > max_age_s:
                return None
            return dict(rec)

    def invalidate_app(self, app) -> int:
        """Drop every entry for an app (e.g. after a window move/resize that
        the screen hash somehow missed). Returns entries removed."""
        norm = normalize_app(app)
        with self._lock:
            doomed = [k for k in self._d if k[0] == norm]
            for k in doomed:
                del self._d[k]
            doomed_s = [k for k in self._sig if k[0] == norm]
            for k in doomed_s:
                del self._sig[k]
            return len(doomed)

    def clear(self) -> None:
        with self._lock:
            self._d.clear()
            self._sig.clear()
            self.hits = self.misses = 0


CACHE = CoordCache()


# ---------------------------------------------------------------------------
# Stability scoring (opendock agent-cache-optimizer pattern, DEEP_RESEARCH_ROUND2.md §6)
#
# Each cached target gets a stability score 0.0 (volatile) -> 1.0 (rock
# solid), updated on every observation:
#   - hit with matching frame hash        -> score += 0.10 (reinforce)
#   - hit via signature re-find (scrolled) -> score += 0.05 (still there)
#   - miss after N hits (element moved)    -> score -= 0.20 (penalize)
# Scores clamp to [0, 1]. Entries with score >= 0.80 may SKIP
# re-verification (the element has proven stable); below that the
# normal verify path runs. Cold entries start at 0.50.
# ---------------------------------------------------------------------------

_STABILITY_HIT_BONUS = 0.10
_STABILITY_SIG_BONUS = 0.05
_STABILITY_MISS_PENALTY = 0.20
_STABILITY_SKIP_VERIFY = 0.80
_STABILITY_COLD = 0.50


class StabilityTracker:
    """Per-(app, normalized target) stability scores."""

    def __init__(self):
        self._scores: Dict[Tuple[str, str], float] = {}
        self._lock = threading.Lock()

    def _key(self, app, ref_expr) -> Tuple[str, str]:
        return (normalize_app(app), normalize_ref(ref_expr))

    def score(self, app, ref_expr) -> float:
        with self._lock:
            return self._scores.get(self._key(app, ref_expr), _STABILITY_COLD)

    def note_hit(self, app, ref_expr, via_signature: bool = False) -> float:
        with self._lock:
            k = self._key(app, ref_expr)
            s = self._scores.get(k, _STABILITY_COLD)
            s += _STABILITY_SIG_BONUS if via_signature else _STABILITY_HIT_BONUS
            s = min(1.0, s)
            self._scores[k] = s
            return s

    def note_miss(self, app, ref_expr) -> float:
        with self._lock:
            k = self._key(app, ref_expr)
            s = self._scores.get(k, _STABILITY_COLD)
            s = max(0.0, s - _STABILITY_MISS_PENALTY)
            self._scores[k] = s
            return s

    def skip_verify(self, app, ref_expr) -> bool:
        """True when this target is stable enough to skip re-verification."""
        return self.score(app, ref_expr) >= _STABILITY_SKIP_VERIFY


STABILITY = StabilityTracker()


# ---------------------------------------------------------------------------
# Attempt accounting: 3-strike cap per (app, window, target)
# ---------------------------------------------------------------------------

_attempts: Dict[tuple, Dict] = {}
_attempts_lock = threading.Lock()


def attempt_key(app, window_rect, screen_wh, ref_expr) -> tuple:
    return (normalize_app(app), normalize_winrect(window_rect),
            tuple(screen_wh or (0, 0)), normalize_ref(ref_expr))


def note_attempt(key: tuple, ok: bool) -> None:
    now = time.time()
    with _attempts_lock:
        rec = _attempts.setdefault(key, {"fails": [], "last_ok": 0.0})
        # Prune old failures outside the window.
        rec["fails"] = [t for t in rec["fails"]
                        if now - t < _ATTEMPT_WINDOW_S]
        if ok:
            rec["fails"] = []
            rec["last_ok"] = now
        else:
            rec["fails"].append(now)


def failed_recent(key: tuple, limit: int = _MAX_FAILED_ATTEMPTS,
                  window_s: float = _ATTEMPT_WINDOW_S) -> bool:
    """True when this target already failed `limit` times recently with no
    success since — the caller should stop and escalate, not retry."""
    now = time.time()
    with _attempts_lock:
        rec = _attempts.get(key)
        if not rec:
            return False
        fails = [t for t in rec["fails"] if now - t < window_s]
        return len(fails) >= limit and rec["last_ok"] < min(fails)


def reset_attempts(key: tuple) -> None:
    with _attempts_lock:
        _attempts.pop(key, None)


# ---------------------------------------------------------------------------
# Attempt log: one JSON line per grounding attempt (training records)
# ---------------------------------------------------------------------------

def _log_path() -> str:
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(here, "..", "logs", "grounding_attempts.jsonl")


def log_attempt(record: Dict) -> None:
    """Append a grounding-attempt record. Best-effort: never raises."""
    try:
        rec = dict(record)
        rec.setdefault("ts", time.time())
        path = _log_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, default=str) + "\n")
    except Exception:
        pass


def recent_attempts(n: int = 50) -> List[Dict]:
    """Read back the last n attempt records (for review / training)."""
    try:
        with open(_log_path(), encoding="utf-8") as f:
            lines = f.readlines()
        out = []
        for ln in lines[-n:]:
            try:
                out.append(json.loads(ln))
            except Exception:
                pass
        return out
    except Exception:
        return []
