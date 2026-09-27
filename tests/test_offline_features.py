"""Offline mode applied: each outbound feature fails at once with the message.

Mock transports and fakes only. Each test turns offline mode on, calls the
feature, and checks the refusal names the feature and host and that nothing
was sent. conftest.py turns offline mode off again after every test.
"""

from __future__ import annotations

import base64
import json
import shutil
import subprocess
import sys
import time
from types import SimpleNamespace

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from lumi import offline

MESSAGE = "; allow it or turn offline mode off."


class _Recorder:
    """A mock transport that records every request it receives."""

    def __init__(self, status: int = 200, body: dict | None = None):
        self.requests: list[httpx.Request] = []
        self.status = status
        self.body = body or {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(self.status, json=self.body)


def _on(*hosts: str) -> None:
    offline.set_for_tests(enabled=True, allowed_hosts=hosts)


# ── Lumi Cloud: check-ins, sign-in, sharing, the team library ───────────────


@pytest.fixture
def enrolled_cloud(tmp_path):
    from lumi.cloud import DEVICE_SECRET, CloudClient
    from lumi.gui.settings import SettingsManager

    settings = SettingsManager(tmp_path / "settings.json")
    key = Ed25519PrivateKey.generate()
    raw = key.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())
    settings.update_section("cloud", {"url": "https://cloud.example.test", "device": {
        "id": "dev-1", "organization_id": "org-1", "organization_name": "Acme", "how": "joined"}})
    settings.set("api_keys", DEVICE_SECRET, base64.b64encode(raw).decode())
    settings.set("api_keys", "lumi_cloud_refresh", "refresh-token")
    recorder = _Recorder()
    opened: list[str] = []
    return CloudClient(settings, transport=httpx.MockTransport(recorder), open_browser=opened.append), recorder, opened


class TestLumiCloud:
    def test_check_in_fails_fast_with_the_message(self, enrolled_cloud):
        from lumi.cloud import CloudError

        client, recorder, _ = enrolled_cloud
        _on()
        started = time.monotonic()
        with pytest.raises(CloudError) as caught:
            client.check_in()
        assert time.monotonic() - started < 2
        assert str(caught.value) == "Offline mode: Lumi Cloud needs cloud.example.test" + MESSAGE
        assert caught.value.code == "offline"
        assert recorder.requests == []
        # The background loop records it and waits for its next round.
        assert client.background_step() > 0 and client.last_error.startswith("Offline mode: Lumi Cloud")

    def test_sign_in_is_refused_before_the_browser_opens(self, enrolled_cloud):
        from lumi.cloud import CloudError

        client, recorder, opened = enrolled_cloud
        _on()
        with pytest.raises(CloudError, match="Offline mode: Lumi Cloud needs cloud.example.test"):
            client.begin_sign_in("https://cloud.example.test")
        assert opened == [] and client.status()["signing_in"] is False

    def test_sharing_and_the_team_library(self, enrolled_cloud):
        from lumi import share, team_library
        from lumi.cloud import CloudError

        client, recorder, _ = enrolled_cloud
        _on()
        with pytest.raises(CloudError, match="Offline mode: Lumi Cloud needs"):
            share.share(client, {"messages": []}, organization_id="org-1", visibility="organization")
        with pytest.raises(team_library.LibraryError, match="Offline mode: Lumi Cloud needs"):
            team_library.sync(client)
        assert recorder.requests == []

    def test_an_allowed_lumi_cloud_is_reached(self, enrolled_cloud):
        client, recorder, _ = enrolled_cloud
        recorder.body = {"access_token": "t", "expires_in": 3600, "next_checkin_seconds": 3600}
        _on("cloud.example.test")
        client.check_in()
        assert [request.url.host for request in recorder.requests] == ["cloud.example.test"] * 2


# ── Update checks and downloads ─────────────────────────────────────────────


class TestUpdates:
    def _prefs(self, tmp_path, **offline_settings):
        from lumi import update_channels

        path = tmp_path / "settings.json"
        path.write_text(json.dumps({"offline": offline_settings}), encoding="utf-8")
        return update_channels.read(path, SimpleNamespace(policy=None, error=""), installer="")

    def test_offline_mode_keeps_the_updater_from_the_update_site(self, tmp_path, monkeypatch):
        from lumi import updater

        prefs = self._prefs(tmp_path, enabled=True)
        assert prefs.offline == ("Offline mode: the update check needs luminary-analytics.github.io" + MESSAGE)
        assert prefs.as_dict()["offline"] == prefs.offline
        monkeypatch.setattr(updater, "_load_dll", lambda: pytest.fail("WinSparkle must not load in offline mode"))
        assert updater.init_updater(prefs) is False
        assert updater.check_for_updates_now() is False

    def test_the_check_says_why(self, tmp_path):
        from lumi.gui.ws_commands import _update_check_message

        prefs = self._prefs(tmp_path, enabled=True)
        message = _update_check_message({**prefs.as_dict()}, False)
        assert message.startswith("Offline mode: the update check needs luminary-analytics.github.io")
        assert "install one from a file" in message

    def test_an_allowed_update_site_keeps_checking(self, tmp_path):
        assert self._prefs(tmp_path, enabled=True, allowed_hosts=["luminary-analytics.github.io"]).offline == ""
        assert self._prefs(tmp_path, enabled=False).offline == ""

    def test_turning_it_on_stops_a_running_winsparkle(self, tmp_path, monkeypatch):
        from lumi import updater
        from lumi.update_channels import UpdatePreferences

        calls = []
        monkeypatch.setattr(updater, "_dll", SimpleNamespace(win_sparkle_cleanup=lambda: calls.append("cleanup")))
        monkeypatch.setattr(updater, "_initialized", True)
        monkeypatch.setattr(updater, "read_update_preferences", lambda: UpdatePreferences(offline="Offline mode: x"))
        assert updater.apply_offline_mode() == "Offline mode: x"
        assert calls == ["cleanup"] and updater._dll is None
        # Stopped for this run: turning it off again waits for a restart.
        monkeypatch.setattr(updater, "read_update_preferences", lambda: UpdatePreferences())
        assert updater.status()["restart_to_check"] is True

    def test_on_at_startup_then_off_waits_for_a_restart(self, monkeypatch):
        from lumi import updater
        from lumi.update_channels import UpdatePreferences

        assert updater.init_updater(UpdatePreferences(offline="Offline mode: x")) is False
        monkeypatch.setattr(updater, "read_update_preferences", lambda: UpdatePreferences(offline="Offline mode: x"))
        assert updater.status()["offline"] == "Offline mode: x" and updater.status()["restart_to_check"] is False
        monkeypatch.setattr(updater, "read_update_preferences", lambda: UpdatePreferences())
        info = updater.status()
        assert info["offline"] == "" and info["restart_to_check"] is True and info["available"] is False


# ── Model requests and provider discovery ───────────────────────────────────


class _Backend:
    tool_mode = "native"

    def __init__(self, name: str, base_url: str = "", label: str = "", model: str = "m"):
        self.name, self.base_url, self.model = name, base_url, model
        if label:
            self.PROVIDER_LABEL = label
        self.calls = 0

    def stream(self, **kwargs):
        from lumi.backends import EVENT_DONE, EVENT_TEXT_DELTA

        self.calls += 1
        yield EVENT_TEXT_DELTA, {"delta": "ok"}
        yield EVENT_DONE, {"cognitive_state": None, "stats": None, "model": self.model}


class TestModelRequests:
    def _run(self, backend):
        from lumi.engine.session import Session

        return list(Session(backend, auto_approve=True).run("hi"))

    def test_cloud_provider_turn_is_refused_before_sending(self):
        _on()
        backend = _Backend("openai", "https://api.openai.com/v1", "OpenAI")
        errors = [e for e in self._run(backend) if e.get("event") == "error"]
        assert backend.calls == 0
        assert errors[0]["message"] == "Offline mode: OpenAI needs api.openai.com" + MESSAGE

    def test_local_and_allowed_providers_run(self):
        _on("llm.corp.example")
        local = _Backend("ollama", "http://127.0.0.1:11434", "Ollama")
        onprem = _Backend("conn-gpu", "https://llm.corp.example/v1", "GPU cluster")
        self._run(local)
        self._run(onprem)
        assert local.calls == 1 and onprem.calls == 1

    @pytest.mark.parametrize("name, label, host", [("codex", "Codex", "chatgpt.com"),
                                                    ("claude-code", "Claude Code", "api.anthropic.com")])
    def test_cli_providers_are_refused_whatever_is_allowed(self, name, label, host):
        _on(host)
        backend = _Backend(name)
        errors = [e["message"] for e in self._run(backend) if e.get("event") == "error"]
        assert backend.calls == 0
        assert errors[0].startswith(f"Offline mode: {label} needs {host} and runs as its own program")

    def test_extension_providers_are_refused(self):
        _on()
        backend = _Backend("conn-local-runner")
        backend.connection = {"type": "extension", "name": "Local runner"}
        assert offline.backend_refusal(backend).startswith("Offline mode: Local runner runs as a process")

    def test_auxiliary_requests_are_refused_too(self):
        from lumi.engine.request_purpose import auxiliary_stream

        _on()
        backend = _Backend("anthropic", "https://api.anthropic.com", "Anthropic")
        events = list(auxiliary_stream(backend, "title", user_msg="x", conversation_history=[], instructions="",
                                       tools=[]))
        assert events == [("error", {"message": "Offline mode: Anthropic needs api.anthropic.com" + MESSAGE})]
        assert backend.calls == 0

    def test_a_request_to_another_backend_during_a_turn_is_refused_too(self):
        # A compression model, or offline mode turned on after the turn started.
        _on()
        session = self._session_on(_Backend("ollama", "http://127.0.0.1:11434"))
        cloud = _Backend("openai", "https://api.openai.com/v1", "OpenAI")
        events = list(session._model_stream(purpose="compression", backend=cloud, user_msg="x",
                                            conversation_history=[], instructions="", tools=[]))
        assert events == [("error", {"message": "Offline mode: OpenAI needs api.openai.com" + MESSAGE})]
        assert cloud.calls == 0

    @staticmethod
    def _session_on(backend):
        from lumi.engine.session import Session

        return Session(backend, auto_approve=True)

    def test_fallbacks_offline_mode_blocks_are_skipped(self):
        from lumi.engine.session import Session

        _on()
        session = Session(_Backend("ollama", "http://127.0.0.1:11434"), auto_approve=True)
        cloud = _Backend("openai", "https://api.openai.com/v1", "OpenAI")
        local = _Backend("ollama", "http://localhost:11434", model="small")
        session.fallback_provider = lambda: [("openai:gpt", lambda: cloud), ("ollama:small", lambda: local)]
        list(session._next_fallback("failed"))
        assert session.backend is local

    def test_an_openai_compatible_stream_fails_fast_with_the_message(self):
        from lumi.connections import OpenAICompatibleBackend

        recorder = _Recorder()
        backend = OpenAICompatibleBackend({"id": "gw", "name": "Gateway", "type": "openai-compatible",
                                           "base_url": "https://gateway.example.com/v1", "auth": "none",
                                           "headers": {}}, "model", transport=httpx.MockTransport(recorder))
        _on()
        events = list(backend.stream("hi", [], "", []))
        assert events[-1] == ("error", {"message": "Offline mode: Gateway needs gateway.example.com" + MESSAGE})
        assert recorder.requests == []

    def test_an_anthropic_stream_fails_fast_with_the_message(self):
        from lumi.anthropic_api import AnthropicBackend

        recorder = _Recorder()
        backend = AnthropicBackend("sk-test", "claude-test", transport=httpx.MockTransport(recorder))
        _on()
        events = list(backend.stream("hi", [], "", []))
        assert ("error", {"message": "Offline mode: Anthropic needs api.anthropic.com" + MESSAGE}) in events
        assert recorder.requests == []


class TestWithDataLossPrevention:
    """Offline mode and the organization's DLP rules (lumi/dlp.py) on the same requests.

    Offline mode refuses first: a provider it can't reach gets nothing, and
    DLP neither checks nor records the request. A reachable provider's
    request still passes DLP and reaches the backend through ``dlp.send``.
    """

    CARD = "4111 1111 1111 1111"

    @pytest.fixture
    def records(self, tmp_path):
        from lumi import audit, policy
        from lumi.audit import AuditLog

        log = AuditLog(tmp_path / "audit")
        audit.set_for_tests(log)
        policy.set_for_tests(policy.parse({"schema": policy.SCHEMA, "organization": "Acme",
                                           "dlp": {"version": 1, "detectors": {"credit_card": "redact"},
                                                   "rules": []}}, source="test policy"))
        return lambda: [json.loads(line) for path in sorted(log.root.glob("*.jsonl"))
                        for line in path.read_text(encoding="utf-8").splitlines()]

    @staticmethod
    def _guarded(name, url, label=""):
        from lumi import dlp

        @dlp.guard_backend
        class Guarded(_Backend):
            """Guarded like the real backends: a request that skipped DLP raises instead of arriving."""

            def stream(self, **kwargs):
                self.sent = kwargs
                yield from _Backend.stream(self, **kwargs)

        return Guarded(name, url, label)

    def _request(self):
        return {"user_msg": f"Charge {self.CARD} now", "conversation_history": [], "instructions": "",
                "tools": []}

    def test_an_unreachable_provider_gets_nothing_not_even_a_dlp_check(self, records):
        from lumi.engine.request_purpose import auxiliary_stream
        from lumi.engine.session import Session

        _on()
        cloud = self._guarded("openai", "https://api.openai.com/v1", "OpenAI")
        refused = [("error", {"message": "Offline mode: OpenAI needs api.openai.com" + MESSAGE})]
        assert list(auxiliary_stream(cloud, "title", **self._request())) == refused
        session = Session(self._guarded("ollama", "http://127.0.0.1:11434"), auto_approve=True)
        assert list(session._model_stream(purpose="compression", backend=cloud, **self._request())) == refused
        errors = [e for e in Session(cloud, auto_approve=True).run(f"Charge {self.CARD} now")
                  if e.get("event") == "error"]
        assert errors[0]["message"] == "Offline mode: OpenAI needs api.openai.com" + MESSAGE
        assert cloud.calls == 0
        assert not [record for record in records() if record["type"].startswith("dlp.")]

    def test_planning_asks_nothing_of_a_provider_it_cant_reach(self, records):
        from lumi import dlp
        from lumi.engine.session import Session

        @dlp.guard_backend
        class Classifying(_Backend):
            def classify(self, prompt, max_tokens=20):
                self.calls += 1
                return "COMPLEX"

        # The terminal UI asks before each turn; Codex's classify would start its own program.
        _on()
        codex = Classifying("codex", label="Codex")
        assert Session(codex, auto_approve=True).should_plan(f"Charge {self.CARD} now") is False
        assert codex.calls == 0 and not [r for r in records() if r["type"].startswith("dlp.")]
        local = Classifying("ollama", "http://127.0.0.1:11434")
        assert Session(local, auto_approve=True).should_plan("Refactor the parser") is True and local.calls == 1

    def test_a_reachable_provider_still_gets_dlps_redacted_copy(self, records):
        from lumi.engine.request_purpose import auxiliary_stream

        _on("llm.corp.example")
        local = self._guarded("conn-gpu", "https://llm.corp.example/v1", "GPU cluster")
        events = list(auxiliary_stream(local, "title", **self._request()))
        assert local.calls == 1 and events[-1][0] == "done"
        assert local.sent["user_msg"] == "Charge [REDACTED:credit_card] now"
        assert any(record["type"].startswith("dlp.") for record in records())


class TestProviderDiscovery:
    def _state(self, tmp_path, monkeypatch):
        from lumi.backends import ExoBackend
        from lumi.gui import app as gui_app
        from lumi.gui.settings import SettingsManager

        settings = SettingsManager(tmp_path / "settings.json")
        settings.set("api_keys", "openai", "sk-test")
        settings.set("connections", None, [
            {"id": "gpu", "name": "GPU cluster", "type": "openai-compatible",
             "base_url": "https://llm.corp.example/v1", "auth": "none", "models": ["qwen3-coder"]},
            {"id": "gateway", "name": "Gateway", "type": "openai-compatible",
             "base_url": "https://gateway.example.com/v1", "auth": "none", "models": ["gpt-x"]},
        ])
        state = gui_app.AppState.__new__(gui_app.AppState)
        state.settings = settings
        state.available_backends = {}
        state.offline_hidden_backends = {}
        state.ollama_url = "http://10.0.0.131:11434"
        state.exo_url = "http://127.0.0.1:52415/v1"
        monkeypatch.setattr(gui_app.AppState, "refresh_network_defaults", lambda self: None)
        monkeypatch.setattr(ExoBackend, "discover_models", staticmethod(
            lambda **_: {"models": [], "downloaded_models": [], "running_models": []}))
        monkeypatch.setattr(gui_app, "resolve_codex_cli_path", lambda: "C:/fake/codex.exe")
        monkeypatch.setattr(gui_app, "resolve_claude_cli_path", lambda: "")
        monkeypatch.setattr(gui_app.OpenAIResponsesBackend, "list_available_models",
                            staticmethod(lambda *a, **k: pytest.fail("a hidden provider must not be probed")))
        monkeypatch.setattr(httpx, "get", lambda *a, **k: pytest.fail("a hidden Ollama must not be probed"))
        return state

    def test_the_model_picker_offers_only_reachable_providers(self, tmp_path, monkeypatch):
        state = self._state(tmp_path, monkeypatch)
        _on("llm.corp.example")
        available = state.detect_backends(force=True)
        assert set(available) == {"conn-gpu"}
        hidden = state.offline_hidden_backends
        assert set(hidden) == {"ollama", "openai", "conn-gateway", "codex"}
        assert hidden["openai"]["reason"] == "Offline mode: OpenAI needs api.openai.com" + MESSAGE
        assert hidden["ollama"]["reason"] == "Offline mode: Ollama needs 10.0.0.131" + MESSAGE
        assert hidden["conn-gateway"]["reason"] == "Offline mode: Gateway needs gateway.example.com" + MESSAGE
        assert "runs as its own program" in hidden["codex"]["reason"]
        status = state.offline_status()
        assert status["enabled"] and {item["provider"] for item in status["hidden"]} == set(hidden)
        assert status["license"]["offline_use"] == "unlicensed"

    def test_nothing_is_hidden_while_off(self, tmp_path, monkeypatch):
        state = self._state(tmp_path, monkeypatch)
        monkeypatch.setattr(state, "ollama_url", "http://127.0.0.1:11434")
        from lumi.gui import app as gui_app

        monkeypatch.setattr(httpx, "get", lambda *a, **k: (_ for _ in ()).throw(httpx.ConnectError("down")))
        monkeypatch.setattr(gui_app.OpenAIResponsesBackend, "list_available_models", staticmethod(lambda *a, **k: []))
        available = state.detect_backends(force=True)
        assert {"openai", "conn-gpu", "conn-gateway", "codex"} <= set(available)
        assert state.offline_hidden_backends == {}


# ── The agent's tools ───────────────────────────────────────────────────────


class TestTools:
    def _session(self, tmp_path):
        from lumi.engine.session import Session

        session = Session(_Backend("ollama", "http://127.0.0.1:11434"), auto_approve=True)
        session.project_path = str(tmp_path)
        return session

    def test_browsing_elsewhere_is_refused_with_the_reason(self, tmp_path):
        from lumi.engine.session import ToolBoundaryViolation

        session = self._session(tmp_path)
        _on("docs.corp.example")
        with pytest.raises(ToolBoundaryViolation) as caught:
            session._prepare_workspace_tool_args("browser_navigate", {"url": "example.com/search?q=x"})
        assert str(caught.value) == "Offline mode: browsing needs example.com" + MESSAGE
        with pytest.raises(ToolBoundaryViolation, match="browsing needs evil.example"):
            session._prepare_workspace_tool_args("browser_tabs", {"action": "new", "url": "https://evil.example/"})
        for url in ("http://localhost:3000/", "http://127.0.0.1:8080", "https://docs.corp.example/x", "about:blank"):
            session._prepare_workspace_tool_args("browser_navigate", {"url": url})
        session._prepare_workspace_tool_args("browser_tabs", {"action": "list"})

    @pytest.mark.parametrize("url, host", [
        ("file://evil.example/share/page.html", "evil.example"),
        ("file:////evil.example/share/page.html", "evil.example"),
        ("file://///evil.example/share/page.html", "evil.example"),
        ("file://localhost//evil.example/share/page.html", "evil.example"),
        ("file://%65vil.example/share/page.html", "evil.example"),
        ("file://EVIL.example/C$/page.html", "evil.example"),
    ])
    def test_files_on_other_computers_are_refused(self, url, host):
        # Chrome on Windows opens these as network shares, through Windows' own connections.
        _on("docs.corp.example")
        for tool, args in (("browser_navigate", {"url": url}), ("browser_tabs", {"action": "new", "url": url})):
            assert offline.tool_refusal(tool, args) == (
                f"Offline mode: opening a file from another computer needs {host}" + MESSAGE)

    def test_this_computers_files_and_allowed_shares_open(self, monkeypatch):
        _on("files.corp.example")
        for url in ("file:///C:/Users/me/report.html", "file:///home/me/report.html", "file://localhost/C:/x.html",
                    "file://LOCALHOST/srv/x.html", "file://C:/x.html", "file://127.0.0.1/c$/x.html",
                    "file://files.corp.example/share/x.html", "file:///C:/My%20Files/x.html"):
            assert offline.tool_refusal("browser_navigate", {"url": url}) == "", url
        for url in ("file:///%2F%2Fevil.example/share", "file:///x\t/evil"):
            assert offline.tool_refusal("browser_navigate", {"url": url}) == (
                "Offline mode: opening a file needs an address Lumi can't read" + MESSAGE)
        # Outside Windows a path with no host is this computer's; on Windows Chrome reads it as a share.
        monkeypatch.setattr(offline, "_WINDOWS", True)
        assert offline.file_url_host("file:/evil.example/share") == "evil.example"
        monkeypatch.setattr(offline, "_WINDOWS", False)
        assert offline.file_url_host("file:/etc/hosts") == ""

    def test_the_browser_tools_check_addresses_themselves(self, monkeypatch):
        from lumi.engine import browser

        monkeypatch.setattr(browser, "_ensure", lambda start: pytest.fail("Chrome must not start"))
        _on()
        for execute, args in ((browser.exec_browser_navigate, {"url": "file://evil.example/share/x.html"}),
                              (browser.exec_browser_tabs, {"action": "new", "url": "https://evil.example/"})):
            result = execute(args, time.time())
            assert result.is_error and result.output.startswith("Error: Offline mode: ")

    def test_open_application_may_not_open_an_address(self):
        _on()
        for name in ("https://evil.example/", "microsoft-edge:https://evil.example/", "\\\\evil.example\\share",
                     "//evil.example/share", "mailto:someone@evil.example"):
            assert offline.tool_refusal("open_application", {"name": name}) == (
                "Offline mode: open_application would open that address in another program, which offline mode "
                "can't check; use the browser tools, which reach only allowed hosts, or turn offline mode off."), name
        for name in ("notepad", "C:\\Program Files\\App\\app.exe", "code"):
            assert offline.tool_refusal("open_application", {"name": name}) == "", name

    def test_a_refused_call_reaches_the_model_as_a_refusal(self, tmp_path):
        from lumi.backends import EVENT_DONE, EVENT_TOOL_CALL
        from lumi.engine.session import Session

        class Browsing(_Backend):
            def stream(self, **kwargs):
                self.calls += 1
                if self.calls == 1:
                    yield EVENT_TOOL_CALL, {"id": "c1", "name": "browser_navigate",
                                            "arguments": json.dumps({"url": "https://example.com"})}
                yield EVENT_DONE, {"cognitive_state": None, "stats": None, "model": self.model}

        _on()
        session = Session(Browsing("ollama", "http://127.0.0.1:11434"), auto_approve=True)
        session.project_path = str(tmp_path)
        results = [e for e in session.run("look it up") if e.get("event") == "tool.result"]
        assert results and results[0]["denied"] is True
        assert "Offline mode: browsing needs example.com" in results[0]["output"]

    def test_lumis_browser_starts_limited_to_reachable_hosts(self, tmp_path, monkeypatch):
        from lumi.engine import browser

        launched: list[list[str]] = []
        port = {"open": False}

        class FakeChrome:
            returncode = None

            def poll(self):
                return None

            def terminate(self):
                port["open"] = False

            def wait(self, timeout=None):
                return 0

        def popen(args, **kwargs):
            launched.append(list(args))
            port["open"] = True
            return FakeChrome()

        monkeypatch.setattr(browser, "_find_chrome", lambda: "chrome.exe")
        monkeypatch.setattr(browser, "_profile_dir", lambda: str(tmp_path / "profile"))
        monkeypatch.setattr(browser, "_prepare_extension", lambda *a: None)
        monkeypatch.setattr(browser.subprocess, "Popen", popen)
        monkeypatch.setattr(browser, "_port_is_open", lambda *a, **k: port["open"])
        manager = browser.BrowserManager()
        monkeypatch.setattr(manager, "_attach", lambda target_id="": "attached")
        monkeypatch.setattr(manager, "_load_extension", lambda: None)
        monkeypatch.setattr(manager, "_sync_session_indicator", lambda: None)

        _on("docs.corp.example", "10.20.0.0/16", "fd00::5")
        assert manager.ensure_started() == "attached"
        args = launched[0]
        assert "--proxy-server=http://127.0.0.1:9" in args
        bypass = next(arg for arg in args if arg.startswith("--proxy-bypass-list=")).split("=", 1)[1].split(";")
        # <-loopback> removes Chrome's own direct route to link-local addresses (169.254.0.0/16, fe80::/10).
        assert bypass[0] == "<-loopback>"
        for entry in ("localhost", "127.0.0.0/8", "[::1]", "docs.corp.example", "10.20.0.0/16", "[fd00::5]"):
            assert entry in bypass
        assert "--force-webrtc-ip-handling-policy=disable_non_proxied_udp" in args

        # Offline mode turned off while it runs: Lumi's own Chrome starts again without the limits.
        manager._conn = SimpleNamespace(close=lambda: None)
        offline.reset_for_tests()
        assert manager.ensure_started() == "attached"
        assert len(launched) == 2 and not any(arg.startswith("--proxy-server") for arg in launched[1])

    def test_turning_it_on_closes_lumis_unlimited_chrome_at_once(self, tmp_path, monkeypatch):
        import threading

        from lumi.engine import browser

        closed = threading.Event()

        class FakeChrome:
            def terminate(self):
                closed.set()

            def wait(self, timeout=None):
                return 0

        manager = browser.BrowserManager()
        manager._proc, manager._launched_by_us, manager._network_rules = FakeChrome(), True, ()
        monkeypatch.setattr(browser, "_manager", manager)
        # Its pages would keep connecting until the next browser tool; offline mode closes it instead.
        _on("docs.corp.example")
        assert closed.wait(5)

    def test_a_browser_lumi_didnt_start_isnt_used_offline(self, monkeypatch):
        from lumi.engine import browser

        monkeypatch.setattr(browser, "_port_is_open", lambda *a, **k: True)
        manager = browser.BrowserManager()
        monkeypatch.setattr(manager, "_attach", lambda target_id="": pytest.fail("must not attach"))
        _on()
        assert manager.ensure_started().startswith("Error: Offline mode: browsing needs a browser Lumi starts")

    def test_pull_request_pushes_are_checked_before_git_connects(self, tmp_path):
        from lumi.engine import github_tools

        if not shutil.which("git"):
            pytest.skip("git isn't installed")
        subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
        subprocess.run(["git", "-C", str(tmp_path), "remote", "add", "origin", "https://github.com/acme/app.git"],
                       check=True)
        _on()
        with pytest.raises(github_tools.GitHubError) as caught:
            github_tools._git(str(tmp_path), "push", "--set-upstream", "origin", "feature")
        assert str(caught.value) == "Offline mode: pushing to Git needs github.com" + MESSAGE

    def test_every_push_address_is_checked(self, tmp_path):
        from lumi.engine import github_tools

        if not shutil.which("git"):
            pytest.skip("git isn't installed")
        subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
        # Git pushes to each of a remote's push addresses, not only the first.
        for command in (["remote", "add", "origin", "https://git.corp.example/acme/app.git"],
                        ["remote", "set-url", "--add", "--push", "origin", "https://git.corp.example/acme/app.git"],
                        ["remote", "set-url", "--add", "--push", "origin", "\\\\evil.example\\share\\app.git"]):
            subprocess.run(["git", "-C", str(tmp_path), *command], check=True)
        _on("git.corp.example")
        with pytest.raises(github_tools.GitHubError) as caught:
            github_tools._git(str(tmp_path), "push", "--set-upstream", "origin", "feature")
        assert str(caught.value) == "Offline mode: pushing to Git needs evil.example" + MESSAGE

    @pytest.mark.parametrize("url, host", [
        ("https://github.com/acme/app.git", "github.com"), ("git@github.com:acme/app.git", "github.com"),
        ("ssh://git@git.corp.example:2222/acme/app.git", "git.corp.example"),
        ("https://user@dev.azure.com/org/p/_git/r", "dev.azure.com"), ("C:\\repos\\app", ""),
        ("/srv/git/app.git", ""), ("../app", ""), ("file:///srv/app.git", ""), ("file://localhost/srv/app.git", ""),
        # Network shares are other computers.
        ("\\\\fileserver\\git\\app.git", "fileserver"), ("//fileserver/git/app.git", "fileserver"),
        ("file://fileserver/git/app.git", "fileserver"), ("file:////fileserver/git/app.git", "fileserver"),
        ("\\\\?\\UNC\\fileserver\\git\\app.git", "fileserver"), ("\\\\?\\C:\\repos\\app", ""),
        # %-escapes stay as written, so the host isn't a plain name and offline mode refuses it.
        ("https://%67ithub.com/acme/app.git", "%67ithub.com"),
    ])
    def test_remote_hosts(self, url, host):
        from lumi.engine.github_tools import remote_host

        assert remote_host(url) == host
        _on("github.com", "fileserver.corp.example")
        assert not host or offline.host_allowed(host) is (host == "github.com")

    def test_pull_request_and_issue_apis_fail_fast_with_the_message(self, monkeypatch):
        from lumi.engine import github_tools, issue_trackers

        recorder = _Recorder()
        github_tools.set_transport_for_tests(httpx.MockTransport(recorder))
        monkeypatch.setattr(issue_trackers, "_transport", httpx.MockTransport(recorder))
        monkeypatch.setattr(github_tools, "_token", lambda: "ghp_test")
        _on()
        try:
            repo = github_tools.Repo(host="github.com", owner="acme", name="app")
            with pytest.raises(github_tools.GitHubError, match="^Offline mode: GitHub needs api.github.com"):
                github_tools._request(repo, "GET", repo.path)
            with pytest.raises(issue_trackers.IssueError, match="^Offline mode: Linear needs api.linear.app"):
                issue_trackers._http("POST", "https://api.linear.app/graphql", tracker="linear", headers={})
        finally:
            github_tools.set_transport_for_tests(None)
        assert recorder.requests == []

    def test_remote_mcp_servers_are_refused(self):
        from lumi.engine.mcp import MCPConnection, MCPServerConfig

        recorder = _Recorder()
        connection = MCPConnection(MCPServerConfig(name="docs", transport="http", url="https://mcp.example.com/mcp"),
                                   http_transport=httpx.MockTransport(recorder))
        _on()
        assert connection.connect() is False
        assert connection.last_error == "Offline mode: the MCP server docs needs mcp.example.com" + MESSAGE
        assert recorder.requests == []


# ── Telemetry and extension installs ────────────────────────────────────────


class TestTelemetryAndExtensions:
    def test_opentelemetry_export_is_refused_and_says_why(self):
        from lumi.audit import OtlpExporter

        recorder = _Recorder()
        exporter = OtlpExporter("https://collector.example.com:4318", transport=httpx.MockTransport(recorder),
                                interval=3600)
        try:
            _on()
            exporter.submit({"type": "turn.start", "seq": 1, "ts": "2026-09-27T00:00:00.000Z", "hash": "a" * 64})
            exporter.flush()
        finally:
            exporter.stop()
        assert recorder.requests == []
        assert exporter.dropped == 1
        assert exporter.status()["last_error"] == (
            "Offline mode: the OpenTelemetry export needs collector.example.com" + MESSAGE)

    def test_a_local_collector_is_reached(self):
        from lumi.audit import OtlpExporter

        recorder = _Recorder()
        exporter = OtlpExporter("http://127.0.0.1:4318", transport=httpx.MockTransport(recorder), interval=3600)
        try:
            _on()
            exporter.submit({"type": "turn.start", "seq": 1, "ts": "2026-09-27T00:00:00.000Z", "hash": "a" * 64})
            exporter.flush()
        finally:
            exporter.stop()
        assert len(recorder.requests) == 1 and exporter.sent == 1

    def test_pack_installs_from_git_are_refused_before_git_runs(self, tmp_path, monkeypatch):
        from lumi.engine import pack_install

        monkeypatch.setattr(pack_install.subprocess, "run",
                            lambda *a, **k: pytest.fail("git must not run for a refused address"))
        _on()
        with pytest.raises(pack_install.PackInstallError) as caught:
            pack_install.resolve("https://github.com/acme/lumi-pack", "v1.0")
        assert str(caught.value) == "Offline mode: installing a capability pack from Git needs github.com" + MESSAGE
        monkeypatch.setattr(pack_install, "_git", _refuse_all_but_network(pack_install))
        with pytest.raises(pack_install.PackInstallError, match="needs github.com"):
            pack_install.install_from_git("https://github.com/acme/lumi-pack", "a" * 40, dest_root=tmp_path)

    def test_an_allowed_git_server_is_used(self, monkeypatch):
        from lumi.engine import pack_install

        ran = []

        def fake_run(args, **kwargs):
            ran.append(args)
            if "--get-url" in args:  # the address after Git's own rewriting: unchanged here
                return SimpleNamespace(returncode=0, stdout="https://git.corp.example/acme/pack\n", stderr="")
            return SimpleNamespace(returncode=0, stdout=f"{'b' * 40}\trefs/tags/v1.0\n", stderr="")

        monkeypatch.setattr(pack_install.shutil, "which", lambda name: "git")
        monkeypatch.setattr(pack_install.subprocess, "run", fake_run)
        _on("git.corp.example")
        assert pack_install.resolve("https://git.corp.example/acme/pack", "v1.0") == "b" * 40
        # Git doesn't follow a redirect to a host offline mode didn't check.
        assert len(ran) == 2 and all("http.followRedirects=false" in args for args in ran)

    def test_git_is_checked_at_the_address_it_rewrites_to(self, tmp_path, monkeypatch):
        from lumi.engine import pack_install

        if not shutil.which("git"):
            pytest.skip("git isn't installed")
        # The person's Git settings send git.corp.example's addresses somewhere else.
        config = tmp_path / "gitconfig"
        config.write_text('[url "https://evil.example/"]\n\tinsteadOf = https://git.corp.example/\n', encoding="utf-8")
        monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
        real_run = subprocess.run
        ran = []

        def only_rewriting(args, **kwargs):
            ran.append(args)
            if "--get-url" not in args:  # reading the rewritten address never connects; anything else would
                pytest.fail("Git must not connect")
            return real_run(args, **kwargs)

        monkeypatch.setattr(pack_install.subprocess, "run", only_rewriting)
        _on("git.corp.example")
        with pytest.raises(pack_install.PackInstallError) as caught:
            pack_install.resolve("https://git.corp.example/acme/pack", "v1.0")
        assert str(caught.value) == "Offline mode: installing a capability pack from Git needs evil.example" + MESSAGE
        assert len(ran) == 1


def _refuse_all_but_network(pack_install):
    """_git that runs nothing: local commands succeed, the fetch meets offline mode's check."""
    real = pack_install._git

    def fake(args, *, cwd=None, local=False, remote=""):
        if remote:
            return real(args, cwd=cwd, local=local, remote=remote)
        return ""

    return fake


# ── Dictation and sign-in ───────────────────────────────────────────────────


class TestDictationAndSignIn:
    def test_the_browsers_recognizer_is_off_and_the_service_must_be_reachable(self, tmp_path):
        from lumi import voice
        from lumi.gui.settings import SettingsManager

        settings = SettingsManager(tmp_path / "settings.json")
        settings.set("api_keys", "openai", "sk-test")
        settings.update_section("voice", {"engine": "auto", "service": "openai"})
        assert voice.status(settings)["browser"] is True and voice.status(settings)["service_ready"] is True
        _on()
        state = voice.status(settings)
        # The webview sends the audio to its maker's service, which Lumi can't see or limit.
        assert state["browser"] is False and state["browser_reason"] == (
            "Offline mode: this window's speech recognition sends your voice to its maker's service, which offline "
            "mode can't check; choose a transcription service on this computer or an allowed host in Settings > "
            "Voice or turn offline mode off.")
        assert state["service_ready"] is False
        assert state["reason"] == "Offline mode: dictation with OpenAI needs api.openai.com" + MESSAGE
        recorder = _Recorder()
        with pytest.raises(voice.VoiceError, match="needs api.openai.com"):
            voice.transcribe(settings, b"audio", "audio/webm", transport=httpx.MockTransport(recorder))
        assert recorder.requests == []
        _on("api.openai.com")
        assert voice.status(settings)["service_ready"] is True and voice.status(settings)["browser"] is False

    def test_entra_sign_in_without_a_client_id_is_checked_before_azure_signs_in(self, monkeypatch):
        from lumi import auth_tokens, connections

        # azure-identity and the Azure CLI connect by themselves; neither may start.
        monkeypatch.setattr(auth_tokens.shutil, "which", lambda name: pytest.fail("the Azure CLI must not run"))
        monkeypatch.setattr(auth_tokens.subprocess, "run", lambda *a, **k: pytest.fail("nothing may run"))
        monkeypatch.setitem(sys.modules, "azure.identity", None)  # an import of it fails at once
        monkeypatch.delenv("AZURE_AUTHORITY_HOST", raising=False)
        _on("llm.corp.example")
        with pytest.raises(auth_tokens.SignInError) as caught:
            auth_tokens.entra_token("contoso.onmicrosoft.com")
        assert str(caught.value) == (
            "Offline mode: signing in to Microsoft Entra ID needs login.microsoftonline.com" + MESSAGE)
        # The model picker hides such a connection with the same reason, whatever its endpoint.
        connection = {"type": "azure-openai", "name": "Azure", "base_url": "https://llm.corp.example/openai",
                      "auth": "entra"}
        assert connections.offline_refusal(connection) == "Offline mode: Azure needs login.microsoftonline.com" + MESSAGE
        monkeypatch.setenv("AZURE_AUTHORITY_HOST", "login.microsoftonline.us")
        assert connections.offline_refusal(connection) == "Offline mode: Azure needs login.microsoftonline.us" + MESSAGE
        _on("llm.corp.example", "login.microsoftonline.us")
        assert connections.offline_refusal(connection) == ""


# ── Settings over the socket ────────────────────────────────────────────────


class TestSettingsCommands:
    def test_values_are_validated(self):
        from lumi.gui.ws_commands import _socket_setting_value

        assert _socket_setting_value("offline", "enabled", True) is True
        assert _socket_setting_value("offline", "allowed_hosts", ["LLM.corp.example", "", "10.0.0.0/8"]) == [
            "llm.corp.example", "10.0.0.0/8"]
        with pytest.raises(ValueError, match="true or false"):
            _socket_setting_value("offline", "enabled", "yes")
        with pytest.raises(ValueError, match="turns offline mode off"):
            _socket_setting_value("offline", "allowed_hosts", ["*"])

    def test_a_policy_lock_refuses_the_change(self):
        import asyncio

        from lumi import policy
        from lumi.gui import ws_commands

        policy.set_for_tests(policy.parse({"schema": policy.SCHEMA, "organization": "Acme",
                                           "settings": {"offline.enabled": True}}, source="test"))
        sent = []

        class WS:
            async def send_json(self, payload):
                sent.append(payload)

        state = SimpleNamespace(settings=SimpleNamespace(get=lambda *a, **k: None))
        ctx = ws_commands.CommandContext(ws=WS(), state=state, runs=SimpleNamespace(busy=False),
                                         msg={"command": "update_settings", "section": "offline", "key": "enabled",
                                              "value": False})
        asyncio.run(ws_commands.HANDLERS["update_settings"](ctx))
        assert sent == [{"event": "error", "source": "settings",
                         "message": "offline.enabled is managed by Acme and can't be changed here."}]


def test_connection_checks_say_why_before_starting_anything(monkeypatch):
    import asyncio

    from lumi import codex_account
    from lumi.gui import ws_commands

    monkeypatch.setattr(codex_account.codex_account, "status",
                        lambda: pytest.fail("Codex's program must not start in offline mode"))
    sent = []

    class WS:
        async def send_json(self, payload):
            sent.append(payload)

    state = SimpleNamespace(settings=SimpleNamespace(get_all=lambda: {}),
                            _api_key_details=lambda *a: pytest.fail("no provider is asked"))
    _on()
    for provider in ("codex", "openai"):
        ctx = ws_commands.CommandContext(ws=WS(), state=state, runs=SimpleNamespace(busy=False),
                                         msg={"command": "provider_connection", "provider": provider,
                                              "action": "status"})
        asyncio.run(ws_commands.HANDLERS["provider_connection"](ctx))
    assert sent[0]["data"]["error"].startswith("Offline mode: Codex needs chatgpt.com and runs as its own program")
    assert sent[1]["data"]["error"] == "Offline mode: OpenAI needs api.openai.com" + MESSAGE


def test_panels_from_capability_packs_stay_closed_offline():
    # A panel's content security policy leaves it no network but WebRTC, which offline mode can't check.
    from lumi.gui import extension_panels

    settings = SimpleNamespace(get=lambda section, key=None, default=None: default)
    assert extension_panels.enabled(settings) == (True, "")
    _on("llm.corp.example")
    allowed, reason = extension_panels.enabled(settings)
    assert not allowed and reason == ("Offline mode: panels from capability packs can connect by WebRTC, which "
                                      "offline mode can't check, so they stay closed while it's on.")
    with pytest.raises(extension_panels.PanelError, match="WebRTC"):
        extension_panels.open_panel(SimpleNamespace(settings=settings), "acme.board", "board", owner=1)


def test_team_preview_refuses_new_work_offline():
    from lumi.engine.swarming.service import policy_refusal

    assert policy_refusal("start") == ""
    _on()
    assert policy_refusal("start").startswith("Offline mode is on, and the Team preview doesn't follow it yet")
