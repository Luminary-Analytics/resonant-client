"""
Regression tests for bug #9+#10 — backend swap context loss.

Bug history:
    v0.1.x — `Session.set_backend(backend)` deliberately called
    `self.conversation_history.clear()` on every swap. The GUI dropdown's
    `switch_model` handler additionally rebuilt the entire session via
    `build_session()`, double-wiping history. Users swapping models
    mid-conversation lost ALL prior turns.

    v0.2.1 fix — `set_backend` now defaults to `reset_history=False`.
    GUI's `switch_model` calls a new `swap_backend()` method that mutates
    the existing session instead of building a fresh one.

These tests pin the new behavior so it doesn't regress.
"""

import pytest
from types import SimpleNamespace
from unittest.mock import Mock

from resonant_client.engine.session import Session
from resonant_client.gui.app import AppState
from resonant_client.gui.runtime import BackendSpec


class _StubBackend:
    """Minimal backend that satisfies Session.__init__ without touching network."""
    def __init__(self, name: str = "stub"):
        self.name = name
        self.model = f"{name}-model"
        self.tool_mode = "native"
        self.handles_tools = True

    def stream(self, **kwargs):
        return iter([])

    def classify(self, *args, **kwargs):
        return ""

    def health(self):
        return {"status": "ready"}


# ---------- core invariant: set_backend preserves history by default ----------

class TestSetBackendDefaultPreservesHistory:

    @pytest.mark.unit
    def test_empty_history_swap_no_op(self):
        be1 = _StubBackend("ollama")
        be2 = _StubBackend("claude")
        s = Session(backend=be1)

        s.set_backend(be2)

        assert s.backend is be2
        assert s.conversation_history == []

    @pytest.mark.unit
    def test_one_turn_swap_preserves_history(self):
        be1 = _StubBackend("ollama")
        be2 = _StubBackend("claude")
        s = Session(backend=be1)
        s.conversation_history.append({"role": "user", "content": "hello"})
        s.conversation_history.append({"role": "assistant", "content": "hi back"})

        s.set_backend(be2)

        assert s.backend is be2
        assert len(s.conversation_history) == 2
        assert s.conversation_history[0]["content"] == "hello"
        assert s.conversation_history[1]["content"] == "hi back"

    @pytest.mark.unit
    def test_round_trip_swap_preserves_history(self):
        """Bug #10 — Ollama → claude-code → Ollama should still have history.

        The original bug manifested as a silent 1-step "success" with zero
        work after the swap-back, because the second swap also wiped history
        and Ollama got an empty context."""
        ollama = _StubBackend("ollama")
        claude_code = _StubBackend("claude-code")
        s = Session(backend=ollama)
        s.conversation_history.extend([
            {"role": "user", "content": "build a flask app"},
            {"role": "assistant", "content": "Done. 3 files written."},
            {"role": "user", "content": "now add tests"},
        ])

        s.set_backend(claude_code)
        s.set_backend(ollama)

        assert s.backend is ollama
        assert len(s.conversation_history) == 3
        assert s.conversation_history[-1]["content"] == "now add tests"


# ---------- explicit opt-in: callers can still request a clear ----------

class TestSetBackendResetHistoryOptIn:

    @pytest.mark.unit
    def test_reset_history_true_clears(self):
        """TUI '/model' command path — explicit user intent to clear."""
        be1 = _StubBackend("ollama")
        be2 = _StubBackend("ollama")
        s = Session(backend=be1)
        s.conversation_history.append({"role": "user", "content": "hello"})

        s.set_backend(be2, reset_history=True)

        assert s.backend is be2
        assert s.conversation_history == []

    @pytest.mark.unit
    def test_reset_history_keyword_only(self):
        """`reset_history` must be passed as keyword to avoid silent positional misuse."""
        be1 = _StubBackend("ollama")
        be2 = _StubBackend("claude")
        s = Session(backend=be1)
        s.conversation_history.append({"role": "user", "content": "hello"})

        # Positional should fail because reset_history is keyword-only.
        with pytest.raises(TypeError):
            s.set_backend(be2, True)  # type: ignore[misc]

        # History still preserved (the failed call is a no-op).
        assert len(s.conversation_history) == 1


# ---------- defensive: make sure docstring & signature stay in sync ----------

class TestSetBackendSignature:

    @pytest.mark.unit
    def test_signature_matches_docstring_intent(self):
        """The docstring promises `reset_history` is a keyword arg with default False.
        If someone "fixes" the API by removing the kwarg, this test catches it."""
        import inspect
        sig = inspect.signature(Session.set_backend)
        params = sig.parameters

        assert "reset_history" in params, (
            "set_backend lost its reset_history kwarg — bug #9+#10 may regress"
        )
        rh = params["reset_history"]
        assert rh.default is False, (
            "set_backend's reset_history default flipped to True — "
            "this re-introduces bug #9 (silent history loss on dropdown swap)"
        )
        assert rh.kind == inspect.Parameter.KEYWORD_ONLY, (
            "reset_history must be keyword-only to prevent positional misuse"
        )


@pytest.mark.unit
def test_app_state_swap_persists_live_backend_identity():
    """The session list must show the same backend/model as the composer."""
    old_backend = _StubBackend("kimi")
    new_backend = _StubBackend("exo")
    new_backend.model = "mlx-community/GLM-5.2-mxfp4"
    spec = BackendSpec(
        backend_type="exo",
        model=new_backend.model,
        base_url="http://exo.test:52415/v1",
    )
    spec.create_backend = Mock(return_value=new_backend)
    record = SimpleNamespace(
        backend_type="kimi",
        model="kimi-k3",
        thinking_mode="max",
    )
    project = SimpleNamespace(
        project_path="D:/Repos/example",
        current_session=record,
        save_current_session=Mock(),
    )
    engine_session = SimpleNamespace(set_backend=Mock())
    state = AppState.__new__(AppState)
    state.project = project
    state.session = engine_session
    state.backend = old_backend
    state.backend_spec = BackendSpec(backend_type="kimi", model="kimi-k3")
    state.settings = SimpleNamespace()
    state.build_backend_spec = Mock(return_value=spec)

    result = state.swap_backend("exo", new_backend.model)

    assert result is new_backend
    engine_session.set_backend.assert_called_once_with(new_backend)
    assert record.backend_type == "exo"
    assert record.model == new_backend.model
    assert record.thinking_mode == ""
    project.save_current_session.assert_called_once_with(
        engine_session=engine_session
    )
