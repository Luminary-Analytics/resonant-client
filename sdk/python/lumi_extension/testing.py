"""The test kit: run an extension's provider as Lumi does, and check what it writes.

::

    from lumi_extension.testing import call, check_stream

    def test_it_answers():
        events = call(["python", "provider.py"], "stream", {
            "model": "acme-1", "messages": [{"role": "user", "content": "hi"}], "tools": []}, cwd=PACK)
        assert check_stream(events) == []
        assert events[-1]["type"] == "done"
"""

from __future__ import annotations

import json
import os
import subprocess

from . import PROTOCOL

EVENT_TYPES = {"text", "tool_call", "done", "error"}


def call(command: list[str], method: str, params: dict | None = None, *, cwd: str = ".", api_key: str = "",
         env: dict | None = None, timeout: float = 60) -> list[dict]:
    """Start the provider like Lumi, send one request, and return every JSON object it wrote."""
    environment = {**os.environ, **(env or {}), "LUMI_EXTENSION_PROTOCOL": str(PROTOCOL)}
    if api_key:
        environment["LUMI_PROVIDER_API_KEY"] = api_key
    request = {"lumi_extension": PROTOCOL, "method": method, **({"params": params} if params is not None else {})}
    result = subprocess.run(command, cwd=cwd, env=environment, input=json.dumps(request) + "\n", text=True,
                            encoding="utf-8", capture_output=True, timeout=timeout, check=False)
    events = []
    for line in result.stdout.splitlines():
        if line.strip():
            try:
                events.append(json.loads(line))
            except ValueError:
                raise AssertionError(f"The provider wrote a line that isn't JSON: {line[:200]!r}") from None
    if not events and result.returncode:
        raise AssertionError(f"The provider exited with {result.returncode}: {result.stderr[-600:]}")
    return events


def check_models(events: list[dict]) -> list[str]:
    """What's wrong with a ``models`` answer; [] when it's what Lumi expects."""
    if len(events) != 1 or events[0].get("type") != "models":
        return ["Answer 'models' with exactly one {\"type\": \"models\", \"models\": [...]} line."]
    problems = []
    for model in events[0].get("models") or []:
        if not isinstance(model, dict) or not model.get("id"):
            problems.append(f"Each model needs an id: {model!r}")
        elif "context_window" in model and not isinstance(model["context_window"], int):
            problems.append(f"{model['id']}: context_window must be a number of tokens.")
    return problems


def check_stream(events: list[dict]) -> list[str]:
    """What's wrong with a ``stream`` answer; [] when it's what Lumi expects."""
    problems = []
    if not events or events[-1].get("type") not in ("done", "error"):
        problems.append("End the answer with done() or error().")
    for index, event in enumerate(events):
        kind = event.get("type")
        if kind not in EVENT_TYPES:
            problems.append(f"Line {index + 1}: unknown type {kind!r}.")
        elif kind == "text" and not isinstance(event.get("text"), str):
            problems.append(f"Line {index + 1}: text must be a string.")
        elif kind == "tool_call" and (not event.get("name") or not isinstance(event.get("arguments", {}), dict)):
            problems.append(f"Line {index + 1}: a tool_call needs a name and arguments as an object.")
        elif kind in ("done", "error") and index != len(events) - 1:
            problems.append(f"Line {index + 1}: nothing may follow {kind}.")
    return problems
