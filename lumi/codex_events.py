"""Translate Codex JSONL observations without asking the engine to execute tools."""

import json
import re
import shlex


def check_command(command: str) -> bool:
    """Recognize standalone named checks; compound shell exit codes are ambiguous."""
    value = command.strip()
    try:
        words = shlex.split(value)
    except ValueError:
        return False
    if words and words[0].replace('\\', '/').rsplit('/', 1)[-1].lower() in {
        'powershell', 'powershell.exe', 'pwsh', 'pwsh.exe', 'bash', 'sh',
    }:
        for index, word in enumerate(words[1:], 1):
            if word.lower() in {'-command', '-c', '-lc'}:
                value = ' '.join(words[index + 1:])
                break
    # Codex often prefixes a standalone check with literal environment setup.
    # Do not accept preceding executable commands or a trailing exit-code mask.
    value = re.sub(r'^(?:\s*\$env:[A-Za-z_][A-Za-z_0-9]*\s*=\s*(?:\'[^\']*\'|"[^"]*"|\d+)\s*;\s*)+', '', value)
    if any(token in value for token in (';', '&', '|', '\n', '>', '`', '$(')):
        return False
    if re.search(r'\s(?:--(?:help|version|collect-only|listTests)|-h)(?:\s|$)', value):
        return False
    return bool(re.match(
        r'^(?:(?:python(?:3|\.exe)?\s+-m\s+)?(?:pytest|unittest|mypy)\b'
        r'|(?:python(?:3|\.exe)?\s+-m\s+)?ruff\s+check\b'
        r'|node(?:\.exe)?\s+--(?:test|check)\b'
        r'|(?:npm|pnpm|yarn)(?:\.cmd)?\s+(?:test\b|(?:run\s+)?(?:lint|check|typecheck|build)\b)'
        r'|(?:npx(?:\.cmd)?\s+)?(?:vitest|jest|tsc)\b'
        r'|cargo\s+(?:test|check|clippy)\b|go\s+test\b)', value, re.I))


class CodexEvents:
    """Per-stream item IDs deduplicate updates and preserve message boundaries."""

    def __init__(self, model: str):
        self.model = model
        self.messages: dict[str, str] = {}
        self.started: set[str] = set()
        self.completed: set[str] = set()

    def translate(self, event: dict) -> list[tuple[str, dict]]:
        kind = event.get('type')
        if kind in {'thread.started', 'turn.started'}:
            return [('backend.status', {'kind': 'generation_progress', 'phase': 'working', 'model': self.model})]
        if kind not in {'item.started', 'item.updated', 'item.completed'}:
            return []
        item = event.get('item')
        if not isinstance(item, dict):
            return []
        item_type = item.get('type')
        item_id = str(item.get('id') or f'item_{len(self.completed)}')
        if item_id in self.completed:
            return []
        complete = kind == 'item.completed'
        if complete:
            self.completed.add(item_id)
        if item_type == 'agent_message':
            text = str(item.get('text') or '')
            previous = self.messages.get(item_id, '')
            if not text or not text.startswith(previous) or text == previous:
                return []
            delta = ('\n\n' if self.messages and item_id not in self.messages else '') + text[len(previous):]
            self.messages[item_id] = text
            return [('text.delta', {'delta': delta})]
        if item_type == 'reasoning':
            # Report activity only, not raw reasoning content.
            return [('backend.status', {'kind': 'generation_progress', 'phase': 'reasoning', 'model': self.model})]
        names = {'command_execution': 'codex_command', 'file_change': 'codex_file_change',
                 'mcp_tool_call': 'codex_mcp', 'web_search': 'codex_web_search'}
        if item_type not in names:
            return []
        paths = [str(change['path']) for change in item.get('changes', [])
                 if isinstance(change, dict) and change.get('path')]
        command = str(item.get('command') or '')
        args = ({'command': command} if item_type == 'command_execution' else
                {'paths': paths} if item_type == 'file_change' else
                {'server': item.get('server', ''), 'tool': item.get('tool', '')} if item_type == 'mcp_tool_call' else
                {'query': item.get('query', '')})
        base = {'name': names[item_type], 'call_id': 'codex_' + item_id, 'arguments': args, 'source': 'codex'}
        result = []
        if item_id not in self.started:
            self.started.add(item_id)
            result.append(('external.tool', {**base, 'stage': 'started'}))
        if not complete:
            return result
        status = item.get('status', '')
        exit_code = item.get('exit_code')
        success = status == 'completed' and not item.get('error')
        if item_type == 'command_execution':
            success = success and type(exit_code) is int and exit_code == 0
        output = item.get('aggregated_output') if item_type == 'command_execution' else item.get('result')
        if item_type == 'file_change':
            output = '\n'.join(paths)
        if output is None:
            output = item.get('error') or status
        if not isinstance(output, str):
            output = json.dumps(output, ensure_ascii=False)
        metadata = {'source': 'codex', 'status': status, 'exit_code': exit_code}
        if item_type == 'command_execution' and check_command(command):
            metadata['check'] = {'requirement': 'Codex command', 'command': command,
                                 'status': 'passed' if success else 'failed', 'exit_code': exit_code,
                                 'source': 'codex'}
        result.append(('external.tool', {**base, 'stage': 'completed', 'is_error': not success,
                                        'output': output[:16000], 'metadata': metadata,
                                        'changed_files': paths if item_type == 'file_change' and success else []}))
        return result
