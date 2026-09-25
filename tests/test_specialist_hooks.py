"""Orchestration specialists run the person's hooks, as a chat session in the project does.

LocalSpecialistRunner builds the Session of every specialist that /plan, a
Mission's "Build this roadmap" and autonomous sessions run. It left
``session.hook_runner`` unset, so the person's Settings hooks (``hooks`` in
settings.json) and the project's approved capability-pack hooks never ran for
that work: a ``pre_tool_use`` guard that refuses a call in the app, failing
closed, let the same call through in a specialist.

These run the runner with a scripted model and the real Session, and check the
side effect (the file the call writes) as well as what the model is told.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

from lumi.engine.hooks import HookDefinition, HookRunner
from lumi.engine.tools import AGENT_TOOLS
from lumi.gui import ws_commands
from lumi.gui.autonomous_factory import make_reflect_runner
from lumi.gui.roadmap import Roadmap
from lumi.gui.settings import SettingsManager
from lumi.orchestration import LocalSpecialistRunner, NodeSpecialization, NodeStatus, PlanGraph, PlanNode, new_node_id
from lumi.orchestration.reflect import ReflectPassResult
from tests.streaming_stub import StreamingBackend, done, events_of_kind, text_delta, tool_call


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    # Settings, trust decisions and plan graphs live in the state folder: keep them in the test's own.
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("LUMI_STATE_HOME", str(home / ".lumi"))


@pytest.fixture
def project(tmp_path) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    return root


@pytest.fixture
def seen(tmp_path) -> Path:
    """Where the test's hooks note each run."""
    return tmp_path / "seen.txt"


def hook_command(tmp_path: Path, name: str, body: str) -> str:
    """A hook command that runs ``body`` with this Python, kept outside the project."""
    folder = tmp_path / "hooks"
    folder.mkdir(exist_ok=True)
    script = folder / f"{name}.py"
    script.write_text(body, encoding="utf-8")
    return f'"{sys.executable}" "{script}"'


def recording(seen: Path, who: str) -> str:
    """Hook code that notes each run as "<who> <hook type> <tool name>"."""
    return ("import os\n"
            f"open({str(seen)!r}, 'a').write({who!r} + ' ' + os.environ['LUMI_HOOK_TYPE'] + ' '"
            " + os.environ['LUMI_TOOL_NAME'] + '\\n')\n")


def refusing(message: str) -> str:
    """Hook code that refuses, as a failing gate hook does."""
    return f"import sys\nsys.stderr.write({message!r})\nsys.exit(1)\n"


def settings_guard(tmp_path: Path, seen: Path, tool: str = "file_write") -> dict:
    """A Settings pre_tool_use hook that refuses ``tool`` by exiting 1."""
    command = hook_command(tmp_path, f"guard-{tool}", recording(seen, "settings") + refusing("no writes from plans"))
    return {"hook_type": "pre_tool_use", "matcher": tool, "name": "no writes", "command": command}


def settings_recorder(tmp_path: Path, seen: Path, hook_type: str, who: str = "settings") -> dict:
    """A Settings hook that only notes its runs."""
    return {"hook_type": hook_type, "command": hook_command(tmp_path, f"{who}-{hook_type}", recording(seen, who))}


def person_settings(*hooks: dict) -> SettingsManager:
    """The person's settings.json in the test's state folder, with these hooks."""
    settings = SettingsManager()
    settings.set("hooks", None, list(hooks))
    return settings


def app_runner(*hooks: dict) -> HookRunner:
    """A shared runner like the app's, holding these Settings hooks."""
    runner = HookRunner()
    runner.add_hooks([HookDefinition.from_dict(hook) for hook in hooks])
    return runner


def ran(seen: Path) -> list[str]:
    return seen.read_text(encoding="utf-8").splitlines() if seen.exists() else []


def writing_backend(times: int = 1) -> StreamingBackend:
    """A model that writes notes.txt, then reports; once per specialist."""
    return StreamingBackend(scripts=[
        [tool_call("file_write", {"path": "notes.txt", "content": "hi"}, call_id="c1"), done()],
        [text_delta("Finished."), done()],
    ] * times)


def implement_node(goal: str = "write notes.txt", **fields) -> tuple[PlanGraph, PlanNode]:
    graph = PlanGraph.new(goal)
    node = PlanNode(id=new_node_id(), intent_id=graph.intent_id, goal=goal,
                    specialization=NodeSpecialization.IMPLEMENT, **fields)
    graph.add_node(node)
    return graph, node


def run_specialist(project: Path, **runner_kwargs) -> tuple[dict, StreamingBackend]:
    """One implement specialist whose model writes notes.txt; returns the tool result and the model."""
    events: list[dict] = []
    backend = writing_backend()
    graph, node = implement_node(working_subdir=runner_kwargs.pop("working_subdir", None))
    runner = LocalSpecialistRunner(backend=backend, project_path=str(project), all_tools=list(AGENT_TOOLS),
                                   on_session_event=events.append, **runner_kwargs)
    result = runner(node, graph)
    assert result.status == NodeStatus.DONE, result.summary
    [tool_result] = events_of_kind(events, "tool.result")
    return tool_result, backend


class TestTheRunner:
    def test_a_settings_guard_refuses_a_specialists_write(self, tmp_path, project, seen):
        # Built outside the app, the runner loads the Settings hooks itself, as `lumi run` does.
        settings = person_settings(settings_guard(tmp_path, seen), settings_recorder(tmp_path, seen, "session_start"),
                                   settings_recorder(tmp_path, seen, "session_end"))

        result, _ = run_specialist(project, settings=settings)

        assert result["denied"] is True, result["output"]
        assert result["output"] == "Blocked by hook: no writes from plans"
        assert not (project / "notes.txt").exists()
        assert ran(seen) == ["settings session_start ", "settings pre_tool_use file_write", "settings session_end "]

    def test_without_settings_nothing_is_attached(self, project):
        result, _ = run_specialist(project)

        assert result["denied"] is False, result["output"]
        assert (project / "notes.txt").read_text(encoding="utf-8") == "hi"

    def test_the_apps_runner_is_used_and_asked_from_the_project_root(self, tmp_path, project, seen):
        # A specialist working in web/ still gets the project's hooks: packs are found at the root.
        (project / "web").mkdir()
        roots: list[str] = []
        apps = app_runner(settings_guard(tmp_path, seen))
        # With the app's runner, the runner doesn't load its settings' hooks again beside it.
        mine = person_settings(settings_recorder(tmp_path, seen, "session_start", who="own settings"))

        result, _ = run_specialist(project, settings=mine, working_subdir="web",
                                   hook_runner_for=lambda root: roots.append(root) or apps.scoped([]))

        assert result["output"] == "Blocked by hook: no writes from plans"
        assert not (project / "web" / "notes.txt").exists()
        assert [os.path.normcase(os.path.realpath(root)) for root in roots] == [
            os.path.normcase(os.path.realpath(project))]
        assert ran(seen) == ["settings pre_tool_use file_write"]

    def test_a_hook_lookup_that_fails_blocks_the_specialist_before_the_model(self, project):
        def broken(_root):
            raise RuntimeError("pack discovery failed")

        backend = writing_backend()
        runner = LocalSpecialistRunner(backend=backend, project_path=str(project), all_tools=list(AGENT_TOOLS),
                                       settings=person_settings(), hook_runner_for=broken)
        graph, node = implement_node()

        result = runner(node, graph)

        assert result.status == NodeStatus.BLOCKED
        assert "pack discovery failed" in result.summary
        assert backend.stream_count == 0
        assert not (project / "notes.txt").exists()


class TestTheReflectPass:
    def test_the_reflect_pass_runs_the_hooks_it_is_given(self, tmp_path, project, seen):
        # An autonomous session's reflect pass is a specialist too, with bash.
        apps = app_runner(settings_guard(tmp_path, seen, tool="bash"))
        verdict = {"verdict": "continue", "chrome_results": [], "added": [], "blocked": [], "manual_pending": [],
                   "summary": "Still going.", "estimated_remaining_minutes": 5}
        backend = StreamingBackend(scripts=[
            [tool_call("bash", {"command": "echo reflected > ran.txt"}, call_id="c1"), done()],
            [text_delta(f"```json\n{json.dumps(verdict)}\n```"), done()],
        ])
        events: list[dict] = []
        reflect = make_reflect_runner(
            backend=backend, project_path=str(project), project_instructions="", settings=None,
            roadmap_path=str(project / "roadmap.md"), on_session_event=events.append,
            hook_runner_for=lambda _root: apps.scoped([]),
        )

        outcome = reflect(Roadmap(), ReflectPassResult())

        [result] = events_of_kind(events, "tool.result")
        assert result["output"] == "Blocked by hook: no writes from plans"
        assert not (project / "ran.txt").exists()
        assert outcome.verdict == "continue"
        assert ran(seen) == ["settings pre_tool_use bash"]

    def test_a_mission_hands_its_reflect_pass_the_apps_hooks(self, monkeypatch, tmp_path):
        from lumi.gui import autonomous_session
        from tests.test_autonomous_session import _SPEC_MD, _StubAppState, _StubProject

        passed: dict = {}
        real = autonomous_session.build_autonomous_mission_hooks

        def spy(**kwargs):
            passed.update(kwargs)
            return real(**kwargs)

        monkeypatch.setattr(autonomous_session, "build_autonomous_mission_hooks", spy)
        state = _StubAppState(project=_StubProject(str(tmp_path)))
        state.specialist_hook_runner = lambda _root: HookRunner()

        daemon = autonomous_session.start_autonomous_mission(
            state=state, intent_id="auto-1", feature="counter", spec_markdown=_SPEC_MD, on_event=lambda _ev: None,
        )
        daemon.stop()
        daemon.join(timeout=3.0)

        assert passed["hook_runner_for"] is state.specialist_hook_runner


# ── The desktop application ────────────────────────────────────────────


def _send(state, command: str, message: dict) -> list[dict]:
    class _Socket:
        def __init__(self):
            self.sent = []

        async def send_json(self, payload):
            self.sent.append(payload)

    socket = _Socket()
    asyncio.run(ws_commands.HANDLERS[command](ws_commands.CommandContext(ws=socket, state=state, msg=message)))
    return socket.sent


def _pack(project: Path, pack_id: str, hook: dict, script_body: str) -> None:
    """A repository pack whose one hook runs ``script_body``, kept in the pack."""
    folder = project / ".lumi" / "packs" / pack_id
    (folder / "hooks").mkdir(parents=True)
    script = folder / "hooks" / "hook.py"
    script.write_text(script_body, encoding="utf-8")
    manifest = {"id": pack_id, "name": pack_id.title(), "version": "1.0.0",
                "hooks": [{**hook, "command": f'"{sys.executable}" "{script}"'}]}
    (folder / "lumi-pack.json").write_text(json.dumps(manifest), encoding="utf-8")


def _approve(state, pack_id: str) -> None:
    """Approve the pack as Settings > Capability packs does."""
    [listed] = _send(state, "capability_pack_list", {})
    [pack] = [pack for pack in listed["packs"] if pack["id"] == pack_id]
    replies = _send(state, "capability_pack_approve", {"pack_id": pack_id, "path": pack["path"],
                                                        "digest": pack["digest"]})
    [refreshed] = [reply for reply in replies if reply["event"] == "capability.pack_list"]
    assert [p["status"] for p in refreshed["packs"] if p["id"] == pack_id] == ["approved"]


def _app_state(monkeypatch, project: Path):
    from lumi.gui import app as gui_app

    monkeypatch.chdir(project)
    monkeypatch.setattr(gui_app.AppState, "detect_backends", lambda self, force=False: {})
    state = gui_app.AppState()
    state.apply_project_context(str(project))
    state.project.current_session = None
    return state


def _plan(state, events: list[dict]) -> dict:
    """Run one implement specialist through the app's intent service, as /plan does; return its tool result."""
    before = len(events_of_kind(events, "tool.result"))
    service = state.get_intent_service(on_event=events.append)
    intent_id = service.start_intent("write notes.txt", planner_specialization=NodeSpecialization.IMPLEMENT)
    worker = service._get(intent_id).thread
    worker.join(timeout=60)
    assert not worker.is_alive()
    results = events_of_kind(events, "tool.result")
    assert len(results) == before + 1, results
    return results[-1]


def _pack_hooks(runner, project: Path) -> list[str]:
    return [hook.command for hook in runner.hooks if str(project / ".lumi") in hook.command]


class TestTheApp:
    def test_plan_specialists_run_settings_and_approved_pack_hooks(self, monkeypatch, tmp_path, project, seen):
        # The person's settings.json has a session_start hook; the project has two packs, one approved.
        person_settings(settings_recorder(tmp_path, seen, "session_start"))
        _pack(project, "guard", {"hook_type": "pre_tool_use", "matcher": "file_write"},
              recording(seen, "guard pack") + refusing("the guard pack refuses writes"))
        _pack(project, "unreviewed", {"hook_type": "session_start"}, recording(seen, "unreviewed pack"))
        state = _app_state(monkeypatch, project)
        _approve(state, "guard")
        state.backend = writing_backend()

        result = _plan(state, [])

        assert result["denied"] is True, result["output"]
        assert result["output"] == "Blocked by hook: the guard pack refuses writes"
        assert not (project / "notes.txt").exists()
        # The Settings hook ran once, the approved pack's guard ran, the unapproved pack's hook didn't.
        assert ran(seen) == ["settings session_start ", "guard pack pre_tool_use file_write"]
        # Pack hooks rode on the specialist's own runner, never the shared one.
        assert _pack_hooks(state.hook_runner, project) == []

    def test_a_pack_approved_meanwhile_guards_the_next_specialist(self, monkeypatch, tmp_path, project, seen):
        _pack(project, "guard", {"hook_type": "pre_tool_use", "matcher": "file_write"},
              recording(seen, "guard pack") + refusing("the guard pack refuses writes"))
        state = _app_state(monkeypatch, project)
        state.backend = writing_backend(times=2)
        events: list[dict] = []

        first = _plan(state, events)
        assert first["denied"] is False, first["output"]
        (project / "notes.txt").unlink()

        _approve(state, "guard")
        second = _plan(state, events)

        # The same intent service: hooks are looked up as each specialist starts.
        assert second["output"] == "Blocked by hook: the guard pack refuses writes"
        assert not (project / "notes.txt").exists()
        assert ran(seen) == ["guard pack pre_tool_use file_write"]
