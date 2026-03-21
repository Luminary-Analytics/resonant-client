"""
MCP (Model Context Protocol) Server Integration for Resonant Engine.

Manages connections to MCP servers and routes tool calls to them.
MCP tools are exposed with the prefix: mcp_<server>_<tool>
"""

import json
import logging
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger(__name__)


@dataclass
class MCPServerConfig:
    """Configuration for an MCP server."""
    name: str
    command: str  # Command to start the server
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    enabled: bool = True

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "command": self.command,
            "args": self.args,
            "env": self.env,
            "enabled": self.enabled,
        }

    @classmethod
    def from_dict(cls, name: str, data: dict) -> "MCPServerConfig":
        return cls(
            name=name,
            command=data.get("command", ""),
            args=data.get("args", []),
            env=data.get("env", {}),
            enabled=data.get("enabled", True),
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
        """Convert to OpenAI function calling format."""
        return {
            "type": "function",
            "function": {
                "name": self.prefixed_name,
                "description": f"[MCP:{self.server_name}] {self.description}",
                "parameters": self.input_schema or {"type": "object", "properties": {}},
            },
        }


class MCPConnection:
    """Manages a single MCP server connection via stdio."""

    def __init__(self, config: MCPServerConfig):
        self.config = config
        self._process: Optional[subprocess.Popen] = None
        self._tools: list[MCPTool] = []
        self._lock = threading.Lock()
        self._request_id = 0
        self.connected = False

    def connect(self) -> bool:
        """Start the MCP server process and initialize."""
        try:
            env = {**dict(__import__('os').environ), **self.config.env}
            cmd = [self.config.command] + self.config.args

            self._process = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
                shell=(sys.platform == "win32"),
            )

            # Send initialize request
            init_result = self._send_request("initialize", {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "resonant", "version": "0.1.0"},
            })

            if init_result and "capabilities" in init_result:
                # Send initialized notification
                self._send_notification("notifications/initialized", {})

                # Fetch tools
                tools_result = self._send_request("tools/list", {})
                if tools_result and "tools" in tools_result:
                    self._tools = [
                        MCPTool(
                            server_name=self.config.name,
                            name=t["name"],
                            description=t.get("description", ""),
                            input_schema=t.get("inputSchema", {}),
                        )
                        for t in tools_result["tools"]
                    ]
                self.connected = True
                logger.info(f"MCP server '{self.config.name}' connected with {len(self._tools)} tools")
                return True

        except Exception as e:
            logger.error(f"Failed to connect MCP server '{self.config.name}': {e}")
            self.disconnect()

        return False

    def disconnect(self):
        """Stop the MCP server process."""
        self.connected = False
        self._tools = []
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
        """Call a tool on this MCP server."""
        result = self._send_request("tools/call", {
            "name": tool_name,
            "arguments": arguments,
        })
        if result is None:
            return {"error": "No response from MCP server"}
        return result

    @property
    def tools(self) -> list[MCPTool]:
        return self._tools

    def _send_request(self, method: str, params: dict) -> Optional[dict]:
        """Send a JSON-RPC request and wait for response."""
        if not self._process or self._process.poll() is not None:
            return None

        with self._lock:
            self._request_id += 1
            req_id = self._request_id

        request = {
            "jsonrpc": "2.0",
            "id": req_id,
            "method": method,
            "params": params,
        }

        try:
            msg = json.dumps(request) + "\n"
            self._process.stdin.write(msg)
            self._process.stdin.flush()

            # Read response line
            line = self._process.stdout.readline()
            if not line:
                return None

            response = json.loads(line.strip())
            if "result" in response:
                return response["result"]
            if "error" in response:
                logger.error(f"MCP error: {response['error']}")
                return None

        except Exception as e:
            logger.error(f"MCP request error: {e}")

        return None

    def _send_notification(self, method: str, params: dict):
        """Send a JSON-RPC notification (no response expected)."""
        if not self._process or self._process.poll() is not None:
            return

        notification = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
        }

        try:
            msg = json.dumps(notification) + "\n"
            self._process.stdin.write(msg)
            self._process.stdin.flush()
        except Exception as e:
            logger.error(f"MCP notification error: {e}")


class MCPManager:
    """Manages multiple MCP server connections."""

    def __init__(self, settings=None):
        self._settings = settings
        self._connections: dict[str, MCPConnection] = {}
        self._lock = threading.Lock()

    def connect(self, server_name: str, config: Optional[MCPServerConfig] = None) -> bool:
        """Connect to an MCP server."""
        if not config and self._settings:
            servers = self._settings.get("mcp_servers") or {}
            if server_name in servers:
                config = MCPServerConfig.from_dict(server_name, servers[server_name])

        if not config:
            logger.error(f"No config for MCP server '{server_name}'")
            return False

        with self._lock:
            # Disconnect existing
            if server_name in self._connections:
                self._connections[server_name].disconnect()

            conn = MCPConnection(config)
            if conn.connect():
                self._connections[server_name] = conn
                return True
            return False

    def disconnect(self, server_name: str):
        """Disconnect an MCP server."""
        with self._lock:
            conn = self._connections.pop(server_name, None)
            if conn:
                conn.disconnect()

    def disconnect_all(self):
        """Disconnect all MCP servers."""
        with self._lock:
            for conn in self._connections.values():
                conn.disconnect()
            self._connections.clear()

    def get_all_tools(self) -> list[dict]:
        """Get all tools from all connected MCP servers in OpenAI function format."""
        tools = []
        with self._lock:
            for conn in self._connections.values():
                if conn.connected:
                    tools.extend(t.to_openai_function() for t in conn.tools)
        return tools

    def call_tool(self, prefixed_name: str, arguments: dict) -> dict:
        """Call an MCP tool by its prefixed name (mcp_server_tool)."""
        parts = prefixed_name.split("_", 2)
        if len(parts) < 3 or parts[0] != "mcp":
            return {"error": f"Invalid MCP tool name: {prefixed_name}"}

        server_name = parts[1]
        tool_name = parts[2]

        with self._lock:
            conn = self._connections.get(server_name)

        if not conn or not conn.connected:
            return {"error": f"MCP server '{server_name}' not connected"}

        return conn.call_tool(tool_name, arguments)

    def health_check(self) -> dict:
        """Return health status of all configured servers."""
        result = {}
        with self._lock:
            for name, conn in self._connections.items():
                result[name] = {
                    "connected": conn.connected,
                    "tools": len(conn.tools),
                }
        return result

    def list_servers(self) -> list[dict]:
        """List all servers with their status."""
        servers = []
        # From settings
        if self._settings:
            configured = self._settings.get("mcp_servers") or {}
            for name, data in configured.items():
                with self._lock:
                    conn = self._connections.get(name)
                servers.append({
                    "name": name,
                    "command": data.get("command", ""),
                    "enabled": data.get("enabled", True),
                    "connected": conn.connected if conn else False,
                    "tools": len(conn.tools) if conn and conn.connected else 0,
                })
        return servers
