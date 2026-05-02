"""Tests for v0.5.1a4 — cross-platform shell handling in BashRunner.

The v0.5.0 GA smoke surfaced that on Windows, `subprocess.run(
shell=True)` uses `cmd.exe` which doesn't have POSIX tools like
`wc`, `find`, `grep`. Real specs routinely use these, so a
criterion like `wc -l < file.py` would silently fail. v0.5.1a4
prefers `bash` (e.g. Git Bash on Windows) when it's on PATH;
falls back to the platform default otherwise.

These tests pin:
- `_detect_bash()` returns a path when bash is available, None
  otherwise (cached per-process)
- `BashRunner.run()` uses bash when detected
- Falls back to platform default when bash isn't available
- Tests can override the bash-path detection via `_bash_path`
- Test helper `_reset_bash_detection_cache` works
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from resonant_client.orchestration.acceptance_check import (
    BashRunner,
    _detect_bash,
    _reset_bash_detection_cache,
)


@pytest.fixture(autouse=True)
def _clear_bash_cache():
    """Each test starts with a fresh detection cache."""
    _reset_bash_detection_cache()
    yield
    _reset_bash_detection_cache()


# ── _detect_bash ──────────────────────────────────────────────────────


class TestDetectBash:
    def test_returns_path_when_bash_in_PATH(self):
        with patch("shutil.which", return_value="/usr/bin/bash") as m:
            result = _detect_bash()
        assert result == "/usr/bin/bash"
        m.assert_called_once_with("bash")

    def test_returns_none_when_bash_not_in_PATH(self):
        with patch("shutil.which", return_value=None):
            result = _detect_bash()
        assert result is None

    def test_result_is_cached(self):
        with patch("shutil.which", return_value="/usr/bin/bash") as m:
            _detect_bash()
            _detect_bash()
            _detect_bash()
        # Three calls but `which` was only invoked once — cache hit
        assert m.call_count == 1

    def test_reset_cache_works(self):
        with patch("shutil.which", return_value="/usr/bin/bash") as m:
            _detect_bash()
            _reset_bash_detection_cache()
            _detect_bash()
        # After reset, the second call probes again
        assert m.call_count == 2


# ── BashRunner shell selection ────────────────────────────────────────


class TestBashRunnerShell:
    def test_uses_bash_with_dash_c_when_bash_detected(self):
        captured = {}

        def fake_run(args, **kwargs):
            captured["args"] = args
            captured["shell"] = kwargs.get("shell", False)
            return MagicMock(returncode=0, stdout="ok", stderr="")

        runner = BashRunner(_bash_path="/usr/bin/bash")
        with patch(
            "resonant_client.orchestration.acceptance_check.subprocess.run",
            side_effect=fake_run,
        ):
            runner.run("echo hello")

        # Bash invoked as `[bash, -c, command]` — NOT shell=True
        assert captured["args"] == ["/usr/bin/bash", "-c", "echo hello"]
        assert captured["shell"] is False

    def test_falls_back_to_shell_true_when_bash_unavailable(self):
        captured = {}

        def fake_run(*args, **kwargs):
            captured["args"] = args
            captured["shell"] = kwargs.get("shell", False)
            return MagicMock(returncode=0, stdout="ok", stderr="")

        # Stub bash detection to return None
        runner = BashRunner()
        with patch(
            "resonant_client.orchestration.acceptance_check._detect_bash",
            return_value=None,
        ), patch(
            "resonant_client.orchestration.acceptance_check.subprocess.run",
            side_effect=fake_run,
        ):
            runner.run("echo hello")

        # Falls back to shell=True with raw command
        assert captured["args"][0] == "echo hello"
        assert captured["shell"] is True

    def test_explicit_bash_path_overrides_detection(self):
        """If a test (or runtime config) sets `_bash_path` explicitly,
        the BashRunner uses that instead of probing PATH."""
        captured = {}

        def fake_run(args, **kwargs):
            captured["args"] = args
            return MagicMock(returncode=0, stdout="", stderr="")

        runner = BashRunner(_bash_path="/custom/path/to/bash")
        with patch(
            "resonant_client.orchestration.acceptance_check.subprocess.run",
            side_effect=fake_run,
        ), patch(
            "resonant_client.orchestration.acceptance_check._detect_bash"
        ) as detect:
            runner.run("echo hi")
            # _detect_bash should NOT have been consulted because
            # _bash_path was explicit.
            detect.assert_not_called()
        assert captured["args"][0] == "/custom/path/to/bash"

    def test_run_callback_takes_priority(self):
        """If `_run` is supplied (test-stub mode), shell detection
        is skipped entirely — preserves test hermeticity."""
        called_args = []

        def stub_run(cmd, **kwargs):
            called_args.append(cmd)
            return (0, "stubbed", "")

        runner = BashRunner(_run=stub_run)
        with patch(
            "resonant_client.orchestration.acceptance_check._detect_bash"
        ) as detect:
            runner.run("echo hello")
            detect.assert_not_called()
        assert called_args == ["echo hello"]

    def test_propagates_cwd_and_timeout(self):
        captured = {}

        def fake_run(args, **kwargs):
            captured.update(kwargs)
            return MagicMock(returncode=0, stdout="", stderr="")

        runner = BashRunner(
            _bash_path="/bin/bash",
            cwd="/tmp/proj",
            timeout_seconds=15.0,
        )
        with patch(
            "resonant_client.orchestration.acceptance_check.subprocess.run",
            side_effect=fake_run,
        ):
            runner.run("ls")

        assert captured["cwd"] == "/tmp/proj"
        assert captured["timeout"] == 15.0

    def test_timeout_returns_124(self):
        import subprocess as sp

        runner = BashRunner(_bash_path="/bin/bash", timeout_seconds=0.1)
        with patch(
            "resonant_client.orchestration.acceptance_check.subprocess.run",
            side_effect=sp.TimeoutExpired(cmd="x", timeout=0.1),
        ):
            rc, out, err = runner.run("anything")

        assert rc == 124
        assert "timeout" in err

    def test_subprocess_error_returns_127(self):
        runner = BashRunner(_bash_path="/bin/bash")
        with patch(
            "resonant_client.orchestration.acceptance_check.subprocess.run",
            side_effect=OSError("no such bash"),
        ):
            rc, out, err = runner.run("anything")

        assert rc == 127
        assert "subprocess error" in err
