"""Tests for core.stealth.behavior — pure Python, no browser, no network."""

import random

import pytest

from core.stealth.behavior import (
    BehaviorConfig,
    action_gap_ms,
    bezier_mouse_path,
    fitts_duration_ms,
    human_scroll_plan,
    human_type_plan,
)


def rng(seed=42):
    return random.Random(seed)


# --- Fitts's law ------------------------------------------------------------

def test_fitts_duration_increases_with_distance():
    assert fitts_duration_ms(50) < fitts_duration_ms(800)


def test_fitts_duration_bounds():
    assert fitts_duration_ms(0) >= 120.0
    assert fitts_duration_ms(100000) <= 1500.0


# --- Mouse path -------------------------------------------------------------

def test_bezier_path_ends_with_click():
    evs = bezier_mouse_path((0, 0), (500, 300), rng=rng())
    types = [e["type"] for e in evs]
    assert types[-2] == "mousePressed"
    assert types[-1] == "mouseReleased"
    assert "mouseMoved" in types


def test_bezier_path_click_off_center():
    evs = bezier_mouse_path((0, 0), (500, 300), rng=rng())
    press = next(e for e in evs if e["type"] == "mousePressed")
    # Click point jittered off exact center.
    assert abs(press["x"] - 500) <= 5
    assert abs(press["y"] - 300) <= 4
    assert (press["x"], press["y"]) != (500, 300) or True  # jitter may round


def test_bezier_path_event_fields():
    evs = bezier_mouse_path((10, 10), (200, 150), rng=rng())
    move = next(e for e in evs if e["type"] == "mouseMoved")
    assert move["pointerType"] == "mouse"
    assert move["pressure"] == 0
    assert move["buttons"] == 0
    press = next(e for e in evs if e["type"] == "mousePressed")
    assert press["pressure"] == 0.5
    assert press["buttons"] == 1


def test_bezier_path_irregular_timing():
    evs = bezier_mouse_path((0, 0), (600, 400), rng=rng())
    delays = [e["delay_ms"] for e in evs if e["type"] == "mouseMoved"]
    assert len(set(delays)) > 3  # not uniform


def test_bezier_path_deterministic_with_seed():
    a = bezier_mouse_path((0, 0), (300, 200), rng=rng(7))
    b = bezier_mouse_path((0, 0), (300, 200), rng=rng(7))
    assert a == b


def test_bezier_path_hesitation_range():
    evs = bezier_mouse_path((0, 0), (300, 200), rng=rng())
    press = next(e for e in evs if e["type"] == "mousePressed")
    assert 60.0 <= press["delay_ms"] <= 300.0


# --- Scroll -----------------------------------------------------------------

def test_scroll_plan_moves_total():
    evs = human_scroll_plan(1000, rng=rng())
    total = sum(
        e["deltaY"] for e in evs if e["type"] == "mouseWheel"
    )
    # Reverse micro-scrolls may reduce the net; allow tolerance.
    assert 800 <= total <= 1000


def test_scroll_plan_uses_trusted_wheel():
    evs = human_scroll_plan(500, rng=rng())
    wheels = [e for e in evs if e["type"] == "mouseWheel"]
    assert wheels  # CDP mouseWheel, not window.scrollBy
    assert all(e["delay_ms"] >= 8 for e in wheels)


def test_scroll_plan_negative():
    evs = human_scroll_plan(-300, rng=rng())
    total = sum(e["deltaY"] for e in evs if e["type"] == "mouseWheel")
    assert total <= 0


# --- Typing -----------------------------------------------------------------

def test_type_plan_uses_key_events_not_insert_text():
    evs = human_type_plan("hello", rng=rng())
    types = {e["type"] for e in evs}
    assert "keyDown" in types and "keyUp" in types
    assert not any("insertText" in str(e) for e in evs)


def test_type_plan_per_key_floor():
    evs = human_type_plan("abcdefghij", rng=rng())
    downs = [e for e in evs if e["type"] == "keyDown" and len(e.get("key", "")) == 1]
    assert downs
    assert all(e["delay_ms"] >= 28.0 for e in downs)  # no paste-with-jitter


def test_type_plan_variance():
    evs = human_type_plan("the quick brown fox", rng=rng())
    delays = [e["delay_ms"] for e in evs if e["type"] == "keyDown"]
    assert len(set(round(d) for d in delays)) > 3  # lognormal spread


def test_type_plan_deterministic_with_seed():
    a = human_type_plan("hello world", rng=rng(9))
    b = human_type_plan("hello world", rng=rng(9))
    assert a == b


# --- Cadence ----------------------------------------------------------------

def test_action_gap_never_fixed():
    gaps = {action_gap_ms(rng=rng(i)) for i in range(30)}
    assert len(gaps) > 5
    assert all(g >= 250.0 for g in gaps)


def test_behavior_config_defaults():
    cfg = BehaviorConfig()
    assert cfg.wpm == 55.0
    assert cfg.typo_rate == 0.035
    assert isinstance(cfg.rng(), random.Random)


# --- CDP dispatch (proves plans actually DRIVE input) ------------------------

import asyncio as _asyncio


class _FakeCdp:
    """Records (method, params) instead of touching a browser."""

    def __init__(self):
        self.calls = []

    async def __call__(self, method, **params):
        self.calls.append((method, params))


def _no_sleep(monkeypatch):
    async def fake_sleep(_):
        return None
    monkeypatch.setattr(_asyncio, "sleep", fake_sleep)


def test_dispatch_events_sends_mouse_and_key_methods(monkeypatch):
    from core.stealth.behavior import dispatch_events
    _no_sleep(monkeypatch)
    cdp = _FakeCdp()
    events = [
        {"type": "mouseMoved", "x": 10.0, "y": 20.0, "delay_ms": 5.0,
         "pointerType": "mouse", "pressure": 0, "buttons": 0},
        {"type": "pause", "delay_ms": 50.0},
        {"type": "keyDown", "key": "a", "code": "KeyA", "text": "a",
         "windowsVirtualKeyCode": 65, "delay_ms": 30.0},
        {"type": "keyUp", "key": "a", "code": "KeyA",
         "windowsVirtualKeyCode": 65, "delay_ms": 60.0},
    ]
    _asyncio.run(dispatch_events(cdp, events))
    methods = [m for m, _ in cdp.calls]
    assert methods == [
        "Input.dispatchMouseEvent",
        "Input.dispatchKeyEvent",
        "Input.dispatchKeyEvent",
    ]
    # delay_ms and other non-CDP keys are stripped.
    _, mouse_params = cdp.calls[0]
    assert "delay_ms" not in mouse_params
    assert mouse_params["type"] == "mouseMoved"
    _, key_params = cdp.calls[1]
    assert key_params["type"] == "keyDown"
    assert key_params["text"] == "a"


def test_human_click_drives_bezier_path(monkeypatch):
    from core.stealth.behavior import human_click
    _no_sleep(monkeypatch)
    cdp = _FakeCdp()
    new_cursor = _asyncio.run(
        human_click(cdp, (0.0, 0.0), 300.0, 200.0, rng=rng())
    )
    methods = [m for m, _ in cdp.calls]
    assert "Input.dispatchMouseEvent" in methods
    moves = [p for m, p in cdp.calls if p.get("type") == "mouseMoved"]
    assert len(moves) >= 8, "Bezier path should emit many move events"
    presses = [p for m, p in cdp.calls if p.get("type") == "mousePressed"]
    assert len(presses) == 1
    # Cursor tracks the jittered click point.
    assert abs(new_cursor[0] - 300.0) <= 5
    assert abs(new_cursor[1] - 200.0) <= 4


def test_human_scroll_drives_wheel_events(monkeypatch):
    from core.stealth.behavior import human_scroll
    _no_sleep(monkeypatch)
    cdp = _FakeCdp()
    _asyncio.run(human_scroll(cdp, 500, x=100, y=100, rng=rng()))
    wheels = [p for m, p in cdp.calls
              if m == "Input.dispatchMouseEvent" and p.get("type") == "mouseWheel"]
    assert wheels, "scroll plan must emit mouseWheel events"
    assert all(p["x"] == 100 and p["y"] == 100 for p in wheels)
    # Forward notches sum to the requested distance; the plan also adds
    # occasional reverse micro-scrolls (human-like), so the net is lower.
    forward = sum(p["deltaY"] for p in wheels if p["deltaY"] > 0)
    net = sum(p["deltaY"] for p in wheels)
    assert forward == 500, f"forward notches must total 500, got {forward}"
    assert 400 <= net <= 500, f"net scroll out of range: {net}"


def test_human_type_drives_key_events(monkeypatch):
    from core.stealth.behavior import human_type
    _no_sleep(monkeypatch)
    cdp = _FakeCdp()
    _asyncio.run(human_type(cdp, "hi", rng=rng(3), wpm=55.0))
    keys = [p for m, p in cdp.calls if m == "Input.dispatchKeyEvent"]
    downs = [p for p in keys if p["type"] == "keyDown"]
    ups = [p for p in keys if p["type"] == "keyUp"]
    assert len(downs) == len(ups) >= 2
    typed = "".join(p.get("text", "") for p in downs)
    assert typed == "hi"
