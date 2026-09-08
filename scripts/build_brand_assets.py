"""Build Resonant's vector wave mark and matching Windows icon sizes.

Run with the existing desktop Pillow dependency: python scripts/build_brand_assets.py
The same cubic curves define the SVG and raster exports.
"""
from pathlib import Path

from PIL import Image, ImageDraw


DEST = Path(__file__).resolve().parents[1] / "resonant_client/gui/static"
CURVES = [
    ((10, 32), (16, 32), (16, 18), (22, 18)),
    ((22, 18), (28, 18), (28, 46), (34, 46)),
    ((34, 46), (40, 46), (40, 18), (46, 18)),
    ((46, 18), (52, 18), (50, 32), (54, 32)),
]
SVG = '''<svg xmlns="http://www.w3.org/2000/svg" width="64" height="64" viewBox="0 0 64 64" fill="none">
  <title>Resonant</title>
  <rect x="1" y="1" width="62" height="62" rx="17" fill="#0D2626" stroke="#20504B" stroke-width="2"/>
  <path d="M10 32 C16 32 16 18 22 18 C28 18 28 46 34 46 C40 46 40 18 46 18 C52 18 50 32 54 32" stroke="#54E3C2" stroke-width="5" stroke-linecap="round"/>
</svg>
'''


def main():
    (DEST / "favicon.svg").write_text(SVG, encoding="utf-8")
    scale = 16
    image = Image.new("RGBA", (64 * scale, 64 * scale))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((scale, scale, 63 * scale, 63 * scale), radius=17 * scale,
                           fill="#0D2626", outline="#20504B", width=2 * scale)
    points = []
    for curve in CURVES:
        for step in range(101):
            t = step / 100
            weights = ((1-t)**3, 3*(1-t)**2*t, 3*(1-t)*t*t, t**3)
            points.append(tuple(sum(w*p[axis] for w, p in zip(weights, curve)) * scale for axis in (0, 1)))
    draw.line(points, fill="#54E3C2", width=5 * scale, joint="curve")
    for x, y in (points[0], points[-1]):
        r = 2.5 * scale
        draw.ellipse((x-r, y-r, x+r, y+r), fill="#54E3C2")
    image.resize((512, 512), Image.Resampling.LANCZOS).save(DEST / "resonant.png")
    image.resize((256, 256), Image.Resampling.LANCZOS).save(
        DEST / "resonant.ico", sizes=[(s, s) for s in (16, 24, 32, 48, 64, 128, 256)],
    )


if __name__ == "__main__":
    main()
