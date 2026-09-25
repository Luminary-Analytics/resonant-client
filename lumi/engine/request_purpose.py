"""Keep auxiliary generation distinct from customer conversation learning."""


def auxiliary_stream(backend, purpose: str, *, usage_context: dict | None = None, **kwargs):
    """Stream an auxiliary request (a title, a summary), recording its usage.

    ``purpose`` names the request in the usage records (lumi/usage.py);
    ``usage_context`` adds the session, project and agent when known.
    """
    method = getattr(backend, "stream_auxiliary", None)
    stream = method(purpose=purpose, **kwargs) if callable(method) else backend.stream(**kwargs)
    return _recorded(stream, backend, purpose, dict(usage_context or {}))


def _recorded(stream, backend, purpose: str, context: dict):
    from .. import usage

    try:
        for event_type, data in stream:
            if event_type == "done" and isinstance(data, dict) and isinstance(data.get("stats"), dict):
                usage.record(
                    provider=str(getattr(backend, "name", "") or ""),
                    model=str(data.get("model") or getattr(backend, "model", "") or ""),
                    stats=data["stats"],
                    purpose=purpose,
                    **context,
                )
            yield event_type, data
    finally:
        close = getattr(stream, "close", None)
        if callable(close):
            close()
