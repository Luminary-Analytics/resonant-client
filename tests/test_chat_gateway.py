"""The chat gateway (lumi/gateway): approvals and stops from the chat, Telegram, Slack, and its sessions."""

from __future__ import annotations

import json
import threading
import time
from argparse import Namespace
from types import SimpleNamespace

import httpx
import pytest

from lumi import headless, secret_scan
from lumi import policy as lumi_policy
from lumi.gateway import cli
from lumi.gateway.base import ChannelAdapter, InboundMessage
from lumi.gateway.service import GatewayService, describe_call, parse_command
from lumi.gateway.slack import SlackChannel, _plain
from lumi.gateway.telegram import TelegramChannel
from lumi.policy import parse
from tests.streaming_stub import StreamingBackend, done, text_delta, tool_call


def wait_for(condition, timeout: float = 5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = condition()
        if value:
            return value
        time.sleep(0.01)
    raise AssertionError("timed out waiting")


class Chat(ChannelAdapter):
    """A channel that records what the gateway says and asks."""

    name = "test"

    def __init__(self):
        self.sent: list[tuple[str, str]] = []
        self.asks: list[tuple[str, str, str]] = []

    def run(self, on_message):  # the tests call service.receive themselves
        raise NotImplementedError

    def send(self, chat_id, text):
        self.sent.append((chat_id, text))

    def ask(self, chat_id, text, approval_id):
        self.asks.append((chat_id, text, approval_id))

    def texts(self):
        return [text for _, text in self.sent]


class AskingSession:
    """Asks to run a command, then says whether it was allowed."""

    def __init__(self):
        self.cancel_requested = False
        self.cancels = 0
        self.prompts: list[str] = []

    def run(self, text, on_permission=None):
        self.prompts.append(text)
        allowed = on_permission("bash", {"command": "rm -rf build"})
        yield {"event": "tool.call", "name": "bash"}
        yield {"event": "text.done", "text": f"ran it: {allowed}"}

    def cancel(self):
        self.cancel_requested = True
        self.cancels += 1

    def reset_cancel(self):
        self.cancel_requested = False


@pytest.fixture
def gateway():
    chat = Chat()
    sessions: dict[str, AskingSession] = {}

    def factory(chat_id):
        sessions[chat_id] = AskingSession()
        return sessions[chat_id]

    service = GatewayService(chat, factory, describe=lambda: "Project: demo", approval_seconds=5)
    worker = threading.Thread(target=service._worker, daemon=True)
    worker.start()
    yield service, chat, sessions
    service.stop()
    worker.join(timeout=2)


def message(text, chat_id="c1"):
    return InboundMessage(chat_id=chat_id, sender="alice", text=text, channel="test")


def test_approvals_are_answered_from_the_chat(gateway):
    service, chat, sessions = gateway
    service.receive(message("clean the build"))
    chat_id, question, approval_id = wait_for(lambda: chat.asks and chat.asks[-1])
    assert chat_id == "c1" and question == "Lumi wants to run `rm -rf build`."
    service.receive(message("/status"))
    assert "Project: demo\nWaiting for your approval." in chat.texts()[-1]
    service.receive(message("/approve nope"))  # someone else's request
    assert chat.texts()[-1] == "Nothing is waiting for an answer."
    service.receive(message(f"/approve {approval_id}"))
    wait_for(lambda: "ran it: True" in chat.texts())
    assert "Approved." in chat.texts()

    service.receive(message("again"))
    wait_for(lambda: len(chat.asks) == 2)
    service.receive(message("/DENY"))  # without an id, and in capitals
    wait_for(lambda: "ran it: False" in chat.texts())
    assert "Denied." in chat.texts()
    assert sessions["c1"].prompts == ["clean the build", "again"]  # one session per chat


def test_stop_and_unanswered_approvals(gateway):
    service, chat, sessions = gateway
    service.receive(message("/stop"))
    assert chat.texts()[-1] == "Nothing is running."
    service.receive(message("clean the build"))
    wait_for(lambda: chat.asks)
    service.receive(message("and then this"))  # arrives while the first waits
    assert chat.texts()[-1] == "Queued: Lumi is working on another request."
    service._approval_seconds = 0.05  # the queued message's approval will go unanswered
    service.receive(message("/stop"))
    wait_for(lambda: "Stopped." in chat.texts())
    assert sessions["c1"].cancels == 1 and "Stopping." in chat.texts()
    assert "Denied." not in chat.texts()  # a stop isn't reported as a denial
    wait_for(lambda: any("No answer within 1 minute, so it wasn't done." in text for text in chat.texts()))
    wait_for(lambda: "ran it: False" in chat.texts())


def test_help_and_clear(gateway):
    service, chat, sessions = gateway
    service.receive(message("/help"))
    wait_for(lambda: chat.sent and "approve, deny — answer" in chat.texts()[-1])
    service.receive(message("/clear"))
    wait_for(lambda: chat.texts()[-1] == "Conversation cleared.")


def test_commands_with_or_without_the_slash():
    assert parse_command("/stop") == ("stop", "")
    assert parse_command("  Stop ") == ("stop", "")  # Slack keeps a leading slash for its own commands
    assert parse_command("/stop@lumi_bot") == ("stop", "")  # Telegram groups
    assert parse_command("approve 1a2b") == ("approve", "1a2b")
    assert parse_command("/DENY") == ("deny", "")
    for text in ("stop the server", "status report please", "clear 1a2b", "approved", "", "/unknown"):
        assert parse_command(text) == ("", ""), text


def test_plain_words_answer_and_stop(gateway):
    service, chat, sessions = gateway
    service.receive(message("clean the build"))
    wait_for(lambda: chat.asks)
    service.receive(message("approve"))
    wait_for(lambda: "ran it: True" in chat.texts())
    service.receive(message("stop"))
    assert chat.texts()[-1] == "Nothing is running."
    service.receive(message("please stop the server"))  # a request for the agent, not a command
    wait_for(lambda: len(chat.asks) == 2)
    assert sessions["c1"].prompts[-1] == "please stop the server"
    service.receive(message("deny"))
    wait_for(lambda: "ran it: False" in chat.texts())


def test_actions_are_described_without_saved_keys(monkeypatch):
    assert describe_call("file_write", {"path": "a.py", "content": "x" * 12}) == "write a.py (12 characters)"
    assert describe_call("file_edit", {"path": "a.py", "old": "a", "new": "b"}) == "edit a.py"
    assert describe_call("git_commit", {"message": "Fix"}) == 'use git_commit ({"message": "Fix"})'
    assert describe_call("web_fetch", {}) == "use web_fetch"
    assert len(describe_call("bash", {"command": "echo " + "y" * 900})) < 330
    secret_scan.configure(SimpleNamespace(get=lambda section, key=None, default=None: {
        ("privacy", "secret_scan"): True}.get((section, key), default), get_all=lambda: {
            "api_keys": {"openai": "sk-live-abcdefghijklmnop1234"}}))
    try:
        shown = describe_call("bash", {"command": "curl -H 'Authorization: sk-live-abcdefghijklmnop1234' x"})
        assert "sk-live-abcdefghijklmnop1234" not in shown and "REDACTED" in shown
    finally:
        secret_scan.reset()


# ── Telegram ─────────────────────────────────────────────────────────


class TelegramApi:
    def __init__(self, batches):
        self.batches = list(batches)
        self.calls: list[tuple[str, dict]] = []
        self.channel = None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content or b"{}")
        self.calls.append((method, body))
        if method == "getMe":
            return httpx.Response(200, json={"ok": True, "result": {"username": "lumi_bot"}})
        if method == "getUpdates":
            if not self.batches:
                self.channel.stop()
                return httpx.Response(200, json={"ok": True, "result": []})
            return httpx.Response(200, json={"ok": True, "result": self.batches.pop(0)})
        return httpx.Response(200, json={"ok": True, "result": {}})


def test_telegram_buttons_and_allowlist():
    updates = [
        {"update_id": 1, "message": {"chat": {"id": 42}, "from": {"username": "alice"}, "text": "hello"}},
        {"update_id": 2, "message": {"chat": {"id": 99}, "from": {"username": "mallory"}, "text": "hi"}},
        {"update_id": 3, "callback_query": {"id": "q1", "data": "approve:ab12", "from": {"username": "alice"},
                                            "message": {"chat": {"id": 42}}}},
        {"update_id": 4, "callback_query": {"id": "q2", "data": "approve:ab12", "from": {"username": "mallory"},
                                            "message": {"chat": {"id": 99}}}},
    ]
    api = TelegramApi([updates])
    channel = TelegramChannel("123:ABC", ["42"], client=httpx.Client(transport=httpx.MockTransport(api)))
    api.channel = channel
    received = []
    channel.run(received.append)
    assert [(m.chat_id, m.text) for m in received] == [("42", "hello"), ("42", "/approve ab12")]
    polls = [body for method, body in api.calls if method == "getUpdates"]
    assert polls[0]["allowed_updates"] == ["message", "callback_query"] and polls[1]["offset"] == 5
    answers = [body for method, body in api.calls if method == "answerCallbackQuery"]
    assert [(a["callback_query_id"], a["text"]) for a in answers] == [("q1", "Approved"), ("q2", "Not allowed")]
    rejected = [body for method, body in api.calls if method == "sendMessage"]
    assert rejected[0]["chat_id"] == "99" and "Your chat ID is: 99" in rejected[0]["text"]

    channel.ask("42", "Lumi wants to run `ls`.", "cd34")
    method, body = api.calls[-1]
    assert method == "sendMessage" and body["text"] == "Lumi wants to run `ls`."
    assert body["reply_markup"] == {"inline_keyboard": [[{"text": "Approve", "callback_data": "approve:cd34"},
                                                         {"text": "Deny", "callback_data": "deny:cd34"}]]}
    with pytest.raises(ValueError, match="Telegram bot token"):
        TelegramChannel("")


# ── Slack ────────────────────────────────────────────────────────────


class SlackApi:
    def __init__(self):
        self.calls: list[tuple[str, str, dict]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content or b"{}")
        self.calls.append((method, request.headers["authorization"], body))
        if method == "auth.test":
            return httpx.Response(200, json={"ok": True, "user_id": "UBOT", "user": "lumi", "team": "Acme"})
        if method == "apps.connections.open":
            return httpx.Response(200, json={"ok": True, "url": "wss://wss.slack.test/link"})
        return httpx.Response(200, json={"ok": True})


class Socket:
    """A Socket Mode connection that plays envelopes, then asks to reconnect."""

    def __init__(self, envelopes, on_empty):
        self.envelopes = [json.dumps(e) for e in envelopes]
        self.sent: list[dict] = []
        self.on_empty = on_empty

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def recv(self, timeout=None):
        if self.envelopes:
            return self.envelopes.pop(0)
        self.on_empty()
        raise TimeoutError

    def send(self, text):
        self.sent.append(json.loads(text))

    def close(self):
        pass


def _event(envelope_id, **event):
    return {"envelope_id": envelope_id, "type": "events_api", "payload": {"event": {"type": "message", **event}}}


def test_slack_socket_mode(monkeypatch):
    api = SlackApi()
    envelopes = [
        {"type": "hello"},
        _event("e1", channel="D1", channel_type="im", user="UALICE", text="Fix <https://x.test/a|the bug> &amp; test"),
        _event("e2", channel="C1", channel_type="channel", user="UALICE", text="no mention here"),
        _event("e3", channel="C1", channel_type="channel", user="UBOB", text="<@UBOT> run the tests"),
        _event("e4", channel="D1", channel_type="im", bot_id="B1", text="a bot's own message"),
        _event("e5", channel="D1", channel_type="im", user="UALICE", subtype="message_changed", text="edit"),
        _event("e6", channel="D9", channel_type="im", user="UMALLORY", text="let me in"),
        {"envelope_id": "i1", "type": "interactive", "payload": {
            "type": "block_actions", "user": {"id": "UALICE"}, "channel": {"id": "D1"},
            "actions": [{"action_id": "lumi_approve", "value": "ab12"}]}},
        {"envelope_id": "i2", "type": "interactive", "payload": {
            "type": "block_actions", "user": {"id": "UMALLORY"}, "channel": {"id": "D9"},
            "actions": [{"action_id": "lumi_deny", "value": "ab12"}]}},
        {"type": "disconnect", "reason": "refresh_requested"},
    ]
    sockets = []
    channel = SlackChannel("xoxb-bot", "xapp-app", ["UALICE", "C1"],
                           client=httpx.Client(transport=httpx.MockTransport(api)),
                           connect=lambda url: sockets.append(Socket(envelopes if not sockets else [], channel.stop))
                           or sockets[-1])
    received = []
    channel.run(received.append)
    assert [(m.chat_id, m.sender, m.text) for m in received] == [
        ("D1", "UALICE", "Fix the bug (https://x.test/a) & test"),
        ("C1", "UBOB", "run the tests"),  # allowed through the channel
        ("D1", "UALICE", "/approve ab12"),
    ]
    assert len(sockets) == 2  # reconnected after "disconnect"
    assert [ack["envelope_id"] for ack in sockets[0].sent] == ["e1", "e2", "e3", "e4", "e5", "e6", "i1", "i2"]
    opens = [auth for method, auth, _ in api.calls if method == "apps.connections.open"]
    assert opens == ["Bearer xapp-app", "Bearer xapp-app"]
    posts = [(auth, body) for method, auth, body in api.calls if method == "chat.postMessage"]
    assert posts[0][0] == "Bearer xoxb-bot" and posts[0][1]["channel"] == "D9"
    assert "Your user ID: UMALLORY" in posts[0][1]["text"]

    channel.ask("D1", "Lumi wants to run `ls`.", "cd34")
    _, _, body = api.calls[-1]
    buttons = body["blocks"][1]["elements"]
    assert [(b["action_id"], b["value"]) for b in buttons] == [("lumi_approve", "cd34"), ("lumi_deny", "cd34")]
    assert body["blocks"][0]["text"] == {"type": "plain_text", "text": "Lumi wants to run `ls`."}
    for bot, app in (("xapp-x", "xapp-y"), ("xoxb-x", "xoxb-y")):
        with pytest.raises(ValueError):
            SlackChannel(bot, app)
    assert _plain("see <https://a.test>") == "see https://a.test"


# ── Sessions built like `lumi run` ───────────────────────────────────


class _Settings:
    def __init__(self, data=None):
        self.data = data or {}

    def get(self, section, key=None, default=None):
        values = self.data.get(section, {})
        return values if key is None else values.get(key, default)

    def get_all(self):
        return self.data


def _args(**overrides):
    values = dict(channel="", project="", mode="", backend="anthropic", model="claude-haiku-4-5", token="",
                  slack_bot_token="", slack_app_token="", allow=[], approval_minutes=0, max_tokens=None)
    values.update(overrides)
    return Namespace(**values)


def test_the_channel_comes_from_the_tokens(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-1")
    monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-1")
    settings = _Settings({"gateway": {"slack_allowed": ["C1"]}})
    adapter, allowed = cli.build_adapter(_args(), settings)
    assert adapter.name == "slack" and allowed == ["C1"]
    adapter, allowed = cli.build_adapter(_args(token="123:ABC", allow=["42"]), settings)
    assert adapter.name == "telegram" and allowed == ["42"]
    with pytest.raises(cli.GatewayError):
        cli.build_adapter(_args(channel="irc"), settings)


def test_sessions_follow_the_project_and_mode(tmp_path, monkeypatch):
    backend = StreamingBackend(name="anthropic", model="claude-haiku-4-5", scripts=[
        [tool_call("file_write", {"path": "notes.txt", "content": "from the chat\n"}), done()],
        [text_delta("Wrote notes.txt."), done()],
        [tool_call("file_write", {"path": "other.txt", "content": "no\n"}), done()],
        [text_delta("Left it."), done()],
    ])
    monkeypatch.setattr(headless, "build_spec", lambda settings, provider, model, project: SimpleNamespace(
        create_backend=lambda settings: backend, permission_mode=""))
    chat = Chat()
    service = cli.build_service(_args(project=str(tmp_path)), _Settings(), chat)
    session = service._session_for("42")
    assert (session.autonomy_tier, session.project_path, session.audit_session_id) == ("ask", str(tmp_path),
                                                                                      "gateway:42")
    assert session.auto_approve is False and session.project_content_trusted is False
    assert "Permission mode: ask" in service._describe()

    worker = threading.Thread(target=service._worker, daemon=True)
    worker.start()
    try:
        service.receive(message("write the notes", "42"))
        _, question, _ = wait_for(lambda: chat.asks and chat.asks[-1])
        assert question == "Lumi wants to write notes.txt (14 characters)."
        assert not (tmp_path / "notes.txt").exists()
        service.receive(message("/approve", "42"))
        wait_for(lambda: "Wrote notes.txt." in chat.texts())
        assert (tmp_path / "notes.txt").read_text(encoding="utf-8") == "from the chat\n"

        service.receive(message("write another", "42"))
        wait_for(lambda: len(chat.asks) == 2)
        service.receive(message("/deny", "42"))
        wait_for(lambda: "Left it." in chat.texts())
        assert not (tmp_path / "other.txt").exists()
    finally:
        service.stop()
        worker.join(timeout=2)

    bypass = cli.build_service(_args(project=str(tmp_path), mode="bypass"), _Settings(), Chat())
    assert bypass._session_for("1").autonomy_tier == "full-auto"
    with pytest.raises(cli.GatewayError, match="not a folder"):
        cli.build_service(_args(project=str(tmp_path / "missing")), _Settings(), Chat())


def test_the_organization_policy_applies(tmp_path, monkeypatch):
    monkeypatch.setattr(headless, "build_spec", lambda *a: SimpleNamespace(create_backend=lambda s: None,
                                                                         permission_mode=""))
    lumi_policy.set_for_tests(parse({"schema": "lumi.policy/v1", "organization": "Acme",
                                     "permissions": {"allowed_modes": ["ask"]}}, source="t"))
    with pytest.raises(cli.GatewayError, match="Acme's policy doesn't allow --mode bypass"):
        cli.build_service(_args(project=str(tmp_path), mode="bypass"), _Settings(), Chat())
    assert cli.build_service(_args(project=str(tmp_path)), _Settings(), Chat())
