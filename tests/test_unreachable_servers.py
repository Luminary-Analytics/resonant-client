"""A model server on this computer that isn't running says where Lumi looked and what to do.

A stopped Ollama used to reach the conversation as the socket's own words
("[WinError 10061] No connection could be made because the target machine
actively refused it"). Now Ollama, EXO and a custom connection to this
computer say "Lumi couldn't reach <it> at <address>. Start <it> there, or
check Settings › Connections", as the Ollama card in Settings does; offline
mode's own reason still comes first.
"""

from __future__ import annotations

import socket
from unittest.mock import MagicMock, patch

import httpx
import pytest

from lumi import offline
from lumi.backends import ExoBackend, OllamaBackend, unreachable_message
from lumi.connections import OpenAICompatibleBackend, normalize_connection

REFUSED = "[WinError 10061] No connection could be made because the target machine actively refused it"
MODEL = "qwen3-coder:30b"


@pytest.fixture(autouse=True)
def ollama_state(monkeypatch):
    """No vision probe over the network, no retries' waits, and a circuit breaker of these tests' own."""
    monkeypatch.setitem(OllamaBackend._vision_support_cache, MODEL, False)
    monkeypatch.setattr(OllamaBackend, "_circuit", {})


def _errors(events) -> list[str]:
    return [data["message"] for kind, data in events if kind == "error"]


def _closed_port() -> int:
    """A port on this computer that nothing listens on (bound, then released)."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _ollama_stream(error: BaseException) -> list:
    backend = OllamaBackend("http://127.0.0.1:11434", MODEL)
    backend._use_native_tools = True
    backend._supervised_single_request = True  # one try: a timeout isn't retried after backoff waits
    with patch("httpx.Client") as client_class:
        client = MagicMock()
        client.stream = MagicMock(side_effect=error)
        client_class.return_value = client
        return list(backend.stream("Hi", [], "Be helpful", tools=[]))


def test_a_stopped_ollama_says_where_lumi_looked_and_what_to_do():
    [message] = _errors(_ollama_stream(httpx.ConnectError(REFUSED)))
    assert message == ("Lumi couldn't reach Ollama at http://127.0.0.1:11434. Start Ollama there, or check "
                       "Settings › Connections.")
    assert "WinError" not in message


def test_a_real_refused_connection_reads_the_same(monkeypatch):
    # No mock: the connection this computer refuses, as a stopped Ollama's is.
    address = f"http://127.0.0.1:{_closed_port()}"
    backend = OllamaBackend(address, MODEL)
    backend._use_native_tools = True
    [message] = _errors(backend.stream("Hi", [], "Be helpful", tools=[]))
    assert message == unreachable_message("Ollama", address)


def test_an_ollama_on_another_computer_that_never_answers_says_so_too():
    events = _ollama_stream(httpx.ConnectTimeout("timed out"))
    assert _errors(events) == [unreachable_message("Ollama", "http://127.0.0.1:11434")]
    # The retry chip still ends as it did.
    assert any(kind == "backend.status" and data.get("kind") == "ollama_exhausted" for kind, data in events)


def test_other_ollama_errors_keep_their_own_words():
    assert _errors(_ollama_stream(RuntimeError("model 'x' not found"))) == ["model 'x' not found"]


def test_offline_modes_own_reason_comes_first():
    refused = offline.OfflineBlocked("Ollama", "10.0.0.131")
    assert _errors(_ollama_stream(refused)) == [str(refused)]
    assert unreachable_message("Ollama", "http://10.0.0.131:11434", refused) == str(refused)


def _refusing(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError(REFUSED, request=request)


def test_a_stopped_exo_says_the_same():
    backend = ExoBackend("mlx-community/Qwen3-4bit", base_url="http://127.0.0.1:52415/v1",
                         transport=httpx.MockTransport(_refusing))
    [message] = _errors(backend.stream("Hi", [], "Be helpful", []))
    assert message == ("Lumi couldn't reach EXO at http://127.0.0.1:52415/v1. Start EXO there, or check "
                       "Settings › Connections.")


def test_exos_generation_stream_says_the_same_when_exo_stops_midway():
    backend = ExoBackend("mlx-community/Qwen3-4bit", base_url="http://127.0.0.1:52415/v1",
                         transport=httpx.MockTransport(_refusing))
    backend._ensure_instance = lambda cancel_event=None: False  # it was running a moment ago
    assert _errors(backend.stream("Hi", [], "Be helpful", [])) == [
        unreachable_message("EXO", "http://127.0.0.1:52415/v1")]


@pytest.mark.parametrize("base_url, local", [("http://127.0.0.1:1234/v1", True), ("http://localhost:4000/v1", True),
                                             ("https://llm.acme.example/v1", False)])
def test_a_custom_connection_on_this_computer_says_the_same(base_url, local):
    connection = normalize_connection({"name": "LM Studio", "type": "openai-compatible", "base_url": base_url,
                                       "auth": "none", "models": "qwen3-coder"})
    backend = OpenAICompatibleBackend(connection, "qwen3-coder", transport=httpx.MockTransport(_refusing))
    [message] = _errors(backend.stream("Hi", [], "Be helpful", []))
    if local:
        assert message == (f"Lumi couldn't reach LM Studio at {base_url}. Start LM Studio there, or check "
                           "Settings › Connections.")
    else:
        # Someone else's server: the provider's own message, as before.
        assert message == "LM Studio API connection failed: ConnectError"
