"""Language servers for the model: the ``code_intel`` tool.

``code_intel`` asks a language server where a symbol is defined, where it is
used, what its type and documentation are, what errors and warnings a file
has, and what a file contains. Lumi starts one server per language per
project the first time it's needed and keeps it for later calls, stopping it
after ``IDLE_SECONDS`` unused or when Lumi exits.

The server is the one named in Settings' ``lsp_servers`` for the file's type,
or else a well-known server found on PATH (``KNOWN``). Lumi speaks the
Language Server Protocol over the server's standard input and output; it
never asks a server to change files.

A language server can run the project's own code (rust-analyzer runs build
scripts; Java and Gradle servers run the build), so servers start only in
trusted projects (gui/workspace_trust.py), as automatic lint and test runs do.
They get the environment other children get (``secrets_store.child_env``),
pass the command guardrails, and run in the shell sandbox when it's on
(engine/os_sandbox.py). Answers never name files the exclusions hide.
"""

from __future__ import annotations

import atexit
import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence
from urllib.parse import urlsplit
from urllib.request import url2pathname

logger = logging.getLogger(__name__)

ACTIONS = ("definition", "references", "hover", "diagnostics", "symbols")
START_SECONDS = 60.0  # initialize: a first start can index the whole project
REQUEST_SECONDS = 30.0
DIAGNOSTIC_SECONDS = 20.0  # waiting for a server that pushes diagnostics
SETTLE_SECONDS = 0.75  # further pushes after the first (syntax, then types)
IDLE_SECONDS = 600.0
RETRY_SECONDS = 60.0  # a server that failed isn't started again sooner
MAX_FILE_BYTES = 2_000_000
MAX_LINES = 100  # locations, diagnostics
MAX_SYMBOLS = 300
MAX_HOVER = 4000
TOOL_NAME = "code_intel"


class LspError(RuntimeError):
    """A language server that is missing, failed or refused; the message is safe to show."""


@dataclass(frozen=True)
class ServerSpec:
    id: str
    name: str
    command: tuple[str, ...]  # program and arguments
    languages: dict[str, str] = field(default_factory=dict)  # extension -> languageId


# Well-known servers, used for a file type when Settings names none. Order
# matters: the first one installed wins.
_TS = {".ts": "typescript", ".mts": "typescript", ".cts": "typescript", ".tsx": "typescriptreact",
       ".js": "javascript", ".mjs": "javascript", ".cjs": "javascript", ".jsx": "javascriptreact"}
_PY = {".py": "python", ".pyi": "python"}
_C = {".c": "c", ".h": "c", ".cc": "cpp", ".cpp": "cpp", ".cxx": "cpp", ".hpp": "cpp", ".hh": "cpp"}
KNOWN: tuple[ServerSpec, ...] = (
    ServerSpec("python-pyright", "Python (Pyright)", ("pyright-langserver", "--stdio"), _PY),
    ServerSpec("python-pylsp", "Python (pylsp)", ("pylsp",), _PY),
    ServerSpec("typescript", "TypeScript/JavaScript", ("typescript-language-server", "--stdio"), _TS),
    ServerSpec("rust-analyzer", "Rust Analyzer", ("rust-analyzer",), {".rs": "rust"}),
    ServerSpec("gopls", "Go", ("gopls",), {".go": "go"}),
    ServerSpec("clangd", "C/C++ (clangd)", ("clangd",), _C),
    ServerSpec("csharp", "C#", ("csharp-ls",), {".cs": "csharp"}),
    ServerSpec("omnisharp", "C# (OmniSharp)", ("omnisharp", "-lsp"), {".cs": "csharp"}),
    ServerSpec("java", "Java", ("jdtls",), {".java": "java"}),
    ServerSpec("lua", "Lua", ("lua-language-server",), {".lua": "lua"}),
)
LANGUAGE_IDS = {language for spec in KNOWN for language in spec.languages.values()}

_SYMBOL_KINDS = ("", "file", "module", "namespace", "package", "class", "method", "property", "field",
                 "constructor", "enum", "interface", "function", "variable", "constant", "string", "number",
                 "boolean", "array", "object", "key", "null", "enum member", "struct", "event", "operator",
                 "type parameter")
_SEVERITIES = {1: "error", 2: "warning", 3: "info", 4: "hint"}


# ── Which server ────────────────────────────────────────────────────────────


def _program_name(program: str) -> str:
    name = os.path.basename(program.strip('"')).lower()
    for suffix in (".exe", ".cmd", ".bat"):
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return name


def _argv(command: Any) -> list[str]:
    if isinstance(command, list):
        return [str(part) for part in command if str(part)]
    text = str(command or "").strip()
    if not text:
        return []
    try:
        # posix=False on Windows keeps the backslashes in C:\tools\server.exe.
        parts = shlex.split(text, posix=os.name != "nt")
    except ValueError:
        parts = text.split()
    return [part.strip('"') for part in parts if part.strip('"')]


def configured(settings: Any) -> list[tuple[ServerSpec, bool]]:
    """Settings' ``lsp_servers`` as specs, with whether each is enabled.

    An entry is ``{"command": "...", "enabled": true, "extensions": [".py"]}``
    (or ``"languages": ["python"]``); without either, a server whose program
    matches a well-known one serves that one's file types.
    """
    raw = settings.get("lsp_servers") if settings is not None else {}
    result = []
    for name, value in (raw.items() if isinstance(raw, dict) else ()):
        data = value if isinstance(value, dict) else {"command": value}
        argv = _argv(data.get("command"))
        if not argv:
            continue
        languages: dict[str, str] = {}
        for extension in data.get("extensions") or []:
            extension = str(extension).lower()
            extension = extension if extension.startswith(".") else "." + extension
            known = next((spec.languages[extension] for spec in KNOWN if extension in spec.languages), "")
            languages[extension] = str(data.get("language_id") or known or extension[1:])
        for language in data.get("languages") or []:
            languages.update({ext: lang for spec in KNOWN for ext, lang in spec.languages.items()
                              if lang == str(language).lower()})
        if not languages:
            program = _program_name(argv[0])
            match = next((spec for spec in KNOWN if _program_name(spec.command[0]) == program), None)
            languages = dict(match.languages) if match else {}
        spec = ServerSpec(f"configured:{name}", str(data.get("name") or name), tuple(argv), languages)
        result.append((spec, data.get("enabled", True) is not False))
    return result


def choose(path: str, settings: Any) -> tuple[ServerSpec, list[str]]:
    """The server for ``path`` and the program and arguments to start it."""
    extension = Path(path).suffix.lower()
    for spec, enabled in configured(settings):
        if enabled and extension in spec.languages:
            program = shutil.which(spec.command[0]) or spec.command[0]
            return spec, [program, *spec.command[1:]]
    for spec in KNOWN:
        if extension in spec.languages:
            program = shutil.which(spec.command[0])
            if program:
                return spec, [program, *spec.command[1:]]
    candidates = [spec.command[0] for spec in KNOWN if extension in spec.languages]
    hint = (f" Install {' or '.join(candidates)}, or name a server in Settings' lsp_servers."
            if candidates else " Name a server for this file type in Settings' lsp_servers.")
    raise LspError(f"No language server for {extension or 'these'} files.{hint}")


# ── Paths and positions ─────────────────────────────────────────────────────


def path_to_uri(path: str) -> str:
    return Path(os.path.abspath(path)).as_uri()


def uri_to_path(uri: str) -> str:
    parts = urlsplit(uri)
    if parts.scheme != "file":
        return uri
    # Servers write a Windows drive as C: or c%3A; url2pathname finds only
    # the first, and unquotes the rest itself.
    location = re.sub("%3[aA]", ":", parts.path)
    if parts.netloc:
        location = f"//{parts.netloc}{location}"
    return os.path.normpath(url2pathname(location))


def _key(path: str) -> str:
    return os.path.normcase(os.path.abspath(path))


def _utf16_length(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def _from_utf16(text: str, units: int) -> int:
    """The character index in ``text`` at ``units`` UTF-16 code units."""
    count = 0
    for index, character in enumerate(text):
        if count >= units:
            return index
        count += 2 if ord(character) > 0xFFFF else 1
    return len(text)


def _lines(text: str) -> list[str]:
    return [line.rstrip("\r") for line in text.split("\n")]


def position(text: str, line: int, *, symbol: str = "", column: int = 0) -> dict:
    """An LSP position from a 1-based line and a symbol on it (or a 1-based column)."""
    lines = _lines(text)
    if not isinstance(line, int) or not 1 <= line <= len(lines):
        raise LspError(f"Give a line from 1 to {len(lines)}.")
    content = lines[line - 1]
    if symbol:
        match = re.search(rf"(?<![\w$]){re.escape(symbol)}(?![\w$])", content)
        index = match.start() if match else content.find(symbol)
        if index < 0:
            raise LspError(f"'{symbol}' isn't on line {line}: {content.strip()[:200]}")
    elif column:
        index = min(max(int(column) - 1, 0), len(content))
    else:
        index = len(content) - len(content.lstrip())
    return {"line": line - 1, "character": _utf16_length(content[:index])}


# ── One server ──────────────────────────────────────────────────────────────


class LanguageServer:
    """A running language server for one project, spoken to over stdio."""

    def __init__(self, spec: ServerSpec, root: str, launch: Sequence[str]):
        self.spec = spec
        self.root = root
        self.launch = list(launch)
        self.state = "new"  # new, starting, running, failed, stopped
        self.error = ""
        self.failed_at = 0.0
        self.last_used = time.monotonic()
        self.capabilities: dict = {}
        self._process: subprocess.Popen | None = None
        self._job = None
        self._write_lock = threading.Lock()
        self._lock = threading.Lock()
        self._start_lock = threading.Lock()
        self._next_id = 0
        self._pending: dict[int, dict] = {}
        self._documents: dict[str, dict] = {}
        self._diagnostics: dict[str, dict] = {}
        self._published = threading.Condition()
        self._stderr: deque[str] = deque(maxlen=20)

    # Starting and stopping

    def ensure_started(self) -> None:
        with self._start_lock:
            if self.state == "running" and self.alive:
                return
            if self.state == "failed" and time.monotonic() - self.failed_at < RETRY_SECONDS:
                raise LspError(self.error)
            self._start()

    @property
    def alive(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def _start(self) -> None:
        from ..processes import background_process_kwargs, windows_kill_job
        from ..secrets_store import child_env

        self.state, self.error = "starting", ""
        self._documents.clear()  # a new process has nothing open
        with self._published:
            self._diagnostics.clear()
        try:
            self._process = subprocess.Popen(
                self.launch, cwd=self.root, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, env=child_env(), **background_process_kwargs(new_process_group=True))
        except OSError as exc:
            self._fail(f"{self.spec.name} didn't start: {exc}")
            raise LspError(self.error) from exc
        try:
            self._job = windows_kill_job(self._process)
        except OSError:
            self._job = None
        threading.Thread(target=self._read, daemon=True, name=f"lsp-{self.spec.id}").start()
        stderr_reader = threading.Thread(target=self._drain_stderr, daemon=True, name=f"lsp-{self.spec.id}-err")
        stderr_reader.start()
        from .. import __version__

        root_uri = path_to_uri(self.root)
        try:
            result = self.request("initialize", {
                "processId": os.getpid(),
                "clientInfo": {"name": "Lumi", "version": __version__},
                "rootUri": root_uri,
                "rootPath": self.root,
                "workspaceFolders": [{"uri": root_uri, "name": os.path.basename(self.root) or self.root}],
                "capabilities": {
                    "general": {"positionEncodings": ["utf-16"]},
                    "textDocument": {
                        "synchronization": {"dynamicRegistration": False, "didSave": False},
                        "definition": {"linkSupport": True},
                        "references": {},
                        "hover": {"contentFormat": ["markdown", "plaintext"]},
                        "documentSymbol": {"hierarchicalDocumentSymbolSupport": True},
                        "publishDiagnostics": {"versionSupport": True},
                        "diagnostic": {"dynamicRegistration": False},
                    },
                    "workspace": {"workspaceFolders": True, "configuration": True},
                    "window": {"workDoneProgress": False},
                },
            }, timeout=START_SECONDS)
        except LspError as exc:
            self.stop()
            stderr_reader.join(2)  # what it printed before it ended says why
            detail = self._stderr_tail()
            self._fail(f"{self.spec.name} didn't start: {exc}" + (f" Its output: {detail}" if detail else ""))
            raise LspError(self.error) from exc
        self.capabilities = (result or {}).get("capabilities") or {}
        self.notify("initialized", {})
        self.state = "running"
        logger.info("Started %s for %s", self.spec.name, self.root)

    def _fail(self, message: str) -> None:
        self.state, self.error, self.failed_at = "failed", message[:1000], time.monotonic()

    def stop(self) -> None:
        """Ask the server to shut down, then end its process tree."""
        from ..processes import close_windows_job

        process = self._process
        if process is None:
            return
        was_running, self.state = self.state == "running", "stopping" if self.state == "running" else self.state
        if process.poll() is None and was_running:
            try:
                self.request("shutdown", None, timeout=3)
                self.notify("exit", None)
                process.wait(timeout=3)
            except (LspError, OSError, subprocess.TimeoutExpired):
                pass
        if self._job is not None:
            close_windows_job(self._job)
            self._job = None
        elif os.name != "nt" and process.poll() is None:
            import signal

            try:
                os.killpg(process.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        if process.poll() is None:
            process.kill()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        if self.state != "failed":
            self.state = "stopped"

    # The protocol

    def _send(self, message: dict) -> None:
        process = self._process
        if process is None or process.stdin is None:
            raise LspError(f"{self.spec.name} isn't running.")
        body = json.dumps(message, ensure_ascii=False).encode("utf-8")
        try:
            with self._write_lock:
                process.stdin.write(b"Content-Length: %d\r\n\r\n" % len(body) + body)
                process.stdin.flush()
        except (OSError, ValueError) as exc:
            raise LspError(f"{self.spec.name} stopped: {self._stderr_tail() or exc}") from exc

    def notify(self, method: str, params: Any) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def request(self, method: str, params: Any, *, timeout: float = REQUEST_SECONDS,
                cancel_event: threading.Event | None = None) -> Any:
        with self._lock:
            self._next_id += 1
            request_id = self._next_id
            slot = {"done": threading.Event(), "result": None, "error": None}
            self._pending[request_id] = slot
        self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        deadline = time.monotonic() + timeout
        while not slot["done"].wait(0.1):
            if cancel_event is not None and cancel_event.is_set() or time.monotonic() >= deadline:
                with self._lock:
                    self._pending.pop(request_id, None)
                try:
                    self.notify("$/cancelRequest", {"id": request_id})
                except LspError:
                    pass
                if cancel_event is not None and cancel_event.is_set():
                    raise LspError("Cancelled.")
                raise LspError(f"{self.spec.name} didn't answer {method} within {timeout:.0f} seconds.")
        if slot["error"] is not None:
            error = slot["error"]
            message = error.get("message") if isinstance(error, dict) else str(error)
            raise LspError(f"{self.spec.name}: {message}")
        return slot["result"]

    def _read(self) -> None:
        stream = self._process.stdout
        try:
            while True:
                length = None
                while True:
                    header = stream.readline()
                    if not header:
                        raise EOFError
                    header = header.strip()
                    if not header:
                        break
                    name, _, value = header.decode("ascii", "replace").partition(":")
                    if name.strip().lower() == "content-length":
                        length = int(value.strip())
                if length is None:
                    continue
                body = stream.read(length)
                if len(body) < length:
                    raise EOFError
                try:
                    message = json.loads(body.decode("utf-8"))
                except ValueError:
                    logger.debug("%s sent a message that isn't JSON", self.spec.name)
                    continue
                if isinstance(message, dict):
                    self._dispatch(message)
        except (EOFError, OSError, ValueError):
            pass
        finally:
            if self.state in ("starting", "running"):
                self._fail(f"{self.spec.name} stopped." + (f" Its output: {self._stderr_tail()}"
                                                            if self._stderr_tail() else ""))
            with self._lock:
                pending, self._pending = self._pending, {}
            for slot in pending.values():
                slot["error"] = {"message": self.error or "the server stopped"}
                slot["done"].set()
            with self._published:
                self._published.notify_all()

    def _dispatch(self, message: dict) -> None:
        method = message.get("method")
        if method is None and "id" in message:  # an answer
            with self._lock:
                slot = self._pending.pop(message["id"], None)
            if slot is not None:
                slot["result"], slot["error"] = message.get("result"), message.get("error")
                slot["done"].set()
        elif "id" in message:  # the server asks us something
            self._answer(message["id"], method, message.get("params"))
        elif method == "textDocument/publishDiagnostics":
            params = message.get("params") or {}
            key = _key(uri_to_path(str(params.get("uri") or "")))
            with self._published:
                previous = self._diagnostics.get(key, {})
                self._diagnostics[key] = {"items": list(params.get("diagnostics") or []),
                                          "count": previous.get("count", 0) + 1, "at": time.monotonic()}
                self._published.notify_all()

    def _answer(self, request_id: Any, method: str, params: Any) -> None:
        if method == "workspace/configuration":
            items = (params or {}).get("items") or []
            reply = {"result": [None] * len(items)}
        elif method == "workspace/workspaceFolders":
            reply = {"result": [{"uri": path_to_uri(self.root), "name": os.path.basename(self.root)}]}
        elif method == "workspace/applyEdit":
            reply = {"result": {"applied": False, "failureReason": "Lumi doesn't apply language server edits."}}
        elif method in ("client/registerCapability", "client/unregisterCapability",
                        "window/workDoneProgress/create", "window/showMessageRequest",
                        "workspace/codeLens/refresh", "workspace/semanticTokens/refresh",
                        "workspace/inlayHint/refresh", "workspace/diagnostic/refresh"):
            reply = {"result": None}
        else:
            reply = {"error": {"code": -32601, "message": f"Lumi doesn't handle {method}."}}
        try:
            self._send({"jsonrpc": "2.0", "id": request_id, **reply})
        except LspError:
            pass

    def _drain_stderr(self) -> None:
        stream = self._process.stderr
        try:
            for line in iter(stream.readline, b""):
                text = line.decode("utf-8", "replace").strip()
                if text:
                    self._stderr.append(text[:500])
        except (OSError, ValueError):
            pass

    def _stderr_tail(self) -> str:
        return " / ".join(list(self._stderr)[-3:])[:600]

    # Documents

    def sync(self, path: str) -> tuple[str, str, bool]:
        """Open ``path`` in the server or send its new text: (uri, text, whether it changed)."""
        try:
            if os.path.getsize(path) > MAX_FILE_BYTES:
                raise LspError(f"{os.path.basename(path)} is too large for code_intel.")
            text = Path(path).read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise LspError(f"Can't read {path}: {exc.strerror or exc}") from exc
        uri = path_to_uri(path)
        with self._lock:
            document = self._documents.get(uri)
            if document is not None and document["text"] == text:
                return uri, text, False
            version = document["version"] + 1 if document else 1
            self._documents[uri] = {"version": version, "text": text}
        if document is None:
            language = self.spec.languages.get(Path(path).suffix.lower(), "plaintext")
            self.notify("textDocument/didOpen", {"textDocument": {
                "uri": uri, "languageId": language, "version": version, "text": text}})
        else:
            self.notify("textDocument/didChange", {"textDocument": {"uri": uri, "version": version},
                                                   "contentChanges": [{"text": text}]})
        return uri, text, True

    def diagnostics(self, path: str, *, cancel_event: threading.Event | None = None
                    ) -> tuple[list[dict] | None, str]:
        """(The file's diagnostics, or None when the server reported nothing in time; its text)."""
        key = _key(path)
        with self._published:
            before = self._diagnostics.get(key, {}).get("count", 0)
        uri, text, changed = self.sync(path)
        if self.capabilities.get("diagnosticProvider"):
            result = self.request("textDocument/diagnostic", {"textDocument": {"uri": uri}},
                                  cancel_event=cancel_event) or {}
            if result.get("kind") == "full" or "items" in result:
                return list(result.get("items") or []), text
        with self._published:
            if not changed and key in self._diagnostics:
                return list(self._diagnostics[key]["items"]), text
            deadline = time.monotonic() + DIAGNOSTIC_SECONDS
            while self._diagnostics.get(key, {}).get("count", 0) <= before:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not self.alive or (cancel_event is not None and cancel_event.is_set()):
                    return None, text
                self._published.wait(min(remaining, 0.25))
            # Some servers publish syntax errors first and type errors after.
            settle = time.monotonic() + SETTLE_SECONDS
            count = self._diagnostics[key]["count"]
            while time.monotonic() < settle:
                self._published.wait(settle - time.monotonic())
                if self._diagnostics[key]["count"] != count:
                    count, settle = self._diagnostics[key]["count"], time.monotonic() + SETTLE_SECONDS
            return list(self._diagnostics[key]["items"]), text


# ── The servers of every project ────────────────────────────────────────────


class LspManager:
    """One server per language per project, started when needed and stopped when idle."""

    def __init__(self):
        self._servers: dict[tuple[str, str], LanguageServer] = {}
        self._lock = threading.Lock()
        self._reaper: threading.Thread | None = None

    def server_for(self, root: str, path: str, settings: Any, *, sandbox_roots: Sequence[str] = ()) -> LanguageServer:
        from . import guardrails, os_sandbox

        spec, argv = choose(path, settings)
        reason = guardrails.blocked_argv(argv)
        if reason:
            raise LspError(guardrails.refusal(reason))
        key = (os.path.normcase(os.path.abspath(root)), spec.id)
        with self._lock:
            server = self._servers.get(key)
            if server is not None and server.state == "failed" and time.monotonic() - server.failed_at < RETRY_SECONDS:
                raise LspError(server.error)
            if server is None or server.state in ("failed", "stopped") or (server.state == "running"
                                                                            and not server.alive):
                try:
                    launch = os_sandbox.prepare_argv(argv, roots=sandbox_roots or [root], cwd=root)
                except ValueError as exc:  # the sandbox is on and can't run here
                    raise LspError(str(exc)) from exc
                server = LanguageServer(spec, root, launch)
                self._servers[key] = server
            server.last_used = time.monotonic()
            self._start_reaper()
        server.ensure_started()
        return server

    def status(self, root: str = "") -> dict[str, dict]:
        """{server id: {state, error}} for a project's servers (all projects when root is empty)."""
        wanted = os.path.normcase(os.path.abspath(root)) if root else ""
        with self._lock:
            return {spec_id: {"state": server.state, "error": server.error}
                    for (key_root, spec_id), server in self._servers.items() if not wanted or key_root == wanted}

    def _start_reaper(self) -> None:
        if self._reaper is None:
            self._reaper = threading.Thread(target=self._reap, daemon=True, name="lsp-reaper")
            self._reaper.start()

    def _reap(self) -> None:
        while True:
            time.sleep(30)
            self.stop_idle()

    def stop_idle(self, idle_seconds: float = IDLE_SECONDS) -> int:
        """Stop the servers nobody has asked anything for ``idle_seconds``."""
        with self._lock:
            idle = [(key, server) for key, server in self._servers.items()
                    if server.state == "running" and time.monotonic() - server.last_used > idle_seconds]
            for key, _ in idle:
                del self._servers[key]
        for _, server in idle:
            server.stop()
        return len(idle)

    def close(self) -> None:
        with self._lock:
            servers, self._servers = list(self._servers.values()), {}
        for server in servers:
            try:
                server.stop()
            except Exception:
                logger.debug("Stopping %s failed", server.spec.name, exc_info=True)


servers = LspManager()
atexit.register(servers.close)


# ── The tool ────────────────────────────────────────────────────────────────


CODE_INTEL_TOOLS = [{
    "type": "function",
    "function": {
        "name": TOOL_NAME,
        "description": (
            "Ask the project's language server about code: where a symbol is defined (definition), where it's "
            "used (references), its type and documentation (hover), a file's errors and warnings (diagnostics), "
            "or a file's classes and functions (symbols). More precise than grep for renames, call sites and "
            "type errors. Name the symbol and the line it's on; lines start at 1."),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": list(ACTIONS)},
                "path": {"type": "string", "description": "The file, relative to the project."},
                "line": {"type": "integer", "minimum": 1,
                         "description": "The line the symbol is on (definition, references, hover)."},
                "symbol": {"type": "string", "description": "The name on that line to ask about."},
                "column": {"type": "integer", "minimum": 1,
                           "description": "Instead of symbol: the 1-based column on that line."},
            },
            "required": ["action", "path"],
        },
    },
}]


@dataclass
class _Context:
    root: str
    roots: list[str]
    exclusions: Any

    def inside(self, path: str) -> bool:
        target = os.path.normcase(os.path.abspath(path))
        for root in self.roots:
            base = os.path.normcase(os.path.abspath(root))
            if target == base or target.startswith(base.rstrip("\\/") + os.sep):
                return True
        return False

    def hidden(self, path: str) -> bool:
        return bool(self.exclusions and self.inside(path) and self.exclusions.match(path))

    def label(self, path: str) -> str:
        if self.inside(path):
            try:
                return os.path.relpath(path, self.root).replace("\\", "/")
            except ValueError:
                pass
        return path


def _location_list(result: Any) -> list[tuple[str, dict]]:
    """(path, start position) for each Location or LocationLink in an answer."""
    items = result if isinstance(result, list) else [result] if result else []
    found = []
    for item in items:
        if not isinstance(item, dict):
            continue
        uri = item.get("targetUri") or item.get("uri")
        where = item.get("targetSelectionRange") or item.get("targetRange") or item.get("range") or {}
        if uri:
            found.append((uri_to_path(str(uri)), where.get("start") or {"line": 0, "character": 0}))
    return found


def _describe_locations(found: Iterable[tuple[str, dict]], context: _Context, *, noun: str) -> tuple[str, int]:
    lines, hidden, shown = [], 0, 0
    cache: dict[str, list[str]] = {}
    seen = set()
    for path, start in found:
        line0, units = int(start.get("line", 0)), int(start.get("character", 0))
        if (path, line0, units) in seen:
            continue
        seen.add((path, line0, units))
        if context.hidden(path):
            hidden += 1
            continue
        shown += 1
        if len(lines) >= MAX_LINES:
            continue
        text, column = "", units + 1
        if context.inside(path):  # the line itself only from project files
            if path not in cache:
                try:
                    cache[path] = _lines(Path(path).read_text(encoding="utf-8", errors="replace"))
                except OSError:
                    cache[path] = []
            content = cache[path][line0] if line0 < len(cache[path]) else ""
            column = _from_utf16(content, units) + 1
            text = "  " + content.strip()[:200] if content.strip() else ""
        lines.append(f"{context.label(path)}:{line0 + 1}:{column}{text}")
    if shown > MAX_LINES:
        lines.append(f"… and {shown - MAX_LINES} more")
    if hidden:
        lines.append(f"({hidden} in excluded files not shown)")
    if not shown and not hidden:
        return f"No {noun} found.", 0
    return "\n".join(lines), shown


def _hover_text(result: Any) -> str:
    contents = (result or {}).get("contents") if isinstance(result, dict) else None

    def flatten(value: Any) -> str:
        if isinstance(value, str):
            return value
        if isinstance(value, list):
            return "\n\n".join(part for part in (flatten(item) for item in value) if part)
        if isinstance(value, dict):
            if "language" in value:
                return f"```{value.get('language')}\n{value.get('value', '')}\n```"
            return str(value.get("value", ""))
        return ""

    text = flatten(contents).strip()
    return text[:MAX_HOVER] + ("\n…(truncated)" if len(text) > MAX_HOVER else "")


def _symbol_lines(result: Any) -> list[str]:
    lines: list[str] = []

    def kind(value: Any) -> str:
        return _SYMBOL_KINDS[value] if isinstance(value, int) and 0 < value < len(_SYMBOL_KINDS) else "symbol"

    def walk(items: list, depth: int) -> None:
        for item in items:
            if len(lines) >= MAX_SYMBOLS or not isinstance(item, dict):
                return
            where = (item.get("selectionRange") or item.get("range")
                     or (item.get("location") or {}).get("range") or {}).get("start") or {}
            detail = f" {item['detail']}" if item.get("detail") else ""
            container = f" (in {item['containerName']})" if item.get("containerName") and depth == 0 else ""
            lines.append(f"{'  ' * depth}{kind(item.get('kind'))} {item.get('name', '?')}{detail}{container}"
                         f" — line {int(where.get('line', 0)) + 1}")
            walk(item.get("children") or [], depth + 1)

    walk(result if isinstance(result, list) else [], 0)
    return lines


def _diagnostic_lines(items: list[dict], text: str) -> tuple[list[str], dict[str, int]]:
    content = _lines(text)
    counts: dict[str, int] = {}
    ordered = sorted(items, key=lambda d: ((d.get("range") or {}).get("start") or {}).get("line", 0))
    lines = []
    for item in ordered:
        start = (item.get("range") or {}).get("start") or {}
        line0 = int(start.get("line", 0))
        column = _from_utf16(content[line0], int(start.get("character", 0))) + 1 if line0 < len(content) else 1
        severity = _SEVERITIES.get(item.get("severity") or 1, "error")
        counts[severity] = counts.get(severity, 0) + 1
        origin = " ".join(str(part) for part in (item.get("source"), item.get("code")) if part not in (None, ""))
        if len(lines) < MAX_LINES:
            message = " ".join(str(item.get("message", "")).split())[:400]
            lines.append(f"{line0 + 1}:{column} {severity}: {message}" + (f" [{origin}]" if origin else ""))
    if len(ordered) > MAX_LINES:
        lines.append(f"… and {len(ordered) - MAX_LINES} more")
    return lines, counts


def code_intel(arguments: dict, *, project_path: str, settings: Any = None, exclusions: Any = None,
               sandbox_roots: Sequence[str] = (), trusted: bool = False,
               cancel_event: threading.Event | None = None) -> tuple[str, dict]:
    """Answer one ``code_intel`` call: (text for the model, metadata). Raises LspError."""
    action = str(arguments.get("action") or "")
    if action not in ACTIONS:
        raise LspError(f"Choose an action: {', '.join(ACTIONS)}.")
    path = str(arguments.get("path") or "")
    if not path:
        raise LspError("Name the file to ask about.")
    root = project_path or os.getcwd()
    path = os.path.normpath(path if os.path.isabs(path) else os.path.join(root, path))
    if not os.path.isfile(path):
        raise LspError(f"{path} isn't a file.")
    if not trusted:
        raise LspError("Language servers start only in trusted projects, because some run the project's build "
                       "scripts. Trust the project in Settings > Project trust, or use grep and file_read.")
    context = _Context(root, list(sandbox_roots) or [root], exclusions)
    server = servers.server_for(root, path, settings, sandbox_roots=sandbox_roots)
    metadata = {"server": server.spec.name, "action": action, "path": context.label(path)}

    if action == "diagnostics":
        items, text = server.diagnostics(path, cancel_event=cancel_event)
        if items is None:
            return (f"{server.spec.name} reported nothing for {context.label(path)} within "
                    f"{DIAGNOSTIC_SECONDS:.0f} seconds; this doesn't mean the file has no problems."), metadata
        lines, counts = _diagnostic_lines(items, text)
        metadata["counts"] = counts
        if not lines:
            return f"{server.spec.name} reports no problems in {context.label(path)}.", metadata
        summary = ", ".join(f"{n} {kind}{'s' if n != 1 else ''}" for kind, n in counts.items())
        return f"{context.label(path)}: {summary} ({server.spec.name})\n" + "\n".join(lines), metadata

    uri, text, _ = server.sync(path)
    if action == "symbols":
        result = server.request("textDocument/documentSymbol", {"textDocument": {"uri": uri}},
                                cancel_event=cancel_event)
        lines = _symbol_lines(result)
        metadata["count"] = len(lines)
        return ("\n".join(lines) if lines else f"{server.spec.name} lists no symbols in {context.label(path)}."), metadata

    line = arguments.get("line")
    if not isinstance(line, int) or isinstance(line, bool):
        raise LspError(f"{action} needs the line the symbol is on.")
    where = position(text, line, symbol=str(arguments.get("symbol") or ""), column=int(arguments.get("column") or 0))
    params = {"textDocument": {"uri": uri}, "position": where}
    if action == "hover":
        hover = _hover_text(server.request("textDocument/hover", params, cancel_event=cancel_event))
        return (hover or f"{server.spec.name} has nothing to say about that position."), metadata
    if action == "definition":
        found = _location_list(server.request("textDocument/definition", params, cancel_event=cancel_event))
        output, metadata["count"] = _describe_locations(found, context, noun="definition")
        return output, metadata
    params["context"] = {"includeDeclaration": True}
    found = _location_list(server.request("textDocument/references", params, cancel_event=cancel_event))
    output, metadata["count"] = _describe_locations(found, context, noun="references")
    return output, metadata


def exec_code_intel(arguments: dict, start: float, **kwargs):
    from .tools import ToolResult  # tools.py lists this definition, so import at use

    try:
        output, metadata = code_intel(arguments, **kwargs)
    except LspError as exc:
        return ToolResult(str(exc), is_error=True, elapsed=time.time() - start)
    return ToolResult(output, elapsed=time.time() - start, metadata={"code_intel": metadata})
