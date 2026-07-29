"""Retiring a default entry must actually reach existing installs."""

import json

from resonant_client.gui.settings import DEFAULTS, SettingsManager

STOCK_URL = "http://127.0.0.1:9239/mcp"


def _write(path, servers):
    path.write_text(json.dumps({"mcp_servers": servers}), encoding="utf-8")


def test_no_mcp_server_ships_enabled_by_default():
    """Browsing is native; nothing should be configured out of the box.

    A default server entry that nobody runs reports itself as an unavailable
    tool server on every launch.
    """
    assert DEFAULTS["mcp_servers"] == {}


def test_stock_browseros_entry_is_removed_from_existing_settings(tmp_path):
    """Dropping it from DEFAULTS is not enough.

    `_apply_defaults` only fills in missing keys — it never prunes — so every
    install created before v0.11.15 keeps its browseros entry forever and keeps
    reporting it as unavailable.
    """
    path = tmp_path / "settings.json"
    _write(path, {"browseros": {
        "transport": "http", "url": STOCK_URL, "enabled": True,
        "description": "Default browser MCP.",
    }})

    settings = SettingsManager(path)

    assert settings.get("mcp_servers") == {}
    # And the removal is persisted, not just in memory.
    assert json.loads(path.read_text(encoding="utf-8"))["mcp_servers"] == {}


def test_a_customised_browseros_entry_is_preserved(tmp_path):
    """A changed URL means the user set this up deliberately."""
    path = tmp_path / "settings.json"
    _write(path, {"browseros": {
        "transport": "http", "url": "http://10.0.0.55:9239/mcp", "enabled": True,
    }})

    settings = SettingsManager(path)

    servers = settings.get("mcp_servers")
    assert "browseros" in servers
    assert servers["browseros"]["url"] == "http://10.0.0.55:9239/mcp"


def test_other_mcp_servers_are_untouched(tmp_path):
    path = tmp_path / "settings.json"
    _write(path, {
        "browseros": {"transport": "http", "url": STOCK_URL, "enabled": True},
        "mine": {"transport": "http", "url": "http://localhost:1234/mcp", "enabled": True},
    })

    servers = SettingsManager(path).get("mcp_servers")

    assert "browseros" not in servers
    assert servers["mine"]["url"] == "http://localhost:1234/mcp"


def test_migration_is_idempotent(tmp_path):
    path = tmp_path / "settings.json"
    _write(path, {"browseros": {"transport": "http", "url": STOCK_URL, "enabled": True}})

    SettingsManager(path)
    assert SettingsManager(path).get("mcp_servers") == {}


def test_migration_survives_a_malformed_entry(tmp_path):
    """Hand-edited settings must not crash startup."""
    path = tmp_path / "settings.json"
    _write(path, {"browseros": "not-a-dict"})

    servers = SettingsManager(path).get("mcp_servers")

    assert servers["browseros"] == "not-a-dict"


def test_ollama_is_reprobed_before_being_declared_unreachable(monkeypatch, tmp_path):
    """A stale cached probe must not be reported as "Ollama is down".

    `available_backends` comes from one probe with a 2s connect timeout. Ollama
    commonly runs on another machine, so a single slow reply at startup poisons
    that cache for the whole session and the user is told Ollama is unreachable
    while it is plainly running.
    """
    import importlib
    from pathlib import Path

    project = tmp_path / "p"
    project.mkdir()
    (project / ".resonant").mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.chdir(project)
    import resonant_client.gui.app as app_module
    app_module = importlib.reload(app_module)

    probes = []

    def _detect(self):
        # Ollama is up; only the cached snapshot said otherwise.
        probes.append(1)
        self.available_backends = {"ollama": {"url": "http://10.0.0.133:11434",
                                              "models": ["deepseek-v4:cloud"]}}
        return self.available_backends

    monkeypatch.setattr(app_module.AppState, "detect_backends", _detect)
    state = app_module.AppState()
    # Simulate the poisoned cache: one slow probe at startup left this empty.
    state.available_backends = {}
    probes.clear()

    spec = state.build_backend_spec("ollama")

    assert probes, "build_backend_spec gave up without re-probing"
    assert spec.model == "deepseek-v4:cloud"


def test_unreachable_ollama_error_names_the_configured_url(monkeypatch, tmp_path):
    """Quoting the default URL misleads exactly when the URL is the problem."""
    import importlib
    from pathlib import Path

    project = tmp_path / "p2"
    project.mkdir()
    (project / ".resonant").mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.chdir(project)
    import resonant_client.gui.app as app_module
    app_module = importlib.reload(app_module)

    monkeypatch.setattr(app_module.AppState, "detect_backends", lambda self, force=False: {})
    state = app_module.AppState()
    state.available_backends = {}
    state.ollama_url = "http://10.0.0.133:11434"

    try:
        state.build_backend_spec("ollama")
    except ValueError as exc:
        assert "http://10.0.0.133:11434" in str(exc)
    else:
        raise AssertionError("expected a ValueError for unreachable Ollama")


def test_backend_probe_is_reused_within_the_freshness_window(monkeypatch, tmp_path):
    """Providers do not appear and disappear inside a few seconds.

    detect_backends runs on every WebSocket connect and every project switch.
    With unreachable hosts each call costs seconds of connect timeout — 6.4s
    measured with Ollama and EXO both down — and that time is a frozen UI. The
    reported symptom was "I'm clicking sessions and nothing is loading".
    """
    import importlib
    from pathlib import Path

    project = tmp_path / "p3"
    project.mkdir()
    (project / ".resonant").mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.chdir(project)
    import resonant_client.gui.app as app_module
    app_module = importlib.reload(app_module)

    probes = []
    real = app_module.AppState.detect_backends

    def _counting(self, force=False):
        result = real(self, force=force)
        return result

    monkeypatch.setattr(app_module.AppState, "refresh_network_defaults",
                        lambda self: probes.append("probe"))
    state = app_module.AppState()
    state.available_backends = {"kimi": {"models": ["kimi-k3"]}}
    state._last_backend_probe = __import__("time").time()

    probes.clear()
    state.detect_backends()
    assert probes == [], "a fresh probe was repeated on a routine call"

    # An explicit refresh must still go to the network.
    state.detect_backends(force=True)
    assert probes, "force=True must bypass the freshness window"


def test_a_stale_probe_is_refreshed(monkeypatch, tmp_path):
    """The window must not pin a stale answer forever.

    Starting Ollama and coming back has to be picked up without a restart.
    """
    import importlib
    import time as _time
    from pathlib import Path

    project = tmp_path / "p4"
    project.mkdir()
    (project / ".resonant").mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.chdir(project)
    import resonant_client.gui.app as app_module
    app_module = importlib.reload(app_module)

    probes = []
    monkeypatch.setattr(app_module.AppState, "refresh_network_defaults",
                        lambda self: probes.append("probe"))
    state = app_module.AppState()
    state.available_backends = {"kimi": {"models": ["kimi-k3"]}}
    state._last_backend_probe = _time.time() - (state._PROBE_FRESH_SECONDS + 5)

    probes.clear()
    state.detect_backends()

    assert probes, "a stale probe should have been refreshed"
