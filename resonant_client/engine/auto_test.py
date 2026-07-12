"""
Per-file test runner for the auto-test-on-edit feedback loop.

Used by Session.run() to optionally run a scoped test command after a successful
file_edit / file_write, and inject any failures back into the conversation as a
synthetic user turn.

The test target is inferred from the edited file path with simple heuristics:
- Python: foo/bar.py    → tests/test_bar.py | tests/foo/test_bar.py | test_bar.py
- JS/TS:  foo.ts        → foo.test.ts | foo.spec.ts | __tests__/foo.test.ts
"""

from __future__ import annotations

import shlex
import subprocess
from pathlib import Path
from typing import Optional

from resonant_client.processes import background_process_kwargs


def find_test_target(project_path: Path | str, edited_file: Path | str) -> Optional[Path]:
    """Locate a likely test file for `edited_file`. Returns absolute path or None."""
    project = Path(project_path).resolve()
    edited = Path(edited_file).resolve()

    if not edited.is_file():
        return None
    suffix = edited.suffix.lower()
    stem = edited.stem

    # Don't try to test the test file itself
    if stem.startswith("test_") or stem.endswith("_test") or stem.endswith(".test") or stem.endswith(".spec"):
        return edited

    # Compute the file path relative to project root, for "mirrored" layouts
    try:
        rel = edited.relative_to(project)
    except ValueError:
        rel = None

    candidates: list[Path] = []

    if suffix in (".py", ".pyi"):
        # Common Python layouts
        if rel is not None:
            # tests/<same/path>/test_<stem>.py
            mirror_under_tests = project / "tests" / rel.parent / f"test_{stem}.py"
            candidates.append(mirror_under_tests)
            # Strip top-level package: pkg/sub/foo.py → tests/sub/test_foo.py
            if len(rel.parts) > 1:
                stripped = Path(*rel.parts[1:]).parent / f"test_{stem}.py"
                candidates.append(project / "tests" / stripped)
        # tests/test_<stem>.py at project root
        candidates.append(project / "tests" / f"test_{stem}.py")
        # test_<stem>.py next to the source
        candidates.append(edited.parent / f"test_{stem}.py")
        # <stem>_test.py next to the source
        candidates.append(edited.parent / f"{stem}_test.py")

    elif suffix in (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"):
        # Adjacent test files
        for ext in (suffix,):
            candidates.append(edited.parent / f"{stem}.test{ext}")
            candidates.append(edited.parent / f"{stem}.spec{ext}")
        # __tests__/ subdir convention
        candidates.append(edited.parent / "__tests__" / f"{stem}.test{suffix}")
        candidates.append(edited.parent / "__tests__" / f"{stem}.spec{suffix}")

    for c in candidates:
        if c.is_file():
            return c
    return None


def run_tests_for_edit(
    project_path: Path | str,
    edited_file: Path | str,
    *,
    command: str = "pytest -x",
    timeout: float = 60.0,
) -> dict:
    """
    Find the test target for `edited_file` and run `command` scoped to it.

    Returns:
        {
            "ok": bool,                 # True if tests passed, no target found, or runner unavailable
            "target": str,              # path of the test file run (relative if possible)
            "output": str,              # captured stdout+stderr (only on failure)
            "skipped_reason": str,      # populated when skipped
            "command": list[str],       # the actual argv that ran
        }
    """
    project = Path(project_path)
    target = find_test_target(project, edited_file)
    if target is None:
        return {
            "ok": True,
            "target": "",
            "output": "",
            "skipped_reason": "no test target found",
            "command": [],
        }

    # Build argv: split the configured command, then append the target.
    try:
        base = shlex.split(command, posix=False)
    except ValueError:
        base = command.split()
    if not base:
        return {
            "ok": True,
            "target": str(target),
            "output": "",
            "skipped_reason": "empty test command",
            "command": [],
        }

    # Make the target relative to project_path for cleaner output
    try:
        target_arg = str(target.relative_to(project.resolve()))
    except ValueError:
        target_arg = str(target)

    argv = base + [target_arg]
    try:
        proc = subprocess.run(
            argv,
            cwd=str(project),
            capture_output=True,
            text=True,
            timeout=max(0.5, timeout),
            shell=False,
            encoding="utf-8",
            errors="replace",
            **background_process_kwargs(),
        )
    except FileNotFoundError:
        return {
            "ok": True,  # treat "no runner installed" as non-failure (don't block the agent)
            "target": str(target),
            "output": "",
            "skipped_reason": f"{base[0]} not installed",
            "command": argv,
        }
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "target": str(target),
            "output": f"(test run exceeded {timeout}s timeout)",
            "skipped_reason": "",
            "command": argv,
        }

    output = ((proc.stdout or "") + (proc.stderr or "")).strip()
    return {
        "ok": proc.returncode == 0,
        "target": target_arg,
        "output": output if proc.returncode != 0 else "",
        "skipped_reason": "",
        "command": argv,
    }
