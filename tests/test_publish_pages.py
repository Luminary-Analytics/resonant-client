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
    path = tmp_path / "dist" / f"resonant-setup-{version}.exe"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(f"installer {version}".encode())
    return path


def test_new_installer_is_copied_and_linked(tmp_path):
    pages = _load()
    site = tmp_path / "site"
    site.mkdir()
    target = pages.publish(site, _installer(tmp_path, "0.19.2"), "0.19.2")
    assert target == site / "downloads" / "v0.19.2" / "resonant-setup-0.19.2.exe"
    assert target.read_bytes() == b"installer 0.19.2"
    index = (site / "index.html").read_text(encoding="utf-8")
    assert 'href="downloads/v0.19.2/resonant-setup-0.19.2.exe"' in index
    assert (site / ".nojekyll").exists()


def test_only_the_newest_installers_are_kept_by_numeric_version(tmp_path):
    pages = _load()
    site = tmp_path / "site"
    site.mkdir()
    for version in ("0.9.0", "0.10.0", "0.19.1", "0.19.10"):
        pages.publish(site, _installer(tmp_path, version), version, keep=3)
    kept = sorted(p.name for p in (site / "downloads").iterdir())
    assert kept == ["v0.10.0", "v0.19.1", "v0.19.10"]
    assert "0.19.10" in (site / "index.html").read_text(encoding="utf-8")


def test_unrelated_files_are_left_alone(tmp_path):
    pages = _load()
    site = tmp_path / "site"
    (site / "downloads" / "notes").mkdir(parents=True)
    (site / "appcast.xml").write_text("<rss/>", encoding="utf-8")
    pages.publish(site, _installer(tmp_path, "1.0.0"), "1.0.0", keep=1)
    assert (site / "downloads" / "notes").is_dir()
    assert (site / "appcast.xml").read_text(encoding="utf-8") == "<rss/>"


@pytest.mark.parametrize("version", ["0.19.2.dev11", "1.0.0-rc1", "v1.0.0"])
def test_non_release_versions_are_rejected(tmp_path, version):
    pages = _load()
    with pytest.raises(ValueError):
        pages.publish(tmp_path, _installer(tmp_path, "x"), version)
