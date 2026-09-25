"""Settings and scripts from before the Lumi rebrand keep working."""

from __future__ import annotations

import json
import sys

from pathlib import Path

import lumi
from lumi import paths
from lumi.engine.hooks import HookDefinition, HookRunner, HookType


def _home(monkeypatch, tmp_path) -> Path:
    monkeypatch.delenv("LUMI_STATE_HOME", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    return tmp_path


def test_pre_rebrand_state_moves_to_the_lumi_folder_once(monkeypatch, tmp_path):
    home = _home(monkeypatch, tmp_path)
    (home / ".resonant" / "projects").mkdir(parents=True)
    (home / ".resonant" / "settings.json").write_text("{}", encoding="utf-8")

    assert paths.migrate_legacy_home() == home / ".lumi"

    assert (home / ".lumi" / "settings.json").is_file()
    assert not (home / ".resonant").exists()
    assert paths.state_home() == home / ".lumi"
    assert paths.migrate_legacy_home() is None


def test_existing_lumi_state_is_never_overwritten(monkeypatch, tmp_path):
    home = _home(monkeypatch, tmp_path)
    (home / ".resonant").mkdir()
    (home / ".lumi").mkdir()

    assert paths.migrate_legacy_home() is None
    assert (home / ".resonant").is_dir()
    assert paths.state_home() == home / ".lumi"


def test_a_blocked_move_keeps_using_the_old_folder(monkeypatch, tmp_path):
    home = _home(monkeypatch, tmp_path)
    (home / ".resonant").mkdir()

    def locked(self, target):
        raise PermissionError("in use")

    monkeypatch.setattr(Path, "rename", locked)

    assert paths.migrate_legacy_home() is None
    assert paths.state_home() == home / ".resonant"


def test_state_home_override_wins_and_is_not_migrated(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    (tmp_path / ".resonant").mkdir()
    monkeypatch.setenv("LUMI_STATE_HOME", str(tmp_path / "custom"))

    assert paths.state_home() == tmp_path / "custom"
    assert paths.migrate_legacy_home() is None
    assert (tmp_path / ".resonant").is_dir()


def test_projects_keep_an_existing_resonant_folder(tmp_path):
    assert paths.project_dir(tmp_path) == tmp_path / ".lumi"
    (tmp_path / ".resonant").mkdir()
    assert paths.project_dir(tmp_path) == tmp_path / ".resonant"
    (tmp_path / ".lumi").mkdir()
    assert paths.project_dir(tmp_path) == tmp_path / ".lumi"


def test_legacy_environment_variables_are_honored(monkeypatch):
    monkeypatch.setenv("RESONANT_OLLAMA_KEEP_ALIVE", "45m")
    monkeypatch.delenv("LUMI_OLLAMA_KEEP_ALIVE", raising=False)

    lumi._mirror_legacy_environment()

    assert lumi.os.environ["LUMI_OLLAMA_KEEP_ALIVE"] == "45m"


def test_an_explicit_lumi_variable_wins_over_the_legacy_one(monkeypatch):
    monkeypatch.setenv("RESONANT_DEFAULT_MODEL", "old")
    monkeypatch.setenv("LUMI_DEFAULT_MODEL", "new")

    lumi._mirror_legacy_environment()

    assert lumi.os.environ["LUMI_DEFAULT_MODEL"] == "new"


def test_hook_scripts_receive_both_new_and_legacy_variable_names(tmp_path):
    names = ["LUMI_HOOK_TYPE", "RESONANT_HOOK_TYPE", "LUMI_TOOL_NAME", "RESONANT_TOOL_NAME"]
    script = tmp_path / "print_env.py"
    script.write_text(
        "import json, os\n"
        f"print(json.dumps({{name: os.environ.get(name) for name in {names!r}}}))\n",
        encoding="utf-8",
    )
    runner = HookRunner()
    runner.add_hooks([HookDefinition(
        hook_type=HookType.POST_TOOL_USE,
        command=f'"{sys.executable}" "{script}"',
    )])

    result = runner.run_hooks(
        HookType.POST_TOOL_USE, {"project_path": str(tmp_path)}, tool_name="bash",
    )

    seen = json.loads(result.output.strip().splitlines()[-1])
    assert seen == {
        "LUMI_HOOK_TYPE": "post_tool_use", "RESONANT_HOOK_TYPE": "post_tool_use",
        "LUMI_TOOL_NAME": "bash", "RESONANT_TOOL_NAME": "bash",
    }
