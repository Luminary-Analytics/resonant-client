"""Tasks from Slack and Microsoft Teams, relayed by Lumi Cloud and run on this computer.

With **Tasks from Slack and Teams** turned on (Settings > Lumi account, the
``cloud.remote_tasks`` setting) on a computer enrolled with its person's own
account, the app asks Lumi Cloud every 20 seconds for that person's next
request from chat (lumi_cloud/chat_tasks.py in Lumi Cloud). It runs requests
one at a time in the chosen project and permission mode, in a session built
like ``lumi run``'s (lumi/headless.py) with the default model.

An action the mode doesn't allow is sent to the chat for approval. It is
refused when nobody answers within 10 minutes or the person says stop. The
reply, or what went wrong, goes back to the chat.

An organization can turn it off by locking ``cloud.remote_tasks`` in its
policy. A managed computer never takes anyone's requests.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any, Callable

logger = logging.getLogger(__name__)

POLL_SECONDS = 20.0
IDLE_SECONDS = 60.0
APPROVAL_SECONDS = 600.0
ANSWER_SECONDS = 2.0
STOP_CHECK_SECONDS = 5.0
MODES = ("ask", "auto-edit", "bypass")


class RemoteTasks:
    """Claims this computer's person's requests from chat and runs them."""

    def __init__(self, settings: Any, cloud: Any, *, session_factory: Callable | None = None,
                 sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic) -> None:
        self.settings = settings
        self.cloud = cloud
        self._factory = session_factory or self._build_session
        self._sleep = sleep
        self._clock = clock
        self.current = ""  # the task running now
        self.last: dict = {}  # how the last task ended, for Settings

    # ── Settings ───────────────────────────────────────────────────────────
    def options(self) -> dict:
        section = self.settings.get("cloud") or {}
        section = section if isinstance(section, dict) else {}
        mode = str(section.get("remote_tasks_mode") or "ask")
        return {"enabled": bool(section.get("remote_tasks")), "project": str(section.get("remote_tasks_project") or ""),
                "mode": mode if mode in MODES else "ask"}

    def blocked(self) -> str:
        """Why requests can't be taken now, or ""."""
        from .policy import blocked_reason

        options = self.options()
        device = self.cloud.device()
        if not options["enabled"]:
            return "Turned off."
        if not device:
            return "This computer isn't enrolled in Lumi Cloud."
        if device.get("how") == "managed":
            return "A managed computer doesn't take anyone's requests."
        if not os.path.isdir(options["project"]):
            return "Choose the project folder requests run in."
        return blocked_reason() or ""

    def status(self) -> dict:
        return {**self.options(), "blocked": self.blocked(), "running": bool(self.current), "last": self.last}

    # ── The loop ───────────────────────────────────────────────────────────
    def step(self) -> float:
        """Claim and run the next request, if any; returns seconds until the next look."""
        from .cloud import CloudError

        if self.blocked():
            return IDLE_SECONDS
        try:
            task = self.cloud.device_call("POST", "/api/v1/devices/tasks/claim")
        except CloudError as exc:
            logger.info("Couldn't ask Lumi Cloud for requests from chat: %s", exc)
            return IDLE_SECONDS
        if not task.get("id"):
            return POLL_SECONDS
        self.run(task)
        return 1.0  # there may be another one waiting

    def run(self, task: dict) -> None:
        from .cloud import CloudError

        task_id = str(task["id"])
        options = self.options()
        self.current = task_id
        status, text = "failed", ""
        try:
            session = self._factory(options["project"], options["mode"], task_id)
            stop = threading.Event()
            watcher = threading.Thread(target=self._watch, args=(task_id, session, stop), daemon=True,
                                       name="remote-task-stop")
            watcher.start()
            parts, error = [], ""
            try:
                for event in session.run(str(task.get("prompt") or ""), on_permission=self._asker(task_id, session)):
                    kind = event.get("event")
                    if kind == "text.done" and event.get("text"):
                        parts.append(str(event["text"]))
                    elif kind == "error":
                        error = str(event.get("message") or "unknown error")
            finally:
                stop.set()
            text = "\n\n".join(parts).strip()
            if session.cancel_requested:
                status = "stopped"
            elif error and not text:
                status, text = "failed", error
            else:
                status = "done"
        except Exception as exc:  # the chat must hear something, whatever went wrong here
            logger.exception("A request from chat failed")
            status, text = "failed", f"{type(exc).__name__}: {exc}"
        finally:
            self.current = ""
        self.last = {"status": status, "at": time.time()}
        try:
            self.cloud.device_call("POST", f"/api/v1/devices/tasks/{task_id}/result",
                                   json={"status": status, "text": text})
        except CloudError as exc:
            logger.warning("Couldn't report a request's result to Lumi Cloud: %s", exc)

    def _watch(self, task_id: str, session: Any, stop: threading.Event) -> None:
        """Stop the session when the person says stop in the chat."""
        from .cloud import CloudError

        while not stop.wait(STOP_CHECK_SECONDS):
            try:
                if self.cloud.device_call("GET", f"/api/v1/devices/tasks/{task_id}").get("stop_requested"):
                    session.cancel()
                    return
            except CloudError:
                continue

    def _asker(self, task_id: str, session: Any) -> Callable[[str, dict], bool]:
        """The session's permission prompt: ask in the chat, through Lumi Cloud."""
        from .cloud import CloudError
        from .gateway.service import describe_call

        def ask(tool_name: str, tool_args: dict) -> bool:
            try:
                approval = self.cloud.device_call("POST", f"/api/v1/devices/tasks/{task_id}/approvals",
                                                  json={"text": describe_call(tool_name, tool_args)})
            except CloudError as exc:
                logger.warning("Couldn't ask for approval in the chat: %s", exc)
                return False
            deadline = self._clock() + APPROVAL_SECONDS
            while self._clock() < deadline and not session.cancel_requested:
                try:
                    state = self.cloud.device_call("GET", f"/api/v1/devices/tasks/{task_id}")
                except CloudError:
                    state = {}
                if state.get("stop_requested"):
                    session.cancel()
                    return False
                answer = next((a.get("status") for a in state.get("approvals") or []
                               if a.get("id") == approval.get("id")), "pending")
                if answer in ("approved", "denied"):
                    return answer == "approved"
                self._sleep(ANSWER_SECONDS)
            return False

        return ask

    def _build_session(self, project: str, mode: str, task_id: str):
        """A session built like ``lumi run``'s, with the default model; its Ask mode asks in the chat."""
        from .gateway.cli import TIERS
        from .headless import _mode, build_session, build_spec

        mode = _mode(self.settings, mode)
        provider = str(self.settings.get("general", "default_backend", "") or "").strip().lower()
        model = str(self.settings.get("general", "default_model", "") or "").strip()
        spec = build_spec(self.settings, provider, model, project)
        if provider in {"codex", "claude-code"}:
            spec.permission_mode = mode
        session = build_session(self.settings, spec, project=project, mode=mode, trust_project=False,
                                max_requests=None, run_id=f"chat-{task_id}", tier=TIERS[mode])
        # Audit and usage records name the request.
        session.audit_session_id = f"chat-task:{task_id}"
        return session


def start(runner: RemoteTasks, *, first_delay: float = 30.0) -> threading.Thread:
    """Look for requests from chat for the app's lifetime."""

    def loop() -> None:
        time.sleep(first_delay)
        while True:
            try:
                delay = runner.step()
            except Exception:
                logger.exception("Tasks from chat stopped for a round")
                delay = IDLE_SECONDS
            time.sleep(delay)

    thread = threading.Thread(target=loop, daemon=True, name="lumi-remote-tasks")
    thread.start()
    return thread
