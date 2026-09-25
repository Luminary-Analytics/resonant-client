"""Enforced dispatch boundaries retain real work across a resumed turn."""
import copy

import pytest

from lumi.engine.session import Session
from tests.streaming_stub import StreamingBackend, done, text_delta, tool_call


def test_empty_recovery_attempts_cannot_escape_request_limit():
    backend = StreamingBackend(events=[done()])
    session = Session(backend, max_model_requests=2)
    events = list(session.run("Inspect the project"))
    end = events[-1]
    assert end["event"] == "session.end"
    assert end["evidence"]["model_requests"] == 2
    assert end["evidence"]["request_limit_reached"] is True
    assert any(e.get("code") == "request_limit_reached" for e in events)
    assert end["outcome"] != "completed"


def test_real_tool_result_survives_limit_and_restored_continuation(tmp_path):
    source = tmp_path / "input.txt"
    source.write_text("retain this exact observation", encoding="utf-8")
    backend = StreamingBackend(events=[tool_call("file_read", {"path": str(source)}), done()])
    first = Session(backend, max_model_requests=1)
    first.project_path = str(tmp_path)
    events = list(first.run("Read input.txt and explain it"))
    assert events[-1]["evidence"]["request_limit_reached"]
    history = copy.deepcopy(first.conversation_history)
    assert any("retain this exact observation" in str(m) and m["role"] == "tool_result" for m in history)
    resumed = Session(StreamingBackend(events=[text_delta("The file contains the retained observation."), done()]), max_model_requests=1)
    resumed.project_path = str(tmp_path)
    resumed.conversation_history = history
    result = list(resumed.run("Continue"))[-1]
    assert result["evidence"]["model_requests"] == 1
    assert result["evidence"]["request_limit_reached"] is False
    assert resumed.conversation_history[:len(first.conversation_history)] == first.conversation_history


@pytest.mark.parametrize("limit", [None, 0, "0"])
def test_unlimited_default_still_completes(limit):
    session = Session(StreamingBackend(events=[text_delta("Hello."), done()]), max_model_requests=limit)
    assert not list(session.run("hello"))[-1]["evidence"]["request_limit_reached"]


@pytest.mark.parametrize("limit", [-1, 2.5, "not a number"])
def test_invalid_limits_do_not_silently_disable_boundary(limit):
    with pytest.raises(ValueError):
        Session(StreamingBackend(), max_model_requests=limit)
