"""Regression tests for model-aware context management and prompt stability."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from resonant_client.engine.sandbox import PathSandbox
from resonant_client.engine.session import Session
from tests.streaming_stub import StreamingBackend, done, text_delta, tool_call


class _InstructionBackend(StreamingBackend):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.full_instructions: list[str] = []

    def stream(self, **kwargs):
        self.full_instructions.append(kwargs.get("instructions", ""))
        yield from super().stream(**kwargs)


def _tool_loop_backend() -> _InstructionBackend:
    return _InstructionBackend(scripts=[
        [tool_call("file_read", {"path": "sample.txt"}), done()],
        [text_delta("finished"), done()],
    ])


def test_retrieved_context_is_stable_across_tool_steps(tmp_path):
    (tmp_path / "sample.txt").write_text("hello", encoding="utf-8")
    backend = _tool_loop_backend()
    session = Session(backend=backend, max_steps=3, auto_approve=True)
    session.project_path = str(tmp_path)
    session.sandbox = PathSandbox(str(tmp_path))
    session._engram = SimpleNamespace(
        enabled=True,
        get_context_for_prompt=lambda _: "\n--- PINNED MEMORY ---\nstable\n",
    )

    list(session.run("read it"))

    assert len(backend.full_instructions) == 2
    assert all("PINNED MEMORY" in value for value in backend.full_instructions)
    assert all("EXECUTION PROFILE: ADAPTIVE AGENT" in value for value in backend.full_instructions)
    assert backend.full_instructions[0] == backend.full_instructions[1]


def test_context_compression_is_rechecked_mid_turn(tmp_path):
    (tmp_path / "sample.txt").write_text("hello", encoding="utf-8")
    backend = _tool_loop_backend()
    backend.effective_context_tokens = 32_768
    session = Session(backend=backend, max_steps=3, auto_approve=True)
    session.project_path = str(tmp_path)
    session.sandbox = PathSandbox(str(tmp_path))

    compressed = [{"role": "assistant", "content": "summary"}]
    with (
        patch(
            "resonant_client.engine.session.should_compress",
            side_effect=[False, True],
        ) as should,
        patch(
            "resonant_client.engine.session.compress",
            return_value=(compressed, "summary"),
        ) as compress_history,
    ):
        events = list(session.run("read it"))

    assert should.call_count == 2
    assert compress_history.call_count == 1
    assert any(event.get("event") == "context.compression" for event in events)
    assert session.conversation_history[0] == compressed[0]


def test_goal_and_checklist_are_recited_after_tool_results(tmp_path):
    (tmp_path / "sample.txt").write_text("hello", encoding="utf-8")
    backend = _InstructionBackend(scripts=[
        [
            text_delta("- [ ] inspect behavior\n- [x] locate file"),
            tool_call("file_read", {"path": "sample.txt"}),
            done(),
        ],
        [text_delta("finished"), done()],
    ])
    session = Session(backend=backend, max_steps=3, auto_approve=True)
    session.project_path = str(tmp_path)
    session.sandbox = PathSandbox(str(tmp_path))

    list(session.run("Fix the parser without changing its public API"))

    continuation = backend.stream_calls[1]["user_msg"]
    assert "<goal_recitation>" in continuation
    assert "Fix the parser without changing its public API" in continuation
    assert "- [ ] inspect behavior" in continuation
    assert "- [x] locate file" in continuation
