"""Local UI preferences and drafts, independent of the browser's launch port."""
import hashlib
import json
import threading

from .sessions import _project_dir, _projects_dir

_lock = threading.RLock()


def ui_state(data, *, write=False):
    if data.get('project'):
        project = str(data['project'])
        session = str(data.get('session_id', ''))
        name = hashlib.sha256(session.encode()).hexdigest()[:32]
        path = _project_dir(project) / 'drafts' / f'{name}.json'
        value = {'text': str(data.get('text', ''))}
        if len(value['text']) > 100000:
            raise ValueError('Draft is too long to save')
    else:
        path = _projects_dir().parent / 'ui-preferences.json'
        sidebar = data.get('sidebar', {})
        if not isinstance(sidebar, dict):
            raise ValueError('Invalid sidebar preference')
        value = {'sidebar': {k: v for k, v in sidebar.items()
                             if k in {'desktop', 'mobile'} and isinstance(v, bool)}}
    with _lock:
        if write:
            if data.get('project') and not value['text']:
                path.unlink(missing_ok=True)
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                temporary = path.with_suffix('.tmp')
                temporary.write_text(json.dumps(value), encoding='utf-8')
                temporary.replace(path)
            return value
        try:
            return json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return {'text': ''} if data.get('project') else {'sidebar': {}}
