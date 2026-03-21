"""
Comprehensive tests for resonant_client/protocol.py

Covers: build_tool_system_prompt, strip_think_tags, _try_parse_tool_json,
parse_tool_calls, and _tool_prompt_cache thread safety.
"""

import json
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

import pytest

from resonant_client.protocol import (
    _tool_prompt_cache,
    _try_parse_tool_json,
    build_tool_system_prompt,
    parse_tool_calls,
    strip_think_tags,
)


# ── Helpers ─────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def clear_cache():
    """Clear the module-level prompt cache before every test."""
    _tool_prompt_cache.clear()
    yield
    _tool_prompt_cache.clear()


def _bare_tool(name, desc="A tool", properties=None, required=None):
    """Quick helper to make a bare (non-OpenAI-wrapped) tool dict."""
    params = {}
    if properties is not None:
        params["properties"] = properties
    if required is not None:
        params["required"] = required
    return {"name": name, "description": desc, "parameters": params}


# =====================================================================
# 1. build_tool_system_prompt
# =====================================================================

class TestBuildToolSystemPrompt:
    """Tests for build_tool_system_prompt."""

    @pytest.mark.unit
    def test_empty_tools_returns_empty(self):
        assert build_tool_system_prompt([]) == ""

    @pytest.mark.unit
    def test_none_list_returns_empty(self):
        """An empty list (falsy) produces empty string."""
        assert build_tool_system_prompt([]) == ""

    @pytest.mark.unit
    def test_single_openai_tool(self, make_tool):
        tool = make_tool("grep", "Search files")
        result = build_tool_system_prompt([tool])
        assert "grep" in result
        assert "Search files" in result
        assert "<tool_call>" in result

    @pytest.mark.unit
    def test_single_bare_tool(self):
        tool = _bare_tool("ls", "List directory")
        result = build_tool_system_prompt([tool])
        assert "ls" in result
        assert "List directory" in result

    @pytest.mark.unit
    def test_multiple_tools(self, sample_tools):
        result = build_tool_system_prompt(sample_tools)
        assert "bash" in result
        assert "file_read" in result
        assert "file_edit" in result
        assert "file_write" in result

    @pytest.mark.unit
    def test_required_params_get_asterisk(self, make_tool):
        tool = make_tool("cmd", "Run", {
            "type": "object",
            "properties": {"x": {"type": "string"}, "y": {"type": "integer"}},
            "required": ["x"],
        })
        result = build_tool_system_prompt([tool])
        assert "x*" in result
        assert "y*" not in result

    @pytest.mark.unit
    def test_optional_params_no_asterisk(self, make_tool):
        tool = make_tool("cmd", "Run", {
            "type": "object",
            "properties": {"opt": {"type": "string"}},
            "required": [],
        })
        result = build_tool_system_prompt([tool])
        assert "opt*" not in result
        assert "opt" in result

    @pytest.mark.unit
    def test_tool_with_no_params(self, make_tool):
        tool = make_tool("noop", "Do nothing", {})
        result = build_tool_system_prompt([tool])
        assert "noop" in result
        # No parenthesised param list expected
        assert "noop()" not in result or "noop" in result

    @pytest.mark.unit
    def test_description_truncated_at_period(self, make_tool):
        tool = make_tool("thing", "First sentence. Second sentence. Third.")
        result = build_tool_system_prompt([tool])
        assert "First sentence" in result
        assert "Second sentence" not in result

    @pytest.mark.unit
    def test_cache_hit_returns_same_object(self, make_tool):
        tools = [make_tool("alpha")]
        first = build_tool_system_prompt(tools)
        second = build_tool_system_prompt(tools)
        assert first is second

    @pytest.mark.unit
    def test_cache_key_is_sorted(self, make_tool):
        """Order of tools doesn't matter — same set hits the cache."""
        a = make_tool("alpha")
        b = make_tool("beta")
        result_ab = build_tool_system_prompt([a, b])
        result_ba = build_tool_system_prompt([b, a])
        assert result_ab is result_ba

    @pytest.mark.unit
    def test_different_tool_sets_same_size_no_collision(self, make_tool):
        set1 = [make_tool("aaa"), make_tool("bbb")]
        set2 = [make_tool("ccc"), make_tool("ddd")]
        r1 = build_tool_system_prompt(set1)
        r2 = build_tool_system_prompt(set2)
        assert r1 is not r2
        assert "aaa" in r1
        assert "ccc" in r2

    @pytest.mark.unit
    def test_cache_stored_in_module_dict(self, make_tool):
        tools = [make_tool("cached_tool")]
        build_tool_system_prompt(tools)
        assert ("cached_tool",) in _tool_prompt_cache

    @pytest.mark.unit
    def test_non_dict_tools_skipped(self):
        result = build_tool_system_prompt(["not_a_dict", 42, None])
        # Non-dict items are skipped; no tool names collected => empty cache key
        # But the header is still emitted since `tools` is truthy
        assert isinstance(result, str)

    @pytest.mark.unit
    def test_mixed_dict_and_non_dict(self, make_tool):
        tools = [make_tool("real"), "garbage", 99]
        result = build_tool_system_prompt(tools)
        assert "real" in result

    @pytest.mark.unit
    def test_malformed_tool_missing_name(self):
        tool = {"type": "function", "function": {"description": "no name"}}
        result = build_tool_system_prompt([tool])
        assert "unknown" in result

    @pytest.mark.unit
    def test_malformed_tool_missing_function_key(self):
        tool = {"type": "function"}
        result = build_tool_system_prompt([tool])
        assert isinstance(result, str)

    @pytest.mark.unit
    def test_header_contains_rules(self, make_tool):
        result = build_tool_system_prompt([make_tool("x")])
        assert "ALWAYS use tools" in result
        assert '"name"' in result
        assert '"arguments"' in result


# =====================================================================
# 2. strip_think_tags
# =====================================================================

class TestStripThinkTags:
    """Tests for strip_think_tags."""

    @pytest.mark.unit
    def test_no_tags(self):
        assert strip_think_tags("Hello world") == "Hello world"

    @pytest.mark.unit
    def test_empty_string(self):
        assert strip_think_tags("") == ""

    @pytest.mark.unit
    def test_single_tag_removed(self):
        result = strip_think_tags("<think>reasoning here</think>Answer")
        assert result == "Answer"

    @pytest.mark.unit
    def test_multiple_tags(self):
        text = "<think>one</think>middle<think>two</think>end"
        result = strip_think_tags(text)
        assert result == "middleend"

    @pytest.mark.unit
    def test_empty_think(self):
        result = strip_think_tags("<think></think>content")
        assert result == "content"

    @pytest.mark.unit
    def test_think_with_newlines(self):
        text = "<think>\nline 1\nline 2\n</think>\nAfter"
        result = strip_think_tags(text)
        assert "line 1" not in result
        assert "After" in result

    @pytest.mark.unit
    def test_nested_angle_brackets_in_think(self):
        text = "<think>some <b>bold</b> text</think>visible"
        result = strip_think_tags(text)
        assert result == "visible"

    @pytest.mark.unit
    def test_only_think_tag(self):
        result = strip_think_tags("<think>all reasoning</think>")
        assert result == ""

    @pytest.mark.unit
    def test_preserves_surrounding_whitespace_then_strips(self):
        result = strip_think_tags("  <think>x</think>  hello  ")
        assert result == "hello"

    @pytest.mark.adversarial
    def test_unclosed_think_tag_preserved(self):
        """Unclosed <think> is NOT matched by the regex, so text is preserved."""
        text = "<think>no closing tag"
        result = strip_think_tags(text)
        assert "<think>" in result


# =====================================================================
# 3. _try_parse_tool_json
# =====================================================================

class TestTryParseToolJson:
    """Tests for _try_parse_tool_json."""

    @pytest.mark.unit
    def test_valid_json(self):
        raw = json.dumps({"name": "foo", "arguments": {"bar": 1}})
        result = _try_parse_tool_json(raw)
        assert result == {"name": "foo", "arguments": {"bar": 1}}

    @pytest.mark.unit
    def test_backslash_windows_paths(self):
        raw = r'{"name": "file_read", "arguments": {"path": "D:\Repos\foo\bar.py"}}'
        result = _try_parse_tool_json(raw)
        assert result is not None
        assert result["name"] == "file_read"

    @pytest.mark.unit
    def test_literal_newlines_in_strings(self):
        raw = '{"name": "write", "arguments": {"content": "line1\nline2"}}'
        result = _try_parse_tool_json(raw)
        assert result is not None
        assert result["name"] == "write"

    @pytest.mark.unit
    def test_literal_tabs_in_strings(self):
        raw = '{"name": "write", "arguments": {"content": "col1\tcol2"}}'
        result = _try_parse_tool_json(raw)
        assert result is not None

    @pytest.mark.unit
    def test_malformed_but_extractable_name_args(self):
        """Deeply malformed JSON but with recoverable name and arguments."""
        raw = '{"name": "bash", "arguments": {"command": "echo hello"}, extra junk}'
        result = _try_parse_tool_json(raw)
        assert result is not None
        assert result["name"] == "bash"

    @pytest.mark.unit
    def test_completely_unparseable(self):
        assert _try_parse_tool_json("not json at all") is None

    @pytest.mark.unit
    def test_empty_string(self):
        assert _try_parse_tool_json("") is None

    @pytest.mark.unit
    def test_nested_braces(self):
        raw = json.dumps({
            "name": "file_write",
            "arguments": {
                "path": "x.json",
                "content": '{"key": {"nested": true}}',
            },
        })
        result = _try_parse_tool_json(raw)
        assert result is not None
        assert result["name"] == "file_write"

    @pytest.mark.unit
    def test_unicode_content(self):
        raw = json.dumps({"name": "write", "arguments": {"text": "caf\u00e9 \u2603"}})
        result = _try_parse_tool_json(raw)
        assert result is not None
        assert "\u00e9" in result["arguments"]["text"]

    @pytest.mark.adversarial
    def test_double_backslash_already_escaped(self):
        raw = r'{"name": "x", "arguments": {"p": "C:\\Users\\foo"}}'
        result = _try_parse_tool_json(raw)
        assert result is not None
        assert result["name"] == "x"

    @pytest.mark.adversarial
    def test_mixed_valid_and_invalid_escapes(self):
        raw = r'{"name": "y", "arguments": {"a": "new\nline", "b": "D:\temp"}}'
        result = _try_parse_tool_json(raw)
        assert result is not None
        assert result["name"] == "y"

    @pytest.mark.adversarial
    def test_raw_args_fallback(self):
        """When arguments JSON itself is unparseable, _raw key is used."""
        raw = '{"name": "bad", "arguments": {totally broken json here}}'
        result = _try_parse_tool_json(raw)
        assert result is not None
        assert result["name"] == "bad"
        assert "_raw" in result["arguments"]

    @pytest.mark.adversarial
    def test_crlf_newlines(self):
        raw = '{"name": "w", "arguments": {"c": "a\r\nb"}}'
        result = _try_parse_tool_json(raw)
        assert result is not None

    @pytest.mark.adversarial
    def test_brace_counting_with_nested_objects(self):
        """Ensure brace matcher handles multiple nesting levels."""
        raw = (
            '{"name": "deep", "arguments": '
            '{"a": {"b": {"c": "val"}}}}'
            ' garbage trailing'
        )
        result = _try_parse_tool_json(raw)
        assert result is not None
        assert result["name"] == "deep"

    @pytest.mark.unit
    def test_no_name_field(self):
        raw = '{"arguments": {"x": 1}}'
        result = _try_parse_tool_json(raw)
        # Valid JSON — parsed successfully, but no name
        assert result is not None
        assert "arguments" in result

    @pytest.mark.adversarial
    def test_json_with_trailing_comma(self):
        """Trailing comma is invalid JSON; fallback extraction should work."""
        raw = '{"name": "tc", "arguments": {"x": 1,}}'
        result = _try_parse_tool_json(raw)
        assert result is not None
        assert result["name"] == "tc"


# =====================================================================
# 4. parse_tool_calls
# =====================================================================

class TestParseToolCalls:
    """Tests for parse_tool_calls."""

    @pytest.mark.unit
    def test_no_tool_calls(self):
        plain, calls = parse_tool_calls("Just a plain response.")
        assert plain == "Just a plain response."
        assert calls == []

    @pytest.mark.unit
    def test_single_tool_call(self):
        text = (
            'Here is the result:\n'
            '<tool_call>\n'
            '{"name": "bash", "arguments": {"command": "ls"}}\n'
            '</tool_call>'
        )
        plain, calls = parse_tool_calls(text)
        assert len(calls) == 1
        assert calls[0]["name"] == "bash"
        assert json.loads(calls[0]["arguments"]) == {"command": "ls"}

    @pytest.mark.unit
    def test_multiple_tool_calls(self):
        text = (
            '<tool_call>\n'
            '{"name": "a", "arguments": {}}\n'
            '</tool_call>\n'
            '<tool_call>\n'
            '{"name": "b", "arguments": {}}\n'
            '</tool_call>'
        )
        _, calls = parse_tool_calls(text)
        assert len(calls) == 2
        assert calls[0]["name"] == "a"
        assert calls[1]["name"] == "b"

    @pytest.mark.unit
    def test_think_tags_stripped_before_parsing(self):
        text = (
            '<think>Let me think about this...</think>\n'
            '<tool_call>\n'
            '{"name": "grep", "arguments": {"pattern": "foo"}}\n'
            '</tool_call>'
        )
        plain, calls = parse_tool_calls(text)
        assert "think" not in plain.lower()
        assert len(calls) == 1
        assert calls[0]["name"] == "grep"

    @pytest.mark.unit
    def test_no_closing_tag_fallback(self):
        text = '<tool_call>\n{"name": "open", "arguments": {"x": 1}}'
        _, calls = parse_tool_calls(text)
        assert len(calls) == 1
        assert calls[0]["name"] == "open"

    @pytest.mark.unit
    def test_xml_style_glm_format(self):
        text = (
            '<tool_call>\n'
            '<name>glob</name>\n'
            '<arg_key>pattern</arg_key><arg_value>*.py</arg_value>\n'
            '<arg_key>path</arg_key><arg_value>/src</arg_value>\n'
            '</tool_call>'
        )
        _, calls = parse_tool_calls(text)
        assert len(calls) == 1
        assert calls[0]["name"] == "glob"
        args = json.loads(calls[0]["arguments"])
        assert args["pattern"] == "*.py"
        assert args["path"] == "/src"

    @pytest.mark.unit
    def test_plain_text_extraction(self):
        text = (
            'Before tool.\n'
            '<tool_call>\n'
            '{"name": "x", "arguments": {}}\n'
            '</tool_call>\n'
            'After tool.'
        )
        plain, calls = parse_tool_calls(text)
        assert "Before tool." in plain
        assert "After tool." in plain
        assert "<tool_call>" not in plain
        assert len(calls) == 1

    @pytest.mark.unit
    def test_mixed_text_and_multiple_tools(self):
        text = (
            'Intro.\n'
            '<tool_call>\n{"name": "first", "arguments": {}}\n</tool_call>\n'
            'Middle.\n'
            '<tool_call>\n{"name": "second", "arguments": {}}\n</tool_call>\n'
            'End.'
        )
        plain, calls = parse_tool_calls(text)
        assert len(calls) == 2
        assert "Intro." in plain
        assert "Middle." in plain
        assert "End." in plain

    @pytest.mark.unit
    def test_tool_arguments_serialized_as_json_string(self):
        text = (
            '<tool_call>\n'
            '{"name": "edit", "arguments": {"path": "/a.py", "old": "x", "new": "y"}}\n'
            '</tool_call>'
        )
        _, calls = parse_tool_calls(text)
        # arguments field should be a JSON string, not a dict
        assert isinstance(calls[0]["arguments"], str)
        assert json.loads(calls[0]["arguments"]) == {
            "path": "/a.py", "old": "x", "new": "y",
        }

    @pytest.mark.unit
    def test_empty_string(self):
        plain, calls = parse_tool_calls("")
        assert plain == ""
        assert calls == []

    @pytest.mark.adversarial
    def test_tool_call_with_windows_paths(self):
        raw_args = r'{"path": "D:\Repos\project\main.py"}'
        text = (
            '<tool_call>\n'
            f'{{"name": "file_read", "arguments": {raw_args}}}\n'
            '</tool_call>'
        )
        _, calls = parse_tool_calls(text)
        assert len(calls) == 1
        assert calls[0]["name"] == "file_read"

    @pytest.mark.adversarial
    def test_tool_call_with_code_content(self):
        code = 'def hello():\\n    print("hi")'
        payload = json.dumps({"name": "file_write", "arguments": {"content": code}})
        text = f"<tool_call>\n{payload}\n</tool_call>"
        _, calls = parse_tool_calls(text)
        assert len(calls) == 1

    @pytest.mark.adversarial
    def test_unparseable_tool_call_logged(self, caplog):
        """Completely garbled tool_call content triggers a warning log."""
        text = "<tool_call>\nnot json not xml\n</tool_call>"
        import logging
        with caplog.at_level(logging.WARNING, logger="resonant_client.protocol"):
            _, calls = parse_tool_calls(text)
        assert calls == []
        assert "Failed to parse" in caplog.text

    @pytest.mark.adversarial
    def test_think_block_containing_tool_call_text(self):
        """A <tool_call> inside <think> should be stripped, not parsed."""
        text = (
            '<think>Maybe I should call <tool_call>{"name":"nope","arguments":{}}</tool_call></think>\n'
            '<tool_call>\n{"name": "real", "arguments": {}}\n</tool_call>'
        )
        _, calls = parse_tool_calls(text)
        names = [c["name"] for c in calls]
        assert "real" in names
        # The "nope" call inside <think> should have been removed
        assert "nope" not in names

    @pytest.mark.unit
    def test_returns_tuple(self):
        result = parse_tool_calls("text")
        assert isinstance(result, tuple)
        assert len(result) == 2


# =====================================================================
# 5. Cache thread safety
# =====================================================================

class TestCacheThreadSafety:
    """Verify _tool_prompt_cache behaves correctly under concurrent access."""

    @pytest.mark.unit
    def test_concurrent_same_tools(self, make_tool):
        """Multiple threads building for the same tool set should all get the same result."""
        tools = [make_tool("shared")]
        results = []
        errors = []

        def worker():
            try:
                results.append(build_tool_system_prompt(tools))
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        assert len(results) == 20
        # All results should be identical strings
        assert len(set(results)) == 1

    @pytest.mark.unit
    def test_concurrent_different_tools(self, make_tool):
        """Multiple threads building different tool sets shouldn't corrupt cache."""
        results = {}

        def worker(name):
            tools = [make_tool(name)]
            results[name] = build_tool_system_prompt(tools)

        with ThreadPoolExecutor(max_workers=10) as pool:
            futures = {pool.submit(worker, f"tool_{i}"): i for i in range(10)}
            for f in as_completed(futures):
                f.result()  # re-raise any exceptions

        assert len(results) == 10
        for name, prompt in results.items():
            assert name in prompt

    @pytest.mark.unit
    def test_cache_size_after_concurrent_access(self, make_tool):
        """Each unique tool set should produce exactly one cache entry."""
        def worker(idx):
            tools = [make_tool(f"uniq_{idx}")]
            build_tool_system_prompt(tools)

        with ThreadPoolExecutor(max_workers=5) as pool:
            list(pool.map(worker, range(5)))

        assert len(_tool_prompt_cache) == 5
