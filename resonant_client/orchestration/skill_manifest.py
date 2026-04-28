"""
Per-project skill manifest at `.resonant/skills.toml`.

Like `requirements.txt` for skills: the project declares which skills it depends
on; implementations stay per-machine in `~/.resonant/skills/`. On project load,
the GUI scans the manifest, checks installed status, and surfaces gaps so the
user can install missing skills (or the agent can attempt to auto-install if a
registry is wired in the future).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# Python 3.11+ ships tomllib in stdlib. Fall back to a vendored dict if absent.
try:
    import tomllib as _toml  # type: ignore
except ImportError:  # pragma: no cover - runs on 3.10 only
    _toml = None  # type: ignore

from .skills import Skill, list_skills, load_skill

logger = logging.getLogger(__name__)


MANIFEST_RELPATH = Path(".resonant") / "skills.toml"


# ── Manifest data model ─────────────────────────────────────────────────


@dataclass
class SkillRequirement:
    """One entry in `[required]` or `[optional]`."""
    skill_id: str           # "fix-python-import-error"
    version_spec: str = ""  # "" | ">=1.2" | "@1.0.3" | ...

    @classmethod
    def parse(cls, raw: str) -> "SkillRequirement":
        """Parse `skill-id@version` or `skill-id` syntax."""
        raw = raw.strip()
        if "@" in raw:
            sid, spec = raw.split("@", 1)
            return cls(skill_id=sid.strip(), version_spec=spec.strip())
        return cls(skill_id=raw)


@dataclass
class SkillManifest:
    required: list[SkillRequirement] = field(default_factory=list)
    optional: list[SkillRequirement] = field(default_factory=list)
    auto_install: bool = True
    warn_on_missing: bool = True


# ── Read / write ────────────────────────────────────────────────────────


def manifest_path(project_path: str | Path) -> Path:
    return Path(project_path) / MANIFEST_RELPATH


def read_manifest(project_path: str | Path) -> Optional[SkillManifest]:
    """Load the manifest. Returns None if absent (project uses no skills)."""
    path = manifest_path(project_path)
    if not path.is_file():
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("Failed to read %s: %s", path, exc)
        return None

    data: dict = {}
    if _toml is not None:
        try:
            data = _toml.loads(text)
        except Exception as exc:
            logger.warning("Failed to parse %s as TOML: %s", path, exc)
            return None
    else:
        # Minimal fallback for Python 3.10 without tomllib: very small subset
        # of TOML — supports just `key = value` and `[section]` headers used here.
        data = _toml_lite_parse(text)

    required_raw = (data.get("required") or {}).get("skills") or []
    optional_raw = (data.get("optional") or {}).get("skills") or []
    install_section = data.get("install") or {}

    return SkillManifest(
        required=[SkillRequirement.parse(s) for s in required_raw if isinstance(s, str)],
        optional=[SkillRequirement.parse(s) for s in optional_raw if isinstance(s, str)],
        auto_install=bool(install_section.get("auto", True)),
        warn_on_missing=bool(install_section.get("warn-on-missing", True)),
    )


def write_manifest(project_path: str | Path, manifest: SkillManifest) -> Path:
    """Serialize a manifest to `.resonant/skills.toml`. Returns the path."""
    path = manifest_path(project_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    def _format(req: SkillRequirement) -> str:
        return f"{req.skill_id}@{req.version_spec}" if req.version_spec else req.skill_id

    lines: list[str] = [
        "# Skills this project depends on. Implementations live in ~/.resonant/skills/.",
        "# Run `resonant skills install` (or load the project) to materialize them.",
        "",
    ]
    if manifest.required:
        lines.append("[required]")
        lines.append("skills = [")
        for r in manifest.required:
            lines.append(f'    "{_format(r)}",')
        lines.append("]")
        lines.append("")
    if manifest.optional:
        lines.append("[optional]")
        lines.append("skills = [")
        for r in manifest.optional:
            lines.append(f'    "{_format(r)}",')
        lines.append("]")
        lines.append("")
    lines.append("[install]")
    lines.append(f"auto = {str(manifest.auto_install).lower()}")
    lines.append(f"warn-on-missing = {str(manifest.warn_on_missing).lower()}")
    lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


# ── Status check ────────────────────────────────────────────────────────


@dataclass
class ManifestStatus:
    manifest: Optional[SkillManifest]
    installed: list[Skill] = field(default_factory=list)
    missing_required: list[SkillRequirement] = field(default_factory=list)
    missing_optional: list[SkillRequirement] = field(default_factory=list)

    def has_gaps(self) -> bool:
        return bool(self.missing_required) or bool(self.missing_optional)


def check_manifest_status(project_path: str | Path) -> ManifestStatus:
    """Resolve the manifest against the locally installed skill library."""
    manifest = read_manifest(project_path)
    if manifest is None:
        return ManifestStatus(manifest=None)

    installed: list[Skill] = []
    missing_req: list[SkillRequirement] = []
    missing_opt: list[SkillRequirement] = []

    for req in manifest.required:
        skill = _resolve_skill(req.skill_id, project_path)
        if skill:
            installed.append(skill)
        else:
            missing_req.append(req)

    for req in manifest.optional:
        skill = _resolve_skill(req.skill_id, project_path)
        if skill:
            installed.append(skill)
        else:
            missing_opt.append(req)

    return ManifestStatus(
        manifest=manifest,
        installed=installed,
        missing_required=missing_req,
        missing_optional=missing_opt,
    )


def _resolve_skill(skill_id: str, project_path: str | Path) -> Optional[Skill]:
    """Look up a skill by id across all scopes (global → project → stack)."""
    # Try global first (most common); fall back to project-scoped.
    for scope, kwargs in (
        ("global", {}),
        ("project", {"project_path": project_path}),
    ):
        skill = load_skill(skill_id, scope=scope, **kwargs)
        if skill:
            return skill
    return None


def save_current_skill_set(
    project_path: str | Path,
    *,
    used_skill_ids: list[str],
) -> Path:
    """Generate / update the manifest from a list of skill ids the project actively uses."""
    existing = read_manifest(project_path) or SkillManifest()
    # Keep optional skills + install settings; rewrite required to match used set.
    existing.required = [SkillRequirement(skill_id=s) for s in sorted(set(used_skill_ids))]
    return write_manifest(project_path, existing)


# ── Tiny TOML fallback for 3.10 (kept minimal on purpose) ──────────────


_SECTION_RE = re.compile(r"^\s*\[([\w.\-]+)\s*\]\s*$")
_KV_RE = re.compile(r"^\s*([\w\-]+)\s*=\s*(.+?)\s*$")


def _toml_lite_parse(text: str) -> dict:
    """Bare-minimum TOML parser (sections + scalars + simple arrays).

    This exists only so we don't hard-require Python 3.11. CI runs on 3.13
    where tomllib is available; this branch is exercised by the fallback test.
    """
    result: dict = {}
    section: dict = result
    in_array_key: Optional[str] = None
    array_buf: list[str] = []

    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        if in_array_key is not None:
            array_buf.append(line.strip())
            if "]" in line:
                joined = "\n".join(array_buf)
                items = re.findall(r'"([^"]*)"', joined)
                section[in_array_key] = items
                in_array_key = None
                array_buf = []
            continue
        m_section = _SECTION_RE.match(line)
        if m_section:
            name = m_section.group(1)
            section = result.setdefault(name, {})
            continue
        m_kv = _KV_RE.match(line)
        if m_kv:
            key, value = m_kv.group(1), m_kv.group(2).strip()
            if value.startswith("["):
                if "]" in value:
                    items = re.findall(r'"([^"]*)"', value)
                    section[key] = items
                else:
                    in_array_key = key
                    array_buf = [value]
                continue
            if value.startswith('"') and value.endswith('"'):
                section[key] = value[1:-1]
            elif value.lower() in ("true", "false"):
                section[key] = value.lower() == "true"
            else:
                try:
                    section[key] = int(value)
                except ValueError:
                    try:
                        section[key] = float(value)
                    except ValueError:
                        section[key] = value
    return result
