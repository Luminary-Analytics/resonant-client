"""Normalized multimodal content parts and graceful text fallbacks."""

from __future__ import annotations

import base64
import json
from typing import Any, Iterable


SUPPORTED_PART_TYPES = {
    "text", "image", "audio", "video", "document", "file", "diagnostic",
}


def normalize_content(content: Any) -> list[dict[str, Any]]:
    """Return content as typed, serializable parts without losing evidence."""
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if not isinstance(content, list):
        return [{"type": "text", "text": str(content or "")}]

    normalized: list[dict[str, Any]] = []
    for raw in content:
        if not isinstance(raw, dict):
            normalized.append({"type": "text", "text": str(raw)})
            continue
        part = dict(raw)
        part_type = str(part.get("type") or "text").strip().lower()
        if part_type not in SUPPORTED_PART_TYPES:
            part = {
                "type": "diagnostic",
                "name": f"unsupported:{part_type}",
                "data": raw,
            }
        else:
            part["type"] = part_type
        if part["type"] == "text":
            part["text"] = str(part.get("text") or "")
        normalized.append(part)
    return normalized


def build_user_content(
    text: str,
    images: Iterable[tuple[bytes, str]] | None = None,
) -> str | list[dict[str, Any]]:
    """Build a normalized user payload while preserving legacy text scalars."""
    image_parts: list[dict[str, Any]] = []
    for image_bytes, media_type in images or ():
        image_parts.append({
            "type": "image",
            "media_type": str(media_type or "image/png"),
            "data": base64.b64encode(image_bytes).decode("ascii"),
        })
    if not image_parts:
        return str(text or "")
    return [*image_parts, {"type": "text", "text": str(text or "")}]


def _media_description(part: dict[str, Any]) -> str:
    for key in ("description", "caption", "alt_text", "transcript", "extracted_text"):
        value = str(part.get(key) or "").strip()
        if value:
            return value
    return ""


def text_fallback(part: dict[str, Any]) -> str:
    """Render one non-text part honestly for a text-only model."""
    part_type = str(part.get("type") or "diagnostic")
    description = _media_description(part)
    name = str(part.get("name") or part.get("path") or "").strip()
    media_type = str(part.get("media_type") or "").strip()
    label_bits = [part_type.capitalize()]
    if name:
        label_bits.append(name)
    if media_type:
        label_bits.append(media_type)
    label = ": ".join(label_bits[:2]) + (f" ({label_bits[2]})" if len(label_bits) > 2 else "")
    if description:
        return f"[{label}]\n{description}"
    if part_type == "diagnostic":
        data = part.get("data")
        try:
            rendered = json.dumps(data, ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError):
            rendered = str(data or "")
        return f"[{label}]\n{rendered}".strip()
    return (
        f"[{label} attached. No textual representation is available. "
        "Use a compatible multimodal model or a modality processor before relying on its contents.]"
    )


def content_text(content: Any, *, include_fallbacks: bool = True) -> str:
    """Extract ordered text, optionally representing every non-text part."""
    rendered: list[str] = []
    for part in normalize_content(content):
        if part["type"] == "text":
            text = str(part.get("text") or "").strip()
            if text:
                rendered.append(text)
        elif include_fallbacks:
            rendered.append(text_fallback(part))
    return "\n\n".join(rendered)


def ollama_message_content(content: Any, *, allow_images: bool) -> tuple[str, list[str]]:
    """Convert normalized parts to Ollama text plus its native image field."""
    text_parts: list[str] = []
    images: list[str] = []
    for part in normalize_content(content):
        if part["type"] == "text":
            text = str(part.get("text") or "").strip()
            if text:
                text_parts.append(text)
            continue
        if part["type"] == "image" and allow_images:
            data = str(part.get("data") or "").strip()
            if data:
                images.append(data)
            description = _media_description(part)
            if description:
                text_parts.append(f"[Image description]\n{description}")
            continue
        text_parts.append(text_fallback(part))
    return "\n\n".join(text_parts), images
