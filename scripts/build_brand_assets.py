"""Rasterize the Lumi app icon for Windows and macOS builds.

The SVG masters in ``brand/`` are the source of truth. This script draws the
same geometry with Pillow so a release build needs no SVG renderer.

Windows (``lumi.ico``) uses the full-bleed tile. Icons at 16 and 32 pixels use
pixel-aligned variants; 20, 24 and 40 drop the glow and thicken the stroke; 48
and larger match ``brand/lumi-app-icon.svg``.

macOS (``lumi.icns``, ``lumi-macos.png``) follows Apple's icon grid: the tile
is 824 of 1024 units, centred, with a soft shadow, so Lumi sits at the same
optical size as other apps in the Dock.

Run with Pillow installed: ``python scripts/build_brand_assets.py``
"""
import io
import struct
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "lumi" / "gui" / "static"
MACOS = ROOT / "packaging" / "macos"

NIGHT = (0x10, 0x12, 0x2B, 255)
PAPER = (0xF6, 0xF4, 0xEE, 255)
LUMEN = (0xFF, 0xC2, 0x4B, 255)
# The light's glow fades linearly from GLOW_PEAK at the dot's edge to nothing
# at 2.25x its radius (lumi-app-icon.svg's radialGradient). A flat translucent
# ring read as a letter "o" beside the L, so the fade matters.
GLOW_PEAK = 0.35

ICO_SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)
ICNS_SIZES = (16, 32, 64, 128, 256, 512, 1024)

# brand/lumi-app-icon.svg draws the mark at translate(9 9) scale(0.72) inside
# a 64-unit tile; these coordinates are that transform already applied.
LARGE = {
    "grid": 64, "radius": 15,
    "points": [(20.52, 20.52), (20.52, 43.56), (43.56, 43.56)],
    "stroke": 5.76, "light": (34.92, 29.16), "r": 5.76, "glow": 12.96,
}
SMALL = {**LARGE, "stroke": 7.92, "r": 6.84, "glow": None}
# Pixel-aligned: every stroke edge lands on a whole pixel at 1x.
HINT32 = {
    "grid": 32, "radius": 7,
    "points": [(10, 10), (10, 22), (22, 22)],
    "stroke": 4, "light": (18, 14), "r": 4, "glow": None,
}
HINT16 = {
    "grid": 16, "radius": 3.5,
    "points": [(5, 5), (5, 11), (11, 11)],
    "stroke": 2, "light": (9, 7), "r": 2, "glow": None,
}


def windows_variant(size: int) -> dict:
    if size == 16:
        return HINT16
    if size == 32:
        return HINT32
    return SMALL if size < 48 else LARGE


def _circle(draw, centre, radius, unit, fill):
    cx, cy, r = centre[0] * unit, centre[1] * unit, radius * unit
    draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill=fill)


def _glow(image, centre, inner, outer, unit, offset=(0.0, 0.0)):
    """Composite a linear radial fade; each smaller disc overwrites the last."""
    layer = Image.new("RGBA", image.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    steps = 64
    shifted = (centre[0] + offset[0] / unit, centre[1] + offset[1] / unit)
    for step in range(steps + 1):
        t = step / steps
        radius = outer - (outer - inner) * t
        _circle(draw, shifted, radius, unit, LUMEN[:3] + (round(255 * GLOW_PEAK * t),))
    return Image.alpha_composite(image, layer)


def _draw_mark(image, spec, unit, offset=(0.0, 0.0)):
    """Draw the tile contents (glow, L, light) with an optional pixel offset."""
    if spec["glow"]:
        image = _glow(image, spec["light"], spec["r"], spec["glow"], unit, offset)
    draw = ImageDraw.Draw(image)
    ox, oy = offset
    pts = [(x * unit + ox, y * unit + oy) for x, y in spec["points"]]
    width = max(1, round(spec["stroke"] * unit))
    draw.line(pts, fill=PAPER, width=width, joint="curve")
    for x, y in (pts[0], pts[-1]):
        draw.ellipse((x - width / 2, y - width / 2, x + width / 2, y + width / 2), fill=PAPER)
    light = (spec["light"][0] + ox / unit, spec["light"][1] + oy / unit)
    _circle(draw, light, spec["r"], unit, LUMEN)
    return image


def render_windows(size: int) -> Image.Image:
    """The full-bleed tile at `size` pixels, supersampled then box-filtered."""
    spec = windows_variant(size)
    scale = max(4, min(16, 4096 // size))
    px = size * scale
    unit = px / spec["grid"]
    image = Image.new("RGBA", (px, px), (0, 0, 0, 0))
    ImageDraw.Draw(image).rounded_rectangle(
        (0, 0, px - 1, px - 1), radius=spec["radius"] * unit, fill=NIGHT,
    )
    image = _draw_mark(image, spec, unit)
    return image.resize((size, size), Image.Resampling.BOX)


def render_macos(size: int) -> Image.Image:
    """Apple's grid: an 824/1024 tile, centred, with a soft drop shadow."""
    spec = SMALL if size <= 32 else LARGE
    scale = max(2, min(16, 4096 // size))
    px = size * scale
    tile = px * 824 / 1024
    inset = (px - tile) / 2
    unit = tile / spec["grid"]
    radius = tile * 185.4 / 824
    image = Image.new("RGBA", (px, px), (0, 0, 0, 0))
    if size >= 64:
        shadow = Image.new("RGBA", (px, px), (0, 0, 0, 0))
        drop = px * 12 / 1024
        ImageDraw.Draw(shadow).rounded_rectangle(
            (inset, inset + drop, inset + tile, inset + tile + drop),
            radius=radius, fill=(0, 0, 0, 90),
        )
        image = Image.alpha_composite(
            image, shadow.filter(ImageFilter.GaussianBlur(px * 14 / 1024)),
        )
    ImageDraw.Draw(image).rounded_rectangle(
        (inset, inset, inset + tile, inset + tile), radius=radius, fill=NIGHT,
    )
    image = _draw_mark(image, spec, unit, offset=(inset, inset))
    return image.resize((size, size), Image.Resampling.BOX)


def write_ico(path: Path, images: list[Image.Image]) -> None:
    """Write a Windows icon holding each hand-tuned size as a PNG entry.

    Pillow's own ICO writer downsamples one image to every size, which would
    discard the pixel-aligned 16 and 32 pixel drawings.
    """
    payloads = []
    for image in images:
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        payloads.append((image.size[0], buffer.getvalue()))
    offset = 6 + 16 * len(payloads)
    directory, data = b"", b""
    for size, png in payloads:
        edge = 0 if size >= 256 else size
        directory += struct.pack(
            "<BBBBHHII", edge, edge, 0, 0, 1, 32, len(png), offset + len(data),
        )
        data += png
    path.write_bytes(struct.pack("<HHH", 0, 1, len(payloads)) + directory + data)


def main(static: Path = STATIC, macos: Path = MACOS) -> None:
    static.mkdir(parents=True, exist_ok=True)
    macos.mkdir(parents=True, exist_ok=True)
    write_ico(static / "lumi.ico", [render_windows(size) for size in ICO_SIZES])
    render_windows(512).save(static / "lumi.png")
    render_macos(512).save(static / "lumi-macos.png")
    mac = {size: render_macos(size) for size in ICNS_SIZES}
    mac[1024].save(
        macos / "lumi.icns",
        append_images=[mac[size] for size in ICNS_SIZES if size != 1024],
    )


if __name__ == "__main__":
    main()
