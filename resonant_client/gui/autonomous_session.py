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
import re
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
from ..orchestration.plan_graph import NodeSpecialization

logger = logging.getLogger(__name__)


# ── Planner-tier mapping (v0.5.1a2) ────────────────────────────────────


# Different DeepSeek tiers behave differently on the planner role.
# v0.5.0 GA smoke (docs/v0.5.0-smoke-results-step2c.md) showed:
#   flash → emits clean JSON immediately, decomposes well from spec
#   pro   → wants to read the codebase first; the strict PLAN prompt
#           treats that exploration as malformed output
# So we route each tier to the planner spec it's wired for. PLAN is
# the snappy "decompose immediately" specialist; PLAN_DEEP is the
# research-first variant that gives pro room to explore before
# emitting the JSON envelope.
#
# Match is by exact model_id. Unrecognized models fall back to PLAN
# (the tighter contract — safer default).
PLANNER_BY_TIER: dict[str, str] = {
    "deepseek-v4-flash:cloud": NodeSpecialization.PLAN,
    "deepseek-v4-pro:cloud": NodeSpecialization.PLAN_DEEP,
}


def planner_for_model(model_id: str) -> str:
    """Return the planner specialization to use for the given Ollama
    model id. Falls back to `PLAN` for unknown models."""
    return PLANNER_BY_TIER.get(model_id or "", NodeSpecialization.PLAN)


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

    Title is short (≤80 chars). Description carries the full
    refined_intent so the implementer's planner sees the complete
    context.

    The original v0.5.0 implementation split on the first period to
    take "the first sentence" as the title — which broke for any
    intent containing filenames like `wordcount.py` or version
    numbers (split would happen INSIDE the filename, producing a
    truncated title like "Build a Python CLI utility `wordcount").
    Pro's planner specifically called this out in v0.5.0 GA smoke:
    "the goal is cut off — I can't tell what the utility should do
    beyond its name." (See `docs/v0.5.0-smoke-results-step2c.md`
    §4.1.)

    v0.5.1a1 fix: use _smart_title that handles filename / version
    periods correctly via a sentence-boundary heuristic (period
    followed by whitespace + capital letter, OR end of string).
    Falls back to clean word-boundary truncation at 80 chars.
    """
    intent = (refined_intent or "").strip()
    if not intent:
        intent = (feature or "").strip()
    if not intent:
        intent = "Implement the feature described in the spec."

    title = _smart_title(intent, max_len=80)
    return title, intent


# Sentence-boundary regex: a period (or `?` / `!`) followed by
# whitespace and an uppercase letter, OR end-of-string. This skips
# periods inside `wordcount.py`, `v1.2.3`, `e.g.`, etc. — those are
# followed by either a non-space char or a lowercase letter, not the
# capital-led start of a new sentence.
_SENTENCE_END_RE = re.compile(r"[.?!]\s+(?=[A-Z])|[.?!]\s*$")


def _smart_title(text: str, *, max_len: int = 80) -> str:
    """Extract a clean title from a multi-sentence intent.

    Strategy (in order):
    1. If the first sentence (per `_SENTENCE_END_RE`) is ≤ max_len,
       use it. This handles "Build X. Add Y. Verify Z." cases where
       the first sentence is the natural title.
    2. Otherwise truncate at max_len, but PREFER to break on a word
       boundary so we don't slice mid-token.
    3. Strip trailing punctuation that would dangle awkwardly.

    The heuristic respects filenames (`wordcount.py`), version
    numbers (`v1.2.3`), abbreviations (`e.g.`), and other periods
    that aren't sentence terminators.
    """
    text = text.strip()
    if not text:
        return "Implement the feature"

    # Try the first sentence first
    match = _SENTENCE_END_RE.search(text)
    if match:
        first_sentence = text[: match.start()].rstrip(".?!").strip()
        if first_sentence and len(first_sentence) <= max_len:
            return first_sentence

    # Truncate at max_len with word-boundary preservation
    if len(text) <= max_len:
        return text.rstrip(".?!,;:")

    truncated = text[:max_len]
    # Walk back to the last whitespace if we sliced mid-word.
    last_space = truncated.rfind(" ")
    if last_space > max_len * 0.6:  # don't truncate too aggressively
        truncated = truncated[:last_space]
    return truncated.rstrip(".?!,;:")


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

    # v0.5.1a2 — pick the planner specialist that suits the model
    # tier. Pro gets PLAN_DEEP (research-first); flash + unknown
    # tiers get PLAN (decompose-immediately). See PLANNER_BY_TIER.
    model_id = getattr(state.backend, "model", "") or ""
    planner_spec = planner_for_model(model_id)
    logger.info(
        "Autonomous mission %s using planner=%s for model=%s",
        intent_id, planner_spec, model_id,
    )

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
        planner_specialization=planner_spec,
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
