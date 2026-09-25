"""Organization policy applied: locked settings, modes, models, rules,
exclusions, MCP servers, capability packs and refused turns."""

from __future__ import annotations

import asyncio
import importlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from lumi import policy as lumi_policy
from lumi.gui.settings import SettingsManager
from lumi.policy import parse

POLICY = {
    "schema": "lumi.policy/v1",
    "organization": "Acme",
    "settings": {"privacy.secret_scan": True, "security.cli_adapters": False,
                 "general.default_permission_mode": "ask"},
    "permissions": {"allowed_modes": ["ask", "auto-edit"]},
    "models": {"allowed": ["ollama:*", "anthropic:*"], "blocked": ["ollama:blocked-*"]},
    "files": {"exclude": ["*.pem"]},
    "shell": {"rules": [{"tool_pattern": "bash", "action": "deny", "arg_patterns": {"command": "curl"},
                         "reason": "no downloads"}]},
    "mcp": {"allowed_servers": ["docs"], "allow_stdio": False},
    "extensions": {"allowed_packs": ["team-*"]},
}


@pytest.fixture
def acme():
    policy = parse(json.loads(json.dumps(POLICY)), source="test policy")
    lumi_policy.set_for_tests(policy)
    return policy


class _StubWS:
    def __init__(self):
        self.sent = []

    async def send_json(self, payload):
        self.sent.append(payload)


def _command(settings, command, **msg):
    from lumi.gui import ws_commands

    def update_setting_value(section, key, value, *, clear_secret=False):
        settings.set(section, key, value)
        return settings.get_masked()

    applied = []
    state = SimpleNamespace(settings=settings, backend_spec=None, update_setting_value=update_setting_value,
                            apply_permission_mode=applied.append,
                            get_init_data=lambda refresh_only=False: {"event": "init"})
    ctx = ws_commands.CommandContext(ws=_StubWS(), state=state, msg={"command": command, **msg},
                                     runs=SimpleNamespace(busy=False))
    asyncio.run(ws_commands.HANDLERS[command](ctx))
    return ctx.ws.sent, applied


class TestLockedSettings:
    def test_policy_values_win_and_are_reported(self, tmp_path, acme):
        settings = SettingsManager(tmp_path / "settings.json")
        settings.set("privacy", "secret_scan", False)
        assert settings.get("privacy", "secret_scan") is True
        assert settings.get("privacy")["secret_scan"] is True
        assert settings.get_all()["security"]["cli_adapters"] is False
        meta = settings.get_masked()["_meta"]
        assert meta["locked"]["privacy.secret_scan"] == "Acme"
        assert meta["policy"]["active"] and meta["policy"]["summary"]["organization"] == "Acme"
        # The user's own value comes back once the policy stops locking it.
        lumi_policy.set_for_tests(None)
        assert settings.get("privacy", "secret_scan") is False

    def test_socket_refuses_locked_keys(self, tmp_path, acme):
        settings = SettingsManager(tmp_path / "settings.json")
        sent, _ = _command(settings, "update_settings", section="security", key="cli_adapters", value=True)
        assert "managed by Acme" in sent[0]["message"]
        assert settings.get("security", "cli_adapters") is False

    def test_socket_refuses_disallowed_modes(self, tmp_path, acme):
        settings = SettingsManager(tmp_path / "settings.json")
        sent, applied = _command(settings, "set_permission_mode", mode="bypass")
        assert "doesn't allow" in sent[0]["message"] and applied == []
        _, applied = _command(settings, "set_permission_mode", mode="auto-edit")
        assert applied == ["auto-edit"]


def _load_app(monkeypatch, cwd: Path):
    monkeypatch.setattr(Path, "home", lambda: cwd)
    monkeypatch.chdir(cwd)
    import lumi.gui.app as app_module

    return importlib.reload(app_module)


class TestAppEnforcement:
    def test_modes_models_rules_and_exclusions(self, monkeypatch, tmp_path, acme):
        app = _load_app(monkeypatch, tmp_path)
        monkeypatch.setattr(app.AppState, "detect_backends", lambda self, force=False: {})
        state = app.AppState()

        assert state.permission_mode == "ask"
        assert state.apply_permission_mode("bypass") == "ask" or state.permission_mode == "ask"

        with pytest.raises(ValueError, match="doesn't allow blocked-7b"):
            state.build_backend_spec("ollama", "blocked-7b")
        state.available_backends = {"ollama": {"models": ["llama3", "blocked-7b"]},
                                    "openrouter": {"models": ["x/y"]}}
        init = state.get_init_data()
        assert init["backends"]["ollama"]["models"] == ["llama3"]
        assert init["backends"]["openrouter"]["models"] == []

        rules = app.AppState._execution_policy_for("bypass", str(tmp_path))
        assert rules.evaluate("bash", {"command": "curl example.com"}).value == "deny"
        assert rules.get_reason("bash", {"command": "curl example.com"}) == "no downloads"

        exclusions = state.exclusions_for(str(tmp_path))
        match = exclusions.match(str(tmp_path / "server.pem"))
        assert match and match.source == "organization policy"


class TestIntegrations:
    def test_mcp_servers_outside_the_allowlist_are_refused(self, acme):
        from lumi.engine.mcp import MCPManager, MCPServerConfig

        manager = MCPManager()
        assert manager.connect("other", MCPServerConfig(name="other", transport="http", url="http://127.0.0.1:9/mcp")) is False
        assert "Acme" in manager._errors["other"]
        assert manager.connect("docs", MCPServerConfig(name="docs", command="docs-server")) is False
        assert "Acme" in manager._errors["docs"]  # stdio servers are off

    def test_packs_outside_the_allowlist_are_blocked(self, tmp_path, acme):
        from lumi.engine.capability_packs import CapabilityPackManager

        for pack_id in ("team-tools", "outsider"):
            folder = tmp_path / "project" / ".lumi" / "packs" / pack_id
            folder.mkdir(parents=True)
            (folder / "lumi-pack.json").write_text(json.dumps({
                "schema": "lumi.extension/v1", "id": pack_id, "name": pack_id, "version": "1.0.0",
            }), encoding="utf-8")
        packs = {p.id: p for p in CapabilityPackManager(str(tmp_path / "project")).discover()}
        assert "Acme" in (packs["outsider"].problem or "")
        assert "Acme" not in (packs["team-tools"].problem or "")


class _Backend:
    name = "ollama"
    tool_mode = "native"

    def __init__(self, model):
        self.model = model
        self.calls = 0

    def stream(self, **kwargs):
        from lumi.backends import EVENT_DONE, EVENT_TEXT_DELTA

        self.calls += 1
        yield EVENT_TEXT_DELTA, {"delta": "ok"}
        yield EVENT_DONE, {"cognitive_state": None, "stats": None, "model": self.model}


class TestRefusedTurns:
    def test_blocked_model_and_expired_policy_stop_the_turn(self, acme):
        from lumi.engine.session import Session

        blocked = _Backend("blocked-7b")
        events = list(Session(blocked, auto_approve=True).run("hi"))
        assert blocked.calls == 0
        assert any("doesn't allow blocked-7b" in e.get("message", "") for e in events if e.get("event") == "error")

        allowed = _Backend("llama3")
        list(Session(allowed, auto_approve=True).run("hi"))
        assert allowed.calls == 1

        lumi_policy.set_for_tests(parse({**POLICY, "expires_at": "2020-01-01T00:00:00Z", "grace_days": 0},
                                        source="test policy"))
        late = _Backend("llama3")
        events = list(Session(late, auto_approve=True).run("hi"))
        assert late.calls == 0 and any("expired" in e.get("message", "") for e in events)

    def test_invalid_policy_stops_the_turn(self):
        from lumi.engine.session import Session

        lumi_policy.set_for_tests(None, error="The organization policy at X is invalid: bad JSON")
        backend = _Backend("llama3")
        events = list(Session(backend, auto_approve=True).run("hi"))
        assert backend.calls == 0 and any("administrator" in e.get("message", "") for e in events)
