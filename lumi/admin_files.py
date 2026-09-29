"""Whether a machine file can only have come from an administrator.

Organization policy (lumi/policy.py), the keys that sign it, and an offline
license (lumi/license.py) are read from files an administrator puts on the
computer. Such a file counts only when nobody else can have written it or can
change it now: the file, and every folder above it up to a root the operating
system protects, must be writable only by administrators. ``check`` says
whether that holds, and why not.

**Windows.** The owner and the discretionary access control list of each, read
with the Win32 security API through ctypes:

* The file and each folder above it must be owned by SYSTEM, Administrators or
  TrustedInstaller, and no entry may allow anyone else a right that changes
  it: write or append (in a folder, add files or folders), change attributes,
  delete, delete what's in it, change permissions, take ownership, or the
  generic write and full-control rights. That includes the entry every folder
  made under ProgramData inherits, ``BUILTIN\\Users:(CI)(WD,AD,WEA,WA)``, which
  lets any user add files to a folder an administrator created. Entries that
  apply only to what is created later (inherit-only) don't count, nor do deny
  entries, which only take rights away. CREATOR OWNER and CREATOR GROUP grant
  nothing on an object that exists; OWNER RIGHTS is the owner, checked first.
  An allow entry Lumi can't read counts against the file.
* On a network share (a UNC path, ``\\\\server\\share``) the Domain Admins and
  Enterprise Admins of the domain this computer belongs to count as
  administrators too: they own and run file servers, and they administer
  every computer in the domain. Never another domain's, and never on a local
  path, where only this computer's administrators count.
* The root is ProgramData for a file under it, otherwise the drive's root or
  the network share's root. People may create folders there (Windows lets any
  user add folders to ``C:\\``), so only what would let them replace a folder
  that exists counts: an owner who isn't an administrator, or anyone else's
  right to delete what's in it, change its permissions or take ownership, and
  for ProgramData to delete (rename) the folder itself.
* A link anywhere on the way (a symbolic link or junction), the root included,
  points somewhere this check didn't look, so it counts against the file.
* A path that names an alternate data stream (``policy.json:other``) is never
  a policy file.

The check and the later read open the path separately. That is safe because
the check proved the file and every folder above it can be changed only by
administrators: replacing either between the two takes administrator rights.
(A person can always change what their own copy of Lumi sees, by running a
changed copy; this protects the organization's files, not a process from the
person running it.)

**macOS and Linux.** ``stat``: the file and each folder above it up to the
root (``/etc``, ``/Library/Application Support``, ``/Library``) must be owned
by root and not writable by their group or others; the root must be owned by
root and not writable by others unless it is sticky. Symbolic links are
followed, since only root can put one in a folder that passes. Access control
lists aren't read: on Linux a POSIX ACL that allows writing shows in the group
bits, and on macOS only an administrator can add one to these folders.

A result also says whether an administrator put the file there
(``admin_owned``: the file, with no other name, and its folder are an
administrator's, with no link on the way), even when a folder isn't safe, so
a policy there fails closed instead of reading as none. A person can make
neither true: they can move a file an administrator owns into a folder of
their own, but that folder stays theirs.

Tests replace the check with ``set_for_tests``.
"""

from __future__ import annotations

import functools
import logging
import os
import stat as stat_module
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)

# ── Who counts as an administrator (Windows) ────────────────────────────────

SYSTEM = "S-1-5-18"
ADMINISTRATORS = "S-1-5-32-544"
TRUSTED_INSTALLER = "S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464"
ADMIN_SIDS = frozenset({SYSTEM, ADMINISTRATORS, TRUSTED_INSTALLER})
# Domain Admins and Enterprise Admins (S-1-5-21-<domain>-<rid>): administrators
# of every computer in the domain and the usual owners of files on a file
# server. They count only on a network share, and only for this computer's
# own domain (machine_domain_sid).
DOMAIN_ADMIN_RIDS = ("512", "519")
# Placeholders that grant nothing on an object that already exists (CREATOR
# OWNER, CREATOR GROUP), and OWNER RIGHTS, which is the owner checked first.
NO_GRANT_SIDS = frozenset({"S-1-3-0", "S-1-3-1", "S-1-3-4"})
WELL_KNOWN_NAMES = {
    "S-1-1-0": "Everyone",
    "S-1-5-2": "NETWORK",
    "S-1-5-3": "BATCH",
    "S-1-5-4": "INTERACTIVE",
    "S-1-5-6": "SERVICE",
    "S-1-5-7": "ANONYMOUS LOGON",
    "S-1-5-11": "Authenticated Users",
    "S-1-5-18": "SYSTEM",
    "S-1-5-19": "LOCAL SERVICE",
    "S-1-5-20": "NETWORK SERVICE",
    "S-1-5-32-544": "BUILTIN\\Administrators",
    "S-1-5-32-545": "BUILTIN\\Users",
    "S-1-5-32-546": "BUILTIN\\Guests",
    "S-1-5-32-547": "BUILTIN\\Power Users",
    "S-1-15-2-1": "ALL APPLICATION PACKAGES",
    TRUSTED_INSTALLER: "NT SERVICE\\TrustedInstaller",
}

# ── Access rights and entry types (winnt.h) ─────────────────────────────────

FILE_WRITE_DATA = 0x2  # in a folder: add a file
FILE_APPEND_DATA = 0x4  # in a folder: add a folder
FILE_WRITE_EA = 0x10
FILE_DELETE_CHILD = 0x40
FILE_WRITE_ATTRIBUTES = 0x100
DELETE = 0x10000
WRITE_DAC = 0x40000
WRITE_OWNER = 0x80000
MAXIMUM_ALLOWED = 0x02000000
GENERIC_ALL = 0x10000000
GENERIC_WRITE = 0x40000000
# Any of these lets someone change a file or folder, or what a folder holds.
CHANGES = (FILE_WRITE_DATA | FILE_APPEND_DATA | FILE_WRITE_EA | FILE_DELETE_CHILD | FILE_WRITE_ATTRIBUTES | DELETE
           | WRITE_DAC | WRITE_OWNER | MAXIMUM_ALLOWED | GENERIC_ALL | GENERIC_WRITE)
# Any of these lets someone replace a folder that already exists in a root.
REPLACES = FILE_DELETE_CHILD | WRITE_DAC | WRITE_OWNER | MAXIMUM_ALLOWED | GENERIC_ALL

INHERIT_ONLY_ACE = 0x8
# ACCESS_ALLOWED, _COMPOUND, _OBJECT, _CALLBACK and _CALLBACK_OBJECT: entries
# that grant. Deny, audit, alarm, label and attribute entries don't.
ALLOW_TYPES = frozenset({0x0, 0x4, 0x5, 0x9, 0xB})
_SIMPLE_TYPES = frozenset({0x0, 0x1, 0x9, 0xA})  # the SID follows the mask
_OBJECT_TYPES = frozenset({0x5, 0x6, 0xB, 0xC})  # flags and optional GUIDs come first

_RIGHT_NAMES = (
    (GENERIC_ALL | MAXIMUM_ALLOWED, "full control", "full control"),
    (GENERIC_WRITE, "write", "write"),
    (FILE_WRITE_DATA, "write", "add files"),
    (FILE_APPEND_DATA, "append", "add folders"),
    (FILE_WRITE_ATTRIBUTES | FILE_WRITE_EA, "change attributes", "change attributes"),
    (DELETE, "delete", "delete"),
    (FILE_DELETE_CHILD, "delete", "delete what's in it"),
    (WRITE_DAC, "change permissions", "change permissions"),
    (WRITE_OWNER, "take ownership", "take ownership"),
)

FILE_ATTRIBUTE_REPARSE_POINT = 0x400
# Reparse points that stand for another path: symbolic links, junctions.
REPARSE_NAME_SURROGATE = 0x20000000


@dataclass(frozen=True)
class Ace:
    """One entry of a discretionary access control list."""

    type: int
    flags: int
    mask: int
    sid: str | None  # None for an entry whose layout Lumi doesn't read


@dataclass(frozen=True)
class Descriptor:
    """The parts of a security descriptor the check reads."""

    owner: str | None
    # None: no access control list at all, which lets everyone do anything.
    dacl: tuple[Ace, ...] | None


@dataclass(frozen=True)
class Trust:
    """Whether a file can only have come from an administrator, and why not."""

    trusted: bool
    reason: str = ""  # for people: "C:\\ProgramData\\Lumi lets BUILTIN\\Users change it (add files, ...)"
    # The file (with no other name) and its folder belong to an administrator,
    # with no link on the way: someone with administrator rights put it
    # there, even when a folder above it isn't safe.
    admin_owned: bool = False


# ── The check ───────────────────────────────────────────────────────────────

_override: Callable[[Path, Path | None], Trust] | None = None


def check(path: str | os.PathLike, root: str | os.PathLike | None = None) -> Trust:
    """Whether only administrators can have written ``path`` and can change it. Never raises.

    ``root`` is the protected folder the walk up stops at (ProgramData,
    ``/etc``): the file and the folders between are checked strictly, the root
    only for what would replace them. Without one, or when the file isn't
    under it, the walk goes up to the drive's or share's root (``/`` elsewhere).
    """
    if _override is not None:
        return _override(Path(path), None if root is None else Path(root))
    return real_check(path, root)


def real_check(path: str | os.PathLike, root: str | os.PathLike | None = None) -> Trust:
    """``check`` as it runs outside tests, even while a test replaced it."""
    target = Path(path)
    top = None if root is None else Path(root)
    try:
        return _check_windows(target, top) if os.name == "nt" else _check_posix(target, top)
    except Exception as exc:  # a check that fails is a file Lumi can't trust
        logger.warning("Couldn't check who can change %s", target, exc_info=True)
        return Trust(False, f"Lumi couldn't check who can change {target} ({exc})")


def set_for_tests(checker: Callable[[Path, Path | None], Trust] | None) -> None:
    """Replace ``check`` (tests only); None restores the real one."""
    global _override
    _override = checker


def _same(first: Path, second: Path) -> bool:
    return os.path.normcase(os.path.normpath(str(first))) == os.path.normcase(os.path.normpath(str(second)))


def _inside(path: Path, folder: Path) -> bool:
    prefix = os.path.normcase(os.path.normpath(str(folder))).rstrip("\\/") + os.sep
    return os.path.normcase(os.path.normpath(str(path))).startswith(prefix)


def chain(path: Path, root: Path | None) -> list[tuple[Path, str]]:
    """``path`` and the folders above it up to ``root``, each with its level.

    Levels: ``file``; ``folder`` (checked like the file); ``root`` (a
    protected folder such as ProgramData: only what replaces what's in it, or
    renames it); ``anchor`` (the drive's, share's or file system's root: only
    what replaces what's in it).
    """
    anchor = Path(path.anchor)
    stop = root if root is not None and _inside(path, root) else anchor
    items = [(path, "file")]
    for parent in path.parents:
        if _same(parent, stop):
            items.append((parent, "anchor" if _same(parent, anchor) else "root"))
            break
        items.append((parent, "folder"))
    return items


def _join(words: list[str]) -> str:
    return words[0] if len(words) == 1 else ", ".join(words[:-1]) + " and " + words[-1]


FILE_ALL_ACCESS = 0x1F01FF


def describe_rights(mask: int, *, folder: bool) -> str:
    """The rights in ``mask`` that change things, in words: "add files, add folders and change attributes"."""
    if mask & FILE_ALL_ACCESS == FILE_ALL_ACCESS:
        return "full control"
    names: list[str] = []
    for bits, on_file, on_folder in _RIGHT_NAMES:
        name = on_folder if folder else on_file
        if mask & bits and name not in names:
            names.append(name)
    return _join(names) if names else f"rights 0x{mask:x}"


def is_admin_sid(sid: str | None, domain: str | None = None) -> bool:
    """SYSTEM, Administrators or TrustedInstaller; with ``domain`` (the computer's
    domain SID, for a network share) also that domain's Domain Admins and Enterprise Admins."""
    if not sid:
        return False
    if sid in ADMIN_SIDS:
        return True
    return bool(domain) and sid in {f"{domain}-{rid}" for rid in DOMAIN_ADMIN_RIDS}


def descriptor_problem(descriptor: Descriptor, level: str, name: Callable[[str], str] | None = None,
                       domain: str | None = None) -> str:
    """Why people who aren't administrators can change an object with ``descriptor``, or ''.

    Words that follow the object's path: "is owned by DESKTOP\\ana, not by
    Administrators, SYSTEM or TrustedInstaller", "lets BUILTIN\\Users change it
    (add files and add folders)". ``level`` is one of ``chain``'s; ``domain``
    is the computer's domain SID for an object on a network share
    (``is_admin_sid``), None on a local path.
    """
    name = name or account_name
    if not descriptor.owner:
        return "has no owner"
    if not is_admin_sid(descriptor.owner, domain):
        admins = "Administrators, SYSTEM or TrustedInstaller"
        if domain:
            admins += ", or this computer's domain's Domain Admins or Enterprise Admins"
        return f"is owned by {name(descriptor.owner)}, not by {admins}"
    if descriptor.dacl is None:
        return "has no access control list, so everyone can change it"
    watched = {"root": REPLACES | DELETE, "anchor": REPLACES}.get(level, CHANGES)
    for ace in descriptor.dacl:
        if ace.flags & INHERIT_ONLY_ACE or ace.type not in ALLOW_TYPES:
            continue
        rights = ace.mask & watched
        if not rights:
            continue
        # All of a file's rights read as full control; otherwise the ones that count here.
        whole = ace.mask & FILE_ALL_ACCESS == FILE_ALL_ACCESS
        words = describe_rights(ace.mask if whole else rights, folder=level != "file")
        if ace.sid is None:
            return f"has a permission entry Lumi can't read that allows changes ({words})"
        if ace.sid in NO_GRANT_SIDS or is_admin_sid(ace.sid, domain):
            continue
        return f"lets {name(ace.sid)} change it ({words})"
    return ""


# ── Windows ─────────────────────────────────────────────────────────────────


def _plain_windows_path(text: str) -> str | None:
    r"""``text`` without a ``\\?\`` prefix, or None for a device path (``\\.\``)."""
    if text.startswith("\\\\?\\UNC\\"):
        return "\\\\" + text[8:]
    if text.startswith("\\\\?\\"):
        return text[4:]
    if text.startswith("\\\\.\\"):
        return None
    return text


def names_stream(text: str) -> bool:
    r"""Whether a Windows path names an alternate data stream (``C:\x\policy.json:other``), not a file.

    A colon is valid only right after a drive letter; one anywhere else opens
    a stream of the file or folder before it. The same on every platform, so
    tests of Windows paths agree.
    """
    import ntpath

    plain = _plain_windows_path(text) or text
    drive, rest = ntpath.splitdrive(plain)
    return ":" in rest or (drive.startswith("\\\\") and ":" in drive)


def _is_link(path: Path) -> bool:
    try:
        info = os.lstat(path)
    except OSError:
        return False  # reading its permissions says why
    if stat_module.S_ISLNK(info.st_mode):
        return True
    return bool(getattr(info, "st_file_attributes", 0) & FILE_ATTRIBUTE_REPARSE_POINT
                and getattr(info, "st_reparse_tag", 0) & REPARSE_NAME_SURROGATE)


def _single_name(path: Path) -> bool:
    """Whether a file has no other name (hard link) that may be somewhere a person could put one."""
    try:
        return os.stat(path).st_nlink == 1
    except OSError:
        return False


def _check_windows(path: Path, root: Path | None) -> Trust:
    plain = _plain_windows_path(str(path))
    if plain is None:
        return Trust(False, f"{path} is a device path, not a file")
    if names_stream(plain):
        return Trust(False, f"{path} names an alternate data stream, not a file")
    target = Path(os.path.abspath(plain))
    top = None
    if root is not None:
        top_plain = _plain_windows_path(str(root))
        top = Path(os.path.abspath(top_plain)) if top_plain else None
    items = chain(target, top)
    # Links first, at every level down from the root: any user may make a
    # folder under ProgramData a junction to an administrator's folder
    # elsewhere, so behind a link even a file an administrator owns says
    # nothing about who put it on this path.
    for item, _ in items:
        if _is_link(item):
            return Trust(False, f"{item} is a link (a symbolic link or junction) to another place")
    # A network share's files may belong to this computer's domain's Domain
    # Admins or Enterprise Admins; a local path's only to this computer's administrators.
    domain = machine_domain_sid() if str(target).startswith("\\\\") else None
    descriptors = []
    for item, _ in items:
        try:
            descriptors.append(read_descriptor(item))
        except OSError as exc:
            return Trust(False, f"Lumi couldn't read who can change {item} ({exc.strerror or exc})")
    # An administrator put the file there when it and its folder are an
    # administrator's: a person can move a file an administrator owns (one an
    # elevated installer left in their profile) into a folder they made, but
    # can't make that folder an administrator's.
    admin_owned = (is_admin_sid(descriptors[0].owner, domain) and _single_name(target)
                   and len(descriptors) > 1 and is_admin_sid(descriptors[1].owner, domain))
    for (item, level), descriptor in zip(items, descriptors):
        problem = descriptor_problem(descriptor, level, domain=domain)
        if problem:
            return Trust(False, f"{item} {problem}", admin_owned)
    return Trust(True, admin_owned=admin_owned)


@functools.lru_cache(maxsize=1)
def machine_domain_sid() -> str | None:
    """The SID of the domain this computer belongs to (LSA's primary domain), or None.

    None on a computer in a workgroup, off Windows, or when Windows won't say:
    then no domain group counts as an administrator. Read locally from the
    computer's own settings, never from the environment or the network.
    """
    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        advapi = ctypes.WinDLL("advapi32", use_last_error=True)

        class _LsaUnicodeString(ctypes.Structure):
            _fields_ = [("Length", wintypes.USHORT), ("MaximumLength", wintypes.USHORT),
                        ("Buffer", ctypes.c_void_p)]

        class _LsaObjectAttributes(ctypes.Structure):
            _fields_ = [("Length", wintypes.ULONG), ("RootDirectory", wintypes.HANDLE),
                        ("ObjectName", ctypes.c_void_p), ("Attributes", wintypes.ULONG),
                        ("SecurityDescriptor", ctypes.c_void_p), ("SecurityQualityOfService", ctypes.c_void_p)]

        class _PrimaryDomainInfo(ctypes.Structure):
            _fields_ = [("Name", _LsaUnicodeString), ("Sid", ctypes.c_void_p)]

        advapi.LsaOpenPolicy.argtypes = [ctypes.c_void_p, ctypes.POINTER(_LsaObjectAttributes), wintypes.DWORD,
                                         ctypes.POINTER(ctypes.c_void_p)]
        advapi.LsaOpenPolicy.restype = wintypes.ULONG
        advapi.LsaQueryInformationPolicy.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p)]
        advapi.LsaQueryInformationPolicy.restype = wintypes.ULONG
        advapi.LsaFreeMemory.argtypes = [ctypes.c_void_p]
        advapi.LsaFreeMemory.restype = wintypes.ULONG
        advapi.LsaClose.argtypes = [ctypes.c_void_p]
        advapi.LsaClose.restype = wintypes.ULONG
        attributes, handle = _LsaObjectAttributes(), ctypes.c_void_p()
        # POLICY_VIEW_LOCAL_INFORMATION, which every user has.
        if advapi.LsaOpenPolicy(None, ctypes.byref(attributes), 0x1, ctypes.byref(handle)):
            return None
        try:
            buffer = ctypes.c_void_p()
            if advapi.LsaQueryInformationPolicy(handle, 3, ctypes.byref(buffer)):  # PolicyPrimaryDomainInformation
                return None
            try:
                info = ctypes.cast(buffer, ctypes.POINTER(_PrimaryDomainInfo)).contents
                sid = _sid_text(info.Sid) if info.Sid else ""
                return sid if sid.startswith("S-1-5-21-") else None
            finally:
                advapi.LsaFreeMemory(buffer)
        finally:
            advapi.LsaClose(handle)
    except Exception:  # an unknown domain grants nobody anything
        logger.debug("This computer's domain couldn't be read", exc_info=True)
        return None


@functools.lru_cache(maxsize=1)
def _api():
    import ctypes
    from ctypes import wintypes

    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    pointer = ctypes.c_void_p
    advapi.GetNamedSecurityInfoW.argtypes = [wintypes.LPCWSTR, ctypes.c_int, wintypes.DWORD,
                                             ctypes.POINTER(pointer), ctypes.POINTER(pointer),
                                             ctypes.POINTER(pointer), ctypes.POINTER(pointer),
                                             ctypes.POINTER(pointer)]
    advapi.GetNamedSecurityInfoW.restype = wintypes.DWORD
    advapi.GetSecurityDescriptorOwner.argtypes = [pointer, ctypes.POINTER(pointer), ctypes.POINTER(wintypes.BOOL)]
    advapi.GetSecurityDescriptorOwner.restype = wintypes.BOOL
    advapi.GetSecurityDescriptorDacl.argtypes = [pointer, ctypes.POINTER(wintypes.BOOL), ctypes.POINTER(pointer),
                                                 ctypes.POINTER(wintypes.BOOL)]
    advapi.GetSecurityDescriptorDacl.restype = wintypes.BOOL
    advapi.GetAce.argtypes = [pointer, wintypes.DWORD, ctypes.POINTER(pointer)]
    advapi.GetAce.restype = wintypes.BOOL
    advapi.IsValidSid.argtypes = [pointer]
    advapi.IsValidSid.restype = wintypes.BOOL
    advapi.ConvertSidToStringSidW.argtypes = [pointer, ctypes.POINTER(wintypes.LPWSTR)]
    advapi.ConvertSidToStringSidW.restype = wintypes.BOOL
    advapi.ConvertStringSidToSidW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(pointer)]
    advapi.ConvertStringSidToSidW.restype = wintypes.BOOL
    advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(pointer), ctypes.POINTER(wintypes.ULONG)]
    advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL
    advapi.LookupAccountSidW.argtypes = [wintypes.LPCWSTR, pointer, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD),
                                         wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD),
                                         ctypes.POINTER(wintypes.DWORD)]
    advapi.LookupAccountSidW.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [pointer]
    kernel.LocalFree.restype = pointer
    return ctypes, wintypes, advapi, kernel


def _sid_text(address: int) -> str:
    ctypes, wintypes, advapi, kernel = _api()
    text = wintypes.LPWSTR()
    if not advapi.ConvertSidToStringSidW(address, ctypes.byref(text)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return str(text.value)
    finally:
        kernel.LocalFree(ctypes.cast(text, ctypes.c_void_p))


def _parse_descriptor(descriptor: int) -> Descriptor:
    """A security descriptor in memory (self-relative or absolute), as a Descriptor."""
    ctypes, wintypes, advapi, _ = _api()
    owner, defaulted = ctypes.c_void_p(), wintypes.BOOL()
    if not advapi.GetSecurityDescriptorOwner(descriptor, ctypes.byref(owner), ctypes.byref(defaulted)):
        raise ctypes.WinError(ctypes.get_last_error())
    present, dacl = wintypes.BOOL(), ctypes.c_void_p()
    if not advapi.GetSecurityDescriptorDacl(descriptor, ctypes.byref(present), ctypes.byref(dacl),
                                            ctypes.byref(defaulted)):
        raise ctypes.WinError(ctypes.get_last_error())
    owner_sid = _sid_text(owner.value) if owner.value else None
    if not present.value or not dacl.value:
        return Descriptor(owner_sid, None)
    # ACL header: BYTE revision, BYTE reserved, WORD size, WORD entry count, WORD reserved.
    count = ctypes.c_ushort.from_address(dacl.value + 4).value
    entries = []
    for index in range(count):
        entry = ctypes.c_void_p()
        if not advapi.GetAce(dacl, index, ctypes.byref(entry)):
            raise ctypes.WinError(ctypes.get_last_error())
        base = entry.value
        # ACE header: BYTE type, BYTE flags, WORD size; then the DWORD mask.
        kind = ctypes.c_ubyte.from_address(base).value
        flags = ctypes.c_ubyte.from_address(base + 1).value
        size = ctypes.c_ushort.from_address(base + 2).value
        mask = ctypes.c_uint32.from_address(base + 4).value
        where = None
        if kind in _SIMPLE_TYPES:
            where = base + 8
        elif kind in _OBJECT_TYPES:
            present_guids = ctypes.c_uint32.from_address(base + 8).value
            where = base + 12 + (16 if present_guids & 1 else 0) + (16 if present_guids & 2 else 0)
        sid = None
        if where is not None and where + 8 <= base + size and advapi.IsValidSid(where):
            sid = _sid_text(where)
        entries.append(Ace(kind, flags, mask, sid))
    return Descriptor(owner_sid, tuple(entries))


def read_descriptor(path: str | os.PathLike) -> Descriptor:
    """The owner and access control list of a file or folder (Windows); OSError when they can't be read."""
    ctypes, _, advapi, kernel = _api()
    descriptor = ctypes.c_void_p()
    # SE_FILE_OBJECT; OWNER_SECURITY_INFORMATION | DACL_SECURITY_INFORMATION.
    status = advapi.GetNamedSecurityInfoW(str(path), 1, 0x1 | 0x4, None, None, None, None, ctypes.byref(descriptor))
    if status:
        raise OSError(0, ctypes.FormatError(status).strip(), str(path), status)
    try:
        return _parse_descriptor(descriptor.value)
    finally:
        kernel.LocalFree(descriptor)


def descriptor_from_sddl(sddl: str) -> Descriptor:
    """A security descriptor written in SDDL, as the check reads it (Windows): the MSI's, and tests'."""
    ctypes, _, advapi, kernel = _api()
    descriptor = ctypes.c_void_p()
    if not advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, 1, ctypes.byref(descriptor), None):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return _parse_descriptor(descriptor.value)
    finally:
        kernel.LocalFree(descriptor)


@functools.lru_cache(maxsize=256)
def account_name(sid: str) -> str:
    """``DOMAIN\\name`` for a SID, or the SID itself when Windows can't name it."""
    if sid in WELL_KNOWN_NAMES:
        return WELL_KNOWN_NAMES[sid]
    if os.name != "nt":
        return sid
    try:
        ctypes, wintypes, advapi, kernel = _api()
        binary = ctypes.c_void_p()
        if not advapi.ConvertStringSidToSidW(sid, ctypes.byref(binary)):
            return sid
        try:
            name, domain = ctypes.create_unicode_buffer(256), ctypes.create_unicode_buffer(256)
            name_size, domain_size, use = wintypes.DWORD(256), wintypes.DWORD(256), wintypes.DWORD()
            if not advapi.LookupAccountSidW(None, binary, name, ctypes.byref(name_size), domain,
                                            ctypes.byref(domain_size), ctypes.byref(use)):
                return sid
            return f"{domain.value}\\{name.value}" if domain.value else name.value
        finally:
            kernel.LocalFree(binary)
    except Exception:  # only a label for a message
        return sid


# ── macOS and Linux ─────────────────────────────────────────────────────────


def _user_name(uid: int) -> str:
    try:
        import pwd

        return f"{pwd.getpwuid(uid).pw_name} (uid {uid})"
    except (ImportError, KeyError):
        return f"uid {uid}"


def posix_problem(info: os.stat_result, level: str) -> str:
    """Why an object with this ``stat`` can be changed by someone other than root, or ''."""
    if info.st_uid != 0:
        return f"is owned by {_user_name(info.st_uid)}, not by root"
    mode = stat_module.S_IMODE(info.st_mode)
    if level in ("root", "anchor"):
        if mode & 0o002 and not mode & stat_module.S_ISVTX:
            return f"lets others replace what's in it (mode {mode:o})"
        return ""
    if mode & 0o022:
        who = "its group and others" if mode & 0o022 == 0o022 else "its group" if mode & 0o020 else "others"
        return f"lets {who} change it (mode {mode:o})"
    return ""


def _check_posix(path: Path, root: Path | None) -> Trust:
    target = Path(os.path.abspath(path))
    top = Path(os.path.abspath(root)) if root is not None else None
    items = chain(target, top)
    # Links are followed (stat), but where a folder isn't safe a person may
    # have put one there, so the file's owner then says nothing about who
    # placed it.
    linked = any(level in ("file", "folder") and os.path.islink(item) for item, level in items)
    infos = []
    for item, _ in items:
        try:
            infos.append(os.stat(item))
        except OSError as exc:
            return Trust(False, f"Lumi couldn't check who can change {item} ({exc.strerror or exc})")
    # As on Windows: root put the file there when root owns it and its folder.
    admin_owned = (infos[0].st_uid == 0 and infos[0].st_nlink == 1 and not linked
                   and len(infos) > 1 and infos[1].st_uid == 0)
    for (item, level), info in zip(items, infos):
        problem = posix_problem(info, level)
        if problem:
            return Trust(False, f"{item} {problem}", admin_owned)
    return Trust(True, admin_owned=admin_owned)
