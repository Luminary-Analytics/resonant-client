"""Commands run as the person's own cmd.exe runs them; their output is decoded, whatever wrote it.

The second review of PR #102 found that switching cmd.exe to UTF-8 (a nested
cmd.exe after ``chcp 65001``) changed what commands do: over-long commands
"succeeded" without running, batch files saved in the console's code page
broke, an unquoted program path with spaces stopped running, a check's
timeout no longer ended cmd.exe's own loops, and ``PYTHONUTF8`` changed what
the person's scripts read and wrote. These pin the single ``cmd.exe /c`` of
before, output read by decoding instead (lumi/processes.py), and timeouts that
end everything a command started. Probes: review-102/shell_*.py,
decode_probe.py, utf8mode_probe.py.
"""

from __future__ import annotations

import os
from pathlib import Path
import random
import shutil
import subprocess
import sys
import time

import psutil
import pytest

from lumi.engine.tools import execute_tool
from lumi.processes import (CMD_COMMAND_LIMIT, CMD_TOO_LONG, _oem_code_page, background_process_kwargs,
                            decode_output, run_command, utf8_env)
from lumi.secrets_store import child_env

windows = pytest.mark.skipif(sys.platform != "win32", reason="cmd.exe")
PAGES = {"ansi_code_page": "cp1252", "oem_code_page": "cp437"}


def _shell(command: str, cwd: Path):
    return execute_tool("bash", {"command": command, "cwd": str(cwd)}, project_path=str(cwd))


def _lines(text: str) -> list[str]:
    return [line.rstrip() for line in text.splitlines() if line.strip()]


def _left_running(marker: str) -> list[str]:
    """Processes a test command started that still run: their command lines hold ``marker`` (see _endless)."""
    found = []
    for process in psutil.process_iter(["name", "cmdline"]):
        try:
            line = " ".join(process.info["cmdline"] or [])
            if f"-w {marker} " in line or f"f102-{marker}" in line:
                found.append(process.info["name"])
        except psutil.Error:
            continue
    return found


# ── Exactly what cmd.exe /c does ────────────────────────────────────────────

PARITY = ["echo hello!world", "echo %LUMI_F102_VAR%", "echo %LUMI_F102_VAR:~0,5%", "echo 100%", "echo %NOPE_F102%",
          "echo a^&b", "echo a^^b", 'echo "a & b"', 'echo "hi!" ^& more', "echo a && echo b",
          "(exit /b 3) || echo failed", "exit /b 3", "exit 7", "cmd /c exit 5",
          "dir /b missing-f102 2>nul || echo missing", "call echo called", "set FOO_F102=1 & echo %FOO_F102%",
          "for %i in (a b) do @echo %i", "if 1==1 (echo yes) else (echo no)", 'echo "unbalanced', "echo ^<tag^>",
          "echo %CMDCMDLINE%", "echo %LUMI_SHELL_COMMAND%", "echo Jürgen Jöhn & dir /b", "ver"]


@windows
def test_the_shell_tool_runs_a_command_as_cmd_exe_c_does(tmp_path, monkeypatch):
    """Output and exit status match a plain `cmd.exe /c`, even what reveals how cmd.exe was started."""
    monkeypatch.setenv("LUMI_F102_VAR", "value with spaces & amp")
    (tmp_path / "Jöhn Smith").mkdir()
    for command in PARITY:
        tool = _shell(command, tmp_path)
        plain = subprocess.run(command, shell=True, cwd=tmp_path, capture_output=True, timeout=60,
                               **background_process_kwargs())
        expected = _lines(decode_output(plain.stdout) + "\n" + decode_output(plain.stderr))
        assert tool.metadata["exit_code"] == plain.returncode, command
        assert [line for line in _lines(tool.output) if not line.startswith("(exit code:")] == expected, command
    # How cmd.exe was started, and nothing Lumi put in its environment.
    assert _lines(_shell("echo %CMDCMDLINE%", tmp_path).output) == [f'{os.environ["ComSpec"]} /c "echo %CMDCMDLINE%"']
    assert _lines(_shell("echo %LUMI_SHELL_COMMAND%", tmp_path).output) == ["%LUMI_SHELL_COMMAND%"]
    assert _lines(_shell("echo Jürgen Jöhn & dir /b", tmp_path).output) == ["Jürgen Jöhn", "Jöhn Smith"]


@windows
def test_a_program_path_with_spaces_runs_without_quotes(tmp_path):
    """cmd.exe /c keeps the quotes around a lone program path with spaces; `/s` would drop them."""
    folder = tmp_path / "Program Files F102"
    folder.mkdir()
    shutil.copy(Path(os.environ["SystemRoot"]) / "System32" / "hostname.exe", folder / "hostname.exe")
    result = _shell(str(folder / "hostname.exe"), tmp_path)
    assert not result.is_error and result.output.strip() and "is not recognized" not in result.output, result.output


@windows
@pytest.mark.parametrize("length", [CMD_COMMAND_LIMIT, CMD_COMMAND_LIMIT + 1, 8159, 8184, 8192, 9000, 20000])
def test_a_command_too_long_for_cmd_is_refused_never_half_run(tmp_path, length):
    head = "echo made> made.txt & echo "
    command = head + "y" * (length - len(head))
    result = _shell(command, tmp_path)
    made = (tmp_path / "made.txt").exists()
    if length <= CMD_COMMAND_LIMIT:
        assert not result.is_error and made, result.output[-200:]
        return
    assert result.is_error and not made
    assert result.output.splitlines()[:2] == [CMD_TOO_LONG, "(exit code: 1)"]
    assert result.metadata["exit_code"] == 1 and result.metadata["not_executed"] is True
    # Every other caller that runs a shell command gets cmd.exe's own answer too.
    done = run_command(command, shell=True, cwd=tmp_path, timeout=30)
    assert (done.returncode, done.stdout, decode_output(done.stderr)) == (1, b"", CMD_TOO_LONG + "\n")
    assert not (tmp_path / "made.txt").exists()


@windows
def test_a_batch_file_in_the_consoles_code_page_runs_as_in_cmd(tmp_path):
    """Batch files are read in the console's code page: a nested chcp 65001 broke non-ASCII paths in them."""
    folder = tmp_path / "Jöhn Smith"
    folder.mkdir()
    (folder / "marker.txt").write_text("found\n", encoding="utf-8")
    page = _oem_code_page()
    (tmp_path / "saved.bat").write_bytes(("@echo off\r\necho Größe\r\ntype \"" + str(folder / "marker.txt")
                                          + "\"\r\n").encode(page))
    result = _shell("call .\\saved.bat", tmp_path)
    assert not result.is_error and _lines(result.output) == ["Größe", "found"], result.output


@windows
def test_console_programs_output_reads_right(tmp_path):
    """more, sort and tree write the console's (or, redirected, the ANSI) code page: each line decodes."""
    page = _oem_code_page()
    (tmp_path / "names.txt").write_bytes("bär\r\nzebra\r\näpfel\r\n".encode(page))
    system = Path(os.environ["SystemRoot"]) / "System32"
    assert _lines(_shell(f'"{system / "more.com"}" < names.txt', tmp_path).output) == ["bär", "zebra", "äpfel"]
    assert _lines(_shell(f'"{system / "sort.exe"}" names.txt', tmp_path).output) == ["äpfel", "bär", "zebra"]
    root = tmp_path / "Jöhn Smith"
    (root / "Übersicht" / "Straße").mkdir(parents=True)
    (root / "Übersicht" / "Größe.txt").write_text("x", encoding="utf-8")
    (root / "marker.txt").write_text("x", encoding="utf-8")
    tree = _shell(f'"{system / "tree.com"}" /f "{root}"', tmp_path).output
    assert "JÖHN SMITH" in tree.upper() and "Übersicht" in tree and "Größe.txt" in tree and "Straße" in tree, tree
    assert not {"\N{REPLACEMENT CHARACTER}", "ª", "▄", "▀"} & set(tree), tree


# ── Output in any code page ────────────────────────────────────────────────


OEM_LINES = ["Übersicht", "Ärger", "Straße", "Genève", "Crème brûlée", "Größe", "Jürgen", "Jöhn Smith", "Ångström",
             "Français", "naïve", "Zürich", "ÜBERSICHT", "├─── src", "└── test.py", "│   ├── Jürgen", "╔═══╗",
             r"C:\Users\Jöhn Smith\Documents"]
ANSI_LINES = ["Jöhn", "Übersicht", "Straße", "Crème", "été", "Zürich", "Ärger", "ÉCOLE", "año", "canción",
              "voilà", "naïve", "Genève", "Größe", "São Paulo", "déjà vu"]


@pytest.mark.parametrize("oem", ["cp437", "cp850"])
def test_a_line_reads_in_the_code_page_it_was_written_in(oem):
    """What console programs write (OEM), ties included, and the ANSI code page's clear cases."""
    pages = {"oem_code_page": oem, "ansi_code_page": "cp1252"}
    for line in OEM_LINES:
        assert decode_output(line.encode(oem), **pages) == line, (oem, line)
    for line in ANSI_LINES:
        assert decode_output(line.encode("cp1252"), **pages) == line, ("cp1252", line)
        assert decode_output(line.encode("cp1252"), prefer="ansi", **pages) == line, ("cp1252", line)
    # A tie goes to the preferred code page: OEM for commands, ANSI for files' own text.
    tie = "Málaga".encode("cp1252")
    assert decode_output(tie, **pages) == "Mßlaga" and decode_output(tie, prefer="ansi", **pages) == "Málaga"
    # Redirected, tree writes the ANSI code page with "¦" for its lines.
    assert decode_output("¦   marker.txt\r\n    ¦   Größe.txt".encode("cp1252"), **pages) == "¦   marker.txt\n    ¦   Größe.txt"


def test_cyrillic_consoles_and_files_read_right():
    pages = {"oem_code_page": "cp866", "ansi_code_page": "cp1251"}
    for line in ("Привет мир", "Файл не найден", "ПАПКА"):
        assert decode_output(line.encode("cp866"), **pages) == line
        assert decode_output(line.encode("cp1251"), **pages) == line


def test_decoding_is_deterministic_and_cheap():
    """0.5 s per 2 MB before: each line was scored a character at a time in Python."""
    generator = random.Random(7)
    noise = bytes(generator.getrandbits(8) for _ in range(2_000_000))
    names = ["Größe", "Übersicht", "Jürgen", "Zürich", "Crème", "naïve", "Ångström", "Genève", "Straße"]
    listing = "".join(f"{generator.choice(names)} {index:07d} {generator.choice(names)}.txt\r\n"
                      for index in range(70_000)).encode("cp850")[:2_000_000]
    for data in (noise, listing):
        started = time.perf_counter()
        first = decode_output(data, oem_code_page="cp850", ansi_code_page="cp1252")
        elapsed = time.perf_counter() - started
        assert decode_output(data, oem_code_page="cp850", ansi_code_page="cp1252") == first
        assert elapsed < 2.0, elapsed  # about 0.3 s here; generous for a busy CI runner
    started = time.perf_counter()
    decode_output(("Größe line\n" * 150_000).encode("utf-8"))
    assert time.perf_counter() - started < 0.5


# ── Timeouts end everything a command started ──────────────────────────────


def _endless(marker: str) -> list[str]:
    """A cmd.exe built-in loop, and an external program that outlives cmd.exe if only cmd.exe is killed.

    Each names ``marker`` in its command line, for _left_running.
    """
    if sys.platform == "win32":
        return [f"for /l %i in (1,1,4000000) do @echo f102-{marker}>nul",
                f"ping -n 60 -w {marker} 127.0.0.1 >nul & echo finished"]
    # A program whose own command line names the marker (a shell may exec `sleep` in its place).
    return [f"\"{sys.executable}\" -c \"import time; time.sleep(60)\" f102-{marker}"]


def _marker() -> str:
    return str(random.randint(100000, 999999))


def test_run_command_ends_the_whole_tree_at_its_timeout(tmp_path):
    marker = _marker()
    for command in _endless(marker):
        started = time.monotonic()
        with pytest.raises(subprocess.TimeoutExpired):
            run_command(command, shell=True, cwd=tmp_path, env=utf8_env(child_env()), timeout=2)
        assert time.monotonic() - started < 10, command  # was 11+ s: the pipes waited for ping
        time.sleep(0.3)
        assert _left_running(marker) == [], command


def test_run_command_ends_what_a_finished_command_left_running(tmp_path):
    marker = _marker()
    if sys.platform == "win32":
        # A child that doesn't inherit the output pipe (``start /b`` would, and
        # keep the command running): the command ends while it still runs.
        (tmp_path / "leave.py").write_text(
            "import subprocess\n"
            f"subprocess.Popen(['ping', '-n', '60', '-w', '{marker}', '127.0.0.1'], stdin=subprocess.DEVNULL,\n"
            "                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)\n"
            "print('started')\n", encoding="utf-8")
        command = f'"{sys.executable}" leave.py'
    else:
        command = (f"\"{sys.executable}\" -c \"import time; time.sleep(60)\" f102-{marker} >/dev/null 2>&1 & "
                   "echo started")
    done = run_command(command, shell=True, cwd=tmp_path, env=utf8_env(child_env()), timeout=30)
    assert (done.returncode, decode_output(done.stdout).strip()) == (0, "started")
    time.sleep(0.5)
    assert _left_running(marker) == []


@windows
def test_an_acceptance_checks_timeout_ends_everything_it_started(tmp_path, monkeypatch):
    from lumi.orchestration import acceptance_check

    monkeypatch.setattr(acceptance_check, "_detect_bash", lambda *args: None)  # cmd.exe, as without Git Bash
    marker = _marker()
    for command in _endless(marker):
        started = time.monotonic()
        code, _out, error = acceptance_check.BashRunner(cwd=str(tmp_path), timeout_seconds=2).run(command)
        assert (code, error) == (124, "timeout after 2s") and time.monotonic() - started < 10
        time.sleep(0.3)
        assert _left_running(marker) == [], command


@windows
def test_a_comparisons_check_timeout_ends_everything_it_started(tmp_path, monkeypatch):
    from lumi import model_evals

    monkeypatch.setattr(model_evals, "CHECK_SECONDS", 2)
    marker = _marker()
    for command in _endless(marker):
        started = time.monotonic()
        passed, output = model_evals._check(command, str(tmp_path))
        assert not passed and "didn't finish" in output and time.monotonic() - started < 10
        time.sleep(0.3)
        assert _left_running(marker) == [], command


@windows
def test_a_harness_validation_timeout_ends_everything_it_started(tmp_path, monkeypatch):
    from lumi.harness import prompts

    monkeypatch.setattr(prompts, "VALIDATION_PROBE_SECONDS", 2)
    marker = _marker()
    command = _endless(marker)[1]
    started = time.monotonic()
    _checks, artifacts, _evidence = prompts.HarnessPrompts(None).run_harness_generator_validation_probes(
        project_path=str(tmp_path), summary={"validation_commands": [command]})
    assert any("timed out after 2s" in item for item in artifacts), artifacts
    assert time.monotonic() - started < 10
    time.sleep(0.3)
    assert _left_running(marker) == []


@windows
def test_a_worktree_validation_command_has_a_timeout(tmp_path, monkeypatch):
    from lumi.engine import worktrees
    from lumi.engine.worktrees import WorktreeError, WorktreeManager
    from tests.test_modern_harness_runtime import _init_repo

    monkeypatch.setattr(worktrees, "VALIDATION_SECONDS", 2)
    project = tmp_path / "repo"
    _init_repo(project)
    manager = WorktreeManager(project, root=tmp_path / "managed-worktrees")
    lease = manager.create("agent")
    (Path(lease.path) / "change.txt").write_text("change\n")
    manager.finalize(lease)
    marker = _marker()
    started = time.monotonic()
    with pytest.raises(WorktreeError, match="didn't finish within 0 minutes"):
        manager.integrate(lease, validation_commands=[_endless(marker)[1]])
    assert time.monotonic() - started < 20 and lease.status == "validation_failed"
    time.sleep(0.3)
    assert _left_running(marker) == []
