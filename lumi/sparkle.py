"""Sparkle 2 on macOS: the counterpart of WinSparkle, driven through PyObjC.

Lumi.app carries Sparkle.framework in ``Contents/Frameworks`` (pinned and
checksum-verified by packaging/fetch_sparkle.sh, copied in by
packaging/build_macos.sh). The app's Info.plist (packaging/lumi.spec) holds
what Sparkle reads before anything runs:

* ``SUPublicEDKey``: ``updater.EDDSA_PUBLIC_KEY``, the key WinSparkle checks
  with. One signing key and one appcast format serve both platforms; the
  macOS feeds are separate files (update_channels.feed_name).
* ``SUFeedURL``: the stable macOS feed. The delegate below answers with the
  feed for the channel or pin in effect instead.
* ``SUEnableAutomaticChecks``: Sparkle never asks whether to check; Settings >
  Updates (or the policy) decides, and ``start`` sets it at every launch.
* ``SUAllowsAutomaticUpdates`` off: nothing installs silently, so every
  install passes the "is a turn running?" question below.
* ``SUVerifyUpdateBeforeExtraction``: the disk image's EdDSA signature is
  checked before Sparkle mounts it.
* ``SURequireSignedFeed``: the feeds are EdDSA-signed too
  (packaging/feed_signature.py), so what a feed lists, and where it says to
  download from, is trusted only when it verifies.

The delegate (``_delegate_class``) answers Sparkle on the main thread:

* which feed to read;
* whether a check may start, and whether a download may: not after offline
  mode stopped the updater (``stop``), and never to a host offline mode
  refuses now. Sparkle asks about a download only once, when it finds the
  update, so an update window left open could start one much later; the
  download request itself is checked (``download_refusal``) and a refused
  one is pointed at an address Sparkle's downloader won't load;
* whether to go on with an update it found: not one this copy's channel or
  pin doesn't take (``may_proceed``), whichever feed listed it;
* whether to install now: while an agent turn runs Sparkle waits, and Lumi
  lets it carry on once the turn ends (``postpone``), so an update never cuts
  a turn off;
* what happened, for the audit log: the same ``update.*`` records as on
  Windows.

Sparkle's API belongs to the main thread. ``start`` runs there (``__main__``
starts the updater before the GUI takes the thread); checks asked for from
other threads are handed to it (``Bridge.on_main``). In the desktop window
the main thread runs AppKit's event loop; in browser mode ``run_until`` turns
the run loop instead of just waiting for the server thread, so Sparkle's
timers fire there too.

``SparkleUpdater`` makes the decisions and never imports PyObjC; ``Bridge``
is the only part that does, and tests replace it (tests/test_sparkle.py).
"""

from __future__ import annotations

import logging
import sys
import threading
from pathlib import Path
from typing import Any, Callable

from . import update_channels

logger = logging.getLogger(__name__)

FRAMEWORK = "Sparkle.framework"
# The NSError domain of Lumi's own refusals, so they aren't recorded as failed checks.
ERROR_DOMAIN = "com.luminaryanalytics.lumi.updates"
SPARKLE_ERROR_DOMAIN = "SUSparkleErrorDomain"
# SPUUpdateCheck, SPUUserUpdateChoice and the SUError codes Lumi tells apart.
CHECK_USER, CHECK_BACKGROUND, CHECK_INFORMATION = 0, 1, 2
CHOICE_SKIP, CHOICE_INSTALL, CHOICE_DISMISS = 0, 1, 2
SU_NO_UPDATE = 1001
SU_DOWNLOAD_ERROR = 2001
SU_INSTALLATION_CANCELED = 4007
SU_INSTALLATION_AUTHORIZE_LATER = 4008
# Where a refused download is sent: Sparkle's downloader loads only http and
# https addresses and fails any other at once, before any connection.
REFUSED_DOWNLOAD_URL = "lumi-update-refused:offline"
# How often an install that waits for a turn asks again.
POSTPONE_POLL_SECONDS = 5.0
# In browser mode nothing answers Sparkle's request to quit (that takes AppKit's
# event loop), so Lumi closes itself this long after the installer starts.
CLOSE_DELAY_SECONDS = 1.0


class SparkleError(RuntimeError):
    """Sparkle couldn't be loaded or started; the message says why."""


def framework_path(executable: str | None = None) -> Path | None:
    """Sparkle.framework inside Lumi.app (``Contents/Frameworks``), or None."""
    exe = Path(executable or sys.executable)
    candidates = []
    if exe.parent.name == "MacOS":
        candidates.append(exe.parent.parent / "Frameworks" / FRAMEWORK)
    meipass = getattr(sys, "_MEIPASS", None)  # PyInstaller: Contents/Frameworks in an app bundle
    if meipass:
        candidates.append(Path(meipass) / FRAMEWORK)
    for candidate in candidates:
        if (candidate / "Sparkle").exists():  # the symlink to Versions/Current/Sparkle
            return candidate
    return None


class SparkleUpdater:
    """What Lumi decides for Sparkle: the feed, when to check, when to install, what to record.

    ``record(event_type, **fields)`` writes an audit record; ``turn_running()``
    says whether an agent turn runs (True when unsure); ``close_app()`` closes
    Lumi gracefully, or is None where nothing can (the terminal).
    """

    def __init__(self, framework: Path, *, record: Callable[..., Any], turn_running: Callable[[], bool],
                 close_app: Callable[[], Callable[[], Any] | None], bridge: Any = None,
                 host_bundle: str | None = None):
        self.framework = Path(framework)
        self._record = record
        self._turn_running = turn_running
        self._close_app = close_app
        self._bridge = bridge if bridge is not None else Bridge(self.framework, host_bundle)
        self._preferences: Any = None
        self._refusal = ""  # why Sparkle may not check or download (offline mode), or ""
        self._postponed: Callable[[], Any] | None = None  # Sparkle's "install now", while a turn runs
        self._last_check: float | None = None
        self._info: dict[str, Any] = {}
        self._pumping = False  # run_until turns the run loop (browser mode)
        self._download_failure_recorded = False  # Sparkle reports it twice: as itself, then as the abort
        self._download_refused = False  # download_refusal recorded why the download that failed never started
        self._lock = threading.Lock()
        self.started = False

    # ── Starting and stopping ────────────────────────────────────────────

    def start(self, preferences: Any) -> bool:
        """Load Sparkle and start its update cycle; must run on the main thread."""
        self._preferences = preferences
        try:
            if not self._bridge.on_main_thread():
                logger.warning("Sparkle starts only on the main thread; this run doesn't update itself")
                return False
            info = self._bridge.start(self, automatic=preferences.mode == "automatic")
        except Exception:
            logger.exception("Sparkle didn't start; this run doesn't update itself")
            return False
        self._info = dict(info or {})
        self._last_check = self._info.get("last_check")
        self.started = True
        logger.info("Sparkle %s started: feed=%s automatic=%s", self._info.get("version"),
                    self._info.get("feed"), self._info.get("automatic"))
        return True

    def stop(self, reason: str) -> None:
        """Offline mode came on: no check or download starts again in this run.

        Sparkle has no call that stops it, so its delegate refuses instead.
        Every check (scheduled, asked for or resuming a downloaded update)
        asks ``may_check`` first, and every download's request passes
        ``download_refusal`` before Sparkle loads it, including one started
        from an update window that was already open. A restart after offline
        mode goes off starts checking again.
        """
        self._refusal = reason or "Update checks are stopped."
        logger.info("Stopped checking for updates: %s", self._refusal)

    @property
    def stopped(self) -> bool:
        return bool(self._refusal)

    def check(self, *, user_initiated: bool) -> bool:
        """Ask Sparkle to check now (with its window when ``user_initiated``); any thread."""
        if not self.started or self._refusal:
            return False
        self._bridge.on_main(self._bridge.check_for_updates if user_initiated else self._bridge.check_in_background)
        return True

    def last_check(self) -> int | None:
        value = self._last_check
        return int(value) if value and value > 0 else None

    def status(self) -> dict[str, Any]:
        """What Settings > Updates shows about Sparkle, as it reported at start."""
        return {"version": str(self._info.get("version") or ""), "feed": str(self._info.get("feed") or ""),
                "automatic": bool(self._info.get("automatic")), "stopped": self.stopped}

    def run_until(self, done: Callable[[], bool], interval: float = 0.5) -> None:
        """Turn the main run loop until ``done()``: browser mode's wait, so Sparkle's timers fire.

        Returns to Python every ``interval`` seconds, so Ctrl+C still stops Lumi.
        """
        self._pumping = True
        try:
            while not done():
                self._bridge.run_loop_once(interval)
        finally:
            self._pumping = False

    # ── The delegate's answers (main thread) ───────────────────────────

    def feed_url(self) -> str:
        return self._preferences.feed_url

    def _offline_refusal(self, url: str, feature: str) -> str:
        """Why offline mode, as it is now, refuses ``url``, or "" (the check the app applies to its own requests)."""
        if not url:
            return ""
        from . import offline

        return offline.refusal(url, feature)

    def may_check(self, kind: int) -> str:
        """Why Sparkle may not check now (``kind`` is an SPUUpdateCheck), or ""."""
        return self._refusal or self._offline_refusal(self.feed_url(), "the update check")

    def may_proceed(self, version: str) -> str:
        """Why Sparkle may not go on with ``version`` it just found, or "".

        Offline mode came on after the check, or the update isn't one this
        copy's channel or pin takes (recorded as ``update.refused``). Choosing
        the feed already means that, but one key signs every macOS feed, so
        someone who can change the update site could serve the beta feed, or
        a newer line's, at this copy's address. The version is part of what a
        feed's signature covers, so it is checked here too.
        """
        if self._refusal:
            return self._refusal
        prefs = self._preferences
        pin = getattr(prefs, "pin", "")
        reason = update_channels.refusal_for(version, getattr(prefs, "channel", "stable"), pin)
        if reason:
            self._record("update.refused", stage="pin" if pin else "channel", to_version=version, reason=reason)
        return reason

    def download_refusal(self, url: str, version: str) -> str:
        """Why Sparkle may not download ``url`` now, or "". A refusal is recorded (``update.refused``).

        This is the check that counts for downloads: Sparkle asks
        ``may_proceed`` only when it finds an update, and a person can press
        Install Update in a window that stayed open long after. It refuses
        after ``stop``, whenever offline mode refuses the download's host now,
        which the feed names, and when the address can't be read at all.
        Offline mode checks the address Sparkle starts with; Sparkle follows
        a redirect from there without asking (the disk image's signature is
        still checked).
        """
        if self._refusal:
            reason = self._refusal
        elif not url:
            reason = "Lumi couldn't read the update's download address."
        else:
            reason = self._offline_refusal(url, "the update download")
        if reason:
            self._download_refused = True
            self._record("update.refused", stage="download", to_version=version, reason=reason)
        return reason

    def release_notes_refusal(self, url: str) -> str:
        """Why Sparkle may not fetch the release notes at ``url``, or "" (Lumi's feeds carry them inline)."""
        return self._refusal or self._offline_refusal(url, "the update's release notes")

    def found(self, version: str) -> None:
        self._record("update.check", result="found", to_version=version)

    def not_found(self) -> None:
        self._record("update.check", result="none")

    def aborted(self, domain: str, code: int) -> None:
        """Sparkle stopped a check or an install with an error."""
        if self._download_failure_recorded:
            self._download_failure_recorded = False
            return  # the failed download that download_failed just recorded
        if domain == ERROR_DOMAIN:
            return  # Lumi's own refusal (offline mode): no check was made
        if domain == SPARKLE_ERROR_DOMAIN and code == SU_NO_UPDATE:
            return  # recorded as "none" already
        if domain == SPARKLE_ERROR_DOMAIN and code in (SU_INSTALLATION_CANCELED, SU_INSTALLATION_AUTHORIZE_LATER):
            self._record("update.cancelled")
            return
        self._record("update.check", result="error", code=int(code))

    def download_failed(self, version: str, domain: str, code: int) -> None:
        """The update's download failed; Sparkle then aborts with the same error, which isn't recorded again.

        A download ``download_refusal`` refused fails this way too, at once;
        its ``update.refused`` record already says why.
        """
        self._download_failure_recorded = True
        if self._download_refused:
            self._download_refused = False
            return
        self._record("update.check", result="error", stage="download", to_version=version, code=int(code),
                     domain=domain)

    def download_cancelled(self) -> None:
        self._record("update.cancelled")

    def choice(self, choice: int, version: str) -> None:
        """The person's answer in Sparkle's update window."""
        if choice == CHOICE_SKIP:
            self._record("update.skipped", to_version=version)
        elif choice == CHOICE_DISMISS:
            self._record("update.postponed", to_version=version)

    def cycle_finished(self, last_check: float | None) -> None:
        if last_check:
            self._last_check = last_check

    def postpone(self, version: str, proceed: Callable[[], Any]) -> bool:
        """Sparkle is about to close Lumi and install ``version``: wait while an agent turn runs.

        Returns True to make Sparkle wait; ``proceed`` lets it carry on, and
        is called once the turn is over. Sparkle asks this once per install.
        """
        if not self._turn_running():
            return False
        self._record("update.deferred", reason="an agent turn is running", to_version=version)
        with self._lock:
            self._postponed = proceed
        self._bridge.after(POSTPONE_POLL_SECONDS, self._install_when_idle)
        return True

    def _install_when_idle(self) -> None:
        with self._lock:
            proceed = self._postponed
        if proceed is None:
            return
        if self._turn_running():
            self._bridge.after(POSTPONE_POLL_SECONDS, self._install_when_idle)
            return
        with self._lock:
            self._postponed = None
        try:
            proceed()
        except Exception:
            logger.exception("Couldn't let Sparkle install the update; it installs when Lumi quits")

    def will_install(self, version: str) -> None:
        """Sparkle's installer took over: it replaces Lumi.app once Lumi quits, then opens it again."""
        self._record("update.install", to_version=version)
        if self._pumping:
            # Browser mode: AppKit's event loop isn't running, so Sparkle's
            # request to quit goes unanswered. Close as WinSparkle's flow does.
            self._bridge.after(CLOSE_DELAY_SECONDS, self._close)

    def _close(self) -> None:
        close = self._close_app()
        if callable(close):
            try:
                close()
            except Exception:
                logger.exception("Closing for the update failed; quit Lumi to install it")


# ── PyObjC ──────────────────────────────────────────────────────────────────

# Sparkle holds its delegate weakly; these keep the updater, its delegate and
# its user driver alive for the whole process, even after ``stop``.
_KEEP: list[tuple[Any, ...]] = []
_DELEGATE_CLASS: Any = None


class Bridge:
    """The PyObjC side: load Sparkle.framework, create the updater, run things on the main thread."""

    def __init__(self, framework: Path, host_bundle: str | None = None):
        self.framework = Path(framework)
        self.host_bundle = host_bundle  # tests: an app bundle other than the running one
        self._updater: Any = None

    def on_main_thread(self) -> bool:
        from Foundation import NSThread

        return bool(NSThread.isMainThread())

    def start(self, owner: SparkleUpdater, *, automatic: bool) -> dict[str, Any]:
        import objc
        from AppKit import NSApplication
        from Foundation import NSBundle

        bundle = NSBundle.bundleWithPath_(str(self.framework))
        if bundle is None:
            raise SparkleError(f"{self.framework} isn't a framework")
        loaded = bundle.loadAndReturnError_(None)
        ok, error = loaded if isinstance(loaded, tuple) else (loaded, None)
        if not ok:
            raise SparkleError(f"Sparkle.framework didn't load: {error.localizedDescription() if error else ''}")
        _register_metadata()
        updater_class = objc.lookUpClass("SPUUpdater")
        driver_class = objc.lookUpClass("SPUStandardUserDriver")
        # Sparkle's windows need the application object, which browser mode doesn't otherwise make.
        NSApplication.sharedApplication()
        host = NSBundle.bundleWithPath_(self.host_bundle) if self.host_bundle else NSBundle.mainBundle()
        if host is None or host.bundleIdentifier() is None:
            raise SparkleError("Lumi isn't running from an app bundle Sparkle can update")
        delegate = _delegate_class().alloc().init()
        delegate.owner = owner
        driver = driver_class.alloc().initWithHostBundle_delegate_(host, None)
        updater = updater_class.alloc().initWithHostBundle_applicationBundle_userDriver_delegate_(
            host, host, driver, delegate)
        _KEEP.append((updater, delegate, driver))
        # Before starting: Sparkle reads it when its cycle starts, and a later
        # change would reschedule. Every launch, so Settings (or the policy) wins
        # over anything saved in Sparkle's own preferences.
        updater.setAutomaticallyChecksForUpdates_(bool(automatic))
        started = updater.startUpdater_(None)
        ok, error = started if isinstance(started, tuple) else (started, None)
        if not ok:
            raise SparkleError(f"Sparkle didn't start: {error.localizedDescription() if error else 'no reason given'}")
        self._updater = updater
        feed = updater.feedURL()  # asks the delegate, as every check does
        last = updater.lastUpdateCheckDate()
        info = bundle.infoDictionary() or {}
        return {
            "version": str(info.get("CFBundleShortVersionString") or ""),
            "feed": str(feed.absoluteString()) if feed is not None else "",
            "automatic": bool(updater.automaticallyChecksForUpdates()),
            "last_check": float(last.timeIntervalSince1970()) if last is not None else None,
        }

    def check_for_updates(self) -> None:
        self._updater.checkForUpdates()

    def check_in_background(self) -> None:
        self._updater.checkForUpdatesInBackground()

    def on_main(self, function: Callable[[], Any]) -> None:
        from PyObjCTools import AppHelper

        AppHelper.callAfter(function)

    def after(self, seconds: float, function: Callable[[], Any]) -> None:
        from PyObjCTools import AppHelper

        AppHelper.callLater(seconds, function)

    def run_loop_once(self, seconds: float) -> None:
        from Foundation import NSDate, NSDefaultRunLoopMode, NSRunLoop

        NSRunLoop.currentRunLoop().runMode_beforeDate_(NSDefaultRunLoopMode, NSDate.dateWithTimeIntervalSinceNow_(seconds))


def _register_metadata() -> None:
    """What PyObjC can't read from Sparkle's binary: out-parameters and a block's arguments."""
    import objc

    objc.registerMetaDataForSelector(b"SPUUpdater", b"startUpdater:", {
        "arguments": {2: {"type_modifier": objc._C_OUT}}})
    objc.registerMetaDataForSelector(b"NSObject", b"updater:shouldPostponeRelaunchForUpdate:untilInvokingBlock:", {
        "arguments": {4: {"callable": {"retval": {"type": b"v"}, "arguments": {0: {"type": b"^v"}}}}}})


def _version(item: Any) -> str:
    """An appcast item's release version (``sparkle:shortVersionString``, as in the tag)."""
    for name in ("displayVersionString", "versionString"):
        try:
            value = getattr(item, name)()
        except Exception:
            continue
        if value:
            return str(value)
    return ""


def _error_parts(error: Any) -> tuple[str, int]:
    try:
        return str(error.domain()), int(error.code())
    except Exception:
        return "", 0


def _delegate_class() -> Any:
    """The Objective-C delegate class, made once per process (Objective-C class names are global)."""
    global _DELEGATE_CLASS
    if _DELEGATE_CLASS is not None:
        return _DELEGATE_CLASS
    import objc
    from Foundation import NSURL, NSError, NSLocalizedDescriptionKey, NSObject

    try:
        _DELEGATE_CLASS = objc.lookUpClass("LumiSparkleUpdaterDelegate")
        return _DELEGATE_CLASS
    except objc.nosuchclass_error:
        pass

    def call(delegate: Any, name: str, *args: Any, default: Any = None) -> Any:
        # An exception must never reach Sparkle: PyObjC would turn it into an
        # Objective-C exception that nothing catches, and Lumi would crash.
        try:
            return getattr(delegate.owner, name)(*args)
        except Exception:
            logger.exception("The update delegate's %s failed", name)
            return default

    def refusal(reason: str) -> Any:
        return NSError.errorWithDomain_code_userInfo_(ERROR_DOMAIN, 1, {NSLocalizedDescriptionKey: reason})

    # Every signature is spelled out: Sparkle's BOOL results, NSInteger enums,
    # NSError** out-parameters and blocks can't be inferred from the Python
    # functions, and a wrong one would corrupt what Sparkle reads back.
    class LumiSparkleUpdaterDelegate(NSObject):
        owner = objc.ivar()

        @objc.typedSelector(b"@@:@")
        def feedURLStringForUpdater_(self, updater):
            return call(self, "feed_url")

        @objc.typedSelector(b"Z@:@qo^@")
        def updater_mayPerformUpdateCheck_error_(self, updater, check, error):
            reason = call(self, "may_check", int(check), default="Lumi couldn't decide whether to check.")
            return (False, refusal(reason)) if reason else (True, None)

        @objc.typedSelector(b"Z@:@@qo^@")
        def updater_shouldProceedWithUpdate_updateCheck_error_(self, updater, item, check, error):
            reason = call(self, "may_proceed", _version(item), default="Lumi couldn't decide whether to update.")
            return (False, refusal(reason)) if reason else (True, None)

        @objc.typedSelector(b"Z@:@")
        def updaterShouldPromptForPermissionToCheckForUpdates_(self, updater):
            return False  # Settings > Updates decides, never Sparkle's own prompt

        @objc.typedSelector(b"v@:@@")
        def updater_didFindValidUpdate_(self, updater, item):
            call(self, "found", _version(item))

        @objc.typedSelector(b"v@:@@")
        def updaterDidNotFindUpdate_error_(self, updater, error):
            call(self, "not_found")

        @objc.typedSelector(b"v@:@@")
        def updater_didAbortWithError_(self, updater, error):
            call(self, "aborted", *_error_parts(error))

        @objc.typedSelector(b"v@:@q@")
        def updater_didFinishUpdateCycleForUpdateCheck_error_(self, updater, check, error):
            try:
                last = updater.lastUpdateCheckDate()
                when = float(last.timeIntervalSince1970()) if last is not None else None
            except Exception:
                when = None
            call(self, "cycle_finished", when)

        @objc.typedSelector(b"v@:@q@@")
        def updater_userDidMakeChoice_forUpdate_state_(self, updater, choice, item, state):
            call(self, "choice", int(choice), _version(item))

        @objc.typedSelector(b"v@:@")
        def userDidCancelDownload_(self, updater):
            call(self, "download_cancelled")

        @objc.typedSelector(b"v@:@@@")
        def updater_willDownloadUpdate_withRequest_(self, updater, item, request):
            # The request Sparkle downloads next (SPUCoreBasedUpdateDriver): a
            # refused one gets an address its downloader won't load, so it
            # fails before connecting. Unsure means refused.
            try:
                url = str(request.URL().absoluteString())
            except Exception:
                url = ""
            reason = call(self, "download_refusal", url, _version(item),
                          default="Lumi couldn't check the update's download address.")
            if reason:
                try:
                    request.setURL_(NSURL.URLWithString_(REFUSED_DOWNLOAD_URL))
                except Exception:  # never into Sparkle; a real NSMutableURLRequest always takes it
                    logger.exception("Couldn't stop the update's download (%s)", reason)

        @objc.typedSelector(b"Z@:@@")
        def updater_shouldDownloadReleaseNotesForUpdate_(self, updater, item):
            try:
                link = item.releaseNotesURL()
                url = str(link.absoluteString()) if link is not None else ""
            except Exception:
                url = ""
            return not call(self, "release_notes_refusal", url, default="unsure")

        @objc.typedSelector(b"v@:@@@")
        def updater_failedToDownloadUpdate_error_(self, updater, item, error):
            call(self, "download_failed", _version(item), *_error_parts(error))

        @objc.typedSelector(b"v@:@@")
        def updater_willInstallUpdate_(self, updater, item):
            call(self, "will_install", _version(item))

        @objc.typedSelector(b"Z@:@@@?")
        def updater_shouldPostponeRelaunchForUpdate_untilInvokingBlock_(self, updater, item, install):
            return bool(call(self, "postpone", _version(item), install, default=False))

    _DELEGATE_CLASS = LumiSparkleUpdaterDelegate
    return _DELEGATE_CLASS
