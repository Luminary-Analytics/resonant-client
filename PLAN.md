# Resonant Client — Phase Plan: Features 3, 4, 5

## Overview

Three features to add in order, building on the existing architecture:
- **Feature 3**: Multimodal (Ctrl+V image paste)
- **Feature 4**: Sub-agents (Task tool with isolated sessions)
- **Feature 5**: Batch tool (parallel tool execution)

**Architecture context**: Python TUI, synchronous generators for streaming, 4 backends (Ollama, Claude, OpenAI, Resonant), Rich + prompt_toolkit, engine/session agentic loop.

---

## Feature 3: Multimodal Image Paste

### Goal
Users press Ctrl+V to paste clipboard images into the prompt. Images are sent to vision-capable models (Claude, GPT-4o, Ollama vision models) as base64-encoded content blocks.

### Files to modify
- `resonant_client/engine/clipboard.py` — **NEW** — Platform clipboard image reader
- `resonant_client/backends.py` — Add image content block support to all backends
- `resonant_client/engine/session.py` — Support image attachments in conversation history
- `resonant_client/tui.py` — Ctrl+V keybinding, image attachment indicator in prompt

### Step 3.1: Clipboard image reader
Create `resonant_client/engine/clipboard.py`:

```python
def read_clipboard_image() -> tuple[bytes | None, str]:
    """
    Read image from system clipboard.
    Returns (image_bytes, media_type) or (None, "") if no image.

    Platform detection:
    - Windows: PowerShell [System.Windows.Forms.Clipboard]::GetImage()
    - macOS: osascript clipboard as PNGf
    - Linux: wl-paste or xclip
    """
```

- Write image to temp file, read back as bytes
- Return `(bytes, "image/png")` or `(None, "")`
- Handle all error cases gracefully (no image, clipboard locked, etc.)

**Verification**: Run on Windows, paste a screenshot, confirm bytes returned.

### Step 3.2: Backend image support

**Claude backend** (`backends.py`):
- In `stream()`, check if `user_msg` is a list (mixed content) vs string (text only)
- Support message format: `[{"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "..."}}, {"type": "text", "text": "describe this"}]`
- In conversation history conversion, handle `role: "user"` entries where content is a list

**OpenAI backend** (`backends.py`):
- Support message format: `[{"type": "image_url", "image_url": {"url": "data:image/png;base64,..."}}, {"type": "text", "text": "describe this"}]`

**Ollama backend** (`backends.py`):
- Ollama uses `images` field in message: `{"role": "user", "content": "describe", "images": ["base64..."]}`
- Only works with vision models (llava, llama3.2-vision, etc.)

**Resonant backend**: Pass through as-is or skip image (not supported).

**Verification**: Send a test image to Claude API, confirm vision response.

### Step 3.3: Session image support

Modify `Session.run()` in `engine/session.py`:
- Accept optional `images: list[tuple[bytes, str]]` parameter
- When images present, construct mixed content message instead of plain string
- Store in conversation history as: `{"role": "user", "content": [image_blocks + text_block]}`
- Backend-specific content formatting happens in each backend's `stream()` method

**Verification**: Run session with image, confirm it appears in conversation history correctly.

### Step 3.4: TUI keybinding and display

In `tui.py`:
- Create custom `KeyBindings` for the prompt_toolkit prompt
- Bind `Ctrl+V` to:
  1. Try `read_clipboard_image()` first
  2. If image found, store in `pending_images` list, show indicator
  3. If no image, fall back to normal paste behavior
- Show image attachment indicator in prompt: `📎 1 image │ cwd ❯`
- When submitting, pass `pending_images` to `session.run()`
- After submit, clear `pending_images`

**Verification**: Paste image, see indicator, submit prompt, confirm image sent to API.

---

## Feature 4: Sub-Agents (Task Tool)

### Goal
The LLM can spawn isolated child sessions to handle subtasks. Each sub-agent has its own context window, restricted tools, and model. Results flow back to the parent.

### Files to modify
- `resonant_client/engine/agents.py` — **NEW** — Agent type registry and SubAgent class
- `resonant_client/engine/tools.py` — Add `task` tool to AGENT_TOOLS
- `resonant_client/engine/session.py` — Child session spawning, parent-child relationship
- `resonant_client/tui.py` — Sub-agent activity rendering (nested display)
- `resonant_client/events.py` — Add SUBAGENT_START, SUBAGENT_END events

### Step 4.1: Agent registry

Create `resonant_client/engine/agents.py`:

```python
@dataclass
class AgentType:
    name: str              # "build", "explore", "plan"
    description: str       # For LLM tool description
    model: str | None      # Override model, or None = inherit parent
    allowed_tools: list[str]  # Tool names this agent can use
    system_prompt: str     # Agent-specific instructions
    max_steps: int         # Step limit for this agent type

AGENT_TYPES = {
    "build": AgentType(
        name="build",
        description="Full coding agent with all tools",
        model=None,  # inherit
        allowed_tools=["bash", "file_read", "file_write", "file_edit", "glob", "grep"],
        system_prompt="You are a coding agent...",
        max_steps=15,
    ),
    "explore": AgentType(
        name="explore",
        description="Fast read-only agent for codebase exploration",
        model=None,  # could default to haiku for speed
        allowed_tools=["file_read", "glob", "grep", "bash"],  # bash for read-only commands
        system_prompt="You are a read-only exploration agent...",
        max_steps=10,
    ),
    "plan": AgentType(
        name="plan",
        description="Planning agent that analyzes without modifying",
        model=None,
        allowed_tools=["file_read", "glob", "grep"],
        system_prompt="You are a planning agent...",
        max_steps=8,
    ),
}
```

**Verification**: Import and instantiate each agent type.

### Step 4.2: Task tool definition

Add to `engine/tools.py`:

```python
{
    "type": "function",
    "function": {
        "name": "task",
        "description": "Spawn a sub-agent to handle a subtask independently. The sub-agent gets its own context window and runs to completion. Use 'explore' for fast read-only searches, 'plan' for analysis, 'build' for coding tasks.",
        "parameters": {
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": "The task description for the sub-agent"
                },
                "agent_type": {
                    "type": "string",
                    "enum": ["build", "explore", "plan"],
                    "description": "The type of agent to spawn"
                }
            },
            "required": ["prompt", "agent_type"]
        }
    }
}
```

**Verification**: Confirm tool appears in AGENT_TOOLS list.

### Step 4.3: Child session spawning

Modify `engine/session.py`:

- Add `parent_session: Optional[Session] = None` to `Session.__init__`
- Add `is_subagent: bool` property (True if parent_session is set)
- In `execute_tool`, when `name == "task"`:
  1. Look up `AgentType` from registry
  2. Create child `Session` with:
     - Same backend (or override model if agent type specifies)
     - Filtered `AGENT_TOOLS` (only allowed tools)
     - `parent_session=self`
     - `max_steps=agent_type.max_steps`
  3. Run child session synchronously, collecting all text output
  4. Return collected text as `ToolResult.output`
  5. Yield sub-agent events (SUBAGENT_START, TEXT_DELTA, SUBAGENT_END) through parent
- **Recursion guard**: If `self.is_subagent`, remove "task" from available tools

**Verification**: Trigger task tool, confirm child session runs and returns result.

### Step 4.4: Events and TUI rendering

Add to `events.py`:
```python
SUBAGENT_START = "subagent.start"   # {"agent_type": "explore", "prompt": "..."}
SUBAGENT_END = "subagent.end"       # {"agent_type": "explore", "result_preview": "..."}
```

In `tui.py`:
- Render SUBAGENT_START as:
  ```
  ┃ │ Task  explore agent
  │ │   "Find all API endpoints in the codebase"
  ```
- Render sub-agent activity with double-indent `│ │` prefix
- Render SUBAGENT_END as:
  ```
  │ │   ✓ explore · 3 steps · 4.2s
  ```
- Collapse sub-agent steps using same grouping logic as main steps

**Verification**: Run full flow, confirm nested display renders correctly.

---

## Feature 5: Batch Tool (Parallel Tool Execution)

### Goal
Execute multiple tool calls in parallel within a single turn. Useful for reading many files at once, running grep across multiple patterns, etc.

### Files to modify
- `resonant_client/engine/tools.py` — Add `batch` tool, parallel execution logic
- `resonant_client/engine/session.py` — Handle batch tool results
- `resonant_client/tui.py` — Render parallel tool execution display

### Step 5.1: Batch tool definition

Add to `engine/tools.py`:

```python
{
    "type": "function",
    "function": {
        "name": "batch",
        "description": "Execute multiple tool calls in parallel. Use this when you need to read several files, search for multiple patterns, or run independent operations concurrently. Maximum 25 calls per batch.",
        "parameters": {
            "type": "object",
            "properties": {
                "calls": {
                    "type": "array",
                    "maxItems": 25,
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string", "description": "Tool name"},
                            "arguments": {"type": "object", "description": "Tool arguments"}
                        },
                        "required": ["name", "arguments"]
                    }
                }
            },
            "required": ["calls"]
        }
    }
}
```

### Step 5.2: Parallel execution

In `engine/tools.py`, add `execute_batch()`:

```python
def execute_batch(calls: list[dict]) -> ToolResult:
    """
    Execute multiple tool calls in parallel using ThreadPoolExecutor.

    - Max 25 calls
    - Cannot batch 'batch' or 'task' (no recursion)
    - Each call runs independently; one failure doesn't stop others
    - Returns aggregated results
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    FORBIDDEN = {"batch", "task"}
    results = []

    with ThreadPoolExecutor(max_workers=min(len(calls), 10)) as pool:
        futures = {}
        for i, call in enumerate(calls[:25]):
            name = call.get("name", "")
            args = call.get("arguments", {})
            if name in FORBIDDEN:
                results.append({"index": i, "name": name, "status": "error",
                               "output": f"Cannot batch '{name}'"})
                continue
            future = pool.submit(execute_tool, name, args)
            futures[future] = (i, name)

        for future in as_completed(futures):
            i, name = futures[future]
            try:
                result = future.result()
                results.append({"index": i, "name": name,
                               "status": "error" if result.is_error else "success",
                               "output": result.output, "elapsed": result.elapsed})
            except Exception as e:
                results.append({"index": i, "name": name, "status": "error",
                               "output": str(e)})

    results.sort(key=lambda r: r["index"])
    successes = sum(1 for r in results if r["status"] == "success")
    failures = len(results) - successes

    summary = f"{successes} succeeded, {failures} failed\n\n"
    for r in results:
        summary += f"[{r['name']}] {r['status']}: {r['output'][:500]}\n"

    return ToolResult(output=summary, is_error=failures > 0,
                     elapsed=max((r.get("elapsed", 0) for r in results), default=0),
                     metadata={"results": results, "successes": successes, "failures": failures})
```

### Step 5.3: Session integration

In `engine/session.py`, the batch tool flows through normal `execute_tool` dispatch. The `execute_tool` function routes `name == "batch"` to `execute_batch()`.

No special session handling needed — batch is just a tool that returns a composite result.

### Step 5.4: TUI rendering

In `tui.py`, add batch-specific display:

```
  ┃ ⚡ Batch  5 parallel calls
  │   → Read src/main.py         142 lines
  │   → Read src/config.py        89 lines
  │   → Read src/utils.py        234 lines
  │   ✱ Glob **/*.test.ts         12 files
  │   / Grep "TODO"                7 matches
  │   ✓ 5/5 succeeded · 0.8s
```

- Show individual results inline (like collapsed step group)
- Summary line with success/failure count
- Use `⚡` icon for batch operations

**Verification**: Trigger batch tool with 5 file reads, confirm parallel execution and display.

---

## Implementation Order

```
Feature 3: Multimodal
  3.1 clipboard.py (platform reader)     → test on Windows
  3.2 Backend image support              → test with Claude API
  3.3 Session image history              → test round-trip
  3.4 TUI keybinding + indicator         → test paste workflow

Feature 4: Sub-agents
  4.1 agents.py (registry)               → test imports
  4.2 Task tool definition               → test tool list
  4.3 Child session spawning             → test nested execution
  4.4 Events + TUI rendering             → test visual output

Feature 5: Batch tool
  5.1 Tool definition                    → test tool list
  5.2 Parallel execution                 → test ThreadPoolExecutor
  5.3 Session integration                → test end-to-end
  5.4 TUI rendering                      → test visual output
```

## Dependencies Between Features
- Feature 3 is independent
- Feature 4 depends on the tool system (already exists)
- Feature 5 depends on the tool system (already exists)
- Features 4 and 5 are independent of each other
- Feature 4's "task" tool must be excluded from Feature 5's batch (recursion guard)
