"""The release lock, third-party notices, SBOM additions, and the release workflow's
guards: who may sign, what the signing jobs run, and what they publish."""

from __future__ import annotations

import json
import re
import sys
import tomllib
from importlib import metadata
from pathlib import Path

import pytest
import yaml
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
        assert "itself is © Luminary Analytics, all rights reserved" in text
        assert "the Lumi End User License Agreement." in text
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


GITHUB = ROOT / ".github"
WORKFLOWS = GITHUB / "workflows"
SIGNING_ACTION = GITHUB / "actions" / "authenticode-sign" / "action.yml"
# Where the signing action records each file it signs, and the release's check reads them.
RECORD = "${{ runner.temp }}/lumi-authenticode.jsonl"
# Every release.yml job whose output is released: all but `test`.
RELEASED_JOBS = ("build", "release", "macos", "publish-macos")


class _StrictLoader(yaml.SafeLoader):
    """YAML with each key once per mapping. GitHub refuses a workflow that repeats
    one; PyYAML would keep the last, and a guard reading that could miss a grant."""

    def construct_mapping(self, node, deep=False):
        keys = [self.construct_object(key, deep=deep) for key, _ in node.value]
        repeated = sorted({str(key) for key in keys if keys.count(key) > 1})
        if repeated:
            raise yaml.constructor.ConstructorError(None, None, f"repeated keys {repeated}", node.start_mark)
        return super().construct_mapping(node, deep=deep)


def _parse(text: str):
    return yaml.load(text, Loader=_StrictLoader)  # noqa: S506 - a SafeLoader subclass


def _load(path: Path):
    """A workflow or action as GitHub reads it (YAML, whatever its style)."""
    return _parse(path.read_text(encoding="utf-8"))


def _workflows() -> dict[str, dict]:
    """Every workflow GitHub runs, by file name: .yml and .yaml alike."""
    return {path.name: _load(path) for path in sorted([*WORKFLOWS.glob("*.yml"), *WORKFLOWS.glob("*.yaml")])}


def _local_action(uses: str) -> Path:
    """The action a `uses: ./folder` step runs."""
    folder = ROOT / uses.removeprefix("./")
    for name in ("action.yml", "action.yaml"):
        if (folder / name).is_file():
            return (folder / name).resolve()
    raise AssertionError(f"{uses} names no action")


def _actions() -> list[Path]:
    """Every action defined under .github, and any other a workflow runs from this repository."""
    found = {path.resolve() for name in ("action.yml", "action.yaml") for path in GITHUB.rglob(name)}
    for workflow in _workflows().values():
        for job in workflow["jobs"].values():
            found |= {_local_action(step["uses"]) for step in job.get("steps", [])
                      if str(step.get("uses", "")).startswith("./")}
    return sorted(found)


def _job_permissions(workflow: dict, job: dict):
    """A job's token permissions: its own, else the workflow's (None: the repository's default)."""
    return job["permissions"] if "permissions" in job else workflow.get("permissions")


def _grants_oidc(permissions) -> bool:
    """Whether a `permissions` value lets a job ask GitHub for an OIDC token."""
    if permissions is None:
        return False
    if isinstance(permissions, str):
        return permissions.strip().lower() == "write-all"
    if isinstance(permissions, dict):
        return any(str(key).strip().lower() == "id-token" and str(value).strip().lower() != "none"
                   for key, value in permissions.items())
    return True  # not a form GitHub documents: count it as a grant


@pytest.mark.parametrize("text", [
    "permissions: write-all",
    "permissions: {contents: read, id-token: write}",
    "permissions:\n  'id-token': write",
    'permissions:\n  "id-token": "write"',
    "permissions:\n  ID-Token: write",
])
def test_the_oidc_guard_sees_every_way_to_grant_a_token(text):
    assert _grants_oidc(_parse(text)["permissions"])


@pytest.mark.parametrize("text", [
    "permissions: read-all",
    "permissions: {}",
    "permissions: {contents: write, id-token: none}",
    "permissions:\n  contents: read\n  # id-token: write",
])
def test_the_oidc_guard_passes_what_grants_none(text):
    assert not _grants_oidc(_parse(text)["permissions"])


def test_a_key_given_twice_is_refused():
    # GitHub refuses such a workflow; the guards must not read the last one alone.
    with pytest.raises(yaml.constructor.ConstructorError, match="repeated keys"):
        _parse("jobs:\n  build:\n    permissions: {id-token: write}\n    permissions: {contents: read}\n")


def test_every_workflow_sets_its_tokens_permissions():
    # Explicitly, so no job depends on the repository's default token permissions.
    unset = [f"{name}: {job_name}" for name, workflow in _workflows().items()
             for job_name, job in workflow["jobs"].items() if _job_permissions(workflow, job) is None]
    assert not unset


def test_only_the_windows_release_job_can_ask_for_an_oidc_token():
    # The Azure identity that signs as Luminary Analytics trusts GitHub's OIDC
    # subject for the release environment; any job that could get a token in
    # that environment could sign anything (docs/release-pipeline.md).
    granted = []
    for name, workflow in _workflows().items():
        assert not _grants_oidc(workflow.get("permissions")), f"{name} gives every job an OIDC token"
        granted += [(name, job_name) for job_name, job in workflow["jobs"].items()
                    if _grants_oidc(_job_permissions(workflow, job))]
    assert granted == [("release.yml", "release")]
    release = _workflows()["release.yml"]["jobs"]["release"]
    assert release["environment"] == "release"
    assert release["permissions"] == {"contents": "write", "id-token": "write"}


def test_no_action_holds_permissions_or_asks_for_a_token_itself():
    actions = _actions()
    assert SIGNING_ACTION.resolve() in actions
    for path in actions:
        text = path.read_text(encoding="utf-8")
        action = _parse(text)
        assert "permissions" not in action, path
        assert not any("permissions" in step for step in action.get("runs", {}).get("steps", [])), path
        # Only azure/login, pinned, asks for the token; no step reads the request itself.
        assert "ACTIONS_ID_TOKEN_REQUEST" not in text and "getIDToken" not in text, path


# Tools a step would fetch as it runs, from wherever they resolve then.
RUNTIME_INSTALL = re.compile(
    r"\bdotnet(?:\.exe)?\s+(?:tool\s+(?:install|update|restore)|add\s+(?:\S+\s+)?package"
    r"|workload\s+(?:install|update|restore)|new\s+install)\b"
    r"|\bdnx\b|\bnuget(?:\.exe)?\s+(?:install|restore|update)\b"
    r"|\b(?:npm|pnpm|yarn|bun)(?:\.cmd|\.exe)?\s+\w|\b(?:npx|bunx|pnpx|uvx|pipx)\b"
    r"|\b(?:choco|chocolatey|cinst|winget|scoop)(?:\.exe)?\s+\w"
    r"|\b(?:Install|Save|Update)-(?:Module|Package|Script|PSResource)\b"
    r"|\b(?:apt-get|apt|yum|dnf|zypper|apk|brew|port|gem|cargo|go|conda|mamba)\s+(?:install|add|get)\b"
    r"|\buv\s+(?:tool|pip|add|sync|run)\b"
    r"|\b(?:curl|wget|iwr|irm|Invoke-WebRequest|Invoke-RestMethod)\b[^\n]*\|\s*(?:sudo\s+)?(?:ba|z|da)?sh\b"
    r"|\|\s*(?:iex|Invoke-Expression)\b",
    re.IGNORECASE,
)
PIP = re.compile(r"\bpip3?(?:\.exe)?\s+(?:install|download|wheel)\b"
                 r"""|["']pip3?["']\s*,\s*["'](?:install|download)["']""", re.IGNORECASE)
# Scripts that install a tool only after checking its package against a pinned
# SHA-256, from a source holding nothing else (tested below).
HASH_CHECKED_INSTALLERS = {"packaging/fetch_wix.ps1"}
SCRIPT_NAME = re.compile(r"[\w./-]*?[\w-]+\.(?:ps1|sh|py)\b")


def _commands(text: str, suffix: str = "") -> list[str]:
    """A script's commands: comments dropped, continued lines (` or \\ at the end) joined."""
    if suffix == ".ps1":
        text = re.sub(r"<#.*?#>", "", text, flags=re.S)
    commands, current = [], ""
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.endswith(("`", "\\")):
            current += stripped[:-1] + " "
            continue
        commands.append(current + stripped)
        current = ""
    return commands + ([current] if current else [])


def _unchecked_installs(text: str, suffix: str = "") -> list[str]:
    """Commands that install something at run time without checking it against a pinned hash."""
    found = []
    for command in _commands(text, suffix):
        if RUNTIME_INSTALL.search(command):
            found.append(command)
        elif PIP.search(command) and "--require-hashes" not in command and not all(
                flag in command for flag in ("--no-index", "--no-deps", "--no-build-isolation")):
            found.append(command)  # only hash-checked packages, or the local source with no index at all
    return found


def _scripts_named(text: str, near: Path | None = None) -> set[Path]:
    """The build and release scripts (packaging/, scripts/) a text names: from the root, beside ``near``,
    or by name alone."""
    found = set()
    for name in SCRIPT_NAME.findall(text):
        name = name.removeprefix("./")
        candidates = [ROOT / name, *([near.parent / name] if near else []),
                      ROOT / "packaging" / Path(name).name, ROOT / "scripts" / Path(name).name]
        existing = [candidate.resolve() for candidate in candidates if candidate.is_file()
                    and candidate.resolve().parent in (ROOT / "packaging", ROOT / "scripts")]
        if existing:
            found.add(existing[0])
    return found


def _code_a_job_runs(job: dict) -> list[tuple[str, str, str]]:
    """(where, text, suffix) for all a job runs: its steps, the local actions they use (and theirs), and the
    repository scripts any of those name (and theirs)."""
    texts, scripts, seen = [], set(), set()

    def walk(steps: list[dict], where: str) -> None:
        for step in steps:
            if "run" in step:
                texts.append((f"{where}: {step.get('name', '')}", step["run"], ""))
                scripts.update(_scripts_named(step["run"]))
            if str(step.get("uses", "")).startswith("./"):
                action = _local_action(step["uses"])
                if action not in seen:
                    seen.add(action)
                    walk(_load(action)["runs"].get("steps", []), action.relative_to(ROOT).as_posix())

    walk(job.get("steps", []), "workflow")
    done: set[Path] = set()
    while scripts - done:
        script = sorted(scripts - done)[0]
        done.add(script)
        text = script.read_text(encoding="utf-8")
        if script.relative_to(ROOT).as_posix() not in HASH_CHECKED_INSTALLERS:
            texts.append((script.relative_to(ROOT).as_posix(), text, script.suffix))
        scripts |= _scripts_named(text, near=script)
    return texts


@pytest.mark.parametrize("command", [
    "dotnet tool install --global wix --version 5.0.2",
    "python -m pip install --upgrade pip",
    'pip install -e ".[all,dev]"',
    "npm ci",
    "npx some-tool",
    "choco install innosetup",
    "winget install Microsoft.DotNet.SDK.8",
    "Install-Module ArtifactSigning -Force",
    "curl -fsSL https://example.com/install.sh | bash",
    "iwr https://example.com/tool.ps1 | iex",
    "& $python -m pip install --no-deps $repo",
    "python -m pip install `\n    -r requirements.txt",
    'subprocess.run([sys.executable, "-m", "pip", "install", "cryptography"])',
])
def test_the_install_guard_sees_run_time_installs(command):
    assert _unchecked_installs(command, ".ps1")


@pytest.mark.parametrize("command", [
    "python -m pip install --require-hashes -r packaging/tools-requirements.txt",
    '& $python -m pip install --no-cache-dir --require-hashes `\n    -r (Join-Path $repo "requirements.txt")',
    '"$PY" -m pip install --no-cache-dir --no-index --no-deps --no-build-isolation "$ROOT"',
    "# dotnet tool install --global wix",
    "<#\n    dotnet tool install --global wix --version 5.0.2\n#>",
])
def test_the_install_guard_passes_hash_checked_installs(command):
    assert not _unchecked_installs(command, ".ps1")


def test_the_jobs_whose_output_is_released_install_nothing_unchecked():
    # The release job holds the Azure sign-in and the EdDSA key, and the others
    # make what it (or the Developer ID) signs: whatever they run is pinned.
    jobs = _workflows()["release.yml"]["jobs"]
    assert set(jobs) == {"test", *RELEASED_JOBS}
    for name in RELEASED_JOBS:
        code = _code_a_job_runs(jobs[name])
        problems = [f"{where}: {command}" for where, text, suffix in code for command in _unchecked_installs(text, suffix)]
        assert not problems, (name, problems)
        # A fresh Python from setup-python, never a cache another run could have filled.
        for step in jobs[name]["steps"]:
            if str(step.get("uses", "")).startswith("actions/setup-python@"):
                assert "cache" not in step.get("with", {}), name
    # The walk reaches the scripts and the signing action the release job runs.
    reached = {where for where, _, _ in _code_a_job_runs(jobs["release"])}
    assert {"packaging/sign_windows.ps1", "packaging/fetch_artifact_signing.ps1", "packaging/build_msi.ps1",
            "packaging/check_release_files.ps1"} <= reached
    assert any(where.startswith(".github/actions/authenticode-sign/action.yml") for where in reached)


def test_wix_is_installed_only_from_its_checked_package():
    script = (PACKAGING / "fetch_wix.ps1").read_text(encoding="utf-8")
    assert re.search(r'^\$Sha256 = "[0-9a-f]{64}"$', script, re.M)
    check = script.index("if ($actual -ne $Sha256)")
    install = script.index("& dotnet tool install")
    assert script.index("$actual = Get-Sha256 $kept") < check < install
    assert "throw" in script[check:script.index("}", check)]
    # Its only package source is a folder that holds just the checked package.
    assert "<clear />" in script and "--configfile $config" in script[install:script.index("\n", install)]
    release = _workflows()["release.yml"]["jobs"]["release"]
    msi = next(step for step in release["steps"] if step.get("name") == "Build the MSI")
    assert "./packaging/fetch_wix.ps1" in msi["run"] and "-Wix $wix" in msi["run"]


@pytest.mark.parametrize("script", ["scripts/build_clean.ps1", "packaging/build_macos.sh", "packaging/build_linux.sh"])
def test_release_builds_install_only_pinned_packages(script):
    path = ROOT / script
    text = path.read_text(encoding="utf-8")
    assert not _unchecked_installs(text, path.suffix)
    pips = [command for command in _commands(text, path.suffix) if PIP.search(command)]
    assert len(pips) == 2 and all("--no-cache-dir" in command for command in pips)
    assert not any("--upgrade" in command for command in pips)  # the pip that came with the Python


def test_tests_and_the_signed_bytes_run_in_separate_jobs():
    jobs = _workflows()["release.yml"]["jobs"]
    test, build, release = jobs["test"], jobs["build"], jobs["release"]

    def runs(job: dict) -> str:
        return "\n".join(step.get("run", "") for step in job["steps"])

    def uses(job: dict, action: str) -> list[dict]:
        return [step for step in job["steps"] if str(step.get("uses", "")).startswith(action + "@")]

    # `test` runs Ruff and pytest on packages from PyPI, and hands nothing on.
    assert "python -m pytest" in runs(test) and "ruff check" in runs(test) and 'pip install -e ".[all,dev]"' in runs(test)
    assert not uses(test, "actions/upload-artifact") and "outputs" not in test and "environment" not in test
    assert test["permissions"] == {"contents": "read"}
    # `build` makes the bundle from pinned inputs, and runs no tests.
    assert "pytest" not in runs(build) and "ruff" not in runs(build) and "pip install -e" not in runs(build)
    assert "environment" not in build and build["permissions"] == {"contents": "read"}
    assert [step["with"]["name"] for step in uses(build, "actions/upload-artifact")] == ["lumi-windows-bundle"]
    # `release` waits for both, takes only the bundle `build` made, and runs no tests.
    assert sorted(release["needs"]) == ["build", "test"]
    assert [step["with"] for step in uses(release, "actions/download-artifact")] == [
        {"name": "lumi-windows-bundle", "path": "dist"}]
    assert "pytest" not in runs(release)


def test_only_commits_on_main_are_built_or_signed():
    jobs = _workflows()["release.yml"]["jobs"]
    for name in ("build", "release", "macos"):
        checkout, check = jobs[name]["steps"][:2]
        assert checkout["uses"].startswith("actions/checkout@") and checkout["with"]["fetch-depth"] == 0, name
        assert check["name"] == "Check the tag is on main", name
        assert 'tagged="$(git rev-parse "${GITHUB_REF}^{commit}")"' in check["run"]
        assert 'git merge-base --is-ancestor "$tagged" refs/remotes/origin/main' in check["run"]


def test_every_action_is_pinned_to_a_commit():
    files = [WORKFLOWS / name for name in ("release.yml", "build-macos.yml", "build-check.yml")] + _actions()
    for path in files:
        data = _load(path)
        jobs = data.get("jobs", {})
        steps = [step for job in jobs.values() for step in job.get("steps", [])]
        steps += data["runs"].get("steps", []) if isinstance(data.get("runs"), dict) else []
        for action in [str(step["uses"]) for step in steps if "uses" in step] + [
                str(job["uses"]) for job in jobs.values() if "uses" in job]:
            if not action.startswith("./"):
                assert re.fullmatch(r"[\w.-]+/[\w./-]+@[0-9a-f]{40}", action), (path.name, action)
        # With the version it is beside the commit.
        for line in path.read_text(encoding="utf-8").splitlines():
            if re.match(r"\s*(?:- )?uses:\s*(?!\./)[\w.-]+/", line):
                assert re.search(r"@[0-9a-f]{40} # v\d+(\.\d+)*$", line), (path.name, line)


def test_the_release_signs_all_three_files_through_the_signing_action():
    steps = _workflows()["release.yml"]["jobs"]["release"]["steps"]
    signing = [step for step in steps if step.get("uses") == "./.github/actions/authenticode-sign"]
    assert [step["with"]["files"] for step in signing] == [
        "dist/lumi/lumi.exe", "dist/installer/lumi-${{ steps.version.outputs.version }}.msi",
        "dist/installer/lumi-setup-${{ steps.version.outputs.version }}.exe"]
    assert not any("sign_windows.ps1" in step.get("run", "") for step in steps)  # only through the action
    for step in signing:
        # The Azure identity, the account and the expected subject are variables: no secret signs with Azure.
        for key, name in (("azure-client-id", "AZURE_CLIENT_ID"), ("azure-tenant-id", "AZURE_TENANT_ID"),
                          ("azure-subscription-id", "AZURE_SUBSCRIPTION_ID"),
                          ("artifact-signing-endpoint", "ARTIFACT_SIGNING_ENDPOINT"),
                          ("artifact-signing-account", "ARTIFACT_SIGNING_ACCOUNT"),
                          ("artifact-signing-profile", "ARTIFACT_SIGNING_PROFILE"),
                          ("expected-subject", "WINDOWS_SIGN_EXPECTED_SUBJECT"),
                          ("command", "WINDOWS_SIGN_COMMAND"), ("required", "WINDOWS_SIGNING_REQUIRED")):
            assert step["with"][key] == f"${{{{ vars.{name} }}}}", key
        assert step["with"]["pfx-base64"] == "${{ secrets.WINDOWS_SIGN_PFX_BASE64 }}"
    order = {step.get("name"): index for index, step in enumerate(steps)}
    # The build's files are taken first; lumi.exe is signed before the installer wraps it, the
    # installer before its EdDSA signature; what's published is checked after both, before each upload.
    assert (order["Take only the bundle, SBOM and notices from the build"] < order["Authenticode-sign lumi.exe"]
            < order["Build installer with Inno Setup"] < order["Authenticode-sign the installer"]
            < order["Sign installer with EdDSA"] < order["Check the release files"]
            < order["Create GitHub Release with installer"] < order["Check the release files again"]
            < order["Publish installer and appcast to the Pages site"])


def test_the_release_publishes_only_what_it_checked():
    steps = {step.get("name"): step for step in _workflows()["release.yml"]["jobs"]["release"]["steps"]}
    assert "-Handover" in steps["Take only the bundle, SBOM and notices from the build"]["run"]
    for name, id_ in (("Check the release files", "files"), ("Check the release files again", "pages-files")):
        step = steps[name]
        assert step["id"] == id_ and "./packaging/check_release_files.ps1" in step["run"], name
        assert "-Handover" not in step["run"]
        assert step["env"] == {"WINDOWS_SIGN_RECORD": RECORD,
                               "WINDOWS_SIGN_EXPECTED_SUBJECT": "${{ vars.WINDOWS_SIGN_EXPECTED_SUBJECT }}",
                               "WINDOWS_SIGNING_REQUIRED": "${{ vars.WINDOWS_SIGNING_REQUIRED }}"}
    assert steps["Create GitHub Release with installer"]["with"]["files"] == "${{ steps.files.outputs.files }}"
    publish = steps["Publish installer and appcast to the Pages site"]["run"]
    assert '$msi = "${{ steps.pages-files.outputs.msi }}"' in publish and "Test-Path" not in publish
    # The action records each file where the check reads it.
    action = {step.get("name"): step for step in _load(SIGNING_ACTION)["runs"]["steps"]}
    assert action["Sign and check"]["env"]["WINDOWS_SIGN_RECORD"] == RECORD


def test_the_signing_action_signs_in_to_azure_only_when_it_is_configured():
    steps = {step.get("name"): step for step in _load(SIGNING_ACTION)["runs"]["steps"]}
    login = steps["Sign in to Azure with GitHub's OIDC token"]
    assert login["uses"].startswith("azure/login@")
    assert login["if"] == "${{ inputs.azure-client-id != '' && inputs.azure-tenant-id != '' }}"
    assert "Why there's no Azure sign-in" in steps  # the reason, when it's skipped
    sign = steps["Sign and check"]
    assert sign["env"]["ARTIFACT_SIGNING_SIGNED_IN"] == "${{ steps.azure.outcome == 'success' }}"
    assert sign["env"]["WINDOWS_SIGN_EXPECTED_SUBJECT"] == "${{ inputs.expected-subject }}"
    # The dry run runs the same action, and the same check, as a pull request can: no identity, no token.
    workflow = _workflows()["build-check.yml"]
    dry_run = workflow["jobs"]["signing-dry-run"]
    signing = [step for step in dry_run["steps"] if step.get("uses") == "./.github/actions/authenticode-sign"]
    assert len(signing) == 3 and not any("azure-client-id" in step.get("with", {}) for step in signing)
    assert not _grants_oidc(_job_permissions(workflow, dry_run))
    runs = "\n".join(step.get("run", "") for step in dry_run["steps"])
    assert "./packaging/sign_windows.ps1 -CheckTools" in runs and "./packaging/check_release_files.ps1" in runs
