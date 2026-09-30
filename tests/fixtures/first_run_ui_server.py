"""Isolated source-app server for a new tester's first run (tests/first_run.browser.cjs).

A new install (SettingsManager writes its defaults, so Auto-edit) whose model is a
real OllamaBackend talking to a loopback Ollama stub (first_run_ollama_stub.py).
Throwaway home (HOME, USERPROFILE, APPDATA, LOCALAPPDATA), LUMI_KEYCHAIN=off,
loopback-only sockets, no Node.js or Codex CLI on PATH. Never a live-model or
packaged-desktop qualification.

    python first_run_ui_server.py <output folder> <scenario>

Scenarios:

* ``chat``: the stub is the saved Ollama address, so a model runs from the start.
* ``card``: the saved address answers nothing, so no model runs until one is set up.
* ``card-env``: like ``card``, with OLLAMA_HOST set to an address that answers nothing.

``/__fixture__/evidence`` returns the conversation's mode, the settings file and
what the stub received.
"""
from __future__ import annotations

import json
import os
import socket
import sys
from pathlib import Path

NOTHING_LISTENS = "http://127.0.0.1:9"


def main() -> None:
    root = Path(sys.argv[1]).resolve()
    scenario = sys.argv[2]
    home = root / "home"
    home.mkdir(parents=True)
    workspace = root / "project"
    workspace.mkdir()
    (workspace / "README.md").write_text("First-run fixture project.\n", encoding="utf-8")
    for key in ("HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA"):
        os.environ[key] = str(home)
    os.environ["CODEX_HOME"] = str(home / ".codex")
    os.environ["CLAUDE_CONFIG_DIR"] = str(home / ".claude")
    for key in tuple(os.environ):
        if key.startswith(("LUMI_", "RESONANT_")):
            os.environ.pop(key)
        elif any(part in key.upper() for part in ("API_KEY", "TOKEN", "SECRET", "PASSWORD")):
            os.environ.pop(key)
    os.environ["LUMI_KEYCHAIN"] = "off"
    os.environ["LUMI_OS_SCHEDULER"] = "off"
    for key in ("OLLAMA_HOST", "CODEX_CLI_PATH"):
        os.environ.pop(key, None)
    # No Codex CLI or Node.js: nothing outside the fixture starts.
    os.environ["PATH"] = os.pathsep.join(
        part for part in os.environ.get("PATH", "").split(os.pathsep)
        if "npm" not in part.lower() and "nodejs" not in part.lower())
    if scenario == "card-env":
        os.environ["OLLAMA_HOST"] = NOTHING_LISTENS
    os.chdir(workspace)
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    original_connect = socket.socket.connect

    def local_connect(sock, address):
        if not isinstance(address, tuple) or address[0] not in {"127.0.0.1", "::1", "localhost"}:
            raise OSError("Fixture prohibits non-loopback connections")
        return original_connect(sock, address)

    socket.socket.connect = local_connect
    import webbrowser

    webbrowser.open = lambda *args, **kwargs: False

    import first_run_ollama_stub as stub
    import lumi
    from lumi.paths import state_home

    # The code and the state folder must be the fixture's before anything imports the app.
    repository = Path(__file__).resolve().parents[2]
    assert Path(lumi.__file__).resolve().is_relative_to(repository), lumi.__file__
    assert state_home().resolve().is_relative_to(home) and Path.home().resolve().is_relative_to(home), state_home()
    recorder = stub.Recorder()
    stub_server, stub_url = stub.start(recorder)
    state_home().mkdir(parents=True)
    settings_path = state_home() / "settings.json"

    # A new install: SettingsManager writes its defaults first, then the model is chosen.
    from lumi.gui.settings import SettingsManager

    first = SettingsManager(settings_path)
    first_mode = first.get("general", "default_permission_mode")
    first.set("general", "default_backend", "ollama")
    first.set("general", "default_model", stub.MODEL)
    first.set("security", "cli_adapters", False)
    first.set("network", "exo_url", NOTHING_LISTENS)
    first.set("network", "ollama_url", stub_url if scenario == "chat" else ("" if scenario == "card-env" else NOTHING_LISTENS))
    del first

    import uvicorn
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    from lumi import policy
    from lumi.gui import app as gui

    policy.set_for_tests(None)
    state = gui.state
    state.project.set_project(str(workspace))
    state.apply_project_context(str(workspace), refresh_index=True)

    async def evidence(request):
        session = state.session
        return JSONResponse({
            "first_mode": first_mode,
            "stub_url": stub_url,
            "chats": [{"last": (chat["body"].get("messages") or [{}])[-1] if isinstance(chat["body"], dict) else None,
                       "tools": bool(isinstance(chat["body"], dict) and chat["body"].get("tools"))}
                      for chat in recorder.chats()],
            "settings_file": json.loads(settings_path.read_text(encoding="utf-8")),
            "permission_mode": state.permission_mode,
            "tier": getattr(session, "autonomy_tier", None),
            "backend": getattr(state.backend, "name", None),
            "ollama_url": state.ollama_url,
            "current_session": getattr(state.project.current_session, "id", None),
        })

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
    print(json.dumps({"url": f"http://127.0.0.1:{listener.getsockname()[1]}", "stub": stub_url,
                      "home": str(home), "first_mode": first_mode}), flush=True)
    server.run(sockets=[listener])
    stub_server.shutdown()


if __name__ == "__main__":
    main()
