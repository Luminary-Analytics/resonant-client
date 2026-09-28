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
it), so the signature on the installer is what makes it genuine, and the
version checked is the one inside the signed installer (its Windows version
resource, which packaging/installer.iss sets to the release), never only the
feed's: an old signed installer listed as a new version is refused. The feed
is parsed without a document type, so it can't declare entities.

Installing (``install``) hands the verified installer to the same flow
WinSparkle uses: never while an agent turn runs, an ``update.install``
record in the audit log, the installer started (Windows asks for
administrator rights, as for a downloaded update), and Lumi closed so the
installer can replace its files. It runs a copy of exactly the bytes that
were verified, saved under a name Lumi makes (``lumi-setup-<version>.exe``)
in a new private folder, whatever the bundle called it. Only an installed
copy of Lumi on Windows updates itself this way; elsewhere ``instructions``
says what to do instead.
"""

from __future__ import annotations

import base64
import hashlib
import os
import re
import struct
import sys
import tempfile
import xml.etree.ElementTree as ET
import xml.parsers.expat
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
# The feeds list only the setup program (packaging/update_appcast.py).
_INSTALLER_SUFFIXES = (".exe",)
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


def _refuse_document_type(*_args: Any) -> None:
    raise UpdateFileError("The update feed declares a document type or entities, which update feeds don't.")


def _parse_feed(feed: bytes) -> ET.Element:
    """A feed's XML, refusing a document type as the parser meets it.

    The parser reads the feed in whatever encoding it declares (UTF-16
    included), so a document type can't hide from the check, and without one
    there are no entities to expand.
    """
    builder = ET.TreeBuilder()
    parser = xml.parsers.expat.ParserCreate(namespace_separator="}")
    parser.SetParamEntityParsing(xml.parsers.expat.XML_PARAM_ENTITY_PARSING_NEVER)
    parser.StartDoctypeDeclHandler = _refuse_document_type
    parser.EntityDeclHandler = _refuse_document_type
    parser.ExternalEntityRefHandler = _refuse_document_type
    parser.buffer_text = True

    def name(raw: str) -> str:  # expat's "uri}local" is ElementTree's "{uri}local"
        return "{" + raw if "}" in raw else raw

    parser.StartElementHandler = lambda tag, attributes: builder.start(
        name(tag), {name(key): value for key, value in attributes.items()})
    parser.EndElementHandler = lambda tag: builder.end(name(tag))
    parser.CharacterDataHandler = builder.data
    try:
        parser.Parse(feed, True)
        return builder.close()
    except (xml.parsers.expat.ExpatError, AssertionError) as exc:  # TreeBuilder asserts on an empty document
        raise UpdateFileError(f"The update feed isn't valid XML: {exc}") from None


def _items(feed: bytes) -> list[dict[str, str]]:
    """The releases a feed lists: version, installer file name, size and signature."""
    root = _parse_feed(feed)
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


# ── The version inside a signed installer ──────────────────────────────────
#
# The feed's version is only a claim: the feed isn't signed. The version the
# signature vouches for is the one in the installer's Windows version
# resource (ProductVersion, which packaging/installer.iss sets to the
# release), read here without Windows so every platform checks it the same.

_RT_VERSION = 16


def _unpack(layout: str, data: bytes, offset: int) -> tuple:
    if offset < 0 or offset + struct.calcsize(layout) > len(data):
        raise ValueError("it ends early")
    return struct.unpack_from(layout, data, offset)


def _align4(offset: int) -> int:
    return (offset + 3) & ~3


def pe_resources(data: bytes, type_id: int) -> list[bytes]:
    """The resources of one type (``16``: version information) in a Windows program; ValueError if unreadable."""
    if data[:2] != b"MZ":
        raise ValueError("it isn't a Windows program")
    (header,) = _unpack("<I", data, 0x3C)
    if data[header:header + 4] != b"PE\0\0":
        raise ValueError("it isn't a Windows program")
    _machine, section_count, _stamp, _symbols, _symbol_count, optional_size, _flags = _unpack(
        "<HHIIIHH", data, header + 4)
    optional = header + 24
    (magic,) = _unpack("<H", data, optional)
    directories = {0x10B: optional + 96, 0x20B: optional + 112}.get(magic)
    if directories is None:
        raise ValueError("it isn't a Windows program")
    (directory_count,) = _unpack("<I", data, directories - 4)
    resource_rva = _unpack("<I", data, directories + 2 * 8)[0] if directory_count > 2 else 0
    if not resource_rva:
        return []
    sections = [_unpack("<IIII", data, optional + optional_size + 40 * index + 8)
                for index in range(min(section_count, 96))]

    def file_offset(rva: int, size: int) -> int:
        for _virtual_size, virtual_address, raw_size, raw_pointer in sections:
            delta = rva - virtual_address
            if 0 <= delta and delta + size <= raw_size:
                return raw_pointer + delta
        raise ValueError("its resources are out of place")

    base = file_offset(resource_rva, 16)

    def entries(directory: int) -> list[tuple[int, int]]:
        named, numbered = _unpack("<HH", data, base + directory + 12)
        return [_unpack("<II", data, base + directory + 16 + 8 * index) for index in range(min(named + numbered, 4096))]

    found = []
    # Three levels: type, then name, then language; a leaf gives the data's address and size.
    for kind, names in entries(0):
        if kind != type_id or not names & 0x80000000:
            continue
        for _name, languages in entries(names & 0x7FFFFFFF):
            if not languages & 0x80000000:
                continue
            for _language, leaf in entries(languages & 0x7FFFFFFF):
                if leaf & 0x80000000:
                    continue
                rva, size, _code_page, _reserved = _unpack("<IIII", data, base + leaf)
                start = file_offset(rva, size)
                found.append(data[start:start + size])
    return found


def _blocks(resource: bytes, start: int, end: int) -> list[tuple[str, int, int, int]]:
    """(key, value offset, children offset, end) of each VS_VERSIONINFO block between ``start`` and ``end``."""
    blocks = []
    position = start
    while position + 6 <= end:
        length, value_length, value_type = _unpack("<HHH", resource, position)
        if length == 0:
            break  # padding
        if length < 6 or position + length > end:
            raise ValueError("its version information is malformed")
        block_end = position + length
        key_end = position + 6
        while key_end + 2 <= block_end and resource[key_end:key_end + 2] != b"\0\0":
            key_end += 2
        key = resource[position + 6:key_end].decode("utf-16-le", "replace")
        value = _align4(key_end + 2)
        # A text value's length counts characters, a binary one's bytes; children follow the value.
        children = _align4(value + (value_length * 2 if value_type == 1 else value_length))
        blocks.append((key, value, children, block_end))
        position = _align4(block_end)
    return blocks


def version_strings(resource: bytes, wanted: str) -> list[str]:
    """The values of one string, such as ``ProductVersion``, in a version resource (every language)."""
    values = []
    for root, _value, children, end in _blocks(resource, 0, len(resource)):
        if root != "VS_VERSION_INFO":
            continue
        for section, _value, tables, section_end in _blocks(resource, children, end):
            if section != "StringFileInfo":
                continue
            for _table, _value, strings, table_end in _blocks(resource, tables, section_end):
                for key, value, _children, string_end in _blocks(resource, strings, table_end):
                    if key == wanted:
                        values.append(resource[value:string_end].decode("utf-16-le", "replace").split("\0", 1)[0])
    return values


def installer_version(data: bytes) -> str:
    """The release a Lumi installer installs (its ProductVersion); ValueError when it doesn't say."""
    found = {text.strip() for resource in pe_resources(data, _RT_VERSION)
             for text in version_strings(resource, "ProductVersion") if text.strip()}
    if len(found) != 1:
        raise ValueError("it doesn't say which version it installs" if not found
                         else "it names more than one version")
    version = found.pop()
    version_key(version)  # ValueError for anything but a release version
    return version


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
    # The signature vouches for the installer's bytes, not for the feed: the
    # version that counts is the one inside the installer.
    try:
        version = installer_version(data)
    except ValueError as exc:
        raise UpdateFileError(f"Lumi can't tell which version {name} installs ({exc}), so it can't check it. "
                              "Nothing was installed.") from None
    try:
        listed_as = version_key(item["version"])
    except ValueError:
        listed_as = None
    if listed_as != version_key(version):
        raise UpdateFileError(f"The update feed lists {name} as Lumi {item['version'] or '(no version)'}, but the "
                              f"signed installer is Lumi {version}. Use the feed downloaded with this installer.")
    try:
        newer = version_key(version) > version_key(current_version)
    except ValueError:
        raise UpdateFileError(f"This copy's version ({current_version}) isn't a release version, so Lumi can't "
                              "tell whether the update is newer.") from None
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
    """What to do where Lumi doesn't install updates from a file (macOS and Linux)."""
    if sys.platform == "darwin":
        return ("On macOS Lumi installs only the updates it downloads itself (Settings > Updates), not a file. "
                "Open the new lumi-X.Y.Z.dmg and drag Lumi to Applications to replace it, or install the new "
                "lumi-X.Y.Z.pkg with your device management or `sudo installer -pkg lumi-X.Y.Z.pkg -target /` "
                "(check it first with `pkgutil --check-signature`).")
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
    # A name Lumi makes, never the bundle's: a .zip entry such as "C:setup.exe"
    # or "..\setup.exe" would put the copy somewhere else.
    try:
        plain = version_key(update.version) and re.fullmatch(r"[0-9A-Za-z][0-9A-Za-z.-]{0,63}", update.version)
    except ValueError:
        plain = None
    if not plain:
        raise UpdateFileError(f"{update.version!r} isn't a release version; nothing was installed.")
    target = target_folder / f"lumi-setup-{update.version}.exe"
    if target.parent != target_folder:
        raise UpdateFileError("Lumi couldn't prepare the installer; nothing was installed.")
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
