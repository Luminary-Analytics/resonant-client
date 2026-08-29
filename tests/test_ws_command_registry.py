"""Contract tests for the extracted WebSocket command handlers.

These handlers used to be branches inside a ~2,600-line `websocket_endpoint`,
reachable only by standing up a real socket and the whole app state. Now they
take an explicit context, so each one can be driven directly.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from resonant_client.gui import ws_commands
from resonant_client.gui.app import websocket_endpoint  # noqa: F401  (import smoke)


class _StubWS:
    def __init__(self):
        self.sent = []

    async def send_json(self, payload):
        self.sent.append(payload)


def _ctx(*, msg=None, session=None, state=None, runs=None):
    state = state or SimpleNamespace(
        session=session,
        project=SimpleNamespace(project_path="/tmp/project", current_session=None),
        backend=None,
        backend_spec=None,
        settings=None,
        active_thread=None,
        evaluations=None,
    )
    return ws_commands.CommandContext(
        ws=_StubWS(), state=state, msg=msg or {}, runs=runs,
    )


def _run(handler, ctx):
    asyncio.run(handler(ctx))
    return ctx.ws.sent


def test_every_registered_command_is_unique_and_callable():
    assert ws_commands.HANDLERS
    for name, handler in ws_commands.HANDLERS.items():
        assert callable(handler), name
        assert asyncio.iscoroutinefunction(handler), name


def test_registering_a_duplicate_command_is_rejected():
    # Two handlers silently claiming one command is the failure mode a plain
    # elif-chain made impossible to notice.
    with pytest.raises(RuntimeError, match="Duplicate"):
        ws_commands.command("artifact_list")(lambda ctx: None)


def test_agent_runtime_list_tolerates_no_session():
    sent = _run(ws_commands.HANDLERS["agent_runtime_list"], _ctx())

    assert sent == [{"event": "agent.runtime_list", "agents": []}]


def test_agent_runtime_list_reports_registry_records():
    record = SimpleNamespace(to_dict=lambda: {"id": "a1", "status": "running"})
    session = SimpleNamespace(agent_registry=SimpleNamespace(list=lambda: [record]))

    sent = _run(ws_commands.HANDLERS["agent_runtime_list"], _ctx(session=session))

    assert sent[0]["agents"] == [{"id": "a1", "status": "running"}]


def test_agent_runtime_control_reports_a_missing_agent():
    session = SimpleNamespace(agent_registry=SimpleNamespace(get=lambda _id: None))

    sent = _run(
        ws_commands.HANDLERS["agent_runtime_control"],
        _ctx(msg={"agent_id": "gone", "action": "pause"}, session=session),
    )

    assert sent == [{"event": "error", "message": "Agent is no longer available"}]


def test_agent_runtime_control_surfaces_a_rejected_resume():
    """A stuck agent cannot be resumed; the user must see why."""

    def _resume(_agent_id):
        raise ValueError("Agent a1 is stuck and has no live thread to resume.")

    registry = SimpleNamespace(
        get=lambda _id: SimpleNamespace(status="stuck"),
        resume=_resume,
        list=lambda: [],
    )
    session = SimpleNamespace(agent_registry=registry)

    sent = _run(
        ws_commands.HANDLERS["agent_runtime_control"],
        _ctx(msg={"agent_id": "a1", "action": "resume"}, session=session),
    )

    assert sent[0]["event"] == "error"
    assert "no live thread" in sent[0]["message"]


def test_agent_runtime_control_rejects_an_unknown_action():
    registry = SimpleNamespace(get=lambda _id: SimpleNamespace(), list=lambda: [])
    session = SimpleNamespace(agent_registry=registry)

    sent = _run(
        ws_commands.HANDLERS["agent_runtime_control"],
        _ctx(msg={"agent_id": "a1", "action": "detonate"}, session=session),
    )

    assert sent[0]["event"] == "error"
    assert "Unknown agent action" in sent[0]["message"]


def test_context_state_falls_back_to_an_empty_snapshot_without_a_session():
    sent = _run(ws_commands.HANDLERS["get_context_state"], _ctx())

    assert sent[0]["event"] == "context.state"
    assert sent[0]["context_window"] == 0
    assert sent[0]["todos"] == []


def test_context_state_uses_the_live_session_snapshot():
    session = SimpleNamespace(context_snapshot=lambda: {"model": "glm-5.2", "utilization": 0.4})

    sent = _run(ws_commands.HANDLERS["get_context_state"], _ctx(session=session))

    assert sent[0]["model"] == "glm-5.2"
    assert sent[0]["utilization"] == 0.4


def test_artifact_list_returns_newest_first():
    items = [
        SimpleNamespace(to_dict=lambda: {"id": "old"}),
        SimpleNamespace(to_dict=lambda: {"id": "new"}),
    ]
    session = SimpleNamespace(artifact_store=SimpleNamespace(list=lambda: items))

    sent = _run(ws_commands.HANDLERS["artifact_list"], _ctx(session=session))

    assert [item["id"] for item in sent[0]["artifacts"]] == ["new", "old"]


def test_session_timeline_restore_refuses_while_a_turn_is_streaming():
    sent = _run(
        ws_commands.HANDLERS["session_timeline_restore"],
        _ctx(msg={"checkpoint_id": "c1"}, runs=SimpleNamespace(busy=True)),
    )

    assert sent[0]["event"] == "error"
    assert "Stop the active run" in sent[0]["message"]


def test_model_telemetry_reports_a_missing_ollama_backend():
    sent = _run(ws_commands.HANDLERS["get_model_telemetry"], _ctx())

    assert sent[0] == {"event": "model_telemetry", "data": {"error": "no Ollama backend"}}


def test_director_commands_are_not_registered():
    assert "director_status" not in ws_commands.HANDLERS
    assert "director_configure" not in ws_commands.HANDLERS


# ---------------------------------------------------------------------------
# Second extraction batch
# ---------------------------------------------------------------------------


def test_settings_are_sent_masked():
    """Secrets must not leave the process in the clear."""
    state = SimpleNamespace(
        settings=SimpleNamespace(get_masked=lambda: {"api_key": "sk-***"}),
    )
    sent = _run(ws_commands.HANDLERS["get_settings"], _ctx(state=state))

    assert sent[0] == {"event": "settings", "data": {"api_key": "sk-***"}}


def test_set_permission_mode_defaults_and_sends_nothing():
    applied = []
    state = SimpleNamespace(apply_permission_mode=applied.append)

    assert _run(ws_commands.HANDLERS["set_permission_mode"], _ctx(state=state)) == []
    assert applied == ["bypass"]


def test_git_status_runs_against_the_active_project():
    """Regression guard: _git_status used to read a module-level AppState
    singleton, so which repository it inspected was global state."""
    seen = {}

    def _fake_status(project_path):
        seen["path"] = project_path
        return {"is_repo": True, "branch": "main"}

    with patch("resonant_client.gui.ws_commands._git_status", _fake_status):
        sent = _run(ws_commands.HANDLERS["git_status"], _ctx())

    assert seen["path"] == "/tmp/project"
    assert sent[0]["data"]["branch"] == "main"


def test_git_quick_passes_the_action_and_project():
    seen = {}

    def _fake_quick(action, msg, project_path):
        seen.update(action=action, project_path=project_path, count=msg.get("count"))
        return {"output": "abc123 commit"}

    with patch("resonant_client.gui.ws_commands._git_quick", _fake_quick):
        sent = _run(
            ws_commands.HANDLERS["git_quick"],
            _ctx(msg={"action": "log", "count": 3}),
        )

    assert seen == {"action": "log", "project_path": "/tmp/project", "count": 3}
    assert sent[0]["event"] == "git_result"


def test_mcp_connect_refreshes_session_tools_and_drops_the_intent_cache():
    """New tools change the session's surface; a cached intent service
    captured the old one."""
    session = SimpleNamespace(mcp_tools=["old"])
    manager = SimpleNamespace(
        connect=lambda name: True,
        get_all_tools=lambda: ["old", "new"],
        list_servers=lambda: [{"name": "fs"}],
    )
    state = SimpleNamespace(
        session=session, mcp_manager=manager, _intent_service=object(),
        project=SimpleNamespace(project_path="/tmp/project"),
    )

    sent = _run(ws_commands.HANDLERS["mcp_connect"], _ctx(msg={"name": "fs"}, state=state))

    assert session.mcp_tools == ["old", "new"]
    assert state._intent_service is None
    assert sent[0]["connected"] is True


def test_mcp_connect_without_a_name_does_nothing():
    state = SimpleNamespace(session=None, mcp_manager=None, _intent_service="kept")

    assert _run(ws_commands.HANDLERS["mcp_connect"], _ctx(msg={}, state=state)) == []
    assert state._intent_service == "kept"


def test_rag_stats_reports_an_unindexed_project():
    state = SimpleNamespace(codebase_index=None)

    sent = _run(ws_commands.HANDLERS["rag_stats"], _ctx(state=state))

    assert sent[0] == {"event": "rag_stats", "total_files": 0, "is_indexed": False}


def test_rag_search_without_an_index_returns_empty_rather_than_failing():
    state = SimpleNamespace(codebase_index=None)

    sent = _run(ws_commands.HANDLERS["rag_search"], _ctx(msg={"query": "x"}, state=state))

    assert sent[0] == {"event": "rag_results", "results": []}


def test_engram_recall_reports_when_memory_is_disabled():
    state = SimpleNamespace(engram=SimpleNamespace(enabled=False, recall=lambda q: []))

    sent = _run(ws_commands.HANDLERS["engram_recall"], _ctx(msg={"query": "x"}, state=state))

    assert sent[0] == {"event": "engram_recall", "memories": [], "enabled": False}


def test_engram_remember_is_a_no_op_when_disabled():
    stored = []
    state = SimpleNamespace(
        engram=SimpleNamespace(enabled=False, remember=stored.append),
    )

    assert _run(ws_commands.HANDLERS["engram_remember"], _ctx(msg={"text": "x"}, state=state)) == []
    assert stored == []


def test_session_replay_reports_a_missing_session():
    state = SimpleNamespace(
        project=SimpleNamespace(project_path="/tmp/project", get_recent_projects=list),
    )

    sent = _run(
        ws_commands.HANDLERS["get_session_replay_events"],
        _ctx(msg={"session_id": "nope"}, state=state),
    )

    assert sent[0]["error"] == "not found"
    assert sent[0]["events"] == []


def test_session_history_page_does_not_activate_the_inspected_session():
    calls = []
    record = SimpleNamespace(
        history_snapshot=lambda **kwargs: {
            "page": {"events": [{"event": "user_message"}], **kwargs},
            "projections": {"stats": {"turns": 1}},
        },
    )
    project = SimpleNamespace(
        project_path="/tmp/project",
        load_session=lambda session_id, activate=True, hydrate=True: (
            calls.append((session_id, activate, hydrate)) or record
        ),
    )
    state = SimpleNamespace(project=project)

    sent = _run(
        ws_commands.HANDLERS["get_session_history_page"],
        _ctx(msg={"session_id": "s1", "before_seq": 20, "limit": 10}, state=state),
    )

    assert calls == [("s1", False, False)]
    assert sent[0]["page"]["before_seq"] == 20
    assert sent[0]["projections"]["stats"]["turns"] == 1


def test_session_history_page_rejects_a_malformed_cursor():
    state = SimpleNamespace(project=SimpleNamespace(project_path="/tmp/project"))

    sent = _run(
        ws_commands.HANDLERS["get_session_history_page"],
        _ctx(msg={"session_id": "s1", "before_seq": "nope"}, state=state),
    )

    assert sent[0]["error"] == "invalid paging cursor"


def test_open_workspace_path_opens_a_file_inside_the_active_project(tmp_path):
    target = tmp_path / "result.md"
    target.write_text("done", encoding="utf-8")
    state = SimpleNamespace(
        project=SimpleNamespace(project_path=str(tmp_path), current_session=None),
    )

    with (
        patch.object(ws_commands.sys, "platform", "win32"),
        patch.object(ws_commands.os, "startfile", create=True) as startfile,
    ):
        sent = _run(
            ws_commands.HANDLERS["open_workspace_path"],
            _ctx(msg={"path": "result.md"}, state=state),
        )

    startfile.assert_called_once_with(str(target.resolve()))
    assert sent[-1]["message"] == "Opened result.md"


def test_open_workspace_path_rejects_paths_outside_the_project(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    outside = tmp_path / "secret.txt"
    outside.write_text("secret", encoding="utf-8")
    state = SimpleNamespace(
        project=SimpleNamespace(project_path=str(project), current_session=None),
    )

    with patch.object(ws_commands.os, "startfile", create=True) as startfile:
        sent = _run(
            ws_commands.HANDLERS["open_workspace_path"],
            _ctx(msg={"path": str(outside)}, state=state),
        )

    startfile.assert_not_called()
    assert "outside the active project" in sent[-1]["message"]
