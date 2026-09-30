"""A request's tool definitions come from its own conversation, never from a shared backend.

The app keeps one backend for every conversation on the same model
(gui/app.py restore_session_runtime). A request that offers no tools (plan
mode, a Team participant's last request) in a conversation whose history
holds tool calls still needs those calls' definitions on the Messages and
Responses APIs: the session passes its own (engine/session.py), and the
adapters send them with tool choice none. These run whole turns through
Session.run against the scripted Messages and Responses servers, which refuse
tool calls in a history without tool definitions as the Messages API does.
"""

import pytest

from lumi.anthropic_api import AnthropicBackend
from lumi.engine import Session
from lumi.engine.sandbox import PathSandbox
from lumi.engine.tools import AGENT_TOOLS
from lumi.openai_api import OpenAIResponsesBackend
from tests.api_provider_stub import KEY, Reply, ScriptedProviders

DEPLOY = {"type": "function", "function": {"name": "mcp__projectx__deploy",
                                           "description": "Deploy Project X to production with its release token",
                                           "parameters": {"type": "object", "properties": {}}}}
FILE_READ = next(tool for tool in AGENT_TOOLS if tool["function"]["name"] == "file_read")


@pytest.fixture
def providers():
    server = ScriptedProviders()

    def script(turn):
        if "Plan the next change" in turn.prompt:
            return Reply(text="Plan: 1. Edit a.txt.")
        return Reply(tool=("file_read", {"path": "a.txt"})) if turn.step == 0 else Reply(text="a.txt says hello.")
    server.script = script
    yield server
    server.close()


def _backend(protocol, providers):
    if protocol == "anthropic":
        return AnthropicBackend(KEY, "claude-sonnet-5", base_url=providers.url)
    return OpenAIResponsesBackend(KEY, "gpt-5", base_url=providers.url + "/v1")


def _session(backend, project, **options):
    project.mkdir()
    (project / "a.txt").write_text("hello\n", encoding="utf-8")
    session = Session(backend, **options)
    session.project_path = str(project)
    session.sandbox = PathSandbox(str(project), enabled=True)
    return session


def _run(session, prompt):
    events = list(session.run(prompt))
    return [event.get("message") for event in events if event.get("event") == "error"]


def _names(request):
    return sorted(tool["name"] for tool in request["body"].get("tools") or [])


NONE = {"anthropic": {"type": "none"}, "openai": "none"}


@pytest.mark.parametrize("protocol", ["anthropic", "openai"])
def test_plan_mode_after_a_tool_loop_sends_the_conversations_tools_and_lets_none_run(tmp_path, providers, protocol):
    # The terminal UI's /plan after a tool loop (tui.py): plan mode offers no
    # tools, and before tools went with it the Messages API refused the request.
    session = _session(_backend(protocol, providers), tmp_path / "project")
    assert _run(session, "Read a.txt") == []
    session.plan_mode = True
    assert _run(session, "Plan the next change") == []
    assert providers.errors == []
    first, *_, plan = providers.of(protocol)
    assert _names(plan) == _names(first) and "file_read" in _names(plan)
    assert plan["body"]["tool_choice"] == NONE[protocol]


@pytest.mark.parametrize("protocol", ["anthropic", "openai"])
def test_two_conversations_on_one_backend_never_send_each_others_tools(tmp_path, providers, protocol):
    backend = _backend(protocol, providers)
    project_x = _session(backend, tmp_path / "x", allowed_tools=[DEPLOY, FILE_READ])
    project_y = _session(backend, tmp_path / "y")
    assert _run(project_y, "Read a.txt") == []
    # Project X's turn runs on the same backend in between, offering its own tool.
    assert _run(project_x, "Read a.txt") == []
    project_y.plan_mode = True
    assert _run(project_y, "Plan the next change") == []
    assert providers.errors == []
    y_first, _, x_first, _, plan = providers.of(protocol)
    assert "mcp__projectx__deploy" in _names(x_first)
    # Project Y's plan request carries project Y's own tools, as it offered them.
    assert "mcp__projectx__deploy" not in _names(plan)
    assert _names(plan) == _names(y_first) and plan["body"]["tool_choice"] == NONE[protocol]
