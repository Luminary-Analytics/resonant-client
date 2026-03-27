"""
Command Center Coordinator Agent.

The coordinator is a background agent that:
1. Reads the user's high-level strategy
2. Breaks it into ordered tasks
3. Spawns worker agents for each task
4. Monitors worker progress
5. Posts updates to the project activity feed
6. Reports completion back to the dashboard
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

COORDINATOR_SYSTEM_PROMPT = """\
You are a Project Coordinator AI. Your job is to orchestrate work on a software project.

## Your Role
You receive a high-level strategy from the user and must:
1. Analyze the project codebase to understand the current state
2. Break the strategy into concrete, ordered tasks
3. Spawn worker agents to execute each task
4. Monitor their progress
5. Report status updates

## Available Tools
You have these special coordinator tools:
- `update_plan(tasks)` — Set the task breakdown. Each task has: title, description, priority, status.
- `spawn_worker(task_index, prompt)` — Launch a worker agent to handle a specific task.
- `check_workers()` — Check status of all spawned workers.
- `post_update(message)` — Post a status update visible on the project dashboard.
- `complete_project(summary)` — Mark the project as completed with a summary.

## Instructions
1. First, read the project files to understand the codebase structure.
2. Then call `update_plan` with your task breakdown.
3. Spawn workers for tasks that can run in parallel.
4. Periodically check worker status and post updates.
5. When all tasks are done, call `complete_project`.

Be efficient. Spawn multiple workers in parallel when tasks are independent.
Keep status updates concise but informative.
"""


def build_coordinator_prompt(strategy: str, project_path: str) -> str:
    """Build the full prompt for the coordinator agent."""
    return f"""\
## Project Path
{project_path}

## Strategy
{strategy}

Please analyze this project, create a task plan, and begin executing it by spawning worker agents.
Start by reading key project files to understand the codebase, then create your plan.
"""


@dataclass
class CoordinatorState:
    """Tracks coordinator state for a project."""

    project_id: str = ""
    tasks: list[dict] = field(default_factory=list)
    workers: dict[str, dict] = field(default_factory=dict)  # worker_id -> {task_index, status, ...}
    updates: list[dict] = field(default_factory=list)
    completed: bool = False
    summary: str = ""


class CoordinatorToolHandler:
    """
    Handles coordinator-specific tool calls during agent execution.

    The coordinator agent calls these tools to manage the project.
    Results are injected back into the agent's conversation.
    """

    def __init__(
        self,
        project_id: str,
        project_store: Any,
        task_runner: Any,
        session_factory: Callable,
        on_event_callback: Optional[Callable] = None,
        backend_type: str = "",
        model: str = "",
        backend_spec: Optional[dict] = None,
        project_path: str = "",
    ):
        self.project_id = project_id
        self.project_store = project_store
        self.task_runner = task_runner
        self.session_factory = session_factory
        self.on_event_callback = on_event_callback
        self.backend_type = backend_type
        self.model = model
        self.backend_spec = backend_spec or {}
        self.project_path = project_path
        self.state = CoordinatorState(project_id=project_id)
        self._lock = threading.Lock()

    def handle_tool_call(self, tool_name: str, tool_args: dict) -> str:
        """Handle a coordinator tool call. Returns result string."""
        handlers = {
            "update_plan": self._handle_update_plan,
            "spawn_worker": self._handle_spawn_worker,
            "check_workers": self._handle_check_workers,
            "post_update": self._handle_post_update,
            "complete_project": self._handle_complete_project,
        }
        handler = handlers.get(tool_name)
        if not handler:
            return f"Unknown coordinator tool: {tool_name}"
        try:
            return handler(tool_args)
        except Exception as e:
            logger.error("Coordinator tool %s failed: %s", tool_name, e)
            return f"Error in {tool_name}: {e}"

    def _handle_update_plan(self, args: dict) -> str:
        """Update the project task plan."""
        tasks = args.get("tasks", [])
        if not isinstance(tasks, list):
            return "Error: tasks must be a list"

        normalized = []
        for i, t in enumerate(tasks):
            if isinstance(t, str):
                t = {"title": t}
            normalized.append({
                "title": t.get("title", f"Task {i+1}"),
                "description": t.get("description", ""),
                "priority": t.get("priority", "medium"),
                "status": t.get("status", "todo"),
                "assigned_worker": t.get("assigned_worker", ""),
            })

        with self._lock:
            self.state.tasks = normalized

        # Update project store
        project = self.project_store.get_project(self.project_id)
        if project:
            self.project_store.update_project(
                self.project_id,
                tasks=normalized,
                status="running",
            )

        return f"Plan updated with {len(normalized)} tasks."

    def _handle_spawn_worker(self, args: dict) -> str:
        """Spawn a worker agent for a specific task."""
        task_index = args.get("task_index", 0)
        prompt = args.get("prompt", "")

        if not prompt:
            return "Error: prompt is required"

        if task_index < 0 or task_index >= len(self.state.tasks):
            return f"Error: task_index {task_index} out of range (have {len(self.state.tasks)} tasks)"

        task_info = self.state.tasks[task_index]
        worker_name = f"Worker: {task_info['title'][:40]}"

        try:
            bg_task = self.task_runner.submit(
                name=worker_name,
                prompt=prompt,
                session_factory=self.session_factory,
                backend_type=self.backend_type,
                model=self.model,
                project_path=self.project_path,
                session_mode="code",
                session_role="generator",
                backend_spec=self.backend_spec,
                on_event=self.on_event_callback,
            )

            worker_id = bg_task.id
            with self._lock:
                self.state.workers[worker_id] = {
                    "task_index": task_index,
                    "name": worker_name,
                    "status": "running",
                    "started_at": datetime.now().isoformat(),
                }
                self.state.tasks[task_index]["status"] = "running"
                self.state.tasks[task_index]["assigned_worker"] = worker_id

            # Update project store
            project = self.project_store.get_project(self.project_id)
            if project:
                agents = project.agents.copy() if hasattr(project, 'agents') else []
                agents.append({
                    "id": worker_id,
                    "name": worker_name,
                    "role": "worker",
                    "status": "running",
                    "task_index": task_index,
                })
                self.project_store.update_project(
                    self.project_id,
                    tasks=self.state.tasks,
                    agents=agents,
                )

            return f"Worker spawned: {worker_id} for task '{task_info['title']}'"

        except Exception as e:
            return f"Failed to spawn worker: {e}"

    def _handle_check_workers(self, args: dict) -> str:
        """Check status of all spawned workers."""
        results = []
        completed_count = 0
        total_count = len(self.state.workers)

        for worker_id, info in self.state.workers.items():
            bg_task = self.task_runner.get_task(worker_id)
            if bg_task:
                status = bg_task.status.value
                steps = bg_task.steps
                elapsed = round(bg_task.elapsed, 1)
                result_preview = (bg_task.result or "")[:200]

                with self._lock:
                    info["status"] = status
                    task_idx = info.get("task_index", 0)
                    if task_idx < len(self.state.tasks):
                        if status in ("completed", "failed", "cancelled"):
                            self.state.tasks[task_idx]["status"] = status
                        elif status == "running":
                            self.state.tasks[task_idx]["status"] = "running"

                if status in ("completed", "failed", "cancelled"):
                    completed_count += 1

                results.append({
                    "worker_id": worker_id,
                    "name": info["name"],
                    "task_index": info["task_index"],
                    "status": status,
                    "steps": steps,
                    "elapsed": elapsed,
                    "result_preview": result_preview if status == "completed" else "",
                })
            else:
                results.append({
                    "worker_id": worker_id,
                    "name": info["name"],
                    "status": "unknown",
                })

        # Update project store
        self.project_store.update_project(
            self.project_id,
            tasks=self.state.tasks,
        )

        summary = f"{completed_count}/{total_count} workers done."
        return json.dumps({"summary": summary, "workers": results}, indent=2)

    def _handle_post_update(self, args: dict) -> str:
        """Post a status update to the project activity feed."""
        message = args.get("message", "")
        if not message:
            return "Error: message is required"

        update = {
            "id": uuid.uuid4().hex[:12],
            "timestamp": datetime.now().isoformat(),
            "sender_type": "coordinator",
            "sender_name": "Coordinator",
            "content": message,
        }

        with self._lock:
            self.state.updates.append(update)

        # Add to project activity
        project = self.project_store.get_project(self.project_id)
        if project:
            activity = project.activity.copy() if hasattr(project, 'activity') else []
            activity.append(update)
            self.project_store.update_project(self.project_id, activity=activity)

        return "Update posted."

    def _handle_complete_project(self, args: dict) -> str:
        """Mark the project as completed."""
        summary = args.get("summary", "Project completed.")

        with self._lock:
            self.state.completed = True
            self.state.summary = summary

        self.project_store.update_project(
            self.project_id,
            status="completed",
        )

        # Post final update
        self._handle_post_update({"message": f"✅ Project completed: {summary}"})

        return "Project marked as completed."

    def get_tool_definitions(self) -> list[dict]:
        """Return tool definitions for the coordinator agent."""
        return [
            {
                "name": "update_plan",
                "description": "Set the task breakdown for the project. Call this after analyzing the codebase.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "tasks": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "title": {"type": "string"},
                                    "description": {"type": "string"},
                                    "priority": {"type": "string", "enum": ["high", "medium", "low"]},
                                },
                                "required": ["title"],
                            },
                            "description": "List of tasks to execute",
                        },
                    },
                    "required": ["tasks"],
                },
            },
            {
                "name": "spawn_worker",
                "description": "Launch a worker agent to handle a specific task. The worker will run autonomously.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "task_index": {
                            "type": "integer",
                            "description": "Index of the task in the plan (0-based)",
                        },
                        "prompt": {
                            "type": "string",
                            "description": "Detailed instructions for the worker agent",
                        },
                    },
                    "required": ["task_index", "prompt"],
                },
            },
            {
                "name": "check_workers",
                "description": "Check the status of all spawned worker agents. Returns their progress.",
                "input_schema": {
                    "type": "object",
                    "properties": {},
                },
            },
            {
                "name": "post_update",
                "description": "Post a status update to the project dashboard. Use this to keep the user informed.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "message": {
                            "type": "string",
                            "description": "Status update message",
                        },
                    },
                    "required": ["message"],
                },
            },
            {
                "name": "complete_project",
                "description": "Mark the project as completed. Call this when all tasks are done.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "summary": {
                            "type": "string",
                            "description": "Final summary of what was accomplished",
                        },
                    },
                    "required": ["summary"],
                },
            },
        ]
