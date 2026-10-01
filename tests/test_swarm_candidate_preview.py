"""Real immutable Git previews, hostile drivers and bounded subprocess output."""

from dataclasses import replace
from pathlib import Path
import subprocess
import sys
import time

import pytest

from lumi.engine.swarming.integration import CheckSpec
from lumi.engine.swarming.integration_preview import _git_bytes
from lumi.engine.swarming.models import Conflict, ScopeDenied
from tests.test_swarm_integration import setup as integration_fixture, writers, finish, git

setup = integration_fixture


def candidate(setup, files):
    _, _, authority, integration, _, _ = setup
    context, writer = writers(setup, names=("review",), roots=(".",))[0]
    for name, content in files.items():
        path = Path(writer["path"]) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    finalized = finish(setup, context, writer)
    return integration.prepare_candidate(authority, writer_ids=(finalized["id"],),
        required_checks=(CheckSpec("preview", (sys.executable, "-c", "pass"), 5),))


def preview(setup, record, **kwargs):
    return setup[3].inspect_candidate(setup[2].scope, setup[2].run_id, record["id"], **kwargs)


def test_preview_preserves_unicode_and_remains_readable_after_lease_expiry(setup):
    record = candidate(setup, {"résumé-日本語.txt": "Résumé and 日本語\n", "a.txt": "changed-a\n"})
    before = setup[0].snapshot(setup[2].scope, setup[2].run_id)
    setup[0].clock = lambda: 100000
    result = preview(setup, record)
    assert result["base_revision"] == record["base_revision"]
    assert result["result_revision"] == record["result_revision"]
    assert result["changed_paths"] == ["a.txt", "résumé-日本語.txt"]
    assert "+Résumé and 日本語" in result["diff"] and "+changed-a" in result["diff"]
    assert not result["diff_truncated"] and not result["changed_paths_truncated"]
    assert result["errors"] == []
    assert setup[0].snapshot(setup[2].scope, setup[2].run_id) == before


def test_large_diff_and_path_inventory_are_bounded_with_explicit_truncation(setup):
    files = {f"many/{number:03}.txt": "content\n" for number in range(220)}
    files["000-large.txt"] = "Résumé 日本語\n" * 100000
    record = candidate(setup, files)
    result = preview(setup, record, max_diff_bytes=301)
    assert result["diff_truncated"] and len(result["diff"].encode("utf-8")) <= 301
    assert result["changed_paths_truncated"] and len(result["changed_paths"]) == 200


def test_candidate_preview_never_executes_configured_diff_textconv_or_fsmonitor(setup, tmp_path, monkeypatch):
    record = candidate(setup, {"a.txt": "new content\n", ".gitattributes": "*.txt diff=fixture filter=fixture\n"})
    marker = tmp_path / "driver-ran"
    driver = tmp_path / "fixture_driver.py"
    driver.write_text(f"from pathlib import Path\nPath({str(marker)!r}).write_text('ran')\n", encoding="utf-8")
    command = f'"{Path(sys.executable).as_posix()}" "{driver.as_posix()}"'
    for option in ("diff.fixture.command", "diff.fixture.textconv", "diff.external", "core.fsmonitor", "core.pager",
                   "filter.fixture.clean", "filter.fixture.smudge", "filter.fixture.process"):
        git(setup[4], "config", option, command)
    git(setup[4], "config", "filter.fixture.required", "true")
    (Path(record["path"]) / "a.txt").write_text("new content\n")
    monkeypatch.setenv("GIT_EXTERNAL_DIFF", command)
    result = preview(setup, record)
    assert "+new content" in result["diff"]
    assert not marker.exists()


def test_dirty_candidate_is_rejected_without_returning_a_committed_patch(setup):
    record = candidate(setup, {"a.txt": "new content\n"})
    (Path(record["path"]) / "a.txt").write_text("unobserved later edit\n")
    with pytest.raises(Conflict, match="changed"):
        preview(setup, record)


def test_candidate_changed_during_diff_is_rejected(setup, monkeypatch):
    import lumi.engine.swarming.integration_preview as module
    record = candidate(setup, {"a.txt": "new content\n"})
    original = module._git_bytes
    def mutate(integration, path, args, **kwargs):
        value = original(integration, path, args, **kwargs)
        if args[0] == "diff" and "--name-only" not in args:
            (path / "a.txt").write_text("changed during inspection\n")
        return value
    monkeypatch.setattr(module, "_git_bytes", mutate)
    with pytest.raises(Conflict, match="changed"):
        preview(setup, record)


def test_foreign_scope_cannot_start_candidate_git_inspection(setup, monkeypatch):
    import lumi.engine.swarming.integration_preview as module
    record = candidate(setup, {"a.txt": "new content\n"})
    monkeypatch.setattr(module, "_git_bytes", lambda *args, **kwargs: pytest.fail("Foreign scope reached Git"))
    with pytest.raises(ScopeDenied):
        setup[3].inspect_candidate(replace(setup[2].scope, owner_id="other"), setup[2].run_id, record["id"])


@pytest.mark.parametrize("mode", ["flood", "stall"])
def test_git_preview_caps_live_output_and_terminates_stalled_process(setup, tmp_path, monkeypatch, mode):
    import lumi.engine.swarming.integration_preview as module
    script = tmp_path / "fixture_output.py"
    script.write_text("import sys,time\n" + ("sys.stdout.buffer.write(b'x'*8000000)\nsys.stdout.flush()\n" if mode == "flood" else "")
                      + "time.sleep(60)\n", encoding="utf-8")
    original = subprocess.Popen
    processes = []
    def launch(command, **kwargs):
        process = original([sys.executable, str(script)], **kwargs)
        processes.append(process)
        return process
    monkeypatch.setattr(module.subprocess, "Popen", launch)
    started = time.monotonic()
    if mode == "flood":
        data, truncated = _git_bytes(setup[3], setup[4], ("diff",), limit=1024, deadline=started + 3)
        assert truncated and len(data) == 1024
    else:
        with pytest.raises(Conflict, match="time limit"):
            _git_bytes(setup[3], setup[4], ("diff",), limit=1024, deadline=started + .3)
    assert time.monotonic() - started < 4
    assert processes and all(process.poll() is not None for process in processes)
