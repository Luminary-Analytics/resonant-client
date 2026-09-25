# Lumi brand

The **Lantern** mark: an L holding a gold light, shown against night. Chosen
24 September 2026. The exploration, rules and production icons are on the
design canvas at https://claude.ai/artifact/5FaJAThJpNPY3truBpT8qA.

## Colors

| Name | Hex | Use |
| --- | --- | --- |
| Night | `#10122B` | The ground the light lives on: the app, icons, dark surfaces |
| Lumen | `#FFC24B` | The light: the mark, focus and live status. Always shown on night |
| Paper | `#F6F4EE` | Light ground for the portal, documents and print |
| Ink | `#15172E` | Text and strokes on paper |
| Mist | `#A7ABCB` | Secondary text on night |
| Lumen deep | `#8A5600` | Links and highlights on paper, where Lumen is too light to read |

In the desktop app these are the `--brand`, `--on-brand` and background tokens
at the end of `lumi/gui/static/styles.css`. Warnings use orange, never amber, so
they are not mistaken for the accent.

## Type

Outfit for display, Instrument Sans for body text, JetBrains Mono for code.

## Rules

- The light is always shown against night. On light backgrounds use the
  `-on-light` files, which give the light a thin night outline.
- Clear space around the lockup is one light diameter on every side.
- Minimum sizes: the icon at 16 px, the lockup at 96 px wide. Below 48 px use
  the pixel-aligned icon (`favicon.svg`, or the small sizes in `lumi.ico`).
- Don't color the L, and don't replace the soft glow with a flat ring: beside
  the L a ring reads as the letter "o".

## Files

| File | Use |
| --- | --- |
| `lumi-app-icon.svg` | Master app icon, with the glow |
| `favicon.svg` | Browser tabs; pixel-aligned on a 32-unit grid |
| `lumi-mark.svg`, `lumi-mark-on-light.svg`, `lumi-mark-mono.svg` | The mark for dark, light and one-color use |
| `lumi-wordmark.svg`, `lumi-wordmark-on-light.svg` | The wordmark |
| `lumi-lockup.svg`, `lumi-lockup-on-light.svg` | Mark and wordmark together |

`scripts/build_brand_assets.py` draws the raster icons from the same geometry:
`lumi/gui/static/lumi.ico` (Windows, 16–256 px), `lumi.png` (notifications),
`lumi-macos.png` (macOS Dock when run from source) and
`packaging/macos/lumi.icns` (macOS app bundle). Run it after changing the mark.
