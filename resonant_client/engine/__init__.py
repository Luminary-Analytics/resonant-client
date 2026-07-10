"""
Resonant Engine — the server-side brain.

Manages backends, tools, sessions, and the agentic loop.
Can run embedded (in-process with TUI) or as a standalone server.
"""

from .session import Session
from .tools import AGENT_TOOLS, execute_tool, get_tool_icon, ToolResult
from .agents import AgentType, AGENT_TYPES, get_agent_type, list_agent_types
from .model_prompts import (
    ModelPromptProfile,
    build_model_prompt,
    detect_model_family,
    get_model_prompt_profile,
)
from .clipboard import read_clipboard_image, image_to_base64, image_size_label
from . import browser, computer

__all__ = [
    "Session", "AGENT_TOOLS", "execute_tool", "get_tool_icon", "ToolResult",
    "AgentType", "AGENT_TYPES", "get_agent_type", "list_agent_types",
    "ModelPromptProfile", "build_model_prompt", "detect_model_family",
    "get_model_prompt_profile",
    "read_clipboard_image", "image_to_base64", "image_size_label",
    "browser", "computer",
]
