"""
Session management for the Resonant Engine.

A Session holds conversation history, manages the agentic loop,
and coordinates between backends and tools.
"""

import hashlib
import base64
import copy
import json
import os
import re
import sys
import time
import logging
import threading
import platform as plat
import queue
import uuid as _uuid
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

from ..backends import (
    EVENT_TEXT_DELTA,
    EVENT_TOOL_CALL,
    EVENT_DONE,
    EVENT_ERROR,
    EVENT_BACKEND_STATUS,
)
from ..events import EngineEvent, make_event
from ..content import build_user_content
from .tools import (
    AGENT_TOOLS,
    BATCH_ALLOWED_TOOL_NAMES,
    DIRECTOR_TOOLS,
    execute_tool,
    get_tool_icon,
)
from .agents import get_agent_type
from .agent_runtime import AgentHandoff, AgentRegistry, AgentStatus
from .artifacts import ArtifactKind, ArtifactStore
from .compression import (
    CONTEXT_HEADROOM_RATIO,
    compress,
    estimate_tokens,
    model_context_budget,
    should_compress,
)
from .hooks import HookResult, HookRunner, HookType
from .model_prompts import build_model_prompt, get_model_prompt_profile
from .tool_arguments import ToolArgumentError, normalize_tool_arguments
from .turn_outcomes import (
    VALIDATION_TOOL_NAMES,
    WRITE_TOOL_NAMES,
    classify_turn_outcome,
    request_requires_workspace_change,
    response_promises_future_action,
    unique_strings,
)

logger = logging.getLogger(__name__)


class ToolBoundaryViolation(Exception):
    """A tool call escaped the active session's execution boundary."""


# ── Doom Loop Detection ────────────────────────────────────────────────
# Repetition signals are advisory only. They can redirect an unproductive
# model, but never terminate a run that may be doing valid long-horizon work.
DOOM_LOOP_NUDGE_AT = 2

# v0.3.3 — sliding-window cycle detection. The strict trailing-identical
# check above only catches `tool A → tool A → tool A` back-to-back. Real
# stuck specialists also exhibit `tool A → tool B → tool A → tool C →
# tool A` — 3 occurrences of the same call inside a 12-call window. The
# windowed signature multiset catches both.
CYCLE_WINDOW = 12
CYCLE_WINDOW_REPEAT = 3

CORE_TOOL_NAMES = frozenset({
    "search_tools",
    "bash",
    "file_read",
    "file_write",
    "file_edit",
    "glob",
    "grep",
    "batch",
    "task",
    "await_user",
})

# The provider sees these short descriptions on every inference step. Richer
# discovery copy stays in AGENT_TOOLS for search_tools ranking and UI help, but
# does not need to occupy the model's stable prefix.
_CORE_TOOL_DESCRIPTIONS = {
    "bash": "Run a non-interactive shell command with an optional timeout.",
    "file_read": "Read a file, optionally by line offset and limit.",
    "file_write": "Create or overwrite a file with complete content.",
    "file_edit": "Replace uniquely identified text in an existing file.",
    "glob": "Find paths matching a glob pattern.",
    "grep": "Search file contents with a regular expression.",
    "batch": "Run independent read-only tool calls in parallel.",
    "task": "Delegate one bounded independent assignment to a sub-agent.",
    "search_tools": "Load a specialized tool by capability when the core tools are insufficient.",
    "await_user": "Ask one genuinely blocking question; include and recommend an option when possible.",
}


def _compact_provider_tool(tool: dict) -> dict:
    """Return a concise provider schema without mutating the canonical tool."""
    compact = copy.deepcopy(tool)
    function = compact.get("function", {})
    name = str(function.get("name") or "")
    if name in _CORE_TOOL_DESCRIPTIONS:
        function["description"] = _CORE_TOOL_DESCRIPTIONS[name]

    def shorten_descriptions(value):
        if isinstance(value, dict):
            for key, item in list(value.items()):
                if key == "description" and isinstance(item, str) and len(item) > 180:
                    sentence = item.split(". ", 1)[0].strip()
                    value[key] = (sentence or item[:177]).rstrip(". ") + "."
                else:
                    shorten_descriptions(item)
        elif isinstance(value, list):
            for item in value:
                shorten_descriptions(item)

    shorten_descriptions(function.get("parameters", {}))
    return compact

# Read-only tools are classified for preflight behavior only. There is no
# lookup cap: large repositories can legitimately require extensive discovery.
READ_ONLY_TOOLS = frozenset({"glob", "grep", "file_read"})

_OPEN_ENDED_NEXT_STEP_PATTERNS = (
    r"\bwhat(?:'s| is|s) (?:the )?next\b",
    r"\bwhat(?:'s| is|s) the next move\b",
    r"\bwhat should (?:i|we) do next\b",
    r"\bhow should (?:i|we) proceed\b",
    r"\bshould (?:i|we) (?:continue|proceed|keep going)\b",
    r"\b(?:do you want|would you like) me to\b",
)


def _is_open_ended_next_step_question(question: str) -> bool:
    """Reject permission/continuation prompts that should be final prose."""
    normalized = re.sub(
        r"\s+", " ", str(question or "").replace("’", "'")
    ).strip().casefold()
    return any(re.search(pattern, normalized) for pattern in _OPEN_ENDED_NEXT_STEP_PATTERNS)


PREFLIGHT_RESEARCH_TOOLS = READ_ONLY_TOOLS | frozenset({
    "batch", "git_status", "git_diff", "git_log", "skill_view", "search_tools",
})

_READ_ONLY_COMMAND_PREFIXES = (
    "git status", "git diff", "git log", "git show", "git branch --show-current",
    "rg ", "grep ", "find ", "ls", "dir", "get-childitem", "select-string",
    "python -m pytest --collect-only", "pytest --collect-only",
)


_READ_ONLY_SHELL_SEGMENT_PREFIXES = _READ_ONLY_COMMAND_PREFIXES + (
    "cd ", "set-location ", "pushd ", "popd",
    "echo ", "write-output ",
    "python -m pytest", "pytest ", "py -m pytest",
    "tail ", "head ", "findstr ", "more", "select-object ", "out-string",
)


def _shell_segment_is_read_only(segment: str) -> bool:
    value = str(segment or "").strip()
    if not value or value.startswith(_READ_ONLY_SHELL_SEGMENT_PREFIXES):
        return True
    # Test runners are commonly invoked through a project-local virtual
    # environment, so their prefix is repository-specific.  Running tests
    # validates work but does not mean implementation has started.
    return bool(re.match(
        r"^(?:&\s*)?(?:[^\s]+[\\/])?"
        r"(?:python(?:\d+(?:\.\d+)*)?)(?:\.exe)?\s+-m\s+pytest\b",
        value,
    ))


def _shell_command_is_read_only(command: str) -> bool:
    """Recognize common chained discovery and validation commands.

    This is deliberately conservative: every shell segment must have a known
    read-only prefix, and output redirection to a file keeps the command on the
    mutating path.  It covers the normal ``cd && dir && git status`` and
    ``cd && pytest`` probes used by coding models without treating arbitrary
    shell execution as safe.
    """
    normalized = str(command or "").strip().casefold()
    if not normalized:
        return True
    # Redirecting stdout/stderr to another stream is harmless; other output
    # redirection may create or overwrite workspace files.
    without_stream_redirects = re.sub(r"\b[12]?>\s*&[12]\b", "", normalized)
    if re.search(r"(?<!<)>{1,2}(?!&)", without_stream_redirects):
        return False

    segments = re.split(r"\s*(?:&&|\|\||;|\|)\s*", without_stream_redirects)
    return bool(segments) and all(_shell_segment_is_read_only(segment) for segment in segments)


def _tool_may_mutate_workspace(tool_name: str, arguments: dict) -> bool:
    if tool_name in WRITE_TOOL_NAMES or tool_name in {"git_commit", "git_branch_create"}:
        return True
    if tool_name == "batch":
        calls = arguments.get("calls") or []
        return any(
            _tool_may_mutate_workspace(str(call.get("name") or ""), call.get("arguments") or {})
            for call in calls if isinstance(call, dict)
        )
    if tool_name != "bash":
        return False
    return not _shell_command_is_read_only(arguments.get("command") or "")


def _tool_starts_implementation(tool_name: str, arguments: dict) -> bool:
    """Return whether a tool call closes the initial-alignment window."""
    if not tool_name or tool_name == "await_user":
        return False
    if tool_name in PREFLIGHT_RESEARCH_TOOLS:
        return False
    if tool_name == "bash":
        return _tool_may_mutate_workspace(tool_name, arguments)
    # Preserve the conservative boundary for delegated, MCP, computer-use,
    # and other stateful tools whose side effects are provider-defined.
    return True


# A cloud model can occasionally terminate a successful HTTP stream with only
# hidden reasoning metadata and no user-visible text or tool call. Treating
# that as completed produces a blank answer and a misleading green "Done".
EMPTY_RESPONSE_RETRY_LIMIT = 2
PROMISE_CONTINUATION_LIMIT = 2

def cycle_window_repeat_for_model(model_name: str | None) -> int:
    """Return the model-neutral advisory cycle threshold.

    ``model_name`` remains accepted for compatibility with integrations that
    already call this helper. Repetition is behavioral evidence rather than a
    reason to encode policy for named models.
    """
    return CYCLE_WINDOW_REPEAT


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


def _windowed_cycle_repeat(history: list, *, window: int = CYCLE_WINDOW) -> tuple[int, str, str]:
    """Return (max_repeat_count, tool_name, args_repr) for the most-repeated
    tool-call signature inside the last `window` calls of the current turn.

    A return of (1, "", "") means no signature appeared more than once.
    Useful when the agent is varying its calls just enough to dodge the
    strict trailing check but is clearly cycling through the same handful
    of probes (the C:\\Dev scavenger-hunt from Bug #25 was the trigger).
    """
    counts: dict[tuple, int] = {}
    last_winner: tuple = ("", "")
    seen = 0
    for entry in reversed(history):
        role = entry.get("role")
        if role == "user":
            break  # current turn only
        if role != "tool_call":
            continue
        sig = (entry.get("name", ""), entry.get("arguments", ""))
        counts[sig] = counts.get(sig, 0) + 1
        if counts[sig] > counts.get(last_winner, 0):
            last_winner = sig
        seen += 1
        if seen >= window:
            break
    if not counts:
        return 0, "", ""
    return counts[last_winner], last_winner[0], last_winner[1]


# ── System Instructions ────────────────────────────────────────────────

def get_system_instructions(
    plan_mode: bool = False,
    project_instructions: str | None = None,
    *,
    working_directory: str | None = None,
    model_name: str | None = None,
    prompt_role: str = "primary",
    role_instructions: str | None = None,
) -> str:
    """Build layered, model-aware system instructions."""
    layers = get_system_instruction_layers(
        plan_mode=plan_mode,
        project_instructions=project_instructions,
        working_directory=working_directory,
        model_name=model_name,
        prompt_role=prompt_role,
        role_instructions=role_instructions,
    )
    return "\n\n".join(layer["content"] for layer in layers)


def get_system_instruction_layers(
    plan_mode: bool = False,
    project_instructions: str | None = None,
    *,
    working_directory: str | None = None,
    model_name: str | None = None,
    prompt_role: str = "primary",
    role_instructions: str | None = None,
) -> list[dict[str, str]]:
    """Return the exact assembled prompt as named, inspectable layers."""
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

    runtime = "\n".join((
        f"Environment: {platform_name}",
        platform_hints,
        f"Working directory: {working_directory or os.getcwd()}",
    ))
    layers = [
        {"id": "runtime", "label": "Runtime environment", "content": runtime},
        {
            "id": "model_profile",
            "label": "Agent contract, model profile, and role",
            "content": build_model_prompt(model_name, role=prompt_role),
        },
    ]

    if project_instructions:
        layers.append({
            "id": "project",
            "label": "Project instructions",
            "content": "\n\n".join((
                "--- PROJECT INSTRUCTIONS ---",
                project_instructions.strip(),
                "--- END PROJECT INSTRUCTIONS ---",
            )),
        })

    if plan_mode:
        layers.append({
            "id": "mode",
            "label": "Plan mode",
            "content": "\n\n".join((
                "--- CURRENT MODE: PLAN ---",
                "Do not call tools in this response. Produce a concise, grounded "
                "numbered plan with dependencies, intended tools, verification, "
                "and real risks or unresolved product decisions. Do not invent "
                "repository facts that have not been inspected.",
                "--- END CURRENT MODE ---",
            )),
        })
    else:
        layers.append({
            "id": "tools",
            "label": "Tool notes",
            "content": (
                "Tool notes: `bash` is non-interactive and time-limited. Prefer "
                "`file_edit` for existing files. Use `batch` only for independent "
                "read-only calls."
            ),
        })

    if role_instructions and role_instructions.strip():
        layers.append({
            "id": "scoped_role",
            "label": "Scoped role instructions",
            "content": "\n\n".join((
                "--- SCOPED ROLE INSTRUCTIONS ---",
                role_instructions.strip(),
                "--- END SCOPED ROLE INSTRUCTIONS ---",
            )),
        })

    return layers


def inspect_system_instructions(**kwargs) -> dict:
    """Build metadata and the exact text sent as system instructions."""
    model_name = kwargs.get("model_name")
    profile = get_model_prompt_profile(model_name)
    layers = get_system_instruction_layers(**kwargs)
    prompt = "\n\n".join(layer["content"] for layer in layers)
    return {
        "model": model_name or "",
        "family": profile.family,
        "profile": profile.display_name,
        "role": kwargs.get("prompt_role", "primary"),
        "plan_mode": bool(kwargs.get("plan_mode", False)),
        "characters": len(prompt),
        "estimated_tokens": (len(prompt) + 3) // 4,
        "sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        "layers": [
            {
                **layer,
                "characters": len(layer["content"]),
                "estimated_tokens": (len(layer["content"]) + 3) // 4,
            }
            for layer in layers
        ],
        "prompt": prompt,
    }


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
        max_steps: Optional[int] = None,
        max_tokens: int | None = None,
        auto_approve: bool = True,
        auto_plan: bool = False,
        parent_session: Optional["Session"] = None,
        allowed_tools: Optional[list[dict]] = None,
        project_instructions: Optional[str] = None,
        cancel_event: Optional[threading.Event] = None,
        role_instructions: Optional[str] = None,
        prompt_role: str = "primary",
        pause_event: Optional[threading.Event] = None,
    ):
        self.backend = backend
        try:
            parsed_max_steps = int(max_steps) if max_steps is not None else 0
        except (TypeError, ValueError):
            parsed_max_steps = 0
        self.max_steps: Optional[int] = parsed_max_steps if parsed_max_steps > 0 else None
        self.max_tokens = max_tokens
        self.auto_approve = auto_approve
        self.auto_plan = auto_plan
        self.conversation_history: list = []
        self.todos: list[dict] = []
        self.plan_mode: bool = False
        self.parent_session = parent_session
        self._allowed_tools = allowed_tools  # None = use AGENT_TOOLS
        self._pending_permission: Optional[dict] = None
        self._pending_choice: Optional[dict] = None
        self.project_instructions = project_instructions
        self.role_instructions = role_instructions
        self.prompt_role = prompt_role
        self.hook_runner: Optional[HookRunner] = None
        self.mcp_tools: list[dict] = []  # Extra tools from MCP servers
        self._engram = None  # EngramIntegration, set externally
        self._codebase_index = None  # CodebaseIndex, set externally
        self._skill_context_provider = None
        self._last_context_sources: dict[str, dict] = {}
        self._compression_count = 0
        self._cancel_event = cancel_event or threading.Event()
        self._pause_event = pause_event or threading.Event()
        self._steering_queue: queue.SimpleQueue[dict[str, str]] = queue.SimpleQueue()
        self._steering_lock = threading.Lock()
        self.project_path: Optional[str] = None  # Set externally for path resolution
        # Three-tier autonomy: suggest (read-only) | auto-edit (files ok) | full-auto (sandboxed)
        self.autonomy_tier: str = "full-auto" if auto_approve else "suggest"
        self.sandbox = None  # PathSandbox, set externally
        self.event_logger = None  # EventLogger, set externally for JSONL logging
        self.agent_registry: Optional[AgentRegistry] = None
        self.agent_id: str = ""
        self.artifact_store: Optional[ArtifactStore] = None
        self.checkpoint_store = None
        self.checkpoint_display_provider = None
        self.flight_recorder = None
        self.context_broker = None
        self.model_role_router = None
        self.director_run = None
        self.benchmark_store = None
        self.model_role: str = prompt_role or "primary"
        self.worktree_manager = None
        self.capability_packs = None
        self.execution_policy = None  # ExecutionPolicy, set externally
        # Auto-feedback loops (set externally by AppState.build_session from settings)
        self.auto_lint_enabled: bool = False
        self.auto_test_enabled: bool = False
        self.auto_test_command: str = "pytest -x"
        # Doom-loop guards: file path → hash of last feedback we injected
        self._lint_feedback_cache: dict[str, str] = {}
        self._test_feedback_cache: dict[str, str] = {}
        # Per-turn flags prevent advisory repetition guidance from spamming.
        self._doom_loop_nudged: bool = False
        self._windowed_cycle_nudged: bool = False
        self._read_result_cache: dict[str, dict[str, object]] = {}
        self._last_checkpoint_tool_call: str = ""

    @property
    def is_subagent(self) -> bool:
        """True if this session was spawned by a parent session."""
        return self.parent_session is not None

    def context_snapshot(self) -> dict:
        """Return a compact, serializable view of the active context budget."""
        model = getattr(self.backend, "model", "") if self.backend else ""
        context_window = getattr(self.backend, "effective_context_tokens", None)
        budget = model_context_budget(model, context_window=context_window)
        if not context_window:
            context_window = int(budget / CONTEXT_HEADROOM_RATIO)

        inspected = inspect_system_instructions(
            plan_mode=self.plan_mode,
            project_instructions=self.project_instructions,
            working_directory=self.project_path,
            model_name=model,
            prompt_role=self.prompt_role,
            role_instructions=self.role_instructions,
        )
        history_tokens = estimate_tokens(self.conversation_history)
        source_tokens = sum(
            int(item.get("estimated_tokens", 0) or 0)
            for item in self._last_context_sources.values()
        )
        estimated_total = history_tokens + inspected["estimated_tokens"] + source_tokens

        role_counts: dict[str, int] = {}
        role_tokens: dict[str, int] = {}
        tool_payloads: list[dict] = []
        for index, entry in enumerate(self.conversation_history):
            role = str(entry.get("role") or "unknown")
            role_counts[role] = role_counts.get(role, 0) + 1
            tokens = estimate_tokens([entry])
            role_tokens[role] = role_tokens.get(role, 0) + tokens
            if role == "tool_result":
                content = entry.get("content", "")
                chars = len(content) if isinstance(content, str) else len(str(content))
                tool_payloads.append({
                    "index": index,
                    "name": entry.get("name") or "tool",
                    "characters": chars,
                    "estimated_tokens": tokens,
                })

        return {
            "model": model,
            "context_window": int(context_window),
            "compression_threshold": budget,
            "estimated_total_tokens": estimated_total,
            "utilization": min(1.0, estimated_total / max(1, int(context_window))),
            "history": {
                "entries": len(self.conversation_history),
                "estimated_tokens": history_tokens,
                "role_counts": role_counts,
                "role_tokens": role_tokens,
            },
            "system_prompt": {
                "estimated_tokens": inspected["estimated_tokens"],
                "sha256": inspected["sha256"],
                "layers": [
                    {
                        "id": layer["id"],
                        "label": layer["label"],
                        "estimated_tokens": layer["estimated_tokens"],
                    }
                    for layer in inspected["layers"]
                ],
            },
            "sources": self._last_context_sources,
            "largest_tool_payloads": sorted(
                tool_payloads,
                key=lambda item: item["estimated_tokens"],
                reverse=True,
            )[:6],
            "todos": list(self.todos),
            "compression_count": self._compression_count,
        }

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
        self._skill_context_provider = parent._skill_context_provider
        self.agent_registry = parent.agent_registry
        self.artifact_store = parent.artifact_store
        self.checkpoint_store = parent.checkpoint_store
        self.checkpoint_display_provider = parent.checkpoint_display_provider
        self.flight_recorder = parent.flight_recorder
        self.context_broker = parent.context_broker
        self.model_role_router = parent.model_role_router
        self.worktree_manager = parent.worktree_manager
        self.capability_packs = parent.capability_packs
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
        if self.director_run is not None and not self.is_subagent:
            base = copy.deepcopy(base)
            for tool in base:
                function = tool.get("function") or {}
                if function.get("name") == "task_batch":
                    tasks_schema = function["parameters"]["properties"]["tasks"]
                    tasks_schema["maxItems"] = self.director_run.config.max_parallel_workers
                    function["description"] = (
                        "Run independent ready Director tasks concurrently, up to the configured "
                        "worker-pool limit. Writing tasks always use isolated git worktrees."
                    )
                    break
            base = base + DIRECTOR_TOOLS
        if self.mcp_tools:
            base = base + self.mcp_tools
        return base

    @property
    def provider_tools(self) -> list[dict]:
        """Return the compact tool inventory advertised to the backend.

        Backends that support dynamic catalogs get a small stable core and can
        load specialist definitions on demand. Loaded definitions are included
        directly for providers such as Ollama; Kimi replays them through its
        native history catalog. Explicit specialist allowlists pass through.
        """
        if (
            not bool(getattr(self.backend, "supports_dynamic_tool_catalog", False))
            or self._allowed_tools is not None
        ):
            return self.tools

        core_names = set(CORE_TOOL_NAMES)
        if self.director_run is not None and not self.is_subagent:
            core_names.add("task_batch")
            core_names.update(
                tool.get("function", {}).get("name", "")
                for tool in DIRECTOR_TOOLS
            )
        selected = [
            _compact_provider_tool(tool)
            for tool in self.tools
            if tool.get("function", {}).get("name") in core_names
        ]

        # Kimi can declare additional tools as an in-history system item. Most
        # OpenAI/Ollama-compatible providers require loaded tools in the current
        # request's top-level `tools`, so merge prior search_tools results here.
        if not bool(getattr(self.backend, "dynamic_tool_catalog_via_history", False)):
            selected_names = {
                tool.get("function", {}).get("name", "") for tool in selected
            }
            for entry in self.conversation_history:
                if entry.get("role") != "tool_catalog":
                    continue
                for tool in entry.get("tools") or []:
                    name = tool.get("function", {}).get("name", "")
                    if name and name not in selected_names:
                        selected.append(_compact_provider_tool(tool))
                        selected_names.add(name)
        return selected

    def _search_tool_catalog(self, query: str, limit: int = 8) -> list[dict]:
        """Rank full tool definitions for on-demand tool loading."""
        terms = {
            term for term in re.findall(r"[a-z0-9_]+", str(query or "").casefold())
            if len(term) > 1
        }
        aliases = {
            "web": {"browser"},
            "page": {"browser"},
            "ui": {"browser", "computer"},
            "desktop": {"computer", "window", "screen"},
            "python": {"repl_python"},
            "node": {"repl_node"},
            "javascript": {"repl_node"},
            "terminal": {"bash", "process"},
            "shell": {"bash", "process"},
            "plugin": {"mcp"},
        }
        expanded = set(terms)
        for term in tuple(terms):
            expanded.update(aliases.get(term, set()))

        ranked: list[tuple[int, str, dict]] = []
        for tool in self.tools:
            function = tool.get("function", {})
            name = str(function.get("name") or "")
            if not name or name == "search_tools":
                continue
            description = str(function.get("description") or "")
            haystack = f"{name} {description}".casefold()
            name_terms = set(re.findall(r"[a-z0-9_]+", name.casefold()))
            score = 0
            for term in expanded:
                if term == name.casefold():
                    score += 12
                elif term in name_terms or name.casefold().startswith(term):
                    score += 8
                elif term in name.casefold():
                    score += 5
                elif term in haystack:
                    score += 2
            if score:
                ranked.append((score, name, tool))

        ranked.sort(key=lambda item: (-item[0], item[1]))
        safe_limit = min(12, max(1, int(limit or 8)))
        return [tool for _, _, tool in ranked[:safe_limit]]

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
        self.todos.clear()

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
        self.discard_steering()

    def pause(self) -> None:
        """Pause at the next safe agent-loop boundary."""
        self._pause_event.set()

    def resume(self) -> None:
        self._pause_event.clear()

    def reset_cancel(self):
        """Clear any pending cancellation request before starting a new run."""
        self._cancel_event.clear()

    def steer(self, text: str, *, message_id: str = "") -> bool:
        """Queue live user direction for the current agentic run.

        Steering is deliberately independent from cancellation. A backend
        inference or tool already in flight is allowed to finish; ``run``
        consumes this queue at its next safe loop boundary and adds the new
        direction to the same conversation history.
        """
        direction = str(text or "").strip()
        if not direction:
            return False
        with self._steering_lock:
            self._steering_queue.put({
                "message_id": str(message_id or ""),
                "text": direction,
            })
        return True

    def _drain_steering(self) -> list[dict[str, str]]:
        """Return all steering messages currently waiting, without blocking."""
        with self._steering_lock:
            return self._drain_steering_unlocked()

    def _drain_steering_unlocked(self) -> list[dict[str, str]]:
        """Drain steering while ``_steering_lock`` is already held."""
        messages: list[dict[str, str]] = []
        while True:
            try:
                messages.append(self._steering_queue.get_nowait())
            except queue.Empty:
                return messages

    def remove_steering(self, message_id: str) -> bool:
        """Remove one live direction if the agent has not consumed it yet."""
        target = str(message_id or "")
        if not target:
            return False
        with self._steering_lock:
            queued = self._drain_steering_unlocked()
            removed = False
            for item in queued:
                if not removed and item.get("message_id") == target:
                    removed = True
                    continue
                self._steering_queue.put(item)
            return removed

    def discard_steering(self) -> None:
        """Drop steering that can no longer belong to an active run."""
        self._drain_steering()

    def _log_event(self, event: dict) -> None:
        """Log an event to the JSONL logger if configured."""
        if self.event_logger:
            try:
                self.event_logger.log(event)
            except Exception:
                pass
        if self.agent_registry and self.agent_id:
            try:
                self.agent_registry.append_event(self.agent_id, event)
            except Exception:
                pass
        if self.flight_recorder:
            try:
                self.flight_recorder.record(event, agent_id=self.agent_id)
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

        if tool_name == "search_tools":
            return True
        if self.autonomy_tier == "suggest":
            return PathSandbox.is_read_only_tool(tool_name)
        elif self.autonomy_tier == "auto-edit":
            # File tools are OK, exec tools need approval
            return not PathSandbox.is_exec_tool(tool_name)
        else:  # full-auto
            return True

    def _prepare_workspace_tool_args(self, tool_name: str, tool_args: dict) -> dict:
        """Normalize and validate path-bearing tool arguments.

        ``project_path`` is the working directory for relative arguments while
        ``sandbox`` is the trust root. They may differ for specialists running
        inside an inherited working subdirectory.
        """
        if not isinstance(tool_args, dict):
            raise ToolBoundaryViolation(
                f"Tool '{tool_name}' arguments must be a JSON object, got "
                f"{type(tool_args).__name__}."
            )
        prepared = dict(tool_args)
        working_dir = self.project_path or os.getcwd()

        if tool_name == "batch":
            calls = prepared.get("calls", [])
            if not isinstance(calls, list):
                raise ToolBoundaryViolation("Batch 'calls' must be an array.")
            allowed_session_names = None
            if self._allowed_tools is not None:
                allowed_session_names = {
                    item.get("function", {}).get("name", "")
                    for item in self._allowed_tools
                }
            normalized_calls = []
            for index, call in enumerate(calls):
                if not isinstance(call, dict):
                    raise ToolBoundaryViolation(f"Batch call {index} must be an object.")
                child_name = str(call.get("name") or "")
                if child_name not in BATCH_ALLOWED_TOOL_NAMES:
                    raise ToolBoundaryViolation(
                        f"Batch child '{child_name}' is not a permitted read-only batch tool."
                    )
                if allowed_session_names is not None and child_name not in allowed_session_names:
                    raise ToolBoundaryViolation(
                        f"Batch child '{child_name}' is not in this specialist's tool allowlist."
                    )
                normalized_calls.append({
                    "name": child_name,
                    "arguments": self._prepare_workspace_tool_args(
                        child_name,
                        call.get("arguments", {}),
                    ),
                })
            prepared["calls"] = normalized_calls
            return prepared

        file_tools = {"file_write", "file_read", "file_edit"}
        search_tools = {"glob", "grep"}
        cwd_tools = {
            "bash",
            "git_status", "git_diff", "git_commit", "git_branch_create", "git_log",
            "repl_python_start", "repl_node_start",
        }

        if tool_name in file_tools:
            path = str(prepared.get("path") or "")
            if path and not os.path.isabs(path):
                path = os.path.join(working_dir, path)
            if path:
                prepared["path"] = path
        elif tool_name in search_tools:
            # Search patterns are data, not paths. Only the search root is
            # resolved; rewriting ``needle`` as ``<project>/needle`` corrupts
            # every normal grep regex.
            path = str(prepared.get("path") or working_dir)
            if not os.path.isabs(path):
                path = os.path.join(working_dir, path)
            prepared["path"] = path
        elif tool_name in cwd_tools:
            cwd = str(prepared.get("cwd") or working_dir)
            if not os.path.isabs(cwd):
                cwd = os.path.join(working_dir, cwd)
            prepared["cwd"] = cwd

        if self.sandbox:
            if tool_name in file_tools | search_tools and prepared.get("path"):
                prepared["path"] = self.sandbox.validate_path(prepared["path"])
            if tool_name == "glob" and prepared.get("pattern"):
                prepared["pattern"] = self.sandbox.validate_glob_pattern(
                    str(prepared["pattern"]),
                    base_path=prepared.get("path"),
                )
            if tool_name == "grep" and prepared.get("glob"):
                file_glob = str(prepared["glob"])
                if os.path.isabs(file_glob) or any(
                    separator in file_glob for separator in ("/", "\\")
                ):
                    raise ToolBoundaryViolation(
                        "Grep 'glob' must be a filename pattern, not a path."
                    )
            if tool_name in cwd_tools and prepared.get("cwd"):
                prepared["cwd"] = self.sandbox.validate_bash_cwd(prepared["cwd"])

        return prepared

    def _cancelled_events(self, total_start: float, total_steps: int) -> Iterator[dict]:
        if self.agent_registry and self.agent_id:
            try:
                record = self.agent_registry.get(self.agent_id)
                if record and not AgentStatus(record.status).terminal:
                    self.agent_registry.transition(
                        self.agent_id, AgentStatus.CANCELLED, current_action="",
                    )
            except Exception:
                logger.debug("Unable to finalize cancelled agent", exc_info=True)
        if self.flight_recorder:
            try:
                self.flight_recorder.close("cancelled", total_steps=total_steps)
            except Exception:
                pass
        if self.hook_runner:
            self.hook_runner.emit(
                HookType.SESSION_ERROR,
                {
                    "project_path": self.project_path or "",
                    "agent_id": self.agent_id,
                    "reason": "cancelled",
                    "steps": total_steps,
                },
            )
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

    def _compact_tool_result_for_context(
        self,
        tool_name: str,
        tool_args: dict,
        call_id: str,
        output: str,
        *,
        is_error: bool,
    ) -> tuple[str, dict]:
        """Deduplicate identical file reads while preserving full UI output."""
        if tool_name != "file_read" or is_error:
            return output, {}

        signature_payload = {
            "path": os.path.normcase(os.path.normpath(str(tool_args.get("path") or ""))),
            "offset": tool_args.get("offset"),
            "limit": tool_args.get("limit"),
        }
        signature = json.dumps(signature_payload, sort_keys=True, default=str)
        digest = hashlib.sha256(str(output).encode("utf-8", errors="replace")).hexdigest()
        previous = self._read_result_cache.get(signature)
        self._read_result_cache[signature] = {
            "digest": digest,
            "call_id": call_id,
            "characters": len(str(output)),
        }
        prior_call = str(previous.get("call_id") or "") if previous else ""
        prior_still_in_context = bool(prior_call) and any(
            entry.get("role") == "tool_result" and entry.get("call_id") == prior_call
            for entry in self.conversation_history
        )
        if not previous or previous.get("digest") != digest or not prior_still_in_context:
            return output, {}

        compact = (
            f"[Unchanged file_read result: identical to call {prior_call}; "
            f"sha256={digest[:12]}; {len(str(output))} characters omitted from "
            "repeated context. Use the earlier result.]"
        )
        return compact, {
            "context_deduplicated": True,
            "context_original_characters": len(str(output)),
            "context_reference_call_id": prior_call,
            "context_sha256": digest,
        }

    def run(
        self,
        user_msg: str,
        on_permission: Optional[Callable] = None,
        on_choice: Optional[Callable] = None,
        on_user_input: Optional[Callable] = None,
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
            on_user_input: Callback(question, options) -> str for the
                          await_user tool. Synchronous — caller blocks
                          on a threading.Event until the GUI replies.
                          If None, await_user returns "(no user available)".
            images: Optional list of (image_bytes, media_type) for multimodal input
        """
        turn_text_blocks: list[str] = []
        turn_tool_names: list[str] = []
        turn_successful_tools: list[str] = []
        turn_failed_tools: list[str] = []
        turn_changed_files: list[str] = []
        turn_validation_tools: list[str] = []
        terminal_error = ""
        promise_continuations = 0
        empty_response_attempts = 0
        last_done_stats = None
        last_done_model = getattr(self.backend, "model", "") if self.backend else ""

        if self.director_run is not None and not self.is_subagent:
            try:
                from .director import DirectorPhase, DirectorRun

                if self.director_run.phase in {
                    DirectorPhase.COMPLETED.value,
                    DirectorPhase.FAILED.value,
                    DirectorPhase.CANCELLED.value,
                }:
                    prior = self.director_run
                    self.director_run = DirectorRun(
                        self.project_path or prior.project_path,
                        config=prior.config,
                        on_event=prior._on_event,
                    )
                    self.role_instructions = self.director_run.system_prompt()
                if not self.director_run.objective:
                    self.director_run.set_objective(user_msg)
            except Exception:
                logger.debug("Unable to initialize Director turn", exc_info=True)

        if self.agent_registry and not self.agent_id:
            try:
                record = self.agent_registry.create(
                    agent_type="primary" if not self.is_subagent else self.prompt_role,
                    prompt=user_msg,
                    parent_id=getattr(self.parent_session, "agent_id", "") or "",
                    model=last_done_model,
                    role=self.model_role,
                    workspace=self.project_path or "",
                    policy=self.autonomy_tier,
                    max_steps=self.max_steps,
                )
                self.agent_id = record.id
                self.agent_registry.attach_control(
                    self.agent_id,
                    cancel_event=self._cancel_event,
                    pause_event=self._pause_event,
                    steer=lambda message: self.steer(message),
                )
                self.agent_registry.transition(self.agent_id, AgentStatus.RUNNING)
            except Exception:
                logger.debug("Unable to register durable agent", exc_info=True)

        input_artifacts = []
        if images and self.artifact_store:
            for index, (image_bytes, media_type) in enumerate(images, start=1):
                try:
                    suffix = ".png" if media_type == "image/png" else ".jpg" if media_type == "image/jpeg" else ".bin"
                    input_artifacts.append(self.artifact_store.put_bytes(
                        image_bytes,
                        kind=ArtifactKind.IMAGE,
                        label=f"User image {index}",
                        source=self.agent_id or "user",
                        media_type=media_type,
                        suffix=suffix,
                        metadata={"input_index": index},
                    ))
                except Exception:
                    logger.debug("Unable to persist input image artifact", exc_info=True)

        # Store one normalized content contract now. Backend adapters decide
        # which parts are native and which require an honest text fallback.
        user_entry = {
            "role": "user",
            "content": build_user_content(user_msg, images),
        }
        if input_artifacts:
            user_entry["artifacts"] = [artifact.to_dict() for artifact in input_artifacts]
        self.conversation_history.append(user_entry)

        iteration = 0
        exec_step = 0
        current_msg = user_msg
        active_goal = user_msg
        total_start = time.time()
        # Older embedders supplied a runner with only the original
        # run_hooks(PRE/POST_TOOL) surface. Keep that contract intact while
        # treating all newer lifecycle emissions as no-ops for that runner.
        if self.hook_runner and not callable(getattr(self.hook_runner, "emit", None)):
            try:
                self.hook_runner.emit = lambda *args, **kwargs: HookResult(allowed=True)
            except Exception:
                logger.debug("Legacy hook runner cannot accept lifecycle adapter")
        executing_plan = False
        last_tool_used = None
        # Repetition guidance is one-shot per turn and never terminates work.
        self._doom_loop_nudged = False
        # v0.4.11 (T2.6) — same per-turn reset for the windowed-cycle nudge.
        self._windowed_cycle_nudged = False
        empty_response_retries = 0
        step_limit_reached = False
        implementation_started = False

        def consume_steering() -> list[dict[str, str]]:
            """Append live direction once before the next model inference."""
            nonlocal active_goal, current_msg
            messages = self._drain_steering()
            if not messages:
                return []

            directions = []
            for item in messages:
                direction = item["text"]
                directions.append(direction)
                self.conversation_history.append({
                    "role": "user",
                    "content": (
                        "<user_steer>\n"
                        f"{direction}\n"
                        "</user_steer>"
                    ),
                })
            combined = "\n\n".join(directions)
            active_goal = (
                f"{active_goal}\n\nAdditional live user direction:\n{combined}"
            )
            # The steer is already in conversation_history. An empty current
            # message tells provider adapters to continue directly from that
            # appended state instead of duplicating the direction and goal.
            current_msg = ""
            return messages

        def completion_payload(total_elapsed: float, total_steps: int) -> dict:
            assistant_text = "\n\n".join(turn_text_blocks).strip()
            changed_files = unique_strings(turn_changed_files)
            validation_tools = unique_strings(turn_validation_tools)
            successful_tools = unique_strings(turn_successful_tools)
            failed_tools = unique_strings(turn_failed_tools)
            tool_names = unique_strings(turn_tool_names)
            outcome = classify_turn_outcome(
                user_request=active_goal,
                assistant_text=assistant_text,
                changed_files=changed_files,
                validation_tools=validation_tools,
                successful_tools=successful_tools,
                terminal_error=terminal_error,
            )
            evidence = {
                "requires_workspace_change": request_requires_workspace_change(active_goal),
                "visible_answer": bool(assistant_text),
                "output_characters": len(assistant_text),
                "tool_calls": len(turn_tool_names),
                "tool_names": tool_names,
                "successful_tools": successful_tools,
                "failed_tools": failed_tools,
                "changed_files": changed_files,
                "validation_tools": validation_tools,
                "empty_response_attempts": empty_response_attempts,
                "promise_continuations": promise_continuations,
            }
            provider_stats = {
                str(key): value
                for key, value in (last_done_stats or {}).items()
                if isinstance(value, (int, float, str, bool)) or value is None
            }
            telemetry = {
                "model": last_done_model or backend_model or "",
                "outcome": outcome,
                "elapsed_seconds": round(float(total_elapsed), 6),
                "steps": int(total_steps),
                "tool_calls": len(turn_tool_names),
                "output_characters": len(assistant_text),
                "empty_response_attempts": empty_response_attempts,
                "promise_continuations": promise_continuations,
                "changed_files": len(changed_files),
                "validation_tools": validation_tools,
                "provider_stats": provider_stats,
            }
            return {"outcome": outcome, "evidence": evidence, "telemetry": telemetry}

        if self.cancel_requested:
            yield from self._cancelled_events(total_start, 0)
            return

        tool_mode = getattr(self.backend, 'tool_mode', 'native')
        if self.flight_recorder:
            try:
                system_prompt = get_system_instructions(
                    plan_mode=self.plan_mode,
                    project_instructions=self.project_instructions,
                    working_directory=self.project_path,
                    model_name=getattr(self.backend, "model", ""),
                    prompt_role=self.prompt_role,
                    role_instructions=self.role_instructions,
                )
                self.flight_recorder.configure(
                    backend=getattr(self.backend, "name", ""),
                    model=getattr(self.backend, "model", ""),
                    model_role=self.model_role,
                    prompt=user_msg,
                    system_prompt=system_prompt,
                    tools=self.provider_tools,
                    provider_options={"max_tokens": self.max_tokens, "tool_mode": tool_mode},
                    capability_profile={
                        "multimodal_input": bool(input_artifacts),
                        "artifact_bus": self.artifact_store is not None,
                        "durable_agents": self.agent_registry is not None,
                    },
                )
            except Exception:
                logger.debug("Unable to configure flight recorder", exc_info=True)
        _start_event = make_event(EngineEvent.SESSION_START,
                        plan_mode=self.plan_mode,
                        backend=self.backend.name,
                        model=self.backend.model,
                        tool_mode=tool_mode)
        self._log_event(_start_event)
        yield _start_event
        for artifact in input_artifacts:
            artifact_event = make_event(
                EngineEvent.ARTIFACT_CREATED,
                artifact_id=artifact.id,
                kind=artifact.kind,
                media_type=artifact.media_type,
                size=artifact.size,
                label=artifact.label,
                path=artifact.path,
            )
            self._log_event(artifact_event)
            yield artifact_event

        if self.hook_runner:
            hook = self.hook_runner.emit(
                HookType.SESSION_START,
                {
                    "project_path": self.project_path or "",
                    "agent_id": self.agent_id,
                    "model": getattr(self.backend, "model", ""),
                    "role": self.model_role,
                    "user_prompt": user_msg,
                },
            )
            if not hook.continue_run:
                yield make_event(EngineEvent.ERROR, message=hook.reason or "Stopped by session hook")
                return

        # Log session metadata for JSONL replay
        self._log_event({
            "event": "session.meta",
            "session_id": id(self),
            "project_path": self.project_path,
            "autonomy_tier": self.autonomy_tier,
            "sandbox_enabled": bool(self.sandbox),
        })

        # ── Context compression ──
        # v0.4.6 (T2.1) — pass `model_name` so the threshold is sized
        # to the actual model's context window. Pre-T2.1 every model
        # used the 100K default, which never fired for flash (smaller
        # window) and was overly conservative for pro (larger window).
        backend_model = getattr(self.backend, "model", None) if self.backend else None
        context_window = getattr(self.backend, "effective_context_tokens", None)

        # Resolve memory/RAG once for this user turn, then keep the system
        # prompt byte-stable across every tool step.
        turn_context = ""
        turn_sources: dict[str, str] = {}
        if self._engram and self._engram.enabled:
            try:
                memory_context = self._engram.get_context_for_prompt(user_msg) or ""
                turn_context += memory_context
                if memory_context:
                    turn_sources["memory"] = memory_context
            except Exception as e:
                logger.warning(f"Engram recall failed: {e}")
        if self._codebase_index and self._codebase_index.is_indexed:
            try:
                rag_context = self._codebase_index.get_context_for_prompt(user_msg) or ""
                turn_context += rag_context
                if rag_context:
                    turn_sources["rag"] = rag_context
            except Exception as e:
                logger.warning(f"RAG context failed: {e}")
        if callable(self._skill_context_provider):
            try:
                skill_context = self._skill_context_provider(user_msg)
                skill_text = getattr(skill_context, "block", skill_context) or ""
                turn_context += skill_text
                if skill_text:
                    turn_sources["skills"] = skill_text
            except Exception as e:
                logger.warning(f"Interactive skill lookup failed: {e}")
        if self.context_broker:
            try:
                explicit_items = self.context_broker.resolve_mentions(user_msg)
                explicit_context = self.context_broker.render(explicit_items)
                turn_context += explicit_context
                if explicit_context:
                    turn_sources["explicit"] = explicit_context
                    for item in explicit_items:
                        turn_sources[f"attachment:{item.id}"] = item.content
            except Exception as e:
                logger.warning("Explicit context resolution failed: %s", e)
        self._last_context_sources = {
            name: {
                "characters": len(text),
                "estimated_tokens": (len(text) + 3) // 4,
            }
            for name, text in turn_sources.items()
        }

        # Freeze the provider prefix for the entire model/tool loop. Volatile
        # state belongs in append-only messages; rebuilding or reordering the
        # system/tool prefix forces local models to prefill it again.
        base_instructions = get_system_instructions(
            plan_mode=self.plan_mode,
            project_instructions=self.project_instructions,
            working_directory=self.project_path or os.getcwd(),
            model_name=backend_model,
            prompt_role=self.prompt_role,
            role_instructions=self.role_instructions,
        ) + turn_context

        while True:
            if self.max_steps is not None and iteration >= self.max_steps:
                step_limit_reached = True
                break
            if self.cancel_requested:
                yield from self._cancelled_events(total_start, exec_step)
                return
            while self._pause_event.is_set() and not self.cancel_requested:
                time.sleep(0.05)
            for steering in consume_steering():
                yield make_event(
                    EngineEvent.STEER_APPLIED,
                    message_id=steering["message_id"],
                    text=steering["text"],
                    step=exec_step + 1,
                )
            # A single specialist turn can add dozens of tool results, so
            # enforce the real backend window before every inference step.
            if should_compress(
                self.conversation_history,
                model_name=backend_model,
                context_window=context_window,
            ):
                try:
                    if self.hook_runner:
                        pre_compact = self.hook_runner.emit(
                            HookType.PRE_COMPACT,
                            {
                                "project_path": self.project_path or "",
                                "agent_id": self.agent_id,
                                "history_entries": len(self.conversation_history),
                                "history_tokens": estimate_tokens(self.conversation_history),
                            },
                        )
                        if not pre_compact.allowed:
                            raise RuntimeError(pre_compact.reason or "Compaction blocked by hook")
                    old_count = len(self.conversation_history)
                    old_tokens = estimate_tokens(self.conversation_history)
                    compressed, summary = compress(
                        self,
                        model_name=backend_model,
                        context_window=context_window,
                    )
                    if summary:
                        self.conversation_history = compressed
                        self._compression_count += 1
                        yield make_event(
                            EngineEvent.COMPRESSION,
                            old_entries=old_count,
                            new_entries=len(compressed),
                            old_tokens=old_tokens,
                            new_tokens=estimate_tokens(compressed),
                            summary_preview=summary[:200],
                        )
                        if self.hook_runner:
                            self.hook_runner.emit(
                                HookType.POST_COMPACT,
                                {
                                    "project_path": self.project_path or "",
                                    "agent_id": self.agent_id,
                                    "old_entries": old_count,
                                    "new_entries": len(compressed),
                                    "summary": summary,
                                },
                            )
                except Exception as e:
                    logger.warning(f"Context compression failed: {e}")
            yield make_event(EngineEvent.CONTEXT_STATE, **self.context_snapshot())
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
            instructions = base_instructions

            if self.hook_runner:
                before_model = self.hook_runner.emit(
                    HookType.BEFORE_MODEL,
                    {
                        "project_path": self.project_path or "",
                        "agent_id": self.agent_id,
                        "model": backend_model or "",
                        "role": self.model_role,
                        "step": exec_step,
                        "message": current_msg,
                    },
                )
                if before_model.additional_context:
                    instructions += "\n\n" + before_model.additional_context
                if not before_model.allowed or not before_model.continue_run:
                    terminal_error = before_model.reason or "Model call blocked by hook"
                    yield make_event(EngineEvent.ERROR, message=terminal_error)
                    break

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
                    tools=[] if is_planning else self.provider_tools,
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
                        if fn_name:
                            turn_tool_names.append(fn_name)
                        fn_args_str = data.get("arguments", "{}")
                        try:
                            fn_args = normalize_tool_arguments(
                                fn_name,
                                fn_args_str,
                                self.tools,
                            )
                            fn_args_str = json.dumps(
                                fn_args,
                                ensure_ascii=False,
                                sort_keys=True,
                            )
                            data["arguments"] = fn_args_str
                            data["_normalized_arguments"] = fn_args
                        except ToolArgumentError as exc:
                            fn_args = {}
                            data["_argument_error"] = str(exc)
                        yield make_event(EngineEvent.TOOL_CALL,
                                        name=fn_name, arguments=fn_args,
                                        arguments_str=fn_args_str,
                                        call_id=data.get("call_id", ""),
                                        icon=get_tool_icon(fn_name))

                    elif event_type == EVENT_DONE:
                        cog_state = data.get("cognitive_state")
                        done_stats = data.get("stats")
                        done_model = data.get("model")
                        last_done_stats = done_stats
                        last_done_model = done_model or last_done_model

                    elif event_type == EVENT_ERROR:
                        terminal_error = data.get("message", "Unknown")
                        yield make_event(
                            EngineEvent.ERROR,
                            message=terminal_error,
                            discard_partial_output=bool(
                                data.get("discard_partial_output", False)
                            ),
                        )
                        elapsed = time.time() - total_start
                        yield make_event(EngineEvent.SESSION_END,
                                        total_elapsed=elapsed,
                                        total_steps=exec_step,
                                        **completion_payload(elapsed, exec_step))
                        return

                    elif event_type == EVENT_BACKEND_STATUS:
                        # v0.5.6a1 — backend-emitted operational status
                        # (e.g. transparent 503 retry on Ollama Cloud).
                        # Forward verbatim so downstream (autonomous-
                        # mission daemon, GUI) can surface "still
                        # alive, retrying" rather than leaving users
                        # staring at a stalled "thinking" counter.
                        if data.get("reset_partial_output"):
                            # A provider runner can disappear after streaming
                            # prose but before it commits any tool call. The
                            # backend can safely replay that generation; drop
                            # the abandoned prose so it is neither duplicated
                            # in the UI nor persisted in model history.
                            collected_text.clear()
                        yield make_event(EngineEvent.BACKEND_STATUS, **data)

            except KeyboardInterrupt:
                self.cancel()
                yield from self._cancelled_events(total_start, exec_step)
                return
            except Exception as e:
                terminal_error = f"Stream error: {e}"
                yield make_event(EngineEvent.ERROR, message=terminal_error)
                elapsed = time.time() - total_start
                yield make_event(EngineEvent.SESSION_END,
                                total_elapsed=elapsed,
                                total_steps=exec_step,
                                **completion_payload(elapsed, exec_step))
                return

            step_elapsed = time.time() - step_start

            # ── Process collected text ──
            full_text = "".join(collected_text).strip()
            full_text = strip_tool_call_tags(full_text)
            if self.hook_runner:
                after_model = self.hook_runner.emit(
                    HookType.AFTER_MODEL,
                    {
                        "project_path": self.project_path or "",
                        "agent_id": self.agent_id,
                        "model": done_model or backend_model or "",
                        "role": self.model_role,
                        "step": exec_step,
                        "text": full_text,
                        "tool_calls": tool_calls,
                        "stats": done_stats or {},
                    },
                )
                if after_model.additional_context:
                    self.conversation_history.append({
                        "role": "user",
                        "content": f"[Lifecycle feedback]\n{after_model.additional_context}",
                    })

            if not full_text and not tool_calls:
                empty_response_retries += 1
                empty_response_attempts += 1
                yield make_event(
                    EngineEvent.STATUS,
                    model=done_model,
                    stats=done_stats,
                    cognitive_state=cog_state,
                    elapsed=step_elapsed,
                )

                if empty_response_retries <= EMPTY_RESPONSE_RETRY_LIMIT:
                    yield make_event(
                        EngineEvent.BACKEND_STATUS,
                        kind="empty_response_retry",
                        attempt=empty_response_retries,
                        max=EMPTY_RESPONSE_RETRY_LIMIT,
                        model=backend_model,
                    )
                    yield make_event(
                        EngineEvent.STEP_END,
                        step=exec_step,
                        elapsed=step_elapsed,
                    )
                    # Empty provider responses are retries, not productive
                    # agent steps, so they do not consume max_steps.
                    iteration -= 1
                    current_msg = (
                        "Your previous turn returned no user-visible text and no "
                        "tool call. Continue the user's current request now. "
                        "Either provide a concrete answer or call the appropriate "
                        "tool; do not return reasoning-only or an empty response."
                    )
                    continue

                attempts = EMPTY_RESPONSE_RETRY_LIMIT + 1
                message = (
                    f"The model returned an empty response {attempts} times. "
                    "No answer was produced. Retry the request or switch models."
                )
                terminal_error = message
                logger.warning("%s model=%s", message, backend_model)
                yield make_event(EngineEvent.ERROR, message=message)
                yield make_event(
                    EngineEvent.STEP_END,
                    step=exec_step,
                    elapsed=step_elapsed,
                )
                break

            empty_response_retries = 0

            if full_text:
                turn_text_blocks.append(full_text)
                yield make_event(EngineEvent.TEXT_DONE, text=full_text)

                todo_items = parse_markdown_todos(full_text)
                if todo_items:
                    self.todos = todo_items
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
            terminal_suppressed_question = False

            # CLI backends (claude-code, codex) handle tool execution internally.
            # We received tool_call events for display only — skip execution.
            if has_tool_calls and getattr(self.backend, 'handles_tools', False):
                # Add a synthetic assistant message with tool info for history
                tool_summary = ", ".join(item.get("name", "") for item in tool_calls)
                self.conversation_history.append({
                    "role": "assistant",
                    "content": f"[CLI executed tools: {tool_summary}]"
                })
                for cli_item in tool_calls:
                    cli_name = cli_item.get("name", "")
                    if cli_name:
                        turn_successful_tools.append(cli_name)
                    if cli_name in WRITE_TOOL_NAMES:
                        cli_args = cli_item.get("_normalized_arguments", {})
                        cli_path = cli_args.get("path") if isinstance(cli_args, dict) else ""
                        if cli_path:
                            turn_changed_files.append(str(cli_path))
                    if cli_name in VALIDATION_TOOL_NAMES and turn_changed_files:
                        turn_validation_tools.append(cli_name)
                has_tool_calls = False  # Don't loop — CLI already completed
                yield make_event(EngineEvent.STATUS,
                                model=done_model, stats=done_stats,
                                cognitive_state=cog_state,
                                elapsed=step_elapsed)
                yield make_event(EngineEvent.STEP_END, step=exec_step, elapsed=step_elapsed)
                break  # Exit the agentic loop — CLI ran to completion

            reasoning_by_call_id = {
                item.get("call_id", ""): item.get("reasoning_content") or item.get("thinking")
                for item in tool_calls
                if item.get("call_id") and (item.get("reasoning_content") or item.get("thinking"))
            }
            provider_metadata_by_call_id = {
                item.get("call_id", ""): {
                    key: item[key]
                    for key in ("assistant_content", "response_id", "response_tool_calls")
                    if key in item
                }
                for item in tool_calls
                if item.get("call_id")
            }
            for item in tool_calls:
                if self.cancel_requested:
                    yield from self._cancelled_events(total_start, exec_step)
                    return
                fn_name = item.get("name", "")
                fn_args_str = item.get("arguments", "{}")
                call_id = item.get("call_id", "")
                fn_args = item.get("_normalized_arguments", {})
                argument_error = item.get("_argument_error", "")
                # Close the initial-alignment window only when the model
                # actually attempts a workspace mutation.  Read-only shell
                # probes and validation commands are still discovery: treating
                # every `bash` call as implementation caused legitimate late
                # preflight decisions to be suppressed, which in turn forced
                # an unnecessary extra model step.
                if (
                    fn_name
                    and _tool_starts_implementation(fn_name, fn_args)
                ):
                    implementation_started = True
                if argument_error:
                    turn_failed_tools.append(fn_name)
                    result_output = (
                        f"Tool arguments were malformed: {argument_error}. "
                        "Correct the arguments and call the tool again."
                    )
                    yield make_event(
                        EngineEvent.TOOL_RESULT,
                        name=fn_name,
                        call_id=call_id,
                        output=result_output,
                        is_error=True,
                        denied=True,
                        elapsed=0.0,
                    )
                    self.conversation_history.append({
                        "role": "tool_call",
                        "name": fn_name,
                        "arguments": fn_args_str,
                        "call_id": call_id,
                        "content": f"Called {fn_name}",
                    })
                    self.conversation_history.append({
                        "role": "tool_result",
                        "call_id": call_id,
                        "content": result_output,
                    })
                    continue

                # Tool call event already yielded during streaming (above)
                # No duplicate yield here — go straight to execution

                # Pre-tool hook check
                if self.hook_runner:
                    hook_result = self.hook_runner.run_hooks(
                        HookType.PRE_TOOL_USE,
                        context={
                            "tool_args": fn_args,
                            "project_path": self.project_path or "",
                            "agent_id": self.agent_id,
                            "call_id": call_id,
                        },
                        tool_name=fn_name,
                    )
                    if not hook_result.allowed:
                        turn_failed_tools.append(fn_name)
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
                    if hook_result.modified_args is not None:
                        fn_args = hook_result.modified_args
                        fn_args_str = json.dumps(fn_args, ensure_ascii=False, sort_keys=True)
                    if hook_result.additional_context:
                        self.conversation_history.append({
                            "role": "user",
                            "content": f"[Pre-tool lifecycle context]\n{hook_result.additional_context}",
                        })

                # Execution policy check (declarative rules, evaluated first)
                if self.execution_policy:
                    from .policies import PolicyAction
                    policy_action = self.execution_policy.evaluate(fn_name, fn_args)
                    if policy_action == PolicyAction.DENY:
                        turn_failed_tools.append(fn_name)
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
                    turn_failed_tools.append(fn_name)
                    # Policy says PROMPT — defer to permission callback
                    if on_permission:
                        approved = on_permission(fn_name, fn_args)
                    else:
                        approved = self.auto_approve  # Fallback to legacy flag

                if not approved and self.hook_runner:
                    permission_hook = self.hook_runner.emit(
                        HookType.PERMISSION_REQUEST,
                        {
                            "tool_name": fn_name,
                            "tool_args": fn_args,
                            "project_path": self.project_path or "",
                            "agent_id": self.agent_id,
                            "call_id": call_id,
                        },
                    )
                    if permission_hook.decision in {"allow", "approve"}:
                        approved = True
                    elif permission_hook.decision in {"deny", "block"}:
                        approved = False
                    if permission_hook.modified_args is not None:
                        fn_args = permission_hook.modified_args
                        fn_args_str = json.dumps(fn_args, ensure_ascii=False, sort_keys=True)

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

                # Universal recovery boundary. This is tied to the conversation
                # cursor and created before any tool likely to mutate files.
                if (
                    self.checkpoint_store
                    and call_id != self._last_checkpoint_tool_call
                    and _tool_may_mutate_workspace(fn_name, fn_args)
                ):
                    try:
                        checkpoint = self.checkpoint_store.create(
                            conversation_history=self.conversation_history,
                            display_events=(
                                self.checkpoint_display_provider()
                                if callable(self.checkpoint_display_provider) else []
                            ),
                            reason=f"Before {fn_name}",
                            agent_id=self.agent_id,
                            tool_name=fn_name,
                            metadata={"call_id": call_id, "arguments": fn_args},
                        )
                        self._last_checkpoint_tool_call = call_id
                        checkpoint_event = make_event(
                            EngineEvent.CHECKPOINT_CREATED,
                            checkpoint_id=checkpoint.id,
                            reason=checkpoint.reason,
                            agent_id=self.agent_id,
                            tool_name=fn_name,
                            sequence=checkpoint.sequence,
                        )
                        self._log_event(checkpoint_event)
                        yield checkpoint_event
                        if self.hook_runner:
                            self.hook_runner.emit(
                                HookType.CHECKPOINT_CREATED,
                                {
                                    "project_path": self.project_path or "",
                                    "agent_id": self.agent_id,
                                    "checkpoint": checkpoint.to_dict(),
                                },
                            )
                    except Exception as exc:
                        logger.warning("Session checkpoint failed before %s: %s", fn_name, exc)

                # Execute the tool
                if fn_name == "search_tools":
                    search_start = time.time()
                    matches = self._search_tool_catalog(
                        str(fn_args.get("query") or ""),
                        fn_args.get("limit", 8),
                    )
                    summaries = [
                        {
                            "name": tool.get("function", {}).get("name", ""),
                            "description": tool.get("function", {}).get("description", ""),
                        }
                        for tool in matches
                    ]
                    result_output = json.dumps(
                        {
                            "matches": summaries,
                            "instruction": (
                                "The matching tool definitions are now loaded. "
                                "Call the needed tool directly."
                                if matches else
                                "No matching specialized tools were found. Refine the capability query."
                            ),
                        },
                        ensure_ascii=False,
                    )
                    elapsed = time.time() - search_start
                    yield make_event(
                        EngineEvent.TOOL_RESULT,
                        name=fn_name,
                        call_id=call_id,
                        output=result_output,
                        is_error=False,
                        elapsed=elapsed,
                        metadata={"matches": [item["name"] for item in summaries]},
                        denied=False,
                    )
                    self.conversation_history.append({
                        "role": "tool_call",
                        "name": fn_name,
                        "arguments": fn_args_str,
                        "call_id": call_id,
                        "content": f"Called {fn_name}",
                    })
                    self.conversation_history.append({
                        "role": "tool_result",
                        "name": fn_name,
                        "call_id": call_id,
                        "content": result_output,
                    })
                    if matches:
                        self.conversation_history.append({
                            "role": "tool_catalog",
                            "tools": matches,
                            "content": "Dynamically loaded tool definitions.",
                        })
                    turn_successful_tools.append(fn_name)
                elif fn_name == "task":
                    # Task tool — spawn a sub-agent session
                    yield from self._execute_task(fn_args, call_id, fn_args_str)
                    turn_successful_tools.append(fn_name)
                elif fn_name == "task_batch":
                    yield from self._execute_task_batch(fn_args, call_id, fn_args_str)
                    turn_successful_tools.append(fn_name)
                elif fn_name.startswith("director_") and self.director_run is not None:
                    yield from self._execute_director_tool(fn_name, fn_args, call_id, fn_args_str)
                    turn_successful_tools.append(fn_name)
                elif fn_name == "await_user":
                    # v0.3.5 — pause and ask the user. Synchronously
                    # waits via the on_user_input callback. The GUI
                    # path is wired in app.py: WS event "await_user"
                    # → modal in app.js → user_input WS command →
                    # threading.Event released → callback returns →
                    # this branch resumes with the answer as the tool
                    # result text. If no callback was wired (CLI mode,
                    # tests), we return a sentinel and let the agent
                    # decide how to proceed.
                    import time as _time
                    awu_start = _time.time()
                    question = fn_args.get("question") or ""
                    options = fn_args.get("options") or []
                    if self.hook_runner:
                        input_hook = self.hook_runner.emit(
                            HookType.USER_INPUT_REQUEST,
                            {
                                "project_path": self.project_path or "",
                                "agent_id": self.agent_id,
                                "question": question,
                                "options": options,
                            },
                        )
                        if input_hook.additional_context:
                            question = f"{question}\n\n{input_hook.additional_context}".strip()
                    urgency = str(fn_args.get("urgency") or "alignment").strip().casefold()
                    suppressed_reason = ""
                    if _is_open_ended_next_step_question(question):
                        suppressed_reason = (
                            "Generic continuation and next-step questions are not shown to "
                            "the user. Complete the current task and present the outcome. "
                            "Put optional recommendations or next steps in the final response "
                            "as statements, not questions."
                        )
                    elif implementation_started and urgency != "catastrophic":
                        suppressed_reason = (
                            "Implementation has already started, so ordinary clarification is "
                            "closed. Resolve the issue using repository evidence and the safest "
                            "reasonable assumption, then continue. Only an imminent destructive, "
                            "security, or irreversible blocker may pause the user now."
                        )
                    recommended = re.sub(
                        r"\s*\(recommended\)\s*$", "",
                        str(fn_args.get("recommended_option") or ""),
                        flags=re.IGNORECASE,
                    ).strip()
                    if options:
                        clean_options = []
                        marked_index = None
                        for index, option in enumerate(options):
                            option_text = str(option).strip()
                            was_marked = bool(re.search(
                                r"\s*\(recommended\)\s*$", option_text,
                                flags=re.IGNORECASE,
                            ))
                            clean_option = re.sub(
                                r"\s*\(recommended\)\s*$", "", option_text,
                                flags=re.IGNORECASE,
                            ).strip()
                            clean_options.append(clean_option)
                            if was_marked and marked_index is None:
                                marked_index = index

                        recommended_index = next((
                            index for index, option in enumerate(clean_options)
                            if recommended and option.casefold() == recommended.casefold()
                        ), None)
                        if recommended_index is None:
                            recommended_index = marked_index if marked_index is not None else 0

                        options = [
                            f"{option} (Recommended)" if index == recommended_index else option
                            for index, option in enumerate(clean_options)
                        ]
                    if suppressed_reason:
                        answer = f"(question suppressed by Resonant policy: {suppressed_reason})"
                        terminal_suppressed_question = bool(full_text.strip())
                    elif on_user_input:
                        try:
                            answer = on_user_input(question, options)
                        except Exception as exc:
                            logger.exception("on_user_input callback raised")
                            answer = f"(error obtaining user input: {exc})"
                    else:
                        answer = "(no user available — proceed with your best judgment)"
                    awu_elapsed = _time.time() - awu_start
                    from .tools import ToolResult
                    awu_result = ToolResult(
                        output=str(answer or ""),
                        is_error=False,
                        elapsed=awu_elapsed,
                    )
                    yield make_event(EngineEvent.TOOL_RESULT,
                                    name=fn_name, call_id=call_id,
                                    output=awu_result.output, is_error=False,
                                    elapsed=awu_elapsed, metadata={
                                        "question": question,
                                        "suppressed": bool(suppressed_reason),
                                    },
                                    denied=False)
                    self.conversation_history.append({
                        "role": "tool_call", "name": fn_name,
                        "arguments": fn_args_str, "call_id": call_id,
                        "content": f"Called {fn_name}",
                    })
                    self.conversation_history.append({
                        "role": "tool_result", "call_id": call_id,
                        "content": awu_result.output,
                    })
                    turn_successful_tools.append(fn_name)
                elif fn_name.startswith("mcp_") and hasattr(self, '_mcp_manager') and self._mcp_manager:
                    # MCP tool — route to MCP server
                    import time as _time
                    mcp_start = _time.time()
                    mcp_result = self._mcp_manager.call_tool(fn_name, fn_args)
                    content = mcp_result.get("content")
                    if isinstance(content, list):
                        text_parts = [
                            item.get("text", "")
                            for item in content
                            if isinstance(item, dict) and item.get("type") == "text"
                        ]
                        mcp_output = "\n".join(part for part in text_parts if part)
                        if not mcp_output:
                            mcp_output = json.dumps(content, ensure_ascii=False)
                    elif isinstance(content, str):
                        mcp_output = content
                    else:
                        mcp_output = json.dumps(mcp_result, ensure_ascii=False, default=str)
                    from .tools import ToolResult
                    result = ToolResult(
                        output=mcp_output,
                        is_error=bool(mcp_result.get("isError") or "error" in mcp_result),
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
                    (turn_failed_tools if result.is_error else turn_successful_tools).append(fn_name)
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
                            turn_failed_tools.append(fn_name)
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

                    # Normalize every path-bearing call at one boundary. This
                    # also validates batch children before parallel fan-out.
                    from .sandbox import SandboxViolation
                    try:
                        fn_args = self._prepare_workspace_tool_args(fn_name, fn_args)
                    except (SandboxViolation, ToolBoundaryViolation) as exc:
                        turn_failed_tools.append(fn_name)
                        result_output = f"Blocked by tool boundary: {exc}"
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

                    history_output, context_meta = self._compact_tool_result_for_context(
                        fn_name,
                        fn_args,
                        call_id,
                        result.output,
                        is_error=result.is_error,
                    )
                    event_metadata = dict(result.metadata)
                    event_metadata.update(context_meta)
                    if self.artifact_store and isinstance(result.output, str) and len(result.output) > 50_000:
                        try:
                            artifact = self.artifact_store.put_text(
                                result.output,
                                kind=(ArtifactKind.TERMINAL if fn_name == "bash" else ArtifactKind.TEXT),
                                label=f"{fn_name} result",
                                source=call_id,
                                metadata={"tool_name": fn_name, "call_id": call_id},
                            )
                            reference = self.artifact_store.reference(artifact)
                            history_output = f"{history_output[:4000]}\n\n{reference}"
                            event_metadata["artifact"] = artifact.to_dict()
                        except Exception:
                            logger.debug("Unable to persist oversized tool result", exc_info=True)

                    if self.artifact_store and result.metadata.get("screenshot_b64"):
                        try:
                            media_type = result.metadata.get("media_type", "image/png")
                            artifact = self.artifact_store.put_bytes(
                                base64.b64decode(result.metadata["screenshot_b64"]),
                                kind=ArtifactKind.IMAGE,
                                label=f"{fn_name} screenshot",
                                source=call_id,
                                media_type=media_type,
                                suffix=".png" if media_type == "image/png" else ".bin",
                                metadata={"tool_name": fn_name, "call_id": call_id},
                            )
                            event_metadata["artifact"] = artifact.to_dict()
                        except Exception:
                            logger.debug("Unable to persist screenshot artifact", exc_info=True)

                    yield make_event(EngineEvent.TOOL_RESULT,
                                    name=fn_name, call_id=call_id,
                                    output=result.output, is_error=result.is_error,
                                    elapsed=result.elapsed, metadata=event_metadata,
                                    denied=False)

                    if result.is_error:
                        turn_failed_tools.append(fn_name)
                    else:
                        turn_successful_tools.append(fn_name)
                        if fn_name in WRITE_TOOL_NAMES:
                            changed_path = fn_args.get("path") or ""
                            if changed_path:
                                turn_changed_files.append(str(changed_path))
                        if fn_name in VALIDATION_TOOL_NAMES and turn_changed_files:
                            turn_validation_tools.append(fn_name)

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
                        "content": history_output,
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
                            if self.auto_lint_enabled:
                                turn_validation_tools.append("auto_lint")
                            if self.auto_test_enabled:
                                turn_validation_tools.append("auto_test")
                            for fb_event in self._run_post_edit_feedback(edited_path):
                                yield fb_event

                    # Post-tool hook
                    if self.hook_runner:
                        self.hook_runner.run_hooks(
                            HookType.POST_TOOL_USE,
                            context={
                                "tool_args": fn_args,
                                "result": result.output,
                                "is_error": result.is_error,
                                "project_path": self.project_path or "",
                                "agent_id": self.agent_id,
                                "call_id": call_id,
                            },
                            tool_name=fn_name,
                        )
                        if fn_name in VALIDATION_TOOL_NAMES:
                            validation_hook = self.hook_runner.emit(
                                HookType.VALIDATION_COMPLETE,
                                {
                                    "project_path": self.project_path or "",
                                    "agent_id": self.agent_id,
                                    "tool_name": fn_name,
                                    "result": result.output,
                                    "is_error": result.is_error,
                                    "changed_files": list(turn_changed_files),
                                },
                                tool_name=fn_name,
                            )
                            if not validation_hook.allowed:
                                self.conversation_history.append({
                                    "role": "user",
                                    "content": (
                                        "[Validation gate rejected completion]\n"
                                        f"{validation_hook.reason or validation_hook.error}"
                                    ),
                                })

                    if self.cancel_requested or result.metadata.get("cancelled"):
                        yield from self._cancelled_events(total_start, exec_step)
                        return

                if self.cancel_requested:
                    yield from self._cancelled_events(total_start, exec_step)
                    return

            if (
                terminal_suppressed_question
                and tool_calls
                and all(item.get("name") == "await_user" for item in tool_calls)
            ):
                # The model already gave the user a visible answer, then tried
                # to append a generic or disallowed clarification. The
                # synthetic suppression result exists only for protocol
                # completeness; asking the provider for another turn would
                # hide a finished answer behind a fresh spinner.
                has_tool_calls = False

            # ── Status ──
            if reasoning_by_call_id or provider_metadata_by_call_id:
                for entry in self.conversation_history:
                    if entry.get("role") != "tool_call":
                        continue
                    call_id = entry.get("call_id", "")
                    reasoning = reasoning_by_call_id.get(call_id)
                    if reasoning:
                        entry["reasoning_content"] = reasoning
                    entry.update(provider_metadata_by_call_id.get(call_id, {}))

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

            # ── Repetition signals ──
            # These are advisory only. Long-running agents may revisit the same
            # evidence legitimately, so repetition cannot terminate a run.
            identical_count = _count_trailing_identical_tool_calls(self.conversation_history) if has_tool_calls else 0

            # v0.3.3 — sliding-window cycle detection. Catches the looser
            # case where the agent isn't repeating *immediately* but
            # cycles through the same handful of probes (Bug #25's
            # scavenger hunt: 24 tool calls hunting for `C:\Dev\roguelite`
            # by varying findstr filters and target dirs). If any single
            # signature appears several times inside the recent call window.
            if has_tool_calls:
                # v0.4.9 (T2.4) — per-model thresholds. Pro gets more
                # tolerance (legitimate "retry with intentional small
                # variation" while reasoning); flash + everything else
                # keep the default tighter thresholds.
                _model_for_guards = (
                    getattr(self.backend, "model", None) if self.backend else None
                )
                _cycle_repeat_threshold = cycle_window_repeat_for_model(_model_for_guards)
                wrep_count, wrep_tool, wrep_args = _windowed_cycle_repeat(
                    self.conversation_history, window=CYCLE_WINDOW
                )
                # The post-tool-call block below may inject one recovery hint.
            # ── Continue or stop ──
            yield make_event(EngineEvent.STEP_END, step=exec_step, elapsed=step_elapsed)

            # A steer that arrived during this inference or tool batch belongs
            # to the same turn. Consume it before normal completion so even a
            # just-finished answer can be revised with the new direction.
            applied_steering = consume_steering()
            if applied_steering:
                for steering in applied_steering:
                    yield make_event(
                        EngineEvent.STEER_APPLIED,
                        message_id=steering["message_id"],
                        text=steering["text"],
                        step=exec_step + 1,
                    )
                continue

            if (
                not has_tool_calls
                and request_requires_workspace_change(active_goal)
                and not turn_changed_files
                and response_promises_future_action(full_text)
                and promise_continuations < PROMISE_CONTINUATION_LIMIT
            ):
                promise_continuations += 1
                yield make_event(
                    EngineEvent.BACKEND_STATUS,
                    kind="action_promise_continuation",
                    attempt=promise_continuations,
                    max=PROMISE_CONTINUATION_LIMIT,
                    model=backend_model,
                )
                iteration -= 1
                current_msg = (
                    "You promised to perform the requested workspace change but "
                    "ended the turn without taking action. Continue now: inspect "
                    "only what is necessary, make the change with tools, validate "
                    "it, and then report concrete evidence. Do not merely describe "
                    "what you intend to do."
                )
                continue

            if has_tool_calls:
                last_names = [item.get("name", "") for item in tool_calls if item.get("name")]
                if last_names:
                    last_tool_used = ", ".join(last_names)
                tool_calls = []
                collected_text = []

                # Early-warning nudge: if the agent is starting to repeat itself,
                # tell it to try a different approach without ending the run.
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
                # v0.4.11 (T2.6) — windowed-cycle nudge. The strict
                # doom-loop nudge above only catches back-to-back
                # identical calls; the windowed variant catches the
                # looser "varying probes for the same thing" pattern.
                # Trigger once when a recent signature starts dominating.
                # Doom-loop nudge wins if both fire on the same turn —
                # no need to stack messages.
                elif (
                    wrep_count >= max(2, _cycle_repeat_threshold - 1)
                    and not self._windowed_cycle_nudged
                    and not self._doom_loop_nudged
                ):
                    args_repr = wrep_args if len(wrep_args) <= 60 else wrep_args[:57] + "..."
                    current_msg = (
                        f"You've called `{wrep_tool}` with args `{args_repr}` "
                        f"{wrep_count} times in the last {CYCLE_WINDOW} steps "
                        f"(varying details slightly each time). You're cycling "
                        f"through probes instead of converging. STOP, reassess the "
                        f"available evidence, and choose a different approach, or "
                        f"summarize what you've learned and answer the user with the "
                        f"best supported outcome. Do not ask what to do next. "
                        f"Continue independently; this is guidance, not a run limit."
                    )
                    self._windowed_cycle_nudged = True
                else:
                    # Tool results already form a complete continuation boundary.
                    # Do not append a synthetic "continue" user turn: keeping
                    # history append-only and sparse improves KV-prefix reuse.
                    current_msg = ""
                continue
            else:
                if self.hook_runner:
                    completion_gate = self.hook_runner.emit(
                        HookType.TASK_COMPLETED,
                        {
                            "project_path": self.project_path or "",
                            "agent_id": self.agent_id,
                            "role": self.model_role,
                            "goal": active_goal,
                            "response": full_text,
                            "changed_files": unique_strings(turn_changed_files),
                            "validation_tools": unique_strings(turn_validation_tools),
                            "successful_tools": unique_strings(turn_successful_tools),
                            "failed_tools": unique_strings(turn_failed_tools),
                        },
                    )
                    if not completion_gate.allowed or completion_gate.retry:
                        iteration -= 1
                        current_msg = (
                            "A deterministic completion gate rejected this result. "
                            "Address the following requirement, validate it, and try "
                            "completion again:\n\n"
                            f"{completion_gate.reason or completion_gate.error or completion_gate.additional_context}"
                        )
                        continue
                break

        total_elapsed = time.time() - total_start

        # ── Engram: auto-remember session summary ──
        if self._engram and self._engram.enabled and not self.is_subagent:
            try:
                self._engram.session_summary(self.conversation_history)
            except Exception as e:
                logger.warning(f"Engram session summary failed: {e}")

        if step_limit_reached:
            terminal_error = f"Reached {self.max_steps} step limit — use /clear to reset"
            yield make_event(EngineEvent.ERROR, message=terminal_error)

        final_steps = exec_step if exec_step > 0 else iteration
        final_payload = completion_payload(total_elapsed, final_steps)
        if self.benchmark_store and not self.is_subagent:
            try:
                telemetry = final_payload.get("telemetry") or {}
                evidence = final_payload.get("evidence") or {}
                self.benchmark_store.record(
                    mode="director" if self.director_run else "single",
                    objective=active_goal,
                    outcome=str(final_payload.get("outcome") or "unknown"),
                    elapsed_seconds=total_elapsed,
                    steps=final_steps,
                    tool_calls=int(telemetry.get("tool_calls") or 0),
                    validation_tools=len(evidence.get("validation_tools") or []),
                    changed_files=len(evidence.get("changed_files") or []),
                    director_run_id=getattr(self.director_run, "id", ""),
                    provider_stats=telemetry.get("provider_stats") or {},
                )
            except Exception:
                logger.debug("Unable to record mode benchmark", exc_info=True)
        if self.agent_registry and self.agent_id and not self.is_subagent:
            try:
                evidence = final_payload.get("evidence") or {}
                handoff = AgentHandoff(
                    outcome=str(final_payload.get("outcome") or "completed"),
                    summary="\n\n".join(turn_text_blocks).strip(),
                    evidence=[
                        f"{evidence.get('tool_calls', 0)} tool calls",
                        f"{final_steps} steps",
                    ],
                    changed_files=list(evidence.get("changed_files") or []),
                    validation=list(evidence.get("validation_tools") or []),
                    blockers=[terminal_error] if terminal_error else [],
                )
                self.agent_registry.complete(self.agent_id, handoff)
            except Exception:
                logger.debug("Unable to finalize primary agent record", exc_info=True)
        if self.flight_recorder:
            try:
                self.flight_recorder.close(
                    "failed" if terminal_error else "completed",
                    outcome=final_payload.get("outcome"),
                )
            except Exception:
                pass
        if self.hook_runner:
            self.hook_runner.emit(
                HookType.SESSION_END,
                {
                    "project_path": self.project_path or "",
                    "agent_id": self.agent_id,
                    "outcome": final_payload.get("outcome"),
                    "steps": final_steps,
                    "elapsed": total_elapsed,
                },
            )
        yield make_event(
            EngineEvent.SESSION_END,
            total_elapsed=total_elapsed,
            total_steps=final_steps,
            **final_payload,
        )

    def _execute_task(
        self,
        fn_args: dict,
        call_id: str,
        fn_args_str: str,
        *,
        append_parent_history: bool = True,
    ) -> Iterator[dict]:
        """
        Execute the 'task' tool — spawn a sub-agent with isolated session.

        The sub-agent gets:
        - Its own conversation history (empty)
        - Restricted tools based on agent type
        - No parent-history bleed-through
        - No ability to spawn further sub-agents (recursion guard)

        All sub-agent events are yielded through the parent for TUI display.
        """
        prompt = str(fn_args.get("prompt", "") or "").strip()
        agent_type_name = str(fn_args.get("agent_type", "explore") or "explore")
        director_task_id = str(fn_args.get("director_task_id") or "").strip()
        worker_id = str(fn_args.get("worker_id") or "").strip()
        director_task = None
        if self.director_run is not None:
            if not director_task_id:
                yield from self._director_task_error(
                    call_id, fn_args_str,
                    "Director Mode requires director_task_id for every delegated task.",
                )
                return
            try:
                director_task = self.director_run.tasks[director_task_id]
                if not worker_id:
                    worker_id = self.director_run.select_worker(director_task_id).id
                else:
                    configured_worker = next(
                        (item for item in self.director_run.config.workers if item.id == worker_id),
                        None,
                    )
                    if configured_worker is None:
                        raise ValueError(f"Unknown configured worker: {worker_id}")
                    if not configured_worker.supports(
                        director_task.role, director_task.required_capabilities,
                    ):
                        raise ValueError(
                            f"Worker {worker_id} is not eligible for role "
                            f"{director_task.role} and required capabilities"
                        )
                prompt = prompt or director_task.objective
                agent_type_name = director_task.agent_type
                fn_args["agent_type"] = agent_type_name
                fn_args["model_role"] = director_task.role
                if director_task.artifact_ids and not fn_args.get("artifact_ids"):
                    fn_args["artifact_ids"] = list(director_task.artifact_ids)
                if director_task.write_scope:
                    fn_args["isolation"] = "worktree"
            except Exception as exc:
                yield from self._director_task_error(call_id, fn_args_str, str(exc))
                return

        agent_type = get_agent_type(agent_type_name)
        if not agent_type and self.capability_packs:
            agent_type = self.capability_packs.get_agent_type(agent_type_name)
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
        model_role = str(fn_args.get("model_role") or agent_type.model_role or "primary")
        requested_isolation = str(
            fn_args.get("isolation") or agent_type.default_isolation or "shared"
        )
        try:
            requested_max_steps = int(fn_args.get("max_steps") or agent_type.max_steps or 0)
        except (TypeError, ValueError):
            requested_max_steps = 0
        worker_max_steps = requested_max_steps if requested_max_steps > 0 else None

        allowed_tools = agent_type.filter_tools(AGENT_TOOLS)
        allowed_tools = [
            tool for tool in allowed_tools
            if tool.get("function", {}).get("name") not in {"task", "task_batch"}
        ]
        try:
            child_backend = (
                self.model_role_router.backend_for(
                    model_role, self.backend, worker_id=worker_id,
                )
                if self.model_role_router else self.backend
            )
        except Exception as exc:
            yield from self._director_task_error(
                call_id,
                fn_args_str,
                f"Worker backend {worker_id or model_role} is unavailable: {exc}",
            )
            return

        # Notify TUI of sub-agent start
        child_cancel = threading.Event()
        child_pause = threading.Event()
        worker_done = threading.Event()
        workspace = self.project_path or ""
        lease = None
        if (
            requested_isolation == "worktree"
            and self.worktree_manager
            and getattr(self.worktree_manager, "available", False)
        ):
            try:
                if self.hook_runner:
                    worktree_hook = self.hook_runner.emit(
                        HookType.WORKTREE_CREATE,
                        {
                            "project_path": self.project_path or "",
                            "parent_agent_id": self.agent_id,
                            "agent_type": agent_type_name,
                            "prompt": prompt,
                        },
                    )
                    if not worktree_hook.allowed:
                        raise RuntimeError(worktree_hook.reason or "Worktree creation blocked")
                lease = self.worktree_manager.create(call_id or _uuid.uuid4().hex[:10])
                workspace = lease.path
            except Exception as exc:
                logger.warning("Worktree isolation unavailable; using shared workspace: %s", exc)
                lease = None

        record = None
        if self.agent_registry:
            try:
                record = self.agent_registry.create(
                    agent_type=agent_type_name,
                    prompt=prompt,
                    parent_id=self.agent_id,
                    model=getattr(self.backend, "model", ""),
                    role=model_role,
                    workspace=workspace,
                    policy=self.autonomy_tier,
                    max_steps=worker_max_steps,
                    metadata={
                        "call_id": call_id,
                        "isolation": "worktree" if lease else "shared",
                        "worktree": lease.to_dict() if lease else None,
                        "director_task_id": director_task_id,
                        "worker_id": worker_id,
                    },
                )
            except Exception:
                logger.debug("Unable to persist child agent", exc_info=True)

        agent_id = record.id if record else f"task:{call_id or _uuid.uuid4().hex[:8]}"
        if director_task is not None:
            try:
                self.director_run.mark_dispatched(
                    director_task_id, worker_id=worker_id, agent_id=agent_id,
                )
            except Exception as exc:
                if lease:
                    try:
                        self.worktree_manager.remove(lease, delete_branch=False)
                    except Exception:
                        pass
                yield from self._director_task_error(call_id, fn_args_str, str(exc))
                return
        if self.hook_runner:
            created_hook = self.hook_runner.emit(
                HookType.TASK_CREATED,
                {
                    "project_path": workspace,
                    "agent_id": agent_id,
                    "parent_agent_id": self.agent_id,
                    "agent_type": agent_type_name,
                    "model_role": model_role,
                    "prompt": prompt,
                },
            )
            if not created_hook.allowed:
                if record:
                    self.agent_registry.fail(agent_id, created_hook.reason or "Task blocked by hook")
                result_output = f"Sub-agent blocked by lifecycle hook: {created_hook.reason}"
                yield make_event(
                    EngineEvent.TOOL_RESULT, name="task", call_id=call_id,
                    output=result_output, is_error=True, elapsed=0.0,
                    metadata={"agent_id": agent_id}, denied=True,
                )
                return

        yield make_event(
            EngineEvent.SUBAGENT_START,
            agent_type=agent_type_name,
            agent_id=agent_id,
            parent_agent_id=self.agent_id,
            director_task_id=director_task_id,
            worker_id=worker_id,
            model_role=model_role,
            isolation="worktree" if lease else "shared",
            workspace=workspace,
            prompt=prompt,
            call_id=call_id,
        )

        worker_profile = (
            self.model_role_router.worker_profile(worker_id)
            if self.model_role_router and worker_id else None
        )
        worker_instructions = agent_type.system_prompt
        if worker_profile and worker_profile.system_suffix:
            worker_instructions = f"{worker_instructions}\n\n{worker_profile.system_suffix}".strip()
        worker_images: list[tuple[bytes, str]] = []
        artifact_context: list[str] = []
        for artifact_id in (fn_args.get("artifact_ids") or []):
            artifact = self.artifact_store.get(str(artifact_id)) if self.artifact_store else None
            if artifact is None:
                errors_message = f"Unknown input artifact: {artifact_id}"
                artifact_context.append(errors_message)
                continue
            if artifact.kind == "image":
                try:
                    worker_images.append((Path(artifact.path).read_bytes(), artifact.media_type))
                except OSError:
                    artifact_context.append(f"Unreadable image artifact: {artifact.id}")
            else:
                artifact_context.append(self.artifact_store.reference(artifact))
        if artifact_context:
            prompt = f"{prompt}\n\nINPUT ARTIFACTS:\n" + "\n".join(artifact_context)
        child = Session(
            backend=child_backend,
            max_steps=worker_max_steps,
            max_tokens=self.max_tokens,
            auto_approve=True,  # Sub-agents auto-approve (no interactive prompts)
            parent_session=self,
            allowed_tools=allowed_tools,
            role_instructions=worker_instructions,
            prompt_role="subagent",
            cancel_event=child_cancel,
            pause_event=child_pause,
        )
        child.copy_execution_context_from(self)
        child.agent_id = agent_id
        child.model_role = model_role
        if workspace:
            child.project_path = workspace
            from .sandbox import PathSandbox
            child.sandbox = PathSandbox(workspace, enabled=True)
        if record and self.agent_registry:
            self.agent_registry.attach_control(
                agent_id,
                cancel_event=child_cancel,
                pause_event=child_pause,
                steer=lambda message: child.steer(message),
            )
            self.agent_registry.transition(agent_id, AgentStatus.RUNNING)

        def _inherit_parent_cancel() -> None:
            while not worker_done.wait(0.1):
                if self.cancel_requested:
                    child_cancel.set()
                    return

        threading.Thread(target=_inherit_parent_cancel, daemon=True).start()

        # Run the sub-agent, collecting text output and forwarding events
        collected_text = []
        sub_start = time.time()
        sub_steps = 0
        changed_files: list[str] = []
        validation: list[str] = []
        errors: list[str] = []

        for event in child.run(
            user_msg=prompt,
            on_permission=None,
            on_choice=None,
            images=worker_images or None,
        ):
            etype = event.get("event", "")
            if record and self.agent_registry:
                try:
                    self.agent_registry.append_event(agent_id, event)
                except Exception:
                    pass

            # Forward relevant events for TUI display (with nesting indicator)
            event["_subagent"] = True
            event["_agent_type"] = agent_type_name
            event["_agent_id"] = agent_id

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
            elif etype == EngineEvent.TOOL_CALL.value:
                name = str(event.get("name") or "")
                args = event.get("arguments") or {}
                if name in WRITE_TOOL_NAMES and isinstance(args, dict) and args.get("path"):
                    value = str(args["path"])
                    if value not in changed_files:
                        changed_files.append(value)
            elif etype == EngineEvent.TOOL_RESULT.value:
                name = str(event.get("name") or "")
                if name in VALIDATION_TOOL_NAMES:
                    outcome = "failed" if event.get("is_error") else "passed"
                    validation.append(f"{name}: {outcome}")
            elif etype == EngineEvent.ERROR.value:
                errors.append(str(event.get("message") or "Worker error"))

        worker_done.set()
        sub_elapsed = time.time() - sub_start
        full_output = "\n\n".join(collected_text) if collected_text else "(no output)"
        artifact_refs: list[str] = []
        if len(full_output) > 8000 and self.artifact_store:
            artifact = self.artifact_store.put_text(
                full_output,
                kind=ArtifactKind.TEXT,
                label=f"{agent_type_name} handoff",
                source=agent_id,
                metadata={"agent_id": agent_id, "call_id": call_id},
            )
            artifact_refs.append(self.artifact_store.reference(artifact))

        if lease:
            try:
                lease = self.worktree_manager.finalize(
                    lease, message=f"Resonant {agent_type_name} agent {agent_id}",
                )
                if director_task is None:
                    lease = self.worktree_manager.integrate(lease)
                for path in lease.changed_files or []:
                    if path not in changed_files:
                        changed_files.append(path)
                artifact_refs.append(
                    f"worktree branch={lease.branch} commit={lease.commit or '(none)'} "
                    f"status={lease.status} path={lease.path}"
                )
                if lease.status in {"unchanged", "integrated"}:
                    self.worktree_manager.remove(lease, delete_branch=True)
            except Exception as exc:
                errors.append(f"Worktree integration: {exc}")

        outcome = "failed" if errors else "completed"
        summary = full_output if len(full_output) <= 4000 else full_output[:4000].rstrip() + "..."
        handoff = AgentHandoff(
            outcome=outcome,
            summary=summary,
            evidence=[f"{sub_steps} worker steps", f"{sub_elapsed:.1f}s elapsed"],
            changed_files=changed_files,
            validation=validation,
            blockers=errors,
            recommended_next_action=(
                "Review and integrate the retained worktree branch."
                if lease and lease.status == "awaiting_integration"
                else "Review the evidence and continue the parent task."
            ),
            artifacts=artifact_refs,
        )
        result_output = handoff.to_context()

        completion_allowed = True
        if self.hook_runner:
            stop_hook = self.hook_runner.emit(
                HookType.SUBAGENT_STOP,
                {
                    "project_path": workspace,
                    "agent_id": agent_id,
                    "parent_agent_id": self.agent_id,
                    "agent_type": agent_type_name,
                    "handoff": handoff.to_dict(),
                },
            )
            completion_allowed = stop_hook.allowed
            if not completion_allowed:
                handoff.blockers.append(stop_hook.reason or "Completion blocked by hook")
                handoff.outcome = "failed"
                result_output = handoff.to_context()

        if director_task is not None:
            try:
                self.director_run.record_handoff(
                    director_task_id,
                    handoff.to_dict(),
                    worktree=lease.to_dict() if lease else None,
                )
                for item in validation:
                    name, _, outcome_text = item.partition(":")
                    self.director_run.record_validation(
                        director_task_id,
                        name=name.strip() or "validation",
                        passed=outcome_text.strip() == "passed",
                        evidence=item,
                        source="worker.tool_result",
                    )
            except Exception as exc:
                handoff.blockers.append(f"Director handoff recording failed: {exc}")
                handoff.outcome = "failed"
                result_output = handoff.to_context()

        if record and self.agent_registry:
            if completion_allowed and not errors:
                self.agent_registry.complete(agent_id, handoff)
            else:
                self.agent_registry.fail(
                    agent_id,
                    "; ".join(handoff.blockers) or "Worker failed",
                )

        # Notify TUI of sub-agent completion
        yield make_event(EngineEvent.SUBAGENT_END,
                        agent_type=agent_type_name,
                        agent_id=agent_id,
                        call_id=call_id,
                        steps=sub_steps,
                        elapsed=sub_elapsed,
                        result_preview=full_output[:200],
                        result=full_output,
                        handoff=handoff.to_dict())

        # Return result to parent context
        yield make_event(EngineEvent.TOOL_RESULT,
                        name="task", call_id=call_id,
                        output=result_output,
                        is_error=False,
                        elapsed=sub_elapsed,
                        metadata={
                            "agent_type": agent_type_name,
                            "agent_id": agent_id,
                            "model_role": model_role,
                            "isolation": "worktree" if lease else "shared",
                            "handoff": handoff.to_dict(),
                            "director_task_id": director_task_id,
                            "worker_id": worker_id,
                            "steps": sub_steps,
                        },
                        denied=False)

        # Add to parent conversation history
        if append_parent_history:
            self.conversation_history.append({
                "role": "tool_call", "name": "task",
                "arguments": fn_args_str, "call_id": call_id,
                "content": f"Called task ({agent_type_name})",
            })
            self.conversation_history.append({
                "role": "tool_result", "call_id": call_id,
                "content": result_output,
            })

    def _director_task_error(
        self,
        call_id: str,
        fn_args_str: str,
        message: str,
    ) -> Iterator[dict]:
        output = f"Director dispatch rejected: {message}"
        yield make_event(
            EngineEvent.TOOL_RESULT,
            name="task",
            call_id=call_id,
            output=output,
            is_error=True,
            elapsed=0.0,
            metadata={"director_run_id": getattr(self.director_run, "id", "")},
            denied=True,
        )
        self.conversation_history.extend([
            {"role": "tool_call", "name": "task", "arguments": fn_args_str,
             "call_id": call_id, "content": "Called task"},
            {"role": "tool_result", "name": "task", "call_id": call_id,
             "content": output},
        ])

    def _execute_director_tool(
        self,
        name: str,
        args: dict[str, Any],
        call_id: str,
        args_json: str,
    ) -> Iterator[dict]:
        run = self.director_run
        started = time.time()
        is_error = False
        try:
            if name == "director_plan":
                tasks = run.create_plan(args.get("tasks") or [])
                payload = {"run": run.to_dict(), "created_tasks": [item.id for item in tasks]}
            elif name == "director_status":
                payload = run.to_dict()
            elif name == "director_validate":
                task = run.record_validation(
                    str(args.get("task_id") or ""),
                    name=str(args.get("name") or "validation"),
                    passed=bool(args.get("passed")),
                    evidence=str(args.get("evidence") or ""),
                    source="director.observed",
                )
                payload = {"task": task.to_dict(), "gate": run.acceptance_gate(task.id)}
            elif name == "director_decide":
                action = str(args.get("action") or "")
                task_id = str(args.get("task_id") or "")
                if action == "accept" and run.config.auto_integrate and run.tasks[task_id].worktree:
                    args = {**args, "action": "integrate"}
                    action = "integrate"
                if action == "integrate":
                    allowed, reasons = run.acceptance_gate(task_id)
                    if not allowed:
                        raise ValueError("Integration gate rejected the decision: " + "; ".join(reasons))
                    task = run.tasks[task_id]
                    if task.worktree:
                        from .worktrees import WorktreeLease
                        lease = WorktreeLease(**task.worktree)
                        lease = self.worktree_manager.integrate(lease)
                        task.worktree = lease.to_dict()
                        if lease.status not in {"integrated", "unchanged"}:
                            run._save()
                            raise ValueError(
                                "Worktree is ready but the primary checkout is not clean; "
                                "integration was deferred without changing user work."
                            )
                        self.worktree_manager.remove(lease, delete_branch=True)
                task = run.decide(args)
                payload = {"task": task.to_dict(), "gate": run.acceptance_gate(task.id)}
            elif name == "director_complete":
                run.complete()
                payload = run.to_dict()
            else:
                raise ValueError(f"Unknown Director tool: {name}")
            output = json.dumps(payload, ensure_ascii=False, default=str)
        except Exception as exc:
            is_error = True
            output = f"Director operation failed: {exc}"
        self.conversation_history.extend([
            {"role": "tool_call", "name": name, "arguments": args_json,
             "call_id": call_id, "content": f"Called {name}"},
            {"role": "tool_result", "name": name, "call_id": call_id,
             "content": output},
        ])
        yield make_event(
            EngineEvent.TOOL_RESULT,
            name=name,
            call_id=call_id,
            output=output,
            is_error=is_error,
            elapsed=time.time() - started,
            metadata={"director_run_id": getattr(run, "id", "")},
            denied=is_error,
        )

    def _execute_task_batch(
        self,
        fn_args: dict,
        call_id: str,
        fn_args_str: str,
    ) -> Iterator[dict]:
        """Fan out independent workers and serialize their handoffs."""
        limit = self.director_run.config.max_parallel_workers if self.director_run else 4
        tasks = [item for item in (fn_args.get("tasks") or []) if isinstance(item, dict)][:limit]
        if len(tasks) < 2:
            output = f"task_batch requires between two and {limit} task specifications."
            yield make_event(
                EngineEvent.TOOL_RESULT, name="task_batch", call_id=call_id,
                output=output, is_error=True, denied=True, elapsed=0.0,
            )
            self.conversation_history.extend([
                {"role": "tool_call", "name": "task_batch", "arguments": fn_args_str,
                 "call_id": call_id, "content": "Called task_batch"},
                {"role": "tool_result", "call_id": call_id, "content": output},
            ])
            return

        if self.hook_runner:
            gate = self.hook_runner.emit(
                HookType.PRE_TOOL_BATCH,
                {
                    "project_path": self.project_path or "",
                    "agent_id": self.agent_id,
                    "call_id": call_id,
                    "tasks": tasks,
                },
            )
            if not gate.allowed:
                output = f"Task batch blocked by hook: {gate.reason or gate.error or 'denied'}"
                yield make_event(
                    EngineEvent.TOOL_RESULT, name="task_batch", call_id=call_id,
                    output=output, is_error=True, denied=True, elapsed=0.0,
                )
                return

        if self.director_run is not None:
            active_counts: dict[str, int] = {}
            try:
                for spec in tasks:
                    director_task_id = str(spec.get("director_task_id") or "")
                    if not director_task_id:
                        raise ValueError("Every Director batch item requires director_task_id")
                    worker_id = str(spec.get("worker_id") or "")
                    if not worker_id:
                        worker = self.director_run.select_worker(
                            director_task_id, active_counts=active_counts,
                        )
                        worker_id = worker.id
                        spec["worker_id"] = worker_id
                    configured = next(
                        (item for item in self.director_run.config.workers if item.id == worker_id),
                        None,
                    )
                    if configured is None:
                        raise ValueError(f"Unknown configured worker: {worker_id}")
                    active_counts[worker_id] = active_counts.get(worker_id, 0) + 1
                    if (
                        configured.max_parallel is not None
                        and active_counts[worker_id] > configured.max_parallel
                    ):
                        raise ValueError(
                            f"Worker {worker_id} exceeds its parallel capacity "
                            f"of {configured.max_parallel}"
                        )
            except Exception as exc:
                output = f"Director batch rejected: {exc}"
                yield make_event(
                    EngineEvent.TOOL_RESULT, name="task_batch", call_id=call_id,
                    output=output, is_error=True, denied=True, elapsed=0.0,
                )
                return

        channel: queue.Queue[tuple[int, dict | None]] = queue.Queue()
        outputs: dict[int, str] = {}
        started = time.time()

        def run_worker(index: int, spec: dict[str, Any]) -> None:
            child_args = dict(spec)
            if str(child_args.get("agent_type") or "") == "build":
                child_args["isolation"] = "worktree"
            child_call_id = f"{call_id}:{index + 1}"
            try:
                for event in self._execute_task(
                    child_args,
                    child_call_id,
                    json.dumps(child_args, ensure_ascii=False),
                    append_parent_history=False,
                ):
                    if (
                        event.get("event") == EngineEvent.TOOL_RESULT.value
                        and event.get("name") == "task"
                        and event.get("call_id") == child_call_id
                    ):
                        outputs[index] = str(event.get("output") or "")
                        continue
                    event["_batch_index"] = index
                    channel.put((index, event))
            except Exception as exc:
                outputs[index] = f"Worker failed: {exc}"
                channel.put((index, make_event(
                    EngineEvent.ERROR,
                    message=f"Parallel worker {index + 1} failed: {exc}",
                    _agent_id=f"batch:{child_call_id}",
                )))
            finally:
                channel.put((index, None))

        threads = []
        for index, spec in enumerate(tasks):
            thread = threading.Thread(
                target=run_worker,
                args=(index, spec),
                name=f"resonant-agent-{index + 1}",
                daemon=True,
            )
            threads.append(thread)
            thread.start()

        finished = 0
        while finished < len(threads):
            _, event = channel.get()
            if event is None:
                finished += 1
            else:
                yield event
        for thread in threads:
            thread.join(timeout=0.1)

        sections = []
        for index, spec in enumerate(tasks):
            label = str(spec.get("agent_type") or "worker")
            sections.append(
                f"Worker {index + 1} ({label}):\n{outputs.get(index, '(no handoff)')}"
            )
        output = "\n\n".join(sections)
        elapsed = time.time() - started
        self.conversation_history.extend([
            {"role": "tool_call", "name": "task_batch", "arguments": fn_args_str,
             "call_id": call_id, "content": f"Called task_batch ({len(tasks)} workers)"},
            {"role": "tool_result", "call_id": call_id, "content": output},
        ])
        if self.hook_runner:
            self.hook_runner.emit(
                HookType.POST_TOOL_BATCH,
                {
                    "project_path": self.project_path or "",
                    "agent_id": self.agent_id,
                    "call_id": call_id,
                    "tasks": tasks,
                    "outputs": outputs,
                    "elapsed": elapsed,
                },
            )
        yield make_event(
            EngineEvent.TOOL_RESULT, name="task_batch", call_id=call_id,
            output=output, is_error=False, denied=False, elapsed=elapsed,
            metadata={"workers": len(tasks), "parallel": True},
        )
