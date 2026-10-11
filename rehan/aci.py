"""FastACI: OSWorldACI with a page-map fast path for coordinate grounding.

The planner writes exactly the same action code as upstream S3
(e.g. grounding_agent.click("the red Submit button")). generate_coords first
tries a local page-map lookup (zero tokens, microseconds); only on a miss
does it call the vision grounding model, exactly like upstream.

All 15 upstream actions, notes/knowledge, text-span grounding and the code
agent are inherited untouched.
"""

from typing import Dict, List
import re

from gui_agents.s3.agents.grounding import OSWorldACI, agent_action

from rehan.perception import match_element


class FastACI(OSWorldACI):
    # Measured 2026-09-30 on a gpt-4o-class vision endpoint:    # the UI-TARS-style "output only the coordinate" prompt leaves content empty
    # (answer trapped in reasoning). This strict variant returns "x, y" in
    # content, in 1000-space, accurate to a few px on a known-target test.
    STRICT_GROUND_PROMPT = (
        "Locate {ref}. Reply with ONLY its center point as two integers like "
        "250, 496. No explanation, no other text."
    )

    def _ask_vision(self, prompt: str, png: bytes, attempts: int = 2) -> str:
        """One semantic vision question; the model answers with words or a
        number, NEVER with absolute pixel coordinates (Rehan's research:
        coordinate estimation is the model's weak point, semantic judgment
        is its strong point)."""
        from gui_agents.s3.utils.common_utils import call_llm_safe

        last = ""
        for _ in range(attempts):
            self.grounding_model.reset()
            self.grounding_model.add_message(
                text_content=prompt,
                image_content=png,
                put_text_last=True,
            )
            last = call_llm_safe(self.grounding_model, max_tokens=300) or ""
            if last.strip():
                print("RAW VISION REPLY:", last[:160])
                return last
        return last

    def _vision_ground(self, ref_expr: str, obs: Dict, attempts: int = 3) -> List[int]:
        from gui_agents.s3.utils.common_utils import call_llm_safe

        # Measured 2026-09-30: a gpt-4o-class vision endpoint is accurate when it
        # answers (~pixel-perfect) but intermittently leaves content empty
        # (answer trapped in the reasoning field). Retry the ask itself.
        last = ""
        for _ in range(attempts):
            self.grounding_model.reset()
            # UI-TARS demo uses no system prompt; keep that, put text last.
            self.grounding_model.add_message(
                text_content=self.STRICT_GROUND_PROMPT.format(ref=ref_expr),
                image_content=obs["screenshot"],
                put_text_last=True,
            )
            last = call_llm_safe(self.grounding_model) or ""
            numericals = re.findall(r"\d+", last)
            if len(numericals) >= 2:
                print("RAW GROUNDING MODEL RESPONSE:", last)
                return [int(numericals[0]), int(numericals[1])]
        raise AssertionError(
            "vision grounder returned no coordinates after "
            f"{attempts} tries (last: {last[:120]!r})"
        )

    def generate_coords(self, ref_expr: str, obs: Dict) -> List[int]:
        elements = (obs or {}).get("page_map_elements") or []
        # Last grounding's instrumentation (per-stage timings, model calls,
        # screen hash) for the runner's staleness gate. Reset per call.
        self.last_ground_detail = None
        hit = match_element(ref_expr, elements)
        if hit is not None:
            # Page-map stores screen pixels; upstream actions expect
            # grounding-model space and call resize_coordinates afterwards,
            # so inverse-scale here for an exact round-trip (±1px).
            gw = self.engine_params_for_grounding["grounding_width"]
            gh = self.engine_params_for_grounding["grounding_height"]
            return [
                round(hit["x"] * gw / self.width),
                round(hit["y"] * gh / self.height),
            ]
        if getattr(self, "pagemap_only", False):
            # The runner polls fresh UIA a few times on a loading screen
            # before it ever allows the ~20s vision fallback.
            raise LookupError(f"page-map miss (pagemap_only): {ref_expr!r}")
        # --- s3rehan: lane router by TOTAL time (rehan/lanerouter.py). ---
        # Order: cache (~50ms) -> vision-first (~3s, one call) ->
        # coordrefine pipeline (~8s) -> legacy vision. The cache probe is
        # explicit here so a cache hit never pays for a vision call.
        png = (obs or {}).get("screenshot")
        if png:
            try:
                from rehan import coordcache as _cc

                _shash = _cc.screen_hash(png)
                _app = (obs or {}).get("app_name")
                _winrect = (obs or {}).get("window_rect")
                try:
                    _dpi = round(float((obs or {}).get("dpi_scale", 1.0)), 2)
                except Exception:
                    _dpi = 1.0
                from PIL import Image
                import io as _io

                _sw, _sh = Image.open(_io.BytesIO(png)).size
                _chit = _cc.CACHE.get(_app, _winrect, (_sw, _sh), _dpi,
                                      ref_expr, _shash)
                if _chit:
                    gw = self.engine_params_for_grounding["grounding_width"]
                    gh = self.engine_params_for_grounding["grounding_height"]
                    self.last_ground_detail = {
                        "path": "cache", "model_calls": 0, "ms": 5.0,
                        "screen_hash": _shash}
                    print(f"CACHE HIT: {ref_expr!r} -> "
                          f"({_chit['x']}, {_chit['y']})")
                    return [round(_chit["x"] * gw / self.width),
                            round(_chit["y"] * gh / self.height)]
            except Exception as _ce:  # noqa: BLE001
                # Cache probe must never break grounding; fall through.
                print(f"cache probe skipped: {_ce!r}")
        # Vision-first lane: one screenshot -> one vision call -> coords.
        # Beats coordrefine on total time for hard targets (one 2-4s call
        # vs a 5-15s multi-stage pipeline). Falls through to coordrefine
        # when the model yields nothing usable.
        if png:
            try:
                from rehan.visionfirst import (
                    VisionFirst,
                    VisionFirstError,
                    visionfirst_enabled,
                )

                if visionfirst_enabled():
                    _v = VisionFirst(
                        lambda p, pr: self._ask_vision(pr, p, attempts=1))
                    _r = _v.locate(ref_expr, png, self.width, self.height)
                    self.last_ground_detail = {
                        "path": "vision-first",
                        "model_calls": _r["model_calls"],
                        "ms": _r["ms"],
                        "screen_hash": None,
                    }
                    print(f"VISION-FIRST: {ref_expr!r} -> "
                          f"({_r['x']}, {_r['y']}) "
                          f"{_r['ms']:.0f}ms calls={_r['model_calls']}")
                    return [_r["x"], _r["y"]]
            except VisionFirstError as _ve:
                print(f"VISION-FIRST miss: {_ve}; trying coordrefine")
            except Exception as _ve:  # noqa: BLE001
                print(f"VISION-FIRST error ({_ve!r}); trying coordrefine")
        # Miss: Rehan's coordinate research pipeline FIRST — candidate fusion
        # (UIA+OCR+CPU contours), Set-of-Marks number selection, CPU edge
        # probing, and pre-click ring verification. The model is asked only
        # semantic questions (which NUMBER? what's UNDER the ring?), never
        # for absolute coordinates. Falls through to the legacy absolute-
        # coordinate prompt only if refinement cannot ground reliably.
        import os as _os

        try:
            from rehan import coordrefine

            _preclick = _os.environ.get("S3REHAN_PRECLICK_VERIFY", "1") == "1"
            _x, _y, _detail = coordrefine.refine(
                ref_expr, obs, self._ask_vision, preclick=_preclick
            )
            self.last_ground_detail = _detail
            print(f"COORDREFINE: {ref_expr!r} -> ({_x}, {_y}) "
                  f"path={_detail['path']} calls={_detail['model_calls']} "
                  f"{_detail['ms']:.0f}ms")
            gw = self.engine_params_for_grounding["grounding_width"]
            gh = self.engine_params_for_grounding["grounding_height"]
            return [round(_x * gw / self.width), round(_y * gh / self.height)]
        except LookupError as _e:
            print(f"COORDREFINE miss: {_e}; trying legacy vision grounding")
            self.last_ground_detail = {"path": "legacy-vision",
                                       "model_calls": 0, "ms": 0.0,
                                       "screen_hash": None}
        except Exception as _e:  # noqa: BLE001
            print(f"COORDREFINE error ({_e!r}); trying legacy vision grounding")
            self.last_ground_detail = {"path": "legacy-vision",
                                       "model_calls": 0, "ms": 0.0,
                                       "screen_hash": None}
        # Last resort: legacy absolute-coordinate vision grounding (upstream
        # behavior, strict prompt). Kept so full S3 functionality is preserved.
        return self._vision_ground(ref_expr, obs)

    @agent_action
    def drag_across(self, element_description: str):
        """Drag the mouse diagonally across an element to draw a circle.

        Deterministic drawing primitive: use it after selecting a shape tool
        (e.g. drag across the Paint 3D canvas to draw a circle). The drag is
        a SQUARE diagonal (top-left -> bottom-right of the largest centered
        square, 15% margin) so the oval tool produces a circle, not an
        ellipse. The box comes from the page-map; for a white drawing canvas
        (a GPU surface invisible to the UIA tree) it falls back to visual
        white-rectangle detection on the screenshot — no vision model call.
        Args:
            element_description:str, a detailed description of the element to drag across, e.g. "the Paint 3D canvas".
        """
        from rehan.perception import find_white_canvas

        elements = (self.obs or {}).get("page_map_elements") or []
        hit = match_element(element_description, elements)
        # UIA boxes are center-based: {"x","y"} = center, plus "w","h".
        box = None
        if hit and hit.get("w") and hit.get("h"):
            box = {
                "cx": hit["x"],
                "cy": hit["y"],
                "w": hit["w"],
                "h": hit["h"],
            }
        if "canvas" in element_description.lower():
            shot = (self.obs or {}).get("screenshot")
            use_visual = box is None
            if not use_visual and shot:
                # A "canvas" match covering most of the screen is the window
                # container, not the drawable canvas — prefer vision.
                try:
                    from PIL import Image
                    import io as _io

                    sw, sh = Image.open(_io.BytesIO(shot)).size
                    if box["w"] * box["h"] > 0.60 * sw * sh:
                        use_visual = True
                except Exception:
                    pass
            if use_visual and shot:
                v = find_white_canvas(shot)
                if v:
                    box = {
                        "cx": v["x"] + v["w"] // 2,
                        "cy": v["y"] + v["h"] // 2,
                        "w": v["w"],
                        "h": v["h"],
                    }
        if box is None:
            raise ValueError(
                "drag_across: no page-map bounding box for "
                f"{element_description!r}; use drag_and_drop instead"
            )
        side = int(min(box["w"], box["h"]) * 0.70)
        x1, y1 = box["cx"] - side // 2, box["cy"] - side // 2
        x2, y2 = box["cx"] + side // 2, box["cy"] + side // 2
        return (
            "import pyautogui; "
            # Focus-click: the canvas (DirectX surface) ignores drag gestures
            # from synthetic input until it receives an explicit click.
            f"pyautogui.click({box['cx']}, {box['cy']}); "
            f"pyautogui.moveTo({x1}, {y1}); "
            f"pyautogui.dragTo({x2}, {y2}, duration=0.8, button='left'); "
        )


class GroundingLiteACI(FastACI):
    """FastACI with ONLY the vision grounding engine (fast path).

    OSWorldACI.__init__ builds 3 LLM engines (grounding + text-span +
    code-agent, ~700ms of SDK imports each). The compiled fast path only
    ever calls generate_coords (page-map, then vision fallback) and the
    plain pyautogui action builders — it never touches the text-span or
    code agents. This lite version builds the single grounding engine,
    cutting fast-path startup from ~3.7s to ~0.7s.

    If the fast path fails and the runner falls back to the LLM planner,
    the full AgentS3 (with all engines) is built then.
    """

    def __init__(self, env, platform, engine_params_for_grounding,
                 width=1920, height=1080):
        # Bypass OSWorldACI.__init__ (3 engines); grounding only.
        super(OSWorldACI, self).__init__()  # ACI base: notes = []
        from gui_agents.s3.core.mllm import LMMAgent

        self.env = env
        self.platform = platform
        self.width = width
        self.height = height
        self.notes = []
        self.obs = None
        self.grounding_model = LMMAgent(engine_params_for_grounding)
        self.pagemap_only = False  # runner polls UIA before vision fallback
        self.engine_params_for_grounding = engine_params_for_grounding
        self.text_span_agent = None
        self.code_agent = None
        self.current_task_instruction = None
        self.last_code_agent_result = None
        # Instrumentation from the last generate_coords call (None until the
        # first grounding). The runner's staleness gate reads it.
        self.last_ground_detail = None
