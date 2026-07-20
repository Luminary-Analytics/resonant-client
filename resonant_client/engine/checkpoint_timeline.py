"""Conversation-linked workspace checkpoints for every coding session."""

from __future__ import annotations

import json
import time
import uuid
import zipfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

from .artifacts import project_state_dir

RestoreMode = Literal["files", "conversation", "both"]


class CheckpointTimelineError(RuntimeError):
    pass


@dataclass(slots=True)
class SessionCheckpoint:
    id: str
    session_id: str
    sequence: int
    created_at: float
    reason: str
    project_path: str
    conversation_path: str
    workspace_ref: str = ""
    workspace_archive: str = ""
    agent_id: str = ""
    tool_name: str = ""
    plan_node_id: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class SessionCheckpointStore:
    """Persist workspace and conversation state behind one timeline cursor."""

    def __init__(
        self,
        project_path: str | Path,
        *,
        session_id: str,
        root: str | Path | None = None,
    ):
        self.project_path = Path(project_path).expanduser().resolve()
        self.session_id = session_id or "session"
        self.root = (
            Path(root)
            if root
            else project_state_dir(self.project_path) / "checkpoints" / self.session_id
        )
        self.root.mkdir(parents=True, exist_ok=True)
        self._index_path = self.root / "timeline.jsonl"
        self._sequence = len(self.list())

    def create(
        self,
        *,
        conversation_history: list[dict[str, Any]],
        display_events: list[dict[str, Any]] | None = None,
        reason: str,
        agent_id: str = "",
        tool_name: str = "",
        plan_node_id: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> SessionCheckpoint:
        self._sequence += 1
        checkpoint_id = f"cp_{self._sequence:05d}_{uuid.uuid4().hex[:8]}"
        conversation_path = self.root / f"{checkpoint_id}.conversation.json"
        conversation_path.write_text(
            json.dumps(
                {
                    "conversation_history": conversation_history,
                    "display_events": display_events or [],
                },
                indent=2,
                ensure_ascii=False,
                default=str,
            ),
            encoding="utf-8",
        )

        workspace_ref = ""
        workspace_archive = ""
        try:
            from resonant_client.orchestration.checkpoints import IterationCheckpointStore

            git_store = IterationCheckpointStore(self.project_path)
            result = git_store.create(
                intent_id=f"session-{self.session_id}",
                iteration=self._sequence,
                item_id=checkpoint_id,
            )
            workspace_ref = result["ref"]
        except Exception:
            workspace_archive = str(self._create_archive(checkpoint_id))

        checkpoint = SessionCheckpoint(
            id=checkpoint_id,
            session_id=self.session_id,
            sequence=self._sequence,
            created_at=time.time(),
            reason=reason,
            project_path=str(self.project_path),
            conversation_path=str(conversation_path),
            workspace_ref=workspace_ref,
            workspace_archive=workspace_archive,
            agent_id=agent_id,
            tool_name=tool_name,
            plan_node_id=plan_node_id,
            metadata=dict(metadata or {}),
        )
        with self._index_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(checkpoint.to_dict(), ensure_ascii=False) + "\n")
        return checkpoint

    def list(self) -> list[SessionCheckpoint]:
        if not self._index_path.exists():
            return []
        checkpoints = []
        for line in self._index_path.read_text(encoding="utf-8").splitlines():
            try:
                checkpoints.append(SessionCheckpoint(**json.loads(line)))
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
        return sorted(checkpoints, key=lambda item: item.sequence, reverse=True)

    def get(self, checkpoint_id: str) -> SessionCheckpoint:
        for checkpoint in self.list():
            if checkpoint.id == checkpoint_id:
                return checkpoint
        raise CheckpointTimelineError(f"Checkpoint not found: {checkpoint_id}")

    def restore(self, checkpoint_id: str, mode: RestoreMode = "both") -> dict[str, Any]:
        if mode not in {"files", "conversation", "both"}:
            raise CheckpointTimelineError(f"Invalid restore mode: {mode}")
        checkpoint = self.get(checkpoint_id)
        result: dict[str, Any] = {"checkpoint": checkpoint.to_dict(), "mode": mode}

        if mode in {"files", "both"}:
            if checkpoint.workspace_ref:
                from resonant_client.orchestration.checkpoints import IterationCheckpointStore

                result["workspace"] = IterationCheckpointStore(self.project_path).restore(
                    checkpoint.workspace_ref
                )
            elif checkpoint.workspace_archive:
                result["workspace"] = self._restore_archive(checkpoint)
            else:
                raise CheckpointTimelineError("Checkpoint has no workspace snapshot")

        if mode in {"conversation", "both"}:
            try:
                payload = json.loads(Path(checkpoint.conversation_path).read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise CheckpointTimelineError(f"Conversation snapshot is unreadable: {exc}") from exc
            result["conversation_history"] = payload.get("conversation_history") or []
            result["display_events"] = payload.get("display_events") or []
        return result

    def fork_payload(self, checkpoint_id: str) -> dict[str, Any]:
        """Return the saved conversation without mutating files or the source task."""
        return self.restore(checkpoint_id, "conversation")

    def compare(self, checkpoint_id: str) -> dict[str, Any]:
        checkpoint = self.get(checkpoint_id)
        if checkpoint.workspace_ref:
            from resonant_client.orchestration.checkpoints import IterationCheckpointStore

            return IterationCheckpointStore(self.project_path).compare(checkpoint.workspace_ref)
        return {
            "checkpoint": checkpoint.to_dict(),
            "message": "Archive-backed checkpoints do not support a live git diff.",
        }

    def _create_archive(self, checkpoint_id: str) -> Path:
        archive_path = self.root / f"{checkpoint_id}.workspace.zip"
        manifest: list[str] = []
        with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for path in self.project_path.rglob("*"):
                if not path.is_file() or path.is_symlink():
                    continue
                relative = path.relative_to(self.project_path)
                if relative.parts and relative.parts[0] in {
                    ".git", ".resonant", ".resonant-worktrees", "node_modules", "dist", "build",
                }:
                    continue
                manifest.append(relative.as_posix())
                archive.write(path, relative.as_posix())
            archive.writestr(".resonant-checkpoint-manifest.json", json.dumps(manifest))
        return archive_path

    def _restore_archive(self, checkpoint: SessionCheckpoint) -> dict[str, Any]:
        archive_path = Path(checkpoint.workspace_archive)
        if not archive_path.exists():
            raise CheckpointTimelineError(f"Workspace archive is missing: {archive_path}")
        recovery = self.root / f"recovery-{int(time.time())}-{checkpoint.id}.zip"
        self._write_recovery_archive(recovery)
        with zipfile.ZipFile(archive_path, "r") as archive:
            members = [
                name for name in archive.namelist()
                if name != ".resonant-checkpoint-manifest.json"
            ]
            for name in members:
                destination = (self.project_path / name).resolve()
                if self.project_path not in destination.parents:
                    raise CheckpointTimelineError(f"Archive path escaped project: {name}")
            archive.extractall(self.project_path, members=members)
        return {"archive": str(archive_path), "recovery_archive": str(recovery)}

    def _write_recovery_archive(self, destination: Path) -> None:
        with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for path in self.project_path.rglob("*"):
                if not path.is_file() or path.is_symlink():
                    continue
                relative = path.relative_to(self.project_path)
                if relative.parts and relative.parts[0] in {".git", ".resonant-worktrees"}:
                    continue
                archive.write(path, relative.as_posix())
