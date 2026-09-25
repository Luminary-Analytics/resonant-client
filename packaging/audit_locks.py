"""Audit every pin in the release and tools locks for known vulnerabilities.

    python packaging/audit_locks.py [extra pip-audit arguments]

pip-audit evaluates environment markers and silently skips requirements that
don't apply to the machine it runs on, so auditing the universal lock on one
operating system would miss the others' packages (pyobjc on macOS, pythonnet on
Windows). This removes the markers, keeps the hashes, and audits each lock with
pip-audit (from packaging/tools-requirements.txt).
"""

from __future__ import annotations

import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOCKS = (ROOT / "packaging" / "requirements-release.txt", ROOT / "packaging" / "tools-requirements.txt")

# "name==1.0 ; sys_platform == 'win32' \" -> "name==1.0 \"
_MARKER = re.compile(r"^(?P<pin>[A-Za-z0-9][^\s;]*==[^\s;]+)\s*;[^\\\n]*(?P<rest>\\?)$", re.MULTILINE)


def without_markers(text: str) -> str:
    """The lock with every environment marker removed, hashes and comments kept."""
    return _MARKER.sub(lambda m: m.group("pin") + (" \\" if m.group("rest") else ""), text)


def main(argv: list[str] | None = None) -> int:
    extra = list(sys.argv[1:] if argv is None else argv)
    worst = 0
    with tempfile.TemporaryDirectory() as folder:
        for lock in LOCKS:
            flat = Path(folder) / lock.name
            flat.write_text(without_markers(lock.read_text(encoding="utf-8")), encoding="utf-8")
            print(f"Auditing every pin in {lock.relative_to(ROOT)}", flush=True)
            result = subprocess.run(
                [sys.executable, "-m", "pip_audit", "--disable-pip", "-r", str(flat),
                 "--progress-spinner", "off", *extra],
            )
            worst = max(worst, result.returncode)
    return worst


if __name__ == "__main__":
    sys.exit(main())
