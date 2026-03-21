"""
Persistent settings manager for Resonant Client.
Reads/writes ~/.resonant/settings.json with section-based access.
"""

import json
import logging
import os
import threading
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DEFAULTS = {
    "general": {
        "default_backend": "",
        "default_model": "",
        "default_permission_mode": "bypass",
        "theme": "dark",
    },
    "api_keys": {
        "anthropic": "",
        "openai": "",
    },
    "hooks": [],
    "mcp_servers": {},
    "keyboard_shortcuts": {},
    "cost_tracking": {
        "enabled": True,
        "budget_alert_usd": None,
    },
    "engram": {
        "enabled": False,
        "server_url": "",
    },
}


class SettingsManager:
    """Thread-safe settings manager with JSON persistence."""

    def __init__(self, path: str | Path | None = None):
        self._path = Path(path) if path else Path.home() / ".resonant" / "settings.json"
        self._lock = threading.Lock()
        self._data: dict = {}
        self._load()

    # ── Public API ───────────────────────────────────────────

    def get(self, section: str, key: str | None = None, default: Any = None) -> Any:
        """Get a value.  get('general', 'theme') or get('hooks')."""
        with self._lock:
            sect = self._data.get(section, DEFAULTS.get(section))
            if key is None:
                return sect
            if isinstance(sect, dict):
                return sect.get(key, default)
            return default

    def set(self, section: str, key: str | None, value: Any) -> None:
        """Set a value and persist.  set('general', 'theme', 'light') or set('hooks', None, [...])."""
        with self._lock:
            if key is None:
                self._data[section] = value
            else:
                if section not in self._data:
                    self._data[section] = {}
                self._data[section][key] = value
            self._save_locked()

    def get_all(self) -> dict:
        """Return the full settings dict (deep copy)."""
        with self._lock:
            return json.loads(json.dumps(self._data))

    def update_section(self, section: str, updates: dict) -> None:
        """Merge updates into a section."""
        with self._lock:
            if section not in self._data:
                self._data[section] = {}
            if isinstance(self._data[section], dict):
                self._data[section].update(updates)
            else:
                self._data[section] = updates
            self._save_locked()

    def get_masked(self) -> dict:
        """Return settings with API keys masked for frontend display."""
        data = self.get_all()
        if "api_keys" in data:
            for k, v in data["api_keys"].items():
                if v:
                    data["api_keys"][k] = v[:4] + "•" * max(0, len(v) - 8) + v[-4:] if len(v) > 8 else "••••"
                else:
                    data["api_keys"][k] = ""
        return data

    # ── Internal ─────────────────────────────────────────────

    def _load(self) -> None:
        """Load from disk, merging with defaults for any missing keys."""
        with self._lock:
            if self._path.exists():
                try:
                    self._data = json.loads(self._path.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, OSError) as e:
                    logger.warning(f"Failed to read settings: {e}")
                    self._data = {}
            else:
                self._data = {}
            self._apply_defaults()
            self._save_locked()

    def _apply_defaults(self) -> None:
        """Merge missing keys from DEFAULTS into current data."""
        for section, default_val in DEFAULTS.items():
            if section not in self._data:
                self._data[section] = (
                    dict(default_val) if isinstance(default_val, dict)
                    else list(default_val) if isinstance(default_val, list)
                    else default_val
                )
            elif isinstance(default_val, dict) and isinstance(self._data[section], dict):
                for k, v in default_val.items():
                    if k not in self._data[section]:
                        self._data[section][k] = v

    def _save_locked(self) -> None:
        """Write to disk (caller must hold lock)."""
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(
                json.dumps(self._data, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except OSError as e:
            logger.error(f"Failed to save settings: {e}")
