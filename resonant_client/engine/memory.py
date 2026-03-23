"""
Engram Memory Integration for Resonant Engine.

Connects to the Engram memory system for persistent, context-aware recall
across sessions. Uses MCP transport or direct HTTP to the engram server.
"""

import json
import logging
from typing import Optional

logger = logging.getLogger(__name__)


class EngramIntegration:
    """Interface to the Engram memory system."""

    def __init__(self, settings=None):
        self._settings = settings
        self._enabled = False
        self._server_url = ""
        self._namespace = "resonant"
        self._mcp_manager = None  # Set externally if using MCP transport

        if settings:
            self.reload()

    @property
    def enabled(self) -> bool:
        return self._enabled and bool(self._server_url or self._mcp_manager)

    def set_mcp_manager(self, mcp_manager):
        """Set MCP manager for MCP-based transport to engram."""
        self._mcp_manager = mcp_manager

    def set_namespace(self, namespace: str):
        """Set the memory namespace (e.g., project name)."""
        self._namespace = namespace

    def reload(self):
        """Reload runtime config from settings."""
        if not self._settings:
            return
        engram_config = self._settings.get("engram") or {}
        self._enabled = engram_config.get("enabled", False)
        self._server_url = engram_config.get("server_url", "")

    def clone(self, namespace: str = "") -> "EngramIntegration":
        """Create a project-scoped copy that shares the same settings source."""
        clone = EngramIntegration(self._settings)
        clone.set_mcp_manager(self._mcp_manager)
        clone.set_namespace(namespace or self._namespace)
        return clone

    def recall(self, query: str, namespace: str = "") -> list[str]:
        """Recall memories relevant to a query.

        Returns list of memory strings, most relevant first.
        """
        if not self.enabled:
            return []

        ns = namespace or self._namespace

        # Try MCP transport first
        if self._mcp_manager:
            try:
                result = self._mcp_manager.call_tool("mcp_engram_engram_recall", {
                    "query": query,
                    "namespace": ns,
                })
                if "content" in result:
                    # Parse MCP result
                    content = result["content"]
                    if isinstance(content, list):
                        return [c.get("text", "") for c in content if c.get("text")]
                    return [str(content)]
            except Exception as e:
                logger.warning(f"Engram MCP recall failed: {e}")

        # Fallback: direct HTTP
        if self._server_url:
            return self._http_recall(query, ns)

        return []

    def remember(self, text: str, namespace: str = ""):
        """Store a memory."""
        if not self.enabled:
            return

        ns = namespace or self._namespace

        if self._mcp_manager:
            try:
                self._mcp_manager.call_tool("mcp_engram_engram_remember", {
                    "text": text,
                    "namespace": ns,
                })
                return
            except Exception as e:
                logger.warning(f"Engram MCP remember failed: {e}")

        if self._server_url:
            self._http_remember(text, ns)

    def forget(self, memory_id: str):
        """Remove a memory by ID."""
        if not self.enabled:
            return

        if self._mcp_manager:
            try:
                self._mcp_manager.call_tool("mcp_engram_engram_forget", {
                    "memory_id": memory_id,
                })
                return
            except Exception as e:
                logger.warning(f"Engram MCP forget failed: {e}")

        if self._server_url:
            self._http_forget(memory_id)

    def session_summary(self, conversation_history: list) -> str:
        """Generate and store a session summary from conversation history."""
        if not self.enabled:
            return ""

        # Extract key points from conversation
        points = []
        for entry in conversation_history:
            role = entry.get("role", "")
            content = entry.get("content", "")
            if isinstance(content, str) and role in ("user", "assistant"):
                # Keep short entries as-is, truncate long ones
                if len(content) < 200:
                    points.append(f"{role}: {content}")
                else:
                    points.append(f"{role}: {content[:200]}...")

        if not points:
            return ""

        # Build summary items
        summary_text = "Session summary:\n" + "\n".join(points[-10:])  # Last 10 entries

        if self._mcp_manager:
            try:
                result = self._mcp_manager.call_tool("mcp_engram_engram_session_summary", {
                    "items": points[-20:],
                    "session_context": f"namespace={self._namespace}",
                })
                if result and "content" in result:
                    return str(result["content"])
            except Exception as e:
                logger.warning(f"Engram session summary failed: {e}")

        # Fallback: just remember the summary
        self.remember(summary_text)
        return summary_text

    def get_context_for_prompt(self, user_msg: str) -> str:
        """Get relevant memories formatted for system prompt injection."""
        memories = self.recall(user_msg)
        if not memories:
            return ""

        memory_block = "\n".join(f"- {m}" for m in memories[:5])
        return f"\n\n--- RECALLED MEMORIES ---\n{memory_block}\n--- END MEMORIES ---"

    # ── HTTP Transport ──────────────────────────────────────

    def _http_recall(self, query: str, namespace: str) -> list[str]:
        try:
            import httpx
            resp = httpx.post(
                f"{self._server_url}/recall",
                json={"query": query, "namespace": namespace},
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
            return data.get("memories", [])
        except Exception as e:
            logger.warning(f"Engram HTTP recall failed: {e}")
            return []

    def _http_remember(self, text: str, namespace: str):
        try:
            import httpx
            httpx.post(
                f"{self._server_url}/remember",
                json={"text": text, "namespace": namespace},
                timeout=10,
            )
        except Exception as e:
            logger.warning(f"Engram HTTP remember failed: {e}")

    def _http_forget(self, memory_id: str):
        try:
            import httpx
            httpx.delete(
                f"{self._server_url}/forget/{memory_id}",
                timeout=10,
            )
        except Exception as e:
            logger.warning(f"Engram HTTP forget failed: {e}")
