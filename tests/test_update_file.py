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
import sys
import zipfile
from pathlib import Path

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


class Release:
    """An installer and the feed that lists it, as the release workflow publishes them."""

    def __init__(self, folder: Path, *, version: str = "0.21.0", key: Ed25519PrivateKey | None = None,
                 content: bytes = b"MZ fake Lumi installer " * 1000):
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
        for feed in release.folder.glob("appcast*.xml"):
            feed.unlink()
        with pytest.raises(UpdateFileError, match="No update feed"):
            _check(release.installer, release)
        (release.folder / "appcast.xml").write_text(
            '<?xml version="1.0"?><!DOCTYPE rss [<!ENTITY x "y">]><rss/>', encoding="utf-8")
        with pytest.raises(UpdateFileError, match="DTD"):
            _check(release.installer, release)

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
