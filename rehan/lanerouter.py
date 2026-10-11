"""Smart lane router for s3rehan: pick by TOTAL user-perceived time.

Rehan's insight (2026-10-10): the user feels
    TOTAL = thinking + code-writing + grounding + execution + retries
not just execution time. Every lane below is ranked by measured or
empirically-grounded TOTAL time for a single action, and the router
always picks the cheapest lane that can handle the action.

Lane ranking (single action, total time):
  1. "cache"        coordcache hit ........................ ~50ms
  2. "pagemap"       UIA page-map hit ...................... ~200ms
  3. "compiler"      task compiler, easy grounding ......... ~800ms
  4. "vision_first"  screenshot + ONE vision call .......... ~3000ms
  5. "jev"           observe + decide + execute ............ ~3500ms/step
  6. "coordrefine"   UIA+OCR+contours+vision pipeline ...... ~8000ms
  7. "planner"       think + write code + ground ........... ~15000ms/step

Notes on the estimates:
  - "cache": dict lookup + pyautogui click; measured in coordcache tests.
  - "pagemap": match_element over the UIA walk; microseconds + click.
  - "compiler": zero LLM calls; grounding is the only cost (page-map hit
    assumed; a miss escalates the estimate to vision_first).
  - "vision_first": screenshot ~200ms + one vision call 1.9-3.9s
    (measured 2026-10-04/05: gemini-flash avg ~2.7s) + click ~50ms.
  - "jev": observe ~1000ms (screenshot+UIA+table) + decide ~2400ms
    (measured 2026-10-04) + execute ~100ms.
  - "coordrefine": UIA fusion ~500ms + OCR ~1000ms + contours ~500ms +
    2-3 vision calls + pre-click verify ~1000ms (measured paths).
  - "planner": planner LLM 5-20s (catalog) + code-gen + grounding.

The router is a pure function of observable signals — no model calls —
so routing itself costs microseconds and never adds to total time.

Env: S3REHAN_VISIONFIRST=0 removes the vision_first lane from routing
(default ON). S3REHAN_JEV=0 removes the jev lane (default ON).
"""

import os
from typing import Dict, List, Optional

# Estimated TOTAL ms per lane for one action (see module docstring).
# Strictly ascending in LANE_ORDER below.
LANE_COST_MS: Dict[str, float] = {
    "cache": 50.0,
    "pagemap": 200.0,
    "compiler": 800.0,
    "vision_first": 3000.0,
    # jev per step: observe (~1000ms screenshot+UIA+table) + decide
    # (~2400ms measured) + execute (~100ms).
    "jev": 3500.0,
    "coordrefine": 8000.0,
    "planner": 15000.0,
}

# Ordered cheapest-first. The router walks this list and takes the first
# lane whose requirements are met.
LANE_ORDER: List[str] = [
    "cache",
    "pagemap",
    "compiler",
    "vision_first",
    "jev",
    "coordrefine",
    "planner",
]


def visionfirst_routing_enabled() -> bool:
    return os.environ.get("S3REHAN_VISIONFIRST", "1") != "0"


def jev_routing_enabled() -> bool:
    return os.environ.get("S3REHAN_JEV", "1") != "0"


def route_action(
    *,
    cache_hit: bool = False,
    page_map_hit: bool = False,
    compiler_parseable: bool = False,
    single_action: bool = True,
    needs_reasoning: bool = False,
) -> Dict:
    """Pick the cheapest lane for one action. Returns {"lane", "est_ms"}.

    Signals (all cheap, observable without model calls):
      cache_hit:          coordcache.get() returned a point on a fresh frame
      page_map_hit:        match_element() found the target in the UIA walk
      compiler_parseable:  compile_task() parsed the task (zero LLM)
      single_action:       the step is one click/type (not a multi-step plan)
      needs_reasoning:     the task needs judgment no lane can shortcut
                           (forces planner regardless of cost)
    """
    if needs_reasoning:
        return {"lane": "planner", "est_ms": LANE_COST_MS["planner"]}
    if cache_hit:
        return {"lane": "cache", "est_ms": LANE_COST_MS["cache"]}
    if page_map_hit:
        return {"lane": "pagemap", "est_ms": LANE_COST_MS["pagemap"]}
    if compiler_parseable and single_action:
        # Compiler actions still need grounding; with no page-map hit the
        # honest estimate is vision_first's cost (one vision call), not the
        # best-case 800ms. Report the lane as compiler (zero LLM) with the
        # realistic total.
        return {"lane": "compiler", "est_ms": LANE_COST_MS["vision_first"]}
    if compiler_parseable:
        return {"lane": "compiler", "est_ms": LANE_COST_MS["compiler"]}
    if single_action and visionfirst_routing_enabled():
        return {"lane": "vision_first", "est_ms": LANE_COST_MS["vision_first"]}
    if single_action:
        return {"lane": "coordrefine", "est_ms": LANE_COST_MS["coordrefine"]}
    if jev_routing_enabled():
        return {"lane": "jev", "est_ms": LANE_COST_MS["jev"]}
    return {"lane": "planner", "est_ms": LANE_COST_MS["planner"]}


def route_task(task_text: str, n_actions: int = 1) -> Dict:
    """Coarse task-level routing (used before any perception is captured).

    Only the compiler signal is available pre-perception, so this is
    intentionally conservative: parseable -> compiler lane, else jev for
    short tasks, planner for long/complex ones. Per-action route_action()
    refines the choice once cache/page-map signals exist.
    """
    try:
        from rehan.compiler import compile_task

        parsed = compile_task(task_text) is not None
    except Exception:
        parsed = False
    if parsed:
        return {"lane": "compiler", "est_ms": LANE_COST_MS["compiler"] * max(1, n_actions)}
    if n_actions <= 1:
        if visionfirst_routing_enabled():
            return {"lane": "vision_first", "est_ms": LANE_COST_MS["vision_first"]}
        return {"lane": "coordrefine", "est_ms": LANE_COST_MS["coordrefine"]}
    if jev_routing_enabled():
        return {"lane": "jev", "est_ms": LANE_COST_MS["jev"] * n_actions}
    return {"lane": "planner", "est_ms": LANE_COST_MS["planner"] * n_actions}


class TotalTimer:
    """Honest total-time measurement: from user request to action done.

    Records named stages (perception, thinking, codegen, grounding,
    execution, retries...). The user feels the SUM — this makes it
    visible. Use as a context manager per stage:

        t = TotalTimer()
        with t.stage("thinking"):
            ...llm call...
        print(t.total_ms(), t.breakdown())
    """

    def __init__(self):
        import time as _time

        self._time = _time
        self._t0 = _time.perf_counter()
        self.stages: Dict[str, float] = {}
        self._open: Optional[str] = None
        self._open_t0: float = 0.0

    def stage(self, name: str):
        return _Stage(self, name)

    def total_ms(self) -> float:
        return (self._time.perf_counter() - self._t0) * 1000.0

    def breakdown(self) -> Dict[str, float]:
        out = dict(self.stages)
        out["TOTAL"] = self.total_ms()
        return out


class _Stage:
    def __init__(self, timer: TotalTimer, name: str):
        self._timer = timer
        self._name = name

    def __enter__(self):
        self._t0 = self._timer._time.perf_counter()
        return self

    def __exit__(self, *exc):
        ms = (self._timer._time.perf_counter() - self._t0) * 1000.0
        self._timer.stages[self._name] = (
            self._timer.stages.get(self._name, 0.0) + ms
        )
        return False


def speculative_route(
    *,
    cache_hit: bool = False,
    page_map_hit: bool = False,
    compiler_parseable: bool = False,
    single_action: bool = True,
) -> Dict:
    """SpecEyes-style speculative routing (Tier 2, DEEP_RESEARCH_ROUND2.md §7).

    Returns BOTH the primary lane AND a speculative cheap lane to run in
    parallel. The caller runs the cheap lane's perception concurrently
    with the primary lane's setup; if the cheap lane yields a confident
    result first, the expensive lane is cancelled.

    Returns {"primary": {...}, "speculative": {...} or None}.
    Speculation only helps when the primary is vision_first or slower
    (cache/page-map/compiler are already the cheapest — nothing to beat).
    """
    primary = route_action(
        cache_hit=cache_hit,
        page_map_hit=page_map_hit,
        compiler_parseable=compiler_parseable,
        single_action=single_action,
    )
    # Speculation only helps when the primary is SLOWER than vision_first.
    # (cache/page-map/compiler/vision_first are already the cheapest —
    # nothing to beat.)
    if primary["lane"] in ("cache", "pagemap", "compiler", "vision_first"):
        return {"primary": primary, "speculative": None}
    if visionfirst_routing_enabled():
        return {
            "primary": primary,
            "speculative": {"lane": "vision_first",
                            "est_ms": LANE_COST_MS["vision_first"]},
        }
    return {"primary": primary, "speculative": None}


# ---------------------------------------------------------------------------
# Display-isolation routing (unified architecture, 2026-10-10)
# ---------------------------------------------------------------------------
# Every GUI task needs a screen to act on. Priority:
#   1. "monitor"  — virtual monitor (IddCx): own real screen, zero flicker.
#   2. "vdesktop" — Windows virtual desktop: fallback, switch-capture.
#   3. "inline"   — no isolation: headless/API tasks, or nothing available
#                   (background-only input is then mandatory).
# Pure function — microseconds, no model calls.

DISPLAY_ORDER = ["monitor", "vdesktop", "inline"]


def vmonitor_routing_enabled() -> bool:
    return os.environ.get("S3REHAN_NO_VMONITOR", "0") != "1"


def route_display(
    task: Optional[Dict] = None,
    *,
    needs_gui: Optional[bool] = None,
    monitors_available: bool = False,
    vdesktop_available: bool = False,
) -> Dict:
    """Pick the display-isolation backend for a task.

    Returns {"backend", "reason"}. ``needs_gui`` defaults to the task's
    ``needs_gui``/``needs_windows_ui`` flags (either true counts).
    Non-GUI tasks route "inline" — they need no screen at all.
    """
    task = task or {}
    if needs_gui is None:
        needs_gui = bool(task.get("needs_gui") or
                         task.get("needs_windows_ui"))
    if not needs_gui:
        return {"backend": "inline",
                "reason": "no GUI — headless/API task needs no screen"}
    if monitors_available and vmonitor_routing_enabled():
        return {"backend": "monitor",
                "reason": "virtual monitor: own screen, zero flicker"}
    if vdesktop_available:
        return {"backend": "vdesktop",
                "reason": "virtual desktop fallback (no VDD driver)"}
    return {"backend": "inline",
            "reason": ("no display isolation available — background-only "
                       "input required")}


def route_placement(task: Optional[Dict] = None, **kwargs) -> Dict:
    """Combined platform + display routing for the unified worker pool.

    Returns {"platform": "linux"|"windows", "display": {...route_display}}.
    Platform comes from rehan.linux_worker.route_platform; display from
    route_display above. kwargs are passed to route_display
    (monitors_available, vdesktop_available, needs_gui).
    """
    try:
        from rehan import linux_worker as _lw

        platform = _lw.route_platform(task or {})
    except Exception:
        platform = "windows"
    return {"platform": platform,
            "display": route_display(task, **kwargs)}
