"""
Project conventions loader.

Reads project-specific instructions from a markdown file at the project root.
Prefers `AGENTS.md` (the cross-tool standard adopted by Codex CLI, OpenCode,
Cursor, OpenHands as of 2026) and falls back to legacy `RESONANT.md` for older
Resonant projects. `CLAUDE.md` (Anthropic's pattern) is recognized as a final
fallback so users coming from Claude Code get continuity for free.
"""

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# Files to search, in priority order (first found wins).
#
# AGENTS.md is the cross-tool convention — committing one file gives a project
# instant interop with Codex, OpenCode, Cursor, OpenHands, and (via bridge
# import) Claude Code. RESONANT.md is the historical Resonant-only filename;
# kept for back-compat. CLAUDE.md is a courtesy fallback so users with an
# existing Claude Code project don't have to copy/paste their conventions.
INSTRUCTION_FILES = [
    "AGENTS.md",
    ".agents/AGENTS.md",
    "RESONANT.md",
    ".resonant/RESONANT.md",
    "CLAUDE.md",
]


def find_instruction_file(project_path: str) -> Path | None:
    """Find the RESONANT.md file for a project."""
    root = Path(project_path)
    for rel in INSTRUCTION_FILES:
        candidate = root / rel
        if candidate.is_file():
            return candidate
    return None


def load_project_instructions(project_path: str) -> str | None:
    """Load and return project instructions text, or None if not found."""
    path = find_instruction_file(project_path)
    if not path:
        return None
    try:
        text = path.read_text(encoding="utf-8").strip()
        if not text:
            return None
        logger.info(f"Loaded project instructions from {path}")
        return text
    except OSError as e:
        logger.warning(f"Failed to read {path}: {e}")
        return None


def get_instruction_sections(text: str) -> dict[str, str]:
    """Parse RESONANT.md into sections by top-level headings.

    Returns dict like {"instructions": "...", "conventions": "...", ...}
    """
    sections: dict[str, str] = {}
    current_key = "_preamble"
    current_lines: list[str] = []

    for line in text.split("\n"):
        stripped = line.strip()
        if stripped.startswith("# "):
            # Save previous section
            if current_lines:
                sections[current_key] = "\n".join(current_lines).strip()
            current_key = stripped[2:].strip().lower().replace(" ", "_")
            current_lines = []
        else:
            current_lines.append(line)

    # Save last section
    if current_lines:
        sections[current_key] = "\n".join(current_lines).strip()

    return sections


def find_all_instruction_files(project_path: str, cwd: str | None = None) -> list[tuple[str, Path]]:
    """
    Find all RESONANT.md files in the hierarchy (Codex AGENTS.md pattern).

    Returns list of (scope_label, path) in resolution order:
    1. Global: ~/.resonant/RESONANT.md
    2. Project root: <project>/RESONANT.md
    3. Directory walk: from cwd up to project root

    Later entries take precedence for same-named headings.
    """
    found: list[tuple[str, Path]] = []
    root = Path(project_path).resolve()

    # 1. Global instructions — AGENTS.md preferred, RESONANT.md kept for back-compat.
    for global_name in ("AGENTS.md", "RESONANT.md"):
        global_path = Path.home() / ".resonant" / global_name
        if global_path.is_file():
            found.append(("global", global_path))
            break

    # 2. Project root instructions
    for rel in INSTRUCTION_FILES:
        candidate = root / rel
        if candidate.is_file():
            found.append(("project", candidate))
            break

    # 3. Directory-scoped instructions (walk from project root toward cwd)
    if cwd:
        cwd_path = Path(cwd).resolve()
        # Collect directories between project root and cwd
        try:
            relative = cwd_path.relative_to(root)
            parts = relative.parts
            current = root
            for part in parts:
                current = current / part
                for rel in INSTRUCTION_FILES:
                    candidate = current / rel
                    if candidate.is_file() and candidate.resolve() != root.resolve() / rel:
                        scope_label = f"directory:{'/'.join(current.relative_to(root).parts)}"
                        found.append((scope_label, candidate))
                        break
        except ValueError:
            pass  # cwd not under project root

    return found


def load_hierarchical_instructions(project_path: str, cwd: str | None = None) -> str | None:
    """
    Load and merge RESONANT.md files from all hierarchy levels.

    Global → project → directory-scoped, concatenated with section headers.
    """
    files = find_all_instruction_files(project_path, cwd)
    if not files:
        return None

    parts: list[str] = []
    for scope_label, path in files:
        try:
            text = path.read_text(encoding="utf-8").strip()
            if text:
                label_map = {
                    "global": f"GLOBAL INSTRUCTIONS ({path.name})",
                    "project": f"PROJECT INSTRUCTIONS ({path.name})",
                }
                label = label_map.get(scope_label, f"DIRECTORY INSTRUCTIONS ({scope_label})")
                parts.append(f"--- {label} ---\n{text}\n--- END {label.split('(')[0].strip()} ---")
                logger.info(f"Loaded {scope_label} instructions from {path}")
        except OSError as e:
            logger.warning(f"Failed to read {path}: {e}")

    return "\n\n".join(parts) if parts else None


def format_for_system_prompt(project_path: str, cwd: str | None = None) -> str | None:
    """Load RESONANT.md hierarchy and format for injection into the system prompt."""
    text = load_hierarchical_instructions(project_path, cwd)
    if not text:
        return None
    return f"\n\n{text}"


def get_instruction_info(project_path: str) -> dict:
    """Return metadata about instruction files for frontend display."""
    path = find_instruction_file(project_path)
    files = find_all_instruction_files(project_path)
    if not path and not files:
        return {"exists": False}
    try:
        text = load_project_instructions(project_path) or ""
        sections = get_instruction_sections(text) if text else {}
        return {
            "exists": bool(path or files),
            "path": str(path) if path else "",
            "sections": list(sections.keys()),
            "size": len(text),
            "active_files": [
                {"scope": scope, "path": str(p)} for scope, p in files
            ],
        }
    except OSError:
        return {"exists": False}
