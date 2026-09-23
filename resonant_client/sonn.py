"""SONN's project-scoped Chat Completions transport and model discovery."""

from __future__ import annotations

import copy
import hashlib
import json
import threading
import time
import uuid
from urllib.parse import urlsplit

import httpx

from .backends import EVENT_ERROR, ExoBackend, KimiBackend, _convert_tools_for_ollama
from .capabilities import ModelCapabilities
from .network_defaults import resolve_sonn_url


class SonnBackend(KimiBackend):
    """Use the standard OpenAI wire format without Moonshot/OpenRouter extensions."""

    PROVIDER_LABEL = "SONN"
    RETRY_EVENT_KIND = "sonn_retry"
    DEFAULT_MODEL = "sonn-auto"
    dynamic_tool_catalog_via_history = False
    # Enables the inherited watcher to close a blocked local stream on Stop.
    # SONN's supplied contract does not define a server-side cancel endpoint.
    supports_remote_cancel = True
    _catalog_lock = threading.Lock()
    _catalogs: dict[tuple[str, str], tuple[float, list[dict]]] = {}

    @staticmethod
    def validate_base_url(value: str) -> str:
        """Preserve the project path and reject credentials embedded in URLs."""
        value = str(value or "").strip().rstrip("/")
        try:
            parsed = urlsplit(value)
            local_http = parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
            if (not parsed.hostname or not (parsed.scheme == "https" or local_http)
                    or parsed.username or parsed.password or parsed.query or parsed.fragment):
                raise ValueError
            parsed.port  # Validate malformed ports before a request is constructed.
        except ValueError:
            raise ValueError("Set a SONN API base URL in Settings → Network (HTTPS, or HTTP on localhost).") from None
        return value

    @classmethod
    def _cache_key(cls, base_url: str, api_key: str) -> tuple[str, str]:
        return base_url, hashlib.sha256(api_key.encode()).hexdigest()

    @classmethod
    def catalog(cls, api_key: str, *, base_url: str, force: bool = False, transport=None) -> list[dict]:
        """Discover models, caching for five minutes per project URL and credential."""
        base_url = cls.validate_base_url(base_url)
        api_key = str(api_key or "").strip()
        if not api_key or api_key == "YOUR_PRIVATE_INVITATION":
            raise ValueError("Add your SONN API key in Settings → API keys, or set SONN_API_KEY.")
        cache_key = cls._cache_key(base_url, api_key)
        with cls._catalog_lock:
            cached = cls._catalogs.get(cache_key)
            if cached and not force and time.monotonic() - cached[0] < 300:
                return copy.deepcopy(cached[1])
        # Never hold the catalog lock during network I/O or serialize other accounts.
        try:
            with httpx.Client(timeout=5.0, transport=transport) as client:
                response = client.get(f"{base_url}/models", headers={"Authorization": f"Bearer {api_key}"})
                if response.status_code != 200:
                    with cls._catalog_lock:
                        cls._catalogs.pop(cache_key, None)
                    raise ValueError(cls._user_error_message(response.status_code, "", ""))
                body = response.json()
            if not isinstance(body, dict) or not isinstance(body.get("data"), list):
                raise ValueError("SONN returned an invalid model catalog.")
            rows = []
            seen = set()
            for row in body["data"]:
                if not isinstance(row, dict) or not isinstance(row.get("id"), str) or not row["id"].strip():
                    continue
                model = row["id"].strip()
                if model in seen:
                    continue
                seen.add(model)
                # Model discovery has no required capability extensions. Keep only
                # recognized fields; never echo arbitrary server metadata into UI state.
                clean = {"id": model, "name": str(row.get("name") or model)}
                window = row.get("context_length")
                if isinstance(window, int) and not isinstance(window, bool) and window > 0:
                    clean["context_length"] = window
                rows.append(clean)
        except httpx.HTTPError:
            raise ValueError("Cannot reach SONN. Check the API base URL and your connection.") from None
        except (TypeError, KeyError, json.JSONDecodeError):
            raise ValueError("SONN returned an invalid model catalog.") from None
        with cls._catalog_lock:
            if cache_key not in cls._catalogs and len(cls._catalogs) >= 16:
                oldest = min(cls._catalogs, key=lambda key: cls._catalogs[key][0])
                cls._catalogs.pop(oldest)
            cls._catalogs[cache_key] = (time.monotonic(), rows)
        return copy.deepcopy(rows)

    def __init__(self, api_key: str, model: str = DEFAULT_MODEL, *, base_url: str = "", transport=None):
        self.base_url = self.validate_base_url(resolve_sonn_url(base_url))
        self.api_key = str(api_key or "").strip()
        if not self.api_key or self.api_key == "YOUR_PRIVATE_INVITATION":
            raise ValueError("Add your SONN API key in Settings → API keys, or set SONN_API_KEY.")
        self.model = str(model or self.DEFAULT_MODEL).strip()
        self.name = "sonn"
        self.conversation_id = ""
        self._auxiliary = False
        self.task_controller = None
        self._task_request_id = ''
        self.handles_tools = False
        self.thinking_mode = ""
        self._transport = transport
        self._timeout = httpx.Timeout(connect=15, read=180, write=60, pool=60)
        with self._catalog_lock:
            cached = self._catalogs.get(self._cache_key(self.base_url, self.api_key))
            rows = cached[1] if cached and time.monotonic() - cached[0] < 300 else []
            row = next((item for item in rows if item["id"] == self.model), {})
        # Standard /models does not advertise vision, reasoning, or context limits.
        # Try ordinary function tools; retain the engine's text fallback. Do not
        # infer capabilities of the routed model from the sonn-auto alias.
        self._capabilities = ModelCapabilities(
            model=self.model, context_window=row.get("context_length", 32768),
            native_tools=True, parallel_tools=None, modalities=("text",),
            max_safe_concurrency=1, source="fallback",
        )

    @property
    def effective_context_tokens(self) -> int:
        return self._capabilities.context_window

    def _api_content(self, content):
        return ExoBackend._api_content(self, content)

    def _messages(self, conversation_history, instructions, user_msg, **kwargs):
        history = []
        for turn in conversation_history:
            if turn.get("role") == "tool_catalog":
                continue
            if turn.get("role") == "tool_result" and turn.get("image"):
                # Kimi's shared adapter appends tool screenshots as synthetic
                # user image messages, bypassing _api_content. SONN's gateway
                # accepts text only. Preserve the image locally and retain this
                # as a tool observation, never an extra human learning input.
                turn = {key: value for key, value in turn.items() if key != "image"}
                turn["content"] = str(turn.get("content") or "") + (
                    "\nImage artifact retained locally. This SONN connection accepts text only; "
                    "inspect browser DOM, accessibility or evaluation results for page behavior. "
                    "This notice is not evidence of visual appearance."
                )
            history.append(turn)
        messages = super()._messages(history, instructions, user_msg, **kwargs)
        retained = [{"role": "system", "content": str(turn.get("content") or "")}
                    for turn in history if turn.get("role") == "system" and turn.get("content")]
        messages[1:1] = retained
        for message in messages:
            message.pop("reasoning_content", None)
        return messages

    def _payload(self, user_msg, conversation_history, instructions, tools, max_tokens):
        payload = {
            "model": self.model, "messages": self._messages(conversation_history, instructions, user_msg),
            "stream": True, "stream_options": {"include_usage": True},
        }
        if self.conversation_id:
            # SONN scopes this conversation label to the authenticated project.
            # It is continuity metadata, never account-selection authority.
            payload["user"] = self.conversation_id
        if self._auxiliary:
            payload["metadata"] = {
                "sonn_input_origin": "generated", "sonn_capture_inputs": False,
                "sonn_observation_learning": False, "sonn_human_learning": False,
                "sonn_learning_control": "none",
            }
        else:
            # Match user turns in order, not by a set of their text: a human can
            # legitimately repeat text previously produced by the harness.
            users = iter(turn for turn in conversation_history if turn.get("role") == "user")
            current = next(users, None)
            excluded = []
            for index, message in enumerate(payload["messages"]):
                if message.get("role") != "user":
                    continue
                if current is not None and message["content"] == self._api_content(current.get("content", "")):
                    if current.get("input_origin") == "generated":
                        excluded.append(index)
                    current = next(users, None)
                elif conversation_history:
                    # An appended continuation or tool image is not a new
                    # human message. Human turns are persisted before dispatch.
                    excluded.append(index)
            if excluded:
                payload["metadata"] = {"sonn_generated_user_indices": excluded}
        converted = _convert_tools_for_ollama(tools)
        if converted:
            payload["tools"] = list({tool["function"]["name"]: tool for tool in converted}.values())
        if max_tokens:
            payload["max_tokens"] = max(1, int(max_tokens))
        if self.task_controller:
            task_state = self.task_controller.state()
            payload.setdefault('metadata', {}).update(sonn_task_id=task_state['root_id'],
                                                     sonn_max_internal_calls=1)
            if self._auxiliary:
                payload['metadata']['sonn_task_purpose'] = 'compression'
            advice = task_state.get('advice')
            if advice and advice.get('following_request') == self._task_request_id:
                payload['metadata']['sonn_after_advice'] = advice['request_id']
        return payload

    def enable_employee_task(self, journal_path, *, ceiling_microusd, descriptor=None):
        """Explicitly bind this native run to a durable, server-enforced allowance."""
        from .sonn_tasks import SonnTaskController
        self.task_controller = SonnTaskController(self, journal_path, ceiling_microusd=ceiling_microusd,
                                                  descriptor=descriptor)
        return self.task_controller.start()

    def _request_headers(self):
        headers = super()._request_headers()
        if self._task_request_id:
            headers['Idempotency-Key'] = self._task_request_id
        return headers

    def consult_employee(self, question, *, source_teaching_ids=None, mode='advise'):
        """Request bounded advice; the next execution receives its generated context."""
        if self.task_controller is None:
            from .sonn_tasks import SonnTaskError
            raise SonnTaskError('A saved employee root task is required for frontier advice')
        return self.task_controller.ask_advice(question, source_teaching_ids=source_teaching_ids, mode=mode)

    def stream_auxiliary(self, *, purpose: str, **kwargs):
        """Use an isolated request without mutating a concurrent coding stream."""
        if self.task_controller and purpose != 'compression':
            yield EVENT_ERROR, {'message': 'Optional generation is deferred during a bounded employee task.',
                                'code': 'sonn_task_auxiliary_deferred'}
            return
        auxiliary = copy.copy(self)
        auxiliary._auxiliary = True
        auxiliary.conversation_id = self.conversation_id if self.task_controller else "sonn-client:aux:" + uuid.uuid4().hex
        yield from auxiliary.stream(**kwargs)

    def cancel_task(self):
        """Keep Stop responsive while cancelling the captured server root."""
        controller = self.task_controller
        if controller:
            controller.mark_cancelled()
            def notify():
                from .sonn_tasks import SonnTaskError
                try:
                    controller.cancel()
                except SonnTaskError:
                    pass  # Local cancellation persists; recovery keeps uncertain holds.
            threading.Thread(target=notify, name='sonn-task-cancel', daemon=True).start()

    @classmethod
    def _user_error_message(cls, status_code, error_type, message):
        if status_code in {401, 403}:
            return "SONN rejected the request. Check your API key and access to the configured project."
        if status_code == 404:
            return "SONN endpoint or model not found. Check the project API base URL and model name."
        if status_code == 402:
            return "SONN has insufficient available prepaid credit. Check your workspace balance and pending reservations."
        if status_code == 429:
            if error_type == "learning_queue_full":
                return ("SONN is catching up on saved learning observations. This request was rejected "
                        "before paid generation. Completed work is retained; continue after the learning queue drains.")
            return "SONN is temporarily rate limited or at capacity. Wait before continuing."
        if status_code == 422:
            return "This request exceeds a SONN project or model limit. Check the context, output and per-request allowance in your workspace."
        if status_code >= 500:
            return (f"SONN service or upstream generation failed (HTTP {status_code}). "
                    "Completed work is retained. Check request status and pending reservations before continuing; "
                    "the request was not automatically retried.")
        return f"SONN rejected the request (HTTP {status_code}). Check its format and the selected model."

    @classmethod
    def _is_retryable_error(cls, status_code, error_type, message):
        # Only this structured admission code is issued before reservation and
        # upstream dispatch. A generic 429, timeout or stream failure is uncertain.
        return status_code == 429 and error_type == "learning_queue_full"

    def _http_retry_delay(self, response, attempt):
        # Worker ticks are approximately one minute apart. Bound the two waits,
        # respect a longer advertised cooldown, and keep Stop interruptible.
        raw = response.headers.get("retry-after", "60")
        try:
            seconds = int(raw)
        except (ValueError, TypeError):
            return None
        return max(60, seconds) if 0 <= seconds <= 120 else None

    @staticmethod
    def _is_retryable_stream_error(message):
        return False

    def _progress_idle_timeout_seconds(self):
        return 0.0  # Use SONN's configured HTTP read timeout, not EXO runner state.

    def _timeout_error_message(self):
        return ("SONN stopped responding before this request completed. Completed work is retained. "
                "Check request status and pending reservations before continuing; "
                "the request was not automatically retried.")

    @classmethod
    def _error_details(cls, response):
        error_type, detail = KimiBackend._error_details(response)
        try:
            body = response.json()
            error = body.get("error") if isinstance(body, dict) else None
            if (response.status_code == 429 and isinstance(error, dict)
                    and error.get("code") == "learning_queue_full"):
                return "learning_queue_full", cls._user_error_message(429, "learning_queue_full", "")
        except (ValueError, TypeError):
            pass
        quota = "insufficient_quota" if cls._is_quota_error(error_type, detail) else ""
        return quota, cls._user_error_message(response.status_code, quota, "")

    def stream(self, *args, **kwargs):
        from .sonn_tasks import SonnTaskError
        try:
            if self.task_controller:
                _, self._task_request_id = self.task_controller.begin_request(
                    purpose='compression' if self._auxiliary else 'execution')
        except SonnTaskError as exc:
            yield EVENT_ERROR, {'message': str(exc), 'code': 'sonn_task_unresolved'}
            return
        for event, data in super().stream(*args, **kwargs):
            if self.task_controller and event == 'done':
                try:
                    self.task_controller.recover()
                except SonnTaskError as exc:
                    yield EVENT_ERROR, {'message': str(exc), 'code': 'sonn_task_unresolved'}
                    return
            if event == EVENT_ERROR and data.get("message", "").startswith("SONN generation failed:"):
                data = {**data, "code": "sonn_stream_failed", "message": (
                    "SONN generation was interrupted by a service or upstream stream failure. "
                    "Completed work is retained. Check request status and pending reservations before continuing; "
                    "the request was not automatically retried.")}
            yield event, data

    def health(self) -> dict:
        """Check authenticated model discovery without generating tokens."""
        rows = self.catalog(self.api_key, base_url=self.base_url, force=True, transport=self._transport)
        return {"status": "ready", "backend": self.name, "model_count": len(rows),
                "models": [row["id"] for row in rows],
                "model_labels": {row["id"]: row["name"] for row in rows}}
