# Resonant Engine — Command Center & Coordinator Chat Requirements

## What Needs to Work

The Resonant GUI Command Center allows users to:
1. **Create projects** — give a strategy, AI builds the code
2. **Chat with coordinator** — interactive back-and-forth where the AI reads, writes, and modifies project files
3. **Spawn worker agents** — coordinator delegates tasks to parallel workers

All of these require the engine's `/v1/responses` endpoint to support **tool calling** — returning `function_call` items that the client executes locally, then sending results back.

---

## The Core Problem

When the client sends tools in the request, the engine currently ignores them and returns only `output_text` with cognitive state metadata. The engine needs to actually **decide to call tools** when the task requires file operations.

---

## End-to-End Flow: Project Creation

### Step 1: Client sends initial request with tools

```json
POST /v1/responses
{
  "model": "resonant-engine",
  "input": [
    {
      "role": "user",
      "content": [
        {
          "type": "input_text",
          "text": "You are the project coordinator for: Todo REST API\nProject path: D:/Repos/todo-api\nStrategy: Build a simple REST API for a todo list using Python Flask.\n\nUser says: Create the Flask API with GET, POST, PUT, DELETE endpoints."
        }
      ]
    }
  ],
  "instructions": "You are a coding assistant. When asked to create or modify files, use the provided tools. Do not just describe what you would do — actually call the tools to do it.",
  "tools": [
    {
      "type": "function",
      "function": {
        "name": "bash",
        "description": "Execute a shell command and return stdout/stderr",
        "parameters": {
          "type": "object",
          "properties": {
            "command": {"type": "string", "description": "Shell command to execute"}
          },
          "required": ["command"]
        }
      }
    },
    {
      "type": "function",
      "function": {
        "name": "file_read",
        "description": "Read the contents of a file at the given path",
        "parameters": {
          "type": "object",
          "properties": {
            "path": {"type": "string", "description": "Absolute or relative file path"}
          },
          "required": ["path"]
        }
      }
    },
    {
      "type": "function",
      "function": {
        "name": "file_write",
        "description": "Create or overwrite a file with the given content",
        "parameters": {
          "type": "object",
          "properties": {
            "path": {"type": "string", "description": "File path to write"},
            "content": {"type": "string", "description": "Full file content to write"}
          },
          "required": ["path", "content"]
        }
      }
    },
    {
      "type": "function",
      "function": {
        "name": "file_edit",
        "description": "Find and replace text within an existing file",
        "parameters": {
          "type": "object",
          "properties": {
            "path": {"type": "string", "description": "File path to edit"},
            "old_text": {"type": "string", "description": "Exact text to find"},
            "new_text": {"type": "string", "description": "Replacement text"}
          },
          "required": ["path", "old_text", "new_text"]
        }
      }
    }
  ],
  "stream": true,
  "max_output_tokens": 4096
}
```

### Step 2: Engine returns function_call items

The engine should analyze the request and decide which tools to call. For "Create the Flask API", it should call `file_write` to create `app.py`:

**Non-streaming response:**
```json
{
  "output": [
    {
      "type": "message",
      "role": "assistant",
      "content": [
        {
          "type": "output_text",
          "text": "I'll create the Flask API with all CRUD endpoints."
        }
      ]
    },
    {
      "type": "function_call",
      "call_id": "call_001",
      "name": "file_write",
      "arguments": "{\"path\": \"D:/Repos/todo-api/app.py\", \"content\": \"from flask import Flask, jsonify, request\\nfrom flask_cors import CORS\\nimport json\\n\\napp = Flask(__name__)\\nCORS(app)\\n\\n# ... full API code ...\\n\"}"
    },
    {
      "type": "function_call",
      "call_id": "call_002",
      "name": "file_write",
      "arguments": "{\"path\": \"D:/Repos/todo-api/requirements.txt\", \"content\": \"flask\\nflask-cors\\n\"}"
    }
  ]
}
```

**Streaming (SSE) response:**
```
event: response.output_text.delta
data: {"delta": "I'll create the Flask API"}

event: response.output_text.delta
data: {"delta": " with all CRUD endpoints."}

event: response.output_item.done
data: {"item": {"type": "function_call", "call_id": "call_001", "name": "file_write", "arguments": "{\"path\": \"D:/Repos/todo-api/app.py\", \"content\": \"from flask import Flask...\"}"}}

event: response.output_item.done
data: {"item": {"type": "function_call", "call_id": "call_002", "name": "file_write", "arguments": "{\"path\": \"D:/Repos/todo-api/requirements.txt\", \"content\": \"flask\\nflask-cors\\n\"}"}}

event: response.completed
data: {"response": {"status": "requires_action", "cognitive_state": {...}}}
```

### Step 3: Client executes tools and sends results back

The client executes each `function_call` locally (writes the files, runs commands) and sends the results back:

```json
POST /v1/responses
{
  "model": "resonant-engine",
  "input": [
    {"role": "user", "content": [{"type": "input_text", "text": "Create the Flask API..."}]},
    {
      "type": "function_call",
      "call_id": "call_001",
      "name": "file_write",
      "arguments": "{\"path\": \"D:/Repos/todo-api/app.py\", \"content\": \"...\"}"
    },
    {
      "type": "function_call_output",
      "call_id": "call_001",
      "output": "File written: D:/Repos/todo-api/app.py (3142 bytes)"
    },
    {
      "type": "function_call",
      "call_id": "call_002",
      "name": "file_write",
      "arguments": "{\"path\": \"D:/Repos/todo-api/requirements.txt\", \"content\": \"flask\\nflask-cors\\n\"}"
    },
    {
      "type": "function_call_output",
      "call_id": "call_002",
      "output": "File written: D:/Repos/todo-api/requirements.txt (18 bytes)"
    }
  ],
  "instructions": "...",
  "tools": [...],
  "stream": true,
  "max_output_tokens": 4096
}
```

### Step 4: Engine responds with summary (no more tool calls)

```json
{
  "output": [
    {
      "type": "message",
      "role": "assistant",
      "content": [
        {
          "type": "output_text",
          "text": "Created the Flask API with all endpoints:\n- app.py (3142 bytes) — GET/POST/PUT/DELETE /todos\n- requirements.txt — flask, flask-cors\n\nRun `pip install -r requirements.txt && python app.py` to start."
        }
      ]
    }
  ]
}
```

The loop ends when the engine returns NO `function_call` items — only text.

---

## End-to-End Flow: Coordinator Chat (Multi-Turn)

The coordinator chat is a multi-turn conversation where each turn goes through the same tool-calling loop.

### Turn 1: User asks to analyze

```json
POST /v1/responses
{
  "input": [
    {"role": "user", "content": [{"type": "input_text", "text": "What files are in this project? List them with sizes."}]}
  ],
  "tools": [bash, file_read, file_write, file_edit],
  ...
}
```

**Engine should return:**
```json
{
  "output": [
    {"type": "function_call", "call_id": "c1", "name": "bash", "arguments": "{\"command\": \"ls -la D:/Repos/todo-api/\"}"}
  ]
}
```

Client runs the command, sends result back, engine responds with summary text.

### Turn 2: User asks to modify code

```json
POST /v1/responses
{
  "input": [
    {"role": "user", "content": [{"type": "input_text", "text": "What files are in this project?"}]},
    ... (previous turn's function_call + result) ...,
    {"role": "assistant", "content": "Here are the files: app.py (3KB), requirements.txt (18B)..."},
    {"role": "user", "content": [{"type": "input_text", "text": "Add input validation to the POST endpoint"}]}
  ],
  "tools": [bash, file_read, file_write, file_edit],
  ...
}
```

**Engine should return** tool calls to read the file, then edit it:
```json
{
  "output": [
    {"type": "function_call", "call_id": "c2", "name": "file_read", "arguments": "{\"path\": \"D:/Repos/todo-api/app.py\"}"}
  ]
}
```

After getting the file contents back, the engine calls `file_edit`:
```json
{
  "output": [
    {"type": "function_call", "call_id": "c3", "name": "file_edit", "arguments": "{\"path\": \"D:/Repos/todo-api/app.py\", \"old_text\": \"@app.route('/todos', methods=['POST'])\ndef create_todo():\n    data = request.json\", \"new_text\": \"@app.route('/todos', methods=['POST'])\ndef create_todo():\n    data = request.json\n    if not data or 'title' not in data:\n        return jsonify({'error': 'title is required'}), 400\"}"}
  ]
}
```

---

## Decision Logic: When to Call Tools

The engine should call tools when:

| User Request | Expected Tool Calls |
|-------------|-------------------|
| "Create a file called X" | `file_write` |
| "What files are in the project?" | `bash` (ls) or `file_read` |
| "Read app.py" | `file_read` |
| "Fix the bug in line 42" | `file_read` then `file_edit` |
| "Run the tests" | `bash` (pytest/npm test) |
| "Install dependencies" | `bash` (pip install/npm install) |
| "Add a new endpoint" | `file_read` then `file_edit` or `file_write` |
| "What does this code do?" (analysis only) | No tools needed — just `output_text` |

**Rule of thumb**: If the user asks to CREATE, MODIFY, DELETE, or INSPECT files/commands, return `function_call` items. If they're asking for explanations or analysis that doesn't need file access, just return `output_text`.

---

## What the Client Already Handles

The Resonant GUI client already has all the plumbing for this:

1. **Sends tools in every request** — `bash`, `file_read`, `file_write`, `file_edit` are always included
2. **Parses `function_call` items** from the response (both streaming and non-streaming)
3. **Executes tools locally** — Session runs tools on the user's machine
4. **Sends results back** — `function_call_output` items in the follow-up request
5. **Loops until done** — keeps calling `/v1/responses` until no more `function_call` items
6. **Streams text** — `response.output_text.delta` events render in real-time in the chat UI

The ONLY thing missing is the engine actually returning `function_call` items.

---

## SSE Event Reference

The client's `ResonantBackend._parse_events()` handles these SSE event types:

| SSE Event | Client Action |
|-----------|---------------|
| `response.output_text.delta` | Append text delta to streaming display |
| `response.output_item.done` with `type: "function_call"` | Queue tool for execution |
| `response.completed` | End of response, check if tools need execution |

**The client already handles all three.** Just emit them from the engine.

---

## Testing Checklist

Once the engine supports tool calling, verify with:

### Test 1: Simple file creation
```bash
curl -X POST http://10.0.0.133:8000/v1/responses \
  -H "Content-Type: application/json" \
  -d '{
    "model": "resonant-engine",
    "input": [{"role": "user", "content": [{"type": "input_text", "text": "Create hello.py that prints hello world"}]}],
    "instructions": "Use tools to create files when asked.",
    "tools": [{"type": "function", "function": {"name": "file_write", "description": "Write a file", "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}}}],
    "stream": false,
    "max_output_tokens": 2048
  }'
```
**Expected**: Response contains `"type": "function_call"` with `"name": "file_write"`

### Test 2: File reading
```bash
curl -X POST http://10.0.0.133:8000/v1/responses \
  -H "Content-Type: application/json" \
  -d '{
    "model": "resonant-engine",
    "input": [{"role": "user", "content": [{"type": "input_text", "text": "Read the contents of README.md"}]}],
    "instructions": "Use tools to read files.",
    "tools": [{"type": "function", "function": {"name": "file_read", "description": "Read a file", "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}}],
    "stream": false,
    "max_output_tokens": 2048
  }'
```
**Expected**: Response contains `"type": "function_call"` with `"name": "file_read"`

### Test 3: Multi-turn with tool results
```bash
# First: engine returns function_call for file_read
# Then send back with results:
curl -X POST http://10.0.0.133:8000/v1/responses \
  -H "Content-Type: application/json" \
  -d '{
    "model": "resonant-engine",
    "input": [
      {"role": "user", "content": [{"type": "input_text", "text": "Read README.md and tell me what it says"}]},
      {"type": "function_call", "call_id": "c1", "name": "file_read", "arguments": "{\"path\": \"README.md\"}"},
      {"type": "function_call_output", "call_id": "c1", "output": "# My Project\nA simple test project."}
    ],
    "instructions": "Use tools to read files.",
    "tools": [{"type": "function", "function": {"name": "file_read", "description": "Read a file", "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}}],
    "stream": false,
    "max_output_tokens": 2048
  }'
```
**Expected**: Response contains only `output_text` summarizing the file contents (no more function_calls)

### Test 4: Streaming with tool calls
Same as Test 1 but with `"stream": true`. Expected SSE events:
```
event: response.output_text.delta
data: {"delta": "I'll create the file."}

event: response.output_item.done
data: {"item": {"type": "function_call", "call_id": "c1", "name": "file_write", "arguments": "{...}"}}

event: response.completed
data: {"response": {"status": "requires_action"}}
```

---

## Summary of Changes Needed on Engine Side

1. **When `tools` array is non-empty**: Treat as an agentic coding request
2. **Analyze user intent**: If they ask to create/read/modify/run → return `function_call` items
3. **Generate valid tool arguments**: JSON strings matching the tool's parameter schema
4. **Handle `function_call_output` in input**: Use the tool results to continue the conversation
5. **Multi-turn loop**: Keep returning `function_call` items until the task is complete, then return only `output_text`
6. **Streaming**: Emit `response.output_item.done` SSE events for each function_call

The cognitive state can still be included — the client ignores it for tool execution. Just make sure `function_call` items are ALSO present when tools should be used.
