"""
v0.3.4 — diagnostics bundle (Help → Save diagnostics).

Walks the user's `~/.resonant/` data dir, redacts API keys / auth
headers / common secret patterns, and zips everything into a single
file the user can attach to a GitHub issue. Triggered from the WS
command `save_diagnostics` (see `app.py`) and surfaced in the UI.

Why bundle instead of streaming telemetry: phase 1 of the log-shipping
plan (see ../docs/long-running-agents.md and the v0.3.3 conversation)
keeps everything user-controlled. Nothing leaves the machine without
an explicit drag-and-drop into a GitHub issue. Phase 2 (opt-in Sentry
or similar) can come later once we know which error categories
warrant always-on collection.
"""
from __future__ import annotations

import io
import json
import logging
import os
import platform
import re
import sys
import time
import zipfile
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


# ── Redaction patterns ────────────────────────────────────────────────────
#
# Defense-in-depth: we strip both raw secrets that look like keys (sk-…
# tokens, generic 32+-char hex / b64 blobs near a "key" word) AND keys
# inside JSON / env-style assignments. The redaction is overzealous on
# purpose — false positives just produce a slightly less informative
# log line, false negatives leak credentials.

_SECRET_PATTERNS: tuple[tuple[re.Pattern, str], ...] = (
    # OpenAI / Anthropic / GitHub tokens by prefix.
    (re.compile(r"\b(sk-[a-zA-Z0-9_\-]{16,})\b"), r"sk-[REDACTED]"),
    (re.compile(r"\b(ghp_[a-zA-Z0-9]{20,})\b"), r"ghp_[REDACTED]"),
    (re.compile(r"\b(github_pat_[a-zA-Z0-9_]{40,})\b"), r"github_pat_[REDACTED]"),
    # Bearer / api-key / authorization HTTP headers.
    (re.compile(r"(?i)(authorization\s*[:=]\s*bearer\s+)[a-zA-Z0-9._\-]{8,}"), r"\1[REDACTED]"),
    (re.compile(r"(?i)(x[-_]?api[-_]?key\s*[:=]\s*)[a-zA-Z0-9._\-]{8,}"), r"\1[REDACTED]"),
    (re.compile(r"(?i)(api[-_]?key\s*[:=]\s*)[a-zA-Z0-9._\-]{8,}"), r"\1[REDACTED]"),
    # JSON-looking secret fields. Triple-quoted regex skipped — we keep
    # these single-line because the JSONL/JSON files we redact are
    # always one-record-per-line.
    (re.compile(r'(?i)("(?:api_?key|token|password|secret|access_?key|client_?secret)"\s*:\s*)"[^"]{4,}"'),
     r'\1"[REDACTED]"'),
    # Env-style assignments (often appear in startup logs from os.environ dumps).
    (re.compile(r"(?i)((?:OPENAI|ANTHROPIC|GROQ|GEMINI|CEREBRAS|TOGETHER|MISTRAL)_API_KEY\s*=\s*)\S+"),
     r"\1[REDACTED]"),
    (re.compile(r"(?i)(GITHUB_TOKEN\s*=\s*)\S+"), r"\1[REDACTED]"),
)


def redact(text: str) -> str:
    """Strip API keys, tokens, and other obvious secrets from `text`.

    Each line is run through every pattern. Order matters slightly — we
    catch the prefixed forms (sk-…, ghp_…) before the generic
    JSON / header captures so the more-specific pattern wins.
    """
    if not text:
        return text
    for pattern, replacement in _SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def _read_redacted(path: Path, *, max_bytes: int = 2 * 1024 * 1024) -> bytes:
    """Read up to `max_bytes` bytes from `path`, decode lossily, run
    through `redact`, return as UTF-8 bytes. Truncation is from the
    HEAD (we want the most-recent log lines, which sit at the tail).
    """
    try:
        size = path.stat().st_size
    except OSError:
        return b""
    offset = max(0, size - max_bytes)
    try:
        with open(path, "rb") as f:
            f.seek(offset)
            raw = f.read()
    except OSError as exc:
        logger.warning("diagnostics: failed to read %s: %s", path, exc)
        return b""
    text = raw.decode("utf-8", errors="replace")
    if offset > 0:
        text = f"[…truncated head: kept last {max_bytes:,} bytes…]\n" + text
    return redact(text).encode("utf-8")


# ── Bundle layout ────────────────────────────────────────────────────────
#
# A successful bundle looks like:
#
#   resonant-diagnostics-2026-05-01T223045.zip
#     ├── meta.txt                            ← version, platform, settings (redacted)
#     ├── logs/
#     │   ├── resonant-startup.log
#     │   └── 2026-05-01/<session_id>.jsonl   ← per-session event logs
#     └── intents/
#         └── <project_hash>/<intent_id>/audit.jsonl
#
# Limits:
# - Per-file 2 MB cap (head-truncated); user can override per request.
# - At most LATEST_N session JSONLs and LATEST_N intent audits.
# - Total uncompressed budget ~30 MB; compresses to ~2-5 MB typically.

LATEST_N_SESSIONS = 20
LATEST_N_INTENTS = 10
# v0.5.9a5 — per-intent iteration metadata files. Each iter records
# a small JSON snapshot; capping to the latest N keeps the bundle
# from blowing up on missions that ran 100+ iters. The most-recent
# 30 are usually where the failure is.
LATEST_N_ITERS_PER_INTENT = 30
MAX_BYTES_PER_FILE = 2 * 1024 * 1024  # 2 MB head-truncated


def _meta_text(version: str, resonant_dir: Path) -> str:
    """Tiny text manifest at the top of the bundle so a triager has
    everything they need without unzipping (version, platform, env).
    """
    settings_blob = ""
    settings_path = resonant_dir / "settings.json"
    if settings_path.is_file():
        try:
            settings_blob = settings_path.read_text(encoding="utf-8", errors="replace")
            settings_blob = redact(settings_blob)
        except OSError:
            settings_blob = "(settings.json unreadable)"

    lines = [
        f"# Resonant Client diagnostics",
        f"",
        f"version: {version}",
        f"python: {sys.version.split()[0]}",
        f"platform: {platform.platform()}",
        f"timestamp: {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}",
        f"",
        f"# settings.json (redacted)",
        f"",
        settings_blob or "(no settings.json on disk)",
    ]
    return "\n".join(lines)


def _collect_recent_session_logs(logs_dir: Path) -> list[Path]:
    """Walk `~/.resonant/logs/` and return up to `LATEST_N_SESSIONS`
    most-recent .jsonl session logs across all date subdirs.
    """
    if not logs_dir.is_dir():
        return []
    candidates: list[Path] = []
    for date_dir in sorted(logs_dir.iterdir(), reverse=True):
        if not date_dir.is_dir():
            continue
        for jsonl in date_dir.glob("*.jsonl"):
            try:
                size = jsonl.stat().st_size
            except OSError:
                continue
            if size == 0:
                continue  # empty placeholder logs aren't useful
            candidates.append(jsonl)
            if len(candidates) >= LATEST_N_SESSIONS:
                return candidates
    return candidates


def _collect_recent_intent_audits(projects_dir: Path) -> list[tuple[str, str, Path]]:
    """Return `(project_hash, intent_id, audit_path)` tuples for the
    `LATEST_N_INTENTS` most-recently-modified intent audit logs.
    """
    if not projects_dir.is_dir():
        return []
    audits: list[tuple[float, str, str, Path]] = []
    for project_dir in projects_dir.iterdir():
        if not project_dir.is_dir():
            continue
        intents_dir = project_dir / "intents"
        if not intents_dir.is_dir():
            continue
        for intent_dir in intents_dir.iterdir():
            audit = intent_dir / "audit.jsonl"
            if not audit.is_file():
                continue
            try:
                mtime = audit.stat().st_mtime
                if audit.stat().st_size == 0:
                    continue
            except OSError:
                continue
            audits.append((mtime, project_dir.name, intent_dir.name, audit))
    audits.sort(reverse=True, key=lambda t: t[0])
    return [(p, i, a) for _, p, i, a in audits[:LATEST_N_INTENTS]]


def _collect_iter_metadata(intent_dir: Path) -> list[Path]:
    """v0.5.9a5 — return per-iteration metadata files inside an
    intent's `iterations/` subdir. Each iter has a small JSON file
    (intent_id, iter_count, model, started/ended_at, verdict, etc.)
    that turns into a useful timeline when bundled.

    Capped at LATEST_N_ITERS_PER_INTENT to avoid runaway diagnostics
    bundle size on missions that ran for hundreds of iterations.
    """
    iters_dir = intent_dir / "iterations"
    if not iters_dir.is_dir():
        return []
    candidates: list[tuple[float, Path]] = []
    for child in iters_dir.iterdir():
        if not child.is_file():
            continue
        try:
            mtime = child.stat().st_mtime
            if child.stat().st_size == 0:
                continue
        except OSError:
            continue
        candidates.append((mtime, child))
    candidates.sort(reverse=True, key=lambda t: t[0])
    return [p for _, p in candidates[:LATEST_N_ITERS_PER_INTENT]]


def _build_mission_summary(
    intent_audits: list[tuple[str, str, Path]],
) -> str:
    """v0.5.9a5 — a small JSON manifest of the included intents, so
    a triager can see the shape of what's bundled without unzipping
    everything. Maps each intent's audit entry to size + mtime + iter
    count + project hash. Helps prioritize which intent to dig into
    first when several are included."""
    summary: list[dict] = []
    for project_hash, intent_id, audit in intent_audits:
        try:
            audit_size = audit.stat().st_size
            audit_mtime = audit.stat().st_mtime
        except OSError:
            audit_size = 0
            audit_mtime = 0.0
        iter_files = _collect_iter_metadata(audit.parent)
        # Best-effort: latest iter's mtime tells us when the daemon
        # last did productive work (vs audit which captures every
        # tool call, rotating constantly).
        latest_iter_mtime = 0.0
        for it in iter_files:
            try:
                m = it.stat().st_mtime
                if m > latest_iter_mtime:
                    latest_iter_mtime = m
            except OSError:
                pass
        summary.append({
            "project_hash": project_hash,
            "intent_id": intent_id,
            "audit_bytes": audit_size,
            "audit_mtime_iso": time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime(audit_mtime),
            ) if audit_mtime else "",
            "iter_files_included": len(iter_files),
            "latest_iter_mtime_iso": time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime(latest_iter_mtime),
            ) if latest_iter_mtime else "",
        })
    return json.dumps({
        "schema_version": 1,
        "intents": summary,
        "captured_at_iso": time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime(),
        ),
    }, indent=2)


def build_diagnostics_zip(
    resonant_dir: Path,
    output_dir: Path,
    *,
    version: str = "unknown",
) -> Path:
    """Create a redacted diagnostics ZIP under `output_dir` and return
    its path. Caller must have write access to `output_dir`.

    Raises OSError if the output dir doesn't exist and can't be created.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = time.strftime("%Y-%m-%dT%H%M%S", time.gmtime())
    zip_path = output_dir / f"resonant-diagnostics-{timestamp}.zip"

    logs_dir = resonant_dir / "logs"
    projects_dir = resonant_dir / "projects"

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        # Top-level manifest.
        zf.writestr("meta.txt", _meta_text(version, resonant_dir))

        # Startup log (rotates as the user runs; capture the latest).
        startup_log = logs_dir / "resonant-startup.log"
        if startup_log.is_file():
            zf.writestr("logs/resonant-startup.log",
                       _read_redacted(startup_log, max_bytes=MAX_BYTES_PER_FILE))

        # v0.5.9a5 — costs.json. Just dates + numbers (no secrets),
        # but still pass through redact() as defense-in-depth in case
        # a future schema adds string fields. Tells the triager
        # whether the user was hitting their daily budget alert when
        # the issue happened.
        costs_path = resonant_dir / "costs.json"
        if costs_path.is_file():
            try:
                zf.writestr(
                    "costs.json",
                    _read_redacted(costs_path, max_bytes=MAX_BYTES_PER_FILE),
                )
            except OSError:
                logger.debug("costs.json read failed", exc_info=True)

        # Per-session JSONL event logs.
        for session_log in _collect_recent_session_logs(logs_dir):
            try:
                # Reproduce the date-dir layout inside the zip so the
                # triager can correlate logs to the date they happened.
                rel = session_log.relative_to(logs_dir)
                arcname = f"logs/{rel.as_posix()}"
            except ValueError:
                arcname = f"logs/{session_log.name}"
            zf.writestr(arcname, _read_redacted(session_log, max_bytes=MAX_BYTES_PER_FILE))

        # Per-intent specialist audit trails — the gold standard for
        # debugging mission failures since they show every tool call.
        intent_audits = _collect_recent_intent_audits(projects_dir)
        for project_hash, intent_id, audit in intent_audits:
            arcname = f"intents/{project_hash}/{intent_id}/audit.jsonl"
            zf.writestr(arcname, _read_redacted(audit, max_bytes=MAX_BYTES_PER_FILE))
            # v0.5.9a5 — also include the per-iteration metadata
            # snapshots. Each iter's JSON has model, verdict,
            # duration, item_id; the timeline of these is much more
            # readable than parsing audit.jsonl by hand. Capped to
            # LATEST_N_ITERS_PER_INTENT to keep bundle size bounded.
            for iter_file in _collect_iter_metadata(audit.parent):
                rel_iter = iter_file.name
                iter_arc = f"intents/{project_hash}/{intent_id}/iterations/{rel_iter}"
                try:
                    zf.writestr(
                        iter_arc,
                        _read_redacted(iter_file, max_bytes=MAX_BYTES_PER_FILE),
                    )
                except OSError:
                    logger.debug(
                        "iter metadata read failed for %s", iter_file,
                        exc_info=True,
                    )

        # v0.5.9a5 — mission summary manifest at the root of the
        # bundle. Quick-reference index of what intents are included,
        # their byte size, and when they last touched disk. The
        # triager can read this without unzipping the audit trails.
        try:
            zf.writestr(
                "mission-summary.json",
                _build_mission_summary(intent_audits),
            )
        except Exception:
            logger.debug("mission-summary build failed", exc_info=True)

    return zip_path


def default_output_dir() -> Path:
    """Pick a sensible default output dir: ~/Downloads if it exists,
    else ~/Desktop, else home.
    """
    home = Path.home()
    for candidate in ("Downloads", "Desktop"):
        path = home / candidate
        if path.is_dir():
            return path
    return home
