"""Linux packages from a built one-folder Lumi (``dist/lumi``): a .deb, an .rpm, an AppImage and a tarball.

packaging/build_linux.sh builds the bundle with PyInstaller, then runs:

    python3 packaging/linux_packages.py deb --bundle dist/lumi --version X.Y.Z --out dist/installer
    python3 packaging/linux_packages.py rpm --bundle dist/lumi --version X.Y.Z --root build/rpm-root --spec build/lumi.spec
    python3 packaging/linux_packages.py appdir --bundle dist/lumi --out build/Lumi.AppDir
    python3 packaging/linux_packages.py tarball --bundle dist/lumi --version X.Y.Z --out dist/installer

- **deb and rpm** install the bundle in ``/opt/lumi``, with ``/usr/bin/lumi``,
  a desktop entry and an icon. They carry ``lumi-install.json``
  (``{"installer": "deb"}`` or ``"rpm"``): the package manager updates those
  copies, and Lumi never checks for updates itself (lumi/update_channels.py).
- **The .deb is written here**, in Python (an ar archive of control.tar.gz
  and data.tar.gz), so the build machine needs no dpkg. CI checks it with
  dpkg-deb and installs it with apt.
- **The .rpm** is built by rpmbuild from a staged root and the spec written
  here, with stripping and debug packages off: strip would cut off the
  archive PyInstaller appends to its executable.
- **The AppImage folder** starts the GUI when opened without arguments;
  build_linux.sh makes the AppImage from it with a pinned appimagetool.
- **The tarball** is the bundle alone, for installs without root, such as a
  service account on a server.

Every file gets ``SOURCE_DATE_EPOCH``'s time (or 0) and root ownership, so
the same bundle gives the same packages. Standard library only.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import os
import re
import shutil
import stat
import sys
import tarfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
ICON = REPO / "lumi" / "gui" / "static" / "lumi.png"  # 512x512
PREFIX = "opt/lumi"
HOMEPAGE = "https://github.com/Luminary-Analytics/resonant-client"
# A placeholder until Luminary names a packaging contact: Debian wants a name and an address.
MAINTAINER = os.environ.get("LUMI_PACKAGE_MAINTAINER", "Luminary Analytics <packages@luminary-analytics.invalid>")
SUMMARY = "Lumi, a coding agent with a desktop app and a command line"
DESCRIPTION = (
    "Lumi works in your projects with the model providers you choose: hosted\n"
    "models with your own keys, or models on your own machines. It has a\n"
    "desktop app (opened in your browser on Linux), a terminal UI, and\n"
    "unattended runs for servers and CI (lumi run)."
)


def mtime() -> int:
    try:
        return int(os.environ.get("SOURCE_DATE_EPOCH", "0"))
    except ValueError:
        return 0


def package_version(version: str) -> str:
    """A Python version as Debian and RPM order it: pre-releases and dev builds sort before the release.

    ``0.19.2.dev11`` becomes ``0.19.2~dev11`` and ``0.20.0rc1`` becomes
    ``0.20.0~rc1``: both formats sort ``~`` before anything, even the end.
    """
    match = re.fullmatch(r"(\d+(?:\.\d+)*)(?:(a|b|rc)(\d+))?(?:\.post(\d+))?(?:\.dev(\d+))?", version)
    if not match:
        raise SystemExit(f"Not a release version: {version!r}")
    base, pre, pre_n, post, dev = match.groups()
    text = base
    if pre:
        text += f"~{pre}{pre_n}"
    if post:
        text += f"+post{post}"
    if dev:
        text += f"~dev{dev}"
    return text


def desktop_entry(exec_path: str) -> str:
    return (
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=Lumi\n"
        "GenericName=Coding agent\n"
        "Comment=Work on your projects with the models you choose\n"
        f"Exec={exec_path} gui\n"
        "Icon=lumi\n"
        "Terminal=false\n"
        "Categories=Development;IDE;\n"
        "Keywords=agent;coding;AI;\n"
    )


COPYRIGHT = f"""Format: https://www.debian.org/doc/packaging-manuals/copyright-format/1.0/
Upstream-Name: Lumi
Source: {HOMEPAGE}

Files: *
Copyright: Luminary Analytics
License: MIT
 Lumi's source is available under the MIT license; see LICENSE in the
 repository. The bundle in /opt/lumi also contains third-party components
 under their own licenses, listed in
 /opt/lumi/_internal/licenses/THIRD_PARTY_NOTICES.txt.
"""


@dataclass(frozen=True)
class Entry:
    """One path in a package: a directory, a file (from disk or bytes) or a symlink."""

    path: str  # relative, with "/" separators, no leading "./"
    kind: str  # "dir" | "file" | "link"
    mode: int = 0o755
    source: Path | None = None
    data: bytes | None = None
    target: str = ""

    def content(self) -> bytes:
        if self.data is not None:
            return self.data
        assert self.source is not None
        return self.source.read_bytes()

    def size(self) -> int:
        return len(self.data) if self.data is not None else self.source.stat().st_size if self.source else 0


def _bundle_entries(bundle: Path, prefix: str) -> Iterator[Entry]:
    """The bundle's directories, files and symlinks under ``prefix``, in a stable order."""
    executable = bundle / "lumi"
    if not executable.is_file():
        raise SystemExit(f"{bundle} isn't a Lumi bundle (no lumi executable)")
    for root, dirs, files in os.walk(bundle):
        dirs.sort()
        here = Path(root)
        relative = here.relative_to(bundle).as_posix()
        base = prefix if relative == "." else f"{prefix}/{relative}"
        yield Entry(base, "dir")
        for name in sorted(files + [d for d in dirs if (here / d).is_symlink()]):
            path = here / name
            if path.is_symlink():
                yield Entry(f"{base}/{name}", "link", mode=0o777, target=os.readlink(path))
                continue
            # The launcher is executable whatever the build machine's file modes say.
            executable_bits = path == executable or path.stat().st_mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
            yield Entry(f"{base}/{name}", "file", mode=0o755 if executable_bits else 0o644, source=path)
        dirs[:] = [d for d in dirs if not (here / d).is_symlink()]


def system_payload(bundle: Path, installer: str) -> list[Entry]:
    """What the deb and rpm install: /opt/lumi, /usr/bin/lumi, the desktop entry, icon and copyright."""
    entries = [Entry("opt", "dir"), *_bundle_entries(bundle, PREFIX)]
    entries.append(Entry(f"{PREFIX}/lumi-install.json", "file", mode=0o644,
                         data=json.dumps({"installer": installer}).encode()))
    for directory in ("usr", "usr/bin", "usr/share", "usr/share/applications", "usr/share/icons",
                      "usr/share/icons/hicolor", "usr/share/icons/hicolor/512x512",
                      "usr/share/icons/hicolor/512x512/apps", "usr/share/doc", "usr/share/doc/lumi"):
        entries.append(Entry(directory, "dir"))
    entries.append(Entry("usr/bin/lumi", "link", mode=0o777, target=f"/{PREFIX}/lumi"))
    entries.append(Entry("usr/share/applications/lumi.desktop", "file", mode=0o644,
                         data=desktop_entry("/usr/bin/lumi").encode()))
    entries.append(Entry("usr/share/icons/hicolor/512x512/apps/lumi.png", "file", mode=0o644, source=ICON))
    entries.append(Entry("usr/share/doc/lumi/copyright", "file", mode=0o644, data=COPYRIGHT.encode()))
    return entries


def _tar(entries: list[Entry], *, prefix: str = "", top: str | None = None) -> bytes:
    """A gzip-compressed tar of the entries, owned by root, with fixed times."""
    buffer = io.BytesIO()
    stamp = mtime()
    with gzip.GzipFile(fileobj=buffer, mode="wb", mtime=stamp) as zipped, \
            tarfile.open(fileobj=zipped, mode="w", format=tarfile.GNU_FORMAT) as archive:
        if top is not None:
            info = tarfile.TarInfo(top)
            info.type, info.mode, info.mtime = tarfile.DIRTYPE, 0o755, stamp
            archive.addfile(info)
        for entry in entries:
            info = tarfile.TarInfo(prefix + entry.path)
            info.mode, info.mtime, info.uid, info.gid, info.uname, info.gname = entry.mode, stamp, 0, 0, "root", "root"
            if entry.kind == "dir":
                info.type = tarfile.DIRTYPE
                archive.addfile(info)
            elif entry.kind == "link":
                info.type, info.linkname = tarfile.SYMTYPE, entry.target
                archive.addfile(info)
            else:
                data = entry.content()
                info.size = len(data)
                archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def _ar(members: list[tuple[str, bytes]]) -> bytes:
    """A System V ar archive, as dpkg reads it."""
    out = io.BytesIO()
    out.write(b"!<arch>\n")
    for name, data in members:
        header = f"{name:<16}{mtime():<12}{0:<6}{0:<6}{'100644':<8}{len(data):<10}`\n".encode("ascii")
        assert len(header) == 60, header
        out.write(header)
        out.write(data)
        if len(data) % 2:
            out.write(b"\n")
    return out.getvalue()


def deb_control(version: str, installed_kib: int, *, arch: str = "amd64") -> str:
    description = "\n".join(" " + (line or ".") for line in DESCRIPTION.split("\n"))
    return (
        "Package: lumi\n"
        f"Version: {package_version(version)}\n"
        f"Architecture: {arch}\n"
        f"Maintainer: {MAINTAINER}\n"
        f"Installed-Size: {installed_kib}\n"
        # PyInstaller built on Ubuntu 22.04 needs its glibc; the browser opens the GUI.
        "Depends: libc6 (>= 2.35)\n"
        "Recommends: xdg-utils, git\n"
        "Section: devel\n"
        "Priority: optional\n"
        f"Homepage: {HOMEPAGE}\n"
        f"Description: {SUMMARY}\n"
        f"{description}\n"
    )


def build_deb(bundle: Path, version: str, out_dir: Path, *, arch: str = "amd64") -> Path:
    entries = system_payload(bundle, "deb")
    files = [e for e in entries if e.kind == "file"]
    installed_kib = (sum(e.size() for e in files) + 1023) // 1024
    md5sums = "".join(f"{hashlib.md5(e.content(), usedforsecurity=False).hexdigest()}  {e.path}\n" for e in files)
    control = [Entry("control", "file", mode=0o644, data=deb_control(version, installed_kib, arch=arch).encode()),
               Entry("md5sums", "file", mode=0o644, data=md5sums.encode())]
    package = _ar([
        ("debian-binary", b"2.0\n"),
        ("control.tar.gz", _tar(control, prefix="./", top="./")),
        ("data.tar.gz", _tar(entries, prefix="./", top="./")),
    ])
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"lumi_{package_version(version)}_{arch}.deb"
    path.write_bytes(package)
    return path


def stage(entries: list[Entry], root: Path) -> None:
    """Write the entries to disk under ``root`` (for rpmbuild's %install)."""
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)
    for entry in entries:
        target = root / entry.path
        if entry.kind == "dir":
            target.mkdir(parents=True, exist_ok=True)
        elif entry.kind == "link":
            target.parent.mkdir(parents=True, exist_ok=True)
            os.symlink(entry.target, target)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            if entry.source is not None and entry.data is None:
                shutil.copyfile(entry.source, target)
            else:
                target.write_bytes(entry.content())
            target.chmod(entry.mode)


def rpm_spec(version: str, root: Path, *, arch: str = "x86_64") -> str:
    """A spec that packages the staged root as it is: no stripping, no generated dependencies."""
    return f"""# Written by packaging/linux_packages.py; the files come from a staged root.
%global debug_package %{{nil}}
%global __os_install_post %{{nil}}
%global _build_id_links none

Name:           lumi
Version:        {package_version(version)}
Release:        1
Summary:        {SUMMARY}
License:        MIT
URL:            {HOMEPAGE}
BuildArch:      {arch}
# PyInstaller bundles its libraries; only the C library comes from the system.
AutoReqProv:    no
Requires:       glibc >= 2.35

%description
{DESCRIPTION}

%install
cp -a {root.as_posix()}/. %{{buildroot}}/

%files
/{PREFIX}
/usr/bin/lumi
/usr/share/applications/lumi.desktop
/usr/share/icons/hicolor/512x512/apps/lumi.png
%doc /usr/share/doc/lumi/copyright
"""


APPRUN = """#!/bin/sh
# Lumi's AppImage: opened without arguments, it starts the desktop app.
HERE="$(dirname "$(readlink -f "$0")")"
if [ "$#" -eq 0 ]; then
  set -- gui
fi
exec "$HERE/usr/lib/lumi/lumi" "$@"
"""


def build_appdir(bundle: Path, out: Path) -> Path:
    """The AppDir appimagetool turns into an AppImage (no install marker: it doesn't update through a package manager)."""
    entries = list(_bundle_entries(bundle, "usr/lib/lumi"))
    entries += [Entry("usr", "dir"), Entry("usr/lib", "dir"),
                Entry("AppRun", "file", mode=0o755, data=APPRUN.encode()),
                Entry("lumi.desktop", "file", mode=0o644, data=desktop_entry("lumi").encode()),
                Entry("lumi.png", "file", mode=0o644, source=ICON),
                Entry(".DirIcon", "link", target="lumi.png")]
    stage(entries, out)
    return out


def build_tarball(bundle: Path, version: str, out_dir: Path, *, arch: str = "x86_64") -> Path:
    name = f"lumi-{version}-linux-{arch}"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{name}.tar.gz"
    path.write_bytes(_tar(list(_bundle_entries(bundle, name))))
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("deb", "rpm", "appdir", "tarball"):
        command = commands.add_parser(name)
        command.add_argument("--bundle", type=Path, required=True)
        if name != "appdir":
            command.add_argument("--version", required=True)
        if name == "rpm":
            command.add_argument("--root", type=Path, required=True)
            command.add_argument("--spec", type=Path, required=True)
        else:
            command.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.command == "deb":
        print(f"Wrote {build_deb(args.bundle, args.version, args.out)}")
    elif args.command == "rpm":
        stage(system_payload(args.bundle, "rpm"), args.root)
        args.spec.parent.mkdir(parents=True, exist_ok=True)
        args.spec.write_text(rpm_spec(args.version, args.root.resolve()), encoding="utf-8")
        print(f"Staged {args.root} and wrote {args.spec}")
    elif args.command == "appdir":
        print(f"Wrote {build_appdir(args.bundle, args.out)}")
    else:
        print(f"Wrote {build_tarball(args.bundle, args.version, args.out)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
