"""Which updates Lumi takes: the update mode, a channel and a version pin.

Settings > Updates, or an organization policy, sets three values:

* ``updates.mode``: ``automatic`` checks in the background once a day and
  whenever someone chooses Check for updates; ``manual`` checks only then;
  ``off`` never checks, for organizations that deploy Lumi themselves.
* ``updates.channel``: ``stable`` or ``beta``. The beta feed lists beta
  releases and every stable release, so beta users also get stable fixes.
* ``updates.pin``: a release line such as ``0.20``. Lumi then takes only
  stable releases of that line and nothing newer. A pin wins over the
  channel.

Each channel and release line has its own feed beside the installers on the
update site; ``packaging/update_appcast.py`` writes them. WinSparkle and
Sparkle take a feed URL, not a filter, so choosing the feed is how the choice
is enforced. On macOS the update Sparkle found is checked against it too
(``refusal_for``, from lumi/sparkle.py): the macOS feeds are signed, but with
one key for all of them, so the beta feed could be served at the stable
feed's address without breaking a signature. macOS has feeds of its own (``appcast-macos.xml`` and so on,
listing disk images), so the Windows feeds every installed copy polls keep
their addresses and contents; ``platform_name`` picks the set.

A copy installed from the MSI package (packaging/lumi.wxs), the macOS
installer package (packaging/macos_pkg.py) or a Linux .deb or .rpm
(packaging/linux_packages.py) never updates itself: the device management or
package manager that installed it does, and two updaters would otherwise fight
over the same folder. The package puts ``lumi-install.json`` beside the
executable, or in ``Lumi.app/Contents/Resources``, to say so; it wins over
everything.

Like the policy, these are read once at startup (``read``): a change applies
the next time Lumi starts. ``read`` parses settings.json itself rather than
constructing a ``SettingsManager``, which would write the file and move keys
into the credential store before the app has started.

In offline mode (lumi/offline.py) the update site is reachable only when it
is an allowed host; otherwise ``offline`` says why and neither WinSparkle nor
Sparkle is loaded, so nothing checks or downloads. Updates then come from a
file (lumi/update_file.py). Turning offline mode on stops a running updater
at once (``updater.apply_offline_mode``).
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# The update site. Still the pre-rebrand Pages address on purpose; see
# APPCAST_URL in lumi/updater.py.
FEED_BASE = "https://luminary-analytics.github.io/resonant-client/"
MODES = ("automatic", "manual", "off")
CHANNELS = ("stable", "beta")
# Which feeds a copy reads: macOS reads its own; Windows, and Linux (which
# never updates itself), the feeds every earlier install polls.
PLATFORMS = ("windows", "macos", "linux")
DEFAULTS = {"mode": "automatic", "channel": "stable", "pin": ""}
# Packages that update the copy they installed, by their install marker's name:
# where the copy came from, and what updates it.
MANAGED_INSTALLERS = {
    "msi": ("the MSI package", "your organization's device management"),
    "pkg": ("the macOS installer package", "your organization's device management"),
    "deb": ("the Debian package", "your package manager"),
    "rpm": ("the RPM package", "your package manager"),
}
_PIN = re.compile(r"(0|[1-9]\d{0,3})\.(0|[1-9]\d{0,3})")
# A release version as the feeds give it (0.21.0-beta.1), as a Mac bundle
# gives it (0.21.0beta.1), or a development build's (0.19.2.dev11).
_RELEASE = re.compile(r"(\d+)\.(\d+)\.(\d+)(?:[.-]?(dev|alpha|a|beta|b|rc)\.?(\d+))?", re.IGNORECASE)


def parse_pin(value: Any) -> str:
    """A release line ``X.Y``, or ``""`` for no pin; raise ValueError otherwise."""
    text = str(value if value is not None else "").strip().removeprefix("v")
    if not text:
        return ""
    if not _PIN.fullmatch(text):
        raise ValueError("A version pin is a release line such as 0.20, or empty for none.")
    return text


def normalize(key: str, value: Any) -> Any:
    """The value to store for ``updates.<key>``; raise ValueError with a fix."""
    if key == "mode":
        if value not in MODES:
            raise ValueError("updates.mode must be automatic, manual or off.")
        return value
    if key == "channel":
        if value not in CHANNELS:
            raise ValueError("updates.channel must be stable or beta.")
        return value
    if key == "pin":
        return parse_pin(value)
    raise ValueError(f"updates.{key} isn't an update setting; use mode, channel or pin.")


def validate_policy_settings(settings: dict[str, Any]) -> None:
    """Refuse a policy whose ``updates.*`` values Lumi couldn't apply."""
    for name, value in settings.items():
        if name.startswith("updates."):
            normalize(name.split(".", 1)[1], value)


def installed_by(executable: str | None = None) -> str:
    """The package this copy came from (a MANAGED_INSTALLERS name), else ``""``.

    The MSI's marker is beside ``lumi.exe``. In a macOS app the executable is
    in ``Contents/MacOS``, and the installer package keeps its marker in
    ``Contents/Resources``, inside what the signature covers.
    """
    if executable is None:
        if not getattr(sys, "frozen", False):
            return ""
        executable = sys.executable
    path = Path(executable)
    markers = [path.with_name("lumi-install.json")]
    if path.parent.name == "MacOS":
        markers.append(path.parent.parent / "Resources" / "lumi-install.json")
    for marker in markers:
        try:
            data = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        return str(data.get("installer") or "") if isinstance(data, dict) else ""
    return ""


def platform_name() -> str:
    """The feeds this copy reads: ``macos``, ``windows``, or ``linux``."""
    if sys.platform == "darwin":
        return "macos"
    return "windows" if sys.platform == "win32" else "linux"


def refusal_for(version: str, channel: str, pin: str) -> str:
    """Why a copy on ``channel``, or pinned to ``pin``, doesn't take ``version``; "" when it does.

    What choosing the feed means, checked on the version itself: a pin takes
    only stable releases of its line, the stable channel only stable
    releases, and the beta channel anything. A version Lumi can't read is
    refused wherever there's something to check.
    """
    if not pin and channel == "beta":
        return ""
    text = str(version or "").strip().removeprefix("v")
    match = _RELEASE.fullmatch(text)
    if not match:
        where = f"the {pin} release line" if pin else "the stable channel"
        return f"Lumi can't tell what release {text or 'this update'} is, so this copy on {where} doesn't take it."
    major, minor, _patch, prerelease, _number = match.groups()
    if pin:
        if prerelease or f"{int(major)}.{int(minor)}" != pin:
            return (f"Lumi {text} isn't a stable release of the {pin} line this copy stays on "
                    "(Settings > Updates).")
        return ""
    if prerelease:
        return f"Lumi {text} is a beta, and this copy takes stable releases (Settings > Updates > Channel)."
    return ""


def feed_name(channel: str, pin: str, platform: str = "windows") -> str:
    """The feed file for a channel or pin: ``appcast.xml`` and its kin, or the macOS ones."""
    prefix = "appcast-macos" if platform == "macos" else "appcast"
    if pin:
        return f"{prefix}-{pin}.xml"
    return f"{prefix}-beta.xml" if channel == "beta" else f"{prefix}.xml"


@dataclass(frozen=True)
class UpdatePreferences:
    """The update settings in effect, and who decided them."""

    mode: str = "automatic"
    channel: str = "stable"
    pin: str = ""
    managed_by: str = ""  # the organization whose policy sets any of these
    locked: tuple[str, ...] = ()  # which keys the policy sets
    problems: tuple[str, ...] = field(default=())  # stored values that were ignored, and why
    installed_by: str = ""  # a MANAGED_INSTALLERS name: that updates this copy
    offline: str = ""  # why offline mode keeps the updater from the update site, or ""
    # Whose feeds (PLATFORMS): the running copy's unless a caller says otherwise.
    platform: str = field(default_factory=lambda: platform_name())

    @property
    def feed_url(self) -> str:
        return FEED_BASE + feed_name(self.channel, self.pin, self.platform)

    def describe(self) -> str:
        """A short phrase for the feed, e.g. "the 0.20 release line"."""
        if self.pin:
            return f"the {self.pin} release line"
        return "the beta channel" if self.channel == "beta" else "the stable channel"

    def as_dict(self) -> dict[str, Any]:
        return {"mode": self.mode, "channel": self.channel, "pin": self.pin, "feed": self.feed_url,
                "describe": self.describe(), "managed_by": self.managed_by, "locked": list(self.locked),
                "problems": list(self.problems), "installed_by": self.installed_by, "offline": self.offline,
                "platform": self.platform}


def read(settings_path: Path | None = None, policy_state: Any = None,
         installer: str | None = None) -> UpdatePreferences:
    """The update settings in effect: the policy's, else settings.json's, else the defaults.

    An invalid policy pauses automatic updates (``manual``): the administrator
    may have meant to turn them off, and a person can still check by hand.
    An MSI, PKG, deb or rpm installation turns them off whatever the settings say.
    """
    from . import policy as policy_module
    from .paths import state_home

    stored: dict[str, Any] = {}
    path = settings_path or state_home() / "settings.json"
    try:
        section = json.loads(path.read_text(encoding="utf-8")).get("updates")
        stored = section if isinstance(section, dict) else {}
    except (OSError, ValueError, AttributeError):
        stored = {}
    state = policy_state if policy_state is not None else policy_module.load()
    policy = getattr(state, "policy", None)
    locked = {name.split(".", 1)[1]: value for name, value in (policy.settings if policy else {}).items()
              if name.startswith("updates.")}
    values, problems = dict(DEFAULTS), []
    for key in DEFAULTS:
        if key in locked:
            values[key] = normalize(key, locked[key])  # validated when the policy loaded
        elif key in stored:
            try:
                values[key] = normalize(key, stored[key])
            except ValueError as exc:
                problems.append(str(exc))
    if getattr(state, "error", "") and values["mode"] == "automatic":
        values["mode"] = "manual"
        problems.append("The organization policy is invalid, so automatic updates are paused.")
    source = installed_by() if installer is None else installer
    if source in MANAGED_INSTALLERS:
        values["mode"] = "off"
    platform = platform_name()
    offline_reason = ""
    if values["mode"] != "off":
        from . import offline

        offline_reason = offline.refusal(FEED_BASE + feed_name(values["channel"], values["pin"], platform),
                                         "the update check", offline.read(path, state))
    return UpdatePreferences(mode=values["mode"], channel=values["channel"], pin=values["pin"],
                             managed_by=policy.organization if (policy and locked) else "",
                             locked=tuple(sorted(locked)), problems=tuple(problems), installed_by=source,
                             offline=offline_reason, platform=platform)


def main(argv: list[str] | None = None) -> int:
    """``lumi updates``: print the update settings in effect as JSON.

    For administrators and detection scripts; it reads settings, the policy
    and the install marker, and never checks for updates. ``lumi updates
    verify <file>`` checks an offline update bundle as the updater would
    (lumi/update_file.py), without installing it.
    """
    from . import __version__

    if argv and argv[0] == "verify" and len(argv) == 2:
        from .update_file import UpdateFileError, verify

        try:
            update = verify(argv[1])
        except UpdateFileError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        print(json.dumps(update.summary(), indent=2))
        return 0
    if argv:
        print("usage: lumi updates [verify <installer, folder or .zip>]", file=sys.stderr)
        return 2
    print(json.dumps({"version": __version__, **read().as_dict()}, indent=2))
    return 0
