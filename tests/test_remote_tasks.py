"""Tasks from Slack and Teams on this computer (lumi/remote_tasks.py), against a fake Lumi Cloud."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from lumi import headless, remote_tasks
from lumi import policy as lumi_policy
from lumi.gui import ws_commands
from lumi.policy import parse
from lumi.remote_tasks import RemoteTasks
from tests.streaming_stub import StreamingBackend, done, text_delta, tool_call
from tests.test_cloud import URL, FakeCloud, _client, _sign_in, fake  # noqa: F401 (the fixture)
from tests.test_connections import _StubWS


class TaskCloud(FakeCloud):
    """The fake Lumi Cloud, with the device side of tasks from chat."""

    def __init__(self) -> None:
        super().__init__()
        self.queue: list[dict] = []
        self.tasks: dict[str, dict] = {}
        self.answer = "approved"  # what the person answers, after one look

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if not path.startswith("/api/v1/devices/tasks"):
            return super().__call__(request)
        device = self.device_tokens.get(request.headers.get("authorization", "")[7:])
        if device is None:
            return httpx.Response(401, json={"error": "invalid_token"})
        if path.endswith("/claim"):
            if not self.queue:
                return httpx.Response(204)
            task = self.queue.pop(0)
            self.tasks[task["id"]] = {"device": device, "stop_requested": False, "approvals": [], "result": None,
                                      "looks": 0}
            return httpx.Response(200, json=task)
        task_id = path.split("/")[5]
        task = self.tasks[task_id]
        if path.endswith("/approvals"):
            approval = {"id": f"rap_{len(task['approvals']) + 1}", "status": "pending",
                        "text": json.loads(request.content)["text"]}
            task["approvals"].append(approval)
            return httpx.Response(201, json={"id": approval["id"]})
        if path.endswith("/result"):
            task["result"] = json.loads(request.content)
            return httpx.Response(200, json={"status": task["result"]["status"]})
        task["looks"] += 1
        for approval in task["approvals"]:
            if approval["status"] == "pending" and task["looks"] > 1 and self.answer:
                approval["status"] = self.answer
        return httpx.Response(200, json={"id": task_id, "stop_requested": task["stop_requested"],
                                         "approvals": [{"id": a["id"], "status": a["status"]}
                                                       for a in task["approvals"]]})


@pytest.fixture
def cloud_fake(request):
    request.getfixturevalue("fake")  # test_cloud's isolation: no machine policy, a fresh usage ledger
    return TaskCloud()


@pytest.fixture
def enrolled(cloud_fake, tmp_path):
    client = _client(cloud_fake)
    _sign_in(client, cloud_fake)
    client.enroll("org_acme")
    project = tmp_path / "project"
    project.mkdir()
    client.settings.update_section("cloud", {"remote_tasks": True, "remote_tasks_project": str(project),
                                             "remote_tasks_mode": "ask"})
    client.settings.update_section("general", {"default_backend": "anthropic", "default_model": "claude-haiku-4-5"})
    return client, project


def runner_for(client, **kwargs) -> RemoteTasks:
    runner = RemoteTasks(client.settings, client, sleep=lambda seconds: None, **kwargs)
    client.remote_tasks = runner
    return runner


def test_a_request_is_claimed_asked_about_and_answered(enrolled, cloud_fake, monkeypatch):
    client, project = enrolled
    backend = StreamingBackend(name="anthropic", model="claude-haiku-4-5", scripts=[
        [tool_call("file_write", {"path": "notes.txt", "content": "from Slack\n"}), done()],
        [text_delta("Wrote notes.txt."), done()],
    ])
    monkeypatch.setattr(headless, "build_spec", lambda settings, provider, model, project: SimpleNamespace(
        create_backend=lambda settings: backend, permission_mode=""))
    runner = runner_for(client)
    assert runner.step() == remote_tasks.POLL_SECONDS  # nothing queued

    cloud_fake.queue.append({"id": "rtk_1", "prompt": "Write notes.txt", "source": "Slack"})
    assert runner.step() == 1.0
    task = cloud_fake.tasks["rtk_1"]
    assert [a["text"] for a in task["approvals"]] == ["write notes.txt (11 characters)"]
    assert task["result"] == {"status": "done", "text": "Wrote notes.txt."}
    assert (project / "notes.txt").read_text(encoding="utf-8") == "from Slack\n"
    assert runner.status()["last"]["status"] == "done" and not runner.status()["running"]


class AskingSession:
    def __init__(self, actions=1):
        self.cancel_requested = False
        self.actions = actions

    def run(self, prompt, on_permission=None):
        answers = [on_permission("bash", {"command": "make deploy"}) for _ in range(self.actions)]
        yield {"event": "text.done", "text": f"answers: {answers}"}

    def cancel(self):
        self.cancel_requested = True


def test_denials_silence_and_stops(enrolled, cloud_fake):
    client, _ = enrolled
    runner = runner_for(client, session_factory=lambda project, mode, task_id: AskingSession())
    cloud_fake.answer = "denied"
    cloud_fake.queue.append({"id": "rtk_1", "prompt": "deploy"})
    runner.step()
    assert cloud_fake.tasks["rtk_1"]["result"] == {"status": "done", "text": "answers: [False]"}
    assert cloud_fake.tasks["rtk_1"]["approvals"][0]["text"] == "run `make deploy`"

    # Nobody answers: refused once the time is up.
    ticks = iter(range(0, 10_000, 100))
    cloud_fake.answer = ""
    silent = runner_for(client, session_factory=lambda project, mode, task_id: AskingSession(),
                        clock=lambda: float(next(ticks)))
    cloud_fake.queue.append({"id": "rtk_2", "prompt": "deploy"})
    silent.step()
    assert cloud_fake.tasks["rtk_2"]["result"]["text"] == "answers: [False]"

    # The person says stop while an approval waits.
    stopper = runner_for(client, session_factory=lambda project, mode, task_id: AskingSession(actions=2))
    cloud_fake.queue.append({"id": "rtk_3", "prompt": "deploy"})
    original = cloud_fake.__call__

    def stop_on_look(request):
        if request.url.path == "/api/v1/devices/tasks/rtk_3" and request.method == "GET":
            cloud_fake.tasks["rtk_3"]["stop_requested"] = True
        return original(request)

    cloud_fake.__call__ = stop_on_look
    client._transport = httpx.MockTransport(stop_on_look)
    stopper.step()
    assert cloud_fake.tasks["rtk_3"]["result"] == {"status": "stopped", "text": "answers: [False, False]"}


def test_failures_still_reach_the_chat(enrolled, cloud_fake):
    client, _ = enrolled

    class Failing:
        cancel_requested = False

        def run(self, prompt, on_permission=None):
            yield {"event": "error", "message": "The model is unavailable."}

    broken = runner_for(client, session_factory=lambda *a: (_ for _ in ()).throw(RuntimeError("no model")))
    cloud_fake.queue.append({"id": "rtk_10", "prompt": "x"})
    broken.step()
    assert cloud_fake.tasks["rtk_10"]["result"] == {"status": "failed", "text": "RuntimeError: no model"}
    cloud_fake.queue.append({"id": "rtk_11", "prompt": "x"})
    runner_for(client, session_factory=lambda *a: Failing()).step()
    assert cloud_fake.tasks["rtk_11"]["result"] == {"status": "failed", "text": "The model is unavailable."}


def test_what_keeps_it_from_running(enrolled, cloud_fake, tmp_path):
    client, project = enrolled
    runner = runner_for(client)
    assert runner.blocked() == ""
    client.settings.update_section("cloud", {"remote_tasks_project": str(tmp_path / "gone")})
    assert "project folder" in runner.blocked()
    client.settings.update_section("cloud", {"remote_tasks_project": str(project), "remote_tasks": False})
    assert runner.blocked() == "Turned off." and runner.step() == remote_tasks.IDLE_SECONDS
    client.settings.update_section("cloud", {"remote_tasks": True})
    device = dict(client.device(), how="managed")
    client.settings.update_section("cloud", {"device": device})
    assert "managed computer" in runner.blocked()
    client.settings.update_section("cloud", {"device": {}})
    assert "isn't enrolled" in runner.blocked()


def _run(client, command: str, **msg) -> list[dict]:
    ctx = ws_commands.CommandContext(ws=_StubWS(), state=SimpleNamespace(cloud=client, settings=client.settings),
                                     msg={"command": command, **msg}, runs=SimpleNamespace(busy=False))
    asyncio.run(ws_commands.HANDLERS[command](ctx))
    return ctx.ws.sent


def test_the_lumi_account_page_turns_it_on(enrolled, tmp_path):
    client, project = enrolled
    runner_for(client)
    client.settings.update_section("cloud", {"remote_tasks": False})
    [reply] = _run(client, "cloud_remote_tasks", enabled=True, project=str(tmp_path / "missing"), mode="ask")
    assert "must exist" in reply["data"]["error"] and reply["data"]["remote_tasks"]["enabled"] is False
    [reply] = _run(client, "cloud_remote_tasks", enabled=True, project=str(project), mode="auto-edit")
    assert reply["data"]["error"] == ""
    assert reply["data"]["remote_tasks"] == {"enabled": True, "project": str(project), "mode": "auto-edit",
                                             "blocked": "", "running": False, "last": {}}
    [reply] = _run(client, "cloud_remote_tasks", enabled=False, project=str(project), mode="yolo")
    assert "Ask, Auto-edit or Bypass" in reply["data"]["error"]

    lumi_policy.set_for_tests(parse({"schema": "lumi.policy/v1", "organization": "Acme",
                                     "settings": {"cloud.remote_tasks": False}}, source="t"))
    [reply] = _run(client, "cloud_remote_tasks", enabled=True, project=str(project), mode="ask")
    assert "Acme manages tasks from Slack and Teams" in reply["data"]["error"]
    assert reply["data"]["remote_tasks"]["enabled"] is False  # the lock wins over what was saved
    assert client.settings.get("cloud", "remote_tasks") is False


def test_device_calls_carry_the_device_token(enrolled, cloud_fake):
    client, _ = enrolled
    assert client.device_call("POST", "/api/v1/devices/tasks/claim") == {}  # 204: nothing to do
    cloud_fake.queue.append({"id": "rtk_5", "prompt": "hi"})
    assert client.device_call("POST", "/api/v1/devices/tasks/claim")["id"] == "rtk_5"
    assert URL.startswith("https://")
