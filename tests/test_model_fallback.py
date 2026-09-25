"""Model roles, fallback on failure, and capability overrides."""

from __future__ import annotations

import asyncio
import io
import json
from types import SimpleNamespace

import pytest

from lumi import audit, headless
from lumi import policy as lumi_policy
from lumi.audit import AuditLog
from lumi.capabilities import infer_model_capabilities
from lumi.engine.model_roles import ModelRoleRouter, parse_fallback_models, parse_role_models
from lumi.policy import PolicyError, parse
from tests.streaming_stub import StreamingBackend, done, error, text_delta


def failing(message="HTTP 529 overloaded", *, name="anthropic", model="claude-opus-5-5"):
    return StreamingBackend(name=name, model=model, events=[error(message)])


def answering(text="Answered by the fallback.", *, name="ollama", model="qwen3:32b"):
    return StreamingBackend(name=name, model=model, events=[text_delta(text), done(model=model)])


def session_with(primary, *fallbacks):
    from lumi.engine.session import Session

    session = Session(primary)
    session.fallback_provider = lambda: [(label, factory) for label, factory in fallbacks]
    return session


@pytest.fixture
def log(tmp_path):
    instance = AuditLog(tmp_path / "audit")
    audit.set_for_tests(instance)
    return instance


def records(log, kind):
    return [json.loads(line)["data"] for path in log._files()
            for line in path.read_text(encoding="utf-8").splitlines() if json.loads(line)["type"] == kind]


class TestFallback:
    def test_a_failed_request_continues_with_the_next_model(self, log, tmp_path):
        primary, spare = failing(), answering()
        session = session_with(primary, ("ollama:qwen3:32b", lambda: spare))
        session.project_path = str(tmp_path)
        events = list(session.run("Explain the project"))
        kinds = [e.get("event") for e in events]
        assert "error" not in kinds
        notice = next(e for e in events if e.get("kind") == "model_fallback")
        assert notice["message"].startswith("anthropic:claude-opus-5-5 failed (HTTP 529 overloaded)")
        assert next(e for e in events if e.get("event") == "text.done")["text"] == "Answered by the fallback."
        assert (primary.stream_count, spare.stream_count) == (1, 1)
        assert session.backend is primary  # the next turn tries the chosen model first
        [record] = records(log, "model.fallback")
        assert (record["from_model"], record["to_model"]) == ("anthropic:claude-opus-5-5", "ollama:qwen3:32b")
        assert records(log, "turn.end")[-1]["outcome"] == "completed"

    def test_unusable_fallbacks_are_skipped(self, log, tmp_path):
        lumi_policy.set_for_tests(parse({"schema": "lumi.policy/v1", "organization": "Acme",
                                         "models": {"blocked": ["openrouter:*"]}}, source="t"))

        def broken():
            raise ValueError("Add your OpenAI API key")

        spare = answering()
        session = session_with(failing(), ("openrouter:some/model", answering), ("openai:gpt-5.5", broken),
                               ("sonn:sonn-auto", answering), ("ollama:qwen3:32b", lambda: spare))
        # SONN is unpriced; a budget that needs prices rules it out too.
        lumi_policy.set_for_tests(parse({"schema": "lumi.policy/v1", "organization": "Acme",
                                         "models": {"blocked": ["openrouter:*"]},
                                         "budgets": [{"scope": "user", "block_usd": 100, "block_unpriced": True}]},
                                        source="t"))
        session.project_path = str(tmp_path)
        events = list(session.run("Go"))
        assert next(e for e in events if e.get("kind") == "model_fallback")["model"] == "ollama:qwen3:32b"
        assert spare.stream_count == 1

    def test_no_fallback_after_output_or_when_none_is_left(self, log, tmp_path):
        partial = StreamingBackend(name="anthropic", model="m", events=[text_delta("Half an answer"), error("reset")])
        spare = answering()
        session = session_with(partial, ("ollama:qwen3:32b", lambda: spare))
        session.project_path = str(tmp_path)
        assert "error" in [e.get("event") for e in session.run("Go")] and spare.stream_count == 0

        session = session_with(failing())
        session.project_path = str(tmp_path)
        assert "error" in [e.get("event") for e in session.run("Go")]

    def test_a_crashed_stream_falls_back_too(self, log, tmp_path):
        crashed = StreamingBackend(name="anthropic", model="m", raise_on_stream=ConnectionError("refused"))
        spare = answering()
        session = session_with(crashed, ("ollama:qwen3:32b", lambda: spare))
        session.project_path = str(tmp_path)
        events = list(session.run("Go"))
        assert "Stream error: refused" in next(e for e in events if e.get("kind") == "model_fallback")["message"]
        assert spare.stream_count == 1

    def test_lumi_run_takes_fallbacks(self, monkeypatch, tmp_path, log):
        backends = {"anthropic": failing(), "ollama": answering()}
        monkeypatch.setattr(headless, "build_spec", lambda settings, provider, model, project: SimpleNamespace(
            create_backend=lambda settings: backends[provider], permission_mode=""))
        out = io.StringIO()
        code = headless.main(["Go", "--provider", "anthropic", "--model", "claude-opus-5-5", "--project", str(tmp_path),
                              "--fallback", "ollama:qwen3:32b"], stdin=io.StringIO(""), stdout=out, stderr=io.StringIO())
        result = json.loads(out.getvalue())
        assert (code, result["status"], result["text"]) == (0, "completed", "Answered by the fallback.")
        err = io.StringIO()
        assert headless.main(["Go", "--provider", "anthropic", "--model", "m", "--project", str(tmp_path),
                              "--fallback", "nonsense"], stdin=io.StringIO(""), stdout=io.StringIO(), stderr=err) == 2
        assert "provider:model" in err.getvalue()


class TestSettingsLines:
    def test_fallback_and_role_lines(self):
        assert parse_fallback_models(["anthropic:claude-sonnet-5", "# spare", "ollama:qwen3:32b",
                                      "anthropic:claude-sonnet-5"]) == ["anthropic:claude-sonnet-5", "ollama:qwen3:32b"]
        assert parse_role_models("summarize ollama:qwen3:8b\nreview conn-acme:gpt-5.5") == {
            "summarize": {"backend_type": "ollama", "model": "qwen3:8b"},
            "review": {"backend_type": "conn-acme", "model": "gpt-5.5"}}
        for bad, message in [(["gpt-5"], "provider:model"), (["acme:x"], "provider:model"),
                             ([f"ollama:m{i}" for i in range(6)], "five")]:
            with pytest.raises(ValueError, match=message):
                parse_fallback_models(bad)
        with pytest.raises(ValueError, match="Unknown role"):
            parse_role_models(["boss anthropic:x"])

    def test_the_socket_saves_normalized_lines(self, tmp_path):
        from lumi.gui import ws_commands
        from lumi.gui.settings import SettingsManager

        settings = SettingsManager(tmp_path / "settings.json")

        class _WS:
            sent: list = []

            async def send_json(self, payload):
                self.sent.append(payload)

        def update(key, value):
            state = SimpleNamespace(settings=settings, get_init_data=lambda refresh_only=False: {"event": "init"},
                                    update_setting_value=lambda s, k, v, clear_secret=False: settings.set(s, k, v) or {})
            ws = _WS()
            ws.sent = []
            ctx = ws_commands.CommandContext(ws=ws, state=state, runs=SimpleNamespace(busy=False),
                                             msg={"command": "update_settings", "section": "general", "key": key, "value": value})
            asyncio.run(ws_commands.HANDLERS["update_settings"](ctx))
            return ws.sent

        update("fallback_models", ["Ollama:qwen3:32b"])
        assert settings.get("general", "fallback_models") == ["ollama:qwen3:32b"]
        update("role_models", ["summarize ollama:qwen3:8b"])
        assert settings.get("general", "role_models") == ["summarize ollama:qwen3:8b"]
        sent = update("role_models", ["summarize qwen3"])
        assert sent[0]["source"] == "settings" and "provider:model" in sent[0]["message"]


class TestRoles:
    def test_a_summarize_model_names_sessions(self, monkeypatch):
        from lumi.gui import session_titles

        summarizer = SimpleNamespace(handles_tools=False, name="ollama", model="qwen3:8b")
        router = ModelRoleRouter({"summarize": {"backend_type": "ollama", "model": "qwen3:8b"}},
                                 backend_factory=lambda profile: summarizer)
        used = []

        def generate(backend, prompt, cancel, **kwargs):
            used.append((backend, kwargs.get("usage_context")))
            return ""

        monkeypatch.setattr(session_titles, "generate_session_title", generate)
        record = SimpleNamespace(title_source="auto", title="New task", id="conv-1")
        # The chat model is a CLI tool loop; the summarize model still names the session.
        state = SimpleNamespace(backend=SimpleNamespace(handles_tools=True), session=SimpleNamespace(model_role_router=router),
                                project=SimpleNamespace(project_path="C:/p", current_session=record))

        async def run():
            session_titles.schedule_title_refinement(state, None, record, "Fix login")
            await state._session_title_task

        asyncio.run(run())
        assert used == [(summarizer, {"session": "conv-1", "project": "C:/p"})]


class TestCapabilityOverrides:
    def test_an_organization_states_a_models_capabilities(self):
        lumi_policy.set_for_tests(parse({"schema": "lumi.policy/v1", "models": {"capabilities": {
            "claude-*": {"context_window": 100_000, "vision": False},
            "qwen3*": {"tools": True, "reasoning": ["low", "high"], "computer_use": False}}}}, source="t"))
        claude = infer_model_capabilities("claude-sonnet-5")
        assert (claude.context_window, claude.modalities, claude.can_use_computer, claude.source) == (
            100_000, ("text",), False, "organization")
        reported = claude.with_runtime_metadata(["vision", "tools"], context_window=200_000)
        assert (reported.context_window, "image" in reported.modalities) == (100_000, False)
        qwen = infer_model_capabilities("qwen3:32b")
        assert qwen.native_tools and qwen.reasoning_levels == ("low", "high")
        lumi_policy.set_for_tests(None)
        assert infer_model_capabilities("claude-sonnet-5").context_window == 200_000

    @pytest.mark.parametrize("overrides, message", [
        ({"claude-*": {"context_window": 0}}, "positive whole number"),
        ({"claude-*": {"vision": "yes"}}, "true or false"),
        ({"claude-*": {"reasoning": "max"}}, "list of levels"),
        ({"claude-*": {"telepathy": True}}, "unknown capability"),
    ])
    def test_bad_overrides_are_refused(self, overrides, message):
        with pytest.raises(PolicyError, match=message):
            parse({"schema": "lumi.policy/v1", "models": {"capabilities": overrides}}, source="t")
