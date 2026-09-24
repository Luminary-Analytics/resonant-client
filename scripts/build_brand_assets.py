"""Rasterize the shared SONN continuity mark for compatible desktop icon files.

The SVG is copied from SONN's product brand assets. Run with Pillow installed:
python scripts/build_brand_assets.py. Existing filenames remain upgrade-compatible.
"""
import math
from pathlib import Path

from PIL import Image, ImageDraw

DEST = Path(__file__).resolve().parents[1] / "lumi/gui/static"
CURVES = [
    ((32, 32), (24, 16), (8, 18), (8, 32)),
    ((8, 32), (8, 46), (24, 48), (32, 32)),
    ((32, 32), (40, 16), (56, 18), (56, 32)),
    ((56, 32), (56, 46), (40, 48), (32, 32)),
]


def main():
    scale = 8
    mask = Image.new("L", (64 * scale, 64 * scale))
    draw = ImageDraw.Draw(mask)
    points = []
    angle = math.radians(-55)
    for curve in CURVES:
        for step in range(101):
            t = step / 100
            weights = ((1-t)**3, 3*(1-t)**2*t, 3*(1-t)*t*t, t**3)
            x, y = (sum(w*p[axis] for w, p in zip(weights, curve)) - 32 for axis in (0, 1))
            points.append(((32+x*math.cos(angle)-y*math.sin(angle))*scale,
                           (32+x*math.sin(angle)+y*math.cos(angle))*scale))
    draw.line(points, fill=255, width=8 * scale, joint="curve")
    image = Image.new("RGBA", mask.size)
    pixels = image.load()
    for y in range(mask.height):
        for x in range(mask.width):
            # The SVG's gradient rotates with the path.
            dx, dy = x/scale-32, y/scale-32
            px = dx*math.cos(-angle)-dy*math.sin(-angle)+32
            py = dx*math.sin(-angle)+dy*math.cos(-angle)+32
            t = max(0, min(1, ((px-10)*41+(py-9)*47)/(41**2+47**2)))
            pixels[x, y] = tuple(round(a+(b-a)*t) for a, b in zip((219,255,227),(120,217,162))) + (255,)
    image.putalpha(mask)
    image.save(DEST / "resonant.png")
    image.resize((256, 256), Image.Resampling.LANCZOS).save(
        DEST / "resonant.ico", sizes=[(s, s) for s in (16, 24, 32, 48, 64, 128, 256)],
    )


if __name__ == "__main__":
    main()
