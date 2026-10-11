"""Vision-first fast path for s3rehan.

Rehan's insight (2026-10-10): we measured EXECUTION time, but the user feels
TOTAL time = thinking + writing code + execution + retries. A "fast" method
that needs a 5s planner call plus a code-generation step is slower in
user-perceived time than just looking at the screen and clicking.

This module is the fastest single-action lane: screenshot -> ONE vision call
-> coordinates -> click. No planner, no code agent, no multi-stage grounding
pipeline, no script generation.

Where it sits (by total user-perceived time, fastest first):
  1. coordcache hit            (~50ms — no model, no screenshot)
  2. page-map hit              (~100-300ms — UIA lookup, no model)
  3. vision-first (this)       (~2.5-4.5s — screenshot + ONE vision call)
  4. coordrefine pipeline      (~5-15s — UIA+OCR+contours+several vision calls)
  5. jev lane                  (~2.5s/step — one decision call per step)
  6. full planner              (~10-30s/step — think + write code + ground)

Vision-first BEATS coordrefine on hard targets (one 2-4s call vs a 5-15s
pipeline) but LOSES to cache/page-map on easy targets — so the router
(lanerouter.py) always tries cache and page-map first. This module never
replaces them; it replaces the expensive coordrefine chain as the first
vision fallback.

Design rules:
  - The model is asked for COORDINATES ONLY (x, y in 0-1000 space), never
    for code, plans, or explanations. One call, one job.
  - Pluggable vision function: production wires the proxy/LMMAgent, tests
    inject a mock. No network code lives here.
  - Every call is timed; the timing breakdown is returned so the router
    and benchmarks measure TOTAL time honestly.
  - Pure Python, no Windows-only imports: importable and unit-testable
    on any OS.

Env: S3REHAN_VISIONFIRST=0 disables the lane (default ON).
"""

import os
import re
import time
from typing import Callable, Dict, Optional, Tuple

ENV_ENABLE = "S3REHAN_VISIONFIRST"

# The model outputs coordinates in this space (matches the grounding
# model's native space used by generate_coords in aci.py).
GROUND_W = 1000
GROUND_H = 1000

# Strict prompt: coordinates only. Matches the proven STRICT_GROUND_PROMPT
# pattern in aci.py (measured 2026-09-30: free-form prompts leave the
# answer trapped in the reasoning field; "x, y" in content works).
LOCATE_PROMPT = (
    "Look at this screenshot. Locate {ref}. "
    "Reply with ONLY its center point as two integers like 250, 496. "
    "Coordinates are in 0-1000 space (0,0 is top-left, 1000,1000 is "
    "bottom-right). No explanation, no other text."
)

_COORD_RE = re.compile(r"(-?\d+)\s*[,;:\s]\s*(-?\d+)")

# Zero-shot spatial cues (arXiv:2509.11548 "Auxiliary Reasoning for GUI
# Grounding", DEEP_RESEARCH_ROUND2.md §1e): VLMs have latent grounding
# ability but fail at explicit coordinates. Overlaying axes + a labeled
# grid unlocks it with no model changes — near-zero latency cost (one
# PIL draw pass, ~5-15ms), measurable accuracy gain across 7 VLMs.
GRID_STEP = 100  # gridlines every 100 units in 0-1000 space


def add_grid_overlay(png: bytes, step: int = GRID_STEP) -> bytes:
    """Draw a coordinate grid + axes on the screenshot.

    Gridlines every `step` units (0-1000 space) with numeric labels on
    the top/left edges, plus a slightly stronger axis at 500. The model
    still replies in 0-1000 space; the grid just gives it landmarks.
    Returns the original bytes on any failure (never break grounding).
    """
    try:
        from PIL import Image, ImageDraw
        import io as _io

        img = Image.open(_io.BytesIO(png)).convert("RGB")
        w, h = img.size
        d = ImageDraw.Draw(img, "RGBA")
        # Faint gridlines.
        for gx in range(0, 1001, step):
            x = int(gx * w / 1000)
            strong = gx % 500 == 0
            d.line([(x, 0), (x, h)], fill=(255, 0, 0, 70 if strong else 35),
                   width=2 if strong else 1)
            d.text((x + 2, 2), str(gx), fill=(255, 0, 0, 160))
        for gy in range(0, 1001, step):
            y = int(gy * h / 1000)
            strong = gy % 500 == 0
            d.line([(0, y), (w, y)], fill=(255, 0, 0, 70 if strong else 35),
                   width=2 if strong else 1)
            d.text((2, y + 2), str(gy), fill=(255, 0, 0, 160))
        out = _io.BytesIO()
        img.save(out, format="PNG")
        return out.getvalue()
    except Exception:
        return png


ENV_GRID = "S3REHAN_GRID_OVERLAY"


def grid_overlay_enabled() -> bool:
    """S3REHAN_GRID_OVERLAY=0 disables; default ON."""
    return os.environ.get(ENV_GRID, "1") != "0"


def visionfirst_enabled() -> bool:
    """S3REHAN_VISIONFIRST=0 disables the lane; default ON."""
    return os.environ.get(ENV_ENABLE, "1") != "0"


def parse_coords(raw: str) -> Optional[Tuple[int, int]]:
    """Extract (x, y) from a model reply. Returns None when unparseable.

    Takes the FIRST plausible pair; clamps to 0-1000. Rejects pairs where
    both numbers are identical and >100 (usually a repeated token, not a
    point) — a cheap guard against degenerate output.
    """
    if not raw:
        return None
    for m in _COORD_RE.finditer(raw):
        try:
            x, y = int(m.group(1)), int(m.group(2))
        except ValueError:
            continue
        if not (0 <= x <= GROUND_W and 0 <= y <= GROUND_H):
            continue
        if x == y and x > 100:
            continue  # degenerate repetition, keep scanning
        return (x, y)
    return None


class VisionFirstError(Exception):
    """Base for vision-first failures."""


class VisionFirstNoCoords(VisionFirstError):
    """The vision model did not return usable coordinates."""


class VisionFirst:
    """One-shot screenshot -> coordinates -> click.

    `ask_vision` is a callable `(png_bytes, prompt) -> str` wired by the
    caller: production passes the proxy/LMMAgent call, tests pass a mock.
    `click_fn` is `(x, y)` in SCREEN pixels; when None, locate() only
    returns coordinates without clicking (the caller executes).

    `input_scale`: downscale the screenshot before sending it to the
    vision model (research 2026-10-10: visual token pruning is how the
    fastest VLA systems cut inference time — fewer input tokens, faster
    call). The model still replies in 0-1000 normalized space, so the
    output contract is unchanged; only the input detail drops. 1.0 =
    full resolution (most accurate), 0.5 = quarter pixels (~2-4x fewer
    vision tokens, best for large targets). Default 1.0: accuracy first,
    opt into speed explicitly.
    """

    def __init__(
        self,
        ask_vision: Callable[[bytes, str], str],
        click_fn: Optional[Callable[[int, int], None]] = None,
        # Default 1: the lane's contract is ONE vision call per locate
        # (module docstring: "One call, one job"; lane cost 3000ms is
        # priced for a single call). A retry is a second full vision
        # call — pass attempts=2 explicitly to opt into it.
        attempts: int = 1,
        input_scale: float = 1.0,
        grid_overlay: Optional[bool] = None,
        focus_region: Optional[Tuple[int, int, int, int]] = None,
    ):
        self.ask_vision = ask_vision
        self.click_fn = click_fn
        self.attempts = max(1, int(attempts))
        self.input_scale = min(1.0, max(0.25, float(input_scale)))
        self.grid_overlay = (grid_overlay if grid_overlay is not None
                             else grid_overlay_enabled())
        # focus_region: (x, y, w, h) in 0-1000 space. When set, the
        # screenshot is CROPPED to the region before the vision call
        # (RegionFocus paper, DEEP_RESEARCH_ROUND2.md §1c: +28% accuracy
        # by removing background clutter). Coordinates are mapped back
        # to full-frame 0-1000 space before return.
        self.focus_region = focus_region

    def _prep_png(self, png: bytes) -> Tuple[bytes, Optional[Dict]]:
        """Downscale per input_scale, crop per focus_region, add grid.

        Returns (png_bytes, crop_info) where crop_info is None when no
        crop was applied, else {"x0","y0","w","h"} in 0-1000 space for
        mapping coordinates back.
        """
        crop_info = None
        img_png = png
        if self.focus_region is not None:
            try:
                from PIL import Image
                import io as _io

                fx, fy, fw, fh = self.focus_region
                img = Image.open(_io.BytesIO(img_png))
                w, h = img.size
                x0 = int(fx * w / 1000)
                y0 = int(fy * h / 1000)
                x1 = int(min(w, x0 + fw * w / 1000))
                y1 = int(min(h, y0 + fh * h / 1000))
                if x1 > x0 and y1 > y0:
                    buf = _io.BytesIO()
                    img.crop((x0, y0, x1, y1)).save(buf, format="PNG")
                    img_png = buf.getvalue()
                    crop_info = {"x0": fx, "y0": fy, "w": fw, "h": fh}
            except Exception:
                pass
        if self.input_scale < 1.0:
            try:
                from PIL import Image
                import io as _io

                img = Image.open(_io.BytesIO(img_png))
                w, h = img.size
                nw = max(1, int(w * self.input_scale))
                nh = max(1, int(h * self.input_scale))
                if (nw, nh) != (w, h):
                    buf = _io.BytesIO()
                    img.resize((nw, nh), Image.BILINEAR).save(buf, format="PNG")
                    img_png = buf.getvalue()
            except Exception:
                pass
        if self.grid_overlay:
            img_png = add_grid_overlay(img_png)
        return img_png, crop_info

    @staticmethod
    def _uncrop(pt: Tuple[int, int], crop_info: Optional[Dict]
                ) -> Tuple[int, int]:
        """Map crop-local 0-1000 coords back to full-frame 0-1000."""
        if not crop_info:
            return pt
        x = crop_info["x0"] + pt[0] * crop_info["w"] // 1000
        y = crop_info["y0"] + pt[1] * crop_info["h"] // 1000
        return (max(0, min(1000, x)), max(0, min(1000, y)))

    def locate(
        self,
        ref_expr: str,
        png: bytes,
        screen_w: int,
        screen_h: int,
    ) -> Dict:
        """One vision call -> grounding-space coordinates + timing.

        Returns {"x", "y", "ms", "model_calls", "attempts_used"} with x, y
        in 0-1000 grounding space (same contract as aci._vision_ground).
        Raises VisionFirstNoCoords when the model never yields coordinates.
        """
        t0 = time.perf_counter()
        prompt = LOCATE_PROMPT.format(ref=ref_expr)
        png_small, crop_info = self._prep_png(png)
        last_raw = ""
        used = 0
        for _ in range(self.attempts):
            used += 1
            last_raw = (self.ask_vision(png_small, prompt) or "")
            pt = parse_coords(last_raw)
            if pt is not None:
                pt = self._uncrop(pt, crop_info)
                ms = (time.perf_counter() - t0) * 1000
                return {
                    "x": pt[0],
                    "y": pt[1],
                    "ms": ms,
                    "model_calls": used,
                    "attempts_used": used,
                }
        raise VisionFirstNoCoords(
            f"vision-first: no coordinates for {ref_expr!r} after "
            f"{used} attempt(s) (last: {last_raw[:120]!r})"
        )

    def ground_to_screen(
        self,
        ref_expr: str,
        png: bytes,
        screen_w: int,
        screen_h: int,
    ) -> Dict:
        """locate() then convert 0-1000 space -> screen pixels.

        Returns the locate() dict plus "sx", "sy" (screen pixels).
        """
        r = self.locate(ref_expr, png, screen_w, screen_h)
        r["sx"] = int(round(r["x"] * screen_w / GROUND_W))
        r["sy"] = int(round(r["y"] * screen_h / GROUND_H))
        return r

    def click(
        self,
        ref_expr: str,
        png: bytes,
        screen_w: int,
        screen_h: int,
    ) -> Dict:
        """Full one-shot action: locate on the screenshot, then click.

        Total user-perceived time for the action is r["ms"] (vision +
        click). Raises VisionFirstError when no click_fn was wired or no
        coordinates came back.
        """
        if self.click_fn is None:
            raise VisionFirstError("vision-first click: no click_fn wired")
        t0 = time.perf_counter()
        r = self.ground_to_screen(ref_expr, png, screen_w, screen_h)
        self.click_fn(r["sx"], r["sy"])
        r["ms"] = (time.perf_counter() - t0) * 1000  # total, incl. click
        r["action"] = "click"
        return r
