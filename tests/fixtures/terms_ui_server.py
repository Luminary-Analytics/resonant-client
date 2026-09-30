"""Isolated source-app server for the terms browser check (tests/terms_acceptance.browser.cjs).

A throwaway home with nothing accepted: the app shows Lumi's terms at first
launch (this source tree's version is a development build, so the Alpha and
Beta Test Terms come with the End User License Agreement), and inference is
scripted. The shipped template, app, WebSocket handlers, terms module,
oversight gate and Session run as they do in the app, which asks its person
(``terms.mark_app_process``: LUMI_ACCEPT_TERMS doesn't apply). Choosing a model
in the app builds a real OllamaBackend against a loopback Ollama that records
every request (tests/fixtures/ollama_recorder.py), so the check can tell
whether a warm-up reached it before the terms were accepted. Nothing leaves
the loopback. Never a live-model or packaged-desktop qualification.
"""
from __future__ import annotations

import json
import os
import socket
import sys
from pathlib import Path


def main() -> None:
    root = Path(sys.argv[1]).resolve()
    home = root / "home"
    home.mkdir(parents=True)
    workspace = root / "project"
    workspace.mkdir()
    (workspace / "README.md").write_text("Terms fixture project.\n", encoding="utf-8")
    for key in ("HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA"):
        os.environ[key] = str(home)
    os.environ["LUMI_KEYCHAIN"] = "off"
    for key in ("LUMI_STATE_HOME", "RESONANT_STATE_HOME", "LUMI_POLICY_FILE", "LUMI_ACCEPT_TERMS",
                "RESONANT_ACCEPT_TERMS", "OLLAMA_HOST", "LUMI_CODEX_CLI", "LUMI_CLAUDE_CLI"):
        os.environ.pop(key, None)
    for key in tuple(os.environ):
        if any(part in key.upper() for part in ("API_KEY", "TOKEN", "SECRET", "PASSWORD")):
            os.environ.pop(key)
    os.chdir(workspace)
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    # Fail closed on anything that would leave this computer.
    original_connect = socket.socket.connect

    def local_connect(sock, address):
        if not isinstance(address, tuple) or address[0] not in {"127.0.0.1", "::1", "localhost"}:
            raise OSError("Fixture prohibits non-loopback connections")
        return original_connect(sock, address)

    socket.socket.connect = local_connect

    from tests.fixtures import ollama_recorder

    recorder = ollama_recorder.Recorder()
    ollama_server, ollama_url = ollama_recorder.start(recorder)

    from lumi.paths import state_home

    # The state folder must be the fixture's, before anything imports the app.
    assert str(state_home()).startswith(str(home)) and str(Path.home()).startswith(str(home)), state_home()
    state_home().mkdir(parents=True)
    (state_home() / "settings.json").write_text(json.dumps({
        "general": {"default_backend": "ollama", "default_model": "fixture-native"},
        "security": {"cli_adapters": False},
        "network": {"ollama_url": ollama_url, "exo_url": "http://127.0.0.1:9"},
    }), encoding="utf-8")

    import uvicorn
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    from lumi import policy, terms
    from lumi.engine import Session
    from lumi.gui import app as gui
    from lumi.gui.runtime import BackendSpec
    from tests.streaming_stub import StreamingBackend, done, text_delta

    policy.set_for_tests(None)
    terms.set_for_tests(None)
    terms.mark_app_process()  # as lumi/gui/server.py does: the app asks its person
    backend = StreamingBackend(name="ollama", model="fixture-native",
                               events=[text_delta("Scripted reply after the terms."), done(model="fixture-native")])
    state = gui.state
    state.project.set_project(str(workspace))
    state.apply_project_context(str(workspace), refresh_index=True)
    current = state.project.create_session("ollama", "fixture-native")
    current.title = "Terms fixture conversation"
    current.save()
    spec = BackendSpec("ollama", "fixture-native")
    state.backend_spec = spec
    state.backend = backend
    state.session = Session(backend=backend, project_instructions="Isolated terms fixture.")
    state.session.project_path = str(workspace)
    state.available_backends = {"ollama": {"url": ollama_url, "models": [spec.model, ollama_recorder.MODEL]}}
    state.detect_backends = lambda *args, **kwargs: None

    async def evidence(request):
        path = terms.record_path()
        return JSONResponse({
            "kind": "source-app-scripted-terms-browser", "live_providers_called": False,
            "requests": [call["user_msg"] for call in backend.stream_calls],
            # What reached the recording Ollama, a model chosen in the app (warm-ups included).
            "ollama_requests": [{"method": item["method"], "path": item["path"]} for item in recorder.requests],
            "ollama_model_requests": [item["body"] for item in recorder.chats()],
            "backend": f"{getattr(state.backend, 'name', '')}:{getattr(state.backend, 'model', '')}",
            "record": json.loads(path.read_text(encoding="utf-8")) if path.exists() else None,
            "status": terms.status(),
        })

    async def forget(request):
        # As a new version would: nothing accepted on record here.
        if terms.record_path().exists():
            terms.record_path().unlink()
        return JSONResponse({"forgotten": True})

    async def organization(request):
        # As a machine policy an administrator set (Group Policy, a configuration profile, the machine file).
        policy.set_for_tests(policy.parse({"schema": policy.SCHEMA, "organization": "Acme",
                                           "legal": {"accepted_by_organization": "Acme Corp"}},
                                          source="fixture machine policy"), machine=True)
        return JSONResponse({"organization": "Acme Corp"})

    async def personal(request):
        # No organization policy again: each person accepts for themselves.
        policy.set_for_tests(None)
        return JSONResponse({"organization": ""})

    async def scripted(request):
        # Back to the scripted model, after a check chose the recording Ollama.
        state.backend_spec = spec
        state.backend = backend
        state.session = Session(backend=backend, project_instructions="Isolated terms fixture.")
        state.session.project_path = str(workspace)
        state.apply_permission_mode(state.permission_mode, session=state.session)
        return JSONResponse({"backend": "scripted"})

    async def clear(request):
        recorder.clear()
        return JSONResponse({"cleared": True})

    async def shutdown(request):
        server.should_exit = True
        return JSONResponse({"stopping": True})

    async def fixture_launch(request):
        from lumi.gui.local_access import access
        return JSONResponse({"url": access.launch_url(str(request.base_url))})

    gui.app.routes.extend([Route("/__fixture__/launch", fixture_launch), Route("/__fixture__/evidence", evidence),
                           Route("/__fixture__/forget", forget, methods=["POST"]),
                           Route("/__fixture__/organization", organization, methods=["POST"]),
                           Route("/__fixture__/personal", personal, methods=["POST"]),
                           Route("/__fixture__/scripted", scripted, methods=["POST"]),
                           Route("/__fixture__/clear", clear, methods=["POST"]),
                           Route("/__fixture__/shutdown", shutdown, methods=["POST"])])
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    server = uvicorn.Server(uvicorn.Config(gui.app, host="127.0.0.1", log_level="warning"))
    print(json.dumps({"url": f"http://127.0.0.1:{listener.getsockname()[1]}", "session_id": current.id,
                      "home": str(home), "python": sys.executable, "ollama": ollama_url,
                      "ollama_model": ollama_recorder.MODEL}), flush=True)
    server.run(sockets=[listener])
    ollama_server.shutdown()


if __name__ == "__main__":
    main()
