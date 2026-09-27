"""Fail-closed native request and file-tool boundaries for supervised sessions.

The injected guard owns durable authority and accounting. This adapter owns the
ordering contract: persist admission before invocation, settle fully drained
requests before tools, and persist observations before publishing results. It
does not authenticate callers or turn a tool allowlist into a filesystem sandbox.
"""

from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Callable, Iterator
from typing import Any, Literal, Protocol

RequestPurpose = Literal["primary", "planning", "compression"]
ObservationOutcome = Literal["completed", "uncertain"]
FILE_TOOL_NAMES = frozenset({"file_read", "glob", "grep", "artifact_read"})
WRITE_TOOL_NAMES = frozenset({"file_write", "file_edit"})
SWARM_TOOL_NAMES = frozenset({"swarm_status", "swarm_send", "swarm_receive", "swarm_submit"})


class ExecutionGuardError(RuntimeError):
    """Admission is closed or its durable outcome cannot be established."""


class ToolScopeRefused(ExecutionGuardError):
    """One tool call was refused before admission; admission stays open.

    Nothing ran and no receipt exists, so the model hears the reason as the
    call's error result and may narrow it, as with an ordinary sandbox denial.
    Only lexical scope, sandbox and allowlist checks raise it: lost admission,
    integrity and persistence failures still close the boundary.
    """


class ExecutionGuard(Protocol):
    """Trusted, attempt-bound persistence interface; never model-supplied."""

    artifact_reader: Any
    """Attempt-bound artifact view; None disables artifact_read entirely."""

    def begin_request(self, *, purpose: RequestPurpose, inputs: dict[str, Any]) -> str:
        """Commit request identity, input assignment and allowance before use."""
        ...

    def end_request(
        self, request_id: str, *, outcome: ObservationOutcome,
        usage: dict[str, Any] | None, error: str,
    ) -> None:
        """Persist observed completion or retain uncertain execution/accounting."""
        ...

    def check_tool(self, request_id: str, name: str, arguments: dict[str, Any]) -> None:
        """Recheck current authority and tool scope without admitting an effect."""
        ...

    def begin_tool(
        self, request_id: str, call_id: str, name: str,
        arguments: dict[str, Any], arguments_sha256: str,
    ) -> str:
        """Commit an action receipt bound to the originating model request."""
        ...

    def end_tool(
        self, receipt_id: str, *, outcome: ObservationOutcome, output: str,
        is_error: bool, metadata: dict[str, Any],
    ) -> None:
        """Persist the actual observation before it enters outward completion."""
        ...


class ExecutionBoundary:
    """Per-session adapter that never recovers a failed admission implicitly."""

    def __init__(self, guard: ExecutionGuard, *, runtime_tools: set[str] | None = None) -> None:
        self.guard = guard
        self.runtime_tools = frozenset(runtime_tools or ())
        if not self.runtime_tools <= SWARM_TOOL_NAMES:
            raise ValueError("Only fixed scoped swarm handlers may extend guarded tools")
        write_tools = getattr(guard, "write_tools", frozenset())
        if type(write_tools) is not frozenset or not write_tools <= WRITE_TOOL_NAMES:
            raise ValueError("Guarded writes require an explicit trusted file-write capability")
        self.file_tools = FILE_TOOL_NAMES | write_tools
        self.closed = False
        self.request_id = ""
        self._active_request: str | None = None
        self._request_ids: set[str] = set()
        self._completed_requests: set[str] = set()
        self._tool_calls: set[tuple[str, str]] = set()
        self._request_models: dict[str, dict[str, str]] = {}

    def reject(self, message: str) -> None:
        """Close local admission; resuming requires explicit runtime recovery."""
        self.closed = True
        raise ExecutionGuardError(message)

    def refuse(self, message: str) -> None:
        """Refuse one tool call before admission, leaving the boundary open."""
        raise ToolScopeRefused(message)

    def ensure_open(self) -> None:
        """Prevent later work after denial, uncertainty or failed persistence."""
        if self.closed:
            raise ExecutionGuardError("Execution admission is closed; reconcile before continuing")

    def check_backend(self, backend: Any) -> None:
        """Only native backends can honor this file-only execution contract."""
        self.ensure_open()
        if getattr(backend, "handles_tools", False):
            self.reject("Guarded execution requires a native backend; CLI tool loops are unsupported")
        from ..backends import KimiBackend, OllamaBackend
        if isinstance(backend, (OllamaBackend, KimiBackend)):
            # One durable request allowance must not hide additional provider
            # generations inside a transport's ordinary retry loop.
            backend._supervised_single_request = True

    def _persist(self, method: str, *args: Any, **kwargs: Any) -> Any:
        # End observations must remain possible after admission closes. Never
        # retry a callback: its commit may have succeeded before an I/O failure.
        try:
            return getattr(self.guard, method)(*args, **kwargs)
        except ToolScopeRefused:
            raise  # Refused before admission: nothing ran, so nothing to reconcile.
        except Exception as exc:
            self.closed = True
            raise ExecutionGuardError(f"Execution guard {method} failed: {exc}") from exc
        except BaseException:
            self.closed = True
            raise

    def _begin(self, backend: Any, purpose: RequestPurpose, inputs: dict[str, Any]) -> str:
        self.check_backend(backend)
        if self._active_request is not None:
            self.reject("A guarded session already owns an active model request")
        if "_model_selection" in inputs:
            self.reject("Model selection is runtime-owned and cannot be supplied in request inputs")
        selection = {"provider": getattr(backend, "name", None), "model": getattr(backend, "model", None)}
        if any(not isinstance(value, str) or not value.strip() for value in selection.values()):
            self.reject("Guarded requests require an explicit provider and model identity")
        captured_inputs = {**copy.deepcopy(inputs), "_model_selection": selection}
        request_id = self._persist("begin_request", purpose=purpose, inputs=copy.deepcopy(captured_inputs))
        if not isinstance(request_id, str) or not request_id.strip() or request_id in self._request_ids:
            self.reject("Execution guard returned an invalid or reused model request identity")
        self._request_ids.add(request_id)
        self._request_models[request_id] = dict(selection)
        self._active_request = request_id
        if purpose == "primary":
            self.request_id = request_id
        return request_id

    def _check_model_selection(self, backend: Any, request_id: str) -> None:
        actual = {"provider": getattr(backend, "name", None), "model": getattr(backend, "model", None)}
        if actual != self._request_models[request_id]:
            self.reject("Backend selection changed after durable request admission")

    def stream(
        self, backend: Any, *, purpose: RequestPurpose,
        inputs: dict[str, Any], invoke: Callable[[], Iterator[tuple[str, dict]]],
    ) -> Iterator[tuple[str, dict]]:
        """Settle only a full iterator drain with a done marker and no failure."""
        request_id = self._begin(backend, purpose, inputs)
        iterator = None
        iterator_closed = False
        end_attempted = False
        saw_done = False
        usage = None
        error = "Model stream closed before its complete response was observed"
        try:
            self._check_model_selection(backend, request_id)
            iterator = iter(invoke())
            for event_type, data in iterator:
                if event_type in {"error", "cancelled"}:
                    raise ExecutionGuardError(str(data.get("message") or "Provider failed or cancelled"))
                if event_type == "done":
                    saw_done = True
                    usage = copy.deepcopy(data.get("stats"))
                yield event_type, data
            if not saw_done:
                raise ExecutionGuardError("Provider stream ended without a done observation")
            close = getattr(iterator, "close", None)
            if callable(close):
                close()
            iterator_closed = True
            end_attempted = True
            self._persist("end_request", request_id, outcome="completed", usage=usage, error="")
            self._completed_requests.add(request_id)
        except BaseException as exc:
            self.closed = True
            if not isinstance(exc, GeneratorExit):
                # Provider exception strings can include credential-bearing
                # URLs. The ledger records failure class, not raw diagnostics.
                error = f"Model response could not be fully observed ({type(exc).__name__})"
            raise
        finally:
            self._active_request = None
            try:
                if iterator is not None and not iterator_closed:
                    close = getattr(iterator, "close", None)
                    if callable(close):
                        close()
            finally:
                if not end_attempted:
                    self.closed = True
                    self._persist("end_request", request_id, outcome="uncertain", usage=usage, error=error)

    def classify(self, backend: Any, prompt: str, *, max_tokens: int) -> str:
        """Account for the synchronous auxiliary planning request separately."""
        request_id = self._begin(backend, "planning", {"prompt": prompt, "max_tokens": max_tokens})
        end_attempted = False
        error = "Classification did not return a complete response"
        try:
            self._check_model_selection(backend, request_id)
            result = backend.classify(prompt, max_tokens=max_tokens)
            if not isinstance(result, str):
                raise ExecutionGuardError("Classification returned an invalid response")
            end_attempted = True
            self._persist("end_request", request_id, outcome="completed", usage=None, error="")
            self._completed_requests.add(request_id)
            return result
        except BaseException as exc:
            self.closed = True
            error = f"Classification response could not be fully observed ({type(exc).__name__})"
            raise
        finally:
            self._active_request = None
            if not end_attempted:
                self._persist("end_request", request_id, outcome="uncertain", usage=None, error=error)

    def check_tool(
        self, name: str, arguments: dict[str, Any], *, allowed_names: set[str] | None,
    ) -> None:
        """Check all names before hooks and special handlers can execute."""
        self.ensure_open()
        if name not in self.file_tools | self.runtime_tools or (allowed_names is not None and name not in allowed_names):
            self.refuse(f"Tool '{name}' is outside this guarded session's explicit allowlist")
        if self.request_id not in self._completed_requests:
            self.reject("Tools require a durably completed originating model request")
        self._persist("check_tool", self.request_id, name, copy.deepcopy(arguments))

    def execute_tool(
        self, name: str, call_id: str, arguments: dict[str, Any], *,
        allowed_names: set[str] | None, invoke: Callable[[], Any],
    ) -> Any:
        """Bind and observe one native file result before returning it."""
        self.check_tool(name, arguments, allowed_names=allowed_names)
        if not isinstance(call_id, str) or not call_id.strip():
            self.reject("Guarded tools require an originating tool call identity")
        identity = (self.request_id, call_id)
        if identity in self._tool_calls:
            self.reject("A tool call identity cannot be executed twice")
        digest = hashlib.sha256(json.dumps(
            arguments, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False,
        ).encode("utf-8")).hexdigest()
        receipt_id = self._persist("begin_tool", self.request_id, call_id, name,
                                   copy.deepcopy(arguments), digest)
        if not isinstance(receipt_id, str) or not receipt_id.strip():
            self.reject("Execution guard returned an invalid action receipt")
        self._tool_calls.add(identity)
        try:
            result = invoke()
            output, is_error, metadata = result.output, result.is_error, copy.deepcopy(result.metadata)
            if not isinstance(output, str) or type(is_error) is not bool or not isinstance(metadata, dict):
                raise ExecutionGuardError("Tool returned a malformed observation")
            result.metadata = {**metadata, "model_request_id": self.request_id,
                               "action_receipt_id": receipt_id, "arguments_sha256": digest}
        except BaseException as exc:
            self.closed = True
            self._persist("end_tool", receipt_id, outcome="uncertain", output="", is_error=True,
                          metadata={"error": f"Tool observation failed ({type(exc).__name__})"})
            raise
        self._persist("end_tool", receipt_id, outcome="completed", output=output,
                      is_error=is_error, metadata=metadata)
        return result
