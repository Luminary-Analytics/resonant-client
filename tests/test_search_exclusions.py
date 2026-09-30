"""Search results keep an organization's excluded files out, whatever the files' text is.

ripgrep (and grep off Windows) print each match as its path in UTF-8, then
the matched line as the file's own bytes. The whole line used to be decoded
as one unit, so a match whose text wasn't UTF-8 (a file saved in the ANSI
code page) had its path read as cp1252 too: "Jöhn Smith" became "JÃ¶hn Smith",
the path no longer matched the project, and a ``files.exclude`` rule let the
excluded file's text through. A carriage return inside a matched line did the
same by starting a line with no path at all. Both tools now end each path
with a NUL (``--null``), and the path and the text are decoded on their own.
"""

from __future__ import annotations

import os
import sys

import pytest

from lumi import policy as lumi_policy
from lumi.engine import tools
from lumi.engine.exclusions import ExclusionRules
from lumi.policy import current as current_policy, parse

# Text saved in the ANSI code page reads as such only on Windows (decode_output).
windows = pytest.mark.skipif(sys.platform != "win32", reason="files saved in the ANSI code page")
NOTE = "[2 matches in excluded files not shown (file exclusion rules)]"


def _project(tmp_path):
    """A project under "Jöhn Smith": a UTF-8 file, an ANSI one, and two excluded ANSI files with a secret."""
    project = tmp_path / "Jöhn Smith" / "Alpha Project"
    (project / "conf").mkdir(parents=True)
    (project / "notes.md").write_bytes("needle in UTF-8 text\r\n".encode("utf-8"))
    (project / "legacy.ini").write_bytes("needle café=1\r\n".encode("cp1252"))
    (project / "conf" / "secret.ini").write_bytes("needle password=héhé\r\n".encode("cp1252"))
    # The secret after a lone carriage return started a line of its own, with no path.
    (project / "conf" / "lone-cr.ini").write_bytes("needle first\rpassword=héhé\r\n".encode("cp1252"))
    return project


@pytest.fixture
def organization_rules():
    """The organization's rule excluding conf/*.ini, read the way the app reads its policy (gui/app.py)."""
    lumi_policy.set_for_tests(parse({"schema": "lumi.policy/v1", "organization": "Acme",
                                     "files": {"exclude": ["conf/*.ini"]}}, source="test policy"))

    def rules(project):
        return ExclusionRules.for_project(str(project), policy_patterns=lambda: current_policy().exclude
                                          if current_policy() else ())
    return rules


def _assert_only_the_allowed_files(result):
    assert "password" not in result.output and "héhé" not in result.output, result.output
    matches = sorted(line for line in result.output.splitlines() if ":1:" in line)
    assert matches == ["legacy.ini:1:needle café=1", "notes.md:1:needle in UTF-8 text"], result.output
    assert NOTE in result.output


@windows
@pytest.mark.parametrize("spelling", ["as named", "case-folded", "relative"])
def test_ripgrep_keeps_excluded_ansi_files_out_of_a_john_smith_project(tmp_path, monkeypatch, organization_rules,
                                                                      spelling):
    """The real ripgrep; the sandbox hands tools a case-folded root, and a search can name "."."""
    if not tools._ripgrep_executable():
        pytest.skip("needs ripgrep (packaging/fetch_ripgrep.ps1)")
    project = _project(tmp_path)
    path = {"as named": str(project), "case-folded": os.path.normcase(str(project)), "relative": "."}[spelling]
    monkeypatch.chdir(project)
    result = tools.execute_tool("grep", {"pattern": "needle", "path": path}, project_path=str(project),
                                exclusions=organization_rules(project))
    assert not result.is_error, result.output
    _assert_only_the_allowed_files(result)


@windows
def test_a_nul_after_each_path_keeps_it_with_its_own_text(tmp_path, monkeypatch, organization_rules):
    """What ripgrep --null printed for that project, where ripgrep isn't installed (CI's test job)."""
    project = _project(tmp_path)
    seen = []

    def search(command, **kwargs):
        seen.append(command)
        records = [(project / "notes.md", "needle in UTF-8 text\r".encode("utf-8")),
                   (project / "legacy.ini", "needle café=1\r".encode("cp1252")),
                   (project / "conf" / "secret.ini", "needle password=héhé\r".encode("cp1252")),
                   (project / "conf" / "lone-cr.ini", "needle first\rpassword=héhé\r".encode("cp1252"))]
        output = b"".join(str(path).encode("utf-8") + b"\0" + b"1:" + text + b"\n" for path, text in records)
        return 0, output, b"", False

    monkeypatch.setattr(tools, "_ripgrep_executable", lambda trusted_only=False, project=None: r"C:\tools\rg.exe")
    monkeypatch.setattr(tools, "_run_subprocess_with_cancel", search)
    result = tools._exec_grep({"pattern": "needle", "path": str(project)}, 0.0,
                              exclusions=organization_rules(project), project_path=str(project))
    assert "--null" in seen[0]
    _assert_only_the_allowed_files(result)


def test_null_records_decode_each_path_and_keep_each_match_on_one_line():
    data = ("/home/jöhn/a.ini".encode("utf-8") + "\x0012:über\r\n".encode("utf-8")
            + b"b.txt\x003:first\rsecond\n" + b"no NUL: not a match\n" + b"\n")
    assert tools._null_records(data) == [("/home/jöhn/a.ini", ":12:über"), ("b.txt", ":3:first second")]


@windows
def test_null_records_read_a_matched_line_in_the_ansi_code_page():
    data = "C:\\Users\\Jöhn Smith\\a.ini".encode("utf-8") + b"\x0012:caf\xe9 \xfcber\r\n"
    assert tools._null_records(data) == [("C:\\Users\\Jöhn Smith\\a.ini", ":12:café über")]
