"""Isolated source-app server for the machine policy trust browser check (tests/policy_trust.browser.cjs).

The machine policy folder is a temporary one this user owns, standing in for
C:\\ProgramData\\Lumi, and the real check (lumi/admin_files.py) decides what
counts, as it does for a person. Two modes:

* ``planted``: a policy and a key file a person put in the machine folder;
  both are ignored and Settings says so, and turns still run.
* ``unreadable``: Group Policy names a PolicyFile that can't be read; Lumi
  refuses model requests, and nothing reaches the scripted model.

``/__fixture__/evidence`` returns what the scripted model received and the
audit log. Never a live-model or packaged-desktop qualification, and never
the machine's own policy locations.
"""
from __future__ import annotations

import json
import os
import socket
import sys
from pathlib import Path


def main() -> None:
    root = Path(sys.argv[1]).resolve()
    mode = sys.argv[2]
    root.mkdir(exist_ok=True)
    home = root / "home"
    home.mkdir()
    workspace = root / "project"
    workspace.mkdir()
    for key in ("HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA"):
        os.environ[key] = str(home)
    os.environ["LUMI_KEYCHAIN"] = "off"
    for key in ("LUMI_STATE_HOME", "RESONANT_STATE_HOME", "LUMI_POLICY_FILE", "LUMI_LICENSE_FILE"):
        os.environ.pop(key, None)
    for key in tuple(os.environ):
        if any(part in key.upper() for part in ("API_KEY", "TOKEN", "SECRET", "PASSWORD")):
            os.environ.pop(key)
    # What a person without administrator rights can do: make the machine folder and write in it.
    program_data = root / "ProgramData"
    machine = program_data / "Lumi"
    machine.mkdir(parents=True)
    (machine / "policy.json").write_text(json.dumps({
        "schema": "lumi.policy/v1", "organization": "Planted Corp",
        "permissions": {"allowed_modes": ["ask", "auto-edit", "plan", "bypass"]}}), encoding="utf-8")
    (machine / "policy-keys.json").write_text(json.dumps({"planted": "AAAA"}), encoding="utf-8")
    missing = root / "share" / "it" / "lumi-policy.json"
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
    from lumi import terms as lumi_terms

    # This computer user accepted Lumi's terms already, as in the app (lumi/terms.py), before the machine
    # policy below was set up (while an administrator's policy can't be read, no acceptance is recorded):
    # the checks here are about other things, and the terms dialog would lock the message box first.
    lumi_policy.set_for_tests(None)
    lumi_terms.accept({doc.id: doc.version for doc in lumi_terms.required()}, "app")

    # The fixture's machine folder and Group Policy, never this computer's.
    lumi_policy.machine_policy_file = lambda: machine / "policy.json"
    lumi_policy._program_data = lambda: str(program_data)
    lumi_policy.MAC_MANAGED_PREFERENCES = root / "no-managed-preferences"
    lumi_policy._registry_values = (lambda: {"PolicyFile": str(missing)}) if mode == "unreadable" else (lambda: {})
    loaded = lumi_policy.load(force=True)
    if mode == "unreadable":
        assert loaded.policy is None and "couldn't be read" in loaded.error, loaded
    else:
        assert loaded.policy is None and not loaded.error and loaded.ignored, loaded

    import uvicorn
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    from lumi import dlp
    from lumi.engine import Session
    from lumi.gui import app as gui
    from lumi.gui.runtime import BackendSpec
    from tests.streaming_stub import StreamingBackend, done, text_delta

    received: list[dict] = []

    @dlp.guard_backend
    class RecordingBackend(StreamingBackend):
        def stream(self, **kwargs):
            received.append({"user_msg": kwargs.get("user_msg"), "max_tokens": kwargs.get("max_tokens")})
            yield from super().stream(**kwargs)

        def classify(self, prompt, max_tokens=20):
            return super().classify(prompt, max_tokens=max_tokens)

    state = gui.state
    state.project.set_project(str(workspace))
    state.apply_project_context(str(workspace), refresh_index=False)
    current = state.project.create_session("ollama", "fixture-native")
    current.title = "Policy trust fixture conversation"
    current.save()
    spec = BackendSpec("ollama", "fixture-native")
    state.backend_spec = spec
    state.backend = RecordingBackend(name=spec.backend_type, model=spec.model,
                                     events=[text_delta("Noted."), done(model=spec.model)])
    state.session = Session(backend=state.backend, project_instructions="Isolated policy trust browser fixture.")
    state.session.project_path = str(workspace)
    state.available_backends = {"ollama": {"models": [spec.model]}}
    state.detect_backends = lambda *args, **kwargs: None

    async def evidence(request):
        return JSONResponse({"kind": "source-app-scripted-native-browser", "live_providers_called": False,
                             "requests": received,
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
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(gui.app, host="127.0.0.1", log_level="warning"))
    print(json.dumps({"url": f"http://127.0.0.1:{port}", "session_id": current.id, "home": str(home),
                      "machine": str(machine), "missing": str(missing), "python": sys.executable}), flush=True)
    server.run(sockets=[listener])


if __name__ == "__main__":
    main()
