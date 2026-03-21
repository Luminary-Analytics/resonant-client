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
