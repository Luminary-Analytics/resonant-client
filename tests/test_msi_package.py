"""The MSI for device management (packaging/lumi.wxs) and how the app treats an MSI install.

WiX itself runs in CI (build-check.yml builds, installs, checks and removes
the package); these tests keep the package, the EXE installer, the policy
reader and the update settings in agreement.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from types import SimpleNamespace

import pytest

from lumi import policy, update_channels

ROOT = Path(__file__).resolve().parents[1]
WIX = "{http://wixtoolset.org/schemas/v4/wxs}"


@pytest.fixture(scope="module")
def package():
    return ET.parse(ROOT / "packaging" / "lumi.wxs").getroot().find(f"{WIX}Package")


def test_the_package_installs_per_machine_and_replaces_earlier_versions(package):
    assert package.get("Scope") == "perMachine"
    # Changing the upgrade code would install side by side instead of upgrading.
    assert package.get("UpgradeCode") == "{144A302C-54FB-441F-B742-7B597A6A3F4A}"
    assert package.find(f"{WIX}MajorUpgrade").get("AllowSameVersionUpgrades") == "yes"
    folder = package.find(f".//{WIX}StandardDirectory[@Id='ProgramFiles64Folder']/{WIX}Directory")
    assert (folder.get("Id"), folder.get("Name")) == ("INSTALLFOLDER", "Lumi")
    shortcut = package.find(f".//{WIX}Shortcut")
    assert shortcut.get("Target") == "[INSTALLFOLDER]lumi.exe" and shortcut.get("Arguments") == "gui"


def test_it_refuses_to_install_over_the_exe_installer(package):
    iss = (ROOT / "packaging" / "installer.iss").read_text(encoding="utf-8")
    app_id = re.search(r"^AppId=\{(\{[0-9A-F-]+\})", iss, re.M).group(1)
    search = package.find(f".//{WIX}RegistrySearch")
    assert search.get("Key").endswith(f"\\Uninstall\\{app_id}_is1")
    assert package.find(f"{WIX}Launch").get("Condition") == "Installed OR NOT EXEINSTALL"
    # And the EXE installer refuses to install over the MSI's copy.
    assert "{commonpf64}\\Lumi\\lumi-install.json" in iss


def test_policyfile_writes_the_value_the_policy_reader_uses(package):
    component = package.find(f".//{WIX}Component[@Id='PolicyFileValue']")
    assert component.get("Condition") == "POLICYFILE"
    value = component.find(f"{WIX}RegistryValue")
    assert (value.get("Root"), value.get("Key"), value.get("Name"), value.get("Value")) == (
        "HKLM", policy.REGISTRY_KEY, "PolicyFile", "[POLICYFILE]")


def test_the_remembered_policyfile_comes_back_only_while_the_value_is_still_in_place(package):
    """The re-review of PR #104: an upgrade restored the remembered POLICYFILE even after an administrator
    deleted the PolicyFile value, so it couldn't be cleared."""
    searches = {search.get("Id"): search for search in package.iter(f"{WIX}RegistrySearch")}
    live = searches["LivePolicyFile"]
    assert (live.get("Root"), live.get("Key"), live.get("Name"), live.get("Bitness")) == (
        "HKLM", policy.REGISTRY_KEY, "PolicyFile", "always64")
    remembered = searches["RememberedPolicyFile"]
    assert remembered.get("Key") == r"SOFTWARE\Luminary Analytics\Lumi\Msi" and remembered.get("Name") == "PolicyFile"
    [restore] = [item for item in package.iter(f"{WIX}SetProperty") if item.get("Id") == "POLICYFILE"]
    assert restore.get("Condition") == (
        "NOT POLICYFILE AND REMEMBERED_POLICYFILE AND REMEMBERED_POLICYFILE = LIVE_POLICYFILE")
    assert (restore.get("Value"), restore.get("After"), restore.get("Sequence")) == (
        "[REMEMBERED_POLICYFILE]", "AppSearch", "both")
    for name in ("POLICYFILE", "REMEMBERED_POLICYFILE", "LIVE_POLICYFILE"):
        assert package.find(f"{WIX}Property[@Id='{name}']").get("Secure") == "yes", name
    # CI deletes the value, upgrades, and checks it stays deleted and the copy goes.
    check = (ROOT / ".github" / "workflows" / "build-check.yml").read_text(encoding="utf-8")
    assert "Remove-ItemProperty -Path $policyKey -Name PolicyFile" in check
    assert "The upgrade brought back the PolicyFile value an administrator deleted" in check
    assert "reg delete" in (ROOT / "docs" / "deploy-windows.md").read_text(encoding="utf-8")
    # The installers name Luminary's legal name as the publisher.
    assert package.get("Manufacturer") == "Luminary Analytics, LLC"
    iss = (ROOT / "packaging" / "installer.iss").read_text(encoding="utf-8")
    assert '#define AppPublisher   "Luminary Analytics, LLC"' in iss


def test_it_creates_the_machine_policy_folder_locked(package):
    # Any user may create folders under ProgramData, and one made there inherits every user's
    # right to add files, so the package makes %ProgramData%\Lumi itself: owned by Administrators,
    # SYSTEM and Administrators full control, Users read and execute, nothing inherited.
    folder = package.find(f".//{WIX}StandardDirectory[@Id='CommonAppDataFolder']/{WIX}Directory")
    assert folder.get("Name") == "Lumi"  # C:\ProgramData\Lumi, beside lumi/policy.machine_policy_file
    component = folder.find(f"{WIX}Component[@Id='MachinePolicyFolder']")
    sddl = component.find(f"{WIX}CreateFolder/{WIX}PermissionEx").get("Sddl")
    assert sddl == "O:BAD:PAI(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)(A;OICI;0x1200a9;;;BU)"
    assert component.find(f"{WIX}RegistryValue").get("KeyPath") == "yes"
    assert package.find(f".//{WIX}Feature/{WIX}ComponentRef[@Id='MachinePolicyFolder']") is not None
    if sys.platform == "win32":
        # The folder it makes is one Lumi trusts (lumi/admin_files.py).
        from lumi import admin_files

        assert admin_files.descriptor_problem(admin_files.descriptor_from_sddl(sddl), "folder") == ""


def test_the_marker_the_build_writes_turns_updates_off(package, tmp_path):
    marker = package.find(f".//{WIX}Component[@Id='InstallMarker']/{WIX}File")
    assert marker.get("Source").endswith("\\lumi-install.json")
    script = (ROOT / "packaging" / "build_msi.ps1").read_text(encoding="utf-8")
    content = re.search(r"lumi-install\.json\"\) -Value '([^']+)'", script).group(1)
    (tmp_path / "lumi-install.json").write_text(content, encoding="ascii")
    assert update_channels.installed_by(str(tmp_path / "lumi.exe")) == "msi"
    assert update_channels.installed_by(str(tmp_path / "elsewhere" / "lumi.exe")) == ""


def test_an_msi_install_never_updates_itself(tmp_path):
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"updates": {"mode": "automatic", "channel": "beta"}}), encoding="utf-8")
    automatic = policy.parse({"schema": policy.SCHEMA, "organization": "Example Corp",
                              "settings": {"updates.mode": "automatic"}}, source="test")
    prefs = update_channels.read(settings, SimpleNamespace(policy=automatic, error=""), installer="msi")
    assert (prefs.mode, prefs.installed_by, prefs.channel) == ("off", "msi", "beta")
    # Running from source never counts as an MSI install.
    assert update_channels.installed_by() == ""


def test_check_for_updates_explains_an_msi_install():
    from lumi.gui.ws_commands import _update_check_message

    message = _update_check_message({"mode": "off", "installed_by": "msi"}, False)
    assert "MSI package" in message and "device management" in message


def test_lumi_updates_prints_the_settings_in_effect(tmp_path):
    env = {**os.environ, "LUMI_STATE_HOME": str(tmp_path), "LUMI_KEYCHAIN": "off", "PYTHONIOENCODING": "utf-8"}
    env.pop("LUMI_POLICY_FILE", None)
    (tmp_path / "settings.json").write_text(json.dumps({"updates": {"mode": "manual", "pin": "0.20"}}),
                                            encoding="utf-8")
    result = subprocess.run([sys.executable, "-m", "lumi", "updates"], cwd=ROOT, env=env,
                            capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr
    status = json.loads(result.stdout)
    assert (status["mode"], status["pin"], status["installed_by"]) == ("manual", "0.20", "")
    assert status["feed"].endswith("/appcast-0.20.xml") and status["version"]
    assert subprocess.run([sys.executable, "-m", "lumi", "updates", "extra"], cwd=ROOT, env=env,
                          capture_output=True, text=True, timeout=120).returncode == 2
