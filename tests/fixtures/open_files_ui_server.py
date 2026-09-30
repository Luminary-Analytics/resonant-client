"""Isolated source-app server for the open-files browser check (tests/open_files.browser.cjs).

A scripted model writes a script and a document into the project; the page
shows them as changed files, and the check clicks them. Opening a document
and showing a file in its folder are recorded here instead of starting the
system's programs, so the check can read what the server did
(``/__fixture__/evidence``); the script itself only appends its name to a
marker file outside the project, which must stay empty. Never a live-model
or packaged-desktop qualification.

With ``git`` after the folder (tests/git_trust.browser.cjs), the project is
a Git repository whose own settings name a clean filter: a harmless program
outside the project that appends to ``git-ran.txt``. Until the person
trusts the project, Lumi must run no Git there.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path


def armed_repository(workspace: Path, root: Path) -> Path:
    """The project as a repository whose .git/config names a clean filter (a copied folder can bring one)."""
    git = shutil.which("git")  # the fixture's own Git, to build the repository
    marker = root / "git-ran.txt"
    tools = root / "tools"
    tools.mkdir()
    if sys.platform == "win32":
        clean = tools / "clean.bat"
        clean.write_text(f'@echo off\r\necho clean>>"{marker}"\r\nmore\r\n', encoding="ascii")
    else:
        clean = tools / "clean.sh"
        clean.write_text(f'#!/bin/sh\necho clean >> "{marker}"\ncat\n', encoding="ascii")
        clean.chmod(0o755)
    identity = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
                "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}
    (workspace / "data.bin").write_text("one\n", encoding="utf-8")
    (workspace / ".gitattributes").write_text("*.bin filter=review\n", encoding="utf-8")
    for args in (["init", "-q"], ["add", "."], ["commit", "-q", "-m", "first"],
                 ["config", "filter.review.clean", clean.as_posix()]):
        subprocess.run([git, *args], cwd=workspace, env=identity, check=True, capture_output=True)
    (workspace / "data.bin").write_text("two\n", encoding="utf-8")
    os.utime(workspace / "data.bin", (1, 1))  # "racy": status would ask the filter
    return marker


def main() -> None:
    root = Path(sys.argv[1]).resolve()
    root.mkdir(exist_ok=True)
    home = root / "home"
    home.mkdir()
    workspace = root / "project"
    workspace.mkdir()
    marker = root / "ran.txt"
    for key in ("HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA"):
        os.environ[key] = str(home)
    os.environ["LUMI_KEYCHAIN"] = "off"
    os.environ.pop("LUMI_STATE_HOME", None)
    for key in tuple(os.environ):
        if any(part in key.upper() for part in ("API_KEY", "TOKEN", "SECRET", "PASSWORD")):
            os.environ.pop(key)
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    os.chdir(workspace)
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    # Only loopback connections: no provider or account is ever reached.
    original_connect = socket.socket.connect

    def local_connect(sock, address):
        if not isinstance(address, tuple) or address[0] not in {"127.0.0.1", "::1", "localhost"}:
            raise OSError("Fixture prohibits non-loopback connections")
        return original_connect(sock, address)

    socket.socket.connect = local_connect
    git_marker = armed_repository(workspace, root) if sys.argv[2:3] == ["git"] else None

    if sys.platform == "win32":
        script, body = "setup.cmd", f'@echo off\r\necho setup.cmd>>"{marker}"\r\n'
    elif sys.platform == "darwin":
        script, body = "setup.command", f'#!/bin/sh\necho setup.command >> "{marker}"\n'
    else:
        script, body = "setup.sh", f'#!/bin/sh\necho setup.sh >> "{marker}"\n'

    import uvicorn
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    from lumi.engine import Session
    from lumi.gui import app as gui
    from lumi.gui import ws_commands
    from lumi.gui.runtime import BackendSpec
    from tests.streaming_stub import StreamingBackend, done, text_delta, tool_call

    opened: list[str] = []
    revealed: list[str] = []
    # The system's programs aren't started: what the server asked for is recorded.
    ws_commands.open_path = lambda path: opened.append(str(path))
    ws_commands.show_in_folder = lambda path: revealed.append(str(path))

    class ScriptedBackend(StreamingBackend):
        wrote = False

        def stream(self, **kwargs):
            if kwargs.get("max_tokens") == 32:  # the session's title
                yield text_delta("Setup script")
                yield done(model=self.model)
                return
            if not self.wrote:  # the turn's first request writes; the next one answers
                self.wrote = True
                yield tool_call("file_write", {"path": script, "content": body}, "write-script")
                yield tool_call("file_write", {"path": "notes.md", "content": "# Notes\n"}, "write-notes")
                yield done(model=self.model)
                return
            yield text_delta("Wrote the setup script and the notes.")
            yield done(model=self.model)

        def classify(self, prompt, max_tokens=20):
            return "SIMPLE"

    # This computer user accepted Lumi's terms already, as in the app (lumi/terms.py): the checks here
    # are about other things, and the terms dialog would lock the message box first.
    from lumi import terms as lumi_terms

    lumi_terms.accept({doc.id: doc.version for doc in lumi_terms.required()}, "app")
    state = gui.state
    state.project.set_project(str(workspace))
    state.apply_project_context(str(workspace), refresh_index=False)
    current = state.project.create_session("ollama", "fixture-native")
    current.title = "Open files fixture"
    current.save()
    spec = BackendSpec("ollama", "fixture-native")
    state.backend_spec = spec
    state.backend = ScriptedBackend(name=spec.backend_type, model=spec.model)
    state.session = Session(backend=state.backend, project_instructions="Isolated open-files browser fixture.")
    state.session.project_path = str(workspace)
    state.available_backends = {"ollama": {"models": [spec.model]}}
    state.detect_backends = lambda *args, **kwargs: None

    async def evidence(request):
        return JSONResponse({"opened": opened, "revealed": revealed,
                             "ran": marker.read_text(encoding="utf-8") if marker.exists() else "",
                             "git_ran": git_marker.read_text(encoding="utf-8")
                             if git_marker and git_marker.exists() else "",
                             "script_exists": (workspace / script).is_file()})

    async def shutdown(request):
        server.should_exit = True
        return JSONResponse({"stopping": True})

    async def fixture_launch(request):
        # The page redeems a one-time launch code (lumi/gui/local_access.py).
        from lumi.gui.local_access import access
        return JSONResponse({"url": access.launch_url(str(request.base_url))})

    gui.app.routes.extend([Route("/__fixture__/launch", fixture_launch), Route("/__fixture__/evidence", evidence),
                           Route("/__fixture__/shutdown", shutdown, methods=["POST"])])
    server = uvicorn.Server(uvicorn.Config(gui.app, host="127.0.0.1", log_level="warning"))
    print(json.dumps({"url": f"http://127.0.0.1:{port}", "session_id": current.id, "script": script,
                      "project": str(workspace), "home": str(home)}), flush=True)
    server.run(sockets=[listener])


if __name__ == "__main__":
    main()
