"""Private native child entry point; authority and durable state stay in host."""

from __future__ import annotations

from dataclasses import fields
import os
from pathlib import Path
import queue
import sys
import threading
from typing import Any

from ...gui.runtime import BackendSpec, bind_sonn_conversation
from ..exclusions import ExclusionRules
from ..execution_guard import (ExecutionGuardError, FILE_TOOL_NAMES, SWARM_TOOL_NAMES, ToolScopeRefused,
                               WRITE_TOOL_NAMES)
from ..sandbox import PathSandbox
from ..session import Session
from ..tools import AGENT_TOOLS, ToolResult
from ...connections import backend_key
from . import connections as team_connections
from .policy import NATIVE_PROVIDERS, is_connection_provider
from .process_worker import PROTOCOL_VERSION, encode_frame, read_frame, stdio_pipes as _stdio
from .tools import SWARM_WORKER_TOOLS


class _Channel:
    def __init__(self, reader, writer):
        self.reader, self.writer = reader, writer
        self.cancel = threading.Event()
        self.pause = threading.Event()
        self.closed = threading.Event()
        self._write_lock = threading.Lock()
        self._call_lock = threading.Lock()
        self._responses: queue.Queue = queue.Queue(maxsize=1)
        self._sequence = 0

    def send(self, **value):
        with self._write_lock:
            remaining = memoryview(encode_frame({"version": PROTOCOL_VERSION, **value}))
            while remaining:
                written = self.writer.write(remaining)
                if not written:
                    raise BrokenPipeError("Worker protocol output closed")
                remaining = remaining[written:]
            self.writer.flush()

    def receive(self):
        try:
            while True:
                value = read_frame(self.reader)
                if value is None:
                    return
                if (set(value) == {"version", "kind", "cancel", "paused"}
                        and value["kind"] == "control" and type(value["cancel"]) is bool and type(value["paused"]) is bool):
                    if value["cancel"]:
                        self.cancel.set()  # A later frame cannot undo cancellation.
                    if value["paused"]:
                        self.pause.set()
                    else:
                        self.pause.clear()
                elif value.get("kind") == "response" and type(value.get("id")) is int and type(value.get("ok")) is bool:
                    self._responses.put_nowait(value)
                else:
                    raise ValueError("Invalid parent protocol message")
        except BaseException:
            pass
        finally:
            self.closed.set()
            self.cancel.set()

    def call(self, operation, *args, **kwargs):
        # Native Session is sequential; no arbitrary concurrent call multiplexing.
        with self._call_lock:
            if self.closed.is_set():
                raise ExecutionGuardError("Worker host connection is closed")
            self._sequence += 1
            identity = self._sequence
            self.send(kind="rpc", id=identity, operation=operation, args=list(args), kwargs=kwargs)
            while not self.closed.is_set():
                try:
                    response = self._responses.get(timeout=.1)
                except queue.Empty:
                    continue
                if response["id"] != identity:
                    raise ExecutionGuardError("Worker host response identity does not match")
                if response["ok"] is not True:
                    if type(response.get("refused")) is str and response["refused"]:
                        raise ToolScopeRefused(response["refused"][:500])
                    raise ExecutionGuardError(str(response.get("error") or "Worker operation denied"))
                return response.get("result")
            raise ExecutionGuardError("Worker host response was not observed")


class _RemoteGuard:
    # The parent owns this actual native child's OS process group. This is a
    # host capability, never a field in model-supplied tool arguments.
    owns_process_group = True

    def __init__(self, channel, writes):
        self.channel = channel
        self.write_tools = frozenset(writes)
        self.artifact_reader = self
        self._receipt = None
        self._arguments = None

    def begin_request(self, **kwargs):
        return self.channel.call("begin_request", **kwargs)

    def end_request(self, *args, **kwargs):
        return self.channel.call("end_request", *args, **kwargs)

    def check_tool(self, *args):
        return self.channel.call("check_tool", *args)

    def begin_tool(self, *args):
        self._receipt = self.channel.call("begin_tool", *args)
        self._arguments = args[3]
        return self._receipt

    def end_tool(self, *args, **kwargs):
        return self.channel.call("end_tool", *args, **kwargs)

    def read_text_page(self, artifact_id, offset=0, limit=8000):
        arguments = self._arguments or {}
        if (arguments.get("artifact_id") != artifact_id or arguments.get("offset", 0) != offset
                or arguments.get("limit", 8000) != limit):
            raise ExecutionGuardError("Artifact read differs from its admitted arguments")
        return self.channel.call("artifact_read", self._receipt, arguments)

    def runtime_tool(self, name, arguments):
        result = self.channel.call("runtime_tool", self._receipt, name, arguments)
        if type(result) is not dict or set(result) != {"output", "is_error", "metadata"}:
            raise ExecutionGuardError("Runtime tool observation is invalid")
        return ToolResult(**result)


def _validate_initial(value: Any) -> dict[str, Any]:
    expected = {"backend", "workspace", "conversation_key", "prompt", "instructions", "role",
                "request_limit", "tools", "write_tools", "exclusions", "connection"}
    if type(value) is not dict or set(value) != expected:
        raise ValueError("Invalid worker initialization fields")
    if type(value["backend"]) is not dict or set(value["backend"]) - {field.name for field in fields(BackendSpec)}:
        raise ValueError("Invalid private backend fields")
    provider = value["backend"].get("backend_type")
    if provider in NATIVE_PROVIDERS:
        if value["connection"] is not None:
            raise ValueError("A native provider takes no connection")
    elif is_connection_provider(provider):
        # The host captured and checked it (workers.py); check it again here,
        # since this process builds the backend from it.
        if type(value["connection"]) is not dict:
            raise ValueError("Worker requires its captured connection")
        value["connection"] = team_connections.team_connection(value["connection"])
        if backend_key(value["connection"]["id"]) != provider:
            raise ValueError("Connection differs from the captured provider")
    else:
        raise ValueError("Worker requires an explicitly selected native provider or connection")
    if not value["backend"].get("model"):
        raise ValueError("Worker requires an explicit model")
    for key in ("workspace", "conversation_key", "prompt", "instructions", "role"):
        if type(value[key]) is not str:
            raise ValueError("Worker initialization text is invalid")
    if not Path(value["workspace"]).is_absolute() or not Path(value["workspace"]).is_dir():
        raise ValueError("Worker workspace is unavailable")
    if type(value["request_limit"]) is not int or not 1 <= value["request_limit"] <= 10000:
        raise ValueError("Worker request allowance is invalid")
    for key in ("tools", "write_tools"):
        if type(value[key]) is not list or any(type(name) is not str for name in value[key]):
            raise ValueError("Worker tool capability is invalid")
    if not set(value["tools"]) <= FILE_TOOL_NAMES | WRITE_TOOL_NAMES | SWARM_TOOL_NAMES:
        raise ValueError("Unsupported worker tool")
    if not set(value["write_tools"]) <= WRITE_TOOL_NAMES or not set(value["write_tools"]) <= set(value["tools"]):
        raise ValueError("Invalid writer capability")
    # The project's file exclusions as [pattern, source] pairs (workers.py).
    rules = value["exclusions"]
    if (type(rules) is not list or len(rules) > 10000
            or any(type(rule) is not list or len(rule) != 2
                   or any(type(part) is not str or not part or len(part) > 4096 for part in rule)
                   for rule in rules)):
        raise ValueError("Worker file exclusions are invalid")
    return value


def main(*, backend_factory=None) -> int:
    """Execute one private handshake. Test factories are trusted Python inputs."""
    reader, writer = _stdio()
    # Third-party print/log output must never enter the protocol or startup logs.
    sink = open(os.devnull, "w", encoding="utf-8")
    sys.stdout = sys.stderr = sink
    channel = _Channel(reader, writer)
    backend = stream = None
    cleanup_ok = True
    try:
        channel.send(kind="ready")
        first = read_frame(reader)
        if first is None or set(first) != {"version", "kind", "payload"} or first["kind"] != "init":
            raise ValueError("Worker initialization was not received")
        initial = _validate_initial(first["payload"])
        threading.Thread(target=channel.receive, daemon=True, name="swarm-host-control").start()
        spec = BackendSpec.from_dict(initial["backend"])
        if backend_factory:
            backend = backend_factory(spec)
        elif initial["connection"] is not None:
            backend = team_connections.create_backend(initial["connection"], spec)
        else:
            backend = spec.create_backend()
        if (getattr(backend, "name", None), getattr(backend, "model", None)) != (spec.backend_type, spec.model):
            raise ValueError("Constructed provider differs from the captured model")
        backend._supervised_single_request = True
        bind_sonn_conversation(backend, initial["workspace"], initial["conversation_key"])
        guard = _RemoteGuard(channel, initial["write_tools"])
        names = set(initial["tools"])
        schemas = [tool for tool in AGENT_TOOLS + SWARM_WORKER_TOOLS if tool["function"]["name"] in names]
        handlers = {name: (lambda args, name=name: guard.runtime_tool(name, args)) for name in names & SWARM_TOOL_NAMES}
        session = Session(backend, execution_guard=guard, allowed_tools=schemas, guarded_tool_handlers=handlers,
            project_instructions=initial["instructions"], role_instructions=initial["role"], prompt_role="subagent",
            max_model_requests=initial["request_limit"], cancel_event=channel.cancel, pause_event=channel.pause)
        session.project_path = initial["workspace"]
        session.sandbox = PathSandbox(initial["workspace"], enabled=True)
        session.exclusions = ExclusionRules(initial["workspace"], rules=[tuple(rule) for rule in initial["exclusions"]])
        # Organization oversight (lumi/oversight.py) admits and records it as team work.
        session.oversight_trigger = "team"
        queued = set()
        def collect():
            if channel.cancel.is_set():
                return
            messages = channel.call("collect")
            for message in messages:
                if type(message) is not dict or set(message) != {"kind", "id", "text"} or message["kind"] not in {"peer", "owner"}:
                    raise ExecutionGuardError("Invalid generated guidance envelope")
                identity = (message["kind"], message["id"])
                if identity not in queued and session.steer(message["text"],
                        message_id=message["id"] if message["kind"] == "peer" else "", input_origin="generated"):
                    queued.add(identity)
        collect()
        stream = session.run(initial["prompt"], input_origin="generated")
        for event in stream:
            channel.send(kind="event", event=event)
            if event.get("event") == "step.end":
                collect()
    except BaseException as exc:
        try:
            channel.send(kind="event", event={"event": "error", "message": f"Native worker failed ({type(exc).__name__})"})
        except BaseException:
            pass
    finally:
        for resource in (stream, backend):
            close = getattr(resource, "close", None)
            if callable(close):
                try:
                    close()
                except BaseException:
                    cleanup_ok = False
        if cleanup_ok:
            try:
                channel.send(kind="closed")
            except BaseException:
                cleanup_ok = False
        # A daemon receiver may still hold stdin's read lock. Process exit owns
        # that descriptor; do not block exit by closing it on this thread.
        writer.close()
    return 0 if cleanup_ok else 1
