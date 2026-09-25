"""A session built with a tool list runs only those tools.

Regression coverage for delegated workers and other sessions with a tool
list. The allowlist check sat after the branches that run a tool themselves
(`task`, `task_batch`, `await_user`, `search_tools`, `mcp_*`), so it never
applied to them. A read-only explore worker whose model called `task`, a tool
it was never offered, started a build worker that wrote a file. The list shown
to a model is only a hint; dispatch is the boundary.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from lumi.engine.agent_runtime import AgentRegistry
from lumi.engine.sandbox import PathSandbox
from lumi.engine.session import Session
from lumi.engine.tools import AGENT_TOOLS
from tests.streaming_stub import (
    StreamingBackend,
    done,
    events_of_kind,
    first_of_kind,
    text_delta,
    tool_call,
)

_BUILD = "You are a build worker handling one bounded coding assignment"
_EXPLORE = "You are a fast, read-only exploration worker"


class _Roles(StreamingBackend):
    """Answers by who is asking, so every session's turn is scripted.

    The parent delegates to an explore worker, which asks for ``forbidden``.
    A build worker, if one ever starts, writes ``escalated.txt``. Each one
    finishes with text once it has a tool result.
    """

    def __init__(self, forbidden: tuple[str, dict]):
        super().__init__()
        self.forbidden = forbidden

    def stream(self, *, user_msg, conversation_history, instructions, tools, max_tokens, cancel_event=None):
        if any(entry.get("role") == "tool_result" for entry in conversation_history):
            yield text_delta("Finished.")
        elif _BUILD in instructions:
            yield tool_call("file_write", {"path": "escalated.txt", "content": "x"}, call_id="b1")
        elif _EXPLORE in instructions:
            yield tool_call(*self.forbidden, call_id="w1")
        else:
            yield tool_call("task", {"prompt": "look around", "agent_type": "explore"}, call_id="t1")
        yield done()


class _WritingMcp:
    """An MCP server whose one tool writes a file into the project."""

    def __init__(self, root: Path):
        self.root = root
        self.calls: list[str] = []

    def call_tool(self, name, arguments):
        self.calls.append(name)
        (self.root / "escalated.txt").write_text("x", encoding="utf-8")
        return {"content": [{"type": "text", "text": "wrote escalated.txt"}]}


def _parent(tmp_path: Path, forbidden: tuple[str, dict], *, auto_approve: bool = True) -> Session:
    session = Session(backend=_Roles(forbidden), max_steps=3, auto_approve=auto_approve)
    session.project_path = str(tmp_path)
    session.sandbox = PathSandbox(str(tmp_path), enabled=True)
    session.agent_registry = AgentRegistry(tmp_path, root=tmp_path / "agents")
    session._mcp_manager = _WritingMcp(tmp_path)
    return session


def _worker_result(events: list[dict]) -> dict:
    return next(e for e in events_of_kind(events, "tool.result") if e.get("call_id") == "w1")


_BUILD_TASK = {"prompt": "write it", "agent_type": "build"}


@pytest.mark.parametrize(
    "forbidden",
    [
        pytest.param(("task", _BUILD_TASK), id="task"),
        pytest.param(("task_batch", {"tasks": [_BUILD_TASK, _BUILD_TASK]}), id="task_batch"),
        pytest.param(("mcp_files_write", {"path": "escalated.txt", "content": "x"}), id="mcp tool"),
        pytest.param(("await_user", {"question": "Which folder?"}), id="await_user"),
        pytest.param(("search_tools", {"query": "write files"}), id="search_tools"),
    ],
)
def test_a_worker_cannot_run_a_tool_outside_its_list(tmp_path, forbidden):
    session = _parent(tmp_path, forbidden)

    events = list(session.run("explore the project"))

    result = _worker_result(events)
    assert (result["is_error"], result["denied"]) == (True, True)
    assert f"Tool '{forbidden[0]}' is not in this session's allowlist" in result["output"]
    # Nothing ran: no other worker started, the MCP server wasn't called and
    # nothing was written.
    assert sorted(record.agent_type for record in session.agent_registry.list()) == ["explore", "primary"]
    assert session._mcp_manager.calls == []
    assert not (tmp_path / "escalated.txt").exists()


def test_a_tool_outside_the_list_is_refused_without_asking(tmp_path):
    session = _parent(tmp_path, ("task", _BUILD_TASK), auto_approve=False)
    asked: list[str] = []

    events = list(session.run("explore the project", on_permission=lambda name, args: asked.append(name) or True))

    # Only the parent's own delegation was put to the user.
    assert asked == ["task"]
    assert _worker_result(events)["denied"] is True
    assert not (tmp_path / "escalated.txt").exists()


def test_a_listed_tool_still_runs(tmp_path):
    # Specialists list await_user; a tool in the list keeps working.
    listed = [tool for tool in AGENT_TOOLS if tool["function"]["name"] in {"await_user", "file_read"}]
    backend = StreamingBackend(scripts=[
        [tool_call("await_user", {"question": "Which file holds the notes?"}, call_id="q1"), done()],
        [text_delta("Thanks."), done()],
    ])
    session = Session(backend=backend, max_steps=2, auto_approve=True, allowed_tools=listed)
    session.project_path = str(tmp_path)
    asked: list[str] = []

    events = list(session.run(
        "read the notes", on_user_input=lambda question, options: asked.append(question) or "notes.txt",
    ))

    assert asked == ["Which file holds the notes?"]
    result = first_of_kind(events, "tool.result")
    assert (result["denied"], result["output"]) == (False, "notes.txt")
