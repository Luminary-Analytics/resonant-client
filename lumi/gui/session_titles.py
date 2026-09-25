"""Optional post-turn title refinement with stale-result protection."""

import asyncio
import copy
import threading

from ..engine.session_titles import generate_session_title

TITLE_TIMEOUT_SECONDS = 12


def cancel_title_refinement(state) -> None:
    """Give new coding work priority over a pending metadata request."""
    cancel = getattr(state, "_session_title_cancel", None)
    if cancel:
        cancel.set()
    task = getattr(state, "_session_title_task", None)
    if task and not task.done():
        task.cancel()


def schedule_title_refinement(state, ws, record, prompt: str) -> None:
    """Refine only this active auto-title after the first coding turn finishes."""
    cancel_title_refinement(state)
    if not record or record.title_source != "auto" or not state.backend:
        return
    # A configured summarize model names sessions; otherwise the chat model.
    router = getattr(getattr(state, "session", None), "model_role_router", None)
    try:
        chosen = router.backend_for("summarize", state.backend) if router else state.backend
    except Exception:
        chosen = state.backend
    # Never start a CLI tool loop just to name a session.
    if getattr(chosen, "handles_tools", True):
        return
    backend = copy.copy(chosen) if chosen is state.backend else chosen
    usage_context = {"session": str(getattr(record, "id", "") or ""),
                     "project": str(getattr(state.project, "project_path", "") or "")}
    expected = record.title
    cancel = threading.Event()
    state._session_title_cancel = cancel

    async def refine():
        try:
            title = await asyncio.wait_for(
                asyncio.to_thread(generate_session_title, backend, prompt, cancel, usage_context=usage_context),
                timeout=TITLE_TIMEOUT_SECONDS,
            )
            # Never activate another session, overwrite a manual rename, or write
            # an old record after navigation/new work has replaced its context.
            if (not title or cancel.is_set() or state.project.current_session is not record
                    or record.title_source != "auto" or record.title != expected):
                return
            record.title = title
            record.save()
            await ws.send_json({
                "event": "sessions_updated", "sessions": state.project.list_sessions(),
                "all_sessions": state.project.list_all_sessions(), "current_session_id": record.id,
            })
        except (asyncio.TimeoutError, asyncio.CancelledError):
            pass
        except Exception:
            # Naming is best effort. Keep the existing title and coding result.
            pass
        finally:
            cancel.set()

    state._session_title_task = asyncio.create_task(refine())
