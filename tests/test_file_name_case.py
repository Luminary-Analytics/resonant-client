"""New files and folders keep the case the agent asked for.

On Windows ``os.path.normcase`` lowercases, and the path sandbox handed tools
its case-folded boundary key as the path to use. Every file and folder the
agent created came out in lowercase: "Docs/NewFile.md" as "docs\\newfile.md",
"MakeFile.TXT" as "makefile.txt", and writer teams committed them that way.
Found on a clean-PC pass of the packaged build (2026-09-30). The sandbox now
folds case only to compare (lumi/engine/sandbox.py), and managed jobs run in
the project as it is spelled (lumi/engine/jobs.py).
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

from lumi.engine.jobs import JobManager
from lumi.engine.sandbox import PathSandbox, SandboxViolation
from lumi.engine.session import Session
from tests.streaming_stub import StreamingBackend, done, events_of_kind, text_delta, tool_call

windows_only = pytest.mark.skipif(os.name != "nt", reason="the file system ignores case on Windows")

JUERGEN = "Jürgen-ö.txt"


def _write_through_the_agent(project: Path, paths: list[str]) -> list[dict]:
    """Each path written by a scripted model through Session.run: the tool path the app uses."""
    calls = [tool_call("file_write", {"path": path, "content": f"{path}\n"}, call_id=f"w{index}")
             for index, path in enumerate(paths)]
    backend = StreamingBackend(scripts=[[*calls, done()], [text_delta("Done."), done()]])
    session = Session(backend=backend, max_steps=2, auto_approve=True)
    session.project_path = str(project)
    session.sandbox = PathSandbox(str(project), enabled=True)
    results = events_of_kind(list(session.run("write the files")), "tool.result")
    assert len(results) == len(paths), results
    assert not any(result["is_error"] for result in results), [result["output"] for result in results]
    return results


def test_new_files_and_folders_keep_the_case_the_agent_asked_for(tmp_path):
    project = tmp_path / "Alpha Project"
    project.mkdir()

    results = _write_through_the_agent(project, ["Docs/NewFile.md", "MakeFile.TXT", f"notes/{JUERGEN}"])

    assert sorted(os.listdir(project)) == sorted(["Docs", "MakeFile.TXT", "notes"])
    assert os.listdir(project / "Docs") == ["NewFile.md"]
    assert os.listdir(project / "notes") == [JUERGEN]
    assert (project / "Docs" / "NewFile.md").read_text(encoding="utf-8") == "Docs/NewFile.md\n"
    # What the agent is told names each file as it is spelled, in the project as it is spelled.
    for result, name in zip(results, (os.path.join("Docs", "NewFile.md"), "MakeFile.TXT",
                                      os.path.join("notes", JUERGEN))):
        assert os.path.join(os.path.realpath(project), name) in result["output"], result["output"]


@windows_only
def test_a_file_under_an_existing_folder_spelled_in_another_case_goes_into_that_folder(tmp_path):
    project = tmp_path / "Alpha Project"
    (project / "src").mkdir(parents=True)
    (project / "src" / "existing.py").write_text("x = 1\n", encoding="utf-8")

    results = _write_through_the_agent(project, ["SRC/Helper.py", "Src/Deep/Module.PY"])

    # No second folder: the file lands in the real one, and keeps its own case.
    assert os.listdir(project) == ["src"]
    assert sorted(os.listdir(project / "src")) == sorted(["Deep", "Helper.py", "existing.py"])
    assert os.listdir(project / "src" / "Deep") == ["Module.PY"]
    real = os.path.realpath(project)
    assert os.path.join(real, "src", "Helper.py") in results[0]["output"], results[0]["output"]
    assert os.path.join(real, "src", "Deep", "Module.PY") in results[1]["output"], results[1]["output"]


def test_the_sandbox_returns_paths_as_spelled_and_folds_case_only_to_compare(tmp_path):
    project = tmp_path / "Alpha Project"
    (project / "src").mkdir(parents=True)
    real = os.path.realpath(project)
    sandbox = PathSandbox(str(project))

    assert sandbox.project_path == real
    assert sandbox.validate_path("Docs/NewFile.md") == os.path.join(real, "Docs", "NewFile.md")
    assert sandbox.validate_path(os.path.join(str(project), "MakeFile.TXT")) == os.path.join(real, "MakeFile.TXT")
    assert sandbox.validate_bash_cwd(str(project)) == real
    if os.name == "nt":
        # Another spelling of the project still counts as inside, and comes back as it is on disk.
        assert sandbox.validate_path(str(project).upper() + "\\SRC\\New.txt") == os.path.join(real, "src", "New.txt")
        assert sandbox.validate_path(str(project).lower() + "\\docs\\NewFile.md") == os.path.join(
            real, "docs", "NewFile.md")
        assert sandbox.validate_bash_cwd(str(project).lower()) == real
        assert PathSandbox(str(project).lower()).project_path == real


def test_boundary_checks_still_refuse_escapes_in_any_case(tmp_path):
    project = tmp_path / "Project"
    (project / "src").mkdir(parents=True)
    (tmp_path / "ProjectX").mkdir()
    (tmp_path / "Outside").mkdir()
    sandbox = PathSandbox(str(project))

    escapes = [
        os.path.join("..", "Outside", "x.txt"),
        os.path.join("SRC", "..", "..", "Outside", "x.txt"),
        os.path.join("..", "PROJECTX", "a.txt"),
        str(tmp_path / "ProjectX" / "a.txt"),
        # A differently cased absolute path outside, and one sharing the project's name as a prefix.
        os.path.join(str(tmp_path / "Outside").upper(), "x.txt"),
        os.path.join(str(tmp_path).upper(), "PROJECTX", "a.txt"),
        os.path.join(str(tmp_path).lower(), "projectx", "a.txt"),
    ]
    for path in escapes:
        with pytest.raises(SandboxViolation):
            sandbox.validate_path(path)
    for cwd in (str(tmp_path / "Outside").upper(), os.path.join(str(project), "..", "outside"), str(tmp_path)):
        with pytest.raises(SandboxViolation):
            sandbox.validate_bash_cwd(cwd)


@windows_only
def test_managed_jobs_run_in_the_project_as_spelled_on_disk(tmp_path):
    project = tmp_path / "Alpha Project"
    project.mkdir()
    real = os.path.realpath(project)
    manager = JobManager()
    try:
        started = manager.start(str(project).lower(), [sys.executable, "-c", "import os; print(os.getcwd())"])
        assert started["project"] == real
        deadline = time.monotonic() + 30
        while manager.status(str(project).upper(), started["id"])["state"] == "running":
            assert time.monotonic() < deadline, "the job didn't finish"
            time.sleep(.05)
        status = manager.status(str(project), started["id"])
        assert status["exit_code"] == 0, status
        # os.getcwd() is the folder as the job was started in it: TypeScript, webpack and
        # Jest see a differently cased folder as another path.
        assert status["logs"].strip() == real
        assert [job["id"] for job in manager.list(str(project).upper())] == [started["id"]]
    finally:
        manager.close()
