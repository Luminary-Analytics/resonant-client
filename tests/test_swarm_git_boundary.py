"""Model-written attributes never activate host-configured executable drivers."""

from pathlib import Path
import os
import subprocess
import sys

import pytest

from lumi.engine.swarming.integration import CheckSpec, SwarmIntegration
from lumi.engine.swarming.models import Conflict, ScopeDenied
from tests.test_swarm_integration import setup as integration_fixture, writers, finish, git, approved

setup = integration_fixture


@pytest.mark.parametrize("base_attributes,conditional", [(False, False), (True, False), (True, True)])
def test_executable_filters_never_run_during_writer_or_candidate_effects(setup, tmp_path, base_attributes, conditional):
    project = setup[4]
    if base_attributes:
        (project / ".gitattributes").write_text("*.txt filter=fixture diff=fixture\n")
        git(project, "add", ".gitattributes")
        git(project, "commit", "-m", "Fixture attributes without executable driver")
        setup = (*setup[:5], git(project, "rev-parse", "HEAD"))
    marker = tmp_path / "filter-executed"
    driver = tmp_path / "filter.py"
    driver.write_text(f"from pathlib import Path\nPath({str(marker)!r}).write_text('executed')\n", encoding="utf-8")
    command = f'"{Path(sys.executable).as_posix()}" "{driver.as_posix()}"'
    config_args = ()
    if conditional:
        config = tmp_path / "worktree.config"
        config_args = ("--file", str(config))
        common = (project / git(project, "rev-parse", "--git-common-dir")).resolve()
        git(project, "config", f"includeIf.gitdir:{common.as_posix()}/worktrees/.path", str(config))
    for component in ("clean", "smudge", "process"):
        git(project, "config", *config_args, f"filter.fixture.{component}", command)
    for option in ("diff.external", "diff.fixture.command", "diff.fixture.textconv", "core.fsmonitor"):
        git(project, "config", *config_args, option, command)
    git(project, "config", *config_args, "filter.fixture.required", "true")
    context, writer = writers(setup, names=("a",), roots=(".",))[0]
    path = Path(writer["path"])
    assert (path / "a.txt").read_text() == "base-a\n"
    (path / ".gitattributes").write_text("*.txt filter=fixture diff=fixture\n")
    (path / "a.txt").write_text("new-a\n")
    final = finish(setup, context, writer)
    candidate = setup[3].prepare_candidate(setup[2], writer_ids=(final["id"],),
        required_checks=(CheckSpec("unchanged-contents", (sys.executable, "-c", "pass"), 5),))
    assert candidate["state"] == "ready"
    assert (Path(candidate["path"]) / "a.txt").read_text() == "new-a\n"
    assert (project / "a.txt").read_text() == "base-a\n"
    setup[3].run_check(setup[2], candidate["id"], "unchanged-contents")
    setup[3].apply(setup[2], candidate["id"], approval=approved(setup, candidate))
    assert (project / "a.txt").read_text() == "new-a\n"
    assert not marker.exists()


def test_custom_merge_driver_is_rejected_without_execution(setup, tmp_path):
    context, writer = writers(setup, names=("a",), roots=(".",))[0]
    (Path(writer["path"]) / "a.txt").write_text("new-a\n")
    final = finish(setup, context, writer)
    marker = tmp_path / "merge-executed"
    script = tmp_path / "merge.py"
    script.write_text(f"from pathlib import Path\nPath({str(marker)!r}).write_text('executed')\n")
    git(setup[4], "config", "merge.fixture.driver", f'"{Path(sys.executable).as_posix()}" "{script.as_posix()}"')
    with pytest.raises(Conflict, match="Custom Git merge drivers"):
        setup[3].prepare_candidate(setup[2], writer_ids=(final["id"],),
            required_checks=(CheckSpec("fixture", (sys.executable, "-c", "pass"), 5),))
    assert not marker.exists()


def test_writer_subproject_cannot_expand_to_repository_root(setup, tmp_path):
    nested = setup[4] / "nested-project"
    nested.mkdir()
    with pytest.raises(ScopeDenied, match="Git repository root"):
        SwarmIntegration(setup[0], nested, root=tmp_path / "different-runtime")
    assert (setup[4] / "forbidden.txt").read_text() == "preserve\n"


def test_git_binary_is_pinned_outside_repository_and_worktree_path(setup, tmp_path, monkeypatch):
    project = setup[4]
    local_binary = project / ("git.exe" if os.name == "nt" else "git")
    local_binary.write_bytes(b"This repository executable must never be launched")
    local_binary.chmod(0o755)
    monkeypatch.setenv("PATH", str(project) + os.pathsep + "." + os.pathsep + os.environ["PATH"])
    original = subprocess.Popen
    observed = []
    def launch(command, **kwargs):
        observed.append(command[0])
        return original(command, **kwargs)
    monkeypatch.setattr(subprocess, "Popen", launch)
    integration = SwarmIntegration(setup[0], project, root=tmp_path / "pinned-runtime")
    integration._git(project, "rev-parse", "HEAD")
    assert observed and set(observed) == {integration._git_executable}
    assert Path(integration._git_executable).is_absolute()
    assert not Path(integration._git_executable).is_relative_to(project)
