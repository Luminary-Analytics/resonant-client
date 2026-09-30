"""glob leaves out what grep does: Git's own folder, and what's inside folders .gitignore excludes.

`glob **/*` in a repository used to list every object and hook under .git
before the project's own files (tools._exec_glob was a plain Path.glob).
grep searches with ripgrep's `--glob !.git/` and its .gitignore handling;
glob now matches it, unless the search names the folder.
"""

from __future__ import annotations

import time

import pytest

from lumi.engine.tools import _exec_glob, execute_tool


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "proj"
    for name in ("src/app.py", "src/util.py", "README.md", ".github/workflows/ci.yml",
                 ".git/HEAD", ".git/config", ".git/objects/ab/cdef0123", ".git/hooks/pre-commit.sample",
                 "node_modules/react/index.js", "node_modules/react/package.json",
                 "web/node_modules/lodash/lodash.js", "build/out.js", "dist/app.min.js", ".venv/Lib/site.py"):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x", encoding="utf-8")
    (root / ".gitignore").write_text("node_modules/\n/build\n.venv\n*.log\n# a comment\n", encoding="utf-8")
    return root


def _glob(root, pattern, path=None):
    result = _exec_glob({"pattern": pattern, "path": str(path or root), "limit": 200}, time.time(),
                        project_path=str(root))
    listed = [line.replace("\\", "/") for line in result.output.splitlines() if line and not line.startswith("[")]
    return result, listed


def test_everything_leaves_out_git_and_what_ignored_folders_hold(project):
    result, listed = _glob(project, "**/*")
    assert not any(path == ".git" or path.startswith(".git/") for path in listed)
    assert not any("node_modules/" in path or path.startswith(("build/", ".venv/")) for path in listed)
    # The project's own files, dotfiles such as .github and .gitignore included, as grep searches them.
    assert {"src/app.py", "src/util.py", "README.md", ".github/workflows/ci.yml", ".gitignore",
            "dist/app.min.js"} <= set(listed)
    # An ignored folder itself still shows, so the agent knows it's there.
    assert {"node_modules", "build", ".venv", "web/node_modules"} <= set(listed)
    hidden = result.metadata["ignored"]
    assert hidden > 0 and result.metadata["count"] == len(listed)
    assert f"[{hidden} paths in .git or in folders .gitignore excludes not shown" in result.output


def test_a_search_that_names_the_folder_lists_it(project):
    _, listed = _glob(project, ".git/**/*")
    assert {".git/HEAD", ".git/config", ".git/objects/ab/cdef0123"} <= set(listed)
    _, listed = _glob(project, "**/node_modules/**/*.js")
    assert {"node_modules/react/index.js", "web/node_modules/lodash/lodash.js"} <= set(listed)
    # Or its path does.
    _, listed = _glob(project, "*", path=project / ".git")
    assert {".git/HEAD", ".git/config"} <= set(listed)
    _, listed = _glob(project, "**/*.js", path=project / "node_modules")
    assert listed == ["node_modules/react/index.js"]


def test_a_gitignore_with_a_negation_isnt_followed_for_folders(project):
    # Which folders `/*` then `!/src/` keep needs Git's full rules: show too much rather than hide files.
    (project / ".gitignore").write_text("/*\n!/src/\n", encoding="utf-8")
    _, listed = _glob(project, "**/*")
    assert "src/app.py" in listed and "node_modules/react/index.js" in listed
    assert not any(path.startswith(".git/") for path in listed)


def test_without_a_gitignore_only_git_is_left_out(tmp_path):
    root = tmp_path / "plain"
    for name in (".git/HEAD", "node_modules/x.js", "a.py"):
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text("x", encoding="utf-8")
    result = execute_tool("glob", {"pattern": "**/*", "path": str(root)}, project_path=str(root))
    listed = sorted(line.replace("\\", "/") for line in result.output.splitlines() if line and not line.startswith("["))
    assert listed == ["a.py", "node_modules", "node_modules/x.js"]
    assert "[2 paths in .git" in result.output  # the folder itself too: nothing in it is the project's
