"""Read the SONN workspace identity and prepaid balance without generating tokens."""

from __future__ import annotations

import datetime
import json
import re
from urllib.parse import urlsplit, urlunsplit

import httpx

from .sonn import SonnBackend


def workspace_url(base_url: str) -> str:
    """Derive the workspace route only from SONN's documented project route."""
    parsed = urlsplit(SonnBackend.validate_base_url(base_url))
    match = re.fullmatch(r"(.*)/v1/workspace/projects/[a-zA-Z0-9_-]{8,80}/openai/v1", parsed.path)
    if not match:
        raise ValueError("Use the complete project URL from SONN's Connect an app page.")
    return urlunsplit((parsed.scheme, parsed.netloc, match[1] + "/v1/workspace", "", ""))


def read_account(api_key: str, *, base_url: str, transport=None) -> dict:
    """Return allowlisted account fields; never return credentials or project data."""
    api_key = str(api_key or "").strip()
    if not api_key or api_key == "YOUR_PRIVATE_INVITATION":
        raise ValueError("Enter your SONN private invitation in Settings → API keys to connect your account.")
    url = workspace_url(base_url)
    try:
        with httpx.Client(timeout=8.0, transport=transport, follow_redirects=False) as client:
            with client.stream("GET", url, headers={"Authorization": f"Bearer {api_key}"}) as response:
                if response.status_code in (401, 403):
                    raise ValueError("SONN could not verify your account. Check your private invitation.")
                if response.status_code != 200:
                    raise ValueError("SONN account details are unavailable. Try refreshing later.")
                payload = bytearray()
                for chunk in response.iter_bytes():
                    payload.extend(chunk)
                    if len(payload) > 2_000_000:
                        raise ValueError("SONN returned an oversized account response.")
        data = json.loads(payload)
    except httpx.HTTPError:
        raise ValueError("Cannot reach your SONN account. Check the connection and project URL.") from None
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise ValueError("SONN returned an invalid account response.") from None
    if not isinstance(data, dict) or not isinstance(data.get("identity"), dict):
        raise ValueError("SONN returned an invalid account identity.")
    user = data["identity"].get("user")
    if not isinstance(user, str) or not user.strip() or len(user) > 256 or api_key in user:
        raise ValueError("SONN returned an invalid account identity.")
    billing = data.get("billing")
    if not isinstance(billing, dict) or type(billing.get("enabled")) is not bool:
        raise ValueError("SONN returned invalid billing details.")
    clean = {"enabled": billing["enabled"]}
    for key in ("available_credit_microusd", "reserved_credit_microusd", "total_charged_microusd"):
        value = billing.get(key)
        # JS numbers must preserve integer money values; unavailable is not zero.
        clean[key] = value if type(value) is int and abs(value) <= 2**53 - 1 else None
    checkout = data.get("checkout")
    mode = checkout.get("mode") if isinstance(checkout, dict) else None
    return {"user": user.strip(), "billing": clean,
            "checkout_mode": mode if mode in {"test", "live"} else None,
            "checked_at": datetime.datetime.now(datetime.timezone.utc).isoformat()}
