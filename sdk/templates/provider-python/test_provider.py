"""Tests for the provider, run as Lumi runs it: ``python -m pytest`` in this folder."""

import sys
from pathlib import Path

from lumi_extension.testing import call, check_models, check_stream

HERE = Path(__file__).resolve().parent
PROVIDER = [sys.executable, "provider.py"]
READ_FILE = {"name": "read_file", "description": "Read a file", "parameters": {"type": "object"}}


def stream(messages, model="echo", tools=()):
    return call(PROVIDER, "stream", {"model": model, "messages": messages, "tools": list(tools)}, cwd=HERE)


def test_it_lists_its_models():
    events = call(PROVIDER, "models", cwd=HERE)
    assert check_models(events) == []
    assert [model["id"] for model in events[0]["models"]] == ["echo", "remote"]


def test_echo_answers():
    events = stream([{"role": "user", "content": "hello"}])
    assert check_stream(events) == []
    assert events[0] == {"type": "text", "text": "You said: hello"}


def test_echo_calls_a_tool_and_reads_its_answer():
    events = stream([{"role": "user", "content": 'call read_file {"path": "README.md"}'}], tools=[READ_FILE])
    assert check_stream(events) == []
    assert events[0]["type"] == "tool_call"
    assert (events[0]["name"], events[0]["arguments"]) == ("read_file", {"path": "README.md"})

    events = stream([
        {"role": "user", "content": 'call read_file {"path": "README.md"}'},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "name": "read_file", "arguments": {"path": "README.md"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "# My project"},
    ], tools=[READ_FILE])
    assert events[0] == {"type": "text", "text": "The tool answered: # My project"}


def test_unknown_models_are_refused():
    events = stream([{"role": "user", "content": "hi"}], model="nope")
    assert check_stream(events) == []
    assert events[-1]["type"] == "error"


def test_remote_sends_chat_completions_messages():
    from provider import to_openai

    converted = to_openai([
        {"role": "system", "content": "Be brief."},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "name": "read_file", "arguments": {"path": "a"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "text"},
    ])
    assert converted[1]["tool_calls"][0]["function"] == {"name": "read_file", "arguments": '{"path": "a"}'}
    assert converted[2] == {"role": "tool", "tool_call_id": "c1", "content": "text"}
