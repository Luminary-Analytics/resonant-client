"""Tests for v0.5.13a1 — engine/session.py small-method coverage.

session.py is the biggest under-tested module in the project (62%
line coverage, 609 stmts, 229 missed). It's also the most central:
it's the agentic loop, tool dispatch, history management, and
event emission for every chat session.

Existing test files cover specific behaviors (auto-feedback, doom
loop, await-user, backend-swap, todos, ergonomics) but the cross-
cutting small public/private methods on Session — and the module-
level parser helpers — were largely untested. A regression in
parse_choices, _should_auto_approve, or copy_execution_context_from
would have shipped silently.

Coverage delta target on session.py: 62% → ~70% (small-method pass).
The big chunks inside `run()` and `_execute_task` are deferred to
v0.5.13a2+ since they require a much heavier stub harness.

Covered here:
- parse_choices (module-level): <choices> block parsing, no-match,
  bullet-stripped options, options with no leading bullet, empty.
- parse_markdown_todos (module-level): GitHub-checkbox extraction,
  done flag from `[x]` vs `[ ]`, returns None when no task lines.
- strip_tool_call_tags (module-level): removes <tool_call>...</tool_call>
  blocks (single-line + DOTALL multiline).
- Session.is_subagent: True when parent_session set; False otherwise.
- Session.tools: returns _allowed_tools when set, AGENT_TOOLS by
  default, AGENT_TOOLS+mcp_tools when MCP attached.
- Session.clear: empties conversation_history.
- Session.cancel / reset_cancel / cancel_requested: thread-safe flag
  toggle.
- Session._log_event: forwards to event_logger when set, no-op when
  None, swallows logger exceptions.
- Session._should_auto_approve: suggest tier (read-only only),
  auto-edit tier (no exec), full-auto (everything).
- Session._cancelled_events: yields ERROR + SESSION_END.
- Session.copy_execution_context_from: mirrors all the parent
  attributes the comment promises (project_path, sandbox, autonomy_
  tier, etc.) plus the event-logger fork pattern.
- Session.should_plan: COMPLEX classification → True; classify
  exception → False (defensive).
"""
from __future__ import annotations

import threading


from resonant_client.engine.session import (
    Session,
    parse_choices,
    parse_markdown_todos,
    strip_tool_call_tags,
)


# ── Stub backend ───────────────────────────────────────────────────────


class _StubBackend:
    """Minimal duck-typed backend for Session construction."""

    def __init__(self, name: str = "ollama", model: str = "deepseek-v4-flash:cloud"):
        self.name = name
        self.model = model
        self.base_url = "http://10.0.0.133:11434"
        self.api_key = None
        self._classify_response = "SIMPLE"
        self._classify_should_raise = False

    def classify(self, prompt: str, max_tokens: int = 20) -> str:
        if self._classify_should_raise:
            raise RuntimeError("classify failed")
        return self._classify_response


# ── Module-level parser helpers ────────────────────────────────────────


class TestParseChoices:
    def test_no_choices_block_returns_text_and_nones(self):
        result = parse_choices("just regular text")
        assert result == ("just regular text", None, None)

    def test_extracts_bullet_options(self):
        text = "Pick one:\n<choices>\n- option a\n- option b\n</choices>\nEnd."
        before, options, after = parse_choices(text)
        assert before == "Pick one:"
        assert options == ["option a", "option b"]
        assert after == "End."

    def test_strips_asterisk_bullets(self):
        text = "<choices>\n* foo\n* bar\n</choices>"
        _, options, _ = parse_choices(text)
        assert options == ["foo", "bar"]

    def test_handles_lines_without_bullets(self):
        # Lines without `- ` or `* ` prefix are taken as-is.
        text = "<choices>\noption alpha\noption beta\n</choices>"
        _, options, _ = parse_choices(text)
        assert options == ["option alpha", "option beta"]

    def test_empty_choices_block_returns_no_options(self):
        text = "<choices>\n   \n</choices>"
        _, options, _ = parse_choices(text)
        # The "no options" branch returns (text, None, None) — falls
        # back to no-match shape.
        assert options is None

    def test_multiline_dotall_match(self):
        # The regex uses re.DOTALL so newlines inside the block are fine.
        text = "<choices>\n- a\n- b\n- c\n</choices>"
        _, options, _ = parse_choices(text)
        assert options == ["a", "b", "c"]


class TestParseMarkdownTodos:
    def test_returns_none_when_no_checkboxes(self):
        assert parse_markdown_todos("just text") is None

    def test_extracts_unchecked_items(self):
        text = "- [ ] todo one\n- [ ] todo two"
        items = parse_markdown_todos(text)
        assert items == [
            {"text": "todo one", "done": False},
            {"text": "todo two", "done": False},
        ]

    def test_done_flag_lowercase_and_uppercase_x(self):
        text = "- [x] lower\n- [X] upper"
        items = parse_markdown_todos(text)
        assert items[0]["done"] is True
        assert items[1]["done"] is True

    def test_supports_asterisk_bullets(self):
        text = "* [ ] async\n* [x] sync"
        items = parse_markdown_todos(text)
        assert len(items) == 2
        assert items[0]["text"] == "async"
        assert items[1]["done"] is True

    def test_ignores_lines_without_checkbox(self):
        text = "- regular bullet\n- [ ] real todo"
        items = parse_markdown_todos(text)
        assert items == [{"text": "real todo", "done": False}]


class TestStripToolCallTags:
    def test_removes_single_line_block(self):
        text = "before <tool_call>foo()</tool_call> after"
        assert strip_tool_call_tags(text) == "before  after"

    def test_removes_multiline_block(self):
        text = "before\n<tool_call>\nfoo(\n  bar=1\n)\n</tool_call>\nafter"
        # DOTALL so the block spans newlines.
        result = strip_tool_call_tags(text)
        assert "<tool_call>" not in result
        assert "</tool_call>" not in result
        assert "foo(" not in result
        assert "before" in result
        assert "after" in result

    def test_removes_multiple_blocks(self):
        text = "<tool_call>a</tool_call> mid <tool_call>b</tool_call>"
        result = strip_tool_call_tags(text)
        assert result == "mid"

    def test_no_op_when_no_blocks(self):
        assert strip_tool_call_tags("clean text") == "clean text"


# ── Session small-method coverage ──────────────────────────────────────


class TestSessionIsSubagent:
    def test_no_parent_is_not_subagent(self):
        s = Session(backend=_StubBackend())
        assert s.is_subagent is False

    def test_with_parent_is_subagent(self):
        parent = Session(backend=_StubBackend())
        child = Session(backend=_StubBackend(), parent_session=parent)
        assert child.is_subagent is True


class TestSessionTools:
    def test_default_returns_agent_tools(self):
        s = Session(backend=_StubBackend())
        from resonant_client.engine.tools import AGENT_TOOLS
        assert s.tools == AGENT_TOOLS

    def test_allowed_tools_overrides_default(self):
        custom = [{"function": {"name": "fake_tool"}}]
        s = Session(backend=_StubBackend(), allowed_tools=custom)
        assert s.tools == custom

    def test_mcp_tools_appended_when_set(self):
        s = Session(backend=_StubBackend())
        from resonant_client.engine.tools import AGENT_TOOLS
        s.mcp_tools = [{"function": {"name": "mcp_thing"}}]
        result = s.tools
        assert result[: len(AGENT_TOOLS)] == AGENT_TOOLS
        assert result[-1]["function"]["name"] == "mcp_thing"

    def test_builtin_definition_wins_an_unprefixed_mcp_name_collision(self):
        s = Session(backend=_StubBackend())
        duplicate = {
            "type": "function",
            "function": {"name": "browser_click", "description": "MCP duplicate"},
        }
        s.mcp_tools = [duplicate]

        matches = [
            tool for tool in s.tools
            if tool.get("function", {}).get("name") == "browser_click"
        ]

        assert len(matches) == 1
        assert matches[0]["function"]["description"] != "MCP duplicate"

    def test_allowed_tools_wins_over_mcp_when_both_set(self):
        # _allowed_tools=None means "use AGENT_TOOLS"; if explicitly
        # set, MCP tools are ignored.
        custom = [{"function": {"name": "fake_tool"}}]
        s = Session(backend=_StubBackend(), allowed_tools=custom)
        s.mcp_tools = [{"function": {"name": "mcp_thing"}}]
        assert s.tools == custom


class TestSessionClear:
    def test_clear_empties_history(self):
        s = Session(backend=_StubBackend())
        s.conversation_history.append({"role": "user", "content": "hi"})
        s.conversation_history.append({"role": "assistant", "content": "yo"})
        assert len(s.conversation_history) == 2
        s.clear()
        assert s.conversation_history == []


class TestSessionCancel:
    def test_starts_not_cancelled(self):
        s = Session(backend=_StubBackend())
        assert s.cancel_requested is False

    def test_cancel_sets_flag(self):
        s = Session(backend=_StubBackend())
        s.cancel()
        assert s.cancel_requested is True

    def test_reset_cancel_clears_flag(self):
        s = Session(backend=_StubBackend())
        s.cancel()
        s.reset_cancel()
        assert s.cancel_requested is False

    def test_external_cancel_event_shared(self):
        # When a cancel_event is passed at construction, external code
        # can flip the flag without holding the Session reference.
        ev = threading.Event()
        s = Session(backend=_StubBackend(), cancel_event=ev)
        assert s.cancel_requested is False
        ev.set()
        assert s.cancel_requested is True


class TestSessionLogEvent:
    def test_no_op_when_logger_none(self):
        s = Session(backend=_StubBackend())
        # Default: event_logger is None.
        assert s.event_logger is None
        s._log_event({"event": "x"})  # must not raise

    def test_forwards_to_logger_when_set(self):
        s = Session(backend=_StubBackend())
        captured = []

        class _StubLogger:
            def log(self, event):
                captured.append(event)

        s.event_logger = _StubLogger()
        s._log_event({"event": "x"})
        assert captured == [{"event": "x"}]

    def test_swallows_logger_exceptions(self):
        s = Session(backend=_StubBackend())

        class _BadLogger:
            def log(self, event):
                raise RuntimeError("disk full")

        s.event_logger = _BadLogger()
        s._log_event({"event": "x"})  # must not raise


class TestSessionShouldAutoApprove:
    def test_suggest_tier_only_read_only_tools(self):
        s = Session(backend=_StubBackend(), auto_approve=False)
        # auto_approve=False maps to autonomy_tier="suggest" per __init__.
        assert s.autonomy_tier == "suggest"
        # file_read is read-only → auto-approved at suggest tier.
        assert s._should_auto_approve("file_read") is True
        # file_write needs approval at suggest tier.
        assert s._should_auto_approve("file_write") is False
        # bash certainly needs approval at suggest tier.
        assert s._should_auto_approve("bash") is False

    def test_auto_edit_tier_files_ok_exec_not(self):
        s = Session(backend=_StubBackend())
        s.autonomy_tier = "auto-edit"
        # File tools approved.
        assert s._should_auto_approve("file_write") is True
        assert s._should_auto_approve("file_edit") is True
        # Exec tools NOT approved.
        assert s._should_auto_approve("bash") is False

    def test_full_auto_approves_everything(self):
        s = Session(backend=_StubBackend(), auto_approve=True)
        assert s.autonomy_tier == "full-auto"
        assert s._should_auto_approve("file_read") is True
        assert s._should_auto_approve("file_write") is True
        assert s._should_auto_approve("bash") is True
        assert s._should_auto_approve("anything_at_all") is True


class TestSessionCancelledEvents:
    def test_yields_error_then_session_end(self):
        s = Session(backend=_StubBackend())
        events = list(s._cancelled_events(total_start=100.0, total_steps=3))
        assert len(events) == 2
        assert events[0]["event"] == "error"
        assert "Interrupted" in events[0]["message"]
        # Second is SESSION_END with elapsed + steps.
        assert events[1]["event"] == "session.end"
        assert events[1]["total_steps"] == 3
        assert events[1]["total_elapsed"] >= 0


class TestSessionShouldPlan:
    def test_complex_returns_true(self):
        backend = _StubBackend()
        backend._classify_response = "COMPLEX: needs planning"
        s = Session(backend=backend)
        assert s.should_plan("write a complete app") is True

    def test_simple_returns_false(self):
        backend = _StubBackend()
        backend._classify_response = "SIMPLE"
        s = Session(backend=backend)
        assert s.should_plan("hi") is False

    def test_classify_exception_returns_false(self):
        # Defensive: if the classifier crashes, default to "no plan
        # needed" rather than blocking the session.
        backend = _StubBackend()
        backend._classify_should_raise = True
        s = Session(backend=backend)
        assert s.should_plan("anything") is False

    def test_case_insensitive_complex_detection(self):
        backend = _StubBackend()
        backend._classify_response = "complex"  # lowercase
        s = Session(backend=backend)
        # The check uppercases the response then looks for "COMPLEX".
        assert s.should_plan("anything") is True


class TestSessionSetBackend:
    def test_swap_preserves_history_by_default(self):
        s = Session(backend=_StubBackend(name="ollama"))
        s.conversation_history.append({"role": "user", "content": "hi"})
        new_backend = _StubBackend(name="ollama2")
        s.set_backend(new_backend)
        assert s.backend is new_backend
        # History preserved by default — the bug-fix in v0.x that the
        # docstring describes.
        assert len(s.conversation_history) == 1

    def test_reset_history_clears_when_requested(self):
        s = Session(backend=_StubBackend())
        s.conversation_history.append({"role": "user", "content": "hi"})
        s.set_backend(_StubBackend(), reset_history=True)
        assert s.conversation_history == []


class TestSessionCopyExecutionContext:
    def test_mirrors_parent_attributes(self):
        parent = Session(backend=_StubBackend())
        parent.project_path = "/projects/foo"
        parent.autonomy_tier = "auto-edit"
        parent.project_instructions = "the rules"
        parent.mcp_tools = [{"function": {"name": "mcp_a"}}]

        # Sandbox + execution_policy + hook_runner are stub objects;
        # we just need them to round-trip identity.
        sentinel_sandbox = object()
        sentinel_policy = object()
        sentinel_hook = object()
        parent.sandbox = sentinel_sandbox
        parent.execution_policy = sentinel_policy
        parent.hook_runner = sentinel_hook

        child = Session(backend=_StubBackend(), parent_session=parent)
        child.copy_execution_context_from(parent)

        assert child.project_path == "/projects/foo"
        assert child.autonomy_tier == "auto-edit"
        assert child.project_instructions == "the rules"
        assert child.mcp_tools == parent.mcp_tools
        assert child.sandbox is sentinel_sandbox
        assert child.execution_policy is sentinel_policy
        assert child.hook_runner is sentinel_hook

    def test_event_logger_disabled_parent_passes_through(self):
        # Parent has an event_logger that's disabled — child should
        # just inherit it as-is (no fork attempt).
        parent = Session(backend=_StubBackend())
        class _DisabledLogger:
            enabled = False
        parent.event_logger = _DisabledLogger()

        child = Session(backend=_StubBackend(), parent_session=parent)
        child.copy_execution_context_from(parent)
        assert child.event_logger is parent.event_logger

    def test_event_logger_none_passes_through(self):
        parent = Session(backend=_StubBackend())
        parent.event_logger = None
        child = Session(backend=_StubBackend(), parent_session=parent)
        child.copy_execution_context_from(parent)
        assert child.event_logger is None

    def test_event_logger_enabled_parent_forks_to_child_logger(self, tmp_path):
        # When the parent has an enabled logger, the child gets a
        # FRESH EventLogger (not the same instance) with its own
        # session_id, so child events don't pollute the parent's log
        # file.
        from resonant_client.engine.event_log import EventLogger
        parent = Session(backend=_StubBackend())
        parent.event_logger = EventLogger(
            log_dir=tmp_path, session_id="parent", enabled=True,
        )
        try:
            child = Session(backend=_StubBackend(), parent_session=parent)
            child.copy_execution_context_from(parent)
            # Child gets a NEW logger instance (different session_id).
            assert child.event_logger is not parent.event_logger
            assert child.event_logger is not None
            assert child.event_logger.session_id != "parent"
        finally:
            parent.event_logger.close()
            if child.event_logger:
                child.event_logger.close()
