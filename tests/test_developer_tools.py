"""Lumi's own model evaluations are a developer tool, off unless asked for.

Settings > Model evaluations showed every tester a "GLM / DeepSeek
Evaluations" panel with models and specs built in for Lumi's own releases.
It now shows (and starts) only with general.developer_tools in settings.json
or LUMI_DEVELOPER_TOOLS=1; comparing models on your own tasks stays for
everyone (settings_view.js _settingsPages).
"""

from __future__ import annotations

from types import SimpleNamespace

from lumi.gui import ws_commands
from lumi.gui.settings import DEVELOPER_TOOLS_ENV, SettingsManager, developer_tools
from tests.test_ws_command_registry import _run, _StubWS


def test_off_unless_the_setting_or_the_environment_turns_them_on(tmp_path, monkeypatch):
    monkeypatch.delenv(DEVELOPER_TOOLS_ENV, raising=False)
    settings = SettingsManager(tmp_path / "settings.json")
    assert developer_tools(settings) is False and settings.get_masked()["_meta"]["developer_tools"] is False
    settings.set("general", "developer_tools", True)
    assert developer_tools(settings) is True and settings.get_masked()["_meta"]["developer_tools"] is True
    settings.set("general", "developer_tools", "yes")  # true itself, nothing else
    assert developer_tools(settings) is False
    monkeypatch.setenv(DEVELOPER_TOOLS_ENV, "1")
    assert developer_tools(settings) is True and developer_tools(None) is True
    # The page can't turn them on: settings.json only.
    try:
        ws_commands._socket_setting_value("general", "developer_tools", True)
    except ValueError as refused:
        assert "can't be changed from the app" in str(refused)
    else:
        raise AssertionError("general.developer_tools changed from the page")


def test_an_evaluation_starts_only_with_them_on(tmp_path, monkeypatch):
    monkeypatch.delenv(DEVELOPER_TOOLS_ENV, raising=False)
    started = []
    settings = SettingsManager(tmp_path / "settings.json")
    state = SimpleNamespace(settings=settings, project=SimpleNamespace(project_path=str(tmp_path)),
                            evaluations=SimpleNamespace(start=lambda **kwargs: started.append(kwargs) or {"id": "eval-1"}))

    async def allowed(ctx, trigger="app"):
        return ""

    monkeypatch.setattr(ws_commands, "_oversight_refusal", allowed)
    msg = {"command": "evaluation_start", "model": "glm", "spec": "minimal", "n": 1}

    def start() -> list[dict]:
        return _run(ws_commands.HANDLERS["evaluation_start"],
                    ws_commands.CommandContext(ws=_StubWS(), state=state, msg=msg, runs=None))

    [refused] = start()
    assert refused["event"] == "error" and DEVELOPER_TOOLS_ENV in refused["message"] and started == []
    settings.set("general", "developer_tools", True)
    [event] = start()
    assert event == {"event": "evaluation_started", "record": {"id": "eval-1"}} and len(started) == 1
