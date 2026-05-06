"""v0.6.1a2 — CLI for the skill library.

Wired as the `resonant-skill` console script in pyproject.toml.
Subcommands cover the day-to-day skill management surface:

    resonant-skill list                      # list all skills
    resonant-skill list --created-by agent   # filter by provenance
    resonant-skill list --pinned             # only pinned
    resonant-skill list --scope project      # only project-scoped
    resonant-skill list --json               # machine-readable output

    resonant-skill view <id>                 # print skill body
    resonant-skill view <id> --json          # full skill.json

    resonant-skill pin <id>                  # mark pinned
    resonant-skill unpin <id>                # mark unpinned

    resonant-skill archive <id> [--reason X] # curator-style archival
    resonant-skill curate [--dry-run]        # run a curator pass now

The `promote` and `demote` subcommands (user-global elevation) ship
in v0.6.1a3.

The CLI is a THIN wrapper around the existing skills.py / skill_curator.py
public API. No orchestration logic lives here — it's pure surface.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Optional

from .skill_curator import run_curation
from .skills import (
    DEFAULT_SCOPE,
    SKILL_SCOPES,
    Skill,
    archive_skill,
    list_skills,
    list_skills_filtered,
    load_skill,
    set_pinned,
    skill_dir,
)

logger = logging.getLogger(__name__)


# ── Helpers ────────────────────────────────────────────────────────────


def _resolve_skill(
    skill_id: str,
    *,
    project_path: Optional[str] = None,
) -> Optional[tuple[Skill, str]]:
    """Find a skill across scopes. Returns (skill, scope) or None.

    Order: project (if given) → global → stack. First hit wins.
    The caller usually wants to know WHICH scope the skill came
    from for path-printing.
    """
    candidates = []
    if project_path:
        candidates.append(("project", {"project_path": project_path}))
    candidates.append(("global", {}))
    # stack scope needs a stack_sig — out of scope for v0.6.1; skip.
    for scope, kwargs in candidates:
        skill = load_skill(skill_id, scope=scope, **kwargs)
        if skill is not None:
            return skill, scope
    return None


def _format_skill_row(skill: Skill, *, scope_label: str = "") -> str:
    """One-line summary for `list` output.

    ASCII-only on purpose: Windows cp1252 console can't print emoji
    via plain stdout. The format_skills_for_prompt path in
    skill_loader.py keeps 📌 because that prose goes into the model's
    prompt (UTF-8 always). CLI output is for humans on whatever
    terminal they have."""
    pin = "[PIN]" if skill.pinned else "     "
    provenance = f"[{skill.created_by:7}]"
    scope = f"({skill.scope})" if not scope_label else f"({scope_label})"
    desc = skill.description[:80]
    if len(skill.description) > 80:
        desc = desc[:79] + "..."
    return f"{pin} {provenance} {skill.id:40} {scope:10} {desc}"


# ── Subcommand handlers ────────────────────────────────────────────────


def cmd_list(args: argparse.Namespace) -> int:
    """List skills, with optional filters."""
    scope = args.scope if args.scope != "all" else None
    project_path = args.project_path

    skills = list_skills_filtered(
        scope=scope,
        project_path=project_path,
        created_by=args.created_by,
        pinned=(True if args.pinned else None),
        include_deprecated=args.include_deprecated,
    )

    if args.json:
        print(json.dumps(
            [s.to_dict() for s in skills],
            indent=2, ensure_ascii=False,
        ))
        return 0

    if not skills:
        print("No skills found matching the filters.")
        return 0

    print(f"Found {len(skills)} skill(s):")
    print()
    for s in sorted(skills, key=lambda s: (s.scope, s.id)):
        print(_format_skill_row(s))
    return 0


def cmd_view(args: argparse.Namespace) -> int:
    """Print a skill's procedure body or full JSON."""
    found = _resolve_skill(args.skill_id, project_path=args.project_path)
    if found is None:
        print(f"Skill not found: {args.skill_id}", file=sys.stderr)
        return 1

    skill, scope = found
    if args.json:
        out = skill.to_dict()
        out["_resolved_scope"] = scope
        print(json.dumps(out, indent=2, ensure_ascii=False))
        return 0

    # Human-readable: header + procedure.md body if present.
    print(f"# {skill.name}")
    print()
    print(f"**ID:** `{skill.id}` * **Scope:** {scope} * **Provenance:** {skill.created_by}")
    if skill.pinned:
        print("**[PINNED]**")
    print(f"**Description:** {skill.description}")
    print(f"**Version:** {skill.version}  ·  **Uses:** {skill.success_count} ok / {skill.fail_count} fail")
    print()
    target = skill_dir(
        skill.id, scope=scope,
        project_path=args.project_path if scope == "project" else None,
    )
    procedure = target / "procedure.md"
    if procedure.exists():
        print("---")
        print()
        print(procedure.read_text(encoding="utf-8"))
    else:
        print("_(no procedure.md sidecar — skill metadata only)_")
    return 0


def cmd_pin(args: argparse.Namespace) -> int:
    """Mark a skill as pinned (curator-exempt)."""
    return _set_pinned_helper(args, pinned=True)


def cmd_unpin(args: argparse.Namespace) -> int:
    """Mark a skill as unpinned."""
    return _set_pinned_helper(args, pinned=False)


def _set_pinned_helper(args: argparse.Namespace, *, pinned: bool) -> int:
    found = _resolve_skill(args.skill_id, project_path=args.project_path)
    if found is None:
        print(f"Skill not found: {args.skill_id}", file=sys.stderr)
        return 1
    skill, scope = found
    project_kw = (
        args.project_path if scope == "project" else None
    )
    updated = set_pinned(
        skill.id, pinned=pinned,
        scope=scope, project_path=project_kw,
    )
    state = "pinned" if pinned else "unpinned"
    print(f"{updated.id} ({scope}) is now {state}.")
    return 0


def cmd_archive(args: argparse.Namespace) -> int:
    """Manually archive a skill. Refused on bundled / user / pinned."""
    found = _resolve_skill(args.skill_id, project_path=args.project_path)
    if found is None:
        print(f"Skill not found: {args.skill_id}", file=sys.stderr)
        return 1
    skill, scope = found

    project_kw = args.project_path if scope == "project" else None
    dest = archive_skill(
        skill,
        project_path=project_kw,
        reason=args.reason or "manually archived via CLI",
    )
    if dest is None:
        # archive_skill prints a warning via logger; surface a clean
        # CLI message too.
        print(
            f"Refused to archive `{skill.id}`: skill must be "
            f"created_by=agent and not pinned. Got "
            f"created_by={skill.created_by!r}, pinned={skill.pinned}.",
            file=sys.stderr,
        )
        return 2
    print(f"Archived `{skill.id}` ({scope}) to {dest}")
    return 0


def cmd_curate(args: argparse.Namespace) -> int:
    """Run a curator pass for the project NOW (bypasses rate limit)."""
    if not args.project_path:
        print(
            "curate requires --project-path (or pass it as a positional "
            "argument)",
            file=sys.stderr,
        )
        return 1
    report = run_curation(args.project_path, dry_run=args.dry_run)
    archived = report.archived()
    retained = report.retained()
    print(f"Curator pass complete: reviewed {report.skills_reviewed} skill(s).")
    print(f"  Archived: {len(archived)}")
    for a in archived:
        print(f"    - {a.skill_id} — {a.reason}")
    print(f"  Retained: {len(retained)}")
    if args.dry_run:
        print("(dry-run — no changes written to disk)")
    elif report.state_dir:
        print(f"Report: {report.state_dir}")
    return 0


# ── Argparse builder ───────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="resonant-skill",
        description=(
            "Manage the resonant-client skill library. Skills are reusable "
            "patterns extracted from successful autonomous-mission iters. "
            "See docs/skills.md for the full lifecycle."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # list
    p_list = subparsers.add_parser("list", help="List skills with optional filters")
    p_list.add_argument(
        "--scope", choices=list(SKILL_SCOPES) + ["all"], default="all",
        help="Restrict to one scope (default: all)",
    )
    p_list.add_argument("--project-path", default=None,
                        help="Required for --scope project")
    p_list.add_argument(
        "--created-by", choices=["bundled", "agent", "user"], default=None,
        help="Filter by provenance",
    )
    p_list.add_argument(
        "--pinned", action="store_true",
        help="Only show pinned skills",
    )
    p_list.add_argument(
        "--include-deprecated", action="store_true",
        help="Include skills that match auto-deprecation thresholds",
    )
    p_list.add_argument("--json", action="store_true", help="Machine-readable output")

    # view
    p_view = subparsers.add_parser("view", help="Show a skill's body")
    p_view.add_argument("skill_id")
    p_view.add_argument("--project-path", default=None)
    p_view.add_argument("--json", action="store_true",
                        help="Print full skill.json instead of procedure.md body")

    # pin
    p_pin = subparsers.add_parser("pin", help="Mark a skill pinned (curator-exempt)")
    p_pin.add_argument("skill_id")
    p_pin.add_argument("--project-path", default=None)

    # unpin
    p_unpin = subparsers.add_parser("unpin", help="Mark a skill unpinned")
    p_unpin.add_argument("skill_id")
    p_unpin.add_argument("--project-path", default=None)

    # archive
    p_archive = subparsers.add_parser(
        "archive",
        help="Archive a skill (curator-style; bundled/user/pinned refused)",
    )
    p_archive.add_argument("skill_id")
    p_archive.add_argument("--project-path", default=None)
    p_archive.add_argument("--reason", default="")

    # curate
    p_curate = subparsers.add_parser("curate", help="Run a curator pass now")
    p_curate.add_argument(
        "project_path", nargs="?", default=None,
        help="Project to curate (or use --project-path)",
    )
    p_curate.add_argument("--project-path", dest="project_path_flag", default=None)
    p_curate.add_argument("--dry-run", action="store_true")

    return parser


def _ensure_utf8_stdout() -> None:
    """Force UTF-8 on stdout/stderr so Unicode in skill descriptions
    (em-dashes, arrows, etc.) doesn't crash on Windows cp1252 consoles.

    `errors="replace"` is the safety net — if the underlying terminal
    can't actually display a glyph, it gets `?` instead of a crash.
    Modern Windows Terminal handles UTF-8 natively; only the legacy
    `cmd.exe` cp1252 path needed this dance.
    """
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if stream is None or not hasattr(stream, "reconfigure"):
            continue
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            # Pytest captures use io.StringIO which doesn't have
            # reconfigure; that's fine, those don't have encoding
            # restrictions anyway.
            pass


def main(argv: Optional[list[str]] = None) -> int:
    _ensure_utf8_stdout()
    parser = build_parser()
    args = parser.parse_args(argv)

    # `curate` accepts project_path as positional OR --flag; reconcile.
    if args.command == "curate":
        args.project_path = args.project_path or args.project_path_flag

    try:
        if args.command == "list":
            return cmd_list(args)
        if args.command == "view":
            return cmd_view(args)
        if args.command == "pin":
            return cmd_pin(args)
        if args.command == "unpin":
            return cmd_unpin(args)
        if args.command == "archive":
            return cmd_archive(args)
        if args.command == "curate":
            return cmd_curate(args)
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        logger.exception("resonant-skill subcommand crashed")
        return 1

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
