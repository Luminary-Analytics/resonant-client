"""Keep auxiliary generation distinct from customer conversation learning."""


def auxiliary_stream(backend, purpose: str, *, usage_context: dict | None = None, record: bool = True, **kwargs):
    """Stream an auxiliary request (a title, a summary), recording its usage.

    ``purpose`` names the request in the usage records (lumi/usage.py);
    ``usage_context`` adds the session, project and agent when known.
    requests are recorded by the host that admits them. A provider offline
    mode can't reach answers with its reason instead (lumi/offline.py), as a
    turn does, before anything else looks at the request. Otherwise the
    organization's DLP rules (lumi/dlp.py) check it: it is sent with their
    redactions, or ``dlp.Blocked`` is raised and nothing is sent.
    """
    from .. import dlp

    refused = _offline_refusal(backend)
    if refused is not None:
        return refused
    checked = dlp.check_request(
        kwargs, purpose=purpose, provider=str(getattr(backend, "name", "") or ""),
        model=str(getattr(backend, "model", "") or ""), audit_fields=usage_context,
    )
    return send_checked(backend, purpose, usage_context=usage_context, record=record, **checked.request)


def _offline_refusal(backend):
    """An answer carrying offline mode's refusal when it can't reach ``backend``, else None."""
    from .. import offline

    refusal = offline.backend_refusal(backend)
    return iter([("error", {"message": refusal})]) if refusal else None


def send_checked(backend, purpose: str, *, usage_context: dict | None = None, record: bool = True, **kwargs):
    """Send an auxiliary request that already passed ``dlp.check_request``, recording
    its usage unless ``record`` is False (as for ``auxiliary_stream``).

    Only ``Session._model_stream``, which checks its requests itself, calls
    this directly; anything else uses ``auxiliary_stream``. Offline mode's
    refusal applies here too, so no path sends to a provider it can't reach.
    """
    from .. import dlp

    refused = _offline_refusal(backend)
    if refused is not None:
        return refused
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
