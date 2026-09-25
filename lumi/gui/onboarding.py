"""The first-run checklist: connect a model, open a project, finish a first task.

The chat's empty state shows three steps until they are done or dismissed:

1. **Connect a model**: done when any provider lists a model.
2. **Open a project**: done when a project other than the Playground is open.
   *Try the sample project* makes ``Documents/Lumi Projects/lumi-sample``, a
   tiny Python program with a planted bug, and opens it.
3. **Finish a first task**: done after the first turn that completes without
   an error; ``onboarding.first_task_done`` in the settings records it.
   *Use the sample task* puts a prompt in the composer; nothing is sent until
   the person sends it.

``onboarding.dismissed`` hides the checklist for good.
"""

from __future__ import annotations

from pathlib import Path

SAMPLE_NAME = "lumi-sample"
SAMPLE_TASK = "Add unit tests for app.py with unittest, run them, and fix any bug they find."
GENERAL_TASK = "Explain what this project does, then suggest one small improvement and make it."

SAMPLE_FILES = {
    "README.md": """# Lumi sample project

A tiny Python program for trying Lumi. `app.py` has two small functions, and
one of them has a bug.

Ask Lumi:

> Add unit tests for app.py with unittest, run them, and fix any bug they find.

Lumi reads the code, writes `test_app.py`, runs the tests, and fixes what
fails. Review the changes it proposes, then keep or undo them.
""",
    "app.py": '''"""Two small helpers for the Lumi sample project."""


def greet(name: str) -> str:
    """A friendly greeting."""
    return f"Hello, {name.strip() or 'there'}!"


def average(numbers: list[float]) -> float:
    """The mean of ``numbers``; 0.0 for an empty list."""
    return sum(numbers) / len(numbers)


if __name__ == "__main__":
    print(greet("Lumi"))
    print(average([2, 4, 9]))
''',
}


def sample_project_path(projects_root: Path | None = None) -> Path:
    from .sessions import _documents_projects_dir

    return (projects_root or _documents_projects_dir()) / SAMPLE_NAME


def create_sample_project(projects_root: Path | None = None) -> Path:
    """Make the sample project, or return it unchanged if it already exists."""
    target = sample_project_path(projects_root)
    if target.is_dir() and any(target.iterdir()):
        return target  # someone's work may be in it: never overwrite
    target.mkdir(parents=True, exist_ok=True)
    for name, text in SAMPLE_FILES.items():
        (target / name).write_text(text, encoding="utf-8", newline="\n")
    return target


def turn_finished(events: list) -> bool:
    """Whether a turn's events end in an answer rather than an error."""
    kinds = [event.get("event") for event in events if isinstance(event, dict)]
    return "error" not in kinds and any(kind in {"text.done", "session.end"} for kind in kinds)
