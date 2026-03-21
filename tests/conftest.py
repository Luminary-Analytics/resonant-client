"""
Shared fixtures for Resonant Client test suite.

Provides reusable test helpers, temp directories, mock backends,
and tool definition factories used across all test modules.
"""

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Ensure project root is importable
PROJECT_ROOT = Path(__file__).parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


# ── Fixtures: Temporary directories ────────────────────────────────

@pytest.fixture
def tmp_project(tmp_path):
    """Create a temporary project directory with sample source files."""
    proj = tmp_path / "test_project"
    proj.mkdir()

    # Python file
    (proj / "main.py").write_text(
        "import os\nimport sys\n\n"
        "class App:\n    pass\n\n"
        "def main():\n    print('hello')\n\n"
        "async def fetch_data():\n    pass\n",
        encoding="utf-8",
    )

    # JavaScript file
    (proj / "app.js").write_text(
        "import React from 'react';\n"
        "import { useState } from 'react';\n"
        "const fetch = require('node-fetch');\n\n"
        "function App() { return null; }\n"
        "function* generateItems() { yield 1; }\n"
        "const helper = (x) => x + 1;\n"
        "class Widget {}\n"
        "export default function Main() {}\n",
        encoding="utf-8",
    )

    # Go file
    (proj / "server.go").write_text(
        'package main\n\nimport "fmt"\n\n'
        'type Server struct{}\n\n'
        'func (s *Server) Start() {}\n'
        'func main() { fmt.Println("go") }\n',
        encoding="utf-8",
    )

    # Nested directory
    sub = proj / "lib"
    sub.mkdir()
    (sub / "auth.py").write_text(
        "class Authenticator:\n    def login(self): pass\n"
        "def verify_token(token): pass\n",
        encoding="utf-8",
    )

    # Config file
    (proj / "config.yaml").write_text("server:\n  port: 8080\n", encoding="utf-8")

    # .resonant dir (for cache tests)
    (proj / ".resonant").mkdir()

    return proj


@pytest.fixture
def tmp_file(tmp_path):
    """Factory fixture: create a temp file with given content."""
    created = []

    def _make(content: str, name: str = "test.txt", encoding: str = "utf-8",
              newline: str = None):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding=encoding, newline=newline) as f:
            f.write(content)
        created.append(path)
        return str(path)

    yield _make

    # Cleanup
    for p in created:
        if p.exists():
            p.unlink()


# ── Fixtures: Tool definitions ─────────────────────────────────────

@pytest.fixture
def make_tool():
    """Factory: create an OpenAI-format tool definition."""
    def _make(name: str, desc: str = "A tool", params: dict = None):
        if params is None:
            params = {
                "type": "object",
                "properties": {"arg1": {"type": "string"}},
                "required": ["arg1"],
            }
        return {
            "type": "function",
            "function": {
                "name": name,
                "description": desc,
                "parameters": params,
            },
        }
    return _make


@pytest.fixture
def sample_tools(make_tool):
    """A standard set of tool definitions for testing."""
    return [
        make_tool("bash", "Execute a shell command", {
            "type": "object",
            "properties": {"command": {"type": "string", "description": "The command to run"}},
            "required": ["command"],
        }),
        make_tool("file_read", "Read a file", {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        }),
        make_tool("file_edit", "Edit a file", {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "old_text": {"type": "string"},
                "new_text": {"type": "string"},
            },
            "required": ["path", "old_text", "new_text"],
        }),
        make_tool("file_write", "Write a file", {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["path", "content"],
        }),
    ]


# ── Fixtures: Mock backend ─────────────────────────────────────────

@pytest.fixture
def mock_ollama_backend():
    """Create a mock OllamaBackend without network access."""
    from resonant_client.backends import OllamaBackend

    # Clear the class-level cache
    OllamaBackend._tool_support_cache.clear()

    backend = OllamaBackend("http://10.0.0.133:11434", "llama3.1:8b")
    return backend


# ── Helpers ────────────────────────────────────────────────────────

def make_tool_call_xml(name: str, args: dict) -> str:
    """Build a <tool_call> XML block."""
    return f'<tool_call>\n{json.dumps({"name": name, "arguments": args})}\n</tool_call>'


def make_tool_call_response(text: str = "", tool_calls: list = None) -> str:
    """Build a model response with optional text and tool calls."""
    parts = []
    if text:
        parts.append(text)
    for tc in (tool_calls or []):
        parts.append(make_tool_call_xml(tc["name"], tc.get("arguments", {})))
    return "\n".join(parts)
