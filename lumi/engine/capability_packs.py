"""Unified installable packs for agents, skills, hooks, MCP, recipes, and UI.

Trust model
-----------
A pack's manifest describes what the pack contains. It never decides whether
the pack runs: ``trust``, ``enabled`` and ``sha256`` written in a manifest are
ignored, because a repository can put anything in its own files. Trust and
enablement come only from user settings (the ``plugins`` section):

* A *location approval*, which the Settings UI writes when the user approves a
  pack after reviewing it::

      plugins[<id>]["approvals"][<pack directory>] = {"sha256": ..., "enabled": true}

  It covers the pack at that directory only, so an approval never follows a
  copied pack into another repository. This is the only way to trust a pack
  inside the open project.
* *Pinned trust by id*, for packs outside the project (``~/.lumi/packs``
  or a configured ``path``)::

      plugins[<id>] = {"trust": "local", "enabled": true, "sha256": ...}

Both pin :meth:`CapabilityPackManager._digest`: a digest of every file in the
pack directory plus the repository files that the pack's hook and MCP commands
name. Any change to those files withdraws trust until the pack is approved
again. Trusted packs are re-verified before their hooks run and before they
contribute skills, agents or MCP servers.
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import os
import re
import shlex
import threading
import time
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Iterable

from .agents import AgentType
from .hooks import HookDefinition
from ..paths import project_dirs, state_home


logger = logging.getLogger(__name__)

PACK_MANIFEST = "lumi-pack.json"
# Packs written before the Lumi rebrand keep their original manifest name.
LEGACY_PACK_MANIFEST = "resonant-pack.json"
TRUST_LEVELS = frozenset({"local", "trusted", "signed"})
# Bounds on what one pack may ask us to hash on every verification. A pack
# beyond them cannot be verified and therefore cannot be trusted.
MAX_PACK_FILES = 4000
MAX_PACK_BYTES = 64 * 1024 * 1024
_DIGEST_VERSION = b"lumi-pack-digest-v1\n"
# Version-control metadata is not executed by packs, and tools rewrite it on
# ordinary reads (`git status` refreshes the index), which would otherwise
# withdraw approval for no change to the pack's content.
_UNHASHED_NAMES = frozenset({".git"})
# A file hash is reused only while size, mtime, inode and ctime are unchanged
# and the file was already older than this when hashed. Newer files are always
# re-read, so two edits within one filesystem timestamp tick cannot hide.
_STABLE_MTIME_NS = 2_000_000_000


class CapabilityPackError(RuntimeError):
    pass


@dataclass(slots=True)
class CapabilityPack:
    id: str
    name: str
    version: str
    description: str
    path: str
    enabled: bool
    trusted: bool
    digest: str
    permissions: list[str] = field(default_factory=list)
    agents: list[str] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)
    hooks: list[dict[str, Any]] = field(default_factory=list)
    mcp_servers: dict[str, dict[str, Any]] = field(default_factory=dict)
    commands: list[dict[str, Any]] = field(default_factory=list)
    recipes: list[dict[str, Any]] = field(default_factory=list)
    ui_panels: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    # "project" for packs inside the open project, otherwise "user".
    scope: str = "user"
    # approved | disabled | needs_approval | changed | unverifiable
    status: str = "needs_approval"
    # Why the pack cannot be verified, when status is "unverifiable".
    problem: str = ""
    # Repository files outside the pack that its commands run; in the digest.
    pinned_files: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _manifest_path(directory: Path) -> Path | None:
    """The pack manifest in `directory`, under its current or pre-rebrand name."""
    for name in (PACK_MANIFEST, LEGACY_PACK_MANIFEST):
        candidate = directory / name
        if candidate.is_file():
            return candidate
    return None


def pack_location_key(path: str | Path) -> str:
    """The settings key that binds an approval to one pack directory."""
    return os.path.normcase(str(Path(path).expanduser().resolve()))


def approve_pack(
    plugins: dict | None,
    pack: CapabilityPack,
    *,
    reviewed_digest: str,
    enabled: bool = True,
) -> dict:
    """Return a copy of ``plugins`` with ``pack`` approved at its location.

    ``reviewed_digest`` is the digest of the content the user reviewed. The
    approval is refused when the pack cannot be verified or no longer has that
    digest, so a pack edited between review and approval is not approved.
    """
    if not pack.digest:
        raise CapabilityPackError(
            f"Capability pack {pack.id} cannot be verified: {pack.problem or 'no digest'}"
        )
    if not reviewed_digest or reviewed_digest != pack.digest:
        raise CapabilityPackError(
            f"Capability pack {pack.id} changed after it was reviewed. "
            "Review it again before approving it."
        )
    updated = copy.deepcopy(plugins) if isinstance(plugins, dict) else {}
    entry = updated.get(pack.id)
    entry = dict(entry) if isinstance(entry, dict) else {}
    approvals = entry.get("approvals")
    approvals = dict(approvals) if isinstance(approvals, dict) else {}
    approvals[pack_location_key(pack.path)] = {
        "sha256": pack.digest,
        "enabled": bool(enabled),
        "approved_at": int(time.time()),
        "name": pack.name,
        "version": pack.version,
    }
    entry["approvals"] = approvals
    updated[pack.id] = entry
    return updated


def revoke_pack_approval(plugins: dict | None, pack: CapabilityPack) -> dict:
    """Return a copy of ``plugins`` that no longer trusts ``pack``."""
    updated = copy.deepcopy(plugins) if isinstance(plugins, dict) else {}
    entry = updated.get(pack.id)
    if not isinstance(entry, dict):
        return updated
    entry = dict(entry)
    approvals = entry.get("approvals")
    if isinstance(approvals, dict):
        approvals = dict(approvals)
        approvals.pop(pack_location_key(pack.path), None)
        if approvals:
            entry["approvals"] = approvals
        else:
            entry.pop("approvals", None)
    if pack.scope == "user":
        for key in ("trust", "sha256", "enabled"):
            entry.pop(key, None)
    if entry:
        updated[pack.id] = entry
    else:
        updated.pop(pack.id, None)
    return updated


# ── Content digest ─────────────────────────────────────────────────────


_FILE_DIGESTS: dict[str, tuple[tuple[int, int, int, int], str]] = {}
_FILE_DIGESTS_LOCK = threading.Lock()


def _is_link(path: str | Path) -> bool:
    path = str(path)
    if os.path.islink(path):
        return True
    isjunction = getattr(os.path, "isjunction", None)
    return bool(isjunction and isjunction(path))


def _file_sha256(path: Path) -> str:
    stat = path.stat()
    signature = (stat.st_size, stat.st_mtime_ns, int(getattr(stat, "st_ino", 0) or 0), stat.st_ctime_ns)
    key = str(path)
    with _FILE_DIGESTS_LOCK:
        cached = _FILE_DIGESTS.get(key)
    if cached and cached[0] == signature:
        return cached[1]
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    value = digest.hexdigest()
    if time.time_ns() - stat.st_mtime_ns > _STABLE_MTIME_NS:
        with _FILE_DIGESTS_LOCK:
            _FILE_DIGESTS[key] = (signature, value)
    return value


def _pack_files(root: Path) -> list[tuple[str, Path]]:
    """Every file under ``root``, sorted by relative path. Links are refused.

    A link could point outside the pack, where changes would not alter the
    digest, so a pack that contains one cannot be verified.
    """
    files: list[tuple[str, Path]] = []
    total = 0
    for current, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(name for name in dirnames if name not in _UNHASHED_NAMES)
        for name in [*dirnames, *filenames]:
            full = Path(current) / name
            if name not in _UNHASHED_NAMES and _is_link(full):
                raise CapabilityPackError(f"it contains a link: {full.relative_to(root).as_posix()}")
        for name in sorted(filenames):
            if name in _UNHASHED_NAMES:
                continue
            full = Path(current) / name
            files.append((full.relative_to(root).as_posix(), full))
            total += full.stat().st_size
            if len(files) > MAX_PACK_FILES:
                raise CapabilityPackError(f"it has more than {MAX_PACK_FILES} files")
            if total > MAX_PACK_BYTES:
                raise CapabilityPackError(f"it is larger than {MAX_PACK_BYTES // (1024 * 1024)} MB")
    files.sort(key=lambda item: item[0])
    return files


def _command_tokens(command: str) -> list[str]:
    try:
        tokens = shlex.split(command, posix=(os.name != "nt"))
    except ValueError:
        tokens = command.split()
    return [token.strip().strip("\"'") for token in tokens]


# ── Manifest parsing helpers ───────────────────────────────────────────


def _strings(value: Any) -> list[str]:
    return [str(item) for item in value if isinstance(item, (str, int, float))] if isinstance(value, list) else []


def _dicts(value: Any) -> list[dict[str, Any]]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _named_dicts(value: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(value, dict):
        return {}
    return {str(key): item for key, item in value.items() if isinstance(item, dict)}


class CapabilityPackManager:
    """Discover, verify and activate local or repository-scoped capability packs."""

    def __init__(
        self,
        project_path: str | Path,
        *,
        configured: dict[str, Any] | None = None,
        roots: Iterable[str | Path] = (),
    ):
        self.project_path = Path(project_path).expanduser().resolve()
        self.configured = configured if isinstance(configured, dict) else {}
        default_roots = [
            *(folder / "packs" for folder in project_dirs(self.project_path)),
            state_home() / "packs",
        ]
        self.roots = [Path(root).expanduser() for root in (*default_roots, *roots)]
        for value in self.configured.values():
            if isinstance(value, dict) and (value.get("path") or value.get("directory")):
                self.roots.append(Path(value.get("path") or value.get("directory")).expanduser())
        self._packs: dict[str, CapabilityPack] = {}
        self._manifests: dict[str, dict[str, Any]] = {}
        self._reported_changes: set[str] = set()

    def discover(self) -> list[CapabilityPack]:
        packs: dict[str, CapabilityPack] = {}
        manifests: dict[str, dict[str, Any]] = {}
        candidates: list[Path] = []
        for root in self.roots:
            if _manifest_path(root) is not None:
                candidates.append(root)
            elif root.is_dir():
                candidates.extend(
                    child for child in sorted(root.iterdir())
                    if child.is_dir() and _manifest_path(child) is not None
                )
        for directory in candidates:
            try:
                pack, data = self._load(directory)
            except (CapabilityPackError, OSError):
                logger.debug("Skipping unreadable capability pack %s", directory, exc_info=True)
                continue
            packs[pack.id] = pack
            manifests[pack.id] = data
        self._packs = packs
        self._manifests = manifests
        self._reported_changes.clear()
        return sorted(packs.values(), key=lambda pack: (pack.name.casefold(), pack.version))

    def get(self, pack_id: str) -> CapabilityPack | None:
        if not self._packs:
            self.discover()
        return self._packs.get(pack_id)

    def active(self) -> list[CapabilityPack]:
        """Approved, enabled packs whose files still match their approval."""
        if not self._packs:
            self.discover()
        return [
            pack for pack in self._packs.values()
            if pack.enabled and pack.trusted and self.verify(pack)
        ]

    def pending(self) -> list[CapabilityPack]:
        """Packs in the open project that are waiting for the user's decision.

        An approved pack whose files changed since discovery is reported as
        "changed" here too, so the user learns why it stopped contributing.
        """
        if not self._packs:
            self.discover()
        waiting = []
        for pack in self._packs.values():
            if pack.scope != "project":
                continue
            if pack.status in {"needs_approval", "changed", "unverifiable"}:
                waiting.append(pack)
            elif pack.trusted and not self.verify(pack):
                waiting.append(replace(pack, trusted=False, enabled=False, status="changed"))
        return sorted(waiting, key=lambda pack: (pack.name.casefold(), pack.id))

    def verify(self, pack: CapabilityPack) -> bool:
        """True while ``pack`` on disk still has the digest it was trusted with."""
        data = self._manifests.get(pack.id)
        if data is None or not pack.digest:
            return False
        try:
            digest, _ = self._digest(Path(pack.path), data, pack.scope)
        except (CapabilityPackError, OSError):
            digest = ""
        if digest == pack.digest:
            return True
        if pack.id not in self._reported_changes:
            self._reported_changes.add(pack.id)
            logger.warning(
                "Capability pack %s changed after approval; it stays off until approved again",
                pack.id,
            )
        return False

    def get_agent_type(self, name: str) -> AgentType | None:
        for pack in self.active():
            root = Path(pack.path).resolve()
            for relative in pack.agents:
                path = (root / relative).resolve()
                if not path.is_file() or root not in path.parents:
                    continue
                agent = self._parse_agent(path)
                if agent and agent.name == name:
                    return agent
        return None

    def hook_definitions(self) -> list[HookDefinition]:
        definitions = []
        for pack in self.active():
            for data in pack.hooks:
                try:
                    definition = HookDefinition.from_dict(data)
                except (TypeError, ValueError):
                    continue
                # Re-checked immediately before each run of the command.
                definition.precondition = lambda pack=pack: self.verify(pack)
                definitions.append(definition)
        return definitions

    def mcp_servers(self) -> dict[str, dict[str, Any]]:
        servers: dict[str, dict[str, Any]] = {}
        for pack in self.active():
            for name, config in pack.mcp_servers.items():
                servers[f"{pack.id}-{name}"] = dict(config)
        return servers

    def context_catalog(self) -> dict[str, Any]:
        active = self.active()
        return {
            "packs": [pack.to_dict() for pack in active],
            "commands": [command for pack in active for command in pack.commands],
            "recipes": [recipe for pack in active for recipe in pack.recipes],
            "ui_panels": [panel for pack in active for panel in pack.ui_panels],
        }

    def skill_context(self, query: str, *, max_skills: int = 6, max_tokens: int = 500) -> str:
        """Render matching trusted pack skills without copying them globally."""
        stop = {"the", "and", "for", "with", "this", "that", "use", "create", "build", "please", "can", "you", "from", "new"}
        terms = set(re.findall(r"[A-Za-z0-9_]{3,}", query.casefold())) - stop
        ranked: list[tuple[int, CapabilityPack, Path, str]] = []
        for pack in self.active():
            root = Path(pack.path).resolve()
            for relative in pack.skills:
                path = (root / relative).resolve()
                if not path.is_file() or root not in path.parents:
                    continue
                text = path.read_text(encoding="utf-8", errors="replace")
                haystack = f"{path.stem} {text[:4000]}".casefold()
                score = len(terms & set(re.findall(r"[A-Za-z0-9_]{3,}", haystack)))
                if score:
                    ranked.append((score, pack, path, text))
        ranked.sort(key=lambda item: (-item[0], item[1].id, item[2].name))
        if not ranked:
            return ""
        blocks = ["\n## Trusted capability-pack skills\n"]
        for _, pack, path, body in ranked[:max_skills]:
            description = next((line.partition(':')[2].strip() for line in body.splitlines() if line.lower().startswith('description:')), pack.description)
            if not description:
                description = next((line.strip('# ').strip() for line in body.splitlines() if line.strip() and line.strip() != '---'), path.stem)
            handle = f"pack:{pack.id}:{path.relative_to(Path(pack.path)).as_posix()}"
            row = f"### {pack.name}: {path.stem}\n{description[:300]}\nSource: trusted pack {pack.id}. Load with skill_view skill_id={handle}\n"
            if len('\n'.join(blocks)) + len(row) > max_tokens * 4:
                break
            blocks.append(row)
        return "\n".join(blocks)

    def read_skill(self, handle: str) -> str:
        _, pack_id, relative = handle.split(':', 2)
        for pack in self.active():
            if pack.id != pack_id or relative not in pack.skills:
                continue
            root = Path(pack.path).resolve()
            path = (root / relative).resolve()
            if root not in path.parents:
                break
            return path.read_text(encoding='utf-8')[:24000]
        raise ValueError('Unknown or untrusted pack skill')

    # ── Loading and trust ──────────────────────────────────────────────

    def _load(self, directory: Path) -> tuple[CapabilityPack, dict[str, Any]]:
        directory = directory.resolve()
        manifest_path = _manifest_path(directory) or directory / PACK_MANIFEST
        try:
            data = json.loads(manifest_path.read_bytes().decode("utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise CapabilityPackError(f"Invalid pack manifest {manifest_path}: {exc}") from exc
        if not isinstance(data, dict):
            raise CapabilityPackError(f"Invalid pack manifest {manifest_path}: not an object")
        pack_id = str(data.get("id") or directory.name).strip()
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,80}", pack_id):
            raise CapabilityPackError(f"Invalid pack id: {pack_id}")
        scope = "project" if self._inside_project(directory) else "user"
        problem = ""
        try:
            digest, pinned_files = self._digest(directory, data, scope)
        except (CapabilityPackError, OSError) as exc:
            digest, pinned_files = "", []
            problem = f"The pack cannot be verified because {exc}."
        configured = self.configured.get(pack_id)
        from ..policy import current as current_policy

        org_policy = current_policy()
        if not problem and org_policy and not org_policy.pack_allowed(pack_id):
            # Blocked whatever the user approved; the approval itself is kept.
            problem = f"{org_policy.organization}'s policy doesn't allow this pack."
        trusted, enabled, status = self._trust(
            configured if isinstance(configured, dict) else {},
            directory, digest, scope, problem,
        )
        metadata = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
        pack = CapabilityPack(
            id=pack_id,
            name=str(data.get("name") or pack_id),
            version=str(data.get("version") or "0.0.0"),
            description=str(data.get("description") or ""),
            path=str(directory),
            enabled=enabled,
            trusted=trusted,
            digest=digest,
            permissions=_strings(data.get("permissions")),
            agents=_strings(data.get("agents")),
            skills=_strings(data.get("skills")),
            hooks=_dicts(data.get("hooks")),
            mcp_servers=_named_dicts(data.get("mcp_servers")),
            commands=_dicts(data.get("commands")),
            recipes=_dicts(data.get("recipes")),
            ui_panels=_dicts(data.get("ui_panels")),
            metadata={**metadata, "manifest": str(manifest_path)},
            scope=scope,
            status=status,
            problem=problem,
            pinned_files=pinned_files,
        )
        return pack, data

    def _inside_project(self, directory: Path) -> bool:
        return directory == self.project_path or self.project_path in directory.parents

    @staticmethod
    def _trust(
        configured: dict[str, Any],
        directory: Path,
        digest: str,
        scope: str,
        problem: str,
    ) -> tuple[bool, bool, str]:
        """``(trusted, enabled, status)`` from user settings alone."""
        if problem:
            return False, False, "unverifiable"
        approvals = configured.get("approvals")
        approval = approvals.get(pack_location_key(directory)) if isinstance(approvals, dict) else None
        if isinstance(approval, dict):
            if str(approval.get("sha256") or "") != digest:
                return False, False, "changed"
            enabled = approval.get("enabled", True) is True and configured.get("enabled") is not False
            return True, enabled, "approved" if enabled else "disabled"
        # Pinned trust by id never covers a pack inside the project: a hostile
        # repository could ship a byte-identical copy of a trusted pack whose
        # hooks run *repository* scripts by relative path.
        if scope == "user" and str(configured.get("trust") or "").lower() in TRUST_LEVELS:
            pinned = str(configured.get("sha256") or "")
            if not pinned:
                return False, False, "needs_approval"
            if pinned != digest:
                return False, False, "changed"
            enabled = configured.get("enabled") is True
            return True, enabled, "approved" if enabled else "disabled"
        return False, False, "needs_approval"

    def _digest(self, directory: Path, data: dict[str, Any], scope: str) -> tuple[str, list[str]]:
        """Digest of the pack's files and the repository files its commands run."""
        digest = hashlib.sha256(_DIGEST_VERSION)
        for relative, path in _pack_files(directory):
            digest.update(f"pack\0{relative}\0{_file_sha256(path)}\n".encode("utf-8"))
        pinned: list[str] = []
        if scope == "project":
            for relative, path in self._referenced_project_files(directory, data):
                digest.update(f"project\0{relative}\0{_file_sha256(path)}\n".encode("utf-8"))
                pinned.append(relative)
        return digest.hexdigest(), pinned

    def _referenced_project_files(self, directory: Path, data: dict[str, Any]) -> list[tuple[str, Path]]:
        """Repository files outside the pack that its hook and MCP commands name.

        Hooks and MCP servers run with the project as their working directory,
        so ``python scripts/check.py`` executes repository code that the pack's
        own files do not contain. Pinning the files a command names means that
        editing them needs a new approval, like editing the pack. This reads
        command-line tokens only: files that a named script runs in turn are
        not followed, and a review should still read what the commands do.
        """
        tokens: list[str] = []
        for hook in _dicts(data.get("hooks")):
            tokens.extend(_command_tokens(str(hook.get("command") or "")))
        for server in _named_dicts(data.get("mcp_servers")).values():
            tokens.extend(_command_tokens(str(server.get("command") or "")))
            tokens.extend(token.strip().strip("\"'") for token in _strings(server.get("args")))
        project = self.project_path
        found: dict[str, Path] = {}
        for token in tokens:
            if not token or token.startswith("-") or len(token) > 1024 or "\0" in token:
                continue
            named = Path(token)
            if not named.is_absolute():
                named = project / named
            lexical = Path(os.path.normpath(named))
            if lexical != project and project not in lexical.parents:
                continue  # interpreters, system tools and other non-repository paths
            try:
                resolved = lexical.resolve()
            except (OSError, RuntimeError, ValueError):
                continue
            if resolved != project and project not in resolved.parents:
                raise CapabilityPackError(
                    f"its command names {lexical.relative_to(project).as_posix()}, "
                    "which links outside the project"
                )
            if not resolved.is_file() or resolved == directory or directory in resolved.parents:
                continue  # not a file, or already covered as part of the pack
            found[resolved.relative_to(project).as_posix()] = resolved
        return sorted(found.items())

    @staticmethod
    def _parse_agent(path: Path) -> AgentType | None:
        text = path.read_text(encoding="utf-8", errors="replace")
        if not text.startswith("---") or text.count("---") < 2:
            return None
        _, frontmatter, body = text.split("---", 2)
        values: dict[str, Any] = {}
        for line in frontmatter.splitlines():
            if ":" not in line:
                continue
            key, value = line.split(":", 1)
            values[key.strip()] = value.strip().strip("\"'")
        name = str(values.get("name") or path.stem).strip()
        if not name:
            return None
        tools = [
            value.strip() for value in str(values.get("tools") or "").strip("[]").split(",")
            if value.strip()
        ]
        try:
            max_steps = int(values.get("max_steps") or 0) or None
        except ValueError:
            max_steps = None
        return AgentType(
            name=name,
            description=str(values.get("description") or name),
            allowed_tools=tools,
            system_prompt=body.strip(),
            model=str(values.get("model") or "") or None,
            model_role=str(values.get("model_role") or "primary"),
            default_isolation=str(values.get("isolation") or "shared"),
            max_steps=max_steps,
        )
