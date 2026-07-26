"""Contract tests for the extracted WebSocket command handlers.

These handlers used to be branches inside a ~2,600-line `websocket_endpoint`,
reachable only by standing up a real socket and the whole app state. Now they
take an explicit context, so each one can be driven directly.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from resonant_client.gui import ws_commands
from resonant_client.gui.app import websocket_endpoint  # noqa: F401  (import smoke)


class _StubWS:
    def __init__(self):
        self.sent = []

    async def send_json(self, payload):
        self.sent.append(payload)


def _ctx(*, msg=None, session=None, state=None, chat_runner=None):
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
        ws=_StubWS(), state=state, msg=msg or {}, chat_runner=chat_runner,
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
    running = SimpleNamespace(done=lambda: False)

    sent = _run(
        ws_commands.HANDLERS["session_timeline_restore"],
        _ctx(msg={"checkpoint_id": "c1"}, chat_runner=running),
    )

    assert sent[0]["event"] == "error"
    assert "Stop the active run" in sent[0]["message"]


def test_model_telemetry_reports_a_missing_ollama_backend():
    sent = _run(ws_commands.HANDLERS["get_model_telemetry"], _ctx())

    assert sent[0] == {"event": "model_telemetry", "data": {"error": "no Ollama backend"}}


def test_director_status_tolerates_no_session():
    sent = _run(ws_commands.HANDLERS["director_status"], _ctx())

    assert sent[0] == {"event": "director.status", "run": None, "benchmark": None}
