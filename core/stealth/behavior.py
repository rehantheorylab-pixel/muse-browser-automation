"""core.stealth.behavior — human-like input via CDP trusted events.

All events are dispatched through CDP ``Input.*`` so ``isTrusted`` is
true. The *fields* and *timing* are what we humanize:

Mouse
  - Cubic Bezier path with jittered control points (seeded RNG).
  - Fitts's-law duration: MT = a + b*log2(D/W + 1).
  - Bell-shaped velocity profile, overshoot on long moves.
  - Pre-click hesitation 60-300ms; click point jittered off-center.
  - Correct event fields: pointerType, pressure, buttons bitmask,
    fractional coords, irregular timestamps.

Scroll
  - Notch bursts (8-120ms gaps, 40-120px), flick + momentum tail,
    lognormal reading pauses (0.8-4s), occasional reverse micro-scrolls.
  - Uses CDP ``Input.dispatchMouseEvent`` mouseWheel (trusted).

Typing
  - Lognormal per-char flight times (~40-150ms), digraph speedup,
    2-5% typo + backspace corrections, per-key floor ~28ms.
  - Uses CDP ``Input.dispatchKeyEvent`` (never insertText).

Cadence
  - Gaussian/lognormal gaps between actions; never fixed intervals.
  - Physical total-time floors so a "task" can't complete inhumanly fast.

All randomness is seeded (``rng`` param) so runs are reproducible.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------
# Mouse
# ---------------------------------------------------------------------------

def _cubic_bezier(
    p0: Tuple[float, float],
    p1: Tuple[float, float],
    p2: Tuple[float, float],
    p3: Tuple[float, float],
    t: float,
) -> Tuple[float, float]:
    u = 1.0 - t
    x = u**3 * p0[0] + 3 * u**2 * t * p1[0] + 3 * u * t**2 * p2[0] + t**3 * p3[0]
    y = u**3 * p0[1] + 3 * u**2 * t * p1[1] + 3 * u * t**2 * p2[1] + t**3 * p3[1]
    return (x, y)


def fitts_duration_ms(distance_px: float, target_width_px: float = 24.0) -> float:
    """Fitts's-law movement time in ms. a=120ms, b=90ms/bit (typical)."""
    if distance_px <= 0:
        return 120.0
    mt = 120.0 + 90.0 * math.log2(distance_px / max(target_width_px, 1.0) + 1.0)
    return max(120.0, min(mt, 1500.0))


def bezier_mouse_path(
    start: Tuple[float, float],
    end: Tuple[float, float],
    rng: Optional[random.Random] = None,
    target_width_px: float = 24.0,
) -> List[Dict[str, Any]]:
    """Build a human-like mouse path as a list of CDP mouse events.

    Returns a list of dicts: {"type": "mouseMoved"|"mousePressed"|...,
    "x", "y", "delay_ms", ...}. The caller dispatches them via CDP with
    the given per-event delays (irregular timestamps).
    """
    rng = rng or random.Random()
    dx = end[0] - start[0]
    dy = end[1] - start[1]
    dist = math.hypot(dx, dy)

    # Jittered control points -> natural curvature.
    mx, my = (start[0] + end[0]) / 2.0, (start[1] + end[1]) / 2.0
    spread = max(dist * 0.25, 8.0)
    p1 = (mx + rng.uniform(-spread, spread), my + rng.uniform(-spread, spread))
    p2 = (mx + rng.uniform(-spread, spread), my + rng.uniform(-spread, spread))

    # Overshoot on long moves: aim slightly past, then settle back.
    overshoot = dist > 400
    final = end
    if overshoot:
        ox = end[0] + rng.uniform(4, 14) * (1 if dx >= 0 else -1)
        oy = end[1] + rng.uniform(4, 14) * (1 if dy >= 0 else -1)
        final = (ox, oy)

    duration = fitts_duration_ms(dist, target_width_px)
    steps = max(8, min(60, int(dist / 12)))

    events: List[Dict[str, Any]] = []
    prev = start
    elapsed = 0.0
    for i in range(1, steps + 1):
        t = i / steps
        # Bell velocity profile: slow-fast-slow.
        te = t * t * (3 - 2 * t)
        x, y = _cubic_bezier(start, p1, p2, final, te)
        step_dur = duration / steps
        # Velocity-shaped jitter on timing (not uniform).
        jitter = rng.uniform(0.6, 1.4)
        delay = step_dur * jitter * (0.5 + abs(te - 0.5))
        elapsed += delay
        events.append(
            {
                "type": "mouseMoved",
                "x": round(x + rng.uniform(-0.6, 0.6), 1),
                "y": round(y + rng.uniform(-0.6, 0.6), 1),
                "delay_ms": round(delay, 1),
                "movementX": round(x - prev[0], 1),
                "movementY": round(y - prev[1], 1),
                "pointerType": "mouse",
                "pressure": 0,
                "buttons": 0,
            }
        )
        prev = (x, y)

    if overshoot:
        # Settle back to the true target.
        events.append(
            {
                "type": "mouseMoved",
                "x": round(end[0], 1),
                "y": round(end[1], 1),
                "delay_ms": round(rng.uniform(40, 120), 1),
                "movementX": round(end[0] - prev[0], 1),
                "movementY": round(end[1] - prev[1], 1),
                "pointerType": "mouse",
                "pressure": 0,
                "buttons": 0,
            }
        )

    # Pre-click hesitation.
    hesitation = rng.uniform(60, 300)
    # Click point jittered off-center.
    cx = round(end[0] + rng.uniform(-4, 4), 1)
    cy = round(end[1] + rng.uniform(-3, 3), 1)
    events.append(
        {
            "type": "mousePressed",
            "x": cx,
            "y": cy,
            "delay_ms": round(hesitation, 1),
            "button": "left",
            "clickCount": 1,
            "pointerType": "mouse",
            "pressure": 0.5,
            "buttons": 1,
        }
    )
    events.append(
        {
            "type": "mouseReleased",
            "x": cx,
            "y": cy,
            "delay_ms": round(rng.uniform(40, 120), 1),
            "button": "left",
            "clickCount": 1,
            "pointerType": "mouse",
            "pressure": 0,
            "buttons": 0,
        }
    )
    return events


# ---------------------------------------------------------------------------
# Scroll
# ---------------------------------------------------------------------------

def human_scroll_plan(
    total_px: int,
    rng: Optional[random.Random] = None,
) -> List[Dict[str, Any]]:
    """Plan a human-like scroll as CDP mouseWheel events.

    Notch bursts (40-120px, 8-120ms gaps), flick + momentum tail,
    lognormal reading pauses, occasional reverse micro-scrolls.
    Positive total_px scrolls down.
    """
    rng = rng or random.Random()
    events: List[Dict[str, Any]] = []
    remaining = abs(total_px)
    direction = 1 if total_px >= 0 else -1

    while remaining > 0:
        # Burst of 2-6 notches.
        burst = rng.randint(2, 6)
        for _ in range(burst):
            if remaining <= 0:
                break
            notch = min(remaining, int(rng.uniform(40, 120)))
            remaining -= notch
            events.append(
                {
                    "type": "mouseWheel",
                    "deltaX": 0,
                    "deltaY": notch * direction,
                    "delay_ms": round(rng.uniform(8, 120), 1),
                }
            )
        if remaining <= 0:
            break
        # Reading pause (lognormal-ish 0.8-4s) or momentum tail.
        if rng.random() < 0.35:
            events.append(
                {"type": "pause", "delay_ms": round(rng.uniform(800, 4000), 1)}
            )
        elif rng.random() < 0.25 and remaining > 60:
            # Flick + momentum tail: decaying deltas.
            v = rng.uniform(60, 140)
            while v > 8 and remaining > 0:
                step = min(remaining, int(v))
                remaining -= step
                events.append(
                    {
                        "type": "mouseWheel",
                        "deltaX": 0,
                        "deltaY": step * direction,
                        "delay_ms": 16.7,  # ~60fps frame
                    }
                )
                v *= 0.95
        # Occasional reverse micro-scroll.
        if rng.random() < 0.12:
            back = int(rng.uniform(10, 40))
            events.append(
                {
                    "type": "mouseWheel",
                    "deltaX": 0,
                    "deltaY": -back * direction,
                    "delay_ms": round(rng.uniform(100, 400), 1),
                }
            )
    return events


# ---------------------------------------------------------------------------
# Typing
# ---------------------------------------------------------------------------

# Common digraphs typed faster (relative speedup factor).
_DIGRAPH_FAST = {
    "th", "he", "in", "er", "an", "re", "on", "at", "en", "nd",
    "ti", "es", "or", "te", "of", "ed", "is", "it", "al", "ar",
}


def human_type_plan(
    text: str,
    rng: Optional[random.Random] = None,
    wpm: float = 55.0,
) -> List[Dict[str, Any]]:
    """Plan human-like typing as CDP key events.

    Lognormal flight times (~40-150ms), digraph speedup, 2-5% typo +
    backspace corrections, per-key floor ~28ms. Uses dispatchKeyEvent
    (keyDown/char/keyUp), never insertText.
    """
    rng = rng or random.Random()
    events: List[Dict[str, Any]] = []
    base_interval = 60000.0 / (wpm * 5.0)  # ms per char at given WPM

    i = 0
    while i < len(text):
        ch = text[i]
        # Typo injection: 2-5% chance on letters.
        if ch.isalpha() and rng.random() < 0.035:
            wrong = rng.choice("abcdefghijklmnopqrstuvwxyz")
            events.extend(_key_events(wrong, rng, base_interval * 0.8))
            # Realize mistake, pause, backspace, continue.
            events.append({"type": "pause", "delay_ms": round(rng.uniform(150, 500), 1)})
            events.append(
                {
                    "type": "keyDown",
                    "key": "Backspace",
                    "code": "Backspace",
                    "windowsVirtualKeyCode": 8,
                    "delay_ms": round(rng.uniform(40, 120), 1),
                }
            )
            events.append(
                {
                    "type": "keyUp",
                    "key": "Backspace",
                    "code": "Backspace",
                    "windowsVirtualKeyCode": 8,
                    "delay_ms": round(rng.uniform(30, 80), 1),
                }
            )

        # Digraph speedup.
        interval = base_interval
        if i > 0:
            dg = (text[i - 1] + ch).lower()
            if dg in _DIGRAPH_FAST:
                interval *= rng.uniform(0.55, 0.8)
        # Lognormal-ish flight time.
        flight = rng.lognormvariate(math.log(interval), 0.45)
        flight = max(28.0, min(flight, 600.0))
        # Dwell time.
        dwell = rng.uniform(30, 110)
        events.extend(_key_events(ch, rng, flight, dwell))
        i += 1
    return events


def _key_events(
    ch: str,
    rng: random.Random,
    flight_ms: float,
    dwell_ms: float = 60.0,
) -> List[Dict[str, Any]]:
    if len(ch) == 1 and ch.isprintable():
        code = _dom_code(ch)
        vk = ord(ch.upper()) if ch.isalnum() else 0
        return [
            {
                "type": "keyDown",
                "key": ch,
                "code": code,
                "text": ch,
                "windowsVirtualKeyCode": vk,
                "delay_ms": round(flight_ms, 1),
            },
            {
                "type": "keyUp",
                "key": ch,
                "code": code,
                "windowsVirtualKeyCode": vk,
                "delay_ms": round(dwell_ms, 1),
            },
        ]
    return [
        {
            "type": "keyDown",
            "key": ch,
            "code": ch,
            "delay_ms": round(flight_ms, 1),
        },
        {"type": "keyUp", "key": ch, "code": ch,
         "delay_ms": round(dwell_ms, 1)},
    ]


def _dom_code(ch: str) -> str:
    if ch.isalpha():
        return "Key" + ch.upper()
    if ch.isdigit():
        return "Digit" + ch
    return {
        " ": "Space", ".": "Period", ",": "Comma", "/": "Slash",
        ";": "Semicolon", "'": "Quote", "[": "BracketLeft",
        "]": "BracketRight", "-": "Minus", "=": "Equal",
    }.get(ch, "Unidentified")


# ---------------------------------------------------------------------------
# Cadence
# ---------------------------------------------------------------------------

def action_gap_ms(rng: Optional[random.Random] = None) -> float:
    """Gaussian gap between high-level actions (never fixed intervals)."""
    rng = rng or random.Random()
    return round(max(250.0, rng.gauss(1200.0, 450.0)), 1)


@dataclass
class BehaviorConfig:
    """Tunable behavior profile. Defaults are the researched safe values."""

    seed: Optional[int] = None
    wpm: float = 55.0
    pre_click_hesitation: Tuple[float, float] = (60.0, 300.0)
    typo_rate: float = 0.035
    overshoot: bool = True

    def rng(self) -> random.Random:
        return random.Random(self.seed)
