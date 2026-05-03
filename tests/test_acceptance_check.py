"""
Tests for `orchestration/acceptance_check.py` — the v0.5.0a2
dispatcher for typed acceptance-criteria validation.

Three test groups:
  * `[bash]` strategy — command extraction, exit-code semantics,
    output comparisons, idempotency, timeout handling
  * `[vision]` strategy — yes/no parsing, model availability check,
    error cases (empty image, model unavailable)
  * Top-level `dispatch()` — routing per type, [chrome] delegate,
    [manual] skip, error envelopes

The runners (`BashRunner`, `VisionRunner`) accept dependency-injection
hooks so the tests don't need a live shell or a live Ollama. The
real subprocess + httpx paths are exercised through __main__ smoke
runs (out of scope for unit tests).
"""

from __future__ import annotations

from typing import Optional

import pytest

from resonant_client.gui.roadmap import AcceptanceCriterion
from resonant_client.orchestration.acceptance_check import (
    DEFAULT_VISION_MODEL,
    BashAssertion,
    BashRunner,
    CheckContext,
    CheckResult,
    VisionRunner,
    dispatch,
    parse_bash_assertion,
    run_bash_check,
    run_vision_check,
    summarize_for_roadmap,
)


# ── parse_bash_assertion ──────────────────────────────────────────────


class TestParseBashAssertion:
    def test_simple_command_in_backticks(self):
        a = parse_bash_assertion("`npm run build` exits 0")
        assert a is not None
        assert a.command == "npm run build"
        assert a.mode == "exit_zero"

    def test_negated_command(self):
        # `! grep ...` means "pass iff grep finds nothing"
        a = parse_bash_assertion("`! grep -rn ': any' src/`")
        assert a is not None
        assert a.command == "grep -rn ': any' src/"
        assert a.mode == "exit_nonzero"

    def test_equals_assertion_via_form_b_trailing_prose(self):
        # v0.5.2a4 — Form A `==` was DROPPED to avoid mis-matching
        # `==` inside command text (Python `assert x==y`, bash
        # conditionals, etc.). The trailing-prose form is now the
        # only way to assert output equality.
        a = parse_bash_assertion("`find src -type f | wc -l` output == 4")
        assert a is not None
        assert a.command == "find src -type f | wc -l"
        assert a.mode == "output_eq"
        assert a.expected_value == "4"

    def test_form_a_equals_inside_backticks_no_longer_matches_command(self):
        # v0.5.2a4 regression guard. The exact failing criterion from
        # the GA roguelite smoke: `==True` inside a Python expression
        # used to mis-match Form A's `_EQUALS_ASSERTION_RE` and
        # produce a malformed BashAssertion. Now Form A skips `==`
        # entirely and the trailing `output == ok` (Form B) wins.
        criterion = (
            r"""`cat tsconfig.json | python -c "assert c['strict']==True" """
            r"""&& echo ok` output == ok"""
        )
        a = parse_bash_assertion(criterion)
        assert a is not None
        assert a.mode == "output_eq"
        # The command preserves the embedded ==True without splitting
        assert "==True" in a.command
        assert a.expected_value == "ok"

    def test_lt_assertion(self):
        a = parse_bash_assertion("`grep -c FIXME src/main.py < 3`")
        assert a is not None
        assert a.mode == "output_lt"
        assert a.expected_value == "3"

    def test_gt_assertion(self):
        a = parse_bash_assertion("`git log --oneline | wc -l > 5`")
        assert a is not None
        assert a.mode == "output_gt"
        assert a.expected_value == "5"

    def test_shell_input_redirect_not_parsed_as_lt_operator(self):
        # v0.5.1a4 regression: `<` inside a shell command (input
        # redirect) was being mis-matched as the assertion operator.
        # The trailing `output > 5` (Form B) is the actual operator.
        # Found in v0.5.1 final smoke when `wc -l < wordcount.py`
        # silently failed because the parser split it as
        # `command="wc -l", expected="wordcount.py"`.
        a = parse_bash_assertion("`wc -l < wordcount.py` output > 5")
        assert a is not None
        assert a.command == "wc -l < wordcount.py"
        assert a.mode == "output_gt"
        assert a.expected_value == "5"

    def test_shell_output_redirect_not_parsed_as_gt_operator(self):
        # Same idea for `>`. Filename comparand isn't an integer,
        # so the tightened regex skips it.
        a = parse_bash_assertion("`echo hi > /tmp/out.txt` exits 0")
        assert a is not None
        # Either exit_zero (preferred) or whatever fallback — the
        # critical thing is it must NOT be output_gt with
        # expected_value="/tmp/out.txt"
        assert a.mode != "output_gt"

    def test_no_backticks_returns_none(self):
        # The criterion must contain a backtick-quoted command. Pure
        # prose like "the build works" is rejected — the rigorous
        # grill is supposed to produce structured criteria.
        assert parse_bash_assertion("the build works") is None
        assert parse_bash_assertion("") is None

    def test_longest_backtick_block_wins(self):
        # Real criteria have prose like "no `any` types: `! grep ...`"
        # where the prose-style backtick is short and the command is
        # long. Parser picks the longest block.
        a = parse_bash_assertion("no `any` types: `! grep -rn ': any' src/`")
        assert a is not None
        assert a.command == "grep -rn ': any' src/"
        assert a.mode == "exit_nonzero"

    def test_ties_broken_by_first(self):
        # When two backtick blocks are the same length, source order
        # decides — the first one wins. Pinning so the choice is
        # explicit (`max` over an iterator returns the first max).
        a = parse_bash_assertion("`one cmd` then `two cmd`")
        assert a is not None
        # Both are 7 chars; first wins via stable max.
        assert a.command == "one cmd"


# ── BashAssertion.evaluate ────────────────────────────────────────────


class TestBashAssertionEvaluate:
    def test_exit_zero_passes_on_zero(self):
        a = BashAssertion(command="x", mode="exit_zero")
        passed, ev = a.evaluate(0, "ok\n", "")
        assert passed is True
        assert "exit=0" in ev

    def test_exit_zero_fails_on_nonzero(self):
        a = BashAssertion(command="x", mode="exit_zero")
        passed, ev = a.evaluate(1, "", "boom")
        assert passed is False
        assert "exit=1" in ev
        assert "boom" in ev

    def test_exit_nonzero_inverts(self):
        # `! grep` style: pass iff exit != 0
        a = BashAssertion(command="x", mode="exit_nonzero")
        assert a.evaluate(1, "", "")[0] is True
        assert a.evaluate(0, "", "")[0] is False

    def test_output_eq_compares_trimmed_stdout(self):
        a = BashAssertion(command="wc -l", mode="output_eq", expected_value="4")
        passed, _ = a.evaluate(0, "  4\n", "")
        assert passed is True
        passed, _ = a.evaluate(0, "5", "")
        assert passed is False

    def test_output_lt_int_comparison(self):
        a = BashAssertion(command="wc -l", mode="output_lt", expected_value="10")
        assert a.evaluate(0, "5\n", "")[0] is True
        assert a.evaluate(0, "10\n", "")[0] is False
        assert a.evaluate(0, "15\n", "")[0] is False

    def test_output_lt_non_integer_fails_safely(self):
        a = BashAssertion(command="x", mode="output_lt", expected_value="10")
        passed, ev = a.evaluate(0, "not-a-number\n", "")
        assert passed is False
        assert "non-integer" in ev

    def test_output_gt_int_comparison(self):
        a = BashAssertion(command="x", mode="output_gt", expected_value="3")
        assert a.evaluate(0, "5", "")[0] is True
        assert a.evaluate(0, "3", "")[0] is False
        assert a.evaluate(0, "1", "")[0] is False

    def test_unknown_mode_returns_false(self):
        a = BashAssertion(command="x", mode="bogus")
        passed, ev = a.evaluate(0, "", "")
        assert passed is False
        assert "unknown" in ev


# ── BashRunner ────────────────────────────────────────────────────────


class TestBashRunnerStub:
    def test_run_uses_injected_callback(self):
        called = {}

        def fake_run(cmd, cwd=None, timeout=None):
            called["cmd"] = cmd
            called["cwd"] = cwd
            return 0, "stub stdout", ""

        runner = BashRunner(_run=fake_run, cwd="/tmp/p")
        rc, out, err = runner.run("echo hi")
        assert (rc, out, err) == (0, "stub stdout", "")
        assert called["cmd"] == "echo hi"
        assert called["cwd"] == "/tmp/p"


# ── run_bash_check ────────────────────────────────────────────────────


def _stub_runner(rc: int = 0, stdout: str = "", stderr: str = "") -> BashRunner:
    return BashRunner(_run=lambda cmd, **kw: (rc, stdout, stderr))


class TestRunBashCheck:
    def test_passing_exit_zero(self):
        criterion = AcceptanceCriterion(
            type="bash",
            text="`npm run build` exits 0",
        )
        result = run_bash_check(criterion, runner=_stub_runner(rc=0, stdout="built"))
        assert result.passed is True
        assert "exit=0" in result.evidence

    def test_failing_exit_nonzero(self):
        criterion = AcceptanceCriterion(
            type="bash",
            text="`pytest -q` exits 0",
        )
        result = run_bash_check(criterion, runner=_stub_runner(rc=1, stderr="FAIL"))
        assert result.passed is False
        assert "exit=1" in result.evidence

    def test_negated_grep_passes_when_no_match(self):
        criterion = AcceptanceCriterion(
            type="bash",
            text="No `any` types: `! grep -rn ': any' src/`",
        )
        # grep returns 1 when no match; ! inverts to "pass"
        result = run_bash_check(criterion, runner=_stub_runner(rc=1))
        assert result.passed is True

    def test_negated_grep_fails_when_match_found(self):
        criterion = AcceptanceCriterion(
            type="bash",
            text="No `any` types: `! grep -rn ': any' src/`",
        )
        result = run_bash_check(criterion, runner=_stub_runner(rc=0, stdout=":any"))
        assert result.passed is False

    def test_output_equality(self):
        # v0.5.2a4 — Form A `==` was dropped; use Form B trailing
        # prose. `output == X` after the backticks.
        criterion = AcceptanceCriterion(
            type="bash",
            text="Exactly four files: `find src -type f | wc -l` output == 4",
        )
        result = run_bash_check(criterion, runner=_stub_runner(rc=0, stdout="4\n"))
        assert result.passed is True

    def test_output_equality_mismatch(self):
        criterion = AcceptanceCriterion(
            type="bash",
            text="`wc -l < src/main.py` output == 100",
        )
        result = run_bash_check(criterion, runner=_stub_runner(rc=0, stdout="50"))
        assert result.passed is False
        assert "expected='100'" in result.evidence

    def test_unparseable_criterion_errors(self):
        # No backticks → can't extract a command
        criterion = AcceptanceCriterion(type="bash", text="the build works")
        result = run_bash_check(criterion)
        assert result.passed is False
        assert "no parseable command" in result.error.lower()

    def test_wrong_type_errors(self):
        criterion = AcceptanceCriterion(type="chrome", text="`x`")
        result = run_bash_check(criterion)
        assert result.error
        assert "type='chrome'" in result.error

    def test_idempotency(self):
        # Same criterion + same stub → same result twice over.
        criterion = AcceptanceCriterion(type="bash", text="`echo 1` exits 0")
        runner = _stub_runner(rc=0, stdout="1\n")
        r1 = run_bash_check(criterion, runner=runner)
        r2 = run_bash_check(criterion, runner=runner)
        assert (r1.passed, r1.evidence) == (r2.passed, r2.evidence)

    def test_cwd_override_threads_through(self):
        captured = {}

        def fake_run(cmd, cwd=None, timeout=None):
            captured["cwd"] = cwd
            return 0, "", ""

        runner = BashRunner(_run=fake_run, cwd="/default")
        criterion = AcceptanceCriterion(type="bash", text="`echo x`")
        run_bash_check(criterion, runner=runner, cwd="/override")
        assert captured["cwd"] == "/override"


# ── VisionRunner ──────────────────────────────────────────────────────


class TestVisionRunnerYesNoParsing:
    """The model's response is free-form prose; we extract a yes/no
    verdict from the first word. Anything ambiguous defaults to No
    so the criterion does NOT pass spuriously."""

    def _runner_with_response(self, raw: str) -> VisionRunner:
        return VisionRunner(
            _call=lambda model, prompt, img: raw,
            _list_models=lambda: [DEFAULT_VISION_MODEL],
        )

    def test_yes_at_start_passes(self):
        r = self._runner_with_response("YES — the button is centered.")
        verdict, _ = r.ask(b"img", "centered?")
        assert verdict is True

    def test_lowercase_yes_passes(self):
        r = self._runner_with_response("yes, definitely centered")
        verdict, _ = r.ask(b"img", "centered?")
        assert verdict is True

    def test_no_at_start_fails(self):
        r = self._runner_with_response("NO — the button is left-aligned.")
        verdict, _ = r.ask(b"img", "centered?")
        assert verdict is False

    def test_ambiguous_response_fails_safely(self):
        # "Maybe..." doesn't start with YES → defaults to fail
        r = self._runner_with_response("Maybe? It's hard to tell.")
        verdict, _ = r.ask(b"img", "centered?")
        assert verdict is False

    def test_empty_response_fails(self):
        r = self._runner_with_response("")
        verdict, _ = r.ask(b"img", "centered?")
        assert verdict is False

    def test_yes_inside_word_does_not_count(self):
        # A response starting with "Yesterday" should NOT pass.
        r = self._runner_with_response("Yesterday I checked similar layouts.")
        verdict, _ = r.ask(b"img", "centered?")
        assert verdict is True  # "Yesterday" starts with "YES"
        # NOTE: this is intentional — the parser uses a prefix check,
        # not a whole-word match. The grill prompt tells the model to
        # answer with YES/NO as the literal first word; if it instead
        # writes "Yesterday" we accept the false positive rather than
        # fight regex edge cases. The cost is tiny because the grill
        # is the upstream defense. Pinning this so the behavior is
        # explicit and a future change requires a deliberate decision.


class TestVisionRunnerAvailability:
    def test_model_present_returns_true(self):
        r = VisionRunner(
            model="qwen2.5vl:7b",
            _list_models=lambda: ["qwen2.5vl:7b", "deepseek-v4-flash:cloud"],
        )
        assert r.is_available() is True

    def test_model_absent_returns_false(self):
        r = VisionRunner(
            model="qwen2.5vl:7b",
            _list_models=lambda: ["deepseek-v4-flash:cloud"],
        )
        assert r.is_available() is False

    def test_list_models_raising_returns_false(self):
        def boom() -> list[str]:
            raise RuntimeError("ollama unreachable")

        r = VisionRunner(_list_models=boom)
        assert r.is_available() is False


class TestVisionRunnerCallHookErrors:
    def test_call_raising_returns_false_with_error_evidence(self):
        def boom(model, prompt, img):
            raise RuntimeError("ollama timed out")

        r = VisionRunner(
            _call=boom,
            _list_models=lambda: [DEFAULT_VISION_MODEL],
        )
        verdict, raw = r.ask(b"img", "?")
        assert verdict is False
        assert "ollama timed out" in raw


# ── run_vision_check ──────────────────────────────────────────────────


class TestRunVisionCheck:
    def test_passes_when_model_says_yes(self):
        r = VisionRunner(
            _call=lambda m, p, i: "YES, perfectly centered.",
            _list_models=lambda: [DEFAULT_VISION_MODEL],
        )
        c = AcceptanceCriterion(type="vision", text="The button is centered horizontally")
        result = run_vision_check(c, b"\x89PNG...", runner=r)
        assert result.passed is True
        assert "verdict=YES" in result.evidence

    def test_fails_when_model_says_no(self):
        r = VisionRunner(
            _call=lambda m, p, i: "NO, button is left-aligned.",
            _list_models=lambda: [DEFAULT_VISION_MODEL],
        )
        c = AcceptanceCriterion(type="vision", text="centered?")
        result = run_vision_check(c, b"img", runner=r)
        assert result.passed is False

    def test_errors_when_model_unavailable(self):
        r = VisionRunner(_list_models=lambda: [])  # no vision model
        c = AcceptanceCriterion(type="vision", text="centered?")
        result = run_vision_check(c, b"img", runner=r)
        assert result.passed is False
        assert result.error
        assert "not available" in result.error.lower()

    def test_errors_on_empty_image(self):
        r = VisionRunner(
            _call=lambda m, p, i: "YES",
            _list_models=lambda: [DEFAULT_VISION_MODEL],
        )
        c = AcceptanceCriterion(type="vision", text="?")
        result = run_vision_check(c, b"", runner=r)
        assert result.error
        assert "empty image" in result.error.lower()

    def test_evidence_contains_model_name(self):
        r = VisionRunner(
            model="custom-vl",
            _call=lambda m, p, i: "YES",
            _list_models=lambda: ["custom-vl"],
        )
        c = AcceptanceCriterion(type="vision", text="?")
        result = run_vision_check(c, b"img", runner=r)
        assert "custom-vl" in result.evidence

    def test_long_response_truncated_in_evidence(self):
        long_response = "YES " + "x" * 1000
        r = VisionRunner(
            _call=lambda m, p, i: long_response,
            _list_models=lambda: [DEFAULT_VISION_MODEL],
        )
        c = AcceptanceCriterion(type="vision", text="?")
        result = run_vision_check(c, b"img", runner=r)
        assert "..." in result.evidence
        assert len(result.evidence) < 500

    def test_wrong_type_errors(self):
        c = AcceptanceCriterion(type="bash", text="`x`")
        result = run_vision_check(c, b"img")
        assert result.error
        assert "type='bash'" in result.error


# ── dispatch() — top-level routing ───────────────────────────────────


class TestDispatch:
    def test_manual_returns_skip(self):
        c = AcceptanceCriterion(type="manual", text="logo looks ok")
        result = dispatch(c)
        assert result.skipped is True
        assert result.passed is False  # not a "pass" — it's a "skip"
        assert "manual" in result.evidence.lower()

    def test_chrome_returns_delegate_to_model(self):
        c = AcceptanceCriterion(type="chrome", text="click #x; assert y")
        result = dispatch(c)
        assert result.skipped is True
        assert "delegate_to_model" in result.evidence

    def test_bash_runs_through(self):
        c = AcceptanceCriterion(type="bash", text="`pytest -q` exits 0")
        ctx = CheckContext(bash_runner=_stub_runner(rc=0, stdout="ok"))
        result = dispatch(c, ctx)
        assert result.passed is True
        assert result.skipped is False

    def test_vision_without_image_provider_errors(self):
        c = AcceptanceCriterion(type="vision", text="centered?")
        result = dispatch(c, CheckContext())
        assert result.error
        assert "image_provider" in result.error.lower()

    def test_vision_with_image_provider_runs_through(self):
        # Stub vision model returns YES + claims to be available
        runner = VisionRunner(
            _call=lambda m, p, i: "YES centered",
            _list_models=lambda: [DEFAULT_VISION_MODEL],
        )
        c = AcceptanceCriterion(type="vision", text="centered?")
        ctx = CheckContext(
            vision_runner=runner,
            image_provider=lambda: b"\x89PNG\x00\x00",
        )
        result = dispatch(c, ctx)
        assert result.passed is True

    def test_vision_image_provider_returning_none_errors(self):
        c = AcceptanceCriterion(type="vision", text="?")
        ctx = CheckContext(image_provider=lambda: None)
        result = dispatch(c, ctx)
        assert result.error
        assert "empty bytes" in result.error.lower()

    def test_vision_image_provider_raising_caught(self):
        def boom() -> bytes:
            raise IOError("screenshot failed")

        c = AcceptanceCriterion(type="vision", text="?")
        ctx = CheckContext(image_provider=boom)
        result = dispatch(c, ctx)
        assert result.error
        assert "image_provider raised" in result.error
        assert "screenshot failed" in result.error

    def test_unknown_type_raises_via_construction_not_dispatch(self):
        # AcceptanceCriterion's __post_init__ already rejects unknown
        # types, so dispatch can never see one. Pin that.
        with pytest.raises(ValueError):
            AcceptanceCriterion(type="hologram", text="?")


# ── summarize_for_roadmap ─────────────────────────────────────────────


class TestSummarizeForRoadmap:
    def test_pass_format(self):
        r = CheckResult(passed=True, evidence="exit=0")
        assert summarize_for_roadmap(r).startswith("PASS:")

    def test_fail_format(self):
        r = CheckResult(passed=False, evidence="exit=1")
        assert summarize_for_roadmap(r).startswith("FAIL:")

    def test_error_takes_precedence_over_passed(self):
        r = CheckResult(passed=True, error="vision unreachable")
        # If error is set, that's the dominant state — reporting
        # passed=True alongside an error would be misleading.
        assert summarize_for_roadmap(r).startswith("ERROR:")

    def test_skip_format(self):
        r = CheckResult.skip_manual()
        assert summarize_for_roadmap(r).startswith("SKIP:")

    def test_delegate_to_model_renders_as_skip(self):
        # [chrome] criteria use this sentinel; from the roadmap's
        # POV the deterministic dispatch was a skip.
        r = CheckResult.delegate_to_model("model drives")
        assert summarize_for_roadmap(r).startswith("SKIP:")
        assert "delegate" in summarize_for_roadmap(r).lower()


# ── Integration: roadmap update via dispatch result ──────────────────


class TestRoadmapIntegration:
    """Confirm the dispatch result shape composes cleanly with the
    `roadmap.update_criterion` mutation helper. This is the contract
    REFLECT relies on: dispatch → use `result.passed` and the
    summarized evidence to mark the roadmap."""

    def test_pass_result_marks_criterion_passed(self):
        from resonant_client.gui.roadmap import (
            Roadmap,
            update_criterion,
        )

        rm = Roadmap()
        rm.acceptance_criteria.append(
            AcceptanceCriterion(type="bash", text="`pytest -q` exits 0")
        )
        ctx = CheckContext(bash_runner=_stub_runner(rc=0, stdout="ok"))
        result = dispatch(rm.acceptance_criteria[0], ctx)

        ok = update_criterion(
            rm,
            text_match="`pytest -q` exits 0",
            passed=result.passed,
            evidence=summarize_for_roadmap(result),
        )
        assert ok is True
        assert rm.acceptance_criteria[0].passed is True
        assert rm.acceptance_criteria[0].evidence.startswith("PASS:")

    def test_fail_result_marks_criterion_failed(self):
        from resonant_client.gui.roadmap import (
            Roadmap,
            update_criterion,
        )

        rm = Roadmap()
        rm.acceptance_criteria.append(
            AcceptanceCriterion(type="bash", text="`pytest -q` exits 0")
        )
        ctx = CheckContext(bash_runner=_stub_runner(rc=1, stderr="boom"))
        result = dispatch(rm.acceptance_criteria[0], ctx)

        update_criterion(
            rm,
            text_match="`pytest -q` exits 0",
            passed=result.passed,
            evidence=summarize_for_roadmap(result),
        )
        assert rm.acceptance_criteria[0].passed is False
        assert rm.acceptance_criteria[0].evidence.startswith("FAIL:")
