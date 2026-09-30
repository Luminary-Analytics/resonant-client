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
        sparkle = (PACKAGING / "fetch_sparkle.sh").read_text(encoding="utf-8")
        mac = {item["name"]: item for item in notices.load_components(platform="darwin")}
        assert f'SPARKLE_VERSION="{mac["Sparkle"]["version"]}"' in sparkle
        assert mac["Sparkle"]["purl"].endswith("@" + mac["Sparkle"]["version"])
        # Only the macOS build ships it, and only the Windows build WinSparkle.
        assert "Sparkle" not in {item["name"] for item in notices.load_components(platform="win32")}
        assert "WinSparkle" not in mac

    def test_the_sparkle_download_is_pinned_by_hash(self):
        script = (PACKAGING / "fetch_sparkle.sh").read_text(encoding="utf-8")
        assert re.search(r'^SPARKLE_SHA256="[0-9a-f]{64}"$', script, re.M)
        # Verified before extraction, and the build fails on a mismatch.
        check = script.index('if [[ "$ACTUAL" != "$SPARKLE_SHA256" ]]')
        assert script.index('ACTUAL="$(sha256 "$WORK/$ARCHIVE")"') < check < script.index("tar -xJf")
        assert "exit 1" in script[check:script.index("tar -xJf")]
        # Nothing from an earlier run is used unchecked (tests/test_sparkle.py runs it on macOS).
        assert "exit 0" not in script

    def test_license_files_exist_or_are_fetched(self):
        fetched = {"packaging/ripgrep/LICENSE-MIT", "packaging/ripgrep/UNLICENSE", "packaging/sparkle/LICENSE"}
        for platform in ("win32", "darwin", "linux"):
            for component in notices.load_components(platform=platform):
                for relative in component.get("license_files", []):
                    assert relative in fetched or (ROOT / relative).is_file(), relative

    def test_ported_code_keeps_its_license(self):
        # lumi/engine/truncation.py is ported from pi-coding-agent's truncate.ts
        # (MIT): the notices ship the full license, and the file keeps it too.
        pi = next(item for item in notices.load_components() if item["name"] == "pi-coding-agent")
        [(_, text)] = notices.component_texts(pi)
        assert text.startswith("MIT License\n\nCopyright (c) 2025 Mario Zechner\n")
        assert "Permission is hereby granted, free of charge" in text
        source = (ROOT / "lumi" / "engine" / "truncation.py").read_text(encoding="utf-8")
        assert "Copyright (c) 2025 Mario Zechner" in source and "pi-coding-agent" in source


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
        # Lumi's own terms, since the bundle doesn't ship the repository's LICENSE.
        assert "itself is © Luminary Analytics, LLC, all rights reserved" in text
        assert "under the Lumi End User License Agreement." in text
        assert "WinSparkle 0.9.2 — MIT" in text
        assert "pi-coding-agent 0.70.6 (Ported source code)" in text
        assert "Copyright (c) 2025 Mario Zechner" in text
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
        _built_with_pyinstaller(monkeypatch)
        _with_fetched_license_files(monkeypatch, tmp_path)
        out = tmp_path / "licenses" / "THIRD_PARTY_NOTICES.txt"
        assert notices.main(["--out", str(out)]) == 0
        text = out.read_text(encoding="utf-8")
        assert "ripgrep 15.2.0 — MIT OR Unlicense" in text
        assert f"Python {notices.platform.python_version()} — PSF-2.0" in text
        assert "PyInstaller bootloader 6.22.3 — GPL-2.0-or-later WITH Bootloader-exception" in text
        assert "No license file is distributed" not in text


def _built_with_pyinstaller(monkeypatch, version="6.22.3"):
    """The build environment's PyInstaller (packaging/requirements-release.txt), which tests don't install."""
    real = notices.distribution_license

    def distribution_license(name):
        if name == "pyinstaller":
            return version, [("COPYING.txt", "GNU GENERAL PUBLIC LICENSE Version 2 ... Bootloader Exception")]
        return real(name)

    monkeypatch.setattr(notices, "distribution_license", distribution_license)


def _with_fetched_license_files(monkeypatch, tmp_path):
    """Stand-ins for the license files the build fetches (ripgrep's, Sparkle's), which a checkout may not have."""
    fetched = {"packaging/ripgrep/LICENSE-MIT", "packaging/ripgrep/UNLICENSE", "packaging/sparkle/LICENSE"}
    real = notices.component_texts

    def component_texts(component):
        texts = real(component)
        missing = [name for name in component.get("license_files", []) if name in fetched
                   and not (notices.ROOT / name).is_file()]
        return texts + [(Path(name).name, f"{name} as the build fetches it") for name in missing]

    monkeypatch.setattr(notices, "component_texts", component_texts)


class TestLicenseTexts:
    """Every bundled component ships its real license text, for the version that ships (PR #104 review item 11:
    Python, the PyInstaller bootloader, marked, highlight.js, DOMPurify and Inter had none)."""

    def test_the_web_assets_and_font_have_their_license_files_pinned_to_their_version(self):
        components = {item["name"]: item for item in notices.load_components()}
        for name, start in (("marked", "# License information"), ("highlight.js", "BSD 3-Clause License"),
                            ("DOMPurify", "DOMPurify\nCopyright 2023 Dr.-Ing. Mario Heiderich, Cure53"),
                            ("Inter", "Copyright 2020 The Inter Project Authors")):
            component = components[name]
            [relative] = component["license_files"]
            # The file name carries the version it is the text of; a version bump needs the new text.
            assert f"-{component['version']}-" in Path(relative).name, (name, relative)
            [(_filename, text)] = notices.component_texts(component)
            assert text.startswith(start), name
        texts = {name: notices.component_texts(components[name])[0][1]
                 for name in ("marked", "highlight.js", "DOMPurify", "Inter")}
        assert "Permission is hereby granted" in texts["marked"] and "John Gruber" in texts["marked"]
        assert "Apache License" in texts["DOMPurify"] and "Mozilla Public License" in texts["DOMPurify"]
        assert "SIL OPEN FONT LICENSE Version 1.1" in texts["Inter"]

    def test_python_and_the_bootloader_come_from_the_build(self, monkeypatch):
        python = next(item for item in notices.load_components() if item["name"] == "Python")
        # The Python running the build is the one PyInstaller embeds: its version and the LICENSE.txt it ships.
        assert notices.resolved(python)["version"] == notices.platform.python_version()
        [(filename, text)] = notices.component_texts(python)
        assert filename == "LICENSE.txt" and "PYTHON SOFTWARE FOUNDATION LICENSE VERSION 2" in text
        bootloader = next(item for item in notices.load_components() if item["name"] == "PyInstaller bootloader")
        _built_with_pyinstaller(monkeypatch)
        assert notices.resolved(bootloader)["version"] == "6.22.3"
        assert notices.component_texts(bootloader)[0][0] == "COPYING.txt"
        # Any patch release of the pinned minor (the release jobs set up Python 3.13) and major is the pin.
        monkeypatch.setattr(notices.platform, "python_version", lambda: "3.13.9")
        assert notices.version_problems(notices.load_components()) == []

    def test_a_component_without_its_text_or_on_another_version_fails_the_build(self, tmp_path, monkeypatch,
                                                                                capsys):
        monkeypatch.setattr(notices.metadata, "distributions", _some_distributions)
        _with_fetched_license_files(monkeypatch, tmp_path)
        out = tmp_path / "THIRD_PARTY_NOTICES.txt"
        # No PyInstaller in the environment: its bootloader's text is missing.
        monkeypatch.setattr(notices, "distribution_license", lambda name: ("", []))
        assert notices.main(["--out", str(out)]) == 1
        assert "PyInstaller bootloader 6 has no license text" in capsys.readouterr().err and not out.exists()
        # A build on another Python minor or PyInstaller major than the components file pins.
        _built_with_pyinstaller(monkeypatch, version="7.0.0")
        monkeypatch.setattr(notices.platform, "python_version", lambda: "3.14.1")
        assert notices.main(["--out", str(out)]) == 1
        err = capsys.readouterr().err
        assert "Python 3.14.1 isn't 3.13" in err and "PyInstaller bootloader 7.0.0 isn't 6" in err


def _distribution(folder: Path, name: str, version: str, license_file: str = "") -> metadata.Distribution:
    """An installed package's metadata, with a license file in its dist-info or none."""
    info = folder / f"{name}-{version}.dist-info"
    info.mkdir(parents=True)
    (info / "METADATA").write_text(f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\nLicense: MIT\n",
                                   encoding="utf-8")
    record = [f"{info.name}/METADATA,,"]
    if license_file:
        (info / "LICENSE").write_text(license_file, encoding="utf-8")
        record.append(f"{info.name}/LICENSE,,")
    (info / "RECORD").write_text("\n".join(record) + "\n", encoding="utf-8")
    return metadata.PathDistribution(info)


class TestEveryShippedPartHasItsText:
    """Re-review of PR #104: proxy-tools and pygetwindow shipped without license texts, the build checked only
    the non-Python components, proxy-tools was labelled MIT, and WinSparkle's entry left out the OpenSSL and
    wxWidgets code in its DLL."""

    def test_a_shipped_package_without_a_license_text_fails_the_build(self, tmp_path, monkeypatch, capsys):
        bare = _distribution(tmp_path / "site", "bare-lib", "1.0")
        monkeypatch.setattr(notices.metadata, "distributions", lambda: [*_some_distributions(), bare])
        _built_with_pyinstaller(monkeypatch)
        _with_fetched_license_files(monkeypatch, tmp_path)
        out = tmp_path / "THIRD_PARTY_NOTICES.txt"
        assert notices.main(["--out", str(out)]) == 1
        assert "bare-lib 1.0 ships no license text" in capsys.readouterr().err and not out.exists()
        # With its text it ships.
        with_text = _distribution(tmp_path / "site2", "bare-lib", "1.0", "MIT License\n\nCopyright (c) Someone")
        monkeypatch.setattr(notices.metadata, "distributions", lambda: [*_some_distributions(), with_text])
        assert notices.main(["--out", str(out)]) == 0
        assert "Copyright (c) Someone" in out.read_text(encoding="utf-8")

    def test_packages_that_ship_no_text_get_the_committed_one_for_their_version(self, tmp_path):
        committed = notices.committed_package_licenses()
        assert set(committed) == {"proxy-tools", "pygetwindow", "pyobjc-core", "pyobjc-framework-security",
                                  "pyobjc-framework-uniformtypeidentifiers"}
        pins = _pins(LOCK)
        for name, entry in committed.items():
            # The text is the one for the version the release installs.
            assert pins[name] == entry["version"], name
            [relative] = entry["license_files"]
            assert f"-{entry['version']}-" in Path(relative).name, name
            assert notices.component_texts(entry), name
        # proxy-tools says MIT in its metadata; its source and repository say BSD.
        proxy = committed["proxy-tools"]
        [(_, text)] = notices.component_texts(proxy)
        assert proxy["license"] == "BSD-2-Clause" and text.startswith("The BSD License (BSD)")
        assert "Copyright (c) 2013 Armin Ronacher" in text and "Copyright (c) 2014 Jonathan Tushman" in text
        packages = notices.python_packages([_distribution(tmp_path, "proxy_tools", "0.1.0"),
                                            _distribution(tmp_path, "pygetwindow", "0.0.10")])
        assert [item["license"] for item in packages] == ["BSD-2-Clause", "BSD-3-Clause"]
        # A new release of a package whose text is committed needs its own text checked.
        assert notices.package_problems(packages) == [
            "pygetwindow 0.0.10 isn't 0.0.9, the version whose license text python_packages holds"]

    def test_winsparkle_lists_the_libraries_its_dll_links(self):
        winsparkle = next(item for item in notices.load_components() if item["name"] == "WinSparkle")
        texts = dict(notices.component_texts(winsparkle))
        assert "OpenSSL" in winsparkle["license"] and "WxWindows-exception-3.1" in winsparkle["license"]
        assert "Zlib" in winsparkle["license"]
        assert "the OpenSSL License and the original SSLeay license apply" in " ".join(
            texts["openssl-1.0.2u-LICENSE.txt"].split())
        assert "Original SSLeay License" in texts["openssl-1.0.2u-LICENSE.txt"]
        assert texts["wxwidgets-3.2.2.1-licence.txt"].startswith("wxWindows Library Licence, Version 3.1")
        assert "GNU LIBRARY GENERAL PUBLIC LICENSE" in texts["wxwidgets-3.2.2.1-lgpl.txt"]
        assert texts["ed25519-7fa6712-license.txt"].startswith("Copyright (c) 2015 Orson Peters")

    def test_ripgreps_crates_and_pcre2_come_with_their_texts(self):
        components = {item["name"]: item for item in notices.load_components(platform="win32")}
        crates = components["ripgrep's Rust crates and PCRE2"]
        # The file is for the ripgrep that ships; a new ripgrep needs packaging/ripgrep_crate_licenses.py again.
        assert crates["version"] == components["ripgrep"]["version"]
        [(filename, text)] = notices.component_texts(crates)
        assert filename == f"ripgrep-{crates['version']}-crates-LICENSES.txt"
        assert text.startswith(f"Third-party code in ripgrep {crates['version']}'s Windows release (rg.exe)")
        for crate in ("regex-automata 0.4.15", "serde_json 1.0.150", "encoding_rs 0.8.35", "windows-sys 0.61.2"):
            assert f"\n{crate} (" in text, crate
        assert "--- LICENSE-WHATWG ---" in text and "PCRE2 10.45 (BSD-3-Clause WITH PCRE2-exception)" in text
        assert "Philip Hazel" in text

    def test_the_webview2_dlls_pywebview_ships_have_their_text(self):
        webview2 = next(item for item in notices.load_components(platform="win32")
                        if item["name"] == "Microsoft WebView2 SDK")
        # The DLLs come with pywebview: a new pywebview may bring another WebView2 SDK.
        shipped_by = webview2["shipped_by"]
        assert _pins(LOCK)[shipped_by["package"]] == shipped_by["version"]
        texts = dict(notices.component_texts(webview2))
        assert texts["microsoft.web.webview2-1.0.3856.49-LICENSE.txt"].startswith(
            "Copyright (C) Microsoft Corporation. All rights reserved.")
        assert "NOTICES AND INFORMATION" in texts["microsoft.web.webview2-1.0.3856.49-NOTICE.txt"]
        assert "Microsoft WebView2 SDK" not in {item["name"] for item in notices.load_components(platform="darwin")}

    def test_third_party_texts_keep_upstreams_bytes(self):
        # .gitattributes spares them git diff --check, so a trailing blank line upstream stays (DOMPurify's).
        attributes = (ROOT / ".gitattributes").read_text(encoding="utf-8")
        assert "packaging/licenses/** -whitespace" in attributes.splitlines()
        assert (PACKAGING / "licenses" / "dompurify-3.0.6-LICENSE.txt").read_bytes().replace(b"\r\n", b"\n") \
            .endswith(b"Mozilla Public License, v. 2.0.\n\n")


class TestSbom:
    def test_the_bootloader_is_the_locked_pyinstaller_whatever_python_makes_the_sbom(self, tmp_path,
                                                                                   monkeypatch):
        # The SBOM step runs outside the build environment, where PyInstaller may be missing or another.
        _built_with_pyinstaller(monkeypatch, version="6.0.0")
        sbom = tmp_path / "sbom.cdx.json"
        sbom.write_text(json.dumps({"bomFormat": "CycloneDX", "specVersion": "1.6", "version": 1}),
                        encoding="utf-8")
        notices.add_to_sbom(sbom, notices.load_components())
        listed = {item["name"]: item for item in json.loads(sbom.read_text(encoding="utf-8"))["components"]}
        assert listed["PyInstaller bootloader"]["version"] == _pins(LOCK)["pyinstaller"] == "6.22.3"
        # The notices, made in the build environment, name the same version, or the build fails.
        assert notices.version_problems(notices.load_components()) == [
            "PyInstaller bootloader 6.0.0 isn't 6.22.3, the version packaging/requirements-release.txt locks"]

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


@pytest.mark.skipif(sys.platform != "win32", reason="Authenticode signing runs on Windows")
def test_signing_required_without_credentials_fails_the_release(tmp_path):
    # Once a certificate exists, WINDOWS_SIGNING_REQUIRED turns losing it into a failed release.
    target = tmp_path / "lumi.exe"
    target.write_bytes(b"MZ")
    env = {key: value for key, value in os.environ.items()
           if key not in {"WINDOWS_SIGN_PFX_BASE64", "WINDOWS_SIGN_PFX_PASSWORD", "WINDOWS_SIGN_COMMAND"}}
    env["WINDOWS_SIGNING_REQUIRED"] = "true"
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
         str(PACKAGING / "sign_windows.ps1"), "-Files", str(target)],
        capture_output=True, text=True, timeout=120, env=env,
    )
    assert result.returncode != 0
    assert "WINDOWS_SIGNING_REQUIRED" in result.stdout + result.stderr
    assert target.read_bytes() == b"MZ"


def _jobs(workflow: str) -> dict[str, str]:
    """Each job's text in a workflow file, by name."""
    text = (ROOT / ".github" / "workflows" / workflow).read_text(encoding="utf-8")
    parts = re.split(r"^  ([a-z][\w-]*):\n", text.split("\njobs:\n", 1)[1], flags=re.M)
    return dict(zip(parts[1::2], parts[2::2]))


def test_gh_pages_is_published_byte_for_byte_and_checked_before_it_is_pushed():
    # The macOS feeds are signed over their bytes. Git for Windows, where the
    # release publishes, would convert line ends on checkout and commit unless
    # told not to before the checkout, and only push_pages.py checks the
    # staged blobs before pushing.
    release = {name: job for name, job in _jobs("release.yml").items() if "ref: gh-pages" in job}
    rehearsal = _jobs("build-macos.yml")["publish-dry-run"]
    assert set(release) == {"release", "publish-macos"}
    for name, job in [*release.items(), ("publish-dry-run", rehearsal)]:
        setting = job.find("git config --global core.autocrlf false")
        assert 0 <= setting < job.index("ref: gh-pages"), name
    for name, job in release.items():
        assert "python packaging/push_pages.py gh-pages-checkout" in job, name
        assert "git push" not in job and "git add" not in job, name
    assert "scripts/rehearse_pages_publish.py" in rehearsal


def test_no_workflow_asks_for_an_oidc_token():
    # A cloud role that trusts the release environment's identity is only as
    # safe as that environment's tag rule (docs/release-pipeline.md).
    for workflow in (ROOT / ".github" / "workflows").glob("*.yml"):
        assert "id-token" not in workflow.read_text(encoding="utf-8"), workflow.name
