"""Computer-use capability gating and the on-screen indicator.

The overlay's windows are Win32 and are verified by eye on Windows. What is
tested here is everything that decides *whether* it runs and *which* screen it
points at, the capability rule that decides which models are offered the
desktop tools at all — all of which fail silently — and the pixels it draws.
"""

import time
from types import SimpleNamespace

import pytest

from lumi.capabilities import ModelCapabilities, infer_model_capabilities
from lumi.engine import screen_overlay
from lumi.engine.session import Session
from lumi.engine.tools import (
    AGENT_TOOLS,
    DESKTOP_TOOL_NAMES,
    _computer_use_indicator_enabled,
)


# ── capability inference ─────────────────────────────────────────────

@pytest.mark.parametrize(
    ("model", "expected"),
    [
        ("kimi-k3", True),          # vision + native tools
        ("qwen3-vl:8b", True),      # any vision model with tools, no code change
        ("deepseek-v4:cloud", False),   # tools but no vision
        ("glm-5.2:cloud", False),
        ("llama3", False),          # neither
    ],
)
def test_computer_use_requires_vision_and_tools(model, expected):
    """Both halves are required.

    Without vision the model cannot see the screen; without reliable tool
    calling it cannot act on it. Handed the tools anyway it would invent
    coordinates, click confidently in the wrong place, and report success.
    """
    assert infer_model_capabilities(model).computer_use is expected


def test_a_literal_profile_that_never_set_the_field_still_derives_it():
    """Profiles handed over by a provider adapter miss fields added later.

    Only the name-inference path was updated when `computer_use` was
    introduced. Kimi K3's profile is constructed literally in `backends.py`,
    so it reported `computer_use=None` — and the flagship model for this
    feature was denied it. `None` means "unstated", not "no".
    """
    literal = ModelCapabilities(
        model="kimi-k3", context_window=256000,
        modalities=("text", "image"), native_tools=True,
        source="provider",   # note: computer_use never set
    )

    assert literal.computer_use is None
    assert literal.can_use_computer is True
    assert literal.supports("computer_use") is True


def test_an_explicit_false_is_not_overridden_by_the_derivation():
    """A provider saying "no" must outrank the inference."""
    stated = ModelCapabilities(
        model="x", context_window=8192, modalities=("text", "image"),
        native_tools=True, computer_use=False,
    )

    assert stated.can_use_computer is False
    assert stated.supports("computer_use") is False


def test_runtime_report_can_grant_computer_use():
    """A provider's runtime report is authoritative.

    A model whose family inference guessed text-only but which advertises
    vision and tools at runtime must gain desktop control, or the report is
    honoured for every capability except this one.
    """
    inferred = infer_model_capabilities("some-new-model")
    assert inferred.computer_use is False

    updated = inferred.with_runtime_metadata(["vision", "tools"])

    assert updated.computer_use is True
    assert updated.supports("computer") is True


def test_runtime_report_can_revoke_computer_use():
    """Losing vision at runtime must also remove it."""
    capable = ModelCapabilities(
        model="x", context_window=8192, modalities=("text", "image"),
        native_tools=True, computer_use=True,
    )

    assert capable.with_runtime_metadata(["tools"]).computer_use is False


# ── tool catalogue gating ────────────────────────────────────────────

def _session_for(profile):
    backend = SimpleNamespace(name="test", model="test", capability_profile=profile)
    return Session(backend=backend)


def test_capable_model_is_offered_the_desktop_tools():
    session = _session_for(infer_model_capabilities("kimi-k3"))

    names = {tool["function"]["name"] for tool in session.tools}

    assert DESKTOP_TOOL_NAMES <= names


def test_incapable_model_is_not_offered_the_desktop_tools():
    session = _session_for(infer_model_capabilities("llama3"))

    names = {tool["function"]["name"] for tool in session.tools}

    assert not (DESKTOP_TOOL_NAMES & names)
    # Only the desktop tools go; everything else is untouched.
    assert {"file_read", "bash", "grep"} <= names


def _dynamic_backend(profile):
    """A backend that compacts its tool catalogue — Ollama and Kimi both do."""
    return SimpleNamespace(
        name="ollama", model="test", capability_profile=profile,
        supports_dynamic_tool_catalog=True,
    )


def test_desktop_tools_survive_dynamic_catalogue_compaction():
    """Gating `Session.tools` alone leaves the feature inert where it matters.

    Both Ollama and Kimi advertise a dynamic catalogue, so `provider_tools`
    trims the payload to a small core — and that trim removed every desktop
    tool the capability gate had just added. The model never saw them and had
    to discover them through `search_tools`.

    Measured against qwen3-vl:8b, that indirection is where it broke: asked for
    a screenshot it replied "I cannot take screenshots" via `await_user`, and
    with the same tools present in the payload it called `computer_screenshot`
    on the first turn.
    """
    session = Session(backend=_dynamic_backend(infer_model_capabilities("kimi-k3")))

    advertised = {tool["function"]["name"] for tool in session.provider_tools}

    assert DESKTOP_TOOL_NAMES <= advertised, (
        "desktop tools were stripped from the payload the model actually sees"
    )
    assert "search_tools" in advertised, "the compact core should still be present"


def test_compaction_still_excludes_desktop_tools_for_incapable_models():
    """The extra schemas are only paid for by models that can use them."""
    session = Session(backend=_dynamic_backend(infer_model_capabilities("llama3")))

    advertised = {tool["function"]["name"] for tool in session.provider_tools}

    assert not (DESKTOP_TOOL_NAMES & advertised)
    assert "search_tools" in advertised


def test_compaction_is_still_smaller_than_the_full_catalogue():
    """Adding the desktop tools must not defeat the point of compaction."""
    session = Session(backend=_dynamic_backend(infer_model_capabilities("kimi-k3")))

    assert len(session.provider_tools) < len(session.tools)


def test_backend_without_a_capability_profile_keeps_everything():
    """An unknown backend must not be silently stripped of tools."""
    session = Session(backend=SimpleNamespace(name="mystery", model="?"))

    names = {tool["function"]["name"] for tool in session.tools}

    assert DESKTOP_TOOL_NAMES <= names


def test_every_desktop_tool_name_is_a_real_tool():
    """A typo here would silently un-gate a tool and skip its indicator."""
    registered = {tool["function"]["name"] for tool in AGENT_TOOLS}

    assert DESKTOP_TOOL_NAMES <= registered


# ── indicator wiring ─────────────────────────────────────────────────

def test_indicator_is_on_by_default():
    """Opt-out, not opt-in — the whole point is that it is visible."""
    assert _computer_use_indicator_enabled(None) is True
    assert _computer_use_indicator_enabled(SimpleNamespace(get=lambda *a: True)) is True


def test_indicator_can_be_turned_off():
    settings = SimpleNamespace(get=lambda section, key, default=None: False)

    assert _computer_use_indicator_enabled(settings) is False


def test_broken_settings_do_not_disable_the_indicator():
    """Failing closed here would hide the fact that the agent has the mouse."""
    def _raise(*args, **kwargs):
        raise RuntimeError("settings unavailable")

    assert _computer_use_indicator_enabled(SimpleNamespace(get=_raise)) is True


# ── monitor targeting ────────────────────────────────────────────────

@pytest.fixture
def two_monitors(monkeypatch):
    """A left-hand monitor at negative x, as on a real multi-screen desk."""
    monitors = [
        {"index": 0, "x": -2560, "y": 0, "width": 2560, "height": 1080, "primary": False},
        {"index": 1, "x": 0, "y": 0, "width": 2560, "height": 1080, "primary": True},
    ]
    monkeypatch.setattr(
        "lumi.engine.computer_use.list_monitors", lambda: monitors
    )
    return monitors


def test_explicit_monitor_argument_wins(two_monitors):
    assert screen_overlay.monitor_index_for_args({"monitor": 0}) == 0


def test_click_coordinates_choose_the_screen_they_land_on(two_monitors):
    """The primary screen is often not the one being driven."""
    assert screen_overlay.monitor_index_for_args({"x": -1200, "y": 500}) == 0
    assert screen_overlay.monitor_index_for_args({"x": 1200, "y": 500}) == 1


def test_region_origin_is_used_when_there_are_no_coordinates(two_monitors):
    args = {"region": {"x": -2000, "y": 100, "width": 400, "height": 400}}

    assert screen_overlay.monitor_index_for_args(args) == 0


def test_unlocatable_arguments_fall_through_to_the_primary(two_monitors):
    """None means "caller has no opinion", which show_for_monitor reads as primary."""
    assert screen_overlay.monitor_index_for_args({}) is None
    assert screen_overlay.monitor_index_for_args({"target_window": "Chrome"}) is None
    assert screen_overlay.monitor_index_for_args(None) is None


def test_a_point_outside_every_monitor_is_not_forced_onto_one(two_monitors):
    assert screen_overlay.monitor_index_for_point(99999, 99999) is None


def test_activity_on_two_monitors_keeps_two_borders_visible(two_monitors, monkeypatch):
    """Crossing displays must add a border, not move the first border away."""
    created = []

    class FakeOverlay:
        def __init__(self, *, cursor_indicator=True):
            self.cursor_indicator = cursor_indicator
            self.visible = False
            self._bounds = (0, 0, 0, 0)
            created.append(self)

        def show(self, *bounds):
            self._bounds = bounds
            self.visible = True
            return True

    primary = FakeOverlay()
    created.clear()
    monkeypatch.setattr(screen_overlay, "IS_WINDOWS", True)
    monkeypatch.setattr(screen_overlay, "_instance", lambda: primary)
    monkeypatch.setattr(screen_overlay, "_Overlay", FakeOverlay)
    monkeypatch.setattr(screen_overlay, "_secondary_overlays", [])
    monkeypatch.setattr(screen_overlay, "_suppressed", 0)

    assert screen_overlay.show_for_monitor(0)
    assert screen_overlay.show_for_monitor(1)
    assert primary.visible
    assert primary._bounds == (-2560, 0, 2560, 1080)
    assert len(created) == 1
    assert created[0].visible
    assert created[0]._bounds == (0, 0, 2560, 1080)
    assert created[0].cursor_indicator is False


def test_repeated_activity_on_one_monitor_skips_the_window_redraw(
    two_monitors, monkeypatch
):
    class FakeOverlay:
        def __init__(self):
            self.visible = False
            self._bounds = (0, 0, 0, 0)
            self.show_calls = 0

        def show(self, *bounds):
            self.show_calls += 1
            self._bounds = bounds
            self.visible = True
            return True

    overlay = FakeOverlay()
    monkeypatch.setattr(screen_overlay, "_instance_for_region", lambda bounds: overlay)
    monkeypatch.setattr(screen_overlay, "_suppressed", 0)

    assert screen_overlay.show_for_region(0, 0, 1920, 1080)
    assert screen_overlay.show_for_region(0, 0, 1920, 1080)
    assert overlay.show_calls == 1


# ── cursor glow ──────────────────────────────────────────────────────

def _alpha_at(frame: bytearray, x: int, y: int) -> int:
    box = screen_overlay.RING_BOX
    return frame[(y * box + x) * 4 + 3]


def _test_cursor_mask():
    """A small opaque cursor body with its hot spot one pixel in."""
    width, height = 8, 10
    return bytearray([255] * (width * height)), width, height, 1, 1


def test_ring_frame_is_an_annulus_not_a_disc():
    """The ring must outline the cursor, not cover it.

    A filled circle would hide the pointer and whatever it is hovering, which
    defeats the point of showing where the agent is working.
    """
    box = screen_overlay.RING_BOX
    centre = box // 2
    radius = screen_overlay._RING_RADIUS
    frame = screen_overlay._build_ring_frame(float(radius), 3.0, 1.0)

    assert _alpha_at(frame, centre, centre) == 0, "centre must stay clear"
    assert _alpha_at(frame, centre, centre - radius) > 0, "stroke must be drawn"
    assert _alpha_at(frame, centre, centre - radius - 8) == 0, "outside must be clear"


def test_ring_stroke_is_feathered():
    """A hard-edged circle reads as jagged at this size."""
    box = screen_overlay.RING_BOX
    centre = box // 2
    radius = screen_overlay._RING_RADIUS
    frame = screen_overlay._build_ring_frame(float(radius), 3.0, 1.0)

    column = [_alpha_at(frame, centre, y) for y in range(centre - radius - 4, centre - radius + 4)]
    # Partial alpha either side of the solid core is what antialiases it.
    assert any(0 < value < 255 for value in column)


def test_idle_cursor_indicator_follows_the_supplied_cursor_mask_exactly():
    anchor = screen_overlay._CURSOR_ANCHOR
    mask, width, height, hotspot_x, hotspot_y = _test_cursor_mask()
    frame = screen_overlay._build_cursor_glow_frame(
        mask, width, height, hotspot_x, hotspot_y
    )
    left = anchor - hotspot_x
    top = anchor - hotspot_y
    right = left + width - 1
    bottom = top + height - 1

    assert _alpha_at(frame, left, anchor) > 0
    assert _alpha_at(frame, right, anchor) > 0
    assert _alpha_at(frame, anchor, top) > 0
    assert _alpha_at(frame, anchor, bottom) > 0
    assert _alpha_at(frame, anchor + 2, anchor + 2) == 0, "interior stays clear"
    assert _alpha_at(frame, left - 12, anchor) == 0


def test_cursor_glow_has_a_soft_bloom_outside_its_contour():
    anchor = screen_overlay._CURSOR_ANCHOR
    mask, width, height, hotspot_x, hotspot_y = _test_cursor_mask()
    frame = screen_overlay._build_cursor_glow_frame(
        mask, width, height, hotspot_x, hotspot_y
    )
    left = anchor - hotspot_x

    alphas = [
        _alpha_at(frame, left - distance, anchor)
        for distance in range(0, 11)
    ]
    assert alphas[0] > alphas[2] > alphas[6] > 0
    assert alphas[10] == 0


def test_ring_pixels_are_premultiplied():
    """The compositor expects colour already scaled by alpha.

    Un-premultiplied pixels wash the ring out to white at its soft edges.
    """
    box = screen_overlay.RING_BOX
    centre = box // 2
    radius = screen_overlay._RING_RADIUS
    frame = screen_overlay._build_ring_frame(float(radius), 3.0, 1.0)

    offset = ((centre - radius) * box + centre) * 4
    blue, green, red, alpha = frame[offset:offset + 4]
    assert alpha > 0
    for channel in (blue, green, red):
        assert channel <= alpha, "channel exceeds alpha — not premultiplied"


def test_click_frame_keeps_cursor_contour_visible():
    anchor = screen_overlay._CURSOR_ANCHOR
    mask, width, height, hotspot_x, hotspot_y = _test_cursor_mask()
    cursor = screen_overlay._build_cursor_glow_frame(
        mask, width, height, hotspot_x, hotspot_y
    )
    ripple = screen_overlay._build_ring_frame(
        float(screen_overlay._PULSE_MAX_RADIUS), 1.5, 0.15
    )
    combined = screen_overlay._composite_frames(cursor, ripple)

    assert _alpha_at(combined, anchor - hotspot_x, anchor) >= _alpha_at(
        cursor, anchor - hotspot_x, anchor
    )


def test_pulse_frames_expand_and_fade():
    """Later frames are larger and fainter, so a click reads as a ripple."""
    radius = screen_overlay._RING_RADIUS
    near = screen_overlay._build_ring_frame(float(radius), 3.0, 1.0)
    far = screen_overlay._build_ring_frame(
        float(screen_overlay._PULSE_MAX_RADIUS), 1.5, 0.15
    )

    assert max(near[3::4]) > max(far[3::4]), "the pulse should fade as it expands"

    box = screen_overlay.RING_BOX
    centre = box // 2
    # The wide frame draws its stroke further out than the idle ring does.
    assert _alpha_at(far, centre, centre - screen_overlay._PULSE_MAX_RADIUS) > 0
    assert _alpha_at(near, centre, centre - screen_overlay._PULSE_MAX_RADIUS) == 0


def test_click_pulse_is_ignored_when_the_indicator_is_not_showing(monkeypatch):
    """A click outside a computer-use run must not flash a ring."""
    calls = []
    fake = SimpleNamespace(visible=False, click_pulse=lambda: calls.append(1))
    monkeypatch.setattr(screen_overlay, "_instance", lambda: fake)
    monkeypatch.setattr(screen_overlay, "IS_WINDOWS", True)

    screen_overlay.note_click()

    assert calls == []


def test_click_pulse_fires_while_showing(monkeypatch):
    calls = []
    fake = SimpleNamespace(visible=True, click_pulse=lambda: calls.append(1))
    monkeypatch.setattr(screen_overlay, "_instance", lambda: fake)
    monkeypatch.setattr(screen_overlay, "IS_WINDOWS", True)

    screen_overlay.note_click()

    assert calls == [1]


def _alpha_in(frame: bytearray, box: int, x: int, y: int) -> int:
    return frame[(y * box + x) * 4 + 3]


def test_cursor_outline_follows_a_partially_covered_edge():
    """The outline moves with antialiasing instead of jumping a whole pixel.

    Distances used to be measured from the centres of pixels past a faint
    alpha threshold, so the outline stepped with the cursor's pixel grid along
    every slanted edge. A column that is only partly covered now moves the
    glow partway: between no column at all and a solid one.
    """
    width, height = 9, 10
    anchor = screen_overlay._CURSOR_ANCHOR
    glows = []
    for column_zero in (0, 96, 255):
        mask = bytearray(width * height)
        for y in range(height):
            mask[y * width] = column_zero
            for x in range(1, width):
                mask[y * width + x] = 255
        frame = screen_overlay._build_cursor_glow_frame(mask, width, height, 1, 1)
        # Two pixels left of column 0, level with the middle of the mask.
        glows.append(_alpha_at(frame, anchor - 3, anchor + 3))

    assert glows[0] < glows[1] < glows[2]


def test_a_large_cursor_bitmap_keeps_the_glow_its_usual_size():
    """Big cursor bitmaps are common at 100% and must not inflate the glow.

    Applications and pointer settings hand Windows 64 px bitmaps holding an
    ordinary arrow in one corner. Sizing the glow from the bitmap would double
    the outline and the click ripple on exactly those desktops.
    """
    width = height = 64
    mask = bytearray(width * height)
    for y in range(20):
        for x in range(12):
            mask[y * width + x] = 255

    frames, box, anchor = screen_overlay._build_cursor_frames(mask, width, height, 0, 0)

    assert box == screen_overlay.RING_BOX
    # The bloom still ends about eight pixels out from the arrow's left edge.
    assert _alpha_at(frames[0], anchor - 5, anchor + 10) > 0
    assert _alpha_at(frames[0], anchor - 10, anchor + 10) == 0


def test_cursor_glow_and_ripple_scale_with_the_display():
    """At 200% the ripple travels twice as far, in a window large enough."""
    mask, width, height, hotspot_x, hotspot_y = _test_cursor_mask()
    normal, normal_box, anchor = screen_overlay._build_cursor_frames(
        mask, width, height, hotspot_x, hotspot_y
    )
    double, double_box, double_anchor = screen_overlay._build_cursor_frames(
        mask, width, height, hotspot_x, hotspot_y, scale=2.0
    )

    assert double_box > normal_box
    halfway = len(normal) // 2
    # Halfway through the pulse the ring sits at 42.5 px, or 85 px at 200%.
    assert _alpha_in(normal[halfway], normal_box, anchor, anchor - 42) > 0
    assert _alpha_in(double[halfway], double_box, double_anchor, double_anchor - 85) > 0
    assert _alpha_in(normal[halfway], normal_box, anchor, anchor - 70) == 0


def test_click_ripple_eases_out_as_it_fades():
    """Fast off the click point, settling as it fades, not a linear sweep."""
    mask, width, height, hotspot_x, hotspot_y = _test_cursor_mask()
    frames, box, anchor = screen_overlay._build_cursor_frames(
        mask, width, height, hotspot_x, hotspot_y
    )
    radii, peaks = [], []
    # Above the hot spot, clear of the cursor's own glow; the last frame has
    # faded out completely.
    for frame in frames[1:-1]:
        column = {r: _alpha_in(frame, box, anchor, anchor - r) for r in range(12, box // 2)}
        radii.append(sum(r * alpha for r, alpha in column.items()) / sum(column.values()))
        peaks.append(max(column.values()))

    steps = [later - earlier for earlier, later in zip(radii, radii[1:])]
    # Expanding throughout (the last faint frames all but stop, within the
    # rounding of a few alpha levels) and decelerating as it goes.
    assert radii[-1] - radii[0] > 20
    assert all(step > -0.1 for step in steps)
    assert steps[0] > 4 * max(steps[-3:])
    assert peaks == sorted(peaks, reverse=True) and peaks[0] > peaks[-1]


# ── edge glow ────────────────────────────────────────────────────────

def test_glow_corners_turn_smoothly_instead_of_creasing():
    """The nearer-edge distance alone meets itself in a hard diagonal crease.

    Stepping one pixel across a corner's diagonal drops that distance by a
    whole pixel, so the glow folds along the diagonal. The rounded distance
    barely changes there, and away from the corner it is the same as before.
    """
    distance = screen_overlay._corner_distance

    assert distance(31.0, 29.0) == pytest.approx(distance(30.0, 30.0), abs=0.2)
    assert distance(200.0, 10.0) == 10.0
    assert distance(10.0, 200.0) == 10.0


def test_edge_glow_has_a_crisp_rim_and_fades_inward():
    width, height = 400, 300
    glow = screen_overlay._build_glow_rows(width, height)
    column = [glow[(y * width + width // 2) * 4 + 3] for y in range(height // 2)]
    rim = int(screen_overlay._RIM_PX)

    assert len(glow) == width * height * 4
    # A distinct step at the rim's inner edge, then a smooth falloff.
    assert column[rim - 1] - column[rim] > 4 * (column[rim] - column[rim + 1])
    assert column[rim] > column[10] > column[60] > 0
    assert column[screen_overlay.GLOW_PX] == 0
    assert glow[((height // 2) * width + width // 2) * 4 + 3] == 0


def test_edge_glow_reaches_further_on_a_high_density_display():
    width, height = 600, 500
    normal = screen_overlay._build_glow_rows(width, height)
    double = screen_overlay._build_glow_rows(width, height, 2.0)
    probe = (120 * width + width // 2) * 4 + 3

    assert normal[probe] == 0
    assert double[probe] > 0


# ── banner ───────────────────────────────────────────────────────────

def _banner_row(pixels: bytes, width: int, y: int) -> list[tuple[int, ...]]:
    start = y * width * 4
    return [tuple(pixels[start + x * 4:start + x * 4 + 4]) for x in range(width)]


def test_banner_is_a_premultiplied_pill_with_its_words_drawn():
    pixels, width, height, pill_top = screen_overlay._build_banner_frame(1.0)

    assert len(pixels) == width * height * 4
    for offset in range(0, len(pixels), 4):
        assert max(pixels[offset:offset + 3]) <= pixels[offset + 3], "not premultiplied"
    # Only the pill and its shadow are drawn: the frame's corners are clear.
    for x, y in ((0, 0), (width - 1, 0), (0, height - 1), (width - 1, height - 1)):
        assert pixels[(y * width + x) * 4 + 3] == 0
    row = _banner_row(pixels, width, pill_top + screen_overlay._BANNER_HEIGHT // 2)
    # A near-opaque dark body carrying light text.
    assert max(alpha for *_, alpha in row) >= 240
    assert max(green for _, green, _, _ in row) > 180


def test_banner_grows_with_the_display_scale():
    """At 200% the pill is twice the size and twice as far from the top."""
    _, width, height, _ = screen_overlay._build_banner_frame(1.0)
    _, double_width, double_height, pill_top = screen_overlay._build_banner_frame(2.0)

    assert double_height == 2 * height
    assert abs(double_width - 2 * width) <= 6
    left, top = screen_overlay._banner_origin(3840, double_width, pill_top, 2.0)
    assert left == (3840 - double_width) // 2
    assert top + pill_top == 2 * screen_overlay._BANNER_TOP


@pytest.mark.skipif(not screen_overlay.IS_WINDOWS, reason="GDI fallback is Win32-only")
def test_banner_keeps_its_words_without_pillow(monkeypatch):
    """The announcement must survive a missing imaging library."""
    def no_pillow(scale):
        raise ImportError("No module named PIL")

    monkeypatch.setattr(screen_overlay, "_freetype_text_mask", no_pillow)
    monkeypatch.setattr(screen_overlay, "_banner_cache", {})

    pixels, width, height, pill_top = screen_overlay._build_banner_frame(1.0)

    row = _banner_row(pixels, width, pill_top + screen_overlay._BANNER_HEIGHT // 2)
    assert max(green for _, green, _, _ in row) > 180


# ── fades ────────────────────────────────────────────────────────────

def test_the_indicator_fades_out_instead_of_vanishing():
    overlay = screen_overlay._Overlay()
    overlay._shown = True
    overlay._set_fade(1.0)

    overlay._apply_hide()

    # Hidden as far as any caller is concerned, while it eases off screen.
    assert overlay.visible is False
    assert overlay._hiding
    started = overlay._fade_started
    overlay._advance_fade(started + screen_overlay._FADE_OUT_S / 2)
    assert 0.0 < overlay._fade < 1.0
    overlay._advance_fade(started + screen_overlay._FADE_OUT_S)
    assert overlay._fade == 0.0


@pytest.mark.skipif(not screen_overlay.IS_WINDOWS, reason="Win32-only")
def test_a_capture_takes_the_indicator_down_at_once_even_mid_fade():
    """Nothing may linger into a screenshot, and a fading glow is not restored."""
    overlay = screen_overlay._Overlay()
    overlay._shown = True
    overlay._set_fade(1.0)
    overlay._apply_hide()
    result: dict = {}

    overlay._commands.put(("suppress", (), None, result))
    overlay._drain_commands()

    assert result["was_shown"] is False
    assert not overlay._hiding
    assert overlay._fade == 0.0


def test_restoring_after_a_capture_skips_the_fade(monkeypatch):
    """A fade either side of every screenshot would blink the glow all run."""
    calls = []

    class FakeOverlay:
        def suppress(self):
            return True, (0, 0, 1920, 1080)

        def show(self, *bounds, fade_in=True):
            calls.append((bounds, fade_in))
            return True

    monkeypatch.setattr(screen_overlay, "_all_instances", lambda: [FakeOverlay()])
    monkeypatch.setattr(screen_overlay, "_rearm_linger", lambda *args: None)

    with screen_overlay.hidden_for_capture():
        pass

    assert calls == [((0, 0, 1920, 1080), False)]


def test_a_fade_completes_on_time():
    overlay = screen_overlay._Overlay()
    now = time.monotonic()
    overlay._start_fade(1.0, screen_overlay._FADE_IN_S, now)

    assert overlay._advance_fade(now + screen_overlay._FADE_IN_S * 1.01)
    assert overlay._fade == 1.0
    # Finished: later ticks leave it alone.
    assert overlay._advance_fade(now + 1.0) is False
