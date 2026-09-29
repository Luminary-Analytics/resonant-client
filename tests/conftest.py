"""
Shared fixtures for Lumi test suite.

Provides reusable test helpers, temp directories, mock backends,
and tool definition factories used across all test modules.
"""

import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

# Ensure project root is importable
PROJECT_ROOT = Path(__file__).parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


# ── Credential store isolation (process-wide) ──────────────────────
# Settings keep API keys in the OS credential store (lumi/secrets_store.py).
# Tests must never read or write the developer's real Credential Manager or
# Keychain, so the store is off for the whole run; keychain tests install an
# in-memory keyring explicitly.
os.environ["LUMI_KEYCHAIN"] = "off"

# Nor may an organization policy installed on the machine (registry, managed
# preferences, ProgramData) change what the tests see. Policy tests install
# their own through lumi.policy.set_for_tests.
os.environ.pop("LUMI_POLICY_FILE", None)
import lumi.policy as _lumi_policy  # noqa: E402
import lumi.terms as _lumi_terms  # noqa: E402

_lumi_policy.set_for_tests(None)

# Lumi's terms (lumi/terms.py) must be accepted before anything reaches a
# model. In this process the autouse fixture below treats them as accepted;
# the processes tests start (lumi run, Team workers) accept them the way CI
# does, through LUMI_ACCEPT_TERMS naming the versions in force. Tests of the
# gate itself turn both off (tests/test_terms.py).
os.environ[_lumi_terms.ENVIRONMENT] = _lumi_terms.acceptance_value()


@pytest.fixture(autouse=True)
def _no_organization_policy():
    # Process-wide configuration must not leak between tests: a test whose
    # AppState turns the secret scan on (a policy can lock it) would otherwise
    # mark every later test's history in the same worker. The audit log and
    # usage records are recreated for each test, under its isolated home.
    from lumi import audit, budgets, dlp, license as lumi_license, offline, pricing, secret_scan, updater, usage
    from lumi.engine import review_gate, second_approval

    def reset():
        _lumi_policy.set_for_tests(None)
        _lumi_terms.set_for_tests(True)
        # Offline mode is process-wide too: a test that turns it on must not
        # refuse the next test's requests. The license is read once per process.
        offline.reset_for_tests()
        lumi_license.reset_for_tests()
        updater.reset_for_tests()
        secret_scan.reset()
        dlp.reset_for_tests()
        audit.set_for_tests(None)
        pricing.reset()
        usage.set_for_tests(None)
        usage.set_listener(None)
        budgets.reset()
        # Every execution policy asks the review gate; a test's settings must not reach the next test.
        review_gate.configure(None)
        review_gate.set_registrar(None)
        second_approval.set_requester(None)

    reset()
    yield
    reset()


# ── Home isolation (process-wide) ──────────────────────────────────
# Importing lumi.gui.app constructs the module-level
# `state = AppState()` singleton, which writes
# ~/.resonant/recent_projects.json. Test modules import app at module
# scope, so that write fires at COLLECTION time — before any fixture
# runs. The redirect therefore has to happen here, at conftest import
# time, for the whole pytest process. The autouse fixture below layers
# a fresh per-test home on top of this session-wide one.

_SESSION_TEST_HOME = Path(tempfile.mkdtemp(prefix="resonant-tests-home-"))
Path.home = staticmethod(lambda: _SESSION_TEST_HOME)  # type: ignore[method-assign]


# ── Machine files in temporary folders (autouse) ───────────────────
# Tests stand in for C:\ProgramData\Lumi, /etc/lumi and a Group Policy
# PolicyFile with temporary folders the test's own user owns, which the real
# check (lumi/admin_files.py) rightly refuses. Files there count as an
# administrator's; anywhere else the real check still runs.
# tests/test_machine_policy_trust.py tests the real check itself.

@pytest.fixture(autouse=True)
def _temporary_machine_files_count_as_administrators(tmp_path_factory):
    from lumi import admin_files

    # realpath on both sides: a runner's TEMP can be an 8.3 short name.
    folders = tuple(os.path.normcase(os.path.realpath(folder)).rstrip("\\/") + os.sep
                    for folder in (tmp_path_factory.getbasetemp(), _SESSION_TEST_HOME))

    def check(path, root):
        if os.path.normcase(os.path.realpath(path)).startswith(folders):
            return admin_files.Trust(True, admin_owned=True)
        return admin_files.real_check(path, root)

    admin_files.set_for_tests(check)
    yield
    admin_files.set_for_tests(None)


# ── Home isolation (autouse) ───────────────────────────────────────

@pytest.fixture(autouse=True)
def _isolated_home(tmp_path_factory, monkeypatch):
    """Point Path.home() at a per-test tmp dir for EVERY test.

    Code under lumi writes into ~/.resonant at runtime
    (recent_projects.json, projects/<hash>/sessions/, settings.json…).
    Tests that forgot to isolate used to leave pytest tmp paths in the
    user's real ~/.resonant/recent_projects.json, so isolation is now
    the default. Tests that need a specific fake home re-patch
    Path.home themselves and their patch wins for the test's duration.

    Note this covers Path.home() only — os.path.expanduser("~") and
    direct env reads still see the real home.
    """
    fake_home = tmp_path_factory.mktemp("isolated_home")
    monkeypatch.setattr(Path, "home", staticmethod(lambda: fake_home))
    return fake_home


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
    from lumi.backends import OllamaBackend

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
