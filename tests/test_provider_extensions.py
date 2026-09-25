"""Extension SDK v1: model providers from capability packs (lumi/engine/provider_extensions.py).

Providers here are real processes started with this interpreter; the
template pack is made with sdk/new_pack.py, as an extension author would.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from lumi.backends import EVENT_DONE, EVENT_ERROR, EVENT_TEXT_DELTA, EVENT_TOOL_CALL
from lumi.engine import provider_extensions as extensions
from lumi.engine.capability_packs import CapabilityPackManager, approve_pack, version_satisfies
from lumi.paths import state_home

ROOT = Path(__file__).resolve().parents[1]
SDK_PYTHON = ROOT / "sdk" / "python"
sys.path.insert(0, str(SDK_PYTHON))
import lumi_extension  # noqa: E402
from lumi_extension import testing as kit  # noqa: E402


def _new_pack_module():
    spec = importlib.util.spec_from_file_location("lumi_sdk_new_pack", ROOT / "sdk" / "new_pack.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Settings:
    """What provider extensions read from Settings: the pack approvals and saved connections."""

    def __init__(self, plugins=None, connections=None):
        self.values = {"plugins": plugins or {}, "connections": connections or [], "api_keys": {}}

    def get(self, section, key=None, default=None):
        value = self.values.get(section, default)
        return value if key is None else (value or {}).get(key, default)


def _template(folder: Path, *, interpreter: bool = True) -> Path:
    """The provider template, made the way an author makes it, runnable with this Python."""
    _new_pack_module().create(folder, name="Acme models")
    if interpreter:
        manifest = json.loads((folder / "lumi-pack.json").read_text(encoding="utf-8"))
        manifest["providers"][0]["command"] = [sys.executable, "provider.py"]
        (folder / "lumi-pack.json").write_text(json.dumps(manifest), encoding="utf-8")
    return folder


def _approve(settings: Settings) -> Settings:
    for pack in extensions.pack_manager(settings).discover():
        settings.values["plugins"] = approve_pack(settings.values["plugins"], pack, reviewed_digest=pack.digest)
    return settings


def _raw_pack(tmp_path: Path, script: str, name: str = "raw") -> SimpleNamespace:
    folder = tmp_path / name
    folder.mkdir()
    (folder / "provider.py").write_text(script, encoding="utf-8")
    return SimpleNamespace(id=name, name="Raw", path=str(folder))


def _run(pack, request=None, **kwargs):
    return list(extensions.run(pack, [sys.executable, "provider.py"], request or {"method": "models"}, **kwargs))


# ── The manifest ────────────────────────────────────────────────────


def _load(folder: Path, manifest: dict):
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "lumi-pack.json").write_text(json.dumps({"id": "p", "name": "P", **manifest}), encoding="utf-8")
    pack, _ = CapabilityPackManager(folder.parent / "no-project", roots=())._load(folder)
    return pack


@pytest.mark.parametrize("manifest, problem", [
    ({"manifest_version": 1, "providers": [{"id": "a", "command": ["x"]}]}, ""),
    ({"manifest_version": 1, "providers": [{"id": "a", "command": {"windows": ["x.exe"], "linux": ["x"]}}]}, ""),
    ({"manifest_version": 1, "providers": [{"id": "A b", "command": ["x"]}]}, "lowercase letters"),
    ({"manifest_version": 1, "providers": [{"id": "a", "command": ["x"]}, {"id": "a", "command": ["y"]}]}, "two providers"),
    ({"manifest_version": 1, "providers": [{"id": "a"}]}, "needs a command"),
    ({"manifest_version": 1, "providers": [{"id": "a", "command": {"beos": ["x"]}}]}, "needs a command"),
    ({"manifest_version": 1, "providers": [{"id": "a", "command": ["x"], "models": [{"name": "m"}]}]}, "needs an id"),
    ({"manifest_version": 1, "providers": [{"id": "a", "command": ["x"], "models": [{"id": "m", "context_window": 5}]}]},
     "context_window"),
    ({"manifest_version": "1"}, "whole number"),
    ({"manifest_version": 2}, "newer Lumi"),
    ({"manifest_version": 1, "lumi": ">=99"}, "It needs Lumi >=99"),
    ({"manifest_version": 1, "lumi": "latest"}, "version requirement"),
])
def test_the_manifest_is_checked_when_it_loads(tmp_path, manifest, problem):
    pack = _load(tmp_path / "pack", manifest)
    if problem:
        assert problem in pack.problem and pack.status == "unverifiable" and not pack.providers
    else:
        assert pack.problem == "" and pack.manifest_version == 1 and pack.providers[0]["id"] == "a"


def test_packs_from_before_the_sdk_still_load(tmp_path):
    pack = _load(tmp_path / "old", {"skills": []})
    assert (pack.problem, pack.manifest_version, pack.providers) == ("", 0, [])


def test_version_requirements():
    assert version_satisfies("0.19.2.dev11", ">=0.19.2")
    assert version_satisfies("0.20.0", ">=0.19,<1")
    assert not version_satisfies("0.19.1", ">=0.19.2")
    assert not version_satisfies("1.0", "<1")
    assert version_satisfies("1.2", "==1.2.0")


# ── From the template to an answer in a session ─────────────────────


def test_a_template_pack_answers_in_a_session_once_approved(tmp_path, monkeypatch):
    from lumi.connections import create_connection_backend, discover_models, normalize_connection
    from lumi.engine.session import Session

    _template(state_home() / "packs" / "acme")
    settings = Settings()
    connection = normalize_connection({"name": "Acme", "type": "extension", "pack": "acme", "provider": "acme",
                                       "auth": "none", "headers": {"X-Ignored": "1"}})
    assert connection["headers"] == {} and connection["base_url"] == ""
    [row] = extensions.available(settings)
    assert (row["pack"], row["provider"], row["models"], row["ready"]) == ("acme", "acme", ["echo", "remote"], False)
    with pytest.raises(extensions.ProviderExtensionError, match="Approve the Acme models pack"):
        create_connection_backend(connection, "echo", settings=settings)

    _approve(settings)
    assert discover_models(connection, settings=settings) == ["echo", "remote"]
    backend = create_connection_backend(connection, "echo", settings=settings)
    assert backend.health() == {"ok": True, "models": ["echo", "remote"]}
    assert backend.capability_profile.context_window == 32768 and backend.name == "conn-acme"

    project = tmp_path / "project"
    project.mkdir()
    (project / "README.md").write_text("# Hello from the project\n", encoding="utf-8")
    monkeypatch.chdir(project)
    session = Session(backend, max_steps=3, auto_approve=True)
    session.project_path = str(project)
    events = list(session.run('call file_read {"path": "README.md"}'))
    [result] = [event for event in events if event.get("event") == "tool.result"]
    assert result["name"] == "file_read" and "Hello from the project" in result["output"]
    reply = "".join(event.get("delta", "") for event in events if event.get("event") == "text.delta")
    assert reply.startswith("The tool answered:") and "Hello from the project" in reply

    # Running it wrote nothing into the pack (Python's cache goes elsewhere), so it stays approved.
    assert extensions.find_provider("acme", "acme", settings)[0].status == "approved"
    assert not list((state_home() / "packs" / "acme").rglob("__pycache__"))
    assert (state_home() / "extensions" / "acme").is_dir()

    # A changed file turns it off before the next request starts anything.
    provider = state_home() / "packs" / "acme" / "provider.py"
    provider.write_text(provider.read_text(encoding="utf-8") + "\n# edited\n", encoding="utf-8")
    kinds = list(backend.stream("hello", [], "", []))
    assert kinds == [(EVENT_ERROR, {"message": "Approve the Acme models pack in Settings > Capability packs to use "
                                               "its models. It changed since you approved it."})]


def test_providers_run_only_from_personal_packs(tmp_path):
    # A pack inside a project is never a provider, even when approved there.
    project = tmp_path / "repo"
    _template(project / ".lumi" / "packs" / "acme")
    manager = CapabilityPackManager(project)
    [pack] = manager.discover()
    assert pack.scope == "project"
    settings = Settings(approve_pack({}, pack, reviewed_digest=pack.digest))
    assert extensions.available(settings) == []
    with pytest.raises(extensions.ProviderExtensionError, match="isn't installed"):
        extensions.find_provider("acme", "acme", settings)


def test_the_template_ships_passing_tests(tmp_path):
    pack = _template(tmp_path / "my-pack", interpreter=False)
    env = {key: value for key, value in os.environ.items() if not key.startswith("PYTEST")}
    result = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"], cwd=pack, env=env,
                            capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "5 passed" in result.stdout
    manifest = json.loads((pack / "lumi-pack.json").read_text(encoding="utf-8"))
    assert (manifest["id"], manifest["name"], manifest["providers"][0]["id"]) == ("my-pack", "Acme models", "my-pack")
    with pytest.raises(ValueError, match="isn't empty"):
        _new_pack_module().create(pack)


def test_lumi_extension_check(tmp_path, capsys):
    from lumi.extension_check import main

    pack = _template(tmp_path / "acme")
    assert main(["check", str(pack)]) == 0
    out = capsys.readouterr().out
    assert "Acme models 0.1.0 loads (manifest version 1)." in out
    assert "offers echo, remote." in out and "answered: You said: Reply with one short sentence." in out

    (pack / "provider.py").write_text("import sys\nsys.exit('no key configured')\n", encoding="utf-8")
    assert main(["check", str(pack)]) == 1
    captured = capsys.readouterr()
    assert "PROBLEM  Acme models didn't list its models: The provider exited with code 1: no key configured" \
        in captured.out and "1 problem." in captured.err

    (pack / "lumi-pack.json").write_text(json.dumps({"id": "acme", "manifest_version": 3}), encoding="utf-8")
    assert main(["check", str(pack), "--no-run"]) == 1
    assert "It's for a newer Lumi (manifest version 3)." in capsys.readouterr().out


# ── The process and the protocol ────────────────────────────────────


def test_the_provider_gets_its_key_and_folder_but_no_other_keys(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-not-for-extensions-000000000000")
    pack = _raw_pack(tmp_path, "import json, os, sys\nsys.stdin.readline()\n"
                               "print(json.dumps({'type': 'models', 'models': [], 'env': {k: os.environ.get(k) for k in "
                               "('OPENAI_API_KEY', 'LUMI_PROVIDER_API_KEY', 'LUMI_EXTENSION_DATA', "
                               "'LUMI_EXTENSION_PROTOCOL', 'PYTHONPYCACHEPREFIX')}}))\n")
    [answer] = _run(pack, api_key="acme-key-123456789")
    env = answer["env"]
    assert env["OPENAI_API_KEY"] is None and env["LUMI_PROVIDER_API_KEY"] == "acme-key-123456789"
    assert Path(env["LUMI_EXTENSION_DATA"]) == state_home() / "extensions" / "raw"
    assert env["LUMI_EXTENSION_PROTOCOL"] == "1" and not env["PYTHONPYCACHEPREFIX"].startswith(pack.path)
    [answer] = _run(pack)
    assert answer["env"]["LUMI_PROVIDER_API_KEY"] is None


def test_what_a_failing_provider_reports(tmp_path):
    key = "acme-secret-key-abcdef123456"
    crash = _raw_pack(tmp_path, f"import sys\nsys.stderr.write('bad key {key}')\nsys.exit(3)\n", "crash")
    with pytest.raises(extensions.ProviderExtensionError, match="exited with code 3") as caught:
        _run(crash, api_key=key)
    assert key not in str(caught.value) and "[REDACTED" in str(caught.value)
    garbage = _raw_pack(tmp_path, "print('hello')\n", "garbage")
    with pytest.raises(extensions.ProviderExtensionError, match="isn't JSON: hello"):
        _run(garbage)
    slow = _raw_pack(tmp_path, "import time\ntime.sleep(30)\n", "slow")
    with pytest.raises(extensions.ProviderExtensionError, match="didn't finish within 0 seconds"):
        _run(slow, timeout=0.3)


def test_stop_ends_the_process(tmp_path):
    marker = tmp_path / "still-running"
    pack = _raw_pack(tmp_path, "import json, sys, time\nsys.stdin.readline()\n"
                               "print(json.dumps({'type': 'text', 'text': 'thinking'}), flush=True)\n"
                               f"time.sleep(20)\nopen({str(marker)!r}, 'w').write('x')\n")
    stop = threading.Event()
    started = time.monotonic()
    events = []
    for event in extensions.run(pack, [sys.executable, "provider.py"], {"method": "stream"}, cancel_event=stop):
        events.append(event)
        stop.set()
    assert events == [{"type": "text", "text": "thinking"}] and time.monotonic() - started < 10
    time.sleep(0.5)
    assert not marker.exists()


def _backend_for(tmp_path, monkeypatch, script: str):
    pack = _raw_pack(tmp_path, script)
    provider = {"id": "raw", "name": "Raw", "command": [sys.executable, "provider.py"],
                "models": [{"id": "m", "context_window": 9000, "tools": False}]}
    monkeypatch.setattr(extensions, "find_provider", lambda *args: (pack, provider))
    return extensions.ExtensionBackend({"id": "raw", "pack": "raw", "provider": "raw"}, "m", "")


def _events(lines: list[dict]) -> str:
    return "import json, sys\nsys.stdin.readline()\n" + "".join(
        f"print(json.dumps({line!r}), flush=True)\n" for line in lines)


def test_events_become_the_engine_contract(tmp_path, monkeypatch):
    backend = _backend_for(tmp_path, monkeypatch, _events([
        {"type": "text", "text": "Reading."}, {"type": "future_kind", "x": 1},
        {"type": "tool_call", "name": "file_read", "arguments": {"path": "a.py"}},
        {"type": "done", "usage": {"input_tokens": "12", "output_tokens": None}, "cost_usd": 0.25}]))
    assert backend.capability_profile.context_window == 9000 and not backend.capability_profile.native_tools
    events = list(backend.stream("go", [], "", []))
    assert events[0] == (EVENT_TEXT_DELTA, {"delta": "Reading."})
    kind, call = events[1]
    assert kind == EVENT_TOOL_CALL and call["name"] == "file_read" and json.loads(call["arguments"]) == {"path": "a.py"}
    assert call["call_id"]
    assert events[2] == (EVENT_DONE, {"model": "m", "cognitive_state": None, "stats": {
        "input_tokens": 12, "output_tokens": 0, "provider": "conn-raw", "cost_usd": 0.25}})


@pytest.mark.parametrize("lines, message", [
    ([{"type": "text", "text": "partial"}], "Raw's provider stopped without finishing its answer."),
    ([{"type": "error", "message": "quota exceeded"}], "Raw: quota exceeded"),
])
def test_unfinished_and_refused_answers_are_errors(tmp_path, monkeypatch, lines, message):
    backend = _backend_for(tmp_path, monkeypatch, _events(lines))
    assert list(backend.stream("go", [], "", []))[-1] == (EVENT_ERROR, {"message": message})


def test_the_conversation_reaches_the_provider_as_text():
    history = [
        {"role": "user", "content": [{"type": "image", "media_type": "image/png", "data": "AAAA"},
                                     {"type": "text", "text": "What's this?"}]},
        {"role": "tool_call", "name": "screenshot", "arguments": '{"full": true}', "call_id": "c1",
         "content": "Called screenshot", "assistant_content": "Let me look."},
        {"role": "tool_result", "call_id": "c1", "content": "Captured.", "name": "screenshot",
         "image": {"media_type": "image/png", "data": "AAAA"}},
        {"role": "assistant", "content": "A cat."},
    ]
    sent = extensions.messages(history, "Be brief.", "And now?")
    assert sent[0] == {"role": "system", "content": "Be brief."}
    assert "What's this?" in sent[1]["content"] and "Image" in sent[1]["content"]
    assert sent[2] == {"role": "assistant", "content": "Let me look.",
                       "tool_calls": [{"id": "c1", "name": "screenshot", "arguments": {"full": True}}]}
    assert sent[3]["role"] == "tool" and sent[3]["content"].startswith("Captured.") and "screenshot" in sent[3]["content"]
    assert sent[-1] == {"role": "user", "content": "And now?"}
    # The message the history just recorded isn't sent twice.
    assert extensions.messages([{"role": "user", "content": "hi"}], "", "hi") == [{"role": "user", "content": "hi"}]


def test_programs_come_from_path_or_the_pack_never_the_current_folder(tmp_path, monkeypatch):
    here = tmp_path / "repo"
    here.mkdir()
    name = "lumi-test-program"
    program = here / (name + (".cmd" if os.name == "nt" else ""))
    program.write_text("@echo off\n" if os.name == "nt" else "#!/bin/sh\n", encoding="utf-8")
    program.chmod(0o755)
    monkeypatch.chdir(here)
    for path in ("", ".", os.pathsep.join([".", "relative"])):
        with pytest.raises(extensions.ProviderExtensionError, match="isn't on PATH"):
            extensions.find_program(name, path)
    assert Path(extensions.find_program(name, str(here))) == program

    pack = SimpleNamespace(name="Acme", path=str(tmp_path / "pack"))
    (tmp_path / "pack" / "bin").mkdir(parents=True)
    system = extensions.SYSTEMS.get(sys.platform, "linux")
    assert extensions.command_for(pack, {"command": {system: ["bin/acme", "--serve"]}}) == \
        [str((tmp_path / "pack" / "bin" / "acme").resolve()), "--serve"]
    with pytest.raises(extensions.ProviderExtensionError, match="outside its pack"):
        extensions.command_for(pack, {"command": ["../elsewhere/acme"]})
    other = "linux" if system != "linux" else "windows"
    with pytest.raises(extensions.ProviderExtensionError, match="doesn't run on this system"):
        extensions.command_for(pack, {"name": "Acme", "command": {other: ["acme"]}})


def test_connections_page_lists_the_providers(tmp_path):
    from lumi.gui.ws_commands import _connections_payload

    _template(state_home() / "packs" / "acme")
    settings = _approve(Settings(connections=[{"name": "Acme", "type": "extension", "pack": "acme",
                                               "provider": "acme", "auth": "none"}]))
    data = _connections_payload(SimpleNamespace(settings=settings))["data"]
    assert data["types"]["extension"] == "A provider from a capability pack"
    assert [(row["pack_name"], row["name"], row["ready"]) for row in data["extension_providers"]] == \
        [("Acme models", "Acme models", True)]
    assert [(item["pack"], item["provider"]) for item in data["items"]] == [("acme", "acme")]


@pytest.mark.parametrize("raw, message", [
    ({"name": "X", "type": "extension", "pack": "acme"}, "names its pack and the pack's provider"),
    ({"name": "X", "type": "extension", "pack": "acme", "provider": "acme", "auth": "oauth"}, "LUMI_PROVIDER_API_KEY"),
])
def test_extension_connections_are_validated(raw, message):
    from lumi.connections import normalize_connection

    with pytest.raises(ValueError, match=message):
        normalize_connection(raw)


# ── The Python SDK ──────────────────────────────────────────────────


class Echo(lumi_extension.Provider):
    def models(self):
        return [{"id": "e"}]

    def stream(self, request):
        if request.model == "boom":
            raise RuntimeError("model crashed")
        yield lumi_extension.text(request.messages[-1]["content"])


def _serve(request) -> tuple[int, list[dict]]:
    out = io.StringIO()
    code = lumi_extension.serve(Echo(), io.StringIO(request if isinstance(request, str) else json.dumps(request)), out)
    return code, [json.loads(line) for line in out.getvalue().splitlines()]


def test_the_sdk_answers_lumis_requests():
    assert _serve({"lumi_extension": 1, "method": "models"}) == (0, [{"type": "models", "models": [{"id": "e"}]}])
    code, events = _serve({"lumi_extension": 1, "method": "stream",
                           "params": {"model": "e", "messages": [{"role": "user", "content": "hi"}]}})
    # An answer that forgets done() still ends with one.
    assert code == 0 and events == [{"type": "text", "text": "hi"}, lumi_extension.done()]
    code, events = _serve({"lumi_extension": 1, "method": "stream", "params": {"model": "boom"}})
    assert code == 1 and events == [{"type": "error", "message": "RuntimeError: model crashed"}]
    assert _serve("not json")[0] == 2 and _serve({"lumi_extension": 2})[1][0]["type"] == "error"
    assert lumi_extension.done(3, 4, cost_usd=0.5) == {"type": "done", "cost_usd": 0.5,
                                                      "usage": {"input_tokens": 3, "output_tokens": 4}}


def test_the_test_kit_says_what_lumi_would_reject():
    assert kit.check_stream([{"type": "text", "text": "a"}, {"type": "done"}]) == []
    assert kit.check_stream([{"type": "text", "text": "a"}]) == ["End the answer with done() or error()."]
    assert kit.check_stream([{"type": "done"}, {"type": "text", "text": "late"}])[0].startswith("End the answer")
    assert "a tool_call needs a name" in kit.check_stream([{"type": "tool_call", "arguments": []}, {"type": "done"}])[0]
    assert kit.check_models([{"type": "models", "models": [{"id": "m", "context_window": "big"}]}]) == \
        ["m: context_window must be a number of tokens."]


def test_the_manifest_schema_accepts_the_template():
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads((ROOT / "sdk" / "schema" / "lumi-pack.schema.json").read_text(encoding="utf-8"))
    template = json.loads((ROOT / "sdk" / "templates" / "provider-python" / "lumi-pack.json").read_text(encoding="utf-8"))
    jsonschema.validate(template, schema)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({**template, "providers": [{"id": "Bad Id", "command": []}]}, schema)
