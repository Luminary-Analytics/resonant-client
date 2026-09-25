"""The local bridge Lumi's code editor extensions use to reach the app.

While the app runs it keeps ``editor-bridge.json`` in Lumi's state folder
(normally ``~/.lumi``): the server's address and a token made for this
launch, readable only by this user. The VS Code extension and ``lumi editor``
(which JetBrains IDEs run as External Tools) read it and call
``/api/editor/<action>`` with ``Authorization: Bearer <token>``:

- ``GET status``: that Lumi is running, and the project it has open.
- ``POST context``: files or line ranges from the editor and an optional
  question. They go into the composer as ``@file:`` attachments; nothing
  reaches a model until the person sends the message.
- ``GET changes``: the files the open session's latest change-making turn
  changed, and ``GET before``: one of them as it was before that turn, which
  the editor shows beside the current file.

The token opens only these endpoints, never the page's socket or settings. A
request with an ``Origin`` header comes from a web page and is refused. Files
must be inside the project Lumi has open, and files the project's privacy
settings exclude are neither attached nor shown. ``security.editor_bridge``
(Settings or organization policy) turns the bridge off.
"""

from __future__ import annotations

import asyncio
import atexit
import hmac
import json
import logging
import os
import re
import secrets
import subprocess
import zipfile
from pathlib import Path
from typing import Any, Mapping

from ..paths import state_home
from ..processes import background_process_kwargs

logger = logging.getLogger(__name__)

BRIDGE_FILE = "editor-bridge.json"
MAX_ITEMS = 50
MAX_QUESTION = 20_000
MAX_WHOLE_FILE = 512 * 1024  # bytes; a larger file needs a line range
MAX_BEFORE = 5 * 1024 * 1024
MAX_LISTED = 500
_CHECKPOINT_ID = re.compile(r"^cp_\d{5,}_[0-9a-f]{8}$")


class BridgeError(Exception):
    """A request the bridge refuses; ``status`` is the HTTP status to answer with."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def bridge_path() -> Path:
    return state_home() / BRIDGE_FILE


def read_bridge() -> dict | None:
    """The running app's address and token, or None when no Lumi window is running."""
    try:
        data = json.loads(bridge_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not data.get("url") or not data.get("token"):
        return None
    # A launch that was killed leaves its file behind; its port may belong to
    # something else now, which must not be sent the old token.
    if isinstance(data.get("pid"), int) and not _running(data["pid"]):
        return None
    return data


def _running(pid: int) -> bool:
    try:
        import psutil
    except ImportError:
        return True
    return psutil.pid_exists(pid)


class EditorBridge:
    """This launch's bridge token and the file that tells editors about it."""

    def __init__(self) -> None:
        self._token = secrets.token_urlsafe(32)
        self.url = ""
        self._published: Path | None = None

    @property
    def published(self) -> bool:
        return self._published is not None

    def publish(self, url: str) -> Path | None:
        """Write the bridge file, readable only by this user, for this launch."""
        self.url = url.rstrip("/")
        path = bridge_path()
        temporary = path.with_name(f".{BRIDGE_FILE}.{os.getpid()}.tmp")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump({"version": 1, "url": self.url, "token": self._token, "pid": os.getpid()}, handle)
            os.replace(temporary, path)
        except OSError:
            logger.warning("Could not write the editor bridge file", exc_info=True)
            temporary.unlink(missing_ok=True)
            return None
        self._published = path
        return path

    def withdraw(self) -> None:
        """Remove the bridge file, unless a later launch has replaced it."""
        path, self._published = self._published, None
        if path is None:
            return
        try:
            if json.loads(path.read_text(encoding="utf-8")).get("pid") == os.getpid():
                path.unlink()
        except (OSError, ValueError, AttributeError):
            pass

    def start(self, url: str, *, enabled: bool) -> None:
        """Called once the server answers at ``url``; removes the file again at exit."""
        self.url = url.rstrip("/")
        if enabled:
            self.publish(self.url)
        atexit.register(self.withdraw)

    def sync(self, enabled: bool) -> None:
        """Follow the setting: write the file when turned on (once started), remove it when off."""
        if not enabled:
            self.withdraw()
        elif self.url and not self.published:
            self.publish(self.url)

    def authorized(self, headers: Mapping[str, str]) -> bool:
        if headers.get("origin") is not None:
            return False  # a web page, never an editor
        scheme, _, token = str(headers.get("authorization") or "").partition(" ")
        token = token.strip()
        if scheme.lower() != "bearer" or not token:
            return False
        return hmac.compare_digest(token.encode("utf-8", "surrogatepass"), self._token.encode("utf-8"))


bridge = EditorBridge()


# ── What an editor sends ──────────────────────────────────────────────


def _inside(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _line_range(raw: dict) -> tuple[int, int] | None:
    start, end = raw.get("start_line"), raw.get("end_line")
    if start is None:
        return None
    try:
        first, last = int(start), int(end if end is not None else start)
    except (TypeError, ValueError):
        raise BridgeError(400, "Line numbers must be whole numbers.") from None
    if first < 1 or last < 1:
        raise BridgeError(400, "Line numbers start at 1.")
    return min(first, last), max(first, last)


def _mention(selector: str) -> str | None:
    """``@file:`` syntax for ``selector``, quoted when it has spaces (engine/context_broker.py)."""
    if not re.search(r"[\s,;\"']", selector):
        return f"@file:{selector}"
    if '"' not in selector:
        return f'@file:"{selector}"'
    if "'" not in selector:
        return f"@file:'{selector}'"
    return None


def _check(value: str, lines: tuple[int, int] | None, root: Path, exclusions: Any) -> tuple[str, str]:
    """(the project-relative path, "") for a file that can be attached, or ("", why not)."""
    candidate = Path(value)
    if not candidate.is_absolute():
        return "", "Send the file's full path."
    path = candidate.resolve()
    if not _inside(path, root):
        return "", f"It isn't in the project Lumi has open ({root})."
    if not path.is_file():
        return "", "It isn't a saved file."
    if exclusions is not None and exclusions.match(str(path)):
        return "", "This project's privacy settings exclude it."
    if lines is None and path.stat().st_size > MAX_WHOLE_FILE:
        return "", "It's too large to attach whole: select the lines you need."
    return path.relative_to(root).as_posix(), ""


def attach(body: Any, *, project: str, exclusions: Any = None) -> dict:
    """Check what an editor sent and make the composer text for it.

    Returns ``text`` (the question, then one ``@file:`` mention per file),
    ``attached`` (a label per file) and ``skipped`` (path and reason).
    Raises BridgeError when nothing can be attached.
    """
    if not isinstance(body, dict):
        raise BridgeError(400, "Send a JSON object.")
    items = body.get("items")
    if not isinstance(items, list) or not items:
        raise BridgeError(400, "Send at least one file.")
    if len(items) > MAX_ITEMS:
        raise BridgeError(413, f"Send at most {MAX_ITEMS} files at a time.")
    question = body.get("text") or ""
    if not isinstance(question, str):
        raise BridgeError(400, "The question must be text.")
    question = question.strip()
    if len(question) > MAX_QUESTION:
        raise BridgeError(413, "The question is too long.")
    if not project:
        raise BridgeError(409, "Lumi has no project open.")
    root = Path(project).expanduser().resolve()
    mentions: list[str] = []
    attached: list[str] = []
    skipped: list[dict] = []
    for raw in items:
        value = raw.get("path") if isinstance(raw, dict) else None
        if not isinstance(value, str) or not value.strip():
            raise BridgeError(400, "Each item needs a path.")
        lines = _line_range(raw)
        relative, reason = _check(value, lines, root, exclusions)
        span = f"#L{lines[0]}-{lines[1]}" if lines else ""
        mention = _mention(relative + span) if relative else None
        if relative and mention is None:
            reason = "Lumi can't attach a file whose name has both kinds of quotation mark."
        if reason:
            skipped.append({"path": value, "reason": reason})
        elif mention not in mentions:
            mentions.append(mention)
            attached.append(relative + (f" lines {lines[0]}-{lines[1]}" if lines else ""))
    if not mentions:
        outside = all("isn't in the project" in item["reason"] for item in skipped)
        reasons = " ".join(f"{Path(item['path']).name}: {item['reason']}" for item in skipped)
        raise BridgeError(409 if outside else 400, reasons)
    text = "\n".join(part for part in (question, " ".join(mentions)) if part)
    return {"text": text, "attached": attached, "skipped": skipped}


# ── What a turn changed ───────────────────────────────────────────────


def latest_changing_turn(events: list[dict]) -> dict | None:
    """The most recent turn that changed files, read from a session's display events.

    ``checkpoint`` is the snapshot taken before the turn's first change, or ""
    when there was none (the Codex and Claude Code backends change files
    inside their own tools, so Lumi knows only the files they report).
    ``changed`` is the files the turn reported, ``finished`` whether it has
    ended and ``prompt`` the start of the message that began it.
    """
    turn_end = len(events)
    for index in range(len(events) - 1, -1, -1):
        if events[index].get("event") != "user_message":
            continue
        turn = events[index + 1:turn_end]
        turn_end = index
        checkpoint = next((str(event.get("checkpoint_id") or "") for event in turn
                           if event.get("event") == "checkpoint.created"), "")
        ends = [event for event in turn if event.get("event") == "session.end"]
        reported = (ends[-1].get("evidence") or {}).get("changed_files") if ends else None
        changed = [str(value) for value in reported or () if isinstance(value, str) and value.strip()]
        if checkpoint or changed:
            prompt = " ".join(str(events[index].get("text") or "").split())
            return {"checkpoint": checkpoint, "changed": changed, "finished": bool(ends),
                    "prompt": prompt[:120]}
    return None


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=root, capture_output=True, check=False, timeout=60,
                          **background_process_kwargs())


def _checkpoint_record(root: Path, checkpoint_id: str) -> dict | None:
    """A session checkpoint's record (engine/checkpoint_timeline.py) by id, from any session."""
    from ..engine.artifacts import project_state_dir  # the engine is slow to import; `lumi editor` needs none of it

    if not _CHECKPOINT_ID.match(checkpoint_id or ""):
        return None
    for marker in (project_state_dir(root) / "checkpoints").glob(f"*/{checkpoint_id}.conversation.json"):
        try:
            lines = (marker.parent / "timeline.jsonl").read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if isinstance(record, dict) and record.get("id") == checkpoint_id:
                return record
    return None


def _source(root: Path, key: str) -> dict:
    """Where "before" versions come from: a checkpoint's commit or archive, or HEAD."""
    if key == "HEAD":
        head = _git(root, "rev-parse", "--verify", "HEAD^{commit}")
        if head.returncode == 0:
            return {"key": "HEAD", "commit": head.stdout.decode().strip(), "ref": ""}
        raise BridgeError(404, "This project has no commits to compare with.")
    record = _checkpoint_record(root, key)
    if record is None:
        raise BridgeError(404, "The snapshot from before that turn is gone.")
    ref = str(record.get("workspace_ref") or "")
    if ref:
        commit = _git(root, "rev-parse", "--verify", f"{ref}^{{commit}}")
        if commit.returncode == 0:
            return {"key": key, "commit": commit.stdout.decode().strip(), "ref": ref}
    archive = str(record.get("workspace_archive") or "")
    if archive and Path(archive).is_file():
        return {"key": key, "archive": archive}
    raise BridgeError(404, "The snapshot from before that turn is gone.")


def _before_bytes(root: Path, source: dict, relative: str) -> bytes | None:
    """``relative`` as it was in ``source``, or None if it didn't exist then."""
    if source.get("commit"):
        spec = f"{source['commit']}:{relative}"
        size = _git(root, "cat-file", "-s", spec)
        if size.returncode != 0:
            return None
        if int(size.stdout.decode().strip() or 0) > MAX_BEFORE:
            raise BridgeError(413, f"{relative} is too large to show.")
        # As a checkout would write it (line endings, filters), so it
        # compares with the file on disk rather than the stored blob.
        blob = _git(root, "cat-file", "--filters", spec)
        return blob.stdout if blob.returncode == 0 else None
    try:
        with zipfile.ZipFile(source["archive"]) as archive:
            info = archive.getinfo(relative)
            if info.file_size > MAX_BEFORE:
                raise BridgeError(413, f"{relative} is too large to show.")
            return archive.read(info)
    except (KeyError, OSError, zipfile.BadZipFile):
        return None


def _relative(root: Path, value: str) -> str:
    """A changed-file label (engine/turn_outcomes.py) as a project-relative path, or ""."""
    path = Path(value)
    path = path.resolve() if path.is_absolute() else (root / path).resolve()
    return path.relative_to(root).as_posix() if _inside(path, root) and path != root else ""


def _checkpoint_diff(root: Path, source: dict) -> list[dict]:
    from ..orchestration.checkpoints import CheckpointError, IterationCheckpointStore

    try:
        # A snapshot of the working tree now, made with a temporary index:
        # the person's index and files are untouched.
        current = IterationCheckpointStore(root).compare(source["ref"])["current"]
    except CheckpointError as exc:
        raise BridgeError(500, f"Could not compare with the snapshot: {exc}") from None
    diff = _git(root, "-c", "core.quotepath=false", "diff", "--name-status", "-z", "-M",
                source["commit"], current, "--")
    parts = diff.stdout.decode("utf-8", "replace").split("\0")
    entries: list[dict] = []
    index = 0
    while index + 1 < len(parts) and parts[index]:
        status = parts[index]
        if status[0] in "RC" and index + 2 < len(parts):
            entries.append({"path": parts[index + 2], "status": "renamed", "before_path": parts[index + 1]})
            index += 3
            continue
        kind = {"A": "added", "D": "deleted"}.get(status[0], "modified")
        entries.append({"path": parts[index + 1], "status": kind,
                        "before_path": "" if kind == "added" else parts[index + 1]})
        index += 2
    return entries


def _same(before: bytes | None, now: bytes | None) -> bool:
    """Equal contents, whatever their line endings (a checkout may have converted them)."""
    if before is None or now is None:
        return before is now
    return before.replace(b"\r\n", b"\n") == now.replace(b"\r\n", b"\n")


def _reported_diff(root: Path, source: dict, changed: list[str]) -> list[dict]:
    """Status of each reported file against ``source``; unchanged ones are left out."""
    entries = []
    for value in changed:
        relative = _relative(root, value)
        if not relative:
            continue
        before = _before_bytes(root, source, relative)
        path = root / relative
        now = path.read_bytes() if path.is_file() else None
        if _same(before, now):
            continue
        kind = "added" if before is None else "deleted" if now is None else "modified"
        entries.append({"path": relative, "status": kind, "before_path": "" if before is None else relative})
    return entries


def turn_changes(project: str, events: list[dict], exclusions: Any = None) -> dict:
    """The files the latest change-making turn changed, for an editor's diff view."""
    root = Path(project).expanduser().resolve()
    turn = latest_changing_turn(events)
    if turn is None:
        return {"project": str(root), "turn": None, "files": []}
    source = _source(root, turn["checkpoint"] or "HEAD")
    if source.get("ref"):
        entries = _checkpoint_diff(root, source)
    else:
        entries = _reported_diff(root, source, turn["changed"])
    shown = [entry for entry in entries
             if not (exclusions is not None and exclusions.match(str(root / entry["path"])))]
    shown.sort(key=lambda entry: entry["path"])
    return {
        "project": str(root),
        "turn": {**turn, "before": source["key"],
                 "compared_with": "the last commit" if source["key"] == "HEAD"
                 else "the snapshot taken before the turn's first change"},
        "files": shown[:MAX_LISTED],
        "more": max(0, len(shown) - MAX_LISTED),
    }


def before_text(project: str, key: str, relative: str, exclusions: Any = None) -> bytes | None:
    """One file as it was in ``key`` (a checkpoint id from turn_changes, or HEAD)."""
    root = Path(project).expanduser().resolve()
    if key != "HEAD" and not _CHECKPOINT_ID.match(key or ""):
        raise BridgeError(400, "Name the snapshot the file list gave.")
    target = (root / relative).resolve() if relative and not Path(relative).is_absolute() else None
    if target is None or not _inside(target, root) or target == root:
        raise BridgeError(400, "Send a path inside the project.")
    if exclusions is not None and exclusions.match(str(target)):
        raise BridgeError(403, "This project's privacy settings exclude it.")
    return _before_bytes(root, _source(root, key), target.relative_to(root).as_posix())


# ── HTTP ──────────────────────────────────────────────────────────────


def enabled(settings: Any) -> bool:
    try:
        return bool(settings.get("security", "editor_bridge", True))
    except Exception:
        return True


async def _json_body(request) -> Any:
    if request.headers.get("content-type", "").split(";")[0].strip() != "application/json":
        raise BridgeError(415, "Send JSON.")
    body = await request.body()
    if len(body) > 256 * 1024:
        raise BridgeError(413, "The request is too large.")
    try:
        return json.loads(body)
    except ValueError:
        raise BridgeError(400, "The request isn't valid JSON.") from None


async def handle(request, state) -> Any:
    """``/api/editor/<action>`` for the app in ``state`` (gui/app.py's AppState)."""
    from starlette.responses import JSONResponse, Response

    from .. import __version__

    no_store = {"Cache-Control": "no-store"}
    if not bridge.authorized(request.headers):
        return JSONResponse({"error": "Forbidden"}, status_code=403, headers=no_store)
    if not enabled(state.settings):
        return JSONResponse({"error": "The editor bridge is turned off in Lumi's Settings > Security."},
                            status_code=403, headers=no_store)
    action = request.path_params.get("action", "")
    project = str(getattr(state.project, "project_path", "") or "")
    try:
        if (action, request.method) == ("status", "GET"):
            return JSONResponse({"ok": True, "project": project, "version": __version__,
                                 "window": getattr(state, "_ws_ref", None) is not None}, headers=no_store)
        if (action, request.method) == ("context", "POST"):
            body = await _json_body(request)
            result = await asyncio.to_thread(attach, body, project=project,
                                             exclusions=state.exclusions_for(project))
            if getattr(state, "_ws_ref", None) is None:
                raise BridgeError(409, "Lumi's window isn't open.")
            source = str(body.get("source") or "your editor")[:40]
            state._push_ws_event({"event": "editor_context", "text": result["text"],
                                  "attached": result["attached"], "source": source, "project": project})
            return JSONResponse({"ok": True, "project": project, **result}, headers=no_store)
        if request.method == "GET" and action in ("changes", "before"):
            record = getattr(state.project, "current_session", None)
            if record is None and action == "changes":
                raise BridgeError(404, "No session is open in Lumi.")
            exclusions = state.exclusions_for(project)
            if action == "changes":
                events = await asyncio.to_thread(record.ledger.project_display_events)
                data = await asyncio.to_thread(turn_changes, project, events, exclusions)
                return JSONResponse(data, headers=no_store)
            params = request.query_params
            content = await asyncio.to_thread(before_text, project, params.get("before", ""),
                                              params.get("path", ""), exclusions)
            if content is None:
                raise BridgeError(404, "The file didn't exist before that turn.")
            return Response(content, media_type="application/octet-stream", headers=no_store)
        raise BridgeError(404, "Unknown editor request.")
    except BridgeError as exc:
        return JSONResponse({"error": str(exc)}, status_code=exc.status, headers=no_store)
