"""Isolated source-app server for the real WebSocket browser check.

Only inference is scripted. The shipped template, app, WS handlers, supervisor,
worker Session, file tools and durable state execute normally. Never a live-model
or packaged-desktop qualification. Invoked by swarm_app.browser.cjs.

The conversation starts in a new install's mode (Auto-edit), or in the one
``--mode <ask|auto-edit|plan|bypass>`` names; the evidence reports it.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
from dataclasses import asdict


def main() -> None:
    root = Path(sys.argv[1]).resolve()
    root.mkdir(exist_ok=True)
    home = root / "home"
    home.mkdir()
    workspace = root / "project"
    workspace.mkdir()
    (workspace / "fact.txt").write_text("Fixture fact: quoted CSV fields preserve commas.\n", encoding="utf-8")
    writer_fixture = "--writer" in sys.argv[2:]
    if writer_fixture:
        (workspace / "src").mkdir()
        for part in ("backend", "frontend"):
            (workspace / "src" / f"{part}.txt").write_text(f"original {part}\n", encoding="utf-8", newline="\n")
        (workspace / "personal.txt").write_text("committed personal\n", encoding="utf-8", newline="\n")
        (workspace / "verify_changes.py").write_text(
            "from pathlib import Path\n"
            "for part in ('backend', 'frontend'):\n"
            "    assert Path('src', part + '.txt').read_text() == 'verified ' + part + '\\n'\n"
            "print('Both independently written files verified')\n", encoding="utf-8")
    for key in ("HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA"):
        os.environ[key] = str(home)
    for key in tuple(os.environ):
        if any(part in key.upper() for part in ("API_KEY", "TOKEN", "SECRET", "PASSWORD")):
            os.environ.pop(key)
    os.chdir(workspace)
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    # Fail closed on accidental provider/account discovery, even if future
    # startup paths begin probing. The only permitted transport is loopback.
    original_connect = socket.socket.connect
    def local_connect(sock, address):
        if not isinstance(address, tuple) or address[0] not in {"127.0.0.1", "::1", "localhost"}:
            raise OSError("Fixture prohibits non-loopback connections")
        return original_connect(sock, address)
    socket.socket.connect = local_connect

    import uvicorn
    from starlette.responses import JSONResponse
    from starlette.routing import Route
    from lumi.engine import Session
    from lumi.engine.swarming.service import SwarmRuntime
    from lumi.gui import app as gui
    from lumi.gui.runtime import BackendSpec
    from tests.streaming_stub import StreamingBackend, done, text_delta, tool_call

    if writer_fixture:
        from lumi.processes import background_process_kwargs
        for arguments in (("init", "-b", "main"), ("config", "core.autocrlf", "false"), ("config", "user.email", "fixture@example.invalid"),
                          ("config", "user.name", "Browser Fixture"), ("add", "."), ("commit", "-m", "Fixture base")):
            subprocess.run(["git", "-C", str(workspace), *arguments], check=True, capture_output=True,
                           timeout=15, **background_process_kwargs())

    instances = []
    stream_gate = threading.Event()
    followup_inputs = []
    participant_gates = {(name, number): threading.Event() for name in ("one", "two", "three") for number in (1, 2)}
    participant_inputs = []
    class FixtureBackend(StreamingBackend):
        def stream(self, **kwargs):
            history = kwargs.get("conversation_history", [])
            if "--followup-planning" in sys.argv[2:] and not self.stream_count:
                planning = next((row.get("content", "") for row in history if row.get("role") == "user"
                    and "Captured planning data:" in str(row.get("content", ""))), None)
                if planning:
                    data = json.loads(planning.split("Captured planning data:\n", 1)[1].split("\n</runtime_message>", 1)[0])
                    followup_inputs.append(data)
                elif "Inspect CSV fixture concern 2" in json.dumps(history) and not stream_gate.wait(60):
                    raise TimeoutError("Fixture peer must be explicitly released")
            if "--participant-controls" in sys.argv[2:]:
                history = kwargs.get("conversation_history", [])
                name = next(name for name in ("one", "two", "three") if f"Participant {name}" in json.dumps(history))
                number = self.stream_count + 1
                participant_inputs.append({"participant": name, "request_number": number,
                    "guidance_prepared": any(row.get("role") == "user" and row.get("input_origin") == "generated"
                        and "Focus on quoted CSV fields." in str(row.get("content")) for row in history)})
                if (number == 1 or (name == "one" and number == 2)) and not participant_gates[name, number].wait(45):
                    raise TimeoutError("Fixture participant gate was not explicitly released")
            if getattr(self, "_fixture_hold", False) and not self.stream_count and not stream_gate.wait(25):
                raise TimeoutError("Fixture worker gate was not explicitly released")
            if writer_fixture and not self.stream_count:
                prompt = json.dumps(kwargs.get("conversation_history", []))
                part = "backend" if "Update src/backend.txt" in prompt else "frontend"
                self._scripts = [
                    [tool_call("file_write", {"path": f"src/{part}.txt", "content": f"verified {part}\n"}), done(model=self.model)],
                    [text_delta(f"Changed isolated {part} file. The combined check and owner review are still required."), done(model=self.model)],
                ]
            autonomous = "--autonomous" in sys.argv[2:]
            if autonomous and not self.stream_count and "Inspect CSV fixture concern 1" in json.dumps(history):
                # The first worker asks the orchestrator a question and waits for its answer.
                self._scripts = [
                    [tool_call("file_read", {"path": "fact.txt"}), done(model=self.model)],
                    [tool_call("swarm_send", {"recipient_attempt_id": "orchestrator", "kind": "question",
                                              "body": "Should the CSV export also be checked for semicolons?",
                                              "command_id": "fixture-question"}), done(model=self.model)],
                    [tool_call("swarm_receive", {"wait_seconds": 30}), done(model=self.model)],
                    [text_delta("Scripted reader finding: quoted CSV fields preserve commas."), done(model=self.model)],
                ]
            if autonomous and not self.stream_count and "Captured answer data:" in json.dumps(history):
                # The orchestrator's answer turn replies to each question's sender.
                answer = next(row.get("content", "") for row in history if row.get("role") == "user"
                              and "Captured answer data:" in str(row.get("content", "")))
                data = json.loads(answer.split("Captured answer data:\n", 1)[1].split("\n</runtime_message>", 1)[0])
                self._scripts = [
                    [tool_call("swarm_send", {"recipient_attempt_id": question["from_attempt_id"], "kind": "answer",
                                              "body": "No: the export writes commas only, so semicolons need no check.",
                                              "command_id": f"fixture-answer-{question['sequence']}"})
                     for question in data["untrusted_questions"]] + [done(model=self.model)],
                    [text_delta("Answered the worker's question."), done(model=self.model)],
                ]
            if autonomous and not self.stream_count and "Captured planning data:" in json.dumps(history):
                planning = next(row.get("content", "") for row in history if row.get("role") == "user"
                                and "Captured planning data:" in str(row.get("content", "")))
                data = json.loads(planning.split("Captured planning data:\n", 1)[1].split("\n</runtime_message>", 1)[0])
                followup_inputs.append(data)
                if writer_fixture and data["proposed_work_namespace"] is None:
                    # Two writers the orchestrator may apply once the owner's check passes.
                    check = next(name for name in data["allowed_criteria"] if name != "owner_review")
                    proposal = {"summary": "Two writers update the backend and frontend files.", "use_team": True,
                        "work_items": [{"id": part, "objective": f"Update src/{part}.txt", "role": "implement",
                            "dependencies": [], "read_roots": ["src"], "write_roots": [f"src/{part}.txt"],
                            "criteria": [check]} for part in ("backend", "frontend")]}
                elif writer_fixture:
                    proposal = {"summary": "**Final report:** both files hold their verified values.\n\n"
                                           "- `src/backend.txt`: verified\n- `src/frontend.txt`: verified",
                                "use_team": False, "work_items": []}
                elif data["proposed_work_namespace"] is None:
                    proposal = {"summary": "Two CSV investigations, then a report.", "use_team": True,
                        "work_items": [{"id": f"csv-{index}", "objective": f"Inspect CSV fixture concern {index}",
                            "role": "explore", "dependencies": [], "read_roots": ["."], "write_roots": [],
                            "criteria": ["owner_review"]} for index in (1, 2)]}
                else:
                    messages = data.get("untrusted_messages_to_orchestrator", [])
                    proposal = {"summary": f"Final report: quoted CSV fields preserve commas. The orchestrator read "
                                           f"{len(data['recent_untrusted_findings'])} findings and {len(messages)} worker "
                                           "question, and semicolons need no separate check.",
                                "use_team": False, "work_items": []}
                self._scripts = [[text_delta(json.dumps(proposal)), done(model=self.model)]]
            elif not self.stream_count and "Captured planning data:" in json.dumps(kwargs.get("conversation_history", [])):
                proposal = {"summary": "Three independent CSV investigations share two worker slots.", "use_team": True,
                    "work_items": [{"id": f"csv-{index}", "objective": f"Inspect CSV fixture concern {index}",
                        "role": "explore", "dependencies": [], "read_roots": ["."], "write_roots": [],
                        "criteria": ["owner_review"]} for index in (1, 2, 3)]}
                if "--followup-planning" in sys.argv[2:]:
                    later = bool(followup_inputs[-1]["proposed_work_namespace"])
                    proposal["summary"] = "Follow up on the first retained finding." if later else "Two independent initial investigations."
                    proposal["work_items"] = proposal["work_items"][2:] if later else proposal["work_items"][:2]
                self._scripts = [[text_delta(json.dumps(proposal)), done(model=self.model)]]
            yield from super().stream(**kwargs)

    def factory(spec):
        backend = FixtureBackend(name=spec.backend_type, model=spec.model, scripts=[
            [tool_call("file_read", {"path": "fact.txt"}), done(model=spec.model)],
            [text_delta("Scripted reader finding: quoted CSV fields preserve commas. Independent review remains required."), done(model=spec.model)],
        ])
        backend._fixture_hold = "--hold-first-workers" in sys.argv[2:] and len(instances) < 2
        instances.append(backend)
        return backend

    # This computer user accepted Lumi's terms already, as in the app (lumi/terms.py): the checks here
    # are about other things, and the terms dialog would lock the message box first. Workers
    # started from here share this home, so the acceptance counts for them too.
    from lumi import terms as lumi_terms

    lumi_terms.accept({doc.id: doc.version for doc in lumi_terms.required()}, "app")
    state = gui.state
    state.project.set_project(str(workspace))
    state.apply_project_context(str(workspace), refresh_index=True)
    other = state.project.create_session("ollama", "fixture-native")
    other.title = "Other fixture conversation"
    if "--collaboration" in sys.argv[2:]:
        other.append_display_events([{"event": "user_message", "text": "Prepare my independent collaboration investigation."}])
    other.save()
    current = state.project.create_session("ollama", "fixture-native")
    current.title = "Team fixture conversation"
    current.append_display_events([{"event": "user_message", "text": "Inspect this isolated fixture project."}])
    current.save()
    spec = BackendSpec("ollama", "fixture-native")
    state.backend_spec = spec
    state.backend = StreamingBackend(name=spec.backend_type, model=spec.model)
    state.session = Session(backend=state.backend, project_instructions="Isolated browser fixture.")
    state.session.project_path = str(workspace)
    arguments = sys.argv[2:]
    if "--mode" in arguments:
        # The conversation's permission mode, as the mode menu sets it.
        state.apply_permission_mode(arguments[arguments.index("--mode") + 1], session=state.session)
    # An orchestrated fixture also offers a second model for the team's workers.
    state.available_backends = {"ollama": {"models": [spec.model] + (["fixture-worker"] if "--autonomous" in sys.argv[2:] else [])}}
    state.detect_backends = lambda *args, **kwargs: None
    state._swarm_desktop = SwarmRuntime(state.settings, backend_factory=factory, state_root=lambda _: root / "swarm-state")
    history_evidence = None
    if "--history" in sys.argv[2:]:
        from dataclasses import replace
        from lumi.engine.swarming.artifacts import SwarmArtifacts
        from lumi.gui.swarming import _capture
        capture = _capture(state, {"project": str(workspace), "session_id": current.id}, state._swarm_desktop)
        store = state._swarm_desktop._store(capture)
        artifacts = SwarmArtifacts(store)
        text = "Retained first page: 🪷 café <script>window.evidenceExecuted=true</script>\r\n" + ("Quoted CSV, exact saved evidence.\n" * 800) + "Final retained line: complete evidence."
        for index in range(23):
            authority = store.create_run(capture.scope, supervisor_id=f"private-history-supervisor-{index}",
                run_id=f"history-{index:02}", objective=f"Retained team {index:02}", request_limit=2)
            if index == 0:
                store.add_work_item(authority, work_item_id="history-work", objective="Inspect preserved content")
                context = store.claim(authority, work_item_id="history-work", worker_id="history-reader", requests=1, command_id="history-claim").context
                ref = artifacts.publish_text(context, text, label="Long retained text")
                image = artifacts.publish_bytes(context, b"not-an-actual-image", kind="image", media_type="image/png", label="Retained image fixture")
                history_evidence = {"run_id": authority.run_id, "artifact_id": ref.id, "image_id": image.id,
                    "sha256": ref.sha256, "text": text}
                store.finish_attempt(authority, context, outcome="cancelled", used_requests=0, command_id="history-finished")
            store.stop(authority, command_id=f"history-stop-{index}")
        foreign = store.create_run(replace(capture.scope, session_id=other.id), supervisor_id="foreign-history-supervisor",
            run_id="foreign-history-team", objective="Other conversation private team", request_limit=1)
        store.stop(foreign, command_id="foreign-history-stop")
    if "--recovery" in sys.argv[2:]:
        from lumi.gui.swarming import _capture
        from lumi.processes import background_process_kwargs
        capture = _capture(state, {"project": str(workspace), "session_id": current.id}, state._swarm_desktop)
        store = state._swarm_desktop._store(capture)
        seed_path = root / "recovery-seed.json"
        seed_path.write_text(json.dumps({"scope": asdict(capture.scope), "store_path": str(store.path),
            "workspace": str(workspace), "trace_path": str(root / "interruption-trace.txt")}), encoding="utf-8")
        # HOME is isolated after this interpreter starts. Carry its already
        # loaded dependency locations without executing new startup hooks.
        paths = [entry for entry in sys.path if entry and Path(entry).is_dir() and Path(entry).resolve() != workspace]
        seed_script = str(Path(__file__).with_name("swarming_recovery_seed.py"))
        bootstrap = f"import sys,runpy;sys.path[:0]={paths!r};sys.argv={[seed_script, str(seed_path)]!r};runpy.run_path({seed_script!r},run_name='__main__')"
        seed = subprocess.run([sys.executable, "-I", "-S", "-c", bootstrap],
            capture_output=True, text=True, timeout=15, **background_process_kwargs())
        if seed.returncode:
            raise RuntimeError(f"Recovery fixture seed failed: {seed.stderr}")
        expiry = store.snapshot(capture.scope, "fixture-crashed-team")["run"]["lease_until"]
        time.sleep(max(0, expiry - store.clock()) + .05)

    async def evidence(request):
        runs = []
        for run_id, (capture, _) in state._swarm_desktop._runners.items():
            runs.append(state._swarm_desktop.operate(capture, {"action": "view", "request_id": "fixture-evidence", "run_id": run_id}))
        return JSONResponse({"kind": "source-app-scripted-native-browser", "live_providers_called": False,
            "writer_runtime": "trusted same-process fixture only; production uses managed children" if writer_fixture else None,
            "participant_inputs": participant_inputs,
            "followup_inputs": followup_inputs,
            "history_fixture": history_evidence,
            "backend_instances": len(instances), "backend_requests": sum(item.stream_count for item in instances),
            "permission_mode": state.permission_mode,
            "current_session_id": state.project.current_session.id, "runs": runs})

    async def shutdown(request):
        stream_gate.set()
        for gate in participant_gates.values():
            gate.set()
        server.should_exit = True
        return JSONResponse({"stopping": True})

    async def release_workers(request):
        stream_gate.set()
        return JSONResponse({"released": True})

    async def release_participant(request):
        payload = await request.json()
        participant_gates[payload["participant"], payload["request_number"]].set()
        return JSONResponse({"released": True})

    async def fixture_launch(request):
        # The app page redeems a one-time launch code for this process's
        # access token (lumi/gui/local_access.py); every page load needs one.
        from lumi.gui.local_access import access
        return JSONResponse({"url": access.launch_url(str(request.base_url))})
    gui.app.routes.append(Route("/__fixture__/launch", fixture_launch))
    gui.app.routes.extend([Route("/__fixture__/evidence", evidence), Route("/__fixture__/shutdown", shutdown, methods=["POST"]),
                          Route("/__fixture__/release-workers", release_workers, methods=["POST"])])
    gui.app.routes.append(Route("/__fixture__/release-participant", release_participant, methods=["POST"]))
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    server = uvicorn.Server(uvicorn.Config(gui.app, host="127.0.0.1", log_level="warning"))
    print(json.dumps({"url": f"http://127.0.0.1:{listener.getsockname()[1]}",
        "session_id": current.id, "other_session_id": other.id, "workspace": str(workspace), "python": sys.executable}), flush=True)
    server.run(sockets=[listener])


if __name__ == "__main__":
    main()
