"""The GUI's permission mode reaches the engine, and the user's answer is final.

These drive the real `_run_session_streaming` loop and the real `approve`
WebSocket handler with a scripted model, so the prompt round-trip is the one
the browser uses. The model is scripted; no provider is called.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from lumi.engine.policies import PolicyAction
from lumi.gui import app as gui_app
from lumi.gui import ws_commands
from tests.streaming_stub import StreamingBackend, done, text_delta, tool_call


@pytest.fixture
def gui_state(monkeypatch, tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.chdir(project)
    monkeypatch.setattr(gui_app.AppState, "detect_backends", lambda self, force=False: {})
    state = gui_app.AppState()
    state.apply_project_context(str(project))
    state.project.current_session = None
    monkeypatch.setattr(gui_app, "state", state)
    return state


class _Socket:
    """Answers each permission prompt the way the browser's buttons do."""

    def __init__(self, state, answer: dict):
        self.state = state
        self.answer = answer
        self.sent: list[dict] = []

    async def send_json(self, payload):
        self.sent.append(payload)
        if payload.get("event") != "tool_permission":
            return
        message = dict(self.answer)
        if payload.get("request_id"):
            message.setdefault("request_id", payload["request_id"])
        await ws_commands.HANDLERS["approve"](
            ws_commands.CommandContext(ws=self, state=self.state, msg=message)
        )

    def events(self, kind: str) -> list[dict]:
        return [event for event in self.sent if event.get("event") == kind]


def _scripted(name: str, arguments: dict) -> StreamingBackend:
    return StreamingBackend(scripts=[
        [tool_call(name, arguments, call_id="call-1"), done()],
        [text_delta("Finished."), done()],
    ])


def _turn(state, name: str, arguments: dict, answer: dict) -> _Socket:
    session = state.build_session(backend=_scripted(name, arguments), project_path=state.project.project_path)
    state.session = session
    socket = _Socket(state, answer)
    asyncio.run(gui_app._run_session_streaming(socket, session, "please do it"))
    return socket


def _marker_command(name: str) -> dict:
    return {"command": f"echo ran > {name}", "requirement": "the command runs"}


def test_permission_mode_switch_updates_the_live_session(gui_state):
    gui_state.apply_permission_mode("bypass")
    session = gui_state.build_session(backend=StreamingBackend(), project_path=gui_state.project.project_path)
    gui_state.session = session
    assert session.autonomy_tier == "full-auto"

    gui_state.apply_permission_mode("ask")
    assert session.autonomy_tier == "ask"
    # Ask asks before changes; it does not refuse them.
    assert session.execution_policy.evaluate("file_write", {"path": "x"}) == PolicyAction.PROMPT
    assert session.execution_policy.evaluate("bash", {"command": "ls"}) == PolicyAction.PROMPT
    assert not session._should_auto_approve("file_edit")
    assert session._should_auto_approve("file_read")

    gui_state.apply_permission_mode("auto-edit")
    assert session.autonomy_tier == "auto-edit"
    assert not session._should_auto_approve("bash")
    assert session._should_auto_approve("file_edit")

    gui_state.apply_permission_mode("bypass")
    assert session.autonomy_tier == "full-auto"
    assert session.execution_policy.evaluate("file_write", {"path": "x"}) == PolicyAction.ALLOW


def test_auto_edit_prompts_for_shell_and_a_socket_deny_is_final(gui_state):
    gui_state.apply_permission_mode("auto-edit")
    project = Path(gui_state.project.project_path)

    socket = _turn(gui_state, "bash", {"command": "echo ran > ran.txt"}, {"approved": False})

    assert [prompt["name"] for prompt in socket.events("tool_permission")] == ["bash"]
    assert socket.events("tool.result")[0]["denied"] is True
    assert not (project / "ran.txt").exists()


def test_auto_edit_runs_shell_after_the_user_allows_it(gui_state):
    gui_state.apply_permission_mode("auto-edit")
    project = Path(gui_state.project.project_path)

    socket = _turn(gui_state, "bash", {"command": "echo ran > ran.txt"}, {"approved": True})

    assert len(socket.events("tool_permission")) == 1
    assert socket.events("tool.result")[0]["denied"] is False
    assert (project / "ran.txt").exists()


@pytest.mark.parametrize("answer", [{}, {"approved": "false"}, {"approved": 1}])
def test_only_an_explicit_true_approves(gui_state, answer):
    gui_state.apply_permission_mode("ask")
    project = Path(gui_state.project.project_path)

    socket = _turn(gui_state, "check_run", _marker_command("ran.txt"), answer)

    assert len(socket.events("tool_permission")) == 1
    assert socket.events("tool.result")[0]["denied"] is True
    assert not (project / "ran.txt").exists()


def test_a_stale_approval_cannot_answer_a_later_prompt(gui_state):
    gui_state.apply_permission_mode("ask")
    project = Path(gui_state.project.project_path)
    # An Allow that arrived after its prompt had already ended (cancelled,
    # or a second click) must not approve the next, different request.
    gui_state.permission_result[0] = True
    gui_state.permission_response.set()

    socket = _turn(gui_state, "check_run", _marker_command("ran.txt"), {"approved": False})

    assert socket.events("tool.result")[0]["denied"] is True
    assert not (project / "ran.txt").exists()


def test_an_answer_for_a_different_request_is_ignored(gui_state):
    gui_state.apply_permission_mode("ask")
    project = Path(gui_state.project.project_path)

    class _WrongRequest(_Socket):
        async def send_json(self, payload):
            if payload.get("event") == "tool_permission":
                await ws_commands.HANDLERS["approve"](ws_commands.CommandContext(
                    ws=self, state=self.state, msg={"approved": True, "request_id": "stale-request"},
                ))
            await super().send_json(payload)

    socket = _WrongRequest(gui_state, {"approved": False})
    session = gui_state.build_session(
        backend=_scripted("check_run", _marker_command("ran.txt")),
        project_path=gui_state.project.project_path,
    )
    gui_state.session = session
    asyncio.run(gui_app._run_session_streaming(socket, session, "please do it"))

    assert socket.events("tool_permission")[0].get("request_id")
    assert socket.events("tool.result")[0]["denied"] is True
    assert not (project / "ran.txt").exists()


def test_repository_policy_cannot_unlock_built_in_denies(gui_state):
    project = Path(gui_state.project.project_path)
    (project / "resonant-policy.json").write_text(
        json.dumps({"rules": [{"tool_pattern": "*", "action": "allow"}]}),
        encoding="utf-8",
    )
    gui_state.apply_permission_mode("auto-edit")
    session = gui_state.build_session(backend=StreamingBackend(), project_path=str(project))
    gui_state.session = session

    assert session.execution_policy.evaluate("bash", {"command": "rm -rf build"}) == PolicyAction.DENY

    gui_state.apply_permission_mode("ask")
    assert session.execution_policy.evaluate("bash", {"command": "rm -rf build"}) == PolicyAction.DENY


def test_repository_restrictions_survive_a_mode_switch(gui_state):
    project = Path(gui_state.project.project_path)
    (project / "resonant-policy.json").write_text(
        json.dumps({"rules": [{"tool_pattern": "file_write", "action": "deny", "reason": "frozen"}]}),
        encoding="utf-8",
    )
    gui_state.apply_permission_mode("bypass")
    session = gui_state.build_session(backend=StreamingBackend(), project_path=str(project))
    gui_state.session = session
    assert session.execution_policy.evaluate("file_write", {"path": "x"}) == PolicyAction.DENY

    gui_state.apply_permission_mode("auto-edit")

    assert session.execution_policy.evaluate("file_write", {"path": "x"}) == PolicyAction.DENY
    assert session.execution_policy.get_reason("file_write", {"path": "x"}) == "frozen"

    # Ask would otherwise ask about the write; the repository still forbids it.
    gui_state.apply_permission_mode("ask")

    assert session.execution_policy.evaluate("file_write", {"path": "x"}) == PolicyAction.DENY
    assert session.execution_policy.get_reason("file_write", {"path": "x"}) == "frozen"


# ── Ask asks before changes instead of refusing them ───────────────────


@pytest.mark.parametrize("approved", [True, False])
def test_ask_prompts_before_a_file_edit_and_the_answer_decides(gui_state, approved):
    gui_state.apply_permission_mode("ask")
    target = Path(gui_state.project.project_path) / "notes.txt"
    target.write_text("old line\n", encoding="utf-8")

    socket = _turn(
        gui_state,
        "file_edit",
        {"path": "notes.txt", "old_text": "old line", "new_text": "new line"},
        {"approved": approved},
    )

    prompts = socket.events("tool_permission")
    assert [prompt["name"] for prompt in prompts] == ["file_edit"]
    # The change is shown for review before anything is written.
    assert prompts[0]["review"]["hunks"]
    result = socket.events("tool.result")[0]
    assert result["denied"] is (not approved)
    assert "Blocked by policy" not in result["output"]
    assert target.read_text(encoding="utf-8") == ("new line\n" if approved else "old line\n")


@pytest.mark.parametrize("approved", [True, False])
def test_ask_prompts_before_writing_a_new_file(gui_state, approved):
    gui_state.apply_permission_mode("ask")
    target = Path(gui_state.project.project_path) / "new.txt"

    socket = _turn(gui_state, "file_write", {"path": "new.txt", "content": "hello"}, {"approved": approved})

    assert [prompt["name"] for prompt in socket.events("tool_permission")] == ["file_write"]
    assert socket.events("tool.result")[0]["denied"] is (not approved)
    assert target.exists() is approved


@pytest.mark.parametrize("approved", [True, False])
def test_ask_prompts_before_a_shell_command(gui_state, approved):
    gui_state.apply_permission_mode("ask")
    project = Path(gui_state.project.project_path)

    socket = _turn(gui_state, "bash", {"command": "echo ran > ran.txt"}, {"approved": approved})

    assert [prompt["name"] for prompt in socket.events("tool_permission")] == ["bash"]
    assert socket.events("tool.result")[0]["denied"] is (not approved)
    assert (project / "ran.txt").exists() is approved


def test_ask_still_refuses_dangerous_commands_without_asking(gui_state):
    gui_state.apply_permission_mode("ask")
    build = Path(gui_state.project.project_path) / "build"
    build.mkdir()
    (build / "keep.txt").write_text("kept", encoding="utf-8")

    socket = _turn(gui_state, "bash", {"command": "rm -rf build"}, {"approved": True})

    assert socket.events("tool_permission") == []
    result = socket.events("tool.result")[0]
    assert result["denied"] is True
    assert "Recursive delete blocked" in result["output"]
    assert (build / "keep.txt").exists()


def test_a_trusted_repositorys_allow_rules_do_not_skip_asks_approval(gui_state):
    project = Path(gui_state.project.project_path)
    (project / "lumi-policy.json").write_text(
        json.dumps({"rules": [{"tool_pattern": "*", "action": "allow"}]}),
        encoding="utf-8",
    )
    gui_state.workspace_trust.trust(str(project))
    assert gui_state.project_trust(str(project)).honor_policy_allows
    gui_state.apply_permission_mode("ask")

    socket = _turn(gui_state, "file_write", {"path": "new.txt", "content": "hello"}, {"approved": False})

    assert [prompt["name"] for prompt in socket.events("tool_permission")] == ["file_write"]
    assert socket.events("tool.result")[0]["denied"] is True
    assert not (project / "new.txt").exists()
