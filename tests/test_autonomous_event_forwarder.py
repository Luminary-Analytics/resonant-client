"""Tests for v0.5.6a3 — `gui/app.py::_make_autonomous_event_forwarder`.

The forwarder is the WS-handler half of the atomic terminal-state
transition. The daemon writes roadmap.md status; the forwarder
writes session.mission_state.phase. Both must converge before the
next user action can observe drift.

Coverage:
- Non-terminal events pass through to WS unchanged (no phase update,
  no sessions_updated fan-out).
- Terminal events (complete / paused / failed) update the session
  mission_state.phase and emit `sessions_updated`.
- intent_id mismatch is a no-op (don't corrupt a different session's
  state).
- Missing `new_phase` in payload is a defensive no-op (don't write
  empty string into the phase field).
- Forwarder doesn't crash if WS send raises.
- Forwarder doesn't crash if session save raises (logs but moves on).

Tests stub ws.send_json and the loop's run_coroutine_threadsafe so
we can assert the exact sequence of calls without spinning up a real
event loop or websocket.
"""
from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock


from resonant_client.gui.app import (
    _AUTONOMOUS_TERMINAL_EVENTS,
    _make_autonomous_event_forwarder,
)


# ── Test scaffolding ────────────────────────────────────────────────────


class _FakeSession:
    """Stand-in for `Session` (resonant_client.gui.sessions). Only
    implements the bits the forwarder touches: id, mission_state,
    advance_mission_phase, save."""

    def __init__(
        self,
        sid: str = "sess-1",
        mission_state: dict | None = None,
    ):
        self.id = sid
        self.mission_state = mission_state
        self.save_calls = 0
        self.advance_calls: list[tuple[str, dict]] = []
        self._save_should_raise: Exception | None = None

    def advance_mission_phase(self, phase: str, **fields) -> None:
        self.advance_calls.append((phase, fields))
        if self.mission_state is None:
            return
        self.mission_state["phase"] = phase
        for k, v in fields.items():
            self.mission_state[k] = v

    def save(self) -> None:
        self.save_calls += 1
        if self._save_should_raise is not None:
            raise self._save_should_raise


class _FakeProject:
    """Stand-in for `ProjectManager`. Holds a current_session and
    yields list_sessions / list_all_sessions for the
    sessions_updated fan-out payload."""

    def __init__(self, current: _FakeSession | None):
        self.current_session = current

    def list_sessions(self) -> list[dict]:
        return [{"id": "stub-active", "title": "active list"}]

    def list_all_sessions(self) -> list[dict]:
        return [{"id": "stub-all", "title": "all list"}]


class _FakeAppState:
    def __init__(self, project: _FakeProject | None):
        self.project = project


class _CapturingWS:
    """Stand-in for the Starlette WebSocket. Records every send_json
    payload — and we make send_json a coroutine so
    run_coroutine_threadsafe can wrap it without complaining."""

    def __init__(self):
        self.sent: list[dict] = []
        self._raise_on_send: Exception | None = None

    async def send_json(self, payload: dict) -> None:
        if self._raise_on_send is not None:
            raise self._raise_on_send
        self.sent.append(payload)


class _SyncLoop:
    """Stand-in for an asyncio loop where
    `run_coroutine_threadsafe(coro, loop)` is called. The forwarder
    only needs the loop to satisfy that call signature; we run the
    coroutine to completion synchronously so we can assert side
    effects in the test thread."""

    def __init__(self):
        self.scheduled: list[Any] = []


def _patch_run_coroutine_threadsafe(monkeypatch):
    """Intercept asyncio.run_coroutine_threadsafe so the coroutine
    gets driven synchronously, in the test thread. Returns a list of
    (coro, loop) tuples that were scheduled."""

    scheduled: list[tuple[Any, Any]] = []

    def fake_rcts(coro, loop):
        scheduled.append((coro, loop))
        # Drive the coroutine to completion. send_json above doesn't
        # await anything async — it's a 1-step coroutine that either
        # appends to a list or raises.
        try:
            coro.send(None)
        except StopIteration:
            pass
        return MagicMock()  # imitate the Future return

    import resonant_client.gui.app as app_module
    monkeypatch.setattr(app_module.asyncio, "run_coroutine_threadsafe", fake_rcts)
    return scheduled


# ── Tests ───────────────────────────────────────────────────────────────


class TestForwarderEventConstants:
    """Sanity check on the immutable terminal-event set."""

    def test_terminal_events_set_is_a_frozenset(self):
        assert isinstance(_AUTONOMOUS_TERMINAL_EVENTS, frozenset)

    def test_all_three_terminal_events_present(self):
        assert _AUTONOMOUS_TERMINAL_EVENTS == frozenset({
            "autonomous_mission_complete",
            "autonomous_mission_paused",
            "autonomous_mission_failed",
        })


class TestNonTerminalEvents:
    """Iteration / reflection / status events should pass through
    to the WS untouched — no session-phase write, no
    sessions_updated fan-out."""

    def test_iteration_started_passes_through(self, monkeypatch):
        scheduled = _patch_run_coroutine_threadsafe(monkeypatch)
        sess = _FakeSession(mission_state={"intent_id": "i1", "phase": "autonomous_running"})
        state = _FakeAppState(_FakeProject(sess))
        ws = _CapturingWS()
        loop = _SyncLoop()
        fwd = _make_autonomous_event_forwarder(state, ws, loop)

        fwd({"event": "autonomous_iteration_started", "iter": 1, "intent_id": "i1"})

        # Single WS send — the original event, nothing else.
        assert len(ws.sent) == 1
        assert ws.sent[0]["event"] == "autonomous_iteration_started"
        # No session updates.
        assert sess.advance_calls == []
        assert sess.save_calls == 0

    def test_reflection_event_passes_through(self, monkeypatch):
        scheduled = _patch_run_coroutine_threadsafe(monkeypatch)
        sess = _FakeSession(mission_state={"intent_id": "i1", "phase": "autonomous_running"})
        state = _FakeAppState(_FakeProject(sess))
        ws = _CapturingWS()
        fwd = _make_autonomous_event_forwarder(state, ws, _SyncLoop())

        fwd({"event": "autonomous_reflection", "verdict": "continue", "intent_id": "i1"})

        assert len(ws.sent) == 1
        assert sess.advance_calls == []

    def test_backend_status_passes_through(self, monkeypatch):
        # backend.status (v0.5.6a1) is NOT in the terminal set.
        scheduled = _patch_run_coroutine_threadsafe(monkeypatch)
        sess = _FakeSession(mission_state={"intent_id": "i1", "phase": "autonomous_running"})
        state = _FakeAppState(_FakeProject(sess))
        ws = _CapturingWS()
        fwd = _make_autonomous_event_forwarder(state, ws, _SyncLoop())

        fwd({"event": "backend.status", "kind": "ollama_retry"})

        assert len(ws.sent) == 1
        assert sess.advance_calls == []


class TestTerminalEventInterception:
    """Terminal events update session.mission_state.phase + emit
    sessions_updated."""

    def test_complete_updates_phase_and_emits_sessions_updated(
        self, monkeypatch,
    ):
        scheduled = _patch_run_coroutine_threadsafe(monkeypatch)
        sess = _FakeSession(
            mission_state={"intent_id": "i1", "phase": "autonomous_running"},
        )
        state = _FakeAppState(_FakeProject(sess))
        ws = _CapturingWS()
        fwd = _make_autonomous_event_forwarder(state, ws, _SyncLoop())

        fwd({
            "event": "autonomous_mission_complete",
            "intent_id": "i1",
            "new_phase": "autonomous_complete",
            "stop_reason": "satisfied",
        })

        # Two sends: the terminal event AND the sessions_updated fanout.
        assert len(ws.sent) == 2
        assert ws.sent[0]["event"] == "autonomous_mission_complete"
        assert ws.sent[1]["event"] == "sessions_updated"
        # Sessions_updated payload carries the active+all+current_session_id.
        assert ws.sent[1]["sessions"] == [{"id": "stub-active", "title": "active list"}]
        assert ws.sent[1]["all_sessions"] == [{"id": "stub-all", "title": "all list"}]
        assert ws.sent[1]["current_session_id"] == "sess-1"

        # Session was advanced + saved.
        assert sess.advance_calls == [("autonomous_complete", {})]
        assert sess.save_calls == 1
        assert sess.mission_state["phase"] == "autonomous_complete"

    def test_paused_updates_phase(self, monkeypatch):
        scheduled = _patch_run_coroutine_threadsafe(monkeypatch)
        sess = _FakeSession(
            mission_state={"intent_id": "i7", "phase": "autonomous_running"},
        )
        state = _FakeAppState(_FakeProject(sess))
        ws = _CapturingWS()
        fwd = _make_autonomous_event_forwarder(state, ws, _SyncLoop())

        fwd({
            "event": "autonomous_mission_paused",
            "intent_id": "i7",
            "new_phase": "autonomous_paused",
            "stop_reason": "blocked",
        })

        assert sess.advance_calls == [("autonomous_paused", {})]
        assert sess.save_calls == 1
        assert sess.mission_state["phase"] == "autonomous_paused"

    def test_failed_updates_phase(self, monkeypatch):
        scheduled = _patch_run_coroutine_threadsafe(monkeypatch)
        sess = _FakeSession(
            mission_state={"intent_id": "i9", "phase": "autonomous_running"},
        )
        state = _FakeAppState(_FakeProject(sess))
        ws = _CapturingWS()
        fwd = _make_autonomous_event_forwarder(state, ws, _SyncLoop())

        fwd({
            "event": "autonomous_mission_failed",
            "intent_id": "i9",
            "new_phase": "autonomous_failed",
            "error": "kaboom",
        })

        assert sess.advance_calls == [("autonomous_failed", {})]
        assert sess.save_calls == 1
        assert sess.mission_state["phase"] == "autonomous_failed"


class TestForwarderDefensiveBehavior:
    """The forwarder runs in a daemon thread; it must never raise
    back out, and it must never corrupt unrelated session state."""

    def test_intent_id_mismatch_is_noop(self, monkeypatch):
        # The current session is for intent `other`, but the daemon
        # event arrives for intent `i1` — this can happen if the
        # user switched sessions while a daemon was still running.
        # The forwarder must NOT touch the unrelated session.
        scheduled = _patch_run_coroutine_threadsafe(monkeypatch)
        sess = _FakeSession(
            mission_state={"intent_id": "other", "phase": "autonomous_running"},
        )
        state = _FakeAppState(_FakeProject(sess))
        ws = _CapturingWS()
        fwd = _make_autonomous_event_forwarder(state, ws, _SyncLoop())

        fwd({
            "event": "autonomous_mission_complete",
            "intent_id": "i1",
            "new_phase": "autonomous_complete",
        })

        # WS event still got forwarded (other listeners may care).
        assert len(ws.sent) == 1
        assert ws.sent[0]["event"] == "autonomous_mission_complete"
        # But session was NOT touched.
        assert sess.advance_calls == []
        assert sess.save_calls == 0
        assert sess.mission_state["phase"] == "autonomous_running"

    def test_missing_new_phase_is_noop(self, monkeypatch):
        # Defensive: if the daemon-side payload doesn't include
        # new_phase (e.g. older daemon, race during upgrade), the
        # forwarder MUST NOT write an empty string to the phase
        # field — that would corrupt the orphan-detection logic.
        scheduled = _patch_run_coroutine_threadsafe(monkeypatch)
        sess = _FakeSession(
            mission_state={"intent_id": "i1", "phase": "autonomous_running"},
        )
        state = _FakeAppState(_FakeProject(sess))
        ws = _CapturingWS()
        fwd = _make_autonomous_event_forwarder(state, ws, _SyncLoop())

        fwd({"event": "autonomous_mission_complete", "intent_id": "i1"})  # no new_phase

        # Forwarded but no phase write.
        assert len(ws.sent) == 1
        assert sess.advance_calls == []
        assert sess.save_calls == 0

    def test_no_current_session_is_noop(self, monkeypatch):
        scheduled = _patch_run_coroutine_threadsafe(monkeypatch)
        state = _FakeAppState(_FakeProject(None))  # no current session
        ws = _CapturingWS()
        fwd = _make_autonomous_event_forwarder(state, ws, _SyncLoop())

        fwd({
            "event": "autonomous_mission_complete",
            "intent_id": "i1",
            "new_phase": "autonomous_complete",
        })

        # Forwarded — but no session to update.
        assert len(ws.sent) == 1

    def test_no_project_is_noop(self, monkeypatch):
        scheduled = _patch_run_coroutine_threadsafe(monkeypatch)
        state = _FakeAppState(None)  # no project at all
        ws = _CapturingWS()
        fwd = _make_autonomous_event_forwarder(state, ws, _SyncLoop())

        fwd({
            "event": "autonomous_mission_complete",
            "intent_id": "i1",
            "new_phase": "autonomous_complete",
        })

        assert len(ws.sent) == 1

    def test_session_without_mission_state_is_noop(self, monkeypatch):
        # A non-mission session (mission_state=None) shouldn't get
        # phase-written.
        scheduled = _patch_run_coroutine_threadsafe(monkeypatch)
        sess = _FakeSession(mission_state=None)
        state = _FakeAppState(_FakeProject(sess))
        ws = _CapturingWS()
        fwd = _make_autonomous_event_forwarder(state, ws, _SyncLoop())

        fwd({
            "event": "autonomous_mission_complete",
            "intent_id": "i1",
            "new_phase": "autonomous_complete",
        })

        assert sess.advance_calls == []
        assert sess.save_calls == 0

    def test_save_failure_is_swallowed(self, monkeypatch):
        # If session.save() raises (disk full, locked, whatever) the
        # forwarder must NOT propagate — daemon thread should keep
        # ticking.
        scheduled = _patch_run_coroutine_threadsafe(monkeypatch)
        sess = _FakeSession(
            mission_state={"intent_id": "i1", "phase": "autonomous_running"},
        )
        sess._save_should_raise = OSError("disk full")
        state = _FakeAppState(_FakeProject(sess))
        ws = _CapturingWS()
        fwd = _make_autonomous_event_forwarder(state, ws, _SyncLoop())

        # Must not raise.
        fwd({
            "event": "autonomous_mission_complete",
            "intent_id": "i1",
            "new_phase": "autonomous_complete",
        })

        # advance was called, save was attempted (and raised).
        assert sess.advance_calls == [("autonomous_complete", {})]
        # The post-save sessions_updated emit is skipped on save
        # failure (we returned out of the try block) — only the
        # original terminal event got forwarded.
        kinds = [p.get("event") for p in ws.sent]
        assert kinds == ["autonomous_mission_complete"]

    def test_ws_send_failure_does_not_block_phase_update(self, monkeypatch):
        # If the WS send raises (client disconnected mid-loop), the
        # forwarder must still update the session phase so the
        # on-disk state is correct on next reconnect.
        scheduled: list[tuple[Any, Any]] = []

        def fake_rcts(coro, loop):
            scheduled.append((coro, loop))
            # Drive the coroutine — this is where send_json raises.
            try:
                coro.send(None)
            except StopIteration:
                pass
            except Exception:
                # The forwarder catches this in its outer try. We
                # simulate that here by absorbing any exception
                # raised by the coroutine, mirroring asyncio's
                # actual behavior (the exception ends up in the
                # Future, not raised back to the scheduler).
                pass
            return MagicMock()

        import resonant_client.gui.app as app_module
        monkeypatch.setattr(app_module.asyncio, "run_coroutine_threadsafe", fake_rcts)

        sess = _FakeSession(
            mission_state={"intent_id": "i1", "phase": "autonomous_running"},
        )
        state = _FakeAppState(_FakeProject(sess))
        ws = _CapturingWS()
        ws._raise_on_send = ConnectionError("client disconnected")
        fwd = _make_autonomous_event_forwarder(state, ws, _SyncLoop())

        # Must not raise.
        fwd({
            "event": "autonomous_mission_complete",
            "intent_id": "i1",
            "new_phase": "autonomous_complete",
        })

        # Even though both sends failed, the session phase was
        # written — that's the whole point of the atomicity guarantee.
        assert sess.advance_calls == [("autonomous_complete", {})]
        assert sess.save_calls == 1
        assert sess.mission_state["phase"] == "autonomous_complete"
