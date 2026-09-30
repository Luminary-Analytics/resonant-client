"""Scripted Anthropic Messages and OpenAI Responses servers for Team tests.

A loopback HTTP server that answers both APIs as the providers stream them:
server-sent events, tool calls with streamed arguments, signed thinking
(Anthropic) or encrypted reasoning (OpenAI), and usage. A test gives it a
``script``: a function from what a request asked (a ``Turn``) to the ``Reply``
it gets. Every request is recorded (path, headers, body), so tests can check
what Lumi's adapters (lumi/anthropic_api.py, lumi/openai_api.py) sent.

A request without the fixture key (``x-api-key``, ``api-key`` or a bearer
token) is refused with 401, and a Messages request whose history holds tool
calls but defines no tools with 400, as the providers would.

A Messages request is also checked as the API checks it for the models in
``CLAUDE_RULES`` (thinking, effort, output length, sampling and forced tool
choice; a 400 with the API's own wording otherwise), and every signed
thinking block it replays must come back unchanged, in the place the
response had it. A model that thinks without being asked answers with
thinking at the default level too. With ``enforce_prefix`` set, the models
that run preserved thinking also refuse a block whose conversation changed
before it, as the API does for accounts created on or after 2026-08-31. That
check is written from Anthropic's description of it (the claude-api skill's
preserved-thinking guide), not from Lumi's own check in
lumi/anthropic_api.py, so a test can catch the two disagreeing.

NOT a test file: test files import it.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import time
from typing import Any, Callable

KEY = "fixture-provider-key"
# Every reply reports the same usage, so a test can price it.
ANTHROPIC_USAGE = {"input_tokens": 1000, "cache_read_input_tokens": 200, "cache_creation_input_tokens": 100}
OPENAI_USAGE = {"input_tokens": 1000, "input_tokens_details": {"cached_tokens": 200},
                "output_tokens": 50, "output_tokens_details": {"reasoning_tokens": 10}}
OUTPUT_TOKENS = 50


@dataclass(frozen=True)
class ClaudeRules:
    """What the Messages API accepts from one Claude model.

    From Anthropic's Thinking and Effort documentation (2026-09-30), written
    out here rather than taken from lumi/claude_models.py, so the stub checks
    Lumi's requests instead of repeating them.
    """

    adaptive: bool          # thinking {"type": "adaptive"}
    budget: bool            # thinking {"type": "enabled", "budget_tokens": N}
    disabled: str           # thinking {"type": "disabled"}: "yes", "no" or "high" (at effort high or below)
    between_tools: bool     # thinking {"type": "between_tools"} (Sonnet 5.5, at effort high or below)
    efforts: frozenset      # output_config.effort values accepted; empty: effort refused
    sampling: bool          # a non-default temperature, top_p or top_k (never with thinking)
    forced_tools: bool      # tool_choice "any" or "tool"
    preserved: bool         # runs preserved thinking's check that the conversation before a block is unchanged
    thinks_by_default: bool = False   # thinks when a request has no thinking field
    max_output: int = 128_000         # the largest max_tokens accepted


FIVE_EFFORTS = frozenset({"low", "medium", "high", "xhigh", "max"})
_ALWAYS_ON = ClaudeRules(True, False, "no", False, FIVE_EFFORTS, False, False, True, thinks_by_default=True)
_OPUS_47 = ClaudeRules(True, False, "yes", False, FIVE_EFFORTS, False, True, False)
_ADAPTIVE_46 = ClaudeRules(True, True, "yes", False, frozenset({"low", "medium", "high", "max"}), True, True, False)
_BUDGET_ONLY = ClaudeRules(False, True, "yes", False, frozenset(), True, True, False, max_output=64_000)
CLAUDE_RULES: dict[str, ClaudeRules] = {
    "claude-opus-5-5": _ALWAYS_ON,
    "claude-fable-5-1": _ALWAYS_ON,
    "claude-fable-5": replace(_ALWAYS_ON, forced_tools=True, preserved=False),
    "claude-sonnet-5-5": replace(_ALWAYS_ON, between_tools=True),
    "claude-opus-5": replace(_OPUS_47, disabled="high", thinks_by_default=True),
    "claude-sonnet-5": replace(_OPUS_47, thinks_by_default=True),
    "claude-opus-4-8": _OPUS_47,
    "claude-opus-4-7": _OPUS_47,
    "claude-opus-4-6": _ADAPTIVE_46,
    "claude-sonnet-4-6": _ADAPTIVE_46,
    "claude-opus-4-5-20251101": replace(_BUDGET_ONLY, efforts=frozenset({"low", "medium", "high"})),
    "claude-sonnet-4-5-20250929": _BUDGET_ONLY,
    "claude-haiku-4-5-20251001": _BUDGET_ONLY,
    "claude-opus-4-1-20250805": replace(_BUDGET_ONLY, max_output=32_000),
    "claude-3-5-haiku-20241022": ClaudeRules(False, False, "yes", False, frozenset(), True, True, False,
                                             max_output=8_192),
}
THINKING_TYPES = frozenset({"thinking", "redacted_thinking"})


def _without_cache_control(value: Any) -> Any:
    """``value`` with its cache_control markers left out (they may move between requests)."""
    if isinstance(value, dict):
        return {key: _without_cache_control(item) for key, item in value.items() if key != "cache_control"}
    if isinstance(value, list):
        return [_without_cache_control(item) for item in value]
    return value


def _compared(content: Any) -> list:
    """Content as preserved thinking compares it.

    From the claude-api skill (shared/preserved-thinking-migration.md): the
    check ignores cache_control markers, a string versus a single text block,
    leading and trailing whitespace of a text block, whitespace-only text
    blocks, key order and the thinking blocks themselves; everything else
    counts.
    """
    blocks = [{"type": "text", "text": content}] if isinstance(content, str) else list(content or [])
    kept = []
    for block in blocks:
        if not isinstance(block, dict) or block.get("type") in THINKING_TYPES:
            continue
        block = _without_cache_control(block)
        if block.get("type") == "text":
            text = str(block.get("text") or "").strip()
            if not text:
                continue
            block = {**block, "text": text}
        kept.append(block)
    return kept


def conversation_before(body: dict, message: int, position: int) -> str:
    """What preserved thinking binds the block at ``messages[message].content[position]`` to.

    The top-level system prompt, the tools (compared as a set, by name) and
    every message and block before it, as _compared sees them. A block's
    place in the chain of thinking blocks is checked separately.
    """
    messages = body.get("messages") or []
    current = messages[message].get("content") if message < len(messages) else []
    tools = sorted((_without_cache_control(tool) for tool in body.get("tools") or []),
                   key=lambda tool: str(tool.get("name")))
    return json.dumps({
        "system": _compared(body.get("system")),
        "tools": tools,
        "messages": [{"role": entry.get("role"), "content": _compared(entry.get("content"))}
                     for entry in messages[:message]],
        "before": _compared(current[:position] if isinstance(current, list) else []),
    }, sort_keys=True)


def _thinking_before(body: dict, message: int, position: int, model_of: Callable[[str], str | None],
                     model: str) -> str | None:
    """The key of the last thinking block ``model`` made before a position of ``body``, or None."""
    last = None
    for i, entry in enumerate(body.get("messages") or []):
        content = entry.get("content") if isinstance(entry.get("content"), list) else []
        for j, block in enumerate(content):
            if (i, j) >= (message, position):
                return last
            if isinstance(block, dict) and block.get("type") in THINKING_TYPES:
                key = str(block.get("signature") or block.get("data") or "")
                if model_of(key) == model:
                    last = key
    return last


def claude_refusal(rules: ClaudeRules, body: dict) -> str:
    """Why the Messages API refuses ``body`` for a model with ``rules``, in its words, or ''."""
    thinking = body.get("thinking") if isinstance(body.get("thinking"), dict) else None
    kind = (thinking or {}).get("type")
    effort = (body.get("output_config") or {}).get("effort")
    tool_choice = (body.get("tool_choice") or {}).get("type")
    max_tokens = int(body.get("max_tokens") or 0)
    if max_tokens > rules.max_output:
        return (f"max_tokens: {max_tokens} > {rules.max_output}, which is the maximum allowed number of output "
                f"tokens for {body.get('model') or 'this model'}")
    if kind not in {None, "enabled", "adaptive", "disabled", "between_tools"}:
        return f"thinking.type: Input tag '{kind}' found using 'type' does not match any of the expected tags"
    if kind == "enabled" and not rules.budget:
        return ('"thinking.type.enabled" is not supported for this model. Use "thinking.type.adaptive" '
                'and "output_config.effort" to control thinking behavior.')
    if kind == "enabled":
        budget = (thinking or {}).get("budget_tokens")
        if not isinstance(budget, int) or budget < 1024:
            return "thinking.enabled.budget_tokens: Input should be greater than or equal to 1024"
        if budget >= int(body.get("max_tokens") or 0):
            return "`max_tokens` must be greater than `thinking.budget_tokens`."
        if tool_choice in {"any", "tool"}:
            return "Thinking may not be enabled when tool_choice forces tool use."
    if kind == "adaptive" and not rules.adaptive:
        return '"thinking.type.adaptive" is not supported for this model.'
    if kind == "disabled" and (rules.disabled == "no" or (rules.disabled == "high" and effort in {"xhigh", "max"})):
        if rules.between_tools:
            return ('"thinking.type.disabled" is not supported for this model. Use "thinking.type.between_tools" '
                    'for the lowest thinking setting, or "thinking.type.adaptive" and "output_config.effort" to '
                    'control thinking behavior.')
        if rules.disabled == "high":
            return (f"output_config.effort '{effort}' is not supported when thinking is disabled on this model. "
                    "Use effort 'high' or below, or enable thinking.")
        return ('"thinking.type.disabled" is not supported for this model. Use "thinking.type.adaptive" and '
                '"output_config.effort" to control thinking behavior.')
    if kind == "between_tools":
        if not rules.between_tools:
            return '"thinking.type.between_tools" is not supported for this model.'
        if set(thinking or {}) != {"type"}:
            return "thinking.between_tools: Extra inputs are not permitted"
        if effort in {"xhigh", "max"}:
            return (f"output_config.effort '{effort}' is not supported when thinking is disabled on this model. "
                    "Use effort 'high' or below, or enable thinking.")
    if effort is not None and effort not in rules.efforts:
        return (f"output_config.effort: '{effort}' is not supported for this model." if rules.efforts
                else "output_config.effort: effort is not supported for this model.")
    if any(name in body for name in ("temperature", "top_p", "top_k")) and (not rules.sampling or kind == "enabled"):
        return "temperature, top_p and top_k are not supported for this model."
    if tool_choice in {"any", "tool"} and not rules.forced_tools:
        return 'tool_choice: type "tool" and "any" are not supported for this model.'
    if kind == "enabled":
        # With a fixed budget, the last assistant turn of an open tool loop starts with thinking.
        messages = body.get("messages") or []
        last = max((index for index, message in enumerate(messages) if message.get("role") == "assistant"),
                   default=None)
        if last is not None and last + 1 < len(messages):
            content = messages[last].get("content") or []
            follows = messages[last + 1].get("content") or []
            if (any(block.get("type") == "tool_use" for block in content)
                    and any(block.get("type") == "tool_result" for block in follows)
                    and content[0].get("type") not in {"thinking", "redacted_thinking"}):
                return (f"messages.{last}.content.0.type: Expected `thinking` or `redacted_thinking`, but found "
                        f"`{content[0].get('type')}`. When `thinking` is enabled, a final `assistant` message must "
                        "start with a thinking block.")
    return ""


@dataclass
class Turn:
    """What one request asked: its protocol, model, the user text and the tool results so far."""

    protocol: str
    model: str
    prompt: str
    results: list[str]
    body: dict

    @property
    def step(self) -> int:
        """How many tool calls this participant has made so far."""
        return len(self.results)


@dataclass
class Reply:
    """One streamed answer: prose, a tool call, reasoning; or a hold, or an HTTP error."""

    text: str = ""
    tool: tuple[str, dict] | None = None
    thinking: str = ""
    # Keep the stream open with pings (Anthropic) until this is set or the client leaves.
    hold: threading.Event | None = None
    status: int = 200
    extra: dict = field(default_factory=dict)
    # Anthropic only: the response's content blocks in order, in place of
    # thinking, text and tool: ("thinking", text) with "" for the empty text
    # of the default display, ("redacted", ""), ("text", text) and
    # ("tool", (name, arguments)).
    blocks: tuple = ()

    def anthropic_blocks(self) -> list[tuple[str, Any]]:
        if self.blocks:
            return list(self.blocks)
        return ([("thinking", self.thinking)] if self.thinking else []) + \
            ([("text", self.text)] if self.text else []) + ([("tool", self.tool)] if self.tool else [])


def _anthropic_turn(body: dict) -> Turn:
    prompt, results = [], []
    for message in body.get("messages") or []:
        for block in message.get("content") or []:
            if message.get("role") == "user" and block.get("type") == "text":
                prompt.append(str(block.get("text") or ""))
            elif block.get("type") == "tool_result":
                content = block.get("content")
                results.append(content if isinstance(content, str) else json.dumps(content))
    return Turn("anthropic", str(body.get("model") or ""), "\n".join(prompt), results, body)


def _openai_turn(body: dict) -> Turn:
    prompt, results = [], []
    for item in body.get("input") or []:
        if item.get("role") == "user":
            prompt += [str(part.get("text") or "") for part in item.get("content") or []
                       if part.get("type") == "input_text"]
        elif item.get("type") == "function_call_output":
            results.append(str(item.get("output") or ""))
    return Turn("openai", str(body.get("model") or ""), "\n".join(prompt), results, body)


def _anthropic_events(reply: Reply, number: int, model: str) -> tuple[list[dict], list[dict]]:
    """The response's stream events, and its content blocks as a client sends them back."""
    message_id = f"msg_fixture_{number}"
    events = [{"type": "message_start", "message": {"id": message_id, "type": "message", "role": "assistant",
               "model": model, "content": [], "usage": {**ANTHROPIC_USAGE, "output_tokens": 1}}}]
    content: list[dict] = []
    index = 0

    def block(start: dict, *deltas: dict) -> None:
        nonlocal index
        events.append({"type": "content_block_start", "index": index, "content_block": start})
        events.extend({"type": "content_block_delta", "index": index, "delta": delta} for delta in deltas)
        events.append({"type": "content_block_stop", "index": index})
        index += 1

    counts = {"thinking": 0, "tool": 0}
    for kind, value in reply.anthropic_blocks():
        if kind in {"thinking", "redacted"}:
            suffix = f".{counts['thinking']}" if counts["thinking"] else ""
            counts["thinking"] += 1
            if kind == "redacted":
                data = f"redacted-{number}{suffix}"
                block({"type": "redacted_thinking", "data": data})
                content.append({"type": "redacted_thinking", "data": data})
                continue
            signature = f"sig-{number}{suffix}"
            # As the API streams it: a thinking_delta (empty under the default
            # display, which returns no text), then the signature.
            block({"type": "thinking", "thinking": "", "signature": ""},
                  {"type": "thinking_delta", "thinking": value},
                  {"type": "signature_delta", "signature": signature})
            content.append({"type": "thinking", "thinking": value, "signature": signature})
        elif kind == "text":
            half = len(value) // 2
            block({"type": "text", "text": ""}, {"type": "text_delta", "text": value[:half]},
                  {"type": "text_delta", "text": value[half:]})
            content.append({"type": "text", "text": value})
        else:
            name, arguments = value
            tool_id = f"toolu_fixture_{number}" + (f"_{counts['tool']}" if counts["tool"] else "")
            counts["tool"] += 1
            encoded = json.dumps(arguments)
            block({"type": "tool_use", "id": tool_id, "name": name, "input": {}},
                  {"type": "input_json_delta", "partial_json": encoded[:5]},
                  {"type": "input_json_delta", "partial_json": encoded[5:]})
            content.append({"type": "tool_use", "id": tool_id, "name": name, "input": arguments})
    events.append({"type": "message_delta", "delta": {"stop_reason": "tool_use" if counts["tool"] else "end_turn"},
                   "usage": {"output_tokens": OUTPUT_TOKENS}})
    events.append({"type": "message_stop"})
    return events, content


def _openai_events(reply: Reply, number: int) -> list[dict]:
    response_id = f"resp_fixture_{number}"
    events = [{"type": "response.created", "response": {"id": response_id, "status": "in_progress"}}]
    output: list[dict] = []

    def item(added: dict, done: dict, *between: dict) -> None:
        index = len(output)
        events.append({"type": "response.output_item.added", "output_index": index, "item": added})
        events.extend({**event, "output_index": index} for event in between)
        events.append({"type": "response.output_item.done", "output_index": index, "item": done})
        output.append(done)

    if reply.thinking:
        reasoning = {"type": "reasoning", "id": f"rs_fixture_{number}",
                     "summary": [{"type": "summary_text", "text": reply.thinking}],
                     "encrypted_content": f"enc-{number}"}
        item({"type": "reasoning", "id": reasoning["id"]}, reasoning,
             {"type": "response.reasoning_summary_text.delta", "delta": reply.thinking})
    if reply.text:
        half = len(reply.text) // 2
        message = {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": reply.text}]}
        item({"type": "message"}, message, {"type": "response.output_text.delta", "delta": reply.text[:half]},
             {"type": "response.output_text.delta", "delta": reply.text[half:]})
    if reply.tool:
        name, arguments = reply.tool
        call = {"type": "function_call", "id": f"fc_fixture_{number}", "call_id": f"call_fixture_{number}",
                "name": name, "arguments": json.dumps(arguments)}
        item({**call, "arguments": ""}, call,
             {"type": "response.function_call_arguments.delta", "delta": call["arguments"]})
    events.append({"type": "response.completed", "response": {"id": response_id, "status": "completed",
                                                              "output": output, "usage": OPENAI_USAGE}})
    return events


class _Handler(BaseHTTPRequestHandler):
    server: ScriptedProviders

    def log_message(self, *_):
        pass

    def _send(self, text: bytes) -> bool:
        try:
            self.wfile.write(text)
            self.wfile.flush()
            return True
        except OSError:
            return False  # The client went away (Stop).

    def do_POST(self):  # noqa: N802 - http.server naming
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        path = self.path.split("?", 1)[0]
        protocol = "anthropic" if path.endswith("/messages") else "openai" if path.endswith("/responses") else ""
        headers = {name.lower(): value for name, value in self.headers.items()}
        with self.server.lock:
            self.server.requests.append({"path": self.path, "protocol": protocol, "headers": headers, "body": body})
            number = len(self.server.requests)
        credentials = {headers.get("x-api-key"), headers.get("api-key"), headers.get("authorization")}
        if not protocol or not credentials & {KEY, f"Bearer {KEY}"}:
            self._error(401 if protocol else 404, "authentication_error", "invalid x-api-key")
            return
        if protocol == "anthropic" and not body.get("tools") and any(
                block.get("type") in {"tool_use", "tool_result"}
                for message in body.get("messages") or [] for block in message.get("content") or []):
            # As the Messages API answers a history with tool calls and no tool definitions.
            self._error(400, "invalid_request_error",
                        "Requests which include `tool_use` or `tool_result` blocks must define tools.")
            return
        rules = self.server.claude_rules.get(str(body.get("model") or "")) if protocol == "anthropic" else None
        if protocol == "anthropic":
            refusal = (claude_refusal(rules, body) if rules else "") or self.server.replay_refusal(body)
            if refusal:
                with self.server.lock:
                    self.server.refused.append(refusal)
                self._error(400, "invalid_request_error", refusal)
                return
        turn = (_anthropic_turn if protocol == "anthropic" else _openai_turn)(body)
        try:
            reply = self.server.script(turn)
        except Exception as exc:  # noqa: BLE001 - an unscripted request says so to the test
            self.server.errors.append(f"request {number}: {type(exc).__name__}: {exc}")
            self._error(400, "invalid_request_error", "The fixture has no reply scripted for this request")
            return
        kind = (body.get("thinking") if isinstance(body.get("thinking"), dict) else {}).get("type")
        asks = (kind in {"adaptive", "enabled"} or body.get("reasoning")
                # A model that thinks without being asked.
                or (kind is None and rules is not None and rules.thinks_by_default)
                # Sonnet 5.5's between_tools: a short progress note before a call.
                or (kind == "between_tools" and reply.tool is not None))
        if not reply.thinking and not reply.blocks and asks:
            # A request that asks for thinking (Claude) or reasoning (GPT) gets
            # it first, signed or encrypted, as the providers answer.
            reply = replace(reply, thinking="Weighing the request.")
        if reply.status != 200:
            self._error(reply.status, "overloaded_error" if reply.status == 529 else "api_error", "fixture refusal")
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()  # No length: the stream ends when the connection closes (HTTP/1.0).
        if protocol == "anthropic":
            events, content = _anthropic_events(reply, number, turn.model)
            self.server.issue(body, content)
        else:
            events = _openai_events(reply, number)
        if reply.hold is not None:
            # A long response: the first event, then keep-alive pings until released.
            if not self._send(_sse(events[0])):
                return
            while not reply.hold.wait(0.05):
                if not self._send(b"event: ping\ndata: {\"type\": \"ping\"}\n\n"):
                    with self.server.lock:
                        self.server.abandoned.append(number)
                    return
            events = events[1:]
        for event in events:
            if not self._send(_sse(event)):
                return

    def _error(self, status: int, kind: str, message: str) -> None:
        payload = json.dumps({"type": "error", "error": {"type": kind, "message": message}}).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self._send(payload)


def _sse(event: dict) -> bytes:
    return f"event: {event['type']}\ndata: {json.dumps(event)}\n\n".encode()


class ScriptedProviders(ThreadingHTTPServer):
    """Both APIs on one loopback address: ``/v1/messages`` and ``/v1/responses`` (or ``/openai/v1/responses``)."""

    daemon_threads = True

    def __init__(self, script: Callable[[Turn], Reply] | None = None, *, enforce_prefix: bool = False) -> None:
        super().__init__(("127.0.0.1", 0), _Handler)
        self.script = script or (lambda turn: Reply(text="Fixture answer."))
        self.lock = threading.Lock()
        self.requests: list[dict] = []
        self.errors: list[str] = []
        self.abandoned: list[int] = []
        # The Messages API's checks per model (a test may add a model), and
        # every 400 they gave.
        self.claude_rules: dict[str, ClaudeRules] = dict(CLAUDE_RULES)
        self.refused: list[str] = []
        # Preserved thinking's prefix check, as enforced for new accounts.
        self.enforce_prefix = enforce_prefix
        # Every signed thinking block answered, by signature (or redacted data).
        self.issued: dict[str, dict] = {}
        self._thread = threading.Thread(target=self.serve_forever, daemon=True, name="provider-fixture")
        self._thread.start()

    def _model_of(self, key: str) -> str | None:
        with self.lock:
            issued = self.issued.get(key)
        return issued["model"] if issued else None

    def issue(self, body: dict, content: list[dict]) -> None:
        """Remember a response's thinking blocks with what produced them, to check their replay.

        Each block is bound to the conversation before it (conversation_before)
        and records the thinking block before it from the same model, in its
        response or else in the request (the claude-api skill: "each block
        records the one before it", which is why blocks can be removed from the
        front of the history and not from the middle).
        """
        model = str(body.get("model") or "")
        messages = list(body.get("messages") or [])
        produced = {**body, "messages": [*messages, {"role": "assistant", "content": content}]}
        previous = _thinking_before(body, len(messages), 0, self._model_of, model)
        records = {}
        for position, block in enumerate(content):
            key = block.get("signature") or block.get("data")
            if block.get("type") in THINKING_TYPES and key:
                records[key] = {"model": model, "content": content, "position": position, "previous": previous,
                                "conversation": conversation_before(produced, len(messages), position)}
                previous = key
        with self.lock:
            self.issued.update(records)

    def replay_refusal(self, body: dict) -> str:
        """Why the API refuses the signed thinking ``body`` sends back, in its words, or ''.

        Each block must come back unchanged, after exactly the blocks that
        preceded it in its response. Where preserved thinking runs (and
        ``enforce_prefix`` is set), what the block is bound to must be as it
        was (conversation_before), and the thinking block before it the one it
        recorded, unless every earlier one is gone.
        """
        model = str(body.get("model") or "")
        rules = self.claude_rules.get(model)
        for i, message in enumerate(body.get("messages") or []):
            content = message.get("content")
            if message.get("role") != "assistant" or not isinstance(content, list):
                continue
            content = _without_cache_control(content)
            for j, block in enumerate(content):
                if block.get("type") not in THINKING_TYPES:
                    continue
                with self.lock:
                    issued = self.issued.get(str(block.get("signature") or block.get("data") or ""))
                if issued is None:
                    return f"messages.{i}.content.{j}: Invalid `signature` in `thinking` block."
                if issued["model"] != model:
                    continue  # Another model's thinking: the API leaves it out.
                start = j - issued["position"]
                if start < 0 or content[start:j + 1] != issued["content"][:issued["position"] + 1]:
                    return (f"messages.{i}.content.{j}: `thinking` or `redacted_thinking` blocks in the latest "
                            "assistant message cannot be modified. These blocks must remain as they were in the "
                            "original response.")
                if not (self.enforce_prefix and rules is not None and rules.preserved):
                    continue
                before = _thinking_before(body, i, j, self._model_of, model)
                if (conversation_before(body, i, j) != issued["conversation"]
                        or before not in {None, issued["previous"]}):
                    return (f"messages.{i}.content.{j}: Invalid `signature` in `thinking` block. The block is bound "
                            "to a different conversation. Remove the block, or set "
                            '`thinking.block_binding.prefix_mismatch_behavior` to "drop_block".')
        return ""

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}"

    def of(self, protocol: str) -> list[dict]:
        with self.lock:
            return [request for request in self.requests if request["protocol"] == protocol]

    def close(self) -> None:
        self.shutdown()
        self.server_close()
        self._thread.join(timeout=5)
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and self._thread.is_alive():
            time.sleep(0.01)
