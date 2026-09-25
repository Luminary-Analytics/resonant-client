"""Durable SONN conversation routing without paid provider calls."""

from types import SimpleNamespace

import pytest

from lumi.gui.runtime import bind_sonn_conversation
from lumi.sonn import SonnBackend
from lumi.engine.request_purpose import auxiliary_stream


BASE = "https://sonn.example/v1/workspace/projects/project-fixture/openai/v1"


def payload(backend, text="continue"):
    return backend._payload(text, [], "fixture instructions", [], 512)


def test_recreated_backend_keeps_saved_conversation_and_separates_fresh_sessions(tmp_path):
    first = SonnBackend("private-test-key", base_url=BASE)
    assert bind_sonn_conversation(first, str(tmp_path / "project"), "saved-session")
    identity = payload(first, "build")['user']
    assert payload(first)['user'] == identity

    restored = SonnBackend("rotated-test-key", base_url=BASE)
    bind_sonn_conversation(restored, str(tmp_path / "project"), "saved-session")
    assert payload(restored)['user'] == identity
    bind_sonn_conversation(restored, str(tmp_path / "project"), "new-session")
    assert payload(restored)['user'] != identity
    bind_sonn_conversation(restored, str(tmp_path / "other"), "saved-session")
    assert payload(restored)['user'] != identity
    assert str(tmp_path) not in identity and "private-test-key" not in identity


def test_path_normalization_and_model_switch_preserve_conversation(tmp_path):
    first = SonnBackend("key", model="sonn-auto", base_url=BASE)
    second = SonnBackend("key", model="other-model", base_url=BASE)
    bind_sonn_conversation(first, str(tmp_path / "project" / ".." / "project"), "saved")
    bind_sonn_conversation(second, str(tmp_path / "project"), "saved")
    assert payload(first)['user'] == payload(second)['user']


@pytest.mark.parametrize("project,session", [("", "saved"), ("project", ""), ("project", "  ")])
def test_missing_durable_identity_fails_before_dispatch(project, session):
    backend = SonnBackend("key", base_url=BASE)
    with pytest.raises(ValueError, match="project and saved session"):
        bind_sonn_conversation(backend, project, session)


def test_other_providers_are_untouched():
    backend = SimpleNamespace(name="other")
    assert not bind_sonn_conversation(backend, "", "")
    assert vars(backend) == {"name": "other"}


def test_generated_history_and_continuation_exclude_only_wire_user_indices():
    backend = SonnBackend("key", base_url=BASE)
    history = [
        {"role": "system", "content": "retained instructions"},
        {"role": "user", "content": "same text", "input_origin": "generated"},
        {"role": "assistant", "content": "ok"},
        {"role": "user", "content": "same text"},
        {"role": "tool_call", "name": "test", "arguments": "{}", "call_id": "c"},
        {"role": "tool_result", "name": "test", "content": "failed", "call_id": "c"},
    ]
    body = backend._payload("generated recovery", history, "system", [], 16384)
    indices = body['metadata']['sonn_generated_user_indices']
    assert [body['messages'][i]['content'] for i in indices] == ['same text', 'generated recovery']
    human = [m for i, m in enumerate(body['messages']) if m['role'] == 'user' and i not in indices]
    assert human == [{'role': 'user', 'content': 'same text'}]
    assert any(m['role'] == 'tool' and m['content'] == 'failed' for m in body['messages'])
    assert body['max_tokens'] == 16384


def test_auxiliary_calls_use_isolated_identity_without_mutating_coding_backend(monkeypatch, tmp_path):
    backend = SonnBackend("key", base_url=BASE)
    bind_sonn_conversation(backend, str(tmp_path), 'saved')
    original = backend.conversation_id
    bodies = []

    def stream(self, **kwargs):
        bodies.append(self._payload(kwargs['user_msg'], [], 'summary', [], 32))
        yield 'done', {}

    monkeypatch.setattr(SonnBackend, 'stream', stream)
    list(auxiliary_stream(backend, 'title', user_msg='synthetic'))
    list(auxiliary_stream(backend, 'compression', user_msg='synthetic'))
    assert bodies[0]['user'] != bodies[1]['user'] != original
    for body in bodies:
        assert body['metadata']['sonn_observation_learning'] is False
        assert body['metadata']['sonn_input_origin'] == 'generated'
        assert body['metadata']['sonn_learning_control'] == 'none'
    assert backend.conversation_id == original
    assert 'metadata' not in payload(backend)
