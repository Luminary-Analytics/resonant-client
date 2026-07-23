"""Wire-contract tests for the EXO OpenAI-compatible provider."""

from __future__ import annotations

import json
from types import SimpleNamespace

import httpx

from resonant_client.backends import (
    EVENT_DONE,
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
