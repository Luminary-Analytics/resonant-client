"""
Pixel-level visual diff between two screenshots.

Used by the `screen_diff` tool to surface "what changed" rectangles. The agent
can use these to focus attention; the frontend can overlay them on the preview.

Algorithm:
1. Decode both PNGs to RGB (PIL).
2. Resize the smaller to match the larger (in case auto-screenshots scaled).
3. Compute per-pixel max-channel-delta; threshold to a binary mask.
4. Run connected-components labeling to extract rectangles (numpy + scipy if
   available; otherwise a simple flood-fill fallback).
5. Merge nearby rectangles, cap at MAX_RECTS.
"""

from __future__ import annotations

import io
import time
from typing import Optional

from .tools import ToolResult


MAX_RECTS = 20
MERGE_PADDING = 8  # pixels — rects within this gap get merged


def _decode_png(png_bytes: bytes):
    """PNG bytes → PIL.Image.Image in RGB mode. Returns None on failure."""
    try:
        from PIL import Image
        img = Image.open(io.BytesIO(png_bytes)).convert("RGB")
        return img
    except Exception:
        return None


def diff_images(prev_png: bytes, curr_png: bytes, *, threshold: int = 30) -> dict:
    """
    Compute changed-region rectangles between two screenshots.

    Returns:
        {
            "rects": [{x, y, width, height}, ...],   # cap MAX_RECTS, merged
            "changed_pixel_pct": float,              # 0.0–100.0
            "size": [width, height],
        }
    Or {"error": str} on failure.
    """
    prev = _decode_png(prev_png)
    curr = _decode_png(curr_png)
    if prev is None or curr is None:
        return {"error": "could not decode one of the PNGs", "rects": []}

    if prev.size != curr.size:
        # Resize prev to match curr (current is what the user cares about)
        prev = prev.resize(curr.size)

    try:
        import numpy as np
    except ImportError:
        return {"error": "numpy not installed (pip install numpy)", "rects": []}

    a = np.asarray(prev, dtype=np.int16)
    b = np.asarray(curr, dtype=np.int16)
    delta = np.max(np.abs(a - b), axis=2)
    mask = (delta > int(threshold))
    changed = int(mask.sum())
    total = mask.size
    pct = round(100.0 * changed / total, 2) if total else 0.0
    w, h = curr.size

    rects = _mask_to_rects(mask)
    return {
        "rects": rects,
        "changed_pixel_pct": pct,
        "size": [w, h],
    }


def _mask_to_rects(mask) -> list[dict]:
    """Convert a boolean change-mask to a list of {x, y, width, height} rects."""
    try:
        import numpy as np
        from scipy.ndimage import label  # type: ignore
        labeled, n = label(mask)
        rects: list[dict] = []
        if n == 0:
            return rects
        for lid in range(1, n + 1):
            ys, xs = np.where(labeled == lid)
            if len(xs) == 0:
                continue
            x0, x1 = int(xs.min()), int(xs.max())
            y0, y1 = int(ys.min()), int(ys.max())
            rects.append({"x": x0, "y": y0, "width": x1 - x0 + 1, "height": y1 - y0 + 1})
        rects.sort(key=lambda r: r["width"] * r["height"], reverse=True)
        rects = _merge_close_rects(rects)
        return rects[:MAX_RECTS]
    except ImportError:
        # Fallback: a coarse single-bbox per row band
        return _coarse_rects(mask)


def _merge_close_rects(rects: list[dict]) -> list[dict]:
    """Merge rectangles whose bounding-boxes overlap or are within MERGE_PADDING."""
    out: list[dict] = []
    for r in rects:
        merged = False
        for o in out:
            if _rects_close(r, o):
                # Expand `o` to cover both
                x0 = min(o["x"], r["x"])
                y0 = min(o["y"], r["y"])
                x1 = max(o["x"] + o["width"], r["x"] + r["width"])
                y1 = max(o["y"] + o["height"], r["y"] + r["height"])
                o["x"], o["y"] = x0, y0
                o["width"], o["height"] = x1 - x0, y1 - y0
                merged = True
                break
        if not merged:
            out.append(dict(r))
    return out


def _rects_close(a: dict, b: dict, pad: int = MERGE_PADDING) -> bool:
    ax0, ay0 = a["x"] - pad, a["y"] - pad
    ax1, ay1 = a["x"] + a["width"] + pad, a["y"] + a["height"] + pad
    bx0, by0 = b["x"], b["y"]
    bx1, by1 = b["x"] + b["width"], b["y"] + b["height"]
    return not (ax1 < bx0 or bx1 < ax0 or ay1 < by0 or by1 < ay0)


def _coarse_rects(mask) -> list[dict]:
    """Fallback when scipy isn't available — single bbox of all changed pixels."""
    try:
        import numpy as np
        ys, xs = np.where(mask)
        if len(xs) == 0:
            return []
        return [{
            "x": int(xs.min()),
            "y": int(ys.min()),
            "width": int(xs.max() - xs.min() + 1),
            "height": int(ys.max() - ys.min() + 1),
        }]
    except Exception:
        return []


# ── Screenshot id cache (last few computer_* screenshots) ──────────────


_CACHE_LIMIT = 8
_screenshot_cache: dict[str, bytes] = {}
_cache_order: list[str] = []


def remember_screenshot(screenshot_id: str, png_bytes: bytes) -> None:
    """Stash a PNG keyed by id. Evicts oldest beyond _CACHE_LIMIT."""
    if not screenshot_id:
        return
    if screenshot_id in _screenshot_cache:
        return
    _screenshot_cache[screenshot_id] = png_bytes
    _cache_order.append(screenshot_id)
    while len(_cache_order) > _CACHE_LIMIT:
        old = _cache_order.pop(0)
        _screenshot_cache.pop(old, None)


def get_remembered_screenshot(screenshot_id: str) -> Optional[bytes]:
    return _screenshot_cache.get(screenshot_id)


def previous_two_ids() -> tuple[Optional[str], Optional[str]]:
    """Return (prev_id, latest_id) — useful for default screen_diff invocation."""
    if len(_cache_order) >= 2:
        return _cache_order[-2], _cache_order[-1]
    if len(_cache_order) == 1:
        return None, _cache_order[-1]
    return None, None


# ── Tool wrapper ───────────────────────────────────────────────────────


def exec_screen_diff(args: dict, start: float) -> ToolResult:
    prev_id = (args.get("prev_id") or "").strip()
    current_id = (args.get("current_id") or "").strip()
    threshold = int(args.get("threshold", 30))

    auto_prev, auto_curr = previous_two_ids()
    if not prev_id:
        prev_id = auto_prev or ""
    if not current_id:
        current_id = auto_curr or ""

    if not prev_id and not current_id:
        return ToolResult(
            "screen_diff: no cached screenshots yet — take at least two screenshots first.",
            is_error=True, elapsed=time.time() - start,
        )

    prev_png = get_remembered_screenshot(prev_id) if prev_id else None
    if not prev_png:
        return ToolResult(
            f"screen_diff: prev_id '{prev_id}' not in cache",
            is_error=True, elapsed=time.time() - start,
        )

    if current_id:
        curr_png = get_remembered_screenshot(current_id)
        if not curr_png:
            return ToolResult(
                f"screen_diff: current_id '{current_id}' not in cache",
                is_error=True, elapsed=time.time() - start,
            )
    else:
        # Take a fresh screenshot now
        try:
            from .computer import _take_screenshot
            png, _w, _h = _take_screenshot(None)
            curr_png = png
        except Exception as exc:
            return ToolResult(f"screen_diff: failed to capture current screenshot: {exc}",
                              is_error=True, elapsed=time.time() - start)

    data = diff_images(prev_png, curr_png, threshold=threshold)
    if data.get("error"):
        return ToolResult(f"screen_diff: {data['error']}", is_error=True, elapsed=time.time() - start, metadata=data)

    rects = data["rects"]
    pct = data["changed_pixel_pct"]
    if not rects:
        text = f"No regions changed (delta={pct}%)."
    else:
        lines = [f"{len(rects)} region(s) changed (delta={pct}%):"]
        for r in rects[:10]:
            lines.append(f"  ({r['x']},{r['y']})  {r['width']}x{r['height']}")
        if len(rects) > 10:
            lines.append(f"  …({len(rects) - 10} more)")
        text = "\n".join(lines)

    return ToolResult(text, elapsed=time.time() - start, metadata=data)
