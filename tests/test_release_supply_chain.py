"""The release lock, third-party notices, SBOM additions and signing step."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tomllib
from importlib import metadata
from pathlib import Path

import pytest
from packaging.requirements import Requirement

ROOT = Path(__file__).resolve().parents[1]
PACKAGING = ROOT / "packaging"
sys.path.insert(0, str(PACKAGING))

import third_party_notices as notices  # noqa: E402

LOCK = PACKAGING / "requirements-release.txt"
_PIN = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==([^\s;\\]+)")


def _pins_text(text: str) -> dict[str, str]:
    pins = {}
    for line in text.splitlines():
        match = _PIN.match(line)
        if match:
            pins[notices.normalize(match.group(1))] = match.group(2)
    return pins


def _pins(path: Path) -> dict[str, str]:
    return _pins_text(path.read_text(encoding="utf-8"))


def _release_requirements() -> list[Requirement]:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    specs = list(project["dependencies"])
    for extra in ("gui", "desktop"):
        specs += project["optional-dependencies"][extra]
    specs += [line for line in (PACKAGING / "build-requirements.in").read_text(encoding="utf-8").splitlines()
              if line.strip() and not line.lstrip().startswith("#")]
    return [Requirement(spec) for spec in specs]


class TestReleaseLock:
    def test_every_release_dependency_is_pinned_within_its_range(self):
        pins = _pins(LOCK)
        problems = []
        for requirement in _release_requirements():
            version = pins.get(notices.normalize(requirement.name))
            if version is None:
                problems.append(f"{requirement.name} is not in the lock")
            elif not requirement.specifier.contains(version, prereleases=True):
                problems.append(f"{requirement.name}=={version} does not satisfy {requirement.specifier}")
        assert not problems, "Run `python scripts/lock_release.py`: " + "; ".join(problems)

    @pytest.mark.parametrize("lock", ["requirements-release.txt", "tools-requirements.txt"])
    def test_every_pin_carries_hashes(self, lock):
        text = (PACKAGING / lock).read_text(encoding="utf-8")
        blocks = re.split(r"\n(?=[A-Za-z0-9])", text)
        pinned = [block for block in blocks if _PIN.match(block)]
        assert pinned
        assert all("--hash=sha256:" in block for block in pinned)

    def test_regeneration_command_is_recorded(self):
        assert "python scripts/lock_release.py" in LOCK.read_text(encoding="utf-8")

    def test_audit_sees_every_platforms_pins(self):
        # pip-audit skips requirements whose markers don't match the runner.
        import audit_locks

        sample = (
            "pyobjc-core==12.2.2 ; sys_platform == 'darwin' \\\n"
            "    --hash=sha256:aa\n"
            "pythonnet==3.1.0 ; sys_platform == 'win32' \\\n"
            "    --hash=sha256:bb\n"
            "httpx==0.28.1 \\\n"
            "    --hash=sha256:cc\n"
            "    # via lumi\n"
        )
        assert audit_locks.without_markers(sample) == (
            "pyobjc-core==12.2.2 \\\n    --hash=sha256:aa\n"
            "pythonnet==3.1.0 \\\n    --hash=sha256:bb\n"
            "httpx==0.28.1 \\\n    --hash=sha256:cc\n    # via lumi\n"
        )
        flat = audit_locks.without_markers(LOCK.read_text(encoding="utf-8"))
        assert " ; " not in flat
        assert _pins_text(flat) == _pins(LOCK)


class TestComponents:
    def test_versions_match_what_the_build_fetches(self):
        components = {item["name"]: item for item in notices.load_components()}
        web = (PACKAGING / "fetch_web_assets.ps1").read_text(encoding="utf-8")
        ripgrep = (PACKAGING / "fetch_ripgrep.ps1").read_text(encoding="utf-8")
        spec = (PACKAGING / "lumi.spec").read_text(encoding="utf-8")

        assert f"marked@{components['marked']['version']}/" in web
        assert f"cdn-release@{components['highlight.js']['version']}/" in web
        assert f"dompurify@{components['DOMPurify']['version']}/" in web
        assert f"@fontsource/inter@{components['Inter']['version']}/" in web
        assert components["ripgrep"]["version"] in ripgrep
        assert f"WinSparkle-{components['WinSparkle']['version']}" in spec

    def test_license_files_exist_or_are_fetched(self):
        fetched = {"packaging/ripgrep/LICENSE-MIT", "packaging/ripgrep/UNLICENSE"}
        for component in notices.load_components():
            for relative in component.get("license_files", []):
                assert relative in fetched or (ROOT / relative).is_file(), relative


def _some_distributions():
    # A few real packages rather than the whole development environment, which
    # holds hundreds; pip stands in for build-only tooling.
    return [metadata.distribution(name) for name in ("httpx", "keyring", "pip")]


class TestNotices:
    def test_render_lists_packages_and_components(self):
        packages = notices.python_packages(_some_distributions())
        names = {notices.normalize(item["name"]) for item in packages}
        assert names == {"httpx", "keyring"}
        text = notices.render(packages, notices.load_components())
        assert text.startswith("Lumi third-party notices")
        assert "WinSparkle 0.9.2 — MIT" in text
        httpx = next(item for item in packages if item["name"] == "httpx")
        assert httpx["texts"], "httpx ships its license file"
        assert f"httpx {httpx['version']} — BSD-3-Clause" in text

    def test_packages_left_out_of_the_bundle_are_skipped(self):
        packages = notices.python_packages(_some_distributions(), skip={"Keyring"})
        assert [item["name"] for item in packages] == ["httpx"]

    def test_unreviewed_copyleft_fails(self, tmp_path):
        components = tmp_path / "components.json"
        components.write_text(json.dumps({
            "components": [], "not_shipped": {}, "license_reviews": {"_comment": "", "reviewed-lib": "why"},
        }), encoding="utf-8")
        packages = [
            {"name": "gpl-lib", "version": "1", "license": "GNU General Public License v3 (GPLv3)"},
            {"name": "lgpl-lib", "version": "2", "license": "LGPL-2.1-or-later"},
            {"name": "reviewed_lib", "version": "3", "license": "GPL-3.0-only"},
            {"name": "mit-lib", "version": "4", "license": "MIT"},
            {"name": "bsd-lib", "version": "5", "license": "BSD-3-Clause"},
        ]
        assert notices.copyleft_problems(packages, components) == [
            "gpl-lib 1 is GNU General Public License v3 (GPLv3)",
            "lgpl-lib 2 is LGPL-2.1-or-later",
        ]

    def test_gpl_helpers_are_kept_out_of_the_bundle(self):
        # PyAutoGUI imports the first two optionally, and python3-xlib (Linux
        # only) for X11. The spec reads this list into PyInstaller's excludes,
        # plus Xlib, python3-xlib's import name; the notices skip them.
        assert set(notices.not_shipped()) == {"mouseinfo", "pymsgbox", "python3-xlib"}
        spec = (PACKAGING / "lumi.spec").read_text(encoding="utf-8")
        assert '["not_shipped"]' in spec and "excludes +=" in spec and 'excludes += ["Xlib"]' in spec
        linux = json.loads((PACKAGING / "bundle-policy-linux.json").read_text(encoding="utf-8"))
        assert {"Xlib", "mouseinfo", "pymsgbox"} <= set(linux["forbidden_path_components"])

    def test_cli_writes_the_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr(notices.metadata, "distributions", _some_distributions)
        out = tmp_path / "licenses" / "THIRD_PARTY_NOTICES.txt"
        assert notices.main(["--out", str(out)]) == 0
        assert "ripgrep 15.2.0 — MIT OR Unlicense" in out.read_text(encoding="utf-8")


class TestSbom:
    def test_bundled_components_are_appended_once(self, tmp_path):
        sbom = tmp_path / "sbom.cdx.json"
        sbom.write_text(json.dumps({
            "bomFormat": "CycloneDX", "specVersion": "1.6", "version": 1,
            "components": [{"type": "library", "bom-ref": "pkg:pypi/httpx@0.28.1", "name": "httpx"}],
        }), encoding="utf-8")
        components = notices.load_components()
        assert notices.add_to_sbom(sbom, components) == len(components)
        assert notices.add_to_sbom(sbom, components) == 0

        listed = json.loads(sbom.read_text(encoding="utf-8"))["components"]
        refs = [item["bom-ref"] for item in listed]
        assert len(refs) == len(set(refs)) == len(components) + 1
        ripgrep = next(item for item in listed if item["name"] == "ripgrep")
        assert ripgrep["purl"] == "pkg:github/BurntSushi/ripgrep@15.2.0"
        assert ripgrep["licenses"] == [{"expression": "MIT OR Unlicense"}]


@pytest.mark.skipif(sys.platform != "win32", reason="Authenticode signing runs on Windows")
def test_signing_without_credentials_warns_and_continues(tmp_path):
    target = tmp_path / "lumi.exe"
    target.write_bytes(b"MZ")
    env = {key: value for key, value in os.environ.items()
           if key not in {"WINDOWS_SIGN_PFX_BASE64", "WINDOWS_SIGN_PFX_PASSWORD", "WINDOWS_SIGN_COMMAND"}}
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
         str(PACKAGING / "sign_windows.ps1"), "-Files", str(target)],
        capture_output=True, text=True, timeout=120, env=env,
    )
    assert result.returncode == 0, result.stderr
    assert "Not Authenticode-signed" in result.stdout
    assert target.read_bytes() == b"MZ"
