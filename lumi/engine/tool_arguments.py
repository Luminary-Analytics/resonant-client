"""Decode, coerce, and validate model-generated tool arguments."""

from __future__ import annotations

import json
from typing import Any


class ToolArgumentError(ValueError):
    """Raised when a tool call does not satisfy its declared schema."""


def _function_definition(tool: dict) -> dict:
    if tool.get("type") == "function":
        return tool.get("function") or {}
    return tool


def _schema_for(tool_name: str, tool_definitions: list[dict]) -> dict:
    for tool in tool_definitions or []:
        function = _function_definition(tool)
        if function.get("name") == tool_name:
            return function.get("parameters") or function.get("inputSchema") or {}
    return {}


def _decode_json_layers(raw: Any) -> Any:
    value = raw
    for _ in range(3):
        if not isinstance(value, str):
            break
        text = value.strip()
        if not text:
            raise ToolArgumentError("arguments were empty")
        try:
            value = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ToolArgumentError(
                f"arguments are not valid JSON ({exc.msg} at character {exc.pos})"
            ) from exc
    return value


def _coerce_value(value: Any, expected_type: str) -> Any:
    if expected_type in {"object", "array"} and isinstance(value, str):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            return value
        return decoded
    if expected_type == "integer" and isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return value
    if expected_type == "number" and isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return value
    if expected_type == "boolean" and isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "false"}:
            return lowered == "true"
    return value


def _matches_type(value: Any, expected_type: str) -> bool:
    if expected_type == "string":
        return isinstance(value, str)
    if expected_type == "object":
        return isinstance(value, dict)
    if expected_type == "array":
        return isinstance(value, list)
    if expected_type == "boolean":
        return isinstance(value, bool)
    if expected_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected_type == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return True


def _placeholder(property_schema: dict) -> Any:
    if "enum" in property_schema and property_schema["enum"]:
        return property_schema["enum"][0]
    expected_type = property_schema.get("type", "string")
    return {
        "string": "value",
        "integer": 1,
        "number": 1.0,
        "boolean": True,
        "array": [],
        "object": {},
    }.get(expected_type, "value")


def expected_arguments_example(schema: dict) -> str:
    properties = schema.get("properties") or {}
    required = schema.get("required") or []
    names = list(required) or list(properties)[:3]
    example = {
        name: _placeholder(properties.get(name) or {})
        for name in names
    }
    return json.dumps(example, ensure_ascii=False, sort_keys=True)


def normalize_tool_arguments(
    tool_name: str,
    raw_arguments: Any,
    tool_definitions: list[dict],
) -> dict:
    """Return schema-valid arguments or a targeted model repair error."""
    schema = _schema_for(tool_name, tool_definitions)
    try:
        value = _decode_json_layers(raw_arguments)
    except ToolArgumentError as exc:
        example = expected_arguments_example(schema)
        raise ToolArgumentError(f"{exc}; expected arguments like {example}") from exc

    if not isinstance(value, dict):
        example = expected_arguments_example(schema)
        raise ToolArgumentError(
            f"arguments decoded to {type(value).__name__}, not an object; "
            f"expected arguments like {example}"
        )
    if not schema:
        return value

    properties = schema.get("properties") or {}
    normalized = dict(value)
    for name, property_schema in properties.items():
        if name not in normalized:
            continue
        expected_type = property_schema.get("type")
        if not expected_type:
            continue
        normalized[name] = _coerce_value(normalized[name], expected_type)
        if not _matches_type(normalized[name], expected_type):
            example = expected_arguments_example(schema)
            raise ToolArgumentError(
                f"argument '{name}' must be {expected_type}, got "
                f"{type(normalized[name]).__name__}; expected arguments like {example}"
            )
        allowed = property_schema.get("enum")
        if allowed and normalized[name] not in allowed:
            raise ToolArgumentError(
                f"argument '{name}' must be one of {allowed}, got {normalized[name]!r}"
            )

    missing = [name for name in schema.get("required", []) if name not in normalized]
    if missing:
        example = expected_arguments_example(schema)
        raise ToolArgumentError(
            f"missing required argument(s): {', '.join(missing)}; "
            f"expected arguments like {example}"
        )
    return normalized
