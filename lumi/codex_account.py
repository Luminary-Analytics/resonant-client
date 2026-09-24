"""Small stdio app-server client for Codex account and model discovery.

Codex owns credentials and browser login. Lumi only receives account
metadata, model choices, and rate limits; tokens never pass through its UI.
"""

from __future__ import annotations

import atexit
import json
import queue
import subprocess
import threading

from .backends import resolve_codex_cli_path


class CodexAccount:
    def __init__(self):
        self._lock = threading.RLock()
        self._process = None
        self._responses = queue.Queue()
        self._next_id = 0
        self._login = None

    def close(self):
        with self._lock:
            proc, self._process = self._process, None
            self._login = None
            if proc:
                if proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait(timeout=3)
                for pipe in (proc.stdin, proc.stdout):
                    if pipe:
                        pipe.close()

    def _start(self):
        if self._process and self._process.poll() is None:
            return
        path = resolve_codex_cli_path()
        if not path:
            raise ValueError("Install Codex CLI to connect your ChatGPT subscription, then refresh.")
        self.close()
        responses = self._responses = queue.Queue()
        self._process = proc = subprocess.Popen(
            [path, "app-server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )

        def read():
            try:
                for line in proc.stdout:
                    try:
                        message = json.loads(line)
                    except (ValueError, TypeError):
                        continue
                    if "id" in message:
                        responses.put(message)
            finally:
                responses.put({"error": {"message": "Codex connection closed. Refresh to reconnect."}})

        threading.Thread(target=read, daemon=True, name="resonant-codex-account").start()
        try:
            self._rpc("initialize", {"clientInfo": {"name": "lumi", "title": "Lumi", "version": "1"}})
            self._send({"method": "initialized", "params": {}})
        except Exception:
            self.close()
            raise

    def _send(self, message):
        self._process.stdin.write(json.dumps(message) + "\n")
        self._process.stdin.flush()

    def _rpc(self, method, params=None):
        self._next_id += 1
        request_id = self._next_id
        self._send({"id": request_id, "method": method, "params": params or {}})
        while True:
            try:
                response = self._responses.get(timeout=15)
            except queue.Empty:
                self.close()
                raise TimeoutError("Codex took too long to respond. Refresh the connection.") from None
            if response.get("id") not in (None, request_id):
                continue
            if "error" in response:
                raise RuntimeError(str(response["error"].get("message") or "Codex request failed"))
            return response.get("result") or {}

    def status(self):
        with self._lock:
            self._start()
            account = self._rpc("account/read", {"refreshToken": False}).get("account")
            result = {"account": account, "models": [], "rate_limits": None}
            cursor = None
            for _ in range(10):
                page = self._rpc("model/list", {"limit": 100, "includeHidden": False, "cursor": cursor})
                result["models"].extend(page.get("data") or [])
                cursor = page.get("nextCursor")
                if not cursor:
                    break
            if account and str(account.get("type", "")).startswith("chatgpt"):
                try:
                    result["rate_limits"] = self._rpc("account/rateLimits/read")
                except RuntimeError:
                    pass  # Account access and quota availability are separate.
                self._login = None
            return result

    def login(self):
        with self._lock:
            self._start()
            if self._login:
                try:
                    self._rpc("account/login/cancel", {"loginId": self._login})
                except RuntimeError:
                    pass  # The previous browser flow may already have expired.
            result = self._rpc("account/login/start", {"type": "chatgpt"})
            self._login = result.get("loginId")
            return {"auth_url": result.get("authUrl")}

    def cancel_login(self):
        with self._lock:
            if self._process and self._login:
                self._rpc("account/login/cancel", {"loginId": self._login})
                self._login = None


codex_account = CodexAccount()
atexit.register(codex_account.close)
