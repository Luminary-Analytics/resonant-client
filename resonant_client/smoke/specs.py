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

from dataclasses import dataclass


@dataclass(frozen=True)
class SmokeSpec:
    """A bundled smoke spec.

    `intent_id_prefix` is used by the harness as the canonical mission
    ID prefix for this spec; it gets a model-label suffix appended so
    multiple model runs under the same project don't collide.
    """
    name: str                  # short kebab-case label (CLI arg)
    description: str           # one-line for `list-specs` output
    intent_id_prefix: str      # for the persisted roadmap filename
    spec_markdown: str         # rigorous-grill spec ready to parse
    expected_iter_seconds: tuple[int, int] = (60, 600)  # (min, max) — for variance bounds checks


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
