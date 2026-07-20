"""Durable, modality-neutral artifacts produced by agent runs.

Conversation history should contain references to large observations, not the
observations themselves.  The artifact store gives text, images, audio, video,
terminal captures, diffs, and future modalities one stable contract while
keeping provider-specific payload encoding out of the rest of the harness.
"""

from __future__ import annotations

import hashlib
import json
import mimetypes
import shutil
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any


class ArtifactKind(str, Enum):
    TEXT = "text"
    IMAGE = "image"
    AUDIO = "audio"
    VIDEO = "video"
    TERMINAL = "terminal"
    DIFF = "diff"
    DOM = "dom"
    ACCESSIBILITY = "accessibility"
    TRACE = "trace"
    BINARY = "binary"


@dataclass(slots=True)
class Artifact:
    id: str
    kind: str
    path: str
    media_type: str
    size: int
    sha256: str
    created_at: float
    label: str = ""
    source: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Artifact":
        fields = cls.__dataclass_fields__
        return cls(**{key: value for key, value in data.items() if key in fields})


@dataclass(slots=True)
class ModelCapabilities:
    """Provider-neutral capability declaration used at model boundaries."""

    modalities: set[str] = field(default_factory=lambda: {"text"})
    max_artifact_bytes: int | None = None
    accepts_data_urls: bool = False

    def supports(self, kind: ArtifactKind | str) -> bool:
        value = kind.value if isinstance(kind, ArtifactKind) else str(kind)
        return value in self.modalities


def project_state_dir(project_path: str | Path) -> Path:
    """Return the app-owned state directory for a project."""
    resolved = str(Path(project_path).expanduser().resolve())
    project_hash = hashlib.sha256(resolved.encode("utf-8")).hexdigest()[:16]
    return Path.home() / ".resonant" / "projects" / project_hash


class ArtifactStore:
    """Content-addressed artifact storage with an append-only manifest."""

    def __init__(self, project_path: str | Path, root: str | Path | None = None):
        self.project_path = Path(project_path).expanduser().resolve()
        self.root = Path(root) if root else project_state_dir(self.project_path) / "artifacts"
        self.root.mkdir(parents=True, exist_ok=True)
        self._manifest_path = self.root / "manifest.jsonl"
        self._lock = threading.RLock()

    def put_text(
        self,
        content: str,
        *,
        kind: ArtifactKind | str = ArtifactKind.TEXT,
        label: str = "",
        source: str = "",
        media_type: str = "text/plain; charset=utf-8",
        metadata: dict[str, Any] | None = None,
    ) -> Artifact:
        return self.put_bytes(
            content.encode("utf-8"),
            kind=kind,
            label=label,
            source=source,
            media_type=media_type,
            suffix=".txt",
            metadata=metadata,
        )

    def put_bytes(
        self,
        content: bytes,
        *,
        kind: ArtifactKind | str = ArtifactKind.BINARY,
        label: str = "",
        source: str = "",
        media_type: str = "application/octet-stream",
        suffix: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> Artifact:
        digest = hashlib.sha256(content).hexdigest()
        artifact_id = f"art_{digest[:12]}_{uuid.uuid4().hex[:6]}"
        safe_suffix = suffix if suffix.startswith(".") or not suffix else f".{suffix}"
        destination = self.root / f"{artifact_id}{safe_suffix}"
        with self._lock:
            destination.write_bytes(content)
            artifact = Artifact(
                id=artifact_id,
                kind=str(kind.value if isinstance(kind, ArtifactKind) else kind),
                path=str(destination),
                media_type=media_type,
                size=len(content),
                sha256=digest,
                created_at=time.time(),
                label=label,
                source=source,
                metadata=dict(metadata or {}),
            )
            self._append_manifest(artifact)
        return artifact

    def import_file(
        self,
        source_path: str | Path,
        *,
        kind: ArtifactKind | str | None = None,
        label: str = "",
        source: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> Artifact:
        path = Path(source_path).expanduser().resolve()
        content = path.read_bytes()
        media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        resolved_kind = kind or self._kind_for_media_type(media_type)
        artifact = self.put_bytes(
            content,
            kind=resolved_kind,
            label=label or path.name,
            source=source or str(path),
            media_type=media_type,
            suffix=path.suffix,
            metadata=metadata,
        )
        return artifact

    def get(self, artifact_id: str) -> Artifact | None:
        for artifact in reversed(self.list()):
            if artifact.id == artifact_id:
                return artifact
        return None

    def list(self, *, kind: ArtifactKind | str | None = None) -> list[Artifact]:
        if not self._manifest_path.exists():
            return []
        wanted = str(kind.value if isinstance(kind, ArtifactKind) else kind or "")
        records: list[Artifact] = []
        with self._lock:
            for raw in self._manifest_path.read_text(encoding="utf-8").splitlines():
                try:
                    artifact = Artifact.from_dict(json.loads(raw))
                except (TypeError, ValueError, json.JSONDecodeError):
                    continue
                if not wanted or artifact.kind == wanted:
                    records.append(artifact)
        return records

    def reference(self, artifact: Artifact) -> str:
        """Return the compact representation safe to place in model context."""
        label = f" ({artifact.label})" if artifact.label else ""
        return (
            f"[artifact:{artifact.id}{label} kind={artifact.kind} "
            f"media_type={artifact.media_type} bytes={artifact.size} "
            f"sha256={artifact.sha256[:12]} path={artifact.path}]"
        )

    def copy_to(self, artifact_id: str, destination: str | Path) -> Path:
        artifact = self.get(artifact_id)
        if not artifact:
            raise KeyError(f"Unknown artifact: {artifact_id}")
        target = Path(destination).expanduser().resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(artifact.path, target)
        return target

    def delivery(self, artifact_id: str, capabilities: ModelCapabilities) -> dict[str, Any]:
        """Negotiate native delivery or a durable reference for any modality."""
        import base64

        artifact = self.get(artifact_id)
        if not artifact:
            raise KeyError(f"Unknown artifact: {artifact_id}")
        if capabilities.max_artifact_bytes and artifact.size > capabilities.max_artifact_bytes:
            return {
                "mode": "reference",
                "reason": "artifact exceeds provider byte limit",
                "content": self.reference(artifact),
                "artifact": artifact.to_dict(),
            }
        if artifact.kind in {"text", "terminal", "diff", "dom", "accessibility", "trace"}:
            return {
                "mode": "native_text",
                "content": Path(artifact.path).read_text(encoding="utf-8", errors="replace"),
                "artifact": artifact.to_dict(),
            }
        if capabilities.supports(artifact.kind) and capabilities.accepts_data_urls:
            encoded = base64.b64encode(Path(artifact.path).read_bytes()).decode("ascii")
            return {
                "mode": "native_data_url",
                "content": f"data:{artifact.media_type};base64,{encoded}",
                "artifact": artifact.to_dict(),
            }
        return {
            "mode": "reference",
            "reason": f"model does not currently accept {artifact.kind}",
            "content": self.reference(artifact),
            "artifact": artifact.to_dict(),
        }

    def _append_manifest(self, artifact: Artifact) -> None:
        with self._manifest_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(artifact.to_dict(), ensure_ascii=False) + "\n")

    @staticmethod
    def _kind_for_media_type(media_type: str) -> ArtifactKind:
        family = media_type.split("/", 1)[0].lower()
        return {
            "text": ArtifactKind.TEXT,
            "image": ArtifactKind.IMAGE,
            "audio": ArtifactKind.AUDIO,
            "video": ArtifactKind.VIDEO,
        }.get(family, ArtifactKind.BINARY)
