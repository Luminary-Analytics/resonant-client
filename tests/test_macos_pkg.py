"""The macOS installer package for device management, and configuration profiles for its policy.

Apple's pkgbuild and productbuild run in CI (build-macos.yml builds the PKG,
installs it and checks the installed copy with managed preferences); these
tests keep the package's pieces, the profile generator, the policy reader and
the update settings in agreement.
"""
from __future__ import annotations

import base64
import importlib.util
import json
import plistlib
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from lumi import policy, update_channels

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "packaging" / "policy" / "example-policy.json"
MACHINE_KEYS = policy.machine_keys  # the real one; the fixture below stubs it for everything else


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


macos_pkg = _load("macos_pkg", ROOT / "packaging" / "macos_pkg.py")
make_mobileconfig = _load("make_mobileconfig", ROOT / "packaging" / "policy" / "make_mobileconfig.py")


@pytest.fixture(autouse=True)
def no_machine_policy(monkeypatch):
    """Nothing reads this computer's policy; each test says where its policy comes from."""
    monkeypatch.setattr(policy, "_registry_policy", lambda: None)
    monkeypatch.setattr(policy, "_macos_managed_policy", lambda: None)
    monkeypatch.setattr(policy, "_machine_file_policy", lambda: None)
    monkeypatch.setattr(policy, "machine_keys", lambda: {})
    monkeypatch.delenv("LUMI_POLICY_FILE", raising=False)
    policy.set_for_tests(None)
    yield
    policy.set_for_tests(None)


def _bundle(tmp_path: Path) -> Path:
    app = tmp_path / "Applications" / "Lumi.app"
    (app / "Contents" / "MacOS").mkdir(parents=True)
    (app / "Contents" / "MacOS" / "lumi").write_bytes(b"")
    return app


def test_the_package_marks_its_copy_and_that_copy_leaves_updates_to_the_mdm(tmp_path):
    app = _bundle(tmp_path)
    executable = str(app / "Contents" / "MacOS" / "lumi")
    assert update_channels.installed_by(executable) == ""  # dragged from the DMG
    marker = macos_pkg.write_marker(app)
    assert marker == app / "Contents" / "Resources" / "lumi-install.json"
    assert update_channels.installed_by(executable) == "pkg"

    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"updates": {"mode": "automatic", "channel": "beta"}}), encoding="utf-8")
    prefs = update_channels.read(settings, SimpleNamespace(policy=None, error=""), installer="pkg")
    assert (prefs.mode, prefs.installed_by, prefs.channel) == ("off", "pkg", "beta")
    with pytest.raises(SystemExit, match="isn't an app bundle"):
        macos_pkg.write_marker(tmp_path / "Nothing.app")


def test_check_for_updates_explains_a_pkg_install():
    from lumi.gui.ws_commands import _update_check_message

    message = _update_check_message({"mode": "off", "installed_by": "pkg"}, False)
    assert message == ("This copy was installed from the macOS installer package, so your organization's "
                       "device management updates it.")


def test_updates_replace_the_app_in_place():
    analyzed = [{"RootRelativeBundlePath": "Applications/Lumi.app", "BundleIsRelocatable": True,
                 "BundleHasStrictIdentifier": False, "BundleOverwriteAction": "update"}]
    [pinned] = macos_pkg.pin_components(analyzed)
    assert pinned["RootRelativeBundlePath"] == "Applications/Lumi.app"
    assert (pinned["BundleIsRelocatable"], pinned["BundleHasStrictIdentifier"], pinned["BundleIsVersionChecked"],
            pinned["BundleOverwriteAction"]) == (False, True, True, "upgrade")
    assert analyzed[0]["BundleIsRelocatable"] is True  # the input isn't changed
    with pytest.raises(SystemExit, match="no bundle"):
        macos_pkg.pin_components([])


def test_the_distribution_installs_system_wide_on_the_apps_minimum_macos():
    spec = (ROOT / "packaging" / "lumi.spec").read_text(encoding="utf-8")
    assert re.search(r'bundle_identifier="([^"]+)"', spec).group(1) == macos_pkg.IDENTIFIER
    minimum = re.search(r'"LSMinimumSystemVersion":\s*"([0-9.]+)"', spec).group(1)

    root = ET.fromstring(macos_pkg.distribution_xml("0.20.1", arch="arm64"))
    assert root.tag == "installer-gui-script" and root.findtext("title") == "Lumi"
    domains = root.find("domains").attrib
    assert domains == {"enable_anywhere": "false", "enable_currentUserHome": "false", "enable_localSystem": "true"}
    options = root.find("options").attrib
    assert (options["customize"], options["hostArchitectures"], options["require-scripts"]) == ("never", "arm64",
                                                                                               "false")
    assert root.find("volume-check/allowed-os-versions/os-version").get("min") == minimum
    [reference] = [ref for ref in root.findall("pkg-ref") if ref.text]
    assert (reference.get("id"), reference.get("version"), reference.text) == (
        macos_pkg.IDENTIFIER, "0.20.1", macos_pkg.COMPONENT)
    assert root.find(f"choice[@id='{macos_pkg.IDENTIFIER}']/pkg-ref").get("id") == macos_pkg.IDENTIFIER
    with pytest.raises(SystemExit, match="architecture"):
        macos_pkg.distribution_xml("0.20.1", arch="ppc")
    with pytest.raises(SystemExit, match="Not a version"):
        macos_pkg.distribution_xml("0.20.1\"/><evil", arch="arm64")


def test_the_command_line_writes_the_pieces(tmp_path):
    app = _bundle(tmp_path)
    assert macos_pkg.main(["marker", str(app)]) == 0
    components = tmp_path / "component.plist"
    components.write_bytes(plistlib.dumps([{"RootRelativeBundlePath": "Applications/Lumi.app",
                                            "BundleIsRelocatable": True}]))
    assert macos_pkg.main(["component", str(components)]) == 0
    assert plistlib.loads(components.read_bytes())[0]["BundleIsRelocatable"] is False
    out = tmp_path / "distribution.xml"
    assert macos_pkg.main(["distribution", "--version", "0.20.1", "--arch", "x86_64", "--out", str(out)]) == 0
    assert ET.parse(out).getroot().find("options").get("hostArchitectures") == "x86_64"


def test_a_generated_profile_carries_the_policy_the_app_reads(tmp_path):
    out = tmp_path / "lumi-policy.mobileconfig"
    assert make_mobileconfig.main([str(EXAMPLE), "--out", str(out)]) == 0
    profile = plistlib.loads(out.read_bytes())
    assert (profile["PayloadType"], profile["PayloadScope"], profile["PayloadOrganization"]) == (
        "Configuration", "System", "Example Corp")
    [payload] = profile["PayloadContent"]
    assert payload["PayloadType"] == policy.MAC_DOMAIN
    document = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    assert json.loads(payload["Policy"]) == document

    # The same policy gives the same profile; a changed one, new UUIDs under the same identifier.
    again = tmp_path / "again.mobileconfig"
    make_mobileconfig.main([str(EXAMPLE), "--out", str(again)])
    assert again.read_bytes() == out.read_bytes()
    changed = make_mobileconfig.profile({**document, "organization": "Acme"}, identifier=profile["PayloadIdentifier"],
                                        organization="Acme")
    assert changed["PayloadIdentifier"] == profile["PayloadIdentifier"]
    assert changed["PayloadUUID"] != profile["PayloadUUID"]

    # --plist: what Jamf's Custom Settings and an Intune preference file take, and what macOS
    # puts in /Library/Managed Preferences.
    prefs = tmp_path / f"{policy.MAC_DOMAIN}.plist"
    assert make_mobileconfig.main([str(EXAMPLE), "--plist", "--out", str(prefs)]) == 0
    text, source = policy.managed_preferences_policy(prefs)
    assert source == f"configuration profile ({policy.MAC_DOMAIN})"
    assert policy.parse(json.loads(text), source=source).organization == "Example Corp"


def test_the_generator_refuses_a_policy_the_app_would_refuse(tmp_path, capsys):
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"schema": "lumi.policy/v1", "organization": "Acme",
                               "settings": {"updates.mode": "sometimes"}}), encoding="utf-8")
    out = tmp_path / "out.mobileconfig"
    assert make_mobileconfig.main([str(bad), "--out", str(out)]) == 1
    assert "Lumi would refuse this policy" in capsys.readouterr().err and not out.exists()
    bad.write_text("[1, 2]", encoding="utf-8")
    assert make_mobileconfig.main([str(bad), "--out", str(out)]) == 1
    # Without keys, a signed policy is checked for structure; each Mac verifies the signature.
    signed = tmp_path / "signed.json"
    signed.write_text(json.dumps({"policy": json.loads(EXAMPLE.read_text(encoding="utf-8")), "key_id": "acme-1",
                                  "signature": "c2lnbmF0dXJl"}), encoding="utf-8")
    assert make_mobileconfig.main([str(signed), "--out", str(out)]) == 0
    assert json.loads(plistlib.loads(out.read_bytes())["PayloadContent"][0]["Policy"])["key_id"] == "acme-1"
    # With them, the signature is checked here too, and a bad one refused.
    private = Ed25519PrivateKey.generate()
    public = base64.b64encode(private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)).decode()
    keys = tmp_path / "keys.json"
    keys.write_text(json.dumps({"acme-1": public}), encoding="utf-8")
    assert make_mobileconfig.main([str(signed), "--keys", str(keys), "--out", str(out)]) == 1
    assert "signature doesn't match" in capsys.readouterr().err
    keys.write_text("[]", encoding="utf-8")
    assert make_mobileconfig.main([str(signed), "--keys", str(keys), "--out", str(out)]) == 1
    assert "keys file must be a JSON object" in capsys.readouterr().err


def test_a_signed_policy_and_its_keys_travel_in_one_profile(tmp_path, monkeypatch):
    private = Ed25519PrivateKey.generate()
    public = base64.b64encode(private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)).decode()
    document = {"schema": policy.SCHEMA, "organization": "Acme", "settings": {"updates.channel": "beta"}}
    signature = base64.b64encode(private.sign(policy.canonical(document))).decode()
    signed = tmp_path / "signed.json"
    signed.write_text(json.dumps({"policy": document, "signature": signature, "key_id": "acme-1"}), encoding="utf-8")
    keys = tmp_path / "keys.json"
    keys.write_text(json.dumps({"acme-1": public}), encoding="utf-8")
    prefs = tmp_path / f"{policy.MAC_DOMAIN}.plist"
    assert make_mobileconfig.main([str(signed), "--keys", str(keys), "--plist", "--out", str(prefs)]) == 0
    assert plistlib.loads(prefs.read_bytes())["PolicyKeys"] == {"acme-1": public}

    # The Mac reads both from its managed preferences, and the signed policy applies.
    monkeypatch.setattr(policy, "MAC_MANAGED_PREFERENCES", tmp_path)
    monkeypatch.setattr(policy.sys, "platform", "darwin")
    monkeypatch.setattr(policy, "machine_keys", MACHINE_KEYS)
    monkeypatch.setattr(policy, "_macos_managed_policy", lambda: policy.managed_preferences_policy(prefs))
    assert MACHINE_KEYS() == {"acme-1": public}
    state = policy.load(force=True)
    assert (state.error, state.policy.organization, state.policy.settings) == ("", "Acme", {"updates.channel": "beta"})


def test_managed_preferences_that_cant_be_used_fail_closed(tmp_path, monkeypatch):
    prefs = tmp_path / f"{policy.MAC_DOMAIN}.plist"
    assert policy.managed_preferences_policy(prefs) is None  # no profile
    prefs.write_bytes(plistlib.dumps({"SomethingElse": 1}))
    assert policy.managed_preferences_policy(prefs) is None  # the domain, but no Lumi policy
    prefs.write_bytes(plistlib.dumps({"Policy": {"schema": policy.SCHEMA, "organization": "Acme"}}))
    text, _ = policy.managed_preferences_policy(prefs)
    assert json.loads(text)["organization"] == "Acme"  # a dictionary works too
    for value, message in (("  ", "an empty string"), (5, "int"), ([1], "list")):
        prefs.write_bytes(plistlib.dumps({"Policy": value}))
        with pytest.raises(ValueError, match=message):
            policy.managed_preferences_policy(prefs)
    prefs.write_bytes(b"not a property list")
    with pytest.raises(ValueError, match="couldn't be read"):
        policy.managed_preferences_policy(prefs)

    # Loaded like the Mac would: Lumi refuses model requests until it's fixed.
    monkeypatch.setattr(policy, "_macos_managed_policy", lambda: policy.managed_preferences_policy(prefs))
    state = policy.load(force=True)
    assert state.policy is None and "couldn't be read" in state.error
    assert policy.blocked_reason().endswith("Ask your administrator to fix it.")
