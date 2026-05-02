"""End-to-end integration test: the bootstrap-roguelite spec from
docs/long-running-agents-phase-2.md §11.2 flowing through every piece
of v0.5.0a1–a4 machinery in sequence.

What this test proves:

1. The exact rigorous-grill spec format the design doc commits to
   parses cleanly via `grill_me.extract_spec` — typed criteria,
   time budget, refined intent.
2. Those parsed criteria construct a `Roadmap` via the v0.5.0a1 data
   layer without losing fidelity.
3. The v0.5.0a4 `run_reflect_pass` correctly routes each criterion
   to the right deterministic dispatcher (or queues for the model
   for [chrome] criteria).
4. Convergence is "real, not a model mood": when ALL [bash] and
   [vision] criteria pass deterministically AND no [chrome] is
   pending, `roadmap.is_converged()` is True. When even one [bash]
   fails, convergence is False — even if every other criterion is
   green.

This test does NOT exercise the model session (REFLECT specialist's
JSON verdict emission) or the autonomous loop daemon (v0.5.0a5
upcoming). It exercises the deterministic spine that the daemon
will rely on.

The roguelite spec has 8 criteria spanning all 4 types:
- 5 × [bash] (npm install, build, tsc, file count, no `any` types)
- 2 × [chrome] (canvas pixel sample, canvas dimensions)
- 1 × [vision] (single green circle on dark navy)

That's the right shape for a real autonomous mission: most criteria
are fast `[bash]`, a couple are interactive `[chrome]`, one is a
visual confirmation `[vision]`. Real missions will look similar.
"""
from __future__ import annotations

from typing import Optional

import pytest

from resonant_client.gui.roadmap import (
    AcceptanceCriterion,
    Roadmap,
    RoadmapItem,
)
from resonant_client.orchestration.acceptance_check import (
    BashRunner,
    CheckContext,
    VisionRunner,
)
from resonant_client.orchestration.grill_me import extract_spec
from resonant_client.orchestration.reflect import run_reflect_pass


# ── The bootstrap-roguelite spec (verbatim from design doc §11.2) ──────


# This is the "gold" spec a rigorous-grill session WOULD produce for
# the bootstrap-roguelite request, written exactly the way the design
# doc says it should look. If the prompt drifts and the model emits a
# differently-shaped spec, this test will fail — pointing the next
# implementer at the format contract before downstream code breaks.
ROGUELITE_SPEC = """\
## Final spec

**Refined intent:** Bootstrap a TypeScript roguelite skeleton with
strict tsc, a centered Canvas rendering the player as a single green
circle on a dark navy background, dev-server-driven via Vite. Six
source files total, no `any` types.

**Key assumptions:**
- Vite is acceptable as the dev server
- Player is rendered with the 2D canvas API, no third-party engine
- TypeScript strict mode is non-negotiable

**In scope:**
- Project scaffold (package.json, tsconfig.json, vite.config.ts)
- Canvas mounting + 800×600 sizing
- Player as a centered green circle (dark navy bg)

**Out of scope:**
- Movement / input
- Map generation
- Combat / enemies / items

**Time budget:** 4h

**Technical constraints:**
- Strict TypeScript (no `any`)
- Exactly 6 source files in src/ (no over-engineering)
- No third-party game engines

**Acceptance criteria:**
- `[bash]` `npm install` exits 0
- `[bash]` `npm run build` exits 0
- `[bash]` `npx tsc --noEmit` exits 0
- `[bash]` `find src -type f | wc -l` output == 6
- `[bash]` `! grep -rn ': any' src/` exits 0
- `[chrome]` Navigate to http://localhost:3000; canvas exists with width 800 height 600
- `[chrome]` Center pixel of canvas reports rgb(0±5, 255±5, 153±5) via `browser_js`
- `[vision]` Screenshot shows a single green circle on a dark navy background, no other shapes visible

**Open risks:**
- WebKit / Chromium rendering parity for the green-shade tolerance
- `wc -l` line-ending behaviour on Windows vs POSIX checkouts
"""


# ── Stub runners (hermetic — no real subprocess / Ollama) ───────────────


def _bash_stub_all_pass() -> BashRunner:
    """Bash runner that simulates a successful build of the roguelite
    project. Exit codes are crafted per-command to match what each
    criterion would produce on a clean run."""

    def _run(cmd: str, **kw):
        if "npm install" in cmd:
            return (0, "added 142 packages\n", "")
        if "npm run build" in cmd:
            return (0, "vite build\n✓ built in 1.2s\n", "")
        if "tsc --noEmit" in cmd:
            return (0, "", "")
        if "wc -l" in cmd or "find src" in cmd:
            return (0, "6\n", "")
        if "grep" in cmd:
            # The parser strips the leading `! ` and runs `grep` directly,
            # with assertion mode=exit_nonzero. Returning exit 1 (grep
            # "no match") is what marks the criterion as PASS — the
            # runtime correctly inverts via the parsed assertion mode,
            # NOT by literally running `! grep` in a shell.
            return (1, "", "")
        return (0, "", "")

    return BashRunner(_run=_run)


def _bash_stub_tsc_fails() -> BashRunner:
    """Bash runner that fails the tsc strict check — simulates an
    iteration where the implementer left an `any` type behind."""

    def _run(cmd: str, **kw):
        if "tsc --noEmit" in cmd:
            return (
                2,
                "",
                "src/player.ts(14,3): error TS7006: Parameter 'pos' "
                "implicitly has an 'any' type.\n",
            )
        return _bash_stub_all_pass().run(cmd)

    return BashRunner(_run=_run)


def _vision_stub(answer: str) -> VisionRunner:
    """Vision runner that returns `answer` as the model verdict.
    `is_available` stubbed to True so we don't hit the live Ollama."""

    def _call(model: str, prompt: str, image_bytes: bytes) -> str:
        return answer

    return VisionRunner(
        _call=_call,
        _list_models=lambda: [VisionRunner().model],
    )


def _vision_stub_unavailable() -> VisionRunner:
    """Vision runner whose model is NOT in the Ollama list — simulates
    the user not having `qwen2.5vl:7b` pulled. Should produce an
    errored CheckResult, not crash the pass."""
    return VisionRunner(_list_models=lambda: ["llama3:latest"])


def _image_provider_ok() -> bytes:
    return b"\x89PNG\r\n\x1a\n" + b"fake roguelite screenshot bytes"


# ── End-to-end: spec → roadmap → reflect ────────────────────────────────


class TestRogueliteSpecParsing:
    """Stage 1: the rigorous-grill spec format parses correctly."""

    def test_extract_spec_finds_block(self):
        result = extract_spec(ROGUELITE_SPEC)
        assert result is not None

    def test_refined_intent_captured(self):
        result = extract_spec(ROGUELITE_SPEC)
        assert "TypeScript" in result.refined_intent
        assert "roguelite" in result.refined_intent

    def test_time_budget_captured(self):
        result = extract_spec(ROGUELITE_SPEC)
        assert result.time_budget == "4h"

    def test_all_eight_criteria_captured(self):
        result = extract_spec(ROGUELITE_SPEC)
        assert len(result.acceptance_criteria) == 8

    def test_criterion_types_match_design_doc(self):
        result = extract_spec(ROGUELITE_SPEC)
        types = [c.type for c in result.acceptance_criteria]
        assert types.count("bash") == 5
        assert types.count("chrome") == 2
        assert types.count("vision") == 1
        assert types.count("manual") == 0

    def test_no_any_types_criterion_uses_bang_grep(self):
        # The "no `any` types" criterion is the trickiest format —
        # the negation prefix `!` inverts a non-zero exit (grep no-match)
        # into a pass. Pin that the parser preserved the command.
        result = extract_spec(ROGUELITE_SPEC)
        bash_criteria = [c for c in result.acceptance_criteria if c.type == "bash"]
        assert any("grep" in c.text and "any" in c.text for c in bash_criteria)


# ── End-to-end: roadmap construction from spec ──────────────────────────


class TestRoadmapFromSpec:
    """Stage 2: criteria flow into a Roadmap object."""

    def _build_roadmap(self) -> Roadmap:
        spec = extract_spec(ROGUELITE_SPEC)
        rm = Roadmap(
            feature="bootstrap roguelite",
            intent_id="test-roguelite",
            time_budget_label=spec.time_budget,
            acceptance_criteria=list(spec.acceptance_criteria),
        )
        return rm

    def test_roadmap_picks_up_all_criteria(self):
        rm = self._build_roadmap()
        assert len(rm.acceptance_criteria) == 8

    def test_roadmap_has_acceptance_criteria_method(self):
        rm = self._build_roadmap()
        assert rm.has_any_acceptance_criteria() is True

    def test_initial_acceptance_summary_zero_passed(self):
        rm = self._build_roadmap()
        passed, total = rm.acceptance_summary()
        assert passed == 0
        # All 8 criteria are blocking (no manual in this spec).
        assert total == 8

    def test_initial_state_is_not_converged(self):
        rm = self._build_roadmap()
        assert rm.is_converged() is False


# ── End-to-end: reflect pass on a "happy path" run ──────────────────────


class TestRogueliteReflectHappyPath:
    """Stage 3: a successful autonomous run hits all bash + vision
    deterministically; only [chrome] still needs the model session."""

    def _build_and_reflect(
        self,
        bash_runner: Optional[BashRunner] = None,
        vision_runner: Optional[VisionRunner] = None,
        image_provider=_image_provider_ok,
    ):
        spec = extract_spec(ROGUELITE_SPEC)
        rm = Roadmap(
            acceptance_criteria=list(spec.acceptance_criteria),
        )
        ctx = CheckContext(
            bash_runner=bash_runner,
            vision_runner=vision_runner,
            image_provider=image_provider,
        )
        result = run_reflect_pass(rm, ctx)
        return rm, result

    def test_all_bash_pass_when_runner_returns_zero(self):
        rm, result = self._build_and_reflect(
            bash_runner=_bash_stub_all_pass(),
            vision_runner=_vision_stub("yes"),
        )

        assert result.bash_passed == 5
        assert result.bash_failed == 0
        assert result.bash_errored == 0

    def test_vision_passes_when_model_says_yes(self):
        rm, result = self._build_and_reflect(
            bash_runner=_bash_stub_all_pass(),
            vision_runner=_vision_stub("yes"),
        )

        assert result.vision_passed == 1
        assert result.vision_failed == 0

    def test_chrome_criteria_queued_for_model(self):
        rm, result = self._build_and_reflect(
            bash_runner=_bash_stub_all_pass(),
            vision_runner=_vision_stub("yes"),
        )

        # Both chrome criteria need REFLECT model session.
        assert len(result.chrome_pending) == 2
        # Daemon shouldn't skip the model session — chrome is pending.
        assert result.needs_model_session() is True

    def test_not_converged_until_chrome_validated(self):
        # All bash + vision pass, but two chrome criteria still
        # have passed=None. Convergence requires ALL non-manual to be
        # green.
        rm, result = self._build_and_reflect(
            bash_runner=_bash_stub_all_pass(),
            vision_runner=_vision_stub("yes"),
        )

        assert result.converged is False
        passed, total = rm.acceptance_summary()
        assert passed == 6  # 5 bash + 1 vision
        assert total == 8

    def test_converges_when_chrome_also_marked_passed(self):
        # Simulate the model session validating both [chrome]
        # criteria via browser_navigate / browser_js — it would mark
        # them passed=True via the roadmap's update_criterion.
        rm, result = self._build_and_reflect(
            bash_runner=_bash_stub_all_pass(),
            vision_runner=_vision_stub("yes"),
        )
        for criterion in result.chrome_pending:
            criterion.passed = True
            criterion.evidence = "PASS: browser_js confirmed"

        assert rm.is_converged() is True
        passed, total = rm.acceptance_summary()
        assert passed == 8
        assert total == 8


# ── End-to-end: failure modes ───────────────────────────────────────────


class TestRogueliteReflectFailureModes:
    """Stage 4: how the integration handles iterations that don't
    produce a clean convergence."""

    def test_one_failing_bash_blocks_convergence(self):
        # Simulates an iteration where the implementer left an `any`
        # type in. tsc fails, every other check passes. Convergence is
        # FALSE — even though 4/5 bash + vision pass, that one red
        # criterion is the canary.
        spec = extract_spec(ROGUELITE_SPEC)
        rm = Roadmap(acceptance_criteria=list(spec.acceptance_criteria))
        ctx = CheckContext(
            bash_runner=_bash_stub_tsc_fails(),
            vision_runner=_vision_stub("yes"),
            image_provider=_image_provider_ok,
        )

        result = run_reflect_pass(rm, ctx)

        assert result.bash_passed == 4   # install / build / find / grep
        assert result.bash_failed == 1   # tsc
        assert rm.is_converged() is False

        # The failed criterion has explanatory evidence the user can
        # read in the roadmap markdown.
        failed_criterion = next(
            c for c in rm.acceptance_criteria
            if c.passed is False
        )
        assert "tsc --noEmit" in failed_criterion.text
        assert failed_criterion.evidence.startswith("FAIL:")

    def test_vision_unavailable_does_not_crash(self):
        # User hasn't pulled qwen2.5vl:7b. The pass should still run
        # bash criteria deterministically and just skip vision with
        # an errored result.
        spec = extract_spec(ROGUELITE_SPEC)
        rm = Roadmap(acceptance_criteria=list(spec.acceptance_criteria))
        ctx = CheckContext(
            bash_runner=_bash_stub_all_pass(),
            vision_runner=_vision_stub_unavailable(),
            image_provider=_image_provider_ok,
        )

        # Doesn't raise.
        result = run_reflect_pass(rm, ctx)

        assert result.bash_passed == 5
        assert result.vision_passed == 0
        assert result.vision_failed == 0
        assert result.vision_errored == 1
        # The vision criterion stays passed=None (couldn't decide).
        vision_criterion = next(
            c for c in rm.acceptance_criteria
            if c.type == "vision"
        )
        assert vision_criterion.passed is None

    def test_idempotent_across_two_passes(self):
        # First pass: tsc fails → criterion is passed=False with FAIL:
        # evidence. User fixes the `any` issue. Second pass: stubs now
        # report tsc green. The fix gets credited; previously-passing
        # criteria are NOT re-run (idempotent).
        spec = extract_spec(ROGUELITE_SPEC)
        rm = Roadmap(acceptance_criteria=list(spec.acceptance_criteria))

        # First pass — tsc fails.
        ctx_fail = CheckContext(
            bash_runner=_bash_stub_tsc_fails(),
            vision_runner=_vision_stub("yes"),
            image_provider=_image_provider_ok,
        )
        first = run_reflect_pass(rm, ctx_fail)
        assert first.bash_failed == 1

        # User fixes the `any`. Second pass — all bash green. The 4
        # already-passing bash criteria don't re-run (idempotency);
        # only the previously-failed tsc + still-pending [chrome] do.
        ctx_pass = CheckContext(
            bash_runner=_bash_stub_all_pass(),
            vision_runner=_vision_stub("yes"),
            image_provider=_image_provider_ok,
        )
        second = run_reflect_pass(rm, ctx_pass)

        # tsc re-ran (was previously False, not True, so not skipped).
        assert second.bash_passed == 1   # just tsc this time
        assert second.bash_failed == 0
        # All bash criteria now passed in the roadmap.
        all_bash = [c for c in rm.acceptance_criteria if c.type == "bash"]
        assert all(c.passed is True for c in all_bash)


# ── End-to-end: the "fully converged without model session" path ────────


class TestRogueliteAllBashSpecConverges:
    """A simpler spec — bash criteria only — should fully converge
    deterministically without the model session ever needing to run.
    Important for the autonomous-loop daemon: it should detect this
    and skip the (expensive) REFLECT model dispatch."""

    SIMPLE_SPEC = """\
## Final spec

**Refined intent:** A trivial bash-only spec for harness testing.

**Time budget:** 1h

**Acceptance criteria:**
- `[bash]` `echo hi` exits 0
- `[bash]` `echo hi` output == hi
"""

    def test_pure_bash_spec_converges_without_model(self):
        spec = extract_spec(self.SIMPLE_SPEC)
        rm = Roadmap(acceptance_criteria=list(spec.acceptance_criteria))
        ctx = CheckContext(
            bash_runner=BashRunner(
                _run=lambda cmd, **kw: (0, "hi\n", ""),
            ),
        )

        result = run_reflect_pass(rm, ctx)

        assert result.converged is True
        assert result.needs_model_session() is False
        assert result.bash_passed == 2
