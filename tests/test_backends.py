"""
Comprehensive tests for resonant_client/backends.py

Tests cover:
  - _convert_tools_for_ollama
  - _detect_json_tool_calls
  - _detect_text_tool_calls
  - _build_simple_tool_call
  - OllamaBackend tool detection, caching, and tool_mode property
"""

import json
import pytest
from unittest.mock import patch, MagicMock

from resonant_client.backends import (
    EVENT_BACKEND_STATUS,
    _convert_tools_for_ollama,
    _detect_json_tool_calls,
    _detect_text_tool_calls,
    _build_simple_tool_call,
    OllamaBackend,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_openai_tool(name="bash", desc="Run a command", props=None, required=None):
    """Build an OpenAI-format tool definition."""
    if props is None:
        props = {"command": {"type": "string"}}
    if required is None:
        required = list(props.keys())
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": desc,
            "parameters": {"type": "object", "properties": props, "required": required},
        },
    }


def _make_bare_tool(name="bash", desc="Run a command"):
    """Build a bare function-def dict (no 'type' wrapper)."""
    return {
        "name": name,
        "description": desc,
        "parameters": {"type": "object", "properties": {"arg": {"type": "string"}}, "required": ["arg"]},
    }


# ---------------------------------------------------------------------------
# _convert_tools_for_ollama
# ---------------------------------------------------------------------------

class TestConvertToolsForOllama:

    @pytest.mark.unit
    def test_already_correct_format_passes_through(self):
        tool = _make_openai_tool()
        result = _convert_tools_for_ollama([tool])
        assert result == [tool]

    @pytest.mark.unit
    def test_multiple_correct_tools(self):
        tools = [_make_openai_tool("bash"), _make_openai_tool("file_read")]
        result = _convert_tools_for_ollama(tools)
        assert len(result) == 2
        assert result[0]["function"]["name"] == "bash"
        assert result[1]["function"]["name"] == "file_read"

    @pytest.mark.unit
    def test_bare_function_def_gets_wrapped(self):
        bare = _make_bare_tool("grep")
        result = _convert_tools_for_ollama([bare])
        assert len(result) == 1
        assert result[0]["type"] == "function"
        assert result[0]["function"] == bare

    @pytest.mark.unit
    def test_multiple_bare_tools_wrapped(self):
        tools = [_make_bare_tool("bash"), _make_bare_tool("file_read")]
        result = _convert_tools_for_ollama(tools)
        assert all(t["type"] == "function" for t in result)

    @pytest.mark.unit
    def test_mixed_correct_and_bare(self):
        tools = [_make_openai_tool("bash"), _make_bare_tool("grep")]
        result = _convert_tools_for_ollama(tools)
        assert len(result) == 2

    @pytest.mark.unit
    def test_non_dict_items_skipped(self):
        result = _convert_tools_for_ollama(["string_item", 42, None, True])
        assert result == []

    @pytest.mark.unit
    def test_dict_without_type_or_name_skipped(self):
        result = _convert_tools_for_ollama([{"random": "data"}, {"key": "value"}])
        assert result == []

    @pytest.mark.unit
    def test_empty_list(self):
        assert _convert_tools_for_ollama([]) == []

    @pytest.mark.unit
    def test_mixed_valid_and_invalid(self):
        tools = [_make_openai_tool(), "invalid", _make_bare_tool(), 99, {"no": "match"}]
        result = _convert_tools_for_ollama(tools)
        assert len(result) == 2

    @pytest.mark.adversarial
    def test_dict_with_type_but_not_function(self):
        result = _convert_tools_for_ollama([{"type": "object", "name": "foo"}])
        # Has 'name' so it gets wrapped despite type != "function"
        assert len(result) == 1
        assert result[0]["type"] == "function"


# ---------------------------------------------------------------------------
# _detect_json_tool_calls
# ---------------------------------------------------------------------------

class TestDetectJsonToolCalls:

    @pytest.mark.unit
    def test_valid_json_tool_call(self):
        text = '{"name": "bash", "arguments": {"command": "ls -la"}}'
        result = _detect_json_tool_calls(text)
        assert len(result) == 1
        assert result[0]["name"] == "bash"
        parsed_args = json.loads(result[0]["arguments"])
        assert parsed_args["command"] == "ls -la"

    @pytest.mark.unit
    def test_tool_call_embedded_in_text(self):
        text = 'Let me run this: {"name": "bash", "arguments": {"command": "echo hello"}} for you.'
        result = _detect_json_tool_calls(text)
        assert len(result) == 1
        assert result[0]["name"] == "bash"

    @pytest.mark.unit
    def test_multiple_tool_calls(self):
        text = (
            '{"name": "bash", "arguments": {"command": "ls"}} '
            '{"name": "file_read", "arguments": {"path": "/tmp/x"}}'
        )
        result = _detect_json_tool_calls(text)
        assert len(result) == 2
        assert result[0]["name"] == "bash"
        assert result[1]["name"] == "file_read"

    @pytest.mark.unit
    def test_no_tool_calls_in_plain_text(self):
        assert _detect_json_tool_calls("This is just plain text.") == []

    @pytest.mark.unit
    def test_no_tool_calls_in_empty_string(self):
        assert _detect_json_tool_calls("") == []

    @pytest.mark.unit
    def test_malformed_json_ignored(self):
        text = '{"name": "bash", "arguments": {"command": "ls"'  # missing closing braces
        result = _detect_json_tool_calls(text)
        assert result == []

    @pytest.mark.unit
    def test_nested_braces_in_arguments(self):
        text = '{"name": "bash", "arguments": {"command": "echo \\"{key: value}\\""}}'
        result = _detect_json_tool_calls(text)
        assert len(result) == 1
        assert result[0]["name"] == "bash"

    @pytest.mark.unit
    def test_call_id_is_deterministic(self):
        text = '{"name": "bash", "arguments": {"command": "ls"}}'
        r1 = _detect_json_tool_calls(text)
        r2 = _detect_json_tool_calls(text)
        assert r1[0]["call_id"] == r2[0]["call_id"]

    @pytest.mark.unit
    def test_call_id_starts_with_call_prefix(self):
        text = '{"name": "bash", "arguments": {"command": "ls"}}'
        result = _detect_json_tool_calls(text)
        assert result[0]["call_id"].startswith("call_")

    @pytest.mark.adversarial
    def test_json_with_extra_fields_still_works(self):
        text = '{"name": "bash", "arguments": {"command": "ls"}, "extra": true}'
        result = _detect_json_tool_calls(text)
        assert len(result) == 1
        assert result[0]["name"] == "bash"

    @pytest.mark.adversarial
    def test_name_empty_string_skipped(self):
        text = '{"name": "", "arguments": {"command": "ls"}}'
        result = _detect_json_tool_calls(text)
        assert result == []

    @pytest.mark.adversarial
    def test_incomplete_brace_never_hangs(self):
        # Unmatched opening brace - should not infinite loop
        text = '{"name": "bash", "arguments": {"command": "ls"'
        result = _detect_json_tool_calls(text)
        assert result == []

    @pytest.mark.adversarial
    def test_deeply_nested_json(self):
        inner = json.dumps({"nested": {"deep": {"value": 42}}})
        text = f'{{"name": "bash", "arguments": {{"command": {json.dumps(inner)}}}}}'
        result = _detect_json_tool_calls(text)
        assert len(result) == 1

    @pytest.mark.adversarial
    def test_arguments_as_non_dict_still_captured(self):
        # The regex requires "arguments": { so a string arg wouldn't match the regex
        text = '{"name": "bash", "arguments": "raw_string"}'
        result = _detect_json_tool_calls(text)
        assert result == []  # Regex requires opening brace after arguments


# ---------------------------------------------------------------------------
# _detect_text_tool_calls
# ---------------------------------------------------------------------------

class TestDetectTextToolCalls:

    @pytest.mark.unit
    def test_bash_parentheses_format(self):
        result = _detect_text_tool_calls("bash(ls -la)")
        assert len(result) == 1
        assert result[0]["name"] == "bash"
        args = json.loads(result[0]["arguments"])
        assert args["command"] == "ls -la"

    @pytest.mark.unit
    def test_file_read_parentheses_format(self):
        result = _detect_text_tool_calls("file_read(/tmp/test.txt)")
        assert len(result) == 1
        assert result[0]["name"] == "file_read"
        args = json.loads(result[0]["arguments"])
        assert args["path"] == "/tmp/test.txt"

    @pytest.mark.unit
    def test_glob_parentheses_format(self):
        result = _detect_text_tool_calls("glob(*.py)")
        assert len(result) == 1
        assert result[0]["name"] == "glob"
        args = json.loads(result[0]["arguments"])
        assert args["pattern"] == "*.py"

    @pytest.mark.unit
    def test_grep_parentheses_format(self):
        result = _detect_text_tool_calls("grep(TODO)")
        assert len(result) == 1
        assert result[0]["name"] == "grep"
        args = json.loads(result[0]["arguments"])
        assert args["pattern"] == "TODO"

    @pytest.mark.unit
    def test_bash_space_format(self):
        result = _detect_text_tool_calls("bash ls -la")
        assert len(result) == 1
        assert result[0]["name"] == "bash"
        args = json.loads(result[0]["arguments"])
        assert args["command"] == "ls -la"

    @pytest.mark.unit
    def test_file_read_space_format(self):
        result = _detect_text_tool_calls("file_read /etc/hosts")
        assert len(result) == 1
        assert result[0]["name"] == "file_read"
        args = json.loads(result[0]["arguments"])
        assert args["path"] == "/etc/hosts"

    @pytest.mark.unit
    def test_bash_newline_format(self):
        result = _detect_text_tool_calls("bash\nls -la")
        assert len(result) == 1
        assert result[0]["name"] == "bash"

    @pytest.mark.unit
    def test_empty_string_returns_empty(self):
        assert _detect_text_tool_calls("") == []

    @pytest.mark.unit
    def test_whitespace_only_returns_empty(self):
        assert _detect_text_tool_calls("   \n  \t  ") == []

    @pytest.mark.unit
    def test_plain_text_no_match(self):
        result = _detect_text_tool_calls("The bash command is useful for running things.")
        assert result == []

    @pytest.mark.unit
    def test_long_text_space_format_rejected(self):
        """Space-format only matches when text < 200 chars."""
        long_text = "bash " + "x" * 200
        result = _detect_text_tool_calls(long_text)
        assert result == []

    @pytest.mark.unit
    def test_parentheses_format_allows_long_args(self):
        """Parentheses format has no length limit."""
        long_cmd = "echo " + "a" * 300
        result = _detect_text_tool_calls(f"bash({long_cmd})")
        assert len(result) == 1

    @pytest.mark.unit
    def test_file_write_not_supported_in_simple_format(self):
        """file_write needs multiple args, so _build_simple_tool_call returns None."""
        result = _detect_text_tool_calls("file_write(/tmp/test.txt)")
        assert result == []

    @pytest.mark.unit
    def test_file_edit_not_supported_in_simple_format(self):
        result = _detect_text_tool_calls("file_edit(/tmp/test.txt)")
        assert result == []

    @pytest.mark.adversarial
    def test_unknown_tool_name_ignored(self):
        result = _detect_text_tool_calls("unknown_tool(arg)")
        assert result == []

    @pytest.mark.adversarial
    def test_tool_name_in_middle_of_sentence_no_match_space_format(self):
        result = _detect_text_tool_calls("please run bash echo hello")
        assert result == []

    @pytest.mark.adversarial
    def test_bash_with_empty_args_space_format(self):
        # "bash " with nothing after should produce empty args
        # Actually text.startswith("bash ") is True, raw_args="" is falsy, so no match
        result = _detect_text_tool_calls("bash ")
        assert result == []

    @pytest.mark.adversarial
    def test_tool_call_with_special_characters(self):
        result = _detect_text_tool_calls("bash(echo 'hello world' && cat /tmp/f.txt)")
        assert len(result) == 1
        args = json.loads(result[0]["arguments"])
        assert "hello world" in args["command"]


# ---------------------------------------------------------------------------
# _build_simple_tool_call
# ---------------------------------------------------------------------------

class TestBuildSimpleToolCall:

    @pytest.mark.unit
    def test_bash_builds_command_arg(self):
        tc = _build_simple_tool_call("bash", "ls -la")
        assert tc is not None
        assert tc["name"] == "bash"
        args = json.loads(tc["arguments"])
        assert args == {"command": "ls -la"}

    @pytest.mark.unit
    def test_file_read_builds_path_arg(self):
        tc = _build_simple_tool_call("file_read", "/tmp/x.txt")
        args = json.loads(tc["arguments"])
        assert args == {"path": "/tmp/x.txt"}

    @pytest.mark.unit
    def test_glob_builds_pattern_arg(self):
        tc = _build_simple_tool_call("glob", "**/*.py")
        args = json.loads(tc["arguments"])
        assert args == {"pattern": "**/*.py"}

    @pytest.mark.unit
    def test_grep_builds_pattern_arg(self):
        tc = _build_simple_tool_call("grep", "TODO")
        args = json.loads(tc["arguments"])
        assert args == {"pattern": "TODO"}

    @pytest.mark.unit
    def test_file_write_returns_none(self):
        assert _build_simple_tool_call("file_write", "content") is None

    @pytest.mark.unit
    def test_file_edit_returns_none(self):
        assert _build_simple_tool_call("file_edit", "content") is None

    @pytest.mark.unit
    def test_unknown_tool_returns_none(self):
        assert _build_simple_tool_call("unknown", "args") is None

    @pytest.mark.unit
    def test_call_id_format(self):
        tc = _build_simple_tool_call("bash", "ls")
        assert tc["call_id"].startswith("call_")
        # 8 hex digits after "call_"
        hex_part = tc["call_id"][5:]
        assert len(hex_part) == 8
        int(hex_part, 16)  # Should not raise

    @pytest.mark.unit
    def test_deterministic_call_id(self):
        tc1 = _build_simple_tool_call("bash", "ls")
        tc2 = _build_simple_tool_call("bash", "ls")
        assert tc1["call_id"] == tc2["call_id"]

    @pytest.mark.unit
    def test_different_args_different_call_id(self):
        tc1 = _build_simple_tool_call("bash", "ls")
        tc2 = _build_simple_tool_call("bash", "pwd")
        assert tc1["call_id"] != tc2["call_id"]

    @pytest.mark.adversarial
    def test_empty_args_string(self):
        tc = _build_simple_tool_call("bash", "")
        assert tc is not None
        args = json.loads(tc["arguments"])
        assert args["command"] == ""

    @pytest.mark.adversarial
    def test_args_with_quotes_and_special_chars(self):
        tc = _build_simple_tool_call("bash", 'echo "hello\\nworld"')
        assert tc is not None
        args = json.loads(tc["arguments"])
        assert 'echo "hello\\nworld"' == args["command"]

    @pytest.mark.adversarial
    def test_model_param_not_used_currently(self):
        """_build_simple_tool_call accepts name and raw_args only (model was removed)."""
        # Just verify the 2-arg call works
        tc = _build_simple_tool_call("bash", "ls")
        assert tc is not None


# ---------------------------------------------------------------------------
# OllamaBackend._detect_tool_support
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def clear_tool_cache():
    """Clear the class-level tool support cache before each test."""
    OllamaBackend._tool_support_cache.clear()
    yield
    OllamaBackend._tool_support_cache.clear()


def _make_backend(model="llama3.1:8b", url="http://10.0.0.133:11434"):
    return OllamaBackend(url, model)


class TestDetectToolSupportKnownModels:

    @pytest.mark.unit
    @pytest.mark.parametrize("model", [
        "llama3.1", "llama3.2", "llama3.3", "llama4",
        "qwen2.5", "qwen2.5-coder", "qwen3",
        "mistral", "mistral-nemo", "mistral-small", "mistral-large",
        "command-r", "command-r-plus",
        "gemma2", "gemma3",
        "phi4", "phi4-mini",
        "deepseek-r1",
    ])
    def test_known_tool_support_models(self, model):
        backend = _make_backend(model)
        assert backend._detect_tool_support() is True

    @pytest.mark.unit
    @pytest.mark.parametrize("model", [
        "llama2", "llama3", "codellama",
        "phi", "phi3",
        "gemma",
        "starcoder", "starcoder2",
        "deepseek-coder", "deepseek-coder-v2",
        "yi",
    ])
    def test_known_no_tool_support_models(self, model):
        backend = _make_backend(model)
        assert backend._detect_tool_support() is False

    @pytest.mark.unit
    def test_model_with_tag_stripped(self):
        backend = _make_backend("llama3.1:70b-q4")
        assert backend._detect_tool_support() is True

    @pytest.mark.unit
    def test_model_with_latest_tag(self):
        backend = _make_backend("mistral:latest")
        assert backend._detect_tool_support() is True

    @pytest.mark.unit
    def test_model_with_size_tag(self):
        backend = _make_backend("qwen2.5:14b")
        assert backend._detect_tool_support() is True

    @pytest.mark.unit
    def test_no_tool_model_with_tag(self):
        backend = _make_backend("codellama:13b")
        assert backend._detect_tool_support() is False

    @pytest.mark.unit
    def test_case_insensitive_base_model(self):
        backend = _make_backend("Llama3.1:8b")
        # .lower() is called so this should match
        assert backend._detect_tool_support() is True

    @pytest.mark.unit
    def test_deepseek_v4_pro_recognized_as_tool_capable(self):
        """v0.5.2a2 fix — pro had been missing from KNOWN_TOOL_SUPPORT,
        which meant it fell through to /api/show, where the cloud
        model's empty template + missing-capabilities-check made it
        report False. Pro then went to text-mode and the parser
        couldn't keep up with its XML format variation."""
        backend = _make_backend("deepseek-v4-pro:cloud")
        assert backend._detect_tool_support() is True


class TestDetectToolSupportCaching:

    @pytest.mark.unit
    def test_cache_populated_after_detection(self):
        backend = _make_backend("llama3.1")
        backend._detect_tool_support()
        assert "llama3.1" in OllamaBackend._tool_support_cache

    @pytest.mark.unit
    def test_second_call_uses_cache(self):
        backend = _make_backend("llama3.1")
        backend._detect_tool_support()
        # Modify the known set to verify cache is used, not the set
        with patch.object(OllamaBackend, '_KNOWN_TOOL_SUPPORT', set()):
            result = backend._detect_tool_support()
        assert result is True

    @pytest.mark.unit
    def test_cache_shared_between_instances(self):
        b1 = _make_backend("llama3.1")
        b1._detect_tool_support()
        b2 = _make_backend("llama3.1")
        # Should return cached result without hitting known lists
        with patch.object(OllamaBackend, '_KNOWN_TOOL_SUPPORT', set()):
            result = b2._detect_tool_support()
        assert result is True

    @pytest.mark.unit
    def test_different_models_cached_separately(self):
        b1 = _make_backend("llama3.1")
        b2 = _make_backend("codellama")
        b1._detect_tool_support()
        b2._detect_tool_support()
        assert OllamaBackend._tool_support_cache["llama3.1"] is True
        assert OllamaBackend._tool_support_cache["codellama"] is False


class TestDetectToolSupportTemplateAndProbe:

    @pytest.mark.unit
    def test_template_with_tools_placeholder(self):
        """Unknown model with {{.Tools}} in template -> True."""
        backend = _make_backend("custom-model:latest")
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"template": "{{.System}}\n{{.Tools}}\n{{.Prompt}}"}
        with patch("httpx.post", return_value=mock_resp):
            assert backend._detect_tool_support() is True

    @pytest.mark.unit
    def test_template_with_spaced_tools_placeholder(self):
        backend = _make_backend("custom-model")
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"template": "{{ .Tools }}"}
        with patch("httpx.post", return_value=mock_resp):
            assert backend._detect_tool_support() is True

    @pytest.mark.unit
    def test_template_without_tools_returns_false(self):
        backend = _make_backend("custom-model")
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"template": "{{.System}}\n{{.Prompt}}"}
        with patch("httpx.post", return_value=mock_resp):
            assert backend._detect_tool_support() is False

    @pytest.mark.unit
    def test_template_empty_falls_through_to_probe(self):
        """Empty template means we can't decide from template; falls to probe."""
        backend = _make_backend("custom-model")
        show_resp = MagicMock()
        show_resp.status_code = 200
        show_resp.json.return_value = {"template": ""}

        probe_resp = MagicMock()
        probe_resp.status_code = 200
        probe_resp.json.return_value = {"message": {"tool_calls": [{"function": {"name": "test_tool"}}]}}

        with patch("httpx.post", side_effect=[show_resp, probe_resp]):
            assert backend._detect_tool_support() is True

    @pytest.mark.unit
    def test_probe_with_native_tool_calls_returns_true(self):
        backend = _make_backend("custom-model")
        show_resp = MagicMock()
        show_resp.status_code = 404  # /api/show fails

        probe_resp = MagicMock()
        probe_resp.status_code = 200
        probe_resp.json.return_value = {"message": {"tool_calls": [{"function": {"name": "test_tool"}}]}}

        with patch("httpx.post", side_effect=[show_resp, probe_resp]):
            assert backend._detect_tool_support() is True

    @pytest.mark.unit
    def test_probe_without_tool_calls_returns_false(self):
        backend = _make_backend("custom-model")
        show_resp = MagicMock()
        show_resp.status_code = 404

        probe_resp = MagicMock()
        probe_resp.status_code = 200
        probe_resp.json.return_value = {"message": {"content": "I called test_tool"}}

        with patch("httpx.post", side_effect=[show_resp, probe_resp]):
            assert backend._detect_tool_support() is False

    @pytest.mark.unit
    def test_all_network_fails_defaults_to_true(self):
        backend = _make_backend("custom-model")
        with patch("httpx.post", side_effect=Exception("Connection refused")):
            assert backend._detect_tool_support() is True

    @pytest.mark.unit
    def test_show_exception_falls_through_to_probe(self):
        backend = _make_backend("custom-model")
        call_count = [0]
        def mock_post(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                raise ConnectionError("timeout")
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = {"message": {"tool_calls": [{"function": {"name": "t"}}]}}
            return resp

        with patch("httpx.post", side_effect=mock_post):
            assert backend._detect_tool_support() is True


# ---------------------------------------------------------------------------
# OllamaBackend.tool_mode property
# ---------------------------------------------------------------------------

class TestToolModeProperty:

    @pytest.mark.unit
    def test_before_detection_returns_unknown(self):
        backend = _make_backend("llama3.1")
        assert backend.tool_mode == "unknown"

    @pytest.mark.unit
    def test_after_native_detection_returns_native(self):
        backend = _make_backend("llama3.1")
        backend._use_native_tools = True
        assert backend.tool_mode == "native"

    @pytest.mark.unit
    def test_after_text_detection_returns_text(self):
        backend = _make_backend("codellama")
        backend._use_native_tools = False
        assert backend.tool_mode == "text"

    @pytest.mark.unit
    def test_tool_mode_none_means_unknown(self):
        backend = _make_backend("anything")
        backend._use_native_tools = None
        assert backend.tool_mode == "unknown"


# ---------------------------------------------------------------------------
# OllamaBackend construction and class attributes
# ---------------------------------------------------------------------------

class TestOllamaBackendInit:

    @pytest.mark.unit
    def test_base_url_trailing_slash_stripped(self):
        backend = OllamaBackend("http://example.com:11434/", "model")
        assert backend.base_url == "http://example.com:11434"

    @pytest.mark.unit
    def test_name_is_ollama(self):
        backend = _make_backend()
        assert backend.name == "ollama"

    @pytest.mark.unit
    def test_initial_native_tools_is_none(self):
        backend = _make_backend()
        assert backend._use_native_tools is None

    @pytest.mark.unit
    def test_known_tool_support_is_set(self):
        assert isinstance(OllamaBackend._KNOWN_TOOL_SUPPORT, set)
        assert "llama3.1" in OllamaBackend._KNOWN_TOOL_SUPPORT

    @pytest.mark.unit
    def test_known_no_tool_support_is_set(self):
        assert isinstance(OllamaBackend._KNOWN_NO_TOOL_SUPPORT, set)
        assert "codellama" in OllamaBackend._KNOWN_NO_TOOL_SUPPORT

    @pytest.mark.unit
    def test_known_sets_are_disjoint(self):
        overlap = OllamaBackend._KNOWN_TOOL_SUPPORT & OllamaBackend._KNOWN_NO_TOOL_SUPPORT
        assert len(overlap) == 0, f"Overlap found: {overlap}"

    @pytest.mark.unit
    def test_cache_is_class_level_dict(self):
        assert isinstance(OllamaBackend._tool_support_cache, dict)

    @pytest.mark.unit
    def test_ollama_options_from_env(self, monkeypatch):
        monkeypatch.setenv("RESONANT_OLLAMA_NUM_CTX", "131072")
        monkeypatch.setenv("RESONANT_OLLAMA_NUM_BATCH", "1024")
        monkeypatch.setenv("RESONANT_OLLAMA_NUM_GPU", "1")
        monkeypatch.setenv("RESONANT_OLLAMA_KEEP_ALIVE", "24h")
        monkeypatch.setenv("RESONANT_OLLAMA_HTTP_READ_TIMEOUT_SEC", "300")
        b = OllamaBackend("http://127.0.0.1:11434", "qwen2.5-coder")
        assert b._ollama_options["num_ctx"] == 131072
        assert b._ollama_options["num_batch"] == 1024
        assert b._ollama_options["num_gpu"] == 1
        assert b._ollama_keep_alive == "24h"
        assert b._ollama_http_read_timeout == 300.0


# ---------------------------------------------------------------------------
# Edge cases / adversarial
# ---------------------------------------------------------------------------

class TestEdgeCases:

    @pytest.mark.adversarial
    def test_empty_model_name(self):
        backend = _make_backend("")
        # Empty model won't match any known list; will fall through to network
        with patch("httpx.post", side_effect=Exception("no network")):
            result = backend._detect_tool_support()
        # Defaults to True when everything fails
        assert result is True

    @pytest.mark.adversarial
    def test_very_long_model_name(self):
        long_name = "a" * 10000
        backend = _make_backend(long_name)
        with patch("httpx.post", side_effect=Exception("no")):
            result = backend._detect_tool_support()
        assert result is True

    @pytest.mark.adversarial
    def test_unicode_model_name(self):
        backend = _make_backend("模型名称:latest")
        with patch("httpx.post", side_effect=Exception("no")):
            result = backend._detect_tool_support()
        assert result is True

    @pytest.mark.adversarial
    def test_model_name_with_many_colons(self):
        backend = _make_backend("org:model:tag:extra")
        # base_model = "org" after split(":")[0]
        with patch("httpx.post", side_effect=Exception("no")):
            result = backend._detect_tool_support()
        assert result is True

    @pytest.mark.adversarial
    def test_detect_json_tool_calls_with_unicode(self):
        text = '{"name": "bash", "arguments": {"command": "echo \\u4f60\\u597d"}}'
        result = _detect_json_tool_calls(text)
        assert len(result) == 1

    @pytest.mark.adversarial
    def test_detect_json_tool_calls_with_newlines_in_value(self):
        text = '{"name": "bash", "arguments": {"command": "line1\\nline2"}}'
        result = _detect_json_tool_calls(text)
        assert len(result) == 1

    @pytest.mark.adversarial
    def test_detect_text_tool_calls_with_leading_whitespace(self):
        result = _detect_text_tool_calls("  bash(ls)")
        assert len(result) == 1

    @pytest.mark.adversarial
    def test_detect_text_tool_calls_with_trailing_whitespace(self):
        result = _detect_text_tool_calls("bash(ls)  \n")
        assert len(result) == 1

    @pytest.mark.adversarial
    def test_convert_tools_preserves_order(self):
        tools = [_make_openai_tool(f"tool_{i}") for i in range(10)]
        result = _convert_tools_for_ollama(tools)
        for i, t in enumerate(result):
            assert t["function"]["name"] == f"tool_{i}"

    @pytest.mark.adversarial
    def test_json_detection_with_only_braces(self):
        assert _detect_json_tool_calls("{}") == []

    @pytest.mark.adversarial
    def test_json_detection_with_name_but_no_arguments_key(self):
        text = '{"name": "bash", "other": {}}'
        assert _detect_json_tool_calls(text) == []

    @pytest.mark.adversarial
    def test_text_detection_tool_name_as_substring(self):
        """'bash' inside a word should not match parentheses format."""
        result = _detect_text_tool_calls("rebash(something)")
        assert result == []

    @pytest.mark.adversarial
    def test_multiple_cache_clears_safe(self):
        OllamaBackend._tool_support_cache.clear()
        OllamaBackend._tool_support_cache.clear()
        assert OllamaBackend._tool_support_cache == {}


# ---------------------------------------------------------------------------
# Integration-style: conftest fixtures
# ---------------------------------------------------------------------------

class TestWithConftestFixtures:

    @pytest.mark.unit
    def test_convert_sample_tools(self, sample_tools):
        result = _convert_tools_for_ollama(sample_tools)
        assert len(result) == len(sample_tools)
        for t in result:
            assert t["type"] == "function"

    @pytest.mark.unit
    def test_mock_backend_starts_unknown(self, mock_ollama_backend):
        assert mock_ollama_backend.tool_mode == "unknown"

    @pytest.mark.unit
    def test_mock_backend_cache_cleared(self, mock_ollama_backend):
        assert OllamaBackend._tool_support_cache == {}

    @pytest.mark.unit
    def test_mock_backend_detects_known_model(self, mock_ollama_backend):
        # mock_ollama_backend uses llama3.1:8b
        result = mock_ollama_backend._detect_tool_support()
        assert result is True


# ── v0.5.6a1: Backend status surfacing for Ollama 503 retries ─────────


class TestOpenChatStreamWithRetryNotify:
    """`_open_chat_stream_with_retry` accepts a `notify_retry` callback
    that fires once per retry attempt with a structured payload. The
    callback's role is to let the engine layer surface "still alive,
    retrying" status events to the GUI — without this hook, retries
    are completely silent and users assume the daemon hung.

    Tests stub the httpx layer to deterministically trigger 503 → 200
    sequences without any network."""

    @pytest.fixture
    def backend(self):
        return OllamaBackend("http://stub", "llama3.1:8b")

    def _make_response(self, status_code, body=""):
        """A minimal stand-in for httpx's Response inside a context."""
        resp = MagicMock()
        resp.status_code = status_code
        resp.read = MagicMock(return_value=body.encode())
        # `with client.stream(...) as resp:` exits cleanly
        cm = MagicMock()
        cm.__enter__ = MagicMock(return_value=resp)
        cm.__exit__ = MagicMock(return_value=None)
        return cm

    def _make_client_stub(self, responses):
        """Build an httpx.Client stand-in that yields a queued response
        per call to `.stream(...)`. Each `responses` entry is a (status,
        body) tuple."""
        responses_iter = iter(responses)
        client = MagicMock()

        def _stream(method, url, **kwargs):
            status, body = next(responses_iter)
            return self._make_response(status, body)

        client.stream = _stream
        return client

    def _patch_client_factory(self, sequence):
        """Patch httpx.Client so each constructor call returns a stub
        backed by ONE response (which httpx in real life would emit
        sequentially — we mimic by constructing fresh each time)."""
        sequence_iter = iter(sequence)

        def _factory(*args, **kwargs):
            status, body = next(sequence_iter)
            return self._make_client_stub([(status, body)])

        return patch("httpx.Client", side_effect=_factory)

    @pytest.mark.unit
    def test_no_retry_no_notify(self, backend):
        """200 OK on first try → notify_retry is never called."""
        notify_calls = []
        with self._patch_client_factory([(200, '{"ok":true}')]), \
             patch("resonant_client.backends._wait_with_cancel", return_value=False):
            with backend._open_chat_stream_with_retry(
                payload={}, stream_timeout=None, cancel_event=None,
                notify_retry=notify_calls.append,
            ) as (client, resp):
                assert resp.status_code == 200
        assert notify_calls == []

    @pytest.mark.unit
    def test_503_then_200_emits_one_retry_notify(self, backend):
        """503 → 200 sequence: notify fires exactly once with the 503
        details. Final yielded response is the 200."""
        notify_calls = []
        with self._patch_client_factory([
            (503, '{"error":"Server overloaded"}'),
            (200, '{"ok":true}'),
        ]), patch("resonant_client.backends._wait_with_cancel", return_value=False):
            with backend._open_chat_stream_with_retry(
                payload={}, stream_timeout=None, cancel_event=None,
                notify_retry=notify_calls.append,
            ) as (client, resp):
                assert resp.status_code == 200
        assert len(notify_calls) == 1
        ev = notify_calls[0]
        assert ev["kind"] == "ollama_retry"
        assert ev["status_code"] == 503
        assert ev["attempt"] == 1
        assert ev["max"] >= 2  # at least 1 retry + first attempt
        assert ev["model"] == "llama3.1:8b"
        assert ev["backoff_seconds"] > 0
        assert "Server overloaded" in ev["body_preview"]

    @pytest.mark.unit
    def test_consecutive_5xx_emit_multiple_retry_notifies(self, backend):
        """503 → 502 → 200 sequence: notify fires twice, with the
        attempt counter advancing each time."""
        notify_calls = []
        with self._patch_client_factory([
            (503, "overloaded"),
            (502, "bad gateway"),
            (200, '{"ok":true}'),
        ]), patch("resonant_client.backends._wait_with_cancel", return_value=False):
            with backend._open_chat_stream_with_retry(
                payload={}, stream_timeout=None, cancel_event=None,
                notify_retry=notify_calls.append,
            ) as (client, resp):
                assert resp.status_code == 200
        assert len(notify_calls) == 2
        assert notify_calls[0]["status_code"] == 503
        assert notify_calls[0]["attempt"] == 1
        assert notify_calls[1]["status_code"] == 502
        assert notify_calls[1]["attempt"] == 2
        # Backoff should grow (exponential)
        assert notify_calls[1]["backoff_seconds"] > notify_calls[0]["backoff_seconds"]

    @pytest.mark.unit
    def test_notify_callback_exception_does_not_break_retry(self, backend):
        """A misbehaving notify_retry must not break the retry loop —
        the GUI is downstream of the backend, not the other way round."""
        def bad_notify(_):
            raise RuntimeError("GUI crashed")

        with self._patch_client_factory([
            (503, "overloaded"),
            (200, '{"ok":true}'),
        ]), patch("resonant_client.backends._wait_with_cancel", return_value=False):
            with backend._open_chat_stream_with_retry(
                payload={}, stream_timeout=None, cancel_event=None,
                notify_retry=bad_notify,
            ) as (client, resp):
                assert resp.status_code == 200

    @pytest.mark.unit
    def test_notify_retry_optional(self, backend):
        """Passing notify_retry=None (the default) must work — old
        callers that didn't know about the parameter shouldn't break."""
        with self._patch_client_factory([
            (503, "overloaded"),
            (200, '{"ok":true}'),
        ]), patch("resonant_client.backends._wait_with_cancel", return_value=False):
            with backend._open_chat_stream_with_retry(
                payload={}, stream_timeout=None, cancel_event=None,
            ) as (client, resp):
                assert resp.status_code == 200


class TestBackendStatusEventConstant:
    """Pin the event-type constant + the payload shape contract so a
    rename or refactor downstream of this fix doesn't silently break the
    GUI handler."""

    @pytest.mark.unit
    def test_event_constant_value(self):
        # Backend yields tuples like (event_type, data); the consumer
        # in engine/session.py compares against EVENT_BACKEND_STATUS.
        assert EVENT_BACKEND_STATUS == "backend.status"

    @pytest.mark.unit
    def test_engine_event_alias_matches(self):
        from resonant_client.events import EngineEvent
        assert EngineEvent.BACKEND_STATUS.value == "backend.status"


# ── v0.6.4 (F2): retry-budget-exhausted status event ─────────────────


class TestOllamaExhaustedStatus:
    """v0.6.4 (F2) — when the retry budget is spent on a transient 5xx,
    `stream()` must emit a terminal `ollama_exhausted` backend-status
    event BEFORE it raises. The v0.6.2 field run found that an exhausted
    retry budget left the GUI silent — the per-retry banner faded and
    nothing told the user the step had stalled. The exhausted event is
    what the GUI renders as a persistent chip.

    Stubs the httpx layer so every attempt 503s — exhausting the budget
    deterministically with no network.
    """

    @pytest.fixture
    def backend(self):
        return OllamaBackend("http://stub", "deepseek-v4-pro:cloud")

    def _all_503_client_factory(self):
        """httpx.Client stand-in: every constructed client streams a 503."""
        def _make_503_cm():
            resp = MagicMock()
            resp.status_code = 503
            resp.read = MagicMock(
                return_value=b'{"error":"Server overloaded, please retry shortly"}'
            )
            resp.request = MagicMock()
            cm = MagicMock()
            cm.__enter__ = MagicMock(return_value=resp)
            cm.__exit__ = MagicMock(return_value=None)
            return cm

        def _factory(*args, **kwargs):
            client = MagicMock()
            client.stream = MagicMock(side_effect=lambda *a, **k: _make_503_cm())
            return client

        return patch("httpx.Client", side_effect=_factory)

    def _drain_stream(self, backend):
        """Run backend.stream() to completion under the all-503 stub,
        returning the full list of yielded (event_type, data) tuples.
        `stream()` catches the terminal HTTPStatusError internally and
        yields an EVENT_ERROR — it does not raise — so a plain drain
        is safe."""
        events = []
        with self._all_503_client_factory(), \
             patch("resonant_client.backends._wait_with_cancel", return_value=False):
            for ev in backend.stream(
                user_msg="hi", conversation_history=[],
                instructions="", tools=[],
            ):
                events.append(ev)
        return events

    @pytest.mark.unit
    def test_exhausted_retries_emit_exhausted_event(self, backend):
        """Every attempt 503s → an `ollama_exhausted` backend-status
        event is yielded with the model + attempt-count detail."""
        events = self._drain_stream(backend)
        exhausted = [
            d for (etype, d) in events
            if etype == EVENT_BACKEND_STATUS and d.get("kind") == "ollama_exhausted"
        ]
        assert len(exhausted) == 1, f"expected 1 exhausted event, got {len(exhausted)}"
        ev = exhausted[0]
        assert ev["status_code"] == 503
        assert ev["model"] == "deepseek-v4-pro:cloud"
        assert ev["attempts"] == 4  # 1 initial + 3 retries
        assert "overloaded" in ev["body_preview"].lower()

    @pytest.mark.unit
    def test_exhausted_event_still_followed_by_error_event(self, backend):
        """The exhausted status is ADDITIVE — `stream()` still yields
        the terminal EVENT_ERROR afterward so existing error handling
        is unchanged. The exhausted event just gives the GUI a chance
        to render the persistent chip first."""
        events = self._drain_stream(backend)
        # An error event is still emitted (stream() catches the raise).
        assert any(etype == "error" for (etype, _d) in events), \
            "terminal error event must still be emitted"

    @pytest.mark.unit
    def test_retry_events_precede_exhausted_event(self, backend):
        """The 3 retry events come first, then the terminal exhausted
        event — so the GUI shows the retry banners, then the chip."""
        events = self._drain_stream(backend)
        kinds = [
            d.get("kind") for (etype, d) in events
            if etype == EVENT_BACKEND_STATUS
        ]
        # 3 retries (attempts 1-3) then 1 exhausted.
        assert kinds == ["ollama_retry", "ollama_retry", "ollama_retry",
                         "ollama_exhausted"]

    @pytest.mark.unit
    def test_non_retryable_4xx_emits_no_exhausted_event(self, backend):
        """A 400 (non-retryable) must NOT produce an exhausted event —
        the chip is only for spent retry budgets on transient 5xx."""
        def _make_400_cm():
            resp = MagicMock()
            resp.status_code = 400
            resp.read = MagicMock(return_value=b'{"error":"bad request"}')
            resp.request = MagicMock()
            cm = MagicMock()
            cm.__enter__ = MagicMock(return_value=resp)
            cm.__exit__ = MagicMock(return_value=None)
            return cm

        def _factory(*args, **kwargs):
            client = MagicMock()
            client.stream = MagicMock(side_effect=lambda *a, **k: _make_400_cm())
            return client

        events = []
        with patch("httpx.Client", side_effect=_factory), \
             patch("resonant_client.backends._wait_with_cancel", return_value=False):
            for ev in backend.stream(
                user_msg="hi", conversation_history=[],
                instructions="", tools=[],
            ):
                events.append(ev)
        assert not [
            d for (etype, d) in events
            if etype == EVENT_BACKEND_STATUS and d.get("kind") == "ollama_exhausted"
        ]
