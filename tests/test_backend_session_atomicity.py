"""Regression tests for the "No backend selected" lie.

`create_backend` assigned `self.backend` before building the session. When
session construction raised — a misconfigured MCP server is the common cause,
since `_wire_session` calls `mcp_manager.get_all_tools()` unguarded — the
backend stayed assigned and the session stayed None.

That split is what the user sees: `get_init_data` reports the model from
`self.backend`, so the composer confidently shows "Kimi K3", while the send
path checks `self.session` and answers "No backend selected". Worse,
`ensure_default_runtime_session` returns early when `self.backend` is set, so
nothing ever rebuilds it — the app stays wedged in a state whose UI claims it
is fine.
"""

import importlib
from pathlib import Path
from types import SimpleNamespace

import pytest


class _DummyBackend:
    def __init__(self, name="kimi", model="kimi-k3"):
        self.name = name
        self.model = model
        self.handles_tools = False


def _load_app_module(monkeypatch, cwd: Path):
    monkeypatch.setattr(Path, "home", lambda: cwd)
    monkeypatch.chdir(cwd)
    import resonant_client.gui.app as app_module

    return importlib.reload(app_module)


@pytest.fixture
def state(monkeypatch, tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / ".resonant").mkdir()
    app_module = _load_app_module(monkeypatch, project)
    monkeypatch.setattr(app_module.AppState, "detect_backends", lambda self: {})
    st = app_module.AppState()
    # A spec whose backend construction always succeeds; the failure under
    # test happens later, while wiring the session.
    monkeypatch.setattr(
        app_module.AppState,
        "build_backend_spec",
        lambda self, backend_type, model=None, project_path=None: SimpleNamespace(
            backend_type=backend_type,
            thinking_mode="",
            create_backend=lambda settings: _DummyBackend(backend_type, model or "m"),
        ),
    )
    return st


def test_failed_session_build_does_not_leave_a_phantom_backend(state):
    """A session that fails to build must not leave `backend` set.

    Otherwise the composer reports a live model while every send is
    rejected with "No backend selected".
    """
    def _boom(*args, **kwargs):
        raise RuntimeError("mcp server 'browseros' failed to start")

    state.build_session = _boom

    with pytest.raises(RuntimeError):
        state.create_backend("kimi", "kimi-k3")

    assert state.session is None
    assert state.backend is None, (
        "create_backend left a backend assigned after session construction "
        "failed — the composer will show this model and refuse to use it"
    )
    assert state.backend_spec is None


def test_wedged_runtime_can_be_rebuilt(state, monkeypatch):
    """Recovery must be possible after a failed build.

    `ensure_default_runtime_session` short-circuits on a set `backend`. If a
    failed build leaves one behind, the app can never rebuild its runtime and
    the user is stuck until restart.
    """
    calls = []

    def _boom(*args, **kwargs):
        calls.append("fail")
        raise RuntimeError("mcp server 'browseros' failed to start")

    state.build_session = _boom
    with pytest.raises(RuntimeError):
        state.create_backend("kimi", "kimi-k3")

    # Now the MCP server is fixed and session construction works.
    state.build_session = lambda *a, **k: SimpleNamespace(
        director_run=None, conversation_history=[], auto_approve=True
    )
    monkeypatch.setattr(
        type(state), "project_chat_backend_choice", lambda self: ("kimi", "kimi-k3")
    )
    monkeypatch.setattr(type(state), "apply_permission_mode", lambda self, *a, **k: None)

    assert state.ensure_default_runtime_session() is True, (
        "runtime stayed wedged after the underlying failure was resolved"
    )
    assert state.session is not None


def test_switching_model_after_a_failed_restore_does_not_leave_a_phantom(state):
    """The path a user actually takes to recover.

    They open a session whose runtime failed, see "No backend selected", and
    pick a model from the dropdown. That fires `switch_model` -> `swap_backend`,
    which finds `session is None` and delegates to `create_backend`. If
    swap_backend has already published `self.backend` by then, create_backend's
    rollback restores *that* backend and the phantom survives the very action
    meant to fix it.
    """
    state.build_session = lambda *a, **k: (_ for _ in ()).throw(
        RuntimeError("mcp server 'browseros' failed to start")
    )

    with pytest.raises(RuntimeError):
        state.swap_backend("kimi", "kimi-k3")

    assert state.session is None
    assert state.backend is None, (
        "swap_backend published a backend before delegating to create_backend, "
        "so the rollback restored the broken one"
    )


def test_runtime_reason_names_the_real_failure(state):
    """The user-facing message must not claim nothing was selected."""
    assert "No model selected" in state.runtime_unavailable_reason()

    state.build_session = lambda *a, **k: (_ for _ in ()).throw(
        RuntimeError("mcp server 'browseros' failed to start")
    )
    with pytest.raises(RuntimeError):
        state.create_backend("kimi", "kimi-k3")

    reason = state.runtime_unavailable_reason()
    assert "browseros" in reason, f"reason lost the underlying cause: {reason!r}"
    assert "kimi" in reason
    assert "No backend selected" not in reason


def test_init_data_reports_runtime_readiness_separately_from_backend(state, monkeypatch):
    """`current_backend` must not be the UI's proxy for "can I send?".

    Conflating them is what let the composer advertise a model while every
    send was refused.
    """
    monkeypatch.setattr(type(state), "detect_backends", lambda self: {})
    data = state.get_init_data(refresh_only=True)

    assert data["runtime_ready"] is False
    assert data["runtime_error"]
    assert "mcp_unavailable" in data


def test_disconnected_mcp_server_is_reported(state):
    """A configured-but-down server must be visible.

    `get_all_tools()` filters on `connected`, so a down server just yields
    fewer tools — the agent then behaves as if browser automation was never
    configured, which reads as the agent ignoring the request.
    """
    state.mcp_manager = SimpleNamespace(
        list_servers=lambda: [
            {"name": "browseros", "enabled": True, "connected": False,
             "endpoint": "http://127.0.0.1:9239/mcp", "error": ""},
            {"name": "other", "enabled": True, "connected": True,
             "endpoint": "http://x", "error": ""},
            {"name": "off", "enabled": False, "connected": False,
             "endpoint": "http://y", "error": ""},
        ]
    )

    names = [s["name"] for s in state.mcp_unavailable_servers()]
    assert names == ["browseros"], (
        "should report only enabled servers that are not connected"
    )


def test_broken_mcp_server_does_not_brick_the_session(monkeypatch, tmp_path):
    """A failing MCP server degrades tools; it must not kill the runtime.

    BrowserOS is the default MCP profile and runs as a separate process, so it
    being down is an ordinary condition — not a reason the user cannot chat.
    """
    project = tmp_path / "project"
    project.mkdir()
    (project / ".resonant").mkdir()
    app_module = _load_app_module(monkeypatch, project)
    monkeypatch.setattr(app_module.AppState, "detect_backends", lambda self: {})
    st = app_module.AppState()

    def _boom():
        raise RuntimeError("browseros: connection refused")

    st.mcp_manager = SimpleNamespace(
        get_all_tools=_boom,
        call_tool=lambda *a, **k: None,
    )

    session = st.build_session(backend=_DummyBackend(), project_path=str(project))

    assert session is not None
    assert session.mcp_tools == [], (
        "a dead MCP server should yield no tools, not an exception"
    )
