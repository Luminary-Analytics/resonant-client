"""Explicit operator-configured managed runs and a bounded parent-side pump.

The browser selects a mode, never an endpoint, tenant, certificate or key path.
Configuration loading and construction perform no network operation. Personal
history is not converted, and reopening a prior journal never renews authority.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import re
import threading
from uuid import UUID

from .managed_client import HostChannelClient, HostChannelConfig
from .managed_journal import ManagedBinding, ManagedJournal, projection_document
from .managed_runtime import ManagedRuntime
from .models import RunAuthority, Scope, ScopeDenied


class ManagedSetupError(ValueError):
    """Safe setup failure without filesystem paths or credential material."""


def _canonical_path(value):
    return os.path.normcase(str(Path(value).resolve(strict=True)))


def _protected_windows_file(path: Path) -> None:
    """Require owner/SYSTEM/Administrators-only discretionary allow ACEs.

    This inspects the native ACL, not localized command output. Unknown allow
    ACE types fail closed. Administrators and the OS remain explicitly trusted;
    this is not protection from a compromised local account or machine owner.
    """
    import ctypes
    from ctypes import wintypes

    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    pointer = ctypes.c_void_p
    advapi.GetNamedSecurityInfoW.argtypes = [wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD,
                                           ctypes.POINTER(pointer), pointer, ctypes.POINTER(pointer), pointer, ctypes.POINTER(pointer)]
    advapi.GetNamedSecurityInfoW.restype = wintypes.DWORD
    advapi.ConvertSidToStringSidW.argtypes = [pointer, ctypes.POINTER(wintypes.LPWSTR)]
    advapi.ConvertSidToStringSidW.restype = wintypes.BOOL
    advapi.GetAce.argtypes = [pointer, wintypes.DWORD, ctypes.POINTER(pointer)]
    advapi.GetAce.restype = wintypes.BOOL
    advapi.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    advapi.OpenProcessToken.restype = wintypes.BOOL
    advapi.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, pointer, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    advapi.GetTokenInformation.restype = wintypes.BOOL
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.LocalFree.argtypes = [pointer]
    kernel.LocalFree.restype = pointer

    def sid_text(sid):
        result = wintypes.LPWSTR()
        if not advapi.ConvertSidToStringSidW(sid, ctypes.byref(result)):
            raise ManagedSetupError("Managed file permissions could not be verified")
        try:
            return result.value
        finally:
            kernel.LocalFree(ctypes.cast(result, pointer))

    token = wintypes.HANDLE()
    if not advapi.OpenProcessToken(kernel.GetCurrentProcess(), 8, ctypes.byref(token)):
        raise ManagedSetupError("Managed file permissions could not be verified")
    try:
        size = wintypes.DWORD()
        advapi.GetTokenInformation(token, 1, None, 0, ctypes.byref(size))
        if not 0 < size.value < 65536:
            raise ManagedSetupError("Managed file permissions could not be verified")
        buffer = ctypes.create_string_buffer(size.value)
        if not advapi.GetTokenInformation(token, 1, buffer, size.value, ctypes.byref(size)):
            raise ManagedSetupError("Managed file permissions could not be verified")
        user_sid = ctypes.cast(buffer, ctypes.POINTER(pointer)).contents.value
        allowed = {sid_text(user_sid), "S-1-5-18", "S-1-5-32-544"}
    finally:
        kernel.CloseHandle(token)
    owner, dacl, descriptor = pointer(), pointer(), pointer()
    status = advapi.GetNamedSecurityInfoW(str(path), 1, 5, ctypes.byref(owner), None, ctypes.byref(dacl), None, ctypes.byref(descriptor))
    if status:
        raise ManagedSetupError("Managed file permissions could not be verified")
    try:
        if not owner or sid_text(owner) not in allowed or not dacl:
            raise ManagedSetupError("Managed files require restricted owner permissions")
        # OWNER RIGHTS addresses this object's owner, whose identity was just
        # verified above; it is not a grant to the Users/Everyone groups.
        allowed.add("S-1-3-4")
        # ACL: BYTE revision, BYTE reserved, WORD size, WORD count, WORD reserved.
        count = ctypes.c_ushort.from_address(dacl.value + 4).value
        for index in range(count):
            ace = pointer()
            if not advapi.GetAce(dacl, index, ctypes.byref(ace)):
                raise ManagedSetupError("Managed file permissions could not be verified")
            ace_type = ctypes.c_ubyte.from_address(ace.value).value
            if ace_type == 1:  # ACCESS_DENIED_ACE never grants access.
                continue
            if ace_type != 0:  # Reject callback/object/unknown allow semantics.
                raise ManagedSetupError("Managed files require simple restricted permissions")
            mask = ctypes.c_uint32.from_address(ace.value + 4).value
            if mask and sid_text(ace.value + 8) not in allowed:
                raise ManagedSetupError("Managed files require restricted owner permissions")
    finally:
        kernel.LocalFree(descriptor)


def _protected_file(value: str, workspace: Path | None = None) -> Path:
    if type(value) is not str:
        raise ManagedSetupError("Managed setup requires protected absolute files")
    source = Path(value)
    if not source.is_absolute() or not source.is_file():
        raise ManagedSetupError("Managed setup requires protected absolute files")
    for part in (source, *source.parents):
        metadata = part.lstat()
        if part.is_symlink() or getattr(metadata, "st_file_attributes", 0) & 0x400:
            raise ManagedSetupError("Managed files cannot use filesystem redirects")
    resolved = source.resolve(strict=True)
    if workspace is not None and resolved.is_relative_to(workspace):
        raise ManagedSetupError("Managed credentials and configuration must be outside the workspace")
    if os.name == "nt":
        _protected_windows_file(resolved)
    elif resolved.stat().st_mode & 0o077 or resolved.stat().st_uid != os.getuid():
        raise ManagedSetupError("Managed files require restricted owner permissions")
    return resolved


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ManagedSetupError("Managed configuration has duplicate fields")
        result[key] = value
    return result


@dataclass(frozen=True, slots=True)
class ManagedDesktopConfig:
    """Trusted operator configuration; never serialize this object to the GUI."""

    transport: HostChannelConfig = field(repr=False)
    tenant_id: str
    project_id: str
    host_id: str
    host_generation: int
    owner_id: str
    local_owner_id: str
    workspace: str = field(repr=False)
    policy_revision: int


def load_configuration(path: str) -> ManagedDesktopConfig:
    """Read a fixed startup file with bounded strict JSON and native ACL checks."""
    try:
        source = _protected_file(path)
        with source.open("rb") as stream:
            encoded = stream.read(32769)
        if len(encoded) > 32768:
            raise ManagedSetupError("Managed configuration exceeds its limit")
        value = json.loads(encoded, object_pairs_hook=_unique_object,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite")))
        required = {"version", "transport", "tenant_id", "project_id", "host_id", "host_generation",
                    "owner_id", "local_owner_id", "workspace", "policy_revision"}
        if type(value) is not dict or set(value) != required or type(value["version"]) is not int or value["version"] != 1:
            raise ManagedSetupError("Managed configuration version or fields are unsupported")
        workspace = Path(value["workspace"])
        if not workspace.is_absolute() or not workspace.is_dir():
            raise ManagedSetupError("Managed setup requires an explicit existing workspace")
        workspace = workspace.resolve(strict=True)
        _protected_file(path, workspace)
        for key in ("tenant_id", "project_id", "host_id"):
            if type(value[key]) is not str or str(UUID(value[key])) != value[key]:
                raise ManagedSetupError("Managed identity is invalid")
        for key in ("host_generation", "policy_revision"):
            if type(value[key]) is not int or value[key] < 1:
                raise ManagedSetupError("Managed revision is invalid")
        for key in ("owner_id", "local_owner_id"):
            if type(value[key]) is not str or not value[key].strip() or len(value[key]) > 256:
                raise ManagedSetupError("Managed owner binding is invalid")
        if not re.fullmatch(r"[a-f0-9]{64}", value["owner_id"]):
            raise ManagedSetupError("Managed owner must be an exact enrolled actor identity")
        transport = value["transport"]
        if type(transport) is not dict:
            raise ManagedSetupError("Managed transport is invalid")
        transport = HostChannelConfig(**transport)
        for protected in (transport.ca_file, transport.certificate_file, transport.private_key_file):
            _protected_file(protected, workspace)
        return ManagedDesktopConfig(transport, *(value[key] for key in ("tenant_id", "project_id", "host_id", "host_generation",
                                     "owner_id", "local_owner_id")), str(workspace), value["policy_revision"])
    except ManagedSetupError:
        raise
    except Exception:
        raise ManagedSetupError("Managed configuration could not be verified") from None


def metadata_projection(snapshot: dict) -> dict:
    """Select protocol counters only; none of the source's freeform data escapes."""
    run = snapshot["run"]
    attempts = snapshot.get("attempts", [])
    requests = snapshot.get("model_requests", [])
    checks = snapshot.get("integration_checks", [])
    unknown = sum(row.get("state") == "uncertain" for row in requests)
    uncertain_effect = any(row.get("state") == "uncertain" for key in ("action_receipts", "integration_operations", "integration_checks")
                           for row in snapshot.get(key, []))
    if run["state"] == "recovery_required":
        alert = "lease_expired"
    elif unknown or uncertain_effect or any(row.get("process_state") == "unknown" for row in attempts):
        alert = "uncertain_effect"
    elif run["state"] == "stopping":
        alert = "unconfirmed_stop"
    elif any(row.get("state") == "failed" for row in checks):
        alert = "failed_check"
    elif snapshot.get("remaining_requests") == 0 and run["state"] == "running":
        alert = "allowance_exhausted"
    elif run["state"] in {"review", "blocked", "paused"}:
        alert = "needs_input"
    else:
        alert = "none"
    return projection_document({"version": 1, "epoch": run["epoch"], "local_revision": run["revision"],
        "state": run["state"], "alert": alert, "counts": {
            "workers_active": sum(row.get("kind") == "worker" and row.get("process_state") == "running" for row in attempts),
            "workers_pending": sum(row.get("kind") == "worker" and row.get("process_state") == "pending" for row in attempts),
            "requests_known": sum(row.get("state") in {"completed", "failed", "not_started"} for row in requests),
            "requests_held": sum(row.get("state") in {"reserved", "started", "uncertain"} for row in requests),
            "requests_unknown": unknown,
            "checks_passed": sum(row.get("state") == "passed" for row in checks),
            "checks_failed": sum(row.get("state") == "failed" for row in checks)}})


class ManagedAttachment:
    """Captured parent runtime plus observer; it never owns local Stop semantics."""

    def __init__(self, runtime: ManagedRuntime):
        self.runtime = runtime
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._connection_state = "not_contacted"
        self._closed = False
        self._effects = None

    def effects(self, store):
        """Create one durable owner-effect coverage journal before any launch."""
        from .managed_effects import ManagedEffects
        with self._lock:
            if self._closed:
                raise ManagedSetupError("Managed observer is closed")
            if self._effects is None:
                self._effects = ManagedEffects(store, self.runtime)
            elif self._effects.store.path.resolve() != store.path.resolve():
                raise ScopeDenied("Managed owner effects belong to another native store")
            return self._effects

    def view(self) -> dict:
        """Return explicit effective policy only after an authenticated lease."""
        policy = self.runtime.policy_view()
        with self._lock:
            status = self._connection_state
            closed = self._closed
            observing = self._thread is not None and self._thread.is_alive()
            effects = self._effects
        return {"mode": "managed", "tenant_id": self.runtime.binding.tenant_id,
                "project_id": self.runtime.binding.project_id, "host_id": self.runtime.binding.host_id,
                "offline_request_allowance": 0, "enforcement_mode": "managed_local_reporting",
                "connection": status, "closed": closed, "observer_running": observing,
                "effective_policy": policy,
                "pending_observations": self.runtime.journal.inspect()["pending_observations"],
                "owner_effects": effects.inspect() if effects is not None else None}

    def start_pump(self, runner, snapshot_callback, *, interval_seconds: float = 2.0) -> None:
        """Start one captured observer only on explicit live managed-run setup."""
        if type(interval_seconds) not in (int, float) or not .05 <= interval_seconds <= 30:
            raise ValueError("Managed observation interval is invalid")
        binding = self.runtime.binding
        authority = runner.authority
        if (authority.run_id != binding.run_id or authority.epoch != binding.epoch
                or authority.scope.values() != (binding.tenant_id, binding.owner_id, binding.local_project_id, binding.session_id)):
            raise ScopeDenied("Managed observer does not match the captured run")
        with self._lock:
            if self._closed:
                raise ManagedSetupError("Managed observer is closed")
            if self._thread is not None:
                return
            self._thread = threading.Thread(target=self._pump, args=(runner, snapshot_callback, interval_seconds),
                                            name="swarm-managed-observer", daemon=True)
            self._thread.start()

    def _pump(self, runner, snapshot_callback, interval):
        previous = None
        while not self._stop.is_set():
            try:
                snapshot = snapshot_callback()
                run = snapshot["run"]
                binding = self.runtime.binding
                if (run["id"] != binding.run_id or run["epoch"] != binding.epoch
                        or tuple(run[name] for name in ("tenant_id", "owner_id", "project_id", "session_id"))
                        != (binding.tenant_id, binding.owner_id, binding.local_project_id, binding.session_id)):
                    raise ScopeDenied("Managed observation scope changed")
                terminal = run["state"] in {"completed", "cancelled", "failed", "recovery_required"}
                if self._stop.is_set():
                    break
                if not self.runtime.journal.inspect()["remote_binding_id"]:
                    if terminal:
                        raise ManagedSetupError("Historical managed run requires explicit recovery")
                    self.runtime.register()
                projection = metadata_projection(snapshot)
                encoded = json.dumps(projection, sort_keys=True, separators=(",", ":"))
                if not self._stop.is_set() and previous != encoded:
                    self.runtime.report(hashlib.sha256(encoded.encode()).hexdigest(), projection)
                    previous = encoded
                if self._stop.is_set():
                    break
                if not terminal:
                    self.runtime.poll_controls(runner)
                if self._stop.is_set():
                    break
                observed = self.runtime.flush(maximum=8)
                with self._lock:
                    self._connection_state = "unavailable" if observed["unavailable"] else "connected"
            except Exception:
                # No response body, config path, private key, or backend error
                # enters the browser or the observer's generic status.
                with self._lock:
                    self._connection_state = "unavailable"
            # Effect cleanup is an independent observation stream. A revoked
            # policy/control endpoint must not prevent its late delivery.
            with self._lock:
                effects = self._effects
            if effects is not None and not self._stop.is_set():
                try:
                    observed = effects.flush(maximum=8)
                    if observed["unavailable"]:
                        with self._lock:
                            self._connection_state = "unavailable"
                except Exception:
                    with self._lock:
                        self._connection_state = "unavailable"
            if self._stop.wait(interval):
                break

    def close(self, timeout: float = 0) -> bool:
        """Prevent future ticks; report whether this observer actually joined."""
        if type(timeout) not in (int, float) or not 0 <= timeout <= 5:
            raise ValueError("Managed observer close timeout is invalid")
        with self._lock:
            self._closed = True
            self._stop.set()
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)
        return thread is None or not thread.is_alive()


class ManagedDesktop:
    """Operator-captured setup bridge; construction never contacts governance."""

    def __init__(self, config: ManagedDesktopConfig, *, client_factory=HostChannelClient, runtime_factory=ManagedRuntime):
        self.config = config
        self._client = client_factory(config.transport)
        self._runtime_factory = runtime_factory
        self._lock = threading.Lock()
        self._attachments = {}
        self._closed = False

    def scope(self, personal_scope: Scope, workspace: str) -> Scope:
        """Construct a new organization scope only for the configured local owner."""
        if (personal_scope.tenant_id != f"personal:{personal_scope.owner_id}"
                or personal_scope.owner_id != self.config.local_owner_id
                or _canonical_path(workspace) != _canonical_path(self.config.workspace)):
            raise ScopeDenied("Managed setup does not match this local owner and workspace")
        expected_project = hashlib.sha256(_canonical_path(workspace).encode()).hexdigest()
        if personal_scope.project_id != expected_project:
            raise ScopeDenied("Managed setup does not match this local project")
        return Scope(self.config.tenant_id, self.config.owner_id, personal_scope.project_id, personal_scope.session_id)

    def attach(self, authority: RunAuthority, workspace: str, state_root: str | Path) -> ManagedAttachment:
        """Attach only a new run/epoch; existing files require explicit recovery."""
        expected = self.scope(Scope.personal(self.config.local_owner_id, authority.scope.project_id,
                                            authority.scope.session_id), workspace)
        if authority.scope != expected:
            raise ScopeDenied("Managed run does not match the configured organization")
        key = (authority.run_id, authority.epoch)
        with self._lock:
            if self._closed:
                raise ManagedSetupError("Managed setup is closed")
            if key in self._attachments:
                existing = self._attachments[key]
                captured = existing.runtime.binding
                if (captured.tenant_id, captured.owner_id, captured.local_project_id, captured.session_id) != authority.scope.values():
                    raise ScopeDenied("Managed attachment belongs to another captured session")
                return existing
            identity = hashlib.sha256(json.dumps(key, separators=(",", ":")).encode()).hexdigest()
            path = Path(state_root).resolve() / "swarm" / "managed" / f"{identity}.sqlite"
            if path.is_relative_to(Path(workspace).resolve()):
                raise ManagedSetupError("Managed state must remain outside the workspace")
            if path.exists():
                raise ManagedSetupError("Retained managed state requires explicit recovery; authority was not renewed")
            binding = ManagedBinding(self.config.transport.endpoint, self.config.transport.certificate_sha256,
                self.config.tenant_id, self.config.project_id, self.config.host_id, self.config.host_generation,
                self.config.owner_id, authority.scope.project_id, authority.scope.session_id, authority.run_id, authority.epoch)
            journal = ManagedJournal(path, binding)
            result = ManagedAttachment(self._runtime_factory(journal, self._client, policy_revision=self.config.policy_revision))
            self._attachments[key] = result
            return result

    def configured_view(self) -> dict:
        """No certificate/config paths or effective-policy guesses are returned."""
        return {"available": True, "mode": "managed", "tenant_id": self.config.tenant_id,
                "project_id": self.config.project_id, "host_id": self.config.host_id,
                "offline_request_allowance": 0, "effective_policy": None}

    def open_history(self, scope: Scope, run_id: str, epoch: int, workspace: str,
                     state_root: str | Path) -> ManagedJournal:
        """Open an exact existing journal for observation-only recovery callers.

        This performs no network, lease renewal, fencing, or history conversion.
        The recovery facade separately validates fresh local supervisor authority
        before using the journal's trusted observation APIs.
        """
        expected = self.scope(Scope.personal(self.config.local_owner_id, scope.project_id, scope.session_id), workspace)
        if scope != expected or type(epoch) is not int or epoch < 1:
            raise ScopeDenied("Managed history does not match the configured organization")
        identity = hashlib.sha256(json.dumps((run_id, epoch), separators=(",", ":")).encode()).hexdigest()
        path = Path(state_root).resolve() / "swarm" / "managed" / f"{identity}.sqlite"
        if path.is_relative_to(Path(workspace).resolve()) or not path.is_file():
            raise ManagedSetupError("Managed history is unavailable")
        binding = ManagedBinding(self.config.transport.endpoint, self.config.transport.certificate_sha256,
            self.config.tenant_id, self.config.project_id, self.config.host_id, self.config.host_generation,
            self.config.owner_id, scope.project_id, scope.session_id, run_id, epoch)
        return ManagedJournal(path, binding)

    def close(self) -> bool:
        """Close observation pumps without claiming workers or the server stopped."""
        with self._lock:
            self._closed = True
            attachments = list(self._attachments.values())
        return all([attachment.close() for attachment in attachments])
