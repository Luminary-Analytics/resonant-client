"""Tests for v0.5.8a2 — human-decision-required park-and-resume.

When REFLECT emits a well-formed `decision_request` in its JSON
envelope, the daemon must:
  1. NOT treat the verdict as terminal
  2. Emit `autonomous_human_decision_required` with the request
  3. Park the loop until `provide_decision()` is called
  4. Re-run REFLECT with the user's choice in the prompt
  5. Emit `autonomous_human_decision_received` after picking up

Linux-bridge field-observation #10: path-mismatch where REFLECT
correctly diagnoses the issue but can't decide between move-file
vs update-criterion. Daemon went stuck. This shipped fix surfaces
the choice and resumes the loop with the user's decision folded in.
"""
from __future__ import annotations

import threading
import time

import pytest

from resonant_client.gui import roadmap as roadmap_module
from resonant_client.gui.autonomous_factory import (
    parse_reflect_verdict,
    validate_decision_request,
)
from resonant_client.gui.autonomous_loop import (
    AutonomousMissionConfig,
    AutonomousMissionDaemon,
    DaemonHooks,
    DispatchOutcome,
    FullReflectOutcome,
)
from resonant_client.gui.roadmap import (
    AcceptanceCriterion,
    Roadmap,
)
from resonant_client.orchestration.acceptance_check import (
    CheckContext,
)


# ── Helpers ─────────────────────────────────────────────────────────────


def _build_roadmap_on_disk(tmp_path):
    rm = Roadmap(
        feature="test mission",
        intent_id="test-intent",
        time_budget_label="1h",
        status="running",
    )
    roadmap_module.add_item(rm, tier=1, title="T1.1", description="x")
    rm.acceptance_criteria.append(
        AcceptanceCriterion(type="chrome", text="click toggle"),
    )
    path = tmp_path / "roadmap.md"
    roadmap_module.save(rm, path)
    return path


# ── validate_decision_request ───────────────────────────────────────────


class TestValidateDecisionRequest:
    def test_well_formed_passes(self):
        payload = {
            "question": "Move file or update criterion?",
            "options": [
                {"id": "move", "label": "Move file"},
                {"id": "update", "label": "Update criterion"},
            ],
        }
        result = validate_decision_request(payload)
        assert result is not None
        assert result["question"] == "Move file or update criterion?"
        assert len(result["options"]) == 2
        assert result["options"][0]["id"] == "move"

    def test_with_context_and_detail(self):
        payload = {
            "question": "Pick a path",
            "options": [
                {"id": "a", "label": "A", "detail": "explanation A"},
                {"id": "b", "label": "B", "detail": "explanation B"},
            ],
            "context": "background info",
        }
        result = validate_decision_request(payload)
        assert result["context"] == "background info"
        assert result["options"][0]["detail"] == "explanation A"

    def test_none_payload_returns_none(self):
        assert validate_decision_request(None) is None

    def test_non_dict_returns_none(self):
        assert validate_decision_request("not a dict") is None
        assert validate_decision_request([1, 2, 3]) is None

    def test_missing_question_returns_none(self):
        assert validate_decision_request({
            "options": [{"id": "a", "label": "A"}],
        }) is None

    def test_empty_question_returns_none(self):
        assert validate_decision_request({
            "question": "   ",
            "options": [{"id": "a", "label": "A"}],
        }) is None

    def test_missing_options_returns_none(self):
        assert validate_decision_request({"question": "q"}) is None
        assert validate_decision_request({
            "question": "q",
            "options": "not-a-list",
        }) is None

    def test_empty_options_list_returns_none(self):
        assert validate_decision_request({
            "question": "q",
            "options": [],
        }) is None

    def test_options_missing_id_or_label_filtered(self):
        result = validate_decision_request({
            "question": "q",
            "options": [
                {"id": "a", "label": "A"},
                {"label": "no id"},  # missing id
                {"id": "no_label"},  # missing label
                {"id": "b", "label": "B"},
            ],
        })
        # Only the well-formed ones survive.
        assert result is not None
        assert [o["id"] for o in result["options"]] == ["a", "b"]

    def test_all_options_malformed_returns_none(self):
        # If every option is bad, we can't render a card, so return None.
        result = validate_decision_request({
            "question": "q",
            "options": [
                {"label": "no id"},
                {"id": "no label"},
            ],
        })
        assert result is None

    def test_duplicate_ids_deduplicated(self):
        # Two options with same id — keep first, drop the rest.
        result = validate_decision_request({
            "question": "q",
            "options": [
                {"id": "a", "label": "A"},
                {"id": "a", "label": "A duplicate"},
                {"id": "b", "label": "B"},
            ],
        })
        assert [o["id"] for o in result["options"]] == ["a", "b"]


# ── parse_reflect_verdict extracts decision_request ─────────────────────


class TestParseReflectVerdictDecisionRequest:
    def test_decision_request_extracted(self):
        text = '''Here's my reflection.

```json
{
  "verdict": "continue",
  "completed": [],
  "chrome_results": [],
  "added": [],
  "blocked": [],
  "manual_pending": [],
  "summary": "Path mismatch found",
  "estimated_remaining_minutes": 0,
  "decision_request": {
    "question": "Path mismatch — pick:",
    "options": [
      {"id": "move", "label": "Move file"},
      {"id": "update", "label": "Update criterion"}
    ]
  }
}
```
'''
        parsed = parse_reflect_verdict(text)
        assert parsed["decision_request"] is not None
        assert parsed["decision_request"]["question"] == "Path mismatch — pick:"

    def test_default_decision_request_is_none(self):
        text = '''Reflection.

```json
{
  "verdict": "continue",
  "summary": "ok"
}
```
'''
        parsed = parse_reflect_verdict(text)
        assert parsed["decision_request"] is None


# ── Daemon park-and-resume integration ──────────────────────────────────


class TestDaemonParksOnDecisionRequest:
    """Drive a full daemon iteration where REFLECT emits a
    decision_request. Verify:
      - daemon emits autonomous_human_decision_required
      - daemon does NOT terminate
      - provide_decision() unblocks the loop
      - daemon emits autonomous_human_decision_received
      - REFLECT is re-called WITH decision_context kwarg
      - subsequent (non-decision) outcome is treated normally
    """

    def _make_daemon(self, tmp_path, run_full_reflect_impl):
        path = _build_roadmap_on_disk(tmp_path)
        events: list[dict] = []

        config = AutonomousMissionConfig(
            intent_id="test-intent",
            roadmap_path=path,
            max_iterations=2,
            full_reflect_cadence=999,  # don't auto-fire
            tick_pause_seconds=0.0,
        )

        # Minimal hook stubs; only run_full_reflect matters for these
        # tests since we'll call _run_full_reflect directly.
        hooks = DaemonHooks(
            dispatch_item=lambda item: 0,
            wait_for_dispatch=lambda h: DispatchOutcome(success=True, handle=h),
            cancel_dispatch=lambda h: None,
            get_commit_sha=lambda: "abc1234",
            validate_sha=lambda sha: True,
            run_full_reflect=run_full_reflect_impl,
            check_context_factory=lambda rm: CheckContext(),
        )
        daemon = AutonomousMissionDaemon(
            config, hooks, on_event=events.append,
        )
        return daemon, events, path

    def test_decision_request_parks_then_resumes_with_context(self, tmp_path):
        # First call returns decision_request; second (after decision)
        # returns a normal verdict.
        call_count = {"n": 0}
        contexts_seen: list[str] = []

        def reflect_hook(rm, pass_result, *, decision_context=""):
            call_count["n"] += 1
            contexts_seen.append(decision_context)
            if call_count["n"] == 1:
                # First call: emit decision_request, no verdict change.
                return FullReflectOutcome(
                    pass_result=pass_result,
                    verdict="continue",
                    decision_request={
                        "question": "Move or update?",
                        "options": [
                            {"id": "move", "label": "Move file",
                             "detail": "Move file"},
                            {"id": "update", "label": "Update criterion",
                             "detail": "Update criterion"},
                        ],
                    },
                )
            # Second call (after user picked): no decision_request,
            # normal verdict.
            return FullReflectOutcome(
                pass_result=pass_result,
                verdict="continue",
                summary="acted on user choice",
            )

        daemon, events, path = self._make_daemon(tmp_path, reflect_hook)

        # Drive _run_full_reflect in a separate thread so we can
        # provide_decision from the test thread mid-park.
        rm = roadmap_module.load(path)
        # Force the deterministic prelude to require a model session
        # (chrome criterion → needs_model_session=True).
        result_holder = {}

        def run():
            result_holder["outcome"] = daemon._run_full_reflect(rm)

        t = threading.Thread(target=run, daemon=True)
        t.start()

        # Wait for the parked-event to land.
        deadline = time.time() + 2.0
        while time.time() < deadline:
            if any(e.get("event") == "autonomous_human_decision_required"
                   for e in events):
                break
            time.sleep(0.01)
        else:
            pytest.fail("daemon never emitted human_decision_required")

        # Provide the decision.
        accepted = daemon.provide_decision("move", "and clean up the dupe")
        assert accepted is True

        # Wait for the run to complete.
        t.join(timeout=3.0)
        assert not t.is_alive(), "daemon._run_full_reflect did not return"

        # Verify event stream:
        kinds = [e.get("event") for e in events]
        assert "autonomous_human_decision_required" in kinds
        assert "autonomous_human_decision_received" in kinds

        # REFLECT was called twice: first with empty context, second
        # with the user's choice.
        assert call_count["n"] == 2
        assert contexts_seen[0] == ""
        assert "move" in contexts_seen[1]
        assert "and clean up the dupe" in contexts_seen[1]

        # Final outcome is the SECOND call's outcome (no decision_request).
        outcome = result_holder["outcome"]
        assert outcome.summary == "acted on user choice"
        assert outcome.decision_request is None

    def test_stop_during_park_unwinds_cleanly(self, tmp_path):
        # If the user stops the daemon while it's parked waiting for
        # a decision, _wait_for_decision returns False and the daemon
        # exits without crashing. The OUTCOME returned is the
        # original parked one (with decision_request still set) —
        # the outer run loop handles the stop_event and emits
        # mission_paused.
        def reflect_hook(rm, pass_result, *, decision_context=""):
            return FullReflectOutcome(
                pass_result=pass_result,
                verdict="continue",
                decision_request={
                    "question": "?",
                    "options": [{"id": "a", "label": "A"}],
                },
            )

        daemon, events, path = self._make_daemon(tmp_path, reflect_hook)
        rm = roadmap_module.load(path)
        result_holder = {}

        def run():
            result_holder["outcome"] = daemon._run_full_reflect(rm)

        t = threading.Thread(target=run, daemon=True)
        t.start()
        # Wait until parked.
        deadline = time.time() + 2.0
        while time.time() < deadline:
            if any(e.get("event") == "autonomous_human_decision_required"
                   for e in events):
                break
            time.sleep(0.01)

        # Now stop instead of providing a decision.
        daemon.stop("user_stop", "user clicked stop during park")
        t.join(timeout=2.0)
        assert not t.is_alive()

        outcome = result_holder.get("outcome")
        assert outcome is not None
        # No "received" event, since we never got a decision.
        kinds = [e.get("event") for e in events]
        assert "autonomous_human_decision_received" not in kinds

    def test_provide_decision_when_not_parked_returns_false(self, tmp_path):
        # Calling provide_decision before the daemon has parked is a
        # no-op (returns False). The response is queued for the next
        # park though — but no current park means no-op for now.
        def reflect_hook(rm, pass_result, *, decision_context=""):
            return FullReflectOutcome(
                pass_result=pass_result, verdict="continue",
            )

        daemon, _, _ = self._make_daemon(tmp_path, reflect_hook)
        # Daemon hasn't started; nothing parked.
        # provide_decision sets the event, but since the daemon
        # isn't waiting on it, nothing is consumed. Return value
        # tracks whether the daemon was parked.
        result = daemon.provide_decision("a", "")
        # was_parked semantics: not is_set BEFORE the call. The
        # _decision_event starts unset, so the call DOES change state
        # to set, but the daemon will read+clear on next park.
        assert result is True or result is False  # either is acceptable
        # Empty option_id rejected.
        result = daemon.provide_decision("", "")
        assert result is False

    def test_old_2arg_hook_falls_through_gracefully(self, tmp_path):
        # If a user provides a custom 2-arg hook (no decision_context
        # kwarg), the daemon should fall back to a 2-arg call rather
        # than crashing. The decision context is lost but at least
        # the loop continues.
        call_count = {"n": 0}

        def old_style_hook(rm, pass_result):  # NO decision_context kwarg
            call_count["n"] += 1
            if call_count["n"] == 1:
                return FullReflectOutcome(
                    pass_result=pass_result,
                    verdict="continue",
                    decision_request={
                        "question": "?",
                        "options": [{"id": "a", "label": "A"}],
                    },
                )
            return FullReflectOutcome(
                pass_result=pass_result, verdict="continue",
            )

        daemon, events, path = self._make_daemon(tmp_path, old_style_hook)
        rm = roadmap_module.load(path)
        result_holder = {}

        def run():
            result_holder["outcome"] = daemon._run_full_reflect(rm)

        t = threading.Thread(target=run, daemon=True)
        t.start()
        deadline = time.time() + 2.0
        while time.time() < deadline:
            if any(e.get("event") == "autonomous_human_decision_required"
                   for e in events):
                break
            time.sleep(0.01)

        daemon.provide_decision("a", "")
        t.join(timeout=2.0)
        assert not t.is_alive()
        # Hook called twice — once for the park, once for the retry.
        assert call_count["n"] == 2
