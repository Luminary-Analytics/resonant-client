"""The provider requires tool results to *follow* their call, not merely exist.

OpenAI-compatible providers reject a request when an assistant message
declaring `tool_calls` is not immediately followed by the matching `tool`
messages:

    Invalid request: an assistant message with 'tool_calls' must be followed
    by tool messages responding to each 'tool_call_id'

`await_user` produces that shape in normal use. The agent announces a tool
call, pauses to ask the user something, and the user's reply is appended before
the tool result arrives — so every call is answered, just not adjacently. The
original repair only checked that a response existed anywhere in the history,
so it saw nothing wrong and the turn failed anyway. Because history is
replayed, the session then failed identically forever.
"""

import pytest

from resonant_client.backends import KimiBackend


def _call(call_id, name="browser_navigate"):
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": "{}"},
    }


def _violations(messages):
    """Assistant tool_calls whose results do not immediately follow, in order."""
    bad = []
    for i, message in enumerate(messages):
        if message.get("role") != "assistant" or not message.get("tool_calls"):
            continue
        expected = [c["id"] for c in message["tool_calls"]]
        window = messages[i + 1: i + 1 + len(expected)]
        actual = [m.get("tool_call_id") for m in window if m.get("role") == "tool"]
        if actual != expected:
            bad.append((expected, actual))
    return bad


def test_await_user_reply_between_call_and_result_is_repaired():
    """The exact shape seen when the agent asks the user to sign in."""
    history = [
        {"role": "user", "content": "open chrome and check gmail"},
        {"role": "assistant", "content": "", "tool_calls": [_call("c1")]},
        {"role": "user", "content": "I'll sign in myself — wait, then continue"},
        {"role": "tool", "tool_call_id": "c1", "content": "navigated to gmail"},
    ]

    repaired = KimiBackend._repair_orphaned_tool_calls(history)

    assert _violations(repaired) == []
    # The real result must be relocated, never replaced by a synthetic one:
    # telling the model its browser call was "interrupted" when it succeeded
    # would make it redo work it already did.
    contents = [m.get("content") for m in repaired if m.get("role") == "tool"]
    assert contents == ["navigated to gmail"]
    # The user's reply must survive.
    assert any(m.get("content", "").startswith("I'll sign in myself") for m in repaired)


def test_results_are_ordered_to_match_the_declared_calls():
    history = [
        {"role": "assistant", "content": "", "tool_calls": [_call("a"), _call("b")]},
        {"role": "tool", "tool_call_id": "b", "content": "B"},
        {"role": "user", "content": "interrupting"},
        {"role": "tool", "tool_call_id": "a", "content": "A"},
    ]

    repaired = KimiBackend._repair_orphaned_tool_calls(history)

    assert _violations(repaired) == []
    assert [m["content"] for m in repaired if m.get("role") == "tool"] == ["A", "B"]


def test_genuinely_missing_result_still_gets_a_synthetic_one():
    """The original guarantee must survive the ordering fix."""
    history = [
        {"role": "assistant", "content": "", "tool_calls": [_call("c9", "bash")]},
        {"role": "user", "content": "what happened?"},
    ]

    repaired = KimiBackend._repair_orphaned_tool_calls(history)

    assert _violations(repaired) == []
    synthetic = next(m for m in repaired if m.get("role") == "tool")
    assert synthetic["tool_call_id"] == "c9"
    assert "bash" in synthetic["content"]
    assert "interrupted" in synthetic["content"]


def test_already_valid_history_is_untouched():
    """Repair must be a no-op on healthy history, and idempotent."""
    history = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "", "tool_calls": [_call("c1")]},
        {"role": "tool", "tool_call_id": "c1", "content": "done"},
        {"role": "assistant", "content": "finished"},
    ]

    once = KimiBackend._repair_orphaned_tool_calls(history)
    assert once == history
    assert KimiBackend._repair_orphaned_tool_calls(once) == once


def test_tool_message_answering_no_declared_call_is_kept():
    """Unmatched results are preserved rather than silently dropped.

    Relocation works by removing the copy left behind; a result with no
    corresponding call has nowhere to move to and must not vanish with it.
    """
    history = [
        {"role": "user", "content": "hi"},
        {"role": "tool", "tool_call_id": "ghost", "content": "orphaned result"},
    ]

    repaired = KimiBackend._repair_orphaned_tool_calls(history)

    assert any(m.get("tool_call_id") == "ghost" for m in repaired)


def test_a_reused_call_id_gets_its_own_result_each_time():
    """Older histories reuse one call_id across separate turns.

    Matching purely by id hands both calls the first answer and drops the
    second — relocation has to consume responses in order, not look them up.
    """
    history = [
        {"role": "assistant", "content": "", "tool_calls": [_call("dup", "read")]},
        {"role": "tool", "tool_call_id": "dup", "content": "first"},
        {"role": "assistant", "content": "again"},
        {"role": "assistant", "content": "", "tool_calls": [_call("dup", "read")]},
        {"role": "tool", "tool_call_id": "dup", "content": "second"},
    ]

    repaired = KimiBackend._repair_orphaned_tool_calls(history)

    assert _violations(repaired) == []
    assert [m["content"] for m in repaired if m.get("role") == "tool"] == ["first", "second"]


def test_result_appearing_before_its_call_is_not_duplicated():
    """Placement is decided up front precisely to avoid emitting it twice."""
    history = [
        {"role": "tool", "tool_call_id": "c1", "content": "early"},
        {"role": "assistant", "content": "", "tool_calls": [_call("c1")]},
    ]

    repaired = KimiBackend._repair_orphaned_tool_calls(history)

    assert _violations(repaired) == []
    assert [m["content"] for m in repaired if m.get("role") == "tool"] == ["early"]


@pytest.mark.parametrize("count", [1, 3, 5])
def test_multiple_interruptions_across_several_turns(count):
    """A long session interleaves many of these; all must come out valid."""
    history = []
    for i in range(count):
        history.append({"role": "user", "content": f"request {i}"})
        history.append({"role": "assistant", "content": "", "tool_calls": [_call(f"c{i}")]})
        history.append({"role": "user", "content": f"aside {i}"})
        history.append({"role": "tool", "tool_call_id": f"c{i}", "content": f"result {i}"})

    repaired = KimiBackend._repair_orphaned_tool_calls(history)

    assert _violations(repaired) == []
    assert len([m for m in repaired if m.get("role") == "tool"]) == count
