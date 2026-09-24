"""
Bundled smoke specs.

Each spec is a self-contained `SmokeSpec` — a label, a description,
and the rigorous-grill-formatted markdown that build_roadmap_from_spec
parses. Specs are deliberately bash-only (no [chrome] / [vision]) so
they run without a dev server / vision model.

Add new specs here. Keep each one minimal-but-realistic — the harness
exists to catch regressions, not to test the model's performance on
hard problems. Smokes that take >10m on pro are too slow to be useful
as part of a tight iteration cycle.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping


@dataclass(frozen=True)
class SmokeSpec:
    """A bundled smoke spec.

    `intent_id_prefix` is used by the harness as the canonical mission
    ID prefix for this spec; it gets a model-label suffix appended so
    multiple model runs under the same project don't collide.

    `seed_files` (v0.5.8a4) is for refactor-style specs that need
    pre-existing code in the fresh project. Maps repo-relative paths
    to verbatim file contents; the smoke runner writes these BEFORE
    dispatching the autonomous mission, then `git add . && git
    commit` so the seeded code is the baseline rather than the
    autonomous loop's first commit. Empty for greenfield specs.
    """
    name: str                  # short kebab-case label (CLI arg)
    description: str           # one-line for `list-specs` output
    intent_id_prefix: str      # for the persisted roadmap filename
    spec_markdown: str         # rigorous-grill spec ready to parse
    expected_iter_seconds: tuple[int, int] = (60, 600)  # (min, max) — for variance bounds checks
    seed_files: Mapping[str, str] = field(default_factory=dict)
    # v0.5.8a4 — None means "validated against Mac Studio in a prior
    # release". False means "added but not yet smoke-validated against
    # a live model"; the CLI surfaces a warning in that case so users
    # know the convergence numbers haven't been pinned yet.
    validated: bool = True


# ── minimal: hello.txt ──────────────────────────────────────────────────


_MINIMAL_SPEC_MD = """\
## Final spec

**Refined intent:** Create a file called `hello.txt` at the project
root containing exactly the text `hello world` followed by a newline.

**Key assumptions:**
- Plain UTF-8 text file
- Project root is the working directory

**In scope:**
- Single file creation

**Out of scope:**
- Anything else

**Time budget:** 1h

**Technical constraints:**
- POSIX-compatible commands

**Acceptance criteria:**
- `[bash]` `test -f hello.txt` exits 0
- `[bash]` `cat hello.txt` output == hello world

**Open risks:**
- File encoding / line endings
"""


# ── wordcount: single-file Python CLI (the v0.5.1 / v0.5.2 driver) ─────


_WORDCOUNT_SPEC_MD = """\
## Final spec

**Refined intent:** Build a Python CLI utility `wordcount.py` at the
project root. It takes a single file path argument and prints
space-separated `<lines> <words> <chars>` to stdout, matching the
output of `wc -lwc` for the same file.

**Key assumptions:**
- Python 3 stdlib only (no third-party deps)
- Newline-terminated lines
- UTF-8 encoded input

**In scope:**
- Single CLI script
- File path argument
- Stdout output: `<lines> <words> <chars>`

**Out of scope:**
- Stdin support
- Multiple file arguments
- Locale-specific word counting

**Time budget:** 1h

**Technical constraints:**
- Python 3 stdlib only
- Single file (`wordcount.py`)
- Exits 0 on success, non-zero on missing file

**Acceptance criteria:**
- `[bash]` `test -f wordcount.py` exits 0
- `[bash]` `python wordcount.py wordcount.py` exits 0
- `[bash]` `python wordcount.py /no/such/file 2>&1; test $? -ne 0` exits 0
- `[bash]` `printf 'a b c\\nd e\\n' > /tmp/wc-smoke.txt && python wordcount.py /tmp/wc-smoke.txt` output == 2 5 10

**Open risks:**
- Trailing-newline counting (matches `wc -l` convention: trailing newline counts as line)
"""


# ── roguelite: multi-file TS scaffold (the v0.5.2 stretch) ─────────────


_ROGUELITE_SPEC_MD = """\
## Final spec

**Refined intent:** Bootstrap a TypeScript roguelite skeleton with
strict tsc, a centered Canvas rendering the player as a single green
circle on a dark navy background, dev-server-driven via Vite. Six
source files total, no `any` types.

**Key assumptions:**
- Greenfield (no existing code touched)
- Vite is acceptable as the dev server
- Player is rendered with the 2D canvas API, no third-party engine
- TypeScript strict mode is non-negotiable

**In scope:**
- Project scaffold (package.json, tsconfig.json, vite.config.ts)
- Canvas mounting + 800×600 sizing
- Player as a centered green circle on dark navy background
- Single index.html entry point

**Out of scope:**
- Movement / input
- Map generation
- Combat / enemies / items
- Tests (covered by criteria)

**Time budget:** 1h

**Technical constraints:**
- Strict TypeScript (no `any`)
- Exactly 6 source files in src/
- No third-party game engines
- Stdlib + Vite + TypeScript only

**Acceptance criteria:**
- `[bash]` `test -f package.json` exits 0
- `[bash]` `test -f tsconfig.json` exits 0
- `[bash]` `test -f index.html` exits 0
- `[bash]` `find src -type f \\( -name '*.ts' -o -name '*.tsx' \\) | wc -l` output == 6
- `[bash]` `! grep -rnE ': any[^a-zA-Z_]' src/` exits 0
- `[bash]` `cat tsconfig.json | python -c "import json,sys; c=json.load(sys.stdin); assert c['compilerOptions']['strict']==True" && echo ok` output == ok

**Open risks:**
- Model may add unnecessary third-party deps despite the constraint
- TS strict mode interpretation can vary
- Source-file counting depends on `find`'s behavior
"""


# ── jsonlines: JSONL → CSV converter (v0.5.8a4) ───────────────────────


_JSONLINES_SPEC_MD = """\
## Final spec

**Refined intent:** Build a Python CLI utility `jsonl2csv.py` at the
project root. It takes a JSONL file path and a comma-separated list of
column names, and writes a CSV (with header row) to stdout. Each JSONL
record is one row in the CSV; missing keys produce empty cells; extra
keys are ignored. Output uses the python `csv` module's default
dialect (RFC 4180-ish, comma separator, quoted as needed).

**Key assumptions:**
- Python 3 stdlib only (json + csv + argparse, no third-party deps)
- One JSON object per input line
- Missing keys in a record → empty cell (NOT an error)
- Extra keys not in the column list → silently ignored

**In scope:**
- Single CLI script (`jsonl2csv.py`)
- File path positional + `--columns col1,col2,col3` argument
- CSV output to stdout, header row first

**Out of scope:**
- Multiple input files
- Type coercion / column-type hints
- Streaming output to a file (use shell redirect)
- Skipping bad lines (malformed JSON should fail loud)

**Time budget:** 1h

**Technical constraints:**
- Python 3 stdlib only
- Single file (`jsonl2csv.py`)
- Exits 0 on success, non-zero on argument or parse errors

**Acceptance criteria:**
- `[bash]` `test -f jsonl2csv.py` exits 0
- `[bash]` `python jsonl2csv.py --help 2>&1 | grep -qi 'columns'` exits 0
- `[bash]` `printf '{"a":1,"b":2}\\n{"a":3,"b":4}\\n' > /tmp/jl-smoke.jsonl && python jsonl2csv.py /tmp/jl-smoke.jsonl --columns a,b` output == a,b\\n1,2\\n3,4
- `[bash]` `printf '{"a":1}\\n{"a":2,"b":3}\\n' > /tmp/jl-missing.jsonl && python jsonl2csv.py /tmp/jl-missing.jsonl --columns a,b` output == a,b\\n1,\\n2,3
- `[bash]` `python jsonl2csv.py /no/such/file --columns a 2>&1; test $? -ne 0` exits 0

**Open risks:**
- Output trailing newline behavior (csv.writer adds \\r\\n by default; criterion may want \\n)
- Quoting differences between csv module versions (negligible in practice)
"""


# ── refactor-py: fix a pre-seeded buggy script (v0.5.8a4) ─────────────


_REFACTOR_PY_SPEC_MD = """\
## Final spec

**Refined intent:** The project already contains a `fizzbuzz.py`
script with a known off-by-one bug (it prints fizzbuzz from 1 to N-1
instead of 1 to N) and a `tests/test_fizzbuzz.py` pytest file that
currently fails because of the bug. Fix the bug in `fizzbuzz.py` so
the tests pass. Do NOT modify the tests. Do NOT add new files. Do
NOT change `fizzbuzz.py`'s public function signature.

**Key assumptions:**
- The bug is in the loop bound — the existing range stops one short
- Tests are the source of truth; do not edit them to make them pass
- The `fizzbuzz` function takes one int arg `n` and returns a list of strings

**In scope:**
- Single-line fix (or thereabouts) to `fizzbuzz.py`
- All existing tests pass

**Out of scope:**
- Adding new tests
- Renaming the function or its parameters
- Adding new files (e.g. README, requirements.txt)
- Changing the project structure

**Time budget:** 1h

**Technical constraints:**
- Python 3 stdlib only
- Preserve the existing function signature `fizzbuzz(n: int) -> list[str]`
- The loop must iterate from 1 to n inclusive (currently 1 to n-1)

**Acceptance criteria:**
- `[bash]` `test -f fizzbuzz.py` exits 0
- `[bash]` `test -f tests/test_fizzbuzz.py` exits 0
- `[bash]` `python -m pytest tests/test_fizzbuzz.py -q` exits 0
- `[bash]` `python -c "import fizzbuzz; r = fizzbuzz.fizzbuzz(15); assert len(r) == 15, len(r); assert r[14] == 'fizzbuzz', r[14]; print('ok')"` output == ok
- `[bash]` `git diff --name-only HEAD~0 -- tests/ 2>/dev/null | wc -l | tr -d ' '` output == 0

**Open risks:**
- Model may rewrite the entire function instead of the minimal fix; that's
  acceptable as long as the criteria pass
- Model may delete tests to "fix" failures; the last criterion guards against this
"""


# Pre-seeded files for the refactor-py spec.
_REFACTOR_PY_BUGGY = '''\
"""FizzBuzz with a known off-by-one bug. The loop stops one short."""
from typing import List


def fizzbuzz(n: int) -> List[str]:
    """Return ['1', '2', 'fizz', '4', 'buzz', ...] up through n inclusive."""
    out: List[str] = []
    # BUG: should be range(1, n + 1) — currently misses the n'th item.
    for i in range(1, n):
        if i % 15 == 0:
            out.append("fizzbuzz")
        elif i % 3 == 0:
            out.append("fizz")
        elif i % 5 == 0:
            out.append("buzz")
        else:
            out.append(str(i))
    return out


if __name__ == "__main__":
    import sys
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 15
    for line in fizzbuzz(n):
        print(line)
'''

_REFACTOR_PY_TEST = '''\
"""Pytest that catches the off-by-one bug. Don't modify this file."""
import pytest

import fizzbuzz as fb


def test_returns_n_items():
    assert len(fb.fizzbuzz(1)) == 1
    assert len(fb.fizzbuzz(15)) == 15
    assert len(fb.fizzbuzz(100)) == 100


def test_first_item_is_1():
    assert fb.fizzbuzz(1) == ["1"]


def test_15_is_fizzbuzz():
    result = fb.fizzbuzz(15)
    assert result[14] == "fizzbuzz"


def test_3_is_fizz():
    assert fb.fizzbuzz(3)[2] == "fizz"


def test_5_is_buzz():
    assert fb.fizzbuzz(5)[4] == "buzz"


def test_signature_unchanged():
    import inspect
    sig = inspect.signature(fb.fizzbuzz)
    assert list(sig.parameters) == ["n"]
    assert sig.parameters["n"].annotation is int
'''


# ── Registry ────────────────────────────────────────────────────────────


SPECS: dict[str, SmokeSpec] = {
    spec.name: spec for spec in [
        SmokeSpec(
            name="minimal",
            description="One file, two bash criteria - fastest convergence check (~30-60s).",
            intent_id_prefix="smoke-minimal",
            spec_markdown=_MINIMAL_SPEC_MD,
            expected_iter_seconds=(20, 180),
        ),
        SmokeSpec(
            name="wordcount",
            description="Single-file Python CLI, 4 bash criteria including output equality.",
            intent_id_prefix="smoke-wordcount",
            spec_markdown=_WORDCOUNT_SPEC_MD,
            expected_iter_seconds=(60, 360),
        ),
        SmokeSpec(
            name="roguelite",
            description="Multi-file TS+Vite scaffold, 6 bash criteria - stress test for planner+implementer.",
            intent_id_prefix="smoke-roguelite",
            spec_markdown=_ROGUELITE_SPEC_MD,
            expected_iter_seconds=(120, 600),
        ),
        # v0.5.8a4 — JSONL → CSV converter. Different I/O shape than
        # wordcount (JSON parsing, columnar output) but similar scope.
        # Tests json + csv + argparse coordination. UNVALIDATED — needs
        # a Mac Studio run before its convergence numbers are pinned.
        SmokeSpec(
            name="jsonlines",
            description="Single-file JSONL-to-CSV converter, 5 bash criteria. UNVALIDATED.",
            intent_id_prefix="smoke-jsonlines",
            spec_markdown=_JSONLINES_SPEC_MD,
            expected_iter_seconds=(60, 360),
            validated=False,
        ),
        # v0.5.8a4 — refactor case. Pre-seeds a buggy fizzbuzz.py +
        # passing-when-fixed pytest. Tests the agent's ability to make
        # a minimal change to existing code without rewriting the
        # world. The first smoke spec that ISN'T greenfield. UNVALIDATED.
        SmokeSpec(
            name="refactor-py",
            description="Fix off-by-one in pre-seeded fizzbuzz.py without breaking tests. UNVALIDATED.",
            intent_id_prefix="smoke-refactor-py",
            spec_markdown=_REFACTOR_PY_SPEC_MD,
            expected_iter_seconds=(60, 360),
            validated=False,
            seed_files={
                "fizzbuzz.py": _REFACTOR_PY_BUGGY,
                "tests/test_fizzbuzz.py": _REFACTOR_PY_TEST,
            },
        ),
    ]
}


def get_spec(name: str) -> SmokeSpec:
    """Look up a spec by name. Raises ValueError on unknown names —
    the CLI catches this and prints the list of valid names."""
    if name not in SPECS:
        valid = ", ".join(sorted(SPECS))
        raise ValueError(
            f"Unknown spec {name!r}. Valid specs: {valid}"
        )
    return SPECS[name]


def list_spec_names() -> list[str]:
    """Sorted list of registered spec names — used by `list-specs` and
    by argparse choices for `--spec`."""
    return sorted(SPECS)
