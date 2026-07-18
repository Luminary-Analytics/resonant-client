"""
Context Compression for Resonant Engine.

When conversation history grows too large, older messages are summarized
into a compact form while keeping recent turns intact.
"""

import logging

from ..capabilities import infer_model_capabilities

logger = logging.getLogger(__name__)

# Rough estimate: 1 token ≈ 4 chars
CHARS_PER_TOKEN = 4

# Default thresholds
DEFAULT_MAX_CONTEXT_TOKENS = 100_000
KEEP_RECENT_TURNS = 6  # Keep last N user+assistant pairs verbatim
CONTEXT_HEADROOM_RATIO = 0.875
MIN_OUTPUT_RESERVE_TOKENS = 8_192
MAX_OUTPUT_RESERVE_TOKENS = 131_072
KEEP_RECENT_TOOL_RESULTS = 8
EVICT_TOOL_RESULT_OVER_CHARS = 1_200

# v0.4.6 (T2.1) — per-tier context budgets for the DeepSeek family
# (and any other model where the upstream context window differs
# materially from the generic 100K default). Pre-T2.1 every model
# used DEFAULT_MAX_CONTEXT_TOKENS = 100_000; flash with its smaller
# effective context window would hit OOM / truncation BEFORE the
# compressor ever fired.
#
# Each value is the threshold ABOVE which `should_compress` returns.
# Large windows retain 95% for input; smaller windows protect a fixed
# response reserve so reasoning and tool results still fit.
#
# Numbers are conservative best-guesses based on public Ollama docs
# at the time of writing (2026-05-02). When new DeepSeek tiers ship,
# add them here. Unknown models fall through to the 100K default,
# which is the right behavior for both larger-context models (we'd
# rather not compress unnecessarily) and unknown smaller models
# (the model itself will tell us via OOM).
def _budget_for_window(context_window: int) -> int:
    """Reserve generation and schema headroom inside the provider window."""
    window = max(4_096, int(context_window))
    reserve = min(
        MAX_OUTPUT_RESERVE_TOKENS,
        max(MIN_OUTPUT_RESERVE_TOKENS, int(window * (1 - CONTEXT_HEADROOM_RATIO))),
    )
    return max(4_096, window - reserve)


def model_context_budget(
    model_name: str | None,
    *,
    context_window: int | None = None,
) -> int:
    """Return a compression threshold from runtime or inferred capabilities."""
    if context_window:
        return _budget_for_window(context_window)
    if not model_name:
        return DEFAULT_MAX_CONTEXT_TOKENS
    return _budget_for_window(infer_model_capabilities(model_name).context_window)


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


def evict_old_tool_outputs(
    history: list,
    *,
    keep_recent: int = KEEP_RECENT_TOOL_RESULTS,
    over_chars: int = EVICT_TOOL_RESULT_OVER_CHARS,
) -> tuple[list, int]:
    """Replace stale, oversized tool payloads with re-fetchable receipts.

    Tool output is usually the cheapest context to discard: file reads, grep
    results, and test logs can be reproduced, while user decisions and agent
    conclusions cannot.  Keep the newest results intact and preserve tool
    identity/size so a model can deliberately re-run a paginated read.
    """
    tool_indexes = [
        index for index, entry in enumerate(history)
        if entry.get("role") == "tool_result"
    ]
    protected = set(tool_indexes[-max(0, keep_recent):]) if keep_recent else set()
    rewritten = list(history)
    evicted = 0
    for index in tool_indexes:
        if index in protected:
            continue
        entry = history[index]
        content = entry.get("content", "")
        if not isinstance(content, str) or len(content) <= over_chars:
            continue
        name = str(entry.get("name") or "tool")
        replacement = dict(entry)
        replacement["content"] = (
            f"[Earlier {name} result evicted from context ({len(content):,} chars). "
            "Re-run the tool with offset/limit pagination if the details are needed.]"
        )
        rewritten[index] = replacement
        evicted += 1
    return rewritten, evicted


def should_compress(
    history: list,
    max_tokens: int = DEFAULT_MAX_CONTEXT_TOKENS,
    *,
    model_name: str | None = None,
    context_window: int | None = None,
) -> bool:
    """Check if the conversation history needs compression.

    v0.4.6 (T2.1) — added `model_name` keyword. When supplied, the
    threshold is resolved via `model_context_budget(model_name)`,
    overriding any explicit `max_tokens` so callers can pass
    `model_name=self.backend.model` without juggling tier numbers.
    Backwards compat: callers that pass only `max_tokens` (or
    neither) keep the pre-T2.1 behavior.
    """
    if len(history) < KEEP_RECENT_TURNS * 2 + 4:
        return False
    if model_name is not None or context_window is not None:
        max_tokens = model_context_budget(
            model_name,
            context_window=context_window,
        )
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


def _merged_tool_catalog(entries: list) -> list[dict]:
    """Keep the latest dynamically loaded definition for each tool name."""
    by_name: dict[str, dict] = {}
    for entry in entries:
        if entry.get("role") != "tool_catalog":
            continue
        for tool in entry.get("tools") or []:
            name = str(tool.get("function", {}).get("name") or "")
            if name:
                by_name[name] = tool
    return list(by_name.values())


def compress(
    session,
    backend=None,
    max_tokens: int = DEFAULT_MAX_CONTEXT_TOKENS,
    *,
    model_name: str | None = None,
    context_window: int | None = None,
) -> tuple[list, str]:
    """Compress conversation history by summarizing older messages.

    Args:
        session: Session object with conversation_history
        backend: Backend to use for summarization (defaults to session.backend)
        max_tokens: Target max context size (overridden by model_name if set)
        model_name: v0.4.6 (T2.1) — when supplied, the threshold is
                    resolved per-model via `model_context_budget`.

    Returns:
        (new_history, summary_text) — the compressed history and the summary that was generated
    """
    history = session.conversation_history
    backend = backend or session.backend

    if not should_compress(
        history,
        max_tokens,
        model_name=model_name,
        context_window=context_window,
    ):
        return history, ""

    # First take the lossless/recoverable tier: evict old, oversized tool
    # payloads before asking the model to summarize human and agent decisions.
    pruned_history, evicted = evict_old_tool_outputs(history)
    if evicted:
        budget = (
            model_context_budget(model_name, context_window=context_window)
            if model_name is not None or context_window is not None
            else max_tokens
        )
        if estimate_tokens(pruned_history) <= budget:
            note = f"Evicted {evicted} stale tool output(s); conversation text was preserved."
            logger.info(note)
            return pruned_history, note
        history = pruned_history

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
    old_catalog = _merged_tool_catalog(old_messages)
    recent_catalog_names = {
        str(tool.get("function", {}).get("name") or "")
        for tool in _merged_tool_catalog(recent_messages)
    }
    preserved_catalog = [
        tool for tool in old_catalog
        if tool.get("function", {}).get("name") not in recent_catalog_names
    ]

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
    if preserved_catalog:
        compressed.append({
            "role": "tool_catalog",
            "tools": preserved_catalog,
            "content": "Dynamically loaded tool definitions preserved across compression.",
        })
    compressed.extend(recent_messages)

    logger.info(
        f"Compressed history: {len(history)} entries → {len(compressed)} entries, "
        f"~{estimate_tokens(history)} tokens → ~{estimate_tokens(compressed)} tokens"
    )

    return compressed, summary.strip()
