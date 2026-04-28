"""End-to-end test: intent → walker → runner → real Session iteration → skill.

Uses a stub backend that emits scripted streaming responses keyed by the
specialization marker in the system prompt. Drives the full IntentService
pipeline and asserts: (1) all nodes complete DONE, (2) audit log captures
the lifecycle, (3) a skill auto-extracts on success, (4) the graph persists.
"""

from __future__ import annotations

import json
import time
from typing import Iterator

import pytest

from resonant_client.orchestration import (
    IntentService,
    NodeStatus,
    list_skills,
    load_graph,
    read_audit_events,
)


# ── Stub backend that mimics OllamaBackend.stream's event protocol ─────


class _StubBackend:
    """A backend whose `stream` yields scripted ('text', token) and ('done', meta)
    events. Returns different scripts based on which specialization the system
    prompt mentions.
    """
    name = "stub"
    model = "stub-model"
    handles_tools = True

    def __init__(self):
        self.calls: list[dict] = []

    def classify(self, prompt: str, max_tokens: int = 20) -> str:
        # Used by Session's auto-plan classifier; we never want planning here.
        return "SIMPLE"

    def stream(self, user_msg: str, conversation_history: list, instructions: str,
               tools: list, max_tokens: int = 4096, cancel_event=None) -> Iterator[tuple]:
        self.calls.append({"user_msg": user_msg, "instructions": instructions, "tools": [t["function"]["name"] for t in tools]})
        # Pick the script based on the specialization marker
        if "SPECIALIZATION: PLAN" in instructions:
            yield from self._yield_text(self._plan_response())
        elif "SPECIALIZATION: VERIFY" in instructions:
            yield from self._yield_text(self._verify_response())
        elif "SPECIALIZATION: IMPLEMENT" in instructions:
            yield from self._yield_text(self._implement_response())
        elif "SPECIALIZATION: EXPLORE" in instructions:
            yield from self._yield_text(self._explore_response())
        else:
            yield from self._yield_text("ok")
        # Final 'done'
        yield ("done", {"total_duration": 1, "eval_count": 5})

    @staticmethod
    def _yield_text(text: str) -> Iterator[tuple]:
        # Stream in 5-char chunks. Backend protocol is (event_type, dict-payload):
        #   ("text.delta", {"delta": "..."})  ← what Session expects to consume
        chunk = 5
        for i in range(0, len(text), chunk):
            yield ("text.delta", {"delta": text[i:i + chunk]})

    @staticmethod
    def _plan_response() -> str:
        return (
            "Decomposing the task.\n"
            "```json\n"
            + json.dumps({
                "subgoals": [
                    {"goal": "explore the existing layout", "specialization": "explore"},
                    {"goal": "implement the change", "specialization": "implement", "depends_on": [0]},
                    {"goal": "verify it works", "specialization": "verify", "depends_on": [1]},
                ],
            }, indent=2)
            + "\n```\nDone."
        )

    @staticmethod
    def _explore_response() -> str:
        return "I read the relevant files. The layout looks straightforward."

    @staticmethod
    def _implement_response() -> str:
        return "I made the change. Touched files: app.py, config.py."

    @staticmethod
    def _verify_response() -> str:
        return (
            "All checks pass.\n"
            "```json\n"
            + json.dumps({"verdict": "pass", "findings": []}, indent=2)
            + "\n```"
        )


# ── Fixtures ────────────────────────────────────────────────────────────


@pytest.fixture
def state_home(tmp_path, monkeypatch):
    home = tmp_path / "state"
    home.mkdir()
    monkeypatch.setenv("RESONANT_STATE_HOME", str(home))
    return home


@pytest.fixture
def project_dir(tmp_path):
    p = tmp_path / "proj"
    p.mkdir()
    return p


def _wait(service: IntentService, intent_id: str, *, timeout: float = 10.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        active = service._get(intent_id)
        if not active or not active.thread.is_alive():
            return
        time.sleep(0.05)
    raise AssertionError(f"intent {intent_id} did not complete within {timeout}s")


# ── End-to-end ──────────────────────────────────────────────────────────


def test_full_intent_pipeline_with_stub_backend(state_home, project_dir):
    """A 4-node intent (plan → 3 subgoals) drives clean through the pipeline."""
    backend = _StubBackend()
    events: list = []
    # Skip auto-feedback hooks that would call out to real linters / tests.
    fake_tools = [{"function": {"name": n}} for n in (
        "file_read", "glob", "grep", "file_write", "file_edit", "bash",
    )]

    service = IntentService(
        project_path=str(project_dir),
        backend=backend,
        all_tools=fake_tools,
        project_instructions="# Project conventions\n",
        settings=None,
        on_event=events.append,
    )

    intent_id = service.start_intent("add a CHANGELOG.md to this project")
    _wait(service, intent_id)

    # Graph persisted
    loaded = load_graph(intent_id, str(project_dir))
    assert loaded is not None
    assert loaded.intent == "add a CHANGELOG.md to this project"

    # All nodes ended DONE (4: root plan + 3 subgoals)
    statuses = [n.status for n in loaded.nodes.values()]
    assert all(s == NodeStatus.DONE for s in statuses), f"unexpected statuses: {statuses}"
    assert len(loaded.nodes) == 4

    # Audit log captured the lifecycle
    audit = read_audit_events(str(project_dir), intent_id)
    kinds = {e["kind"] for e in audit}
    assert "decision" in kinds
    assert "plan_change" in kinds
    summaries = {e["payload"].get("summary", "") for e in audit if e["kind"] == "decision"}
    assert "intent started" in summaries
    assert any("plan complete" in s for s in summaries)

    # Walker events forwarded
    plan_event_kinds = [e["event_payload"]["kind"]
                       for e in events if e.get("event") == "plan.event"]
    assert "node.start" in plan_event_kinds
    assert "node.done" in plan_event_kinds
    assert "plan.complete" in plan_event_kinds

    # Skill auto-extracted (4 nodes all DONE with high confidence → meets threshold)
    complete_evs = [e for e in events if e.get("event") == "intent.complete"]
    assert complete_evs
    assert complete_evs[0].get("extracted_skill_id"), "expected skill auto-extraction"

    skills = list_skills()
    skill_ids = [s.id for s in skills]
    assert any("changelog" in sid for sid in skill_ids), \
        f"expected a CHANGELOG skill, got {skill_ids}"


def test_specialist_tool_filtering_observed_through_pipeline(state_home, project_dir):
    """The explore specialist should never see write tools in its prompt."""
    backend = _StubBackend()
    fake_tools = [{"function": {"name": n}} for n in (
        "file_read", "glob", "grep", "file_write", "file_edit", "bash",
    )]
    service = IntentService(
        project_path=str(project_dir),
        backend=backend,
        all_tools=fake_tools,
        settings=None,
    )
    intent_id = service.start_intent("explore the project")
    _wait(service, intent_id)

    # Find the call that ran with explore specialization
    explore_calls = [c for c in backend.calls if "SPECIALIZATION: EXPLORE" in c["instructions"]]
    if explore_calls:
        # Explore must not see write/exec tools
        names = set(explore_calls[0]["tools"])
        assert "file_write" not in names
        assert "file_edit" not in names
        assert "bash" not in names
        assert "file_read" in names


def test_blocked_root_node_does_not_extract_skill(state_home, project_dir):
    """A graph that ends with a BLOCKED root never produces a skill."""
    class FailingBackend(_StubBackend):
        def stream(self, *a, **k):
            # Crash mid-stream → session.end with reason=error → BLOCKED node
            yield ("text", "starting")
            raise RuntimeError("simulated backend crash")

    backend = FailingBackend()
    fake_tools = [{"function": {"name": n}} for n in ("file_read", "bash")]
    service = IntentService(
        project_path=str(project_dir),
        backend=backend,
        all_tools=fake_tools,
        settings=None,
    )
    intent_id = service.start_intent("a doomed intent")
    _wait(service, intent_id, timeout=5.0)

    # No skill extracted
    skills = list_skills()
    assert not any("doomed" in s.id for s in skills)
