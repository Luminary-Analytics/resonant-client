"""The codebase index on large repositories: .gitignore, a cap, one read and one parse per file."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from lumi.engine import code_intelligence, rag
from lumi.engine.rag import CodebaseIndex


def _write(root, rel: str, text: str = "def f():\n    return 1\n") -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    for rel in ("src/app.py", "src/util.py", "web/main.ts", "vendor/lib.py", "node_modules/pkg/index.js",
                ".hidden/secret.py", "generated/out.py", "generated/deep/more.py", "notes.log.py"):
        _write(root, rel)
    (root / ".gitignore").write_text("generated/\nnotes.log.py\n", encoding="utf-8")
    return root


@pytest.mark.skipif(not shutil.which("git"), reason="needs git")
def test_a_git_repository_is_listed_by_git_so_gitignore_applies(repo):
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    index = CodebaseIndex(repo)
    stats = index.index()
    assert stats["listing"] == "git"
    # Ignored files are out; so are the directories the index always skips,
    # even when tracked, and hidden ones.
    assert sorted(index._entries) == ["src/app.py", "src/util.py", "web/main.ts"]


@pytest.mark.skipif(not shutil.which("git"), reason="needs git")
def test_a_folder_inside_a_repository_uses_its_gitignore(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    mono = tmp_path / "work" / "mono"
    for rel in ("svc/app.py", "svc/generated/out.py", "other/x.py"):
        _write(mono, rel)
    (mono / ".gitignore").write_text("generated/\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=mono, check=True)
    index = CodebaseIndex(mono / "svc")
    assert index.index()["listing"] == "git"
    assert sorted(index._entries) == ["app.py"]


@pytest.mark.skipif(not shutil.which("git"), reason="needs git")
def test_a_repository_in_the_home_folder_is_not_used(tmp_path, monkeypatch):
    # Dotfiles in Git often ignore everything untracked; that must not empty the index.
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    _write(home, "projects/app/main.py")
    (home / ".gitignore").write_text("*\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=home, check=True)
    index = CodebaseIndex(home / "projects" / "app")
    assert index.index()["listing"] == "walk"
    assert sorted(index._entries) == ["main.py"]


def test_outside_git_the_folder_is_walked(repo):
    index = CodebaseIndex(repo)
    stats = index.index()
    assert stats["listing"] == "walk"
    # Without Git there is no .gitignore to honor; the built-in skips still apply.
    assert "generated/out.py" in index._entries
    assert "vendor/lib.py" not in index._entries and ".hidden/secret.py" not in index._entries


def test_the_index_stops_at_its_cap(tmp_path):
    for n in range(10):
        _write(tmp_path, f"m{n}.py")
    index = CodebaseIndex(tmp_path, max_files=4)
    stats = index.index()
    assert stats["truncated"] is True and stats["total_files"] == 4
    bigger = CodebaseIndex(tmp_path, max_files=100)
    assert "truncated" not in bigger.index() and bigger.file_count == 10


def test_each_changed_file_is_read_and_parsed_once(tmp_path, monkeypatch):
    for n in range(6):
        _write(tmp_path, f"m{n}.py", f"import os\n\nclass C{n}:\n    pass\n")
    reads, parses = [], []
    real_read, real_parse = rag._read_bytes, code_intelligence.parse_code
    monkeypatch.setattr(rag, "_read_bytes", lambda item: reads.append(item[0]) or real_read(item))
    monkeypatch.setattr(code_intelligence, "parse_code", lambda c, lang: parses.append(lang) or real_parse(c, lang))
    index = CodebaseIndex(tmp_path)
    index.index()
    assert sorted(reads) == [f"m{n}.py" for n in range(6)] and len(parses) == 6
    entry = index._entries["m3.py"]
    assert entry.symbols == ["C3"] and entry.imports == ["os"]

    reads.clear()
    index.index()  # nothing changed: nothing is read
    assert reads == []
    _write(tmp_path, "m2.py", "class Changed:\n    pass\n")
    index.index()
    assert reads == ["m2.py"] and index._entries["m2.py"].symbols == ["Changed"]


def test_an_unchanged_index_leaves_the_cache_alone(tmp_path):
    _write(tmp_path, "a.py")
    index = CodebaseIndex(tmp_path)
    index.index()
    cache = tmp_path / ".lumi" / "index.json"
    text = cache.read_text(encoding="utf-8")
    assert "\n" not in text  # compact
    before = cache.stat().st_mtime_ns
    index.index()
    assert cache.stat().st_mtime_ns == before
    assert not list(cache.parent.glob("*.tmp"))
    assert CodebaseIndex(tmp_path).file_count == 1  # and it loads back


def test_a_missing_tree_sitter_is_looked_up_once(monkeypatch):
    monkeypatch.setattr(code_intelligence, "_GET_PARSER", None)
    monkeypatch.setitem(sys.modules, "tree_sitter_language_pack", None)  # makes the import fail
    assert code_intelligence.parse_code("const a = 1;", "javascript").parser == "none"
    assert code_intelligence._GET_PARSER is False
    assert code_intelligence.parse_code("const b = 2;", "typescript").parser == "none"


def test_search_results_use_the_cached_fields(tmp_path):
    _write(tmp_path, "billing/invoice_service.py", "class InvoiceService:\n    def total(self):\n        return 0\n")
    _write(tmp_path, "other.py")
    index = CodebaseIndex(tmp_path)
    index.index()
    results = index.search("invoice service")
    assert results[0].path == "billing/invoice_service.py"
    assert index._entries["billing/invoice_service.py"]._search is not None


def test_an_import_counts_for_its_most_specific_match(tmp_path):
    # Monorepos repeat names; an import of one util module credits that one only.
    for package in ("pkg1", "pkg2", "pkg3", "pkg4"):
        _write(tmp_path, f"{package}/util.py", "def helper():\n    return 1\n")
    _write(tmp_path, "app.py", "from pkg2.util import helper\nimport util\n\ndef main():\n    return helper()\n")
    _write(tmp_path, "web/src/view.ts", "import { helper } from '../../pkg3/util';\nexport const v = 1;\n")
    index = CodebaseIndex(tmp_path)
    index.index()
    repo_map = index.get_repo_map(max_tokens=2000)
    assert "- pkg2/util.py: helper [referenced by 1 file(s)]" in repo_map
    assert "- pkg3/util.py: helper [referenced by 1 file(s)]" in repo_map
    # "import util" names four files: too ambiguous to count for any of them.
    assert "- pkg1/util.py: helper\n" in repo_map + "\n" and "pkg4/util.py: helper [" not in repo_map
