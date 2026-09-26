"""Dictation (lumi/voice.py): which engines may listen, and transcription through a service."""

from __future__ import annotations

import asyncio
import base64
import json
import re
from types import SimpleNamespace

import httpx
import pytest

from lumi import audit, usage, voice
from lumi import policy as lumi_policy
from lumi.gui import ws_commands
from lumi.gui.settings import SettingsManager
from lumi.policy import PolicyError, parse

AUDIO = b"\x1aE\xdf\xa3 not really webm"


@pytest.fixture
def settings(tmp_path):
    return SettingsManager(tmp_path / "settings.json")


@pytest.fixture
def logs(tmp_path):
    log = audit.AuditLog(tmp_path / "audit")
    audit.set_for_tests(log)
    ledger = usage.UsageLedger(tmp_path / "usage")
    usage.set_for_tests(ledger)
    return SimpleNamespace(audit=log, usage=ledger)


def _audit_records(log):
    return [json.loads(line) for path in sorted(log.root.glob("*.jsonl"))
            for line in path.read_text(encoding="utf-8").splitlines()]


def _policy(**extra):
    lumi_policy.set_for_tests(parse({"schema": "lumi.policy/v1", "organization": "Acme", **extra}, source="test"))


def _openai(settings, key="sk-test-voice"):
    settings.set("voice", "service", "openai")
    settings.set("api_keys", "openai", key)


def _local_whisper(settings, **extra):
    settings.set("connections", None, [{"id": "whisper", "name": "Office Whisper", "type": "openai-compatible",
                                        "base_url": "http://10.0.0.5:8000/v1", "auth": "none", **extra}])
    settings.set("voice", "service", "conn-whisper")


# ── What may listen ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("engine", ["auto", "browser", "service"])
def test_invalid_policy_disables_every_dictation_engine(settings, monkeypatch, engine):
    settings.set("voice", "engine", engine)
    _openai(settings)
    monkeypatch.setattr(lumi_policy, "blocked_reason", lambda: "Organization policy is invalid")
    result = voice.status(settings)
    assert result["browser"] is False
    assert result["service_ready"] is False
    assert result["browser_reason"] == result["reason"] == "Organization policy is invalid"

def test_by_default_the_browser_listens_and_no_service_is_chosen(settings, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    state = voice.status(settings)
    assert state["engine"] == "auto" and state["browser"] is True
    assert state["service_ready"] is False and "Choose a transcription service" in state["reason"]
    assert state["model"] == voice.DEFAULT_MODEL and state["max_seconds"] == voice.MAX_SECONDS
    assert state["services"] == [{"id": "openai", "name": "OpenAI"}]


def test_openai_needs_its_key_and_the_check_never_reads_the_key(settings, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    settings.set("voice", "service", "openai")
    assert "Add your OpenAI API key" in voice.status(settings)["reason"]
    settings.set("api_keys", "openai", "sk-test-voice")
    monkeypatch.setattr(settings, "get", _refuse_key_reads(settings.get))
    state = voice.status(settings)
    assert state["service_ready"] is True and state["service_name"] == "OpenAI" and state["reason"] == ""


def _refuse_key_reads(get):
    def guarded(section, key=None, default=None):
        assert section != "api_keys", "status() read a key; it should only ask whether one is saved"
        return get(section, key, default)
    return guarded


def test_connections_that_speak_openai_are_offered_and_need_their_keys(settings):
    settings.set("connections", None, [
        {"id": "whisper", "name": "Office Whisper", "type": "openai-compatible",
         "base_url": "http://10.0.0.5:8000/v1", "auth": "bearer"},
        {"id": "claude", "name": "Claude", "type": "anthropic"},
    ])
    settings.set("voice", "service", "conn-whisper")
    state = voice.status(settings)
    assert state["services"] == [{"id": "openai", "name": "OpenAI"}, {"id": "conn-whisper", "name": "Office Whisper"}]
    assert state["reason"] == "Add the key for Office Whisper in Settings > Connections."
    settings.set("api_keys", "conn_whisper", "local-token")
    assert voice.status(settings)["service_ready"] is True
    settings.set("voice", "service", "conn-gone")
    assert "no longer exists" in voice.status(settings)["reason"]


def test_off_says_who_turned_it_off(settings):
    settings.set("voice", "engine", "off")
    state = voice.status(settings)
    assert state["reason"] == "Dictation is off in Settings > Voice." and not state["locked"]
    _policy(settings={"voice.engine": "off"})
    state = voice.status(settings)
    assert state["engine"] == "off" and state["locked"] and state["reason"] == "Acme turned dictation off."


def test_zero_retention_refuses_the_browser_and_services_that_keep_data(settings):
    _openai(settings)
    _policy(models={"require_zero_retention": True})
    state = voice.status(settings)
    assert state["browser"] is False and "keep no data" in state["browser_reason"]
    assert state["service_ready"] is False and "doesn't allow OpenAI's whisper-1" in state["reason"]
    _policy(models={"require_zero_retention": True, "zero_retention_providers": ["openai"]})
    assert voice.status(settings)["service_ready"] is True


def test_a_zero_retention_connection_passes_and_the_model_rules_apply(settings):
    from lumi import connections

    previous = lumi_policy._zero_retention_resolver
    lumi_policy.set_zero_retention_resolver(connections.zero_retention_resolver(settings))
    try:
        _local_whisper(settings, zero_retention=True)
        _policy(models={"require_zero_retention": True})
        assert voice.status(settings)["service_ready"] is True
        _policy(models={"allowed": ["anthropic:*"]})
        assert "doesn't allow Office Whisper's whisper-1" in voice.status(settings)["reason"]
        _policy(models={"allowed": ["anthropic:*", "conn-whisper:*"]})
        assert voice.status(settings)["service_ready"] is True
    finally:
        lumi_policy.set_zero_retention_resolver(previous)


def test_an_unusable_policy_stops_the_service(settings):
    _openai(settings)
    lumi_policy.set_for_tests(None, error="The policy file couldn't be read: bad JSON.")
    assert "couldn't be read" in voice.status(settings)["reason"]


def test_policy_and_settings_values_are_checked(settings):
    with pytest.raises(PolicyError, match="voice.engine"):
        parse({"schema": "lumi.policy/v1", "settings": {"voice.engine": "loud"}}, source="test")
    with pytest.raises(PolicyError, match="voice.service"):
        parse({"schema": "lumi.policy/v1", "settings": {"voice.service": "https://example.com"}}, source="test")
    parse({"schema": "lumi.policy/v1", "settings": {"voice.engine": "service", "voice.service": "conn-whisper",
                                                     "voice.language": "de-DE"}}, source="test")
    assert ws_commands._SOCKET_SETTING_KEYS["voice"] == {"engine", "service", "model", "language"}
    assert ws_commands._socket_setting_value("voice", "language", " en-GB ") == "en-GB"
    for key, value in (("engine", "always"), ("service", "conn-Bad Name"), ("model", "a b"), ("language", "english")):
        with pytest.raises(ValueError):
            ws_commands._socket_setting_value("voice", key, value)


def test_settings_send_the_page_what_dictation_can_use(settings):
    _openai(settings)
    meta = settings.get_masked()["_meta"]
    assert meta["voice"]["service_ready"] is True and meta["voice"]["service"] == "openai"
    assert "sk-test-voice" not in json.dumps(meta)


# ── Transcribing ────────────────────────────────────────────────────────────

def _service(answer):
    seen = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return answer(request) if callable(answer) else answer

    return seen, httpx.MockTransport(handle)


def test_a_recording_goes_to_the_service_and_its_text_comes_back(settings, logs):
    _openai(settings)
    settings.set("voice", "language", "en-GB")
    seen, transport = _service(httpx.Response(200, json={"text": "  run the tests again  "}))
    text = voice.transcribe(settings, AUDIO, "audio/webm;codecs=opus", project="C:/work/app", transport=transport)
    assert text == "run the tests again"
    [request] = seen
    assert str(request.url) == "https://api.openai.com/v1/audio/transcriptions"
    assert request.headers["authorization"] == "Bearer sk-test-voice"
    body = request.content
    assert b'filename="dictation.webm"' in body and b"Content-Type: audio/webm" in body and AUDIO in body
    assert b'name="model"\r\n\r\nwhisper-1' in body and b'name="language"\r\n\r\nen' in body

    [row] = logs.usage.records()
    assert (row["purpose"], row["provider"], row["model"]) == ("dictation", "openai", "whisper-1")
    assert row["cost_usd"] is None and row["price_source"] == "unpriced" and row["project"] == "C:/work/app"
    [entry] = [r for r in _audit_records(logs.audit) if r["type"] == "voice.transcription"]
    assert entry["data"]["outcome"] == "ok" and entry["data"]["characters"] == len(text)
    assert entry["data"]["audio_bytes"] == len(AUDIO) and entry["data"]["media_type"] == "audio/webm"
    # Metadata only: neither the words nor the key reach the log.
    assert "run the tests" not in json.dumps(entry) and "sk-test-voice" not in json.dumps(entry)


def test_a_local_connection_gets_its_headers_and_no_language_when_none_is_set(settings, logs):
    _local_whisper(settings, headers={"X-Team": "platform"})
    settings.set("voice", "model", "Systran/faster-whisper-small")
    seen, transport = _service(httpx.Response(200, json={"text": "hola"}))
    assert voice.transcribe(settings, AUDIO, "audio/mp4", transport=transport) == "hola"
    [request] = seen
    assert str(request.url) == "http://10.0.0.5:8000/v1/audio/transcriptions"
    assert request.headers["x-team"] == "platform" and "authorization" not in request.headers
    assert b'filename="dictation.m4a"' in request.content and b'name="language"' not in request.content
    assert logs.usage.records()[0]["provider"] == "conn-whisper"


@pytest.mark.parametrize("response, message", [
    (httpx.Response(401, json={"error": {"message": "bad key"}}), "OpenAI refused the key (401)"),
    (httpx.Response(404), "no transcription endpoint at https://api.openai.com/v1/audio/transcriptions"),
    (httpx.Response(413), "too large for OpenAI"),
    (httpx.Response(429), "limiting requests"),
    (httpx.Response(400, json={"error": {"message": "Invalid file format.\nSupported: webm"}}),
     "couldn't transcribe the recording (400): Invalid file format. Supported: webm"),
    (httpx.Response(200, text="<html>"), "answered without a transcript"),
    (httpx.Response(200, json={"segments": []}), "answered without a transcript"),
])
def test_service_failures_say_what_to_do(settings, logs, response, message):
    _openai(settings)
    _, transport = _service(response)
    with pytest.raises(voice.VoiceError, match=re.escape(message)):
        voice.transcribe(settings, AUDIO, "audio/webm", transport=transport)
    [entry] = [r for r in _audit_records(logs.audit) if r["type"] == "voice.transcription"]
    assert entry["data"]["outcome"] == "error"


def test_timeouts_and_unreachable_services(settings, logs):
    _openai(settings)

    def slow(request):
        raise httpx.ReadTimeout("slow", request=request)

    def down(request):
        raise httpx.ConnectError("refused", request=request)

    for answer, message in ((slow, "didn't answer in time"), (down, "couldn't reach OpenAI")):
        with pytest.raises(voice.VoiceError, match=message):
            voice.transcribe(settings, AUDIO, "audio/webm", transport=_service(answer)[1])


def test_nothing_is_sent_that_the_service_or_policy_shouldnt_get(settings, logs):
    _openai(settings)
    seen, transport = _service(httpx.Response(200, json={"text": "x"}))
    for audio, kind, message in ((AUDIO, "video/mp4", "can't send this kind"), (b"", "audio/webm", "Nothing was recorded"),
                                 (b"0" * (voice.MAX_AUDIO_BYTES + 1), "audio/webm", "too long")):
        with pytest.raises(voice.VoiceError, match=message):
            voice.transcribe(settings, audio, kind, transport=transport)
    settings.set("voice", "engine", "browser")
    with pytest.raises(voice.VoiceError):
        voice.transcribe(settings, AUDIO, "audio/webm", transport=transport)
    settings.set("voice", "engine", "auto")
    _policy(models={"blocked": ["openai:*"]})
    with pytest.raises(voice.VoiceError, match="doesn't allow"):
        voice.transcribe(settings, AUDIO, "audio/webm", transport=transport)
    assert seen == [] and logs.usage.records() == []


def test_dictation_is_recorded_unpriced_even_when_a_chat_price_would_match(tmp_path):
    ledger = usage.UsageLedger(tmp_path / "usage")
    # gpt-4o-mini* prices text tokens; a transcription model must not be valued at them.
    row = ledger.record(provider="openai", model="gpt-4o-mini-transcribe", stats={}, purpose="dictation", priced=False)
    assert row["cost_usd"] is None and row["computed_cost_usd"] is None and row["price_source"] == "unpriced"
    priced = ledger.record(provider="openai", model="gpt-4o-mini", stats={"input_tokens": 1000}, purpose="turn")
    assert priced["price_source"] == "catalog" and priced["cost_usd"] > 0


# ── The socket command ──────────────────────────────────────────────────────

class _WS:
    def __init__(self):
        self.sent = []

    async def send_json(self, payload):
        self.sent.append(payload)


def _command(settings, **msg):
    state = SimpleNamespace(settings=settings, project=SimpleNamespace(project_path="C:/work/app"))
    ctx = ws_commands.CommandContext(ws=_WS(), state=state, msg={"command": "voice_transcribe", **msg},
                                     runs=SimpleNamespace(busy=False))

    async def go():
        await ws_commands.HANDLERS["voice_transcribe"](ctx)
        await asyncio.gather(*ws_commands._VOICE_TASKS)

    asyncio.run(go())
    return ctx.ws.sent


def test_the_socket_command_answers_the_request_it_came_with(settings, monkeypatch):
    calls = []

    def fake(settings_, audio, audio_type, *, project=""):
        calls.append((audio, audio_type, project))
        return "hello there"

    monkeypatch.setattr(voice, "transcribe", fake)
    sent = _command(settings, request_id="r1", media_type="audio/webm", audio=base64.b64encode(AUDIO).decode())
    assert sent == [{"event": "voice.transcript", "request_id": "r1", "text": "hello there"}]
    assert calls == [(AUDIO, "audio/webm", "C:/work/app")]

    def refuse(*args, **kwargs):
        raise voice.VoiceError("Choose a transcription service in Settings > Voice.")

    monkeypatch.setattr(voice, "transcribe", refuse)
    assert _command(settings, request_id="r2", media_type="audio/webm", audio="AAAA") == [
        {"event": "voice.error", "request_id": "r2", "message": "Choose a transcription service in Settings > Voice."}]


def test_the_socket_command_refuses_damaged_or_oversized_recordings(settings, monkeypatch):
    monkeypatch.setattr(voice, "transcribe", lambda *a, **k: pytest.fail("nothing should be transcribed"))
    [damaged] = _command(settings, request_id="r3", media_type="audio/webm", audio="not base64!")
    assert damaged["event"] == "voice.error" and "didn't arrive intact" in damaged["message"]
    [huge] = _command(settings, request_id="r4", media_type="audio/webm", audio="A" * (voice.MAX_AUDIO_BYTES * 2))
    assert huge["event"] == "voice.error" and "too long" in huge["message"]
