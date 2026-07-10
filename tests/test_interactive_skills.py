"""Interactive-session wiring for Resonant's learned skill library."""

from __future__ import annotations

from types import SimpleNamespace

from resonant_client.engine.session import Session
from resonant_client.engine.tools import AGENT_TOOLS, execute_tool
from resonant_client.orchestration.skills import Skill, save_skill
from tests.streaming_stub import StreamingBackend, done, text_delta


class _InstructionBackend(StreamingBackend):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.instructions = ""

    def stream(self, **kwargs):
        self.instructions = kwargs.get("instructions", "")
        yield from super().stream(**kwargs)


def test_skill_view_reads_project_skill_body(monkeypatch, tmp_path):
    state_home = tmp_path / "state"
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setenv("RESONANT_STATE_HOME", str(state_home))
    skill = Skill(
        id="parser-repair",
        name="Parser repair",
        description="Repair malformed parser inputs safely.",
        scope="project",
        created_by="agent",
    )
    save_skill(
        skill,
        procedure_md="1. Reproduce the malformed input.\n2. Add a narrow repair.",
        verification_md="Run parser regression tests.",
        project_path=project,
    )

    result = execute_tool(
        "skill_view",
        {"skill_id": "parser-repair"},
        project_path=str(project),
    )

    assert not result.is_error
    assert "Add a narrow repair" in result.output
    assert "Run parser regression tests" in result.output
    assert result.metadata["scope"] == "project"


def test_skill_view_is_registered_as_a_read_only_agent_tool():
    names = {tool["function"]["name"] for tool in AGENT_TOOLS}
    assert "skill_view" in names


def test_interactive_skill_index_is_injected_into_turn_context():
    backend = _InstructionBackend(events=[text_delta("done"), done()])
    session = Session(backend=backend, max_steps=1)
    session._skill_context_provider = lambda _query: SimpleNamespace(
        block="\n## Relevant skills\n- parser-repair\n"
    )

    list(session.run("fix the parser"))

    assert "Relevant skills" in backend.instructions
