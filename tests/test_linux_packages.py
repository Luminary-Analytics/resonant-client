"""Linux packages (packaging/linux_packages.py) and the browser fallback they rely on.

The real packages are built, installed and run in CI (build-linux.yml: apt,
dnf in a Fedora container, the AppImage without FUSE). These tests check the
.deb's structure and the rest of the pieces on any machine.
"""
from __future__ import annotations

import gzip
import hashlib
import importlib.util
import io
import json
import sys
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("linux_packages", ROOT / "packaging" / "linux_packages.py")
linux_packages = importlib.util.module_from_spec(spec)
sys.modules["linux_packages"] = linux_packages  # dataclasses look their module up there
spec.loader.exec_module(linux_packages)


@pytest.fixture
def bundle(tmp_path, monkeypatch):
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "1790000000")
    root = tmp_path / "dist" / "lumi"
    (root / "_internal" / "lib").mkdir(parents=True)
    (root / "lumi").write_bytes(b"\x7fELF launcher")
    (root / "_internal" / "lib" / "libfake.so.1").write_bytes(b"library")
    (root / "_internal" / "base_library.zip").write_bytes(b"PK")
    return root


def _ar_members(data: bytes) -> dict[str, bytes]:
    assert data[:8] == b"!<arch>\n"
    members, offset = {}, 8
    while offset < len(data):
        header = data[offset:offset + 60]
        assert header[58:60] == b"`\n", header
        name, size = header[:16].decode().strip(), int(header[48:58])
        members[name] = data[offset + 60:offset + 60 + size]
        offset += 60 + size + (size % 2)
    return members


def _tar(data: bytes) -> tarfile.TarFile:
    return tarfile.open(fileobj=io.BytesIO(gzip.decompress(data)))


def test_versions_sort_like_python_releases():
    cases = {"0.19.2.dev11": "0.19.2~dev11", "0.20.0": "0.20.0", "0.20.0rc1": "0.20.0~rc1",
             "1.2.3a4.dev5": "1.2.3~a4~dev5", "0.21.0.post1": "0.21.0+post1"}
    assert {version: linux_packages.package_version(version) for version in cases} == cases
    with pytest.raises(SystemExit, match="Not a release version"):
        linux_packages.package_version("0.20.0-beta")


def test_the_deb_installs_lumi_under_opt_for_the_package_manager(bundle, tmp_path):
    deb = linux_packages.build_deb(bundle, "0.19.2.dev11", tmp_path / "out")
    assert deb.name == "lumi_0.19.2~dev11_amd64.deb"
    members = _ar_members(deb.read_bytes())
    assert list(members) == ["debian-binary", "control.tar.gz", "data.tar.gz"]
    assert members["debian-binary"] == b"2.0\n"

    with _tar(members["control.tar.gz"]) as control_tar:
        control = control_tar.extractfile("./control").read().decode()
        md5sums = control_tar.extractfile("./md5sums").read().decode()
    fields = dict(line.split(": ", 1) for line in control.splitlines() if line and not line.startswith(" "))
    assert (fields["Package"], fields["Version"], fields["Architecture"]) == ("lumi", "0.19.2~dev11", "amd64")
    assert fields["Depends"] == "libc6 (>= 2.35)" and "xdg-utils" in fields["Recommends"]
    # A summary line, then the long description indented by one space.
    assert fields["Description"].startswith("Lumi, a coding agent") and "\n Lumi works in your projects" in control

    with _tar(members["data.tar.gz"]) as data:
        entries = {member.name: member for member in data.getmembers()}
        launcher = entries["./opt/lumi/lumi"]
        assert launcher.mode == 0o755 and (launcher.uid, launcher.gid, launcher.uname) == (0, 0, "root")
        assert entries["./opt/lumi/_internal/lib/libfake.so.1"].mode == 0o644
        assert entries["./usr/bin/lumi"].issym() and entries["./usr/bin/lumi"].linkname == "/opt/lumi/lumi"
        assert json.loads(data.extractfile("./opt/lumi/lumi-install.json").read()) == {"installer": "deb"}
        desktop = data.extractfile("./usr/share/applications/lumi.desktop").read().decode()
        assert "Exec=/usr/bin/lumi gui\n" in desktop and "Icon=lumi\n" in desktop
        assert "./usr/share/icons/hicolor/512x512/apps/lumi.png" in entries
        assert "MIT" in data.extractfile("./usr/share/doc/lumi/copyright").read().decode()
        assert {member.mtime for member in data.getmembers()} == {1790000000}
        # Every parent directory is in the archive, before what it holds.
        names = list(entries)
        assert names.index("./opt") < names.index("./opt/lumi") < names.index("./opt/lumi/lumi")
        # md5sums lists every file, with paths as dpkg writes them.
        expected = hashlib.md5(b"\x7fELF launcher", usedforsecurity=False).hexdigest()
        assert f"{expected}  opt/lumi/lumi\n" in md5sums
        files = [m.name[2:] for m in data.getmembers() if m.isfile()]
        assert sorted(line.split("  ", 1)[1] for line in md5sums.splitlines()) == sorted(files)

    # The same bundle gives the same package.
    again = linux_packages.build_deb(bundle, "0.19.2.dev11", tmp_path / "again")
    assert again.read_bytes() == deb.read_bytes()


def test_the_rpm_spec_packages_the_staged_root_as_is(bundle, tmp_path):
    root = tmp_path / "rpm-root"
    assert linux_packages.main(["rpm", "--bundle", str(bundle), "--version", "0.20.0rc1", "--root", str(root),
                                "--spec", str(tmp_path / "lumi.spec")]) == 0
    spec_text = (tmp_path / "lumi.spec").read_text(encoding="utf-8")
    assert "Version:        0.20.0~rc1" in spec_text and "AutoReqProv:    no" in spec_text
    # strip would cut off the archive PyInstaller appends to its executable.
    assert "%global __os_install_post %{nil}" in spec_text and "%global debug_package %{nil}" in spec_text
    assert f"cp -a {root.resolve().as_posix()}/. %{{buildroot}}/" in spec_text
    assert "\n/opt/lumi\n/usr/bin/lumi\n" in spec_text
    assert json.loads((root / "opt" / "lumi" / "lumi-install.json").read_text()) == {"installer": "rpm"}
    assert (root / "usr" / "share" / "applications" / "lumi.desktop").is_file()


def test_the_appdir_starts_the_gui_and_carries_no_install_marker(bundle, tmp_path):
    appdir = linux_packages.build_appdir(bundle, tmp_path / "Lumi.AppDir")
    apprun = (appdir / "AppRun").read_text(encoding="utf-8")
    assert 'set -- gui' in apprun and 'exec "$HERE/usr/lib/lumi/lumi" "$@"' in apprun
    assert "Exec=lumi gui" in (appdir / "lumi.desktop").read_text(encoding="utf-8")
    assert (appdir / "lumi.png").is_file() and (appdir / "usr" / "lib" / "lumi" / "lumi").is_file()
    assert not (appdir / "usr" / "lib" / "lumi" / "lumi-install.json").exists()


def test_the_tarball_is_the_bundle_in_one_folder(bundle, tmp_path):
    tarball = linux_packages.build_tarball(bundle, "0.20.0", tmp_path / "out")
    assert tarball.name == "lumi-0.20.0-linux-x86_64.tar.gz"
    with tarfile.open(tarball) as archive:
        names = archive.getnames()
        assert archive.getmember("lumi-0.20.0-linux-x86_64/lumi").mode == 0o755
    assert all(name.startswith("lumi-0.20.0-linux-x86_64") for name in names)
    assert "lumi-0.20.0-linux-x86_64/lumi-install.json" not in names


def test_a_deb_or_rpm_copy_leaves_updates_to_the_package_manager(tmp_path):
    from lumi import update_channels
    from lumi.gui.ws_commands import _update_check_message

    (tmp_path / "opt" / "lumi").mkdir(parents=True)
    (tmp_path / "opt" / "lumi" / "lumi-install.json").write_text(json.dumps({"installer": "deb"}), encoding="utf-8")
    assert update_channels.installed_by(str(tmp_path / "opt" / "lumi" / "lumi")) == "deb"
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"updates": {"mode": "automatic"}}), encoding="utf-8")
    for installer in ("deb", "rpm"):
        prefs = update_channels.read(settings, SimpleNamespace(policy=None, error=""), installer=installer)
        assert (prefs.mode, prefs.installed_by) == ("off", installer)
    assert _update_check_message({"mode": "off", "installed_by": "rpm"}, False) == (
        "This copy was installed from the RPM package, so your package manager updates it.")


def test_a_folder_without_the_launcher_is_refused(tmp_path):
    (tmp_path / "empty").mkdir()
    with pytest.raises(SystemExit, match="isn't a Lumi bundle"):
        linux_packages.build_deb(tmp_path / "empty", "0.20.0", tmp_path / "out")


def test_without_a_native_window_the_page_opens_in_the_browser(monkeypatch):
    from lumi.gui import server

    gui_app = SimpleNamespace(_webview_window=object())
    opened = []
    monkeypatch.setattr(server, "_graphical_session", lambda: True)
    assert server._without_native_window(gui_app, lambda: opened.append("page") or True) is True
    assert gui_app._webview_window is None and opened == ["page"]  # native pickers aren't awaited

    # No screen (a server over SSH): nothing opens, so no console browser takes the terminal.
    monkeypatch.setattr(server, "_graphical_session", lambda: False)
    assert server._without_native_window(SimpleNamespace(), lambda: opened.append("again")) is False
    assert opened == ["page"]
    monkeypatch.setattr(server, "_graphical_session", lambda: True)

    def broken():
        raise RuntimeError("no browser")

    assert server._without_native_window(SimpleNamespace(), broken) is False


def test_a_graphical_session_on_linux_needs_a_display(monkeypatch):
    from lumi.gui import server

    monkeypatch.setattr(server.sys, "platform", "linux")
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    assert server._graphical_session() is False
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    assert server._graphical_session() is True
    monkeypatch.setattr(server.sys, "platform", "win32")
    monkeypatch.delenv("WAYLAND_DISPLAY")
    assert server._graphical_session() is True
