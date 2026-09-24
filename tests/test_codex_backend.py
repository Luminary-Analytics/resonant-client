import io
import json
import threading
from concurrent.futures import ThreadPoolExecutor

from lumi.backends import (
    CodexCliBackend,
    EVENT_DONE,
    EVENT_TEXT_DELTA,
    _build_codex_prompt,
    codex_cli_model_labels,
    codex_cli_models,
    resolve_codex_cli_path,
)


def test_codex_models_include_configured_model(monkeypatch):
    monkeypatch.delenv("LUMI_CODEX_MODELS", raising=False)

    models = codex_cli_models({"model": "gpt-6-preview"})

    assert models[0] == "gpt-6-preview"
    assert "gpt-5.5" in models
    assert "gpt-6-astra" in models


def test_codex_astra_is_available_without_changing_the_default(monkeypatch):
    monkeypatch.delenv("LUMI_CODEX_MODELS", raising=False)
    monkeypatch.setattr("lumi.backends._load_codex_config", lambda: {})

    assert codex_cli_models()[0] == "gpt-5.5"
    assert "gpt-6-astra" in codex_cli_models()
    assert codex_cli_model_labels()["gpt-6-astra"] == "GPT-6 Astra"


def test_codex_models_can_be_overridden_by_env(monkeypatch):
    monkeypatch.setenv("LUMI_CODEX_MODELS", "alpha,beta")

    assert codex_cli_models({"model": "alpha"}) == ["alpha", "beta"]


def test_resolve_codex_cli_path_prefers_configured_bundled_cli(monkeypatch, tmp_path):
    configured = tmp_path / "bundled-codex.exe"
    configured.write_text("", encoding="utf-8")
    path_cli = tmp_path / "path-codex.exe"
    path_cli.write_text("", encoding="utf-8")
    monkeypatch.delenv("LUMI_CODEX_CLI", raising=False)
    monkeypatch.delenv("CODEX_CLI_PATH", raising=False)
    monkeypatch.setattr(
        "lumi.backends._load_codex_config",
        lambda: {"mcp_servers": {"node_repl": {"env": {"CODEX_CLI_PATH": str(configured)}}}},
    )
    monkeypatch.setattr("lumi.backends.shutil.which", lambda _: str(path_cli))

    assert resolve_codex_cli_path() == str(configured)


def test_codex_prompt_uses_native_tools_not_resonant_xml():
    instructions = (
        "You have tools. Use <tool_call> tags.\n"
        "--- PROJECT INSTRUCTIONS (RESONANT.md) ---\n"
        "Keep it tight.\n"
        "--- END PROJECT INSTRUCTIONS ---"
    )

    prompt = _build_codex_prompt(
        user_msg="fix it",
        conversation_history=[{"role": "assistant", "content": "prior"}],
        instructions=instructions,
        cwd="D:/Repo",
    )

    assert "Do not emit Lumi <tool_call> XML" in prompt
    assert "Keep it tight." in prompt
    assert "prior" in prompt
    assert "fix it" in prompt


def test_codex_prompt_does_not_duplicate_current_user_turn():
    prompt = _build_codex_prompt(
        user_msg="fix it",
        conversation_history=[{"role": "user", "content": "fix it"}],
        instructions="",
        cwd="D:/Repo",
    )

    assert prompt.count("fix it") == 1
    assert "CONVERSATION HISTORY" not in prompt


def test_codex_command_uses_supported_noninteractive_permission_flags(tmp_path):
    backend = CodexCliBackend(
        "gpt-5.5",
        cwd=str(tmp_path),
        cli_path="codex",
        permission_mode="bypass",
    )

    command = backend._command()

    assert "--ignore-user-config" not in command
    assert command[1:3] == ["exec", "--json"]
    assert 'approval_policy="never"' in command
    assert command[command.index("--sandbox") + 1] == "workspace-write"


def test_codex_permission_modes_are_safe_and_backend_specific(tmp_path):
    backend = CodexCliBackend("gpt-5.5", cwd=str(tmp_path), cli_path="codex")

    backend.configure_permission_mode("ask")
    assert (backend.sandbox, backend.approval_policy) == ("read-only", "never")

    backend.configure_permission_mode("plan")
    assert (backend.sandbox, backend.approval_policy) == ("read-only", "never")

    backend.configure_permission_mode("auto-edit")
    assert (backend.sandbox, backend.approval_policy) == ("workspace-write", "untrusted")

    backend.configure_permission_mode("bypass")
    assert (backend.sandbox, backend.approval_policy) == ("workspace-write", "never")


def test_codex_explicit_sandbox_override_wins_over_mode(tmp_path):
    backend = CodexCliBackend(
        "gpt-5.5",
        cwd=str(tmp_path),
        cli_path="codex",
        sandbox="danger-full-access",
        permission_mode="ask",
    )

    assert backend.sandbox == "danger-full-access"
    assert backend.approval_policy == "never"


class _FakeProc:
    def __init__(self):
        self.stdin = io.StringIO()
        self.stdout = io.StringIO(
            json.dumps({
                "type": "item.completed",
                "item": {"type": "agent_message", "text": "hello from codex"},
            }) + "\n" +
            json.dumps({
                "type": "turn.completed",
                "usage": {"input_tokens": 10, "output_tokens": 3},
            }) + "\n"
        )
        self.stderr = io.StringIO("")
        self._polls = 0
        self.killed = False

    def poll(self):
        self._polls += 1
        return 0 if self._polls > 2 else None

    def kill(self):
        self.killed = True

    def wait(self, timeout=None):
        return 0


def test_codex_stream_parses_jsonl_final_message(monkeypatch, tmp_path):
    fake_proc = _FakeProc()
    monkeypatch.setattr("lumi.backends.subprocess.Popen", lambda *a, **k: fake_proc)

    backend = CodexCliBackend("gpt-5.5", cwd=str(tmp_path), cli_path="codex")
    events = list(backend.stream(
        user_msg="say hello",
        conversation_history=[],
        instructions="",
        tools=[],
    ))

    assert (EVENT_TEXT_DELTA, {"delta": "hello from codex"}) in events
    done = [data for event, data in events if event == EVENT_DONE][0]
    assert done["model"] == "gpt-5.5"
    assert done["stats"]["input_tokens"] == 10


def test_codex_yields_message_before_process_exits(monkeypatch, tmp_path):
    release = threading.Event()
    class GatedOutput(io.StringIO):
        def readline(self, *args):
            if self.tell():
                release.wait(3)
            return super().readline(*args)
    proc = _FakeProc()
    proc.stdout = GatedOutput(json.dumps({'type': 'item.completed', 'item': {
        'id': 'first', 'type': 'agent_message', 'text': 'Starting the change.'}}) + '\n')
    proc.poll = lambda: 0 if release.is_set() else None
    monkeypatch.setattr('lumi.backends.subprocess.Popen', lambda *a, **kw: proc)
    backend = CodexCliBackend('astra', cwd=str(tmp_path), cli_path='codex')
    stream = backend.stream('fix it', [], '', [])
    with ThreadPoolExecutor() as pool:
        first = pool.submit(next, stream)
        try:
            assert first.result(timeout=2) == (EVENT_TEXT_DELTA, {'delta': 'Starting the change.'})
            assert proc.poll() is None
        finally:
            release.set()
        assert list(stream)[-1][0] == EVENT_DONE


def test_codex_partial_text_does_not_hide_terminal_failure(monkeypatch, tmp_path):
    proc = _FakeProc()
    proc.stdout = io.StringIO('\n'.join(json.dumps(e) for e in [
        {'type': 'item.completed', 'item': {'type': 'agent_message', 'text': 'Working.'}},
        {'type': 'turn.failed', 'error': {'message': 'Connection lost'}},
    ]) + '\n')
    monkeypatch.setattr('lumi.backends.subprocess.Popen', lambda *a, **kw: proc)
    backend = CodexCliBackend('astra', cwd=str(tmp_path), cli_path='codex')
    events = list(backend.stream('fix it', [], '', []))
    assert events[-1] == ('error', {'message': 'Connection lost'})
    assert not any(kind == EVENT_DONE for kind, _ in events)


def test_closing_codex_stream_stops_its_process(monkeypatch, tmp_path):
    proc = _FakeProc()
    proc.poll = lambda: 0 if proc.killed else None
    monkeypatch.setattr('lumi.backends.subprocess.Popen', lambda *a, **kw: proc)
    backend = CodexCliBackend('astra', cwd=str(tmp_path), cli_path='codex')
    stream = backend.stream('fix it', [], '', [])
    assert next(stream)[0] == EVENT_TEXT_DELTA
    stream.close()
    assert proc.killed
