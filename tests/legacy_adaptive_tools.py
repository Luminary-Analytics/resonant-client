"""
Comprehensive adversarial tests for the adaptive tool calling system.

Tests cover:
  - protocol.py: build_tool_system_prompt, parse_tool_calls, _try_parse_tool_json
  - backends.py: OllamaBackend._detect_tool_support, stream edge cases,
                 _detect_json_tool_calls, _detect_text_tool_calls, cache behavior
"""

import json
import re
import sys
import threading
import unittest
from unittest.mock import patch, MagicMock

# Add project root to path
sys.path.insert(0, "D:/Repos/resonant-client")

from resonant_client.protocol import (
    build_tool_system_prompt,
    parse_tool_calls,
    strip_think_tags,
    _try_parse_tool_json,
    _tool_prompt_cache,
)
from resonant_client.backends import (
    OllamaBackend,
    _detect_json_tool_calls,
    _detect_text_tool_calls,
    _build_simple_tool_call,
    _convert_tools_for_ollama,
)


def make_tool(name, desc="A tool", params=None):
    """Helper to create a tool definition in OpenAI format."""
    if params is None:
        params = {
            "type": "object",
            "properties": {"arg1": {"type": "string"}},
            "required": ["arg1"],
        }
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": desc,
            "parameters": params,
        },
    }


# ========================================================================
# 1. Model name edge cases
# ========================================================================
class TestModelNameEdgeCases(unittest.TestCase):

    def setUp(self):
        # Clear the class-level cache before each test
        OllamaBackend._tool_support_cache.clear()

    def test_model_with_tag_suffix(self):
        """Models like 'llama3.1:70b-instruct-q4_K_M' should match 'llama3.1'."""
        backend = OllamaBackend("http://10.0.0.133:11434", "llama3.1:70b-instruct-q4_K_M")
        # _detect_tool_support strips ":..." and checks known lists
        # Mock the network calls to avoid actual HTTP
        result = backend._detect_tool_support()
        self.assertTrue(result, "llama3.1:70b-instruct-q4_K_M should be recognized as tool-supporting")

    def test_model_with_latest_tag(self):
        backend = OllamaBackend("http://10.0.0.133:11434", "mistral:latest")
        result = backend._detect_tool_support()
        self.assertTrue(result)

    def test_codellama_no_tools(self):
        backend = OllamaBackend("http://10.0.0.133:11434", "codellama:7b")
        result = backend._detect_tool_support()
        self.assertFalse(result, "codellama should NOT support native tools")

    def test_model_with_dots_phi4(self):
        """phi4 is in known list — 'phi-4.0' is NOT (different base name)."""
        backend = OllamaBackend("http://10.0.0.133:11434", "phi4:latest")
        result = backend._detect_tool_support()
        self.assertTrue(result)

    def test_unknown_model_falls_to_probe(self):
        """A model not in any known list should probe Ollama."""
        backend = OllamaBackend("http://10.0.0.133:11434", "my-custom-model:latest")
        # Mock the /api/show endpoint to return a template with {{.Tools}}
        with patch("httpx.post") as mock_post:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = {"template": "{{.System}}\n{{.Tools}}\n{{.Prompt}}"}
            mock_post.return_value = mock_resp
            result = backend._detect_tool_support()
            self.assertTrue(result, "Model with {{.Tools}} in template should be detected")

    def test_unknown_model_no_tools_in_template(self):
        """Unknown model with no {{.Tools}} in template."""
        backend = OllamaBackend("http://10.0.0.133:11434", "some-random-model:v1")
        with patch("httpx.post") as mock_post:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = {"template": "{{.System}}\n{{.Prompt}}"}
            mock_post.return_value = mock_resp
            result = backend._detect_tool_support()
            self.assertFalse(result)

    def test_empty_model_name(self):
        """Empty string model name should not crash."""
        backend = OllamaBackend("http://10.0.0.133:11434", "")
        # base_model would be "" — not in any list, so it falls through to API calls
        with patch("httpx.post") as mock_post:
            mock_post.side_effect = Exception("connection refused")
            # Should default to True without crashing
            result = backend._detect_tool_support()
            self.assertTrue(result, "Default should be True on failure")

    def test_case_sensitivity(self):
        """Model names should be lowered for comparison."""
        backend = OllamaBackend("http://10.0.0.133:11434", "Llama3.1:7B")
        result = backend._detect_tool_support()
        self.assertTrue(result, "Case-insensitive match should work via .lower()")

    def test_model_with_multiple_colons(self):
        """Edge case: model name like 'registry.example.com/llama3.1:latest'."""
        backend = OllamaBackend("http://10.0.0.133:11434", "registry.example.com/llama3.1:latest")
        # split(":")[0] gives "registry.example.com/llama3.1" — won't match known lists
        with patch("httpx.post") as mock_post:
            mock_post.side_effect = Exception("timeout")
            result = backend._detect_tool_support()
            # Falls through to default True
            self.assertTrue(result)


# ========================================================================
# 2. Detection cache tests
# ========================================================================
class TestDetectionCache(unittest.TestCase):

    def setUp(self):
        OllamaBackend._tool_support_cache.clear()

    def test_cache_hit(self):
        """Second call should use cache, not make network request."""
        OllamaBackend._tool_support_cache["test-model"] = False
        backend = OllamaBackend("http://10.0.0.133:11434", "test-model")
        with patch("httpx.post") as mock_post:
            result = backend._detect_tool_support()
            mock_post.assert_not_called()
            self.assertFalse(result)

    def test_race_condition_threads(self):
        """Two threads detecting simultaneously should not corrupt cache."""
        results = {}
        errors = []

        def detect(thread_id, model):
            try:
                backend = OllamaBackend("http://10.0.0.133:11434", model)
                with patch("httpx.post") as mock_post:
                    mock_resp = MagicMock()
                    mock_resp.status_code = 200
                    mock_resp.json.return_value = {"template": "{{.Tools}}"}
                    mock_post.return_value = mock_resp
                    result = backend._detect_tool_support()
                    results[thread_id] = result
            except Exception as e:
                errors.append((thread_id, e))

        t1 = threading.Thread(target=detect, args=(1, "race-model"))
        t2 = threading.Thread(target=detect, args=(2, "race-model"))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        self.assertEqual(len(errors), 0, f"Thread errors: {errors}")
        # Both should get the same result
        self.assertEqual(results.get(1), results.get(2), "Race condition: different results")


# ========================================================================
# 3. Tool support cache as class variable (shared across instances)
# ========================================================================
class TestCacheSharing(unittest.TestCase):

    def setUp(self):
        OllamaBackend._tool_support_cache.clear()

    def test_cache_shared_across_instances_same_model(self):
        """BUG CHECK: Two instances with same model name but different URLs share cache."""
        OllamaBackend._tool_support_cache["shared-model"] = True
        b1 = OllamaBackend("http://server1:11434", "shared-model")
        b2 = OllamaBackend("http://server2:11434", "shared-model")

        # Both get the cached value — this IS a bug if servers have different capabilities
        r1 = b1._detect_tool_support()
        r2 = b2._detect_tool_support()
        self.assertEqual(r1, r2, "Cache is keyed by model only, not (model, url)")
        # Document this as a known design issue
        print("  [INFO] Cache is keyed by model name only — different servers with same model share cache")

    def test_different_models_different_cache(self):
        """Different model names should have independent cache entries."""
        OllamaBackend._tool_support_cache["model-a"] = True
        OllamaBackend._tool_support_cache["model-b"] = False
        ba = OllamaBackend("http://10.0.0.133:11434", "model-a")
        bb = OllamaBackend("http://10.0.0.133:11434", "model-b")
        self.assertTrue(ba._detect_tool_support())
        self.assertFalse(bb._detect_tool_support())


# ========================================================================
# 4. Text mode message formatting
# ========================================================================
class TestTextModeFormatting(unittest.TestCase):
    """Test the message construction in stream() for text-based tool mode."""

    def setUp(self):
        OllamaBackend._tool_support_cache.clear()

    def _build_messages(self, history, instructions="You are helpful.", tools=None, use_native=False):
        """Helper: builds the messages array like stream() does, without making HTTP calls."""
        if tools is None:
            tools = [make_tool("bash", "Run a command")]

        messages = []
        sys_content = instructions
        if not use_native and tools:
            sys_content += build_tool_system_prompt(tools)
        messages.append({"role": "system", "content": sys_content})

        for turn in history:
            role = turn["role"]
            content = turn["content"]
            if role == "tool_call":
                if use_native:
                    args = turn.get("arguments", "{}")
                    if isinstance(args, str):
                        try:
                            args = json.loads(args)
                        except json.JSONDecodeError:
                            args = {}
                    messages.append({
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [{"function": {"name": turn.get("name", ""), "arguments": args}}],
                    })
                else:
                    args = turn.get("arguments", "{}")
                    if isinstance(args, dict):
                        args = json.dumps(args)
                    messages.append({
                        "role": "assistant",
                        "content": f'<tool_call>\n{{"name": "{turn.get("name", "")}", "arguments": {args}}}\n</tool_call>',
                    })
            elif role == "tool_result":
                if use_native:
                    messages.append({"role": "tool", "content": content})
                else:
                    tool_name = turn.get("name", "tool")
                    messages.append({"role": "user", "content": f"[{tool_name} result]\n{content}"})
            elif role in ("user", "assistant"):
                if isinstance(content, list):
                    text_parts = []
                    images = []
                    for part in content:
                        if part.get("type") == "image":
                            images.append(part.get("data", ""))
                        elif part.get("type") == "text":
                            text_parts.append(part.get("text", ""))
                    msg = {"role": role, "content": " ".join(text_parts)}
                    if images:
                        msg["images"] = images
                    messages.append(msg)
                else:
                    messages.append({"role": role, "content": content})

        return messages

    def test_many_turns(self):
        """10+ turn conversation should format correctly."""
        history = []
        for i in range(12):
            history.append({"role": "user", "content": f"User turn {i}"})
            history.append({"role": "assistant", "content": f"Assistant turn {i}"})

        msgs = self._build_messages(history)
        # 1 system + 24 conversation = 25
        self.assertEqual(len(msgs), 25)
        self.assertEqual(msgs[0]["role"], "system")
        self.assertEqual(msgs[1]["role"], "user")
        self.assertEqual(msgs[1]["content"], "User turn 0")

    def test_nested_json_in_tool_args(self):
        """Tool call with JSON content inside arguments (e.g., file_write with JSON)."""
        nested_json = json.dumps({"key": "value", "nested": {"a": 1}})
        args = json.dumps({"path": "/tmp/test.json", "content": nested_json})
        history = [
            {"role": "tool_call", "content": "", "name": "file_write", "arguments": args},
            {"role": "tool_result", "content": "File written", "name": "file_write"},
        ]
        msgs = self._build_messages(history, use_native=False)
        # Should not crash; assistant message should contain valid-looking XML
        assistant_msg = msgs[1]
        self.assertEqual(assistant_msg["role"], "assistant")
        self.assertIn("file_write", assistant_msg["content"])

    def test_special_chars_in_tool_results(self):
        """Backslashes, quotes, newlines, unicode in tool results."""
        special_content = 'Line1\nLine2\r\n"quoted"\t\ttabbed\\path\\\\ unicode: \u2603\u2764'
        history = [
            {"role": "tool_call", "content": "", "name": "bash", "arguments": '{"command": "echo test"}'},
            {"role": "tool_result", "content": special_content, "name": "bash"},
        ]
        msgs = self._build_messages(history, use_native=False)
        result_msg = msgs[2]  # system, assistant(tool_call), user(tool_result)
        self.assertIn(special_content, result_msg["content"])

    def test_multimodal_content(self):
        """History with image content."""
        history = [
            {"role": "user", "content": [
                {"type": "text", "text": "What is this?"},
                {"type": "image", "data": "base64encodeddata=="},
            ]},
            {"role": "assistant", "content": "It's a cat."},
        ]
        msgs = self._build_messages(history)
        self.assertEqual(msgs[1]["content"], "What is this?")
        self.assertEqual(msgs[1]["images"], ["base64encodeddata=="])
        self.assertEqual(msgs[2]["content"], "It's a cat.")

    def test_empty_history(self):
        """Empty conversation history."""
        msgs = self._build_messages([])
        self.assertEqual(len(msgs), 1)  # Just system
        self.assertEqual(msgs[0]["role"], "system")

    def test_only_user_messages(self):
        """History with only user messages (no tool calls)."""
        history = [
            {"role": "user", "content": "Hello"},
            {"role": "user", "content": "Another message"},
        ]
        msgs = self._build_messages(history)
        self.assertEqual(len(msgs), 3)  # system + 2 user
        for m in msgs[1:]:
            self.assertEqual(m["role"], "user")

    def test_tool_call_with_dict_arguments(self):
        """Arguments passed as dict (not string) in text mode."""
        history = [
            {"role": "tool_call", "content": "", "name": "bash",
             "arguments": {"command": "ls -la"}},  # dict, not string
        ]
        msgs = self._build_messages(history, use_native=False)
        assistant_msg = msgs[1]
        self.assertIn('"command": "ls -la"', assistant_msg["content"])

    def test_tool_call_with_invalid_json_args_native(self):
        """Native mode: malformed args string should not crash."""
        history = [
            {"role": "tool_call", "content": "", "name": "bash",
             "arguments": "not valid json {{{"},
        ]
        msgs = self._build_messages(history, use_native=True)
        # Should gracefully handle with empty args dict
        self.assertEqual(msgs[1]["tool_calls"][0]["function"]["arguments"], {})


# ========================================================================
# 5. Text mode response parsing (parse_tool_calls)
# ========================================================================
class TestParseToolCalls(unittest.TestCase):

    def test_multiple_tool_calls(self):
        """Multiple <tool_call> blocks in one response."""
        text = '''I'll do two things:
<tool_call>
{"name": "bash", "arguments": {"command": "ls"}}
</tool_call>
Then:
<tool_call>
{"name": "file_read", "arguments": {"path": "/tmp/test.txt"}}
</tool_call>'''
        plain, calls = parse_tool_calls(text)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0]["name"], "bash")
        self.assertEqual(calls[1]["name"], "file_read")

    def test_malformed_incomplete_tag(self):
        """<tool_call> with no closing tag — fallback pattern."""
        text = '<tool_call>\n{"name": "bash", "arguments": {"command": "pwd"}}'
        plain, calls = parse_tool_calls(text)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["name"], "bash")

    def test_think_tags_plus_tool_calls(self):
        """<think> block followed by tool call."""
        text = '''<think>
I need to check the directory structure.
</think>
<tool_call>
{"name": "bash", "arguments": {"command": "ls -la"}}
</tool_call>'''
        plain, calls = parse_tool_calls(text)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["name"], "bash")
        self.assertNotIn("think", plain.lower())

    def test_no_tool_calls_just_text(self):
        """Plain text with no tool calls."""
        text = "Here's how you can solve this problem:\n1. First do X\n2. Then do Y"
        plain, calls = parse_tool_calls(text)
        self.assertEqual(len(calls), 0)
        self.assertEqual(plain, text)

    def test_whitespace_only(self):
        """Just whitespace."""
        plain, calls = parse_tool_calls("   \n\t  \n  ")
        self.assertEqual(len(calls), 0)

    def test_xml_like_not_tool_call(self):
        """XML-like content that is NOT a tool call."""
        text = "<div>Hello</div>\n<span class='tool_call'>Not a real tool</span>"
        plain, calls = parse_tool_calls(text)
        self.assertEqual(len(calls), 0)

    def test_extremely_long_arguments(self):
        """Tool call with 10KB+ arguments."""
        long_content = "A" * 12000
        text = f'<tool_call>\n{{"name": "file_write", "arguments": {{"path": "/tmp/big.txt", "content": "{long_content}"}}}}\n</tool_call>'
        plain, calls = parse_tool_calls(text)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["name"], "file_write")
        args = json.loads(calls[0]["arguments"])
        self.assertEqual(len(args["content"]), 12000)

    def test_nested_braces_in_arguments(self):
        """Arguments containing nested JSON objects."""
        text = '<tool_call>\n{"name": "file_write", "arguments": {"path": "/tmp/test.json", "content": "{\\"key\\": {\\"nested\\": true}}"}}\n</tool_call>'
        plain, calls = parse_tool_calls(text)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["name"], "file_write")

    def test_windows_paths(self):
        """Windows paths with backslashes like D:\\Repos\\test.py."""
        text = '<tool_call>\n{"name": "file_read", "arguments": {"path": "D:\\Repos\\test.py"}}\n</tool_call>'
        plain, calls = parse_tool_calls(text)
        self.assertEqual(len(calls), 1)
        # The _try_parse_tool_json should fix backslashes
        args = json.loads(calls[0]["arguments"])
        self.assertIn("Repos", args["path"])

    def test_code_snippet_with_tool_call_text(self):
        """Code snippet that mentions tool_call but isn't one."""
        text = '''Here's an example of how the protocol works:
```python
text = '<tool_call>{"name": "test"}</tool_call>'
parse_tool_calls(text)
```
That's how you parse it.'''
        plain, calls = parse_tool_calls(text)
        # This will likely parse the code snippet as a tool call — document behavior
        # The regex doesn't distinguish code blocks from actual tool calls
        if calls:
            print(f"  [INFO] Code snippets with <tool_call> ARE parsed as tool calls ({len(calls)} found)")
        else:
            print("  [INFO] Code snippets with <tool_call> are correctly ignored")

    def test_empty_string(self):
        plain, calls = parse_tool_calls("")
        self.assertEqual(len(calls), 0)
        self.assertEqual(plain, "")

    def test_missing_arguments_key(self):
        """Tool call JSON missing 'arguments' key."""
        text = '<tool_call>\n{"name": "bash"}\n</tool_call>'
        plain, calls = parse_tool_calls(text)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["name"], "bash")
        args = json.loads(calls[0]["arguments"])
        self.assertEqual(args, {})

    def test_glm_style_no_closing_tag(self):
        """GLM-4 style: <tool_call> with JSON but no closing tag."""
        text = 'Let me check.\n<tool_call>\n{"name": "bash", "arguments": {"command": "whoami"}}'
        plain, calls = parse_tool_calls(text)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["name"], "bash")

    def test_arguments_with_literal_newlines(self):
        """Arguments with literal newlines in strings (common LLM mistake)."""
        # This is raw text — the JSON has actual newlines inside a string value
        text = '<tool_call>\n{"name": "file_write", "arguments": {"path": "/tmp/test.py", "content": "line1\nline2\nline3"}}\n</tool_call>'
        plain, calls = parse_tool_calls(text)
        self.assertEqual(len(calls), 1, "Should handle literal newlines in JSON strings")


# ========================================================================
# 5b. _try_parse_tool_json edge cases
# ========================================================================
class TestTryParseToolJson(unittest.TestCase):

    def test_valid_json(self):
        result = _try_parse_tool_json('{"name": "bash", "arguments": {"command": "ls"}}')
        self.assertIsNotNone(result)
        self.assertEqual(result["name"], "bash")

    def test_windows_backslashes(self):
        raw = '{"name": "file_read", "arguments": {"path": "D:\\Repos\\test.py"}}'
        result = _try_parse_tool_json(raw)
        self.assertIsNotNone(result)
        self.assertIn("Repos", result["arguments"]["path"])

    def test_literal_newlines_in_string(self):
        raw = '{"name": "file_write", "arguments": {"path": "/tmp/t.py", "content": "a\nb"}}'
        result = _try_parse_tool_json(raw)
        self.assertIsNotNone(result)

    def test_total_garbage(self):
        result = _try_parse_tool_json("not json at all {{{")
        self.assertIsNone(result)

    def test_name_extractable_but_args_malformed(self):
        """Name is parseable but arguments JSON is broken — should still return something."""
        raw = '{"name": "bash", "arguments": {"command": broken}}'
        result = _try_parse_tool_json(raw)
        # Fix 3 should extract name and use _raw fallback for args
        self.assertIsNotNone(result)
        self.assertEqual(result["name"], "bash")

    def test_double_escaped_backslashes(self):
        """Already-escaped backslashes should not be double-escaped."""
        raw = '{"name": "file_read", "arguments": {"path": "D:\\\\Repos\\\\test.py"}}'
        result = _try_parse_tool_json(raw)
        self.assertIsNotNone(result)
        # Path should have single backslashes after JSON parsing
        self.assertEqual(result["arguments"]["path"], "D:\\Repos\\test.py")


# ========================================================================
# 5c. strip_think_tags
# ========================================================================
class TestStripThinkTags(unittest.TestCase):

    def test_basic(self):
        self.assertEqual(strip_think_tags("<think>reasoning</think>answer"), "answer")

    def test_multiline_think(self):
        text = "<think>\nline1\nline2\n</think>\nresult"
        self.assertEqual(strip_think_tags(text), "result")

    def test_no_think_tags(self):
        self.assertEqual(strip_think_tags("just text"), "just text")

    def test_nested_think_tags(self):
        """Nested think tags — regex is non-greedy so this may leave artifacts."""
        text = "<think>outer<think>inner</think>still outer</think>result"
        result = strip_think_tags(text)
        # Non-greedy: first </think> closes first <think>
        self.assertIn("result", result)


# ========================================================================
# 6. Stream method edge cases (mock HTTP)
# ========================================================================
class TestStreamEdgeCases(unittest.TestCase):

    def setUp(self):
        OllamaBackend._tool_support_cache.clear()
        # Clear tool prompt cache too
        _tool_prompt_cache.clear()

    def _mock_stream_response(self, chunks):
        """Create a mock httpx streaming response yielding the given JSON objects as lines."""
        lines = []
        for chunk in chunks:
            lines.append(json.dumps(chunk).encode("utf-8") + b"\n")

        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.iter_raw = MagicMock(return_value=iter(lines))
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        return mock_resp

    def test_empty_tools_list(self):
        """tools=[] should not trigger tool detection."""
        backend = OllamaBackend("http://10.0.0.133:11434", "llama3.1:7b")
        backend._use_native_tools = True  # Pre-set to avoid detection

        chunks = [
            {"message": {"content": "Hello"}, "done": False},
            {"message": {"content": " world"}, "done": False},
            {"done": True, "total_duration": 1000},
        ]

        with patch("httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.stream = MagicMock(return_value=self._mock_stream_response(chunks))
            mock_client_cls.return_value = mock_client

            events = list(backend.stream("Hi", [], "Be helpful", tools=[]))

        # Should get text deltas and done
        text_events = [e for e in events if e[0] == "text.delta"]
        self.assertGreater(len(text_events), 0)

    def test_tools_none(self):
        """tools=None should work without errors."""
        backend = OllamaBackend("http://10.0.0.133:11434", "llama3.1:7b")
        backend._use_native_tools = True

        chunks = [
            {"message": {"content": "Hi"}, "done": False},
            {"done": True},
        ]

        with patch("httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.stream = MagicMock(return_value=self._mock_stream_response(chunks))
            mock_client_cls.return_value = mock_client

            events = list(backend.stream("Hi", [], "Be helpful", tools=None))

        done_events = [e for e in events if e[0] == "done"]
        self.assertEqual(len(done_events), 1)

    def test_native_tool_call_response(self):
        """Native tool call from Ollama API."""
        backend = OllamaBackend("http://10.0.0.133:11434", "llama3.1:7b")
        backend._use_native_tools = True

        chunks = [
            {"message": {"tool_calls": [{"function": {"name": "bash", "arguments": {"command": "ls"}}}]}, "done": False},
            {"done": True, "total_duration": 500},
        ]

        with patch("httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.stream = MagicMock(return_value=self._mock_stream_response(chunks))
            mock_client_cls.return_value = mock_client

            events = list(backend.stream("List files", [], "Be helpful", tools=[make_tool("bash")]))

        tool_events = [e for e in events if e[0] == "tool_call"]
        self.assertEqual(len(tool_events), 1)
        self.assertEqual(tool_events[0][1]["name"], "bash")

    def test_text_mode_tool_call_response(self):
        """Text mode: model outputs <tool_call> XML."""
        backend = OllamaBackend("http://10.0.0.133:11434", "codellama:7b")
        backend._use_native_tools = False

        tool_text = '<tool_call>\n{"name": "bash", "arguments": {"command": "ls"}}\n</tool_call>'
        chunks = []
        for ch in tool_text:
            chunks.append({"message": {"content": ch}, "done": False})
        chunks.append({"done": True})

        with patch("httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.stream = MagicMock(return_value=self._mock_stream_response(chunks))
            mock_client_cls.return_value = mock_client

            events = list(backend.stream("List files", [], "Be helpful", tools=[make_tool("bash")]))

        tool_events = [e for e in events if e[0] == "tool_call"]
        self.assertEqual(len(tool_events), 1, f"Expected 1 tool call, got {len(tool_events)}. Events: {events}")
        self.assertEqual(tool_events[0][1]["name"], "bash")

    def test_connection_error(self):
        """HTTP connection error should yield error event."""
        backend = OllamaBackend("http://10.0.0.133:11434", "llama3.1:7b")
        backend._use_native_tools = True

        with patch("httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.stream = MagicMock(side_effect=Exception("Connection refused"))
            mock_client_cls.return_value = mock_client

            events = list(backend.stream("Hi", [], "Be helpful", tools=[]))

        error_events = [e for e in events if e[0] == "error"]
        self.assertEqual(len(error_events), 1)

    def test_empty_user_msg(self):
        """Empty user message should still work."""
        backend = OllamaBackend("http://10.0.0.133:11434", "llama3.1:7b")
        backend._use_native_tools = True

        chunks = [
            {"message": {"content": "How can I help?"}, "done": False},
            {"done": True},
        ]

        with patch("httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.stream = MagicMock(return_value=self._mock_stream_response(chunks))
            mock_client_cls.return_value = mock_client

            events = list(backend.stream("", [], "Be helpful", tools=[]))

        # Should not crash
        done_events = [e for e in events if e[0] == "done"]
        self.assertEqual(len(done_events), 1)


# ========================================================================
# 7. Protocol edge cases
# ========================================================================
class TestBuildToolSystemPrompt(unittest.TestCase):

    def setUp(self):
        _tool_prompt_cache.clear()

    def test_zero_tools(self):
        result = build_tool_system_prompt([])
        self.assertEqual(result, "")

    def test_one_tool(self):
        result = build_tool_system_prompt([make_tool("bash", "Run a shell command")])
        self.assertIn("bash", result)
        self.assertIn("tool_call", result)

    def test_fifty_tools(self):
        tools = [make_tool(f"tool_{i}", f"Tool number {i}") for i in range(50)]
        result = build_tool_system_prompt(tools)
        self.assertIn("tool_49", result)
        self.assertIn("tool_0", result)

    def test_cache_different_tools_same_count(self):
        """Different tool sets with same count should produce different prompts (cache key is names, not count)."""
        _tool_prompt_cache.clear()
        tools_a = [make_tool("alpha", "Alpha tool")]
        tools_b = [make_tool("beta", "Beta tool")]

        result_a = build_tool_system_prompt(tools_a)
        result_b = build_tool_system_prompt(tools_b)

        self.assertNotEqual(result_a, result_b, "Different tool sets should produce different prompts")
        self.assertIn("alpha", result_a)
        self.assertIn("beta", result_b)

    def test_cache_same_tools_hits_cache(self):
        """Same tool set should return cached result."""
        _tool_prompt_cache.clear()
        tools = [make_tool("bash", "Run a command")]
        result_a = build_tool_system_prompt(tools)
        result_b = build_tool_system_prompt(tools)
        self.assertEqual(result_a, result_b)
        # Should be the exact same object from cache
        self.assertIs(result_a, result_b)

    def test_bare_function_dict(self):
        """Tool passed as bare function dict (no type=function wrapper)."""
        tool = {
            "name": "test",
            "description": "A test tool",
            "parameters": {"type": "object", "properties": {"x": {"type": "string"}}, "required": ["x"]},
        }
        result = build_tool_system_prompt([tool])
        self.assertIn("test", result)

    def test_non_dict_tool_skipped(self):
        """Non-dict items should be skipped."""
        tools = [make_tool("real"), "not a dict", 42, None]
        result = build_tool_system_prompt(tools)
        self.assertIn("real", result)


# ========================================================================
# 7b. _detect_json_tool_calls and _detect_text_tool_calls
# ========================================================================
class TestDetectJsonToolCalls(unittest.TestCase):

    def test_basic_json_tool_call(self):
        text = '{"name": "bash", "arguments": {"command": "ls -la"}}'
        calls = _detect_json_tool_calls(text)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["name"], "bash")

    def test_json_in_prose(self):
        text = 'I will run this: {"name": "bash", "arguments": {"command": "pwd"}} for you.'
        calls = _detect_json_tool_calls(text)
        self.assertEqual(len(calls), 1)

    def test_no_json(self):
        calls = _detect_json_tool_calls("Just plain text")
        self.assertEqual(len(calls), 0)

    def test_json_missing_name(self):
        text = '{"arguments": {"command": "ls"}}'
        calls = _detect_json_tool_calls(text)
        self.assertEqual(len(calls), 0)  # No "name" key in the right pattern

    def test_nested_braces(self):
        text = '{"name": "file_write", "arguments": {"path": "/tmp/a.json", "content": "{\\\"k\\\": {\\\"n\\\": 1}}"}}'
        calls = _detect_json_tool_calls(text)
        self.assertEqual(len(calls), 1)


class TestDetectTextToolCalls(unittest.TestCase):

    def test_bash_parens(self):
        calls = _detect_text_tool_calls("bash(ls -la)")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["name"], "bash")

    def test_bash_space(self):
        calls = _detect_text_tool_calls("bash ls -la")
        self.assertEqual(len(calls), 1)

    def test_file_read_parens(self):
        calls = _detect_text_tool_calls("file_read(/tmp/test.txt)")
        self.assertEqual(len(calls), 1)

    def test_long_text_not_matched(self):
        """Long explanatory text mentioning 'bash' should NOT match."""
        text = "To install bash on your system, you need to " + "x" * 200
        calls = _detect_text_tool_calls(text)
        self.assertEqual(len(calls), 0)

    def test_empty_string(self):
        calls = _detect_text_tool_calls("")
        self.assertEqual(len(calls), 0)

    def test_file_write_not_supported(self):
        """file_write needs multiple args — _build_simple_tool_call returns None."""
        calls = _detect_text_tool_calls("file_write(/tmp/test.txt)")
        self.assertEqual(len(calls), 0)

    def test_grep_pattern(self):
        calls = _detect_text_tool_calls("grep(TODO)")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["name"], "grep")


# ========================================================================
# 7c. _convert_tools_for_ollama
# ========================================================================
class TestConvertToolsForOllama(unittest.TestCase):

    def test_already_formatted(self):
        tools = [make_tool("bash")]
        result = _convert_tools_for_ollama(tools)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["type"], "function")

    def test_bare_function(self):
        tools = [{"name": "bash", "description": "Run", "parameters": {}}]
        result = _convert_tools_for_ollama(tools)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["type"], "function")
        self.assertEqual(result[0]["function"]["name"], "bash")

    def test_empty_list(self):
        self.assertEqual(_convert_tools_for_ollama([]), [])

    def test_non_dict_skipped(self):
        result = _convert_tools_for_ollama(["not a dict", 42])
        self.assertEqual(len(result), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
