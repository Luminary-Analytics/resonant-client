"""Do not count cmd.exe's silent multiline truncation as successful execution."""
import time
from unittest.mock import Mock

import pytest

from resonant_client.engine import tools


@pytest.mark.parametrize("command", ['node -e "\nconsole.log(12345)\n"', 'echo first\r\necho second'])
def test_windows_multiline_is_rejected_before_any_subprocess(monkeypatch, command):
    monkeypatch.setattr(tools.sys, "platform", "win32")
    run = Mock()
    monkeypatch.setattr(tools, "_run_subprocess_with_cancel", run)
    result = tools._exec_bash({"command": command}, time.time())
    assert result.is_error
    assert result.metadata["not_executed"] is True
    assert "exit_code" not in result.metadata
    assert "Write the script" in result.output
    run.assert_not_called()


@pytest.mark.parametrize("platform,command", [('linux', 'echo first\necho second'), ('win32', 'node diagnostic.cjs')])
def test_supported_commands_still_run(monkeypatch, platform, command):
    monkeypatch.setattr(tools.sys, "platform", platform)
    run = Mock(return_value=(0, "12345\n", "", False))
    monkeypatch.setattr(tools, "_run_subprocess_with_cancel", run)
    result = tools._exec_bash({"command": command}, time.time())
    assert not result.is_error
    assert result.output == "12345"
    assert result.metadata["exit_code"] == 0
    run.assert_called_once()
