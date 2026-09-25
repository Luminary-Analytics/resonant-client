"""Commands the agent never runs, in any permission mode (lumi/engine/guardrails.py)."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from lumi.engine import guardrails, tools
from lumi.engine.policies import PolicyAction, policy_for_tier, project_execution_policy
from lumi.orchestration.autonomy import check_floor

NEVER = [
    "rm -rf /", "sudo rm -rf / --no-preserve-root", "rm -rf ~", "rm -rf ~/", "rm -fr $HOME", "rm -r -f /*",
    "cd x && rm -rf ~", 'rm -rf "$HOME"', "rm -Rf ${HOME}/", "chmod -R 777 /", "sudo chown -R me:me /",
    "mkfs.ext4 /dev/sda1", "dd if=/dev/zero of=/dev/sda bs=1M", ":(){ :|:& };:",
    "format c:", "diskpart", "rd /s /q C:\\", "rmdir /q /s c:", "del /f /s /q C:\\*",
    "Remove-Item -Recurse -Force C:\\", "Remove-Item C:\\ -Recurse", "Remove-Item -Recurse $env:USERPROFILE",
    "ri -r C:\\", "echo x; Remove-Item -Recurse -Force ~", "Remove-Item -Rec -Force 'C:\\*'",
    "Remove-Item -Recurse -Force $HOME\\*", "shutdown -h now", "sudo reboot", "Stop-Computer",
]
EVERYDAY = [
    "rm -rf ./build", "rm -rf /tmp/work", "rm -rf ~/project/node_modules", "rm -rf build/", "rm file.txt",
    "python format_code.py", "dd if=disk.img of=out.img", "echo shutdown", "npm run format",
    "git rm -r --cached x", "Remove-Item -Recurse .\\dist", "rd /s /q build", "del /s /q build\\*",
    "rmdir /s /q C:\\work\\tmp", "cat docs/diskpart-notes.md", "grep -r mkfs docs", "ls ~/",
    "rm -rf node_modules && npm i", "echo remove-item -recurse C:\\", "Get-Help Remove-Item",
    "chmod -R 755 ./dist", "Remove-Item -Recurse C:\\Users\\me\\project\\out", "rm -rf $HOME/project/tmp",
    "chown -R me /srv/app", 'git commit -m "shutdown path"', "Remove-Item C:\\temp\\x.txt",
]


@pytest.mark.parametrize("command", NEVER)
def test_commands_that_damage_the_computer_are_recognized(command):
    assert guardrails.blocked(command)


@pytest.mark.parametrize("command", EVERYDAY)
def test_everyday_commands_are_not(command):
    assert guardrails.blocked(command) == ""


@pytest.mark.parametrize("tier", ["suggest", "auto-edit", "full-auto"])
def test_every_tier_refuses_them_before_asking(tier):
    policy = policy_for_tier(tier)
    call = {"command": "rm -rf ~"}
    assert policy.evaluate("bash", call) == PolicyAction.DENY
    assert "never allowed, in any permission mode" in policy.get_reason("bash", call)
    assert policy.evaluate("check_run", {"command": "format c:", "requirement": "r"}) == PolicyAction.DENY


def test_full_auto_still_runs_everything_else():
    policy = policy_for_tier("full-auto")
    assert policy.evaluate("bash", {"command": "rm -rf build && make"}) == PolicyAction.ALLOW


def test_neither_a_repository_nor_an_organization_can_allow_them(tmp_path, monkeypatch):
    allow_all = [{"tool_pattern": "bash", "action": "allow"}]
    (tmp_path / "lumi-policy.json").write_text(json.dumps({"rules": allow_all}), encoding="utf-8")
    monkeypatch.setattr("lumi.policy.current", lambda: SimpleNamespace(shell_rules=allow_all))
    policy = project_execution_policy("auto-edit", str(tmp_path))
    assert policy.evaluate("bash", {"command": "sudo reboot"}) == PolicyAction.DENY
    # The organization's allow still applies to everything else.
    assert policy.evaluate("bash", {"command": "make test"}) == PolicyAction.ALLOW
    assert policy.rules[:len(guardrails.policy_rules())] == guardrails.policy_rules()


def test_the_floor_refuses_them_whatever_approved_them(tmp_path):
    for tool_name, args in [
        ("bash", {"command": "shutdown -h now"}),
        ("check_run", {"command": "diskpart", "requirement": "r"}),
        # Jobs and previews take a program and its arguments.
        ("job_start", {"command": ["bash", "-c", "rm -rf /"]}),
        ("job_start", {"command": ["rm", "-rf", "/"]}),
        ("preview_start", {"command": ["cmd", "/c", "rd /s /q C:\\"], "url": "http://127.0.0.1:9/"}),
    ]:
        violation = check_floor(tool_name=tool_name, args=args, project_path=str(tmp_path))
        assert violation is not None and violation.rule == "never_allowed_command", tool_name
    assert check_floor(tool_name="job_start", args={"command": ["python", "render.py"]},
                       project_path=str(tmp_path)) is None


def test_a_refused_command_never_reaches_a_shell(tmp_path, monkeypatch):
    # With no execution policy at all (a hook's rewrite, a bare session), the
    # tool layer still refuses.
    monkeypatch.setattr(tools, "_run_subprocess_with_cancel", lambda *a, **k: pytest.fail("the command ran"))
    result = tools.execute_tool("bash", {"command": "rm -rf ~", "cwd": str(tmp_path)}, project_path=str(tmp_path))
    assert result.is_error and "never_allowed_command" in result.output
    assert result.metadata["floor_violation"]["rule"] == "never_allowed_command"
