"""Tests for v0.5.12a2 — engine/server.py minimal triage coverage.

The v0.5.11 coverage audit found this module at 0% — completely
untested. Investigation surfaced that the paired client side
(`resonant connect <ws_url>`) was removed in v0.4.4, leaving the
server running but with no bundled client that knows how to connect.
See the module docstring for the full triage note.

We're not putting a lot of test investment into legacy code that may
get removed entirely in a future release. These tests cover the
construction path + the import surface so import-level regressions
(broken refactors, missing dependency, signature drift) fail loudly
rather than silently.

NOT covered here (intentionally):
- handle_client async routing — needs an async websocket mock harness
  that doesn't pay off for legacy code.
- _run_session worker thread — same reason.
- start() — boots a real websockets server and runs forever; not a
  unit-test target.
"""
from __future__ import annotations

import pytest

from resonant_client.engine.server import EngineServer


class _StubBackend:
    """Minimal duck-typed Backend for EngineServer construction."""

    def __init__(self, name: str = "ollama", model: str = "deepseek-v4-flash:cloud"):
        self.name = name
        self.model = model
        self.base_url = "http://10.0.0.133:11434"
        self.api_key = None


class TestEngineServerConstruction:
    def test_default_host_and_port(self):
        server = EngineServer(backend=_StubBackend())
        assert server.host == "0.0.0.0"
        assert server.port == 8765

    def test_custom_host_and_port(self):
        server = EngineServer(
            backend=_StubBackend(), host="127.0.0.1", port=9999,
        )
        assert server.host == "127.0.0.1"
        assert server.port == 9999

    def test_max_steps_and_max_tokens_threaded_to_session(self):
        backend = _StubBackend()
        server = EngineServer(
            backend=backend, max_steps=42, max_tokens=8192,
        )
        # The Session is constructed in __init__ with these caps.
        # Reading them back via the session's exposed attribute names.
        assert server.session.max_steps == 42
        assert server.session.max_tokens == 8192

    def test_session_uses_auto_approve_true(self):
        # Server mode auto-approves tool calls because there's no
        # interactive prompt available — comment in the source.
        server = EngineServer(backend=_StubBackend())
        # Session exposes the approval mode; the server constructs with
        # auto_approve=True.
        assert getattr(server.session, "auto_approve", None) is True

    def test_clients_set_starts_empty(self):
        server = EngineServer(backend=_StubBackend())
        assert server._clients == set()

    def test_run_task_starts_none(self):
        server = EngineServer(backend=_StubBackend())
        assert server._run_task is None

    def test_backend_attribute_held_for_streaming(self):
        # The websocket handle_client path reads backend.name +
        # backend.model on connect to send a STATUS event. Verify the
        # backend reference survives construction.
        backend = _StubBackend(name="ollama", model="deepseek-v4-pro:cloud")
        server = EngineServer(backend=backend)
        assert server.backend is backend
        assert server.backend.name == "ollama"
        assert server.backend.model == "deepseek-v4-pro:cloud"


class TestEngineServerImports:
    """Catch import-level regressions in the legacy module without
    running the server. If any of these imports break, the `resonant
    serve` subcommand crashes at startup."""

    def test_engineserver_class_importable(self):
        from resonant_client.engine.server import EngineServer  # noqa: F401

    def test_module_dependencies_resolve(self):
        # The module imports from .events, .backends, and .session.
        # If any of those modules' surface area drifts, this test
        # fails at import time.
        import resonant_client.engine.server as srv
        assert hasattr(srv, "EngineServer")
        # Public-ish attributes the TUI relies on.
        assert callable(srv.EngineServer)

    def test_engineserver_has_expected_methods(self):
        # The TUI's serve mode (tui.py:1420-1431) calls .start();
        # handle_client is what websockets.serve dispatches to.
        # If either disappears, the TUI breaks silently at runtime.
        assert hasattr(EngineServer, "handle_client")
        assert hasattr(EngineServer, "start")
        assert hasattr(EngineServer, "_run_session")

    def test_module_docstring_marks_legacy_status(self):
        # Lock in the legacy marker so a future refactor that strips
        # the docstring trips this test and the next maintainer
        # consciously decides whether to drop the marker.
        import resonant_client.engine.server as srv
        assert srv.__doc__ is not None
        assert "LEGACY" in srv.__doc__
