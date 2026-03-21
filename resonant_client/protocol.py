"""
Protocol helpers for communicating with the Resonant Engine API.

Extracted from resonant_engine/api.py so the client has no engine dependencies.
"""

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


def parse_tool_calls(text: str) -> tuple:
    """
    Parse <tool_call>...</tool_call> blocks from model output.

    Also handles models that omit the closing </tool_call> tag
    (e.g. GLM-4) by falling back to <tool_call> followed by JSON.

    Returns (plain_text, list_of_tool_calls) where each tool call is
    {"name": str, "arguments": str (JSON)}.
    """
    # Strip <think> blocks first (chain-of-thought reasoning)
    text = strip_think_tags(text)

    # Try closed tags first: <tool_call>...</tool_call>
    pattern_closed = r'<tool_call>\s*(.*?)\s*</tool_call>'
    matches = list(re.finditer(pattern_closed, text, re.DOTALL))

    # Fallback: <tool_call> followed by JSON (no closing tag)
    if not matches:
        pattern_open = r'<tool_call>\s*(\{.*?\})\s*(?:</tool_call>|$)'
        matches = list(re.finditer(pattern_open, text, re.DOTALL))

    if not matches:
        return text, []

    tool_calls = []
    for match in matches:
        raw = match.group(1).strip()
        parsed = _try_parse_tool_json(raw)
        if parsed:
            name = parsed.get("name", "")
            args = parsed.get("arguments", {})
            if isinstance(args, dict):
                args_str = json.dumps(args)
            else:
                args_str = str(args)
            tool_calls.append({"name": name, "arguments": args_str})
        else:
            # Fallback: try XML-style args from GLM
            # e.g. <arg_key>pattern</arg_key><arg_value>*/</arg_value>...
            xml_name = re.search(r'<name>(.*?)</name>', raw)
            xml_args = re.findall(r'<arg_key>(.*?)</arg_key>\s*<arg_value>(.*?)</arg_value>', raw, re.DOTALL)
            if xml_name and xml_args:
                name = xml_name.group(1).strip()
                args = {k.strip(): v.strip() for k, v in xml_args}
                tool_calls.append({"name": name, "arguments": json.dumps(args)})
            else:
                logger.warning("Failed to parse tool call JSON: %s", raw)

    # Remove all tool_call blocks (closed and open) from plain text
    plain = re.sub(r'<tool_call>.*?</tool_call>', '', text, flags=re.DOTALL)
    plain = re.sub(r'<tool_call>\s*\{.*', '', plain, flags=re.DOTALL)
    return plain.strip(), tool_calls
