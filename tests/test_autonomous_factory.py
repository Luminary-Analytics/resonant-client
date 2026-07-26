"""Tests for `gui/autonomous_factory.py` — production wiring helpers
for the autonomous mission daemon.

The factory has two flavors of code:
1. **Pure helpers** — `build_reflect_goal`, `parse_reflect_verdict`,
   `_last_balanced_json_block`, the dispatch tracker. Easy to test
   directly without a backend.
2. **Wrappers around external systems** — `make_git_*`, `make_reflect_runner`,
   `build_autonomous_mission_hooks`. These need either real subprocesses
   or stubs.

These tests cover (1) directly and (2) via stubs. The full live
end-to-end is a separate smoke test (see `docs/v0.5.0-smoke-plan.md`).
"""
from __future__ import annotations

import threading
import time


from resonant_client.gui.autonomous_factory import (
    DispatchTracker,
    build_reflect_goal,
    make_check_context_factory,
    parse_reflect_verdict,
    _last_balanced_json_block,
)
from resonant_client.gui.roadmap import (
    AcceptanceCriterion,
    Roadmap,
)
from resonant_client.orchestration.reflect import ReflectPassResult


# ── DispatchTracker ─────────────────────────────────────────────────────


class TestDispatchTracker:
    def test_watch_then_complete_returns_success(self):
        tracker = DispatchTracker()
        tracker.watch("intent-1")
        tracker.feed_event({
            "event": "intent.complete",
            "intent_id": "intent-1",
        })
        outcome = tracker.wait("intent-1", poll_seconds=0.05)
        assert outcome.success is True
        assert outcome.handle == "intent-1"

    def test_watch_then_fail_returns_failure(self):
        tracker = DispatchTracker()
        tracker.watch("intent-1")
        tracker.feed_event({
            "event": "intent.failed",
            "intent_id": "intent-1",
            "error": "implementer crashed",
        })
        outcome = tracker.wait("intent-1", poll_seconds=0.05)
        assert outcome.success is False
        assert "implementer crashed" in outcome.error

    def test_watch_then_cancelled_returns_failure(self):
        tracker = DispatchTracker()
        tracker.watch("intent-1")
        tracker.feed_event({
            "event": "intent.cancelled",
            "intent_id": "intent-1",
        })
        outcome = tracker.wait("intent-1", poll_seconds=0.05)
        assert outcome.success is False
        assert "cancelled" in outcome.error

    def test_unwatched_intent_id_returns_descriptive_error(self):
        tracker = DispatchTracker()
        outcome = tracker.wait("never-watched", poll_seconds=0.05)
        assert outcome.success is False
        assert "not watched" in outcome.error

    def test_stop_event_unblocks_wait(self):
        tracker = DispatchTracker()
        tracker.watch("intent-1")
        stop = threading.Event()

        # Set stop after a brief delay; tracker.wait should return
        # within ~poll_seconds + a tick.
        def fire():
            time.sleep(0.05)
            stop.set()

        threading.Thread(target=fire, daemon=True).start()
        started = time.time()
        outcome = tracker.wait(
            "intent-1", stop_event=stop, poll_seconds=0.05,
        )
        elapsed = time.time() - started
        assert outcome.success is False
        assert "stop_event" in outcome.error
        assert elapsed < 1.0  # didn't hang

    def test_non_terminal_events_are_ignored(self):
        tracker = DispatchTracker()
        tracker.watch("intent-1")
        # plan.event, intent.started, etc. are all non-terminal.
        tracker.feed_event({
            "event": "plan.event",
            "intent_id": "intent-1",
        })
        tracker.feed_event({
            "event": "intent.started",
            "intent_id": "intent-1",
        })
        # Wait should still timeout (no terminal event yet).
        stop = threading.Event()

        def fire():
            time.sleep(0.05)
            stop.set()

        threading.Thread(target=fire, daemon=True).start()
        outcome = tracker.wait(
            "intent-1", stop_event=stop, poll_seconds=0.05,
        )
        assert "stop_event" in outcome.error

    def test_event_without_intent_id_is_safe(self):
        tracker = DispatchTracker()
        # Doesn't raise.
        tracker.feed_event({"event": "intent.complete"})
        tracker.feed_event({})

    def test_forget_drops_state(self):
        tracker = DispatchTracker()
        tracker.watch("intent-1")
        tracker.feed_event({
            "event": "intent.complete",
            "intent_id": "intent-1",
        })
        tracker.forget("intent-1")
        # After forget, looking up returns the unwatched-error.
        outcome = tracker.wait("intent-1", poll_seconds=0.01)
        assert "not watched" in outcome.error


# ── build_reflect_goal ──────────────────────────────────────────────────


def _build_test_roadmap(criteria: list[tuple[str, str, bool]]) -> Roadmap:
    """`criteria` is `(type, text, passed_or_pending)`. passed_or_pending
    True → passed, False → pending (passed=None)."""
    rm = Roadmap()
    for ctype, text, settled in criteria:
        c = AcceptanceCriterion(type=ctype, text=text)
        if settled:
            c.passed = True
            c.evidence = "PASS: stub"
        rm.acceptance_criteria.append(c)
    return rm


class TestBuildReflectGoal:
    def test_includes_mode_full(self):
        rm = _build_test_roadmap([("bash", "x", True)])
        result = ReflectPassResult(converged=True)
        goal = build_reflect_goal(rm, result)
        assert "mode: full" in goal

    def test_includes_roadmap_path_when_provided(self):
        rm = _build_test_roadmap([("bash", "x", True)])
        result = ReflectPassResult()
        goal = build_reflect_goal(
            rm, result, roadmap_path="/tmp/roadmap.md",
        )
        assert "/tmp/roadmap.md" in goal

    def test_lists_each_criterion_with_status(self):
        rm = _build_test_roadmap([
            ("bash", "first crit", True),
            ("chrome", "second crit", False),
        ])
        # Mark second as failed in-place
        rm.acceptance_criteria[1].passed = False
        rm.acceptance_criteria[1].evidence = "FAIL: button missing"

        result = ReflectPassResult(chrome_pending=[rm.acceptance_criteria[1]])
        goal = build_reflect_goal(rm, result)

        assert "first crit" in goal
        assert "PASS" in goal
        assert "second crit" in goal
        assert "FAIL" in goal

    def test_chrome_pending_section_appears_when_present(self):
        chrome_c = AcceptanceCriterion(type="chrome", text="click button")
        rm = Roadmap(acceptance_criteria=[chrome_c])
        result = ReflectPassResult(chrome_pending=[chrome_c])
        goal = build_reflect_goal(rm, result)
        assert "[chrome]" in goal
        assert "click button" in goal
        assert "mcp_browseros_" in goal

    def test_chrome_pending_section_omitted_when_empty(self):
        rm = _build_test_roadmap([("bash", "x", True)])
        result = ReflectPassResult()
        goal = build_reflect_goal(rm, result)
        assert "you need to validate" not in goal

    def test_manual_pending_listed_for_handoff(self):
        manual_c = AcceptanceCriterion(type="manual", text="eyeball it")
        rm = Roadmap(acceptance_criteria=[manual_c])
        result = ReflectPassResult(manual_pending=[manual_c])
        goal = build_reflect_goal(rm, result)
        assert "[manual]" in goal
        assert "handoff" in goal.lower()
        assert "eyeball it" in goal

    def test_includes_tally(self):
        rm = _build_test_roadmap([
            ("bash", "a", True),
            ("bash", "b", True),
            ("chrome", "c", False),
        ])
        result = ReflectPassResult(
            chrome_pending=[rm.acceptance_criteria[2]],
        )
        goal = build_reflect_goal(rm, result)
        # Two of three blocking pass → "2/3"
        assert "2/3" in goal

    def test_reminds_model_about_cross_check(self):
        rm = _build_test_roadmap([("bash", "x", True)])
        result = ReflectPassResult()
        goal = build_reflect_goal(rm, result)
        # The cross-check warning helps the model understand why
        # claiming `satisfied` falsely is futile.
        assert "cross-check" in goal.lower() or "override" in goal.lower()


# ── parse_reflect_verdict ───────────────────────────────────────────────


class TestParseReflectVerdict:
    def test_fenced_json_extracted_cleanly(self):
        text = """
Some preamble.

```json
{
  "verdict": "satisfied",
  "completed": [{"id": "T1.1", "commit_sha": "abc123", "note": "ok"}],
  "chrome_results": [],
  "added": [],
  "blocked": [],
  "manual_pending": [],
  "summary": "all done",
  "estimated_remaining_minutes": 0
}
```
"""
        parsed = parse_reflect_verdict(text)
        assert parsed["verdict"] == "satisfied"
        assert parsed["summary"] == "all done"
        assert len(parsed["completed"]) == 1

    def test_unfenced_json_at_end_extracted(self):
        text = """
Final summary: everything looks good.

{
  "verdict": "satisfied",
  "summary": "all checks passed"
}
"""
        parsed = parse_reflect_verdict(text)
        assert parsed["verdict"] == "satisfied"
        assert parsed["summary"] == "all checks passed"

    def test_missing_keys_get_safe_defaults(self):
        text = '```json\n{"verdict": "continue"}\n```'
        parsed = parse_reflect_verdict(text)
        assert parsed["verdict"] == "continue"
        # Daemon expects every key present.
        assert parsed["completed"] == []
        assert parsed["chrome_results"] == []
        assert parsed["added"] == []
        assert parsed["blocked"] == []
        assert parsed["manual_pending"] == []
        assert parsed["summary"] == ""
        assert parsed["estimated_remaining_minutes"] == 0

    def test_malformed_json_returns_continue_with_error(self):
        text = "```json\n{verdict: satisfied,}\n```"
        parsed = parse_reflect_verdict(text)
        assert parsed["verdict"] == "continue"
        assert "_parse_error" in parsed

    def test_no_json_block_returns_continue_with_error(self):
        text = "I forgot to emit the JSON. Sorry!"
        parsed = parse_reflect_verdict(text)
        assert parsed["verdict"] == "continue"
        assert "_parse_error" in parsed
        assert "no JSON block" in parsed["_parse_error"]

    def test_empty_response_returns_continue_with_error(self):
        parsed = parse_reflect_verdict("")
        assert parsed["verdict"] == "continue"
        assert "_parse_error" in parsed

    def test_root_array_rejected(self):
        # JSON must be an object, not a list.
        text = "```json\n[1,2,3]\n```"
        parsed = parse_reflect_verdict(text)
        assert parsed["verdict"] == "continue"
        assert "_parse_error" in parsed

    def test_picks_last_balanced_block(self):
        # Multiple braces — should pick the LAST balanced one.
        text = """
First sketch: { incomplete

Final answer:
{
  "verdict": "blocked",
  "summary": "stuck on chrome"
}
"""
        parsed = parse_reflect_verdict(text)
        assert parsed["verdict"] == "blocked"

    def test_handles_strings_with_braces(self):
        # Strings containing braces should NOT confuse the parser.
        text = """
```json
{
  "verdict": "satisfied",
  "summary": "tested with {curly braces} in evidence"
}
```
"""
        parsed = parse_reflect_verdict(text)
        assert parsed["verdict"] == "satisfied"
        assert "curly braces" in parsed["summary"]


class TestLastBalancedJsonBlock:
    def test_simple_object(self):
        assert _last_balanced_json_block('text {"a": 1}') == '{"a": 1}'

    def test_picks_last_when_multiple(self):
        # Two complete objects in sequence; we want the last.
        assert _last_balanced_json_block(
            '{"first": 1} between {"last": 2}'
        ) == '{"last": 2}'

    def test_nested_braces(self):
        block = _last_balanced_json_block(
            'text {"outer": {"inner": 1}, "x": 2}'
        )
        assert block == '{"outer": {"inner": 1}, "x": 2}'

    def test_no_braces_returns_none(self):
        assert _last_balanced_json_block("plain text") is None

    def test_unbalanced_returns_none(self):
        assert _last_balanced_json_block("{ no close") is None

    def test_braces_inside_string_dont_count(self):
        block = _last_balanced_json_block('{"text": "has {brace} inside"}')
        assert block == '{"text": "has {brace} inside"}'


# ── make_check_context_factory ──────────────────────────────────────────


class TestCheckContextFactory:
    def test_factory_returns_callable_producing_context(self):
        factory = make_check_context_factory(project_path="/tmp/proj")
        ctx = factory(Roadmap())
        assert ctx.project_path == "/tmp/proj"
        assert ctx.bash_runner is not None
        assert ctx.vision_runner is not None

    def test_image_provider_is_passed_through(self):
        called = []

        def provider() -> bytes:
            called.append(True)
            return b"bytes"

        factory = make_check_context_factory(
            project_path="/tmp", image_provider=provider,
        )
        ctx = factory(Roadmap())
        assert ctx.image_provider is provider
        # And it actually works when called.
        assert ctx.image_provider() == b"bytes"

    def test_settings_object_is_introspected_for_vision_model(self):
        # Inline duck-typed settings shape.
        class _Vision:
            default_model = "llava-next:13b"

        class _Settings:
            vision = _Vision()
            network = None

        factory = make_check_context_factory(
            project_path="/tmp", settings=_Settings(),
        )
        ctx = factory(Roadmap())
        assert ctx.vision_runner.model == "llava-next:13b"

    def test_settings_introspection_failure_falls_back_to_defaults(self):
        # A weird settings object that raises on attribute access
        # shouldn't crash the factory.
        class _Bad:
            @property
            def vision(self):
                raise RuntimeError("nope")

            @property
            def network(self):
                raise RuntimeError("nope")

        factory = make_check_context_factory(
            project_path="/tmp", settings=_Bad(),
        )
        ctx = factory(Roadmap())
        # Defaults apply — track DEFAULT_VISION_MODEL so a future
        # bump (qwen3-vl → qwen4-vl, whenever that lands) doesn't
        # require touching this test.
        from resonant_client.orchestration.acceptance_check import (
            DEFAULT_VISION_MODEL,
        )
        assert ctx.vision_runner.model == DEFAULT_VISION_MODEL
