"""
autonomous_session — glue between the WS handler in `app.py` and the
`AutonomousMissionDaemon`.

Owns the mid-tier orchestration:
- Parsing the rigorous-grill spec into a usable roadmap (a3 parser
  already produces typed criteria; we just construct the Roadmap +
  save to `<project>/.resonant/`)
- Building the production hooks via `autonomous_factory`
- Constructing + starting the daemon
- Wiring the daemon's `on_event` into the WebSocket emitter
- Routing IntentService events to the dispatch tracker so
  `wait_for_dispatch` works

Per-AppState the daemon is held in `_autonomous_daemons` (dict keyed
by mission intent_id; supports multiple parked missions even though
v0.5.0 only runs one at a time per design §3 non-goals).
"""
from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any, Callable, Optional

from ..gui.autonomous_factory import (
    DispatchTracker,
    build_autonomous_mission_hooks,
)
from ..gui.autonomous_loop import (
    AutonomousMissionConfig,
    AutonomousMissionDaemon,
)
from ..gui import roadmap as roadmap_module
from ..gui.roadmap import Roadmap
from ..orchestration.grill_me import extract_spec
from ..orchestration.intent_service import IntentService

logger = logging.getLogger(__name__)


# ── Time-budget parsing ─────────────────────────────────────────────────


# The rigorous-grill prompt commits to these labels (see §11.5 of the
# Phase 2 design doc). Anything else is treated as full-auto.
_BUDGET_TABLE = {
    "1h": 1 * 3600.0,
    "4h": 4 * 3600.0,
    "6h": 6 * 3600.0,
    "8h": 8 * 3600.0,
    "12h": 12 * 3600.0,
    "24h": 24 * 3600.0,
    "48h": 48 * 3600.0,
}


def parse_time_budget(label: str) -> Optional[float]:
    """Convert a rigorous-grill budget label (`"4h"`, `"full auto"`,
    `"24h"`) to seconds. Returns None for full-auto / unrecognized
    inputs (no time ceiling — the iteration cap still applies)."""
    if not label:
        return None
    key = label.strip().lower()
    if key in _BUDGET_TABLE:
        return _BUDGET_TABLE[key]
    # Tolerate "full auto" / "full-auto" / "no limit" / etc.
    if "full" in key or "auto" in key or "no" in key:
        return None
    # Try to parse e.g. "3h" / "30m" as a sanity-check escape hatch.
    try:
        if key.endswith("h"):
            return float(key[:-1]) * 3600.0
        if key.endswith("m"):
            return float(key[:-1]) * 60.0
    except ValueError:
        pass
    return None


# ── Roadmap bootstrap from spec ─────────────────────────────────────────


def build_roadmap_from_spec(
    *,
    feature: str,
    intent_id: str,
    spec_markdown: str,
    project_path: str,
    started_iso: str = "",
) -> tuple[Roadmap, Path]:
    """Parse a rigorous-grill spec into a Roadmap + persist to
    `<project>/.resonant/roadmap-<intent_id>.md`.

    Returns `(roadmap, path)`. Raises `ValueError` if the spec
    doesn't contain a parseable `## Final spec` block OR the
    parsed criteria list is empty (a misconfigured spec — the
    daemon would refuse to run anyway).

    Bootstraps a single Tier 1 item from the refined_intent (or the
    feature description as a fallback) so the daemon has at least
    one thing to dispatch on its first iteration. The Phase-1 plan-
    graph runner inside that sub-mission decomposes further; REFLECT
    can add follow-up items via the `added` field of its JSON
    verdict if the work needs to grow. (See ADR 12 in the impl
    guide — alternative would be running PLAN up front to split
    the intent into multiple items, but that doubles the cold-start
    latency for missions that converge in 1–3 iterations.)
    """
    parsed = extract_spec(spec_markdown)
    if parsed is None:
        raise ValueError(
            "spec_markdown does not contain a `## Final spec` block — "
            "did the rigorous grill complete?"
        )
    if not parsed.acceptance_criteria:
        raise ValueError(
            "spec contains no typed acceptance criteria. The rigorous "
            "grill should produce ≥4 binary criteria before emitting; "
            "an empty list means the autonomous loop has no convergence "
            "ground truth and would never satisfy."
        )

    rm = Roadmap(
        feature=feature.strip()[:120],
        intent_id=intent_id,
        started_iso=started_iso,
        time_budget_label=parsed.time_budget,
        status="running",
        goal_spec_block=parsed.raw,
        acceptance_criteria=list(parsed.acceptance_criteria),
    )

    # v0.5.0 GA prep — bootstrap T1.1 from the refined_intent so
    # the daemon has work to do on iter 1. Without this, an empty
    # roadmap collides with the "stuck" stopping rule (ADR 10) on
    # the very first reflect pass: criteria fail (because nothing's
    # been built yet), no items added, daemon stops with stuck.
    seed_title, seed_desc = _seed_item_from_intent(parsed.refined_intent, feature)
    roadmap_module.add_item(
        rm, tier=1, title=seed_title, description=seed_desc,
    )

    path = roadmap_module.default_path(project_path, intent_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    roadmap_module.save(rm, path)
    return rm, path


def _seed_item_from_intent(refined_intent: str, feature: str) -> tuple[str, str]:
    """Pick a (title, description) for the bootstrap Tier 1 item.

    Title is short (≤80 chars) — the first sentence of the refined
    intent, or the feature description if refined_intent is empty.
    Description carries the full refined_intent so the implementer
    has the complete context.
    """
    intent = (refined_intent or "").strip()
    if not intent:
        intent = (feature or "").strip()
    if not intent:
        intent = "Implement the feature described in the spec."

    # First sentence (or first 80 chars), stripped of trailing dots.
    first_sentence = intent.split(".", 1)[0].strip()
    title = first_sentence[:80] if first_sentence else intent[:80]
    if not title:
        title = "Implement the feature"

    # Description is the full intent; the implementer's planner sees
    # this as its goal and decomposes from there.
    description = intent
    return title, description


# ── Daemon lifecycle ────────────────────────────────────────────────────


def start_autonomous_mission(
    *,
    state: Any,                       # AppState — duck-typed to avoid a circular import
    intent_id: str,
    feature: str,
    spec_markdown: str,
    on_event: Callable[[dict], None],
    started_iso: str = "",
    image_provider: Optional[Callable[[], Optional[bytes]]] = None,
) -> AutonomousMissionDaemon:
    """Top-level entry point used by the `mission_dispatch_autonomous`
    WS handler.

    What this does:
    1. Parses the spec, builds the roadmap, persists to disk.
    2. Builds production hooks via `autonomous_factory`.
    3. Wires `IntentService.on_event` so terminal sub-mission events
       reach both the GUI (existing chain) and the dispatch tracker
       (so `wait_for_dispatch` unblocks).
    4. Constructs + starts the daemon. Registers it on AppState.
    5. Returns the daemon so the caller can stop / inspect it.

    Raises ValueError for malformed input. Caller is responsible for
    sending an error event to the WS on failure.
    """
    rm, roadmap_path = build_roadmap_from_spec(
        feature=feature,
        intent_id=intent_id,
        spec_markdown=spec_markdown,
        project_path=state.project.project_path,
        started_iso=started_iso,
    )

    # Prep the dispatch tracker — must be ready BEFORE we wire
    # IntentService.on_event so we don't lose any terminal events.
    tracker = DispatchTracker()

    # Wrap the existing on_event chain so terminal sub-mission events
    # ALSO reach the tracker. The GUI emitter still gets every event
    # (so the chat shows mid-iteration tool calls naturally).
    def _combined(ws_event: dict) -> None:
        try:
            tracker.feed_event(ws_event)
        except Exception:
            logger.debug("tracker.feed_event raised", exc_info=True)
        try:
            on_event(ws_event)
        except Exception:
            logger.debug("on_event raised", exc_info=True)

    intent_service: IntentService = state.get_intent_service(on_event=_combined)

    # The daemon's stop_event is what the dispatch tracker's wait()
    # listens on for fast user-stop unblocking. Build it here, pass
    # the same Event into both the tracker and the daemon.
    daemon_stop_event = threading.Event()

    hooks = build_autonomous_mission_hooks(
        intent_service=intent_service,
        dispatch_tracker=tracker,
        project_path=state.project.project_path,
        backend=state.backend,
        project_instructions=getattr(state, "_project_instructions", "") or "",
        settings=state.settings,
        roadmap_path=str(roadmap_path),
        daemon_stop_event=daemon_stop_event,
        on_session_event=on_event,
        image_provider=image_provider,
    )

    config = AutonomousMissionConfig(
        intent_id=intent_id,
        roadmap_path=roadmap_path,
        time_budget_seconds=parse_time_budget(rm.time_budget_label),
    )
    daemon = AutonomousMissionDaemon(
        config=config,
        hooks=hooks,
        on_event=on_event,
    )

    # Override the daemon's stop_event reference so daemon.stop()
    # also signals our tracker-shared Event. The daemon already has
    # its OWN _stop_event; setting it cascades down to wait().
    # We achieve cascading by hooking stop():
    original_stop = daemon.stop

    def _patched_stop(reason: str = "user_stop", message: str = "") -> None:
        daemon_stop_event.set()
        original_stop(reason, message)

    daemon.stop = _patched_stop  # type: ignore[method-assign]

    # Register on AppState so we can find it for autonomous_mission_stop.
    daemons_dict = getattr(state, "_autonomous_daemons", None)
    if daemons_dict is None:
        daemons_dict = {}
        state._autonomous_daemons = daemons_dict
    daemons_dict[intent_id] = daemon

    daemon.start()
    return daemon


def stop_autonomous_mission(
    state: Any,
    intent_id: str,
    *,
    reason: str = "user_stop",
    message: str = "",
) -> bool:
    """Stop a running autonomous mission. Returns True if a daemon
    was found + signalled, False if no such daemon exists."""
    daemons_dict = getattr(state, "_autonomous_daemons", None) or {}
    daemon = daemons_dict.get(intent_id)
    if daemon is None or not daemon.is_running():
        return False
    daemon.stop(reason, message)
    return True


def get_autonomous_daemon(
    state: Any, intent_id: str,
) -> Optional[AutonomousMissionDaemon]:
    """Return the live daemon for `intent_id`, or None."""
    daemons_dict = getattr(state, "_autonomous_daemons", None) or {}
    return daemons_dict.get(intent_id)


def cleanup_finished_daemons(state: Any) -> int:
    """Drop daemons that have already exited from the AppState's
    registry. Called on project switch + periodically. Returns
    the number cleaned up."""
    daemons_dict = getattr(state, "_autonomous_daemons", None) or {}
    if not daemons_dict:
        return 0
    finished = [k for k, d in daemons_dict.items() if not d.is_running()]
    for k in finished:
        daemons_dict.pop(k, None)
    return len(finished)
