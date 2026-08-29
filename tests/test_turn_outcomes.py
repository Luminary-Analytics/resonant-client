from resonant_client.engine.turn_outcomes import (
    classify_turn_outcome,
    request_requires_workspace_change,
    response_promises_future_action,
)


def test_change_request_and_action_promise_detection():
    assert request_requires_workspace_change("awesome can you rewrite it")
    assert not request_requires_workspace_change("summarize this codebase")
    assert not request_requires_workspace_change(
        "Do not change files. What improvement should this project make next?"
    )
    assert not request_requires_workspace_change(
        "Review the parser without editing the workspace."
    )
    assert response_promises_future_action(
        "Let me rewrite it cleanly, then run it to verify."
    )
    assert not response_promises_future_action("The implementation is already correct.")


def test_outcome_requires_change_and_validation_evidence():
    common = {"user_request": "fix the parser", "assistant_text": "Done."}
    assert classify_turn_outcome(**common) == "incomplete"
    assert classify_turn_outcome(**common, changed_files=["parser.py"]) == "changed_unverified"
    assert classify_turn_outcome(
        **common,
        changed_files=["parser.py"],
        validation_tools=["bash"],
    ) == "changed_verified"


def test_answer_no_change_and_failure_outcomes():
    assert classify_turn_outcome(
        user_request="explain this module",
        assistant_text="It parses JSON.",
    ) == "answered"
    assert classify_turn_outcome(
        user_request="rewrite the module",
        assistant_text="It is already implemented; no changes are needed.",
    ) == "no_changes_needed"
    assert classify_turn_outcome(
        user_request="hello",
        assistant_text="",
        terminal_error="empty response",
    ) == "failed"
