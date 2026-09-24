"""
v0.5.4a2 — `FlakyPlannerBackend` wrapper for live walker-retry testing.

The walker auto-retry path (v0.5.1a3) was added defensively to handle
malformed planner output, but in 5+ smoke runs across v0.5.0-v0.5.3
the path NEVER fired: every planner emitted parseable subgoals on the
first try. That's good news for the planner prompt, but bad news for
the retry path — we have no live evidence it actually works end-to-end
when invoked.

This wrapper makes that test cheap. It wraps any backend and intercepts
planner calls (detected by sniffing the system prompt for the PLAN /
PLAN_DEEP signature). On the Nth intercepted call, it emits a malformed
stream — text that contains zero parseable subgoals — forcing the
walker into its retry path. Subsequent calls forward normally.

Use via the smoke CLI:
    resonant-smoke run --spec minimal --model pro --inject-planner-failure

A converged mission proves the retry recovered. A non-converged one
points at a real bug in the retry plumbing.

The wrapper is thread-safe (the call counter uses a lock) so concurrent
sub-missions don't double-count.
"""
from __future__ import annotations

import logging
import threading
from typing import Any, Iterator, Tuple

logger = logging.getLogger(__name__)


# Substrings that uniquely identify a planner system prompt. Both PLAN
# and PLAN_DEEP put the role identifier near the top in CAPS; sniffing
# for either covers both. Keep this conservative — false positives
# (intercepting a non-planner call) would corrupt the smoke result.
_PLANNER_PROMPT_SIGNATURES: tuple[str, ...] = (
    "You are a PLANNER",
    "You are a DEEP PLANNER",
)


# Canned malformed response. Deliberately chosen to:
# 1. Not contain a `{"subgoals"` envelope — guarantees `subgoals=[]`
#    after the runner parses, which is what triggers the retry.
# 2. Look like something a real model COULD emit when confused (a brief
#    natural-language summary instead of structured output) — close to
#    the failure mode the retry was actually designed for.
_MALFORMED_PLANNER_RESPONSE = (
    "I should examine the codebase before decomposing this goal. "
    "Let me start by reading the existing files to understand the "
    "current state of the project."
)


class FlakyPlannerBackend:
    """Wraps a real backend; intercepts the first N planner calls.

    Designed for the smoke harness — production code should never use
    this. The point is to PROVE the walker retry path works by forcing
    it to fire.

    Parameters:
    - `inner`: the backend to delegate to (most calls pass through)
    - `fail_first_n_planner_calls`: how many planner calls to corrupt.
      Default 1 matches the walker's `max_planner_retries=1` default —
      one intercepted call → one retry → second call should succeed.

    All non-planner calls (IMPLEMENT, VERIFY, REFLECT, etc.) and all
    planner calls beyond the first N forward unchanged to `inner`.
    """

    def __init__(self, inner: Any, *, fail_first_n_planner_calls: int = 1):
        if fail_first_n_planner_calls < 0:
            raise ValueError(
                "fail_first_n_planner_calls must be >= 0, "
                f"got {fail_first_n_planner_calls}"
            )
        self._inner = inner
        self._fail_n = fail_first_n_planner_calls
        self._planner_call_count = 0
        self._intercepted_count = 0
        self._lock = threading.Lock()

    # ── Pass-through identity ─────────────────────────────────────

    @property
    def name(self) -> str:
        return getattr(self._inner, "name", "flaky")

    @property
    def model(self) -> str:
        return getattr(self._inner, "model", "")

    @property
    def handles_tools(self) -> bool:
        return getattr(self._inner, "handles_tools", True)

    def __getattr__(self, attr: str) -> Any:
        # Forward anything we don't override to the inner backend.
        # Used for telemetry methods, vision-detection, etc.
        return getattr(self._inner, attr)

    # ── Diagnostics ───────────────────────────────────────────────

    @property
    def planner_call_count(self) -> int:
        """Number of planner calls observed (intercepted + forwarded)."""
        with self._lock:
            return self._planner_call_count

    @property
    def intercepted_count(self) -> int:
        """Number of planner calls that were corrupted (returned malformed)."""
        with self._lock:
            return self._intercepted_count

    # ── The intercept ─────────────────────────────────────────────

    def stream(
        self,
        user_msg: str,
        conversation_history: list,
        instructions: str,
        tools: list,
        max_tokens: int = 4096,
        cancel_event=None,
    ) -> Iterator[Tuple[str, dict]]:
        """Stream a chat completion. Same shape as `OllamaBackend.stream`.

        If the call's `instructions` matches a planner signature AND we
        haven't yet intercepted enough calls, emit the canned malformed
        response (no JSON envelope → walker spawns retry sibling).
        Otherwise forward to `inner.stream(...)` unchanged.
        """
        is_planner = self._is_planner_prompt(instructions)
        should_intercept = False
        if is_planner:
            with self._lock:
                self._planner_call_count += 1
                if self._intercepted_count < self._fail_n:
                    should_intercept = True
                    self._intercepted_count += 1

        if not should_intercept:
            yield from self._inner.stream(
                user_msg=user_msg,
                conversation_history=conversation_history,
                instructions=instructions,
                tools=tools,
                max_tokens=max_tokens,
                cancel_event=cancel_event,
            )
            return

        logger.info(
            "FlakyPlannerBackend intercepting planner call %d/%d "
            "(corrupting with malformed response to trigger walker retry)",
            self._intercepted_count, self._fail_n,
        )
        # Emit the malformed payload as a single text chunk + done event.
        # `OllamaBackend.stream` yields ("text", {"content": ...}) for
        # text chunks and ("done", {...}) when the stream finishes; mirror
        # that shape so the consuming Session loop is none the wiser.
        yield ("text", {"content": _MALFORMED_PLANNER_RESPONSE})
        yield ("done", {"finish_reason": "stop"})

    # ── Internal ──────────────────────────────────────────────────

    def _is_planner_prompt(self, instructions: str) -> bool:
        """True if `instructions` looks like a PLAN or PLAN_DEEP system
        prompt. Conservative match (only the role-identifier prefix);
        we'd rather miss an intercept than corrupt a non-planner call."""
        if not instructions:
            return False
        return any(
            sig in instructions for sig in _PLANNER_PROMPT_SIGNATURES
        )
