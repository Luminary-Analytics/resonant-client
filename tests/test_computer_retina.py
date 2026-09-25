"""Screenshots on screens with more pixels than points (Retina Macs) map clicks in points."""

from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest
from PIL import Image

from lumi.engine import computer


@pytest.fixture(autouse=True)
def forget_captures(monkeypatch):
    """Each capture here is remembered for mapping clicks; put the old one back after."""
    monkeypatch.setattr(computer, "_LAST_CAPTURE", None)


def _fake_pyautogui(monkeypatch, position):
    monkeypatch.setitem(sys.modules, "pyautogui", SimpleNamespace(position=lambda: position))


def test_a_retina_capture_maps_the_models_clicks_to_points(monkeypatch):
    # A 1440 x 900 point screen captured as 2880 x 1800 pixels; the cursor is
    # at its centre, in points, as pyautogui reports it.
    _fake_pyautogui(monkeypatch, (720, 450))
    png, w, h = computer._finish_capture(Image.new("RGB", (2880, 1800), "white"), 0, 0, 1440, 900)
    assert (w, h) < (2880, 1800)  # downscaled for the model
    x, y, _ = computer._map_model_coords(w // 2, h // 2, None)
    assert abs(x - 720) <= 1 and abs(y - 450) <= 1  # the centre, in points, not (1440, 900)
    # The crosshair is drawn where the cursor is: the middle of the image.
    import io

    sent = Image.open(io.BytesIO(png)).convert("RGB")
    assert sent.getpixel((w // 2, h // 2)) == (255, 0, 0)


def test_without_a_separate_screen_size_pixels_are_the_coordinates(monkeypatch):
    # Windows (per-monitor DPI aware) and other screens where pixels and click
    # coordinates are the same: unchanged behaviour.
    _fake_pyautogui(monkeypatch, (0, 0))
    _, w, h = computer._finish_capture(Image.new("RGB", (1920, 1080)), 100, 0)
    x, y, _ = computer._map_model_coords(w // 2, h // 2, None)
    assert abs(x - (100 + 960)) <= 1 and abs(y - 540) <= 1
