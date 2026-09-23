"""Editor setup, native MCP execution, CLI handoff and transport regressions."""

import asyncio
import base64
import json
import sys
from types import SimpleNamespace

import httpx
import pytest

from resonant_client.backends import CodexCliBackend, ClaudeCodeCliBackend, _build_codex_prompt
from resonant_client.engine.editor_integrations import build_config, catalog, cli_arguments, workflow_instructions
from resonant_client.engine.mcp import MCPConnection, MCPManager, MCPServerConfig, normalize_tool_result
from resonant_client.gui.settings import SettingsManager
from resonant_client.gui.ws_commands import CommandContext, HANDLERS


@pytest.fixture
def settings(tmp_path):
    return SettingsManager(tmp_path / "settings.json")


@pytest.mark.parametrize("value", ["https://127.0.0.1/mcp", "http://example.com/mcp", "http://localhost:0/mcp",
                                  "http://key:secret@localhost/mcp", "http://localhost/mcp?key=secret"])
def test_managed_unity_endpoint_is_local_and_secret_free(value):
    with pytest.raises(ValueError):
        build_config("unity", value)


def test_blender_and_unreal_configs_are_argument_vectors(tmp_path, monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: f"C:/Program Files/uv/{name}.exe")
    blender = build_config("blender", "9877")
    assert blender["args"] == ["blender-mcp==1.9.1"]
    assert blender["env"] == {"BLENDER_HOST": "127.0.0.1", "BLENDER_PORT": "9877", "DISABLE_TELEMETRY": "true"}
    folder = tmp_path / "Unreal bridge with spaces"
    folder.mkdir()
    (folder / "unreal_mcp_server.py").write_text("", encoding="utf-8")
    unreal = build_config("unreal", str(folder))
    assert unreal["args"] == ["--directory", str(folder), "run", "unreal_mcp_server.py"]
    with pytest.raises(ValueError):
        build_config("unreal", str(tmp_path))
    with pytest.raises(ValueError):
        build_config("blender", "70000")


def test_missing_dependency_is_actionable(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda _: None)
    with pytest.raises(ValueError, match="Install uv"):
        build_config("blender", "9876")


def test_default_catalog_does_not_launch_or_enable_anything(settings):
    rows = catalog(settings, MCPManager(settings))
    assert [row["id"] for row in rows] == ["blender", "unity", "unreal"]
    assert all(not row["enabled"] and not row["connected"] for row in rows)
    assert not workflow_instructions(settings)
    assert not cli_arguments(settings, "codex")


def test_cli_config_is_per_launch_and_does_not_forward_arbitrary_secrets(settings):
    cfg = build_config("unity", "http://localhost:8080/mcp")
    cfg["headers"] = {"Authorization": "private-value"}
    settings.set("mcp_servers", "resonant_unity", cfg)
    codex = CodexCliBackend("gpt-6-astra", cli_path="codex", permission_mode="bypass")
    codex._editor_settings = settings
    command = codex._command()
    assert 'mcp_servers.resonant_unity.url="http://localhost:8080/mcp"' in command
    assert "private-value" not in str(command)
    assert command[-1] == "-" and codex.model == "gpt-6-astra"
    claude = ClaudeCodeCliBackend("sonnet", cli_path="claude", permission_mode="bypass")
    claude._editor_settings = settings
    command = claude._command()
    config = json.loads(command[command.index("--mcp-config") + 1])
    assert config["mcpServers"]["resonant_unity"] == {"type": "http", "url": cfg["url"]}
    for backend in (codex, claude):
        backend.configure_permission_mode("plan")
        assert not any("resonant_unity" in arg for arg in backend._command())
    settings.set("mcp_servers", "resonant_unity", {**cfg, "enabled": False})
    assert cli_arguments(settings, "codex") == []


def test_editor_guidance_survives_cli_handoff(settings):
    settings.set("mcp_servers", "resonant_unity", build_config("unity", ""))
    guidance = workflow_instructions(settings)
    prompt = _build_codex_prompt(user_msg="Build a level", conversation_history=[], cwd="fixture",
                                instructions=f"--- CREATIVE EDITORS ---\n{guidance}\n--- END CREATIVE EDITORS ---")
    assert "Wait for compilation/domain reload" in prompt
    assert "outside Resonant's path sandbox" in prompt


def test_setup_connect_probe_and_disable(settings, monkeypatch):
    called = []
    def transport(request):
        if request.method == "DELETE":
            return httpx.Response(204)
        payload = json.loads(request.content)
        method = payload["method"]
        if method == "notifications/initialized":
            return httpx.Response(202)
        if method == "initialize":
            result = {"capabilities": {"tools": {}}}
        elif method == "tools/list":
            result = {"tools": [{"name": "manage_scene", "inputSchema": {"type": "object"}}]}
        else:
            called.append(payload["params"])
            result = {"content": [{"type": "text", "text": '{"scene":"Fixture","objects":[]}'}]}
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": payload["id"], "result": result})
    original = MCPConnection
    monkeypatch.setattr("resonant_client.engine.mcp.MCPConnection",
                        lambda cfg: original(cfg, http_transport=httpx.MockTransport(transport)))
    state = SimpleNamespace(settings=settings, mcp_manager=MCPManager(settings),
                            session=SimpleNamespace(mcp_tools=[]))
    sent = []
    async def send(payload):
        sent.append(payload)
    def run(command, **message):
        ctx = CommandContext(ws=SimpleNamespace(send_json=send), state=state,
                             msg={"editor": "unity", **message}, runs=SimpleNamespace(busy=False))
        asyncio.run(HANDLERS[command](ctx))
    run("editor_connect", value="http://localhost:8080/mcp")
    assert catalog(settings, state.mcp_manager)[1]["connected"]
    assert state.session.mcp_tools[0]["function"]["name"] == "mcp_resonant_unity_manage_scene"
    run("editor_check", code="must not execute", arguments={"action": "delete"})
    assert called == [{"name": "manage_scene", "arguments": {"action": "get_hierarchy"}}]
    assert '"Fixture"' in sent[-1]["output"]
    run("editor_connect", action="disconnect")
    assert not state.session.mcp_tools
    assert not settings.get("mcp_servers", "resonant_unity")["enabled"]


def test_blender_scene_check_supplies_required_intent_and_ignores_injected_arguments():
    calls, sent = [], []
    def call_tool(name, args):
        assert name == 'mcp_resonant_blender_get_scene_info'
        assert isinstance(args.get('user_prompt'), str) and args['user_prompt']
        calls.append((name, args))
        return {'content': [{'type': 'text', 'text': '{"object_count":0}'}]}
    async def send(value):
        sent.append(value)
    ctx = CommandContext(ws=SimpleNamespace(send_json=send),
        state=SimpleNamespace(mcp_manager=SimpleNamespace(call_tool=call_tool)),
        msg={'editor': 'blender', 'arguments': {'code': 'must not execute'}, 'user_prompt': 'injected'},
        runs=SimpleNamespace(busy=False))
    asyncio.run(HANDLERS['editor_check'](ctx))
    assert calls == [('mcp_resonant_blender_get_scene_info', {'user_prompt': 'Check editor'})]
    assert sent[-1]['is_error'] is False and 'object_count' in sent[-1]['output']


def test_connection_change_is_rejected_during_a_run(settings):
    state = SimpleNamespace(settings=settings, mcp_manager=MCPManager(settings), session=None)
    sent = []
    async def send(payload):
        sent.append(payload)
    ctx = CommandContext(ws=SimpleNamespace(send_json=send), state=state,
                         msg={"editor": "unity"}, runs=SimpleNamespace(busy=True))
    asyncio.run(HANDLERS["editor_connect"](ctx))
    assert settings.get("mcp_servers") == {}
    assert "Stop the active run" in sent[0]["message"]


def test_mcp_image_is_preserved_without_base64_text_dump():
    data = base64.b64encode(b"fixture image").decode()
    text, meta = normalize_tool_result({"content": [
        {"type": "text", "text": "Scene rendered"},
        {"type": "image", "data": data, "mimeType": "image/png"},
    ]})
    assert "Scene rendered" in text and data not in text
    assert meta == {"screenshot_b64": data, "media_type": "image/png"}
    _, meta = normalize_tool_result({"content": [{"type": "image", "data": "!invalid", "mimeType": "image/png"}]})
    assert meta == {}


def test_stdio_handles_notifications_and_tool_pagination(tmp_path):
    script = tmp_path / "fixture_bridge.py"
    script.write_text('''import sys,json
for line in sys.stdin:
    msg=json.loads(line)
    if "id" not in msg: continue
    print(json.dumps({"jsonrpc":"2.0","method":"notifications/progress","params":{}}),flush=True)
    if msg["method"]=="initialize": result={"capabilities":{"tools":{}}}
    elif msg["method"]=="tools/list":
        result={"tools":[{"name":"second"}]} if msg["params"].get("cursor") else {"tools":[{"name":"first"}],"nextCursor":"page2"}
    else: result={"content":[{"type":"text","text":"fixture scene"}]}
    print(json.dumps({"jsonrpc":"2.0","id":msg["id"],"result":result}),flush=True)
''', encoding="utf-8")
    connection = MCPConnection(MCPServerConfig(name="fixture", command=sys.executable, args=[str(script)]))
    try:
        assert connection.connect()
        assert [tool.name for tool in connection.tools] == ["first", "second"]
        assert connection.call_tool("second", {})["content"][0]["text"] == "fixture scene"
        assert "error" in connection.call_tool("invented", {})
    finally:
        connection.disconnect()


def test_unresponsive_stdio_startup_is_bounded_and_process_stopped(tmp_path):
    script = tmp_path / "unresponsive.py"
    script.write_text("import time; time.sleep(60)", encoding="utf-8")
    connection = MCPConnection(MCPServerConfig(name="fixture", command=sys.executable, args=[str(script)]))
    connection.request_timeout = 0.1
    assert not connection.connect()
    assert "deadline" in connection.last_error
    assert connection._process is None


def test_resource_read_adapts_editor_state_for_native_models():
    methods = []
    def transport(request):
        if request.method == "DELETE":
            return httpx.Response(204)
        msg = json.loads(request.content)
        methods.append(msg["method"])
        if msg["method"] == "notifications/initialized":
            return httpx.Response(202)
        results = {
            "initialize": {"capabilities": {"tools": {}, "resources": {}}},
            "tools/list": {"tools": []},
            "resources/list": {"resources": [{"uri": "unity://editor/state", "name": "State"}]},
            "resources/read": {"contents": [{"uri": "unity://editor/state", "text": "Compiling scripts"}]},
        }
        return httpx.Response(200, json={"id": msg["id"], "result": results[msg["method"]]})
    connection = MCPConnection(MCPServerConfig(name="fixture", transport="http", url="http://localhost/mcp"),
                               http_transport=httpx.MockTransport(transport))
    try:
        assert connection.connect()
        resources = connection.call_tool("resonant_list_resources", {})
        assert "unity://editor/state" in resources["content"][0]["text"]
        output, _ = normalize_tool_result(connection.call_tool("resonant_read_resource", {"uri": "unity://editor/state"}))
        assert output == "Compiling scripts"
        assert "resources/read" in methods
    finally:
        connection.disconnect()


def test_auto_edit_does_not_auto_approve_editor_code():
    from resonant_client.engine.session import Session
    session = Session.__new__(Session)
    session.autonomy_tier = "auto-edit"
    assert not session._should_auto_approve("mcp_resonant_blender_execute_blender_code")
    assert session._should_auto_approve("file_edit")
