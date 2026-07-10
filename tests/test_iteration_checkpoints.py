from __future__ import annotations

import subprocess

from resonant_client.orchestration.checkpoints import IterationCheckpointStore


def _git(repo, *args):
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()


def test_checkpoint_captures_untracked_compare_and_restore(tmp_path):
    _git(tmp_path, "init")
    _git(tmp_path, "config", "user.name", "Test")
    _git(tmp_path, "config", "user.email", "test@example.com")
    tracked = tmp_path / "tracked.txt"
    tracked.write_text("base\n", encoding="utf-8")
    _git(tmp_path, "add", "tracked.txt")
    _git(tmp_path, "commit", "-m", "base")

    tracked.write_text("checkpoint\n", encoding="utf-8")
    (tmp_path / "untracked.txt").write_text("keep me\n", encoding="utf-8")
    store = IterationCheckpointStore(tmp_path)
    checkpoint = store.create(intent_id="mission/one", iteration=1, item_id="T1.1")

    tracked.write_text("broken\n", encoding="utf-8")
    (tmp_path / "untracked.txt").unlink()
    (tmp_path / "new.txt").write_text("failed state\n", encoding="utf-8")

    comparison = store.compare(checkpoint["ref"])
    assert "tracked.txt" in comparison["name_status"]
    assert "new.txt" in comparison["name_status"]
    assert "untracked.txt" in comparison["name_status"]

    restored = store.restore(checkpoint["ref"])
    assert tracked.read_text(encoding="utf-8") == "checkpoint\n"
    assert (tmp_path / "untracked.txt").read_text(encoding="utf-8") == "keep me\n"
    assert not (tmp_path / "new.txt").exists()
    assert restored["recovery_branch"].startswith("resonant-recovery/")
    assert _git(tmp_path, "show", f"{restored['recovery_branch']}:new.txt") == "failed state"
    assert len(store.list()) == 1
