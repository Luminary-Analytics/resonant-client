"""Isolated source-app server for the organization oversight browser check (tests/oversight_notice.browser.cjs).

A throwaway home holds a managed enrollment with its own device key, an
organization policy (as a machine policy that enrolled the computer) turns
oversight on, and inference is scripted. The shipped template, app, WebSocket
handlers, oversight module and Session run as they do in the app; nothing
leaves the loopback. Never a live-model or packaged-desktop qualification.
"""
from __future__ import annotations

import base64
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
    (workspace / "README.md").write_text("Oversight fixture project.\n", encoding="utf-8")
    for key in ("HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA"):
        os.environ[key] = str(home)
    os.environ["LUMI_KEYCHAIN"] = "off"
    for key in ("LUMI_STATE_HOME", "RESONANT_STATE_HOME", "LUMI_POLICY_FILE", "OLLAMA_HOST", "LUMI_CODEX_CLI",
                "LUMI_CLAUDE_CLI"):
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

    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from lumi.paths import state_home

    # The state folder must be the fixture's, before anything imports the app.
    assert str(state_home()).startswith(str(home)) and str(Path.home()).startswith(str(home)), state_home()
    key = Ed25519PrivateKey.generate()
    private = key.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                                serialization.NoEncryption())
    public = key.public_key()
    state_home().mkdir(parents=True)
    (state_home() / "settings.json").write_text(json.dumps({
        "general": {"default_backend": "ollama", "default_model": "fixture-native"},
        "security": {"cli_adapters": False},
        "network": {"ollama_url": "http://127.0.0.1:9", "exo_url": "http://127.0.0.1:9"},
        "api_keys": {"lumi_cloud_device_key": base64.b64encode(private).decode("ascii")},
        "cloud": {"url": "https://cloud.example.test", "device": {
            "id": "dev_fixture", "how": "managed", "organization_id": "org_acme", "organization_name": "Acme",
            "url": "https://cloud.example.test"}},
    }), encoding="utf-8")

    import uvicorn
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    from lumi import oversight, policy
    from lumi.engine import Session
    from lumi.gui import app as gui
    from lumi.gui.runtime import BackendSpec
    from tests.streaming_stub import StreamingBackend, done, text_delta

    policy.set_for_tests(policy.parse({
        "schema": "lumi.policy/v1", "organization": "Acme", "cloud": {"url": "https://cloud.example.test"},
        "oversight": {"version": 1, "activity": True, "messages": "redacted", "security_flags": True,
                      "notice": "Questions: security@acme.example", "unattended": "record"},
    }, source="fixture machine policy"))
    backend = StreamingBackend(name="ollama", model="fixture-native",
                               events=[text_delta("Scripted reply after the notice."), done(model="fixture-native")])
    state = gui.state
    state.project.set_project(str(workspace))
    state.apply_project_context(str(workspace), refresh_index=True)
    current = state.project.create_session("ollama", "fixture-native")
    current.title = "Oversight fixture conversation"
    current.save()
    spec = BackendSpec("ollama", "fixture-native")
    state.backend_spec = spec
    state.backend = backend
    state.session = Session(backend=backend, project_instructions="Isolated oversight fixture.")
    state.session.project_path = str(workspace)
    state.available_backends = {"ollama": {"models": [spec.model]}}
    state.detect_backends = lambda *args, **kwargs: None

    async def evidence(request):
        notice = json.loads((state_home() / "oversight" / "notice.json").read_text(encoding="utf-8")) \
            if (state_home() / "oversight" / "notice.json").exists() else None
        verifies = None
        if notice and notice.get("signature"):
            try:
                public.verify(base64.urlsafe_b64decode(notice["signature"]), oversight.canonical(notice["record"]))
                verifies = True
            except Exception:
                verifies = False
        return JSONResponse({
            "kind": "source-app-scripted-oversight-browser", "live_providers_called": False,
            "requests": [call["user_msg"] for call in backend.stream_calls],
            "notice": notice, "signature_verifies": verifies,
            "upload": oversight.acknowledgment_upload(str((notice or {}).get("id") or "")),
            "records": oversight.queued_records(), "status": oversight.status(),
        })

    async def forget(request):
        oversight.forget_notice("Browser fixture: show the notice again")
        return JSONResponse({"forgotten": True})

    async def shutdown(request):
        server.should_exit = True
        return JSONResponse({"stopping": True})

    async def fixture_launch(request):
        # The app page redeems a one-time launch code for this process's
        # access token (lumi/gui/local_access.py); every page load needs one.
        from lumi.gui.local_access import access
        return JSONResponse({"url": access.launch_url(str(request.base_url))})

    gui.app.routes.extend([Route("/__fixture__/launch", fixture_launch), Route("/__fixture__/evidence", evidence),
                           Route("/__fixture__/forget", forget, methods=["POST"]),
                           Route("/__fixture__/shutdown", shutdown, methods=["POST"])])
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    server = uvicorn.Server(uvicorn.Config(gui.app, host="127.0.0.1", log_level="warning"))
    print(json.dumps({"url": f"http://127.0.0.1:{listener.getsockname()[1]}", "session_id": current.id,
                      "home": str(home), "python": sys.executable}), flush=True)
    server.run(sockets=[listener])


if __name__ == "__main__":
    main()
