"""Dictation in the composer: which engine listens, and the transcription service.

The page listens in one of two ways (docs/voice-input.md):

* ``browser``: the webview's own speech recognition. Chromium sends the audio
  to Google or Microsoft and WebKit to Apple; Lumi can't see where.
* ``service``: the page records while the person dictates, and Lumi sends the
  recording to the transcription service chosen in Settings > Voice. That is
  an OpenAI-compatible ``/audio/transcriptions`` endpoint: OpenAI's own
  (``api_keys.openai``) or one of the person's connections, such as a Whisper
  server on their own network. Audio goes only there, only when the person
  stops dictating, and Lumi keeps no copy.

An organization's policy locks ``voice.*`` like any setting, for example
``"voice.engine": "off"``. The service's model must pass the policy's model
rules (``Policy.model_allowed``), so a policy that requires zero data
retention allows only a service that keeps no data. The same policy turns off
the browser's recognizer, because Lumi can't tell what that service keeps.
"""

from __future__ import annotations

import os
import re
import time
from typing import Any

import httpx

from . import audit, net, usage

ENGINES = ("auto", "browser", "service", "off")
DEFAULT_MODEL = "whisper-1"
OPENAI_URL = "https://api.openai.com/v1"
# The page stops recording at this length; the service sees nothing longer.
MAX_SECONDS = 300
# Five minutes of Opus is about 2 MB. The socket carries the recording as
# base64, and uvicorn refuses WebSocket messages over 16 MiB.
MAX_AUDIO_BYTES = 10 * 1024 * 1024
MAX_TRANSCRIPT = 20_000
# What MediaRecorder makes: WebM in Chromium and WebView2, MP4 in WebKit, Ogg in Firefox.
AUDIO_TYPES = {
    "audio/webm": "webm", "audio/ogg": "ogg", "audio/mp4": "m4a", "audio/mpeg": "mp3",
    "audio/wav": "wav", "audio/x-wav": "wav",
}
# Connection types that offer OpenAI's /audio/transcriptions.
SERVICE_TYPES = ("openai-compatible", "openai")
_LANGUAGE = re.compile(r"^[A-Za-z]{2,3}(-[A-Za-z0-9]{1,8}){0,3}$")
_MODEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,99}$")
_SERVICE = re.compile(r"^(openai|conn-[a-z0-9][a-z0-9-]{0,39})$")


class VoiceError(Exception):
    """Why dictation can't run or failed, in words the person can act on."""


def _setting(settings: Any, key: str) -> str:
    value = settings.get("voice", key, "") if settings is not None else ""
    return str(value or "").strip()


def validate(key: str, value: Any) -> str:
    """The value to store for one ``voice`` setting, or raise ValueError with the fix."""
    text = str(value if value is not None else "").strip()
    if key == "engine":
        if text not in ENGINES:
            raise ValueError("Choose how dictation listens: auto, browser, service or off.")
    elif key == "service":
        if text and not _SERVICE.match(text):
            raise ValueError("Choose OpenAI or one of your connections as the transcription service.")
    elif key == "model":
        if text and not _MODEL.match(text):
            raise ValueError("Enter the transcription model's id, such as whisper-1.")
    elif key == "language":
        if text and not _LANGUAGE.match(text):
            raise ValueError("Enter a language tag such as en, en-US or de-DE, or leave it empty.")
    else:
        raise ValueError(f"voice.{key} isn't a dictation setting.")
    return text


def services(settings: Any) -> list[dict[str, str]]:
    """Transcription services to offer: OpenAI, and connections that speak its API."""
    from .connections import backend_key, list_connections

    choices = [{"id": "openai", "name": "OpenAI"}]
    for connection in list_connections(settings):
        if connection["type"] in SERVICE_TYPES:
            choices.append({"id": backend_key(connection["id"]), "name": connection["name"]})
    return choices


def _openai_key(settings: Any) -> str:
    saved = settings.get("api_keys", "openai", "") if settings is not None else ""
    return str(saved or "").strip() or os.environ.get("OPENAI_API_KEY", "").strip()


def _has_key(settings: Any, name: str) -> bool:
    """Whether a key is saved; the credential store isn't read just to find out."""
    present = getattr(settings, "key_present", None)
    if callable(present):
        return bool(present(name))
    return bool(settings.get("api_keys", name, "")) if settings is not None else False


def _connection(settings: Any, service: str) -> dict[str, Any] | None:
    from .connections import connection_id_from_backend, find_connection

    connection = find_connection(settings, connection_id_from_backend(service))
    return connection if connection and connection["type"] in SERVICE_TYPES else None


def _service_problem(settings: Any, service: str) -> str:
    """Why ``service`` can't transcribe now, without contacting it; empty when it can."""
    if not service:
        return "Choose a transcription service in Settings > Voice."
    if service == "openai":
        if _has_key(settings, "openai") or os.environ.get("OPENAI_API_KEY", "").strip():
            return ""
        return "Add your OpenAI API key in Settings > Connections to transcribe with OpenAI."
    connection = _connection(settings, service)
    if connection is None:
        return "The transcription service chosen in Settings > Voice no longer exists. Choose another."
    from .connections import secret_setting

    # OAuth's key is the client secret it exchanges for a token.
    needs_key = connection["auth"] in ("bearer", "header", "oauth")
    if needs_key and not _has_key(settings, secret_setting(connection["id"])):
        return f"Add the key for {connection['name']} in Settings > Connections."
    return ""


def status(settings: Any) -> dict[str, Any]:
    """What the composer and Settings need: which engines may listen, and why not.

    Reads settings and the policy only; never contacts a service.
    """
    from .policy import blocked_reason, current

    engine = _setting(settings, "engine") or "auto"
    if engine not in ENGINES:
        engine = "auto"
    service = _setting(settings, "service")
    if service and not _SERVICE.match(service):
        service = ""
    model = _setting(settings, "model") or DEFAULT_MODEL
    offered = services(settings)
    names = {item["id"]: item["name"] for item in offered}
    policy = current()
    result: dict[str, Any] = {
        "engine": engine,
        "service": service,
        "service_name": names.get(service, ""),
        "model": model,
        "language": _setting(settings, "language"),
        "browser": engine in ("auto", "browser"),
        "browser_reason": "",
        "service_ready": False,
        "reason": "",
        "locked": bool(policy and policy.locked("voice", "engine")),
        "organization": policy.organization if policy else "",
        "max_seconds": MAX_SECONDS,
        "services": offered,
    }
    if engine == "off":
        result["reason"] = (f"{policy.organization} turned dictation off." if result["locked"]
                            else "Dictation is off in Settings > Voice.")
        return result
    refusal = blocked_reason()
    if refusal:
        result["browser"] = False
        result["browser_reason"] = refusal
        result["reason"] = refusal
        return result
    if policy and policy.require_zero_retention and result["browser"]:
        result["browser"] = False
        result["browser_reason"] = (
            f"{policy.organization} allows only services that keep no data, and Lumi can't tell what your "
            "browser's speech service keeps. Choose a transcription service in Settings > Voice."
        )
    if engine in ("auto", "service"):
        problem = _service_problem(settings, service)
        if not problem and policy and not policy.model_allowed(service, model):
            problem = f"{policy.organization}'s policy doesn't allow {names.get(service, service)}'s {model} model."
        result["service_ready"] = not problem
        result["reason"] = problem
    return result


def _endpoint(settings: Any, service: str, *, transport: Any = None) -> tuple[str, dict[str, str], Any, str]:
    """(base URL, headers, TLS context, name) of a service that passed ``status``."""
    if service == "openai":
        return OPENAI_URL, {"Authorization": f"Bearer {_openai_key(settings)}"}, None, "OpenAI"
    from .connections import secret_setting, sign_in

    connection = _connection(settings, service)
    if connection is None:
        raise VoiceError("Choose a transcription service in Settings > Voice.")
    key = str(settings.get("api_keys", secret_setting(connection["id"]), "") or "")
    headers = dict(connection.get("headers") or {})
    try:
        token_provider, tls = sign_in(connection, key, transport=transport)
        if token_provider is not None:
            # With sign-in, the saved secret goes only to the token endpoint.
            headers["Authorization"] = f"Bearer {token_provider()}"
    except Exception as exc:
        raise VoiceError(f"Signing in to {connection['name']} failed: {exc}") from None
    if token_provider is None and connection["auth"] == "bearer" and key:
        headers["Authorization"] = f"Bearer {key}"
    elif token_provider is None and connection["auth"] == "header" and key:
        headers[connection["auth_header"]] = key
    base = connection["base_url"] or (OPENAI_URL if connection["type"] == "openai" else "")
    return base, headers, tls, connection["name"]


def media_type(value: Any) -> str:
    """The recording's type without codec parameters (``audio/webm;codecs=opus`` is WebM)."""
    return str(value or "").split(";", 1)[0].strip().lower()


def _failure(name: str, response: httpx.Response, base: str, model: str) -> str:
    status_code = response.status_code
    if status_code in (401, 403):
        return f"{name} refused the key ({status_code}). Check it in Settings > Connections."
    if status_code == 404:
        return f"{name} has no transcription endpoint at {base}/audio/transcriptions, or no model {model}."
    if status_code == 413:
        return f"The recording is too large for {name}. Dictate in shorter parts."
    if status_code == 429:
        return f"{name} is limiting requests. Wait a moment and try again."
    detail = ""
    try:
        body = response.json()
        error = body.get("error") if isinstance(body, dict) else None
        detail = str(error.get("message") if isinstance(error, dict) else error or body.get("detail") or "")
    except (ValueError, AttributeError):
        detail = ""
    detail = " ".join(detail.split())[:200]
    return f"{name} couldn't transcribe the recording ({status_code})" + (f": {detail}" if detail else ".")


def transcribe(settings: Any, audio: bytes, audio_type: str, *, project: str = "", transport: Any = None) -> str:
    """Send one recording to the chosen service and return the text; raise VoiceError.

    ``transport`` is for tests (an ``httpx.MockTransport``).
    """
    state = status(settings)
    if state["engine"] == "browser":
        raise VoiceError("Settings > Voice uses this window's speech recognition, not a transcription service.")
    if state["engine"] == "off":
        raise VoiceError(state["reason"])
    if not state["service_ready"]:
        raise VoiceError(state["reason"])
    kind = media_type(audio_type)
    if kind not in AUDIO_TYPES:
        raise VoiceError("Lumi can't send this kind of recording. Dictate again.")
    if not audio:
        raise VoiceError("Nothing was recorded. Check your microphone.")
    if len(audio) > MAX_AUDIO_BYTES:
        raise VoiceError("The recording is too long. Dictate in shorter parts.")

    service, model = state["service"], state["model"]
    base, headers, tls, name = _endpoint(settings, service, transport=transport)
    data = {"model": model, "response_format": "json"}
    language = state["language"].split("-", 1)[0].lower()
    if len(language) == 2:  # the service takes ISO 639-1; anything else is detected
        data["language"] = language
    files = {"file": (f"dictation.{AUDIO_TYPES[kind]}", audio, kind)}
    timeout = httpx.Timeout(connect=10.0, read=120.0, write=60.0, pool=10.0)
    started = time.monotonic()
    outcome, text = "error", ""
    try:
        with httpx.Client(**net.client_options(timeout=timeout, transport=transport, verify=tls)) as client:
            response = client.post(f"{base}/audio/transcriptions", headers=headers, data=data, files=files)
        if response.status_code >= 400:
            raise VoiceError(_failure(name, response, base, model))
        try:
            body = response.json()
        except ValueError:
            body = None
        if not isinstance(body, dict) or not isinstance(body.get("text"), str):
            raise VoiceError(f"{name} answered without a transcript.")
        text = body["text"].strip()[:MAX_TRANSCRIPT]
        outcome = "ok"
        return text
    except httpx.TimeoutException:
        raise VoiceError(f"{name} didn't answer in time. Try a shorter recording.") from None
    except httpx.HTTPError as exc:
        raise VoiceError(f"Lumi couldn't reach {name} ({type(exc).__name__}).") from None
    finally:
        elapsed = time.monotonic() - started
        # Dictation is priced per minute, not per token, so it is recorded
        # unpriced rather than at a chat model's token rates.
        usage.record(provider=service, model=model, stats={}, purpose="dictation", project=project,
                     elapsed=elapsed, priced=False)
        # Metadata only: the transcript reaches the log if and when it is sent.
        audit.record("voice.transcription", project=project, service=service, model=model,
                     audio_bytes=len(audio), media_type=kind, characters=len(text), outcome=outcome,
                     elapsed=round(elapsed, 3))
