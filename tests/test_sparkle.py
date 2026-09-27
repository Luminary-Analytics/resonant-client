"""macOS updates through Sparkle 2 (lumi/sparkle.py, lumi/updater.py).

Three layers:

* ``SparkleUpdater`` and the updater's macOS paths with a fake bridge: the
  feed, the mode, offline mode, the wait for a running turn, audit records
  and the main-thread hand-off. Nothing here needs a Mac.
* On macOS, the PyObjC delegate class: the Objective-C types it registers
  for Sparkle's BOOL results, NSInteger enums, NSError** and blocks.
* With ``LUMI_TEST_SPARKLE_FRAMEWORK`` pointing at the pinned framework
  (CI fetches it with packaging/fetch_sparkle.sh), the real Sparkle: it reads
  the feed through the delegate, finds the macOS item in a feed that
  packaging/update_appcast.py wrote, skips a Windows one, stops asking after
  offline mode, and orders Lumi's versions as the feeds expect.
"""
from __future__ import annotations

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
        engine.download_failed("0.21.0", 2001)
        engine.will_install("0.21.0")
        got = [(r["type"], r["data"].get("result"), r["data"].get("to_version")) for r in records()]
        assert got == [
            ("update.check", "found", "0.21.0"), ("update.check", "none", None), ("update.check", "error", None),
            ("update.cancelled", None, None), ("update.skipped", None, "0.21.0"),
            ("update.postponed", None, "0.21.0"), ("update.cancelled", None, None),
            ("update.check", "error", "0.21.0"), ("update.install", None, "0.21.0")]
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
    """A Finder launch of Lumi.app is the GUI; the same executable in Terminal is the terminal UI."""

    def test_finder_dock_and_open_start_the_gui(self):
        from lumi import __main__ as entry

        app = ["/Applications/Lumi.app/Contents/MacOS/lumi"]
        assert entry._opened_as_mac_app(app, platform="darwin", frozen=True, terminal=False)
        assert entry._opened_as_mac_app(app + ["-psn_0_1234"], platform="darwin", frozen=True, terminal=False)
        # In Terminal, with arguments, from source, or on Windows: as before.
        assert not entry._opened_as_mac_app(app, platform="darwin", frozen=True, terminal=True)
        assert not entry._opened_as_mac_app(app + ["gui", "--browser"], platform="darwin", frozen=True, terminal=False)
        assert not entry._opened_as_mac_app(app, platform="darwin", frozen=False, terminal=False)
        assert not entry._opened_as_mac_app(["lumi.exe"], platform="win32", frozen=True, terminal=False)


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


# ── On a Mac with the pinned framework: Sparkle itself ──────────────────────

FRAMEWORK = os.environ.get("LUMI_TEST_SPARKLE_FRAMEWORK", "")
with_sparkle = pytest.mark.skipif(sys.platform != "darwin" or objc is None or not FRAMEWORK,
                                  reason="set LUMI_TEST_SPARKLE_FRAMEWORK to a Sparkle.framework (macOS)")


def _load_appcast():
    spec = importlib.util.spec_from_file_location("lumi_update_appcast", ROOT / "packaging" / "update_appcast.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Feeds(http.server.SimpleHTTPRequestHandler):
    requests: list[str] = []

    def log_message(self, *args):
        pass

    def do_GET(self):
        type(self).requests.append(self.path)
        super().do_GET()


@pytest.fixture
def sparkle_host(tmp_path):
    """An app bundle for Sparkle to look after (not this Python), and a feed server on this computer."""
    appcast = _load_appcast()
    site = tmp_path / "site"
    site.mkdir()
    dmg = tmp_path / "lumi-0.21.0.dmg"
    dmg.write_bytes(b"disk image")
    base = "http://127.0.0.1:{port}/downloads"
    _Feeds.requests = []
    handler = lambda *a, **k: _Feeds(*a, directory=str(site), **k)  # noqa: E731
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    appcast.publish_feeds(site, "0.21.0", dmg, "c2lnbmF0dXJl", "<p>Lumi 0.21.0</p>", base.format(port=port),
                          platform="macos")
    # A feed with a newer Windows-only item as well: Sparkle must skip it.
    mixed = (site / "appcast-macos.xml").read_text(encoding="utf-8").replace("<item>", (
        '<item><title>Version 0.22.0</title><sparkle:version>0.22.0</sparkle:version>'
        f'<enclosure url="http://127.0.0.1:{port}/downloads/v0.22.0/lumi-setup-0.22.0.exe" sparkle:version="0.22.0" '
        'sparkle:os="windows" length="1" type="application/octet-stream" sparkle:edSignature="c2ln" /></item><item>'), 1)
    (site / "mixed.xml").write_text(mixed, encoding="utf-8")
    bundle_id = f"com.luminaryanalytics.lumi.sparkle-test.{uuid.uuid4().hex[:12]}"
    app = tmp_path / "LumiSparkleTest.app" / "Contents"
    (app / "MacOS").mkdir(parents=True)
    import plistlib

    (app / "Info.plist").write_bytes(plistlib.dumps({
        "CFBundleIdentifier": bundle_id, "CFBundleName": "LumiSparkleTest", "CFBundleExecutable": "test",
        "CFBundlePackageType": "APPL", "CFBundleShortVersionString": "0.20.0", "CFBundleVersion": "0.20.0",
        "SUPublicEDKey": updater.EDDSA_PUBLIC_KEY, "SUEnableAutomaticChecks": True,
        "SUAllowsAutomaticUpdates": False}))
    yield SimpleNamespace(app=str(app.parent), port=port, site=site, bundle_id=bundle_id,
                          feed=f"http://127.0.0.1:{port}/appcast-macos.xml",
                          mixed=f"http://127.0.0.1:{port}/mixed.xml")
    server.shutdown()
    from Foundation import NSUserDefaults

    NSUserDefaults.standardUserDefaults().removePersistentDomainForName_(bundle_id)


@with_sparkle
def test_sparkle_reads_the_feed_lumi_chooses_and_the_macos_item(sparkle_host):
    events = []
    prefs = SimpleNamespace(mode="manual", feed_url=sparkle_host.mixed)
    engine = sparkle.SparkleUpdater(Path(FRAMEWORK), record=lambda kind, **data: events.append((kind, data)),
                                    turn_running=lambda: False, close_app=lambda: None,
                                    host_bundle=sparkle_host.app)
    assert engine.start(prefs)
    status = engine.status()
    assert status["version"] == "2.10.0"
    assert status["feed"] == sparkle_host.mixed  # asked the delegate through Objective-C
    assert status["automatic"] is False

    asked = []
    may_check = engine.may_check
    engine.may_check = lambda kind: asked.append(kind) or may_check(kind)  # the delegate looks it up on the owner
    finished = []
    cycle_finished = engine.cycle_finished
    engine.cycle_finished = lambda when: finished.append(when) or cycle_finished(when)

    def probe():
        before, cycles = len(events), len(finished)
        engine._bridge._updater.checkForUpdateInformation()
        deadline = time.monotonic() + 30
        while len(finished) == cycles and time.monotonic() < deadline:
            engine._bridge.run_loop_once(0.2)
        assert len(finished) > cycles, "Sparkle didn't finish the check"
        return events[before:]

    found = probe()
    assert found == [("update.check", {"result": "found", "to_version": "0.21.0"})], found
    assert asked == [sparkle.CHECK_INFORMATION]
    assert "/mixed.xml" in _Feeds.requests

    # Offline mode: Sparkle asks the delegate first, is refused, and makes no request.
    engine.stop("Offline mode: the update check needs this computer.")
    requests = len(_Feeds.requests)
    assert probe() == []  # Lumi's own refusal isn't recorded as a failed check
    assert asked == [sparkle.CHECK_INFORMATION] * 2
    assert len(_Feeds.requests) == requests


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
