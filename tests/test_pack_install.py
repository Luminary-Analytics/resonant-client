"""Capability packs installed from a git repository, pinned to one commit (lumi/engine/pack_install.py)."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from lumi import policy
from lumi.engine import pack_install
from lumi.engine.capability_packs import CapabilityPackManager
from lumi.engine.pack_install import PackInstallError, install_from_git, normalize_url, resolve, source_allowed

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")


def git(repo: Path, *args: str) -> str:
    env = {**os.environ, "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "t@example.com",
           "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "t@example.com"}
    return subprocess.run(["git", *args], cwd=repo, env=env, check=True, capture_output=True, text=True).stdout.strip()


def make_repo(tmp_path: Path, *, manifest: dict | None = None, subdir: str = "") -> tuple[Path, str]:
    repo = tmp_path / "remote"
    pack = repo / subdir if subdir else repo
    pack.mkdir(parents=True)
    git(repo, "init", "--quiet")
    data = manifest if manifest is not None else {
        "id": "demo-pack", "name": "Demo Pack", "version": "1.0.0", "skills": ["skills/review.md"],
        "hooks": [{"hook_type": "session_start", "command": "python hooks/start.py"}]}
    (pack / "lumi-pack.json").write_text(json.dumps(data), encoding="utf-8")
    (pack / "skills").mkdir()
    (pack / "skills" / "review.md").write_text("description: Review\nCheck the diff.", encoding="utf-8")
    (pack / "hooks").mkdir()
    (pack / "hooks" / "start.py").write_text("print('ready')\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "--quiet", "-m", "first")
    return repo, git(repo, "rev-parse", "HEAD")


def commit_change(repo: Path, text: str) -> str:
    (repo / "skills" / "review.md").write_text(f"description: Review\n{text}", encoding="utf-8")
    git(repo, "commit", "--quiet", "-am", text)
    return git(repo, "rev-parse", "HEAD")


class TestAddresses:
    @pytest.mark.parametrize("url", ["https://github.com/acme/packs", "https://GitHub.com/acme/packs.git/"])
    def test_https_repositories(self, url):
        assert normalize_url(url).startswith("https://github.com/acme/packs")

    @pytest.mark.parametrize(("url", "message"), [
        ("http://github.com/acme/packs", "https"), ("git@github.com:acme/packs.git", "https"),
        ("file:///tmp/packs", "https"), ("https://user:token@github.com/acme/packs", "credentials"),
        ("https://github.com/acme/packs?x=1", "query"), ("https://github.com", "repository path"),
        ("C:/repos/packs", "https"),
    ])
    def test_everything_else_is_refused(self, url, message):
        with pytest.raises(PackInstallError, match=message):
            normalize_url(url)

    def test_the_organizations_sources(self):
        assert source_allowed("https://github.com/acme/packs", None)
        assert source_allowed("https://github.com/acme/packs.git", ("https://github.com/acme/*",))
        assert not source_allowed("https://github.com/other/packs", ("https://github.com/acme/*",))
        parsed = policy.parse({"schema": policy.SCHEMA, "extensions": {"allowed_sources": ["https://github.com/acme/*"]}},
                              source="test")
        assert parsed.sources_allowed == ("https://github.com/acme/*",)


class TestInstalling:
    def test_tags_and_branches_resolve_to_the_commit_they_name(self, tmp_path):
        repo, first = make_repo(tmp_path)
        git(repo, "tag", "v1")
        git(repo, "tag", "-a", "v1-annotated", "-m", "release")
        branch = git(repo, "rev-parse", "--abbrev-ref", "HEAD")
        second = commit_change(repo, "Newer.")
        assert resolve(str(repo), "v1", allow_local=True) == first
        assert resolve(str(repo), "v1-annotated", allow_local=True) == first  # the commit, not the tag object
        assert resolve(str(repo), branch, allow_local=True) == second
        assert resolve(str(repo), first.upper(), allow_local=True) == first
        with pytest.raises(PackInstallError, match="isn't a tag or branch"):
            resolve(str(repo), "v9", allow_local=True)
        with pytest.raises(PackInstallError, match="commit"):
            resolve(str(repo), "--upload-pack=evil", allow_local=True)

    def test_a_pinned_commit_installs_without_trust(self, tmp_path):
        repo, first = make_repo(tmp_path)
        commit_change(repo, "Newer.")  # the pin, not the branch tip, is installed
        packs = tmp_path / "packs"
        installed = install_from_git(str(repo), first, dest_root=packs, allow_local=True)
        assert (installed.id, installed.commit, Path(installed.path)) == ("demo-pack", first, packs / "demo-pack")
        assert (packs / "demo-pack" / "skills" / "review.md").read_text(encoding="utf-8").endswith("Check the diff.")
        assert not (packs / "demo-pack" / ".git").exists()
        assert [p.name for p in packs.iterdir()] == ["demo-pack"]  # no scratch left behind
        [pack] = CapabilityPackManager(tmp_path / "project", roots=(packs,)).discover()
        assert (pack.id, pack.trusted, pack.enabled, pack.status) == ("demo-pack", False, False, "needs_approval")

    def test_what_is_refused(self, tmp_path):
        repo, first = make_repo(tmp_path)
        packs = tmp_path / "packs"
        with pytest.raises(PackInstallError, match="full commit"):
            install_from_git(str(repo), "main", dest_root=packs, allow_local=True)
        with pytest.raises(PackInstallError, match="No lumi-pack.json in tools/"):
            install_from_git(str(repo), first, subdir="tools", dest_root=packs, allow_local=True)
        with pytest.raises(PackInstallError, match="plain path"):
            install_from_git(str(repo), first, subdir="../elsewhere", dest_root=packs, allow_local=True)
        with pytest.raises(PackInstallError, match="policy"):
            install_from_git("https://github.com/other/packs", first, dest_root=packs,
                             allowed_sources=("https://github.com/acme/*",))
        with pytest.raises(PackInstallError, match="Git failed"):
            install_from_git(str(repo), "0" * 40, dest_root=packs, allow_local=True)
        assert not packs.exists() or list(packs.iterdir()) == []

    def test_a_manifest_needs_its_own_id(self, tmp_path):
        repo, first = make_repo(tmp_path, manifest={"name": "No id"})
        with pytest.raises(PackInstallError, match="needs an id"):
            install_from_git(str(repo), first, dest_root=tmp_path / "packs", allow_local=True)

    def test_a_pack_in_a_subfolder(self, tmp_path):
        repo, first = make_repo(tmp_path, subdir="packs/demo")
        installed = install_from_git(str(repo), first, subdir="packs/demo", dest_root=tmp_path / "packs",
                                     allow_local=True)
        assert Path(installed.path).name == "demo-pack" and installed.subdir == "packs/demo"

    @pytest.mark.skipif(sys.platform == "win32", reason="symbolic links need privileges on Windows")
    def test_symbolic_links_are_refused(self, tmp_path):
        repo, _ = make_repo(tmp_path)
        (repo / "secret").symlink_to("/etc/hosts")
        git(repo, "add", "-A")
        git(repo, "commit", "--quiet", "-m", "link")
        with pytest.raises(PackInstallError, match="symbolic links"):
            install_from_git(str(repo), git(repo, "rev-parse", "HEAD"), dest_root=tmp_path / "packs",
                             allow_local=True, )


class TestInSettings:
    """The Settings flow: install, review and approve, reinstall, remove."""

    @pytest.fixture
    def state(self, monkeypatch, tmp_path):
        from tests.test_capability_pack_trust import _app_state

        real_resolve, real_install = pack_install.resolve, pack_install.install_from_git
        # The app accepts only https; these tests fetch from a local repository.
        monkeypatch.setattr(pack_install, "resolve", lambda url, ref: real_resolve(url, ref, allow_local=True))
        monkeypatch.setattr(pack_install, "install_from_git",
                            lambda url, commit, **kw: real_install(url, commit, allow_local=True, **kw))
        project = tmp_path / "project"
        project.mkdir()
        return _app_state(monkeypatch, project)

    def test_install_review_approve_reinstall_and_remove(self, state, tmp_path):
        from lumi import audit
        from lumi.audit import AuditLog
        from tests.test_capability_pack_trust import _send

        log = AuditLog(tmp_path / "audit")
        audit.set_for_tests(log)
        repo, first = make_repo(tmp_path)
        git(repo, "tag", "v1")
        replies = _send(state, "capability_pack_install", {"url": str(repo), "ref": "v1"})
        [listed] = [r for r in replies if r["event"] == "capability.pack_list"]
        [pack] = [p for p in listed["packs"] if p["id"] == "demo-pack"]
        assert pack["status"] == "needs_approval" and pack["hooks"][0]["command"] == "python hooks/start.py"
        assert pack["source"]["commit"] == first and pack["source"]["type"] == "git"
        assert any("It's off until you review" in r.get("message", "") for r in replies)
        assert state.settings.get("plugins")["demo-pack"]["source"]["url"] == str(repo)

        _send(state, "capability_pack_approve", {"pack_id": pack["id"], "path": pack["path"], "digest": pack["digest"]})
        [pack] = [p for p in _send(state, "capability_pack_list", {})[0]["packs"] if p["id"] == "demo-pack"]
        assert pack["status"] == "approved"

        # A new commit is new content: installed, but off until reviewed again.
        second = commit_change(repo, "Changed.")
        replies = _send(state, "capability_pack_install", {"url": str(repo), "ref": second})
        [pack] = [p for p in replies[0]["packs"] if p["id"] == "demo-pack"]
        assert (pack["status"], pack["source"]["commit"]) == ("needs_approval", second)

        replies = _send(state, "capability_pack_remove", {"pack_id": "demo-pack"})
        assert all(p["id"] != "demo-pack" for p in replies[0]["packs"])
        assert "demo-pack" not in (state.settings.get("plugins") or {})
        records = [json.loads(line) for path in log._files() for line in path.read_text(encoding="utf-8").splitlines()]
        assert [(r["type"], r["data"].get("commit")) for r in records if r["type"].startswith("extension.")] == [
            ("extension.install", first), ("extension.install", second), ("extension.remove", second)]

    def test_refusals_come_back_on_the_page(self, state, tmp_path):
        from tests.test_capability_pack_trust import _send

        [reply] = _send(state, "capability_pack_install", {"url": "http://example.com/packs", "ref": "v1"})
        assert "https" in reply["error"]
        [reply] = _send(state, "capability_pack_remove", {"pack_id": "not-installed"})
        assert "installed from Git" in reply["error"]
