"""
Backend abstraction for Resonant Client.

Supports multiple backends:
  - Ollama: Native tool calling via /api/chat with streaming
  - Resonant Engine: SSE streaming via /v1/responses with cognitive state
  - Claude: Anthropic API via anthropic SDK (Claude Max / API key)
  - OpenAI: OpenAI API via openai SDK (GPT Codex / API key)

All backends yield a common stream of events that the TUI consumes.
"""

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Iterator, Tuple, Optional

import httpx

from .protocol import build_tool_system_prompt, parse_tool_calls

logger = logging.getLogger(__name__)

# Common event types yielded by all backends
EVENT_TEXT_DELTA = "text.delta"           # {"delta": "..."}
EVENT_TOOL_CALL = "tool_call"             # {"name": ..., "arguments": ..., "call_id": ...}
EVENT_DONE = "done"                       # {"cognitive_state": ... or None, "stats": ...}
EVENT_ERROR = "error"                     # {"message": "..."}


def _convert_tools_for_ollama(tools: list) -> list:
    """Convert AGENT_TOOLS (OpenAI format) to Ollama's native tools format."""
    ollama_tools = []
    for tool in tools:
        if isinstance(tool, dict) and tool.get("type") == "function":
            ollama_tools.append(tool)  # Already in the right format
        elif isinstance(tool, dict) and "name" in tool:
            # Wrap bare function defs
            ollama_tools.append({
                "type": "function",
                "function": tool,
            })
    return ollama_tools


def _detect_json_tool_calls(text: str) -> list:
    """
    Detect raw JSON tool calls in model output (fallback).
    Handles: {"name": "bash", "arguments": {"command": "..."}}
    """
    results = []
    for match in re.finditer(r'\{[^{}]*"name"\s*:\s*"[^"]+"\s*,\s*"arguments"\s*:\s*\{', text):
        start = match.start()
        depth = 0
        i = start
        while i < len(text):
            if text[i] == '{':
                depth += 1
            elif text[i] == '}':
                depth -= 1
                if depth == 0:
                    candidate = text[start:i+1]
                    try:
                        parsed = json.loads(candidate)
                        name = parsed.get("name", "")
                        args = parsed.get("arguments", {})
                        if name:
                            args_str = json.dumps(args) if isinstance(args, dict) else str(args)
                            call_id = f"call_{hash(name + args_str) & 0xFFFFFFFF:08x}"
                            results.append({
                                "name": name,
                                "arguments": args_str,
                                "call_id": call_id,
                            })
                    except (json.JSONDecodeError, ValueError):
                        pass
                    break
            i += 1
    return results


def _detect_text_tool_calls(text: str) -> list:
    """
    Detect tool invocations in model text output.
    Handles multiple formats models use:
      - bash(command_here)
      - bash command_here
      - file_read(path)
      - file_read path
    Only matches when the ENTIRE response (stripped) looks like a tool call,
    not when "bash" appears in a longer explanation.
    """
    text = text.strip()
    if not text:
        return []

    results = []
    known_tools = {"bash", "file_write", "file_read", "file_edit", "glob", "grep"}

    # First: try tool_name(args) format
    for match in re.finditer(r'\b(' + '|'.join(known_tools) + r')\((.+?)\)\s*$', text, re.DOTALL):
        name = match.group(1)
        raw_args = match.group(2).strip()
        tc = _build_simple_tool_call(name, raw_args)
        if tc:
            results.append(tc)

    if results:
        return results

    # Second: if entire response is "tool_name args" on one or few lines
    # Only match if response is short (< 200 chars) to avoid false positives
    if len(text) < 200:
        for tool_name in known_tools:
            if text.startswith(tool_name + " ") or text.startswith(tool_name + "\n"):
                raw_args = text[len(tool_name):].strip()
                if raw_args:
                    tc = _build_simple_tool_call(tool_name, raw_args)
                    if tc:
                        results.append(tc)
                        break

    return results


def _build_simple_tool_call(name: str, raw_args: str) -> dict | None:
    """Build a tool call dict from a name and raw argument string."""
    if name == "bash":
        args = {"command": raw_args}
    elif name in ("file_read", "glob", "grep"):
        args = {"path" if name == "file_read" else "pattern": raw_args}
    else:
        return None  # file_write, file_edit need multiple args

    args_str = json.dumps(args)
    call_id = f"call_{hash(name + args_str) & 0xFFFFFFFF:08x}"
    return {"name": name, "arguments": args_str, "call_id": call_id}


# ---------------------------------------------------------------------------
# Ollama backend
# ---------------------------------------------------------------------------

class OllamaBackend:
    """Direct connection to Ollama /api/chat with adaptive tool calling.

    Automatically detects whether a model supports native tool calling.
    If it does, tools are passed via the Ollama API. If not, tool definitions
    are injected into the system prompt and responses are parsed for
    <tool_call> XML blocks (same approach as LM Studio).

    Detection runs once per model and is cached for the session.
    """

    # Cache of model -> bool (True = supports native tools)
    _tool_support_cache: dict[str, bool] = {}

    def __init__(self, base_url: str, model: str):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.name = "ollama"
        self._use_native_tools: bool | None = None  # None = not yet detected
        # CRITICAL: Options must be IDENTICAL across ALL requests (warm_up, stream, etc.)
        # for this backend instance. If any option differs between requests, Ollama may
        # UNLOAD and RELOAD the model. Tune via env once per process, not per call.
        self._ollama_options = {
            "num_gpu": int(os.environ.get("RESONANT_OLLAMA_NUM_GPU", "99")),
            "num_batch": int(os.environ.get("RESONANT_OLLAMA_NUM_BATCH", "512")),
            "num_ctx": int(os.environ.get("RESONANT_OLLAMA_NUM_CTX", "4096")),
        }
        self._ollama_keep_alive = (os.environ.get("RESONANT_OLLAMA_KEEP_ALIVE", "60m").strip() or "60m")
        self._ollama_http_timeout = float(os.environ.get("RESONANT_OLLAMA_HTTP_TIMEOUT_SEC", "180"))
        self._ollama_http_read_timeout = float(
            os.environ.get("RESONANT_OLLAMA_HTTP_READ_TIMEOUT_SEC", "120")
        )

    # Models known to support/not support native tool calling
    # (avoids the probe request for common models)
    _KNOWN_TOOL_SUPPORT = {
        # Supports native tools
        "llama3.1", "llama3.2", "llama3.3", "llama4",
        "qwen2.5", "qwen2.5-coder", "qwen3", "qwen3.5", "qwen3-coder-next", "qwen3-next",
        "mistral", "mistral-nemo", "mistral-small", "mistral-large",
        "command-r", "command-r-plus",
        "gemma2", "gemma3", "gemma4",
        "phi4", "phi4-mini",
        "deepseek-r1", "deepseek-v3.2",
        # Ollama cloud models (routed, tool-capable)
        "minimax-m2", "minimax-m2.5", "minimax-m2.7",
        "nemotron-3-super", "nemotron-3-nano",
        "kimi-k2.5",
        "glm-4.7", "glm-4.7-flash", "glm-5", "glm-5.1",
        "devstral-2", "devstral-small-2",
        "cogito-2.1",
        "gemini-3-flash-preview",
        "ministral-3",
        "rnj-1",
    }

    # Cloud models to offer even if not yet pulled locally
    CLOUD_MODELS = [
        "minimax-m2.7:cloud",
        "minimax-m2.5:cloud",
        "nemotron-3-super:cloud",
        "kimi-k2.5:cloud",
        "glm-5.1:cloud",
        "glm-4.7-flash:cloud",
        "deepseek-v3.2:cloud",
        "qwen3.5:cloud",
        "gemma4:cloud",
    ]
    _KNOWN_NO_TOOL_SUPPORT = {
        # Text-only, no native tool calling
        "llama2", "llama3",  # Pre-3.1
        "codellama",
        "phi", "phi3",
        "gemma",  # v1
        "starcoder", "starcoder2",
        "deepseek-coder", "deepseek-coder-v2",
        "yi",
    }

    def _detect_tool_support(self) -> bool:
        """Detect whether the current model supports native tool calling.

        Strategy:
        1. Check known model lists first (instant, no network)
        2. Check Ollama model info for 'tools' in template/capabilities
        3. Probe with a tiny tool call as last resort
        """
        # Check class-level cache
        if self.model in OllamaBackend._tool_support_cache:
            result = OllamaBackend._tool_support_cache[self.model]
            logger.info(f"Tool support for {self.model}: {result} (cached)")
            return result

        # Check known model lists (strip tag suffixes like :7b, :latest)
        base_model = self.model.split(":")[0].lower()
        if base_model in self._KNOWN_TOOL_SUPPORT:
            OllamaBackend._tool_support_cache[self.model] = True
            logger.info(f"Tool support for {self.model}: True (known)")
            return True
        if base_model in self._KNOWN_NO_TOOL_SUPPORT:
            OllamaBackend._tool_support_cache[self.model] = False
            logger.info(f"Tool support for {self.model}: False (known)")
            return False

        # Check Ollama model info endpoint for template/capabilities hints
        try:
            resp = httpx.post(
                f"{self.base_url}/api/show",
                json={"model": self.model},
                timeout=10,
            )
            if resp.status_code == 200:
                info = resp.json()
                template = info.get("template", "")
                # Models with tool support typically have {{.Tools}} in their template
                if "{{.Tools}}" in template or "{{ .Tools }}" in template:
                    OllamaBackend._tool_support_cache[self.model] = True
                    logger.info(f"Tool support for {self.model}: True (template)")
                    return True
                # If template exists but no tools placeholder, likely no support
                if template and "{{.Tools}}" not in template:
                    OllamaBackend._tool_support_cache[self.model] = False
                    logger.info(f"Tool support for {self.model}: False (no tools in template)")
                    return False
        except Exception as e:
            logger.debug(f"Could not check model info for {self.model}: {e}")

        # Probe: send a minimal request with a simple tool and check response format
        try:
            opts = dict(self._ollama_options)
            opts["num_predict"] = 50
            probe_tool = [{
                "type": "function",
                "function": {
                    "name": "test_tool",
                    "description": "Test tool",
                    "parameters": {
                        "type": "object",
                        "properties": {"value": {"type": "string"}},
                        "required": ["value"],
                    },
                },
            }]
            resp = httpx.post(
                f"{self.base_url}/api/chat",
                json={
                    "model": self.model,
                    "messages": [{"role": "user", "content": "Call test_tool with value 'hello'"}],
                    "tools": probe_tool,
                    "stream": False,
                    "keep_alive": self._ollama_keep_alive,
                    "options": opts,
                },
                timeout=30,
            )
            if resp.status_code == 200:
                data = resp.json()
                msg = data.get("message", {})
                has_native = bool(msg.get("tool_calls"))
                OllamaBackend._tool_support_cache[self.model] = has_native
                logger.info(f"Tool support for {self.model}: {has_native} (probed)")
                return has_native
        except Exception as e:
            logger.debug(f"Tool probe failed for {self.model}: {e}")

        # Default: assume native support (will fall back to text parsing anyway)
        OllamaBackend._tool_support_cache[self.model] = True
        logger.info(f"Tool support for {self.model}: True (default)")
        return True

    @property
    def tool_mode(self) -> str:
        """Return the current tool calling mode: 'native', 'text', or 'unknown'."""
        if self._use_native_tools is None:
            return "unknown"
        return "native" if self._use_native_tools else "text"

    def warm_up(self):
        """Pre-load the model into Ollama's memory so the first request is fast."""
        try:
            # Use EXACT same options as stream() to prevent Ollama from reloading
            opts = dict(self._ollama_options)
            opts["num_predict"] = 1
            httpx.post(
                f"{self.base_url}/api/chat",
                json={
                    "model": self.model,
                    "messages": [{"role": "user", "content": "hi"}],
                    "stream": False,
                    "keep_alive": self._ollama_keep_alive,
                    "options": opts,
                },
                timeout=max(120.0, self._ollama_http_timeout),
            )
            logger.info("Model %s warmed up", self.model)
        except Exception:
            pass  # Non-critical

    def health(self) -> dict:
        """Check Ollama is reachable and return model info."""
        try:
            resp = httpx.get(f"{self.base_url}/api/tags", timeout=5)
            resp.raise_for_status()
            data = resp.json()
            models = [m["name"] for m in data.get("models", [])]
            return {
                "status": "ready",
                "backend": "ollama",
                "model": self.model,
                "available_models": models,
            }
        except Exception as e:
            return {"status": "error", "message": str(e)}

    def list_models(self) -> list:
        """Return list of available model names, including cloud models."""
        local: list[str] = []
        try:
            resp = httpx.get(f"{self.base_url}/api/tags", timeout=5)
            resp.raise_for_status()
            local = [m["name"] for m in resp.json().get("models", [])
                     if not any(kw in m["name"].lower() for kw in ("embed", "bert", "bge", "nomic"))]
        except Exception:
            pass
        local_set = {m.lower() for m in local}
        for cloud in self.CLOUD_MODELS:
            if cloud.lower() not in local_set:
                local.append(cloud)
        return local

    def classify(self, prompt: str, max_tokens: int = 50) -> str:
        """Quick non-streaming LLM call for classification. Returns plain text."""
        opts = dict(self._ollama_options)
        opts["num_predict"] = max_tokens
        resp = httpx.post(
            f"{self.base_url}/api/chat",
            json={
                "model": self.model,
                "messages": [{"role": "user", "content": prompt}],
                "stream": False,
                "keep_alive": self._ollama_keep_alive,
                "options": opts,
            },
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json().get("message", {}).get("content", "").strip()

    def stream(
        self,
        user_msg: str,
        conversation_history: list,
        instructions: str,
        tools: list,
        max_tokens: int = 4096,
        cancel_event=None,
    ) -> Iterator[Tuple[str, dict]]:
        """
        Stream a chat completion from Ollama with adaptive tool calling.

        Detects whether the model supports native tool calling:
        - Native mode: tools passed via Ollama API, model returns structured tool_calls
        - Text mode: tool definitions injected into system prompt, model returns
          <tool_call> XML blocks which are parsed from the response

        Both modes fall through to JSON/XML/text detection as a final safety net.
        """
        # Detect tool support on first use
        if self._use_native_tools is None and tools:
            self._use_native_tools = self._detect_tool_support()

        use_native = self._use_native_tools if self._use_native_tools is not None else True

        # Build messages array
        messages = []

        # System message — inject tool definitions for text-based mode
        sys_content = instructions
        if not use_native and tools:
            sys_content += build_tool_system_prompt(tools)

        messages.append({"role": "system", "content": sys_content})

        # Conversation history — format depends on tool mode
        for turn in conversation_history:
            role = turn["role"]
            content = turn["content"]
            if role == "tool_call":
                if use_native:
                    # Native mode: structured tool call
                    args = turn.get("arguments", "{}")
                    if isinstance(args, str):
                        try:
                            args = json.loads(args)
                        except json.JSONDecodeError:
                            args = {}
                    messages.append({
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [{
                            "function": {
                                "name": turn.get("name", ""),
                                "arguments": args,
                            }
                        }],
                    })
                else:
                    # Text mode: reconstruct XML tool call in assistant message
                    args = turn.get("arguments", "{}")
                    if isinstance(args, dict):
                        args = json.dumps(args)
                    messages.append({
                        "role": "assistant",
                        "content": f'<tool_call>\n{{"name": "{turn.get("name", "")}", "arguments": {args}}}\n</tool_call>',
                    })
            elif role == "tool_result":
                if use_native:
                    # Check for screenshot image in tool result (computer use loop)
                    if turn.get("image"):
                        img = turn["image"]
                        # Ollama: include screenshot as user message with image
                        # since tool role doesn't support images in Ollama
                        messages.append({
                            "role": "tool",
                            "content": content,
                        })
                        messages.append({
                            "role": "user",
                            "content": "[Screenshot from tool result]",
                            "images": [img.get("data", "")],
                        })
                    else:
                        messages.append({
                            "role": "tool",
                            "content": content,
                        })
                else:
                    # Text mode: tool results become user messages
                    tool_name = turn.get("name", "tool")
                    if turn.get("image"):
                        img = turn["image"]
                        messages.append({
                            "role": "user",
                            "content": f"[{tool_name} result]\n{content}",
                            "images": [img.get("data", "")],
                        })
                    else:
                        messages.append({
                            "role": "user",
                            "content": f"[{tool_name} result]\n{content}",
                        })
            elif role in ("user", "assistant"):
                # Handle multimodal content (images + text)
                if isinstance(content, list):
                    # Ollama uses "images" field for vision models
                    text_parts = []
                    images = []
                    for part in content:
                        if part.get("type") == "image":
                            images.append(part.get("data", ""))  # base64 string
                        elif part.get("type") == "text":
                            text_parts.append(part.get("text", ""))
                    msg = {"role": role, "content": " ".join(text_parts)}
                    if images:
                        msg["images"] = images
                    messages.append(msg)
                else:
                    messages.append({"role": role, "content": content})

        # Current user message
        messages.append({"role": "user", "content": user_msg})

        # Use fixed options — NEVER change num_ctx or other params between requests,
        # or Ollama will reload the entire model from disk (30-120s penalty)
        opts = dict(self._ollama_options)
        opts["num_predict"] = max_tokens

        payload = {
            "model": self.model,
            "messages": messages,
            "stream": True,
            "keep_alive": self._ollama_keep_alive,
            "options": opts,
        }

        # Pass tools via Ollama API only in native mode
        # In text mode, tools are already in the system prompt
        if tools and use_native:
            payload["tools"] = _convert_tools_for_ollama(tools)

        collected_tokens = []      # Text tokens (buffered initially)
        native_tool_calls = []     # Tool calls from Ollama's native API
        streaming = False          # True once we've started streaming text to TUI
        has_tools = bool(tools)    # Whether tools are available (determines buffering)
        text_mode = has_tools and not use_native  # Text-based tool calling active

        # Buffering strategy:
        # Native mode:
        #   - If response starts with '{' or '<tool' → always buffer (tool call JSON/XML)
        #   - If response is short text starting with a tool name → buffer briefly
        #   - Long text → stream immediately
        # Text mode:
        #   - Always buffer until we can determine if <tool_call> is present
        #   - Stream text outside of tool_call blocks at end
        force_buffer = False   # Always buffer (JSON/XML tool call detected)
        known_tool_names = {"bash", "file_write", "file_read", "file_edit", "glob", "grep"}

        try:
            stream_timeout = httpx.Timeout(
                self._ollama_http_timeout,
                connect=10.0,
                read=self._ollama_http_read_timeout,
            )
            with httpx.Client(timeout=stream_timeout) as client:
                with client.stream("POST", f"{self.base_url}/api/chat", json=payload) as resp:
                    resp.raise_for_status()
                    buf = b""
                    for chunk in resp.iter_raw():
                        if cancel_event is not None and cancel_event.is_set():
                            return
                        buf += chunk
                        while b"\n" in buf:
                            line_bytes, buf = buf.split(b"\n", 1)
                            line = line_bytes.decode("utf-8", errors="replace").strip()
                            if not line:
                                continue
                            try:
                                data = json.loads(line)
                            except json.JSONDecodeError:
                                continue

                            if data.get("done"):
                                # --- End of response ---
                                full_text = "".join(collected_tokens)
                                # Strip model control tokens
                                clean_text = re.sub(r'<\|im_\w+\|>', '', full_text).strip()

                                # Detect tool calls from ALL sources
                                detected_calls = list(native_tool_calls)

                                plain_text = ""  # Text outside tool_call blocks
                                if not detected_calls:
                                    # Try XML tags — returns (plain_text, tool_calls)
                                    plain_text, xml_calls = parse_tool_calls(clean_text)
                                    for tc in xml_calls:
                                        call_id = f"call_{hash(tc['name'] + tc['arguments']) & 0xFFFFFFFF:08x}"
                                        detected_calls.append({
                                            "name": tc["name"],
                                            "arguments": tc["arguments"],
                                            "call_id": call_id,
                                        })

                                if not detected_calls:
                                    # Try raw JSON (in buffered OR streamed text)
                                    detected_calls = _detect_json_tool_calls(clean_text)

                                if not detected_calls:
                                    # Try text format: bash(cmd) or bash cmd
                                    detected_calls = _detect_text_tool_calls(clean_text)

                                if detected_calls:
                                    # In text mode, emit any plain text before the tool calls
                                    # (model may explain what it's doing before the <tool_call>)
                                    if text_mode and plain_text:
                                        yield (EVENT_TEXT_DELTA, {"delta": plain_text})
                                    # Yield tool calls
                                    for tc in detected_calls:
                                        yield (EVENT_TOOL_CALL, tc)
                                elif not streaming and collected_tokens:
                                    # Was buffering text, no tool calls found — flush as text
                                    for t in collected_tokens:
                                        yield (EVENT_TEXT_DELTA, {"delta": t})

                                # Stats
                                stats = {}
                                for key in ("total_duration", "eval_count", "eval_duration",
                                             "prompt_eval_count", "prompt_eval_duration"):
                                    if key in data:
                                        stats[key] = data[key]

                                yield (EVENT_DONE, {
                                    "cognitive_state": None,
                                    "stats": stats,
                                    "model": self.model,
                                })
                                return

                            # Check for native tool calls
                            msg = data.get("message", {})
                            native_tc = msg.get("tool_calls", [])
                            if native_tc:
                                for tc in native_tc:
                                    fn = tc.get("function", {})
                                    name = fn.get("name", "")
                                    args = fn.get("arguments", {})
                                    args_str = json.dumps(args) if isinstance(args, dict) else str(args)
                                    call_id = f"call_{hash(name + args_str) & 0xFFFFFFFF:08x}"
                                    native_tool_calls.append({
                                        "name": name,
                                        "arguments": args_str,
                                        "call_id": call_id,
                                    })
                                continue

                            # Streaming text token
                            token = msg.get("content", "")
                            if token:
                                collected_tokens.append(token)

                                if text_mode:
                                    # Text-based tool calling: always buffer the full
                                    # response since <tool_call> blocks can appear
                                    # anywhere (after explanatory text, at the end, etc.)
                                    # We parse everything at done time.
                                    continue
                                elif streaming:
                                    # Already streaming — send immediately
                                    yield (EVENT_TEXT_DELTA, {"delta": token})
                                elif not has_tools:
                                    # No tools available — stream immediately
                                    streaming = True
                                    yield (EVENT_TEXT_DELTA, {"delta": token})
                                else:
                                    # Native tool mode — check first non-whitespace char
                                    acc = "".join(collected_tokens).lstrip()
                                    if not acc:
                                        continue  # Only whitespace so far
                                    if not force_buffer:
                                        if acc[0] == '{' or acc.startswith('<tool'):
                                            force_buffer = True
                                        elif any(acc.startswith(t + " ") or acc.startswith(t + "(") or acc.startswith(t + "\n") for t in known_tool_names):
                                            # Might be "bash ls" or "bash(ls)" — buffer if short
                                            total_chars = sum(len(t) for t in collected_tokens)
                                            if total_chars < 200:
                                                continue  # Keep buffering, check at done
                                    if force_buffer:
                                        continue  # Keep buffering until done
                                    # Doesn't look like a tool call → stream
                                    streaming = True
                                    for t in collected_tokens:
                                        yield (EVENT_TEXT_DELTA, {"delta": t})

        except Exception as e:
            yield (EVENT_ERROR, {"message": str(e)})


# ---------------------------------------------------------------------------
# Resonant Engine backend
# ---------------------------------------------------------------------------

class ResonantBackend:
    """Connection to the Resonant Cognitive Engine via SSE."""

    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")
        self.model = "resonant-engine"
        self.name = "resonant"

    def health(self) -> dict:
        """Check engine health."""
        try:
            resp = httpx.get(f"{self.base_url}/health", timeout=5)
            resp.raise_for_status()
            data = resp.json()
            data["backend"] = "resonant"
            data["model"] = "resonant-engine"
            return data
        except Exception as e:
            return {"status": "error", "message": str(e)}

    def get_harness_state(self, project_path: str) -> dict:
        """Fetch canonical harness state from the engine."""
        resp = httpx.get(
            f"{self.base_url}/v1/harness/state",
            params={"project_path": project_path},
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()

    def apply_harness_update(
        self,
        *,
        project_path: str,
        session_mode: str = "code",
        session_role: str = "",
        payload: dict | None = None,
        assistant_text: str = "",
        user_request: str = "",
    ) -> dict:
        resp = httpx.post(
            f"{self.base_url}/v1/harness/update",
            json={
                "project_path": project_path,
                "session_mode": session_mode,
                "session_role": session_role,
                "payload": dict(payload or {}),
                "assistant_text": assistant_text,
                "user_request": user_request,
            },
            timeout=60,
        )
        resp.raise_for_status()
        return resp.json()

    def recover_harness(
        self,
        *,
        project_path: str,
        failed_role: str,
        reason: str,
        objective: str = "",
    ) -> dict:
        resp = httpx.post(
            f"{self.base_url}/v1/harness/teacher-recover",
            json={
                "project_path": project_path,
                "failed_role": failed_role,
                "reason": reason,
                "objective": objective,
            },
            timeout=300,
        )
        resp.raise_for_status()
        return resp.json()

    def set_harness_sprint(
        self,
        *,
        project_path: str,
        sprint_id: str,
        feature_name: str = "",
        objective: str,
        deliverables: list[str] | None = None,
        acceptance_checks: list[str] | None = None,
        evaluator_focus: list[str] | None = None,
        target_files: list[str] | None = None,
        target_line_hints: list[str] | None = None,
        validation_commands: list[str] | None = None,
        edit_strategy: str = "",
        status: str = "proposed",
        session_role: str = "planner",
    ) -> dict:
        resp = httpx.post(
            f"{self.base_url}/v1/harness/sprint",
            json={
                "project_path": project_path,
                "sprint_id": sprint_id,
                "feature_name": feature_name,
                "objective": objective,
                "deliverables": list(deliverables or []),
                "acceptance_checks": list(acceptance_checks or []),
                "evaluator_focus": list(evaluator_focus or []),
                "target_files": list(target_files or []),
                "target_line_hints": list(target_line_hints or []),
                "validation_commands": list(validation_commands or []),
                "edit_strategy": edit_strategy,
                "status": status,
                "session_role": session_role,
            },
            timeout=60,
        )
        resp.raise_for_status()
        return resp.json()

    def set_harness_contract_status(
        self,
        *,
        project_path: str,
        status: str,
        session_role: str = "",
    ) -> dict:
        resp = httpx.post(
            f"{self.base_url}/v1/harness/contract-status",
            json={
                "project_path": project_path,
                "status": status,
                "session_role": session_role,
            },
            timeout=60,
        )
        resp.raise_for_status()
        return resp.json()

    def set_evaluator_verdict(
        self,
        *,
        project_path: str,
        sprint_id: str,
        verdict: str,
        findings: list[str] | None = None,
        required_revisions: list[str] | None = None,
        passed_checks: list[str] | None = None,
        failed_checks: list[str] | None = None,
        score: float | None = None,
    ) -> dict:
        resp = httpx.post(
            f"{self.base_url}/v1/harness/evaluator-verdict",
            json={
                "project_path": project_path,
                "sprint_id": sprint_id,
                "verdict": verdict,
                "findings": list(findings or []),
                "required_revisions": list(required_revisions or []),
                "passed_checks": list(passed_checks or []),
                "failed_checks": list(failed_checks or []),
                "score": score,
            },
            timeout=60,
        )
        resp.raise_for_status()
        return resp.json()

    def prepare_harness_step(
        self,
        *,
        project_path: str,
        session_mode: str = "code",
        session_role: str = "",
        objective: str = "",
        execute: bool = False,
        prompt_override: str = "",
    ) -> dict:
        """Prepare or execute a harness step through the engine-owned API."""
        resp = httpx.post(
            f"{self.base_url}/v1/harness/step",
            json={
                "project_path": project_path,
                "session_mode": session_mode,
                "session_role": session_role,
                "objective": objective,
                "execute": execute,
                "prompt_override": prompt_override,
            },
            timeout=180,
        )
        resp.raise_for_status()
        return resp.json()

    def start_harness_cycle(
        self,
        *,
        project_path: str,
        name: str = "",
        objective: str = "",
        max_loops: int = 6,
    ) -> dict:
        resp = httpx.post(
            f"{self.base_url}/v1/harness/cycles/start",
            json={
                "project_path": project_path,
                "name": name,
                "objective": objective,
                "max_loops": max_loops,
            },
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()

    def list_harness_cycles(self, *, limit: int = 20) -> dict:
        resp = httpx.get(
            f"{self.base_url}/v1/harness/cycles",
            params={"limit": limit},
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()

    def get_harness_cycle(self, run_id: str) -> dict:
        resp = httpx.get(
            f"{self.base_url}/v1/harness/cycles/{run_id}",
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()

    def cancel_harness_cycle(self, run_id: str) -> dict:
        resp = httpx.post(
            f"{self.base_url}/v1/harness/cycles/{run_id}/cancel",
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()

    def create_harness_schedule(
        self,
        *,
        project_path: str,
        name: str,
        prompt: str,
        schedule: str,
        max_loops: int = 6,
    ) -> dict:
        resp = httpx.post(
            f"{self.base_url}/v1/harness/schedules",
            json={
                "project_path": project_path,
                "name": name,
                "prompt": prompt,
                "schedule": schedule,
                "max_loops": max_loops,
            },
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()

    def list_harness_schedules(self) -> dict:
        resp = httpx.get(
            f"{self.base_url}/v1/harness/schedules",
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()

    def update_harness_schedule(self, task_id: str, **updates) -> dict:
        resp = httpx.patch(
            f"{self.base_url}/v1/harness/schedules/{task_id}",
            json=updates,
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()

    def delete_harness_schedule(self, task_id: str) -> dict:
        resp = httpx.delete(
            f"{self.base_url}/v1/harness/schedules/{task_id}",
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()

    def list_models(self) -> list:
        """Resonant engine is a single model."""
        return ["resonant-engine"]

    def classify(self, prompt: str, max_tokens: int = 50) -> str:
        """Quick non-streaming LLM call for classification. Returns plain text."""
        payload = {
            "model": "resonant-engine",
            "input": [{"role": "user", "content": [{"type": "input_text", "text": prompt}]}],
            "instructions": "Respond with exactly one word.",
            "tools": [],
            "stream": False,
            "max_output_tokens": max_tokens,
        }
        resp = httpx.post(
            f"{self.base_url}/v1/responses",
            json=payload,
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        for item in data.get("output", []):
            if item.get("type") == "message":
                for c in item.get("content", []):
                    if c.get("type") == "output_text":
                        return c.get("text", "").strip()
        return ""

    def stream(
        self,
        user_msg: str,
        conversation_history: list,
        instructions: str,
        tools: list,
        max_tokens: int = 4096,
        cancel_event=None,
    ) -> Iterator[Tuple[str, dict]]:
        """
        Stream events from the Resonant Engine /v1/responses endpoint.
        """
        inp = []
        for turn in conversation_history:
            role = turn["role"]
            content = turn["content"]
            if role == "tool_result":
                inp.append({
                    "type": "function_call_output",
                    "call_id": turn.get("call_id", ""),
                    "output": content,
                })
            elif role == "tool_call":
                inp.append({
                    "type": "function_call",
                    "name": turn.get("name", ""),
                    "arguments": turn.get("arguments", "{}"),
                    "call_id": turn.get("call_id", ""),
                })
            else:
                inp.append({"role": role, "content": content})

        inp.append({"role": "user", "content": [{"type": "input_text", "text": user_msg}]})

        payload = {
            "model": "resonant-engine",
            "input": inp,
            "instructions": instructions,
            "tools": tools,
            "stream": True,
            "max_output_tokens": max_tokens,
        }

        try:
            with httpx.Client(timeout=httpx.Timeout(180.0, connect=10.0)) as client:
                with client.stream("POST", f"{self.base_url}/v1/responses", json=payload) as resp:
                    resp.raise_for_status()
                    event_type = None
                    for line in resp.iter_lines():
                        if cancel_event is not None and cancel_event.is_set():
                            return
                        line = line.strip()
                        if not line:
                            continue
                        if line.startswith("event: "):
                            event_type = line[7:]
                        elif line.startswith("data: ") and event_type:
                            try:
                                data = json.loads(line[6:])
                            except json.JSONDecodeError:
                                event_type = None
                                continue

                            if event_type == "response.output_text.delta":
                                yield (EVENT_TEXT_DELTA, {"delta": data.get("delta", "")})
                            elif event_type == "response.output_item.done":
                                item = data.get("item", {})
                                if item.get("type") == "function_call":
                                    yield (EVENT_TOOL_CALL, {
                                        "name": item.get("name", ""),
                                        "arguments": item.get("arguments", "{}"),
                                        "call_id": item.get("call_id", ""),
                                    })
                            elif event_type == "response.completed":
                                resp_data = data.get("response", {})
                                yield (EVENT_DONE, {
                                    "cognitive_state": resp_data.get("cognitive_state"),
                                    "stats": {},
                                    "model": "resonant-engine",
                                })
                            event_type = None

        except Exception as e:
            yield (EVENT_ERROR, {"message": str(e)})


# ---------------------------------------------------------------------------
# Claude (Anthropic) backend
# ---------------------------------------------------------------------------

def _convert_tools_for_claude(tools: list) -> list:
    """Convert AGENT_TOOLS (OpenAI format) to Anthropic's tool format."""
    claude_tools = []
    for tool in tools:
        fn = tool.get("function", tool) if tool.get("type") == "function" else tool
        claude_tools.append({
            "name": fn["name"],
            "description": fn.get("description", ""),
            "input_schema": fn.get("parameters", {"type": "object", "properties": {}}),
        })
    return claude_tools


class ClaudeBackend:
    """Anthropic Claude API with native tool use and streaming."""

    # Available Claude models (user-facing)
    MODELS = [
        "claude-sonnet-4-20250514",
        "claude-opus-4-20250514",
        "claude-haiku-4-20250414",
        "claude-3-5-sonnet-20241022",
        "claude-3-5-haiku-20241022",
    ]

    def __init__(self, api_key: str, model: str = "claude-sonnet-4-20250514"):
        self.api_key = api_key
        self.model = model
        self.name = "claude"
        self._client = None

    def _get_client(self):
        if self._client is None:
            try:
                import anthropic
            except ImportError:
                raise ImportError(
                    "anthropic package required for Claude backend. "
                    "Install with: pip install resonant-client[claude]"
                )
            self._client = anthropic.Anthropic(api_key=self.api_key)
        return self._client

    def health(self) -> dict:
        """Check that the API key works."""
        try:
            self._get_client()
            return {
                "status": "ready",
                "backend": "claude",
                "model": self.model,
                "available_models": self.MODELS,
            }
        except Exception as e:
            return {"status": "error", "message": str(e)}

    # Fast model for classification (cheap/quick)
    _CLASSIFY_MODEL = "claude-haiku-4-20250414"

    def list_models(self) -> list:
        return list(self.MODELS)

    def classify(self, prompt: str, max_tokens: int = 50) -> str:
        """Quick non-streaming LLM call using Haiku for classification."""
        client = self._get_client()
        resp = client.messages.create(
            model=self._CLASSIFY_MODEL,
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}],
        )
        for block in resp.content:
            if block.type == "text":
                return block.text.strip()
        return ""

    def stream(
        self,
        user_msg: str,
        conversation_history: list,
        instructions: str,
        tools: list,
        max_tokens: int = 16384,
        cancel_event=None,
    ) -> Iterator[Tuple[str, dict]]:
        """Stream a response from the Claude API with native tool use."""
        client = self._get_client()

        # Build messages
        messages = []
        for turn in conversation_history:
            role = turn["role"]
            content = turn["content"]
            if role == "tool_call":
                # Anthropic: assistant message with tool_use content block
                args = turn.get("arguments", "{}")
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except json.JSONDecodeError:
                        args = {}
                call_id = turn.get("call_id", "toolu_placeholder")
                messages.append({
                    "role": "assistant",
                    "content": [{
                        "type": "tool_use",
                        "id": call_id,
                        "name": turn.get("name", ""),
                        "input": args,
                    }],
                })
            elif role == "tool_result":
                call_id = turn.get("call_id", "toolu_placeholder")
                tool_result_content = [{"type": "text", "text": content}]
                # Include screenshot image if present (computer use / browser)
                if turn.get("image"):
                    img = turn["image"]
                    tool_result_content.append({
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": img.get("media_type", "image/png"),
                            "data": img["data"],
                        },
                    })
                messages.append({
                    "role": "user",
                    "content": [{
                        "type": "tool_result",
                        "tool_use_id": call_id,
                        "content": tool_result_content,
                    }],
                })
            elif role in ("user", "assistant"):
                # Handle multimodal content (images + text)
                if isinstance(content, list):
                    # Convert our image format to Anthropic's format
                    anthropic_content = []
                    for part in content:
                        if part.get("type") == "image":
                            anthropic_content.append({
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": part.get("media_type", "image/png"),
                                    "data": part.get("data", ""),
                                },
                            })
                        elif part.get("type") == "text":
                            anthropic_content.append({"type": "text", "text": part.get("text", "")})
                        else:
                            anthropic_content.append(part)
                    messages.append({"role": role, "content": anthropic_content})
                else:
                    messages.append({"role": role, "content": content})

        messages.append({"role": "user", "content": user_msg})

        # Merge consecutive same-role messages (Anthropic requires alternating)
        merged = []
        for msg in messages:
            if merged and merged[-1]["role"] == msg["role"]:
                prev = merged[-1]["content"]
                curr = msg["content"]
                if isinstance(prev, str) and isinstance(curr, str):
                    merged[-1]["content"] = prev + "\n" + curr
                elif isinstance(prev, list) and isinstance(curr, list):
                    merged[-1]["content"] = prev + curr
                elif isinstance(prev, str) and isinstance(curr, list):
                    merged[-1]["content"] = [{"type": "text", "text": prev}] + curr
                elif isinstance(prev, list) and isinstance(curr, str):
                    merged[-1]["content"] = prev + [{"type": "text", "text": curr}]
            else:
                merged.append(msg)
        messages = merged

        # Build kwargs
        kwargs = {
            "model": self.model,
            "max_tokens": max_tokens,
            "system": instructions,
            "messages": messages,
        }
        if tools:
            kwargs["tools"] = _convert_tools_for_claude(tools)

        try:
            import anthropic
            with client.messages.stream(**kwargs) as stream:
                input_tokens = 0
                output_tokens = 0

                # Track tool_use blocks in real-time (not waiting for stream end)
                # This is how Claude Code gets fast tool call rendering
                pending_tools = {}  # block_index -> {"id": ..., "name": ..., "json_parts": [...]}

                for event in stream:
                    if cancel_event is not None and cancel_event.is_set():
                        return
                    if not hasattr(event, 'type'):
                        continue

                    if event.type == "content_block_start":
                        # Tool use blocks announce themselves immediately
                        # with name and ID — we can show the tool call NOW
                        block = getattr(event, 'content_block', None)
                        idx = getattr(event, 'index', 0)
                        if block and getattr(block, 'type', '') == "tool_use":
                            pending_tools[idx] = {
                                "id": block.id,
                                "name": block.name,
                                "json_parts": [],
                            }

                    elif event.type == "content_block_delta":
                        delta = event.delta
                        idx = getattr(event, 'index', 0)
                        if hasattr(delta, 'text'):
                            yield (EVENT_TEXT_DELTA, {"delta": delta.text})
                        elif hasattr(delta, 'partial_json'):
                            # Accumulate tool arguments as they stream
                            if idx in pending_tools:
                                pending_tools[idx]["json_parts"].append(delta.partial_json)

                    elif event.type == "content_block_stop":
                        # Tool use block complete — emit the tool call immediately
                        idx = getattr(event, 'index', 0)
                        if idx in pending_tools:
                            tool = pending_tools.pop(idx)
                            args_json = "".join(tool["json_parts"])
                            try:
                                # Validate JSON
                                args = json.loads(args_json) if args_json else {}
                                args_str = json.dumps(args)
                            except json.JSONDecodeError:
                                args_str = args_json or "{}"
                            yield (EVENT_TOOL_CALL, {
                                "name": tool["name"],
                                "arguments": args_str,
                                "call_id": tool["id"],
                            })

                    elif event.type == "message_delta":
                        if hasattr(event, 'usage') and event.usage:
                            output_tokens = getattr(event.usage, 'output_tokens', 0)

                    elif event.type == "message_start":
                        if hasattr(event, 'message') and hasattr(event.message, 'usage'):
                            input_tokens = getattr(event.message.usage, 'input_tokens', 0)

                # Get final token counts (don't re-extract tool calls — already emitted)
                try:
                    final_message = stream.get_final_message()
                    if final_message and final_message.usage:
                        output_tokens = final_message.usage.output_tokens or output_tokens
                        input_tokens = final_message.usage.input_tokens or input_tokens
                except Exception:
                    pass  # Token counts are nice-to-have, not critical

                yield (EVENT_DONE, {
                    "cognitive_state": None,
                    "stats": {
                        "input_tokens": input_tokens,
                        "output_tokens": output_tokens,
                    },
                    "model": self.model,
                })

        except Exception as e:
            yield (EVENT_ERROR, {"message": str(e)})


# ---------------------------------------------------------------------------
# OpenAI / GPT backend
# ---------------------------------------------------------------------------

def _convert_tools_for_openai(tools: list) -> list:
    """Ensure tools are in OpenAI function calling format."""
    result = []
    for tool in tools:
        if isinstance(tool, dict) and tool.get("type") == "function":
            result.append(tool)
        elif isinstance(tool, dict) and "name" in tool:
            result.append({"type": "function", "function": tool})
    return result


class OpenAIBackend:
    """OpenAI API with native function calling and streaming."""

    # Available models (user-facing)
    MODELS = [
        "gpt-4o",
        "gpt-4o-mini",
        "gpt-4.1",
        "gpt-4.1-mini",
        "gpt-4.1-nano",
        "o3",
        "o3-mini",
        "o4-mini",
    ]

    def __init__(self, api_key: str, model: str = "gpt-4o", base_url: str = None):
        self.api_key = api_key
        self.model = model
        self.base_url = base_url
        self.name = "lmstudio" if base_url else "openai"
        self._client = None

    def _get_client(self):
        if self._client is None:
            try:
                import openai
            except ImportError:
                raise ImportError(
                    "openai package required for OpenAI backend. "
                    "Install with: pip install resonant-client[openai]"
                )
            if self.base_url:
                read_timeout = float(os.environ.get("RESONANT_LMSTUDIO_READ_TIMEOUT_SEC", "600"))
            else:
                read_timeout = float(os.environ.get("RESONANT_OPENAI_READ_TIMEOUT_SEC", "120"))
            kwargs = {
                "api_key": self.api_key,
                "timeout": openai.Timeout(180.0, connect=10.0, read=read_timeout),
            }
            if self.base_url:
                kwargs["base_url"] = self.base_url
            self._client = openai.OpenAI(**kwargs)
        return self._client

    def health(self) -> dict:
        """Check that the API key is set."""
        try:
            self._get_client()
            return {
                "status": "ready",
                "backend": "openai",
                "model": self.model,
                "available_models": self.MODELS,
            }
        except Exception as e:
            return {"status": "error", "message": str(e)}

    # Fast model for classification (cheap/quick) — only used for OpenAI API
    _CLASSIFY_MODEL = "gpt-4.1-nano"

    def list_models(self) -> list:
        if self.base_url:
            # For LM Studio / custom endpoints, query the server
            try:
                client = self._get_client()
                models = client.models.list()
                return [m.id for m in models.data]
            except Exception:
                return [self.model]
        return list(self.MODELS)

    def classify(self, prompt: str, max_tokens: int = 50) -> str:
        """Quick non-streaming LLM call for classification."""
        client = self._get_client()
        # Use the configured model for custom endpoints (LM Studio etc.),
        # use the fast nano model for OpenAI API
        model = self.model if self.base_url else self._CLASSIFY_MODEL
        resp = client.chat.completions.create(
            model=model,
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}],
        )
        return (resp.choices[0].message.content or "").strip()

    def stream(
        self,
        user_msg: str,
        conversation_history: list,
        instructions: str,
        tools: list,
        max_tokens: int = 4096,
        cancel_event=None,
    ) -> Iterator[Tuple[str, dict]]:
        """Stream a response from OpenAI API with native function calling."""
        # LM Studio / local models: use text-based tool calling (system prompt + XML parsing)
        # OpenAI API: use native function calling
        use_native_tools = not self.base_url

        client = self._get_client()

        # Build system prompt — inject tool definitions for local models
        sys_content = instructions
        if not use_native_tools and tools:
            sys_content += build_tool_system_prompt(tools)

        # Build messages
        messages = [{"role": "system", "content": sys_content}]

        for turn in conversation_history:
            role = turn["role"]
            content = turn["content"]
            if role == "tool_call":
                if use_native_tools:
                    args = turn.get("arguments", "{}")
                    call_id = turn.get("call_id", "call_placeholder")
                    messages.append({
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [{
                            "id": call_id,
                            "type": "function",
                            "function": {
                                "name": turn.get("name", ""),
                                "arguments": args if isinstance(args, str) else json.dumps(args),
                            },
                        }],
                    })
                else:
                    # Text-based: reconstruct what the assistant said
                    args = turn.get("arguments", "{}")
                    if isinstance(args, dict):
                        args = json.dumps(args)
                    messages.append({
                        "role": "assistant",
                        "content": f'<tool_call>\n{{"name": "{turn.get("name", "")}", "arguments": {args}}}\n</tool_call>',
                    })
            elif role == "tool_result":
                if use_native_tools:
                    call_id = turn.get("call_id", "call_placeholder")
                    # OpenAI tool results can include images via content array
                    if turn.get("image"):
                        img = turn["image"]
                        tool_content = [
                            {"type": "text", "text": content},
                            {"type": "image_url", "image_url": {
                                "url": f"data:{img.get('media_type', 'image/png')};base64,{img['data']}",
                            }},
                        ]
                        messages.append({
                            "role": "tool",
                            "tool_call_id": call_id,
                            "content": tool_content,
                        })
                    else:
                        messages.append({
                            "role": "tool",
                            "tool_call_id": call_id,
                            "content": content,
                        })
                else:
                    # Text-based: tool results become user messages
                    tool_name = turn.get("name", "tool")
                    messages.append({
                        "role": "user",
                        "content": f"[{tool_name} result]\n{content}",
                    })
            elif role in ("user", "assistant"):
                # Handle multimodal content (images + text)
                if isinstance(content, list):
                    openai_content = []
                    for part in content:
                        if part.get("type") == "image":
                            b64 = part.get("data", "")
                            media = part.get("media_type", "image/png")
                            openai_content.append({
                                "type": "image_url",
                                "image_url": {"url": f"data:{media};base64,{b64}"},
                            })
                        elif part.get("type") == "text":
                            openai_content.append({"type": "text", "text": part.get("text", "")})
                        else:
                            openai_content.append(part)
                    messages.append({"role": role, "content": openai_content})
                else:
                    messages.append({"role": role, "content": content})

        messages.append({"role": "user", "content": user_msg})

        # Build kwargs
        kwargs = {
            "model": self.model,
            "messages": messages,
            "max_tokens": max_tokens,
            "stream": True,
        }
        if use_native_tools and tools:
            kwargs["tools"] = _convert_tools_for_openai(tools)

        try:
            # Collect tool call chunks (OpenAI streams them incrementally)
            pending_tool_calls = {}  # index -> {id, name, arguments_buf}
            prompt_tokens = 0
            completion_tokens = 0
            full_text = []  # Buffer for text-based tool call parsing

            response = client.chat.completions.create(**kwargs)
            for chunk in response:
                if cancel_event is not None and cancel_event.is_set():
                    return
                choice = chunk.choices[0] if chunk.choices else None
                if not choice:
                    # Usage chunk at the end
                    if chunk.usage:
                        prompt_tokens = chunk.usage.prompt_tokens or 0
                        completion_tokens = chunk.usage.completion_tokens or 0
                    continue

                delta = choice.delta

                # Text content
                if delta and delta.content:
                    if not use_native_tools:
                        full_text.append(delta.content)
                    else:
                        yield (EVENT_TEXT_DELTA, {"delta": delta.content})

                # Native tool call deltas (OpenAI API only)
                if use_native_tools and delta and delta.tool_calls:
                    for tc_delta in delta.tool_calls:
                        idx = tc_delta.index
                        if idx not in pending_tool_calls:
                            pending_tool_calls[idx] = {
                                "id": tc_delta.id or "",
                                "name": "",
                                "arguments_buf": [],
                            }
                        entry = pending_tool_calls[idx]
                        if tc_delta.id:
                            entry["id"] = tc_delta.id
                        if tc_delta.function:
                            if tc_delta.function.name:
                                entry["name"] = tc_delta.function.name
                            if tc_delta.function.arguments:
                                entry["arguments_buf"].append(tc_delta.function.arguments)

                # Check for finish
                if choice.finish_reason:
                    break

            if use_native_tools:
                # Emit collected native tool calls
                for idx in sorted(pending_tool_calls.keys()):
                    entry = pending_tool_calls[idx]
                    args_str = "".join(entry["arguments_buf"])
                    yield (EVENT_TOOL_CALL, {
                        "name": entry["name"],
                        "arguments": args_str,
                        "call_id": entry["id"],
                    })
            else:
                # Parse text-based <tool_call> blocks from full response
                combined = "".join(full_text)
                plain, parsed_calls = parse_tool_calls(combined)

                # Emit cleaned text (without <think> and <tool_call> blocks)
                if plain:
                    yield (EVENT_TEXT_DELTA, {"delta": plain})

                for tc in parsed_calls:
                    yield (EVENT_TOOL_CALL, {
                        "name": tc["name"],
                        "arguments": tc["arguments"],
                        "call_id": f"call_{tc['name']}_{id(tc)}",
                    })

            yield (EVENT_DONE, {
                "cognitive_state": None,
                "stats": {
                    "input_tokens": prompt_tokens,
                    "output_tokens": completion_tokens,
                },
                "model": self.model,
            })

        except Exception as e:
            yield (EVENT_ERROR, {"message": str(e)})


# ---------------------------------------------------------------------------
# CLI subprocess helper
# ---------------------------------------------------------------------------

def _find_cli(name: str) -> Optional[str]:
    """Find a CLI executable, preferring .cmd on Windows."""
    if sys.platform == "win32":
        cmd_path = shutil.which(f"{name}.cmd")
        if cmd_path:
            return cmd_path
    return shutil.which(name)


def _clean_env_for_cli(strip_prefixes: list = None) -> dict:
    """Create a clean env dict, stripping any vars that cause nesting detection."""
    strip_prefixes = strip_prefixes or []
    env = dict(os.environ)
    for prefix in strip_prefixes:
        for k in list(env.keys()):
            if k == prefix or k.startswith(prefix + "_"):
                env.pop(k)
    env["NO_COLOR"] = "1"
    return env


def _stream_subprocess(cmd: list, cwd: str = None, env: dict = None, cancel_event=None) -> Iterator[str]:
    """Spawn a subprocess and yield stdout lines as they arrive."""
    creation_flags = 0
    if sys.platform == "win32":
        creation_flags = subprocess.CREATE_NO_WINDOW

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        stdin=subprocess.DEVNULL,
        cwd=cwd or os.getcwd(),
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        creationflags=creation_flags,
        shell=(sys.platform == "win32"),  # Required for .cmd files on Windows
        env=env,
    )

    watcher = None
    if cancel_event is not None:
        import threading as _threading

        def _watch_cancel():
            cancel_event.wait()
            if proc.poll() is None:
                try:
                    proc.terminate()
                except Exception:
                    pass

        watcher = _threading.Thread(target=_watch_cancel, daemon=True)
        watcher.start()

    try:
        for line in proc.stdout:
            line = line.strip()
            if line:
                yield line
    except GeneratorExit:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        return
    finally:
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        stderr_out = proc.stderr.read() if proc.stderr else ""
        if stderr_out and proc.returncode != 0:
            logger.warning("CLI stderr (%s): %s", cmd[0], stderr_out[:500])


# ---------------------------------------------------------------------------
# Claude Code CLI Backend
# ---------------------------------------------------------------------------

class ClaudeCodeBackend:
    """Claude Code CLI backend — wraps `claude -p` with stream-json output.

    Uses the user's Claude Max subscription via the Claude Code CLI.
    The CLI manages its own tool execution loop internally.
    """

    # Model IDs sent to CLI → display labels
    MODELS = ["sonnet", "opus", "haiku"]
    MODEL_LABELS = {
        "sonnet": "Sonnet 4.6",
        "opus": "Opus 4.6",
        "haiku": "Haiku 4.5",
    }
    handles_tools = True  # CLI handles its own tool loop

    def __init__(self, model: str = "sonnet", cwd: str = None, permission_mode: str = "bypassPermissions"):
        self.model = model
        self.name = "claude-code"
        self.cwd = cwd or os.getcwd()
        self.permission_mode = permission_mode
        self._session_id = None
        self._cli = _find_cli("claude")

    def health(self) -> dict:
        if not self._cli:
            return {"status": "error", "message": "claude CLI not found"}
        try:
            result = subprocess.run(
                [self._cli, "--version"],
                capture_output=True, text=True, timeout=10,
                shell=(sys.platform == "win32"),
            )
            version = result.stdout.strip() or "unknown"
            return {"status": "ready", "backend": "claude-code", "version": version}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    def list_models(self) -> list:
        return list(self.MODELS)

    def classify(self, prompt: str, max_tokens: int = 50) -> str:
        return ""  # Skip classification for CLI backends

    def stream(self, user_msg: str, conversation_history: list, instructions: str,
               tools: list, max_tokens: int = 16384, cancel_event=None) -> Iterator[Tuple[str, dict]]:
        if not self._cli:
            yield (EVENT_ERROR, {"message": "claude CLI not found. Install: npm install -g @anthropic-ai/claude-code"})
            return

        logger.info("ClaudeCodeBackend.stream: model=%s cwd=%s prompt_len=%d prompt=%s...",
                     self.model, self.cwd, len(user_msg), user_msg[:80])

        # For long messages, write to temp file to avoid Windows cmd line length limits
        prompt_file = None
        if len(user_msg) > 2000 or '\n' in user_msg:
            import tempfile
            prompt_file = tempfile.NamedTemporaryFile(
                mode='w', suffix='.txt', prefix='resonant_prompt_',
                delete=False, encoding='utf-8',
            )
            prompt_file.write(user_msg)
            prompt_file.close()
            cmd = [
                self._cli, "-p", f"Follow the instructions in this file exactly: {prompt_file.name}",
                "--output-format", "stream-json",
                "--verbose",
                "--model", self.model,
                "--max-turns", "25",
            ]
        else:
            cmd = [
                self._cli, "-p", user_msg,
                "--output-format", "stream-json",
                "--verbose",  # Required for stream-json output
                "--model", self.model,
                "--max-turns", "25",
            ]

        if self.permission_mode:
            cmd.extend(["--permission-mode", self.permission_mode])

        if self._session_id:
            cmd.extend(["--resume", self._session_id])

        # Strip Claude Code env vars to avoid nesting detection
        env = _clean_env_for_cli(["CLAUDECODE", "CLAUDE_CODE", "CLAUDE_AGENT"])

        got_result = False
        self._streamed_text = False  # Track if we've streamed any text deltas
        try:
            for line in _stream_subprocess(cmd, cwd=self.cwd, env=env, cancel_event=cancel_event):
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue

                for evt in self._parse_claude_event(event):
                    if evt[0] == EVENT_DONE:
                        got_result = True
                    yield evt

            if not got_result:
                label = self.MODEL_LABELS.get(self.model, self.model)
                yield (EVENT_DONE, {
                    "stats": {"input_tokens": 0, "output_tokens": 0},
                    "model": f"Claude {label}",
                })

        except Exception as e:
            yield (EVENT_ERROR, {"message": f"Claude Code CLI error: {e}"})
        finally:
            # Clean up temp prompt file
            if prompt_file:
                try:
                    import os
                    os.unlink(prompt_file.name)
                except Exception:
                    pass

    def _parse_claude_event(self, event: dict) -> Iterator[Tuple[str, dict]]:
        """Parse a Claude Code stream-json event into backend events."""
        event_type = event.get("type", "")

        # --- System init (contains session_id) ---
        if event_type == "system":
            session_id = event.get("session_id", "")
            if session_id:
                self._session_id = session_id

        # --- Assistant message (complete message with content blocks) ---
        elif event_type == "assistant":
            message = event.get("message", {})
            for block in message.get("content", []):
                if block.get("type") == "tool_use":
                    yield (EVENT_TOOL_CALL, {
                        "name": block.get("name", ""),
                        "arguments": json.dumps(block.get("input", {})),
                        "call_id": block.get("id", ""),
                    })
                # Note: we skip text blocks from assistant events.
                # Intermediate text ("Let me read...") is noise for CLI
                # backends. The final response comes via the result event.

        # --- Content block delta (incremental streaming with --verbose) ---
        elif event_type == "content_block_delta":
            delta = event.get("delta", {})
            if delta.get("type") == "text_delta":
                text = delta.get("text", "")
                if text:
                    self._streamed_text = True
                    yield (EVENT_TEXT_DELTA, {"delta": text})

        # --- Tool result from CLI's internal tool execution ---
        elif event_type == "tool_result":
            # Display-only: the CLI already executed the tool
            pass

        # --- Result (final) ---
        elif event_type == "result":
            session_id = event.get("session_id", "")
            if session_id:
                self._session_id = session_id

            subtype = event.get("subtype", "")
            if subtype == "error":
                error_msg = event.get("error", event.get("result", "Unknown error"))
                yield (EVENT_ERROR, {"message": f"Claude Code: {error_msg}"})
                return

            # The result event contains the final text response
            # Only emit if we haven't already streamed deltas (avoid duplication)
            result_text = event.get("result", "")
            if result_text and not getattr(self, '_streamed_text', False):
                yield (EVENT_TEXT_DELTA, {"delta": result_text})

            cost = event.get("total_cost_usd", 0)
            duration = event.get("duration_ms", 0)
            num_turns = event.get("num_turns", 0)
            usage = event.get("usage", {})

            label = self.MODEL_LABELS.get(self.model, self.model)
            yield (EVENT_DONE, {
                "stats": {
                    "input_tokens": usage.get("input_tokens", 0) + usage.get("cache_read_input_tokens", 0),
                    "output_tokens": usage.get("output_tokens", 0),
                    "cost_usd": cost,
                    "duration_ms": duration,
                    "num_turns": num_turns,
                },
                "model": f"Claude {label}",
            })


# ---------------------------------------------------------------------------
# Codex CLI Backend
# ---------------------------------------------------------------------------

class CodexBackend:
    """Codex CLI backend — wraps `codex exec --json` for JSONL streaming.

    Uses the user's Codex/ChatGPT Pro subscription via the Codex CLI.
    The CLI manages its own tool execution loop internally.
    """

    MODELS = [
        "gpt-5.4", "gpt-5.4-mini",
        "gpt-5.3-codex", "gpt-5.2-codex",
        "gpt-5.2",
        "gpt-5.1-codex-max", "gpt-5.1-codex-mini",
        "o3", "o4-mini",
    ]
    MODEL_LABELS = {
        "gpt-5.4": "GPT-5.4",
        "gpt-5.4-mini": "GPT-5.4 Mini",
        "gpt-5.3-codex": "GPT-5.3 Codex",
        "gpt-5.2-codex": "GPT-5.2 Codex",
        "gpt-5.2": "GPT-5.2",
        "gpt-5.1-codex-max": "GPT-5.1 Codex Max",
        "gpt-5.1-codex-mini": "GPT-5.1 Codex Mini",
        "o3": "o3",
        "o4-mini": "o4-mini",
    }
    handles_tools = True  # CLI handles its own tool loop

    def __init__(self, model: str = None, cwd: str = None):
        self.model = model  # None = use CLI default from config
        self.name = "codex"
        self.cwd = cwd or os.getcwd()
        self._cli = _find_cli("codex")

    def health(self) -> dict:
        if not self._cli:
            return {"status": "error", "message": "codex CLI not found"}
        try:
            result = subprocess.run(
                [self._cli, "--version"],
                capture_output=True, text=True, timeout=10,
                shell=(sys.platform == "win32"),
            )
            version = result.stdout.strip() or "unknown"
            return {"status": "ready", "backend": "codex", "version": version}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    def list_models(self) -> list:
        return list(self.MODELS)

    def classify(self, prompt: str, max_tokens: int = 50) -> str:
        return ""  # Skip classification for CLI backends

    def stream(self, user_msg: str, conversation_history: list, instructions: str,
               tools: list, max_tokens: int = 16384, cancel_event=None) -> Iterator[Tuple[str, dict]]:
        if not self._cli:
            yield (EVENT_ERROR, {"message": "codex CLI not found. Install: npm install -g @openai/codex"})
            return

        # For long messages, write to temp file to avoid Windows cmd line length limits
        prompt_file = None
        actual_msg = user_msg
        if len(user_msg) > 2000 or '\n' in user_msg:
            import tempfile
            prompt_file = tempfile.NamedTemporaryFile(
                mode='w', suffix='.txt', prefix='resonant_codex_prompt_',
                delete=False, encoding='utf-8',
            )
            prompt_file.write(user_msg)
            prompt_file.close()
            actual_msg = f"Follow the instructions in this file exactly: {prompt_file.name}"

        cmd = [
            self._cli, "exec",
            "--json",
            "--full-auto",
            "--ephemeral",
            "--skip-git-repo-check",
            "-C", self.cwd,
            actual_msg,
        ]

        if self.model:
            cmd.insert(2, "-m")
            cmd.insert(3, self.model)

        input_tokens = 0
        output_tokens = 0
        # Buffer agent_message text — only emit the final one after the
        # CLI finishes (like Claude Code's result.result), so intermediate
        # "thinking" text doesn't flood the UI while tools are running.
        last_agent_text = ""

        try:
            for line in _stream_subprocess(cmd, cwd=self.cwd, cancel_event=cancel_event):
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue

                yield from self._parse_codex_event(event)

                # Capture agent_message text (don't yield it yet)
                if event.get("type") == "item.completed":
                    item = event.get("item", {})
                    if item.get("type") == "agent_message":
                        text = item.get("text", "")
                        if text:
                            last_agent_text = text

                # Track token usage from turn.completed
                if event.get("type") == "turn.completed":
                    usage = event.get("usage", {})
                    input_tokens += usage.get("input_tokens", 0)
                    output_tokens += usage.get("output_tokens", 0)

            # Emit the final agent response text
            if last_agent_text:
                yield (EVENT_TEXT_DELTA, {"delta": last_agent_text})

            yield (EVENT_DONE, {
                "stats": {
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                },
                "model": f"codex/{self.model or 'default'}",
            })

        except Exception as e:
            yield (EVENT_ERROR, {"message": f"Codex CLI error: {e}"})
        finally:
            if prompt_file:
                try:
                    import os
                    os.unlink(prompt_file.name)
                except Exception:
                    pass

    def _parse_codex_event(self, event: dict) -> Iterator[Tuple[str, dict]]:
        """Parse a Codex JSONL event into backend events."""
        event_type = event.get("type", "")

        # --- Item completed ---
        if event_type == "item.completed":
            item = event.get("item", {})
            item_type = item.get("type", "")

            if item_type == "agent_message":
                # Text is buffered in stream() and emitted at the end —
                # we only show the final response, not intermediate thinking.
                pass

            elif item_type == "command_execution":
                # A shell command was executed
                command = item.get("command", "")
                output = item.get("aggregated_output", "")
                exit_code = item.get("exit_code", 0)

                yield (EVENT_TOOL_CALL, {
                    "name": "bash",
                    "arguments": json.dumps({"command": command}),
                    "call_id": item.get("id", ""),
                })

            elif item_type == "file_edit":
                filepath = item.get("filepath", "")
                yield (EVENT_TOOL_CALL, {
                    "name": "file_edit",
                    "arguments": json.dumps({"path": filepath}),
                    "call_id": item.get("id", ""),
                })

            elif item_type == "file_read":
                filepath = item.get("filepath", "")
                yield (EVENT_TOOL_CALL, {
                    "name": "file_read",
                    "arguments": json.dumps({"path": filepath}),
                    "call_id": item.get("id", ""),
                })

        # --- Item started (in-progress notifications) ---
        # We intentionally skip item.started to avoid duplicate tool call
        # display — item.completed already contains the full result.

        # --- Errors ---
        elif event_type == "error":
            message = event.get("message", "Unknown error")
            yield (EVENT_ERROR, {"message": message})

        elif event_type == "turn.failed":
            error = event.get("error", {})
            message = error.get("message", "Turn failed")
            yield (EVENT_ERROR, {"message": message})


# ---------------------------------------------------------------------------
# Local MLX backend
# ---------------------------------------------------------------------------

class MLXBackend:
    """Local MLX backend backed by the LocalCodingModel repo."""

    MODELS = ["adapter-router", "real-targeted20", "gapround2-10", "fast-14b", "fast-7b"]
    MODEL_BASES = {
        "adapter-router": "mlx-community/Qwen3-Coder-30B-A3B-Instruct-8bit",
        "real-targeted20": "mlx-community/Qwen3-Coder-30B-A3B-Instruct-8bit",
        "gapround2-10": "mlx-community/Qwen3-Coder-30B-A3B-Instruct-8bit",
        "fast-14b": "mlx-community/Qwen2.5-Coder-14B-Instruct-8bit",
        "fast-7b": "mlx-community/Qwen2.5-Coder-7B-Instruct-4bit",
    }

    def __init__(self, local_root: str | None, model: str):
        self.local_root = Path(
            local_root
            or os.environ.get("LOCAL_CODING_MODEL_ROOT", "/Users/richbellantoni/Repos/LocalCodingModel")
        ).expanduser().resolve()
        self.model = model or "adapter-router"
        self.name = "mlx"
        self.tool_mode = "text"

    def _python(self) -> Path:
        return self.local_root / ".venv" / "bin" / "python"

    def _scripts_dir(self) -> Path:
        return self.local_root / "scripts"

    def _adapter_dir(self, key: str) -> Path | None:
        mapping = {
            "real-targeted20": self.local_root / "outputs" / "adapters" / "qwen3-coder-30b-a3b-real-targeted20",
            "gapround2-10": self.local_root / "outputs" / "adapters" / "qwen3-coder-30b-a3b-real-targeted20-gapround2-10",
        }
        return mapping.get(key)

    def _base_model(self) -> str:
        return self.MODEL_BASES.get(self.model, self.MODEL_BASES["adapter-router"])

    def _infer_task_type(self, prompt: str) -> str:
        prompt_lower = prompt.lower()
        if any(token in prompt_lower for token in ("summarize", "list the most likely files", "under 8 bullet", "under 7 bullets")):
            return "summary"
        return "coding"

    def _resolve_adapter(self, prompt: str) -> tuple[Path | None, str]:
        if self.model in {"fast-14b", "fast-7b"}:
            return None, f"fixed mlx base model '{self.model}' selected"
        if self.model != "adapter-router":
            adapter = self._adapter_dir(self.model)
            if adapter:
                return adapter, f"fixed mlx adapter '{self.model}' selected"
            return None, "mlx base model selected"

        scripts_dir = self._scripts_dir()
        if str(scripts_dir) not in sys.path:
            sys.path.insert(0, str(scripts_dir))
        try:
            from adapter_router import route_adapter  # type: ignore

            adapter, reason = route_adapter(
                prompt=prompt,
                task_type=self._infer_task_type(prompt),
            )
            return Path(adapter), reason
        except Exception as exc:
            logger.warning("MLX adapter router fallback triggered: %s", exc)
            return self._adapter_dir("real-targeted20"), "router unavailable, fell back to stable targeted20 adapter"

    @staticmethod
    def _render_content(content) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            text_parts = []
            image_count = 0
            for part in content:
                if not isinstance(part, dict):
                    continue
                if part.get("type") == "text":
                    text = part.get("text", "")
                    if isinstance(text, str) and text:
                        text_parts.append(text)
                elif part.get("type") == "image":
                    image_count += 1
            if image_count:
                text_parts.append(f"[{image_count} image attachment(s) omitted for text-only MLX backend]")
            return "\n".join(part for part in text_parts if part).strip()
        return str(content)

    def _build_prompt(
        self,
        *,
        user_msg: str,
        conversation_history: list,
        instructions: str,
        tools: list,
    ) -> str:
        sys_content = instructions
        if tools:
            sys_content += build_tool_system_prompt(tools)

        lines = [f"System:\n{sys_content.strip()}\n", "Conversation:"]
        for turn in conversation_history:
            role = turn.get("role", "")
            content = self._render_content(turn.get("content", ""))
            if role == "tool_call":
                args = turn.get("arguments", "{}")
                if isinstance(args, dict):
                    args = json.dumps(args)
                content = f'<tool_call>\n{{"name": "{turn.get("name", "")}", "arguments": {args}}}\n</tool_call>'
                lines.append(f"Assistant:\n{content}")
            elif role == "tool_result":
                prefix = turn.get("name", "tool")
                lines.append(f"User:\n[{prefix} result]\n{content}")
            elif role == "assistant":
                lines.append(f"Assistant:\n{content}")
            elif role == "user":
                lines.append(f"User:\n{content}")

        if not conversation_history or conversation_history[-1].get("role") != "user":
            lines.append(f"User:\n{user_msg}")
        lines.append("Assistant:")
        return "\n\n".join(line for line in lines if line.strip())

    def _run_generate(
        self,
        *,
        prompt: str,
        max_tokens: int,
        adapter_dir: Path | None,
        cancel_event=None,
    ) -> str:
        command = [
            str(self._python()),
            "-m",
            "mlx_lm",
            "generate",
            "--model",
            self._base_model(),
            "--prompt",
            prompt,
            "--max-tokens",
            str(max_tokens),
            "--temp",
            "0.0",
            "--verbose",
            "F",
        ]
        if adapter_dir:
            command.extend(["--adapter-path", str(adapter_dir)])

        proc = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        while proc.poll() is None:
            if cancel_event is not None and cancel_event.is_set():
                proc.kill()
                return ""
            time.sleep(0.1)

        stdout, stderr = proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(stderr.strip() or f"mlx generate failed with exit code {proc.returncode}")
        return stdout.strip()

    def health(self) -> dict:
        python_path = self._python()
        adapter_root = self.local_root / "outputs" / "adapters"
        requires_adapters = self.model in {"adapter-router", "real-targeted20", "gapround2-10"}
        ready = python_path.exists() and (adapter_root.exists() if requires_adapters else True)
        return {
            "status": "ready" if ready else "error",
            "backend": "mlx",
            "model": self.model,
            "local_root": str(self.local_root),
            "available_models": self.MODELS,
            "message": "" if ready else f"Missing LocalCodingModel assets under {self.local_root}",
        }

    def warm_up(self):
        try:
            self.classify("Reply with exactly one word: READY", max_tokens=4)
        except Exception:
            pass

    def list_models(self) -> list:
        return list(self.MODELS)

    def classify(self, prompt: str, max_tokens: int = 50) -> str:
        adapter_dir, _ = self._resolve_adapter(prompt)
        return self._run_generate(
            prompt=prompt,
            max_tokens=max_tokens,
            adapter_dir=adapter_dir,
        ).strip()

    def stream(
        self,
        user_msg: str,
        conversation_history: list,
        instructions: str,
        tools: list,
        max_tokens: int = 4096,
        cancel_event=None,
    ) -> Iterator[Tuple[str, dict]]:
        try:
            adapter_dir, reason = self._resolve_adapter(user_msg)
            prompt = self._build_prompt(
                user_msg=user_msg,
                conversation_history=conversation_history,
                instructions=instructions,
                tools=tools,
            )
            full_text = self._run_generate(
                prompt=prompt,
                max_tokens=max_tokens,
                adapter_dir=adapter_dir,
                cancel_event=cancel_event,
            )
            clean_text = full_text.strip()
            plain_text, xml_calls = parse_tool_calls(clean_text)
            detected_calls = []
            if xml_calls:
                for tc in xml_calls:
                    args = tc.get("arguments", "{}")
                    args_str = json.dumps(args) if isinstance(args, dict) else str(args)
                    call_id = f"call_{hash(tc.get('name', '') + args_str) & 0xFFFFFFFF:08x}"
                    detected_calls.append({
                        "name": tc.get("name", ""),
                        "arguments": args_str,
                        "call_id": call_id,
                    })
            if not detected_calls:
                detected_calls = _detect_json_tool_calls(clean_text)
            if not detected_calls:
                detected_calls = _detect_text_tool_calls(clean_text)

            if detected_calls:
                if plain_text.strip():
                    yield (EVENT_TEXT_DELTA, {"delta": plain_text.strip()})
                for tc in detected_calls:
                    yield (EVENT_TOOL_CALL, tc)
            elif clean_text:
                yield (EVENT_TEXT_DELTA, {"delta": clean_text})

            yield (EVENT_DONE, {
                "cognitive_state": None,
                "stats": {"routing_reason": reason},
                "model": self.model,
            })
        except Exception as e:
            yield (EVENT_ERROR, {"message": str(e)})


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def create_backend(backend_type: str, url: str = None, model: str = None,
                   api_key: str = None, base_url: str = None, cwd: str = None,
                   permission_mode: str = None, local_root: str = None):
    if backend_type == "ollama":
        if not model:
            raise ValueError("Model name required for Ollama backend")
        return OllamaBackend(url, model)
    elif backend_type == "resonant":
        return ResonantBackend(url)
    elif backend_type == "claude":
        if not api_key:
            raise ValueError("API key required for Claude backend")
        return ClaudeBackend(api_key, model=model or "claude-sonnet-4-20250514")
    elif backend_type in ("openai", "lmstudio"):
        if not api_key:
            api_key = "lm-studio"  # LM Studio doesn't need a real key
        return OpenAIBackend(api_key, model=model or "gpt-4o", base_url=base_url)
    elif backend_type == "claude-code":
        return ClaudeCodeBackend(
            model=model or "sonnet",
            cwd=cwd,
            permission_mode=permission_mode or "bypassPermissions",
        )
    elif backend_type == "codex":
        return CodexBackend(model=model, cwd=cwd)
    elif backend_type == "mlx":
        return MLXBackend(local_root=local_root, model=model or "adapter-router")
    else:
        raise ValueError(f"Unknown backend: {backend_type}")
