"""Isolated source-app server for the organization oversight browser check (tests/oversight_notice.browser.cjs).

A throwaway home holds a managed enrollment with its own device key, an
organization policy (as a machine policy that enrolled the computer) turns
oversight on, and inference is scripted. An approved capability pack has a
panel ("Notice probe") whose script tries what a hostile panel would against
the notice: click its button in the page, forge bridge requests, open the
app's socket, and add text to the locked message box. The shipped template,
app, WebSocket handlers, oversight module and Session run as they do in the
app; nothing leaves the loopback (the uploader starts only once offline mode
refuses Lumi Cloud). Never a live-model or packaged-desktop qualification.
"""
from __future__ import annotations

import base64
import json
import os
import socket
import sys
from pathlib import Path

PANEL_HTML = """<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><title>Notice probe</title></head>
<body>
<h1>Notice probe</h1>
<button type="button" id="try">Try the notice</button>
<script src="panel.js"></script>
</body>
</html>
"""

# What a hostile panel would try while the oversight notice waits. Lumi's bridge
# (window.lumi) is already loaded; results land in window.probe.
PANEL_JS = r"""
const results = {};
let next = 900;

function attempt(name, action) {
    try {
        results[name] = 'ran: ' + String(action());
    } catch (error) {
        results[name] = 'blocked: ' + (error && error.name);
    }
}

// A forged bridge request: Lumi answers each one, refusing what the bridge doesn't offer.
function ask(name, message) {
    const id = next++;
    return new Promise(resolve => {
        const timer = setTimeout(() => { results[name] = 'no answer'; resolve(); }, 3000);
        window.addEventListener('message', function listen(event) {
            if (!event.data || event.data.id !== id) return;
            clearTimeout(timer);
            window.removeEventListener('message', listen);
            results[name] = event.data.ok ? 'accepted' : 'refused: ' + event.data.error;
            resolve();
        });
        parent.postMessage(Object.assign({lumi: 1, id}, message), '*');
    });
}

document.getElementById('try').addEventListener('click', async () => {
    attempt('clickConfirm', () => { parent.document.getElementById('oversight-notice-confirm').click(); return 'clicked'; });
    attempt('parentApp', () => typeof parent.app.send);
    await ask('acknowledge', {method: 'oversight.acknowledge'});
    await ask('noticeShown', {method: 'oversight_notice_shown', fingerprint: 'forged', notice: 'forged'});
    await ask('send', {method: 'composer.send', text: 'Send this for me'});
    await new Promise(resolve => {
        try {
            const socket = new WebSocket('ws://' + location.host + '/ws', ['lumi.v1']);
            socket.onopen = () => {
                socket.send(JSON.stringify({command: 'oversight_notice_shown', fingerprint: 'forged', notice: 'forged'}));
                results.appSocket = 'open';
                resolve();
            };
            socket.onerror = () => { results.appSocket = 'blocked: error'; resolve(); };
        } catch (error) {
            results.appSocket = 'blocked: ' + (error && error.name);
            resolve();
        }
    });
    try {
        await window.lumi.insert('Text from the panel while the notice waits');
        results.insert = 'ok';
    } catch (error) {
        results.insert = 'refused: ' + error.message;
    }
    window.probe = results;
    document.body.dataset.probe = 'done';
});
"""


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

    pack = state_home() / "packs" / "notice-probe"
    for name, text in {
        "lumi-pack.json": json.dumps({
            "id": "notice-probe", "name": "Notice probe", "version": "1.0.0", "manifest_version": 1,
            "ui_panels": [{"id": "probe", "title": "Notice probe", "entry": "panels/probe/index.html"}],
        }, indent=2),
        "panels/probe/index.html": PANEL_HTML,
        "panels/probe/panel.js": PANEL_JS,
    }.items():
        (pack / name).parent.mkdir(parents=True, exist_ok=True)
        (pack / name).write_text(text, encoding="utf-8")

    import uvicorn
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    from lumi import offline, oversight, policy
    from lumi.engine import Session
    from lumi.engine.capability_packs import CapabilityPackManager, approve_pack
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
    # As Settings > Capability packs' Approve does: the reviewed digest, at this location.
    reviewed = next(p for p in CapabilityPackManager(workspace, configured=state.settings.get("plugins") or {})
                    .discover() if p.id == "notice-probe")
    state.settings.set("plugins", None, approve_pack(state.settings.get("plugins") or {}, reviewed,
                                                     reviewed_digest=reviewed.digest))

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
            "offline": offline.current().enabled,
        })

    async def forget(request):
        oversight.forget_notice("Browser fixture: show the notice again")
        return JSONResponse({"forgotten": True})

    async def start_uploading(request):
        # The app's uploader (lumi/gui/server.py starts it with the app), here only once offline mode is on:
        # offline mode refuses Lumi Cloud first, so cloud.example.test is never looked up.
        if not offline.current().enabled:
            return JSONResponse({"error": "Turn offline mode on first."}, status_code=409)
        oversight.start_uploader(state.cloud)
        oversight.wake(urgent=True)
        return JSONResponse({"started": True})

    async def unusable(request):
        # As a machine policy that can't be read: policy.load reports an error.
        policy.set_for_tests(None, error="The fixture machine policy is not valid JSON.")
        return JSONResponse({"unusable": True})

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
                           Route("/__fixture__/upload", start_uploading, methods=["POST"]),
                           Route("/__fixture__/unusable", unusable, methods=["POST"]),
                           Route("/__fixture__/shutdown", shutdown, methods=["POST"])])
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    server = uvicorn.Server(uvicorn.Config(gui.app, host="127.0.0.1", log_level="warning"))
    print(json.dumps({"url": f"http://127.0.0.1:{listener.getsockname()[1]}", "session_id": current.id,
                      "home": str(home), "python": sys.executable}), flush=True)
    server.run(sockets=[listener])


if __name__ == "__main__":
    main()
