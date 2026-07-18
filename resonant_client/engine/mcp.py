"""Model Context Protocol server integration.

Resonant supports local stdio servers and streamable HTTP servers. Tools are
exposed to models with the stable prefix ``mcp_<server>_<tool>``.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from typing import Any, Optional

import httpx

from resonant_client import __version__
from resonant_client.processes import background_process_kwargs

logger = logging.getLogger(__name__)

MCP_PROTOCOL_VERSION = "2024-11-05"


@dataclass
class MCPServerConfig:
    """Configuration for an stdio or streamable HTTP MCP server."""

    name: str
    command: str = ""
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    enabled: bool = True
    transport: str = "stdio"
    url: str = ""
    headers: dict[str, str] = field(default_factory=dict)

    @property
    def is_http(self) -> bool:
        return self.transport in {"http", "streamable_http"}

    @property
    def endpoint(self) -> str:
        return self.url if self.is_http else " ".join([self.command, *self.args]).strip()

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "command": self.command,
            "args": self.args,
            "env": self.env,
            "enabled": self.enabled,
            "transport": "http" if self.is_http else "stdio",
            "url": self.url,
            "headers": self.headers,
        }

    @classmethod
    def from_dict(cls, name: str, data: dict) -> "MCPServerConfig":
        data = data if isinstance(data, dict) else {}
        url = str(data.get("url", "")).strip()
        transport = str(data.get("transport", "")).strip().lower()
        if transport in {"streamable-http", "streamable_http"}:
            transport = "http"
        if not transport:
            transport = "http" if url else "stdio"
        return cls(
            name=name,
            command=str(data.get("command", "")).strip(),
            args=list(data.get("args", [])),
            env=dict(data.get("env", {})),
            enabled=bool(data.get("enabled", True)),
            transport=transport,
            url=url,
            headers=dict(data.get("headers", {})),
        )


@dataclass
class MCPTool:
    """A tool provided by an MCP server."""

    server_name: str
    name: str
    description: str = ""
    input_schema: dict = field(default_factory=dict)

    @property
    def prefixed_name(self) -> str:
        return f"mcp_{self.server_name}_{self.name}"

    def to_openai_function(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.prefixed_name,
                "description": f"[MCP:{self.server_name}] {self.description}",
                "parameters": self.input_schema or {"type": "object", "properties": {}},
            },
        }


class MCPConnection:
    """Manage one stdio or streamable HTTP MCP connection."""

    def __init__(
        self,
        config: MCPServerConfig,
        *,
        http_transport: httpx.BaseTransport | None = None,
    ):
        self.config = config
        self._process: Optional[subprocess.Popen] = None
        self._http_client: httpx.Client | None = None
        self._http_transport = http_transport
        self._session_id = ""
        self._protocol_version = MCP_PROTOCOL_VERSION
        self._tools: list[MCPTool] = []
        self._lock = threading.Lock()
        self._request_id = 0
        self.connected = False
        self.last_error = ""

    def connect(self) -> bool:
        """Open the configured transport, initialize MCP, and discover tools."""
        if not self.config.enabled:
            self.last_error = "Server is disabled"
            return False
        self.last_error = ""
        try:
            if self.config.is_http:
                if not self.config.url:
                    raise ValueError("HTTP MCP server requires a URL")
                self._http_client = httpx.Client(
                    transport=self._http_transport,
                    timeout=httpx.Timeout(15.0, connect=3.0),
                    follow_redirects=True,
                )
            else:
                if not self.config.command:
                    raise ValueError("stdio MCP server requires a command")
                env = {**dict(os.environ), **self.config.env}
                self._process = subprocess.Popen(
                    [self.config.command, *self.config.args],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    env=env,
                    shell=(sys.platform == "win32"),
                    **background_process_kwargs(),
                )

            init_result = self._send_request(
                "initialize",
                {
                    "protocolVersion": MCP_PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "resonant", "version": __version__},
                },
            )
            if not init_result or "capabilities" not in init_result:
                raise RuntimeError("MCP initialize returned no capabilities")
            self._protocol_version = str(
                init_result.get("protocolVersion") or MCP_PROTOCOL_VERSION
            )

            self._send_notification("notifications/initialized", {})
            tools_result = self._send_request("tools/list", {}) or {}
            self._tools = [
                MCPTool(
                    server_name=self.config.name,
                    name=tool["name"],
                    description=tool.get("description", ""),
                    input_schema=tool.get("inputSchema", {}),
                )
                for tool in tools_result.get("tools", [])
                if tool.get("name")
            ]
            self.connected = True
            logger.info(
                "MCP server '%s' connected over %s with %d tools",
                self.config.name,
                "HTTP" if self.config.is_http else "stdio",
                len(self._tools),
            )
            return True
        except Exception as exc:
            self.last_error = str(exc)
            logger.error("Failed to connect MCP server '%s': %s", self.config.name, exc)
            self.disconnect()
            return False

    def disconnect(self) -> None:
        """Close the active transport and clear discovered tools."""
        self.connected = False
        self._tools = []
        if self._http_client:
            try:
                if self._session_id:
                    self._http_client.delete(
                        self.config.url,
                        headers=self._http_headers(),
                    )
            except Exception:
                pass
            self._http_client.close()
            self._http_client = None
            self._session_id = ""
        if self._process:
            try:
                self._process.terminate()
                self._process.wait(timeout=5)
            except Exception:
                try:
                    self._process.kill()
                except Exception:
                    pass
            self._process = None

    def call_tool(self, tool_name: str, arguments: dict) -> dict:
        result = self._send_request(
            "tools/call",
            {"name": tool_name, "arguments": arguments},
        )
        return result if result is not None else {"error": "No response from MCP server"}

    @property
    def tools(self) -> list[MCPTool]:
        return self._tools

    def _send_request(self, method: str, params: dict) -> Optional[dict]:
        with self._lock:
            self._request_id += 1
            request_id = self._request_id
            request = {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": method,
                "params": params,
            }
            response = self._send_payload(request, expect_response=True)

        if not response:
            return None
        if "error" in response:
            logger.error("MCP error from '%s': %s", self.config.name, response["error"])
            return None
        result = response.get("result")
        return result if isinstance(result, dict) else None

    def _send_notification(self, method: str, params: dict) -> None:
        notification = {"jsonrpc": "2.0", "method": method, "params": params}
        try:
            self._send_payload(notification, expect_response=False)
        except Exception as exc:
            logger.error("MCP notification error from '%s': %s", self.config.name, exc)

    def _send_payload(self, payload: dict, *, expect_response: bool) -> dict | None:
        if self.config.is_http:
            return self._send_http_payload(payload, expect_response=expect_response)
        return self._send_stdio_payload(payload, expect_response=expect_response)

    def _send_stdio_payload(self, payload: dict, *, expect_response: bool) -> dict | None:
        if not self._process or self._process.poll() is not None:
            return None
        assert self._process.stdin is not None
        self._process.stdin.write(json.dumps(payload) + "\n")
        self._process.stdin.flush()
        if not expect_response:
            return None
        assert self._process.stdout is not None
        line = self._process.stdout.readline()
        return json.loads(line.strip()) if line else None

    def _send_http_payload(self, payload: dict, *, expect_response: bool) -> dict | None:
        if not self._http_client:
            return None
        response = self._http_client.post(
            self.config.url,
            headers=self._http_headers(),
            json=payload,
        )
        response.raise_for_status()
        session_id = response.headers.get("Mcp-Session-Id")
        if session_id:
            self._session_id = session_id
        if not expect_response or not response.content:
            return None
        return self._parse_http_response(response, payload.get("id"))

    def _http_headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            "MCP-Protocol-Version": self._protocol_version,
            **self.config.headers,
        }
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        return headers

    @staticmethod
    def _parse_http_response(response: httpx.Response, request_id: Any) -> dict | None:
        content_type = response.headers.get("content-type", "").lower()
        if "text/event-stream" not in content_type:
            data = response.json()
            return data if isinstance(data, dict) else None

        for line in response.text.splitlines():
            if not line.startswith("data:"):
                continue
            raw = line[5:].strip()
            if not raw:
                continue
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if isinstance(data, dict) and (request_id is None or data.get("id") == request_id):
                return data
        return None


class MCPManager:
    """Manage user-configured MCP server connections."""

    def __init__(self, settings=None):
        self._settings = settings
        self._connections: dict[str, MCPConnection] = {}
        self._errors: dict[str, str] = {}
        self._lock = threading.Lock()

    def connect(self, server_name: str, config: Optional[MCPServerConfig] = None) -> bool:
        if not config and self._settings:
            servers = self._settings.get("mcp_servers") or {}
            if server_name in servers:
                config = MCPServerConfig.from_dict(server_name, servers[server_name])
        if not config:
            logger.error("No config for MCP server '%s'", server_name)
            return False
        if not config.enabled:
            logger.info("MCP server '%s' is disabled", server_name)
            return False

        with self._lock:
            old = self._connections.pop(server_name, None)
        if old:
            old.disconnect()

        connection = MCPConnection(config)
        if not connection.connect():
            with self._lock:
                self._errors[server_name] = connection.last_error or "Connection failed"
            return False
        with self._lock:
            self._connections[server_name] = connection
            self._errors.pop(server_name, None)
        return True

    def disconnect(self, server_name: str) -> None:
        with self._lock:
            connection = self._connections.pop(server_name, None)
            self._errors.pop(server_name, None)
        if connection:
            connection.disconnect()

    def disconnect_all(self) -> None:
        with self._lock:
            connections = list(self._connections.values())
            self._connections.clear()
        for connection in connections:
            connection.disconnect()

    def get_all_tools(self) -> list[dict]:
        with self._lock:
            connections = list(self._connections.values())
        return [
            tool.to_openai_function()
            for connection in connections
            if connection.connected
            for tool in connection.tools
        ]

    def call_tool(self, prefixed_name: str, arguments: dict) -> dict:
        if not prefixed_name.startswith("mcp_"):
            return {"error": f"Invalid MCP tool name: {prefixed_name}"}

        suffix = prefixed_name[4:]
        candidates: set[str] = set()
        if self._settings:
            configured = self._settings.get("mcp_servers") or {}
            candidates.update(configured.keys())
        with self._lock:
            candidates.update(self._connections.keys())

        server_name = ""
        tool_name = ""
        for candidate in sorted(candidates, key=len, reverse=True):
            prefix = f"{candidate}_"
            if suffix.startswith(prefix):
                server_name = candidate
                tool_name = suffix[len(prefix):]
                break
        if not server_name or not tool_name:
            return {"error": f"Invalid MCP tool name: {prefixed_name}"}

        with self._lock:
            connection = self._connections.get(server_name)
        if not connection or not connection.connected:
            return {"error": f"MCP server '{server_name}' not connected"}
        return connection.call_tool(tool_name, arguments)

    def health_check(self) -> dict:
        with self._lock:
            return {
                name: {"connected": conn.connected, "tools": len(conn.tools)}
                for name, conn in self._connections.items()
            }

    def list_servers(self) -> list[dict]:
        servers = []
        configured = self._settings.get("mcp_servers") if self._settings else {}
        for name, data in (configured or {}).items():
            config = MCPServerConfig.from_dict(name, data)
            with self._lock:
                connection = self._connections.get(name)
                error = self._errors.get(name, "")
            servers.append(
                {
                    "name": name,
                    "transport": "http" if config.is_http else "stdio",
                    "url": config.url,
                    "command": config.command,
                    "endpoint": config.endpoint,
                    "enabled": config.enabled,
                    "connected": connection.connected if connection else False,
                    "tools": len(connection.tools) if connection and connection.connected else 0,
                    "error": error,
                }
            )
        return servers
