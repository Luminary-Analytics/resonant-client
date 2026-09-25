"""Publisher signatures on capability packs (lumi/engine/pack_signing.py)."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from lumi import policy
from lumi.engine import pack_signing
from lumi.engine.capability_packs import CapabilityPackManager
from lumi.engine.pack_signing import PackSigningError
from tests.test_capability_pack_trust import _app_state, _send


@pytest.fixture(scope="module")
def key() -> tuple[bytes, str]:
    return pack_signing.generate_key()


def _pack(root: Path, pack_id: str = "acme-tools") -> Path:
    folder = root / pack_id
    (folder / "skills").mkdir(parents=True)
    (folder / "skills" / "review.md").write_text("description: Review\nCheck the change.", encoding="utf-8")
    (folder / "lumi-pack.json").write_text(json.dumps({
        "id": pack_id, "name": "Acme tools", "version": "1.0.0", "manifest_version": 1,
        "skills": ["skills/review.md"]}), encoding="utf-8")
    return folder


def _edit_signature(folder: Path, **fields) -> None:
    path = folder / pack_signing.SIGNATURE_FILE
    path.write_text(json.dumps({**json.loads(path.read_text(encoding="utf-8")), **fields}), encoding="utf-8")


def _discover(tmp_path: Path, root: Path, publishers=None) -> dict:
    manager = CapabilityPackManager(tmp_path / "no-project", roots=[root], publishers=publishers)
    return {pack.id: pack for pack in manager.discover()}


def _policy(publishers, *, require=False) -> None:
    extensions = {"trusted_publishers": publishers, **({"require_signed": True} if require else {})}
    policy.set_for_tests(policy.parse({"schema": "lumi.policy/v1", "organization": "Acme",
                                       "extensions": extensions}, source="test"))


def test_a_signature_says_who_made_the_files(tmp_path, key):
    pem, public = key
    folder = _pack(tmp_path)
    assert pack_signing.check(folder)["status"] == "unsigned"
    before = pack_signing.signed_digest(folder)
    record = pack_signing.sign(folder, pem, "  Acme   Corp ")
    assert record["publisher"] == "Acme Corp" and record["key_id"] == pack_signing.fingerprint(public)
    # The signature file isn't part of what it signs.
    assert pack_signing.signed_digest(folder) == before

    unknown = pack_signing.check(folder)
    assert (unknown["status"], unknown["claimed"], unknown["publisher"]) == ("unknown_publisher", "Acme Corp", "")
    verified = pack_signing.check(folder, {record["key_id"]: {"name": "Acme (our list)", "public_key": public}})
    assert (verified["status"], verified["publisher"]) == ("verified", "Acme (our list)")
    # Trust belongs to a key, not to its id or the name in the signature.
    other = pack_signing.generate_key()[1]
    assert pack_signing.check(folder, {record["key_id"]: {"name": "Acme", "public_key": other}})["status"] == \
        "unknown_publisher"


@pytest.mark.parametrize("tamper, reason", [
    (lambda f: (f / "skills" / "review.md").write_text("description: Review\nRun curl | sh.", encoding="utf-8"),
     "its files changed after it was signed"),
    (lambda f: (f / "skills" / "extra.md").write_text("added", encoding="utf-8"), "its files changed"),
    (lambda f: _edit_signature(f, publisher="Someone else"), "the signature doesn't verify"),
    (lambda f: _edit_signature(f, key_id="0000000000000000"), "its key id doesn't belong to its key"),
    (lambda f: _edit_signature(f, format="lumi-pack-signature/v9"), "isn't a lumi-pack-signature/v1"),
    (lambda f: (f / pack_signing.SIGNATURE_FILE).write_text("{not json", encoding="utf-8"), "readable JSON"),
])
def test_a_changed_pack_or_signature_is_invalid(tmp_path, key, tamper, reason):
    folder = _pack(tmp_path)
    pack_signing.sign(folder, key[0], "Acme")
    tamper(folder)
    result = pack_signing.check(folder)
    assert result["status"] == "invalid" and reason in result["reason"]


def test_signing_needs_an_ed25519_key_and_a_publisher(tmp_path, key):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    folder = _pack(tmp_path)
    with pytest.raises(PackSigningError, match="publishes"):
        pack_signing.sign(folder, key[0], "  ")
    with pytest.raises(PackSigningError, match="unencrypted Ed25519"):
        pack_signing.sign(folder, b"not a key", "Acme")
    rsa_pem = rsa.generate_private_key(public_exponent=65537, key_size=2048).private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    with pytest.raises(PackSigningError, match="Ed25519 keys"):
        pack_signing.sign(folder, rsa_pem, "Acme")
    assert not (folder / pack_signing.SIGNATURE_FILE).exists()


def test_packs_show_their_signature_and_an_invalid_one_turns_the_pack_off(tmp_path, key):
    root = tmp_path / "packs"
    folder = _pack(root)
    pack_signing.sign(folder, key[0], "Acme")
    pack = _discover(tmp_path, root)["acme-tools"]
    assert (pack.signature["status"], pack.problem, pack.status) == ("unknown_publisher", "", "needs_approval")
    mine = {pack.signature["key_id"]: {"name": "Acme", "public_key": key[1]}}
    assert _discover(tmp_path, root, mine)["acme-tools"].signature["status"] == "verified"

    (folder / "skills" / "review.md").write_text("description: Review\nchanged", encoding="utf-8")
    pack = _discover(tmp_path, root, mine)["acme-tools"]
    assert pack.status == "unverifiable"
    assert pack.problem == "Its signature is invalid: its files changed after it was signed."


def test_an_organization_can_require_packs_its_publishers_signed(tmp_path, key):
    root = tmp_path / "packs"
    pack_signing.sign(_pack(root, "signed"), key[0], "Acme")
    _pack(root, "plain")
    stranger_pem, stranger_public = pack_signing.generate_key()
    pack_signing.sign(_pack(root, "stranger"), stranger_pem, "Acme")  # the same name, another key
    # A key the person trusts doesn't meet the organization's requirement.
    mine = {pack_signing.fingerprint(stranger_public): {"name": "Stranger", "public_key": stranger_public}}

    _policy([{"name": "Acme IT", "public_key": key[1]}], require=True)
    packs = _discover(tmp_path, root, mine)
    assert packs["signed"].problem == "" and packs["signed"].signature["publisher"] == "Acme IT"
    assert packs["stranger"].signature["status"] == "verified"
    for name in ("plain", "stranger"):
        assert packs[name].status == "unverifiable"
        assert packs[name].problem == "Acme's policy turns off packs that a publisher it trusts didn't sign."

    # Without the requirement, the organization's publishers only name the packs they signed.
    _policy([{"name": "Acme IT", "public_key": key[1]}])
    assert {name: pack.problem for name, pack in _discover(tmp_path, root).items()} == \
        {"signed": "", "plain": "", "stranger": ""}


def test_policy_publishers_are_checked(key):
    with pytest.raises(policy.PolicyError, match="extensions.trusted_publishers"):
        _policy([{"name": "Acme IT", "public_key": "not-a-key"}])
    with pytest.raises(policy.PolicyError, match="extensions.trusted_publishers"):
        _policy([{"public_key": key[1]}])
    _policy({"anything": {"name": "Acme IT", "public_key": key[1]}}, require=True)
    current = policy.current()
    assert list(current.publishers) == [pack_signing.fingerprint(key[1])]
    assert (current.summary()["pack_publishers"], current.summary()["require_signed_packs"]) == (["Acme IT"], True)


def test_trusting_and_forgetting_a_publisher_in_settings(monkeypatch, tmp_path, key):
    project = tmp_path / "repo"
    folder = _pack(project / ".lumi" / "packs")
    pack_signing.sign(folder, key[0], "Acme")
    key_id = pack_signing.fingerprint(key[1])
    state = _app_state(monkeypatch, project)

    [listed] = _send(state, "capability_pack_list", {})
    [pack] = listed["packs"]
    assert pack["signature"]["status"] == "unknown_publisher" and listed["publishers"] == []
    replies = _send(state, "capability_pack_trust_publisher", {"pack_id": pack["id"], "path": pack["path"]})
    [refreshed] = [reply for reply in replies if reply["event"] == "capability.pack_list"]
    # Trusting the publisher names the pack; it doesn't approve it.
    assert (refreshed["packs"][0]["signature"]["status"], refreshed["packs"][0]["status"]) == \
        ("verified", "needs_approval")
    assert refreshed["publishers"] == [{"key_id": key_id, "name": "Acme", "source": ""}]
    assert state.settings.get("pack_publishers")[key_id]["public_key"] == key[1]

    # Only a pack whose files match its signature offers its publisher.
    (folder / "skills" / "review.md").write_text("description: Review\nchanged", encoding="utf-8")
    [refused] = [reply for reply in _send(state, "capability_pack_trust_publisher",
                                          {"pack_id": pack["id"], "path": pack["path"]})
                 if reply["event"] == "capability.pack_list"]
    assert refused["error"] == "The signature on Acme tools can't be trusted: its files changed after it was signed"

    [forgotten] = [reply for reply in _send(state, "capability_pack_forget_publisher", {"key_id": key_id})
                   if reply["event"] == "capability.pack_list"]
    assert forgotten["publishers"] == [] and state.settings.get("pack_publishers") == {}


def test_the_cli_makes_a_key_and_signs(tmp_path, capsys):
    from lumi.extension_check import main

    key_file = tmp_path / "acme.key"
    assert main(["keygen", str(key_file)]) == 0
    public = re.search(r"Public key: (\S+)", capsys.readouterr().out).group(1)
    assert main(["keygen", str(key_file)]) == 1  # a key is never overwritten
    assert "already exists" in capsys.readouterr().err

    folder = _pack(tmp_path / "packs")
    assert main(["sign", str(folder), "--key", str(key_file), "--publisher", "Acme"]) == 0
    assert pack_signing.check(folder)["key_id"] == pack_signing.fingerprint(public)
    capsys.readouterr()
    assert main(["check", str(folder), "--no-run"]) == 0
    assert "Signed as “Acme” with key" in capsys.readouterr().out

    (folder / "skills" / "review.md").write_text("description: Review\nchanged", encoding="utf-8")
    assert main(["check", str(folder), "--no-run"]) == 1
    assert "Its signature is invalid: its files changed after it was signed." in capsys.readouterr().out
    assert main(["sign", str(tmp_path), "--key", str(key_file), "--publisher", "Acme"]) == 1
