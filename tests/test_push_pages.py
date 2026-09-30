"""Publishing the Pages site byte for byte (packaging/push_pages.py), with real Git.

Sparkle reads a macOS feed only when its signature verifies over the bytes
Pages serves, and Pages serves what the gh-pages commit holds. Git for
Windows, where the release publishes, converts line ends as it stages and
checks out text files, which once turned every signed feed into one that
doesn't verify. These tests publish into clones of a bare "origin" with
core.autocrlf=true, Windows' default, and check what the branch holds.
"""
from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

ROOT = Path(__file__).resolve().parents[1]
BASE = "https://luminary-analytics.github.io/resonant-client/downloads"
WINDOWS_DEFAULT = "core.autocrlf=true"
LIVE_FEED = b"""<?xml version='1.0' encoding='utf-8'?>
<rss xmlns:sparkle="http://www.andymatuschak.org/xml-namespaces/sparkle" version="2.0">
    <channel>
        <title>Resonant Client Updates</title>
        <link>https://luminary-analytics.github.io/resonant-client/</link>
        <description>Auto-update channel for Resonant Client</description>
        <language>en</language>
        <item>
            <title>Version 0.19.1</title>
            <sparkle:version>0.19.1</sparkle:version>
            <enclosure url="https://example.invalid/v0.19.1.exe" sparkle:version="0.19.1" length="1" type="application/octet-stream" sparkle:edSignature="c2ln" />
        </item>
    </channel>
</rss>"""


def _load(name: str):
    spec = importlib.util.spec_from_file_location(f"lumi_test_{name}", ROOT / "packaging" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


push_pages = _load("push_pages")
publish_pages = _load("publish_pages")
appcast = _load("update_appcast")
signing = _load("feed_signature")


def git(repo: Path, *args: str, config: tuple[str, ...] = ()) -> bytes:
    options = ["-c", "user.name=Test", "-c", "user.email=test@example.invalid"]
    for setting in config:
        options += ["-c", setting]
    return subprocess.run(["git", *options, "-C", str(repo), *args], check=True, capture_output=True).stdout


def checkout(origin: Path, folder: Path, *config: str) -> Path:
    options = [item for setting in config for item in ("-c", setting)]
    subprocess.run(["git", *options, "clone", "--quiet", "--branch", "gh-pages", str(origin), str(folder)],
                   check=True, capture_output=True)
    return folder


@pytest.fixture
def key():
    private = Ed25519PrivateKey.generate()
    public = base64.b64encode(private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)).decode()
    return SimpleNamespace(sign=lambda data: base64.b64encode(private.sign(data)).decode(), public=public)


@pytest.fixture
def origin(tmp_path):
    """The gh-pages branch as it is live: a Windows feed and a page, and no .gitattributes yet."""
    seed = tmp_path / "seed"
    seed.mkdir()
    git(seed, "init", "--quiet")
    (seed / ".nojekyll").write_bytes(b"")
    (seed / "appcast.xml").write_bytes(LIVE_FEED)
    (seed / "index.html").write_bytes(b"<html>\n<body>Lumi</body>\n</html>\n")
    git(seed, "add", "-A", config=("core.autocrlf=false",))
    git(seed, "commit", "--quiet", "-m", "site")
    bare = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--quiet", "--bare", str(bare)], check=True, capture_output=True)
    git(seed, "push", "--quiet", str(bare), "HEAD:refs/heads/gh-pages")
    return bare


def publish_windows(site: Path, tmp_path: Path, version: str, key) -> Path:
    installer = tmp_path / f"lumi-setup-{version}.exe"
    installer.write_bytes(b"MZ" + os.urandom(64))
    publish_pages.publish(site, installer, version)
    appcast.publish_feeds(site, version, installer, key.sign(installer.read_bytes()), f"<p>{version}</p>", BASE)
    return installer


def signing_record(tmp_path: Path, *installers: Path) -> Path:
    """What sign_windows.ps1 records in the release job: lumi.exe, then each installer as signed."""
    record = tmp_path / "lumi-authenticode.jsonl"
    entries = [{"path": str(tmp_path / "dist" / "lumi" / "lumi.exe"), "sha256": "0" * 64, "signed": True}]
    entries += [{"path": str(tmp_path / "dist" / "installer" / installer.name),
                 "sha256": hashlib.sha256(installer.read_bytes()).hexdigest(), "signed": True}
                for installer in installers]
    record.write_text("".join(json.dumps(entry) + "\n" for entry in entries), encoding="utf-8")
    return record


def publish_macos(site: Path, tmp_path: Path, version: str, key) -> None:
    """As packaging/publish_macos.ps1 does: the disk image, its feeds, and a signature on each feed written."""
    image = tmp_path / f"lumi-{version}.dmg"
    image.write_bytes(os.urandom(4096))
    publish_pages.publish(site, image, version, platform="macos")
    for feed in appcast.publish_feeds(site, version, image, key.sign(image.read_bytes()), f"<p>{version}</p>",
                                      BASE, platform="macos"):
        signing.attach(feed, key.sign(feed.read_bytes()))


def push(site: Path, key, *args: str) -> int:
    return push_pages.main([str(site), "--public-key", key.public, *args])


def test_a_release_reaches_the_branch_and_the_next_checkout_byte_for_byte(origin, tmp_path, key, capsys):
    windows = checkout(origin, tmp_path / "windows", WINDOWS_DEFAULT)
    publish_windows(windows, tmp_path, "0.21.0", key)
    assert push(windows, key, "--message", "Lumi 0.21.0 for Windows") == 0
    mac = checkout(origin, tmp_path / "mac", WINDOWS_DEFAULT)
    publish_macos(mac, tmp_path, "0.21.0", key)
    signed = {path.name: path.read_bytes() for path in mac.glob("appcast-macos*.xml")}
    assert len(signed) == 3
    assert push(mac, key, "--message", "Lumi 0.21.0 for macOS") == 0
    out = capsys.readouterr().out
    assert "downloads/v0.21.0/lumi-0.21.0.dmg: 4096 bytes and signature" in out
    # What the branch holds, what Pages serves, is what was signed...
    assert push(origin, key, "--check", "--rev", "gh-pages") == 0
    for name, data in signed.items():
        assert git(origin, "cat-file", "blob", f"gh-pages:{name}") == data
    # ...one fresh commit each time...
    assert git(origin, "rev-list", "--count", "gh-pages").strip() == b"1"
    # ...and a checkout gets it too, with Windows' default converting nothing.
    again = checkout(origin, tmp_path / "again", WINDOWS_DEFAULT)
    assert {path.name: path.read_bytes() for path in again.glob("appcast-macos*.xml")} == signed
    assert b"\r" not in (again / "appcast.xml").read_bytes()
    assert (again / ".gitattributes").read_bytes() == publish_pages.GIT_ATTRIBUTES


def test_git_changing_line_ends_is_caught_before_anything_is_pushed(origin, tmp_path, key):
    # How it went wrong: a feed written with "\r\n" (text mode on Windows) and
    # signed so, then staged by Git with core.autocrlf=true and no
    # .gitattributes, which stores "\n". The working copy verifies; what Git
    # holds, and Pages would serve, doesn't.
    mac = checkout(origin, tmp_path / "mac")
    publish_macos(mac, tmp_path, "0.21.0", key)
    feed = mac / "appcast-macos.xml"
    written = signing.strip(feed.read_bytes()).replace(b"\n", b"\r\n")
    feed.write_bytes(written + signing.block(key.sign(written), len(written)))
    assert signing.verify(feed, key.public)
    (mac / ".gitattributes").unlink()
    git(mac, "add", "-A", config=(WINDOWS_DEFAULT,))
    problems, _ = push_pages.check(mac, push_pages.cryptography_verifier(key.public))
    assert any(problem.startswith("appcast-macos.xml") for problem in problems), problems
    assert any(problem.startswith(".gitattributes") for problem in problems), problems


def test_the_check_reads_the_staged_blobs_not_the_working_copy(origin, tmp_path, key):
    mac = checkout(origin, tmp_path / "mac")
    publish_macos(mac, tmp_path, "0.21.0", key)
    git(mac, "add", "-A", config=("core.autocrlf=false",))
    (mac / "appcast-macos.xml").write_bytes(b"changed after staging")
    assert push_pages.check(mac, push_pages.cryptography_verifier(key.public))[0] == []


def test_a_feed_changed_after_signing_is_never_pushed(origin, tmp_path, key, capsys):
    mac = checkout(origin, tmp_path / "mac")
    publish_macos(mac, tmp_path, "0.21.0", key)
    feed = mac / "appcast-macos.xml"
    feed.write_bytes(feed.read_bytes().replace(b"lumi-0.21.0.dmg", b"lumi-0.21.9.dmg", 1))
    before = git(origin, "rev-parse", "gh-pages")
    assert push(mac, key, "--message", "must not be pushed") == 1
    assert git(origin, "rev-parse", "gh-pages") == before
    err = capsys.readouterr().err
    assert "appcast-macos.xml's signature doesn't verify" in err and "Nothing was committed or pushed" in err


def test_a_disk_image_that_doesnt_match_its_feed_is_refused(origin, tmp_path, key, capsys):
    mac = checkout(origin, tmp_path / "mac")
    publish_macos(mac, tmp_path, "0.21.0", key)
    image = mac / "downloads" / "v0.21.0" / "lumi-0.21.0.dmg"
    image.write_bytes(os.urandom(4096))
    assert push(mac, key, "--message", "m") == 1
    assert "lumi-0.21.0.dmg doesn't match the signature" in capsys.readouterr().err
    image.write_bytes(os.urandom(100))
    assert push(mac, key, "--message", "m") == 1
    assert "lumi-0.21.0.dmg is 100 bytes, and appcast-macos" in capsys.readouterr().err


def test_a_site_without_its_gitattributes_is_refused(origin, tmp_path, key, capsys):
    windows = checkout(origin, tmp_path / "windows")
    publish_windows(windows, tmp_path, "0.21.0", key)
    (windows / ".gitattributes").unlink()
    assert push(windows, key, "--message", "m") == 1
    assert ".gitattributes doesn't say '* -text'" in capsys.readouterr().err


def test_the_lease_keeps_what_another_run_pushed(origin, tmp_path, key, capsys):
    first = checkout(origin, tmp_path / "first")
    second = checkout(origin, tmp_path / "second")
    publish_windows(first, tmp_path, "0.21.0", key)
    publish_windows(second, tmp_path, "0.21.1", key)
    assert push(first, key, "--message", "first") == 0
    pushed = git(origin, "rev-parse", "gh-pages")
    assert push(second, key, "--message", "second") == 1
    assert git(origin, "rev-parse", "gh-pages") == pushed
    assert "stale info" in capsys.readouterr().err


def test_the_windows_installer_published_is_the_one_signed(origin, tmp_path, key, capsys):
    windows = checkout(origin, tmp_path / "windows", WINDOWS_DEFAULT)
    installer = publish_windows(windows, tmp_path, "0.21.0", key)
    record = signing_record(tmp_path, installer)
    assert push(windows, key, "--check", "--signed", str(record)) == 0
    assert "downloads/v0.21.0/lumi-setup-0.21.0.exe: SHA-256 as sign_windows.ps1 recorded it" in capsys.readouterr().out
    # Changed after it was signed: never pushed.
    (windows / "downloads" / "v0.21.0" / "lumi-setup-0.21.0.exe").write_bytes(b"MZ changed")
    before = git(origin, "rev-parse", "gh-pages")
    assert push(windows, key, "--message", "must not be pushed", "--signed", str(record)) == 1
    assert git(origin, "rev-parse", "gh-pages") == before
    assert "lumi-setup-0.21.0.exe isn't the file sign_windows.ps1 recorded" in capsys.readouterr().err


def test_a_signed_installer_missing_from_the_site_is_refused(origin, tmp_path, key, capsys):
    windows = checkout(origin, tmp_path / "windows")
    installer = publish_windows(windows, tmp_path, "0.21.0", key)
    msi = tmp_path / "lumi-0.21.0.msi"
    msi.write_bytes(b"MSI")
    assert push(windows, key, "--message", "m", "--signed", str(signing_record(tmp_path, installer, msi))) == 1
    assert "lumi-0.21.0.msi, which this release signed, isn't on the site" in capsys.readouterr().err
    (tmp_path / "empty.jsonl").write_text("", encoding="utf-8")
    assert push(windows, key, "--check", "--signed", str(tmp_path / "empty.jsonl")) == 1
    assert "records no installer" in capsys.readouterr().err


def test_nothing_changed_pushes_nothing(origin, tmp_path, key, capsys):
    site = checkout(origin, tmp_path / "site")
    publish_windows(site, tmp_path, "0.21.0", key)
    assert push(site, key, "--message", "first") == 0
    pushed = git(origin, "rev-parse", "gh-pages")
    again = checkout(origin, tmp_path / "again", WINDOWS_DEFAULT)
    assert push(again, key, "--message", "a retry") == 0
    assert git(origin, "rev-parse", "gh-pages") == pushed
    assert "nothing pushed" in capsys.readouterr().out
