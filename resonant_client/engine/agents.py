"""
Agent type registry for sub-agent system.

Defines agent types with specific tool restrictions, models, and system prompts.
Inspired by Claude Code's Agent tool and opencode's task tool.
"""

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class AgentType:
    """Definition of a sub-agent type."""
    name: str
    description: str
    allowed_tools: list[str]
    system_prompt: str
    model: Optional[str] = None  # None = inherit from parent

    def filter_tools(self, all_tools: list[dict]) -> list[dict]:
        """Filter tool definitions to only those allowed for this agent."""
        return [
            t for t in all_tools
            if t.get("function", {}).get("name", "") in self.allowed_tools
        ]


# ── Built-in Agent Types ──────────────────────────────────────────────
# Modeled after Claude Code (build, explore, plan) + opencode (general)

AGENT_TYPES: dict[str, AgentType] = {
    "build": AgentType(
        name="build",
        description=(
            "Full coding agent with all tools. Use for tasks that require "
            "reading, writing, editing files, and running commands."
        ),
        allowed_tools=[
            "bash", "file_read", "file_write", "file_edit", "glob", "grep",
        ],
        system_prompt="""\
You are a sub-agent handling a specific coding task. You have full tool access.

RULES:
1. Focus on the specific task assigned to you.
2. Use tools immediately — don't describe what you would do, just do it.
3. Be concise in your text output. Let tool results speak.
4. When done, provide a clear summary of what you accomplished.
5. Do NOT ask questions — make reasonable assumptions and proceed.""",
    ),

    "explore": AgentType(
        name="explore",
        description=(
            "Fast read-only agent for codebase exploration. Can read files, "
            "search with grep/glob, and run non-destructive commands. "
            "Cannot write or edit files."
        ),
        allowed_tools=[
            "file_read", "glob", "grep", "bash",
        ],
        system_prompt="""\
You are a fast, read-only exploration agent. Your job is to find information quickly.

RULES:
1. Search efficiently — use glob to find files, grep to find patterns.
2. Read files to understand code structure and behavior.
3. You may use bash for non-destructive commands (ls, git log, etc.) but NEVER modify files.
4. Return a clear, concise summary of what you found.
5. Do NOT write or edit any files.""",
    ),

    "plan": AgentType(
        name="plan",
        description=(
            "Planning agent that analyzes code without modifying it. "
            "Reads files and searches to build a plan, but cannot write or edit."
        ),
        allowed_tools=[
            "file_read", "glob", "grep",
        ],
        system_prompt="""\
You are a planning agent. Analyze the codebase and produce a clear plan.

RULES:
1. Read files and search to understand the current state.
2. Produce a numbered plan with specific steps.
3. Mention which files need to change and how.
4. Flag risks or ambiguities.
5. Do NOT write or edit any files — only analyze and plan.""",
    ),
}


def get_agent_type(name: str) -> Optional[AgentType]:
    """Look up an agent type by name."""
    return AGENT_TYPES.get(name)


def list_agent_types() -> list[str]:
    """List available agent type names."""
    return list(AGENT_TYPES.keys())
