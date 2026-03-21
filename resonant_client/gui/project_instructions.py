"""
RESONANT.md — Project Instructions Loader.

Loads project-specific instructions from RESONANT.md files,
similar to how Claude Code uses CLAUDE.md. Supports:
  - RESONANT.md in project root
  - .resonant/RESONANT.md
  - Sections: Instructions, Conventions, Architecture, Memory, Context
"""

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# Files to search, in priority order (first found wins)
INSTRUCTION_FILES = [
    "RESONANT.md",
    ".resonant/RESONANT.md",
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


def format_for_system_prompt(project_path: str) -> str | None:
    """Load RESONANT.md and format it for injection into the system prompt."""
    text = load_project_instructions(project_path)
    if not text:
        return None
    return f"\n\n--- PROJECT INSTRUCTIONS (RESONANT.md) ---\n{text}\n--- END PROJECT INSTRUCTIONS ---"


def get_instruction_info(project_path: str) -> dict:
    """Return metadata about the instruction file for frontend display."""
    path = find_instruction_file(project_path)
    if not path:
        return {"exists": False}
    try:
        text = path.read_text(encoding="utf-8").strip()
        sections = get_instruction_sections(text)
        return {
            "exists": True,
            "path": str(path),
            "sections": list(sections.keys()),
            "size": len(text),
        }
    except OSError:
        return {"exists": False}
