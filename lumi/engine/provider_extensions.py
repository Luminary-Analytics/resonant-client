"""Model providers added as extensions (capability packs with ``providers``; docs/extensions.md).

A pack's manifest (``lumi-pack.json``, ``manifest_version`` 1) can declare
providers, each a command Lumi starts as a separate process for every
request, in the pack's folder::

    "providers": [{"id": "acme", "name": "Acme LLM", "command": ["python", "provider.py"],
                   "models": [{"id": "acme-1", "context_window": 128000, "tools": true}]}]

``command`` may instead give one command per system (``{"windows": [...],
"macos": [...], "linux": [...]}``). A program given as a relative path runs
from inside the pack; a bare name is looked up on PATH, never in Lumi's
current folder.

Lumi writes one JSON request to the process's stdin and closes it; the
process writes one JSON object per line to stdout and exits:

* request ``{"lumi_extension": 1, "method": "models"}`` → ``{"type": "models",
  "models": [{"id": ..., "context_window": ..., "tools": true}]}``;
* request ``{"lumi_extension": 1, "method": "stream", "params": {"model",
  "messages", "tools", "max_tokens"}}`` → any number of ``{"type": "text",
  "text": ...}`` and ``{"type": "tool_call", "id", "name", "arguments": {...}}``,
  then ``{"type": "done", "usage": {"input_tokens", "output_tokens"}}``
  (``"cost_usd"`` too, when the provider knows the price), or
  ``{"type": "error", "message": ...}``. Types Lumi doesn't know are ignored.

Messages are ``{"role": "system" | "user" | "assistant" | "tool", "content": text}``;
an assistant message may carry ``tool_calls`` and a tool message a
``tool_call_id``. Images reach the provider as honest text notices. The
process gets the environment Lumi gives children (``secrets_store.child_env``)
plus the connection's key as ``LUMI_PROVIDER_API_KEY``, when one is saved,
and ``LUMI_EXTENSION_DATA``, a folder of its own for files it keeps.

A provider must not write inside its pack: any change there withdraws the
approval. Python keeps its bytecode cache outside the pack for the same
reason (``PYTHONPYCACHEPREFIX``), which also means ``.pyc`` files shipped in a
pack never run in place of the source that was reviewed.

A provider appears as a model connection of type ``extension``
(lumi/connections.py). It runs only from a pack outside any project that is
approved and enabled in Settings > Capability packs, and every request checks
again, so a pack that changed or lost its approval stops at once.
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Iterator

from ..backends import EVENT_DONE, EVENT_ERROR, EVENT_TEXT_DELTA, EVENT_TOOL_CALL, _described, _new_call_id
from ..capabilities import ModelCapabilities
from ..content import content_text, text_fallback

PROTOCOL = 1
TIMEOUT = float(os.environ.get("LUMI_EXTENSION_TIMEOUT_SEC", "600"))
MODELS_TIMEOUT = 30.0
SYSTEMS = {"win32": "windows", "darwin": "macos"}


class ProviderExtensionError(RuntimeError):
    """The provider extension can't run or answered wrongly; the message says why."""


def pack_manager(settings: Any = None):
    """A pack manager for packs outside any project, with the approvals saved in Settings."""
    from ..paths import state_home
    from .capability_packs import CapabilityPackManager

    configured = (settings.get("plugins") or {}) if settings is not None else {}
    # Providers are global like connections, so only packs outside any project
    # count: no pack can be inside this project path.
    return CapabilityPackManager(state_home() / "packs" / ".no-project", configured=configured)


def available(settings: Any = None) -> list[dict]:
    """Every provider in an installed pack, for Settings, with whether it can run now."""
    rows = []
    for pack in pack_manager(settings).discover():
        if pack.scope != "user":
            continue
        for provider in pack.providers:
            rows.append({"pack": pack.id, "pack_name": pack.name, "provider": provider["id"],
                         "name": provider["name"], "models": [m["id"] for m in provider.get("models") or []],
                         "ready": bool(pack.trusted and pack.enabled), "status": pack.status})
    return rows


def find_provider(pack_id: str, provider_id: str, settings: Any = None) -> tuple[Any, dict]:
    """The approved pack and its provider entry; ProviderExtensionError when it can't run."""
    pack = next((p for p in pack_manager(settings).discover() if p.id == pack_id and p.scope == "user"), None)
    if pack is None:
        raise ProviderExtensionError(f"The {pack_id} pack isn't installed. Install it in Settings > Capability packs.")
    if not (pack.trusted and pack.enabled):
        why = " It changed since you approved it." if pack.status == "changed" else f" {pack.problem}" if pack.problem else ""
        raise ProviderExtensionError(f"Approve the {pack.name} pack in Settings > Capability packs to use its models.{why}")
    provider = next((p for p in pack.providers if p.get("id") == provider_id), None)
    if provider is None:
        raise ProviderExtensionError(f"The {pack.name} pack has no provider {provider_id!r}.")
    return pack, provider


def find_program(name: str, path: str | None = None) -> str:
    """The full path of program ``name`` on PATH.

    Starting a bare name would let Windows look in Lumi's current folder
    first, which may be a repository, so only absolute PATH entries count.
    """
    folders = (os.environ.get("PATH", "") if path is None else path).split(os.pathsep)
    suffixes = [""]
    if os.name == "nt":
        pathext = [ext for ext in (os.environ.get("PATHEXT") or ".COM;.EXE;.BAT;.CMD").split(";") if ext]
        if not any(name.lower().endswith(ext.lower()) for ext in pathext):
            suffixes = pathext
    for folder in folders:
        if not folder or not os.path.isabs(folder):
            continue  # a relative entry would resolve against the current folder
        for suffix in suffixes:
            candidate = os.path.join(folder, name + suffix)
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                return candidate
    raise ProviderExtensionError(f"{name} isn't installed on this computer (it isn't on PATH).")


def command_for(pack: Any, provider: dict) -> list[str]:
    """The provider's command on this system, with the program's full path."""
    command = provider.get("command")
    if isinstance(command, dict):
        command = command.get(SYSTEMS.get(sys.platform, "linux")) or command.get("default")
    label = f"{pack.name}'s {provider.get('name') or provider.get('id')} provider"
    if not isinstance(command, list) or not command:
        raise ProviderExtensionError(f"{label} doesn't run on this system.")
    program = str(command[0])
    if os.path.isabs(program):
        return [program, *command[1:]]
    if os.path.dirname(program):
        root = Path(pack.path).resolve()
        resolved = (root / program).resolve()
        if root not in resolved.parents:
            raise ProviderExtensionError(f"{label} names a program outside its pack: {program}")
        return [str(resolved), *command[1:]]
    return [find_program(program), *command[1:]]


def data_dir(pack_id: str) -> Path:
    """The folder a pack's providers may keep files in (``LUMI_EXTENSION_DATA``)."""
    from ..paths import state_home

    path = state_home() / "extensions" / pack_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def _redacted(text: str, api_key: str) -> str:
    from ..secret_scan import redact_text

    text, _ = redact_text(text, patterns=True)
    if api_key:
        text, _ = redact_text(text, patterns=False, known=[api_key])
    return text


def run(pack: Any, command: list[str], request: dict, *, api_key: str = "",
        cancel_event: threading.Event | None = None, timeout: float = TIMEOUT) -> Iterator[dict]:
    """Start the provider in ``pack``'s folder, send ``request``, and yield each JSON object it writes."""
    from .. import secrets_store
    from ..paths import state_home
    from ..processes import background_process_kwargs

    env = secrets_store.child_env()
    env["LUMI_EXTENSION_PROTOCOL"] = str(PROTOCOL)
    env["LUMI_EXTENSION_DATA"] = str(data_dir(pack.id))
    # Bytecode caches go outside the pack, so running it doesn't change what was approved.
    env["PYTHONPYCACHEPREFIX"] = str(state_home() / "cache" / "extension-pycache")
    env.pop("LUMI_PROVIDER_API_KEY", None)
    if api_key:
        env["LUMI_PROVIDER_API_KEY"] = api_key
    try:
        process = subprocess.Popen(command, cwd=pack.path, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
                                   **background_process_kwargs())
    except OSError as exc:
        raise ProviderExtensionError(f"The provider couldn't start ({' '.join(command)}): {exc}") from exc
    lines: queue.Queue = queue.Queue()
    errors: list[str] = []

    def pump_stdout() -> None:
        for line in process.stdout:
            lines.put(line)
        lines.put(None)  # the end of its output

    def pump_stderr() -> None:
        for line in process.stderr:
            if sum(map(len, errors)) < 64_000:
                errors.append(line)

    threading.Thread(target=pump_stdout, daemon=True).start()
    threading.Thread(target=pump_stderr, daemon=True).start()
    try:
        process.stdin.write(json.dumps({"lumi_extension": PROTOCOL, **request}, ensure_ascii=False) + "\n")
        process.stdin.close()
    except OSError:
        pass  # it may have exited already; its output says why
    deadline = time.monotonic() + timeout
    try:
        while True:
            if cancel_event is not None and cancel_event.is_set():
                return
            if time.monotonic() > deadline:
                raise ProviderExtensionError(f"The provider didn't finish within {int(timeout)} seconds.")
            try:
                line = lines.get(timeout=0.1)
            except queue.Empty:
                continue
            if line is None:
                break
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except ValueError as exc:
                raise ProviderExtensionError(
                    f"The provider wrote something that isn't JSON: {_redacted(line[:200], api_key)}") from exc
            if isinstance(message, dict):
                yield message
        code = process.wait(timeout=10)
        if code:
            tail = _redacted("".join(errors)[-600:].strip(), api_key)
            raise ProviderExtensionError(f"The provider exited with code {code}" + (f": {tail}" if tail else "."))
    finally:
        if process.poll() is None:
            process.kill()


def list_models(pack: Any, provider: dict, *, api_key: str = "", timeout: float = MODELS_TIMEOUT) -> list[dict]:
    """The models the provider reports; the manifest's list if it answers without one."""
    for message in run(pack, command_for(pack, provider), {"method": "models"}, api_key=api_key,
                       timeout=timeout):
        if message.get("type") == "models":
            rows = message.get("models") or []
            return [row for row in rows if isinstance(row, dict) and row.get("id")][:200]
        if message.get("type") == "error":
            raise ProviderExtensionError(str(message.get("message") or "The provider refused to list its models."))
    return list(provider.get("models") or [])


def messages(conversation_history: list, instructions: str, user_msg: Any) -> list[dict]:
    """The conversation in the extension protocol's neutral form, as text."""
    result: list[dict] = [{"role": "system", "content": instructions}] if instructions else []
    for turn in conversation_history:
        role = str(turn.get("role") or "")
        if role in ("user", "assistant"):
            result.append({"role": role, "content": content_text(turn.get("content"))})
        elif role == "tool_call":
            try:
                arguments = json.loads(turn.get("arguments") or "{}")
            except (TypeError, ValueError):
                arguments = {}
            call = {"id": str(turn.get("call_id") or ""), "name": str(turn.get("name") or ""),
                    "arguments": arguments if isinstance(arguments, dict) else {}}
            last = result[-1] if result else {}
            if last.get("role") == "assistant" and last.get("tool_calls") is not None:
                last["tool_calls"].append(call)
            else:
                # "content" of a tool_call entry is a placeholder ("Called x"), not the model's words.
                result.append({"role": "assistant", "content": str(turn.get("assistant_content") or ""),
                               "tool_calls": [call]})
        elif role == "tool_result":
            content = content_text(turn.get("content"))
            image = turn.get("image")
            if isinstance(image, dict) and image:
                # A text-only provider gets the screenshot's description or an
                # honest notice, never a claim that it saw the image.
                notice = text_fallback({"type": "image", "media_type": str(image.get("media_type") or "image/png"),
                                        "name": f"{turn.get('name', 'tool')} screenshot", **_described(image)})
                content = f"{content}\n\n{notice}".strip()
            result.append({"role": "tool", "tool_call_id": str(turn.get("call_id") or ""), "content": content})
    # Like the other adapters: the message is sent unless the history just recorded it.
    text = content_text(user_msg)
    recent = {content_text(t.get("content")).strip() for t in conversation_history[-3:] if t.get("role") == "user"}
    if text.strip() and text.strip() not in recent:
        result.append({"role": "user", "content": text})
    return result


def _count(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


class ExtensionBackend:
    """A model connection whose provider is a pack's process (connection type ``extension``)."""

    supports_dynamic_tool_catalog = False
    supports_remote_cancel = False

    def __init__(self, connection: dict, model: str, api_key: str = "", *, settings: Any = None) -> None:
        from ..connections import backend_key

        self.connection = connection
        self._settings = settings
        self.pack, self.provider = find_provider(connection["pack"], connection["provider"], settings)
        self.model = str(model or "").strip()
        self.api_key = api_key
        self.name = backend_key(connection["id"])
        self.handles_tools = False
        self.tool_mode = "native"
        declared = next((m for m in self.provider.get("models") or [] if m.get("id") == self.model), {})
        context = int(connection.get("context_window") or declared.get("context_window") or 32_768)
        self._capabilities = ModelCapabilities(
            model=self.model, context_window=context, modalities=("text",),
            native_tools=bool(declared.get("tools", True)), parallel_tools=False, structured_output=None,
            reasoning_levels=(), reasoning_can_disable=False, prompt_caching=False, native_continuation=False,
            max_safe_concurrency=1, source="provider")

    @property
    def effective_context_tokens(self) -> int:
        return self._capabilities.context_window

    @property
    def capability_profile(self) -> ModelCapabilities:
        return self._capabilities

    def _current(self) -> tuple[Any, dict]:
        """The pack as it is now: installed, approved, enabled and unchanged, checked before every start."""
        self.pack, self.provider = find_provider(self.connection["pack"], self.connection["provider"], self._settings)
        return self.pack, self.provider

    def list_models(self) -> list[str]:
        pack, provider = self._current()
        return [m["id"] for m in list_models(pack, provider, api_key=self.api_key)]

    def health(self) -> dict:
        try:
            return {"ok": True, "models": self.list_models()}
        except ProviderExtensionError as exc:
            return {"ok": False, "error": str(exc)}

    def stream(self, user_msg: Any, conversation_history: list, instructions: str, tools: list,
               max_tokens: int | None = None, cancel_event=None) -> Iterator[tuple[str, dict]]:
        try:
            pack, provider = self._current()
            request = {"method": "stream", "params": {
                "model": self.model, "messages": messages(conversation_history, instructions, user_msg),
                "tools": [tool.get("function", tool) for tool in tools or [] if isinstance(tool, dict)],
                "max_tokens": max_tokens}}
            for message in run(pack, command_for(pack, provider), request, api_key=self.api_key,
                               cancel_event=cancel_event):
                kind = message.get("type")
                if kind == "text" and message.get("text"):
                    yield EVENT_TEXT_DELTA, {"delta": str(message["text"])}
                elif kind == "tool_call" and message.get("name"):
                    arguments = message.get("arguments") if isinstance(message.get("arguments"), dict) else {}
                    encoded = json.dumps(arguments, ensure_ascii=False)
                    yield EVENT_TOOL_CALL, {"name": str(message["name"]), "arguments": encoded,
                                            "call_id": str(message.get("id") or _new_call_id(message["name"], encoded))}
                elif kind == "error":
                    yield EVENT_ERROR, {"message": f"{pack.name}: {message.get('message') or 'the request failed'}"}
                    return
                elif kind == "done":
                    usage = message.get("usage") if isinstance(message.get("usage"), dict) else {}
                    stats: dict[str, Any] = {"input_tokens": _count(usage.get("input_tokens")),
                                             "output_tokens": _count(usage.get("output_tokens")),
                                             "provider": self.name}
                    cost = message.get("cost_usd")
                    if isinstance(cost, (int, float)) and not isinstance(cost, bool):
                        stats["cost_usd"] = float(cost)  # checked by pricing.price like any reported cost
                    yield EVENT_DONE, {"model": self.model, "stats": stats, "cognitive_state": None}
                    return
        except ProviderExtensionError as exc:
            yield EVENT_ERROR, {"message": str(exc)}
            return
        if cancel_event is None or not cancel_event.is_set():
            yield EVENT_ERROR, {"message": f"{self.pack.name}'s provider stopped without finishing its answer."}

    def classify(self, prompt: str, max_tokens: int = 50) -> str:
        parts = []
        for kind, data in self.stream(prompt, [], "", [], max_tokens):
            if kind == EVENT_TEXT_DELTA:
                parts.append(data["delta"])
        return "".join(parts).strip()
