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
        for entry in ("localhost", "127.0.0.1", "[::1]", "docs.corp.example", "10.20.0.0/16", "[fd00::5]"):
            assert entry in bypass
        assert "--force-webrtc-ip-handling-policy=disable_non_proxied_udp" in args

        # Offline mode turned off while it runs: Lumi's own Chrome starts again without the limits.
        manager._conn = SimpleNamespace(close=lambda: None)
        offline.reset_for_tests()
        assert manager.ensure_started() == "attached"
        assert len(launched) == 2 and not any(arg.startswith("--proxy-server") for arg in launched[1])

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

    @pytest.mark.parametrize("url, host", [
        ("https://github.com/acme/app.git", "github.com"), ("git@github.com:acme/app.git", "github.com"),
        ("ssh://git@git.corp.example:2222/acme/app.git", "git.corp.example"),
        ("https://user@dev.azure.com/org/p/_git/r", "dev.azure.com"), ("C:\\repos\\app", ""),
        ("/srv/git/app.git", ""), ("../app", ""), ("file:///srv/app.git", ""),
    ])
    def test_remote_hosts(self, url, host):
        from lumi.engine.github_tools import remote_host

        assert remote_host(url) == host

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
            return SimpleNamespace(returncode=0, stdout=f"{'b' * 40}\trefs/tags/v1.0\n", stderr="")

        monkeypatch.setattr(pack_install.shutil, "which", lambda name: "git")
        monkeypatch.setattr(pack_install.subprocess, "run", fake_run)
        _on("git.corp.example")
        assert pack_install.resolve("https://git.corp.example/acme/pack", "v1.0") == "b" * 40
        assert ran


def _refuse_all_but_network(pack_install):
    """_git that runs nothing: local commands succeed, the fetch meets offline mode's check."""
    real = pack_install._git

    def fake(args, *, cwd=None, local=False, remote=""):
        if remote:
            return real(args, cwd=cwd, local=local, remote=remote)
        return ""

    return fake


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


def test_team_preview_refuses_new_work_offline():
    from lumi.engine.swarming.service import policy_refusal

    assert policy_refusal("start") == ""
    _on()
    assert policy_refusal("start").startswith("Offline mode is on, and the Team preview doesn't follow it yet")
