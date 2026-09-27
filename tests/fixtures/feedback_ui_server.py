"""Isolated source-app server for the Send feedback browser check (tests/feedback.browser.cjs).

Home, settings and Lumi's startup log live under the folder given as the first
argument; no model is called, and only loopback connections are allowed. Lumi
Cloud is a route on this same server (``/__fixture__/cloud``, the address
settings give Lumi Cloud), which answers POST /api/v1/feedback as the feedback
contract says (201 acknowledging the report's Idempotency-Key, 200 for a
replay) and GET /api/v1/feedback/info, unless ``/__fixture__/cloud-mode`` says
it's down (503), doesn't take feedback (404) or is slow. ``/__fixture__/policy``
sets an organization policy's ``settings``, and ``/__fixture__/signed-in-elsewhere``
a sign-in another Lumi Cloud issued. ``/__fixture__/evidence`` returns what the
route received, the reports waiting on this computer and the audit log's
feedback records, so the check can compare them with what the dialog showed.
Never a qualification against a real Lumi Cloud.
"""
from __future__ import annotations

import json
import os
import socket
import sys
from pathlib import Path

# What a secret looks like in the log and in a message: the report must never carry them.
TOKEN = "ghp_" + "Z9y8" * 9
SAVED_KEY = "sk-fixture-saved-provider-key-0123456789"


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
    os.environ.pop("LUMI_POLICY_FILE", None)
    for key in tuple(os.environ):
        if any(part in key.upper() for part in ("API_KEY", "TOKEN", "SECRET", "PASSWORD")):
            os.environ.pop(key)
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    cloud_url = f"{base}/__fixture__/cloud"
    state_dir = home / ".lumi"
    (state_dir / "logs").mkdir(parents=True)
    (state_dir / "settings.json").write_text(json.dumps({
        "cloud": {"url": cloud_url},
        "api_keys": {"openai": SAVED_KEY},
    }), encoding="utf-8")
    (state_dir / "logs" / "lumi-startup.log").write_text(
        "=== lumi gui pid=4242\n"
        f'  File "{home}\\lumi\\gui\\app.py", line 12, in start\n'
        f"GitHub token in a stack: {TOKEN}\n"
        f"Provider key echoed by a library: {SAVED_KEY}\n"
        "WARNING: uvicorn started\n", encoding="utf-8")
    os.chdir(workspace)
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    # Only loopback connections: no provider, account or real Lumi Cloud is ever reached.
    original_connect = socket.socket.connect

    def local_connect(sock, address):
        if not isinstance(address, tuple) or address[0] not in {"127.0.0.1", "::1", "localhost"}:
            raise OSError("Fixture prohibits non-loopback connections")
        return original_connect(sock, address)

    socket.socket.connect = local_connect

    from lumi import policy as lumi_policy

    # No organization policy from this machine.
    lumi_policy._registry_policy = lambda: None
    lumi_policy._macos_managed_policy = lambda: None
    lumi_policy._machine_file_policy = lambda: None
    lumi_policy.load(force=True)

    import uvicorn
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    from lumi import offline
    from lumi.engine import Session
    from lumi.gui import app as gui
    from lumi.gui.runtime import BackendSpec
    from tests.streaming_stub import StreamingBackend, done, text_delta

    state = gui.state
    state.project.set_project(str(workspace))
    state.apply_project_context(str(workspace), refresh_index=False)
    current = state.project.create_session("ollama", "fixture-native")
    current.title = "Feedback fixture conversation"
    current.save()
    spec = BackendSpec("ollama", "fixture-native")
    state.backend_spec = spec
    state.backend = StreamingBackend(name=spec.backend_type, model=spec.model,
                                     events=[text_delta("Noted."), done(model=spec.model)])
    state.session = Session(backend=state.backend, project_instructions="Isolated feedback browser fixture.")
    state.session.project_path = str(workspace)
    state.available_backends = {"ollama": {"models": [spec.model]}}
    state.detect_backends = lambda *args, **kwargs: None

    received: list[dict] = []
    kept: dict[str, str] = {}  # Idempotency-Key -> Lumi Cloud's id
    mode = {"cloud": "up"}

    async def cloud_feedback(request):
        """Lumi Cloud's POST /api/v1/feedback, as the contract says; 503 down, 404 not taking feedback."""
        import asyncio

        body = await request.body()
        key = request.headers.get("idempotency-key", "")
        status = {"down": 503, "notfound": 404}.get(mode["cloud"], 200 if key in kept else 201)
        received.append({"status": status, "authorization": bool(request.headers.get("authorization")),
                         "content_type": request.headers.get("content-type"), "idempotency_key": key,
                         "body": json.loads(body)})
        if status == 503:
            return JSONResponse({"error": "unavailable"}, status_code=503)
        if status == 404:
            return JSONResponse({"detail": "Not Found"}, status_code=404)
        if mode["cloud"] == "slow":
            await asyncio.sleep(1.5)
        kept.setdefault(key, f"fbk_{len(kept) + 1:04d}")
        return JSONResponse({"id": kept[key], "report": key, "account": bool(request.headers.get("authorization"))},
                            status_code=status)

    async def cloud_info(request):
        return JSONResponse({"accepting": mode["cloud"] != "notfound", "operator": "Fixture Operator"})

    async def set_policy(request):
        from lumi import policy as fixture_policy

        chosen = (await request.json()).get("settings") or {}
        fixture_policy.set_for_tests(fixture_policy.parse({"schema": "lumi.policy/v1", "organization": "Fixture Co",
                                                           "settings": chosen}, source="fixture") if chosen else None)
        return JSONResponse({"settings": chosen})

    async def signed_in_elsewhere(request):
        state.cloud.settings.set("api_keys", "lumi_cloud_refresh", "fixture-refresh-token-of-another-cloud")
        state.cloud.settings.update_section("cloud", {
            "account_url": "https://other.example.test",
            "account": {"user_id": "usr_other", "email": "ada@example.com", "organizations": []}})
        return JSONResponse(state.cloud.status())

    async def set_mode(request):
        mode["cloud"] = (await request.json()).get("mode", "up")
        return JSONResponse(mode)

    async def set_url(request):
        state.cloud.settings.update_section("cloud", {"url": str((await request.json()).get("url") or "")})
        return JSONResponse({"url": state.cloud.url})

    async def set_offline(request):
        data = await request.json()
        offline.set_for_tests(enabled=bool(data.get("enabled")), allowed_hosts=tuple(data.get("allowed") or ()))
        return JSONResponse({"enabled": offline.enabled()})

    async def evidence(request):
        queue_file = state_dir / "feedback" / "queue.json"
        audit = [json.loads(line) for path in sorted((state_dir / "audit").glob("*.jsonl"))
                 for line in path.read_text(encoding="utf-8").splitlines()]
        return JSONResponse({"kind": "source-app-feedback-browser", "live_cloud_called": False,
                             "received": received,
                             "queue": json.loads(queue_file.read_text(encoding="utf-8"))["items"]
                             if queue_file.exists() else [],
                             "audit": [record for record in audit if record["type"].startswith("feedback.")],
                             "audit_text": json.dumps(audit),
                             "home": str(home), "token": TOKEN, "saved_key": SAVED_KEY})

    async def shutdown(request):
        server.should_exit = True
        return JSONResponse({"stopping": True})

    async def fixture_launch(request):
        # The page redeems a one-time launch code (lumi/gui/local_access.py).
        from lumi.gui.local_access import access
        return JSONResponse({"url": access.launch_url(str(request.base_url))})

    gui.app.routes.extend([
        Route("/__fixture__/launch", fixture_launch),
        Route("/__fixture__/evidence", evidence),
        Route("/__fixture__/cloud/api/v1/feedback", cloud_feedback, methods=["POST"]),
        Route("/__fixture__/cloud/api/v1/feedback/info", cloud_info, methods=["GET"]),
        Route("/__fixture__/policy", set_policy, methods=["POST"]),
        Route("/__fixture__/signed-in-elsewhere", signed_in_elsewhere, methods=["POST"]),
        Route("/__fixture__/cloud-mode", set_mode, methods=["POST"]),
        Route("/__fixture__/cloud-url", set_url, methods=["POST"]),
        Route("/__fixture__/offline", set_offline, methods=["POST"]),
        Route("/__fixture__/shutdown", shutdown, methods=["POST"]),
    ])
    server = uvicorn.Server(uvicorn.Config(gui.app, host="127.0.0.1", log_level="warning"))
    listener.listen(64)  # connections made once the check reads the line below wait for the server
    print(json.dumps({"url": base, "session_id": current.id, "home": str(home), "python": sys.executable}), flush=True)
    server.run(sockets=[listener])


if __name__ == "__main__":
    main()
