"""The Pages update site hosts the newest installers and a download page."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load():
    spec = importlib.util.spec_from_file_location("resonant_publish_pages", ROOT / "packaging" / "publish_pages.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _installer(tmp_path: Path, version: str) -> Path:
    path = tmp_path / "dist" / f"lumi-setup-{version}.exe"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(f"installer {version}".encode())
    return path


def test_new_installer_is_copied_and_linked(tmp_path):
    pages = _load()
    site = tmp_path / "site"
    site.mkdir()
    target = pages.publish(site, _installer(tmp_path, "0.19.2"), "0.19.2")
    assert target == site / "downloads" / "v0.19.2" / "lumi-setup-0.19.2.exe"
    assert target.read_bytes() == b"installer 0.19.2"
    index = (site / "index.html").read_text(encoding="utf-8")
    assert 'href="downloads/v0.19.2/lumi-setup-0.19.2.exe"' in index
    assert (site / ".nojekyll").exists()


def test_only_the_newest_installers_are_kept_by_numeric_version(tmp_path):
    pages = _load()
    site = tmp_path / "site"
    site.mkdir()
    for version in ("0.9.0", "0.10.0", "0.19.1", "0.19.10"):
        pages.publish(site, _installer(tmp_path, version), version, keep=3, lines=1)
    kept = sorted(p.name for p in (site / "downloads").iterdir())
    assert kept == ["v0.10.0", "v0.19.1", "v0.19.10"]
    assert "0.19.10" in (site / "index.html").read_text(encoding="utf-8")


def test_each_recent_release_line_keeps_its_newest_installer(tmp_path):
    # Installs pinned to a line (lumi/update_channels.py) need its newest installer.
    pages = _load()
    site = tmp_path / "site"
    site.mkdir()
    for version in ("0.17.0", "0.18.0", "0.18.1", "0.19.0", "0.20.0", "0.20.1", "0.20.2", "0.21.0"):
        pages.publish(site, _installer(tmp_path, version), version, keep=2, lines=3)
    kept = sorted(p.name for p in (site / "downloads").iterdir())
    # The newest two, plus the newest of 0.21, 0.20 and 0.19.
    assert kept == ["v0.19.0", "v0.20.2", "v0.21.0"]
    # A fix for an older line is kept and served, but the page still offers the newest.
    pages.publish(site, _installer(tmp_path, "0.19.1"), "0.19.1", keep=2, lines=3)
    assert (site / "downloads" / "v0.19.1" / "lumi-setup-0.19.1.exe").exists()
    assert not (site / "downloads" / "v0.19.0").exists()
    assert 'href="downloads/v0.21.0/lumi-setup-0.21.0.exe"' in (site / "index.html").read_text(encoding="utf-8")


def test_betas_are_hosted_without_changing_the_download_page(tmp_path):
    pages = _load()
    site = tmp_path / "site"
    site.mkdir()
    pages.publish(site, _installer(tmp_path, "0.20.1"), "0.20.1")
    page = (site / "index.html").read_text(encoding="utf-8")
    target = pages.publish(site, _installer(tmp_path, "0.21.0-beta.1"), "0.21.0-beta.1")
    assert target == site / "downloads" / "v0.21.0-beta.1" / "lumi-setup-0.21.0-beta.1.exe"
    assert (site / "index.html").read_text(encoding="utf-8") == page
    # Once 0.21.0 ships, its betas are gone.
    pages.publish(site, _installer(tmp_path, "0.21.0"), "0.21.0")
    assert not (site / "downloads" / "v0.21.0-beta.1").exists()
    with pytest.raises(ValueError, match="older than the newest stable"):
        pages.publish(site, _installer(tmp_path, "0.21.0-rc.1"), "0.21.0-rc.1")


def test_unrelated_files_are_left_alone(tmp_path):
    pages = _load()
    site = tmp_path / "site"
    (site / "downloads" / "notes").mkdir(parents=True)
    (site / "appcast.xml").write_text("<rss/>", encoding="utf-8")
    pages.publish(site, _installer(tmp_path, "1.0.0"), "1.0.0", keep=1)
    assert (site / "downloads" / "notes").is_dir()
    assert (site / "appcast.xml").read_text(encoding="utf-8") == "<rss/>"


@pytest.mark.parametrize("version", ["0.19.2.dev11", "1.0.0-rc1", "v1.0.0", "1.0.0-beta"])
def test_non_release_versions_are_rejected(tmp_path, version):
    pages = _load()
    with pytest.raises(ValueError):
        pages.publish(tmp_path, _installer(tmp_path, "x"), version)


def test_the_msi_is_hosted_beside_the_installer_and_offered_to_administrators(tmp_path):
    pages = _load()
    site = tmp_path / "site"
    site.mkdir()
    pages.publish(site, _installer(tmp_path, "0.20.0"), "0.20.0")
    assert "MSI package" not in (site / "index.html").read_text(encoding="utf-8")
    msi = tmp_path / "dist" / "lumi-0.21.0.msi"
    msi.write_bytes(b"msi 0.21.0")
    pages.publish(site, _installer(tmp_path, "0.21.0"), "0.21.0", extras=[msi])
    assert (site / "downloads" / "v0.21.0" / "lumi-0.21.0.msi").read_bytes() == b"msi 0.21.0"
    page = (site / "index.html").read_text(encoding="utf-8")
    assert 'href="downloads/v0.21.0/lumi-0.21.0.msi"' in page and "msiexec /i" in page
