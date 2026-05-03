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


# ── Planner specialization (v0.5.4a1: PLAN_DEEP unconditionally) ──────


# v0.5.1a2 introduced PLANNER_BY_TIER, a per-tier routing dict that
# sent flash to PLAN and pro to PLAN_DEEP. The motivation was real:
# pro fails the strict "emit JSON immediately" contract of PLAN.
#
# v0.5.4a1 removes the routing because:
# 1. Pro is the default tier (v0.5.2). The fallback case (unmapped
#    model → PLAN) was a footgun: any new model defaulted to the
#    wrong planner without anyone noticing until a smoke failed.
# 2. PLAN_DEEP is a strict superset of PLAN — its Phase 2 IS the
#    PLAN prompt. Flash benefits from the optional Phase 1 too;
#    skipping exploration is allowed but never required.
# 3. Adding new models (kimi-k2.6, glm-5.1, minimax-m2.7) no longer
#    requires editing this routing dict; they all just work.
#
# Callers can still override via `planner_specialization=` on the
# autonomous-factory hook builder if a specific spec is wanted.
_DEFAULT_PLANNER_SPEC: str = NodeSpecialization.PLAN_DEEP


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
    1. Collapse internal whitespace (newlines + tabs → single space)
       so the title fits on one line of markdown. The roadmap's
       item-line regex is single-line; a newline in the title would
       split the item across two lines and make it unparseable
       (v0.5.1a1 regression discovered during pro smoke).
    2. If the first sentence (per `_SENTENCE_END_RE`) is ≤ max_len,
       use it. This handles "Build X. Add Y. Verify Z." cases where
       the first sentence is the natural title.
    3. Otherwise truncate at max_len, but PREFER to break on a word
       boundary so we don't slice mid-token.
    4. Strip trailing punctuation that would dangle awkwardly.

    The heuristic respects filenames (`wordcount.py`), version
    numbers (`v1.2.3`), abbreviations (`e.g.`), and other periods
    that aren't sentence terminators.
    """
    text = text.strip()
    if not text:
        return "Implement the feature"

    # Collapse internal whitespace BEFORE looking for sentence
    # boundaries. Multi-line intents would otherwise produce a
    # title with embedded `\n`, which breaks the single-line
    # roadmap item parser.
    text = re.sub(r"\s+", " ", text)

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
    return _spawn_autonomous_daemon(
        state=state,
        intent_id=intent_id,
        roadmap=rm,
        roadmap_path=roadmap_path,
        on_event=on_event,
        image_provider=image_provider,
    )


def resume_autonomous_mission(
    *,
    state: Any,
    intent_id: str,
    on_event: Callable[[dict], None],
    image_provider: Optional[Callable[[], Optional[bytes]]] = None,
) -> AutonomousMissionDaemon:
    """v0.5.3a1 — resume an autonomous mission whose daemon was
    interrupted (server restart, app crash, laptop sleep). Loads the
    EXISTING roadmap from disk; does NOT re-bootstrap items, so any
    progress already made (checked items, validated criteria, prior
    iteration log entries) is preserved.

    Raises:
    - ValueError if no roadmap exists at the expected path
    - ValueError if the roadmap has no acceptance criteria (would
      collide with the daemon's misconfigured-stop rule)
    - RuntimeError if a live daemon is already registered for this
      intent_id (don't double-spawn)

    The daemon's first iteration picks up at `next_unchecked_item` —
    if the original mission was mid-iteration when interrupted, the
    interrupted item stays unchecked and gets re-dispatched. Phase-1
    sub-missions are idempotent enough that this is usually fine
    (the implementer reads the file system, sees what's there, no-ops
    or refines as needed).

    The time budget timer RESETS on resume — we don't try to back-
    date it. If a user wants strict budget enforcement, they can
    pass a smaller budget on resume; otherwise the budget is fresh.
    """
    # Reject double-resume.
    existing = getattr(state, "_autonomous_daemons", None) or {}
    prior = existing.get(intent_id)
    if prior is not None and prior.is_running():
        raise RuntimeError(
            f"Autonomous mission {intent_id!r} is already running; "
            f"can't resume what isn't interrupted"
        )

    roadmap_path = roadmap_module.default_path(
        state.project.project_path, intent_id,
    )
    if not roadmap_path.exists():
        raise ValueError(
            f"Cannot resume mission {intent_id!r}: no roadmap at "
            f"{roadmap_path}. The original mission may not have run "
            f"long enough to persist its roadmap, or the project root "
            f"changed since the mission started."
        )

    rm = roadmap_module.load(roadmap_path)
    if not rm.has_any_acceptance_criteria():
        raise ValueError(
            f"Roadmap at {roadmap_path} has no acceptance criteria; "
            f"refusing to resume into a state the daemon would "
            f"immediately stop with `misconfigured`."
        )

    logger.info(
        "Resuming autonomous mission %s — roadmap has %d items "
        "(%d already checked) and %d acceptance criteria",
        intent_id,
        len(rm.items),
        sum(1 for i in rm.items if i.checked),
        len(rm.acceptance_criteria),
    )

    return _spawn_autonomous_daemon(
        state=state,
        intent_id=intent_id,
        roadmap=rm,
        roadmap_path=roadmap_path,
        on_event=on_event,
        image_provider=image_provider,
    )


def build_roadmap_inspector_payload(
    *, intent_id: str, roadmap: Roadmap, roadmap_path: "Path",
    reflection_max_chars: int = 600,
) -> dict:
    """v0.5.3a3 — Marshal a Roadmap into the dict the sidebar inspector
    consumes. Pure function (no I/O) so the WS handler stays thin and
    tests can construct + verify payloads directly.

    Trims the reflection summary to `reflection_max_chars` so the
    sidebar doesn't have to render multi-page narrative — full content
    lives in roadmap.md anyway.

    Acceptance criteria are emitted in their canonical order with
    `passed` (None/True/False), `is_blocking` (False for `[manual]`),
    and the type tag preserved. The frontend uses `is_blocking` to
    distinguish convergence-gating criteria from advisory ones.
    """
    next_item = roadmap.next_unchecked_item()
    passed_count, total_blocking = roadmap.acceptance_summary()
    reflection = (roadmap.reflection_summary or "").strip()
    if reflection_max_chars and len(reflection) > reflection_max_chars:
        reflection = reflection[:reflection_max_chars].rstrip() + "…"

    return {
        "intent_id": intent_id,
        "roadmap_exists": True,
        "roadmap_path": str(roadmap_path),
        "feature": roadmap.feature,
        "status": roadmap.status,
        "time_budget_label": roadmap.time_budget_label,
        "started_iso": roadmap.started_iso,
        "is_converged": roadmap.is_converged(),
        "acceptance_summary": {
            "passed": passed_count,
            "total_blocking": total_blocking,
            "criteria": [
                {
                    "type": c.type,
                    "text": c.text,
                    "passed": c.passed,
                    "is_blocking": c.is_blocking,
                }
                for c in roadmap.acceptance_criteria
            ],
        },
        "items": [
            {
                "id": it.id,
                "tier": it.tier,
                "title": it.title,
                "checked": it.checked,
            }
            for it in roadmap.items
        ],
        "next_item": (
            {"id": next_item.id, "title": next_item.title}
            if next_item else None
        ),
        "iteration_count": len(roadmap.iteration_log),
        "reflection_summary": reflection,
    }


def find_orphaned_autonomous_missions(state: Any) -> list[dict]:
    """v0.5.3a1 — scan the project's sessions for missions in
    `autonomous_running` phase that have NO live daemon. These are
    candidates for resume — the daemon either was never started, or
    was interrupted by an app restart / crash.

    Returns a list of dicts with the info the caller (WS handler /
    GUI) needs to surface a "Resume" button:
    - session_id
    - intent_id
    - feature (the seed description)
    - autonomous_started_at (epoch float, may be missing)
    - roadmap_path (absolute)
    - roadmap_exists (bool)

    Empty list when there are no orphans (the common case at
    steady state).
    """
    if not getattr(state, "project", None):
        return []
    daemons_dict = getattr(state, "_autonomous_daemons", None) or {}
    live_intent_ids = {
        intent_id for intent_id, d in daemons_dict.items() if d.is_running()
    }

    orphans: list[dict] = []
    sessions = state.project.list_all_sessions() if hasattr(
        state.project, "list_all_sessions",
    ) else []
    for sess in sessions:
        ms = sess.get("mission_state") if isinstance(sess, dict) else None
        if not ms:
            continue
        if ms.get("phase") != "autonomous_running":
            continue
        intent_id = ms.get("intent_id", "")
        if not intent_id or intent_id in live_intent_ids:
            continue
        roadmap_path = roadmap_module.default_path(
            state.project.project_path, intent_id,
        )
        orphans.append({
            "session_id": sess.get("id", ""),
            "intent_id": intent_id,
            "feature": ms.get("seed_feature", "")
            or sess.get("title", ""),
            "autonomous_started_at": ms.get("autonomous_started_at"),
            "roadmap_path": str(roadmap_path),
            "roadmap_exists": roadmap_path.exists(),
        })
    return orphans


def _spawn_autonomous_daemon(
    *,
    state: Any,
    intent_id: str,
    roadmap: Roadmap,
    roadmap_path: "Path",
    on_event: Callable[[dict], None],
    image_provider: Optional[Callable[[], Optional[bytes]]] = None,
) -> AutonomousMissionDaemon:
    """v0.5.3a1 internal — shared spawn path used by both
    `start_autonomous_mission` (fresh start) and
    `resume_autonomous_mission` (existing roadmap on disk).

    Builds production hooks, wires the IntentService combined
    callback, hardcodes PLAN_DEEP as the planner spec (v0.5.4a1
    consolidation — was per-tier-routed via PLANNER_BY_TIER), and
    constructs + starts the daemon, registers on AppState. The
    roadmap is assumed to already be persisted to disk — caller's
    job.
    """
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

    # v0.5.4a1 — always use PLAN_DEEP. The previous per-tier routing
    # (PLANNER_BY_TIER) was a footgun for new models; PLAN_DEEP is a
    # strict superset of PLAN so flash works fine under it too.
    model_id = getattr(state.backend, "model", "") or ""
    planner_spec = _DEFAULT_PLANNER_SPEC
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
        time_budget_seconds=parse_time_budget(roadmap.time_budget_label),
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
