"""Installing an update from a file (lumi/update_file.py): verified as the online updater verifies downloads.

The feeds here are written by packaging/update_appcast.py, the code the
release workflow uses, and signed with keys the tests generate. Nothing is
downloaded and no installer runs.
"""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import re
import struct
import sys
import zipfile
from pathlib import Path
from urllib.parse import quote

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from lumi import update_file
from lumi.update_channels import UpdatePreferences
from lumi.update_file import UpdateFileError, verify

ROOT = Path(__file__).resolve().parent.parent
CURRENT = "0.19.2.dev11"


def _appcast_module():
    spec = importlib.util.spec_from_file_location("update_appcast", ROOT / "packaging" / "update_appcast.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _public(key: Ed25519PrivateKey) -> str:
    raw = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return base64.b64encode(raw).decode()


def _block(key: str, value: bytes = b"", children: list[bytes] = (), *, text: bool = True) -> bytes:
    """One block of a Windows version resource, laid out as resource compilers (and Inno Setup) write it."""
    body = b"\0" * 6 + key.encode("utf-16-le") + b"\0\0"
    body += b"\0" * (-len(body) % 4) + value
    for child in children:
        body += b"\0" * (-len(body) % 4) + child
    return struct.pack("<HHH", len(body), len(value) // 2 if text else len(value), 1 if text else 0) + body[6:]


def fake_installer(product_version: str | None, *, bits: int = 32, payload: bytes = b"fake Lumi setup data " * 800):
    """A minimal Windows program whose version resource says ``product_version`` (None: no resource).

    Inno Setup's installer is such a program with the setup data after it;
    its ProductVersion is the release, padded with spaces.
    """
    resource = b""
    if product_version is not None:
        fixed = struct.pack("<13I", 0xFEEF04BD, 0x10000, 0, 0, 0, 0, 0x3F, 0, 0x4, 0x1, 0, 0, 0)
        strings = [_block(name, (value + "\0").encode("utf-16-le")) for name, value in (
            ("CompanyName", "Luminary Analytics"), ("ProductName", "Lumi"),
            ("ProductVersion", product_version.ljust(50)))]
        table = _block("040904b0", children=strings)
        translation = _block("Translation", struct.pack("<HH", 0x409, 0x4B0), text=False)
        resource = _block("VS_VERSION_INFO", fixed, [_block("StringFileInfo", children=[table]),
                                                     _block("VarFileInfo", children=[translation])], text=False)
    rva = 0x1000
    # The resource tree: type 16 (version) > name 1 > language 0x409 > the data's address and size.
    tree = b"".join(struct.pack("<IIHHHHII", 0, 0, 0, 0, 0, 1, entry, target) for entry, target in (
        (16, 0x80000000 | 0x18), (1, 0x80000000 | 0x30), (0x409, 0x48)))
    section = tree + struct.pack("<IIII", rva + 0x58, len(resource), 0, 0) + resource if resource else b"\0" * 16
    raw_size = len(section) + (-len(section) % 0x200)
    optional_size = 224 if bits == 32 else 240
    optional = bytearray(optional_size)
    struct.pack_into("<H", optional, 0, 0x10B if bits == 32 else 0x20B)
    directories = 96 if bits == 32 else 112
    struct.pack_into("<I", optional, directories - 4, 16)
    if resource:
        struct.pack_into("<II", optional, directories + 16, rva, len(section))
    headers = bytearray(0x40)
    headers[:2] = b"MZ"
    struct.pack_into("<I", headers, 0x3C, 0x40)
    headers += b"PE\0\0" + struct.pack("<HHIIIHH", 0x14C if bits == 32 else 0x8664, 1, 0, 0, 0, optional_size, 0x102)
    headers += optional + b".rsrc\0\0\0" + struct.pack("<IIIIIIHHI", len(section), rva, raw_size, 0x200, 0, 0, 0, 0,
                                                        0x40000040)
    return bytes(headers.ljust(0x200, b"\0")) + section.ljust(raw_size, b"\0") + payload


class Release:
    """An installer and the feed that lists it, as the release workflow publishes them."""

    def __init__(self, folder: Path, *, version: str = "0.21.0", key: Ed25519PrivateKey | None = None,
                 content: bytes | None = None, inside: str | None = ""):
        """``inside`` is the version the installer itself names ("" for ``version``, None for none)."""
        if content is None:
            content = fake_installer(version if inside == "" else inside)
        self.folder = folder
        self.key = key or Ed25519PrivateKey.generate()
        folder.mkdir(parents=True, exist_ok=True)
        self.installer = folder / f"lumi-setup-{version}.exe"
        self.installer.write_bytes(content)
        signature = base64.b64encode(self.key.sign(content)).decode()
        (folder / "appcast.xml").write_text(
            '<?xml version="1.0" encoding="utf-8"?><rss version="2.0"><channel><title>Lumi updates</title>'
            "<description>Stable releases of Lumi</description></channel></rss>", encoding="utf-8")
        _appcast_module().publish_feeds(folder, version, self.installer, signature, "<p>Notes</p>",
                                        "https://luminary-analytics.github.io/resonant-client/downloads")
        self.version = version

    @property
    def public(self) -> str:
        return _public(self.key)


def _reencoded(feed: str, encoding: str, before_root: str = "") -> bytes:
    """A feed's text in another encoding (its declaration says which), with ``before_root`` after the declaration."""
    body = re.sub(r"^<\?xml[^>]*\?>", "", feed.lstrip("\ufeff")).lstrip()
    return (f'<?xml version="1.0" encoding="{encoding}"?>' + before_root + body).encode(encoding)


def _check(path, release: Release, prefs: UpdatePreferences | None = None, **kwargs):
    return verify(path, preferences=prefs or UpdatePreferences(), current_version=kwargs.pop("current", CURRENT),
                  public_key=kwargs.pop("public_key", release.public), **kwargs)


class TestVerification:
    def test_a_signed_release_verifies_from_the_installer_folder_or_zip(self, tmp_path):
        release = Release(tmp_path / "bundle")
        for path in (release.installer, release.folder):
            update = _check(path, release)
            assert (update.version, update.installer_name, update.size) == (
                "0.21.0", "lumi-setup-0.21.0.exe", release.installer.stat().st_size)
            assert update.sha256 == hashlib.sha256(release.installer.read_bytes()).hexdigest()
            assert update.summary()["signature"] == "valid" and update.data == release.installer.read_bytes()
        archive = tmp_path / "lumi-update-0.21.0.zip"
        with zipfile.ZipFile(archive, "w") as bundle:
            bundle.write(release.installer, f"lumi-0.21.0/{release.installer.name}")
            for feed in release.folder.glob("appcast*.xml"):
                bundle.write(feed, f"lumi-0.21.0/{feed.name}")
        assert _check(archive, release).version == "0.21.0"

    def test_a_tampered_installer_is_refused(self, tmp_path):
        release = Release(tmp_path / "bundle")
        data = bytearray(release.installer.read_bytes())
        data[100] ^= 1
        release.installer.write_bytes(bytes(data))
        with pytest.raises(UpdateFileError, match="signature doesn't match"):
            _check(release.installer, release)

    def test_an_installer_signed_with_another_key_is_refused(self, tmp_path):
        release = Release(tmp_path / "bundle")
        with pytest.raises(UpdateFileError, match="isn't a Lumi release signed by Luminary Analytics"):
            _check(release.installer, release, public_key=_public(Ed25519PrivateKey.generate()))

    def test_the_built_in_key_is_the_default(self, tmp_path, monkeypatch):
        from lumi import updater

        release = Release(tmp_path / "bundle")
        with pytest.raises(UpdateFileError, match="signature doesn't match"):
            verify(release.installer, preferences=UpdatePreferences(), current_version=CURRENT)
        monkeypatch.setattr(updater, "EDDSA_PUBLIC_KEY", release.public)
        assert verify(release.installer, preferences=UpdatePreferences(), current_version=CURRENT)

    def test_the_feed_must_list_the_installer_with_its_size(self, tmp_path):
        release = Release(tmp_path / "bundle")
        renamed = release.installer.with_name("lumi-setup-9.9.9.exe")
        release.installer.rename(renamed)
        with pytest.raises(UpdateFileError, match="doesn't list lumi-setup-9.9.9.exe"):
            _check(renamed, release)
        renamed.rename(release.installer)
        for feed in release.folder.glob("appcast*.xml"):
            feed.write_text(feed.read_text(encoding="utf-8").replace(
                f'length="{release.installer.stat().st_size}"', 'length="12"'), encoding="utf-8")
        with pytest.raises(UpdateFileError, match="but its feed says 12"):
            _check(release.installer, release)

    def test_no_feed_or_a_feed_with_a_dtd_is_refused(self, tmp_path):
        release = Release(tmp_path / "bundle")
        feeds = {feed.name: feed.read_text(encoding="utf-8") for feed in release.folder.glob("appcast*.xml")}
        for name in feeds:
            (release.folder / name).unlink()
        with pytest.raises(UpdateFileError, match="No update feed"):
            _check(release.installer, release)
        (release.folder / "appcast.xml").write_text(
            '<?xml version="1.0"?><!DOCTYPE rss [<!ENTITY x "y">]><rss/>', encoding="utf-8")
        with pytest.raises(UpdateFileError, match="declares a document type"):
            _check(release.installer, release)

    @pytest.mark.parametrize("encoding", ["UTF-16", "UTF-16LE", "UTF-16BE"])
    def test_a_document_type_is_refused_in_any_encoding(self, tmp_path, encoding):
        release = Release(tmp_path / "bundle")
        # Entities that expand a thousandfold; a search of the bytes for "<!DOCTYPE" misses them in UTF-16.
        laughs = ('<!DOCTYPE rss [<!ENTITY a "aaaaaaaaaa"><!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;">'
                  '<!ENTITY c "&b;&b;&b;&b;&b;&b;&b;&b;&b;&b;">]>')
        for feed in release.folder.glob("appcast*.xml"):
            feed.write_bytes(_reencoded(feed.read_text(encoding="utf-8"), encoding, laughs))
            assert b"<!DOCTYPE" not in feed.read_bytes()
        with pytest.raises(UpdateFileError, match="declares a document type"):
            _check(release.installer, release)

    def test_a_feed_in_utf_16_still_verifies(self, tmp_path):
        release = Release(tmp_path / "bundle")
        for feed in release.folder.glob("appcast*.xml"):
            feed.write_bytes(_reencoded(feed.read_text(encoding="utf-8"), "UTF-16"))
        assert _check(release.installer, release).version == "0.21.0"

    def test_only_a_newer_release_on_the_chosen_channel_and_line(self, tmp_path):
        release = Release(tmp_path / "bundle", version="0.21.0")
        with pytest.raises(UpdateFileError, match="isn't newer than this copy"):
            _check(release.installer, release, current="0.21.0")
        with pytest.raises(UpdateFileError, match="stable release of the 0.20 line"):
            _check(release.installer, release, UpdatePreferences(pin="0.20"))
        assert _check(release.installer, release, UpdatePreferences(pin="0.21")).version == "0.21.0"
        beta = Release(tmp_path / "beta", version="0.22.0-beta.1")
        with pytest.raises(UpdateFileError, match="is a beta"):
            _check(beta.installer, beta)
        assert _check(beta.installer, beta, UpdatePreferences(channel="beta")).prerelease is True

    def test_the_version_that_counts_is_the_one_inside_the_signed_installer(self, tmp_path):
        # A genuine but older installer that an edited feed lists as a new release: a downgrade.
        old = Release(tmp_path / "bundle", version="0.21.0", inside="0.18.0")
        with pytest.raises(UpdateFileError, match="lists lumi-setup-0.21.0.exe as Lumi 0.21.0, but the signed "
                                                  "installer is Lumi 0.18.0"):
            _check(old.installer, old)
        truthful = Release(tmp_path / "truthful", version="0.18.0")
        with pytest.raises(UpdateFileError, match="is Lumi 0.18.0, which isn't newer than this copy"):
            _check(truthful.installer, truthful)

    def test_an_installer_that_doesnt_name_its_version_is_refused(self, tmp_path):
        release = Release(tmp_path / "bundle", inside=None)
        with pytest.raises(UpdateFileError, match="can't tell which version lumi-setup-0.21.0.exe installs"):
            _check(release.installer, release)
        not_a_program = Release(tmp_path / "text", content=b"not a Windows program " * 100)
        with pytest.raises(UpdateFileError, match="it isn't a Windows program"):
            _check(not_a_program.installer, not_a_program)

    @pytest.mark.parametrize("bits", [32, 64])
    def test_reading_the_version_resource(self, bits):
        assert update_file.installer_version(fake_installer("0.22.0-beta.1", bits=bits)) == "0.22.0-beta.1"
        with pytest.raises(ValueError, match="isn't a release version"):
            update_file.installer_version(fake_installer("10.0.26100.1", bits=bits))
        with pytest.raises(ValueError, match="doesn't say which version"):
            update_file.installer_version(fake_installer(None, bits=bits))
        with pytest.raises(ValueError):
            update_file.installer_version(fake_installer("0.21.0", bits=bits)[:0x230])

    def test_updates_off_or_a_managed_install_refuses_files_too(self, tmp_path):
        release = Release(tmp_path / "bundle")
        with pytest.raises(UpdateFileError, match="turned off by Acme"):
            _check(release.installer, release, UpdatePreferences(mode="off", managed_by="Acme", locked=("mode",)))
        with pytest.raises(UpdateFileError, match="installed from the MSI package"):
            _check(release.installer, release, UpdatePreferences(installed_by="msi"))

    @pytest.mark.parametrize("version, newer", [("0.19.2", True), ("0.19.2-rc.1", True), ("0.19.2-alpha.1", True),
                                                ("0.19.1", False), ("0.19.2.dev11", False)])
    def test_version_order(self, version, newer):
        assert (update_file.version_key(version) > update_file.version_key(CURRENT)) is newer


class TestInstalling:
    @pytest.fixture
    def records(self, tmp_path):
        from lumi import audit
        from lumi.audit import AuditLog

        log = AuditLog(tmp_path / "audit")
        audit.set_for_tests(log)
        return lambda: [json.loads(line) for path in log._files()
                        for line in path.read_text(encoding="utf-8").splitlines()]

    def test_the_verified_bytes_run_and_lumi_closes(self, tmp_path, records):
        release = Release(tmp_path / "bundle")
        update = _check(release.folder, release)
        launched, closed = [], []
        path = update_file.install(update, busy=lambda: False, shutdown=lambda: closed.append(1),
                                   launcher=launched.append, folder=tmp_path / "run")
        assert launched == [path] and closed == [1]
        assert Path(path).read_bytes() == release.installer.read_bytes()
        record = [r for r in records() if r["type"] == "update.install"][-1]
        assert record["data"]["to_version"] == "0.21.0" and record["data"]["source"] == "file"
        assert record["data"]["sha256"] == update.sha256

    def test_the_copy_that_runs_has_a_name_lumi_makes(self, tmp_path, records):
        # The feed isn't signed, so it can list the installer under any name, such as a
        # drive-relative one that would put the copy outside its folder on Windows.
        release = Release(tmp_path / "bundle")
        entry = "C:lumi-setup-0.21.0.exe"
        for feed in release.folder.glob("appcast*.xml"):
            text = feed.read_text(encoding="utf-8")
            assert f'/{release.installer.name}"' in text
            feed.write_text(text.replace(f'/{release.installer.name}"', f'/{quote(entry)}"'), encoding="utf-8")
        archive = tmp_path / "bundle.zip"
        with zipfile.ZipFile(archive, "w") as bundle:
            bundle.writestr(entry, release.installer.read_bytes())
            for feed in release.folder.glob("appcast*.xml"):
                bundle.write(feed, feed.name)
        update = _check(archive, release)
        assert update.installer_name == entry
        run = tmp_path / "run"
        path = update_file.install(update, busy=lambda: False, shutdown=lambda: None, launcher=lambda path: None,
                                   folder=run)
        assert Path(path) == run / "lumi-setup-0.21.0.exe" and [p.name for p in run.iterdir()] == [Path(path).name]

    def test_never_while_a_turn_runs(self, tmp_path, records):
        release = Release(tmp_path / "bundle")
        update = _check(release.folder, release)
        launched = []
        with pytest.raises(UpdateFileError, match="agent turn is running"):
            update_file.install(update, busy=lambda: True, shutdown=lambda: None, launcher=launched.append,
                                folder=tmp_path / "run")
        assert launched == []
        assert [r["type"] for r in records()] == ["update.deferred"]

    def test_the_app_hooks_are_used_by_default(self, tmp_path, records):
        from lumi import updater

        release = Release(tmp_path / "bundle")
        update = _check(release.folder, release)
        closed = []
        updater.set_host(busy=lambda: False, shutdown=lambda: closed.append("closed"))
        update_file.install(update, launcher=lambda path: None, folder=tmp_path / "run")
        assert closed == ["closed"]

    def test_only_an_installed_copy_on_windows_installs(self, monkeypatch):
        monkeypatch.setattr(update_file.sys, "platform", "linux")
        assert "package manager" in update_file.can_install_here()
        monkeypatch.setattr(update_file.sys, "platform", "darwin")
        assert "pkgutil --check-signature" in update_file.can_install_here()
        monkeypatch.setattr(update_file.sys, "platform", "win32")
        monkeypatch.delattr(update_file.sys, "frozen", raising=False)
        assert "runs from source" in update_file.can_install_here()
        monkeypatch.setattr(update_file.sys, "frozen", True, raising=False)
        assert update_file.can_install_here() == ""

    def test_the_settings_command_verifies_and_says_what_next(self, tmp_path, monkeypatch):
        from lumi import updater
        from lumi.gui.ws_commands import _update_file_result

        release = Release(tmp_path / "bundle")
        monkeypatch.setattr(updater, "EDDSA_PUBLIC_KEY", release.public)
        monkeypatch.setattr(update_file, "__version__", CURRENT)
        result = _update_file_result(str(release.folder))
        assert result["ok"] and result["version"] == "0.21.0" and result["signature"] == "valid"
        assert "data" not in result  # the bytes stay on the server
        refused = _update_file_result(str(tmp_path / "missing"))
        assert not refused["ok"] and "doesn't exist" in refused["error"]


def test_lumi_updates_verify(tmp_path, monkeypatch, capsys):
    from lumi import update_channels, updater

    release = Release(tmp_path / "bundle")
    monkeypatch.setattr(updater, "EDDSA_PUBLIC_KEY", release.public)
    assert update_channels.main(["verify", str(release.folder)]) == 0
    assert json.loads(capsys.readouterr().out)["version"] == "0.21.0"
    release.installer.write_bytes(b"changed")
    assert update_channels.main(["verify", str(release.folder)]) == 1
    assert "signature doesn't match" in capsys.readouterr().err
    assert update_channels.main(["verify"]) == 2


def test_module_constant_is_the_updater_key():
    from lumi.updater import EDDSA_PUBLIC_KEY

    assert len(base64.b64decode(EDDSA_PUBLIC_KEY)) == 32
    assert sys.modules["lumi.update_file"] is update_file
