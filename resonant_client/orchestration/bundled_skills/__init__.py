"""Bundled skills — shipped with the package.

These are reference skills with `created_by="bundled"` provenance. The
curator NEVER touches them. They get installed on first run via
`install_bundled_skills(force=False)` which:

- Creates a Skill record in `~/.resonant/skills/global/<id>/skill.json`
- Copies the procedure.md + verification.md sidecar files
- Skips if the skill already exists (unless force=True)

The bundled skills serve as:
1. Worked examples of the SKILL.md format / Skill dataclass shape.
2. Sane defaults for common workflows (e.g. greenfield project setup).
3. Targets the auto-extractor and curator can compare against (e.g.
   "is this candidate skill just a worse version of the bundled one?").

Adding a new bundled skill:
1. Write `resonant_client/orchestration/bundled_skills/<id>.md` with
   YAML frontmatter (matching the Skill dataclass fields) + a markdown
   body that becomes the skill's procedure.md.
2. The frontmatter is parsed by `install_bundled_skills`; missing
   fields fall back to dataclass defaults.
3. `created_by` is forced to "bundled" regardless of what the
   frontmatter says.
"""
from __future__ import annotations

import logging
import re
import time
from importlib import resources
from typing import Iterator

from ..skills import Skill, load_skill, save_skill

logger = logging.getLogger(__name__)


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    """Split a `---`-fenced YAML frontmatter from the markdown body.

    Returns (frontmatter_dict, body_markdown). If no frontmatter is
    present, returns ({}, text). YAML is parsed with a tiny inline
    parser to avoid pulling in PyYAML for this one use.
    """
    match = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)$", text, re.DOTALL)
    if not match:
        return {}, text
    fm_text, body = match.group(1), match.group(2)
    fm: dict = {}
    for line in fm_text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip()
        # Strip surrounding quotes.
        if value.startswith(('"', "'")) and value.endswith(value[0]):
            value = value[1:-1]
        # Convert simple types.
        if value.lower() in ("true", "yes"):
            fm[key] = True
        elif value.lower() in ("false", "no"):
            fm[key] = False
        elif value.startswith("[") and value.endswith("]"):
            inner = value[1:-1].strip()
            if not inner:
                fm[key] = []
            else:
                fm[key] = [item.strip().strip('"\'') for item in inner.split(",")]
        else:
            fm[key] = value
    return fm, body


def _iter_bundled_files() -> Iterator[tuple[str, str]]:
    """Yield (skill_id, file_text) for every .md file in this package's
    bundled_skills/ dir.

    Uses importlib.resources so this works when the package is installed
    via pip / pyinstaller / wheel — not just from a source checkout.
    """
    pkg = resources.files("resonant_client.orchestration.bundled_skills")
    for entry in pkg.iterdir():
        if not entry.is_file() or not entry.name.endswith(".md"):
            continue
        skill_id = entry.name[:-3]  # strip .md
        yield skill_id, entry.read_text(encoding="utf-8")


def install_bundled_skills(*, force: bool = False) -> list[Skill]:
    """Materialize bundled skills into `~/.resonant/skills/global/`.

    Idempotent: skips skills that already exist on disk unless
    `force=True`. Returns the list of skills that were freshly
    installed (NOT the list that already existed).

    Called from the resonant-client startup path (e.g. once during
    `AppState.__init__` or first `resonant-gui` launch). Cheap enough
    to run unconditionally — the existence check is just an os.stat
    per bundled skill.
    """
    installed: list[Skill] = []
    for skill_id, text in _iter_bundled_files():
        existing = load_skill(skill_id, scope="global")
        if existing is not None and not force:
            continue
        fm, body = _parse_frontmatter(text)
        # Build a Skill with the parsed frontmatter, falling back to
        # defaults for missing fields. created_by is FORCED to bundled
        # regardless of what the file said — bundled-skill provenance
        # is non-negotiable.
        triggers_raw = fm.get("triggers", [])
        if isinstance(triggers_raw, str):
            triggers_raw = [triggers_raw]
        skill = Skill(
            id=skill_id,
            name=str(fm.get("name") or skill_id.replace("-", " ").title()),
            description=str(fm.get("description") or ""),
            scope="global",
            triggers=list(triggers_raw),
            prerequisites=[],
            success_count=0,
            fail_count=0,
            last_used_at=time.time(),
            version=str(fm.get("version") or "1.0.0"),
            tokens=[],  # populated below
            procedure_steps=[],
            created_by="bundled",
            pinned=bool(fm.get("pinned", False)),
        )
        # Populate tokens for similarity matching: name + description
        # + triggers + a sample of the body.
        from ..skills import tokenize
        token_source = " ".join([skill.name, skill.description] + skill.triggers + [body[:1000]])
        skill.tokens = sorted(set(tokenize(token_source)))

        save_skill(skill, procedure_md=body)
        installed.append(skill)
        logger.info("Installed bundled skill: %s", skill_id)
    return installed


def bundled_skill_ids() -> list[str]:
    """Return the slugs of all bundled skills available in the package.
    Useful for `resonant skill list --bundled`."""
    return sorted(skill_id for skill_id, _ in _iter_bundled_files())
