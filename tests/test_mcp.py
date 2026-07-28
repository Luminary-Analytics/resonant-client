"""MCP transport and the optional BrowserOS profile."""

from __future__ import annotations

import json

import httpx

from resonant_client.engine.mcp import MCPConnection, MCPManager, MCPServerConfig
from resonant_client.engine.tools import AGENT_TOOLS
from resonant_client.gui.settings import SettingsManager


def test_no_mcp_server_is_configured_by_default(tmp_path):
    """Nothing ships pre-configured.

    BrowserOS was the default browser MCP while browsing needed an external
    server. Browsing is native as of v0.11.15, so the entry only produced a
    "tool server unavailable" notice for something nobody was running. It was
    removed outright in v0.11.16, including from existing settings files —
    see tests/test_settings_migration.py.
    """
    settings = SettingsManager(tmp_path / "settings.json")

    assert settings.get("mcp_servers") == {}
    assert MCPManager(settings).list_servers() == []


def test_http_config_is_inferred_from_url():
    config = MCPServerConfig.from_dict("remote", {"url": "http://localhost:8080/mcp"})

    assert config.is_http
    assert config.endpoint == "http://localhost:8080/mcp"


def test_streamable_http_connect_discovers_and_calls_tools():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "DELETE":
            return httpx.Response(204)
        payload = json.loads(request.content)
        if payload.get("method") == "notifications/initialized":
            return httpx.Response(202)
        if payload["method"] == "initialize":
            return httpx.Response(
                200,
                headers={"Mcp-Session-Id": "browseros-session"},
                json={
                    "jsonrpc": "2.0",
                    "id": payload["id"],
                    "result": {"capabilities": {"tools": {}}},
                },
            )
        if payload["method"] == "tools/list":
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": payload["id"],
                    "result": {
                        "tools": [{
                            "name": "navigate_page",
                            "description": "Navigate the active BrowserOS tab.",
                            "inputSchema": {
                                "type": "object",
                                "properties": {"url": {"type": "string"}},
                                "required": ["url"],
                            },
                        }],
                    },
                },
            )
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": payload["id"],
                "result": {"content": [{"type": "text", "text": "navigated"}]},
            },
        )

    connection = MCPConnection(
        MCPServerConfig(
            name="browseros",
            transport="http",
            url="http://127.0.0.1:9239/mcp",
        ),
        http_transport=httpx.MockTransport(handler),
    )

    assert connection.connect()
    assert connection.tools[0].prefixed_name == "mcp_browseros_navigate_page"
    assert connection.call_tool("navigate_page", {"url": "https://example.com"}) == {
        "content": [{"type": "text", "text": "navigated"}],
    }
    assert all(
        request.headers.get("mcp-session-id") == "browseros-session"
        for request in requests[1:]
    )
    assert all(
        request.headers.get("mcp-protocol-version") == "2024-11-05"
        for request in requests
    )
    connection.disconnect()
    assert requests[-1].method == "DELETE"


def test_streamable_http_sse_response_is_supported():
    payload = {
        "jsonrpc": "2.0",
        "id": 7,
        "result": {"tools": []},
    }
    response = httpx.Response(
        200,
        headers={"content-type": "text/event-stream"},
        text=f"event: message\ndata: {json.dumps(payload)}\n\n",
    )

    assert MCPConnection._parse_http_response(response, 7) == payload


def test_builtin_browser_tools_are_advertised():
    """Browsing is a built-in capability again.

    This test previously asserted the opposite: between v0.9.13 and v0.11.15
    browsing came only from an external MCP server, because the native
    implementation had been built on Playwright and cost 150+ MB. The
    replacement speaks CDP directly and adds nothing to the bundle, so the
    tools are back and must be advertised without any server configured.
    """
    names = {tool["function"]["name"] for tool in AGENT_TOOLS}

    assert {name for name in names if name.startswith("browser_")} == {
        "browser_navigate", "browser_click", "browser_type", "browser_read",
        "browser_screenshot", "browser_js", "browser_scroll", "browser_hover",
        "browser_select", "browser_wait", "browser_back", "browser_tabs",
    }
