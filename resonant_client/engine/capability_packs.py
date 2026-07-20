"""Unified installable packs for agents, skills, hooks, MCP, recipes, and UI."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .agents import AgentType
from .hooks import HookDefinition


PACK_MANIFEST = "resonant-pack.json"


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

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class CapabilityPackManager:
    """Discover and validate local or repository-scoped capability packs."""

    def __init__(
        self,
        project_path: str | Path,
        *,
        configured: dict[str, Any] | None = None,
        roots: Iterable[str | Path] = (),
    ):
        self.project_path = Path(project_path).expanduser().resolve()
        self.configured = configured or {}
        default_roots = [
            self.project_path / ".resonant" / "packs",
            Path.home() / ".resonant" / "packs",
        ]
        self.roots = [Path(root).expanduser() for root in (*default_roots, *roots)]
        for value in self.configured.values():
            if isinstance(value, dict) and (value.get("path") or value.get("directory")):
                self.roots.append(Path(value.get("path") or value.get("directory")).expanduser())
        self._packs: dict[str, CapabilityPack] = {}
        self._agent_cache: dict[str, AgentType] = {}

    def discover(self) -> list[CapabilityPack]:
        packs: dict[str, CapabilityPack] = {}
        candidates: list[Path] = []
        for root in self.roots:
            if (root / PACK_MANIFEST).is_file():
                candidates.append(root)
            elif root.is_dir():
                candidates.extend(
                    child for child in root.iterdir()
                    if child.is_dir() and (child / PACK_MANIFEST).is_file()
                )
        for directory in candidates:
            try:
                pack = self._load(directory)
            except CapabilityPackError:
                continue
            packs[pack.id] = pack
        self._packs = packs
        self._agent_cache.clear()
        return sorted(packs.values(), key=lambda pack: (pack.name.casefold(), pack.version))

    def get(self, pack_id: str) -> CapabilityPack | None:
        if not self._packs:
            self.discover()
        return self._packs.get(pack_id)

    def active(self) -> list[CapabilityPack]:
        if not self._packs:
            self.discover()
        return [pack for pack in self._packs.values() if pack.enabled and pack.trusted]

    def get_agent_type(self, name: str) -> AgentType | None:
        if name in self._agent_cache:
            return self._agent_cache[name]
        for pack in self.active():
            for relative in pack.agents:
                path = (Path(pack.path) / relative).resolve()
                if not path.is_file() or Path(pack.path).resolve() not in path.parents:
                    continue
                agent = self._parse_agent(path)
                if agent:
                    self._agent_cache[agent.name] = agent
        return self._agent_cache.get(name)

    def hook_definitions(self) -> list[HookDefinition]:
        definitions = []
        for pack in self.active():
            for data in pack.hooks:
                try:
                    definitions.append(HookDefinition.from_dict(data))
                except (TypeError, ValueError):
                    continue
        return definitions

    def mcp_servers(self) -> dict[str, dict[str, Any]]:
        servers: dict[str, dict[str, Any]] = {}
        for pack in self.active():
            for name, config in pack.mcp_servers.items():
                servers[f"{pack.id}-{name}"] = dict(config)
        return servers

    def context_catalog(self) -> dict[str, Any]:
        return {
            "packs": [pack.to_dict() for pack in self.active()],
            "commands": [command for pack in self.active() for command in pack.commands],
            "recipes": [recipe for pack in self.active() for recipe in pack.recipes],
            "ui_panels": [panel for pack in self.active() for panel in pack.ui_panels],
        }

    def skill_context(self, query: str, *, max_skills: int = 6) -> str:
        """Render matching trusted pack skills without copying them globally."""
        terms = set(re.findall(r"[A-Za-z0-9_]{3,}", query.casefold()))
        ranked: list[tuple[int, CapabilityPack, Path, str]] = []
        for pack in self.active():
            root = Path(pack.path).resolve()
            for relative in pack.skills:
                path = (root / relative).resolve()
                if not path.is_file() or root not in path.parents:
                    continue
                text = path.read_text(encoding="utf-8", errors="replace")
                haystack = f"{path.stem} {text[:4000]}".casefold()
                score = sum(1 for term in terms if term in haystack)
                if score or not terms:
                    ranked.append((score, pack, path, text))
        ranked.sort(key=lambda item: (-item[0], item[1].id, item[2].name))
        if not ranked:
            return ""
        blocks = ["\n## Trusted capability-pack skills\n"]
        for _, pack, path, body in ranked[:max_skills]:
            blocks.append(f"### {pack.name}: {path.stem}\n{body.strip()}\n")
        return "\n".join(blocks)

    def _load(self, directory: Path) -> CapabilityPack:
        manifest_path = directory / PACK_MANIFEST
        try:
            raw = manifest_path.read_bytes()
            data = json.loads(raw.decode("utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise CapabilityPackError(f"Invalid pack manifest {manifest_path}: {exc}") from exc
        pack_id = str(data.get("id") or directory.name).strip()
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,80}", pack_id):
            raise CapabilityPackError(f"Invalid pack id: {pack_id}")
        digest = hashlib.sha256(raw).hexdigest()
        configured = self.configured.get(pack_id) if isinstance(self.configured, dict) else {}
        configured = configured if isinstance(configured, dict) else {}
        expected_digest = str(configured.get("sha256") or data.get("sha256") or "")
        trust = str(configured.get("trust") or data.get("trust") or "").lower()
        trusted = trust in {"local", "trusted", "signed"} and (
            not expected_digest or expected_digest == digest
        )
        enabled = bool(configured.get("enabled", data.get("enabled", False)))
        return CapabilityPack(
            id=pack_id,
            name=str(data.get("name") or pack_id),
            version=str(data.get("version") or "0.0.0"),
            description=str(data.get("description") or ""),
            path=str(directory.resolve()),
            enabled=enabled,
            trusted=trusted,
            digest=digest,
            permissions=[str(value) for value in data.get("permissions") or []],
            agents=[str(value) for value in data.get("agents") or []],
            skills=[str(value) for value in data.get("skills") or []],
            hooks=[value for value in data.get("hooks") or [] if isinstance(value, dict)],
            mcp_servers={
                str(key): value for key, value in (data.get("mcp_servers") or {}).items()
                if isinstance(value, dict)
            },
            commands=[value for value in data.get("commands") or [] if isinstance(value, dict)],
            recipes=[value for value in data.get("recipes") or [] if isinstance(value, dict)],
            ui_panels=[value for value in data.get("ui_panels") or [] if isinstance(value, dict)],
            metadata={"manifest": str(manifest_path), **(data.get("metadata") or {})},
        )

    @staticmethod
    def _parse_agent(path: Path) -> AgentType | None:
        text = path.read_text(encoding="utf-8", errors="replace")
        if not text.startswith("---"):
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
