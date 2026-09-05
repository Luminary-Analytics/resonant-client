"""
Context Compression for Resonant Engine.

When conversation history grows too large, older messages are summarized
into a compact form while keeping recent turns intact.
"""

import json
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

# Context thresholds use the effective deployment window, with generation
# headroom protected even on small local models. Counts are estimates; exact
# tokenization remains provider-specific.
def _budget_for_window(context_window: int) -> int:
    """Reserve generation and schema headroom inside the provider window."""
    window = max(4_096, int(context_window))
    reserve = min(
        MAX_OUTPUT_RESERVE_TOKENS,
        max(MIN_OUTPUT_RESERVE_TOKENS, int(window * (1 - CONTEXT_HEADROOM_RATIO))),
    )
    return max(window // 2, window - reserve)


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
                    if part.get("type") in {"image", "image_url", "input_image"}:
                        total_chars += 16_384  # conservative image reserve, not base64 length
        # Tool arguments and reasoning are replayed to the provider even though
        # their human-readable content may just say "Called file_write".
        for key in ("arguments", "reasoning_content", "thinking", "assistant_content",
                    "response_tool_calls", "tools"):
            value = entry.get(key)
            if value:
                total_chars += len(value) if isinstance(value, str) else len(json.dumps(value, ensure_ascii=False))
        if entry.get("image"):
            total_chars += 16_384
    return total_chars // CHARS_PER_TOKEN


def request_overhead_tokens(instructions: str = "", tools: list | None = None) -> int:
    """Estimate the serialized prefix, including tool schemas and message framing."""
    return (len(json.dumps({"role": "system", "content": instructions,
                           "tools": tools or []}, ensure_ascii=False)) + 3) // CHARS_PER_TOKEN


SUMMARY_FIELDS = ("summary", "decisions", "changes", "verification", "unresolved_failures", "next_action")


def _validated_summary(text: str) -> dict | None:
    try:
        result = json.loads(text)
    except (ValueError, TypeError):
        return None
    if not isinstance(result, dict) or any(
        not isinstance(result.get(key), str) or not result[key].strip() for key in SUMMARY_FIELDS
    ):
        return None
    return {key: result[key] for key in SUMMARY_FIELDS}


def evict_old_tool_outputs(
    history: list,
    *,
    keep_recent: int = KEEP_RECENT_TOOL_RESULTS,
    over_chars: int = EVICT_TOOL_RESULT_OVER_CHARS,
    artifact_store=None,
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
        if entry.get("context_evicted"):
            continue
        replacement = dict(entry)
        receipt = ""
        if artifact_store is not None:
            try:
                artifact = artifact_store.put_text(content, label=f"{name} historical result",
                                                   source=str(entry.get("call_id") or ""))
                receipt = (artifact_store.reference(artifact) +
                           " Read with artifact_read by id; load it with search_tools if needed.")
                replacement["artifact_id"] = artifact.id
            except Exception:
                logger.warning("Unable to archive tool evidence; retaining it", exc_info=True)
                continue
        elif name not in {"file_read", "glob", "grep"}:
            # A command may have side effects or observe a state that no longer
            # exists. Never tell the model to rerun it to recover old evidence.
            continue
        replacement["context_evicted"] = True
        replacement["content"] = (
            f"[Earlier {name} result evicted from context ({len(content):,} chars). "
            + (receipt or "Re-read with offset/limit pagination for the current file contents.") + "]"
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
    overhead_tokens: int = 0,
) -> bool:
    """Check if the conversation history needs compression.

    v0.4.6 (T2.1) — added `model_name` keyword. When supplied, the
    threshold is resolved via `model_context_budget(model_name)`,
    overriding any explicit `max_tokens` so callers can pass
    `model_name=self.backend.model` without juggling tier numbers.
    Backwards compat: callers that pass only `max_tokens` (or
    neither) keep the pre-T2.1 behavior.
    """
    if model_name is not None or context_window is not None:
        max_tokens = model_context_budget(
            model_name,
            context_window=context_window,
        )
    return estimate_tokens(history) + overhead_tokens > max_tokens


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
        "Be specific about file paths and code changes. Keep it under 500 words. "
        "Return only a JSON object with nonempty string fields: summary, decisions, changes, "
        "verification, unresolved_failures, next_action. State 'none recorded' when no evidence "
        "exists; do not invent successes. next_action must identify what remains to do.\n\n"
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
    overhead_tokens: int = 0,
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
        overhead_tokens=overhead_tokens,
    ):
        return history, ""

    # First take the lossless/recoverable tier: evict old, oversized tool
    # payloads before asking the model to summarize human and agent decisions.
    artifact_store = getattr(session, "artifact_store", None)
    allowed_tools = getattr(session, "_allowed_tools", None)
    if allowed_tools is not None and not any(
        tool.get("function", {}).get("name") == "artifact_read" for tool in allowed_tools
    ):
        artifact_store = None  # Do not archive evidence a restricted worker cannot retrieve.
    pruned_history, evicted = evict_old_tool_outputs(history, artifact_store=artifact_store)
    budget = (
        model_context_budget(model_name, context_window=context_window)
        if model_name is not None or context_window is not None else max_tokens
    )
    if estimate_tokens(pruned_history) + overhead_tokens > budget:
        # Even a single new result can overflow a small context. Replace its
        # payload with a receipt too, so the agent can retrieve a bounded page.
        pruned_history, extra_evicted = evict_old_tool_outputs(
            pruned_history, keep_recent=0,
            artifact_store=artifact_store,
        )
        evicted += extra_evicted
    if evicted:
        if estimate_tokens(pruned_history) + overhead_tokens <= budget:
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

    if split_idx <= 2 or split_idx == len(history):
        # A long tool loop can have only one user turn. Keep the latest
        # call/result together and preserve the user's instructions below.
        split_idx = max(0, len(history) - 2)
        while split_idx > 0 and history[split_idx].get("role") == "tool_result":
            split_idx -= 1
        if split_idx == 0:
            return history, (f"Evicted {evicted} stale tool output(s)." if evicted else "")

    old_messages = history[:split_idx]
    recent_messages = history[split_idx:]
    preserved_media = [
        entry for entry in old_messages
        if entry.get("role") == "user" and isinstance(entry.get("content"), list)
        and any(isinstance(part, dict) and part.get("type") != "text" for part in entry["content"])
    ]
    # Mechanical retention is independent of the quality of a model summary.
    preserved = {"user_requirements": [], "checklist": list(getattr(session, "todos", [])),
                 "tool_evidence": []}
    for entry in old_messages:
        previous = entry.get("preserved_context") or {}
        for key in ("user_requirements", "tool_evidence"):
            preserved[key].extend(previous.get(key, []))
        if entry.get("role") == "user":
            preserved["user_requirements"].append(_extract_text(entry))
        if entry.get("role") in {"tool_call", "tool_result"}:
            evidence = {key: entry[key] for key in ("role", "name", "call_id", "is_error", "artifact_id") if key in entry}
            if entry.get("role") == "tool_call":
                try:
                    args = json.loads(entry.get("arguments") or "{}")
                    evidence["targets"] = {key: args[key] for key in ("path", "command") if key in args}
                except (ValueError, TypeError):
                    pass
            else:
                evidence["observation"] = _extract_text(entry)
            preserved["tool_evidence"].append(evidence)
    for key in ("user_requirements", "tool_evidence"):
        preserved[key] = list({json.dumps(item, sort_keys=True): item for item in preserved[key]}.values())
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
    # The summarizer has the same finite window. Do not make an oversized
    # auxiliary request in an attempt to repair an oversized main request.
    if context_window and len(summary_prompt) // CHARS_PER_TOKEN > model_context_budget(model_name, context_window=context_window):
        preview_chars = max(64, (model_context_budget(model_name, context_window=context_window) * 4 - 2048) // max(1, len(old_messages)))
        summary_prompt = _build_summary_prompt([
            {**entry, "content": _extract_text(entry)[:preview_chars]}
            for entry in old_messages
        ])
        if len(summary_prompt) // CHARS_PER_TOKEN > model_context_budget(model_name, context_window=context_window):
            return history, (f"Evicted {evicted} stale tool output(s)." if evicted else "")
    try:
        summary = ""
        stream_options = {}
        if getattr(session, "_cancel_event", None) is not None:
            stream_options["cancel_event"] = session._cancel_event
        for event_type, data in backend.stream(
            user_msg=summary_prompt,
            conversation_history=[],
            instructions="You are a conversation summarizer. Be concise and factual.",
            tools=[],
            max_tokens=1024,
            **stream_options,
        ):
            if event_type == "text.delta":
                summary += data.get("delta", "")
            elif event_type == "done":
                break
    except Exception as e:
        logger.error(f"Compression failed: {e}")
        return history, ""

    structured_summary = _validated_summary(summary)
    if structured_summary is None:
        logger.warning("Compaction summary failed its retention schema; retaining history")
        return history, ""
    summary = structured_summary["summary"]

    # Build compressed history
    compressed = [
        {
            "role": "assistant",
            "content": ("[Previous conversation summary]\n" + json.dumps(structured_summary, ensure_ascii=False) + "\n"
                        "[Preserved requirements, checklist, and observed tool evidence]\n"
                        + json.dumps(preserved, ensure_ascii=False) +
                        "\n[End summary — recent messages follow]"),
            "preserved_context": preserved,
        }
    ]
    compressed.extend(preserved_media)
    if preserved_catalog:
        compressed.append({
            "role": "tool_catalog",
            "tools": preserved_catalog,
            "content": "Dynamically loaded tool definitions preserved across compression.",
        })
    compressed.extend(recent_messages)

    if estimate_tokens(compressed) >= estimate_tokens(history):
        return history, (f"Evicted {evicted} stale tool output(s)." if evicted else "")

    logger.info(
        f"Compressed history: {len(history)} entries → {len(compressed)} entries, "
        f"~{estimate_tokens(history)} tokens → ~{estimate_tokens(compressed)} tokens"
    )

    return compressed, summary.strip()
