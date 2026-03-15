"""
Protocol helpers for communicating with the Resonant Engine API.

Extracted from resonant_engine/api.py so the client has no engine dependencies.
"""

import json
import re
import logging

logger = logging.getLogger(__name__)


def build_tool_system_prompt(tools: list) -> str:
    """
    Convert OpenAI tool definitions into a system prompt section that tells
    the decoder what tools are available and how to invoke them.
    """
    if not tools:
        return ""

    lines = [
        "\n\n# CRITICAL: You MUST use tools to perform actions\n\n",
        "You are an AGENTIC coding assistant. You DO NOT just explain code — you EXECUTE actions using tools.\n",
        "When the user asks you to create, edit, or write files, or run commands, you MUST use the tools below.\n",
        "NEVER just show code in a code block. ALWAYS use tool calls to actually create/edit files.\n\n",
        "## How to call a tool\n\n",
        "Output EXACTLY this format (you can call multiple tools in one response):\n\n",
        "<tool_call>\n",
        '{"name": "tool_name", "arguments": {"param1": "value1"}}\n',
        "</tool_call>\n\n",
        "Example — to create a file:\n",
        "<tool_call>\n",
        '{"name": "file_write", "arguments": {"path": "example.py", "content": "print(\'hello\')"}}\n',
        "</tool_call>\n\n",
        "RULES:\n",
        "- You MUST use <tool_call> tags. This is not optional.\n",
        "- The JSON inside must have \"name\" and \"arguments\" keys.\n",
        "- \"arguments\" must be a JSON object, not a string.\n",
        "- You may include a BRIEF explanation before or after tool calls.\n",
        "- Do NOT wrap tool_call blocks in markdown code fences.\n",
        "- ALWAYS prefer tool calls over showing code blocks.\n\n",
        "## Available Tools\n\n",
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

        lines.append(f"### {name}\n")
        if desc:
            lines.append(f"{desc}\n")
        if params:
            props = params.get("properties", {})
            required = params.get("required", [])
            if props:
                lines.append("Parameters:\n")
                for pname, pinfo in props.items():
                    req = " (required)" if pname in required else ""
                    ptype = pinfo.get("type", "any")
                    pdesc = pinfo.get("description", "")
                    lines.append(f"  - {pname} ({ptype}{req}): {pdesc}\n")
        lines.append("\n")

    return "".join(lines)


def parse_tool_calls(text: str) -> tuple:
    """
    Parse <tool_call>...</tool_call> blocks from model output.

    Returns (plain_text, list_of_tool_calls) where each tool call is
    {"name": str, "arguments": str (JSON)}.
    """
    pattern = r'<tool_call>\s*(.*?)\s*</tool_call>'
    matches = list(re.finditer(pattern, text, re.DOTALL))

    if not matches:
        return text, []

    tool_calls = []
    for match in matches:
        raw = match.group(1).strip()
        try:
            parsed = json.loads(raw)
            name = parsed.get("name", "")
            args = parsed.get("arguments", {})
            if isinstance(args, dict):
                args_str = json.dumps(args)
            else:
                args_str = str(args)
            tool_calls.append({"name": name, "arguments": args_str})
        except json.JSONDecodeError:
            logger.warning("Failed to parse tool call JSON: %s", raw)
            continue

    plain = re.sub(pattern, '', text, flags=re.DOTALL).strip()
    return plain, tool_calls
