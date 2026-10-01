"""packaging/build_config.py: the feedback address a build of Lumi is made with (RELEASING.md).

release.yml passes the repository variable LUMI_FEEDBACK_URL to the builds as
LUMI_BUILD_FEEDBACK_URL; the script writes lumi/_build_config.py, which
lumi/feedback.py reads as BUILD_DESTINATION. The tests write it elsewhere
(``--out``), never into this checkout's lumi/ folder.
"""

from __future__ import annotations

import importlib.util
import os
import re
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from lumi import feedback

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "packaging" / "build_config.py"


def _script():
    spec = importlib.util.spec_from_file_location("lumi_build_config_script", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run(tmp_path: Path, value: str | None, *args: str) -> subprocess.CompletedProcess:
    env = {key: item for key, item in os.environ.items() if key != "LUMI_BUILD_FEEDBACK_URL"}
    if value is not None:
        env["LUMI_BUILD_FEEDBACK_URL"] = value
    return subprocess.run([sys.executable, str(SCRIPT), "--out", str(tmp_path / "_build_config.py"), *args],
                          env=env, capture_output=True, text=True, timeout=60)


def _written(tmp_path: Path) -> dict:
    namespace: dict = {}
    exec((tmp_path / "_build_config.py").read_text(encoding="utf-8"), namespace)
    return namespace


@pytest.mark.parametrize("value, kept", [
    ("", ""),
    ("  ", ""),
    ("https://feedback.luminary.test/", "https://feedback.luminary.test"),
    ("https://cloud.luminary.test:8443/inbox", "https://cloud.luminary.test:8443/inbox"),
])
def test_an_https_address_is_what_the_build_keeps(value, kept):
    assert _script().feedback_url(value) == kept


@pytest.mark.parametrize("value, complaint", [
    ("http://feedback.luminary.test", "must use https"),
    ("ftp://feedback.luminary.test", "must use https"),
    ("https://ada:secret@feedback.luminary.test", "user name or password"),
    ("https://", "needs a host"),
    ("https://feedback.luminary.test/?to=me", "query"),
    ("https://feedback.luminary.test/#inbox", "fragment"),
    ("https://feedback.luminary.test:port", "port"),
    ("https://feedback.luminary.test/a b", "plain address"),
    ('https://feedback.luminary.test/"', "plain address"),
    ("https://rückmeldung.test", "plain address"),
])
def test_anything_else_stops_the_build(value, complaint):
    with pytest.raises(ValueError, match=re.escape(complaint)):
        _script().feedback_url(value)


def test_the_build_writes_the_address_that_lumi_reads(tmp_path, monkeypatch):
    result = _run(tmp_path, "https://feedback.luminary.test/")
    assert result.returncode == 0, result.stderr
    assert "https://feedback.luminary.test" in result.stdout
    written = _written(tmp_path)
    assert written["FEEDBACK_URL"] == "https://feedback.luminary.test"
    # lumi/feedback.py takes it as the build's own address.
    monkeypatch.setitem(sys.modules, "lumi._build_config", SimpleNamespace(FEEDBACK_URL=written["FEEDBACK_URL"]))
    assert feedback._build_destination() == "https://feedback.luminary.test"

    # The variable empty or unset: a build with no address of its own.
    assert _run(tmp_path, "").returncode == 0 and _written(tmp_path)["FEEDBACK_URL"] == ""
    assert _run(tmp_path, None).returncode == 0 and _written(tmp_path)["FEEDBACK_URL"] == ""

    # One Lumi wouldn't use stops the build, and nothing is written.
    (tmp_path / "_build_config.py").unlink()
    refused = _run(tmp_path, "http://feedback.luminary.test")
    assert refused.returncode == 2 and "must use https" in refused.stderr
    assert not (tmp_path / "_build_config.py").exists()

    # The build removes it when it's done.
    assert _run(tmp_path, "https://feedback.luminary.test").returncode == 0
    assert _run(tmp_path, None, "--clean").returncode == 0
    assert not (tmp_path / "_build_config.py").exists()


def test_a_source_checkout_has_no_address_of_its_own(monkeypatch):
    monkeypatch.setitem(sys.modules, "lumi._build_config", None)  # the module doesn't exist
    assert feedback._build_destination() == ""
    assert not (ROOT / "lumi" / "_build_config.py").exists(), "a build left its configuration in the checkout"


def test_releases_pass_the_repository_variable_and_every_build_writes_then_removes_it():
    jobs = yaml.safe_load((ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8"))["jobs"]
    for job, name in (("build", "Build PyInstaller bundle"), ("macos", "Build Lumi.app, the DMG and the PKG")):
        step = next(item for item in jobs[job]["steps"] if item.get("name") == name)
        assert step["env"]["LUMI_BUILD_FEEDBACK_URL"] == "${{ vars.LUMI_FEEDBACK_URL }}", job
    for script in ("scripts/build_clean.ps1", "packaging/build_macos.sh", "packaging/build_linux.sh"):
        text = (ROOT / script).read_text(encoding="utf-8")
        written = text.index("packaging/build_config.py")
        # Before Lumi is installed and bundled, so both copies hold it.
        assert written < text.index("-m pip install --disable-pip-version-check"), script
        assert re.search(r"(rm -f \"\$ROOT/lumi/_build_config\.py\"|Remove-Item -LiteralPath \$buildConfig)", text), script
    assert "lumi/_build_config.py" in (ROOT / ".gitignore").read_text(encoding="utf-8")
