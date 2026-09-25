"""Update mode, channel and pin: settings, policy, the WinSparkle wrapper and Settings commands."""
from __future__ import annotations

import json
import sys
from types import SimpleNamespace

import pytest

from lumi import policy, update_channels, updater
from lumi.update_channels import UpdatePreferences, parse_pin, read


class FakeWinSparkle:
    """Records the calls lumi.updater makes; nothing real runs."""

    def __init__(self, last_check=-1):
        self.calls = []
        self.last_check = last_check

    def __getattr__(self, name):
        if not name.startswith("win_sparkle_"):
            raise AttributeError(name)

        def call(*args):
            self.calls.append((name, args))
            if name == "win_sparkle_set_eddsa_public_key":
                return 1
            if name == "win_sparkle_get_last_check_time":
                return self.last_check
            return None

        return call

    def called(self, name):
        return [args for called, args in self.calls if called == name]


def write_settings(path, **updates):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"updates": updates}), encoding="utf-8")


def org_policy(**settings):
    return policy.parse({"schema": policy.SCHEMA, "organization": "Example Corp",
                         "settings": {f"updates.{k}": v for k, v in settings.items()}}, source="test")


class TestPreferences:
    @pytest.mark.parametrize(("value", "pin"), [("0.20", "0.20"), ("v1.2", "1.2"), (" 0.21 ", "0.21"), ("", ""), (None, "")])
    def test_a_pin_is_a_release_line(self, value, pin):
        assert parse_pin(value) == pin

    @pytest.mark.parametrize("value", ["0.20.1", "latest", "1", "01.2", "0.x"])
    def test_other_pins_are_refused(self, value):
        with pytest.raises(ValueError, match="release line"):
            parse_pin(value)

    def test_each_choice_has_its_own_feed(self):
        base = update_channels.FEED_BASE
        assert UpdatePreferences().feed_url == base + "appcast.xml"
        assert UpdatePreferences(channel="beta").feed_url == base + "appcast-beta.xml"
        # A pin wins over the channel.
        assert UpdatePreferences(channel="beta", pin="0.20").feed_url == base + "appcast-0.20.xml"

    def test_settings_json_and_its_mistakes(self, tmp_path):
        path = tmp_path / "settings.json"
        assert read(path, SimpleNamespace(policy=None, error="")) == UpdatePreferences()
        write_settings(path, mode="manual", channel="beta", pin="0.21")
        prefs = read(path, SimpleNamespace(policy=None, error=""))
        assert (prefs.mode, prefs.channel, prefs.pin, prefs.managed_by) == ("manual", "beta", "0.21", "")
        write_settings(path, mode="sometimes", channel="nightly", pin="0.21")
        prefs = read(path, SimpleNamespace(policy=None, error=""))
        assert (prefs.mode, prefs.channel, prefs.pin) == ("automatic", "stable", "0.21")
        assert len(prefs.problems) == 2

    def test_the_policy_wins_and_says_who(self, tmp_path):
        path = tmp_path / "settings.json"
        write_settings(path, mode="automatic", channel="beta")
        prefs = read(path, SimpleNamespace(policy=org_policy(mode="off", pin="0.20"), error=""))
        assert (prefs.mode, prefs.channel, prefs.pin) == ("off", "beta", "0.20")
        assert prefs.managed_by == "Example Corp" and prefs.locked == ("mode", "pin")

    def test_an_invalid_policy_pauses_automatic_updates(self, tmp_path):
        prefs = read(tmp_path / "missing.json", SimpleNamespace(policy=None, error="bad policy"))
        assert prefs.mode == "manual" and "paused" in prefs.problems[0]

    def test_a_policy_with_values_lumi_cant_apply_is_refused(self):
        with pytest.raises(policy.PolicyError, match="updates.mode"):
            org_policy(mode="never")
        with pytest.raises(policy.PolicyError, match="release line"):
            org_policy(pin="latest")
        with pytest.raises(policy.PolicyError, match="updates.window"):
            org_policy(window="night")


class TestWinSparkleWrapper:
    def start(self, monkeypatch, prefs, dll=None):
        fake = dll or FakeWinSparkle()
        loads = []
        monkeypatch.setattr(updater, "_load_dll", lambda: loads.append(1) or fake)
        return updater.init_updater(prefs), fake, loads

    def test_off_never_loads_winsparkle(self, monkeypatch):
        started, fake, loads = self.start(monkeypatch, UpdatePreferences(mode="off", managed_by="Example Corp"))
        assert not started and loads == [] and fake.calls == []
        assert updater.check_for_updates_now() is False

    def test_the_feed_and_automatic_checks_follow_the_settings(self, monkeypatch):
        started, fake, _ = self.start(monkeypatch, UpdatePreferences(mode="manual", channel="beta"))
        assert started
        assert fake.called("win_sparkle_set_appcast_url") == [(update_channels.FEED_BASE.encode() + b"appcast-beta.xml",)]
        assert fake.called("win_sparkle_set_automatic_check_for_updates") == [(0,)]
        assert fake.called("win_sparkle_init") == [()]
        updater.reset_for_tests()
        _, fake, _ = self.start(monkeypatch, UpdatePreferences(pin="0.20"))
        assert fake.called("win_sparkle_set_appcast_url") == [(update_channels.FEED_BASE.encode() + b"appcast-0.20.xml",)]
        assert fake.called("win_sparkle_set_automatic_check_for_updates") == [(1,)]

    def test_status_reports_the_last_check_and_settings_waiting_for_a_restart(self, monkeypatch, tmp_path):
        self.start(monkeypatch, UpdatePreferences(), FakeWinSparkle(last_check=1_790_000_000))
        saved = UpdatePreferences(channel="beta")
        monkeypatch.setattr(updater, "read_update_preferences", lambda: saved)
        info = updater.status()
        assert info["available"] and info["last_check"] == 1_790_000_000
        assert info["mode"] == "automatic" and info["describe"] == "the stable channel"
        assert info["pending"]["channel"] == "beta"
        # A change that doesn't alter behavior isn't pending.
        updater.reset_for_tests()
        self.start(monkeypatch, UpdatePreferences(mode="off"))
        monkeypatch.setattr(updater, "read_update_preferences", lambda: UpdatePreferences(mode="off", pin="0.20"))
        assert updater.status()["pending"] is None
        updater.reset_for_tests()
        self.start(monkeypatch, UpdatePreferences(pin="0.20"))
        monkeypatch.setattr(updater, "read_update_preferences", lambda: UpdatePreferences(pin="0.20", channel="beta"))
        assert updater.status()["pending"] is None

    def test_running_from_source_never_loads_winsparkle(self, monkeypatch):
        monkeypatch.setattr(updater.sys, "platform", "win32")
        monkeypatch.delattr(updater.sys, "frozen", raising=False)
        monkeypatch.delenv("LUMI_UPDATER_FROM_SOURCE", raising=False)
        monkeypatch.setattr(updater, "_find_dll", lambda: pytest.fail("looked for WinSparkle.dll"))
        assert updater._load_dll() is None
        monkeypatch.setenv("LUMI_UPDATER_FROM_SOURCE", "1")
        monkeypatch.setattr(updater, "_find_dll", lambda: None)
        assert updater._load_dll() is None  # looked, found nothing


class TestSettingsCommands:
    def test_the_values_settings_can_store(self):
        from lumi.gui.ws_commands import _socket_setting_value

        assert _socket_setting_value("updates", "pin", "v0.21") == "0.21"
        assert _socket_setting_value("updates", "channel", "beta") == "beta"
        for key, value in (("mode", "never"), ("channel", "nightly"), ("pin", "0.21.3"), ("window", "x")):
            with pytest.raises(ValueError):
                _socket_setting_value("updates", key, value)

    def test_a_change_reports_what_applies_after_a_restart(self, tmp_path, monkeypatch):
        from lumi.gui.settings import SettingsManager
        from tests.test_connections import _command

        monkeypatch.setattr(updater, "_load_dll", lambda: FakeWinSparkle())
        settings = SettingsManager(tmp_path / "settings.json")
        assert settings.get("updates") == {"mode": "automatic", "channel": "stable", "pin": ""}
        updater.init_updater(UpdatePreferences())
        monkeypatch.setattr(updater, "read_update_preferences",
                            lambda: read(tmp_path / "settings.json", SimpleNamespace(policy=None, error="")))
        sent = _command(settings, "update_settings", section="updates", key="channel", value="beta")
        status = next(event["data"] for event in sent if event["event"] == "update_status")
        assert status["channel"] == "stable" and status["pending"]["channel"] == "beta"
        refused = _command(settings, "update_settings", section="updates", key="pin", value="latest")
        assert refused[0] == {"event": "error", "source": "settings",
                              "message": "A version pin is a release line such as 0.20, or empty for none."}

    def test_a_locked_update_setting_is_refused(self, tmp_path):
        from lumi.gui.settings import SettingsManager
        from tests.test_connections import _command

        policy.set_for_tests(org_policy(mode="off"))
        settings = SettingsManager(tmp_path / "settings.json")
        sent = _command(settings, "update_settings", section="updates", key="mode", value="automatic")
        assert "managed by Example Corp" in sent[0]["message"]
        assert settings.get("updates", "mode") == "off"

    @pytest.mark.parametrize(("info", "started", "message"), [
        ({"mode": "off", "managed_by": "Example Corp", "locked": ["mode"]}, False, "turned off by Example Corp"),
        ({"mode": "off", "managed_by": "", "locked": []}, False, "turned off in Settings > Updates"),
        ({"mode": "off", "pending": {"mode": "automatic"}}, False, "until Lumi restarts"),
        ({"mode": "automatic"}, False, "doesn't update itself"),
        ({"mode": "manual", "describe": "the beta channel"}, True, "Checking the beta channel."),
        ({"mode": "automatic", "describe": "the stable channel", "pending": {"mode": "manual"}}, True,
         "apply after Lumi restarts"),
    ])
    def test_check_for_updates_says_what_happened(self, info, started, message):
        from lumi.gui.ws_commands import _update_check_message

        assert message in _update_check_message(info, started)


class TestInstallingAnUpdate:
    """The handshake with WinSparkle before its installer runs, and what goes to the audit log."""

    @pytest.fixture
    def records(self, tmp_path):
        from lumi import audit
        from lumi.audit import AuditLog

        log = AuditLog(tmp_path / "audit")
        audit.set_for_tests(log)
        return lambda: [json.loads(line) for path in log._files()
                        for line in path.read_text(encoding="utf-8").splitlines()]

    def start(self, monkeypatch):
        fake = FakeWinSparkle()
        monkeypatch.setattr(updater, "_load_dll", lambda: fake)
        assert updater.init_updater(UpdatePreferences(channel="beta"))
        return fake

    def callback(self, fake, name):
        [(thunk,)] = fake.called(f"win_sparkle_set_{name}_callback")
        return thunk

    def test_callbacks_are_set_before_winsparkle_starts(self, monkeypatch):
        fake = self.start(monkeypatch)
        names = [name for name, _ in fake.calls]
        for name in ("can_shutdown", "shutdown_request", "error", "did_find_update", "did_not_find_update",
                     "update_cancelled", "update_skipped", "update_postponed"):
            assert names.index(f"win_sparkle_set_{name}_callback") < names.index("win_sparkle_init")

    def test_an_update_waits_for_the_running_turn(self, monkeypatch, records):
        fake = self.start(monkeypatch)
        can_shutdown = self.callback(fake, "can_shutdown")
        busy = [True]
        closed = []
        updater.set_host(busy=lambda: busy[0], shutdown=lambda: closed.append(1))
        assert can_shutdown() == 0
        busy[0] = False
        assert can_shutdown() == 1
        self.callback(fake, "shutdown_request")()
        assert closed == [1]
        kinds = [(r["type"], r["data"].get("reason")) for r in records()]
        assert kinds == [("update.deferred", "an agent turn is running"), ("update.install", None)]
        assert records()[-1]["data"]["feed"].endswith("/appcast-beta.xml")

    def test_when_unsure_the_turn_is_kept(self, monkeypatch, records):
        fake = self.start(monkeypatch)

        def broken():
            raise RuntimeError("state unavailable")

        updater.set_host(busy=broken)
        assert self.callback(fake, "can_shutdown")() == 0
        # With no app to close (the terminal UI), the installer asks to close Lumi itself.
        updater.set_host()
        self.callback(fake, "shutdown_request")()

    def test_what_the_updater_finds_is_recorded(self, monkeypatch, records):
        fake = self.start(monkeypatch)
        for name in ("did_find_update", "did_not_find_update", "error", "update_skipped",
                     "update_postponed", "update_cancelled"):
            self.callback(fake, name)()
        assert [(r["type"], r["data"].get("result")) for r in records()] == [
            ("update.check", "found"), ("update.check", "none"), ("update.check", "error"),
            ("update.skipped", None), ("update.postponed", None), ("update.cancelled", None)]


@pytest.mark.skipif(sys.platform != "win32", reason="WinSparkle.dll is Windows-only")
def test_the_vendored_winsparkle_exports_every_function_lumi_uses(monkeypatch):
    # Loading the DLL and declaring signatures calls nothing, so no check
    # runs and nothing is written to the registry.
    monkeypatch.setenv("LUMI_UPDATER_FROM_SOURCE", "1")
    assert updater._load_dll() is not None
