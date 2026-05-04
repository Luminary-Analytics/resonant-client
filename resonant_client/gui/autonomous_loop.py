"""
AutonomousMissionDaemon — the outer loop for a Phase 2 Autonomous Mission.

Each daemon instance owns one in-flight autonomous mission (one
roadmap, one intent_id). It runs a background thread that picks
unchecked roadmap items, dispatches each as a Phase-1 sub-mission,
marks the item done with its commit SHA when the sub-mission ships,
and runs the REFLECT pass every K iterations. It stops when:

  1. The user clicked Stop                       → "user_stop"
  2. Wall-clock time budget elapsed              → "time_budget_exhausted"
  3. MAX_ITERATIONS=100 hit (defensive backstop) → "iteration_cap"
  4. Full-reflect verdict was "satisfied"        → "satisfied"
  5. Full-reflect verdict was "blocked" enough   → "blocked"
  6. Consecutive sub-missions failed             → "check_failed"
  7. Roadmap misconfigured (no criteria at all)  → "misconfigured"

Stopping rules are checked in priority order at the top of every
iteration AND between phases of the same iteration where appropriate.

## Architecture: dependency injection for testability

The daemon does NOT directly call `IntentService.start_intent`,
shell out to `git`, or invoke the REFLECT model session. All of
those happen via callables stashed in a `DaemonHooks` dataclass,
injected at construction time. This is what lets the entire
iteration loop be unit-tested with stubs that complete in
microseconds — no real subprocess, no real LLM, no real WebSocket.

Production wiring (a6) builds the hooks from a live `IntentService`,
the project's git repo, and the engine's session machinery. Tests
build hooks from lambdas.

## What this module does NOT own

- Resume-from-restart: that's an AppState concern. The daemon's
  state (iter_count, started_at) is reconstructable from the
  on-disk roadmap's iteration log + a recorded started-at
  timestamp. a6 will own the resume code.
- WS event format: the daemon emits dicts via its `on_event`
  callback. a6 wraps those into the WS protocol.
- Chat session integration: when the daemon's dispatched
  sub-missions emit engine events, the wiring that gets those
  to the right chat session is a6's job.
- Roadmap-item bookkeeping via the model: contrary to the
  design-doc §7 split (where REFLECT does both item-mark and
  full-reflect modes), this implementation runs item-mark
  PURELY in the daemon — no model session. The daemon already
  knows which item it dispatched and can read the commit SHA
  via `git log -1`; spending a model dispatch on bookkeeping
  would burn tokens for no gain. The model session only fires
  in full-reflect mode (every K iters), and only handles
  [chrome] criteria + verdict + added/blocked items. See
  ADR 9 in the implementation guide.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from ..gui import roadmap as roadmap_module
from ..gui.roadmap import Roadmap, RoadmapItem
from ..orchestration.acceptance_check import CheckContext
from ..orchestration.reflect import ReflectPassResult, run_reflect_pass

logger = logging.getLogger(__name__)


# ── Dispatch & reflect outcome types ────────────────────────────────────


@dataclass
class DispatchOutcome:
    """Result of one Phase-1 sub-mission dispatched by the daemon.

    `success=True` means the sub-mission completed without crashing —
    NOT that the work itself was correct. The daemon doesn't try to
    judge correctness here; that's the next iteration's REFLECT pass.
    """
    success: bool
    error: str = ""
    handle: Any = None  # whatever dispatch_item returned (intent_id usually)


@dataclass
class FullReflectOutcome:
    """Result of one full REFLECT pass — deterministic prelude PLUS,
    if needed, the agentic model session.

    `pass_result` is from `run_reflect_pass` (always populated).
    `verdict` is the mechanically-cross-checked final verdict. The
    other fields are the parsed JSON envelope from the model session
    (empty when `needs_model_session() == False`).
    """
    pass_result: ReflectPassResult
    verdict: str = "continue"   # "continue" | "satisfied" | "blocked"
    chrome_results: list[dict] = field(default_factory=list)
    added_items: list[dict] = field(default_factory=list)
    blocked_items: list[dict] = field(default_factory=list)
    manual_pending: list[str] = field(default_factory=list)
    summary: str = ""
    estimated_remaining_minutes: int = 0
    error: str = ""


# ── Hooks (the I/O the daemon needs, injected) ──────────────────────────


@dataclass
class DaemonHooks:
    """Everything the daemon depends on the outside world for.

    Tests pass simple callables; production wraps `IntentService`,
    `subprocess.run('git ...')`, the REFLECT model session, etc.

    `dispatch_item(item)` → opaque handle (intent_id in production).
        Kicks off a Phase-1 sub-mission for the given roadmap item.
        Returns immediately; the daemon then calls
        `wait_for_dispatch(handle)` to block until done.

    `wait_for_dispatch(handle)` → DispatchOutcome.
        Blocks until the handle's sub-mission terminates (completed /
        cancelled / failed). The daemon's stop_event MUST be checkable
        from inside this callable so user_stop interrupts cleanly.

    `cancel_dispatch(handle)` → None.
        Best-effort cancellation. Called by `daemon.stop()` if
        a dispatch is in flight. Safe to be a no-op.

    `get_commit_sha()` → Optional[str].
        Returns the latest HEAD commit SHA, or None if the project
        repo is in a state we can't read (no commits yet, detached
        HEAD with no recorded SHA, git binary missing). Production
        wraps `git log -1 --format=%H`.

    `validate_sha(sha)` → bool.
        True iff `sha` is a real commit in the repo. Production wraps
        `git rev-parse --verify <sha>^{commit}`.

    `run_full_reflect(roadmap, pass_result)` → FullReflectOutcome.
        Run the REFLECT model session in `mode: full`. The
        deterministic prelude (`run_reflect_pass`) has already been
        called by the daemon — its output is `pass_result`, and the
        roadmap's `acceptance_criteria[i].passed` fields already
        reflect those results. The model's job is to validate
        [chrome] criteria + emit the structured JSON verdict.
        IMPORTANT: this callable can be skipped entirely when
        `pass_result.needs_model_session() == False` — the daemon
        decides.

    `check_context_factory(roadmap)` → CheckContext.
        Build a CheckContext for the deterministic pass, with
        bash_runner, vision_runner, image_provider all set up for
        the project's environment. Called once per full-reflect
        pass so the runners can pick up any config drift.
    """
    dispatch_item: Callable[[RoadmapItem], Any]
    wait_for_dispatch: Callable[[Any], DispatchOutcome]
    cancel_dispatch: Callable[[Any], None]
    get_commit_sha: Callable[[], Optional[str]]
    validate_sha: Callable[[str], bool]
    run_full_reflect: Callable[[Roadmap, ReflectPassResult], FullReflectOutcome]
    check_context_factory: Callable[[Roadmap], CheckContext]


# ── Configuration ───────────────────────────────────────────────────────


@dataclass
class AutonomousMissionConfig:
    """Per-mission configuration for one daemon instance."""
    intent_id: str
    roadmap_path: Path
    # None means full-auto (no time ceiling; iteration cap still applies).
    time_budget_seconds: Optional[float] = None
    # Defensive backstop. A user who legitimately needs >100 iterations
    # should run a follow-up mission against the same project.
    max_iterations: int = 100
    # Run a full REFLECT pass every K iterations (and on roadmap empty).
    full_reflect_cadence: int = 3
    # Sleep between iterations so the user can interject and so we don't
    # pin a CPU core if dispatch returns instantly. Cancellable via the
    # stop_event.
    tick_pause_seconds: float = 5.0
    # If we get this many consecutive `verdict=blocked` from full
    # reflect, we stop and let the user untangle it.
    blocked_streak_limit: int = 3
    # If this many consecutive sub-missions fail (DispatchOutcome.success
    # = False), we stop. Don't grind on something fundamentally broken.
    check_failed_streak_limit: int = 2


# ── The daemon ──────────────────────────────────────────────────────────


# Stop reason → kind of WS event the daemon emits at end-of-loop.
# "satisfied" is the only "complete" ending; everything else is "paused".
_COMPLETE_REASONS = frozenset({"satisfied"})


class AutonomousMissionDaemon:
    """Background-thread orchestrator for one autonomous mission.

    Lifecycle: construct → `start()` → events flow via on_event →
    `stop(reason)` (or natural termination) → `join()`.

    Idempotent: `start()` while already running is a no-op. `stop()`
    is safe to call multiple times.

    Thread-safe: `stop()` and `is_running()` may be called from any
    thread. Internal state mutations are guarded by `_lock`.
    """

    def __init__(
        self,
        config: AutonomousMissionConfig,
        hooks: DaemonHooks,
        on_event: Optional[Callable[[dict], None]] = None,
    ):
        self.config = config
        self.hooks = hooks
        self.on_event = on_event or (lambda ev: None)

        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._stop_reason: str = ""
        self._stop_message: str = ""

        # Iteration state (read by tests + emitted in events).
        self._iter_count = 0
        self._started_at: float = 0.0
        self._blocked_streak = 0
        self._check_failed_streak = 0
        self._verdict: str = "continue"
        self._in_flight_handle: Any = None

    # ── Public API ────────────────────────────────────────────────

    def start(self) -> None:
        """Spawn the background thread. Idempotent — calling again
        while running is a no-op."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run,
            name=f"autonomous-{self.config.intent_id[:8]}",
            daemon=True,
        )
        self._thread.start()

    def stop(self, reason: str = "user_stop", message: str = "") -> None:
        """Signal the daemon to wind down at the next safe point.

        Cancels any in-flight dispatch via `cancel_dispatch`. The
        cancelled sub-mission's tool calls finish naturally — we
        don't kill them mid-execution. The daemon thread exits
        cleanly after the current iteration's bookkeeping.

        Safe to call from any thread; safe to call multiple times.
        """
        with self._lock:
            # First-call wins on reason — subsequent stops keep the
            # original explanation. (e.g. budget expires AND user
            # clicks stop within the same tick: budget reason is
            # the "real" cause.)
            if not self._stop_reason:
                self._stop_reason = reason
                self._stop_message = message
            in_flight = self._in_flight_handle
        self._stop_event.set()

        if in_flight is not None:
            try:
                self.hooks.cancel_dispatch(in_flight)
            except Exception:
                logger.debug(
                    "cancel_dispatch raised; ignoring",
                    exc_info=True,
                )

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def join(self, timeout: Optional[float] = None) -> None:
        if self._thread is not None:
            self._thread.join(timeout)

    def state_snapshot(self) -> dict:
        """Read-only view of the daemon's current state — useful for
        WS event payloads and tests. Safe to call from any thread."""
        with self._lock:
            elapsed = (
                time.time() - self._started_at
                if self._started_at else 0.0
            )
            return {
                "intent_id": self.config.intent_id,
                "iter_count": self._iter_count,
                "elapsed_seconds": elapsed,
                "time_budget_seconds": self.config.time_budget_seconds,
                "verdict": self._verdict,
                "blocked_streak": self._blocked_streak,
                "check_failed_streak": self._check_failed_streak,
                "is_running": self.is_running(),
                "stop_reason": self._stop_reason,
            }

    # ── Iteration loop (runs in the daemon thread) ────────────────

    def _run(self) -> None:
        """Thread entrypoint. Iterates until a stopping rule fires
        or an unhandled exception bubbles up (which is logged and
        emitted as `autonomous_mission_failed` so the user isn't
        left wondering)."""
        self._started_at = time.time()
        self._emit("autonomous_mission_started", {
            "started_iso": _now_iso(),
            "time_budget_seconds": self.config.time_budget_seconds,
            "max_iterations": self.config.max_iterations,
        })

        try:
            while True:
                # Stopping rules at top of iteration. These are
                # priority-ordered: the most severe (user_stop)
                # wins.
                stop = self._check_stop_rules()
                if stop is not None:
                    self._emit_stop(*stop)
                    return

                rm = self._load_roadmap()

                # Misconfiguration: no acceptance criteria means
                # the rigorous grill failed silently. Stop loud.
                if not rm.has_any_acceptance_criteria():
                    self._emit_stop(
                        "misconfigured",
                        "roadmap has no acceptance criteria; "
                        "rigorous grill must produce ≥4",
                    )
                    return

                # Pick the next item. None means roadmap is empty
                # (or fully checked); trigger a full reflect to see
                # if we've converged or if REFLECT wants to add
                # items.
                item = rm.next_unchecked_item()
                if item is None:
                    full = self._run_full_reflect(rm)
                    if full.verdict == "satisfied":
                        self._emit_stop(
                            "satisfied",
                            "all roadmap items shipped + acceptance criteria met",
                        )
                        return
                    if full.verdict == "blocked":
                        self._blocked_streak += 1
                        if self._blocked_streak >= self.config.blocked_streak_limit:
                            self._emit_stop(
                                "blocked",
                                f"{self._blocked_streak} consecutive blocked verdicts",
                            )
                            return
                    else:
                        self._blocked_streak = 0
                        # `continue` verdict with no new items + an
                        # empty roadmap means we're stuck: no work to
                        # do, criteria still red, and nobody is
                        # adding items.
                        #
                        # v0.5.0 GA prep — re-load the roadmap from
                        # disk and check `next_unchecked_item` rather
                        # than trusting `full.added_items`. The
                        # REFLECT model can add items two ways:
                        # (1) via the JSON envelope's `added` field
                        # (the daemon's add_items loop applies these
                        # to the roadmap), and (2) via direct
                        # `file_edit` to roadmap.md. Path 2 won't
                        # show up in `outcome.added_items` but WILL
                        # appear in the roadmap on disk. Found in
                        # v0.5.0 GA smoke run #3 — model used
                        # file_edit directly and the daemon then
                        # mis-detected stuck. ADR 15 in the impl
                        # guide.
                        rm_post = self._load_roadmap()
                        if rm_post.next_unchecked_item() is None:
                            self._emit_stop(
                                "stuck",
                                "roadmap empty but acceptance criteria "
                                "not converged; no new items added by "
                                "REFLECT — manual intervention needed",
                            )
                            return
                    # Otherwise: model added items. Loop and re-pick.
                    self._tick_pause_or_stop()
                    continue

                # Dispatch as a Phase-1 sub-mission and wait for it
                # to finish. The daemon thread blocks here — but
                # the engine's existing event stream still flows
                # to the GUI via the IntentService callbacks.
                if not self._run_one_iteration(item):
                    if self._check_failed_streak >= self.config.check_failed_streak_limit:
                        self._emit_stop(
                            "check_failed",
                            f"{self._check_failed_streak} consecutive failed iterations",
                        )
                        return

                # Full reflect every K iterations.
                if self._iter_count % self.config.full_reflect_cadence == 0:
                    rm = self._load_roadmap()
                    full = self._run_full_reflect(rm)
                    if full.verdict == "satisfied":
                        self._emit_stop(
                            "satisfied",
                            "acceptance criteria converged",
                        )
                        return
                    if full.verdict == "blocked":
                        self._blocked_streak += 1
                        if self._blocked_streak >= self.config.blocked_streak_limit:
                            self._emit_stop(
                                "blocked",
                                f"{self._blocked_streak} consecutive blocked verdicts",
                            )
                            return
                    else:
                        self._blocked_streak = 0

                self._tick_pause_or_stop()

        except Exception as exc:
            logger.exception(
                "Autonomous mission daemon crashed for intent %s",
                self.config.intent_id,
            )
            # v0.5.6a3 — same atomicity guarantee as _emit_stop: write
            # roadmap.md status + emit the terminal event with new_phase
            # so the WS handler can update session state in lock-step.
            self._update_roadmap_status_safely("failed")
            self._emit("autonomous_mission_failed", {
                "iter_count": self._iter_count,
                "error": str(exc),
                "elapsed_seconds": time.time() - self._started_at,
                "new_phase": "autonomous_failed",
            })

    # ── Stop-rule evaluation ──────────────────────────────────────

    def _check_stop_rules(self) -> Optional[tuple[str, str]]:
        """Evaluate the priority-ordered stopping rules. Returns
        `(reason, message)` to stop, or None to continue. Called at
        the top of every iteration."""
        # 1. User stop (highest priority).
        if self._stop_event.is_set():
            with self._lock:
                reason = self._stop_reason or "user_stop"
                message = self._stop_message or "stop requested"
            return (reason, message)

        # 2. Time budget. None means full-auto (skip this rule).
        if self.config.time_budget_seconds is not None:
            elapsed = time.time() - self._started_at
            if elapsed >= self.config.time_budget_seconds:
                return (
                    "time_budget_exhausted",
                    f"elapsed {elapsed:.1f}s ≥ budget "
                    f"{self.config.time_budget_seconds:.1f}s",
                )

        # 3. Iteration cap (defensive backstop, always applies).
        if self._iter_count >= self.config.max_iterations:
            return (
                "iteration_cap",
                f"{self._iter_count} iterations ≥ cap "
                f"{self.config.max_iterations}",
            )

        return None

    # ── One iteration: dispatch + mark item ───────────────────────

    def _run_one_iteration(self, item: RoadmapItem) -> bool:
        """Dispatch one roadmap item as a sub-mission, wait for it,
        mark it complete in the roadmap with its commit SHA.

        Returns True if the iteration succeeded (item shipped),
        False if it failed or was cancelled. Updates
        `_check_failed_streak` accordingly.
        """
        self._iter_count += 1
        started = time.time()

        self._emit("autonomous_iteration_started", {
            "iter_count": self._iter_count,
            "item_id": item.id,
            "item_title": item.title,
        })

        try:
            handle = self.hooks.dispatch_item(item)
        except Exception as exc:
            logger.exception("dispatch_item raised for %s", item.id)
            self._check_failed_streak += 1
            self._emit("autonomous_iteration_failed", {
                "iter_count": self._iter_count,
                "item_id": item.id,
                "error": f"dispatch raised: {exc}",
            })
            return False

        with self._lock:
            self._in_flight_handle = handle

        try:
            outcome = self.hooks.wait_for_dispatch(handle)
        except Exception as exc:
            logger.exception("wait_for_dispatch raised for %s", item.id)
            outcome = DispatchOutcome(
                success=False,
                error=f"wait raised: {exc}",
                handle=handle,
            )
        finally:
            with self._lock:
                self._in_flight_handle = None

        if not outcome.success:
            self._check_failed_streak += 1
            self._emit("autonomous_iteration_failed", {
                "iter_count": self._iter_count,
                "item_id": item.id,
                "error": outcome.error or "(no error message)",
            })
            return False

        # Sub-mission shipped. Reset the failure streak.
        self._check_failed_streak = 0

        duration = time.time() - started
        sha = self._read_and_validate_sha()

        # Re-read the roadmap from disk in case the user (or REFLECT
        # in a parallel pass — though we serialize so that shouldn't
        # happen) edited it during the iteration.
        rm = self._load_roadmap()
        # When SHA is None / invalid, write empty commit_sha + a note
        # marker. The roadmap parser only recognizes 6-40-hex SHAs, so
        # passing a "<empty>" placeholder wouldn't survive a round-trip
        # through save/load — the iteration_log carries the explanatory
        # `<empty>` marker instead.
        item_note = (
            f"iter {self._iter_count}"
            if sha
            else f"iter {self._iter_count} (no commit recorded)"
        )
        marked = roadmap_module.mark_item_complete(
            rm,
            item_id=item.id,
            commit_sha=sha or "",
            note=item_note,
        )
        if not marked:
            # User hand-edited the item out mid-run. Don't crash;
            # log + continue.
            logger.info(
                "Item %s vanished from roadmap during iteration; "
                "user must have edited the file. Skipping mark.",
                item.id,
            )

        roadmap_module.append_iteration_log(
            rm,
            iter_num=self._iter_count,
            duration_label=_format_duration(duration),
            note=(
                f"shipped {item.id}"
                if sha
                else f"shipped {item.id} <empty>"
            ),
            item_id=item.id,
            commit_sha=sha or "",
            kind="shipped",
        )
        try:
            roadmap_module.save(rm, self.config.roadmap_path)
        except Exception:
            logger.warning(
                "Failed to persist roadmap after iter %s",
                self._iter_count,
                exc_info=True,
            )

        self._emit("autonomous_iteration_complete", {
            "iter_count": self._iter_count,
            "item_id": item.id,
            "commit_sha": sha or "",
            "duration_seconds": duration,
        })
        return True

    def _read_and_validate_sha(self) -> Optional[str]:
        """Read latest HEAD SHA via the hook, validate it via
        `git rev-parse`. Returns the SHA on success, None when the
        repo is in a weird state OR the SHA fails validation (which
        shouldn't happen for a SHA we just read from `git log`, but
        the validate hook is the same one we'd use to check
        model-claimed SHAs in full reflect).
        """
        try:
            sha = self.hooks.get_commit_sha()
        except Exception:
            logger.debug("get_commit_sha raised", exc_info=True)
            return None
        if not sha:
            return None
        try:
            if not self.hooks.validate_sha(sha):
                logger.warning(
                    "validate_sha rejected SHA %r read from git log; "
                    "marking as <empty>",
                    sha,
                )
                return None
        except Exception:
            logger.debug("validate_sha raised", exc_info=True)
            return None
        return sha

    # ── Full reflect pass ─────────────────────────────────────────

    def _run_full_reflect(self, rm: Roadmap) -> FullReflectOutcome:
        """Run the deterministic prelude, optionally dispatch the
        REFLECT model session, cross-check the verdict against the
        roadmap state, and emit the autonomous_reflection event.

        The cross-check is the "convergence is real, not a model
        mood" enforcement: if the model claims `satisfied` while
        `roadmap.is_converged()` is False, we override to
        `continue`. The model can't fake convergence; the runtime
        is the source of truth.
        """
        # Deterministic prelude.
        try:
            ctx = self.hooks.check_context_factory(rm)
        except Exception as exc:
            logger.exception("check_context_factory raised")
            ctx = CheckContext()  # degraded fallback
        pass_result = run_reflect_pass(rm, ctx)

        # Persist the deterministic results so the user can read the
        # roadmap on disk and see what's been validated.
        try:
            roadmap_module.save(rm, self.config.roadmap_path)
        except Exception:
            logger.warning(
                "Failed to persist roadmap after reflect pass",
                exc_info=True,
            )

        # If everything's settled deterministically, skip the model
        # session entirely. Cost optimization for pure-bash specs.
        if not pass_result.needs_model_session():
            verdict = "satisfied" if pass_result.converged else "continue"
            outcome = FullReflectOutcome(
                pass_result=pass_result,
                verdict=verdict,
                manual_pending=[c.text for c in pass_result.manual_pending],
                summary=(
                    f"Deterministic pass: "
                    f"{pass_result.bash_passed + pass_result.vision_passed} passed, "
                    f"{pass_result.bash_failed + pass_result.vision_failed} failed"
                ),
            )
        else:
            # Hand off to the REFLECT model session for [chrome]
            # criteria validation + verdict + added/blocked items.
            try:
                outcome = self.hooks.run_full_reflect(rm, pass_result)
            except Exception as exc:
                logger.exception("run_full_reflect hook raised")
                outcome = FullReflectOutcome(
                    pass_result=pass_result,
                    verdict="continue",
                    error=f"reflect hook raised: {exc}",
                    summary="REFLECT model session failed; continuing",
                )

        # Cross-check: if the model claimed `satisfied`, the roadmap
        # had better agree. Re-load from disk because the model may
        # have written checkbox flips via file_edit.
        rm_after = self._load_roadmap()

        # v0.5.0 GA prep — apply `added` items from REFLECT's JSON
        # verdict to the roadmap. The model can also write items via
        # `file_edit` directly; we don't preempt that, but if the
        # JSON envelope lists items the model didn't actually edit
        # in (the common case), we add them here so the next
        # iteration has work to do. Without this, REFLECT's add-
        # items signal got lost and the daemon loop stuck on iter 1
        # — found in the v0.5.0 GA smoke. ADR 14 in the impl guide.
        added_count = 0
        for item_dict in outcome.added_items or []:
            if not isinstance(item_dict, dict):
                continue
            tier = item_dict.get("tier") or 1
            try:
                tier = int(tier)
            except (TypeError, ValueError):
                tier = 1
            title = (item_dict.get("title") or "").strip()
            description = (item_dict.get("description") or "").strip()
            if not title:
                continue
            try:
                roadmap_module.add_item(
                    rm_after,
                    tier=tier,
                    title=title,
                    description=description,
                    source_iter=self._iter_count,
                )
                added_count += 1
            except Exception:
                logger.debug(
                    "add_item raised for added entry %r",
                    item_dict, exc_info=True,
                )
        if added_count:
            try:
                roadmap_module.save(rm_after, self.config.roadmap_path)
            except Exception:
                logger.warning(
                    "Failed to persist roadmap after applying %d added items",
                    added_count, exc_info=True,
                )
            logger.info(
                "REFLECT added %d follow-up items to the roadmap "
                "for iter %s+", added_count, self._iter_count + 1,
            )

        if outcome.verdict == "satisfied" and not rm_after.is_converged():
            logger.warning(
                "REFLECT verdict=satisfied but roadmap.is_converged()=False; "
                "overriding to continue. (This usually means the model "
                "mis-judged a chrome criterion.)"
            )
            outcome.verdict = "continue"
            outcome.summary += (
                "  [Daemon override: model claimed satisfied but at least "
                "one acceptance criterion is not yet passed.]"
            )

        # Track verdict for state_snapshot.
        with self._lock:
            self._verdict = outcome.verdict

        passed_count, total_count = rm_after.acceptance_summary()
        self._emit("autonomous_reflection", {
            "iter_count": self._iter_count,
            "verdict": outcome.verdict,
            "added": outcome.added_items,
            "blocked": outcome.blocked_items,
            "manual_pending": outcome.manual_pending,
            "summary": outcome.summary,
            "estimated_remaining_minutes": outcome.estimated_remaining_minutes,
            "acceptance_summary": {
                "passed": passed_count,
                "total": total_count,
            },
            "pass_tally": {
                "bash_passed": pass_result.bash_passed,
                "bash_failed": pass_result.bash_failed,
                "bash_errored": pass_result.bash_errored,
                "vision_passed": pass_result.vision_passed,
                "vision_failed": pass_result.vision_failed,
                "vision_errored": pass_result.vision_errored,
                "chrome_pending": len(pass_result.chrome_pending),
                "manual_pending": len(pass_result.manual_pending),
            },
            "error": outcome.error,
        })

        return outcome

    # ── Internal helpers ──────────────────────────────────────────

    def _load_roadmap(self) -> Roadmap:
        """Load the roadmap from disk. Wrapped so we can swap in
        caching later if the I/O proves expensive (it shouldn't —
        roadmaps are KB-sized markdown files)."""
        return roadmap_module.load(self.config.roadmap_path)

    def _tick_pause_or_stop(self) -> None:
        """Sleep `tick_pause_seconds`, but wake immediately if
        `stop()` is called. The daemon's inner loop calls this at
        the bottom of each iteration so user_stop interrupts within
        ~5s rather than waiting for the next iteration to begin."""
        # `Event.wait(timeout)` returns True if the event was set
        # before the timeout expired. We don't act on it here —
        # the next iteration's `_check_stop_rules` will pick it up.
        # We just want the wait to be cancellable.
        self._stop_event.wait(self.config.tick_pause_seconds)

    def _emit(self, kind: str, payload: dict) -> None:
        """Dispatch an event to the on_event callback. Swallows
        callback errors so a buggy WS handler can't crash the
        daemon thread."""
        try:
            self.on_event({
                "event": kind,
                "intent_id": self.config.intent_id,
                **payload,
            })
        except Exception:
            logger.debug("on_event raised; swallowing", exc_info=True)

    def _emit_stop(self, reason: str, message: str) -> None:
        """Final event emission before the loop exits. Picks
        `mission_complete` for `satisfied`, `mission_paused` for
        everything else.

        v0.5.6a3 — also updates the on-disk roadmap status to match
        the terminal state BEFORE emitting the WS event. Without this
        the GUI's autonomous badge clears (response to the WS event)
        but the roadmap.md keeps `**Status:** running`. After app
        restart the orphan-detection scanner sees a "running" mission
        with no live daemon and offers to resume — which is wrong for
        a stuck/satisfied mission. Linux-bridge field-observation #6.
        """
        with self._lock:
            self._stop_reason = self._stop_reason or reason
            self._stop_message = self._stop_message or message
        is_complete = reason in _COMPLETE_REASONS
        new_status = "complete" if is_complete else "paused"
        self._update_roadmap_status_safely(new_status)
        kind = (
            "autonomous_mission_complete"
            if is_complete
            else "autonomous_mission_paused"
        )
        # v0.5.6a3 — include `new_phase` in the payload so the WS
        # handler in app.py can update session.mission_state.phase
        # atomically with the badge transition. Without this the
        # session record stays in `autonomous_running` forever.
        new_phase = (
            "autonomous_complete" if is_complete else "autonomous_paused"
        )
        self._emit(kind, {
            "iter_count": self._iter_count,
            "stop_reason": reason,
            "stop_message": message,
            "elapsed_seconds": time.time() - self._started_at,
            "final_verdict": self._verdict,
            "new_phase": new_phase,
        })

    def _update_roadmap_status_safely(self, new_status: str) -> None:
        """v0.5.6a3 — load the roadmap, set its status field, persist.
        Best-effort: a write failure here doesn't block the daemon's
        terminal event (the GUI badge update happens regardless), but
        does log loudly so the orphan-detection drift is debuggable.
        """
        try:
            rm = self._load_roadmap()
            if rm.status == new_status:
                return  # idempotent — already at target state
            rm.status = new_status
            roadmap_module.save(rm, self.config.roadmap_path)
        except Exception:
            logger.exception(
                "Failed to update roadmap status to %r for intent %s; "
                "GUI/disk state will diverge until next save",
                new_status, self.config.intent_id,
            )


# ── Helpers ─────────────────────────────────────────────────────────────


def _format_duration(seconds: float) -> str:
    """Compact human-readable duration for the iteration log."""
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        return f"{int(seconds // 60)}m"
    return f"{seconds / 3600:.1f}h"


def _now_iso() -> str:
    """UTC ISO 8601 timestamp suitable for the roadmap header and
    iteration-log entries."""
    return datetime.now(timezone.utc).isoformat()
