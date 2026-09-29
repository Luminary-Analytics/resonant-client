"""
Linter detection and per-file invocation for the auto-lint feedback loop.

Used by Session.run() to optionally re-lint a file right after `file_edit` /
`file_write` and inject any errors back into the conversation as a synthetic
user turn — closing the manual round-trip the agent currently does.

Detection covers the most common configs found in repos this client targets:
- Python: ruff (preferred), flake8
- JS/TS:  eslint
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Optional

from lumi.executables import batch_argument_problem, find_program, project_tool
from lumi.processes import background_process_kwargs
from lumi.secrets_store import server_env


# (linter_name, base_argv). The file path is appended at lint time.
LinterSpec = tuple[str, list[str]]


def detect_linter(project_path: Path | str) -> Optional[LinterSpec]:
    """
    Look at `project_path` to figure out which linter the project uses.

    Returns (name, base_args) where the file path is appended later, or None
    if no linter config is detected.

    Detection precedence (first match wins):
      1. pyproject.toml [tool.ruff]            → ruff
      2. .ruff.toml / ruff.toml               → ruff
      3. pyproject.toml [tool.flake8]          → flake8
      4. .flake8                               → flake8
      5. .eslintrc{,.js,.json,.yml,.yaml}      → eslint
      6. package.json with "eslintConfig" key  → eslint
    """
    p = Path(project_path)
    if not p.is_dir():
        return None

    pyproject = p / "pyproject.toml"
    if pyproject.is_file():
        try:
            text = pyproject.read_text(encoding="utf-8", errors="replace")
            if "[tool.ruff]" in text or "[tool.ruff." in text:
                return ("ruff", ["ruff", "check", "--output-format=concise"])
            if "[tool.flake8]" in text:
                return ("flake8", ["flake8"])
        except OSError:
            pass

    if (p / ".ruff.toml").is_file() or (p / "ruff.toml").is_file():
        return ("ruff", ["ruff", "check", "--output-format=concise"])

    if (p / ".flake8").is_file() or (p / "setup.cfg").is_file():
        # setup.cfg may or may not have flake8 — only return it if present
        if (p / ".flake8").is_file():
            return ("flake8", ["flake8"])
        try:
            cfg = (p / "setup.cfg").read_text(encoding="utf-8", errors="replace")
            if "[flake8]" in cfg:
                return ("flake8", ["flake8"])
        except OSError:
            pass

    for cfg_name in (".eslintrc", ".eslintrc.js", ".eslintrc.cjs",
                     ".eslintrc.json", ".eslintrc.yml", ".eslintrc.yaml"):
        if (p / cfg_name).is_file():
            return ("eslint", ["npx", "--no-install", "eslint", "--format=compact"])

    pkg = p / "package.json"
    if pkg.is_file():
        try:
            data = json.loads(pkg.read_text(encoding="utf-8", errors="replace"))
            if isinstance(data, dict) and "eslintConfig" in data:
                return ("eslint", ["npx", "--no-install", "eslint", "--format=compact"])
        except (OSError, json.JSONDecodeError):
            pass

    return None


def _file_matches_linter(linter_name: str, file_path: Path) -> bool:
    """Don't run a Python linter on a .ts file, etc."""
    suffix = file_path.suffix.lower()
    if linter_name in ("ruff", "flake8"):
        return suffix in {".py", ".pyi"}
    if linter_name == "eslint":
        return suffix in {".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".vue"}
    return True


def lint_file(
    project_path: Path | str,
    file_path: Path | str,
    *,
    timeout: float = 10.0,
    trusted: bool = False,
) -> dict:
    """
    Run the detected linter on a single file. Cheap (per-file scope).

    ``trusted``: the project is trusted, so its own linter (``.venv``,
    ``venv``, ``node_modules/.bin``) comes before one on PATH
    (lumi/executables.py ``project_tool``).

    Returns:
        {
            "linter": str | None,    # name of the linter run
            "ok": bool,              # True if no errors / linter unavailable / no lint applicable
            "errors": str,           # captured stdout+stderr (only set if !ok)
            "skipped_reason": str,   # populated when skipped (no linter, wrong filetype, etc.)
            "source": str,           # "from the project's .venv", "from PATH"
            "notice": str,           # an untrusted project's own linter, unused until it's trusted
        }
    """
    p = Path(project_path)
    f = Path(file_path)

    detected = detect_linter(p)
    if not detected:
        return {"linter": None, "ok": True, "errors": "", "skipped_reason": "no linter detected"}

    name, base_args = detected
    if not _file_matches_linter(name, f):
        return {"linter": name, "ok": True, "errors": "", "skipped_reason": f"{name} doesn't apply to {f.suffix}"}

    # A trusted project's own linter, else the one installed on PATH, never
    # a program from the project otherwise (lumi/executables.py).
    if name == "eslint":
        linter, source, notice = project_tool("eslint", p, trusted=trusted, scripts=True)
        arguments = [*base_args[3:], str(f)]  # without "npx --no-install eslint"
        if not linter:
            linter, source = find_program("npx", exclude=[p]), "from PATH"
            arguments = [*base_args[1:], str(f)]
    else:
        linter, source, notice = project_tool(base_args[0], p, trusted=trusted)
        arguments = [*base_args[1:], str(f)]
    if not linter:
        return {"linter": name, "ok": True, "errors": "", "skipped_reason": f"{name} not installed",
                "source": "", "notice": notice}
    # node_modules/.bin/eslint.cmd starts through cmd.exe, which would parse the file's name.
    problem = batch_argument_problem(linter, arguments)
    if problem:
        return {"linter": name, "ok": True, "errors": "", "skipped_reason": problem, "source": source,
                "notice": notice}
    cmd = [linter, *arguments]
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(p),
            env=server_env(),
            capture_output=True,
            text=True,
            timeout=max(0.5, timeout),
            shell=False,
            encoding="utf-8",
            errors="replace",
            **background_process_kwargs(),
        )
    except FileNotFoundError:
        return {"linter": name, "ok": True, "errors": "", "skipped_reason": f"{name} not installed",
                "source": source, "notice": notice}
    except subprocess.TimeoutExpired:
        return {"linter": name, "ok": True, "errors": "", "skipped_reason": f"{name} timed out after {timeout:.1f}s",
                "source": source, "notice": notice}

    output = ((proc.stdout or "") + (proc.stderr or "")).strip()
    return {
        "linter": name,
        "ok": proc.returncode == 0,
        "errors": output if proc.returncode != 0 else "",
        "skipped_reason": "",
        "source": source,
        "notice": notice,
    }
