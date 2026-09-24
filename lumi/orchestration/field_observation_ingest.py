"""v0.6.2a5 — Ingest field-observation docs as user-provenance skills.

The field run (v0.6.2a1) produced
`docs/field-observations/2026-05-06-v0.6.1-self-improvement-loop.md`.
That doc captures real findings the agent should benefit from on
future missions — but the v0.6.0/v0.6.1 self-improvement loop only
captures skills from autonomous-iter completion, not from
human-authored field reports.

This module bridges the gap: parse a field-observations markdown file
and persist it as a Skill with `created_by="user"` and `pinned=True`.
That combination makes the skill curator-exempt (provenance gate) and
auto-deprecation-exempt (pin gate), so the field-obs survives without
needing a successful agent run to keep it warm.

The conversion is intentionally simple — H1 → name, first paragraph
→ description, full body → procedure_md. No model call, no semantic
extraction. The user-authored doc IS the skill body.

Idempotent by default: re-ingesting a doc whose skill already exists
is a no-op unless `force=True`.

Wired into the resonant-skill CLI as the `ingest-field-obs` subcommand.
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .skill_extraction import slugify
from .skills import Skill, load_skill, save_skill, tokenize

logger = logging.getLogger(__name__)


# Lines we strip from the description-extraction pass — common
# field-observations preamble that isn't useful as a skill summary.
_DESCRIPTION_NOISE_RE = re.compile(
    r"^\s*\*\*(date|predecessor|theme|project path|model|mission|"
    r"started|run setup|project|workspace|driver|gui port|backend)"
    r"[^*]*\*\*[^\n]*$",
    re.IGNORECASE | re.MULTILINE,
)


@dataclass
class IngestResult:
    """One field-obs file's ingestion outcome.

    Returned by `ingest_field_observation_file` so callers (CLI / tests)
    can present a uniform result shape.
    """
    path: Path
    skill_id: str
    skill: Optional[Skill]   # None when skipped (already exists, not forced)
    written: bool             # True if save_skill was called
    skipped_reason: str = ""  # populated when written=False
    dry_run: bool = False


def _parse_field_observation_md(text: str) -> tuple[str, str, str]:
    """Pull (name, description, body) from a field-observations doc.

    `name` — first H1 (`# ...`) line, falling back to `"Field observation"`
    `description` — first prose paragraph after the H1, with field-obs
        preamble (`**Date:**`, `**Project path:**`, etc.) stripped
    `body` — the full file content (verbatim, untouched)

    The body is preserved exactly so anything in the doc — code blocks,
    tables, finding lists, recommendation columns — flows through
    unchanged into procedure_md.
    """
    body = text
    name = "Field observation"
    description = ""

    # H1
    h1_match = re.search(r"^\s*#\s+(.+?)\s*$", text, re.MULTILINE)
    if h1_match:
        name = h1_match.group(1).strip().lstrip("—").strip()

    # First prose paragraph after H1
    after_h1 = text[h1_match.end():] if h1_match else text
    # Drop preamble lines (**Date:** / **Project path:** etc.) one pass
    cleaned = _DESCRIPTION_NOISE_RE.sub("", after_h1)
    paragraphs = re.split(r"\n\s*\n", cleaned.strip())
    for p in paragraphs:
        p_stripped = p.strip().lstrip(">").strip()  # also drop blockquote >
        # Skip headings and table rows.
        if not p_stripped:
            continue
        if p_stripped.startswith("#") or p_stripped.startswith("|") or p_stripped.startswith("---"):
            continue
        # Take the first 280 chars as description, collapsing whitespace.
        description = re.sub(r"\s+", " ", p_stripped)[:280]
        break
    if not description:
        # Fallback: use the name doubled up so we never end with empty.
        description = name

    return name, description, body


def ingest_field_observation_file(
    path: str | Path,
    *,
    skill_id_override: Optional[str] = None,
    force: bool = False,
    dry_run: bool = False,
    pinned: bool = True,
    scope: str = "global",
    project_path: Optional[str | Path] = None,
) -> IngestResult:
    """Parse a single field-observations .md and save it as a Skill.

    Defaults:
    - `created_by="user"` (provenance gate — curator-exempt)
    - `pinned=True` (durability gate — auto-deprecation-exempt)
    - `scope="global"` (field obs almost always cross-project)

    Override `skill_id_override` for tests. Pass `force=True` to
    overwrite an existing skill of the same id (ingestion is idempotent
    by default — repeat runs are no-ops).

    `dry_run=True` returns the parsed Skill without persisting; useful
    for the CLI's `--dry-run` flag.
    """
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(p)

    text = p.read_text(encoding="utf-8")
    name, description, body = _parse_field_observation_md(text)

    # Skill id from file stem (slugified). Field-obs file names are
    # usually `YYYY-MM-DD-topic.md`; slugify drops the date prefix
    # cleanly via word-boundary truncation if the topic is long enough.
    skill_id = skill_id_override or slugify(p.stem, max_len=60, drop_prefixes=False)

    existing = load_skill(skill_id, project_path=project_path) if scope == "global" else None
    if existing is not None and not force:
        return IngestResult(
            path=p,
            skill_id=skill_id,
            skill=existing,
            written=False,
            skipped_reason="already exists (use force=True to overwrite)",
            dry_run=dry_run,
        )

    skill = Skill(
        id=skill_id,
        name=name,
        description=description,
        scope=scope,
        triggers=[name, p.stem],
        prerequisites=[],
        success_count=0,
        fail_count=0,
        last_used_at=time.time(),
        version="1.0.0",
        tokens=sorted(set(tokenize(name + " " + description + " " + body[:2000]))),
        procedure_steps=[],          # field-obs are prose, not plan-graphs
        created_by="user",            # provenance gate — curator-exempt
        pinned=pinned,                 # durability gate — auto-dep-exempt
    )

    if dry_run:
        return IngestResult(
            path=p, skill_id=skill_id, skill=skill,
            written=False, skipped_reason="dry-run",
            dry_run=True,
        )

    save_skill(
        skill,
        procedure_md=body,
        project_path=str(project_path) if project_path else None,
    )
    return IngestResult(
        path=p, skill_id=skill_id, skill=skill,
        written=True, skipped_reason="",
        dry_run=False,
    )


def ingest_field_observation_dir(
    directory: str | Path,
    *,
    pattern: str = "*.md",
    force: bool = False,
    dry_run: bool = False,
) -> list[IngestResult]:
    """Walk a directory and ingest each matching field-obs file.

    Skips dotfiles and any file whose stem starts with a digit-underscore
    convention (NEXT-RUN-PREP, etc. — the user marks "not for skills"
    files this way; if they want it ingested they can pass the path
    explicitly to `ingest_field_observation_file`).

    Returns one IngestResult per file (including skips for already-
    existing). Empty list if the directory has no matching files.
    """
    d = Path(directory)
    if not d.is_dir():
        raise NotADirectoryError(d)
    results: list[IngestResult] = []
    for p in sorted(d.glob(pattern)):
        if p.name.startswith("."):
            continue
        # Heuristic: a file with `NEXT-RUN`, `TODO`, or `DRAFT` in its
        # stem is probably a planning doc, not a field observation.
        # Skip rather than ingest noise.
        upper = p.stem.upper()
        if any(marker in upper for marker in ("NEXT-RUN", "TODO", "DRAFT", ".SPEC")):
            continue
        try:
            result = ingest_field_observation_file(
                p, force=force, dry_run=dry_run,
            )
            results.append(result)
        except Exception as exc:
            logger.warning("Failed to ingest %s: %s", p, exc)
            results.append(IngestResult(
                path=p, skill_id="",
                skill=None, written=False,
                skipped_reason=f"error: {exc}",
                dry_run=dry_run,
            ))
    return results
