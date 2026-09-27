"""Isolated source-app server for the DLP browser check (tests/dlp_ui.browser.cjs).

The organization policy is a fixture file and inference is scripted: the
shipped template, app, WebSocket handlers, Session and DLP check run as they
do for a person. ``/__fixture__/evidence`` returns what the scripted model
received, so the check can compare it with what the page showed. Never a
live-model or packaged-desktop qualification.
"""
from __future__ import annotations

import json
import os
import socket
import sys
from pathlib import Path

POLICY = {
    "schema": "lumi.policy/v1",
    "organization": "Fixture Corp",
    "dlp": {
        "version": 1,
        "detectors": {"credit_card": "redact", "us_ssn": "block"},
        "rules": [{"name": "falcon-codename", "keywords": ["Project Falcon"], "action": "flag",
                   "scope": ["prompt", "attachment"]}],
    },
}


def main() -> None:
    root = Path(sys.argv[1]).resolve()
    root.mkdir(exist_ok=True)
    home = root / "home"
    home.mkdir()
    workspace = root / "project"
    workspace.mkdir()
    for key in ("HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA"):
        os.environ[key] = str(home)
    os.environ["LUMI_KEYCHAIN"] = "off"
    os.environ.pop("LUMI_STATE_HOME", None)
    for key in tuple(os.environ):
        if any(part in key.upper() for part in ("API_KEY", "TOKEN", "SECRET", "PASSWORD")):
            os.environ.pop(key)
    policy_file = root / "policy.json"
    policy_file.write_text(json.dumps(POLICY), encoding="utf-8")
    os.environ["LUMI_POLICY_FILE"] = str(policy_file)
    os.chdir(workspace)
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    # Only loopback connections: no provider or account is ever reached.
    original_connect = socket.socket.connect

    def local_connect(sock, address):
        if not isinstance(address, tuple) or address[0] not in {"127.0.0.1", "::1", "localhost"}:
            raise OSError("Fixture prohibits non-loopback connections")
        return original_connect(sock, address)

    socket.socket.connect = local_connect

    from lumi import policy as lumi_policy

    # The fixture's policy file, never a policy installed on this machine.
    lumi_policy._registry_policy = lambda: None
    lumi_policy._macos_managed_policy = lambda: None
    lumi_policy._machine_file_policy = lambda: None
    loaded = lumi_policy.load(force=True)
    assert loaded.policy is not None and loaded.policy.dlp is not None, loaded.error

    import uvicorn
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    from lumi.engine import Session
    from lumi.gui import app as gui
    from lumi.gui.runtime import BackendSpec
    from tests.streaming_stub import StreamingBackend, done, text_delta

    received: list[dict] = []

    class RecordingBackend(StreamingBackend):
        def stream(self, **kwargs):
            received.append({"user_msg": kwargs.get("user_msg"),
                             "conversation_history": kwargs.get("conversation_history"),
                             "max_tokens": kwargs.get("max_tokens")})
            yield from super().stream(**kwargs)

    state = gui.state
    state.project.set_project(str(workspace))
    state.apply_project_context(str(workspace), refresh_index=False)
    current = state.project.create_session("ollama", "fixture-native")
    current.title = "DLP fixture conversation"
    current.save()
    spec = BackendSpec("ollama", "fixture-native")
    state.backend_spec = spec
    state.backend = RecordingBackend(name=spec.backend_type, model=spec.model,
                                     events=[text_delta("Noted."), done(model=spec.model)])
    state.session = Session(backend=state.backend, project_instructions="Isolated DLP browser fixture.")
    state.session.project_path = str(workspace)
    state.available_backends = {"ollama": {"models": [spec.model]}}
    state.detect_backends = lambda *args, **kwargs: None

    async def evidence(request):
        return JSONResponse({"kind": "source-app-scripted-native-browser", "live_providers_called": False,
                             "requests": json.loads(json.dumps(received, default=str)),
                             "audit": [json.loads(line) for path in sorted((home / ".lumi" / "audit").glob("*.jsonl"))
                                       for line in path.read_text(encoding="utf-8").splitlines()]})

    async def shutdown(request):
        server.should_exit = True
        return JSONResponse({"stopping": True})

    async def fixture_launch(request):
        # The page redeems a one-time launch code (lumi/gui/local_access.py).
        from lumi.gui.local_access import access
        return JSONResponse({"url": access.launch_url(str(request.base_url))})

    gui.app.routes.extend([Route("/__fixture__/launch", fixture_launch), Route("/__fixture__/evidence", evidence),
                           Route("/__fixture__/shutdown", shutdown, methods=["POST"])])
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    server = uvicorn.Server(uvicorn.Config(gui.app, host="127.0.0.1", log_level="warning"))
    print(json.dumps({"url": f"http://127.0.0.1:{listener.getsockname()[1]}", "session_id": current.id,
                      "home": str(home), "python": sys.executable}), flush=True)
    server.run(sockets=[listener])


if __name__ == "__main__":
    main()
