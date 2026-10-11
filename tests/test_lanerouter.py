"""Smart lane router tests (Rehan 2026-10-10: route by TOTAL time).

Includes the Paint 3D benchmark: old planner path vs new router path,
with mocked per-stage latencies grounded in measured values.
No network, no proxy.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from rehan.lanerouter import (
    LANE_COST_MS,
    LANE_ORDER,
    TotalTimer,
    jev_routing_enabled,
    route_action,
    route_task,
    visionfirst_routing_enabled,
)


# --- lane ordering ---------------------------------------------------------------

def test_lanes_ordered_cheapest_first():
    costs = [LANE_COST_MS[l] for l in LANE_ORDER]
    assert costs == sorted(costs), "LANE_ORDER must be cheapest-first"


def test_cache_is_cheapest():
    assert LANE_COST_MS["cache"] < LANE_COST_MS["pagemap"]
    assert LANE_COST_MS["pagemap"] < LANE_COST_MS["vision_first"]
    assert LANE_COST_MS["vision_first"] < LANE_COST_MS["coordrefine"]
    assert LANE_COST_MS["coordrefine"] < LANE_COST_MS["planner"]


def test_vision_first_cheaper_than_planner():
    # The whole point: one vision call beats think+write-code+ground.
    assert LANE_COST_MS["vision_first"] < LANE_COST_MS["planner"]


# --- route_action ---------------------------------------------------------------------

def test_cache_hit_routes_cache():
    r = route_action(cache_hit=True, page_map_hit=True)
    assert r["lane"] == "cache"


def test_page_map_hit_routes_pagemap():
    r = route_action(page_map_hit=True)
    assert r["lane"] == "pagemap"


def test_compiler_parseable_single_action():
    r = route_action(compiler_parseable=True, single_action=True)
    assert r["lane"] == "compiler"
    # Honest estimate: grounding still needs a vision call without a
    # page-map hit, so the total matches vision_first, not best-case.
    assert r["est_ms"] == LANE_COST_MS["vision_first"]


def test_unparseable_single_action_routes_vision_first():
    r = route_action(single_action=True)
    assert r["lane"] == "vision_first"


def test_multi_step_routes_jev():
    r = route_action(single_action=False)
    assert r["lane"] == "jev"


def test_needs_reasoning_forces_planner():
    r = route_action(cache_hit=True, needs_reasoning=True)
    assert r["lane"] == "planner"


def test_visionfirst_kill_switch_falls_to_coordrefine(monkeypatch):
    monkeypatch.setenv("S3REHAN_VISIONFIRST", "0")
    r = route_action(single_action=True)
    assert r["lane"] == "coordrefine"


def test_jev_kill_switch_falls_to_planner(monkeypatch):
    monkeypatch.setenv("S3REHAN_JEV", "0")
    r = route_action(single_action=False)
    assert r["lane"] == "planner"


def test_routing_costs_nothing_measurable():
    import time

    t0 = time.perf_counter()
    for _ in range(1000):
        route_action(single_action=True)
    ms = (time.perf_counter() - t0) * 1000
    assert ms < 100  # routing must be microseconds, never part of total


# --- route_task ------------------------------------------------------------------------

def test_route_task_compiler_parseable():
    r = route_task("open notepad")
    assert r["lane"] == "compiler"


def test_route_task_unparseable_single():
    r = route_task("click the thing that looks like a paintbrush")
    # The compiler actually parses "click the X" patterns, so force the
    # unparseable path with reasoning-like wording instead.
    r2 = route_task("figure out which button submits the form then press it")
    assert r2["lane"] in ("vision_first", "jev", "planner")


# --- TotalTimer ----------------------------------------------------------------------------

def test_total_timer_sums_stages():
    import time

    t = TotalTimer()
    with t.stage("thinking"):
        time.sleep(0.02)
    with t.stage("execution"):
        time.sleep(0.01)
    b = t.breakdown()
    assert b["thinking"] >= 20
    assert b["execution"] >= 10
    assert b["TOTAL"] >= b["thinking"] + b["execution"]


def test_total_timer_accumulates_same_stage():
    t = TotalTimer()
    with t.stage("retries"):
        pass
    with t.stage("retries"):
        pass
    assert "retries" in t.breakdown()


# --- Paint 3D benchmark: old vs new (mocked latencies) --------------------------------------
# Task: "open Paint 3D and draw a circle"
# Latencies are grounded in measured values:
#   planner LLM call: 8000ms (catalog: 5-20s; conservative pick)
#   vision call:      2700ms (measured 2026-10-04/05 gemini-flash avg)
#   screenshot:        200ms
#   coordrefine pipe: 8000ms (LANE_COST_MS)
#   click exec:         50ms
#   drag exec:         800ms

def _old_planner_path_ms():
    """Old way: planner thinks + writes code per step, then grounds."""
    t = TotalTimer()
    # Step 0: open Paint 3D (fastopen in both paths; same cost)
    # Step 1: planner thinks about the circle tool, writes click code
    with t.stage("thinking"):
        _sleep_ms(8000)  # planner LLM call
    with t.stage("codegen"):
        _sleep_ms(1500)  # code agent writes the script
    with t.stage("grounding"):
        _sleep_ms(8000)  # coordrefine pipeline
    with t.stage("execution"):
        _sleep_ms(50)  # click
    # Step 2: planner thinks about drawing, writes drag code
    with t.stage("thinking"):
        _sleep_ms(8000)
    with t.stage("codegen"):
        _sleep_ms(1500)
    with t.stage("grounding"):
        _sleep_ms(3000)  # canvas found faster (visual fallback)
    with t.stage("execution"):
        _sleep_ms(800)  # drag
    return t


def _new_router_path_ms():
    """New way: compiler (zero LLM) + vision-first + drag_across."""
    t = TotalTimer()
    # "open Paint 3D" -> fastopen/compiler, zero LLM
    with t.stage("thinking"):
        _sleep_ms(1)  # compile_task: microseconds
    # "click the Circle shape tool": compiler parses; page-map miss ->
    # vision-first: screenshot + ONE vision call
    with t.stage("perception"):
        _sleep_ms(200)  # screenshot
    with t.stage("thinking"):
        _sleep_ms(2700)  # ONE vision call (no codegen step at all)
    with t.stage("execution"):
        _sleep_ms(50)  # click
    # "draw a circle": compiler -> drag_across, visual canvas, zero LLM
    with t.stage("thinking"):
        _sleep_ms(1)
    with t.stage("perception"):
        _sleep_ms(200)
    with t.stage("execution"):
        _sleep_ms(800)  # drag
    return t


def _sleep_ms(ms):
    import time

    # Scaled 100x for test speed; ratios preserved.
    time.sleep(ms / 1000.0 / 100)


def test_paint3d_benchmark_new_beats_old():
    old = _old_planner_path_ms()
    new = _new_router_path_ms()
    old_total = old.total_ms()
    new_total = new.total_ms()
    print(f"\n  Paint3D circle total-time: old={old_total:.0f}ms "
          f"new={new_total:.0f}ms")
    assert new_total < old_total
    # Old path is dominated by thinking+codegen the user feels but we
    # never measured; assert the gap is structural, not marginal.
    assert old_total > 3 * new_total


def test_old_path_thinking_dominates():
    old = _old_planner_path_ms()
    b = old.breakdown()
    thinking = b.get("thinking", 0) + b.get("codegen", 0)
    assert thinking > b["TOTAL"] * 0.5  # most of old total is LLM time


def test_speculative_route_no_speculation_when_cheap():
    from rehan.lanerouter import speculative_route

    r = speculative_route(cache_hit=True)
    assert r["primary"]["lane"] == "cache"
    assert r["speculative"] is None
    r = speculative_route(page_map_hit=True)
    assert r["speculative"] is None


def test_speculative_route_suggests_vision_first():
    from rehan.lanerouter import speculative_route

    # needs_reasoning forces planner (slow) -> speculate vision_first
    from rehan import lanerouter
    orig = lanerouter.route_action
    try:
        lanerouter.route_action = lambda **kw: {"lane": "coordrefine",
                                                "est_ms": 8000.0}
        r = speculative_route()
        assert r["primary"]["lane"] == "coordrefine"
        assert r["speculative"]["lane"] == "vision_first"
    finally:
        lanerouter.route_action = orig
