"""
Context Compression for Resonant Engine.

When conversation history grows too large, older messages are summarized
into a compact form while keeping recent turns intact.
"""

import logging
from typing import Optional

logger = logging.getLogger(__name__)

# Rough estimate: 1 token ≈ 4 chars
CHARS_PER_TOKEN = 4

# Default thresholds
DEFAULT_MAX_CONTEXT_TOKENS = 100_000
KEEP_RECENT_TURNS = 6  # Keep last N user+assistant pairs verbatim


def estimate_tokens(history: list) -> int:
    """Rough token count estimate from conversation history."""
    total_chars = 0
    for entry in history:
        content = entry.get("content", "")
        if isinstance(content, str):
            total_chars += len(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict):
                    total_chars += len(part.get("text", ""))
    return total_chars // CHARS_PER_TOKEN


def should_compress(history: list, max_tokens: int = DEFAULT_MAX_CONTEXT_TOKENS) -> bool:
    """Check if the conversation history needs compression."""
    if len(history) < KEEP_RECENT_TURNS * 2 + 4:
        return False
    return estimate_tokens(history) > max_tokens


def _extract_text(entry: dict) -> str:
    """Extract text content from a history entry."""
    content = entry.get("content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                parts.append(part["text"])
        return " ".join(parts)
    return str(content)


def _build_summary_prompt(old_messages: list) -> str:
    """Build a prompt asking the LLM to summarize older conversation."""
    lines = []
    for entry in old_messages:
        role = entry.get("role", "unknown")
        text = _extract_text(entry)
        if not text:
            continue
        # Truncate very long entries
        if len(text) > 2000:
            text = text[:2000] + "..."
        if role == "user":
            lines.append(f"User: {text}")
        elif role == "assistant":
            lines.append(f"Assistant: {text}")
        elif role == "tool_call":
            name = entry.get("name", "")
            lines.append(f"[Tool call: {name}]")
        elif role == "tool_result":
            lines.append(f"[Tool result: {text[:500]}]")

    conversation_text = "\n".join(lines)

    return (
        "Summarize this conversation history concisely. "
        "Focus on: decisions made, files changed, key findings, current state of the task. "
        "Be specific about file paths and code changes. Keep it under 500 words.\n\n"
        f"{conversation_text}"
    )


def compress(
    session,
    backend=None,
    max_tokens: int = DEFAULT_MAX_CONTEXT_TOKENS,
) -> tuple[list, str]:
    """Compress conversation history by summarizing older messages.

    Args:
        session: Session object with conversation_history
        backend: Backend to use for summarization (defaults to session.backend)
        max_tokens: Target max context size

    Returns:
        (new_history, summary_text) — the compressed history and the summary that was generated
    """
    history = session.conversation_history
    backend = backend or session.backend

    if not should_compress(history, max_tokens):
        return history, ""

    # Split: older messages to summarize, recent to keep
    # Count backwards to find the split point (keep KEEP_RECENT_TURNS user messages)
    user_count = 0
    split_idx = len(history)
    for i in range(len(history) - 1, -1, -1):
        if history[i].get("role") == "user":
            user_count += 1
            if user_count >= KEEP_RECENT_TURNS:
                split_idx = i
                break

    if split_idx <= 2:
        return history, ""

    old_messages = history[:split_idx]
    recent_messages = history[split_idx:]

    # Use the backend to generate a summary
    summary_prompt = _build_summary_prompt(old_messages)
    try:
        summary = ""
        for event_type, data in backend.stream(
            user_msg=summary_prompt,
            conversation_history=[],
            instructions="You are a conversation summarizer. Be concise and factual.",
            tools=[],
            max_tokens=1024,
        ):
            if event_type == "text.delta":
                summary += data.get("delta", "")
            elif event_type == "done":
                break
    except Exception as e:
        logger.error(f"Compression failed: {e}")
        return history, ""

    if not summary.strip():
        return history, ""

    # Build compressed history
    compressed = [
        {
            "role": "assistant",
            "content": f"[Previous conversation summary]\n{summary.strip()}\n[End summary — recent messages follow]",
        }
    ]
    compressed.extend(recent_messages)

    logger.info(
        f"Compressed history: {len(history)} entries → {len(compressed)} entries, "
        f"~{estimate_tokens(history)} tokens → ~{estimate_tokens(compressed)} tokens"
    )

    return compressed, summary.strip()
