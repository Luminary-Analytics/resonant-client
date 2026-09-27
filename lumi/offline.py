"""Offline mode: Lumi reaches only this computer and the hosts you allow.

Settings > Offline mode (``offline.enabled``) or an organization's policy
turns it on, for air-gapped networks and for organizations that allow no
traffic to the internet. Lumi then works with models on this computer (Ollama,
EXO) or on an inference server the organization runs, and every outbound
connection Lumi makes is checked before it starts. Only these are reachable:

* this computer: ``localhost``, the loopback addresses (``127.0.0.0/8``,
  ``::1`` and IPv4-mapped loopback), the unspecified addresses (``0.0.0.0``,
  ``::``, which never leave the computer) and this computer's own name;
* the hosts in ``offline.allowed_hosts``: a name (``llm.corp.example``), every
  name under a domain (``*.corp.example``), an IP address, or a network
  (``10.20.0.0/16``).

Whether a host is this computer is decided from the name as written, never by
looking it up: ``localhost.example.com`` or ``127.0.0.1.nip.io`` is somewhere
else, whatever it resolves to. A name that isn't a plain host name (``%``,
``/``, ``@``, control characters) is never reachable.

Where the check runs (see docs/offline.md):

* ``net.client_options``, the options Lumi's HTTP clients are built with,
  adds ``request_hook``: each request, redirects included, is checked before
  it connects. The caller names its feature for the message. A new outbound
  client must use it.
* where an address leaves Lumi's process: Git for capability packs
  (engine/pack_install.py) and pull requests (engine/github_tools.py), Lumi's
  browser (engine/browser.py starts Chrome so it can reach only these hosts),
  the update feed WinSparkle reads (update_channels.py), the sign-in page
  Lumi Cloud opens (cloud.py) and Azure's own sign-in (auth_tokens.py);
* model requests: a turn refuses a provider it can't reach, including Codex,
  Claude Code and extension providers, whose own processes Lumi can't check
  (``backend_refusal``, Session and request_purpose), the model picker hides
  them (``provider_refusal``) and the agent's browser tools refuse other hosts
  and other computers' files (``tool_refusal``);
* a backstop for Lumi's Python process (``sys.addaudithook``): once offline
  mode has been on, a host name lookup through Python's socket module
  (``getaddrinfo``, ``gethostbyname``, ``gethostbyaddr``, ``getnameinfo``) for
  anything else fails at once, and so does a connection or datagram to a host
  given by name. It covers the clients not built with ``client_options``
  (Ollama's own API, provider catalogs, the chat gateway), with a generic
  message. It can't see native code that resolves names itself or other
  programs Lumi starts; those are checked where they start.

A blocked call fails at once with ``Offline mode: <feature> needs <host>;
allow it or turn offline mode off.``, never after a timeout.

Settings are read through ``SettingsManager.get``, so an organization's
policy wins. When the policy turns offline mode on, only the hosts the policy
allows are reachable: a person's own ``allowed_hosts`` don't apply. A policy
that exists but can't be used (``policy.load`` reports an error) keeps offline
mode on with no allowed hosts until it's fixed: a mistake in it must never
read as "offline mode off".
"""

from __future__ import annotations

import json
import logging
import re
import socket
import sys
import threading
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import httpx

from . import offline_rules
from .offline_rules import (  # noqa: F401 - the rules are part of this module's interface
    MAX_ALLOWED_HOSTS,
    SECTION,
    _address,
    _compile,
    _matches,
    _Rule,
    _this_computer,
    is_host_name,
    is_local_host,
    normalize_host,
    parse_allowed_hosts,
    validate_policy_settings,
    validate_setting,
)

logger = logging.getLogger(__name__)

# Providers whose requests leave Lumi's process: Lumi can't check where they
# connect, so offline mode refuses them whatever the allowed hosts say. The
# host names what they need first.
CLI_PROVIDERS = {"codex": ("Codex", "chatgpt.com"), "claude-code": ("Claude Code", "api.anthropic.com")}
# The address that Lumi's browser sends everything it may not reach to: a
# closed port on this computer, so a page's requests fail at once.
_BLACKHOLE_PROXY = "http://127.0.0.1:9"
# Chrome on Windows opens file:// addresses that name another computer as
# network shares (tool_refusal); elsewhere a file: path is on this computer.
_WINDOWS = sys.platform == "win32"
_DRIVE = re.compile(r"[A-Za-z][:|](?:[/?#]|$)")
# What a refusal says while the organization's policy can't be used (_fail_closed).
_POLICY_ERROR_NOTE = "Your organization's policy can't be used, so offline mode stays on until your administrator fixes it."


class OfflineBlocked(httpx.ConnectError):
    """A request offline mode refused before it connected; the message says why.

    An ``httpx.ConnectError``, so code that already treats an unreachable host
    gracefully does so here too; ``message_for`` finds the message to show.
    """

    def __init__(self, feature: str, host: str, *, request: httpx.Request | None = None, text: str = "") -> None:
        self.feature = feature
        self.host = host
        super().__init__(text or message(feature, host), request=request)


class BlockedLookup(socket.gaierror):
    """A host name lookup the process-wide backstop refused (``_audit``).

    A ``socket.gaierror``, so every library handles it as a name that didn't
    resolve, at once, with this message.
    """


@dataclass(frozen=True)
class OfflineConfig:
    """Offline mode as it applies now, and who decided it."""

    enabled: bool = False
    allowed_hosts: tuple[str, ...] = ()
    managed_by: str = ""  # the organization whose policy sets offline.*
    locked: tuple[str, ...] = ()  # which of enabled / allowed_hosts the policy sets
    problems: tuple[str, ...] = field(default=())  # saved entries that were ignored, and why
    # Why the organization's policy can't be used; offline mode is then on with no allowed hosts.
    policy_error: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"enabled": self.enabled, "allowed_hosts": list(self.allowed_hosts), "managed_by": self.managed_by,
                "locked": list(self.locked), "problems": list(self.problems), "policy_error": self.policy_error}


@dataclass(frozen=True)
class _State:
    config: OfflineConfig
    rules: tuple[_Rule, ...]


_lock = threading.Lock()
_state = _State(OfflineConfig(), ())
_backstop_installed = False
_listeners: list[Callable[[OfflineConfig], None]] = []


# ── Hosts ──────────────────────────────────────────────────────────────────


def _text(host: Any) -> str:
    if isinstance(host, (bytes, bytearray)):
        return bytes(host).decode("latin-1")
    return str(host if host is not None else "")


def _readable(host: Any) -> bool:
    """False for a host with a control character, space or DEL, which no resolver should see."""
    return not any(ord(ch) < 0x21 or ord(ch) == 0x7F for ch in _text(host))


def _reachable(host: Any, rules: tuple[_Rule, ...]) -> bool:
    """Whether ``host`` is this computer or matches ``rules``; a name that isn't plain never is."""
    if not _readable(host):
        return False
    name = normalize_host(_text(host))
    if is_local_host(name):
        return True
    if _address(name) is None and not is_host_name(name):
        return False
    return _matches(name, rules)


def _shown(host: Any) -> str:
    """A host as a message shows it."""
    return normalize_host(_text(host)) if _readable(host) else "a host name Lumi can't read"


def host_allowed(host: Any, config: OfflineConfig | None = None) -> bool:
    """Whether offline mode lets Lumi reach ``host`` (always True while it is off)."""
    state = _state
    if config is not None and config is not state.config:
        state = _State(config, _compile(config.allowed_hosts))
    if not state.config.enabled:
        return True
    return _reachable(host, state.rules)


# ── Configuration ──────────────────────────────────────────────────────────


def _resolve(enabled: Any, hosts: Any, locked: dict[str, Any], organization: str) -> OfflineConfig:
    """The config from a person's values and the policy's locks (already applied to both)."""
    problems: list[str] = []
    if "enabled" in locked and "allowed_hosts" not in locked:
        # The organization turns offline mode on or off; it alone says what's reachable.
        allowed: tuple[str, ...] = ()
        if enabled is True and hosts:
            problems.append(f"{organization}'s policy turns offline mode on, so only the hosts it allows are "
                            "reachable; the hosts listed here don't apply.")
    else:
        entries = hosts.splitlines() if isinstance(hosts, str) else hosts if isinstance(hosts, (list, tuple)) else []
        kept: list[str] = []
        for entry in entries:
            try:
                kept.extend(parse_allowed_hosts([entry]))
            except ValueError as exc:  # a hand-edited settings.json: fewer hosts, never more
                problems.append(f"Ignored an allowed host: {exc}")
        allowed = tuple(dict.fromkeys(kept))[:MAX_ALLOWED_HOSTS]
    return OfflineConfig(enabled=enabled is True, allowed_hosts=allowed,
                         managed_by=organization if locked else "", locked=tuple(sorted(locked)),
                         problems=tuple(problems))


def _policy_state() -> Any:
    from . import policy

    try:
        return policy.load()
    except Exception as exc:  # policy.load never raises; be as careful here, and fail closed
        return policy.PolicyState(error=f"The organization policy couldn't be loaded: {exc}")


def _fail_closed(error: str) -> OfflineConfig:
    """Offline mode while the organization's policy exists but can't be used.

    Whether that policy turns offline mode on can't be known, so it is on,
    reaching only this computer, until the policy is fixed; a person's
    settings don't turn it off.
    """
    return OfflineConfig(enabled=True, allowed_hosts=(), managed_by="your organization",
                         locked=("allowed_hosts", "enabled"),
                         problems=(f"{error} Until it's fixed, offline mode stays on and Lumi reaches only this "
                                   "computer.",),
                         policy_error=error)


def from_settings(settings: Any) -> OfflineConfig:
    """Offline mode from a SettingsManager (policy locks win) or any ``get``-alike."""
    state = _policy_state()
    if getattr(state, "error", ""):
        return _fail_closed(state.error)
    if settings is None:
        return OfflineConfig()
    get = settings.get
    locked_values = getattr(settings, "locked_values", None)
    locked = dict(locked_values(SECTION)) if callable(locked_values) else {}
    policy = getattr(state, "policy", None)
    organization = policy.organization if (policy is not None and locked) else ""
    return _resolve(get(SECTION, "enabled", False), get(SECTION, "allowed_hosts", []) or [], locked, organization)


def read(settings_path: Path | None = None, policy_state: Any = None) -> OfflineConfig:
    """Offline mode from settings.json and the policy, without a SettingsManager.

    For startup code that runs before the app builds its settings (the
    updater), like update_channels.read.
    """
    from .paths import state_home

    path = settings_path or state_home() / "settings.json"
    try:
        section = json.loads(path.read_text(encoding="utf-8")).get(SECTION)
        stored = section if isinstance(section, dict) else {}
    except (OSError, ValueError, AttributeError):
        stored = {}
    state = policy_state if policy_state is not None else _policy_state()
    if getattr(state, "error", ""):
        return _fail_closed(state.error)
    policy = getattr(state, "policy", None)
    locked = {name.split(".", 1)[1]: value for name, value in (policy.settings if policy else {}).items()
              if name.startswith(SECTION + ".")}
    enabled = locked.get("enabled", stored.get("enabled", False))
    hosts = locked.get("allowed_hosts", stored.get("allowed_hosts", []))
    return _resolve(enabled, hosts, locked, policy.organization if (policy and locked) else "")


def configure(settings: Any) -> OfflineConfig:
    """Apply offline mode from Settings (and the policy) to this process; see net.configure."""
    config = from_settings(settings)
    _set(config)
    return config


def _set(config: OfflineConfig) -> None:
    global _state
    with _lock:
        previous = _state.config
        _state = _State(config, _compile(config.allowed_hosts))
        listeners = list(_listeners)
    if config.enabled:
        _install_backstop()
    if config.enabled != previous.enabled:
        logger.info("Offline mode is %s%s", "on" if config.enabled else "off",
                    f" (managed by {config.managed_by})" if config.managed_by else "")
    if (config.enabled, config.allowed_hosts) != (previous.enabled, previous.allowed_hosts):
        for listener in listeners:
            try:
                listener(config)
            except Exception:
                logger.exception("Applying the new offline mode rules failed")


def add_listener(listener: Callable[[OfflineConfig], None]) -> None:
    """Call ``listener(config)`` when what is reachable changes.

    For what was set up under the old rules and would keep connecting:
    engine/browser.py closes the Chrome it started with other rules at once.
    """
    with _lock:
        if listener not in _listeners:
            _listeners.append(listener)


def current() -> OfflineConfig:
    return _state.config


def enabled() -> bool:
    return _state.config.enabled


def set_for_tests(config: OfflineConfig | None = None, **fields: Any) -> OfflineConfig:
    """Apply a config directly (tests and fixtures): ``set_for_tests(enabled=True, allowed_hosts=(...))``."""
    if config is None:
        hosts = fields.pop("allowed_hosts", ())
        config = OfflineConfig(allowed_hosts=parse_allowed_hosts(list(hosts)), **fields)
    _set(config)
    return config


def reset_for_tests() -> None:
    _set(OfflineConfig())
    offline_rules.reset_for_tests()


# ── Messages ───────────────────────────────────────────────────────────────


def message(feature: str, host: str, config: OfflineConfig | None = None) -> str:
    """``Offline mode: <feature> needs <host>; allow it or turn offline mode off.``"""
    config = config or current()
    if config.policy_error:
        return f"Offline mode: {feature} needs {host or 'another computer'}. {_POLICY_ERROR_NOTE}"
    text = f"Offline mode: {feature} needs {host or 'another computer'}; allow it or turn offline mode off."
    if config.managed_by:
        text += f" {config.managed_by}'s policy manages offline mode, so ask your administrator."
    return text


def unchecked(what: str, instead: str = "choose a local model") -> str:
    """The refusal for something whose connections Lumi can't see: ``what`` says why, ``instead`` what to do."""
    config = current()
    if config.policy_error:
        return f"Offline mode: {what}, which offline mode can't check; {instead.rstrip(',')}. {_POLICY_ERROR_NOTE}"
    text = f"Offline mode: {what}, which offline mode can't check; {instead} or turn offline mode off."
    if config.managed_by:
        text += f" {config.managed_by}'s policy manages offline mode."
    return text


def url_host(url: str) -> str:
    """The host an address points at, or ValueError when that isn't clear.

    Stricter than browsers and Git: a backslash, whitespace, control character
    or user name makes the address ambiguous between parsers, so it is refused
    rather than guessed at.
    """
    text = str(url or "")
    if not text or any(ch in text for ch in "\\ \t\r\n") or any(ord(ch) < 0x20 or ord(ch) == 0x7f for ch in text):
        raise ValueError("an address Lumi can't read")
    try:
        parts = urllib.parse.urlsplit(text)
        host = parts.hostname
        parts.port  # noqa: B018 - a malformed port raises ValueError
    except ValueError:
        raise ValueError("an address Lumi can't read") from None
    if "@" in parts.netloc or not host:
        raise ValueError("an address Lumi can't read")
    return normalize_host(host)


def refusal(url: str, feature: str, config: OfflineConfig | None = None) -> str:
    """Why offline mode refuses ``url`` for ``feature``, or ``""`` when it's reachable (or off).

    ``config`` is one read before the app configured offline mode (``read``),
    for startup code; otherwise the one in force applies.
    """
    config = config or current()
    if not config.enabled:
        return ""
    try:
        host = url_host(url)
    except ValueError as exc:
        return message(feature, str(exc), config)
    return "" if host_allowed(host, config) else message(feature, host, config)


def check_url(url: str, feature: str) -> None:
    """Raise OfflineBlocked unless offline mode lets ``feature`` reach ``url``."""
    text = refusal(url, feature)
    if text:
        try:
            host = url_host(url)
        except ValueError:
            host = ""
        raise OfflineBlocked(feature, host, text=text)


def check_host(host: str, feature: str) -> None:
    """Raise OfflineBlocked unless offline mode lets ``feature`` reach ``host``."""
    if not host_allowed(host):
        raise OfflineBlocked(feature, _shown(host))


def message_for(exc: BaseException | None) -> str:
    """The offline mode message behind ``exc`` (or what caused it), else ``""``."""
    seen = 0
    while exc is not None and seen < 12:
        if isinstance(exc, (OfflineBlocked, BlockedLookup)):
            return str(exc)
        exc = exc.__cause__ or exc.__context__
        seen += 1
    return ""


def request_hook(feature: str) -> Callable[[httpx.Request], None]:
    """An httpx request hook: refuse a request offline mode doesn't allow, before it connects."""

    def check(request: httpx.Request) -> None:
        if not _state.config.enabled:
            return
        try:
            host = request.url.raw_host.decode("ascii")
        except (AttributeError, UnicodeDecodeError):
            host = request.url.host
        if not host_allowed(host):
            raise OfflineBlocked(feature, _shown(host), request=request)

    return check


# ── Providers, tools and the browser ──────────────────────────────────────


def provider_refusal(provider: str, url: str = "", *, label: str = "") -> str:
    """Why offline mode hides a provider in the model picker, or ``""``."""
    if not enabled():
        return ""
    if provider in CLI_PROVIDERS:
        name, host = CLI_PROVIDERS[provider]
        return unchecked(f"{label or name} needs {host} and runs as its own program")
    if not url:
        return ""
    return refusal(url, label or provider)


def extension_refusal(label: str) -> str:
    """Why offline mode refuses a provider a capability pack runs (``""`` while it is off)."""
    if not enabled():
        return ""
    return unchecked(f"{label} runs as a process from a capability pack and makes its own connections")


def backend_refusal(backend: Any) -> str:
    """Why offline mode refuses a model request to ``backend``, or ``""``.

    Codex, Claude Code and extension providers make their requests from their
    own processes, so they are refused whatever the allowed hosts say.
    """
    if backend is None or not enabled():
        return ""
    name = str(getattr(backend, "name", "") or "")
    label = str(getattr(backend, "PROVIDER_LABEL", "") or "")
    if name in CLI_PROVIDERS:
        return provider_refusal(name, label=label)
    connection = getattr(backend, "connection", None)
    if isinstance(connection, dict) and connection.get("type") == "extension":
        return extension_refusal(label or str(connection.get("name") or name))
    if isinstance(connection, dict) and connection.get("type"):
        from .connections import offline_refusal

        reason = offline_refusal(connection)  # its endpoint and where it signs in
        if reason:
            return reason
    url = str(getattr(backend, "base_url", "") or getattr(backend, "url", "") or "")
    if not url:
        return ""  # a provider without an endpoint (tests' fakes); its client still checks
    return refusal(url, label or name or "this provider")


def browser_url(url: str) -> str:
    """The address Lumi's browser opens for ``url`` (engine/browser.py adds https://)."""
    text = str(url or "").strip()
    if not text.startswith(("http://", "https://", "about:", "file://")):
        return "https://" + text
    return text


def file_url_host(url: str) -> str:
    """The other computer a ``file:`` address names, or ``""`` for a file on this one; ValueError if unclear.

    Chrome on Windows opens ``file://host/share``, ``file:////host/share`` and
    ``file:/host/share`` as network shares, through Windows' own connections,
    which Lumi's browser rules don't cover. This computer's files are
    ``file:///path``, ``file://localhost/path`` and a drive (``file:///C:/``);
    outside Windows a path with no host (``file:/etc/hosts``) is too.
    """
    text = str(url or "")
    if not _readable(text.replace(" ", "")) or re.search(r"%(?:2f|5c)", text, re.IGNORECASE):
        raise ValueError("an address Lumi can't read")  # tabs and line breaks vanish; %2F decodes to a slash
    rest = text.split(":", 1)[1].replace("\\", "/") if ":" in text else ""
    if re.match(r"//localhost(?=[/?#]|$)", rest, re.IGNORECASE):
        rest = "//" + rest[len("//localhost"):]  # file://localhost/path is file:///path
    slashes = len(rest) - len(rest.lstrip("/"))
    after = rest[slashes:]
    first = re.split(r"[/?#]", after, maxsplit=1)[0]
    if slashes <= 3 and _DRIVE.match(after):
        return ""  # file:C:/x, file:/C:/x, file://C:/x, file:///C:/x
    if slashes == 3 or (slashes == 2 and not first) or (slashes < 2 and not _WINDOWS):
        return ""
    return urllib.parse.unquote(first) or "another computer"


def file_refusal(url: str, feature: str = "opening a file", config: OfflineConfig | None = None) -> str:
    """Why offline mode refuses a ``file:`` address (one on another computer), or ``""``."""
    config = config or current()
    if not config.enabled:
        return ""
    try:
        host = file_url_host(url)
    except ValueError as exc:
        return message(feature, str(exc), config)
    if not host or host_allowed(host, config):
        return ""
    return message(f"{feature} from another computer", _shown(host), config)


def tool_refusal(tool_name: str, arguments: Any) -> str:
    """Why offline mode refuses one of the agent's tool calls, or ``""``.

    The browser tools that open an address are checked here, so the model
    learns why; Lumi's browser itself can reach only allowed hosts
    (``chrome_arguments``), and a ``file:`` address only files on this
    computer or an allowed host (``file_refusal``). ``open_application`` may
    not open an address, which the default browser or another program would
    reach unchecked. Tools whose requests go through Lumi's HTTP clients (pull
    requests, issue trackers, MCP servers) are refused by those clients, with
    the same message.
    """
    if not enabled():
        return ""
    args = arguments if isinstance(arguments, dict) else {}
    if tool_name == "open_application":
        name = str(args.get("name") or "").strip()
        if not (re.match(r"[A-Za-z][A-Za-z0-9+.-]+:", name) or name.replace("\\", "/").startswith("//")):
            return ""
        return unchecked("open_application would open that address in another program",
                         "use the browser tools, which reach only allowed hosts,")
    if tool_name not in {"browser_navigate", "browser_tabs"}:
        return ""
    url = str(args.get("url") or "").strip()
    if not url or (tool_name == "browser_tabs" and str(args.get("action") or "").lower() != "new"):
        return ""
    target = browser_url(url)
    if target.lower().startswith("about:"):
        return ""
    if target.lower().startswith("file:"):
        return file_refusal(target)
    return refusal(target, "browsing")


def chrome_arguments() -> list[str]:
    """Chrome command-line switches that keep Lumi's browser to reachable hosts (empty when off).

    Everything not on the bypass list goes to a closed port on this computer,
    so it fails at once. ``<-loopback>`` removes Chrome's own bypass for
    loopback and link-local addresses (169.254.0.0/16, fe80::/10), so only
    this computer and the allowed hosts connect directly. WebRTC may not use
    UDP around the proxy.
    """
    state = _state
    if not state.config.enabled:
        return []
    bypass = ["<-loopback>", "localhost", "127.0.0.0/8", "[::1]", *sorted(_this_computer())]
    for entry in state.config.allowed_hosts:
        address = _address(entry) if "/" not in entry else None
        bypass.append(f"[{entry}]" if address is not None and address.version == 6 else entry)
    return [f"--proxy-server={_BLACKHOLE_PROXY}", "--proxy-bypass-list=" + ";".join(dict.fromkeys(bypass)),
            "--force-webrtc-ip-handling-policy=disable_non_proxied_udp"]


# ── The backstop ───────────────────────────────────────────────────────────


def _install_backstop() -> None:
    """Check host name lookups through Python's socket module from now on (the hook can't be removed).

    Installed the first time offline mode is on; while it is off again the
    hook returns at once.
    """
    global _backstop_installed
    with _lock:
        if _backstop_installed:
            return
        _backstop_installed = True
    sys.addaudithook(_audit)


# Lookups, whose first argument is the name or address looked up
# (gethostbyname_ex raises "socket.gethostbyname" too), and the calls whose
# second argument is the address a socket connects or sends to.
_LOOKUPS = frozenset({"socket.getaddrinfo", "socket.gethostbyname", "socket.gethostbyaddr"})
_SENDS = frozenset({"socket.connect", "socket.sendto", "socket.sendmsg"})


def _audit(event: str, args: tuple) -> None:
    """``sys.addaudithook`` hook: refuse a lookup of, or a connection to, a host offline mode doesn't allow.

    ``socket.getaddrinfo`` is what HTTP clients (httpx, urllib, asyncio) call
    before connecting, so it fails before anything is sent; so do
    ``gethostbyname`` and the reverse lookups ``gethostbyaddr`` and
    ``getnameinfo`` (its event doesn't say whether it will look the address
    up, so an address offline mode doesn't allow is refused either way). A
    ``connect`` or ``sendto`` given a name resolves it inside Python before the
    event, so there only the connection is refused. Connections straight to
    an IP address, with no lookup, are checked by Lumi's own clients
    (``request_hook``), not here: an allowed name's address isn't in the rules.
    """
    if not event.startswith("socket."):
        return
    state = _state
    if not state.config.enabled:
        return
    if event in _LOOKUPS:
        host = args[0] if args else None
    elif event == "socket.getnameinfo":
        address = args[0] if args else None
        host = address[0] if isinstance(address, tuple) and address else None
    elif event in _SENDS:
        address = args[1] if len(args) > 1 else None
        host = address[0] if isinstance(address, tuple) and address else None
        if not isinstance(host, (str, bytes, bytearray)) or _address(normalize_host(_text(host))) is not None:
            return
    else:
        return
    if not isinstance(host, (str, bytes, bytearray)) or not host:
        return  # no host (a local lookup), or a type Python itself refuses
    if _reachable(host, state.rules):
        return
    error = BlockedLookup(message("a network connection", _shown(host), state.config))
    error.host = _shown(host)
    raise error
