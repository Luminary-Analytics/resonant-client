"""Keep auxiliary generation distinct from customer conversation learning."""


def auxiliary_stream(backend, purpose: str, *, usage_context: dict | None = None, record: bool = True, **kwargs):
    """Stream an auxiliary request (a title, a summary), recording its usage.

    ``purpose`` names the request in the usage records (lumi/usage.py);
    ``usage_context`` adds the session, project and agent when known.
    ``record=False`` leaves recording to the caller: a Team participant's
    requests are recorded by the host that admits them. The organization's
    DLP rules (lumi/dlp.py) check the request first: it is sent with their
    redactions, or ``dlp.Blocked`` is raised and nothing is sent.
    """
    from .. import dlp

    checked = dlp.check_request(
        kwargs, purpose=purpose, provider=str(getattr(backend, "name", "") or ""),
        model=str(getattr(backend, "model", "") or ""), audit_fields=usage_context,
    )
    return send_checked(backend, purpose, usage_context=usage_context, record=record, **checked.request)


def send_checked(backend, purpose: str, *, usage_context: dict | None = None, record: bool = True, **kwargs):
    """Send an auxiliary request that already passed ``dlp.check_request``, recording
    its usage unless ``record`` is False (as for ``auxiliary_stream``).

    Only ``Session._model_stream``, which checks its requests itself, calls
    this directly; anything else uses ``auxiliary_stream``.
    """
    from .. import dlp

    method = getattr(backend, "stream_auxiliary", None)
    stream = (dlp.send(method, purpose=purpose, **kwargs) if callable(method)
              else dlp.send(backend.stream, **kwargs))
    return _recorded(stream, backend, purpose, dict(usage_context or {})) if record else stream


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
