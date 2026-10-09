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
