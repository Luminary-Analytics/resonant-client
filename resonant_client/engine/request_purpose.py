"""Keep auxiliary generation distinct from customer conversation learning."""


def auxiliary_stream(backend, purpose: str, **kwargs):
    method = getattr(backend, "stream_auxiliary", None)
    if callable(method):
        return method(purpose=purpose, **kwargs)
    return backend.stream(**kwargs)
