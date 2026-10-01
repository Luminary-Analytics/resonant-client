"""The operator-only startup flag cannot silently become personal execution."""

import subprocess
import sys
from types import SimpleNamespace

import pytest

from lumi import __main__ as entry


def test_managed_startup_option_strips_only_fixed_absolute_config_path(tmp_path):
    path = str(tmp_path / "operator-private.json")
    assert entry._managed_startup_arguments(["sonn", "gui", "--browser", "--swarm-managed-config", path]) == (
        ["sonn", "gui", "--browser"], path)
    assert entry._managed_startup_arguments(["sonn", "gui", f"--swarm-managed-config={path}"])[1] == path
    assert entry._redacted_startup_arguments(["sonn", "gui", "--swarm-managed-config", path])[-1] == "<protected-managed-config>"
    assert path not in str(entry._redacted_startup_arguments(["sonn", f"--swarm-managed-config={path}"]))


@pytest.mark.parametrize("arguments", [
    ["sonn", "gui", "--swarm-managed-config"],
    ["sonn", "gui", "--swarm-managed-config", "relative.json"],
    ["sonn", "gui", "--swarm-managed-config="],
])
def test_incomplete_managed_config_is_rejected(arguments):
    with pytest.raises(ValueError):
        entry._managed_startup_arguments(arguments)


def test_duplicate_or_nongui_managed_config_cannot_start_unmanaged_fallback(tmp_path):
    path = str(tmp_path / "private.json")
    for arguments in (["sonn", "gui", "--swarm-managed-config", path, f"--swarm-managed-config={path}"],
                      ["sonn", "--swarm-managed-config", path]):
        with pytest.raises(ValueError):
            entry._managed_startup_arguments(arguments)


def test_actual_entrypoint_invalid_managed_file_stops_before_ui_and_hides_path(tmp_path):
    private = tmp_path / "sensitive-operator-missing.json"
    result = subprocess.run([sys.executable, "-m", "lumi", "gui", "--swarm-managed-config", str(private)],
                            capture_output=True, text=True, timeout=15, check=False)
    assert result.returncode == 2
    assert "Managed setup failed" in result.stderr
    assert str(private) not in result.stdout + result.stderr
    assert "GUI running" not in result.stdout


def test_valid_startup_injects_captured_helper_before_launch_without_network(tmp_path, monkeypatch):
    from lumi.engine.swarming import managed_desktop

    path = str(tmp_path / "operator.json")
    calls, helper = [], object()
    monkeypatch.setattr(managed_desktop, "load_configuration", lambda selected: calls.append(("load", selected)) or "captured")
    monkeypatch.setattr(managed_desktop, "ManagedDesktop", lambda config: calls.append(("helper", config)) or helper)
    monkeypatch.setitem(sys.modules, "lumi.gui.app", SimpleNamespace(configure_managed_startup=lambda value: calls.append(("inject", value))))
    monkeypatch.setitem(sys.modules, "lumi.gui.server", SimpleNamespace(main=lambda: calls.append(("gui", list(sys.argv)))))
    monkeypatch.setitem(sys.modules, "lumi.updater", SimpleNamespace(init_updater=lambda: calls.append(("updater",))))
    monkeypatch.setattr(sys, "argv", ["sonn", "gui", "--browser", "--swarm-managed-config", path])
    entry.main()
    assert calls == [("load", path), ("helper", "captured"), ("inject", helper), ("updater",), ("gui", ["sonn", "--browser"])]
