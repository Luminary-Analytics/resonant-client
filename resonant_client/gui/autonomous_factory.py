"""
autonomous_factory — production wiring for `AutonomousMissionDaemon`.

The daemon (in `gui.autonomous_loop`) takes all its I/O via injected
hooks, so the daemon module stays free of subprocess calls, model
sessions, and threading details around `IntentService`. This module
is the factory that builds those production hooks for live use.

## What this module provides

- `build_autonomous_mission_hooks(...)` — returns a `DaemonHooks`
  populated with real callables: dispatch via `IntentService.start_intent`,
  wait via a `_DispatchTracker` subscribed to intent events, git
  subprocess calls for `get_commit_sha` / `validate_sha`, and a
  REFLECT model session via `LocalSpecialistRunner` against a
  one-node plan graph.
- `_DispatchTracker` — small helper that watches IntentService
  events and signals per-intent `threading.Event`s when a sub-mission
  reaches a terminal state. Tests can construct one and feed events
  directly without spinning up a real `IntentService`.
- `build_reflect_goal` — pure function that takes a roadmap +
  ReflectPassResult and produces the goal string for the REFLECT
  specialist. Easy to unit-test; no model required.
- `parse_reflect_verdict` — pure function that parses the JSON
  envelope from the REFLECT specialist's output. Tolerant of common
  drift modes (extra prose, fenced or unfenced JSON, missing keys).

## Design: keep the factory testable too

Even the factory's helpers are pure where possible. The `LocalSpecialistRunner`
is the only piece that genuinely needs a backend; everything else
(`_DispatchTracker`, `build_reflect_goal`, `parse_reflect_verdict`)
runs without one.
"""
from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Any, Callable, Optional

from resonant_client.processes import background_process_kwargs

from ..engine.tools import AGENT_TOOLS
from ..gui.autonomous_loop import (
    DaemonHooks,
    DispatchOutcome,
    FullReflectOutcome,
)
from ..gui.roadmap import (
    AcceptanceCriterion,
    Roadmap,
    RoadmapItem,
)
from ..orchestration.acceptance_check import (
    BashRunner,
    CheckContext,
    VisionRunner,
)
from ..orchestration.intent_service import IntentService
from ..orchestration.plan_graph import (
    NodeSpecialization,
    NodeStatus,
    PlanGraph,
    PlanNode,
    new_node_id,
)
from ..orchestration.reflect import ReflectPassResult
from ..orchestration.runner import LocalSpecialistRunner

logger = logging.getLogger(__name__)


# ── Dispatch tracking (intent.complete / intent.failed listener) ────────


_TERMINAL_EVENTS = frozenset({
    "intent.complete",
    "intent.failed",
    "intent.cancelled",
})


class DispatchTracker:
    """Subscribes to `IntentService` events and signals per-intent
    `threading.Event`s when a sub-mission reaches a terminal state.

    The autonomous daemon's `dispatch_item` hook uses this to bridge
    the async event stream into a synchronous "wait for this specific
    intent to finish" call.

    Thread-safe. The caller is expected to:
        1. `watch(intent_id)` BEFORE the corresponding sub-mission is
           dispatched (avoid losing events to a race).
        2. `wait(intent_id)` from the daemon thread.
        3. Connect `feed_event` into IntentService's `on_event` chain.
    """

    def __init__(self) -> None:
        self._events: dict[str, threading.Event] = {}
        self._outcomes: dict[str, DispatchOutcome] = {}
        self._lock = threading.Lock()

    def watch(self, intent_id: str) -> None:
        with self._lock:
            if intent_id not in self._events:
                self._events[intent_id] = threading.Event()

    def feed_event(self, ws_event: dict) -> None:
        """Inspect a WS event from IntentService. If it's terminal,
        record the outcome and unblock the watching thread."""
        kind = ws_event.get("event", "")
        if kind not in _TERMINAL_EVENTS:
            return
        intent_id = ws_event.get("intent_id", "")
        if not intent_id:
            return

        if kind == "intent.complete":
            outcome = DispatchOutcome(success=True, handle=intent_id)
        elif kind == "intent.cancelled":
            outcome = DispatchOutcome(
                success=False,
                error="cancelled",
                handle=intent_id,
            )
        else:  # intent.failed
            outcome = DispatchOutcome(
                success=False,
                error=ws_event.get("error", "(no error message)"),
                handle=intent_id,
            )

        with self._lock:
            self._outcomes[intent_id] = outcome
            ev = self._events.get(intent_id)
        if ev is not None:
            ev.set()

    def wait(
        self,
        intent_id: str,
        *,
        stop_event: Optional[threading.Event] = None,
        poll_seconds: float = 0.5,
    ) -> DispatchOutcome:
        """Block until the intent terminates OR `stop_event` fires.
        Returns the recorded `DispatchOutcome` on terminal arrival
        or a synthesized cancelled-outcome on stop."""
        with self._lock:
            ev = self._events.get(intent_id)
        if ev is None:
            return DispatchOutcome(
                success=False,
                error=f"intent_id {intent_id!r} not watched — "
                      f"caller forgot to call watch() before dispatch",
                handle=intent_id,
            )

        while True:
            if ev.wait(timeout=poll_seconds):
                with self._lock:
                    return self._outcomes.get(
                        intent_id,
                        DispatchOutcome(
                            success=False,
                            error="terminal event fired but no outcome recorded",
                            handle=intent_id,
                        ),
                    )
            if stop_event is not None and stop_event.is_set():
                return DispatchOutcome(
                    success=False,
                    error="daemon stop_event triggered",
                    handle=intent_id,
                )

    def forget(self, intent_id: str) -> None:
        """Drop an intent's tracking state. Called by the daemon
        after a successful wait so memory doesn't grow with iteration
        count over very long missions."""
        with self._lock:
            self._events.pop(intent_id, None)
            self._outcomes.pop(intent_id, None)


# ── Git helpers for SHA hooks ───────────────────────────────────────────


def make_git_get_commit_sha(project_path: str) -> Callable[[], Optional[str]]:
    """Returns a callable that reads HEAD's commit SHA via
    `git log -1 --format=%H`. None on any failure (no git binary,
    no commits yet, repo missing).
    """
    git = shutil.which("git")

    def _get() -> Optional[str]:
        if not git:
            return None
        try:
            proc = subprocess.run(
                [git, "log", "-1", "--format=%H"],
                cwd=project_path,
                capture_output=True,
                text=True,
                timeout=5.0,
                check=False,
                **background_process_kwargs(),
            )
            if proc.returncode != 0:
                return None
            sha = proc.stdout.strip()
            if not sha:
                return None
            # Sanity check: hex, 6-40 chars (matches roadmap parser).
            if re.match(r"^[0-9a-f]{6,40}$", sha):
                return sha
            return None
        except (subprocess.TimeoutExpired, OSError):
            logger.debug("git log probe failed", exc_info=True)
            return None

    return _get


def make_git_validate_sha(project_path: str) -> Callable[[str], bool]:
    """Returns a callable that validates a SHA via
    `git rev-parse --verify <sha>^{commit}`. True iff the SHA is a
    real commit object in the repo.
    """
    git = shutil.which("git")

    def _validate(sha: str) -> bool:
        if not git or not sha:
            return False
        try:
            proc = subprocess.run(
                [git, "rev-parse", "--verify", f"{sha}^{{commit}}"],
                cwd=project_path,
                capture_output=True,
                text=True,
                timeout=5.0,
                check=False,
                **background_process_kwargs(),
            )
            return proc.returncode == 0
        except (subprocess.TimeoutExpired, OSError):
            logger.debug("git rev-parse probe failed", exc_info=True)
            return False

    return _validate


# ── REFLECT specialist invocation ───────────────────────────────────────


def build_reflect_goal(
    roadmap: Roadmap,
    pass_result: ReflectPassResult,
    *,
    roadmap_path: Optional[str] = None,
    decision_context: str = "",
) -> str:
    """Produce the goal string for the REFLECT specialist's full-pass
    invocation.

    Includes:
    - `mode: full` directive
    - Roadmap path (so the model knows where to file_edit)
    - Current acceptance-criteria status (deterministic results
      ALREADY written; the model trusts these)
    - The list of [chrome] criteria the model must validate
    - The list of [manual] criteria to surface in the handoff
    - A reminder that the JSON envelope is required at the end
    - v0.5.8a2: optional `decision_context` recording the user's
      response to a previous `decision_request` so the model knows
      to ACT on the choice instead of asking again.

    Pure function — easy to test without a backend.
    """
    lines = ["mode: full"]
    if roadmap_path:
        lines.append(f"roadmap_path: {roadmap_path}")
    if decision_context:
        # Top-of-prompt placement so the model definitely sees it
        # before getting into the criteria walk. The model is
        # expected to act on the user's choice via file_edit /
        # bash / etc. and emit a normal verdict (NOT another
        # decision_request) on this pass.
        lines.append("")
        lines.append("## User decision (act on this)")
        lines.append("")
        lines.append(decision_context.strip())
        lines.append("")
        lines.append(
            "Apply the user's choice via `file_edit` / `bash` / "
            "the appropriate tool, then emit your normal verdict. "
            "Do NOT emit another `decision_request` for the same "
            "question — the user has already answered."
        )
    lines.append("")
    lines.append("## Acceptance criteria status (post-deterministic-pass)")
    lines.append("")
    if not roadmap.acceptance_criteria:
        lines.append("(none — this is a misconfiguration; refuse to declare satisfied)")
    else:
        for c in roadmap.acceptance_criteria:
            status = _criterion_status_label(c)
            lines.append(f"- `[{c.type}]` {c.text} — {status}")
    lines.append("")

    if pass_result.chrome_pending:
        lines.append("## [chrome] criteria you need to validate")
        lines.append("")
        lines.append(
            "Drive the browser with the built-in `browser_*` tools — "
            "`browser_navigate`, `browser_read`, `browser_click`, "
            "`browser_screenshot` and friends. Chrome starts automatically on "
            "first use. A user-configured browser MCP, if present, also works. "
            "Mark each criterion via `file_edit` to the roadmap (flip the "
            "checkbox `[ ]` → `[x]` and append a short evidence note)."
        )
        lines.append("")
        for i, c in enumerate(pass_result.chrome_pending, start=1):
            lines.append(f"{i}. `{c.text}`")
        lines.append("")

    if pass_result.manual_pending:
        lines.append("## [manual] criteria (surface in handoff, do NOT validate)")
        lines.append("")
        for c in pass_result.manual_pending:
            lines.append(f"- `{c.text}`")
        lines.append("")

    # A short tally helps the model orient itself.
    passed_count, total_count = roadmap.acceptance_summary()
    lines.append(
        f"Tally: {passed_count}/{total_count} blocking criteria passed; "
        f"{len(pass_result.chrome_pending)} chrome pending; "
        f"{len(pass_result.manual_pending)} manual pending."
    )
    lines.append("")
    lines.append(
        "Emit the structured JSON envelope at the end of your response, "
        "as documented in your system prompt. The runtime's daemon will "
        "cross-check `verdict=satisfied` against the roadmap state — if "
        "the roadmap says any criterion is still red, your `satisfied` "
        "claim will be overridden to `continue`."
    )
    return "\n".join(lines)


def _criterion_status_label(c: AcceptanceCriterion) -> str:
    """Compact human-readable label for the criterion's current state.
    Used inside the REFLECT goal-text so the model has a quick index
    of what's already settled vs what it needs to drive."""
    if c.passed is True:
        return f"PASS ({c.evidence[:80]})" if c.evidence else "PASS"
    if c.passed is False:
        return f"FAIL ({c.evidence[:80]})" if c.evidence else "FAIL"
    return "PENDING"


# Lenient JSON-block extractor. Models drift in two ways:
#   1. fenced ```json ... ``` blocks (preferred)
#   2. bare {...} at end-of-message
# We try the fence first, fall back to the last balanced-braces block.
_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*\n(\{.*?\})\s*\n```", re.DOTALL)


def parse_reflect_verdict(text: str) -> dict:
    """Pull the JSON envelope out of a REFLECT specialist response.

    Returns a dict with the parsed verdict, or a degraded
    `{"verdict": "continue", "_parse_error": "..."}` if extraction
    failed. We never raise — the daemon needs a usable result.
    """
    if not text:
        return {"verdict": "continue", "_parse_error": "empty response"}

    # Try the fenced block first.
    fence_match = _JSON_FENCE_RE.search(text)
    candidate = fence_match.group(1) if fence_match else None

    if candidate is None:
        # Fall back to the last balanced { ... } in the text.
        candidate = _last_balanced_json_block(text)

    if candidate is None:
        return {
            "verdict": "continue",
            "_parse_error": "no JSON block found in response",
        }

    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError as exc:
        return {
            "verdict": "continue",
            "_parse_error": f"json.loads failed: {exc}",
        }

    if not isinstance(parsed, dict):
        return {
            "verdict": "continue",
            "_parse_error": "JSON root was not an object",
        }

    # Defensive defaults — daemon expects every key, even when empty.
    parsed.setdefault("verdict", "continue")
    parsed.setdefault("completed", [])
    parsed.setdefault("chrome_results", [])
    parsed.setdefault("added", [])
    parsed.setdefault("blocked", [])
    parsed.setdefault("manual_pending", [])
    parsed.setdefault("summary", "")
    parsed.setdefault("estimated_remaining_minutes", 0)
    # v0.5.8a2 — optional structured human-decision-required payload.
    # When present (and well-formed), the daemon parks instead of
    # treating the verdict as terminal. Validated lazily — the daemon
    # checks for question + at least one option before parking.
    parsed.setdefault("decision_request", None)
    return parsed


def validate_decision_request(payload: Any) -> Optional[dict]:
    """v0.5.8a2 — sanity-check a `decision_request` from a REFLECT
    JSON envelope. Returns the cleaned dict if it has the minimum
    required shape (question + at least one option with id+label),
    or None if the payload is malformed/missing/empty.

    The daemon uses this to decide whether to park: an ill-formed
    decision_request means the model tried to ask but didn't follow
    the schema, so we treat it as "no decision requested" and let
    the verdict path proceed normally.
    """
    if not isinstance(payload, dict):
        return None
    question = str(payload.get("question") or "").strip()
    if not question:
        return None
    raw_options = payload.get("options")
    if not isinstance(raw_options, list) or not raw_options:
        return None
    cleaned_options: list[dict] = []
    seen_ids: set[str] = set()
    for opt in raw_options:
        if not isinstance(opt, dict):
            continue
        opt_id = str(opt.get("id") or "").strip()
        opt_label = str(opt.get("label") or "").strip()
        if not opt_id or not opt_label or opt_id in seen_ids:
            continue
        seen_ids.add(opt_id)
        opt_detail = str(opt.get("detail") or "").strip()
        cleaned_options.append({
            "id": opt_id,
            "label": opt_label,
            "detail": opt_detail,
        })
    if not cleaned_options:
        return None
    return {
        "question": question,
        "options": cleaned_options,
        "context": str(payload.get("context") or "").strip(),
    }


def _last_balanced_json_block(text: str) -> Optional[str]:
    """Find the last top-level balanced `{...}` substring in `text`.

    Handles three cases the model commonly produces:
    1. **Single object** at the end of the message — return it.
    2. **Two top-level objects in sequence** (model wrote a sketch
       then the real answer) — return the last.
    3. **Stray unmatched `{`** earlier in the text (model used `{`
       inside prose without closing) followed by the real object —
       skip the stray, return the real object.
    4. **Nested objects** (`{outer: {inner}}`) — return the outer
       block (a single forward balance from the outer's `{` includes
       the inner naturally).

    Strategy: scan forward, find each `{`, attempt to balance from
    it. On success, record `(start, end)` and skip past `end`. On
    failure (stray `{` that never closes), skip this `{` and try
    the next one. Return the LAST recorded block.

    Strings are skipped so `"has {brace}"` doesn't perturb depth.
    """
    candidates: list[tuple[int, int]] = []
    pos = 0
    n = len(text)

    while pos < n:
        i = text.find("{", pos)
        if i < 0:
            break
        end = _try_balance(text, i)
        if end >= 0:
            candidates.append((i, end))
            pos = end + 1  # continue after this whole block
        else:
            pos = i + 1  # skip this stray `{` and try again

    if not candidates:
        return None
    start, end = candidates[-1]
    return text[start:end + 1]


def _try_balance(text: str, start: int) -> int:
    """Try to find the matching `}` for the `{` at position `start`.
    Returns the index of the matching `}` (inclusive) on success,
    -1 if `text` runs out before the brace closes."""
    depth = 0
    in_string = False
    escape = False
    for j in range(start, len(text)):
        c = text[j]
        if escape:
            escape = False
            continue
        if in_string:
            if c == "\\":
                escape = True
            elif c == '"':
                in_string = False
            continue
        if c == '"':
            in_string = True
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return j
    return -1


def make_reflect_runner(
    *,
    backend: Any,
    project_path: str,
    project_instructions: str,
    settings: Any,
    roadmap_path: str,
    cancel_event: Optional[threading.Event] = None,
    on_session_event: Optional[Callable[[dict], None]] = None,
    specialist_backend_resolver: Optional[Callable[[str], Any]] = None,
    mcp_manager: Any = None,
) -> Callable[..., FullReflectOutcome]:
    """Build a callable suitable for `DaemonHooks.run_full_reflect`.

    Each invocation builds a one-node PlanGraph with
    specialization=REFLECT, runs it via `LocalSpecialistRunner`, and
    parses the JSON verdict from the specialist's summary.

    The reflect runner does NOT run the deterministic pass — that's
    `run_reflect_pass`'s job, called by the daemon BEFORE this
    runner. By the time we get here, the roadmap on disk already
    has [bash]/[vision] criteria marked passed/failed.

    v0.5.8a2: the returned callable accepts an optional
    `decision_context: str = ""` kwarg. When non-empty, it's threaded
    into the REFLECT prompt via `build_reflect_goal` so the model can
    act on the user's response to a previous decision_request.
    """
    runner = LocalSpecialistRunner(
        backend=backend,
        project_path=project_path,
        all_tools=list(AGENT_TOOLS) + (mcp_manager.get_all_tools() if mcp_manager else []),
        project_instructions=project_instructions or "",
        settings=settings,
        cancel_event=cancel_event,
        on_session_event=on_session_event,
        specialist_backend_resolver=specialist_backend_resolver,
        mcp_manager=mcp_manager,
    )

    def _run_reflect(
        roadmap: Roadmap,
        pass_result: ReflectPassResult,
        *,
        decision_context: str = "",
    ) -> FullReflectOutcome:
        graph = PlanGraph.new("autonomous mission reflect pass")
        node = PlanNode(
            id=new_node_id(),
            intent_id=graph.intent_id,
            goal=build_reflect_goal(
                roadmap, pass_result, roadmap_path=roadmap_path,
                decision_context=decision_context,
            ),
            specialization=NodeSpecialization.REFLECT,
        )
        graph.add_node(node)

        try:
            result = runner(node, graph)
        except Exception as exc:
            logger.exception("REFLECT runner crashed")
            return FullReflectOutcome(
                pass_result=pass_result,
                verdict="continue",
                error=f"REFLECT runner raised: {exc}",
                summary="REFLECT model session failed",
            )

        if result.status == NodeStatus.ABANDONED:
            return FullReflectOutcome(
                pass_result=pass_result,
                verdict="continue",
                error="REFLECT cancelled before output",
                summary=result.summary or "(cancelled)",
            )

        verdict_dict = parse_reflect_verdict(result.summary or "")
        # v0.5.8a2 — extract + validate the optional decision_request.
        # Malformed payloads are silently dropped (decision_request=None)
        # so the daemon's verdict path proceeds normally rather than
        # parking on garbage.
        decision_request = validate_decision_request(
            verdict_dict.get("decision_request"),
        )
        return FullReflectOutcome(
            pass_result=pass_result,
            verdict=str(verdict_dict.get("verdict", "continue")),
            chrome_results=list(verdict_dict.get("chrome_results", [])),
            added_items=list(verdict_dict.get("added", [])),
            blocked_items=list(verdict_dict.get("blocked", [])),
            manual_pending=list(verdict_dict.get("manual_pending", [])),
            summary=str(verdict_dict.get("summary", "")),
            estimated_remaining_minutes=int(
                verdict_dict.get("estimated_remaining_minutes", 0) or 0
            ),
            error=str(verdict_dict.get("_parse_error", "")),
            decision_request=decision_request,
        )

    return _run_reflect


# ── CheckContext factory ────────────────────────────────────────────────


def make_check_context_factory(
    *,
    project_path: str,
    settings: Any = None,
    image_provider: Optional[Callable[[], Optional[bytes]]] = None,
) -> Callable[[Roadmap], CheckContext]:
    """Returns a callable that builds a CheckContext for each
    deterministic-pass run. The runners are constructed fresh per
    pass so config drift (vision model swap in Settings, project
    path change) is picked up.

    `image_provider` is what `[vision]` checks use to capture the
    surface to compare. For the v0.5.0 GA the daemon won't drive
    real screenshot capture — `[vision]` criteria run only when the
    CALLER supplies an image_provider that knows what to capture.
    Most missions will pass image_provider=None, in which case
    `[vision]` criteria error out gracefully.
    """
    bash_timeout = 60.0
    vision_url = "http://127.0.0.1:11434"
    # v0.5.0a9 — keep the default in sync with
    # acceptance_check.DEFAULT_VISION_MODEL so the GUI and the
    # daemon agree on the fallback when settings.vision is unset.
    from ..orchestration.acceptance_check import DEFAULT_VISION_MODEL
    vision_model = DEFAULT_VISION_MODEL

    if settings is not None:
        try:
            net = getattr(settings, "network", None)
            if net is not None:
                ollama_url = getattr(net, "ollama_url", "") or vision_url
                vision_url = ollama_url
            vision_cfg = getattr(settings, "vision", None)
            if vision_cfg is not None:
                model_attr = getattr(vision_cfg, "default_model", "")
                if model_attr:
                    vision_model = model_attr
        except Exception:
            logger.debug(
                "settings introspection in CheckContext factory raised",
                exc_info=True,
            )

    def _build(_roadmap: Roadmap) -> CheckContext:
        return CheckContext(
            project_path=project_path,
            bash_runner=BashRunner(
                timeout_seconds=bash_timeout,
                cwd=project_path,
            ),
            vision_runner=VisionRunner(
                ollama_url=vision_url,
                model=vision_model,
            ),
            image_provider=image_provider,
        )

    return _build


# ── Top-level factory ───────────────────────────────────────────────────


def build_autonomous_mission_hooks(
    *,
    intent_service: IntentService,
    dispatch_tracker: DispatchTracker,
    project_path: str,
    backend: Any,
    project_instructions: str,
    settings: Any,
    roadmap_path: str,
    daemon_stop_event: threading.Event,
    on_session_event: Optional[Callable[[dict], None]] = None,
    image_provider: Optional[Callable[[], Optional[bytes]]] = None,
    planner_specialization: Optional[str] = None,
    specialist_backend_resolver: Optional[Callable[[str], Any]] = None,
    mcp_manager: Any = None,
    enable_skill_extraction: bool = True,
    enable_skill_curator: bool = True,
    enable_skill_loader: bool = True,
) -> DaemonHooks:
    """Top-level constructor — wires every hook to a real
    implementation.

    The caller is responsible for:
    1. Connecting `dispatch_tracker.feed_event` into the IntentService's
       `on_event` chain (so terminal sub-mission events reach the
       tracker; other events still flow to the GUI).
    2. Calling `daemon.stop()` will also need to set `daemon_stop_event`
       — the daemon's own `_stop_event` is the real signal; this
       parameter is the same Event passed in so the dispatch wait
       loop can exit promptly on user-stop.

    `planner_specialization` (v0.5.1a2) routes each sub-mission's
    root planner node to a specific specialist. None falls through
    to `IntentService`'s default (`PLAN`). The autonomous-session
    helper passes `PLAN_DEEP` unconditionally as of v0.5.4a1; the
    previous per-tier routing (`PLANNER_BY_TIER`) was a footgun for
    new models that defaulted to PLAN without anyone noticing.
    """

    def dispatch_item(item: RoadmapItem) -> str:
        # Build the sub-mission's intent text from the roadmap item.
        # The daemon's design wraps each iteration in a Phase-1
        # mission, so the text the planner sees is ONE goal — this
        # item's title + description. The planner then decomposes
        # as usual.
        goal_text = item.title.strip()
        if item.description.strip():
            goal_text = f"{goal_text}\n\n{item.description.strip()}"

        # v0.6.3a2 — wire the skill loader into the runtime. This
        # closes the READ side of the self-improvement loop: skills
        # extracted from past missions get surfaced into THIS iter's
        # planner context. Best-effort — a skill-lookup failure must
        # never break mission dispatch.
        skill_ids: list[str] = []
        if enable_skill_loader:
            try:
                from ..orchestration.skill_loader import build_skill_context
                from ..orchestration.skills import mark_skill_surfaced
                sctx = build_skill_context(goal_text, project_path=project_path)
                if sctx.block:
                    goal_text = f"{goal_text}\n\n{sctx.block}"
                    skill_ids = sctx.skill_ids
                    # Touch last_used_at so the curator's 90-day
                    # staleness sweep doesn't rot skills that ARE
                    # being surfaced every mission. Not a quality
                    # signal — counts are untouched.
                    for ls in sctx.loaded:
                        try:
                            mark_skill_surfaced(
                                ls.skill,
                                project_path=(
                                    project_path
                                    if ls.skill.scope == "project" else None
                                ),
                            )
                        except Exception:
                            logger.debug(
                                "mark_skill_surfaced raised for %s",
                                ls.skill.id, exc_info=True,
                            )
            except Exception:
                logger.warning(
                    "skill loader wiring raised; dispatching without skills",
                    exc_info=True,
                )

        intent_id = intent_service.start_intent(
            goal_text,
            planner_specialization=planner_specialization,
        )
        # Watch BEFORE we hand the handle back to the daemon, so a
        # very-fast intent that completes between dispatch and wait
        # doesn't lose its terminal event.
        dispatch_tracker.watch(intent_id)

        # v0.6.3a2 — telemetry: surface which skills fed this iter so
        # the GUI can show an iter-card chip ("referenced N skills").
        if skill_ids and on_session_event is not None:
            try:
                on_session_event({
                    "event": "skill_context_loaded",
                    "intent_id": intent_id,
                    "item_id": item.id,
                    "skill_ids": skill_ids,
                    "skill_count": len(skill_ids),
                })
            except Exception:
                logger.debug(
                    "on_session_event raised for skill_context_loaded",
                    exc_info=True,
                )

        return intent_id

    def wait_for_dispatch(handle: str) -> DispatchOutcome:
        outcome = dispatch_tracker.wait(
            handle, stop_event=daemon_stop_event, poll_seconds=0.5,
        )
        # Tracker memory grows with iteration count; clean up.
        dispatch_tracker.forget(handle)
        return outcome

    def cancel_dispatch(handle: str) -> None:
        try:
            intent_service.cancel(handle)
        except Exception:
            logger.debug("intent_service.cancel raised", exc_info=True)

    # v0.6.1a1 — wire skill-extraction + curator into the daemon's
    # optional hooks. Both fire in background threads so the daemon's
    # main loop (and its terminal-event sequence) isn't blocked while
    # the model session for extraction OR the deterministic curator
    # pass run.
    extract_skill_hook = None
    if enable_skill_extraction:
        def _extract_skill(**kwargs: Any) -> None:
            # Look up the extractor by module attribute at call time
            # so test patches via `patch("...skill_mission_extraction.
            # extract_skill_from_iter", ...)` actually take effect.
            # Closure-captured imports would be locked to the original
            # binding and immune to the patch.
            from ..orchestration import skill_mission_extraction as sme
            try:
                ctx = sme.IterContext(**kwargs)
            except TypeError:
                logger.warning(
                    "extract_skill_hook called with unexpected kwargs %r; "
                    "skipping extraction (signature drift?)",
                    sorted(kwargs.keys()),
                )
                return
            # Run in a daemon thread so a slow extractor doesn't pin
            # the iter loop. Best-effort: extract_skill_from_iter
            # already swallows its own exceptions.
            t = threading.Thread(
                target=sme.extract_skill_from_iter,
                args=(ctx,),
                kwargs={"backend": backend},
                daemon=True,
                name=f"skill-extractor-{ctx.intent_id[:8]}-{ctx.iter_count}",
            )
            t.start()

        extract_skill_hook = _extract_skill

    queue_curation_hook = None
    if enable_skill_curator:
        def _queue_curation(curation_project_path: str) -> None:
            # Same attribute-access pattern as the extractor — keeps
            # test patches honest and runtime behavior identical.
            from ..orchestration import skill_curator as sc
            try:
                if not sc.should_run_curation(curation_project_path):
                    logger.debug(
                        "skill curator skipped — within rate limit"
                    )
                    return
            except Exception:
                logger.debug("should_run_curation raised", exc_info=True)
                return

            t = threading.Thread(
                target=sc.run_curation,
                args=(curation_project_path,),
                daemon=True,
                name=f"skill-curator-{Path(curation_project_path).name}",
            )
            t.start()

        queue_curation_hook = _queue_curation

    checkpoint_hook = None
    try:
        from ..orchestration.checkpoints import IterationCheckpointStore
        checkpoint_store = IterationCheckpointStore(project_path)
        checkpoint_hook = checkpoint_store.create
    except Exception:
        logger.debug(
            "iteration checkpoints unavailable for %s", project_path,
            exc_info=True,
        )

    return DaemonHooks(
        dispatch_item=dispatch_item,
        wait_for_dispatch=wait_for_dispatch,
        cancel_dispatch=cancel_dispatch,
        get_commit_sha=make_git_get_commit_sha(project_path),
        validate_sha=make_git_validate_sha(project_path),
        run_full_reflect=make_reflect_runner(
            backend=backend,
            project_path=project_path,
            project_instructions=project_instructions,
            settings=settings,
            roadmap_path=roadmap_path,
            cancel_event=daemon_stop_event,
            on_session_event=on_session_event,
            specialist_backend_resolver=specialist_backend_resolver,
            mcp_manager=mcp_manager,
        ),
        check_context_factory=make_check_context_factory(
            project_path=project_path,
            settings=settings,
            image_provider=image_provider,
        ),
        extract_skill_hook=extract_skill_hook,
        queue_curation_hook=queue_curation_hook,
        checkpoint_hook=checkpoint_hook,
    )
