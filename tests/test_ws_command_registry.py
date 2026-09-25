"""Contract tests for the extracted WebSocket command handlers.

These handlers used to be branches inside a ~2,600-line `websocket_endpoint`,
reachable only by standing up a real socket and the whole app state. Now they
take an explicit context, so each one can be driven directly.
"""

from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from lumi.gui import ws_commands
from lumi.gui.app import websocket_endpoint  # noqa: F401  (import smoke)


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


def test_init_restores_current_conversation_without_rebuilding_runtime():
    snapshot = {
        "page": {"events": [{"event": "user_message", "text": "hello"}]},
        "projections": {"stats": {"turns": 1}},
    }
    record = SimpleNamespace(history_snapshot=lambda **_kwargs: snapshot)
    state = SimpleNamespace(
        backend=object(),
        available_backends={},
        project=SimpleNamespace(current_session=record),
        get_init_data=lambda: {"event": "init", "current_session_id": "s1"},
    )

    runs = SimpleNamespace(
        busy=True,
        started_at=1234.5,
        pending=[
            {"message_id": "follow-1", "text": "keep checking"},
            {"message_id": "follow-2", "text": "then summarize"},
        ],
    )
    sent = _run(ws_commands.HANDLERS["init"], _ctx(state=state, runs=runs))

    assert sent[0]["current_display_events"] == snapshot["page"]["events"]
    assert sent[0]["current_history_page"] == snapshot["page"]
    assert sent[0]["current_projections"] == snapshot["projections"]
    assert sent[0]["run_active"] is True
    assert sent[0]["run_started_at"] == 1234.5
    assert sent[0]["queued_messages"] == [
        {
            "message_id": "follow-1",
            "text": "keep checking",
            "position": 1,
            "steering": False,
        },
        {
            "message_id": "follow-2",
            "text": "then summarize",
            "position": 2,
            "steering": False,
        },
    ]
    assert sent[0]["running_intents"] == []


def _connecting_page_state(attach_intent_viewer):
    return SimpleNamespace(
        backend=object(),
        available_backends={},
        project=SimpleNamespace(current_session=None),
        get_init_data=lambda: {"event": "init"},
        attach_intent_viewer=attach_intent_viewer,
    )


def test_init_hands_the_running_plans_to_the_page_that_connects():
    plan = {"intent_id": "plan-1", "text": "add a toggle", "paused": False, "stopping": False,
            "started_at": 1.0, "snapshot": {"intent_id": "plan-1", "nodes": []}}
    viewers = []
    ctx = _ctx(state=_connecting_page_state(lambda viewer: viewers.append(viewer) or [plan]))

    async def connect_then_hear_from_the_plan():
        await ws_commands.HANDLERS["init"](ctx)
        # What the plan's worker thread sends from now on reaches this socket.
        viewers[0]({"event": "intent.paused", "intent_id": "plan-1"})
        for _ in range(5):
            await asyncio.sleep(0)

    asyncio.run(connect_then_hear_from_the_plan())

    assert ctx.ws.sent[0]["running_intents"] == [plan]
    assert ctx.ws.sent[1:] == [{"event": "intent.paused", "intent_id": "plan-1"}]


def test_init_still_opens_the_page_when_its_plans_cannot_be_handed_over():
    def attach_intent_viewer(_viewer):
        raise RuntimeError("the graph could not be read")

    sent = _run(ws_commands.HANDLERS["init"], _ctx(state=_connecting_page_state(attach_intent_viewer)))

    assert [event["event"] for event in sent] == ["init"]
    assert sent[0]["running_intents"] == []


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


def test_set_permission_mode_requires_an_explicit_known_mode():
    """The backends treat an unknown mode as Full-auto, so none may default in."""
    applied = []
    state = SimpleNamespace(apply_permission_mode=applied.append)
    handler = ws_commands.HANDLERS["set_permission_mode"]

    for msg in ({}, {"mode": None}, {"mode": ""}, {"mode": "full-auto"}, {"mode": ["bypass"]}):
        sent = _run(handler, _ctx(state=state, msg=msg))
        assert sent[0]["event"] == "error", msg
    assert applied == []

    assert _run(handler, _ctx(state=state, msg={"mode": "ask"})) == []
    assert applied == ["ask"]


def _settings_ctx(msg):
    writes = []
    stored: dict = {}

    def update_setting_value(section, key, value, *, clear_secret=False):
        writes.append((section, key, value))
        stored[(section, key)] = value
        return {"saved": True}

    state = SimpleNamespace(
        # The handler compares values before and after to audit what changed.
        settings=SimpleNamespace(get=lambda section, key=None, default=None: stored.get((section, key), default)),
        update_setting_value=update_setting_value,
        get_init_data=lambda refresh_only=False: {"event": "init"},
    )
    return _ctx(state=state, msg=msg, runs=SimpleNamespace(busy=False)), writes


@pytest.mark.parametrize("msg", [
    # Hooks, LSP servers, plugins and the gateway run commands or grant remote
    # control; Settings shows them read-only.
    {"section": "hooks", "value": [{"event": "session_start", "command": "calc.exe"}]},
    {"section": "hooks", "key": "0", "value": {"command": "calc.exe"}},
    {"section": "lsp_servers", "key": "py", "value": {"command": "calc.exe"}},
    {"section": "plugins", "key": "x", "value": {"path": "C:/evil"}},
    {"section": "gateway", "key": "allowed_chat_ids", "value": [1]},
    {"section": "api_keys", "key": "telegram_bot", "value": "123:abc"},
    {"section": "general", "key": "unknown", "value": True},
    {"section": "general", "value": {"theme": "light"}},  # whole-section replace
    {"section": ["general"], "key": "theme", "value": "light"},
    {"section": "general", "key": "default_permission_mode", "value": "yolo"},
    {"section": "general", "key": "default_permission_mode", "value": ["bypass"]},
    {"section": "mcp_servers", "key": "x", "value": {"command": "calc.exe", "args": []}},
    {"section": "mcp_servers", "key": "x", "value": {"transport": "stdio", "command": "calc.exe"}},
    {"section": "mcp_servers", "key": "x", "value": {"transport": "http", "url": "file:///C:/x"}},
    {"section": "mcp_servers", "key": "", "value": {"transport": "http", "url": "http://127.0.0.1:3000/mcp"}},
    {"section": "network", "values": {"ollama_url": "http://x", "hooks": []}},
    {"section": "network", "values": {}},
])
def test_update_settings_refuses_what_settings_does_not_edit(msg):
    ctx, writes = _settings_ctx({"command": "update_settings", **msg})

    sent = _run(ws_commands.HANDLERS["update_settings"], ctx)

    assert writes == []
    assert [event["event"] for event in sent] == ["error"]


def test_update_settings_applies_ui_fields():
    ctx, writes = _settings_ctx({"section": "appearance", "key": "theme", "value": "light"})

    sent = _run(ws_commands.HANDLERS["update_settings"], ctx)

    assert writes == [("appearance", "theme", "light")]
    assert [event["event"] for event in sent] == ["settings", "init"]


def test_update_settings_stores_http_mcp_servers_without_process_fields():
    ctx, writes = _settings_ctx({"section": "mcp_servers", "key": "docs", "value": {
        "transport": "http", "url": " http://127.0.0.1:3000/mcp ", "enabled": True,
        "command": "calc.exe", "args": ["/c"], "env": {"X": "1"}, "headers": {"Authorization": "Bearer t"},
    }})

    _run(ws_commands.HANDLERS["update_settings"], ctx)

    assert writes == [("mcp_servers", "docs", {
        "transport": "http", "url": "http://127.0.0.1:3000/mcp", "enabled": True,
        "headers": {"Authorization": "Bearer t"},
    })]


def test_ollama_wizard_url_is_saved():
    # The setup wizard sends `values`, which the handler used to ignore.
    ctx, writes = _settings_ctx({"section": "network", "values": {"ollama_url": "http://10.0.0.131:11434"}})

    _run(ws_commands.HANDLERS["update_settings"], ctx)

    assert writes == [("network", "ollama_url", "http://10.0.0.131:11434")]


def test_approval_requires_an_explicit_true():
    # The answer must also name the waiting prompt (see test_permission_decisions);
    # this checks the value rule for an answer that does.
    for msg, expected in (({}, False), ({"approved": "yes"}, False), ({"approved": 1}, False),
                          ({"approved": False}, False), ({"approved": True}, True)):
        state = SimpleNamespace(permission_result=[None], permission_response=threading.Event(),
                                permission_request_id="req-1", _permission_lock=threading.Lock())
        _run(ws_commands.HANDLERS["approve"], _ctx(state=state, msg={**msg, "request_id": "req-1"}))
        assert state.permission_result[0] is expected, msg
        assert state.permission_response.is_set()


def test_git_status_runs_against_the_active_project():
    """Regression guard: _git_status used to read a module-level AppState
    singleton, so which repository it inspected was global state."""
    seen = {}

    def _fake_status(project_path):
        seen["path"] = project_path
        return {"is_repo": True, "branch": "main"}

    with patch("lumi.gui.ws_commands._git_status", _fake_status):
        sent = _run(ws_commands.HANDLERS["git_status"], _ctx())

    assert seen["path"] == "/tmp/project"
    assert sent[0]["data"]["branch"] == "main"


def test_git_quick_passes_the_action_and_project():
    seen = {}

    def _fake_quick(action, msg, project_path):
        seen.update(action=action, project_path=project_path, count=msg.get("count"))
        return {"output": "abc123 commit"}

    with patch("lumi.gui.ws_commands._git_quick", _fake_quick):
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


def test_switch_session_recovers_from_an_unavailable_saved_provider():
    saved = []
    record = SimpleNamespace(
        id="s1",
        title="Old GLM session",
        backend_type="ollama",
        model="glm-5.2:cloud",
        message_count=1,
        session_role="generator",
        thinking_mode="",
        conversation_history=[{"role": "user", "content": "hello"}],
        history_snapshot=lambda **_kwargs: {
            "page": {"events": [{"event": "user_message", "text": "hello"}]},
            "projections": {},
        },
    )
    project = SimpleNamespace(
        project_path="/tmp/project",
        current_session=record,
        load_session=lambda _id, **_kwargs: record,
        list_sessions=lambda: [],
        save_current_session=lambda session: saved.append(session),
    )
    state = SimpleNamespace(
        project=project,
        backend=None,
        backend_spec=None,
        session=None,
        _first_message_sent=False,
        _normalize_path=lambda value: value,
        recovery_chat_backend_choice=lambda *_args: ("exo", "glm-5.2"),
        get_init_data=lambda refresh_only=False: {
            "event": "init", "runtime_ready": True,
        },
    )

    def create_backend(backend_type, model, **_kwargs):
        if backend_type == "ollama":
            raise RuntimeError("Ollama is not reachable")
        state.backend = SimpleNamespace(name=backend_type, model=model)
        state.backend_spec = SimpleNamespace(thinking_mode="")
        state.session = SimpleNamespace(conversation_history=[])

    state.create_backend = create_backend
    state.restore_session_runtime = create_backend

    sent = _run(
        ws_commands.HANDLERS["switch_session"],
        _ctx(msg={"session_id": "s1"}, state=state),
    )

    assert state.session.conversation_history == record.conversation_history
    assert (record.backend_type, record.model) == ("exo", "glm-5.2")
    assert saved == [state.session]
    assert any(
        event.get("event") == "status_msg"
        and "Continuing with exo (glm-5.2)" in event.get("message", "")
        for event in sent
    )
    assert not any("runtime is unavailable" in event.get("message", "") for event in sent)


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


# ---------------------------------------------------------------------------
# Intent commands: /plan and the plan-graph toolbar
# ---------------------------------------------------------------------------


class _RecordingIntentService:
    def __init__(self):
        self.calls = []
        # The plans each page took over, and the emitter each plan started with.
        self.routed = []
        self.viewers = {}

    def start_intent(self, text, *, viewer=None):
        self.calls.append(("start_intent", text))
        self.viewers["intent-1"] = viewer
        return "intent-1"

    def route_to(self, intent_id, viewer):
        self.routed.append(intent_id)
        self.viewers[intent_id] = viewer
        return True

    def cancel(self, intent_id):
        self.calls.append(("cancel", intent_id))
        return True

    def pause(self, intent_id):
        self.calls.append(("pause", intent_id))
        return True

    def resume(self, intent_id):
        self.calls.append(("resume", intent_id))
        return True

    def list_snapshots(self, intent_id):
        self.calls.append(("list_snapshots", intent_id))
        return [{"ts_ms": 5, "node_count": 2}]

    def restore_snapshot(self, intent_id, ts_ms):
        # The service refuses to restore a running intent.
        self.calls.append(("restore_snapshot", intent_id, ts_ms))
        return False


def _intent_ctx(msg):
    service = _RecordingIntentService()
    state = SimpleNamespace(backend=object(), get_intent_service=lambda *, on_event=None: service)
    return _ctx(state=state, msg=msg), service


def test_intent_start_starts_the_goal_and_acknowledges_it():
    # The endpoint passes the whole message, "command" included, to the handler.
    ctx, service = _intent_ctx({"command": "intent_start", "text": "  add a dark mode toggle  "})

    sent = _run(ws_commands.HANDLERS["intent_start"], ctx)

    assert service.calls == [("start_intent", "add a dark mode toggle")]
    assert sent == [{"event": "intent.accepted", "intent_id": "intent-1", "text": "add a dark mode toggle"}]
    # The plan's events go to the page that started it.
    assert callable(service.viewers["intent-1"])
    assert service.routed == []


@pytest.mark.parametrize(("msg", "call", "reply"), [
    ({"command": "intent_cancel", "intent_id": "intent-1"}, ("cancel", "intent-1"),
     {"event": "intent.cancel_ack", "intent_id": "intent-1", "ok": True}),
    ({"command": "intent_pause", "intent_id": "intent-1"}, ("pause", "intent-1"),
     {"event": "intent.pause_ack", "intent_id": "intent-1", "ok": True}),
    ({"command": "intent_resume", "intent_id": "intent-1"}, ("resume", "intent-1"),
     {"event": "intent.resume_ack", "intent_id": "intent-1", "ok": True}),
    ({"command": "intent_list_snapshots", "intent_id": "intent-1"}, ("list_snapshots", "intent-1"),
     {"event": "plan.snapshot_list", "intent_id": "intent-1", "snapshots": [{"ts_ms": 5, "node_count": 2}]}),
    ({"command": "intent_restore_snapshot", "intent_id": "intent-1", "ts_ms": 5}, ("restore_snapshot", "intent-1", 5),
     {"event": "intent.restore_ack", "intent_id": "intent-1", "ok": False}),
])
def test_intent_controls_reach_the_service_and_acknowledge(msg, call, reply):
    ctx, service = _intent_ctx(msg)

    sent = _run(ws_commands.HANDLERS[msg["command"]], ctx)

    assert service.calls == [call]
    assert sent == [reply]
    # The page acting on the plan receives what reports the result.
    assert service.routed == ["intent-1"]


def test_intent_start_requires_a_goal():
    ctx, service = _intent_ctx({"command": "intent_start", "text": "   "})

    sent = _run(ws_commands.HANDLERS["intent_start"], ctx)

    assert service.calls == []
    assert sent == [{"event": "error", "message": "intent text is required"}]


@pytest.mark.parametrize("msg", [{}, {"command": "intent_explode"}, {"command": "memory_list"}])
def test_the_intent_handler_refuses_a_command_it_does_not_serve(msg):
    # Doing nothing, silently, is how a wrong name check here went unnoticed.
    built = []
    state = SimpleNamespace(backend=object(), get_intent_service=lambda **kwargs: built.append(kwargs))

    sent = _run(ws_commands.HANDLERS["intent_start"], _ctx(state=state, msg={**msg, "text": "x"}))

    assert built == []
    assert sent[0]["event"] == "error"
    assert "Unknown intent command" in sent[0]["message"]


def test_starting_a_plan_needs_a_backend():
    built = []
    state = SimpleNamespace(backend=None, get_intent_service=lambda **kwargs: built.append(kwargs))

    sent = _run(ws_commands.HANDLERS["intent_start"],
                _ctx(state=state, msg={"command": "intent_start", "text": "add a toggle"}))

    assert built == []
    assert sent == [{"event": "error", "message": "Connect a backend before starting an intent."}]


@pytest.mark.parametrize(("name", "call", "ack"), [
    ("intent_cancel", "cancel", "intent.cancel_ack"),
    ("intent_pause", "pause", "intent.pause_ack"),
    ("intent_resume", "resume", "intent.resume_ack"),
])
def test_a_running_plan_stays_controllable_without_a_backend(name, call, ack):
    # A plan keeps the backend it started with. Opening a conversation whose
    # model can't start leaves the app without one, and that must not cost
    # the running plan its Stop.
    service = _RecordingIntentService()
    state = SimpleNamespace(backend=None, get_intent_service=lambda *, on_event=None: service)

    sent = _run(ws_commands.HANDLERS[name], _ctx(state=state, msg={"command": name, "intent_id": "intent-1"}))

    assert service.calls == [(call, "intent-1")]
    assert sent == [{"event": ack, "intent_id": "intent-1", "ok": True}]


def _events_until(socket, event_name):
    events = []
    while True:
        events.append(socket.receive_json())
        if events[-1].get("event") == event_name:
            return events


def test_plan_through_the_app_socket_starts_the_intent_and_streams_its_events(tmp_path, monkeypatch):
    """The whole /plan path: socket, dispatch, the app's intent service, events back."""
    from lumi.gui import app as gui_app
    from lumi.orchestration import NodeSpecialization, NodeStatus, SpecialistResult
    from tests.gui_access import LocalClient

    monkeypatch.setenv("LUMI_STATE_HOME", str(tmp_path / "state"))
    project = tmp_path / "project"
    project.mkdir()
    state = SimpleNamespace(
        backend=object(),
        project=SimpleNamespace(project_path=str(project), current_session=None),
        mcp_manager=SimpleNamespace(get_all_tools=list),
        _project_instructions="",
        settings=None,
        _build_specialist_backend=lambda _specialization: None,
        session=None,
        available_backends={"ollama": {}},
        codebase_index=object(),
    )
    # The app's own service construction, bound to this state.
    state.get_intent_service = gui_app.AppState.get_intent_service.__get__(state)
    monkeypatch.setattr(gui_app, "state", state)
    goals = []

    def specialist(node, _graph):
        goals.append((node.specialization, node.goal))
        if node.specialization == NodeSpecialization.PLAN:
            return SpecialistResult(status=NodeStatus.DONE, confidence=0.9,
                                    subgoals=[{"goal": "add the toggle", "specialization": "implement"}])
        return SpecialistResult(status=NodeStatus.DONE, confidence=0.95, summary="done")

    # A command that always answers marks where each batch of replies ends,
    # so a command that sends nothing fails the test instead of hanging it.
    sentinel = {"command": "get_context_state"}
    with (
        patch("lumi.orchestration.intent_service.LocalSpecialistRunner", side_effect=lambda **_kwargs: specialist),
        LocalClient(gui_app.app) as client,
        client.websocket_connect("/ws") as socket,
    ):
        socket.send_json({"command": "intent_start", "text": "  add a dark mode toggle  "})
        socket.send_json(sentinel)
        replies = _events_until(socket, "context.state")
        accepted = next((event for event in replies if event["event"] == "intent.accepted"), None)
        assert accepted is not None, replies
        worker = state._intent_service._get(accepted["intent_id"]).thread
        worker.join(timeout=10)
        assert not worker.is_alive()
        # Everything the worker thread emitted is queued before this reply.
        socket.send_json(sentinel)
        replies += _events_until(socket, "context.state")

    assert accepted["text"] == "add a dark mode toggle"
    assert goals[0] == (NodeSpecialization.PLAN, "add a dark mode toggle")
    assert (NodeSpecialization.IMPLEMENT, "add the toggle") in goals
    kinds = [event["event"] for event in replies]
    for kind in ("plan.snapshot", "intent.started", "plan.event", "intent.complete"):
        assert kind in kinds, kinds
    assert all(event["intent_id"] == accepted["intent_id"] for event in replies if event["event"].startswith("intent."))


@pytest.mark.parametrize("backend_meanwhile", ["another model", "none"])
def test_stop_through_the_app_socket_reaches_a_plan_started_before_a_model_switch(
    tmp_path, monkeypatch, backend_meanwhile,
):
    """The Plan tab's Stop: intent_cancel on the app's socket ends the running
    step, and nothing after it starts. Meanwhile the person switched models,
    or opened a conversation whose model can't start (no backend): either
    used to leave the plan out of reach."""
    from lumi.gui import app as gui_app
    from lumi.orchestration import NodeSpecialization, NodeStatus, SpecialistResult
    from tests.gui_access import LocalClient

    monkeypatch.setenv("LUMI_STATE_HOME", str(tmp_path / "state"))
    project = tmp_path / "project"
    project.mkdir()
    state = SimpleNamespace(
        backend=object(),
        project=SimpleNamespace(project_path=str(project), current_session=None),
        mcp_manager=SimpleNamespace(get_all_tools=list),
        _project_instructions="",
        settings=None,
        _build_specialist_backend=lambda _specialization: None,
        session=None,
        available_backends={"ollama": {}},
        codebase_index=object(),
    )
    state.get_intent_service = gui_app.AppState.get_intent_service.__get__(state)
    monkeypatch.setattr(gui_app, "state", state)
    started = []
    implementing = threading.Event()

    def make_specialist(cancel_event):
        def specialist(node, _graph):
            started.append(node.goal)
            if node.specialization == NodeSpecialization.PLAN:
                return SpecialistResult(status=NodeStatus.DONE, confidence=0.9, subgoals=[
                    {"goal": "add the toggle", "specialization": "implement"},
                    {"goal": "check the toggle", "specialization": "verify", "depends_on": [0]},
                ])
            implementing.set()
            # A specialist's Session shares the intent's cancel event.
            cancel_event.wait(timeout=10)
            return SpecialistResult(status=NodeStatus.ABANDONED, confidence=0.0,
                                    summary="Stopped before this step finished.")
        return specialist

    sentinel = {"command": "get_context_state"}
    with (
        patch("lumi.orchestration.intent_service.LocalSpecialistRunner",
              side_effect=lambda **kwargs: make_specialist(kwargs["cancel_event"])),
        LocalClient(gui_app.app) as client,
        client.websocket_connect("/ws") as socket,
    ):
        socket.send_json({"command": "intent_start", "text": "add a dark mode toggle"})
        socket.send_json(sentinel)
        replies = _events_until(socket, "context.state")
        accepted = next((event for event in replies if event["event"] == "intent.accepted"), None)
        assert accepted is not None, replies
        intent_id = accepted["intent_id"]
        assert implementing.wait(timeout=10)
        state.backend = object() if backend_meanwhile == "another model" else None
        socket.send_json({"command": "intent_cancel", "intent_id": intent_id})
        socket.send_json(sentinel)
        replies = _events_until(socket, "context.state")
        worker = state._intent_service._get(intent_id).thread
        worker.join(timeout=10)
        assert not worker.is_alive()
        socket.send_json(sentinel)
        replies += _events_until(socket, "context.state")

    assert started == ["add a dark mode toggle", "add the toggle"]
    assert {"event": "intent.cancel_ack", "intent_id": intent_id, "ok": True} in replies
    kinds = [event["event"] for event in replies]
    assert kinds.count("intent.cancelling") == 1
    assert kinds.count("intent.cancelled") == 1
    assert "intent.complete" not in kinds
    stopped = [event["event_payload"] for event in replies
               if event["event"] == "plan.event" and event["event_payload"]["kind"] == "plan.stopped"]
    assert len(stopped) == 1
    graph = state._intent_service.get_graph(intent_id)
    assert stopped[0]["payload"]["abandoned"] == [
        node.id for node in graph.nodes.values() if node.goal == "check the toggle"
    ]
    assert {node.goal: node.status for node in graph.nodes.values()} == {
        "add a dark mode toggle": NodeStatus.DONE,
        "add the toggle": NodeStatus.ABANDONED,
        "check the toggle": NodeStatus.ABANDONED,
    }


def _app_plan_state(tmp_path, monkeypatch):
    """A minimal app state with the app's own plan wiring bound to it."""
    from lumi.gui import app as gui_app

    monkeypatch.setenv("LUMI_STATE_HOME", str(tmp_path / "state"))
    project = tmp_path / "project"
    project.mkdir()
    state = SimpleNamespace(
        backend=object(),
        project=SimpleNamespace(project_path=str(project), current_session=None),
        mcp_manager=SimpleNamespace(get_all_tools=list),
        _project_instructions="",
        settings=None,
        _build_specialist_backend=lambda _specialization: None,
        session=None,
        available_backends={"ollama": {}},
        codebase_index=object(),
        get_init_data=lambda: {"event": "init"},
    )
    state.get_intent_service = gui_app.AppState.get_intent_service.__get__(state)
    state.attach_intent_viewer = gui_app.AppState.attach_intent_viewer.__get__(state)
    monkeypatch.setattr(gui_app, "state", state)
    return gui_app, state


def test_a_reloaded_page_picks_up_the_running_plan_and_its_stop_still_works(tmp_path, monkeypatch):
    """/plan on one socket, then the page reloads: that socket closes and a
    new one sends init. The new page is told about the plan, receives its
    events without asking for them, and its Stop stops it."""
    from lumi.orchestration import NodeSpecialization, NodeStatus, SpecialistResult
    from tests.gui_access import LocalClient

    gui_app, state = _app_plan_state(tmp_path, monkeypatch)
    implementing, release, checking = threading.Event(), threading.Event(), threading.Event()

    def make_specialist(cancel_event):
        def specialist(node, _graph):
            if node.specialization == NodeSpecialization.PLAN:
                return SpecialistResult(status=NodeStatus.DONE, confidence=0.9, subgoals=[
                    {"goal": "add the toggle", "specialization": "implement"},
                    {"goal": "check the toggle", "specialization": "verify", "depends_on": [0]},
                ])
            if node.specialization == NodeSpecialization.IMPLEMENT:
                implementing.set()
                release.wait(timeout=10)
                return SpecialistResult(status=NodeStatus.DONE, confidence=0.95, summary="added")
            checking.set()
            cancel_event.wait(timeout=10)
            return SpecialistResult(status=NodeStatus.ABANDONED, confidence=0.0,
                                    summary="Stopped before this step finished.")
        return specialist

    sentinel = {"command": "get_context_state"}
    with (
        patch("lumi.orchestration.intent_service.LocalSpecialistRunner",
              side_effect=lambda **kwargs: make_specialist(kwargs["cancel_event"])),
        LocalClient(gui_app.app) as client,
    ):
        with client.websocket_connect("/ws") as first:
            first.send_json({"command": "intent_start", "text": "add a dark mode toggle"})
            first.send_json(sentinel)
            replies = _events_until(first, "context.state")
            accepted = next((event for event in replies if event["event"] == "intent.accepted"), None)
            assert accepted is not None, replies
            intent_id = accepted["intent_id"]
            assert implementing.wait(timeout=10)
        # The reload: that page and its socket are gone; a new one connects.
        with client.websocket_connect("/ws") as second:
            second.send_json({"command": "init"})
            init = _events_until(second, "init")[-1]
            # The step it was on ends by itself, and the next one starts.
            release.set()
            assert checking.wait(timeout=10)
            second.send_json(sentinel)
            unasked = _events_until(second, "context.state")
            second.send_json({"command": "intent_cancel", "intent_id": intent_id})
            worker = state._intent_service._get(intent_id).thread
            worker.join(timeout=10)
            assert not worker.is_alive()
            second.send_json(sentinel)
            stopped = _events_until(second, "context.state")

    [plan] = init["running_intents"]
    assert plan["intent_id"] == intent_id
    assert plan["text"] == "add a dark mode toggle"
    assert (plan["paused"], plan["stopping"]) == (False, False)
    assert {node["goal"]: node["status"] for node in plan["snapshot"]["nodes"]} == {
        "add a dark mode toggle": NodeStatus.DONE,
        "add the toggle": NodeStatus.RUNNING,
        "check the toggle": NodeStatus.PENDING,
    }
    ids = {node["goal"]: node["id"] for node in plan["snapshot"]["nodes"]}
    walk = [(event["event_payload"]["kind"], event["event_payload"]["node_id"])
            for event in unasked if event["event"] == "plan.event"]
    assert ("node.done", ids["add the toggle"]) in walk
    assert ("node.start", ids["check the toggle"]) in walk
    assert {"event": "intent.cancel_ack", "intent_id": intent_id, "ok": True} in stopped
    kinds = [event["event"] for event in stopped]
    assert kinds.count("intent.cancelling") == 1
    assert kinds.count("intent.cancelled") == 1
    assert "intent.complete" not in kinds


def test_a_page_that_connects_leaves_an_autonomous_missions_dispatch_tap_in_place(tmp_path, monkeypatch):
    """A running autonomous mission wraps the service's on_event to feed its
    DispatchTracker (autonomous_session._spawn_autonomous_daemon). A page
    that connects takes over the plan it follows, never the mission's
    iteration, and leaves the wrapper in place: the mission still sees its
    iteration end."""
    from lumi.gui.autonomous_factory import DispatchTracker
    from lumi.orchestration import NodeStatus, SpecialistResult

    _gui_app, state = _app_plan_state(tmp_path, monkeypatch)
    tracker = DispatchTracker()
    mission_page = []

    def combined(event):
        tracker.feed_event(event)
        mission_page.append(event)

    steps_started = threading.Semaphore(0)
    release = threading.Event()

    def specialist(_node, _graph):
        steps_started.release()
        release.wait(timeout=10)
        return SpecialistResult(status=NodeStatus.DONE, confidence=0.95, summary="done")

    with patch("lumi.orchestration.intent_service.LocalSpecialistRunner", side_effect=lambda **_kwargs: specialist):
        service = state.get_intent_service(on_event=combined)
        # As the mission's dispatch_item starts an iteration: no viewer.
        iteration = service.start_intent("iteration 1 of the mission")
        tracker.watch(iteration)
        first_page = []
        plan = service.start_intent("add a toggle", viewer=first_page.append)
        assert steps_started.acquire(timeout=10) and steps_started.acquire(timeout=10)

        second_page = []
        plans = state.attach_intent_viewer(second_page.append)

        assert [described["intent_id"] for described in plans] == [plan]
        assert state._intent_service is service
        assert service.on_event is combined
        release.set()
        for intent_id in (iteration, plan):
            worker = service._get(intent_id).thread
            worker.join(timeout=10)
            assert not worker.is_alive()

    ended = threading.Event()
    ended.set()
    outcome = tracker.wait(iteration, stop_event=ended, poll_seconds=0.01)
    assert outcome.success is True, outcome
    assert {event["intent_id"] for event in mission_page} == {iteration}
    assert [event["event"] for event in second_page][-1] == "intent.complete"
    assert {event["intent_id"] for event in second_page} == {plan}
