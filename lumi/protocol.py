"""
Protocol helpers for communicating with the Resonant Engine API.

Extracted from resonant_engine/api.py so the client has no engine dependencies.
"""

import html
import json
import re
import logging

logger = logging.getLogger(__name__)


_tool_prompt_cache = {}

def build_tool_system_prompt(tools: list) -> str:
    """
    Convert tool definitions into a compact system prompt.
    Results are cached by tool names so different tool sets aren't confused.
    """
    if not tools:
        return ""

    # Build a stable cache key from the sorted tool names
    tool_names = []
    for tool in tools:
        if isinstance(tool, dict) and tool.get("type") == "function":
            fn = tool.get("function", tool)
        elif isinstance(tool, dict):
            fn = tool
        else:
            continue
        tool_names.append(fn.get("name", "unknown"))
    cache_key = tuple(sorted(tool_names))
    if cache_key in _tool_prompt_cache:
        return _tool_prompt_cache[cache_key]

    lines = [
        "\n\n# Tool Use\n",
        "ALWAYS use tools to act. Never just show code blocks.\n",
        "Format: <tool_call>\n",
        '{"name": "tool_name", "arguments": {"param": "value"}}\n',
        "</tool_call>\n\n",
        "Rules: Use <tool_call> tags. JSON must have \"name\" and \"arguments\" (object). No markdown fences around tool calls.\n\n",
    ]

    for tool in tools:
        if isinstance(tool, dict) and tool.get("type") == "function":
            fn = tool.get("function", tool)
        elif isinstance(tool, dict):
            fn = tool
        else:
            continue

        name = fn.get("name", "unknown")
        desc = fn.get("description", "")
        params = fn.get("parameters", {})

        # Compact: name — description (params)
        param_parts = []
        if params:
            for pname, pinfo in params.get("properties", {}).items():
                req = "*" if pname in params.get("required", []) else ""
                param_parts.append(f"{pname}{req}")
        param_str = f"({', '.join(param_parts)})" if param_parts else ""
        # Truncate long descriptions
        short_desc = desc.split(".")[0] if desc else ""
        lines.append(f"- **{name}**{param_str}: {short_desc}\n")

    lines.append("\n")
    result = "".join(lines)
    _tool_prompt_cache[cache_key] = result
    return result


def strip_think_tags(text: str) -> str:
    """Remove <think>...</think> blocks (reasoning/chain-of-thought) from model output."""
    return re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL).strip()


def _try_parse_tool_json(raw: str) -> dict | None:
    """Try to parse JSON from a tool call block, fixing common LLM issues.

    Models like GLM often output:
    1. Windows paths with raw backslashes (D:\\Repos\\foo)
    2. Literal newlines inside JSON string values
    3. Unescaped special chars in code snippets
    """
    # Try as-is first (fast path)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass

    # Fix 1: Escape raw backslashes (not already part of valid JSON escapes)
    fixed = re.sub(
        r'\\(?!["\\/bfnrtu])',
        r'\\\\',
        raw,
    )
    try:
        return json.loads(fixed)
    except json.JSONDecodeError:
        pass

    # Fix 2: Also escape literal newlines/tabs inside JSON string values
    # Replace actual newlines with \n, tabs with \t (only inside strings)
    fixed2 = fixed.replace('\r\n', '\\n').replace('\r', '\\n').replace('\n', '\\n').replace('\t', '\\t')
    try:
        return json.loads(fixed2)
    except json.JSONDecodeError:
        pass

    # Fix 3: Try a more aggressive approach — extract name and arguments manually
    # Handles cases where the JSON is too malformed for standard parsing
    name_match = re.search(r'"name"\s*:\s*"([^"]+)"', raw)
    if name_match:
        name = name_match.group(1)
        # Try to extract the arguments object
        args_match = re.search(r'"arguments"\s*:\s*(\{.*)', raw, re.DOTALL)
        if args_match:
            args_raw = args_match.group(1)
            # Try to find matching brace
            depth = 0
            end = 0
            for i, ch in enumerate(args_raw):
                if ch == '{':
                    depth += 1
                elif ch == '}':
                    depth -= 1
                    if depth == 0:
                        end = i + 1
                        break
            if end > 0:
                args_str = args_raw[:end]
                # Apply same fixes to the args substring
                args_fixed = re.sub(r'\\(?!["\\/bfnrtu])', r'\\\\', args_str)
                args_fixed = args_fixed.replace('\r\n', '\\n').replace('\r', '\\n').replace('\n', '\\n').replace('\t', '\\t')
                try:
                    args = json.loads(args_fixed)
                    return {"name": name, "arguments": args}
                except json.JSONDecodeError:
                    # Last resort: just pass the raw args string
                    return {"name": name, "arguments": {"_raw": args_str}}

    return None


def parse_dsml_tool_calls(text: str) -> tuple[str, list[dict]]:
    """Recover DeepSeek DSML tool calls leaked as assistant text."""
    block_pattern = re.compile(
        r"<\|DSML\|tool_calls>(.*?)</\|DSML\|tool_calls>",
        flags=re.DOTALL | re.IGNORECASE,
    )
    invoke_pattern = re.compile(
        r"<\|DSML\|invoke\s+name=[\"']([^\"']+)[\"']>(.*?)"
        r"</\|DSML\|invoke>",
        flags=re.DOTALL | re.IGNORECASE,
    )
    parameter_pattern = re.compile(
        r"<\|DSML\|parameter\s+name=[\"']([^\"']+)[\"']"
        r"(?:\s+string=[\"'](true|false)[\"'])?>(.*?)"
        r"</\|DSML\|parameter>",
        flags=re.DOTALL | re.IGNORECASE,
    )

    calls: list[dict] = []
    for block in block_pattern.finditer(text or ""):
        for invoke in invoke_pattern.finditer(block.group(1)):
            arguments: dict = {}
            for parameter in parameter_pattern.finditer(invoke.group(2)):
                name, string_flag, raw_value = parameter.groups()
                value = html.unescape(raw_value.strip())
                if (string_flag or "").lower() != "true":
                    try:
                        value = json.loads(value)
                    except (json.JSONDecodeError, TypeError):
                        pass
                arguments[html.unescape(name)] = value
            calls.append({
                "name": html.unescape(invoke.group(1)),
                "arguments": json.dumps(arguments, ensure_ascii=False, sort_keys=True),
            })

    plain_text = block_pattern.sub("", text or "").strip()
    return plain_text, calls


def parse_tool_calls(text: str) -> tuple:
    """
    Parse <tool_call>...</tool_call> blocks from model output.

    Supports four formats:
    1. **Bare-tag form** (most models):
       `<tool_call>{"name": "X", "arguments": {...}}</tool_call>`
    2. **Bare-tag, no closing tag** (e.g. GLM-4):
       `<tool_call>{"name": "X", "arguments": {...}}`
    3. **Attribute form** (e.g. deepseek-v4-pro:cloud — found in
       v0.5.2 variance smoke):
       `<tool_call name="X">{"path":..., "content":...}</tool_call>`
       — name is an XML attribute; the body IS the arguments object
       directly (not wrapped in `{"name":..., "arguments":...}`).
    4. **GLM XML args form**:
       `<tool_call><name>X</name><arg_key>K</arg_key>...</tool_call>`

    Returns (plain_text, list_of_tool_calls) where each tool call is
    {"name": str, "arguments": str (JSON)}.
    """
    # Strip <think> blocks first (chain-of-thought reasoning)
    text = strip_think_tags(text)

    # v0.5.2a2 — unified pattern that captures the optional `name`
    # attribute. Matches forms 1 and 3 in one pass.
    pattern_with_attr = (
        r'<tool_call(?:\s+name="([^"]+)")?\s*>\s*(.*?)\s*</tool_call>'
    )
    matches = list(re.finditer(pattern_with_attr, text, re.DOTALL))

    # Fallback: open tag with no closing (form 2), bare or with attr.
    if not matches:
        pattern_open = (
            r'<tool_call(?:\s+name="([^"]+)")?\s*>\s*(\{.*?\})\s*(?:</tool_call>|$)'
        )
        matches = list(re.finditer(pattern_open, text, re.DOTALL))

    if not matches:
        return text, []

    tool_calls = []
    for match in matches:
        name_attr = match.group(1)  # may be None for forms 1, 2, 4
        raw = match.group(2).strip()
        parsed = _try_parse_tool_json(raw)
        if parsed and isinstance(parsed, dict):
            # Discriminate between forms 1/2 (body has name + arguments)
            # and form 3 (body is the arguments object directly, name
            # comes from the attribute).
            if "name" in parsed and "arguments" in parsed and not name_attr:
                # Form 1 / 2 — body wraps the call.
                name = parsed.get("name", "")
                args = parsed.get("arguments", {})
            elif name_attr:
                # Form 3 — pro variant. Name from attribute, body IS
                # the args object. This was the v0.5.2a2 GA-blocking
                # bug: without this branch, pro's implementer
                # silently emitted <tool_call name="file_write">{...}
                # as TEXT and the daemon went stuck.
                name = name_attr
                args = parsed
            elif "name" in parsed and "arguments" in parsed:
                # Defensive — both attr AND body name present (some
                # models hedge). Prefer the attribute since it's the
                # explicit choice.
                name = name_attr or parsed.get("name", "")
                args = parsed.get("arguments", {})
            else:
                # Body is a JSON object but neither form fits — log
                # and skip rather than mis-routing.
                logger.warning(
                    "tool_call body is JSON but doesn't match name+args "
                    "or name-attr form: %s", raw[:200],
                )
                continue
            args_str = json.dumps(args) if isinstance(args, dict) else str(args)
            tool_calls.append({"name": name, "arguments": args_str})
        else:
            # Fallback: try XML-style args from GLM (form 4)
            # e.g. <arg_key>pattern</arg_key><arg_value>*/</arg_value>...
            xml_name = re.search(r'<name>(.*?)</name>', raw)
            xml_args = re.findall(r'<arg_key>(.*?)</arg_key>\s*<arg_value>(.*?)</arg_value>', raw, re.DOTALL)
            if xml_name and xml_args:
                name = xml_name.group(1).strip()
                args = {k.strip(): v.strip() for k, v in xml_args}
                tool_calls.append({"name": name, "arguments": json.dumps(args)})
            elif name_attr:
                # Form 3 fallback when body isn't valid JSON — last
                # resort: use the attribute name with empty args. The
                # downstream tool dispatcher will surface the bad-args
                # error to the caller cleanly rather than silently
                # losing the call.
                logger.warning(
                    "tool_call has name=%r attr but body isn't JSON: %s",
                    name_attr, raw[:200],
                )
                tool_calls.append({"name": name_attr, "arguments": "{}"})
            else:
                logger.warning("Failed to parse tool call JSON: %s", raw)

    # Remove all tool_call blocks (closed and open, with or without
    # the `name="X"` attribute) from plain text.
    plain = re.sub(
        r'<tool_call(?:\s+name="[^"]+")?\s*>.*?</tool_call>',
        '', text, flags=re.DOTALL,
    )
    plain = re.sub(
        r'<tool_call(?:\s+name="[^"]+")?\s*>\s*\{.*',
        '', plain, flags=re.DOTALL,
    )
    return plain.strip(), tool_calls
