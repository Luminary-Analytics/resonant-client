"""
Session management for the Resonant Engine.

A Session holds conversation history, manages the agentic loop,
and coordinates between backends and tools.
"""

import json
import os
import re
import sys
import time
import logging
import threading
import platform as plat
from typing import Iterator, Optional, Callable

from ..backends import (
    EVENT_TEXT_DELTA,
    EVENT_TOOL_CALL,
    EVENT_DONE,
    EVENT_ERROR,
)
from ..events import EngineEvent, make_event
from .tools import AGENT_TOOLS, execute_tool, get_tool_icon
from .agents import get_agent_type, AGENT_TYPES
from .compression import should_compress, compress, estimate_tokens
from .hooks import HookRunner, HookType

logger = logging.getLogger(__name__)


# ── Doom Loop Detection ────────────────────────────────────────────────
DOOM_LOOP_THRESHOLD = 3  # Same tool+args N times in a row = stop


def _check_doom_loop(history: list, threshold: int = DOOM_LOOP_THRESHOLD) -> bool:
    """Check if the last N tool calls are identical (same name + same args)."""
    recent_calls = []
    for entry in reversed(history):
        if entry.get("role") == "tool_call":
            sig = (entry.get("name", ""), entry.get("arguments", ""))
            recent_calls.append(sig)
            if len(recent_calls) >= threshold:
                break
        elif entry.get("role") == "user":
            break  # Only check within the current turn

    if len(recent_calls) < threshold:
        return False

    return len(set(recent_calls)) == 1


# ── System Instructions ────────────────────────────────────────────────

def get_system_instructions(plan_mode: bool = False, project_instructions: str | None = None) -> str:
    """Build system instructions with platform-specific hints."""
    if sys.platform == "win32":
        platform_name = f"Windows ({plat.release()})"
        platform_hints = "Use 'python' not 'python3'. Use 'pip' not 'pip3'. Paths use backslashes."
    else:
        platform_name = f"Linux/macOS ({plat.system()})"
        platform_hints = "Use 'python3'/'pip3'."

    project_block = ""
    if project_instructions:
        project_block = f"\n\n--- PROJECT INSTRUCTIONS (RESONANT.md) ---\n{project_instructions}\n--- END PROJECT INSTRUCTIONS ---"

    if plan_mode:
        return f"""You are an expert AI coding agent running on {platform_name}. {platform_hints}

You are in PLAN MODE. Your job is to THINK and create a clear plan — do NOT call any tools yet.

Given the user's request:
1. Analyze what they're asking for
2. List the specific steps you would take (numbered)
3. Mention which tools you'd use at each step
4. Flag any risks or questions
5. Keep it concise — bullet points, not paragraphs

Example format:
## Plan
1. Read the project structure with `glob(**/*.py)`
2. Examine the main entry point with `file_read`
3. Identify the bug in the error handler
4. Fix with `file_edit` — change X to Y
5. Run tests with `bash(pytest)`

**Questions:** None — ready to execute.

Do NOT execute tools. Just plan.{project_block}"""

    return f"""You are an expert AI coding agent running on {platform_name}. {platform_hints}
Working directory: {os.getcwd()}

You have tools. Use them to accomplish tasks — don't just talk about code.

RULES:
1. ACT FIRST — use tools immediately for any code task. Start by exploring (glob, file_read).
2. BE CONCISE — short text, let tool output speak. No filler.
3. One tool call per response. Wait for results before the next.
4. After gathering info, provide a clear summary of what you found.
5. bash is non-interactive (no stdin, no servers, no REPLs, no interactive games). Commands have a timeout.
6. Prefer file_edit over file_write for existing files.
7. When asked to evaluate/review code, READ the actual files first.{project_block}"""


# ── Choices Parser ─────────────────────────────────────────────────────

def parse_choices(text: str) -> tuple:
    """
    Parse <choices> blocks from model output.
    Returns (text_before, list_of_options, text_after) or (text, None, None).
    """
    match = re.search(r'<choices>\s*(.*?)\s*</choices>', text, flags=re.DOTALL)
    if not match:
        return (text, None, None)

    before = text[:match.start()].strip()
    after = text[match.end():].strip()
    block = match.group(1)

    options = []
    for line in block.strip().split("\n"):
        line = line.strip()
        if line.startswith("* ") or line.startswith("- "):
            options.append(line[2:].strip())
        elif line:
            options.append(line)

    if not options:
        return (text, None, None)

    return (before, options, after)


def strip_tool_call_tags(text: str) -> str:
    """Remove <tool_call>...</tool_call> blocks from display text."""
    return re.sub(r'<tool_call>.*?</tool_call>', '', text, flags=re.DOTALL).strip()


# ── Session ────────────────────────────────────────────────────────────

class Session:
    """
    Manages a single conversation session with agentic loop.

    The session is the core of the engine. It:
    - Maintains conversation history
    - Runs the agentic loop (LLM → tool → LLM → tool → ...)
    - Yields EngineEvents for any client to consume
    - Handles plan mode, doom loop detection, choices
    """

    # Classification prompt for auto-plan detection
    _CLASSIFY_PROMPT = (
        "Classify this user request as SIMPLE or COMPLEX.\n"
        "SIMPLE: single-file edits, quick questions, small fixes, explanations, one-step tasks\n"
        "COMPLEX: multi-file changes, new features, refactors, architecture changes, multi-step tasks\n"
        "Reply with exactly one word: SIMPLE or COMPLEX\n\n"
        "Request: {user_msg}"
    )

    def __init__(
        self,
        backend,
        max_steps: int = 25,
        max_tokens: int = 16384,
        auto_approve: bool = True,
        auto_plan: bool = False,
        parent_session: Optional["Session"] = None,
        allowed_tools: Optional[list[dict]] = None,
        project_instructions: Optional[str] = None,
        cancel_event: Optional[threading.Event] = None,
    ):
        self.backend = backend
        self.max_steps = max_steps
        self.max_tokens = max_tokens
        self.auto_approve = auto_approve
        self.auto_plan = auto_plan
        self.conversation_history: list = []
        self.plan_mode: bool = False
        self.parent_session = parent_session
        self._allowed_tools = allowed_tools  # None = use AGENT_TOOLS
        self._pending_permission: Optional[dict] = None
        self._pending_choice: Optional[dict] = None
        self.project_instructions = project_instructions
        self.hook_runner: Optional[HookRunner] = None
        self.mcp_tools: list[dict] = []  # Extra tools from MCP servers
        self._engram = None  # EngramIntegration, set externally
        self._codebase_index = None  # CodebaseIndex, set externally
        self._cancel_event = cancel_event or threading.Event()
        self.project_path: Optional[str] = None  # Set externally for path resolution
        # Three-tier autonomy: suggest (read-only) | auto-edit (files ok) | full-auto (sandboxed)
        self.autonomy_tier: str = "full-auto" if auto_approve else "suggest"
        self.sandbox = None  # PathSandbox, set externally
        self.event_logger = None  # EventLogger, set externally for JSONL logging

    @property
    def is_subagent(self) -> bool:
        """True if this session was spawned by a parent session."""
        return self.parent_session is not None

    @property
    def tools(self) -> list[dict]:
        """Get the tools available for this session."""
        if self._allowed_tools is not None:
            return self._allowed_tools
        base = AGENT_TOOLS
        if self.mcp_tools:
            base = base + self.mcp_tools
        return base

    def should_plan(self, user_msg: str) -> bool:
        """Use a quick LLM classification to decide if this request needs planning."""
        try:
            prompt = self._CLASSIFY_PROMPT.format(user_msg=user_msg)
            result = self.backend.classify(prompt, max_tokens=20)
            return "COMPLEX" in result.upper()
        except Exception:
            return False  # On failure, skip planning

    def clear(self):
        """Clear conversation history."""
        self.conversation_history.clear()

    def set_backend(self, backend):
        """Switch to a different backend (clears history)."""
        self.backend = backend
        self.conversation_history.clear()

    @property
    def cancel_requested(self) -> bool:
        return self._cancel_event.is_set()

    def cancel(self):
        """Request cooperative cancellation for the current run."""
        self._cancel_event.set()

    def reset_cancel(self):
        """Clear any pending cancellation request before starting a new run."""
        self._cancel_event.clear()

    def _log_event(self, event: dict) -> None:
        """Log an event to the JSONL logger if configured."""
        if self.event_logger:
            try:
                self.event_logger.log(event)
            except Exception:
                pass

    def _should_auto_approve(self, tool_name: str) -> bool:
        """
        Determine if a tool should be auto-approved based on the autonomy tier.

        Three-tier model (inspired by Codex CLI):
        - suggest: only read-only tools (file_read, glob, grep) are auto-approved
        - auto-edit: file tools auto-approved, bash/exec tools prompt user
        - full-auto: everything auto-approved (sandbox enforces safety)
        """
        from .sandbox import PathSandbox

        if self.autonomy_tier == "suggest":
            return PathSandbox.is_read_only_tool(tool_name)
        elif self.autonomy_tier == "auto-edit":
            # File tools are OK, exec tools need approval
            return not PathSandbox.is_exec_tool(tool_name)
        else:  # full-auto
            return True

    def _cancelled_events(self, total_start: float, total_steps: int) -> Iterator[dict]:
        yield make_event(EngineEvent.ERROR, message="Interrupted")
        yield make_event(
            EngineEvent.SESSION_END,
            total_elapsed=time.time() - total_start,
            total_steps=total_steps,
        )

    def run(
        self,
        user_msg: str,
        on_permission: Optional[Callable] = None,
        on_choice: Optional[Callable] = None,
        images: Optional[list[tuple[bytes, str]]] = None,
    ) -> Iterator[dict]:
        """
        Run the agentic loop for a user message.

        Yields EngineEvent dicts that any client can consume.

        Args:
            user_msg: The user's input message
            on_permission: Callback(tool_name, tool_args) -> bool for tool approval
                          If None, uses self.auto_approve
            on_choice: Callback(options) -> str for choice selection
                      If None, selects first option
            images: Optional list of (image_bytes, media_type) for multimodal input
        """
        # Build user message content (multimodal or text-only)
        if images:
            import base64
            content_parts = []
            for img_bytes, media_type in images:
                b64 = base64.b64encode(img_bytes).decode("utf-8")
                content_parts.append({
                    "type": "image",
                    "media_type": media_type,
                    "data": b64,
                })
            content_parts.append({"type": "text", "text": user_msg})
            self.conversation_history.append({"role": "user", "content": content_parts})
        else:
            self.conversation_history.append({"role": "user", "content": user_msg})

        iteration = 0
        exec_step = 0
        current_msg = user_msg
        total_start = time.time()
        executing_plan = False
        last_tool_used = None

        if self.cancel_requested:
            yield from self._cancelled_events(total_start, 0)
            return

        tool_mode = getattr(self.backend, 'tool_mode', 'native')
        _start_event = make_event(EngineEvent.SESSION_START,
                        plan_mode=self.plan_mode,
                        backend=self.backend.name,
                        model=self.backend.model,
                        tool_mode=tool_mode)
        self._log_event(_start_event)
        yield _start_event

        # Log session metadata for JSONL replay
        self._log_event({
            "event": "session.meta",
            "session_id": id(self),
            "project_path": self.project_path,
            "autonomy_tier": self.autonomy_tier,
            "sandbox_enabled": bool(self.sandbox),
        })

        # ── Context compression ──
        if should_compress(self.conversation_history):
            try:
                compressed, summary = compress(self, max_tokens=100_000)
                if summary:
                    old_count = len(self.conversation_history)
                    old_tokens = estimate_tokens(self.conversation_history)
                    self.conversation_history = compressed
                    new_tokens = estimate_tokens(self.conversation_history)
                    yield make_event(EngineEvent.COMPRESSION,
                                    old_entries=old_count,
                                    new_entries=len(compressed),
                                    old_tokens=old_tokens,
                                    new_tokens=new_tokens,
                                    summary_preview=summary[:200])
            except Exception as e:
                logger.warning(f"Context compression failed: {e}")

        while iteration < self.max_steps:
            if self.cancel_requested:
                yield from self._cancelled_events(total_start, exec_step)
                return
            iteration += 1
            is_planning = self.plan_mode and not executing_plan

            # ── Step start ──
            if is_planning:
                yield make_event(EngineEvent.STEP_START,
                                step=0, step_type="plan",
                                label="analyzing request and outlining steps")
            else:
                exec_step += 1
                ctx = ""
                if exec_step > 1 and last_tool_used:
                    ctx = f"after {last_tool_used}"
                elif exec_step > 1:
                    ctx = "continuing"
                yield make_event(EngineEvent.STEP_START,
                                step=exec_step, step_type="execute",
                                label=ctx)

            step_start = time.time()
            instructions = get_system_instructions(plan_mode=is_planning, project_instructions=self.project_instructions)

            # ── Inject context from memory & RAG (first iteration only) ──
            if iteration == 1:
                # Engram memory context
                if self._engram and self._engram.enabled:
                    try:
                        memory_context = self._engram.get_context_for_prompt(user_msg)
                        if memory_context:
                            instructions += memory_context
                    except Exception as e:
                        logger.warning(f"Engram recall failed: {e}")

                # RAG codebase context
                if self._codebase_index and self._codebase_index.is_indexed:
                    try:
                        rag_context = self._codebase_index.get_context_for_prompt(user_msg)
                        if rag_context:
                            instructions += rag_context
                    except Exception as e:
                        logger.warning(f"RAG context failed: {e}")

            # ── Stream from backend ──
            collected_text = []
            tool_calls = []
            cog_state = None
            done_stats = None
            done_model = None

            try:
                for event_type, data in self.backend.stream(
                    user_msg=current_msg,
                    conversation_history=self.conversation_history,
                    instructions=instructions,
                    tools=[] if is_planning else self.tools,
                    max_tokens=self.max_tokens,
                    cancel_event=self._cancel_event,
                ):
                    if self.cancel_requested:
                        yield from self._cancelled_events(total_start, exec_step)
                        return
                    if event_type == EVENT_TEXT_DELTA:
                        delta = data.get("delta", "")
                        collected_text.append(delta)
                        yield make_event(EngineEvent.TEXT_DELTA, delta=delta)

                    elif event_type == EVENT_TOOL_CALL:
                        tool_calls.append(data)
                        # Yield tool call immediately for TUI display
                        # (don't wait until stream ends to show what's coming)
                        fn_name = data.get("name", "")
                        fn_args_str = data.get("arguments", "{}")
                        try:
                            fn_args = json.loads(fn_args_str) if isinstance(fn_args_str, str) else fn_args_str
                        except json.JSONDecodeError:
                            fn_args = {}
                        yield make_event(EngineEvent.TOOL_CALL,
                                        name=fn_name, arguments=fn_args,
                                        arguments_str=fn_args_str,
                                        call_id=data.get("call_id", ""),
                                        icon=get_tool_icon(fn_name))

                    elif event_type == EVENT_DONE:
                        cog_state = data.get("cognitive_state")
                        done_stats = data.get("stats")
                        done_model = data.get("model")

                    elif event_type == EVENT_ERROR:
                        yield make_event(EngineEvent.ERROR, message=data.get("message", "Unknown"))
                        yield make_event(EngineEvent.SESSION_END,
                                        total_elapsed=time.time() - total_start,
                                        total_steps=exec_step)
                        return

            except KeyboardInterrupt:
                self.cancel()
                yield from self._cancelled_events(total_start, exec_step)
                return
            except Exception as e:
                yield make_event(EngineEvent.ERROR, message=f"Stream error: {e}")
                yield make_event(EngineEvent.SESSION_END,
                                total_elapsed=time.time() - total_start,
                                total_steps=exec_step)
                return

            step_elapsed = time.time() - step_start

            # ── Process collected text ──
            full_text = "".join(collected_text).strip()
            full_text = strip_tool_call_tags(full_text)

            if full_text:
                yield make_event(EngineEvent.TEXT_DONE, text=full_text)

                # Check for choices
                before, choices, after = parse_choices(full_text)
                if choices:
                    yield make_event(EngineEvent.CHOICES,
                                    before=before, options=choices, after=after)

                    # Get user's choice
                    if on_choice:
                        selected = on_choice(choices)
                    else:
                        selected = choices[0]

                    self.conversation_history.append({"role": "assistant", "content": full_text})
                    self.conversation_history.append({"role": "user", "content": selected})
                    current_msg = selected
                    tool_calls = []

                    yield make_event(EngineEvent.STATUS,
                                    model=done_model, stats=done_stats,
                                    cognitive_state=cog_state,
                                    elapsed=step_elapsed)
                    yield make_event(EngineEvent.STEP_END, step=exec_step, elapsed=step_elapsed)
                    continue

                # Normal text — add to history
                self.conversation_history.append({"role": "assistant", "content": full_text})

            # ── Execute tool calls ──
            has_tool_calls = len(tool_calls) > 0

            # CLI backends (claude-code, codex) handle tool execution internally.
            # We received tool_call events for display only — skip execution.
            if has_tool_calls and getattr(self.backend, 'handles_tools', False):
                # Add a synthetic assistant message with tool info for history
                tool_summary = ", ".join(item.get("name", "") for item in tool_calls)
                self.conversation_history.append({
                    "role": "assistant",
                    "content": f"[CLI executed tools: {tool_summary}]"
                })
                has_tool_calls = False  # Don't loop — CLI already completed
                yield make_event(EngineEvent.STATUS,
                                model=done_model, stats=done_stats,
                                cognitive_state=cog_state,
                                elapsed=step_elapsed)
                yield make_event(EngineEvent.STEP_END, step=exec_step, elapsed=step_elapsed)
                break  # Exit the agentic loop — CLI ran to completion

            for item in tool_calls:
                if self.cancel_requested:
                    yield from self._cancelled_events(total_start, exec_step)
                    return
                fn_name = item.get("name", "")
                fn_args_str = item.get("arguments", "{}")
                call_id = item.get("call_id", "")

                try:
                    fn_args = json.loads(fn_args_str) if isinstance(fn_args_str, str) else fn_args_str
                except json.JSONDecodeError:
                    fn_args = {}

                # Tool call event already yielded during streaming (above)
                # No duplicate yield here — go straight to execution

                # Pre-tool hook check
                if self.hook_runner:
                    hook_result = self.hook_runner.run_hooks(
                        HookType.PRE_TOOL_USE,
                        context={"tool_args": fn_args},
                        tool_name=fn_name,
                    )
                    if not hook_result.allowed:
                        result_output = f"Blocked by hook: {hook_result.error or 'denied'}"
                        yield make_event(EngineEvent.TOOL_RESULT,
                                        name=fn_name, call_id=call_id,
                                        output=result_output, is_error=False,
                                        denied=True, elapsed=0.0)
                        self.conversation_history.append({
                            "role": "tool_call", "name": fn_name,
                            "arguments": fn_args_str, "call_id": call_id,
                            "content": f"Called {fn_name}",
                        })
                        self.conversation_history.append({
                            "role": "tool_result", "call_id": call_id,
                            "content": result_output,
                        })
                        continue

                # Permission check (three-tier autonomy model)
                approved = self._should_auto_approve(fn_name)
                if not approved:
                    if on_permission:
                        approved = on_permission(fn_name, fn_args)
                    else:
                        approved = self.auto_approve  # Fallback to legacy flag

                if not approved:
                    result_output = "Tool execution denied by user."
                    yield make_event(EngineEvent.TOOL_RESULT,
                                    name=fn_name, call_id=call_id,
                                    output=result_output, is_error=False,
                                    denied=True, elapsed=0.0)
                    self.conversation_history.append({
                        "role": "tool_call", "name": fn_name,
                        "arguments": fn_args_str, "call_id": call_id,
                        "content": f"Called {fn_name}",
                    })
                    self.conversation_history.append({
                        "role": "tool_result", "call_id": call_id,
                        "content": result_output,
                    })
                    continue

                # Execute the tool
                if fn_name == "task":
                    # Task tool — spawn a sub-agent session
                    yield from self._execute_task(fn_args, call_id, fn_args_str)
                elif fn_name.startswith("mcp_") and hasattr(self, '_mcp_manager') and self._mcp_manager:
                    # MCP tool — route to MCP server
                    import time as _time
                    mcp_start = _time.time()
                    mcp_result = self._mcp_manager.call_tool(fn_name, fn_args)
                    mcp_output = str(mcp_result.get("content", mcp_result))
                    from .tools import ToolResult
                    result = ToolResult(
                        output=mcp_output,
                        is_error="error" in mcp_result,
                        elapsed=_time.time() - mcp_start,
                    )

                    yield make_event(EngineEvent.TOOL_RESULT,
                                    name=fn_name, call_id=call_id,
                                    output=result.output, is_error=result.is_error,
                                    elapsed=result.elapsed, metadata={},
                                    denied=False)
                    self.conversation_history.append({
                        "role": "tool_call", "name": fn_name,
                        "arguments": fn_args_str, "call_id": call_id,
                        "content": f"Called {fn_name}",
                    })
                    self.conversation_history.append({
                        "role": "tool_result", "call_id": call_id,
                        "content": result.output,
                    })
                else:
                    # Resolve relative file paths against project directory
                    if self.project_path:
                        if fn_name in ("file_write", "file_read", "file_edit", "glob", "grep"):
                            for key in ("path", "pattern"):
                                if key in fn_args and fn_args[key]:
                                    p = fn_args[key]
                                    if not os.path.isabs(p):
                                        fn_args[key] = os.path.join(self.project_path, p)
                        if fn_name == "bash" and "cwd" not in fn_args:
                            fn_args["cwd"] = self.project_path

                    # Sandbox validation — block paths outside project directory
                    if self.sandbox:
                        from .sandbox import SandboxViolation
                        try:
                            if fn_name in ("file_write", "file_read", "file_edit"):
                                if "path" in fn_args and fn_args["path"]:
                                    fn_args["path"] = self.sandbox.validate_path(fn_args["path"])
                            elif fn_name == "bash" and "cwd" in fn_args:
                                fn_args["cwd"] = self.sandbox.validate_bash_cwd(fn_args["cwd"])
                        except SandboxViolation as sv:
                            result_output = f"Blocked by sandbox: {sv}"
                            yield make_event(EngineEvent.TOOL_RESULT,
                                            name=fn_name, call_id=call_id,
                                            output=result_output, is_error=True,
                                            denied=True, elapsed=0.0)
                            self.conversation_history.append({
                                "role": "tool_call", "name": fn_name,
                                "arguments": fn_args_str, "call_id": call_id,
                                "content": f"Called {fn_name}",
                            })
                            self.conversation_history.append({
                                "role": "tool_result", "call_id": call_id,
                                "content": result_output,
                            })
                            continue

                    result = execute_tool(fn_name, fn_args, cancel_event=self._cancel_event)

                    yield make_event(EngineEvent.TOOL_RESULT,
                                    name=fn_name, call_id=call_id,
                                    output=result.output, is_error=result.is_error,
                                    elapsed=result.elapsed, metadata=result.metadata,
                                    denied=False)

                    # Add to conversation history
                    self.conversation_history.append({
                        "role": "tool_call", "name": fn_name,
                        "arguments": fn_args_str, "call_id": call_id,
                        "content": f"Called {fn_name}",
                    })

                    # If tool returned a screenshot, include image in result
                    # so the model can see it (computer use / browser screenshot loop)
                    tool_result_entry = {
                        "role": "tool_result", "call_id": call_id,
                        "content": result.output,
                    }
                    if result.metadata.get("screenshot_b64"):
                        tool_result_entry["image"] = {
                            "type": "base64",
                            "media_type": result.metadata.get("media_type", "image/png"),
                            "data": result.metadata["screenshot_b64"],
                        }
                    self.conversation_history.append(tool_result_entry)

                    # Post-tool hook
                    if self.hook_runner:
                        self.hook_runner.run_hooks(
                            HookType.POST_TOOL_USE,
                            context={"tool_args": fn_args, "result": result.output[:500]},
                            tool_name=fn_name,
                        )

                    if self.cancel_requested or result.metadata.get("cancelled"):
                        yield from self._cancelled_events(total_start, exec_step)
                        return

                if self.cancel_requested:
                    yield from self._cancelled_events(total_start, exec_step)
                    return

            # ── Status ──
            yield make_event(EngineEvent.STATUS,
                            model=done_model, stats=done_stats,
                            cognitive_state=cog_state,
                            elapsed=step_elapsed)

            # ── Plan mode: ask for approval ──
            if is_planning and full_text:
                yield make_event(EngineEvent.PLAN_GENERATED, plan=full_text)
                yield make_event(EngineEvent.STEP_END, step=0, elapsed=step_elapsed)

                # The TUI handles plan approval flow and sends back
                # PLAN_APPROVE / PLAN_REJECT / PLAN_EDIT
                # For embedded mode, we use the callback pattern
                # For now, yield the event and let the caller decide
                return  # Caller handles plan approval and re-calls run()

            # ── Doom loop detection ──
            if has_tool_calls and _check_doom_loop(self.conversation_history):
                yield make_event(EngineEvent.ERROR,
                                message=f"Doom loop detected: last {DOOM_LOOP_THRESHOLD} tool calls were identical. Stopping.")
                yield make_event(EngineEvent.STEP_END, step=exec_step, elapsed=step_elapsed)
                break

            # ── Continue or stop ──
            yield make_event(EngineEvent.STEP_END, step=exec_step, elapsed=step_elapsed)

            if has_tool_calls:
                last_names = [item.get("name", "") for item in tool_calls if item.get("name")]
                if last_names:
                    last_tool_used = ", ".join(last_names)
                tool_calls = []
                collected_text = []
                current_msg = "Continue based on the tool results above."
                continue
            else:
                break

        total_elapsed = time.time() - total_start

        # ── Engram: auto-remember session summary ──
        if self._engram and self._engram.enabled and not self.is_subagent:
            try:
                self._engram.session_summary(self.conversation_history)
            except Exception as e:
                logger.warning(f"Engram session summary failed: {e}")

        if iteration >= self.max_steps:
            yield make_event(EngineEvent.ERROR,
                            message=f"Reached {self.max_steps} step limit — use /clear to reset")

        yield make_event(EngineEvent.SESSION_END,
                        total_elapsed=total_elapsed,
                        total_steps=exec_step if exec_step > 0 else iteration)

    def _execute_task(
        self,
        fn_args: dict,
        call_id: str,
        fn_args_str: str,
    ) -> Iterator[dict]:
        """
        Execute the 'task' tool — spawn a sub-agent with isolated session.

        The sub-agent gets:
        - Its own conversation history (empty)
        - Restricted tools based on agent type
        - A lower step limit
        - No ability to spawn further sub-agents (recursion guard)

        All sub-agent events are yielded through the parent for TUI display.
        """
        prompt = fn_args.get("prompt", "")
        agent_type_name = fn_args.get("agent_type", "explore")

        agent_type = get_agent_type(agent_type_name)
        if not agent_type:
            result_output = f"Error: Unknown agent type '{agent_type_name}'. Use: build, explore, or plan."
            yield make_event(EngineEvent.TOOL_RESULT,
                            name="task", call_id=call_id,
                            output=result_output, is_error=True,
                            elapsed=0.0, metadata={}, denied=False)
            self.conversation_history.append({
                "role": "tool_call", "name": "task",
                "arguments": fn_args_str, "call_id": call_id,
                "content": "Called task",
            })
            self.conversation_history.append({
                "role": "tool_result", "call_id": call_id,
                "content": result_output,
            })
            return

        # Filter tools — remove 'task' to prevent recursion
        allowed_tools = agent_type.filter_tools(AGENT_TOOLS)
        allowed_tools = [t for t in allowed_tools
                        if t.get("function", {}).get("name") != "task"]

        # Notify TUI of sub-agent start
        yield make_event(EngineEvent.SUBAGENT_START,
                        agent_type=agent_type_name,
                        prompt=prompt,
                        call_id=call_id)

        # Create child session
        child = Session(
            backend=self.backend,
            max_steps=agent_type.max_steps,
            max_tokens=self.max_tokens,
            auto_approve=True,  # Sub-agents auto-approve (no interactive prompts)
            parent_session=self,
            allowed_tools=allowed_tools,
            cancel_event=self._cancel_event,
        )

        # Run the sub-agent, collecting text output and forwarding events
        collected_text = []
        sub_start = time.time()
        sub_steps = 0

        for event in child.run(
            user_msg=prompt,
            on_permission=None,
            on_choice=None,
        ):
            etype = event.get("event", "")

            # Forward relevant events for TUI display (with nesting indicator)
            event["_subagent"] = True
            event["_agent_type"] = agent_type_name

            if etype in (
                EngineEvent.TEXT_DELTA.value,
                EngineEvent.TOOL_CALL.value,
                EngineEvent.TOOL_RESULT.value,
                EngineEvent.STEP_START.value,
                EngineEvent.STEP_END.value,
                EngineEvent.ERROR.value,
            ):
                yield event

            if etype == EngineEvent.TEXT_DONE.value:
                text = event.get("text", "")
                if text:
                    collected_text.append(text)
                yield event

            if etype == EngineEvent.STEP_END.value:
                sub_steps += 1

        sub_elapsed = time.time() - sub_start
        result_output = "\n\n".join(collected_text) if collected_text else "(no output)"

        # Truncate very long sub-agent output
        if len(result_output) > 8000:
            result_output = result_output[:8000] + "\n\n... (truncated)"

        # Notify TUI of sub-agent completion
        yield make_event(EngineEvent.SUBAGENT_END,
                        agent_type=agent_type_name,
                        call_id=call_id,
                        steps=sub_steps,
                        elapsed=sub_elapsed,
                        result_preview=result_output[:200])

        # Return result to parent context
        yield make_event(EngineEvent.TOOL_RESULT,
                        name="task", call_id=call_id,
                        output=result_output,
                        is_error=False,
                        elapsed=sub_elapsed,
                        metadata={
                            "agent_type": agent_type_name,
                            "steps": sub_steps,
                        },
                        denied=False)

        # Add to parent conversation history
        self.conversation_history.append({
            "role": "tool_call", "name": "task",
            "arguments": fn_args_str, "call_id": call_id,
            "content": f"Called task ({agent_type_name})",
        })
        self.conversation_history.append({
            "role": "tool_result", "call_id": call_id,
            "content": result_output,
        })
