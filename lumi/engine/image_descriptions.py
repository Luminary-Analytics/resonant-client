"""Describe images for a chat model that can't see them.

When the chat model has no vision and Models for roles names a ``vision``
model, the session asks that model to describe each image in the
conversation once: pictures you attach and screenshots from tools. The
description is stored on the image with the model that wrote it
(``description``, ``described_by``), and text-only adapters send it in the
image's place (``content.text_fallback``). The image itself stays in the
history for models that can see it.

Without a vision model the chat model gets the usual notice that an image
is attached and can't be read, never a guess.
"""

from __future__ import annotations

import logging
from typing import Any, Iterator

logger = logging.getLogger(__name__)

# Bounds the extra requests one step can make; the rest wait for the next step.
MAX_PER_STEP = 6
# An image that failed this often keeps its notice instead of costing more requests.
MAX_ATTEMPTS = 2
PROMPT = (
    "Describe this image for a coding assistant that cannot see it. First transcribe every piece of "
    "visible text exactly: code, error messages, logs, labels, menus. Then describe the layout, the "
    "user interface elements and their state, and any chart, diagram or unusual detail. Be factual "
    "and complete; don't guess at anything that isn't visible."
)
INSTRUCTIONS = "You describe images accurately for someone who can't see them."


def pending(history: list[dict]) -> list[dict]:
    """Image parts in ``history`` without a description, oldest first.

    Returns the dicts themselves, so storing a description updates the history.
    """
    found: list[dict] = []
    for turn in history:
        if not isinstance(turn, dict):
            continue
        content = turn.get("content")
        if isinstance(content, list):
            found.extend(part for part in content
                         if isinstance(part, dict) and part.get("type") == "image" and _undescribed(part))
        image = turn.get("image")
        if turn.get("role") == "tool_result" and isinstance(image, dict) and _undescribed(image):
            found.append(image)
    return found


def _undescribed(image: dict) -> bool:
    return bool(image.get("data")) and not image.get("description") \
        and int(image.get("description_attempts") or 0) < MAX_ATTEMPTS


def describe(backend: Any, image: dict, *, usage_context: dict | None = None,
             cancel_event: Any = None) -> str:
    """Ask ``backend`` (a vision model) to describe one image; ``""`` if it can't."""
    from .request_purpose import auxiliary_stream

    part = {"type": "image", "media_type": str(image.get("media_type") or "image/png"), "data": image["data"]}
    options = {"cancel_event": cancel_event} if cancel_event is not None else {}
    text = ""
    try:
        for event_type, data in auxiliary_stream(
            backend, "image_description", usage_context=usage_context,
            user_msg=[part, {"type": "text", "text": PROMPT}], conversation_history=[],
            instructions=INSTRUCTIONS, tools=[], max_tokens=1500, **options,
        ):
            if event_type == "text.delta":
                text += data.get("delta", "")
            elif event_type in {"error", "cancelled"}:
                return ""
    except Exception:
        logger.warning("Describing an image failed", exc_info=True)
        return ""
    return text.strip()


def describe_pending(history: list[dict], backend: Any, label: str, *, usage_context: dict | None = None,
                     cancel_event: Any = None, limit: int = MAX_PER_STEP) -> Iterator[tuple[dict, bool]]:
    """Describe up to ``limit`` undescribed images; yields (image, described)."""
    for image in pending(history)[:limit]:
        if cancel_event is not None and getattr(cancel_event, "is_set", lambda: False)():
            return
        text = describe(backend, image, usage_context=usage_context, cancel_event=cancel_event)
        if text:
            image["description"] = text
            image["described_by"] = label
            image.pop("description_attempts", None)
        else:
            image["description_attempts"] = int(image.get("description_attempts") or 0) + 1
        yield image, bool(text)
