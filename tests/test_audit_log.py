"""The audit log (lumi/audit.py): hash chain, capture levels, retention,
OpenTelemetry export, and the records a turn produces.

The per-mission audit trail of autonomous runs is a different module
(lumi/orchestration/audit.py, tests/test_audit.py).
"""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timezone

import httpx
import pytest

from lumi import audit
from lumi.audit import AuditLog, OtlpExporter, to_span
from tests.streaming_stub import StreamingBackend, done, text_delta, tool_call


class _Settings:
    def __init__(self, data=None):
        self.data = data or {}

    def get(self, section, key=None, default=None):
        values = self.data.get(section, {})
        return values if key is None else values.get(key, default)

    def get_all(self):
        return self.data


@pytest.fixture
def log(tmp_path):
    instance = AuditLog(tmp_path / "audit")
    audit.set_for_tests(instance)
    yield instance
    audit.set_for_tests(None)


def _records(log):
    return [json.loads(line) for path in log._files() for line in path.read_text(encoding="utf-8").splitlines()]


class TestChain:
    def test_records_chain_and_verify(self, log):
        first = log.record("turn.start", session="s1", prompt=log.content("hi"))
        second = log.record("turn.end", session="s1", outcome="completed")
        assert second["prev"] == first["hash"] and second["seq"] == first["seq"] + 1
        assert log.verify() == (True, "")
        assert log.status()["records"] == 2

    def test_edits_removals_and_reordering_are_detected(self, log):
        for index in range(3):
            log.record("tool.result", tool="grep", index=index)
        [path] = log._files()
        lines = path.read_text(encoding="utf-8").splitlines()

        edited = json.loads(lines[1])
        edited["data"]["tool"] = "bash"
        path.write_text("\n".join([lines[0], json.dumps(edited), lines[2]]) + "\n", encoding="utf-8")
        ok, problem = log.verify()
        assert not ok and "was changed" in problem

        path.write_text("\n".join([lines[0], lines[2]]) + "\n", encoding="utf-8")
        ok, problem = log.verify()
        assert not ok and "doesn't follow" in problem

        path.write_text("\n".join([lines[1], lines[0], lines[2]]) + "\n", encoding="utf-8")
        assert not log.verify()[0]

        path.write_text("\n".join([lines[0], "not json", lines[2]]) + "\n", encoding="utf-8")
        ok, problem = log.verify()
        assert not ok and "not a valid record" in problem

    def test_a_new_process_continues_the_chain(self, log, tmp_path):
        log.record("a")
        again = AuditLog(tmp_path / "audit")
        again.record("b")
        assert again.verify() == (True, "")
        assert [r["seq"] for r in _records(again)] == [1, 2]

    def test_writers_in_two_processes_share_one_chain(self, log, tmp_path):
        # Two AuditLog instances stand in for the GUI and the gateway: each
        # continues from what the other wrote instead of forking the chain.
        other = AuditLog(tmp_path / "audit")
        log.record("gui.1")
        other.record("gateway.1")
        log.record("gui.2")
        other.record("gateway.2")
        assert [r["seq"] for r in _records(log)] == [1, 2, 3, 4]
        assert log.verify() == (True, "")

    def test_threads_share_one_chain(self, log):
        threads = [threading.Thread(target=lambda n=n: [log.record("t", n=n, i=i) for i in range(20)])
                   for n in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert log.status()["records"] == 80 and log.verify() == (True, "")

    def test_any_text_round_trips(self, log):
        log.record("tool.result", path="C:/Users/zoë/ファイル.txt", odd="\ud800 lone surrogate")
        assert log.verify() == (True, "")

    def test_resume_reads_a_large_last_record(self, log, tmp_path):
        log.capture = "full"
        log.record("tool.result", output=log.content("x" * 19_000), more=["y" * 30_000] * 3)
        again = AuditLog(tmp_path / "audit")
        assert again._seq == 1
        again.record("next")
        assert again.verify() == (True, "")

    def test_the_chain_continues_across_days(self, log):
        log.root.mkdir(parents=True)
        first = log.record("today")
        # Move the record to an earlier day's file, as if written yesterday.
        [path] = log._files()
        path.rename(log.root / "2026-01-01.jsonl")
        second = log.record("later")
        assert second["prev"] == first["hash"]
        assert len(log._files()) == 2 and log.verify() == (True, "")


class TestCaptureLevels:
    SECRET = "sk-saved-key-for-audit-tests-0123456789"

    def test_metadata_keeps_only_digests(self, log):
        log.configure(_Settings())
        recorded = log.content(f"print('{self.SECRET}')")
        assert set(recorded) == {"chars", "sha256"}
        summary = audit.summarize_args({"path": "src/app.py", "command": f"curl -H {self.SECRET}", "content": "x"})
        assert summary["path"] == "src/app.py"
        assert summary["arguments"] == ["command", "content", "path"]
        assert set(summary["command"]) == {"chars", "sha256"}

    def test_redacted_and_full(self, log):
        settings = _Settings({"privacy": {"audit_capture": "redacted"}, "api_keys": {"openai": self.SECRET}})
        log.configure(settings)
        token = "ghp_" + "a" * 36
        text = log.content(f"key {self.SECRET} and {token}")["text"]
        assert self.SECRET not in text and token not in text and "[REDACTED GitHub token]" in text

        settings.data["privacy"]["audit_capture"] = "full"
        log.configure(settings)
        text = log.content(f"key {self.SECRET} and {token}")["text"]
        assert self.SECRET not in text and token in text  # full keeps content, never saved keys
        assert log.content("x" * 25_000)["text"].endswith("[5000 more]")
        assert log.name(f"/tmp/{self.SECRET}/a.txt") == "/tmp/[REDACTED saved API key]/a.txt"

    def test_an_unknown_level_falls_back_to_metadata(self, log):
        log.configure(_Settings({"privacy": {"audit_capture": "everything"}}))
        assert log.capture == "metadata"

    def test_turned_off(self, log):
        log.configure(_Settings({"privacy": {"audit_log": False}}))
        assert log.record("turn.start") is None and log._files() == []
        assert log.status()["enabled"] is False


def test_retention_deletes_old_days(log):
    log.retention_days = 30
    log.root.mkdir(parents=True, exist_ok=True)
    old = log.root / "2020-01-01.jsonl"
    old.write_text("{}\n", encoding="utf-8")
    log.record("today")
    assert log.purge() == 1 and not old.exists() and len(log._files()) == 1
    assert log.verify() == (True, "")
    log.retention_days = 0
    assert log.purge(now=time.time() + 10 * 365 * 86400) == 0


def test_usage_counts_read_each_providers_names():
    assert audit.usage_counts({"input_tokens": 10, "output_tokens": 2, "cached_tokens": 4, "cost_usd": 0.01}) == {
        "input_tokens": 10, "output_tokens": 2, "cached_tokens": 4, "reported_cost_usd": 0.01}
    assert audit.usage_counts({"prompt_eval_count": 7, "eval_count": 3}) == {
        "input_tokens": 7, "output_tokens": 3, "cached_tokens": 0}


class TestOtlp:
    def test_spans_use_genai_conventions(self, log):
        usage = log.record("model.usage", session="s1", provider="anthropic", model="claude-x",
                           input_tokens=120, output_tokens=30, elapsed=1.5)
        span = to_span(usage)
        attributes = {a["key"]: a["value"] for a in span["attributes"]}
        assert span["name"] == "chat claude-x" and span["kind"] == 3
        assert attributes["gen_ai.provider.name"] == {"stringValue": "anthropic"}
        assert attributes["gen_ai.request.model"] == {"stringValue": "claude-x"}
        assert attributes["gen_ai.usage.input_tokens"] == {"intValue": "120"}
        assert int(span["endTimeUnixNano"]) - int(span["startTimeUnixNano"]) == 1_500_000_000
        tool = to_span(log.record("tool.result", session="s1", tool="grep", call_id="c1", error=False,
                                  output=log.content("found")))
        attributes = {a["key"]: a["value"] for a in tool["attributes"]}
        assert tool["name"] == "execute_tool grep" and tool["traceId"] == span["traceId"]
        assert attributes["gen_ai.tool.call.id"] == {"stringValue": "c1"}
        assert "lumi.output" not in attributes and "lumi.output.sha256" in attributes
        call = to_span(log.record("tool.call", session="s1", tool="grep", call_id="c1"))
        assert call["name"] == "tool.call grep"
        assert "gen_ai.operation.name" not in {a["key"] for a in call["attributes"]}

    def test_exporter_posts_batches(self, log):
        received = []

        def handler(request):
            received.append((request.url.path, request.headers.get("authorization"), json.loads(request.content)))
            return httpx.Response(200)

        exporter = OtlpExporter("http://collector:4318", headers={"Authorization": "Bearer t"},
                                transport=httpx.MockTransport(handler), interval=3600, batch_size=2)
        try:
            for name in ("a", "b", "c"):
                exporter.submit(log.record(name, session="s1"))
            exporter.flush()
        finally:
            exporter.stop()
        assert [len(body["resourceSpans"][0]["scopeSpans"][0]["spans"]) for _, _, body in received] == [2, 1]
        assert {(path, auth) for path, auth, _ in received} == {("/v1/traces", "Bearer t")}
        assert exporter.status()["sent"] == 3

    def test_a_failing_collector_drops_instead_of_blocking(self, log):
        calls = []

        def handler(request):
            calls.append(request)
            return httpx.Response(503)

        exporter = OtlpExporter("http://collector:4318", transport=httpx.MockTransport(handler),
                                interval=3600, max_queue=2, batch_size=1)
        try:
            for name in ("a", "b", "c"):
                exporter.submit(log.record(name))  # the third finds the queue full
            started = time.monotonic()
            exporter.flush()
            assert time.monotonic() - started < 5
        finally:
            exporter.stop()
        # One failed batch ends the round; what is still queued waits for the next.
        assert len(calls) == 1
        assert exporter.status() == {"endpoint": "http://collector:4318/v1/traces", "sent": 0, "dropped": 2,
                                     "queued": 1, "last_error": "HTTP 503"}

    def test_the_background_thread_sends_and_stops(self, log):
        sent = threading.Event()
        exporter = OtlpExporter("http://collector:4318/v1/traces", interval=0.05,
                                transport=httpx.MockTransport(lambda r: (sent.set(), httpx.Response(200))[1]))
        exporter.submit(log.record("a"))
        assert sent.wait(5)
        exporter.stop()
        exporter._thread.join(5)
        assert not exporter._thread.is_alive() and exporter.sent == 1

    def test_configure_starts_and_stops_the_export(self, log):
        log.configure(_Settings({"audit": {"otlp_endpoint": "https://collector.example.com:4318"},
                                 "api_keys": {"otlp": "token-value-for-tests"}}))
        exporter = log._exporter
        assert exporter.url == "https://collector.example.com:4318/v1/traces"
        assert exporter.headers == {"Authorization": "token-value-for-tests"}
        log.configure(_Settings())
        assert log._exporter is None and exporter._stop.is_set()


class TestTurns:
    def test_a_turn_is_recorded_end_to_end(self, log, tmp_path):
        from lumi.engine.session import Session

        project = tmp_path / "project"
        project.mkdir()
        backend = StreamingBackend(scripts=[
            [tool_call("file_write", {"path": "notes.txt", "content": "hello"}, call_id="call-1"),
             done(stats={"prompt_eval_count": 40, "eval_count": 7})],
            [text_delta("Done."), done()],
        ])
        session = Session(backend, auto_approve=True)
        session.project_path = str(project)
        session.audit_session_id = "conv-1"
        list(session.run("Write a note"))

        records = _records(log)
        types = [r["type"] for r in records]
        assert types[0] == "turn.start" and types[-1] == "turn.end"
        for expected in ("tool.call", "tool.result", "file.change", "model.usage"):
            assert expected in types, types
        assert all(r["session"] == "conv-1" and r["project"] == str(project) for r in records)
        start = records[0]["data"]
        assert start["prompt"] == {"chars": len("Write a note"), "sha256": audit.digest("Write a note")}
        assert start["provider"] == "ollama" and start["mode"] == "full-auto"
        call = next(r for r in records if r["type"] == "tool.call")["data"]
        assert call["tool"] == "file_write" and call["path"] == "notes.txt"
        assert call["arguments"] == ["content", "path"] and "hello" not in json.dumps(call)
        change = next(r for r in records if r["type"] == "file.change")["data"]
        assert change == {**change, "tool": "file_write", "call_id": "call-1", "path": "notes.txt"}
        usage = next(r for r in records if r["type"] == "model.usage")["data"]
        assert (usage["input_tokens"], usage["output_tokens"]) == (40, 7)
        assert records[-1]["data"]["outcome"] == "completed"
        assert log.verify() == (True, "")

    def test_approvals_and_denials_are_recorded(self, log, tmp_path):
        from lumi.engine.session import Session

        backend = StreamingBackend(scripts=[
            [tool_call("file_write", {"path": "notes.txt", "content": "hello"}), done()],
            [text_delta("Understood."), done()],
        ])
        session = Session(backend, auto_approve=False)
        session.project_path = str(tmp_path)
        session.audit_session_id = "conv-2"
        list(session.run("Write a note", on_permission=lambda name, args: False))
        records = _records(log)
        approval = next(r for r in records if r["type"] == "approval")["data"]
        assert approval == {**approval, "tool": "file_write", "by": "user", "decision": "denied"}
        result = next(r for r in records if r["type"] == "tool.result")["data"]
        assert result["denied"] is True
        assert "file.change" not in [r["type"] for r in records]

    def test_a_stopped_turn_is_recorded_as_stopped(self, log, tmp_path):
        from lumi.engine.session import Session

        session = Session(StreamingBackend(events=[text_delta("Working"), done()]))
        session.project_path = str(tmp_path)
        events = session.run("Go")
        next(events)
        events.close()
        assert _records(log)[-1]["type"] == "turn.end"
        assert _records(log)[-1]["data"]["outcome"] == "stopped"

    def test_errors_are_recorded(self, log, tmp_path):
        from lumi.engine.session import Session
        from tests.streaming_stub import error

        session = Session(StreamingBackend(events=[error("upstream failed")]))
        session.project_path = str(tmp_path)
        list(session.run("Go"))
        records = _records(log)
        assert "error" in [r["type"] for r in records]
        assert records[-1]["data"]["outcome"] == "error"

    def test_a_failing_log_never_breaks_a_turn(self, log, tmp_path, monkeypatch):
        from lumi.engine.session import Session

        def broken(*args, **kwargs):
            raise RuntimeError("disk gone")

        monkeypatch.setattr(log, "record", broken)
        session = Session(StreamingBackend(events=[text_delta("Hello."), done()]))
        session.project_path = str(tmp_path)
        events = list(session.run("Hi"))
        assert events[-1]["event"] == "session.end"


def test_record_timestamps_are_utc_days(log):
    record = log.record("a")
    assert record["ts"].endswith("Z")
    assert log._files()[0].stem == datetime.now(timezone.utc).strftime("%Y-%m-%d")


class _StubWS:
    def __init__(self):
        self.sent = []

    async def send_json(self, payload):
        self.sent.append(payload)


def _update(settings, section, key, value):
    import asyncio
    from types import SimpleNamespace

    from lumi.gui import ws_commands

    def update_setting_value(section, key, value, *, clear_secret=False):
        settings.set(section, key, value)
        return settings.get_masked()

    state = SimpleNamespace(settings=settings, update_setting_value=update_setting_value,
                            get_init_data=lambda refresh_only=False: {"event": "init"})
    ctx = ws_commands.CommandContext(ws=_StubWS(), state=state,
                                     msg={"command": "update_settings", "section": section, "key": key, "value": value},
                                     runs=SimpleNamespace(busy=False))
    asyncio.run(ws_commands.HANDLERS["update_settings"](ctx))
    return ctx.ws.sent


class TestSettings:
    def test_audit_settings_are_validated(self, log, tmp_path):
        from lumi.gui.settings import SettingsManager

        settings = SettingsManager(tmp_path / "settings.json")
        assert settings.get("privacy", "audit_capture") == "metadata"
        assert settings.get("privacy", "audit_retention_days") == 365
        for section, key, value, message in [
            ("privacy", "audit_capture", "everything", "metadata, redacted or full"),
            ("privacy", "audit_retention_days", -1, "0 (keep) to 3650"),
            ("privacy", "audit_log", "yes", "on or off"),
            ("audit", "otlp_endpoint", "collector:4318", "http(s)://host:4318"),
            ("audit", "otlp_endpoint", "https://user:pw@collector:4318", "API keys, not in the URL"),
            ("audit", "otlp_auth_header", "Bad Header:", "header name"),
        ]:
            sent = _update(settings, section, key, value)
            assert message in sent[0]["message"], (key, sent)
        _update(settings, "audit", "otlp_endpoint", "https://collector.example.com:4318/")
        assert settings.get("audit", "otlp_endpoint") == "https://collector.example.com:4318"

    def test_changes_are_recorded_by_key_only_when_something_changed(self, log, tmp_path):
        from lumi.gui.settings import SettingsManager

        settings = SettingsManager(tmp_path / "settings.json")
        _update(settings, "privacy", "audit_capture", "redacted")
        _update(settings, "privacy", "audit_capture", "redacted")  # saved again on blur
        _update(settings, "audit", "otlp_auth_header", "x-honeycomb-team")
        changes = [r["data"] for r in _records(log) if r["type"] == "settings.change"]
        assert changes == [{"section": "privacy", "keys": ["audit_capture"]},
                           {"section": "audit", "keys": ["otlp_auth_header"]}]
        assert "redacted" not in json.dumps(changes) and "honeycomb" not in json.dumps(changes)
