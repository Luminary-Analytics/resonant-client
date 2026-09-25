import json
from types import SimpleNamespace
import pytest
from lumi.engine.artifacts import ArtifactStore
from lumi.engine.compression import compress, estimate_tokens, model_context_budget

class InvalidSummary:
    def stream(self, **kwargs):
        yield 'text.delta', {'delta': 'The work is probably done.'}
        yield 'done', {}

def history():
    rows = [{'role': 'user', 'content': 'Fix drawing. Preserve authentication and all failing assertions.'}]
    for n in range(18):
        rows += [{'role': 'tool_call', 'name': 'file_edit', 'call_id': str(n), 'content': 'edit',
                  'arguments': json.dumps({'path': 'app.js', 'old_text': 'a'*6000, 'new_text': 'b'*6000})},
                 {'role': 'tool_result', 'name': 'check_run', 'call_id': str(n), 'content': 'FAIL: replay did not pause', 'is_error': True}]
    return rows

def test_invalid_summary_recovers_with_readable_exact_archive_and_failed_evidence(tmp_path):
    original = history()
    session = SimpleNamespace(conversation_history=original, backend=InvalidSummary(), todos=[],
                              artifact_store=ArtifactStore(tmp_path, root=tmp_path/'artifacts'))
    compressed, note = compress(session, context_window=64000, overhead_tokens=4000)
    assert note.startswith('Mechanical context recovery')
    assert estimate_tokens(compressed)+4000 < model_context_budget(None, context_window=64000)
    assert compressed[-2:] == original[-2:]  # intact current call/result pair
    retained = compressed[0]['preserved_context']
    assert retained['user_requirements'] == [original[0]['content']]
    assert any(e.get('is_error') and 'FAIL: replay' in e.get('observation','') for e in retained['tool_evidence'])
    assert 'probably done' not in compressed[0]['content']
    manifests = [json.loads(line) for line in (tmp_path/'artifacts/manifest.jsonl').read_text().splitlines()]
    from pathlib import Path
    archived = next(m for m in manifests if m.get('source') == 'compaction-recovery')
    assert json.loads(Path(archived['path']).read_text()) == original
    assert archived['id'] in compressed[0]['content']
    assert session.conversation_history is original

@pytest.mark.parametrize('unreadable', [True, False])
def test_recovery_preserves_history_when_archive_unavailable(tmp_path, unreadable):
    class BrokenStore:
        def put_text(self, *args, **kwargs):
            raise OSError('disk full')
    original = history()
    session = SimpleNamespace(conversation_history=original, backend=InvalidSummary(), todos=[], artifact_store=BrokenStore())
    if unreadable:
        session._allowed_tools = []
    compressed, note = compress(session, context_window=64000)
    assert compressed is original
    assert not note

def test_provider_error_is_not_reclassified_as_format_recovery(tmp_path):
    class ProviderError:
        def stream(self, **kwargs):
            yield 'error', {'message': 'uncertain provider completion'}
    original = history()
    store = ArtifactStore(tmp_path, root=tmp_path/'artifacts')
    session = SimpleNamespace(conversation_history=original, backend=ProviderError(), todos=[], artifact_store=store)
    compressed, note = compress(session, context_window=64000)
    assert compressed is original and not note
    assert not (tmp_path/'artifacts/manifest.jsonl').exists()
