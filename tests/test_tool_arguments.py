"""Tool-argument decoding, coercion, validation, and repair-loop tests."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from resonant_client.engine.session import Session
from resonant_client.engine.tool_arguments import (
    ToolArgumentError,
    normalize_tool_arguments,
)
from resonant_client.engine.tools import AGENT_TOOLS
from tests.streaming_stub import StreamingBackend, done, text_delta, tool_call


def test_double_encoded_glm_arguments_are_unwrapped():
    raw = json.dumps(json.dumps({"path": "src/app.py"}))

    result = normalize_tool_arguments("file_read", raw, AGENT_TOOLS)

    assert result == {"path": "src/app.py"}


def test_stringified_nested_object_and_array_are_coerced():
    definitions = [{
        "type": "function",
        "function": {
            "name": "custom",
            "parameters": {
                "type": "object",
                "properties": {
                    "config": {"type": "object"},
                    "items": {"type": "array"},
                },
                "required": ["config", "items"],
            },
        },
    }]

    result = normalize_tool_arguments(
        "custom",
        {"config": '{"enabled": true}', "items": '["a", "b"]'},
        definitions,
    )

    assert result == {"config": {"enabled": True}, "items": ["a", "b"]}


def test_safe_primitive_strings_are_coerced_to_declared_types():
    definitions = [{
        "name": "custom",
        "parameters": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer"},
                "enabled": {"type": "boolean"},
            },
            "required": ["limit", "enabled"],
        },
    }]

    result = normalize_tool_arguments(
        "custom",
        {"limit": "20", "enabled": "false"},
        definitions,
    )

    assert result == {"limit": 20, "enabled": False}


def test_missing_required_argument_includes_repair_example():
    with pytest.raises(ToolArgumentError) as raised:
        normalize_tool_arguments("file_read", {}, AGENT_TOOLS)

    message = str(raised.value)
    assert "missing required argument(s): path" in message
    assert 'expected arguments like {"path": "value"}' in message


def test_wrong_argument_type_is_rejected_before_execution():
    with pytest.raises(ToolArgumentError, match="argument 'path' must be string"):
        normalize_tool_arguments("file_read", {"path": ["src/app.py"]}, AGENT_TOOLS)


def test_session_returns_targeted_repair_without_executing_tool():
    backend = StreamingBackend(scripts=[
        [tool_call("file_read", '{"path":'), done()],
        [text_delta("recovered"), done()],
    ])
    session = Session(backend=backend, max_steps=3, auto_approve=True)

    with patch("resonant_client.engine.session.execute_tool") as execute:
        events = list(session.run("read it"))

    execute.assert_not_called()
    result = next(event for event in events if event.get("event") == "tool.result")
    assert result["is_error"] is True
    assert "arguments are not valid JSON" in result["output"]
    assert "expected arguments like" in result["output"]
