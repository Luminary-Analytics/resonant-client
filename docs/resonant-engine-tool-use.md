# Resonant Engine — Tool Use Requirements for Command Center

## Current Status

This protocol work is now implemented on the engine side for `/v1/responses`.

Current verified behavior:

- non-streaming responses return real `function_call` output items
- follow-up requests with `function_call_output` continue the loop correctly
- streaming responses emit:
  - `response.output_text.delta`
  - `response.output_item.done` for `function_call`
  - `response.completed` with `status: "requires_action"` when tool calls are present
- `resonant-client`'s `ResonantBackend` now parses these responses correctly
- a full client `Session` can execute a simple `file_write` loop end-to-end against `resonant-engine`
- project-creation prompts can now return multiple `file_write` calls for small starter scaffolds
- project-creation followups can now continue into cheap validation `bash` calls before summarizing
- coordinator chat followups can now infer a likely source file, call `file_read`, then continue to `file_edit`
- coordinator/project-management toolsets can now chain `update_plan`, `spawn_worker`, `check_workers`, `post_update`, and `complete_project`

Important edge case to preserve:

- if the engine probes a target file with `file_read` and the tool result says the file does not exist,
  create-intent requests should pivot into `file_write` instead of stopping
- missing-file `file_read` is only terminal for inspection-only requests, not for create/bootstrap flows

The main remaining limitation is capability, not protocol:

- simple file/tool requests now work
- small deterministic project scaffolds and coordinator followups now work
- broad autonomous multi-file project generation is still limited by the current `resonant-engine` tool policy and generation quality

## Overview

The Resonant GUI Command Center needs the engine at `/v1/responses` to support **tool calling** — where the engine returns `function_call` output items that the client executes locally, then sends results back in a follow-up request. This is the same pattern used by OpenAI's Responses API.

Currently the engine returns `output_text` with cognitive state metadata even when tools are provided. To work with the Command Center (project creation, file writing, coordinator chat), the engine must actually invoke the tools.

## Previous Broken Behavior

**Request:**
```json
POST /v1/responses
{
  "model": "resonant-engine",
  "input": [
    {"role": "user", "content": [{"type": "input_text", "text": "Create a file called hello.py that prints hello world"}]}
  ],
  "instructions": "You are a coding assistant. Use your tools to create files.",
  "tools": [
    {
      "type": "function",
      "function": {
        "name": "file_write",
        "description": "Write content to a file",
        "parameters": {
          "type": "object",
          "properties": {
            "path": {"type": "string", "description": "File path to write to"},
            "content": {"type": "string", "description": "Content to write"}
          },
          "required": ["path", "content"]
        }
      }
    }
  ],
  "stream": false,
  "max_output_tokens": 2048
}
```

**Current Response (wrong):**
```json
{
  "output": [
    {
      "type": "message",
      "role": "assistant",
      "content": [
        {
          "type": "output_text",
          "text": "[Cognitive State: mode=analytical, coherence=0.83...]\n\nRetrieved Knowledge:\n  ..."
        }
      ]
    }
  ]
}
```

The engine returns text analysis instead of calling the `file_write` tool.

## Required Behavior

### Non-Streaming Response

When the engine decides to use a tool, return a `function_call` item in the output:

```json
{
  "output": [
    {
      "type": "message",
      "role": "assistant",
      "content": [
        {
          "type": "output_text",
          "text": "I'll create the hello.py file for you."
        }
      ]
    },
    {
      "type": "function_call",
      "id": "call_abc123",
      "call_id": "call_abc123",
      "name": "file_write",
      "arguments": "{\"path\": \"hello.py\", \"content\": \"print('Hello, World!')\\n\"}"
    }
  ]
}
```

The client will then:
1. Parse the `function_call` item
2. Execute the tool locally (write the file)
3. Send a follow-up request with the tool result

### Missing-file follow-up behavior

Another required loop behavior for Command Center:

- if the engine starts with `file_read` on a path that is supposed to be created
- and the client sends back `function_call_output` indicating the file is missing
- the engine should continue with `file_write`, not terminate the loop

This matters for project creation and bootstrap requests where the model may first inspect the target
path before writing it.

### Follow-Up Request with Tool Result

```json
POST /v1/responses
{
  "model": "resonant-engine",
  "input": [
    {"role": "user", "content": [{"type": "input_text", "text": "Create a file called hello.py..."}]},
    {
      "type": "function_call",
      "name": "file_write",
      "arguments": "{\"path\": \"hello.py\", \"content\": \"print('Hello, World!')\\n\"}",
      "call_id": "call_abc123"
    },
    {
      "type": "function_call_output",
      "call_id": "call_abc123",
      "output": "File written successfully: hello.py (26 bytes)"
    }
  ],
  "instructions": "...",
  "tools": [...],
  "stream": false,
  "max_output_tokens": 2048
}
```

The engine should then continue the conversation, potentially calling more tools or providing a final text summary.

### Streaming Response (SSE)

For streaming mode (`"stream": true`), the engine should emit these SSE events:

```
event: response.output_text.delta
data: {"delta": "I'll create the file..."}

event: response.output_item.done
data: {"item": {"type": "function_call", "name": "file_write", "arguments": "{\"path\": \"hello.py\", \"content\": \"print('Hello, World!')\\n\"}", "call_id": "call_abc123"}}

event: response.completed
data: {"response": {"cognitive_state": {...}, "status": "requires_action"}}
```

**Key SSE event types the client expects:**

| Event | Purpose | Data Format |
|-------|---------|-------------|
| `response.output_text.delta` | Streaming text chunks | `{"delta": "text..."}` |
| `response.output_item.done` | Completed tool call | `{"item": {"type": "function_call", "name": "...", "arguments": "...", "call_id": "..."}}` |
| `response.completed` | End of response | `{"response": {"cognitive_state": {...}, "status": "completed"}}` |

## Tools the Client Sends

The client sends these tools in every request (from `AGENT_TOOLS` in the Session):

### 1. `bash` — Execute shell commands
```json
{
  "type": "function",
  "function": {
    "name": "bash",
    "description": "Execute a bash/shell command and return the output",
    "parameters": {
      "type": "object",
      "properties": {
        "command": {"type": "string", "description": "The command to execute"}
      },
      "required": ["command"]
    }
  }
}
```

### 2. `file_read` — Read file contents
```json
{
  "type": "function",
  "function": {
    "name": "file_read",
    "description": "Read the contents of a file",
    "parameters": {
      "type": "object",
      "properties": {
        "path": {"type": "string", "description": "Path to the file to read"}
      },
      "required": ["path"]
    }
  }
}
```

### 3. `file_write` — Write/create files
```json
{
  "type": "function",
  "function": {
    "name": "file_write",
    "description": "Write content to a file (creates it if it doesn't exist)",
    "parameters": {
      "type": "object",
      "properties": {
        "path": {"type": "string", "description": "Path to the file to write"},
        "content": {"type": "string", "description": "Content to write to the file"}
      },
      "required": ["path", "content"]
    }
  }
}
```

### 4. `file_edit` — Edit specific lines in a file
```json
{
  "type": "function",
  "function": {
    "name": "file_edit",
    "description": "Replace text in a file",
    "parameters": {
      "type": "object",
      "properties": {
        "path": {"type": "string", "description": "Path to the file"},
        "old_text": {"type": "string", "description": "Text to find and replace"},
        "new_text": {"type": "string", "description": "Replacement text"}
      },
      "required": ["path", "old_text", "new_text"]
    }
  }
}
```

## Multi-Turn Tool Loop

The client runs an **agentic loop** — it keeps calling `/v1/responses` until the engine stops returning tool calls:

```
1. Client sends: user message + tools
2. Engine returns: text + function_call(s)
3. Client executes: tool(s) locally
4. Client sends: original messages + function_call + function_call_output
5. Engine returns: more text + maybe more function_call(s)
6. Repeat until engine returns only text (no function_calls)
7. Done — final text is the response
```

The engine should return `function_call` items when it wants to:
- Read a file to understand the codebase
- Write a new file
- Run a shell command (npm install, python script, etc.)
- Edit an existing file

And return only `output_text` (no function_calls) when the work is done.

## What Needs to Change in the Engine

1. **When `tools` array is non-empty in the request**: The engine should treat this as an agentic coding request, not a pure analysis/retrieval request.

2. **Decision making**: When the user asks to create files, modify code, or run commands, the engine should return `function_call` items instead of (or in addition to) `output_text`.

3. **Tool result handling**: When the input contains `function_call_output` items, the engine should incorporate those results into its next response (continuing the conversation with knowledge of what the tools did).

4. **Cognitive state is fine**: The engine can still include cognitive state in responses — the client ignores it for tool execution purposes. Just make sure `function_call` items are also present when tools should be used.

## Testing

Once the engine supports tool calling, test with:

```bash
curl -X POST http://10.0.0.133:8000/v1/responses \
  -H "Content-Type: application/json" \
  -d '{
    "model": "resonant-engine",
    "input": [{"role": "user", "content": [{"type": "input_text", "text": "Create a file called test.py with print(hello)"}]}],
    "instructions": "You are a coding assistant. When asked to create or modify files, use the provided tools.",
    "tools": [{"type": "function", "function": {"name": "file_write", "description": "Write content to a file", "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}}}],
    "stream": false,
    "max_output_tokens": 2048
  }'
```

**Expected**: Response contains a `function_call` item with `name: "file_write"` and valid arguments.

**Currently**: Response contains only `output_text` with cognitive state metadata.
