"""Machine policy comes only from places only administrators can write (lumi/admin_files.py, lumi/policy.py).

* Security descriptors built in the test, one per kind of access control
  entry, run through the same check real folders get.
* Real folders on Windows, changed with icacls: the half a person can do
  always runs; the half that needs an administrator runs only elevated (CI).
* The reported case end to end: a key file a person put in the machine
  folder and a policy they signed with it no longer replace Group Policy.
* A ``PolicyFile`` Group Policy names that can't be read or used fails closed.

The conftest counts files under pytest's temporary folders as an
administrator's; the tests here say what the check answers, or use the real one.
"""

from __future__ import annotations

import base64
import ctypes
import json
import os
import subprocess
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from lumi import admin_files, audit
from lumi import license as lumi_license
from lumi import policy as lumi_policy
from lumi.admin_files import Ace, Descriptor, Trust, descriptor_problem

ROOT = Path(__file__).resolve().parents[1]
BASE = {"schema": lumi_policy.SCHEMA, "organization": "Acme"}
windows = pytest.mark.skipif(os.name != "nt", reason="Windows access control lists")
posix = pytest.mark.skipif(os.name == "nt", reason="POSIX owners and modes")

SY, BA, BU, AU, EVERYONE = "S-1-5-18", "S-1-5-32-544", "S-1-5-32-545", "S-1-5-11", "S-1-1-0"
INTERACTIVE, CREATOR_OWNER, OWNER_RIGHTS = "S-1-5-4", "S-1-3-0", "S-1-3-4"
PERSON = "S-1-5-21-1111111111-2222222222-3333333333-1001"
FULL, READ_EXECUTE, MODIFY = 0x1F01FF, 0x1200A9, 0x1301BF
OI, CI, IO, INHERITED = 0x1, 0x2, 0x8, 0x10
ALLOW, DENY = 0x0, 0x1


def _elevated() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except (AttributeError, OSError):
        return False


def _icacls(*args) -> None:
    tool = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "icacls.exe"
    result = subprocess.run([str(tool), *map(str, args)], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr


def _public(key: Ed25519PrivateKey) -> str:
    return base64.b64encode(key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)).decode()


def _signed(document: dict, key: Ed25519PrivateKey, key_id: str) -> dict:
    signature = base64.b64encode(key.sign(lumi_policy.canonical(document))).decode()
    return {"policy": document, "signature": signature, "key_id": key_id}


def _names(sid: str) -> str:
    return {BU: "BUILTIN\\Users", AU: "Authenticated Users", EVERYONE: "Everyone", INTERACTIVE: "INTERACTIVE",
            PERSON: "DESKTOP\\ana"}.get(sid, sid)


def _locked(*extra: Ace, owner: str = BA) -> Descriptor:
    """What the MSI and the documented icacls recipe leave: SYSTEM and Administrators full, Users read."""
    return Descriptor(owner, (Ace(ALLOW, OI | CI, FULL, SY), Ace(ALLOW, OI | CI, FULL, BA),
                              Ace(ALLOW, OI | CI, READ_EXECUTE, BU), *extra))


# ── Descriptors, one kind of entry at a time ────────────────────────────────


class TestDescriptors:
    def test_a_folder_only_administrators_change_is_safe(self):
        assert descriptor_problem(_locked(), "folder", _names) == ""
        assert descriptor_problem(_locked(), "file", _names) == ""
        # Read and execute for everyone changes nothing.
        assert descriptor_problem(_locked(Ace(ALLOW, 0, READ_EXECUTE, EVERYONE)), "file", _names) == ""

    def test_the_entry_every_folder_under_programdata_inherits_is_refused(self):
        # BUILTIN\Users:(CI)(WD,AD,WEA,WA): any user may add files to a folder an administrator made.
        inherited = _locked(Ace(ALLOW, CI | INHERITED, 0x116, BU))
        assert descriptor_problem(inherited, "folder", _names) == (
            "lets BUILTIN\\Users change it (add files, add folders and change attributes)")
        # ProgramData itself has it too, and only what would replace its folders counts there.
        assert descriptor_problem(inherited, "root", _names) == ""

    @pytest.mark.parametrize("mask, words", [
        (0x2, "add files"), (0x4, "add folders"), (0x100, "change attributes"), (0x10, "change attributes"),
        (0x10000, "delete"), (0x40, "delete what's in it"), (0x40000, "change permissions"),
        (0x80000, "take ownership"), (0x40000000, "write"), (0x10000000, "full control"),
        (0x02000000, "full control"), (MODIFY, "add files, add folders, change attributes and delete"),
        (FULL, "full control"),
    ], ids=["write data", "append", "write attributes", "write extended attributes", "delete",
            "delete child", "write dac", "write owner", "generic write", "generic all", "maximum allowed",
            "modify", "all file rights"])
    def test_each_right_that_changes_a_folder_is_refused(self, mask, words):
        assert descriptor_problem(_locked(Ace(ALLOW, 0, mask, PERSON)), "folder", _names) == (
            f"lets DESKTOP\\ana change it ({words})")

    @pytest.mark.parametrize("sid, name", [(BU, "BUILTIN\\Users"), (AU, "Authenticated Users"),
                                           (EVERYONE, "Everyone"), (INTERACTIVE, "INTERACTIVE"),
                                           (PERSON, "DESKTOP\\ana"),
                                           ("S-1-5-21-1-2-3-513", "S-1-5-21-1-2-3-513")])
    def test_anyone_but_an_administrator_with_write_is_refused(self, sid, name):
        problem = descriptor_problem(_locked(Ace(ALLOW, OI | CI, 0x2, sid)), "file", _names)
        assert problem == f"lets {name} change it (write)"

    @pytest.mark.parametrize("sid", [SY, BA, admin_files.TRUSTED_INSTALLER, "S-1-5-21-1-2-3-512",
                                     "S-1-5-21-1-2-3-519"], ids=["SYSTEM", "Administrators", "TrustedInstaller",
                                                                 "Domain Admins", "Enterprise Admins"])
    def test_administrators_may_write_and_own_it(self, sid):
        assert descriptor_problem(_locked(Ace(ALLOW, 0, FULL, sid), owner=sid), "folder", _names) == ""

    def test_entries_that_grant_nothing_here_dont_count(self):
        harmless = _locked(
            Ace(ALLOW, OI | CI | IO, FULL, BU),  # only for what is created later
            Ace(ALLOW, OI | CI | IO, 0x10000000, CREATOR_OWNER),
            Ace(ALLOW, 0, FULL, CREATOR_OWNER),  # a placeholder: no token holds it
            Ace(ALLOW, 0, FULL, OWNER_RIGHTS),  # the owner, an administrator
            Ace(DENY, 0, FULL, EVERYONE),  # denies only take rights away
            Ace(0x2, 0, FULL, EVERYONE),  # an audit entry, not a grant
        )
        assert descriptor_problem(harmless, "folder", _names) == ""

    def test_a_deny_entry_doesnt_excuse_an_allow_entry(self):
        both = _locked(Ace(DENY, 0, 0x2, BU), Ace(ALLOW, 0, 0x2, BU))
        assert descriptor_problem(both, "folder", _names) == "lets BUILTIN\\Users change it (add files)"

    @pytest.mark.parametrize("kind", [0x9, 0x5, 0xB, 0x4], ids=["callback", "object", "callback object", "compound"])
    def test_other_allow_entries_count_too(self, kind):
        assert descriptor_problem(_locked(Ace(kind, 0, 0x2, BU)), "folder", _names) == (
            "lets BUILTIN\\Users change it (add files)")
        unreadable = descriptor_problem(_locked(Ace(kind, 0, 0x2, None)), "folder", _names)
        assert unreadable == "has a permission entry Lumi can't read that allows changes (add files)"

    def test_owners_other_than_administrators_are_refused(self):
        assert descriptor_problem(_locked(owner=PERSON), "file", _names) == (
            "is owned by DESKTOP\\ana, not by Administrators, SYSTEM or TrustedInstaller")
        assert descriptor_problem(_locked(owner=BU), "root", _names).startswith("is owned by BUILTIN\\Users")
        assert descriptor_problem(Descriptor(None, ()), "file", _names) == "has no owner"
        # Domain Users (513) own nothing an administrator must.
        assert "is owned by" in descriptor_problem(_locked(owner="S-1-5-21-1-2-3-513"), "file", _names)

    def test_no_access_control_list_lets_everyone_change_it(self):
        assert descriptor_problem(Descriptor(BA, None), "file", _names) == (
            "has no access control list, so everyone can change it")
        # An empty list lets nobody but the owner, an administrator, in.
        assert descriptor_problem(Descriptor(BA, ()), "file", _names) == ""

    def test_roots_count_only_what_replaces_their_folders(self):
        # C:\ as Windows ships it: Authenticated Users may add folders, and Modify for what they create.
        drive = Descriptor(admin_files.TRUSTED_INSTALLER, (
            Ace(ALLOW, OI | CI, FULL, BA), Ace(ALLOW, OI | CI, FULL, SY), Ace(ALLOW, OI | CI, READ_EXECUTE, BU),
            Ace(ALLOW, OI | CI | IO, 0xE0010000, AU), Ace(ALLOW, 0, 0x4, AU)))
        assert descriptor_problem(drive, "anchor", _names) == ""
        # A data drive: Modify for Authenticated Users on the root itself, which can't be renamed.
        data = Descriptor(BA, (Ace(ALLOW, 0, FULL, BA), Ace(ALLOW, 0, FULL, SY), Ace(ALLOW, 0, MODIFY, AU)))
        assert descriptor_problem(data, "anchor", _names) == ""
        # Deleting what's in it, changing its permissions or owner, and renaming ProgramData do count.
        assert descriptor_problem(_locked(Ace(ALLOW, 0, 0x40, AU)), "anchor", _names) == (
            "lets Authenticated Users change it (delete what's in it)")
        assert "change permissions" in descriptor_problem(_locked(Ace(ALLOW, 0, 0x40000, BU)), "anchor", _names)
        assert descriptor_problem(_locked(Ace(ALLOW, 0, 0x10000, BU)), "root", _names) == (
            "lets BUILTIN\\Users change it (delete)")
        assert descriptor_problem(_locked(Ace(ALLOW, 0, 0x10000, BU)), "anchor", _names) == ""

    def test_the_chain_stops_at_the_protected_root(self, tmp_path):
        root = tmp_path / "ProgramData"
        item = root / "Lumi" / "policy.json"
        levels = [(path, level) for path, level in admin_files.chain(item, root)]
        assert levels == [(item, "file"), (root / "Lumi", "folder"), (root, "root")]
        # Without a root, or one the file isn't under, the walk goes to the drive's root.
        whole = admin_files.chain(item, tmp_path / "elsewhere")
        assert whole[-1] == (Path(item.anchor), "anchor") and len(whole) == len(item.parents) + 1


@windows
class TestWindowsDescriptorsFromSddl:
    """Binary security descriptors from SDDL, read by the same code as real files."""

    def test_the_msi_folder_is_safe(self):
        sddl = _msi_sddl()
        descriptor = admin_files.descriptor_from_sddl(sddl)
        assert descriptor.owner == BA
        assert [(ace.type, ace.flags, ace.mask, ace.sid) for ace in descriptor.dacl] == [
            (ALLOW, OI | CI, FULL, SY), (ALLOW, OI | CI, FULL, BA), (ALLOW, OI | CI, READ_EXECUTE, BU)]
        assert descriptor_problem(descriptor, "folder") == ""

    def test_programdata_as_windows_ships_it(self):
        sddl = "O:SYD:PAI(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)(A;OICIIO;GA;;;CO)(A;OICI;0x1200a9;;;BU)(A;CI;0x116;;;BU)"
        descriptor = admin_files.descriptor_from_sddl(sddl)
        assert descriptor_problem(descriptor, "root") == ""
        assert descriptor_problem(descriptor, "folder") == (
            "lets BUILTIN\\Users change it (add files, add folders and change attributes)")

    def test_object_and_callback_entries_are_read(self):
        # An object entry: its SID follows flags and a GUID.
        guid = "bf967a86-0de6-11d0-a285-00aa003049e2"
        descriptor = admin_files.descriptor_from_sddl(f"O:BAD:(OA;;GW;{guid};;BU)")
        assert [(ace.type, ace.sid) for ace in descriptor.dacl] == [(0x5, BU)]
        assert descriptor_problem(descriptor, "file") == "lets BUILTIN\\Users change it (write)"
        # A conditional (callback) entry still grants what it names.
        conditional = admin_files.descriptor_from_sddl('O:BAD:(XA;;FA;;;BU;(@User.Title == "x"))')
        assert conditional.dacl[0].type == 0x9 and conditional.dacl[0].sid == BU
        assert descriptor_problem(conditional, "file").startswith("lets BUILTIN\\Users change it (full control")

    def test_no_access_control_list(self):
        assert admin_files.descriptor_from_sddl("O:BAD:NO_ACCESS_CONTROL").dacl is None


def _msi_sddl() -> str:
    import xml.etree.ElementTree as ET

    wix = "{http://wixtoolset.org/schemas/v4/wxs}"
    package = ET.parse(ROOT / "packaging" / "lumi.wxs").getroot()
    return package.find(f".//{wix}Component[@Id='MachinePolicyFolder']/{wix}CreateFolder/{wix}PermissionEx").get("Sddl")


# ── POSIX owners and modes ──────────────────────────────────────────────────


def _stat(uid: int, mode: int, links: int = 1) -> os.stat_result:
    return os.stat_result((mode, 0, 0, links, uid, 0, 0, 0, 0, 0))


class TestPosixModes:
    @pytest.mark.parametrize("uid, mode, problem", [
        (0, 0o100644, ""),
        (0, 0o100444, ""),
        (0, 0o100664, "lets its group change it (mode 664)"),
        (0, 0o100646, "lets others change it (mode 646)"),
        (0, 0o100666, "lets its group and others change it (mode 666)"),
        (0, 0o040775, "lets its group change it (mode 775)"),
    ])
    def test_files_and_folders_only_root_changes(self, uid, mode, problem):
        assert admin_files.posix_problem(_stat(uid, mode), "file") == problem

    def test_files_root_doesnt_own_are_refused(self):
        assert admin_files.posix_problem(_stat(501, 0o100644), "file").startswith("is owned by ")
        assert "not by root" in admin_files.posix_problem(_stat(501, 0o100644), "file")

    def test_roots_may_be_writable_by_their_group_or_sticky(self):
        # /Library/Application Support is writable by the admin group; /tmp-like roots are sticky.
        assert admin_files.posix_problem(_stat(0, 0o040775), "root") == ""
        assert admin_files.posix_problem(_stat(0, 0o041777), "root") == ""
        assert admin_files.posix_problem(_stat(0, 0o040777), "root") == "lets others replace what's in it (mode 777)"
        assert admin_files.posix_problem(_stat(501, 0o040755), "anchor").startswith("is owned by")

    @posix
    def test_a_file_a_person_wrote_is_refused(self, tmp_path):
        folder = tmp_path / "lumi"
        folder.mkdir()
        (folder / "policy.json").write_text("{}", encoding="utf-8")
        trust = admin_files.real_check(folder / "policy.json", tmp_path)
        if os.geteuid() == 0:  # a root test runner owns what it writes
            os.chmod(folder / "policy.json", 0o666)
            trust = admin_files.real_check(folder / "policy.json", tmp_path)
            assert not trust.trusted and "(mode 666)" in trust.reason and trust.admin_owned
        else:
            assert not trust.trusted and "not by root" in trust.reason and not trust.admin_owned


# ── Real folders on Windows ─────────────────────────────────────────────────


@windows
class TestWindowsFolders:
    def test_a_folder_a_person_made_is_refused(self, tmp_path):
        root = tmp_path / "ProgramData"
        folder = root / "Lumi"
        folder.mkdir(parents=True)
        (folder / "policy.json").write_text("{}", encoding="utf-8")
        trust = admin_files.real_check(folder / "policy.json", root)
        assert not trust.trusted and trust.reason

    def test_the_programdata_entry_on_a_real_folder(self, tmp_path):
        folder = tmp_path / "Lumi"
        folder.mkdir()
        _icacls(folder, "/grant", "*S-1-5-32-545:(CI)(WD,AD)")
        descriptor = admin_files.read_descriptor(folder)
        [entry] = [ace for ace in descriptor.dacl if ace.sid == BU]
        assert entry.type == ALLOW and entry.flags & CI and not entry.flags & IO and entry.mask & 0x6 == 0x6
        # Were the folder an administrator's, that entry alone would still refuse it.
        only = Descriptor(BA, (entry,))
        assert descriptor_problem(only, "folder") == "lets BUILTIN\\Users change it (add files and add folders)"
        assert not admin_files.real_check(folder / "policy.json", tmp_path).trusted

    def test_a_junction_on_the_way_is_refused(self, tmp_path):
        import _winapi

        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        (elsewhere / "policy.json").write_text("{}", encoding="utf-8")
        root = tmp_path / "ProgramData"
        root.mkdir()
        _winapi.CreateJunction(str(elsewhere), str(root / "Lumi"))  # any user may make one here
        trust = admin_files.real_check(root / "Lumi" / "policy.json", root)
        assert not trust.trusted and "is a link" in trust.reason and not trust.admin_owned

    def test_a_missing_file_or_share_is_refused(self, tmp_path):
        assert "couldn't read" in admin_files.real_check(tmp_path / "missing.json").reason
        assert "device path" in admin_files.real_check("\\\\.\\pipe\\lumi-policy").reason

    def test_an_administrators_file_counts_as_placed_by_one_only_in_an_administrators_folder(self, tmp_path,
                                                                                             monkeypatch):
        # A person can move a file an administrator owns (say, one an elevated installer left in their
        # profile) into a folder they made; that must not make every user's Lumi fail closed.
        root = tmp_path / "ProgramData"
        (root / "Lumi").mkdir(parents=True)
        policy_file = root / "Lumi" / "policy.json"
        policy_file.write_text("{}", encoding="utf-8")
        owners = {policy_file: BA, root / "Lumi": PERSON, root: SY}
        monkeypatch.setattr(admin_files, "read_descriptor", lambda path: _locked(owner=owners[Path(path)]))
        trust = admin_files.real_check(policy_file, root)
        assert not trust.trusted and "is owned by" in trust.reason and not trust.admin_owned
        # An administrator's folder that lets everyone add files: an administrator put it there.
        owners[root / "Lumi"] = BA
        monkeypatch.setattr(admin_files, "read_descriptor", lambda path: _locked(
            Ace(ALLOW, CI, 0x6, BU), owner=owners[Path(path)]) if Path(path) == root / "Lumi"
            else _locked(owner=owners[Path(path)]))
        trust = admin_files.real_check(policy_file, root)
        assert not trust.trusted and trust.admin_owned and "add files and add folders" in trust.reason
        # A second name (a hard link) could be anywhere a person put one.
        os.link(policy_file, tmp_path / "another-name.json")
        assert not admin_files.real_check(policy_file, root).admin_owned

    @pytest.mark.skipif(not _elevated(), reason="needs an administrator (elevated) process, as in CI")
    def test_folders_only_administrators_change_are_trusted_until_they_arent(self, tmp_path, monkeypatch):
        root = tmp_path / "ProgramData"
        folder = root / "Lumi"
        folder.mkdir(parents=True)
        try:
            # The stand-in for ProgramData: owned by Administrators, nobody else may replace its folders.
            _icacls(root, "/inheritance:r", "/grant:r", "*S-1-5-18:(OI)(CI)F", "*S-1-5-32-544:(OI)(CI)F",
                    "*S-1-5-32-545:(OI)(CI)RX")
            _icacls(root, "/setowner", "*S-1-5-32-544")
            # The documented recipe (docs/enterprise-policy.md), on a folder made fresh.
            _icacls(folder, "/inheritance:r", "/grant:r", "*S-1-5-18:(OI)(CI)F", "*S-1-5-32-544:(OI)(CI)F",
                    "*S-1-5-32-545:(OI)(CI)RX")
            _icacls(folder, "/setowner", "*S-1-5-32-544", "/T")
            policy_file = folder / "policy.json"
            policy_file.write_text(json.dumps({**BASE, "permissions": {"allowed_modes": ["ask"]}}), encoding="utf-8")
            _icacls(policy_file, "/setowner", "*S-1-5-32-544")
            trust = admin_files.real_check(policy_file, root)
            assert trust.trusted and trust.admin_owned, trust.reason

            monkeypatch.setattr(lumi_policy, "machine_policy_file", lambda: policy_file)
            monkeypatch.setattr(lumi_policy, "_registry_values", lambda: {})
            admin_files.set_for_tests(None)
            state = lumi_policy.load(force=True)
            assert state.error == "" and not state.policy.mode_allowed("bypass") and not state.ignored

            # The entry ProgramData hands down, put back: an administrator's policy in a folder
            # anyone may add files to fails closed.
            _icacls(folder, "/grant", "*S-1-5-32-545:(CI)(WD,AD)")
            trust = admin_files.real_check(policy_file, root)
            assert not trust.trusted and trust.admin_owned
            assert trust.reason == f"{folder} lets BUILTIN\\Users change it (add files and add folders)"
            state = lumi_policy.load(force=True)
            assert state.policy is None and lumi_policy.UNSAFE_TITLE in state.error
            assert lumi_policy.blocked_reason()
            _icacls(folder, "/remove:g", "*S-1-5-32-545")
            _icacls(folder, "/grant", "*S-1-5-32-545:(OI)(CI)RX")

            # Authenticated Users with Modify on the file itself.
            _icacls(policy_file, "/grant", "*S-1-5-11:(M)")
            trust = admin_files.real_check(policy_file, root)
            assert not trust.trusted and "Authenticated Users" in trust.reason
        finally:
            # Leave the folders deletable for the person's own pytest runs.
            subprocess.run([str(Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "icacls.exe"),
                            str(root), "/reset", "/T", "/C", "/Q"], capture_output=True, timeout=60)


# ── Policy: what an ignored file means ──────────────────────────────────────


@pytest.fixture
def machine(tmp_path, monkeypatch):
    """A machine folder standing in for C:\\ProgramData\\Lumi, /Library/Application Support/Lumi or /etc/lumi."""
    root = tmp_path / "ProgramData"
    folder = root / "Lumi"
    folder.mkdir(parents=True)
    monkeypatch.setattr(lumi_policy, "machine_policy_file", lambda: folder / "policy.json")
    monkeypatch.setattr(lumi_policy, "_program_data", lambda: str(root))
    monkeypatch.setattr(lumi_policy, "_registry_values", lambda: {})
    monkeypatch.setattr(lumi_policy, "MAC_MANAGED_PREFERENCES", tmp_path / "no-managed-preferences")
    monkeypatch.delenv("LUMI_POLICY_FILE", raising=False)
    monkeypatch.delenv("LUMI_LICENSE_FILE", raising=False)
    lumi_policy.set_for_tests(None)
    lumi_license.reset_for_tests()
    yield folder
    lumi_policy.set_for_tests(None)
    lumi_license.reset_for_tests()


@pytest.fixture
def answers():
    """What the check says per file name ({name: Trust}); anything else is trusted."""
    table: dict[str, Trust] = {}
    admin_files.set_for_tests(lambda path, root: table.get(Path(path).name, Trust(True, admin_owned=True)))
    return table


@pytest.fixture
def audit_log(tmp_path):
    log = audit.AuditLog(tmp_path / "audit")
    audit.set_for_tests(log)
    yield log
    audit.set_for_tests(None)


def _records(log, kind="policy.file_ignored"):
    return [record for path in log._files() for line in path.read_text(encoding="utf-8").splitlines()
            for record in [json.loads(line)] if record["type"] == kind]


A_PERSONS = Trust(False, "C:\\ProgramData\\Lumi\\policy.json is owned by DESKTOP\\ana, not by Administrators, "
                  "SYSTEM or TrustedInstaller")
UNSAFE_FOLDER = Trust(False, "C:\\ProgramData\\Lumi lets BUILTIN\\Users change it (add files and add folders)",
                      admin_owned=True)


class TestIgnoredFiles:
    def test_a_policy_a_person_put_there_reads_as_absent(self, machine, answers, audit_log, tmp_path, monkeypatch):
        (machine / "policy.json").write_text(json.dumps({**BASE, "organization": "Planted"}), encoding="utf-8")
        answers["policy.json"] = A_PERSONS
        state = lumi_policy.load(force=True)
        assert state.policy is None and state.error == "" and lumi_policy.blocked_reason() == ""
        [ignored] = state.ignored
        assert (ignored.kind, ignored.path, ignored.reason, ignored.title) == (
            "policy", str(machine / "policy.json"), A_PERSONS.reason, "Policy file ignored: writable by non-administrators")
        # The sources below it still apply, as without the file.
        mine = tmp_path / "mine.json"
        mine.write_text(json.dumps({**BASE, "organization": "Pilot"}), encoding="utf-8")
        monkeypatch.setenv("LUMI_POLICY_FILE", str(mine))
        assert lumi_policy.load(force=True).policy.organization == "Pilot"
        # Recorded once per process, however often the policy loads.
        [record] = _records(audit_log)
        assert record["data"] == {"kind": "policy", "path": str(machine / "policy.json"), "reason": A_PERSONS.reason}

    def test_an_administrators_policy_where_others_can_change_it_fails_closed(self, machine, answers, tmp_path,
                                                                                monkeypatch):
        (machine / "policy.json").write_text(json.dumps(BASE), encoding="utf-8")
        answers["policy.json"] = UNSAFE_FOLDER
        mine = tmp_path / "mine.json"
        mine.write_text(json.dumps({**BASE, "organization": "Mine"}), encoding="utf-8")
        monkeypatch.setenv("LUMI_POLICY_FILE", str(mine))
        state = lumi_policy.load(force=True)
        assert state.policy is None and state.source == str(machine / "policy.json")
        assert state.error.startswith("Policy file ignored: writable by non-administrators. " + UNSAFE_FOLDER.reason)
        assert lumi_policy.blocked_reason().endswith("Ask your administrator to fix it.")
        assert [item.kind for item in state.ignored] == ["policy"]

    def test_a_trusted_policy_applies_and_notes_nothing(self, machine, answers):
        (machine / "policy.json").write_text(json.dumps(BASE), encoding="utf-8")
        state = lumi_policy.load(force=True)
        assert state.policy.organization == "Acme" and state.ignored == () and state.error == ""

    def test_settings_and_lumi_policy_show_an_ignored_file(self, machine, answers, tmp_path, capsys):
        from lumi.gui.settings import SettingsManager

        (machine / "policy.json").write_text(json.dumps(BASE), encoding="utf-8")
        answers["policy.json"] = A_PERSONS
        lumi_policy.load(force=True)
        meta = SettingsManager(tmp_path / "settings.json").get_masked()["_meta"]["policy"]
        assert meta["active"] is False and meta["error"] == ""
        assert meta["ignored"] == [{"kind": "policy", "path": str(machine / "policy.json"),
                                    "reason": A_PERSONS.reason,
                                    "title": "Policy file ignored: writable by non-administrators"}]
        assert lumi_policy.main([]) == 0
        printed = json.loads(capsys.readouterr().out)
        assert printed["ignored"] == meta["ignored"] and printed["active"] is False and printed["blocked"] == ""
        answers["policy.json"] = UNSAFE_FOLDER
        lumi_policy.load(force=True)
        assert lumi_policy.main([]) == 1
        printed = json.loads(capsys.readouterr().out)
        assert "writable by non-administrators" in printed["blocked"]
        assert lumi_policy.main(["extra"]) == 2

    def test_on_windows_a_key_file_isnt_read_but_shows(self, machine, answers, monkeypatch):
        monkeypatch.setattr(lumi_policy.sys, "platform", "win32")
        signing = Ed25519PrivateKey.generate()
        (machine / "policy-keys.json").write_text(json.dumps({"k": _public(signing)}), encoding="utf-8")
        assert lumi_policy.machine_keys() == {}
        monkeypatch.setattr(lumi_policy, "_registry_values", lambda: {"PolicyKeys": json.dumps({"k": _public(signing)})})
        assert lumi_policy.machine_keys() == {"k": _public(signing)}
        state = lumi_policy.load(force=True)
        [ignored] = state.ignored
        assert ignored.kind == "policy_keys" and ignored.title == lumi_policy.WINDOWS_KEYS_FILE_TITLE

    def test_on_macos_and_linux_a_key_file_counts_only_when_only_root_can_have_written_it(self, machine, answers,
                                                                                         monkeypatch):
        monkeypatch.setattr(lumi_policy.sys, "platform", "linux")
        signing = Ed25519PrivateKey.generate()
        (machine / "policy-keys.json").write_text(json.dumps({"k": _public(signing)}), encoding="utf-8")
        assert lumi_policy.machine_keys() == {"k": _public(signing)}
        answers["policy-keys.json"] = Trust(False, "/etc/lumi/policy-keys.json lets its group change it (mode 664)")
        assert lumi_policy.machine_keys() == {}

    def test_a_macos_profile_others_could_have_written(self, machine, answers, tmp_path, monkeypatch):
        import plistlib

        prefs = tmp_path / "Managed Preferences"
        prefs.mkdir()
        profile = prefs / f"{lumi_policy.MAC_DOMAIN}.plist"
        profile.write_bytes(plistlib.dumps({"Policy": json.dumps({**BASE, "organization": "Profile"})}))
        monkeypatch.setattr(lumi_policy, "MAC_MANAGED_PREFERENCES", prefs)
        monkeypatch.setattr(lumi_policy.sys, "platform", "darwin")
        assert lumi_policy.load(force=True).policy.organization == "Profile"
        answers[profile.name] = Trust(False, f"{profile} is owned by uid 501, not by root")
        state = lumi_policy.load(force=True)
        assert state.policy is None and state.error == "" and state.ignored[0].kind == "profile"
        answers[profile.name] = Trust(False, f"{prefs} lets its group change it (mode 775)", admin_owned=True)
        assert lumi_policy.load(force=True).error.startswith(lumi_policy.PROFILE_TITLE + ". ")


class TestPolicyFile:
    """Group Policy's ``PolicyFile``: a file that can't be read or used fails closed, never falls through."""

    @pytest.fixture
    def fallbacks(self, machine, tmp_path, monkeypatch):
        """Sources below it that must never stand in: the machine file and LUMI_POLICY_FILE."""
        (machine / "policy.json").write_text(json.dumps({**BASE, "organization": "Machine file"}), encoding="utf-8")
        mine = tmp_path / "mine.json"
        mine.write_text(json.dumps({**BASE, "organization": "Mine"}), encoding="utf-8")
        monkeypatch.setenv("LUMI_POLICY_FILE", str(mine))
        return machine

    def _named(self, monkeypatch, value):
        monkeypatch.setattr(lumi_policy, "_registry_values", lambda: {"PolicyFile": str(value)})

    def _refused(self, message):
        state = lumi_policy.load(force=True)
        assert state.policy is None and message in state.error, state.error
        assert lumi_policy.current() is None and lumi_policy.blocked_reason().endswith("Ask your administrator to fix it.")
        return state

    def test_a_missing_file_fails_closed(self, fallbacks, answers, tmp_path, monkeypatch):
        missing = tmp_path / "share" / "lumi-policy.json"
        self._named(monkeypatch, missing)
        state = self._refused(f"The policy file Group Policy names couldn't be read: {missing}")
        assert state.source == f"{missing} (set by Group Policy)"

    @windows
    def test_a_share_out_of_reach_fails_closed(self, fallbacks, answers, monkeypatch):
        share = "\\\\lumi-policy-test.invalid\\it\\lumi-policy.json"
        self._named(monkeypatch, share)
        self._refused(f"couldn't be read: {share}")

    @pytest.mark.parametrize("value", ["lumi-policy.json", "%USERPROFILE%\\lumi-policy.json"])
    def test_a_path_that_isnt_a_full_one_fails_closed(self, fallbacks, answers, monkeypatch, value):
        self._named(monkeypatch, value)
        self._refused("isn't a full path")

    def test_a_file_others_can_change_fails_closed_and_shows(self, fallbacks, answers, audit_log, tmp_path,
                                                            monkeypatch):
        named = tmp_path / "share" / "lumi-policy.json"
        named.parent.mkdir()
        named.write_text(json.dumps({**BASE, "organization": "Named"}), encoding="utf-8")
        answers[named.name] = Trust(False, f"{named.parent} lets Authenticated Users change it (add files)")
        self._named(monkeypatch, named)
        state = self._refused("Policy file ignored: writable by non-administrators.")
        assert f"Group Policy names {named}" in state.error
        assert [(item.kind, item.path) for item in state.ignored] == [("policy_file", str(named))]
        assert [record["data"]["kind"] for record in _records(audit_log)] == ["policy_file"]

    def test_a_file_only_administrators_change_applies(self, fallbacks, answers, tmp_path, monkeypatch):
        named = tmp_path / "share" / "lumi-policy.json"
        named.parent.mkdir()
        named.write_text(json.dumps({**BASE, "organization": "Named"}), encoding="utf-8")
        self._named(monkeypatch, named)
        state = lumi_policy.load(force=True)
        assert state.policy.organization == "Named" and state.source == f"{named} (set by Group Policy)"

    def test_a_key_lumi_cant_read_fails_closed(self, fallbacks, answers, monkeypatch):
        def denied():
            raise PermissionError(13, "Access is denied")

        monkeypatch.setattr(lumi_policy, "_registry_values", denied)
        self._refused("couldn't be read (Access is denied)")

    def test_the_policy_value_wins_and_an_empty_file_value_sets_nothing(self, fallbacks, answers, tmp_path,
                                                                       monkeypatch):
        monkeypatch.setattr(lumi_policy, "_registry_values", lambda: {
            "Policy": json.dumps({**BASE, "organization": "Registry"}), "PolicyFile": str(tmp_path / "missing")})
        assert lumi_policy.load(force=True).policy.organization == "Registry"
        monkeypatch.setattr(lumi_policy, "_registry_values", lambda: {"Policy": " ", "PolicyFile": "  "})
        assert lumi_policy.load(force=True).policy.organization == "Machine file"


class TestLicenseFiles:
    def test_a_license_file_others_could_have_written_is_ignored_and_shown(self, machine, answers, audit_log):
        signing = Ed25519PrivateKey.generate()
        document = {"schema": lumi_license.SCHEMA, "license_id": "x", "organization": "Planted", "seats": 1,
                    "offline": True, "expires_at": "2099-01-01T00:00:00Z"}
        signature = base64.b64encode(signing.sign(lumi_license.canonical(document))).decode()
        (machine / "license.json").write_text(json.dumps({"license": document, "key_id": "k", "signature": signature}),
                                              encoding="utf-8")
        answers["license.json"] = Trust(False, f"{machine / 'license.json'} is owned by DESKTOP\\ana, not by "
                                               "Administrators, SYSTEM or TrustedInstaller")
        info = lumi_license.status()
        assert info["present"] is False and info["source"] == ""
        assert [item["kind"] for item in info["ignored"]] == ["license"]
        assert "License file ignored: writable by non-administrators." in lumi_license.describe(info)
        assert [record["data"]["kind"] for record in _records(audit_log)] == ["license"]

    def test_a_license_key_file_counts_only_when_only_root_can_have_written_it(self, machine, answers,
                                                                              monkeypatch):
        monkeypatch.setattr(lumi_license.sys, "platform", "linux")
        monkeypatch.setattr(lumi_license, "_managed_key_texts", lambda: [])
        signing = Ed25519PrivateKey.generate()
        (machine / "license-keys.json").write_text(json.dumps({"k": _public(signing)}), encoding="utf-8")
        assert lumi_license.machine_keys() == {"k": _public(signing)}
        answers["license-keys.json"] = Trust(False, "/etc/lumi/license-keys.json lets others change it (mode 646)")
        assert lumi_license.machine_keys() == {}
        monkeypatch.setattr(lumi_license.sys, "platform", "win32")
        answers.clear()
        assert lumi_license.machine_keys() == {}  # never read on Windows


# ── The reported case, end to end ───────────────────────────────────────────


def test_a_planted_key_file_and_a_self_signed_cloud_policy_no_longer_replace_group_policy(machine, audit_log,
                                                                                         monkeypatch):
    """Group Policy allows only Ask. A person without administrator rights creates the machine folder's
    policy-keys.json with their own key and signs a policy that allows Full-auto where Lumi keeps the
    organization's download. With the real check, neither counts: Group Policy stays in force."""
    admin_files.set_for_tests(None)  # the real check, even for pytest's temporary folders
    group_policy = {**BASE, "permissions": {"allowed_modes": ["ask"]},
                    "cloud": {"url": "https://cloud.acme.example", "organization_id": "org_acme",
                              "enrollment_token": "lce_test"}}
    monkeypatch.setattr(lumi_policy, "_registry_values", lambda: {"Policy": json.dumps(group_policy)})
    person = Ed25519PrivateKey.generate()
    planted = machine / "policy-keys.json"
    planted.write_text(json.dumps({"mine": _public(person)}), encoding="utf-8")
    if os.name != "nt" and os.geteuid() == 0:
        os.chown(planted, 65534, 65534)  # as a person's, when the test runs as root
    mine = {**BASE, "permissions": {"allowed_modes": ["ask", "auto-edit", "plan", "bypass"]}}
    cloud = lumi_policy.cloud_policy_path()
    cloud.parent.mkdir(parents=True, exist_ok=True)
    cloud.write_text(json.dumps(_signed(mine, person, "mine")), encoding="utf-8")

    state = lumi_policy.load(force=True)
    assert not state.cloud and state.policy.source.startswith("Group Policy")
    assert state.policy.allowed_modes == ("ask",) and not state.policy.mode_allowed("bypass")
    assert "doesn't trust" in state.cloud_error and "machine policy applies instead" in state.cloud_error
    assert lumi_policy.full_auto_refusal() and lumi_policy.trusted_cloud_keys() == {}
    [ignored] = [item for item in state.ignored if item.path == str(planted)]
    assert ignored.kind == "policy_keys"
    assert [record["data"]["path"] for record in _records(audit_log)] == [str(planted)]
    # The same files, had they been an administrator's, would still need the key in Group Policy on
    # Windows; elsewhere root's key file is what an administrator uses.
    admin_files.set_for_tests(lambda path, root: Trust(True, admin_owned=True))
    state = lumi_policy.load(force=True)
    if os.name == "nt":
        assert not state.cloud
    else:
        assert state.cloud and state.policy.mode_allowed("bypass")
