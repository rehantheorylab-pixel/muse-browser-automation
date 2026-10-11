"""Vision-first lane tests (Rehan 2026-10-10: total-time rebuild).

All with mocked vision — no network, no proxy.
"""
import io
import os
import sys
import time

import pytest
from PIL import Image

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from rehan.visionfirst import (
    VisionFirst,
    VisionFirstError,
    VisionFirstNoCoords,
    parse_coords,
    visionfirst_enabled,
    LOCATE_PROMPT,
    GROUND_W,
    GROUND_H,
)


def _png(w=800, h=600):
    buf = io.BytesIO()
    Image.new("RGB", (w, h), "white").save(buf, format="PNG")
    return buf.getvalue()


# --- parse_coords ------------------------------------------------------------

def test_parse_coords_plain():
    assert parse_coords("250, 496") == (250, 496)


def test_parse_coords_no_space():
    assert parse_coords("250,496") == (250, 496)


def test_parse_coords_in_sentence():
    assert parse_coords("The button is at 300, 400 I think") == (300, 400)


def test_parse_coords_rejects_out_of_range():
    assert parse_coords("1500, 2000") is None


def test_parse_coords_rejects_empty():
    assert parse_coords("") is None
    assert parse_coords("no numbers here") is None


def test_parse_coords_rejects_degenerate_repeat():
    # "500, 500" with both >100 is likely a repeated token, not a point.
    assert parse_coords("500, 500") is None


def test_parse_coords_allows_origin_like():
    assert parse_coords("0, 0") == (0, 0)


def test_parse_coords_takes_first_plausible():
    assert parse_coords("1500, 2000 then 100, 200") == (100, 200)


# --- env flag -----------------------------------------------------------------

def test_visionfirst_enabled_default(monkeypatch):
    monkeypatch.delenv("S3REHAN_VISIONFIRST", raising=False)
    assert visionfirst_enabled() is True


def test_visionfirst_env_kill_switch(monkeypatch):
    monkeypatch.setenv("S3REHAN_VISIONFIRST", "0")
    assert visionfirst_enabled() is False


# --- VisionFirst.locate ----------------------------------------------------------

def test_locate_returns_coords_and_timing():
    vf = VisionFirst(lambda png, prompt: "250, 496")
    r = vf.locate("the OK button", _png(), 1920, 1080)
    assert r["x"] == 250 and r["y"] == 496
    assert r["model_calls"] == 1
    assert r["ms"] >= 0


def test_locate_retries_on_garbage_then_succeeds():
    calls = []

    def mock(png, prompt):
        calls.append(1)
        return "no idea" if len(calls) == 1 else "300, 400"

    vf = VisionFirst(mock, attempts=2)
    r = vf.locate("the OK button", _png(), 1920, 1080)
    assert (r["x"], r["y"]) == (300, 400)
    assert r["model_calls"] == 2
    assert r["attempts_used"] == 2


def test_locate_raises_after_attempts_exhausted():
    vf = VisionFirst(lambda png, prompt: "no idea", attempts=2)
    with pytest.raises(VisionFirstNoCoords):
        vf.locate("the OK button", _png(), 1920, 1080)


def test_locate_prompt_mentions_target():
    seen = {}

    def mock(png, prompt):
        seen["prompt"] = prompt
        return "1, 1"

    VisionFirst(mock).locate("the red Submit button", _png(), 1920, 1080)
    assert "the red Submit button" in seen["prompt"]
    assert "0-1000" in seen["prompt"]


def test_locate_prompt_is_coordinate_only():
    assert "ONLY" in LOCATE_PROMPT
    assert "code" not in LOCATE_PROMPT.lower() or "no" in LOCATE_PROMPT.lower()


# --- ground_to_screen ------------------------------------------------------------

def test_ground_to_screen_scales():
    vf = VisionFirst(lambda png, prompt: "500, 250")
    r = vf.ground_to_screen("x", _png(), 1920, 1080)
    assert r["sx"] == 960
    assert r["sy"] == 270


# --- click ------------------------------------------------------------------------

def test_click_calls_click_fn_with_screen_pixels():
    seen = {}
    vf = VisionFirst(lambda png, prompt: "500, 250",
                     click_fn=lambda x, y: seen.update(x=x, y=y))
    r = vf.click("the button", _png(), 1920, 1080)
    assert seen == {"x": 960, "y": 270}
    assert r["action"] == "click"
    assert r["ms"] >= 0  # total time includes the click


def test_click_without_click_fn_raises():
    vf = VisionFirst(lambda png, prompt: "1, 1")
    with pytest.raises(VisionFirstError):
        vf.click("x", _png(), 1920, 1080)


# --- input_scale (research: visual token pruning) -------------------------------------

def test_input_scale_downscales_png_sent_to_model():
    seen = {}

    def mock(png, prompt):
        seen["png"] = png
        return "100, 100"

    vf = VisionFirst(mock, input_scale=0.5)
    vf.locate("x", _png(800, 600), 1920, 1080)
    from PIL import Image
    import io as _io

    w, h = Image.open(_io.BytesIO(seen["png"])).size
    assert (w, h) == (400, 300)


def test_input_scale_full_res_untouched():
    seen = {}

    def mock(png, prompt):
        seen["png"] = png
        return "100, 100"

    orig = _png(800, 600)
    VisionFirst(mock, input_scale=1.0, grid_overlay=False).locate(
        "x", orig, 1920, 1080)
    assert seen["png"] == orig


def test_input_scale_clamped():
    vf = VisionFirst(lambda p, pr: "1, 1", input_scale=5.0)
    assert vf.input_scale == 1.0
    vf2 = VisionFirst(lambda p, pr: "1, 1", input_scale=0.01)
    assert vf2.input_scale == 0.25


def test_input_scale_coords_still_1000_space():
    # Downscaling must NOT change the output contract: the model still
    # replies in 0-1000 space, so screen mapping is unaffected.
    vf = VisionFirst(lambda png, prompt: "500, 250", input_scale=0.5)
    r = vf.ground_to_screen("x", _png(), 1920, 1080)
    assert r["sx"] == 960 and r["sy"] == 270


# --- timing honesty ------------------------------------------------------------------
# Rehan's core demand: total time must include the vision call.

def test_locate_ms_includes_vision_latency():
    def slow_mock(png, prompt):
        time.sleep(0.05)
        return "100, 100"

    vf = VisionFirst(slow_mock)
    r = vf.locate("x", _png(), 1920, 1080)
    assert r["ms"] >= 50  # the 50ms sleep is inside the measured window
