"""
Audit of DeepSeek-shaped tool-call output against `protocol.parse_tool_calls`.

Why this file exists: DeepSeek-V4 family models (flash + pro on Ollama)
emit `<tool_call>{json}</tool_call>` blocks that mostly match the
OpenAI-style schema, but with a handful of consistent quirks:

  * literal newlines / tabs in `bash` command strings
  * Windows backslash escaping in file paths
  * occasional triple-backtick fences wrapping the tool call (the
    model is trying to be "helpful" with markdown formatting)
  * trailing text after the tool call (commentary the model adds)

Each test below either pins a passing case (so a parser refactor
can't silently break it) or documents a known-tricky case. Failing
tests get FIXED — the test file is the spec, not a wishlist.

If a future DeepSeek-V5 / V6 introduces a new quirk, add a case
here and update the parser. The other agents in the orchestration
graph rely on robust tool-call parsing.
"""

from __future__ import annotations

import json

import pytest

from resonant_client.protocol import parse_tool_calls


# ── Sanity baseline ────────────────────────────────────────────────────


class TestBasicToolCall:
    def test_minimal_well_formed(self):
        text = '<tool_call>{"name":"file_read","arguments":{"path":"src/main.py"}}</tool_call>'
        plain, calls = parse_tool_calls(text)
        assert len(calls) == 1
        assert calls[0]["name"] == "file_read"
        args = json.loads(calls[0]["arguments"])
        assert args["path"] == "src/main.py"

    def test_two_consecutive_calls(self):
        text = (
            '<tool_call>{"name":"glob","arguments":{"pattern":"*.py"}}</tool_call>'
            '<tool_call>{"name":"glob","arguments":{"pattern":"*.md"}}</tool_call>'
        )
        plain, calls = parse_tool_calls(text)
        assert len(calls) == 2
        assert {json.loads(c["arguments"])["pattern"] for c in calls} == {"*.py", "*.md"}


# ── DeepSeek bash-command quirks ───────────────────────────────────────


class TestBashCommandQuirks:
    def test_literal_newline_in_bash_command(self):
        # DeepSeek flash sometimes emits commands with raw \n inside
        # the JSON string instead of escaped \\n. The parser's stage-2
        # fix (replace literal newlines with \\n) should rescue this.
        text = (
            '<tool_call>{"name":"bash","arguments":{"command":"echo line1\n'
            'line2\nline3"}}</tool_call>'
        )
        plain, calls = parse_tool_calls(text)
        assert len(calls) == 1
        args = json.loads(calls[0]["arguments"])
        # The command should be reconstructed with newlines preserved.
        assert "line1" in args["command"]
        assert "line2" in args["command"]
        assert "line3" in args["command"]

    def test_tab_in_bash_command(self):
        text = (
            '<tool_call>{"name":"bash","arguments":{"command":"if true; then\t'
            'echo hi\nfi"}}</tool_call>'
        )
        plain, calls = parse_tool_calls(text)
        assert len(calls) == 1
        args = json.loads(calls[0]["arguments"])
        assert "echo hi" in args["command"]

    def test_carriage_return_line_ending(self):
        # Windows CRLF inside a bash command string.
        text = (
            '<tool_call>{"name":"bash","arguments":{"command":"echo a\r\necho b"}}</tool_call>'
        )
        plain, calls = parse_tool_calls(text)
        assert len(calls) == 1
        args = json.loads(calls[0]["arguments"])
        assert "echo a" in args["command"]
        assert "echo b" in args["command"]


# ── Path / backslash escaping ───────────────────────────────────────────


class TestWindowsPaths:
    def test_properly_escaped_windows_path(self):
        # Well-behaved DeepSeek output — backslashes already doubled.
        # Use a raw Python string to avoid Python's own escape pass.
        text = r'<tool_call>{"name":"file_read","arguments":{"path":"C:\\Users\\rich\\file.py"}}</tool_call>'
        plain, calls = parse_tool_calls(text)
        assert len(calls) == 1
        args = json.loads(calls[0]["arguments"])
        assert args["path"] == "C:\\Users\\rich\\file.py"

    def test_unescaped_windows_path_with_invalid_escape(self):
        # DeepSeek occasionally emits raw single-backslash paths inside
        # JSON. `\U` and `\r` look like JSON escape sequences but
        # aren't valid here. The parser's stage-1 fix (escape raw
        # backslashes that aren't part of a valid JSON escape) should
        # rescue this. Use raw string + manual concat to dodge Python.
        text = (
            '<tool_call>{"name":"file_read","arguments":{"path":"C:'
            '\\' + 'Users' + '\\' + 'rich' + '\\' + 'file.py"}}</tool_call>'
        )
        plain, calls = parse_tool_calls(text)
        assert len(calls) == 1
        args_dict = json.loads(calls[0]["arguments"])
        # We don't pin the exact path string (escape recovery may
        # produce slightly different forms across stages); we just
        # need the parser not to drop the call.
        assert args_dict.get("path", "").lower().startswith("c:")


# ── Surrounding context ────────────────────────────────────────────────


class TestSurroundingText:
    def test_text_before_tool_call(self):
        text = (
            'I will read the file first.\n\n'
            '<tool_call>{"name":"file_read","arguments":{"path":"x"}}</tool_call>'
        )
        plain, calls = parse_tool_calls(text)
        assert len(calls) == 1
        assert "read the file first" in plain

    def test_text_after_tool_call(self):
        text = (
            '<tool_call>{"name":"glob","arguments":{"pattern":"*.py"}}</tool_call>\n\n'
            'Then I will inspect the matches.'
        )
        plain, calls = parse_tool_calls(text)
        assert len(calls) == 1
        assert "inspect the matches" in plain

    def test_text_between_two_tool_calls(self):
        text = (
            '<tool_call>{"name":"glob","arguments":{"pattern":"*.py"}}</tool_call>\n'
            'Now grep for the function:\n'
            '<tool_call>{"name":"grep","arguments":{"pattern":"def main"}}</tool_call>'
        )
        plain, calls = parse_tool_calls(text)
        assert len(calls) == 2
        # Inter-call commentary should land in plain text.
        assert "grep for the function" in plain


# ── Markdown / formatting drift ────────────────────────────────────────


class TestMarkdownDrift:
    def test_tool_call_inside_triple_backticks(self):
        # DeepSeek sometimes wraps the tool call in a code fence as if
        # it's "showing the user what it's doing." The parser strips
        # the fence content cleanly because the regex looks for
        # `<tool_call>` markers regardless of surrounding fences.
        text = (
            "```tool_call\n"
            '<tool_call>{"name":"glob","arguments":{"pattern":"**/*.py"}}</tool_call>\n'
            "```"
        )
        plain, calls = parse_tool_calls(text)
        assert len(calls) == 1
        args = json.loads(calls[0]["arguments"])
        assert args["pattern"] == "**/*.py"

    def test_unclosed_tool_call_falls_back_to_open_pattern(self):
        # GLM-4 famously omits the closing tag; DeepSeek occasionally
        # truncates mid-stream. The parser has an open-pattern fallback.
        text = '<tool_call>{"name":"glob","arguments":{"pattern":"*.py"}}'
        plain, calls = parse_tool_calls(text)
        assert len(calls) == 1
        assert calls[0]["name"] == "glob"


# ── Think-tag interaction ──────────────────────────────────────────────


class TestThinkTagInteraction:
    def test_think_block_before_tool_call(self):
        # DeepSeek-V4 supports thinking. The parser strips <think>
        # blocks before parsing so chain-of-thought doesn't leak.
        text = (
            '<think>Let me check the file structure first.</think>\n'
            '<tool_call>{"name":"glob","arguments":{"pattern":"*.py"}}</tool_call>'
        )
        plain, calls = parse_tool_calls(text)
        assert len(calls) == 1
        # The thinking should be stripped from plain text too.
        assert "<think>" not in plain
        assert "check the file structure" not in plain


# ── Args-shape variations ──────────────────────────────────────────────


class TestArgsShapes:
    def test_args_with_nested_object(self):
        text = (
            '<tool_call>{"name":"task","arguments":{"prompt":"Build the auth flow","agent_type":"build"}}</tool_call>'
        )
        plain, calls = parse_tool_calls(text)
        assert len(calls) == 1
        args = json.loads(calls[0]["arguments"])
        assert args["agent_type"] == "build"

    def test_args_with_array(self):
        text = (
            '<tool_call>{"name":"await_user","arguments":{"question":"Pick one","options":["a","b","c"]}}</tool_call>'
        )
        plain, calls = parse_tool_calls(text)
        assert len(calls) == 1
        args = json.loads(calls[0]["arguments"])
        assert args["options"] == ["a", "b", "c"]

    def test_args_with_unicode(self):
        text = (
            '<tool_call>{"name":"bash","arguments":{"command":"echo café"}}</tool_call>'
        )
        plain, calls = parse_tool_calls(text)
        assert len(calls) == 1
        args = json.loads(calls[0]["arguments"])
        assert "caf" in args["command"]
