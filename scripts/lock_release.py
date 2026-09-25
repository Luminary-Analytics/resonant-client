"""Regenerate the pinned, hashed requirement files used by release builds and CI.

    python scripts/lock_release.py

* packaging/requirements-release.txt: every package the Windows installer is
  built from (core, `gui` and `desktop` extras, PyInstaller), resolved for all
  platforms and Python versions Lumi supports. scripts/build_clean.ps1 installs
  it with --require-hashes, so a release never picks up an unreviewed version.
* packaging/tools-requirements.txt: pip-audit and the CycloneDX generator that
  CI uses to audit the lock and write the SBOM.

Run it after changing dependencies in pyproject.toml, review the diff, and
commit both files. tests/test_release_supply_chain.py fails when pyproject
declares a dependency the release lock does not pin.

Needs uv (https://docs.astral.sh/uv/).
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGING = ROOT / "packaging"
COMMAND = "python scripts/lock_release.py"

LOCKS = (
    (
        PACKAGING / "requirements-release.txt",
        ["pyproject.toml", "packaging/build-requirements.in", "--extra", "gui", "--extra", "desktop"],
    ),
    (PACKAGING / "tools-requirements.txt", ["packaging/tools-requirements.in"]),
)


def main() -> int:
    uv = shutil.which("uv")
    if not uv:
        print("uv is required: https://docs.astral.sh/uv/getting-started/installation/", file=sys.stderr)
        return 1
    for output, inputs in LOCKS:
        command = [
            uv, "pip", "compile", *inputs,
            "--universal", "--python-version", "3.11", "--generate-hashes",
            "--custom-compile-command", COMMAND,
            "--output-file", str(output.relative_to(ROOT)),
        ]
        result = subprocess.run(command, cwd=ROOT)
        if result.returncode != 0:
            return result.returncode
    return 0


if __name__ == "__main__":
    sys.exit(main())
