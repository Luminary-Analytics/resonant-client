"""Provenance-aware context attachments for conversations and agents."""

from __future__ import annotations

import json
import re
import subprocess
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

from resonant_client.processes import background_process_kwargs


@dataclass(slots=True)
class ContextItem:
    id: str
    provider: str
    label: str
    content: str
    provenance: str
    created_at: float
    fresh_until: float | None = None
    pinned: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def estimated_tokens(self) -> int:
        return (len(self.content) + 3) // 4

    def to_dict(self, *, include_content: bool = False) -> dict[str, Any]:
        data = asdict(self)
        data["estimated_tokens"] = self.estimated_tokens
        if not include_content:
            data.pop("content", None)
        return data


Provider = Callable[[str], ContextItem | list[ContextItem] | None]


class ContextBroker:
    """Resolve explicit ``@provider:selector`` attachments on demand."""

    MENTION_RE = re.compile(
        r"(?<!\w)@(?P<provider>[a-z][a-z0-9_-]{1,30}):(?P<selector>"
        r"\"[^\"]+\"|'[^']+'|[^\s,;]+)",
        re.IGNORECASE,
    )

    def __init__(
        self,
        project_path: str | Path,
        *,
        agent_registry: Any = None,
        checkpoint_store: Any = None,
        artifact_store: Any = None,
        codebase_index: Any = None,
    ):
        self.project_path = Path(project_path).expanduser().resolve()
        self.agent_registry = agent_registry
        self.checkpoint_store = checkpoint_store
        self.artifact_store = artifact_store
        self.codebase_index = codebase_index
        self._providers: dict[str, Provider] = {}
        self._pinned: dict[str, ContextItem] = {}
        self.register("file", self._file)
        self.register("symbol", self._symbol)
        self.register("diff", self._diff)
        self.register("checkpoint", self._checkpoint)
        self.register("agent", self._agent)
        self.register("artifact", self._artifact)
        self.register("test-failure", self._test_failure)
        self.register("terminal", self._terminal)
        self.register("plan", self._plan)

    def register(self, name: str, provider: Provider) -> None:
        self._providers[str(name).strip().lower()] = provider

    def resolve_mentions(self, text: str) -> list[ContextItem]:
        items = list(self._pinned.values())
        seen = {item.id for item in items}
        for match in self.MENTION_RE.finditer(text or ""):
            provider_name = match.group("provider").lower()
            selector = match.group("selector").strip("\"'")
            provider = self._providers.get(provider_name)
            if provider is None:
                continue
            try:
                result = provider(selector)
            except Exception:
                continue
            resolved = result if isinstance(result, list) else [result] if result else []
            for item in resolved:
                if item.id not in seen:
                    items.append(item)
                    seen.add(item.id)
        return items

    def render(self, items: list[ContextItem]) -> str:
        if not items:
            return ""
        blocks = ["\n\n--- EXPLICIT CONTEXT ATTACHMENTS ---"]
        for item in items:
            blocks.append(
                f"[{item.provider}:{item.label} | provenance={item.provenance} | "
                f"tokens~{item.estimated_tokens}]\n{item.content}"
            )
        blocks.append("--- END EXPLICIT CONTEXT ATTACHMENTS ---")
        return "\n\n".join(blocks)

    def pin(self, item: ContextItem) -> None:
        item.pinned = True
        self._pinned[item.id] = item

    def unpin(self, item_id: str) -> bool:
        return self._pinned.pop(item_id, None) is not None

    def catalog(self) -> list[dict[str, Any]]:
        return [
            {"name": name, "syntax": f"@{name}:selector"}
            for name in sorted(self._providers)
        ]

    def _file(self, selector: str) -> ContextItem | None:
        path = (self.project_path / selector).resolve()
        if self.project_path not in path.parents and path != self.project_path:
            return None
        if not path.is_file():
            return None
        content = path.read_text(encoding="utf-8", errors="replace")
        return self._item("file", selector, content, str(path))

    def _symbol(self, selector: str) -> list[ContextItem]:
        results = []
        if self.codebase_index and getattr(self.codebase_index, "is_indexed", False):
            for match in self.codebase_index.search(selector, max_results=8):
                path = getattr(match, "path", "")
                context = getattr(match, "context", "")
                symbols = getattr(match, "symbols", [])
                results.append(self._item(
                    "symbol",
                    f"{selector} in {path}",
                    f"Symbols: {', '.join(symbols)}\n{context}",
                    f"repo-index:{path}",
                ))
        return results

    def _diff(self, selector: str) -> ContextItem | None:
        args = ["git", "diff"]
        if selector not in {"working", "workspace", "current", "."}:
            args.append(selector)
        args.append("--")
        result = subprocess.run(
            args,
            cwd=self.project_path,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            **background_process_kwargs(),
        )
        if result.returncode != 0:
            return None
        return self._item("diff", selector, result.stdout or "(no changes)", "git")

    def _checkpoint(self, selector: str) -> ContextItem | None:
        if not self.checkpoint_store:
            return None
        checkpoint = self.checkpoint_store.get(selector)
        return self._item(
            "checkpoint", selector,
            json.dumps(checkpoint.to_dict(), indent=2, ensure_ascii=False),
            checkpoint.conversation_path,
        )
    def _agent(self, selector: str) -> ContextItem | None:
        if not self.agent_registry:
            return None
        record = self.agent_registry.get(selector)
        if not record:
            return None
        payload = record.to_dict()
        if record.handoff:
            payload["handoff"] = record.handoff
        return self._item(
            "agent", selector, json.dumps(payload, indent=2, ensure_ascii=False),
            record.transcript_path,
        )

    def _artifact(self, selector: str) -> ContextItem | None:
        if not self.artifact_store:
            return None
        artifact = self.artifact_store.get(selector)
        if not artifact:
            return None
        path = Path(artifact.path)
        if artifact.kind in {"text", "terminal", "diff", "trace", "dom", "accessibility"}:
            content = path.read_text(encoding="utf-8", errors="replace")
        else:
            content = self.artifact_store.reference(artifact)
        return self._item("artifact", selector, content, artifact.path)

    def _test_failure(self, selector: str) -> ContextItem | None:
        failures = sorted(
            (self.project_path / ".pytest_cache").glob("**/lastfailed")
            if (self.project_path / ".pytest_cache").exists() else [],
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        if not failures:
            return None
        content = failures[0].read_text(encoding="utf-8", errors="replace")
        return self._item("test-failure", selector, content, str(failures[0]))

    def _terminal(self, selector: str) -> ContextItem | None:
        candidates = sorted(
            (self.project_path / ".resonant").glob("terminal*.log")
            if (self.project_path / ".resonant").exists() else [],
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        if not candidates:
            return None
        content = candidates[0].read_text(encoding="utf-8", errors="replace")
        return self._item("terminal", selector, content, str(candidates[0]))

    def _plan(self, selector: str) -> ContextItem | None:
        candidates = [self.project_path / "PLAN.md", self.project_path / "ROADMAP.md"]
        path = next((candidate for candidate in candidates if candidate.exists()), None)
        if not path:
            return None
        return self._item(
            "plan", selector,
            path.read_text(encoding="utf-8", errors="replace"), str(path),
        )

    @staticmethod
    def _item(provider: str, label: str, content: str, provenance: str) -> ContextItem:
        stable = re.sub(r"[^A-Za-z0-9._-]+", "-", f"{provider}-{label}").strip("-")[:96]
        return ContextItem(
            id=stable or f"ctx-{int(time.time())}",
            provider=provider,
            label=label,
            content=content,
            provenance=provenance,
            created_at=time.time(),
        )
