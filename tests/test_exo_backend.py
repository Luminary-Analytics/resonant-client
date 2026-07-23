"""Wire-contract tests for the EXO OpenAI-compatible provider."""

from __future__ import annotations

import json
import threading
from types import SimpleNamespace

import httpx

from resonant_client.backends import (
    EVENT_DONE,
    EVENT_ERROR,
    EVENT_TEXT_DELTA,
    ExoBackend,
    create_backend,
)
from resonant_client.gui.app import AppState
from resonant_client.gui.runtime import BackendSpec


def _sse_response(events: list[dict]) -> str:
    return "".join(f"data: {json.dumps(event)}\n\n" for event in events) + "data: [DONE]\n\n"


def test_exo_streams_openai_chat_without_auth_or_output_cap():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/state":
            return httpx.Response(
                200,
                json={"instances": {"instance-1": {"meta": {"modelId": (
                    "mlx-community/Llama-3.2-3B-Instruct-4bit"
                )}}}},
            )
        captured["url"] = str(request.url)
        captured["headers"] = dict(request.headers)
        captured["payload"] = json.loads(request.read())
        return httpx.Response(
            200,
            text=_sse_response([
                {"id": "exo-1", "choices": [{"delta": {"content": "Ready"}}]},
                {
                    "id": "exo-1",
                    "choices": [],
                    "usage": {"prompt_tokens": 12, "completion_tokens": 4},
                },
            ]),
        )

    backend = ExoBackend(
        "mlx-community/Llama-3.2-3B-Instruct-4bit",
        base_url="http://exo.test:52415",
        transport=httpx.MockTransport(handler),
    )
    events = list(backend.stream(
        "Inspect the repo",
        [],
        "You are a coding agent.",
        [{"type": "function", "function": {"name": "read", "parameters": {"type": "object"}}}],
        max_tokens=128,
    ))

    assert captured["url"] == "http://exo.test:52415/v1/chat/completions"
    assert "authorization" not in captured["headers"]
    payload = captured["payload"]
    assert payload["model"] == "mlx-community/Llama-3.2-3B-Instruct-4bit"
    assert "max_tokens" not in payload
    assert "max_completion_tokens" not in payload
    assert "reasoning_effort" not in payload
    assert payload["stream_options"] == {"include_usage": True}
    assert payload["tools"][0]["function"]["name"] == "read"
    assert (EVENT_TEXT_DELTA, {"delta": "Ready"}) in events
    done = next(data for event, data in events if event == EVENT_DONE)
    assert done["stats"] == {"input_tokens": 12, "output_tokens": 4, "cached_tokens": 0}


def test_exo_catalog_prioritizes_downloaded_models(monkeypatch):
    def fake_get(url, timeout):
        if str(url).endswith("/state"):
            return httpx.Response(
                200,
                json={"instances": {"active": {"modelId": "running/model"}}},
                request=httpx.Request("GET", url),
            )
        if "status=downloaded" in url:
            return httpx.Response(
                200,
                json={"object": "list", "data": [{"id": "downloaded/model"}]},
                request=httpx.Request("GET", url),
            )
        return httpx.Response(
            200,
            json={"object": "list", "data": [{"id": "remote/model"}, {"id": "downloaded/model"}]},
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr(httpx, "get", fake_get)

    catalog = ExoBackend.discover_models(base_url="http://exo.test:52415/v1")

    assert catalog["downloaded_models"] == ["downloaded/model"]
    assert catalog["running_models"] == ["running/model"]
    assert catalog["models"] == ["running/model", "downloaded/model", "remote/model"]


def test_exo_starts_and_awaits_a_missing_instance():
    model = "downloaded/model"
    calls: list[tuple[str, str]] = []
    state_checks = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal state_checks
        calls.append((request.method, request.url.path))
        if request.url.path == "/state":
            state_checks += 1
            instances = {} if state_checks == 1 else {"ready": {"modelId": model}}
            return httpx.Response(200, json={"instances": instances})
        if request.url.path == "/instance/previews":
            return httpx.Response(
                200,
                json={"previews": [{"error": None, "instance": {"MlxRingInstance": {}}}]},
            )
        if request.url.path == "/instance":
            assert json.loads(request.read()) == {
                "instance": {"MlxRingInstance": {}}
            }
            return httpx.Response(200, json={"message": "Command received."})
        if request.url.path == "/instance/await":
            return httpx.Response(
                200,
                text=f'data: {{"type":"ready","instance":{{"modelId":"{model}"}}}}\n\n',
            )
        if request.url.path == "/v1/chat/completions":
            return httpx.Response(
                200,
                text=_sse_response([
                    {"id": "exo-2", "choices": [{"delta": {"content": "Started"}}]},
                ]),
            )
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    backend = ExoBackend(model, transport=httpx.MockTransport(handler))
    events = list(backend.stream("Hello", [], "system", [], None))

    assert ("GET", "/instance/previews") in calls
    assert ("POST", "/instance") in calls
    assert ("GET", "/instance/await") in calls
    assert (EVENT_TEXT_DELTA, {"delta": "Started"}) in events


def test_exo_factory_backend_spec_and_app_routing():
    model = "mlx-community/Llama-3.2-3B-Instruct-4bit"
    spec = BackendSpec(
        backend_type="exo",
        model=model,
        base_url="http://10.0.0.131:52415/v1",
    )
    backend = spec.create_backend()

    assert isinstance(backend, ExoBackend)
    assert backend.model == model
    assert create_backend("exo", model=model).name == "exo"

    class Settings:
        def get(self, section, key=None, default=None):
            if section == "general" and key == "default_model":
                return ""
            return default

    state = AppState.__new__(AppState)
    state.project = SimpleNamespace(project_path="D:/Repos/Playground")
    state.settings = Settings()
    state.permission_mode = "bypass"
    state.backend_spec = None
    state.exo_url = "http://10.0.0.131:52415/v1"
    state.available_backends = {
        "exo": {"url": state.exo_url, "models": [model]},
    }

    routed = state.build_backend_spec("exo")

    assert routed.backend_type == "exo"
    assert routed.model == model
    assert state.select_harness_backend(session_role="generator") == ("exo", model)


def test_exo_supports_future_multimodal_content_shape():
    backend = ExoBackend("vision-capable-model")
    converted = backend._api_content([
        {"type": "image", "media_type": "image/png", "data": "aGVsbG8="},
        {"type": "text", "text": "Inspect this screenshot"},
    ])

    assert backend.capability_profile.supports("vision")
    assert converted[0]["image_url"]["url"] == "data:image/png;base64,aGVsbG8="
    assert converted[1] == {"type": "text", "text": "Inspect this screenshot"}


def test_exo_uses_progress_idle_timeout_without_capping_total_output(monkeypatch):
    monkeypatch.delenv("RESONANT_EXO_READ_TIMEOUT_SEC", raising=False)
    monkeypatch.delenv("RESONANT_EXO_STREAM_IDLE_TIMEOUT_SEC", raising=False)

    backend = ExoBackend("local/model")

    assert backend._stream_idle_timeout == 120.0
    assert backend._timeout.read == 120.0


def test_exo_remote_cancel_uses_command_endpoint():
    requests: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, request.url.path))
        return httpx.Response(200, json={"message": "Command cancelled."})

    backend = ExoBackend(
        "local/model",
        base_url="http://exo.test:52415/v1",
        transport=httpx.MockTransport(handler),
    )

    assert backend._cancel_remote_generation("command-123") is True
    assert requests == [("POST", "/v1/cancel/command-123")]


def test_exo_stop_cancels_a_stream_blocked_waiting_for_more_output():
    release_stream = threading.Event()
    stream_is_blocked = threading.Event()
    cancel_event = threading.Event()
    requests: list[tuple[str, str]] = []

    class BlockingSseStream(httpx.SyncByteStream):
        def __iter__(self):
            yield (
                b'data: {"id":"command-live","choices":'
                b'[{"delta":{"content":"Started"}}]}\n\n'
            )
            stream_is_blocked.set()
            assert release_stream.wait(2.0), "remote cancel did not release stream"
            yield b"data: [DONE]\n\n"

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, request.url.path))
        if request.url.path == "/state":
            return httpx.Response(
                200,
                json={"instances": {"ready": {"modelId": "local/model"}}},
            )
        if request.url.path == "/v1/chat/completions":
            return httpx.Response(200, stream=BlockingSseStream())
        if request.url.path == "/v1/cancel/command-live":
            release_stream.set()
            return httpx.Response(200, json={"message": "Command cancelled."})
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    backend = ExoBackend(
        "local/model",
        base_url="http://exo.test:52415/v1",
        transport=httpx.MockTransport(handler),
    )
    stream = backend.stream("Work", [], "system", [], cancel_event=cancel_event)

    assert next(stream)[1]["kind"] == "exo_instance_check"
    assert next(stream)[1]["kind"] == "exo_generation_started"
    assert next(stream) == (EVENT_TEXT_DELTA, {"delta": "Started"})

    completed = threading.Event()

    def consume_until_stopped():
        try:
            next(stream)
        except StopIteration:
            completed.set()

    consumer = threading.Thread(target=consume_until_stopped, daemon=True)
    consumer.start()
    assert stream_is_blocked.wait(1.0)
    cancel_event.set()

    assert completed.wait(2.0), "blocked backend stream did not stop promptly"
    consumer.join(timeout=0.1)
    assert ("POST", "/v1/cancel/command-live") in requests


def test_exo_progress_watchdog_ignores_keepalives_and_ends_stalled_generation():
    release_stream = threading.Event()
    requests: list[tuple[str, str]] = []

    class KeepaliveOnlyStream(httpx.SyncByteStream):
        def __iter__(self):
            yield (
                b'data: {"id":"command-stalled","choices":'
                b'[{"delta":{"content":"Started"}}]}\n\n'
            )
            while not release_stream.wait(0.01):
                yield b": keepalive\n\n"
            yield b"data: [DONE]\n\n"

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, request.url.path))
        if request.url.path == "/state":
            return httpx.Response(
                200,
                json={"instances": {"ready": {"modelId": "local/model"}}},
            )
        if request.url.path == "/v1/chat/completions":
            return httpx.Response(200, stream=KeepaliveOnlyStream())
        if request.url.path == "/v1/cancel/command-stalled":
            release_stream.set()
            return httpx.Response(200, json={"message": "Command cancelled."})
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    backend = ExoBackend(
        "local/model",
        base_url="http://exo.test:52415/v1",
        transport=httpx.MockTransport(handler),
    )
    backend._stream_idle_timeout = 0.08

    events = list(backend.stream("Work", [], "system", []))

    error = next(data for event, data in events if event == EVENT_ERROR)
    assert "stopped responding" in error["message"]
    assert ("POST", "/v1/cancel/command-stalled") in requests


def test_exo_prefill_comment_is_visible_progress_but_keepalive_is_not():
    backend = ExoBackend("local/model")

    status = backend._provider_comment_status(
        ': prefill_progress {"progress":0.42,"processed_tokens":420}'
    )

    assert status == {
        "kind": "exo_prefill_progress",
        "model": "local/model",
        "progress": {"progress": 0.42, "processed_tokens": 420},
    }
    assert backend._provider_comment_status(": keepalive") is None


def test_exo_repetition_guard_stops_degenerate_output_without_length_cap():
    backend = ExoBackend("local/model")

    assert backend._stream_abort_reason("A normal, detailed coding response.") == ""
    assert backend._stream_abort_reason("Useful preface\n" + ".0" * 140).startswith(
        "EXO generation was stopped"
    )


def test_exo_malformed_fragment_guard_is_conservative():
    backend = ExoBackend("local/model")
    malformed = "Useful preface\n" + "\n".join(
        ["0.0", ":3", "a()", "-4", "00", "|:"] * 45
    )
    normal_code = "\n".join(
        f"result_{index} = parse_value(values[{index}])"
        for index in range(120)
    )
    numeric_table = "\n".join(str(index / 10) for index in range(200))

    assert backend._looks_like_malformed_output(malformed) is True
    assert backend._stream_abort_reason(malformed).startswith(
        "EXO generation was stopped"
    )
    assert backend._stream_abort_reason(normal_code) == ""
    assert backend._stream_abort_reason(numeric_table) == ""


def test_exo_quarantines_malformed_assistant_history_from_future_prompts():
    backend = ExoBackend("local/model")
    malformed = "\n".join(["0.0", ":3", "a()", "-4", "00", "|:"] * 45)
    history = [
        {"role": "user", "content": "Inspect the project"},
        {"role": "assistant", "content": malformed},
        {"role": "user", "content": "Continue with the real task"},
    ]

    messages = backend._messages(history, "system", "")

    assert [message["role"] for message in messages] == [
        "system",
        "user",
        "user",
    ]
    assert all(malformed not in str(message.get("content")) for message in messages)


def test_exo_malformed_stream_is_discardable_and_cancelled():
    cancelled: list[str] = []
    malformed = "\n".join(
        f":{index % 17}]({(index * 7) % 31};"
        for index in range(160)
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/state":
            return httpx.Response(
                200,
                json={"instances": {"ready": {"modelId": "local/model"}}},
            )
        return httpx.Response(
            200,
            text=_sse_response([{
                "id": "command-malformed",
                "choices": [{"delta": {"content": malformed}}],
            }]),
        )

    backend = ExoBackend(
        "local/model",
        base_url="http://exo.test:52415/v1",
        transport=httpx.MockTransport(handler),
    )
    backend._cancel_remote_generation = lambda command_id: (
        cancelled.append(command_id) or True
    )

    events = list(backend.stream("Work", [], "system", []))

    error = next(data for event, data in events if event == EVENT_ERROR)
    assert "malformed token fragments" in error["message"]
    assert error["discard_partial_output"] is True
    assert cancelled == ["command-malformed"]


def test_exo_repetition_guard_surfaces_terminal_error_and_cancels():
    cancelled: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/state":
            return httpx.Response(
                200,
                json={"instances": {"ready": {"modelId": "local/model"}}},
            )
        return httpx.Response(
            200,
            text=_sse_response([
                {
                    "id": "command-loop",
                    "choices": [{"delta": {"content": ".0" * 140}}],
                },
            ]),
        )

    backend = ExoBackend(
        "local/model",
        base_url="http://exo.test:52415/v1",
        transport=httpx.MockTransport(handler),
    )
    backend._cancel_remote_generation = lambda command_id: (
        cancelled.append(command_id) or True
    )

    events = list(backend.stream("Work", [], "system", []))

    error = next(data for event, data in events if event == EVENT_ERROR)
    assert "repetitious" in error["message"]
    assert cancelled == ["command-loop"]
