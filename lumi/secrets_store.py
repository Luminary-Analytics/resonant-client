"""API keys in the operating system's credential store, and clean child environments.

Keys typed into Settings are saved in Windows Credential Manager, the macOS
Keychain or the Secret Service on Linux (through ``keyring``) under the
service name ``Lumi``. ``settings.json`` then holds only a placeholder, so the
file can be backed up, synced or attached to a support ticket without leaking
keys. Where no credential store exists (a headless Linux box, a locked-down
image), keys stay in ``settings.json`` as before and Settings says so.

``LUMI_KEYCHAIN=off`` disables the store (tests and fixtures use it) and
``LUMI_KEYCHAIN_SERVICE`` changes the service name.
"""

from __future__ import annotations

import logging
import os
import sys
import threading

logger = logging.getLogger(__name__)

PLACEHOLDER = "__keychain__"
DEFAULT_SERVICE = "Lumi"

# Model-provider keys Lumi itself reads. They are removed from the environment
# of hooks, MCP servers and the agent's shell: those run code Lumi does not
# control, and none of them needs Lumi's model credentials.
PROVIDER_KEY_ENV = (
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "AZURE_OPENAI_API_KEY",
    "OPENROUTER_API_KEY",
    "MOONSHOT_API_KEY",
    "SONN_API_KEY",
    "EXO_API_KEY",
    "AWS_BEARER_TOKEN_BEDROCK",
)


def child_env(base: dict[str, str] | None = None) -> dict[str, str]:
    """A copy of the environment without Lumi's model-provider keys.

    Callers add what a child is configured with afterwards, such as an MCP
    server entry's own ``env``.
    """
    env = dict(os.environ if base is None else base)
    # Windows variable names are case-insensitive: openai_api_key counts too.
    for name in [key for key in env if key.upper() in PROVIDER_KEY_ENV]:
        del env[name]
    return env


def credential_store_name() -> str:
    """The credential store's name as this operating system calls it."""
    if sys.platform == "win32":
        return "Windows Credential Manager"
    if sys.platform == "darwin":
        return "the macOS Keychain"
    return "your system keyring"


class SecretStore:
    """A thin, failure-tolerant wrapper around ``keyring``."""

    def __init__(self, service: str | None = None):
        self.service = service or os.environ.get("LUMI_KEYCHAIN_SERVICE") or DEFAULT_SERVICE
        self._lock = threading.Lock()
        self._keyring = None
        self.available = False
        self.reason = ""
        if str(os.environ.get("LUMI_KEYCHAIN", "on")).strip().lower() in {"off", "0", "false", "no"}:
            self.reason = "The credential store is turned off (LUMI_KEYCHAIN=off)."
            return
        try:
            import keyring
            from keyring.backends import fail, null

            backend = keyring.get_keyring()
            if isinstance(backend, (fail.Keyring, null.Keyring)):
                self.reason = "No credential store is available on this system."
                return
            self._keyring = keyring
            self.available = True
        except Exception as exc:  # ImportError, or a backend that fails to load
            self.reason = f"The credential store could not be loaded ({type(exc).__name__})."

    def get(self, name: str) -> str:
        if not self.available:
            return ""
        try:
            with self._lock:
                return self._keyring.get_password(self.service, name) or ""
        except Exception:
            logger.warning("Could not read %s from the credential store", name, exc_info=True)
            return ""

    def set(self, name: str, value: str) -> bool:
        if not self.available:
            return False
        try:
            with self._lock:
                self._keyring.set_password(self.service, name, value)
            return True
        except Exception:
            logger.warning("Could not save %s to the credential store", name, exc_info=True)
            return False

    def delete(self, name: str) -> None:
        if not self.available:
            return
        try:
            with self._lock:
                self._keyring.delete_password(self.service, name)
        except Exception:
            pass  # Already absent, or the store refused; nothing more to do.
