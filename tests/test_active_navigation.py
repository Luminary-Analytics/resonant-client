"""A streaming run must retain its project, engine, record and cost bucket."""
import asyncio
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from resonant_client.gui.ws_commands import CommandContext, HANDLERS
from resonant_client.gui.sessions import ProjectManager


@pytest.mark.parametrize("command", ["set_project", "switch_session", "fork_session", "clear"])
def test_navigation_cannot_replace_running_workspace(command):
    sent = []

    async def send(payload):
        sent.append(payload)

    original = object()
    state = SimpleNamespace(session=original, project=object(),
                            get_init_data=Mock(return_value={"event": "init", "cwd": "original"}))
    ctx = CommandContext(ws=SimpleNamespace(send_json=send), state=state,
                         msg={"command": command, "path": "different", "project_switch_id": "nav-1"},
                         runs=SimpleNamespace(busy=True))
    asyncio.run(HANDLERS[command](ctx))
    assert state.session is original
    assert sent[0]["event"] == "ui_notice"
    assert sent[1]["cwd"] == "original"
    assert sent[1]["project_switch_id"] == "nav-1"


def test_project_resolution_race_rechecks_run_before_mutation():
    sent=[]
    async def send(payload): sent.append(payload)
    runs=SimpleNamespace(busy=False)
    def resolve(path):
        runs.busy=True
        return path
    original=object()
    state=SimpleNamespace(session=original,ensure_project_path=resolve,
                          get_init_data=lambda **kw:{"event":"init","cwd":"original"})
    ctx=CommandContext(ws=SimpleNamespace(send_json=send),state=state,
                       msg={"path":"different"},runs=runs)
    asyncio.run(HANDLERS['set_project'](ctx))
    assert state.session is original
    assert sent[0]['event']=='ui_notice'


def test_cancel_targets_active_engine_even_if_selected_session_changed():
    sent=[]
    async def send(payload): sent.append(payload)
    active=SimpleNamespace(cancel=Mock())
    selected=SimpleNamespace(cancel=Mock())
    state=SimpleNamespace(session=selected,active_session=active,cancel_requested=SimpleNamespace(set=Mock()))
    ctx=CommandContext(ws=SimpleNamespace(send_json=send),state=state,msg={},
                       runs=SimpleNamespace(busy=True,pending=[],cancel_request_id=None))
    asyncio.run(HANDLERS['cancel'](ctx))
    active.cancel.assert_called_once()
    selected.cancel.assert_not_called()


def test_save_captured_record_cannot_overwrite_selected_project():
    original=SimpleNamespace(conversation_history=[],message_count=0,save=Mock())
    selected=SimpleNamespace(conversation_history=[{'role':'user','content':'unrelated'}],save=Mock())
    manager=object.__new__(ProjectManager)
    manager.current_session=selected
    engine=SimpleNamespace(conversation_history=[{'role':'user','content':'original task'},
                                                {'role':'tool_result','content':'verified result'}])
    manager.save_session(original,engine)
    assert original.conversation_history==engine.conversation_history
    assert original.message_count==1
    original.save.assert_called_once()
    selected.save.assert_not_called()
    assert selected.conversation_history==[{'role':'user','content':'unrelated'}]
