"""
REFLECT orchestration — the deterministic half of an Autonomous Mission
reflection pass.

This module sits between the runtime acceptance-criteria dispatchers
(`acceptance_check.dispatch`) and the live roadmap object
(`gui.roadmap.Roadmap`). It is the piece of code that runs BEFORE the
REFLECT specialist's model session: it executes every `[bash]` and
`[vision]` criterion deterministically, writes the results back into
the in-memory roadmap, and reports what's left for the model to handle
agentically (`[chrome]` criteria + roadmap-item bookkeeping +
verdict).

## Why split deterministic from agentic

The convergence-ground-truth design (see
`docs/long-running-agents-phase-2.md` §11.4) hinges on the model NOT
being able to fake a `passed=true` for an acceptance criterion. We
get that property by running every check that CAN be deterministic
(bash exit codes, vision-model verdicts) BEFORE handing the model a
session. By the time the REFLECT prompt sees the roadmap, those
results are already written in. The model can drive `[chrome]`
checks (which need real browser interaction it must perform itself)
and emit the structured verdict — but its verdict is cross-checked
against the roadmap state by the autonomous-loop daemon. If the
model claims `satisfied` while a `[bash]` criterion shows
`passed=false`, the daemon overrides to `continue`.

## Two trigger modes

The reflect pass runs in one of two modes (caller chooses):

* **`item-mark`** (after a single roadmap item ships): the model marks
  one item complete with its commit ref. No criteria validation —
  that's the full pass's job. This module does NOTHING in this mode;
  the daemon dispatches the model session directly.
* **`full`** (every K=3 iterations, when the roadmap empties, before
  stopping): every deterministic criterion is run via this module
  first; then the model session takes over to handle `[chrome]` +
  emit the JSON verdict.

This module is concerned only with the `full`-pass deterministic
prelude. Item-mark mode never enters here.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from ..gui.roadmap import AcceptanceCriterion, Roadmap, update_criterion
from .acceptance_check import (
    CheckContext,
    CheckResult,
    dispatch,
    summarize_for_roadmap,
)


# ── Result type ─────────────────────────────────────────────────────────


@dataclass
class ReflectPassResult:
    """Outcome of `run_reflect_pass`.

    The caller (autonomous-loop daemon) uses this to:
    1. Decide whether to skip the model session entirely (if `converged`
       is True after the deterministic pass and there are no `[chrome]`
       criteria still pending, the verdict is mechanically `satisfied`).
    2. Pass `chrome_pending` and `manual_pending` into the REFLECT model
       prompt as context — the model knows which criteria still need
       its attention vs which the runtime already settled.
    3. Render the per-iteration "K bash criteria passed, M failed" line
       in the chat UI.

    `bash_results` and `vision_results` are returned in the same order
    they appear in `roadmap.acceptance_criteria` so the caller can
    correlate them back to the original list. Each tuple is
    `(criterion, result)` — both objects, not just text — because the
    caller may want to inspect both the criterion's `text` (for
    chat-display) and the result's `evidence`/`error`/`skipped` for
    detailed reporting.
    """

    bash_results: list[tuple[AcceptanceCriterion, CheckResult]] = field(
        default_factory=list
    )
    vision_results: list[tuple[AcceptanceCriterion, CheckResult]] = field(
        default_factory=list
    )
    chrome_pending: list[AcceptanceCriterion] = field(default_factory=list)
    manual_pending: list[AcceptanceCriterion] = field(default_factory=list)
    # Convergence AFTER this pass — `roadmap.is_converged()` reflects
    # the deterministic results we just wrote in. NOT necessarily
    # final: a still-pending [chrome] criterion will keep this False
    # until the model session validates it.
    converged: bool = False

    # ── Tally helpers (used by the daemon for status messages) ──

    @property
    def bash_passed(self) -> int:
        return sum(1 for _, r in self.bash_results if r.passed is True)

    @property
    def bash_failed(self) -> int:
        # Errored / skipped don't count as failed — they're "couldn't
        # decide". The daemon may treat repeated errors as blocked.
        return sum(
            1
            for _, r in self.bash_results
            if r.passed is False and not r.error and not r.skipped
        )

    @property
    def bash_errored(self) -> int:
        return sum(1 for _, r in self.bash_results if bool(r.error))

    @property
    def vision_passed(self) -> int:
        return sum(1 for _, r in self.vision_results if r.passed is True)

    @property
    def vision_failed(self) -> int:
        return sum(
            1
            for _, r in self.vision_results
            if r.passed is False and not r.error and not r.skipped
        )

    @property
    def vision_errored(self) -> int:
        return sum(1 for _, r in self.vision_results if bool(r.error))

    def needs_model_session(self) -> bool:
        """True iff any `[chrome]` criterion still needs validation,
        OR the roadmap has manual items the daemon should surface in
        a handoff. False means the daemon can mechanically declare
        `satisfied` (if `converged`) or `blocked` (if not) without
        spinning up the REFLECT model session at all — saves tokens
        for trivially-converged missions."""
        return bool(self.chrome_pending) or bool(self.manual_pending)


# ── Public entrypoint ───────────────────────────────────────────────────


def run_reflect_pass(
    roadmap: Roadmap,
    context: Optional[CheckContext] = None,
) -> ReflectPassResult:
    """Run every deterministic acceptance check on `roadmap` and write
    the results back into the in-memory `Roadmap` object.

    What this does:
    - For each `[bash]` criterion: dispatch via `acceptance_check.dispatch`,
      collect the `CheckResult`, and (for definitive pass/fail outcomes)
      write `passed` and `evidence` back into the roadmap via
      `gui.roadmap.update_criterion`. Errors and skips leave the
      criterion's `passed` field at its current value (usually `None`)
      so the caller can decide whether to retry or surface to the user.
    - For each `[vision]` criterion: same, IF `context.image_provider`
      is set (REFLECT screenshots the relevant surface before the pass
      via the daemon's pre-pass hook).
    - For each `[chrome]` criterion: append to `chrome_pending` for the
      model session to handle.
    - For each `[manual]` criterion: append to `manual_pending`.

    What this does NOT do:
    - Persist the roadmap to disk. The caller (daemon) handles `save()`
      after the model session ALSO writes to it — the disk version
      reflects both halves of the pass.
    - Mark roadmap items (T1.x) complete. Those are the model's job
      via `file_edit` in the agentic phase.
    - Decide the final verdict. This module produces the inputs; the
      model session produces the verdict; the daemon cross-checks both.

    `context` may be None — defaults to a no-op CheckContext (no bash
    runner, no vision runner, no image_provider). With a default
    context, every criterion will error or skip; useful only for
    structural tests of this function.
    """
    context = context or CheckContext()

    result = ReflectPassResult()

    for criterion in roadmap.acceptance_criteria:
        # Idempotency: if a criterion already passed in a prior pass,
        # we don't re-validate. This means a flaky bash check can't
        # ratchet down a previously-passing roadmap. (If the user
        # WANTS to re-validate, they can hand-edit the roadmap to
        # reset the checkbox.)
        if criterion.passed is True:
            if criterion.type == "chrome":
                # Already-passed [chrome] criterion: still no model
                # session needed for it. Skip pending.
                pass
            elif criterion.type == "manual":
                # Manual items always show in handoff regardless of
                # prior state — the user needs the reminder.
                result.manual_pending.append(criterion)
            continue

        check = dispatch(criterion, context)

        if criterion.type == "bash":
            result.bash_results.append((criterion, check))
            _apply_definitive(roadmap, criterion, check)
        elif criterion.type == "vision":
            result.vision_results.append((criterion, check))
            _apply_definitive(roadmap, criterion, check)
        elif criterion.type == "chrome":
            # `dispatch` returns delegate_to_model() for chrome —
            # don't touch the criterion's state, just queue it.
            result.chrome_pending.append(criterion)
        elif criterion.type == "manual":
            # `dispatch` returns skip_manual() — record and move on.
            result.manual_pending.append(criterion)
        # No `else` — AcceptanceCriterion validates `type` against
        # CRITERION_TYPES at construction, so unknown types can't
        # appear here. If they do, we silently drop them.

    result.converged = roadmap.is_converged()
    return result


# ── Internal helpers ────────────────────────────────────────────────────


def _apply_definitive(
    roadmap: Roadmap,
    criterion: AcceptanceCriterion,
    check: CheckResult,
) -> None:
    """Write a `[bash]` / `[vision]` result back into the roadmap if
    it produced a definitive pass/fail. Errors and skips leave the
    criterion alone (`passed=None` stays `None`) but DO write the
    evidence string so the user can see what went wrong via the
    UI/file readout.

    `update_criterion` requires `passed: bool`, so for non-definitive
    results we skip the call — the criterion's `evidence` field stays
    empty. The daemon's iteration log is the surface for "we tried to
    check this but it errored"; the roadmap itself only records
    confirmed pass/fail, which keeps the file-on-disk readable.
    """
    if check.error or check.skipped:
        return  # leave criterion unchanged
    if check.passed is None:
        return  # defensive — shouldn't happen for definitive results
    update_criterion(
        roadmap,
        text_match=criterion.text,
        passed=bool(check.passed),
        evidence=summarize_for_roadmap(check),
    )
