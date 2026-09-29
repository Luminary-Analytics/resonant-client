"""Removing the worktrees Lumi makes, with real Git and real links.

`git worktree remove --force` on Windows followed a directory junction inside
a team's worktree (npm links a `file:` dependency that way) and deleted the
files it pointed to, outside the repository; `git worktree prune` forgot the
person's own worktree while its folder was away. lumi/worktree_removal.py
removes a worktree itself; these tests hold it to that with a real
repository, real junctions and links, and a folder outside that must survive.
"""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

import pytest

from lumi.worktree_removal import admin_entries, remove_tree, remove_worktree, repository_common_dir


def git(path, *args, check=True):
    environment = {key: value for key, value in os.environ.items() if not key.upper().startswith("GIT_")}
    environment.update(GIT_AUTHOR_NAME="Fixture", GIT_AUTHOR_EMAIL="fixture@example.invalid",
                       GIT_COMMITTER_NAME="Fixture", GIT_COMMITTER_EMAIL="fixture@example.invalid")
    result = subprocess.run(["git", "-c", "commit.gpgsign=false", *args], cwd=path, env=environment,
                            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
    if check:
        assert result.returncode == 0, (args, result.stderr)
    return result


def link_folder(link: Path, target: Path) -> None:
    """A directory junction on Windows (what npm makes for a file: dependency), a symlink elsewhere."""
    if os.name == "nt":
        import _winapi

        _winapi.CreateJunction(str(target), str(link))
    else:
        os.symlink(target, link, target_is_directory=True)


@pytest.fixture
def repo(tmp_path):
    project = tmp_path / "Jöhn Smith" / "project"
    project.mkdir(parents=True)
    git(project, "init", "-b", "main")
    (project / "a.txt").write_text("base\n", encoding="utf-8")
    git(project, "add", ".")
    git(project, "commit", "-m", "base")
    outside = tmp_path / "shared-lib"
    outside.mkdir()
    (outside / "index.js").write_text("module.exports = 'the person\\'s own library';\n", encoding="utf-8")
    return project, outside


def worktree(project: Path, path: Path, *extra: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    git(project, "worktree", "add", *extra, str(path), "HEAD")
    return path


def listed(project: Path) -> list[str]:
    return [line[len("worktree "):] for line in git(project, "worktree", "list", "--porcelain").stdout.splitlines()
            if line.startswith("worktree ")]


def test_a_junction_inside_is_unlinked_and_its_target_kept(repo, tmp_path):
    project, outside = repo
    team = worktree(project, tmp_path / "runtime" / "writer-1", "-b", "lumi/team-1")
    (team / "node_modules").mkdir()
    link_folder(team / "node_modules" / "shared-lib", outside)
    (team / "node_modules" / "shared-lib" / "index.js").read_text(encoding="utf-8")  # a real link

    removal = remove_worktree(team, common_dir=repository_common_dir(project))

    assert removal.removed and removal.links == 1 and not removal.error, removal
    assert not team.exists()
    assert sorted(path.name for path in outside.iterdir()) == ["index.js"]
    assert (outside / "index.js").read_text(encoding="utf-8").startswith("module.exports")
    # Git forgot exactly that worktree: its branch is no longer checked out anywhere.
    assert len(listed(project)) == 1
    assert git(project, "branch", "-D", "lumi/team-1").returncode == 0


def test_a_junction_to_a_folder_with_read_only_files(repo, tmp_path):
    project, outside = repo
    (outside / "locked.txt").write_text("read-only\n", encoding="utf-8")
    os.chmod(outside / "locked.txt", 0o444)
    team = worktree(project, tmp_path / "runtime" / "writer-2", "--detach")
    (team / "built.txt").write_text("generated\n", encoding="utf-8")
    os.chmod(team / "built.txt", 0o444)  # a read-only file of the worktree's own goes
    link_folder(team / "dep", outside)

    removal = remove_worktree(team, common_dir=repository_common_dir(project))

    assert removal.removed, removal
    assert not team.exists()
    assert (outside / "locked.txt").read_text(encoding="utf-8") == "read-only\n"


def test_a_symbolic_link_to_a_file_outside_keeps_the_file(repo, tmp_path):
    project, outside = repo
    team = worktree(project, tmp_path / "runtime" / "writer-3", "--detach")
    try:
        os.symlink(outside / "index.js", team / "linked.js")
    except OSError as exc:  # Windows without Developer Mode or the privilege
        pytest.skip(f"symbolic links unavailable here: {exc}")

    assert remove_worktree(team, common_dir=repository_common_dir(project)).removed
    assert (outside / "index.js").exists()


def test_the_worktree_folder_itself_as_a_link_is_unlinked_not_followed(repo, tmp_path):
    project, outside = repo
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    link_folder(runtime / "writer-4", outside)

    removal = remove_worktree(runtime / "writer-4", common_dir=repository_common_dir(project))

    assert removal.removed and not os.path.lexists(runtime / "writer-4")
    assert (outside / "index.js").exists()


def test_only_that_worktrees_record_goes_not_the_persons_own(repo, tmp_path):
    """Not `git worktree prune`: the person's worktree whose folder is away keeps its record."""
    project, _ = repo
    mine = worktree(project, tmp_path / "mine", "-b", "feature")
    (mine / "work.txt").write_text("in progress\n", encoding="utf-8")
    away = tmp_path / "mine-away"
    mine.rename(away)  # an unplugged drive, a folder being moved
    team = worktree(project, tmp_path / "runtime" / "writer-5", "--detach")

    assert remove_worktree(team, common_dir=repository_common_dir(project)).removed

    away.rename(mine)
    assert git(mine, "status", "--short").stdout.strip() == "?? work.txt"
    assert len(listed(project)) == 2


def test_a_folder_already_gone_has_its_record_removed(repo, tmp_path):
    """An interrupted earlier removal: the folder is gone, Git's record isn't."""
    project, _ = repo
    team = worktree(project, tmp_path / "runtime" / "writer-6", "--detach")
    remove_tree(team)
    assert len(listed(project)) == 2

    removal = remove_worktree(team, common_dir=repository_common_dir(project))

    assert removal.removed and len(removal.admin_entries) == 1
    assert len(listed(project)) == 1


def test_a_locked_worktree_is_left_as_it_is(repo, tmp_path):
    project, _ = repo
    team = worktree(project, tmp_path / "runtime" / "writer-7", "--detach")
    git(project, "worktree", "lock", str(team))

    removal = remove_worktree(team, common_dir=repository_common_dir(project))

    assert removal.locked and not removal.removed and team.exists()
    assert len(listed(project)) == 2


def test_another_repositorys_record_is_never_touched(repo, tmp_path):
    """A worktree whose .git names another repository: its folder goes, the other's record stays."""
    project, _ = repo
    other = tmp_path / "other"
    other.mkdir()
    git(other, "init", "-b", "main")
    (other / "b.txt").write_text("b\n", encoding="utf-8")
    git(other, "add", ".")
    git(other, "commit", "-m", "other")
    theirs = worktree(other, tmp_path / "theirs", "--detach")
    stray = tmp_path / "runtime" / "writer-8"
    stray.mkdir(parents=True)
    (stray / ".git").write_text((theirs / ".git").read_text(encoding="utf-8"), encoding="utf-8")

    assert admin_entries(stray, repository_common_dir(project)) == []
    removal = remove_worktree(stray, common_dir=repository_common_dir(project))

    assert removal.removed and removal.admin_entries == []
    assert git(theirs, "status", "--short").returncode == 0 and len(listed(other)) == 2


def test_an_agent_worktree_with_a_junction_keeps_its_target(repo, tmp_path):
    """WorktreeManager.remove: agents have a shell in their worktree and can leave a link there."""
    from lumi.engine.worktrees import WorktreeManager

    project, outside = repo
    manager = WorktreeManager(project, root=tmp_path / "agent-worktrees")
    lease = manager.create("builder")
    link_folder(Path(lease.path) / "node_modules", outside)
    lease.status = "unchanged"
    manager.remove(lease, delete_branch=True)
    assert not os.path.lexists(lease.path) and len(listed(project)) == 1
    assert (outside / "index.js").exists()
    assert git(project, "branch", "--list", lease.branch).stdout.strip() == ""


def test_a_model_comparison_worktree_with_a_junction_keeps_its_target(repo, tmp_path):
    from lumi import model_evals

    project, outside = repo
    run = worktree(project, tmp_path / "evals" / "1-ollama-model", "--detach")
    link_folder(run / "linked", outside)
    model_evals._remove_worktree(str(project), run)
    assert not os.path.lexists(run) and len(listed(project)) == 1
    assert (outside / "index.js").exists()


def test_the_common_dir_is_found_without_git(repo, tmp_path):
    project, _ = repo
    (project / "src" / "deep").mkdir(parents=True)
    linked = worktree(project, tmp_path / "linked", "--detach")
    expected = (project / ".git").resolve()
    assert repository_common_dir(project / "src" / "deep") == expected
    assert repository_common_dir(linked) == expected
    assert repository_common_dir(tmp_path / "Jöhn Smith") is None


@pytest.mark.skipif(sys.platform != "win32", reason="paths past 260 characters are a Windows limit")
def test_a_deeply_nested_tree_is_removed(tmp_path):
    folder = tmp_path / "node_modules"
    current = folder
    for index in range(30):
        current = current / f"package-number-{index:02d}"
    os.makedirs("\\\\?\\" + str(current))
    with open("\\\\?\\" + str(current / "index.js"), "w", encoding="utf-8") as handle:
        handle.write("deep\n")
    remove_tree(folder)
    assert not folder.exists()
