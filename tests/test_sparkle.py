"""macOS updates through Sparkle 2 (lumi/sparkle.py, lumi/updater.py).

Three layers:

* ``SparkleUpdater`` and the updater's macOS paths with a fake bridge: the
  feed, the mode, offline mode (checks and each download), the wait for a
  running turn, audit records and the main-thread hand-off; and how
  ``__main__`` tells a LaunchServices launch from a command line. Nothing
  here needs a Mac.
* On macOS, the PyObjC delegate class: the Objective-C types it registers
  for Sparkle's BOOL results, NSInteger enums, NSError** and blocks.
* With ``LUMI_TEST_SPARKLE_FRAMEWORK`` pointing at the pinned framework
  (CI fetches it with packaging/fetch_sparkle.sh), the real Sparkle: it reads
  the feed through the delegate, finds the macOS item in a feed that
  packaging/update_appcast.py wrote, skips a Windows one, stops asking after
  offline mode, reads only a feed signed with the app's key
  (packaging/feed_signature.py), offers nothing older than what runs, sends
  no download request Lumi refused, and orders Lumi's versions as the feeds
  expect.
"""
from __future__ import annotations

import base64
import http.server
import importlib.util
import json
import os
import sys
import threading
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from lumi import sparkle, update_channels, updater
from lumi.update_channels import UpdatePreferences

ROOT = Path(__file__).resolve().parents[1]
MAC_FEED = update_channels.FEED_BASE + "appcast-macos.xml"


class FakeBridge:
    """Stands in for PyObjC and Sparkle: records calls, runs 'main thread' work when told."""

    def __init__(self, *, main_thread=True, fail=None, last_check=None):
        self.main_thread = main_thread
        self.fail = fail
        self.last_check = last_check
        self.calls = []
        self.dispatched = []  # handed to the main thread
        self.timers = []  # (seconds, function)
        self.turns = 0
        self.owner = None

    def on_main_thread(self):
        return self.main_thread

    def start(self, owner, *, automatic):
        self.owner = owner
        self.calls.append(("start", automatic))
        if self.fail:
            raise sparkle.SparkleError(self.fail)
        # Sparkle asks its delegate for the feed as it starts.
        return {"version": "2.10.0", "feed": owner.feed_url(), "automatic": automatic,
                "last_check": self.last_check}

    def check_for_updates(self):
        self.calls.append(("check", "user"))

    def check_in_background(self):
        self.calls.append(("check", "background"))

    def on_main(self, function):
        self.dispatched.append(function)

    def after(self, seconds, function):
        self.timers.append((seconds, function))

    def run_loop_once(self, seconds):
        self.turns += 1
        self.run_main()

    def run_main(self):
        while self.dispatched:
            self.dispatched.pop(0)()

    def fire_timers(self):
        timers, self.timers = self.timers, []
        for _seconds, function in timers:
            function()


@pytest.fixture
def records(tmp_path):
    from lumi import audit
    from lumi.audit import AuditLog

    log = AuditLog(tmp_path / "audit")
    audit.set_for_tests(log)
    return lambda: [json.loads(line) for path in log._files()
                    for line in path.read_text(encoding="utf-8").splitlines()]


@pytest.fixture
def mac(monkeypatch, tmp_path):
    """This test process as the packaged Lumi.app on macOS, with a fake Sparkle."""
    monkeypatch.setattr(update_channels, "platform_name", lambda: "macos")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    framework = tmp_path / "Lumi.app" / "Contents" / "Frameworks" / "Sparkle.framework"
    monkeypatch.setattr(sparkle, "framework_path", lambda executable=None: framework)
    bridge = FakeBridge()
    monkeypatch.setattr(sparkle, "Bridge", lambda framework, host_bundle=None: bridge)
    return bridge


def start(prefs):
    assert updater.init_updater(prefs)
    return updater._sparkle


@pytest.fixture(autouse=True)
def _saved_settings_match_startup(monkeypatch):
    # status() compares startup settings with what's saved; keep them equal
    # unless a test says otherwise, and never read this machine's policy.
    monkeypatch.setattr(updater, "read_update_preferences", lambda: updater._preferences or UpdatePreferences())


class TestTheMacOSUpdater:
    def test_sparkle_starts_with_the_feed_and_mode_in_effect(self, mac):
        start(UpdatePreferences(mode="manual", channel="beta"))
        assert mac.calls == [("start", False)]
        info = updater.status()
        assert info["available"] and info["engine"] == "sparkle" and info["platform"] == "macos"
        assert info["sparkle"] == {"version": "2.10.0", "feed": update_channels.FEED_BASE + "appcast-macos-beta.xml",
                                   "automatic": False, "stopped": False}
        assert info["feed"] == info["sparkle"]["feed"] and info["unavailable"] == ""

    def test_a_pre_release_starts_on_the_macos_beta_feed_until_someone_chooses(self, mac, tmp_path):
        from lumi.gui.settings import SettingsManager

        path = tmp_path / "settings.json"
        SettingsManager(path)  # a new install: no channel chosen
        nobody = SimpleNamespace(policy=None, error="")
        engine = start(update_channels.read(path, nobody, installer="", version="0.20.0-alpha.1"))
        assert engine.feed_url() == update_channels.FEED_BASE + "appcast-macos-beta.xml"
        updater.reset_for_tests()
        # A saved choice stays.
        path.write_text(json.dumps({"updates": {"channel": "stable"}}), encoding="utf-8")
        engine = start(update_channels.read(path, nobody, installer="", version="0.20.0-alpha.1"))
        assert engine.feed_url() == update_channels.FEED_BASE + "appcast-macos.xml"

    def test_a_pin_picks_its_macos_line_and_automatic_checks_are_on(self, mac):
        engine = start(UpdatePreferences(pin="0.20"))
        assert mac.calls == [("start", True)]
        assert engine.feed_url() == update_channels.FEED_BASE + "appcast-macos-0.20.xml"

    def test_off_and_offline_never_load_sparkle(self, mac):
        assert not updater.init_updater(UpdatePreferences(mode="off", managed_by="Example Corp"))
        updater.reset_for_tests()
        assert not updater.init_updater(UpdatePreferences(offline="Offline mode: the update check needs x."))
        assert mac.calls == [] and updater.check_for_updates_now() is False

    def test_sparkle_starts_only_on_the_main_thread(self, mac, monkeypatch):
        mac.main_thread = False
        assert not updater.init_updater(UpdatePreferences())
        assert mac.calls == []
        monkeypatch.setattr(updater, "read_update_preferences", lambda: UpdatePreferences())
        info = updater.status()
        assert not info["available"] and "didn't start" in info["unavailable"]

    def test_a_failed_start_leaves_lumi_running_without_updates(self, mac, monkeypatch):
        mac.fail = "Sparkle didn't start: no feed"
        assert not updater.init_updater(UpdatePreferences())
        assert updater._sparkle is None and updater.check_for_updates_now() is False

    def test_checks_asked_for_elsewhere_run_on_the_main_thread(self, mac):
        start(UpdatePreferences())
        assert updater.check_for_updates_now(silent=False)
        assert updater.check_for_updates_now(silent=True)
        assert mac.calls == [("start", True)]  # nothing yet: Sparkle belongs to the main thread
        mac.run_main()
        assert mac.calls[1:] == [("check", "user"), ("check", "background")]

    def test_the_last_check_comes_from_sparkle(self, mac, monkeypatch):
        mac.last_check = 1_790_000_000.5
        engine = start(UpdatePreferences())
        monkeypatch.setattr(updater, "read_update_preferences", lambda: UpdatePreferences())
        assert updater.status()["last_check"] == 1_790_000_000
        engine.cycle_finished(1_790_086_400.0)
        assert updater.status()["last_check"] == 1_790_086_400

    def test_offline_mode_stops_sparkle_at_once(self, mac, monkeypatch, records):
        engine = start(UpdatePreferences())
        reason = "Offline mode: the update check needs luminary-analytics.github.io."
        monkeypatch.setattr(updater, "read_update_preferences", lambda: UpdatePreferences(offline=reason))
        assert updater.apply_offline_mode() == reason
        # Sparkle can't be stopped, so its delegate refuses every check and download from now on.
        assert engine.may_check(sparkle.CHECK_BACKGROUND) == reason
        assert engine.may_check(sparkle.CHECK_USER) == reason
        assert engine.may_proceed("0.21.0") == reason
        assert updater.check_for_updates_now() is False
        mac.run_main()
        assert mac.calls == [("start", True)]
        info = updater.status()
        assert not info["available"] and info["offline"] == reason and info["unavailable"] == ""
        # Lumi's own refusal isn't a failed check.
        engine.aborted(sparkle.ERROR_DOMAIN, 1)
        assert records() == []
        # Turning it off again takes a restart, as for WinSparkle.
        monkeypatch.setattr(updater, "read_update_preferences", lambda: UpdatePreferences())
        assert updater.status()["restart_to_check"]

    def test_only_the_app_bundle_loads_sparkle(self, monkeypatch):
        monkeypatch.setattr(update_channels, "platform_name", lambda: "macos")
        monkeypatch.setattr(sparkle, "framework_path", lambda executable=None: pytest.fail("looked for Sparkle"))
        monkeypatch.delattr(sys, "frozen", raising=False)
        monkeypatch.setenv("LUMI_UPDATER_FROM_SOURCE", "1")  # WinSparkle's switch; Sparkle ignores it
        assert updater._load_sparkle() is None
        monkeypatch.setattr(sys, "frozen", True, raising=False)
        monkeypatch.setattr(sparkle, "framework_path", lambda executable=None: None)
        assert updater._load_sparkle() is None  # looked, found nothing
        monkeypatch.setattr(update_channels, "platform_name", lambda: "windows")
        assert updater._load_sparkle() is None

    def test_browser_mode_turns_the_run_loop_until_the_server_stops(self, mac):
        start(UpdatePreferences())
        left = [3]

        def done():
            left[0] -= 1
            return left[0] < 0

        assert updater.run_main_loop(done)
        assert mac.turns == 3
        updater.reset_for_tests()
        assert updater.run_main_loop(lambda: True) is False  # nothing to turn: the caller joins the thread


class TestInstallingAnUpdate:
    """Sparkle asks once before it closes Lumi to install; the answer waits for the running turn."""

    def test_an_update_waits_for_the_running_turn(self, mac, records):
        engine = start(UpdatePreferences(channel="beta"))
        busy = [True]
        installs = []
        updater.set_host(busy=lambda: busy[0], shutdown=lambda: None)
        assert engine.postpone("0.21.0-beta.2", lambda: installs.append(1)) is True
        mac.fire_timers()
        assert installs == [] and len(mac.timers) == 1  # still running: asks again later
        busy[0] = False
        mac.fire_timers()
        assert installs == [1] and mac.timers == []
        [deferred] = records()
        assert deferred["type"] == "update.deferred"
        assert deferred["data"]["reason"] == "an agent turn is running"
        assert deferred["data"]["to_version"] == "0.21.0-beta.2"
        assert deferred["data"]["feed"].endswith("/appcast-macos-beta.xml")

    def test_without_a_turn_it_installs_at_once(self, mac, records):
        engine = start(UpdatePreferences())
        updater.set_host(busy=lambda: False)
        assert engine.postpone("0.21.0", lambda: pytest.fail("Sparkle carries on by itself")) is False
        assert records() == [] and mac.timers == []

    def test_when_unsure_the_turn_is_kept(self, mac):
        engine = start(UpdatePreferences())

        def broken():
            raise RuntimeError("state unavailable")

        updater.set_host(busy=broken)
        assert engine.postpone("0.21.0", lambda: None) is True

    def test_what_sparkle_reports_is_recorded(self, mac, records):
        engine = start(UpdatePreferences())
        engine.found("0.21.0")
        engine.not_found()
        engine.aborted(sparkle.SPARKLE_ERROR_DOMAIN, sparkle.SU_NO_UPDATE)  # already recorded as "none"
        engine.aborted(sparkle.SPARKLE_ERROR_DOMAIN, 2001)
        engine.aborted(sparkle.SPARKLE_ERROR_DOMAIN, sparkle.SU_INSTALLATION_CANCELED)
        engine.choice(sparkle.CHOICE_SKIP, "0.21.0")
        engine.choice(sparkle.CHOICE_DISMISS, "0.21.0")
        engine.choice(sparkle.CHOICE_INSTALL, "0.21.0")  # recorded when it installs
        engine.download_cancelled()
        engine.download_failed("0.21.0", sparkle.SPARKLE_ERROR_DOMAIN, sparkle.SU_DOWNLOAD_ERROR)
        engine.aborted(sparkle.SPARKLE_ERROR_DOMAIN, 2001)  # Sparkle's abort after that failure: once is enough
        engine.will_install("0.21.0")
        got = [(r["type"], r["data"].get("result"), r["data"].get("to_version")) for r in records()]
        assert got == [
            ("update.check", "found", "0.21.0"), ("update.check", "none", None), ("update.check", "error", None),
            ("update.cancelled", None, None), ("update.skipped", None, "0.21.0"),
            ("update.postponed", None, "0.21.0"), ("update.cancelled", None, None),
            ("update.check", "error", "0.21.0"), ("update.install", None, "0.21.0")]
        failed = records()[-2]["data"]
        assert (failed["stage"], failed["code"], failed["domain"]) == ("download", 2001, sparkle.SPARKLE_ERROR_DOMAIN)
        assert all(r["data"]["feed"] == MAC_FEED for r in records())

    def test_in_browser_mode_lumi_closes_itself_for_the_installer(self, mac, records):
        engine = start(UpdatePreferences())
        closed = []
        updater.set_host(busy=lambda: False, shutdown=lambda: closed.append(1))
        engine.will_install("0.21.0")  # the desktop window: Sparkle's own request to quit closes Lumi
        assert mac.timers == []

        def install_during_the_wait():
            if not mac.timers and not closed:
                engine.will_install("0.21.0")
            mac.fire_timers()
            return bool(closed)

        assert updater.run_main_loop(install_during_the_wait)
        assert closed == [1]


class TestWhatSparkleMayReach:
    """Offline mode after Sparkle's own question: every download request is checked as Sparkle sends it."""

    DMG = "https://luminary-analytics.github.io/resonant-client/downloads/v0.21.0/lumi-0.21.0.dmg"

    def test_after_stop_a_download_is_refused_and_recorded_once(self, mac, records):
        engine = start(UpdatePreferences())
        assert engine.download_refusal(self.DMG, "0.21.0") == ""  # nothing refuses it yet
        reason = "Offline mode: the update download needs luminary-analytics.github.io."
        engine.stop(reason)
        # An update window left open from before: Install Update starts a download.
        assert engine.download_refusal(self.DMG, "0.21.0") == reason
        # Sparkle's downloader then fails it at once, and aborts; neither is a failed check.
        engine.download_failed("0.21.0", sparkle.SPARKLE_ERROR_DOMAIN, sparkle.SU_DOWNLOAD_ERROR)
        engine.aborted(sparkle.SPARKLE_ERROR_DOMAIN, sparkle.SU_DOWNLOAD_ERROR)
        [refused] = records()
        assert refused["type"] == "update.refused"
        assert {key: refused["data"][key] for key in ("stage", "to_version", "reason", "feed")} == {
            "stage": "download", "to_version": "0.21.0", "reason": reason, "feed": MAC_FEED}
        assert engine.release_notes_refusal("https://example.com/notes.html") == reason

    def test_offline_mode_refuses_the_hosts_it_doesnt_allow(self, mac, records):
        from lumi import offline

        engine = start(UpdatePreferences())
        offline.set_for_tests(enabled=True, allowed_hosts=("luminary-analytics.github.io",))
        # Offline mode that allows the Pages site: the feed and its disk image are fine.
        assert engine.may_check(sparkle.CHECK_BACKGROUND) == ""
        assert engine.download_refusal(self.DMG, "0.21.0") == ""
        # A feed that names another host for the download, or for its notes, isn't.
        elsewhere = "https://objects.githubusercontent.com/lumi-0.21.0.dmg"
        reason = engine.download_refusal(elsewhere, "0.21.0")
        assert reason.startswith("Offline mode") and "objects.githubusercontent.com" in reason
        assert "objects.githubusercontent.com" in engine.release_notes_refusal("https://objects.githubusercontent.com/n")
        # This computer is always reachable.
        assert engine.download_refusal("http://127.0.0.1:8000/lumi-0.21.0.dmg", "0.21.0") == ""
        # Offline mode without the Pages site refuses the check itself, as it is now.
        offline.set_for_tests(enabled=True, allowed_hosts=())
        assert "luminary-analytics.github.io" in engine.may_check(sparkle.CHECK_USER)
        assert [record["type"] for record in records()] == ["update.refused"]

    def test_a_failed_download_that_wasnt_refused_is_an_error(self, mac, records):
        engine = start(UpdatePreferences())
        assert engine.download_refusal(self.DMG, "0.21.0") == ""
        engine.download_failed("0.21.0", "NSURLErrorDomain", -1009)
        [failed] = records()
        assert failed["type"] == "update.check"
        assert (failed["data"]["result"], failed["data"]["code"], failed["data"]["domain"]) == (
            "error", -1009, "NSURLErrorDomain")

    def test_an_address_lumi_cant_read_is_refused(self, mac, records):
        engine = start(UpdatePreferences())
        assert "couldn't read the update's download address" in engine.download_refusal("", "0.21.0")
        [refused] = records()
        assert (refused["type"], refused["data"]["stage"]) == ("update.refused", "download")


class TestTheChannelAndPinHold:
    """One key signs every macOS feed, so the update Sparkle found is checked against the channel and pin too.

    Someone who can change the update site could otherwise serve the beta
    feed, or a newer line's, at a stable or pinned copy's address.
    """

    def test_the_stable_channel_takes_only_stable_releases(self, mac, records):
        engine = start(UpdatePreferences())
        assert engine.may_proceed("0.21.0") == ""
        assert "is a beta, and this copy takes stable releases" in engine.may_proceed("0.22.0-beta.1")
        assert engine.may_proceed("0.22.0beta.1")  # as a Mac bundle version gives it
        assert engine.may_proceed("0.19.2.dev11")
        assert "can't tell what release" in engine.may_proceed("")
        first = records()[0]
        assert (first["type"], first["data"]["stage"], first["data"]["to_version"]) == (
            "update.refused", "channel", "0.22.0-beta.1")
        assert len(records()) == 4

    def test_a_pin_takes_only_stable_releases_of_its_line(self, mac, records):
        engine = start(UpdatePreferences(channel="beta", pin="0.20"))  # the pin wins
        assert engine.may_proceed("0.20.3") == ""
        assert "isn't a stable release of the 0.20 line" in engine.may_proceed("0.21.0")
        assert "isn't a stable release of the 0.20 line" in engine.may_proceed("0.20.4-beta.1")
        assert [record["data"]["stage"] for record in records()] == ["pin", "pin"]

    def test_the_beta_channel_takes_betas_and_releases(self, mac, records):
        engine = start(UpdatePreferences(channel="beta"))
        assert engine.may_proceed("0.22.0-beta.1") == "" and engine.may_proceed("0.21.0") == ""
        assert records() == []

    def test_offline_mode_comes_first(self, mac, records):
        engine = start(UpdatePreferences())
        engine.stop("Offline mode: the update check needs luminary-analytics.github.io.")
        assert engine.may_proceed("0.22.0-beta.1").startswith("Offline mode")
        assert records() == []  # recorded when a download is refused, not here

    def test_it_is_what_an_update_from_a_file_takes(self):
        # The same rule as Settings > Updates > Install an update from a file (lumi/update_file.py).
        assert update_channels.refusal_for("0.21.0", "stable", "") == ""
        assert update_channels.refusal_for("v0.21.0-rc.1", "stable", "")
        assert update_channels.refusal_for("0.21.0-rc.1", "beta", "") == ""
        assert update_channels.refusal_for("0.20.9", "stable", "0.20") == ""
        assert update_channels.refusal_for("0.2.0", "stable", "0.20")
        assert update_channels.refusal_for("anything", "beta", "") == ""


@pytest.mark.skipif(sys.platform == "win32", reason="packaging/fetch_sparkle.sh runs on macOS")
def test_fetching_sparkle_never_uses_what_it_cant_verify(tmp_path):
    import re
    import subprocess

    script = ROOT / "packaging" / "fetch_sparkle.sh"
    version = re.search(r'^SPARKLE_VERSION="(.+)"$', script.read_text(encoding="utf-8"), re.M).group(1)
    dest = tmp_path / "sparkle"
    (dest / "Sparkle.framework").mkdir(parents=True)  # left by an earlier build: never used as it is
    kept = dest / f"Sparkle-{version}.tar.xz"
    kept.write_bytes(b"an archive someone changed")
    tools = tmp_path / "bin"
    tools.mkdir()
    curl = tools / "curl"  # stands in for the download, which gets something else too
    curl.write_text('#!/bin/sh\nwhile [ $# -gt 0 ]; do\n  if [ "$1" = "-o" ]; then shift; printf "not sparkle" > "$1"; fi\n'
                    '  shift\ndone\ntouch "$(dirname "$0")/downloaded"\n', encoding="utf-8")
    curl.chmod(0o755)
    result = subprocess.run(["bash", str(script), str(dest)], capture_output=True, text=True, timeout=60,
                            env={**os.environ, "PATH": f"{tools}{os.pathsep}{os.environ['PATH']}"})
    assert result.returncode == 1, result.stdout + result.stderr
    assert "SHA-256 mismatch" in result.stderr
    # The kept archive didn't match, so it was fetched again, and isn't kept.
    assert (tools / "downloaded").exists() and not kept.exists()
    assert list((dest / "Sparkle.framework").iterdir()) == []  # nothing was extracted


def test_the_framework_is_found_inside_lumi_app(tmp_path):
    app = tmp_path / "Lumi.app" / "Contents"
    (app / "MacOS").mkdir(parents=True)
    executable = app / "MacOS" / "lumi"
    executable.write_text("", encoding="utf-8")
    assert sparkle.framework_path(str(executable)) is None
    framework = app / "Frameworks" / "Sparkle.framework"
    framework.mkdir(parents=True)
    (framework / "Sparkle").write_text("", encoding="utf-8")
    assert sparkle.framework_path(str(executable)) == framework


class TestOpeningTheApp:
    """A LaunchServices launch of Lumi.app is the GUI; the same executable from a terminal or a script isn't.

    LaunchServices (Finder, the Dock, ``open``, Sparkle's relaunch) starts an
    app as a child of launchd, without a terminal, with ``__CFBundleIdentifier``
    set to the app's identifier. Anything else keeps the command line's
    behavior: the terminal UI without arguments, and output where it was sent.
    """

    APP = ["/Applications/Lumi.app/Contents/MacOS/lumi"]
    LUMI = "com.luminaryanalytics.lumi"
    LAUNCHED = {"platform": "darwin", "frozen": True, "terminal": False, "parent": 1,
                "environ": {"__CFBundleIdentifier": LUMI}, "bundle_id": LUMI}

    def facts(self, **changed):
        return {**self.LAUNCHED, **changed}

    def test_finder_dock_open_and_sparkles_relaunch_start_the_gui(self):
        from lumi import __main__ as entry

        assert entry._launched_by_launchservices(self.APP, **self.LAUNCHED)
        assert entry._opened_as_mac_app(self.APP, **self.LAUNCHED)
        # Old macOS versions add a process serial number, and nothing else.
        assert entry._opened_as_mac_app(self.APP + ["-psn_0_1234"], **self.facts(parent=4242, environ={}))

    def test_a_terminal_a_script_or_a_job_keeps_the_command_line(self):
        from lumi import __main__ as entry

        others = {
            "a terminal": self.facts(terminal=True),
            # Lumi's own shell, or a script: another parent, even with Lumi's identifier inherited.
            "a script": self.facts(parent=4242),
            # cron, or a launchd job: launchd's child without a terminal, but no LaunchServices launch.
            "a launchd job": self.facts(environ={}),
            # Started by another app LaunchServices opened, which passed its own identifier on.
            "another app": self.facts(environ={"__CFBundleIdentifier": "com.apple.Terminal"}),
            "from source": self.facts(frozen=False),
            "Windows": self.facts(platform="win32"),
        }
        for how, facts in others.items():
            assert not entry._launched_by_launchservices(self.APP, **facts), how
            assert not entry._opened_as_mac_app(self.APP, **facts), how

    def test_arguments_mean_what_they_say_even_from_launchservices(self):
        from lumi import __main__ as entry

        assert entry._launched_by_launchservices(self.APP + ["gui", "--browser"], **self.LAUNCHED)
        assert not entry._opened_as_mac_app(self.APP + ["gui", "--browser"], **self.LAUNCHED)

    def test_the_bundle_identifier_comes_from_the_apps_info_plist(self, tmp_path):
        import plistlib

        from lumi import __main__ as entry

        contents = tmp_path / "Lumi.app" / "Contents"
        (contents / "MacOS").mkdir(parents=True)
        executable = contents / "MacOS" / "lumi"
        assert entry._bundle_identifier(str(executable)) == ""
        (contents / "Info.plist").write_bytes(plistlib.dumps({"CFBundleIdentifier": self.LUMI}))
        assert entry._bundle_identifier(str(executable)) == self.LUMI
        (contents / "Info.plist").write_bytes(b"not a plist")
        assert entry._bundle_identifier(str(executable)) == ""

    def test_only_a_launchservices_launch_sends_devnull_output_to_the_startup_log(self, tmp_path):
        from lumi import __main__ as entry

        assert entry._discarded(None, False) and entry._discarded(None, True)  # no stream at all: always
        with open(tmp_path / "out.txt", "w", encoding="utf-8") as regular:
            assert not entry._discarded(regular, True) and not entry._discarded(regular, False)
        if sys.platform == "win32":
            return  # /dev/null is the macOS case; Windows gives a windowed app no streams at all
        with open(os.devnull, "w", encoding="utf-8") as devnull:
            assert entry._discarded(devnull, True)
            # `lumi run … > /dev/null` from a script: what it prints stays discarded, off the disk.
            assert not entry._discarded(devnull, False)


# ── On a Mac: the PyObjC delegate ─────────────────────────────────────────────

try:
    import objc
except ImportError:  # not a Mac, or PyObjC isn't installed
    objc = None
on_macos = pytest.mark.skipif(sys.platform != "darwin" or objc is None, reason="PyObjC and Sparkle are macOS-only")


@on_macos
def test_the_delegate_tells_objective_c_sparkles_types():
    cls = sparkle._delegate_class()
    assert cls is sparkle._delegate_class()  # one class per process

    def types(selector):
        signature = cls.instanceMethodSignatureForSelector_(selector)
        assert signature is not None, selector
        arguments = [signature.getArgumentTypeAtIndex_(i) for i in range(signature.numberOfArguments())]
        return signature.methodReturnType(), [a.decode() if isinstance(a, bytes) else a for a in arguments[2:]]

    boolean = {"B", "c", "Z", b"B", b"c", b"Z"}
    ret, args = types(b"updater:mayPerformUpdateCheck:error:")
    assert ret in boolean and args[0] == "@" and args[1] == "q" and args[2].endswith("^@")
    ret, args = types(b"updater:shouldProceedWithUpdate:updateCheck:error:")
    assert ret in boolean and args[2] == "q" and args[3].endswith("^@")
    ret, args = types(b"updater:shouldPostponeRelaunchForUpdate:untilInvokingBlock:")
    assert ret in boolean and args[2].startswith("@?")
    ret, args = types(b"updater:didFinishUpdateCycleForUpdateCheck:error:")
    assert ret in {"v", b"v"} and args[1] == "q"
    ret, args = types(b"updater:userDidMakeChoice:forUpdate:state:")
    assert args[1] == "q"
    ret, _ = types(b"feedURLStringForUpdater:")
    assert ret in {"@", b"@"}
    ret, args = types(b"updater:willDownloadUpdate:withRequest:")
    assert ret in {"v", b"v"} and args == ["@", "@", "@"]
    ret, args = types(b"updater:shouldDownloadReleaseNotesForUpdate:")
    assert ret in boolean and args == ["@", "@"]
    ret, args = types(b"updater:failedToDownloadUpdate:error:")
    assert ret in {"v", b"v"} and args == ["@", "@", "@"]


@on_macos
def test_the_delegate_answers_through_objective_c():
    from Foundation import NSObject

    owner = SimpleNamespace(feed_url=lambda: MAC_FEED, may_check=lambda kind: "")
    delegate = sparkle._delegate_class().alloc().init()
    delegate.owner = owner
    assert delegate.owner is owner
    # performSelector goes through the Objective-C runtime, not a Python shortcut.
    assert str(delegate.performSelector_withObject_(b"feedURLStringForUpdater:", NSObject.alloc().init())) == MAC_FEED
    assert delegate.respondsToSelector_(b"updater:willInstallUpdate:")
    assert not delegate.respondsToSelector_(b"updater:willInstallUpdateOnQuit:immediateInstallationBlock:")


@on_macos
def test_a_refused_download_request_gets_an_address_sparkle_wont_load():
    from Foundation import NSURL, NSMutableURLRequest

    dmg = "https://luminary-analytics.github.io/resonant-client/downloads/v0.21.0/lumi-0.21.0.dmg"
    asked = []
    answer = [""]
    owner = SimpleNamespace(download_refusal=lambda url, version: asked.append((url, version)) or answer[0])
    delegate = sparkle._delegate_class().alloc().init()
    delegate.owner = owner
    item = SimpleNamespace(displayVersionString=lambda: "0.21.0")

    def download(request):
        delegate.updater_willDownloadUpdate_withRequest_(None, item, request)
        return str(request.URL().absoluteString())

    assert download(NSMutableURLRequest.requestWithURL_(NSURL.URLWithString_(dmg))) == dmg  # allowed: untouched
    answer[0] = "Offline mode: the update download needs luminary-analytics.github.io."
    assert download(NSMutableURLRequest.requestWithURL_(NSURL.URLWithString_(dmg))) == sparkle.REFUSED_DOWNLOAD_URL
    assert asked == [(dmg, "0.21.0")] * 2
    assert NSURL.URLWithString_(sparkle.REFUSED_DOWNLOAD_URL).scheme() not in ("http", "https")
    # Unsure is refused too.
    owner.download_refusal = lambda url, version: 1 / 0
    assert download(NSMutableURLRequest.requestWithURL_(NSURL.URLWithString_(dmg))) == sparkle.REFUSED_DOWNLOAD_URL
    # A request whose address can't be read is asked about as "" (refused by
    # the engine), and nothing it raises reaches Sparkle.
    asked.clear()
    owner.download_refusal = lambda url, version: asked.append((url, version)) or ("refused" if not url else "")
    delegate.updater_willDownloadUpdate_withRequest_(None, item, object())
    assert asked == [("", "0.21.0")]


# ── On a Mac with the pinned framework: Sparkle itself ──────────────────────

FRAMEWORK = os.environ.get("LUMI_TEST_SPARKLE_FRAMEWORK", "")
with_sparkle = pytest.mark.skipif(sys.platform != "darwin" or objc is None or not FRAMEWORK,
                                  reason="set LUMI_TEST_SPARKLE_FRAMEWORK to a Sparkle.framework (macOS)")


def _load_packaging(name):
    spec = importlib.util.spec_from_file_location(f"lumi_packaging_{name}", ROOT / "packaging" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_appcast():
    return _load_packaging("update_appcast")


def _load_feed_signature():
    return _load_packaging("feed_signature")


class _Feeds(http.server.SimpleHTTPRequestHandler):
    requests: list[str] = []

    def log_message(self, *args):
        pass

    def do_GET(self):
        type(self).requests.append(self.path)
        super().do_GET()


@pytest.fixture
def feeds(tmp_path):
    """A web server on this computer for the feeds and files a test puts in ``feeds.site``."""
    site = tmp_path / "site"
    site.mkdir()
    _Feeds.requests = []
    handler = lambda *a, **k: _Feeds(*a, directory=str(site), **k)  # noqa: E731
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"

    def publish(version):
        """Add a macOS release to the site's feeds with update_appcast.py, as publish_macos.ps1 does (unsigned)."""
        dmg = tmp_path / f"lumi-{version}.dmg"
        dmg.write_bytes(b"disk image")
        _load_appcast().publish_feeds(site, version, dmg, "c2lnbmF0dXJl", f"<p>Lumi {version}</p>",
                                      f"{base}/downloads", platform="macos")

    yield SimpleNamespace(site=site, base=base, url=lambda name: f"{base}/{name}", publish=publish)
    server.shutdown()


@pytest.fixture
def host_app(tmp_path):
    """Makes app bundles for Sparkle to look after (not this Python); forgets what Sparkle kept for them."""
    import plistlib

    made = []

    def make(version="0.20.0", bundle_version=None, **info):
        bundle_id = f"com.luminaryanalytics.lumi.sparkle-test.{uuid.uuid4().hex[:12]}"
        contents = tmp_path / bundle_id / "LumiSparkleTest.app" / "Contents"
        (contents / "MacOS").mkdir(parents=True)
        (contents / "Info.plist").write_bytes(plistlib.dumps({
            "CFBundleIdentifier": bundle_id, "CFBundleName": "LumiSparkleTest", "CFBundleExecutable": "test",
            "CFBundlePackageType": "APPL", "CFBundleShortVersionString": version,
            "CFBundleVersion": bundle_version or version, "SUPublicEDKey": updater.EDDSA_PUBLIC_KEY,
            "SUEnableAutomaticChecks": True, "SUAllowsAutomaticUpdates": False, **info}))
        made.append(bundle_id)
        return str(contents.parent)

    yield make
    import shutil

    from Foundation import NSCachesDirectory, NSFileManager, NSUserDefaults, NSUserDomainMask

    defaults = NSUserDefaults.standardUserDefaults()
    caches = NSFileManager.defaultManager().URLsForDirectory_inDomains_(NSCachesDirectory, NSUserDomainMask)
    for bundle_id in made:
        defaults.removePersistentDomainForName_(bundle_id)
        # Where Sparkle notes when a feed first failed its signature check, for a bundle it doesn't run in.
        defaults.removeObjectForKey_(f"SUInitialFailedFeedSigningValidationDate_{bundle_id}")
        for folder in caches:  # a download's folder
            shutil.rmtree(Path(str(folder.path())) / bundle_id, ignore_errors=True)


class _Cycles:
    """A SparkleUpdater for a test bundle: what it records, and one update cycle at a time."""

    def __init__(self, host, feed, *, channel="stable", pin=""):
        self.events = []
        self.finished = []
        self.feed = feed
        self.engine = sparkle.SparkleUpdater(
            Path(FRAMEWORK), record=lambda kind, **data: self.events.append((kind, data)),
            turn_running=lambda: False, close_app=lambda: None, host_bundle=host)
        assert self.engine.start(SimpleNamespace(mode="manual", feed_url=feed, channel=channel, pin=pin))
        # The delegate looks each answer up on its owner, so these stand in for Lumi's own.
        self.engine.feed_url = lambda: self.feed
        cycle_finished = self.engine.cycle_finished
        self.engine.cycle_finished = lambda when: self.finished.append(when) or cycle_finished(when)

    @property
    def updater(self):
        return self.engine._bridge._updater

    def run(self, start=None):
        """Start a cycle (by default the information check, which opens no window) and wait for its end."""
        before, cycles = len(self.events), len(self.finished)
        (start or (lambda updater: updater.checkForUpdateInformation()))(self.updater)
        deadline = time.monotonic() + 30
        while len(self.finished) == cycles and time.monotonic() < deadline:
            self.engine._bridge.run_loop_once(0.2)
        assert len(self.finished) > cycles, "Sparkle didn't finish the update cycle"
        return self.events[before:]


FOUND = ("update.check", {"result": "found", "to_version": "0.21.0"})
NONE = ("update.check", {"result": "none"})
SU_APPCAST_PARSE_ERROR = 1000  # what Sparkle reports for a feed whose signature doesn't verify


@with_sparkle
def test_sparkle_reads_the_feed_lumi_chooses_and_the_macos_item(feeds, host_app):
    feeds.publish("0.21.0")
    # A feed with a newer Windows-only item as well: Sparkle must skip it.
    mixed = (feeds.site / "appcast-macos.xml").read_text(encoding="utf-8").replace("<item>", (
        '<item><title>Version 0.22.0</title><sparkle:version>0.22.0</sparkle:version>'
        f'<enclosure url="{feeds.base}/downloads/v0.22.0/lumi-setup-0.22.0.exe" sparkle:version="0.22.0" '
        'sparkle:os="windows" length="1" type="application/octet-stream" sparkle:edSignature="c2ln" /></item><item>'), 1)
    (feeds.site / "mixed.xml").write_text(mixed, encoding="utf-8")
    run = _Cycles(host_app(), feeds.url("mixed.xml"))
    status = run.engine.status()
    assert status["version"] == "2.10.0"
    assert status["feed"] == feeds.url("mixed.xml")  # asked the delegate through Objective-C
    assert status["automatic"] is False

    asked = []
    may_check = run.engine.may_check
    run.engine.may_check = lambda kind: asked.append(kind) or may_check(kind)
    assert run.run() == [FOUND]
    assert asked == [sparkle.CHECK_INFORMATION]
    assert "/mixed.xml" in _Feeds.requests

    # Offline mode: Sparkle asks the delegate first, is refused, and makes no request.
    run.engine.stop("Offline mode: the update check needs this computer.")
    requests = len(_Feeds.requests)
    assert run.run() == []  # Lumi's own refusal isn't recorded as a failed check
    assert asked == [sparkle.CHECK_INFORMATION] * 2
    assert len(_Feeds.requests) == requests


def _signing_key():
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    key = Ed25519PrivateKey.generate()
    return key, base64.b64encode(key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)).decode()


def _sign(path, key):
    """Sign a feed as publish_macos.ps1 does: over its bytes as written, in feed_signature.py's block."""
    _load_feed_signature().attach(path, base64.b64encode(key.sign(path.read_bytes())).decode())


@with_sparkle
def test_sparkle_reads_only_a_feed_signed_with_the_apps_key(feeds, host_app):
    """Lumi.app's SURequireSignedFeed, with a throwaway key in place of the release key."""
    key, public = _signing_key()
    other, _ = _signing_key()
    feeds.publish("0.21.0")
    feed = feeds.site / "appcast-macos.xml"
    for name in ("unsigned.xml", "other-key.xml"):
        (feeds.site / name).write_bytes(feed.read_bytes())
    _sign(feeds.site / "other-key.xml", other)
    _sign(feed, key)
    signed = feed.read_bytes()
    # Changed after signing, as by someone who could edit the feed: where the download comes from.
    tampered = signed.replace(b"lumi-0.21.0.dmg", b"lumi-0.21.9.dmg", 1)
    assert tampered != signed and len(tampered) == len(signed)
    (feeds.site / "tampered.xml").write_bytes(tampered)

    run = _Cycles(host_app(SUPublicEDKey=public, SURequireSignedFeed=True, SUVerifyUpdateBeforeExtraction=True),
                  feeds.url("appcast-macos.xml"))
    assert run.run() == [FOUND]
    for name in ("tampered.xml", "other-key.xml", "unsigned.xml"):
        run.feed = feeds.url(name)
        assert run.run() == [("update.check", {"result": "error", "code": SU_APPCAST_PARSE_ERROR})], name
    # And the signed feed still reads after those failures.
    run.feed = feeds.url("appcast-macos.xml")
    assert run.run() == [FOUND]


@with_sparkle
def test_sparkle_offers_nothing_older_than_what_runs(feeds, host_app):
    feeds.publish("0.20.0")
    feeds.publish("0.21.0")
    feed = feeds.url("appcast-macos.xml")
    for running in ("0.21.0", "0.21.1"):
        assert _Cycles(host_app(running), feed).run() == [NONE], running
    # The same feed updates what is older, the release's own beta included.
    assert _Cycles(host_app("0.20.0"), feed).run() == [FOUND]
    assert _Cycles(host_app("0.21.0-beta.1", bundle_version="0.21.0beta.1"), feed).run() == [FOUND]


@with_sparkle
def test_sparkle_takes_only_what_the_channel_or_pin_takes_whichever_feed_it_reads(feeds, host_app):
    """The beta feed, or a newer line's, at a stable or pinned copy's address: correctly signed, still refused."""
    feeds.publish("0.21.0-beta.1")
    beta_feed = feeds.url("appcast-macos-beta.xml")
    [(kind, data)] = _Cycles(host_app(), beta_feed).run()  # a copy on the stable channel
    assert (kind, data["stage"], data["to_version"]) == ("update.refused", "channel", "0.21.0-beta.1")
    assert _Cycles(host_app(), beta_feed, channel="beta").run() == [
        ("update.check", {"result": "found", "to_version": "0.21.0-beta.1"})]
    feeds.publish("0.21.0")
    [(kind, data)] = _Cycles(host_app(), feeds.url("appcast-macos.xml"), pin="0.20").run()
    assert (kind, data["stage"], data["to_version"]) == ("update.refused", "pin", "0.21.0")


@with_sparkle
def test_a_download_is_checked_as_sparkle_starts_it(feeds, host_app):
    """Sparkle asks about an update once, when it finds it; the download is checked as it starts.

    With automatic updates on (never in Lumi.app), Sparkle downloads what it
    finds at once, without a window to answer: the same download, request and
    delegate as Install Update in an update window left open.
    """
    from lumi import offline

    feeds.publish("0.21.0")  # its disk image isn't on the server: a download that starts gets a 404
    elsewhere = "http://updates.example.test/downloads"
    remote = (feeds.site / "appcast-macos.xml").read_text(encoding="utf-8").replace(f"{feeds.base}/downloads", elsewhere)
    assert elsewhere in remote
    (feeds.site / "remote.xml").write_text(remote, encoding="utf-8")
    dmg = "/downloads/v0.21.0/lumi-0.21.0.dmg"

    def automatic(feed):
        run = _Cycles(host_app(SUAllowsAutomaticUpdates=True, SUAutomaticallyUpdate=True), feed)
        run.updater.setAutomaticallyDownloadsUpdates_(True)
        assert run.updater.automaticallyDownloadsUpdates()
        return run

    def in_background(updater):
        updater.checkForUpdatesInBackground()

    # Nothing refuses it: Sparkle requests the disk image straight away.
    run = automatic(feeds.url("appcast-macos.xml"))
    assert run.run(in_background) == [FOUND, ("update.check", {
        "result": "error", "stage": "download", "to_version": "0.21.0", "code": sparkle.SU_DOWNLOAD_ERROR,
        "domain": sparkle.SPARKLE_ERROR_DOMAIN})]
    assert dmg in _Feeds.requests

    # Offline mode comes on after Sparkle's question: its download never reaches the server.
    reason = "Offline mode: the update download needs 127.0.0.1."
    may_proceed = run.engine.may_proceed
    run.engine.may_proceed = lambda version: may_proceed(version) or run.engine.stop(reason) or ""
    _Feeds.requests.clear()
    assert run.run(in_background) == [FOUND, ("update.refused", {
        "stage": "download", "to_version": "0.21.0", "reason": reason})]
    assert "/appcast-macos.xml" in _Feeds.requests and dmg not in _Feeds.requests

    # Offline mode reads a feed on this computer, but refuses a download from elsewhere.
    offline.set_for_tests(enabled=True, allowed_hosts=())
    run = automatic(feeds.url("remote.xml"))
    [found, (kind, data)] = run.run(in_background)
    assert found == FOUND and (kind, data["stage"], data["to_version"]) == ("update.refused", "download", "0.21.0")
    assert data["reason"].startswith("Offline mode") and "updates.example.test" in data["reason"]


@with_sparkle
def test_sparkle_orders_lumis_versions_as_the_feeds_do():
    appcast = _load_appcast()
    from Foundation import NSBundle

    bundle = NSBundle.bundleWithPath_(FRAMEWORK)
    assert bundle.load()
    comparator = objc.lookUpClass("SUStandardVersionComparator").defaultComparator()
    ordered = ["0.19.2", "0.20.0", "0.21.0-alpha.1", "0.21.0-alpha.2", "0.21.0-beta.1", "0.21.0-beta.10",
               "0.21.0-rc.1", "0.21.0", "0.21.1", "1.0.0"]
    for older, newer in zip(ordered, ordered[1:]):
        a, b = appcast.macos_bundle_version(older), appcast.macos_bundle_version(newer)
        assert comparator.compareVersion_toVersion_(a, b) == -1, (older, newer)
        assert comparator.compareVersion_toVersion_(b, a) == 1, (older, newer)
    # Sparkle ignores what follows a dash, which is why the feeds drop it.
    assert comparator.compareVersion_toVersion_("0.21.0-beta.1", "0.21.0") == 0
    # A development build (0.19.2.dev11) is older than its release.
    assert comparator.compareVersion_toVersion_("0.19.2.dev11", "0.19.2") == -1
