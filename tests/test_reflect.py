"""Tests for the REFLECT specialist + `run_reflect_pass` orchestration
helper (v0.5.0a4).

REFLECT is the convergence-ground-truth machinery: deterministic
[bash] / [vision] checks run via `run_reflect_pass` and write
results back into the in-memory roadmap; the model session then
handles [chrome] criteria and emits a verdict that the daemon
cross-checks against the roadmap state.

These tests pin:
- The specialist profile is registered with the right name,
  tool allowlist (file_edit, bash, browser_*), and step budget.
- The prompt explicitly instructs against fabricating SHAs / evidence
  and includes the strict JSON-verdict envelope.
- `run_reflect_pass` correctly routes each criterion type, applies
  bash/vision results to the roadmap, queues chrome/manual for the
  model, and tracks convergence + tally helpers.
- Idempotency: already-passed criteria are not re-run (a flaky bash
  check can't ratchet down a previously-passing roadmap).
- Errors and skips leave the criterion's `passed` field untouched —
  the surface for "couldn't decide" is the iteration log, not the
  roadmap state.
"""
from __future__ import annotations

import pytest

from resonant_client.gui.roadmap import (
    AcceptanceCriterion,
    Roadmap,
)
from resonant_client.orchestration.acceptance_check import (
    BashRunner,
    CheckContext,
    VisionRunner,
)
from resonant_client.orchestration.plan_graph import NodeSpecialization
from resonant_client.orchestration.reflect import (
    ReflectPassResult,
    run_reflect_pass,
)
from resonant_client.orchestration.specialists import (
    SPECIALISTS,
    SpecialistProfile,
    filter_tools_for_specialist,
    get_specialist,
)


# ── Fixtures ────────────────────────────────────────────────────────────


def _make_roadmap(criteria: list[tuple[str, str]]) -> Roadmap:
    """Build a minimal Roadmap with the given (type, text) criteria."""
    rm = Roadmap(feature="test feature", intent_id="test")
    for type_tag, text in criteria:
        rm.acceptance_criteria.append(
            AcceptanceCriterion(type=type_tag, text=text)
        )
    return rm


def _stub_bash(rc: int = 0, stdout: str = "", stderr: str = "") -> BashRunner:
    """Build a BashRunner that returns canned subprocess output without
    actually running anything."""
    return BashRunner(
        _run=lambda cmd, **kw: (rc, stdout, stderr),
    )


def _stub_vision_pass(answer: str = "yes") -> VisionRunner:
    """VisionRunner that always answers `answer` (default yes → pass).

    Also stubs `_list_models` so `is_available()` returns True without
    hitting the real Ollama /api/tags endpoint — keeps tests hermetic.
    """

    def _call(model: str, prompt: str, image_bytes: bytes) -> str:
        return answer

    return VisionRunner(
        _call=_call,
        _list_models=lambda: ["test-vision-model"],
    )


def _image_provider_ok() -> bytes:
    return b"\x89PNG\r\n\x1a\n" + b"fake screenshot bytes"


# ── Specialist registration ─────────────────────────────────────────────


class TestReflectSpecialistRegistration:
    def test_reflect_is_in_node_specialization_enum(self):
        assert NodeSpecialization.REFLECT == "reflect"
        assert "reflect" in NodeSpecialization.ALL

    def test_reflect_is_registered(self):
        assert NodeSpecialization.REFLECT in SPECIALISTS
        profile = get_specialist(NodeSpecialization.REFLECT)
        assert isinstance(profile, SpecialistProfile)
        assert profile.name == "reflect"

    def test_reflect_has_file_edit(self):
        # REFLECT is the ONLY specialist that writes to roadmap.md.
        profile = get_specialist(NodeSpecialization.REFLECT)
        assert "file_edit" in profile.tool_allowlist

    def test_reflect_accepts_connected_browser_mcp_tools(self):
        profile = get_specialist(NodeSpecialization.REFLECT)
        assert profile.allow_mcp
        tools = [{"function": {"name": "mcp_browseros_take_screenshot"}}]
        assert filter_tools_for_specialist(NodeSpecialization.REFLECT, tools) == tools

    def test_reflect_has_bash_for_git(self):
        # bash is required for `git log` / `git rev-parse` (commit
        # SHAs) and ad-hoc fallback verification. NOT for re-running
        # [bash] criteria — those are run deterministically before
        # the model session.
        profile = get_specialist(NodeSpecialization.REFLECT)
        assert "bash" in profile.tool_allowlist

    def test_reflect_has_await_user(self):
        profile = get_specialist(NodeSpecialization.REFLECT)
        assert "await_user" in profile.tool_allowlist

    def test_reflect_step_budget_is_generous(self):
        # Per design doc §7: 20 steps because multiple acceptance
        # checks may need real interaction.
        profile = get_specialist(NodeSpecialization.REFLECT)
        assert profile.max_steps == 20


# ── Prompt invariants ───────────────────────────────────────────────────


class TestReflectPromptInvariants:
    """Pin the behavior-shaping parts of the prompt without locking
    every word — small wording tweaks should not break the suite."""

    @pytest.fixture
    def prompt(self) -> str:
        return get_specialist(NodeSpecialization.REFLECT).system_block

    def test_explains_two_modes(self, prompt: str):
        assert "item-mark" in prompt
        assert "mode: full" in prompt or "full" in prompt.lower()

    def test_explicitly_forbids_fabrication(self, prompt: str):
        # Critical: the user pointed out that the model is biased
        # toward "satisfied" because that ends its work. The prompt
        # must explicitly forbid faking SHAs or evidence.
        assert "DO NOT FABRICATE" in prompt or "never invent" in prompt
        assert "git log" in prompt  # the right way to get SHAs
        assert "git rev-parse" in prompt  # how the runtime validates

    def test_lists_all_four_criterion_types(self, prompt: str):
        assert "[bash]" in prompt
        assert "[chrome]" in prompt
        assert "[vision]" in prompt
        assert "[manual]" in prompt

    def test_says_bash_and_vision_are_run_deterministically(self, prompt: str):
        # The model must NOT re-run [bash] or [vision] checks — those
        # are the runtime's job, and re-running them lets the model
        # override ground truth.
        lower = prompt.lower()
        assert "deterministic" in lower
        assert "trust the runtime" in lower or "don't re-run" in lower or \
               "do not re-run" in lower

    def test_says_manual_does_not_gate_convergence(self, prompt: str):
        # [manual] criteria are advisory only — REFLECT must not
        # treat them as blocking.
        assert "manual" in prompt.lower()
        assert "never gate" in prompt.lower() or "doesn't gate" in prompt.lower() or \
               "excluded from" in prompt.lower()

    def test_includes_json_envelope(self, prompt: str):
        # The structured-output schema is what the daemon parses.
        # All required keys must appear in the example so the model
        # has a template to follow.
        assert "```json" in prompt
        for key in (
            '"completed"',
            '"chrome_results"',
            '"added"',
            '"blocked"',
            '"manual_pending"',
            '"verdict"',
            '"summary"',
            '"estimated_remaining_minutes"',
        ):
            assert key in prompt, f"missing JSON key {key} in prompt"

    def test_lists_all_three_verdict_values(self, prompt: str):
        for verdict in ('"continue"', '"satisfied"', '"blocked"'):
            assert verdict in prompt

    def test_satisfied_verdict_is_gated_on_all_criteria_passing(self, prompt: str):
        # The prompt must say "satisfied requires every non-manual
        # criterion to pass" — otherwise the model can claim
        # `satisfied` while criteria are still red.
        lower = prompt.lower()
        assert "satisfied" in lower
        assert "every non-`[manual]`" in lower or \
               "every non-[manual]" in lower or \
               "even one" in lower  # "even one passed: false blocks satisfied"

    def test_includes_await_user_escape_hatch(self, prompt: str):
        # Per v0.4.10 (T2.5) — every specialist gets the await_user
        # escape hatch with examples.
        assert "await_user" in prompt
        assert "ESCAPE HATCH" in prompt


# ── run_reflect_pass: bash routing ──────────────────────────────────────


class TestRunReflectPassBash:
    def test_passing_bash_marks_criterion_passed(self):
        rm = _make_roadmap([("bash", "`true` exits 0")])
        ctx = CheckContext(bash_runner=_stub_bash(rc=0, stdout="ok"))

        result = run_reflect_pass(rm, ctx)

        assert len(result.bash_results) == 1
        criterion = rm.acceptance_criteria[0]
        assert criterion.passed is True
        assert criterion.evidence.startswith("PASS:")
        assert result.bash_passed == 1
        assert result.bash_failed == 0

    def test_failing_bash_marks_criterion_failed(self):
        rm = _make_roadmap([("bash", "`false` exits 0")])
        ctx = CheckContext(bash_runner=_stub_bash(rc=1, stderr="boom"))

        result = run_reflect_pass(rm, ctx)

        criterion = rm.acceptance_criteria[0]
        assert criterion.passed is False
        assert criterion.evidence.startswith("FAIL:")
        assert result.bash_failed == 1
        assert result.bash_passed == 0

    def test_no_bash_runner_errors_leave_criterion_unchanged(self):
        rm = _make_roadmap([("bash", "`true` exits 0")])
        # No bash_runner in context → BashRunner created with
        # default _run=None which falls through to subprocess. To
        # keep tests hermetic, we'd normally stub. But this test
        # specifically wants to prove that an *errored* result
        # (timeout / nonsense command) doesn't mutate the roadmap.
        ctx = CheckContext(
            bash_runner=BashRunner(
                _run=lambda cmd, **kw: (127, "", "command not found"),
            ),
        )
        result = run_reflect_pass(rm, ctx)

        # rc=127 with no specific assertion → exit_zero mode → fail.
        # The criterion gets passed=False (which is "we definitively
        # know it failed"). To get a true error, we need the runner
        # to raise — but BashRunner doesn't raise, it returns the
        # exception in stderr. So this case is "definitive fail."
        criterion = rm.acceptance_criteria[0]
        assert criterion.passed is False
        assert result.bash_errored == 0  # not an error, a definitive fail


# ── run_reflect_pass: vision routing ────────────────────────────────────


class TestRunReflectPassVision:
    def test_passing_vision_marks_criterion_passed(self):
        rm = _make_roadmap([("vision", "single green circle on dark navy")])
        ctx = CheckContext(
            vision_runner=_stub_vision_pass("yes"),
            image_provider=_image_provider_ok,
        )

        result = run_reflect_pass(rm, ctx)

        assert len(result.vision_results) == 1
        criterion = rm.acceptance_criteria[0]
        assert criterion.passed is True
        assert result.vision_passed == 1

    def test_failing_vision_marks_criterion_failed(self):
        rm = _make_roadmap([("vision", "single green circle on dark navy")])
        ctx = CheckContext(
            vision_runner=_stub_vision_pass("no"),
            image_provider=_image_provider_ok,
        )

        result = run_reflect_pass(rm, ctx)

        criterion = rm.acceptance_criteria[0]
        assert criterion.passed is False
        assert result.vision_failed == 1

    def test_missing_image_provider_errors_leave_criterion_unchanged(self):
        rm = _make_roadmap([("vision", "anything")])
        # image_provider=None — dispatch returns errored result.
        ctx = CheckContext(vision_runner=_stub_vision_pass("yes"))

        result = run_reflect_pass(rm, ctx)

        criterion = rm.acceptance_criteria[0]
        assert criterion.passed is None  # unchanged from default
        assert criterion.evidence == ""  # errors don't write evidence
        assert result.vision_errored == 1
        assert result.vision_passed == 0
        assert result.vision_failed == 0


# ── run_reflect_pass: chrome / manual routing ───────────────────────────


class TestRunReflectPassChrome:
    def test_chrome_criteria_queued_for_model(self):
        rm = _make_roadmap([
            ("chrome", "Click toggle, body bg becomes dark"),
            ("chrome", "Counter button increments"),
        ])
        ctx = CheckContext()

        result = run_reflect_pass(rm, ctx)

        assert len(result.chrome_pending) == 2
        # Roadmap state is untouched — model session decides.
        for criterion in rm.acceptance_criteria:
            assert criterion.passed is None
            assert criterion.evidence == ""

    def test_chrome_pending_preserves_criterion_objects(self):
        rm = _make_roadmap([("chrome", "Specific URL with assertion")])

        result = run_reflect_pass(rm)

        # The pending list contains the actual AcceptanceCriterion
        # objects (not copies / dicts) so the model session can mutate
        # passed/evidence on them when it validates.
        assert result.chrome_pending[0] is rm.acceptance_criteria[0]


class TestRunReflectPassManual:
    def test_manual_criteria_queued_for_handoff(self):
        rm = _make_roadmap([("manual", "Eyeball the print preview")])

        result = run_reflect_pass(rm)

        assert len(result.manual_pending) == 1
        # Manual items don't gate convergence per design — see
        # `AcceptanceCriterion.is_satisfied`.
        assert rm.is_converged()  # vacuously: only criterion is manual

    def test_manual_pending_appears_even_when_already_passed(self):
        # Manual items are advisory and always show in the handoff
        # so the user remembers to verify.
        rm = _make_roadmap([("manual", "Eyeball the export")])
        rm.acceptance_criteria[0].passed = True
        rm.acceptance_criteria[0].evidence = "user said ok last run"

        result = run_reflect_pass(rm)

        assert len(result.manual_pending) == 1


# ── Idempotency ─────────────────────────────────────────────────────────


class TestRunReflectPassIdempotency:
    def test_already_passed_bash_is_not_rerun(self):
        rm = _make_roadmap([("bash", "`true` exits 0")])
        rm.acceptance_criteria[0].passed = True
        rm.acceptance_criteria[0].evidence = "PASS: from earlier pass"

        # If this stub were called, it would return rc=1 (fail) — but
        # we expect idempotency to skip it.
        ctx = CheckContext(bash_runner=_stub_bash(rc=1))

        result = run_reflect_pass(rm, ctx)

        # Result lists empty: we didn't re-run anything.
        assert result.bash_results == []
        # Roadmap state preserved.
        assert rm.acceptance_criteria[0].passed is True
        assert "from earlier pass" in rm.acceptance_criteria[0].evidence

    def test_already_passed_vision_is_not_rerun(self):
        rm = _make_roadmap([("vision", "looks right")])
        rm.acceptance_criteria[0].passed = True
        rm.acceptance_criteria[0].evidence = "PASS: from earlier"

        ctx = CheckContext(
            vision_runner=_stub_vision_pass("no"),  # would fail if rerun
            image_provider=_image_provider_ok,
        )

        result = run_reflect_pass(rm, ctx)

        assert result.vision_results == []
        assert rm.acceptance_criteria[0].passed is True

    def test_already_passed_chrome_does_not_queue_for_model(self):
        rm = _make_roadmap([("chrome", "url assertion")])
        rm.acceptance_criteria[0].passed = True

        result = run_reflect_pass(rm)

        # Already-validated chrome criterion shouldn't waste a model
        # session — the daemon can mechanically converge.
        assert result.chrome_pending == []


# ── Tally helpers + needs_model_session ─────────────────────────────────


class TestReflectPassResultHelpers:
    def test_tallies_across_mixed_results(self):
        rm = _make_roadmap([
            ("bash", "`true` exits 0"),    # pass
            ("bash", "`false` exits 0"),   # fail
        ])
        # First call passes, second fails. Ours uses the SAME stub
        # for both — so simulate via swapping ctx mid-pass would be
        # awkward. Instead use a runner that varies by command.
        seen: list[str] = []

        def _run(cmd, **kw):
            seen.append(cmd)
            if "true" in cmd:
                return (0, "ok", "")
            return (1, "", "boom")

        ctx = CheckContext(bash_runner=BashRunner(_run=_run))
        result = run_reflect_pass(rm, ctx)

        assert result.bash_passed == 1
        assert result.bash_failed == 1
        assert result.bash_errored == 0

    def test_needs_model_session_true_when_chrome_pending(self):
        rm = _make_roadmap([("chrome", "url")])
        result = run_reflect_pass(rm)
        assert result.needs_model_session() is True

    def test_needs_model_session_true_when_manual_pending(self):
        rm = _make_roadmap([("manual", "eyeball it")])
        result = run_reflect_pass(rm)
        assert result.needs_model_session() is True

    def test_needs_model_session_false_when_all_bash_settled(self):
        rm = _make_roadmap([
            ("bash", "`true` exits 0"),
            ("bash", "`echo hi` exits 0"),
        ])
        ctx = CheckContext(bash_runner=_stub_bash(rc=0, stdout="hi"))
        result = run_reflect_pass(rm, ctx)
        # All bash passed, no chrome / manual pending → model session
        # not needed; daemon mechanically declares satisfied.
        assert result.needs_model_session() is False
        assert result.converged is True


# ── Convergence flag ────────────────────────────────────────────────────


class TestRunReflectPassConvergence:
    def test_converged_true_when_all_blocking_pass(self):
        rm = _make_roadmap([("bash", "`true` exits 0")])
        ctx = CheckContext(bash_runner=_stub_bash(rc=0))

        result = run_reflect_pass(rm, ctx)

        assert result.converged is True

    def test_converged_false_when_one_fails(self):
        rm = _make_roadmap([
            ("bash", "`true` exits 0"),
            ("bash", "`false` exits 0"),
        ])

        def _run(cmd, **kw):
            if "true" in cmd:
                return (0, "ok", "")
            return (1, "", "")

        ctx = CheckContext(bash_runner=BashRunner(_run=_run))
        result = run_reflect_pass(rm, ctx)

        assert result.converged is False

    def test_converged_false_when_chrome_unvalidated(self):
        # A chrome criterion that hasn't been validated yet (passed=None)
        # blocks convergence — the model session needs to run.
        rm = _make_roadmap([("chrome", "click toggle")])
        result = run_reflect_pass(rm)
        assert result.converged is False

    def test_converged_ignores_manual_criteria(self):
        # Manual items don't gate convergence per
        # AcceptanceCriterion.is_blocking.
        rm = _make_roadmap([
            ("bash", "`true` exits 0"),
            ("manual", "eyeball it"),
        ])
        ctx = CheckContext(bash_runner=_stub_bash(rc=0))

        result = run_reflect_pass(rm, ctx)

        assert result.converged is True


# ── Empty roadmap / defaults ────────────────────────────────────────────


class TestRunReflectPassEdgeCases:
    def test_empty_roadmap_returns_empty_result(self):
        rm = Roadmap(feature="empty")
        result = run_reflect_pass(rm)

        assert result.bash_results == []
        assert result.vision_results == []
        assert result.chrome_pending == []
        assert result.manual_pending == []
        # Empty + all-criteria-satisfied vacuously is True per
        # is_converged's all() over empty list. The daemon's job is
        # to refuse to declare satisfied on an empty roadmap (per
        # has_any_acceptance_criteria check).
        assert result.converged is True

    def test_no_context_routes_chrome_and_manual_only(self):
        # With no context, bash/vision criteria error (no runners) but
        # chrome and manual still queue correctly.
        rm = _make_roadmap([
            ("bash", "`true` exits 0"),
            ("chrome", "url assertion"),
            ("manual", "eyeball"),
        ])

        result = run_reflect_pass(rm)

        # bash: subprocess actually runs (no stub) → may pass or fail
        # depending on environment. The test cares about chrome/manual.
        assert len(result.chrome_pending) == 1
        assert len(result.manual_pending) == 1

    def test_default_reflect_pass_result_fields(self):
        r = ReflectPassResult()
        assert r.bash_results == []
        assert r.vision_results == []
        assert r.chrome_pending == []
        assert r.manual_pending == []
        assert r.converged is False
        assert r.bash_passed == 0
        assert r.bash_failed == 0
        assert r.bash_errored == 0
        assert r.vision_passed == 0
        assert r.vision_failed == 0
        assert r.vision_errored == 0
