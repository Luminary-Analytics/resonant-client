"""Computer-use capability gating and the on-screen indicator.

The overlay itself is Win32 and is verified by eye on Windows. What is tested
here is everything that decides *whether* it runs and *which* screen it points
at, plus the capability rule that decides which models are offered the desktop
tools at all — all of which fail silently.
"""

from types import SimpleNamespace

import pytest

from resonant_client.capabilities import ModelCapabilities, infer_model_capabilities
from resonant_client.engine import screen_overlay
from resonant_client.engine.session import Session
from resonant_client.engine.tools import (
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
        "resonant_client.engine.computer_use.list_monitors", lambda: monitors
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


# ── cursor ring ──────────────────────────────────────────────────────

def _alpha_at(frame: bytearray, x: int, y: int) -> int:
    box = screen_overlay.RING_BOX
    return frame[(y * box + x) * 4 + 3]


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
