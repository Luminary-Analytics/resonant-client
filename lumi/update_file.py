"""Install an update from a file: for computers without the internet (offline mode).

An offline update bundle is what the online updater downloads, carried by
hand: the Windows installer (``lumi-setup-X.Y.Z.exe``) and the update feed
that lists it (``appcast.xml``, or the beta or release-line feed), both from
the update site. They can be side by side in a folder, or in one .zip.

Verification is the online updater's (WinSparkle's), with the same key:

* the feed lists this installer (by file name), with its version, size and
  ``sparkle:edSignature``;
* the installer's bytes carry a valid Ed25519 (EdDSA) signature by
  ``updater.EDDSA_PUBLIC_KEY``, the key the app is built with; nothing else
  can make it valid, so a changed or foreign installer is refused;
* the size matches, and the version is newer than this one, on the channel
  or release line Settings > Updates (or the policy) chooses, as a check
  would offer it. With updates off, or on a copy installed from the MSI, a
  PKG, a .deb or an .rpm, the organization or package manager updates Lumi,
  so a file is refused too.

The feed itself isn't signed (the online updater trusts the update site for
it), so the signature on the installer is what makes it genuine.

Installing (``install``) hands the verified installer to the same flow
WinSparkle uses: never while an agent turn runs, an ``update.install``
record in the audit log, the installer started (Windows asks for
administrator rights, as for a downloaded update), and Lumi closed so the
installer can replace its files. It runs a copy of exactly the bytes that
were verified. Only an installed copy of Lumi on Windows updates itself this
way; elsewhere ``instructions`` says what to do instead.
"""

from __future__ import annotations

import base64
import hashlib
import os
import re
import sys
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Callable
from urllib.parse import unquote, urlsplit

from . import __version__

SPARKLE_NS = "http://www.andymatuschak.org/xml-namespaces/sparkle"
MAX_FEED_BYTES = 4 * 1024 * 1024
MAX_INSTALLER_BYTES = 1024 * 1024 * 1024
_FEED_NAME = re.compile(r"appcast(-[A-Za-z0-9.]+)?\.xml", re.IGNORECASE)
_INSTALLER_SUFFIXES = (".exe", ".msi")
_RELEASE = re.compile(r"(\d+)\.(\d+)\.(\d+)(?:[.-]?(dev|alpha|a|beta|b|rc)\.?(\d+))?", re.IGNORECASE)
_PRE_RANK = {"dev": 0, "alpha": 1, "a": 1, "beta": 2, "b": 2, "rc": 3}


class UpdateFileError(ValueError):
    """An update file Lumi won't install; the message says why and what to do."""


@dataclass(frozen=True)
class VerifiedUpdate:
    """An installer whose signature, size and version checked out."""

    version: str
    installer_name: str
    size: int
    sha256: str
    source: str  # the file or folder it came from
    feed: str  # the feed file that listed it
    prerelease: bool
    data: bytes  # exactly the verified bytes

    def summary(self) -> dict[str, Any]:
        return {"version": self.version, "installer": self.installer_name, "size": self.size,
                "sha256": self.sha256, "source": self.source, "feed": self.feed, "prerelease": self.prerelease,
                "current": __version__, "signature": "valid"}


def version_key(text: str) -> tuple[int, ...]:
    """Sort key for release versions (feed ``0.21.0-beta.1``, app ``0.19.2.dev11``); ValueError otherwise."""
    match = _RELEASE.fullmatch(str(text or "").strip().removeprefix("v"))
    if not match:
        raise ValueError(f"{text!r} isn't a release version")
    major, minor, patch, pre, number = match.groups()
    tail = (0, _PRE_RANK[pre.lower()], int(number)) if pre else (1, 0, 0)
    return (int(major), int(minor), int(patch), *tail)


def _is_prerelease(version: str) -> bool:
    return version_key(version)[3] == 0


# ── Reading a bundle ────────────────────────────────────────────────────────


def _read_limited(path: Path, limit: int, what: str) -> bytes:
    try:
        size = path.stat().st_size
        if size > limit:
            raise UpdateFileError(f"The {what} is larger than an update can be.")
        return path.read_bytes()
    except OSError as exc:
        raise UpdateFileError(f"The {what} couldn't be read: {exc}") from exc


def _bundle(path: Path) -> tuple[str, bytes, list[tuple[str, bytes]]]:
    """(installer name, installer bytes, [(feed name, feed bytes)]) from a file, folder or .zip."""
    if not path.exists():
        raise UpdateFileError(f"{path} doesn't exist.")
    if path.is_dir():
        installers = sorted(p for p in path.iterdir() if p.is_file() and p.suffix.lower() in _INSTALLER_SUFFIXES)
        if len(installers) != 1:
            raise UpdateFileError("The folder should hold one installer (lumi-setup-X.Y.Z.exe) and its update feed "
                                  f"(appcast.xml); it holds {len(installers)} installers.")
        return _bundle_from(installers[0], path)
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            members = [m for m in archive.infolist() if not m.is_dir()]
            installers = [m for m in members if PurePosixPath(m.filename).suffix.lower() in _INSTALLER_SUFFIXES]
            if len(installers) != 1:
                raise UpdateFileError("The .zip should hold one installer and its update feed "
                                      f"(appcast.xml); it holds {len(installers)} installers.")
            if len(members) > 50:
                raise UpdateFileError("The .zip holds more files than an update bundle does.")
            feeds = [m for m in members if _FEED_NAME.fullmatch(PurePosixPath(m.filename).name)]
            if not feeds:
                raise UpdateFileError("The .zip doesn't hold the update feed (appcast.xml) that lists the installer.")
            installer = installers[0]
            if installer.file_size > MAX_INSTALLER_BYTES or any(m.file_size > MAX_FEED_BYTES for m in feeds):
                raise UpdateFileError("The .zip holds files larger than an update can be.")
            return (PurePosixPath(installer.filename).name, archive.read(installer),
                    [(PurePosixPath(m.filename).name, archive.read(m)) for m in feeds])
    if path.suffix.lower() in _INSTALLER_SUFFIXES:
        return _bundle_from(path, path.parent)
    raise UpdateFileError("Choose the installer (lumi-setup-X.Y.Z.exe), the folder that holds it and its update "
                          "feed, or a .zip of both.")


def _bundle_from(installer: Path, folder: Path) -> tuple[str, bytes, list[tuple[str, bytes]]]:
    feeds = sorted(p for p in folder.iterdir() if p.is_file() and _FEED_NAME.fullmatch(p.name))
    if not feeds:
        raise UpdateFileError(f"No update feed (appcast.xml) next to {installer.name}. Download it from the update "
                              "site with the installer and keep them together.")
    return (installer.name, _read_limited(installer, MAX_INSTALLER_BYTES, "installer"),
            [(p.name, _read_limited(p, MAX_FEED_BYTES, "update feed")) for p in feeds])


def _items(feed: bytes) -> list[dict[str, str]]:
    """The releases a feed lists: version, installer file name, size and signature."""
    if b"<!DOCTYPE" in feed[:4096].upper() or b"<!ENTITY" in feed.upper():
        raise UpdateFileError("The update feed declares a DTD or entities, which update feeds don't.")
    try:
        root = ET.fromstring(feed)
    except ET.ParseError as exc:
        raise UpdateFileError(f"The update feed isn't valid XML: {exc}") from exc
    releases = []
    for item in root.iter("item"):
        enclosure = item.find("enclosure")
        if enclosure is None:
            continue
        url = enclosure.get("url") or ""
        version = (enclosure.get(f"{{{SPARKLE_NS}}}version") or item.findtext(f"{{{SPARKLE_NS}}}version") or "")
        releases.append({
            "version": version.strip(),
            "name": unquote(PurePosixPath(urlsplit(url).path).name),
            "length": (enclosure.get("length") or "").strip(),
            "signature": (enclosure.get(f"{{{SPARKLE_NS}}}edSignature") or "").strip(),
        })
    return releases


def _verify_signature(data: bytes, signature_b64: str, public_key_b64: str) -> bool:
    """Ed25519 over the installer's bytes, as WinSparkle checks ``sparkle:edSignature``."""
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    try:
        key = Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key_b64, validate=True))
        key.verify(base64.b64decode(signature_b64, validate=True), data)
        return True
    except (InvalidSignature, ValueError):
        return False


# ── Verifying ───────────────────────────────────────────────────────────────


def verify(path: str | os.PathLike, *, preferences: Any = None, current_version: str = __version__,
           public_key: str | None = None) -> VerifiedUpdate:
    """Check an offline update bundle as the online updater checks a download; UpdateFileError if not.

    ``preferences`` is the update settings in effect (update_channels.read);
    ``public_key`` defaults to the key the app is built with (tests pass their own).
    """
    from .update_channels import MANAGED_INSTALLERS, read as read_preferences
    from .updater import EDDSA_PUBLIC_KEY

    prefs = preferences if preferences is not None else read_preferences()
    installer_source = getattr(prefs, "installed_by", "")
    if installer_source in MANAGED_INSTALLERS:
        package, updater_name = MANAGED_INSTALLERS[installer_source]
        raise UpdateFileError(f"This copy was installed from {package}, so {updater_name} updates it.")
    if getattr(prefs, "mode", "") == "off":
        who = getattr(prefs, "managed_by", "") if "mode" in (getattr(prefs, "locked", ()) or ()) else ""
        raise UpdateFileError(f"Updates are turned off{' by ' + who if who else ' in Settings > Updates'}, so "
                              "Lumi doesn't install them, from a file either.")
    source = Path(path)
    name, data, feeds = _bundle(source)
    listed = [(feed_name, item) for feed_name, feed in feeds for item in _items(feed)
              if item["name"].lower() == name.lower()]
    if not listed:
        raise UpdateFileError(f"The update feed doesn't list {name}. Use the feed downloaded with this installer.")
    signed = [(feed_name, item) for feed_name, item in listed if item["signature"]]
    if not signed:
        raise UpdateFileError(f"The update feed lists {name} without a signature, so it can't be installed.")
    key = public_key or EDDSA_PUBLIC_KEY
    match = next(((feed_name, item) for feed_name, item in signed
                  if _verify_signature(data, item["signature"], key)), None)
    if match is None:
        raise UpdateFileError(f"{name}'s signature doesn't match: it isn't a Lumi release signed by Luminary "
                              "Analytics, or it was changed after signing. Nothing was installed.")
    feed_name, item = match
    if item["length"] and item["length"] != str(len(data)):
        raise UpdateFileError(f"{name} is {len(data)} bytes, but its feed says {item['length']}.")
    version = item["version"]
    try:
        newer = version_key(version) > version_key(current_version)
    except ValueError:
        raise UpdateFileError(f"The update feed gives {name} the version {version!r}, which isn't a release "
                              "version.") from None
    if not newer:
        raise UpdateFileError(f"{name} is Lumi {version}, which isn't newer than this copy ({current_version}).")
    pin = getattr(prefs, "pin", "")
    prerelease = _is_prerelease(version)
    if pin and (prerelease or ".".join(str(part) for part in version_key(version)[:2]) != pin):
        raise UpdateFileError(f"Lumi {version} isn't a stable release of the {pin} line this copy stays on "
                              "(Settings > Updates).")
    if prerelease and not pin and getattr(prefs, "channel", "stable") != "beta":
        raise UpdateFileError(f"Lumi {version} is a beta, and this copy takes stable releases "
                              "(Settings > Updates > Channel).")
    return VerifiedUpdate(version=version, installer_name=name, size=len(data),
                          sha256=hashlib.sha256(data).hexdigest(), source=str(source), feed=feed_name,
                          prerelease=prerelease, data=data)


# ── Installing ──────────────────────────────────────────────────────────────


def can_install_here() -> str:
    """Why this copy can't install an update from a file, or ``""`` when it can."""
    if sys.platform != "win32":
        return instructions()
    if not getattr(sys, "frozen", False):
        return ("This copy runs from source, so an installer wouldn't update it. Verify the file here, then "
                "run the installer on the computer where Lumi is installed.")
    return ""


def instructions() -> str:
    """What to do where Lumi doesn't install updates itself (macOS and Linux)."""
    if sys.platform == "darwin":
        return ("On macOS Lumi doesn't install updates itself. Install the new lumi-X.Y.Z.pkg with your device "
                "management or `sudo installer -pkg lumi-X.Y.Z.pkg -target /` (check it first with "
                "`pkgutil --check-signature`), or replace Lumi.app from the new .dmg.")
    return ("On Linux Lumi doesn't install updates itself. Install the new package with your package manager "
            "(`sudo apt install ./lumi_X.Y.Z_amd64.deb` or `sudo dnf install ./lumi-X.Y.Z-1.x86_64.rpm`), or "
            "replace the AppImage or tarball.")


def install(update: VerifiedUpdate, *, busy: Callable[[], bool] | None = None,
            shutdown: Callable[[], Any] | None = None, launcher: Callable[[str], Any] | None = None,
            folder: Path | None = None) -> str:
    """Run a verified installer the way the online updater does; returns the installer's path.

    Refused while an agent turn runs (``busy``), like WinSparkle's "can Lumi
    close now?". The verified bytes are written to a new private folder and
    that copy is started; then Lumi closes (``shutdown``) so the installer can
    replace its files. ``busy`` and ``shutdown`` default to the app's own
    (updater.set_host). ``launcher`` replaces ``os.startfile`` for tests, and
    with it the check that this is an installed copy on Windows.
    """
    from . import audit, updater

    problem = can_install_here() if launcher is None else ""
    if problem:
        raise UpdateFileError(problem)
    app_busy, app_shutdown = updater.host()
    busy = busy if busy is not None else app_busy
    try:
        running = bool(busy()) if callable(busy) else False
    except Exception:
        running = True
    if running:
        audit.record("update.deferred", version=__version__, to_version=update.version, source="file",
                     reason="an agent turn is running")
        raise UpdateFileError("An agent turn is running. Install the update once it finishes.")
    if folder is not None:
        target_folder = Path(folder)
        target_folder.mkdir(parents=True, exist_ok=True)
    else:
        target_folder = Path(tempfile.mkdtemp(prefix="lumi-update-"))  # private to this user
    target = target_folder / update.installer_name
    with open(target, "xb") as handle:
        handle.write(update.data)
    if hashlib.sha256(target.read_bytes()).hexdigest() != update.sha256:
        raise UpdateFileError("The installer changed while it was being prepared; nothing was installed.")
    audit.record("update.install", version=__version__, to_version=update.version, source="file",
                 installer=update.installer_name, sha256=update.sha256)
    (launcher or os.startfile)(str(target))  # type: ignore[attr-defined]  # ShellExecute: elevation as needed
    close = shutdown if shutdown is not None else app_shutdown
    if callable(close):
        close()
    return str(target)
