import io
import json

from resonant_client.backends import (
    CodexCliBackend,
    EVENT_DONE,
    EVENT_TEXT_DELTA,
    _build_codex_prompt,
    codex_cli_models,
    resolve_codex_cli_path,
)


def test_codex_models_include_configured_model(monkeypatch):
    monkeypatch.delenv("RESONANT_CODEX_MODELS", raising=False)

    models = codex_cli_models({"model": "gpt-6-preview"})

    assert models[0] == "gpt-6-preview"
    assert "gpt-5.5" in models


def test_codex_models_can_be_overridden_by_env(monkeypatch):
    monkeypatch.setenv("RESONANT_CODEX_MODELS", "alpha,beta")

    assert codex_cli_models({"model": "alpha"}) == ["alpha", "beta"]


def test_resolve_codex_cli_path_prefers_configured_bundled_cli(monkeypatch, tmp_path):
    configured = tmp_path / "bundled-codex.exe"
    configured.write_text("", encoding="utf-8")
    path_cli = tmp_path / "path-codex.exe"
    path_cli.write_text("", encoding="utf-8")
    monkeypatch.delenv("RESONANT_CODEX_CLI", raising=False)
    monkeypatch.delenv("CODEX_CLI_PATH", raising=False)
    monkeypatch.setattr(
        "resonant_client.backends._load_codex_config",
        lambda: {"mcp_servers": {"node_repl": {"env": {"CODEX_CLI_PATH": str(configured)}}}},
    )
    monkeypatch.setattr("resonant_client.backends.shutil.which", lambda _: str(path_cli))

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

    assert "Do not emit Resonant <tool_call> XML" in prompt
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


def test_codex_stream_parses_jsonl_final_message(monkeypatch, tmp_path):
    fake_proc = _FakeProc()
    monkeypatch.setattr("resonant_client.backends.subprocess.Popen", lambda *a, **k: fake_proc)

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
