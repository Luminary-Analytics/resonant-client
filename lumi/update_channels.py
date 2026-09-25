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
update site; ``packaging/update_appcast.py`` writes them. WinSparkle takes a
feed URL, not a filter, so choosing the feed is how the choice is enforced.

A copy installed from the MSI package (packaging/lumi.wxs) never updates
itself: the device management that installed it does, and the two
installers would otherwise fight over the same Program Files folder. The package puts
``lumi-install.json`` beside ``lumi.exe`` to say so; it wins over everything.

Like the policy, these are read once at startup (``read``): a change applies
the next time Lumi starts. ``read`` parses settings.json itself rather than
constructing a ``SettingsManager``, which would write the file and move keys
into the credential store before the app has started.
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
DEFAULTS = {"mode": "automatic", "channel": "stable", "pin": ""}
_PIN = re.compile(r"(0|[1-9]\d{0,3})\.(0|[1-9]\d{0,3})")


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
    """``"msi"`` when this copy came from the MSI package, else ``""``."""
    if executable is None:
        if not getattr(sys, "frozen", False):
            return ""
        executable = sys.executable
    try:
        data = json.loads(Path(executable).with_name("lumi-install.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    return str(data.get("installer") or "") if isinstance(data, dict) else ""


def feed_name(channel: str, pin: str) -> str:
    if pin:
        return f"appcast-{pin}.xml"
    return "appcast-beta.xml" if channel == "beta" else "appcast.xml"


@dataclass(frozen=True)
class UpdatePreferences:
    """The update settings in effect, and who decided them."""

    mode: str = "automatic"
    channel: str = "stable"
    pin: str = ""
    managed_by: str = ""  # the organization whose policy sets any of these
    locked: tuple[str, ...] = ()  # which keys the policy sets
    problems: tuple[str, ...] = field(default=())  # stored values that were ignored, and why
    installed_by: str = ""  # "msi": device management updates this copy

    @property
    def feed_url(self) -> str:
        return FEED_BASE + feed_name(self.channel, self.pin)

    def describe(self) -> str:
        """A short phrase for the feed, e.g. "the 0.20 release line"."""
        if self.pin:
            return f"the {self.pin} release line"
        return "the beta channel" if self.channel == "beta" else "the stable channel"

    def as_dict(self) -> dict[str, Any]:
        return {"mode": self.mode, "channel": self.channel, "pin": self.pin, "feed": self.feed_url,
                "describe": self.describe(), "managed_by": self.managed_by, "locked": list(self.locked),
                "problems": list(self.problems), "installed_by": self.installed_by}


def read(settings_path: Path | None = None, policy_state: Any = None,
         installer: str | None = None) -> UpdatePreferences:
    """The update settings in effect: the policy's, else settings.json's, else the defaults.

    An invalid policy pauses automatic updates (``manual``): the administrator
    may have meant to turn them off, and a person can still check by hand.
    An MSI installation turns them off whatever the settings say.
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
    if source == "msi":
        values["mode"] = "off"
    return UpdatePreferences(mode=values["mode"], channel=values["channel"], pin=values["pin"],
                             managed_by=policy.organization if (policy and locked) else "",
                             locked=tuple(sorted(locked)), problems=tuple(problems), installed_by=source)


def main(argv: list[str] | None = None) -> int:
    """``lumi updates``: print the update settings in effect as JSON.

    For administrators and detection scripts; it reads settings, the policy
    and the install marker, and never checks for updates.
    """
    from . import __version__

    if argv:
        print("usage: lumi updates", file=sys.stderr)
        return 2
    print(json.dumps({"version": __version__, **read().as_dict()}, indent=2))
    return 0
