"""The organization's registry of capability packs (policy ``extensions.registry``, ``registry_only``)."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from lumi import policy
from lumi.engine import pack_install, pack_signing
from lumi.engine.capability_packs import CapabilityPackManager
from tests.test_pack_install import commit_change, make_repo

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")

URL = "https://example.com/acme/packs"
A, B = "a" * 40, "b" * 40


def _policy(entries: list[dict], *, registry_only: bool = True) -> None:
    policy.set_for_tests(policy.parse({"schema": "lumi.policy/v1", "organization": "Acme", "extensions": {
        "registry": entries, "registry_only": registry_only}}, source="test"))


def _entry(commit: str, **extra) -> dict:
    return {"id": "demo-pack", "name": "Demo Pack", "url": URL, "commit": commit, **extra}


def _folder(root: Path, pack_id: str) -> Path:
    folder = root / pack_id
    (folder / "skills").mkdir(parents=True)
    (folder / "skills" / "review.md").write_text("description: Review\nCheck it.", encoding="utf-8")
    (folder / "lumi-pack.json").write_text(json.dumps({"id": pack_id, "name": pack_id, "skills": ["skills/review.md"]}),
                                           encoding="utf-8")
    return folder


def _discover(tmp_path: Path, root: Path, configured: dict | None = None) -> dict:
    manager = CapabilityPackManager(tmp_path / "no-project", roots=[root], configured=configured or {})
    return {pack.id: pack for pack in manager.discover()}


def test_the_policy_checks_registry_entries():
    for bad in ({"id": "x", "url": "http://example.com/r", "commit": A}, {"id": "x", "url": URL, "commit": "main"},
                {"id": "x", "url": URL, "commit": A, "digest": "zz"}, {"id": "x", "url": URL, "commit": A,
                                                                        "subdir": "../up"}, "not an entry"):
        with pytest.raises(policy.PolicyError, match="extensions.registry"):
            _policy([bad])
    _policy([_entry(A.upper(), subdir="/packs/demo/")])
    entry = policy.current().registry["demo-pack"]
    assert (entry["commit"], entry["subdir"], entry["digest"]) == (A, "packs/demo", "")
    assert (policy.current().summary()["registry_packs"], policy.current().summary()["registry_only"]) == \
        (["demo-pack"], True)


def test_registry_only_keeps_other_packs_and_versions_off(tmp_path):
    root = tmp_path / "packs"
    demo = _folder(root, "demo-pack")
    _folder(root, "other")
    _policy([_entry(A, digest=pack_signing.signed_digest(demo))])
    packs = _discover(tmp_path, root)
    assert packs["demo-pack"].problem == ""
    assert packs["demo-pack"].registry == {"organization": "Acme", "commit": A, "matches": True}
    assert packs["other"].problem == "Acme's policy allows only packs from its registry."

    (demo / "skills" / "review.md").write_text("description: Review\nchanged", encoding="utf-8")
    assert _discover(tmp_path, root)["demo-pack"].problem == \
        "Acme's registry pins this pack at commit aaaaaaaaaaaa. Install that version from the registry."

    # Without a pinned digest, where the pack was installed from decides.
    _policy([_entry(A)])
    source = {"type": "git", "url": URL + ".git", "commit": A, "subdir": ""}
    assert _discover(tmp_path, root, {"demo-pack": {"source": source}})["demo-pack"].registry["matches"] is True
    for other in ({**source, "commit": B}, {**source, "url": "https://example.com/else/packs"},
                  {**source, "subdir": "packs"}):
        assert _discover(tmp_path, root, {"demo-pack": {"source": other}})["demo-pack"].problem.startswith(
            "Acme's registry pins this pack")
    assert _discover(tmp_path, root)["demo-pack"].registry["matches"] is False

    # Without registry_only, the registry names packs and turns nothing off.
    _policy([_entry(A)], registry_only=False)
    assert {pack_id: pack.problem for pack_id, pack in _discover(tmp_path, root).items()} == \
        {"demo-pack": "", "other": ""}


def test_extension_check_prints_the_digest_a_registry_pins(tmp_path, capsys):
    from lumi.extension_check import main

    folder = _folder(tmp_path / "packs", "demo-pack")
    assert main(["check", str(folder), "--no-run"]) == 0
    assert f"Content digest: {pack_signing.signed_digest(folder)}" in capsys.readouterr().out


class TestInSettings:
    @pytest.fixture
    def state(self, monkeypatch, tmp_path):
        from tests.test_capability_pack_trust import _app_state

        project = tmp_path / "project"
        project.mkdir()
        return _app_state(monkeypatch, project)

    @pytest.fixture
    def repo(self, monkeypatch, tmp_path):
        repo, first = make_repo(tmp_path)
        real = pack_install.install_from_git
        # The registry's https address is served from a local repository here.
        monkeypatch.setattr(pack_install, "install_from_git", lambda url, commit, **kw: real(
            str(repo) if url == URL else url, commit, allow_local=True, **kw))
        return repo, first

    def _install(self, state, pack_id="demo-pack") -> dict:
        from tests.test_capability_pack_trust import _send

        return [reply for reply in _send(state, "capability_pack_install_registry", {"pack_id": pack_id})
                if reply["event"] == "capability.pack_list"][0]

    def test_installing_and_updating_the_pinned_version(self, state, repo):
        from tests.test_capability_pack_trust import _send

        repo_path, first = repo
        _policy([_entry(first)])
        [listed] = _send(state, "capability_pack_list", {})
        assert [(e["id"], e["installed"], e["matches"]) for e in listed["registry"]] == [("demo-pack", False, False)]
        assert (listed["organization"], listed["registry_only"]) == ("Acme", True)

        listed = self._install(state)
        [pack] = [p for p in listed["packs"] if p["id"] == "demo-pack"]
        # Installed at the pinned commit, and still waiting for the person's approval.
        assert (pack["status"], pack["problem"], pack["registry"]["matches"]) == ("needs_approval", "", True)
        assert [(e["installed"], e["matches"]) for e in listed["registry"]] == [(True, True)]
        source = state.settings.get("plugins")["demo-pack"]["source"]
        assert (source["url"], source["commit"], source["registry"]) == (URL, first, "Acme")

        # The organization pins a newer commit: the old one is off until that version is installed.
        second = commit_change(repo_path, "Changed.")
        _policy([_entry(second)])
        [listed] = _send(state, "capability_pack_list", {})
        [pack] = [p for p in listed["packs"] if p["id"] == "demo-pack"]
        assert pack["problem"].startswith("Acme's registry pins this pack at commit") and pack["status"] == "unverifiable"
        listed = self._install(state)
        [pack] = [p for p in listed["packs"] if p["id"] == "demo-pack"]
        assert (pack["problem"], state.settings.get("plugins")["demo-pack"]["source"]["commit"]) == ("", second)

    def test_what_a_registry_install_refuses(self, state, repo):
        repo_path, first = repo
        _policy([_entry(first)])
        self._install(state)
        # A content digest that doesn't match leaves the installed version where it was.
        _policy([_entry(first, digest="0" * 64)])
        assert "don't match the content digest" in self._install(state)["error"]
        assert state.settings.get("plugins")["demo-pack"]["source"]["commit"] == first
        assert "isn't in your organization's registry" in self._install(state, "nope")["error"]
        _policy([{**_entry(first), "id": "other-pack"}])
        assert "holds the pack demo-pack, not other-pack" in self._install(state, "other-pack")["error"]
