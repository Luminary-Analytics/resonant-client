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
import uuid as _uuid
from pathlib import Path
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
# Catch agents that fall into a tight repetition loop — calling the same tool
# with the same args over and over, getting the same result and never moving
# forward. We give the model one corrective nudge first, then hard-stop.
DOOM_LOOP_THRESHOLD = 4   # N identical tool+args calls in a row → hard stop
DOOM_LOOP_NUDGE_AT = 2    # First repeat → inject a "try a different approach" prompt


def _count_trailing_identical_tool_calls(history: list) -> int:
    """How many of the most recent tool calls (within the current turn) are identical."""
    sig = None
    count = 0
    for entry in reversed(history):
        role = entry.get("role")
        if role == "tool_call":
            curr = (entry.get("name", ""), entry.get("arguments", ""))
            if sig is None:
                sig = curr
                count = 1
            elif curr == sig:
                count += 1
            else:
                break
        elif role == "user":
            break  # Only count within the current turn
        # tool_result / assistant entries are skipped — they sit between calls
    return count


def _check_doom_loop(history: list, threshold: int = DOOM_LOOP_THRESHOLD) -> bool:
    """Check if the last N tool calls are identical (same name + same args)."""
    return _count_trailing_identical_tool_calls(history) >= threshold


# ── System Instructions ────────────────────────────────────────────────

def get_system_instructions(
    plan_mode: bool = False,
    project_instructions: str | None = None,
    *,
    working_directory: str | None = None,
) -> str:
    """Build system instructions with platform-specific hints."""
    if sys.platform == "win32":
        platform_name = f"Windows ({plat.release()})"
        platform_hints = (
            "Use 'python' not 'python3'. Use 'pip' not 'pip3'. Paths use backslashes. "
            "Unix tools like `tail`, `head`, `sed`, `awk`, `grep`, `wc`, `find` are "
            "NOT available — use `file_read` for inspection, the `grep` agent tool "
            "for content search, and `glob` for path listing instead of shelling out."
        )
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

    wd = working_directory or os.getcwd()
    base = f"""You are an expert AI coding agent running on {platform_name}. {platform_hints}
Working directory: {wd}

You have tools. Use them to accomplish tasks — don't just talk about code.

RULES:
1. ACT FIRST — use tools immediately for any code task. Start by exploring (glob, file_read, grep).
2. BE CONCISE — short text, let tool output speak. No filler, no preamble, no recap of obvious tool results.
3. PARALLELIZE READS — when you need to read several independent files or run several greps, use the `batch` tool to fan them out in one turn instead of sequential round-trips. Do NOT batch writes or shell commands that share state.
4. After gathering info, provide a clear summary of what you found.
5. bash is non-interactive (no stdin, no servers, no REPLs, no interactive games). Commands have a timeout.
6. Prefer file_edit over file_write for existing files. Show only the smallest diff that solves the problem.
7. When asked to evaluate/review code, READ the actual files first — never guess from filenames.
8. For multi-step work, track progress with a markdown task list the UI can parse, e.g. `- [ ] First step` then `- [x] First step` when done.
9. For independent sub-investigations (codebase exploration, planning, isolated builds), spawn a sub-agent via the `task` tool — keeps the main context window clean.
10. THINK BEFORE ACTING — for non-trivial tasks, briefly reason through the approach before the first tool call. Don't show your full chain of thought; show the conclusion.
11. PREFER FIRST-CLASS TOOLS over `bash` when one exists:
    - Git: use `git_status` / `git_diff` / `git_commit` / `git_branch_create` / `git_log` (not `bash(git ...)`) — safer arg handling, structured UI rendering.
    - Iterative Python/JS exploration: use `repl_python_start` + `repl_python_eval` (not repeated `bash(python -c ...)`) — state persists across calls, so you can build up incrementally without cold starts. Always `repl_python_stop` when done.{project_block}"""

    return base


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


# Markdown task list lines: `- [ ] Todo` / `- [x] Done` (also `*` bullets)
_MARKDOWN_TODO_LINE = re.compile(r"^\s*[-*]\s*\[([ xX])\]\s*(.+?)\s*$")


def parse_markdown_todos(text: str) -> list[dict] | None:
    """Extract GitHub-style checkbox lines from model text.

    Returns a list of {"text": str, "done": bool} or None if no task lines found.
    """
    items: list[dict] = []
    for line in text.splitlines():
        m = _MARKDOWN_TODO_LINE.match(line)
        if not m:
            continue
        items.append({
            "text": m.group(2).strip(),
            "done": m.group(1).lower() == "x",
        })
    return items if items else None


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
        self.execution_policy = None  # ExecutionPolicy, set externally
        # Auto-feedback loops (set externally by AppState.build_session from settings)
        self.auto_lint_enabled: bool = False
        self.auto_test_enabled: bool = False
        self.auto_test_command: str = "pytest -x"
        # Doom-loop guards: file path → hash of last feedback we injected
        self._lint_feedback_cache: dict[str, str] = {}
        self._test_feedback_cache: dict[str, str] = {}
        # Per-turn flag: did we already inject the corrective doom-loop nudge?
        self._doom_loop_nudged: bool = False

    @property
    def is_subagent(self) -> bool:
        """True if this session was spawned by a parent session."""
        return self.parent_session is not None

    def copy_execution_context_from(self, parent: "Session") -> None:
        """Mirror tool cwd, sandbox, and sidecar services from a parent session (e.g. sub-agents)."""
        self.project_path = parent.project_path
        self.sandbox = parent.sandbox
        self.project_instructions = parent.project_instructions
        self.autonomy_tier = parent.autonomy_tier
        self.execution_policy = parent.execution_policy
        self.hook_runner = parent.hook_runner
        self.mcp_tools = parent.mcp_tools
        self._mcp_manager = getattr(parent, "_mcp_manager", None)
        self._engram = parent._engram
        self._codebase_index = parent._codebase_index
        pl = parent.event_logger
        if pl and getattr(pl, "enabled", False):
            try:
                from .event_log import EventLogger

                log_dir = getattr(pl, "_log_dir", None) or (Path.home() / ".resonant" / "logs")
                self.event_logger = EventLogger(
                    log_dir=log_dir,
                    session_id=_uuid.uuid4().hex[:12],
                    enabled=True,
                )
            except Exception:
                self.event_logger = None
        else:
            self.event_logger = pl

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

    def set_backend(self, backend, *, reset_history: bool = False):
        """
        Switch to a different backend.

        Args:
            backend: New backend instance to attach to this session.
            reset_history: If True, clears conversation_history. Default False
                (preserves prior turns so users can swap models mid-conversation).

        Bug #9+#10 fix: was previously always-clear, which silently dropped
        the user's conversation context every time the backend dropdown
        changed. The dropdown UX implies "swap the brain"; users don't
        expect their history to vanish.

        Caveat: CLI-wrapper backends (claude-code, codex) manage their own
        session via --resume <session_id> and ignore the conversation_history
        list we pass to .stream(). For those, our preserved history reaches
        Ollama/Claude API/OpenAI fine but is invisible to claude-code/codex —
        the GUI emits a one-time `backend_swap_warning` event when swapping
        TO those backends so the user knows.
        """
        self.backend = backend
        if reset_history:
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

    def _run_post_edit_feedback(self, edited_path: str) -> Iterator[dict]:
        """
        After a successful file_edit/file_write, optionally run the project
        linter and/or test command on the changed file. If feedback is found,
        inject it into conversation_history as a synthetic user turn so the
        next iteration sees it.

        Doom-loop guard: tracks per-file content hash of the last feedback
        injected; identical feedback is not re-injected.
        """
        import hashlib
        import os as _os

        # Resolve to absolute path under project_path if needed
        abs_path = edited_path
        if not _os.path.isabs(abs_path) and self.project_path:
            abs_path = _os.path.join(self.project_path, abs_path)
        abs_path = _os.path.normpath(abs_path)

        # ── Lint ──
        if self.auto_lint_enabled:
            try:
                from .lint import lint_file
                lint_result = lint_file(self.project_path, abs_path, timeout=10.0)
            except Exception as exc:
                lint_result = {"error": f"lint runner crashed: {exc}", "ok": True, "errors": ""}

            if not lint_result.get("ok") and lint_result.get("errors"):
                errors_text = lint_result["errors"]
                fingerprint = hashlib.sha256(f"lint:{abs_path}:{errors_text}".encode("utf-8")).hexdigest()
                if self._lint_feedback_cache.get(abs_path) != fingerprint:
                    self._lint_feedback_cache[abs_path] = fingerprint
                    linter_name = lint_result.get("linter", "linter")
                    truncated = errors_text if len(errors_text) <= 4000 else errors_text[:4000] + "\n…(truncated)"
                    msg = f"[auto-lint] {linter_name} reported issues in {edited_path}:\n{truncated}"
                    self.conversation_history.append({"role": "user", "content": msg})
                    yield make_event(
                        EngineEvent.STATUS,
                        message=f"Auto-lint feedback injected ({linter_name})",
                    )

        # ── Tests ──
        if self.auto_test_enabled:
            try:
                from .auto_test import run_tests_for_edit
                test_result = run_tests_for_edit(
                    self.project_path,
                    abs_path,
                    command=self.auto_test_command,
                    timeout=60.0,
                )
            except Exception as exc:
                test_result = {"error": f"test runner crashed: {exc}", "ok": True, "output": ""}

            if not test_result.get("ok") and test_result.get("output"):
                output = test_result["output"]
                fingerprint = hashlib.sha256(f"test:{abs_path}:{output}".encode("utf-8")).hexdigest()
                if self._test_feedback_cache.get(abs_path) != fingerprint:
                    self._test_feedback_cache[abs_path] = fingerprint
                    target = test_result.get("target", "")
                    truncated = output if len(output) <= 4000 else output[:4000] + "\n…(truncated)"
                    msg = f"[auto-test] tests failed for {edited_path} (ran: {target}):\n{truncated}"
                    self.conversation_history.append({"role": "user", "content": msg})
                    yield make_event(
                        EngineEvent.STATUS,
                        message=f"Auto-test feedback injected ({target})",
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
        # One corrective nudge per turn before the doom-loop hard-stop fires.
        self._doom_loop_nudged = False

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
            wd = self.project_path or os.getcwd()
            instructions = get_system_instructions(
                plan_mode=is_planning,
                project_instructions=self.project_instructions,
                working_directory=wd,
            )

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

                todo_items = parse_markdown_todos(full_text)
                if todo_items:
                    done_ct = sum(1 for t in todo_items if t.get("done"))
                    yield make_event(
                        EngineEvent.TODOS_UPDATED,
                        todos=todo_items,
                        done=done_ct,
                        total=len(todo_items),
                    )

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

                # Execution policy check (declarative rules, evaluated first)
                if self.execution_policy:
                    from .policies import PolicyAction
                    policy_action = self.execution_policy.evaluate(fn_name, fn_args)
                    if policy_action == PolicyAction.DENY:
                        reason = self.execution_policy.get_reason(fn_name, fn_args)
                        result_output = f"Blocked by policy: {reason or 'denied'}"
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

                # Permission check (three-tier autonomy model)
                approved = self._should_auto_approve(fn_name)
                if not approved:
                    # Policy says PROMPT — defer to permission callback
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
                    # Allowlist guard: when this Session was constructed with a
                    # filtered tool list (e.g. via specialist dispatch), refuse
                    # to dispatch tools the model invented or pulled from text-mode
                    # XML blocks. The filter at the API/system-prompt layer hints
                    # the model away from disallowed tools, but doesn't enforce —
                    # this is the real boundary.
                    if self._allowed_tools is not None:
                        allowed_names = {t.get("function", {}).get("name", "")
                                          for t in self._allowed_tools}
                        if fn_name not in allowed_names:
                            denial = (
                                f"Tool '{fn_name}' is not in this session's allowlist. "
                                f"Allowed tools: {sorted(allowed_names)}"
                            )
                            yield make_event(EngineEvent.TOOL_RESULT,
                                            name=fn_name, call_id=call_id,
                                            output=denial, is_error=True,
                                            denied=True, elapsed=0.0)
                            self.conversation_history.append({
                                "role": "tool_call", "name": fn_name,
                                "arguments": fn_args_str, "call_id": call_id,
                                "content": f"Called {fn_name}",
                            })
                            self.conversation_history.append({
                                "role": "tool_result", "call_id": call_id,
                                "content": denial,
                            })
                            continue

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

                    result = execute_tool(
                        fn_name, fn_args,
                        cancel_event=self._cancel_event,
                        project_path=self.project_path or "",
                        settings=getattr(self, "_settings_ref", None),
                    )

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

                    # Auto-lint / auto-test feedback loop: after a successful edit,
                    # optionally run the project linter (and/or tests) on the changed file
                    # and inject the output back into the conversation as a synthetic user
                    # turn so the model sees it on the next iteration.
                    if (
                        not result.is_error
                        and fn_name in ("file_edit", "file_write")
                        and (self.auto_lint_enabled or self.auto_test_enabled)
                        and self.project_path
                    ):
                        edited_path = fn_args.get("path") or ""
                        if edited_path:
                            for fb_event in self._run_post_edit_feedback(edited_path):
                                yield fb_event

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
            # Two-stage: nudge on the first repeat, hard-stop only if the
            # agent ignores the nudge. This avoids false positives during
            # legitimate retry-with-same-args patterns (e.g. flaky reads)
            # while still catching genuine stuck loops.
            identical_count = _count_trailing_identical_tool_calls(self.conversation_history) if has_tool_calls else 0

            if has_tool_calls and identical_count >= DOOM_LOOP_THRESHOLD:
                last_call = next(
                    (e for e in reversed(self.conversation_history)
                     if e.get("role") == "tool_call"),
                    {},
                )
                tool_name = last_call.get("name", "the same tool")
                yield make_event(
                    EngineEvent.ERROR,
                    message=(
                        f"Stopped: the agent called `{tool_name}` with the same "
                        f"arguments {identical_count} times in a row and isn't making "
                        f"progress. Try rephrasing your request or switching to a "
                        f"stronger model."
                    ),
                )
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

                # Early-warning nudge: if the agent is starting to repeat itself,
                # tell it to try a different approach BEFORE the hard stop fires.
                # One nudge per turn — we don't want to spam the conversation.
                if identical_count >= DOOM_LOOP_NUDGE_AT and not self._doom_loop_nudged:
                    last_call = next(
                        (e for e in reversed(self.conversation_history)
                         if e.get("role") == "tool_call"),
                        {},
                    )
                    tool_name = last_call.get("name", "")
                    current_msg = (
                        f"You just called `{tool_name}` with the same arguments "
                        f"{identical_count} times in a row. The result will not change — "
                        f"try a DIFFERENT approach: read a different file, search a "
                        f"different term, or summarize what you have and answer the "
                        f"user. Do NOT call `{tool_name}` again with these arguments."
                    )
                    self._doom_loop_nudged = True
                else:
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
        child.copy_execution_context_from(self)

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
                EngineEvent.TODOS_UPDATED.value,
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
