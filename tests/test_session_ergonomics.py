"""
Tests for cluster 4 — Session Ergonomics features.

Currently covers:
- ProjectManager.fork_session slicing logic
"""

from __future__ import annotations

from pathlib import Path

import pytest

from resonant_client.gui import sessions as sessions_mod
from resonant_client.gui.sessions import ProjectManager, SessionRecord


@pytest.fixture
def isolated_resonant_home(tmp_path, monkeypatch):
    """Point the sessions storage at a tmp dir so tests don't touch real ~/.resonant."""
    monkeypatch.setattr(sessions_mod.Path, "home", staticmethod(lambda: tmp_path))
    return tmp_path / ".resonant"


def _make_history(num_user_msgs: int) -> list[dict]:
    """Build a synthetic conversation: user → assistant → user → assistant → ..."""
    history: list[dict] = []
    for i in range(num_user_msgs):
        history.append({"role": "user", "content": f"user message {i}"})
        history.append({"role": "assistant", "content": f"assistant reply {i}"})
        # Sprinkle a tool call+result for realism
        if i % 2 == 0:
            history.append({"role": "tool_call", "name": "glob", "call_id": f"c{i}", "content": "Called glob"})
            history.append({"role": "tool_result", "call_id": f"c{i}", "content": "found 3 files"})
    return history


class TestForkSession:
    def test_returns_none_for_unknown_source(self, isolated_resonant_home):
        pm = ProjectManager("/dev/proj")
        assert pm.fork_session("nonexistent", 0) is None

    def test_fork_at_first_user_message(self, isolated_resonant_home):
        pm = ProjectManager("/dev/proj")
        rec = pm.create_session(backend_type="ollama", model="m")
        rec.conversation_history = _make_history(3)
        rec.message_count = 3
        rec.save()

        forked = pm.fork_session(rec.id, 0)
        assert forked is not None
        assert forked.id != rec.id
        assert forked.title.startswith("Fork: ")
        # Should keep up to and including 1st user msg + its assistant/tool turns,
        # cutoff at the 2nd user message
        user_count = sum(1 for m in forked.conversation_history if m.get("role") == "user")
        assert user_count == 1
        # Original untouched
        original = pm.load_session(rec.id)
        assert sum(1 for m in original.conversation_history if m.get("role") == "user") == 3

    def test_fork_at_middle_user_message(self, isolated_resonant_home):
        pm = ProjectManager("/dev/proj")
        rec = pm.create_session(backend_type="ollama", model="m")
        rec.conversation_history = _make_history(5)
        rec.save()

        forked = pm.fork_session(rec.id, 2)
        assert forked is not None
        user_count = sum(1 for m in forked.conversation_history if m.get("role") == "user")
        assert user_count == 3  # kept 0, 1, 2

    def test_fork_at_last_keeps_everything(self, isolated_resonant_home):
        pm = ProjectManager("/dev/proj")
        rec = pm.create_session(backend_type="ollama", model="m")
        rec.conversation_history = _make_history(2)
        rec.save()

        forked = pm.fork_session(rec.id, 99)  # past the last
        assert forked is not None
        assert len(forked.conversation_history) == len(rec.conversation_history)

    def test_fork_preserves_thinking_mode(self, isolated_resonant_home):
        pm = ProjectManager("/dev/proj")
        rec = pm.create_session(backend_type="ollama", model="m")
        rec.thinking_mode = "high"
        rec.conversation_history = _make_history(2)
        rec.save()

        forked = pm.fork_session(rec.id, 0)
        assert forked.thinking_mode == "high"

    def test_fork_title_no_double_prefix(self, isolated_resonant_home):
        pm = ProjectManager("/dev/proj")
        rec = pm.create_session(backend_type="ollama", model="m")
        rec.title = "Fork: original"
        rec.conversation_history = _make_history(2)
        rec.save()

        forked = pm.fork_session(rec.id, 0)
        # Should NOT become "Fork: Fork: original"
        assert forked.title == "Fork: original"

    def test_message_count_recomputed(self, isolated_resonant_home):
        pm = ProjectManager("/dev/proj")
        rec = pm.create_session(backend_type="ollama", model="m")
        rec.conversation_history = _make_history(4)
        rec.message_count = 999  # stale; should be ignored
        rec.save()

        forked = pm.fork_session(rec.id, 1)
        # Kept 2 user messages
        assert forked.message_count == 2
