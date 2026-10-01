"""Launch an unmodified frozen candidate against a loopback scripted Ollama API.

This proves packaged transport and managed process behavior, not model quality.
The disposable profile has no provider/account credentials. Only fixture-owned
processes are stopped. Invoke through swarm_packaged.browser.cjs.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
import urllib.request


def main():
    root, executable = Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve(strict=True)
    home, workspace = root / "home", root / "project"
    home.mkdir(parents=True)
    workspace.mkdir()
    (workspace / "fact.txt").write_text("Quoted CSV fields preserve commas.\n", encoding="utf-8")
    recovery_fixture = "--writer-recovery" in sys.argv[3:]
    csv_fixture = "--csv" in sys.argv[3:]
    followup_fixture = "--followup-planning" in sys.argv[3:]
    writer_fixture = "--writer" in sys.argv[3:] or recovery_fixture or csv_fixture
    writer_cases = []
    if csv_fixture:
        pack_path = Path(__file__).parent / "swarming"
        seed = json.loads((pack_path / "scenarios.json").read_text("utf-8"))["csv_export"]["seed"]
        references = json.loads((pack_path / "references.json").read_text("utf-8"))["csv_export"]
        for name, content in seed.items():
            (workspace / name).write_text(content, encoding="utf-8", newline="\n")
        writer_cases = [{"path": name, "original": seed[name], "expected": content} for name, content in references.items()]
    if writer_fixture:
        (workspace / "src").mkdir()
        for part in ("backend", "frontend"):
            (workspace / "src" / f"{part}.txt").write_text(f"original {part}\n", encoding="utf-8", newline="\n")
        (workspace / "personal.txt").write_text("committed personal\n", encoding="utf-8", newline="\n")
        (workspace / "verify_changes.py").write_text(
            "from pathlib import Path\nimport json,os,time\n"
            f"root=Path({str(root)!r})\n"
            "(root/'check-started.json').write_text(json.dumps({'pid':os.getpid(),'parent':os.getppid()}))\n"
            "deadline=time.monotonic()+15\n"
            "while not (root/'check-release').exists():\n"
            "    assert time.monotonic()<deadline, 'Check release timeout'\n"
            "    time.sleep(.02)\n"
            "for part in ('backend','frontend'):\n"
            "    assert Path('src',part+'.txt').read_text()=='verified '+part+'\\n'\n"
            "print('Packaged combined changes verified')\n", encoding="utf-8")
    if csv_fixture:
        (root / "verify_csv.py").write_text(
            "from pathlib import Path\nimport importlib.util,json,os,shutil,tempfile,time\n"
            f"root=Path({str(root)!r})\n"
            "(root/'check-started.json').write_text(json.dumps({'pid':os.getpid(),'parent':os.getppid()}))\n"
            "deadline=time.monotonic()+15\n"
            "while not (root/'check-release').exists():\n"
            "    assert time.monotonic()<deadline, 'Check release timeout'\n"
            "    time.sleep(.02)\n"
            f"spec=importlib.util.spec_from_file_location('fixture_pack',{str(pack_path / 'benchmark.py')!r})\n"
            "pack=importlib.util.module_from_spec(spec);spec.loader.exec_module(pack)\n"
            "with tempfile.TemporaryDirectory(prefix='sonn-packaged-csv-verify-') as temporary:\n"
            "    candidate=Path(temporary)/'candidate'\n"
            "    shutil.copytree(Path.cwd(),candidate,ignore=shutil.ignore_patterns('.git','__pycache__'))\n"
            "    result=pack.evaluate('csv_export',candidate)\n"
            "(root/('check-result-'+str(time.time_ns())+'.json')).write_text(json.dumps(result))\n"
            "print(json.dumps(result))\n"
            "raise SystemExit(0 if result['accepted'] else 1)\n", encoding="utf-8")
    for key in ("HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA"):
        os.environ[key] = str(home)
    for key in tuple(os.environ):
        if any(part in key.upper() for part in ("API_KEY", "TOKEN", "SECRET", "PASSWORD")):
            os.environ.pop(key)
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from lumi.gui.settings import DEFAULTS
    from lumi.gui.sessions import ProjectManager
    from lumi.processes import background_process_kwargs
    import psutil

    if writer_fixture:
        for arguments in (("init", "-b", "main"), ("config", "core.autocrlf", "false"),
                          ("config", "user.email", "fixture@example.invalid"), ("config", "user.name", "Packaged Fixture"),
                          ("add", "."), ("-c", "commit.gpgsign=false", "commit", "-m", "Fixture base")):
            subprocess.run(["git", "-C", str(workspace), *arguments], check=True, capture_output=True,
                           timeout=15, **background_process_kwargs())

    trace, lock = [], threading.Lock()
    planning_inputs = []
    gate = threading.Event()
    process = None
    owned = []
    writer_calls = {}

    def start_candidate():
        nonlocal process
        with (root / "candidate-stream.log").open("ab") as output:
            process = subprocess.Popen([str(executable), "gui", "--browser", "--debug", "--host", "127.0.0.1", "--port", str(port)],
                                       cwd=workspace, stdin=subprocess.DEVNULL, stdout=output,
                                       stderr=output, **background_process_kwargs())
        for _ in range(250):
            if process.poll() is not None:
                raise RuntimeError(f"Candidate exited during startup: {process.returncode}")
            try:
                with urllib.request.urlopen(url, timeout=.2) as response:
                    if response.status == 200:
                        return process.pid
            except (OSError, TimeoutError):
                time.sleep(.1)
        raise TimeoutError("Candidate HTTP startup timed out")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def respond(self, payload, *, status=200, media="application/json"):
            raw = payload.encode() if isinstance(payload, str) else json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", media)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            try:
                self.wfile.write(raw)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def do_GET(self):
            if self.path == "/api/tags":
                self.respond({"models": [{"name": "fixture-native", "model": "fixture-native", "size": 1}]})
            elif self.path == "/api/version":
                self.respond({"version": "0.0.0-scripted-fixture"})
            elif self.path == "/api/ps":
                self.respond({"models": []})
            elif self.path == "/__fixture__/evidence":
                children = []
                if process and process.poll() is None:
                    for child in psutil.Process(process.pid).children(recursive=True):
                        try:
                            children.append({"pid": child.pid, "exe": child.exe(), "argv": child.cmdline()})
                        except (psutil.NoSuchProcess, psutil.AccessDenied):
                            pass
                with lock:
                    self.respond({"requests": list(trace), "children": children, "host_pid": process.pid,
                                  "followup_inputs": list(planning_inputs)})
            else:
                self.respond({"error": "Unsupported fixture endpoint"}, status=404)

        def do_POST(self):
            size = int(self.headers.get("Content-Length", "0"))
            if size > 4 * 1024 * 1024:
                self.respond({"error": "Fixture input too large"}, status=413)
                return
            payload = json.loads(self.rfile.read(size) or b"{}")
            if self.path == "/__fixture__/release":
                gate.set()
                self.respond({"released": True})
            elif recovery_fixture and self.path == "/__fixture__/crash":
                if process is None or process.poll() is not None:
                    self.respond({"error": "Fixture host is not running"}, status=409)
                    return
                descendants = psutil.Process(process.pid).children(recursive=True)
                identities = [{"pid": child.pid, "created_at": child.create_time()} for child in descendants]
                owned.extend(descendants)
                old_pid = process.pid
                process.kill()  # Deliberately no child cleanup; Windows job ownership must act.
                process.wait(timeout=5)
                _, alive = psutil.wait_procs(descendants, timeout=5)
                crash = {"host_pid": old_pid, "children": identities, "still_alive": [child.pid for child in alive]}
                (root / "crash-observation.json").write_text(json.dumps(crash, indent=2), encoding="utf-8")
                self.respond(crash)
            elif recovery_fixture and self.path == "/__fixture__/restart":
                if process is not None and process.poll() is None:
                    self.respond({"error": "Fixture host is still running"}, status=409)
                    return
                self.respond({"host_pid": start_candidate()})
            elif self.path == "/api/show":
                self.respond({"capabilities": ["completion", "tools"], "template": "{{.Tools}}",
                              "model_info": {"general.architecture": "llama", "llama.context_length": 32768}})
            elif self.path == "/api/chat":
                messages = payload.get("messages", [])
                observed = any(row.get("role") == "tool" for row in messages)
                planning = next((row.get("content", "") for row in messages if row.get("role") == "user"
                    and "Captured planning data:" in str(row.get("content", ""))), None) if followup_fixture else None
                with lock:
                    trace.append({"model": payload.get("model"), "stream": payload.get("stream"),
                                  "observed_file_result": observed,
                                  "input_sha256": hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()})
                if not payload.get("stream") or payload.get("model") != "fixture-native":
                    self.respond({"error": "Only declared streaming worker calls are allowed"}, status=400)
                    return
                held = not followup_fixture or not planning and "Inspect CSV fixture concern 2" in json.dumps(messages)
                if not observed and held and not gate.wait(60 if followup_fixture else 40):
                    self.respond({"error": "Fixture release was not received"}, status=503)
                    return
                message = ({"role": "assistant", "content": "Packaged scripted finding: quoted CSV fields preserve commas. Owner review remains required."}
                           if observed else {"role": "assistant", "content": "", "tool_calls": [
                               {"function": {"name": "file_read", "arguments": {"path": "fact.txt"}}}]})
                if planning:
                    data = json.loads(planning.split("Captured planning data:\n", 1)[1].split("\n</runtime_message>", 1)[0])
                    with lock:
                        planning_inputs.append(data)
                    later = bool(data["proposed_work_namespace"])
                    proposal = {"summary": "Follow up on the first retained finding." if later else "Two independent initial investigations.",
                        "use_team": True, "work_items": [{"id": f"csv-{index}", "objective": f"Inspect CSV fixture concern {index}",
                            "role": "explore", "dependencies": [], "read_roots": ["."], "write_roots": [],
                            "criteria": ["owner_review"]} for index in ((3,) if later else (1, 2))]}
                    message = {"role": "assistant", "content": json.dumps(proposal)}
                if writer_fixture and not observed:
                    part = "backend" if "Update src/backend.txt" in json.dumps(messages) else "frontend"
                    message = {"role": "assistant", "content": "", "tool_calls": [{"function": {
                        "name": "file_write", "arguments": {"path": f"src/{part}.txt", "content": f"verified {part}\n"}}}]}
                    if csv_fixture:
                        prompt = json.dumps(messages)
                        selected = next(item for item in writer_cases if f"Update {item['path']}" in prompt)
                        name = selected["path"]
                        with lock:
                            writer_calls[name] = writer_calls.get(name, 0) + 1
                            count = writer_calls[name]
                        # Deliberately incorrect first frontend result exercises
                        # actual failed verification and a fresh repair attempt.
                        content = selected["expected"]
                        if name == "frontend.html" and count == 1:
                            content = content.replace("/export.csv", "/pending")
                        message = {"role": "assistant", "content": "", "tool_calls": [{"function": {
                            "name": "file_write", "arguments": {"path": name, "content": content}}}]}
                lines = [{"model": "fixture-native", "message": message, "done": False},
                         {"model": "fixture-native", "done": True, "done_reason": "stop", "prompt_eval_count": 10, "eval_count": 5}]
                self.respond("".join(json.dumps(row) + "\n" for row in lines), media="application/x-ndjson")
            else:
                self.respond({"error": "Unsupported fixture endpoint"}, status=404)

    api = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    api.daemon_threads = True
    endpoint = f"http://127.0.0.1:{api.server_port}"
    threading.Thread(target=api.serve_forever, daemon=True).start()
    settings = deepcopy(DEFAULTS)
    settings["general"].update(default_backend="ollama", default_model="fixture-native")
    settings["network"].update(ollama_url=endpoint, exo_url=endpoint, sonn_url="")
    profile = home / ".lumi"
    profile.mkdir(exist_ok=True)
    (profile / "settings.json").write_text(json.dumps(settings), encoding="utf-8")
    os.environ.update(OLLAMA_HOST=endpoint, EXO_API_URL=endpoint, EXO_BASE_URL=endpoint, SONN_API_URL="", SONN_BASE_URL="")
    manager = ProjectManager(str(workspace))
    manager.set_project(str(workspace))
    session = manager.create_session("ollama", "fixture-native")
    session.title = "Packaged team fixture"
    session.title_source = "manual"
    session.append_display_events([{"event": "user_message", "text": "Inspect the fixture."}])
    session.save()
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    url = f"http://127.0.0.1:{port}"
    receipt = {"url": url, "api_url": endpoint, "project": str(workspace), "session_id": session.id,
               "executable": str(executable), "python": sys.executable,
               "executable_sha256": hashlib.sha256(executable.read_bytes()).hexdigest()}
    if csv_fixture:
        receipt.update(writer_cases=writer_cases, check_script=str(root / "verify_csv.py"))
    try:
        # Windowless Python may still inherit valid streams. Capture both the
        # inherited stream output and the fallback startup log, when present.
        start_candidate()
        receipt["host_pid"] = process.pid
        (root / "candidate.json").write_text(json.dumps(receipt, indent=2), encoding="utf-8")
        print(json.dumps(receipt), flush=True)
        # Node controls this pipe; EOF also owns cleanup on browser failures.
        sys.stdin.readline()
    finally:
        gate.set()
        if process and process.poll() is None:
            parent = psutil.Process(process.pid)
            owned.extend(parent.children(recursive=True))
            parent.terminate()
            owned.append(parent)
        for child in owned:
            try:
                child.terminate()
            except psutil.NoSuchProcess:
                pass
        if owned:
            _, alive = psutil.wait_procs(owned, timeout=5)
            for child in alive:
                child.kill()
            psutil.wait_procs(alive, timeout=5)
        api.shutdown()
        (root / "transport.json").write_text(json.dumps(trace, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
