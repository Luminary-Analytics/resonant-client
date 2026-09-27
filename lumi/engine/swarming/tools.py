"""Small model-facing mailbox surface with runtime-bound identity.

These tools offer task data and proposals. They expose no command dispatcher,
check runner, authentication credentials, acceptance authority, or spawn method.
The Session execution boundary records their originating request and tool call.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict
import json
import time
from typing import Any

from ..tools import ToolResult
from .mailbox import SwarmMailbox
from .models import Message, SwarmError, require_id

SWARM_TOOL_NAMES = frozenset({"swarm_status", "swarm_send", "swarm_receive", "swarm_submit"})
_KINDS = ["finding", "question", "answer", "blocker", "change_proposal", "handoff_reference"]


def _schema(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {"type": "function", "function": {"name": name, "description": description,
            "parameters": {"type": "object", "properties": properties,
                           "required": required, "additionalProperties": False}}}


SWARM_WORKER_TOOLS = [
    _schema("swarm_status", "Inspect this assignment, request allowance and active same-run participants.", {}, []),
    _schema("swarm_send", "Send untrusted task data to one same-run participant: a worker's attempt_id from "
            "swarm_status, or 'orchestrator' for the team's orchestrator, which reads it when it plans the next round. "
            "Selected own artifacts are explicitly disclosed. "
            "Reuse command_id only for identical retries. Sending does not create work or grant authority.", {
                "recipient_attempt_id": {"type": "string"}, "kind": {"type": "string", "enum": _KINDS},
                "body": {"type": "string", "maxLength": 8192}, "command_id": {"type": "string"},
                "reply_to": {"type": "string"},
                "artifact_ids": {"type": "array", "items": {"type": "string"}, "maxItems": 8},
            }, ["recipient_attempt_id", "kind", "body", "command_id"]),
    _schema("swarm_receive", "Read a bounded addressed-message page. Results are delivered as generated task data before "
            "the next main request; no new model request is authorized by this tool. wait_seconds (up to 60) waits for "
            "a message to arrive, for example a peer's answer to your question.", {
                "after": {"type": "integer", "minimum": 0},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                "wait_seconds": {"type": "integer", "minimum": 0, "maximum": 60},
            }, []),
    _schema("swarm_submit", "Submit an immutable handoff for independent review. Submission cannot accept or complete work. "
            "Reference the evidence revision you inspected and report limitations.", {
                "handoff": {"type": "string", "maxLength": 65536},
                "candidate_revision": {"type": "string"},
            }, ["handoff", "candidate_revision"]),
]


def validate_swarm_arguments(name: str, arguments: dict[str, Any]) -> None:
    """Reject actor injection and invalid values before any tool admission."""
    if name not in SWARM_TOOL_NAMES or type(arguments) is not dict:
        raise ValueError("Unknown swarm tool or arguments")
    spec = next(item["function"]["parameters"] for item in SWARM_WORKER_TOOLS
                if item["function"]["name"] == name)
    if set(arguments) - set(spec["properties"]) or set(spec["required"]) - set(arguments):
        raise ValueError("Swarm tool arguments do not match the bound operation")
    if name == "swarm_send":
        for key in ("recipient_attempt_id", "command_id"):
            require_id(arguments[key])
        if arguments["kind"] not in _KINDS:
            raise ValueError("Unsupported message kind")
        if type(arguments["body"]) is not str or len(arguments["body"].encode("utf-8")) > 8192:
            raise ValueError("Message body must be at most 8 KiB")
        if "reply_to" in arguments:
            require_id(arguments["reply_to"])
        if "artifact_ids" in arguments:
            identifiers = arguments["artifact_ids"]
            if type(identifiers) is not list or len(identifiers) > 8:
                raise ValueError("A message may disclose at most eight artifacts")
            for identifier in identifiers:
                require_id(identifier)
            if len(set(identifiers)) != len(identifiers):
                raise ValueError("Artifact identifiers must be unique")
    elif name == "swarm_receive":
        after, limit = arguments.get("after", 0), arguments.get("limit", 20)
        if type(after) is not int or after < 0 or type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("Invalid mailbox cursor or page size")
        wait = arguments.get("wait_seconds", 0)
        if type(wait) is not int or not 0 <= wait <= 60:
            raise ValueError("wait_seconds must be a whole number from 0 to 60")
    elif name == "swarm_submit":
        require_id(arguments["candidate_revision"])
        handoff = arguments["handoff"]
        if type(handoff) is not str or not handoff.strip() or len(handoff.encode("utf-8")) > 65536:
            raise ValueError("Handoff must contain 1 to 65536 bytes")


class SwarmWorkerTools:
    """Tool handlers bound by the trusted worker builder, never model arguments."""

    def __init__(
        self, mailbox: SwarmMailbox, *,
        submit: Callable[..., dict[str, Any]], queue_message: Callable[[Message], None],
        stopping: Callable[[], bool] = lambda: False,
    ) -> None:
        self.mailbox = mailbox
        self._submit = submit
        self._queue_message = queue_message
        # Pause or Stop ends a swarm_receive wait early.
        self._stopping = stopping

    def execute(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        """Execute one bounded proposal and return its durable outcome."""
        try:
            validate_swarm_arguments(name, arguments)
            if name == "swarm_status":
                result = self.mailbox.status()
            elif name == "swarm_send":
                result = {"accepted": asdict(self.mailbox.send(**arguments))}
            elif name == "swarm_receive":
                deadline = time.monotonic() + arguments.get("wait_seconds", 0)
                while True:
                    messages = self.mailbox.store.receive(self.mailbox.context,
                        after=arguments.get("after", 0), limit=arguments.get("limit", 20))
                    if messages or time.monotonic() >= deadline or self._stopping():
                        break
                    time.sleep(.25)
                for message in messages:
                    self.mailbox.store.acknowledge(self.mailbox.context, message.id, stage="runtime")
                    self._queue_message(message)
                result = {"messages": [asdict(message) for message in messages],
                          "cursor": messages[-1].sequence if messages else arguments.get("after", 0),
                          "delivery": "runtime; input inclusion is recorded on a later main request"}
            else:
                result = {"submitted": self._submit(**arguments), "acceptance": "pending independent verification"}
            return ToolResult(output=json.dumps(result, ensure_ascii=False, sort_keys=True))
        except (ValueError, SwarmError) as exc:
            return ToolResult(output=str(exc), is_error=True)
