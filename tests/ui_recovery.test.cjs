const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../lumi/gui/static/app.js'), 'utf8').split('function applyMixin(')[0];
const tick = () => new Promise(resolve => setImmediate(resolve));

function setup(fetch, globals = {}) {
    const context = vm.createContext({fetch, URLSearchParams, Blob, console, Event, document: {getElementById: () => null}, WebSocket: {OPEN: 1}, ...globals});
    vm.runInContext(source + '\nthis.App = LumiApp;', context);
    const app = Object.create(context.App.prototype);
    app.userInput = {value: '', style: {}, scrollHeight: 40};
    app.renderAttachedImages = () => {};
    app.showToastMessage = () => {};
    return app;
}

test('late draft reads do not overwrite typing or cross session boundaries', async () => {
    const reads = [];
    const app = setup((url, options) => options ? Promise.resolve({ok:true}) : new Promise(resolve => reads.push(resolve)));
    app._activateDraft('D:/project', 'a');
    await tick();
    app._activateDraft('D:/project', 'b');
    await tick();
    reads[0]({ok:true, json:async () => ({text:'A'})});
    await tick();
    assert.equal(app.userInput.value, '');
    app.userInput.value = 'typing B'; app._draftScope.edited = true;
    reads[1]({ok:true, json:async () => ({text:'older B'})});
    await tick();
    assert.equal(app.userInput.value, 'typing B');
});

test('sent drafts are deleted and a late read cannot restore them', async () => {
    let resolveRead;
    const writes = [];
    const app = setup((url, options) => {
        if (options) { writes.push(JSON.parse(options.body)); return Promise.resolve({ok:true}); }
        return new Promise(resolve => {resolveRead = resolve;});
    });
    app._activateDraft('D:/project', 'a'); await tick();
    app.userInput.value = 'send me'; await app._saveDraft();
    app._clearComposerAfterSend(); await app._draftWrites;
    resolveRead({ok:true, json:async () => ({text:'old draft'})}); await tick();
    assert.equal(app.userInput.value, '');
    assert.equal(writes.at(-1).text, '');
});

test('returning to a session restores only its saved draft', async () => {
    const drafts = new Map();
    const app = setup(async (url, options) => {
        if (options) { const data = JSON.parse(options.body); drafts.set(data.session_id, data.text); return {ok:true}; }
        const session = new URL(url, 'http://localhost').searchParams.get('session_id');
        return {ok:true, json:async () => ({text:drafts.get(session) || ''})};
    });
    app._activateDraft('D:/project', 'a'); await tick();
    app.userInput.value = 'draft A'; app._activateDraft('D:/project', 'b'); await tick();
    assert.equal(app.userInput.value, '');
    app.userInput.value = 'draft B'; app._activateDraft('D:/project', 'a'); await tick();
    assert.equal(app.userInput.value, 'draft A');
});

test('typing then erasing before restoration deletes the older stored draft', async () => {
    const writes = [];
    const app = setup((url, options) => {
        if (options) { writes.push(JSON.parse(options.body)); return Promise.resolve({ok:true}); }
        return new Promise(() => {});
    });
    app._activateDraft('D:/project', 'a'); await tick();
    app.userInput.value = 'temporary'; app._markDraftEdited();
    app.userInput.value = ''; app._markDraftEdited();
    await app._saveDraft();
    assert.equal(writes.at(-1).text, '');
});

test('unified sidebar groups sessions, bounds rows, and reveals the active conversation', () => {
    const app = setup(() => {});
    app.currentCwd = 'D:/alpha';
    app.currentSessionId = 'a-9';
    app._getProjectRailItems = () => [
        {key:app._projectKey('D:/alpha'),path:'D:/alpha',name:'Alpha'},
        {key:app._projectKey('D:/beta'),path:'D:/beta',name:'Beta'}];
    const rows = Array.from({length:10}, (_,i) => ({id:`a-${i}`,title:`Task ${i}`,updated_at:100-i,project_path:'D:/alpha'}));
    rows.push({id:'b',title:'Checkout repair',updated_at:200,project_path:'D:/beta',pinned:true});
    const groups = app._sidebarProjectGroups(rows);
    assert.equal(groups[0].matches.length, 10);
    assert.equal(groups[0].visible.length, 7);
    assert.ok(groups[0].visible.some(s => s.id === 'a-9'));
    assert.equal(groups[1].visible.length, 0);
    const search = app._sidebarProjectGroups(rows, 'checkout');
    assert.equal(search.length, 1);
    assert.equal(search[0].project.name, 'Beta');
    assert.equal(search[0].visible[0].id, 'b');
    assert.equal(app._sidebarProjectGroups(rows, 'alpha')[0].matches.length, 10);
});

test('new session waits for a project choice without changing the current draft', () => {
    const app = setup(() => {});
    app.ws = {readyState: 1};
    app.currentCwd = 'D:/alpha';
    app.currentSessionId = 'existing';
    app.userInput.value = 'unfinished work';
    let opened = 0;
    app.showNewSessionProjectPicker = () => opened++;
    app.send = () => assert.fail('Opening the chooser must not mutate saved work');
    app.startNewSession();
    assert.equal(opened, 1);
    assert.equal(app.currentSessionId, 'existing');
    assert.equal(app.currentCwd, 'D:/alpha');
    assert.equal(app.userInput.value, 'unfinished work');
    app.isRunning = true;
    app.startNewSession();
    assert.equal(opened, 1);
    app.isRunning = false;
    app._pendingProjectSwitchId = 'switching';
    app.startNewSession();
    assert.equal(opened, 1);
});

test('native and fallback folder choices keep the new-session intent', () => {
    const app = setup(() => {});
    const chosen = [];
    app.startNewSession = path => chosen.push(path);
    app._pendingFolderPickConsumer = 'new-session';
    app.handleEvent({event: 'folder_picked', path: 'D:/alpha'});
    assert.deepEqual(chosen, ['D:/alpha']);
    assert.equal(app._pendingFolderPickConsumer, null);
    app._pendingFolderPickConsumer = 'new-session';
    app._promptForProjectPath = (label, callback) => {
        assert.equal(label, 'New session folder');
        callback('D:/new-project');
    };
    app.handleEvent({event: 'folder_picker_unavailable'});
    assert.deepEqual(chosen, ['D:/alpha', 'D:/new-project']);
});


test('next prompt prefers explicit next steps and avoids failed or empty turns', () => {
    const app = setup(() => {});
    assert.equal(app._nextPromptSuggestion('Done.\n## Next steps\n- Add keyboard navigation to the project list.'),
        "Let's do the next step: Add keyboard navigation to the project list.");
    assert.equal(app._nextPromptSuggestion('Done', {outcome:'failed'}, true), '');
    assert.equal(app._nextPromptSuggestion(''), '');
    assert.match(app._nextPromptSuggestion('Updated the parser.', {}, true), /Review these changes/);
    assert.match(app._nextPromptSuggestion('I recommend the smaller approach.'), /implementation plan/);
    assert.doesNotMatch(app._nextPromptSuggestion('Next step: Deploy directly to production.'), /Deploy/);
});

test('Tab accepts a suggestion as an editable draft without sending; Escape dismisses', () => {
    const app = setup(() => {});
    app._draftScope = {key:'project:a'};
    let edits = 0, prevented = 0;
    app.userInput.dispatchEvent = event => { assert.equal(event.type, 'input'); edits++; };
    app.sendMessage = () => assert.fail('Accepting must never send');
    const key = name => ({key:name, preventDefault:() => prevented++});
    app._promptSuggestion = {text:'Add keyboard navigation.', scope:'project:a'};
    assert.equal(app._handlePromptSuggestionKey({...key('Tab'), shiftKey:true}), false);
    assert.equal(app._handlePromptSuggestionKey({...key('Tab'), isComposing:true}), false);
    assert.equal(app._handlePromptSuggestionKey(key('Tab')), true);
    assert.equal(app.userInput.value, 'Add keyboard navigation.');
    assert.equal(edits, 1);
    app.userInput.value = '';
    app._promptSuggestion = {text:'Another suggestion', scope:'project:a'};
    assert.equal(app._handlePromptSuggestionKey(key('Escape')), true);
    assert.equal(app.userInput.value, '');
    assert.equal(app._promptSuggestion, null);
    assert.equal(prevented, 2);
});

test('suggestion never replaces typed text or crosses a session boundary', () => {
    const app = setup(() => {});
    app._draftScope = {key:'project:b'};
    app._promptSuggestion = {text:'From A', scope:'project:a'};
    const tab = {key:'Tab', preventDefault:() => assert.fail('Keep normal Tab navigation')};
    assert.equal(app._handlePromptSuggestionKey(tab), false);
    app._promptSuggestion.scope = 'project:b';
    app.userInput.value = 'My own direction';
    assert.equal(app._handlePromptSuggestionKey(tab), false);
    app.userInput.value = '';
    app._fuzzyOpen = true;
    assert.equal(app._handlePromptSuggestionKey(tab), false);
    app._fuzzyOpen = false;
    app.isRunning = true;
    assert.equal(app._handlePromptSuggestionKey(tab), false);
});


test('completion suggestions preserve drafts and skip replay, errors, and queued follow-ups', () => {
    const app = setup(() => {});
    app._draftScope = {key:'project:a'};
    const task = {resultEl:{querySelectorAll:() => [{innerText:'Next steps\nAdd keyboard navigation to the project list.'}]}};
    app.userInput.value = 'My draft';
    app._offerPromptSuggestion({}, task);
    assert.equal(app.userInput.value, 'My draft');
    assert.equal(app._promptSuggestion, null);
    app.userInput.value = '';
    app.isReplaying = true;
    app._offerPromptSuggestion({}, task);
    assert.equal(app._promptSuggestion, null);
    app.isReplaying = false;
    app._agentRunErrored = true;
    app._offerPromptSuggestion({}, task);
    assert.equal(app._promptSuggestion, null);
    app._agentRunErrored = false;
    app._queuedMessages = new Map([['queued', {}]]);
    app._offerPromptSuggestion({}, task);
    assert.equal(app._promptSuggestion, null);
    app._queuedMessages.clear();
    app._offerPromptSuggestion({}, task);
    assert.equal(app._promptSuggestion.scope, 'project:a');
    assert.match(app.userInput.placeholder, /keyboard navigation/);
    assert.equal(app.userInput.value, '');
});

// Tool events drive the run's changed files; rendering is not under test.
function toolEventApp(fields = {}) {
    const app = setup(() => {});
    const noop = () => {};
    Object.assign(app, {
        removeThinking: noop, _setLiveRunPhase: noop, _finalizeLiveCollapsedGroup: noop,
        ensureStepRendered: noop, renderToolCall: noop, renderToolResult: noop,
        addToToolActivityGroup: noop, flushCollapsedGroup: noop, clearTerminals: noop,
        setRunning: noop, scrollToBottom: noop,
        _liveRunToolActivity: () => ({active: 'Editing', completed: 'Edited'}),
        activeTerminals: new Map(), subagentContainers: new Map(), subagentStreams: new Map(),
        handlesTools: false, _agentRunSummary: {title: '', fileChanges: [], todos: null},
    }, fields);
    app.play = (...events) => events.forEach(e => e.event === 'tool.call' ? app.handleToolCall(e) : app.handleToolResult(e));
    app.changes = () => Array.from(app._agentRunSummary.fileChanges, change => `${change.path}: ${change.detail}`);
    return app;
}
// As the server sends them: file_edit calls carry the diff of old_text to new_text.
const editCall = (callId, path) => ({event: 'tool.call', name: 'file_edit', call_id: callId,
    arguments: {path, old_text: 'alpha', new_text: 'beta'}, presentation: {kind: 'edit', locations: [path]},
    diff_lines: ['--- ', '+++ ', '@@ -1 +1 @@', '-alpha', '+beta']});
const toolResult = (call, fields = {}) => ({event: 'tool.result', name: call.name, call_id: call.call_id,
    output: 'ok', is_error: false, denied: false, ...fields});

test('a file change counts only when its own call succeeds', () => {
    const app = toolEventApp();
    const answer = text => ({resultEl: {querySelectorAll: () => [{innerText: text}]}});
    const rejected = editCall('call_1', 'notes.txt');
    const blocked = editCall('call_2', 'notes.txt');
    const failed = editCall('call_3', 'notes.txt');
    app.play(rejected, toolResult(rejected, {output: 'Tool execution denied by user.', denied: true}),
        blocked, toolResult(blocked, {output: 'Blocked by policy: denied', is_error: true, denied: true}),
        failed, toolResult(failed, {output: 'Error: old_text not found', is_error: true}),
        editCall('call_4', 'stopped.txt'));  // cancelled before it ran: no result
    assert.deepEqual(app.changes(), []);
    app._offerPromptSuggestion({outcome: 'incomplete'}, answer('The edit was rejected.'));
    assert.doesNotMatch(app._promptSuggestion.text, /these changes/);

    const accepted = editCall('call_5', 'notes.txt');
    const write = {event: 'tool.call', name: 'file_write', call_id: 'call_6',
        arguments: {path: 'docs/new.md', content: 'one\ntwo'}, presentation: {kind: 'write', locations: ['docs/new.md']}};
    app.play(accepted, write, toolResult(accepted), toolResult(write));
    assert.deepEqual(app.changes(), ['notes.txt: Diff +1 −1', 'docs/new.md: Wrote 2 lines']);
    app._offerPromptSuggestion({outcome: 'changed_unverified'}, answer('Updated the file.'));
    assert.match(app._promptSuggestion.text, /Review these changes/);

    // Without call ids, results answer calls in order.
    const idless = toolEventApp();
    const first = editCall('', 'first.txt'), second = editCall('', 'second.txt');
    idless.play(first, second, toolResult(first, {denied: true}), toolResult(second));
    assert.deepEqual(idless.changes(), ['second.txt: Diff +1 −1']);
});

test('CLI and worker tool events count only the run\'s own successful changes', () => {
    const cli = toolEventApp({handlesTools: true});
    const codex = (callId, path) => ({event: 'tool.call', name: 'codex_file_change', call_id: callId, external: true,
        arguments: {paths: [path]}, presentation: {kind: 'edit', locations: [path]}});
    const failed = codex('codex_1', 'src/a.py'), applied = codex('codex_2', 'src/b.py');
    cli.play(failed, toolResult(failed, {is_error: true}), applied, toolResult(applied, {changed_files: ['src/b.py']}));
    assert.deepEqual(cli.changes(), ['src/b.py: Edited']);

    // A worker's events can reuse the parent's call id; they settle nothing.
    const app = toolEventApp();
    const parent = editCall('call_1', 'parent.txt');
    const worker = {...editCall('call_1', 'worker.txt'), _subagent: true, _agent_id: 'w1'};
    app.play(parent, worker, {...toolResult(worker), _subagent: true, _agent_id: 'w1'});
    assert.deepEqual(app.changes(), []);
    app.play(toolResult(parent, {denied: true}));
    assert.deepEqual(app.changes(), []);
    const retry = editCall('call_1', 'parent.txt');  // same arguments, same id
    app.play(retry, toolResult(retry));
    assert.deepEqual(app.changes(), ['parent.txt: Diff +1 −1']);
});

test('a finished worker shows its handoff under its result line', () => {
    const app = setup(() => {});
    const done = app._subagentHandoffView({agent_type: 'build', steps: 3, elapsed: 2.14, handoff: {
        outcome: 'completed', summary: 'Edited the file.', changed_files: ['notes.txt'],
        validation: ['check_run: not run'], blockers: [],
        evidence: ['3 worker steps', '2.1s elapsed', 'Budget exhausted after useful output: step limit'],
        artifacts: [], recommended_next_action: 'Review and integrate the change.'}}, false);
    assert.equal(done.line, '✓ build · 3 steps · 2.1s · 1 file changed');
    assert.deepEqual(Array.from(done.sections, s => `${s.label}: ${s.items.join(' | ')}`),
        ['Changed files: notes.txt', 'Checks: check_run: not run', 'Evidence: Budget exhausted after useful output: step limit']);
    assert.equal(done.next, 'Review and integrate the change.');
    assert.equal(done.open, false);

    // A worker whose edit was rejected says so, and a failure opens its details.
    const rejected = app._subagentHandoffView({agent_type: 'build', steps: 1, elapsed: 0, handoff: {
        outcome: 'failed', changed_files: [], blockers: ['Tool execution denied by user.']}}, true);
    assert.equal(rejected.line, '✗ build · 1 step · 0.0s · no files changed');
    assert.equal(rejected.open, true);
    // Without a handoff nothing is claimed about files.
    assert.equal(app._subagentHandoffView({agent_type: 'explore', steps: 2, elapsed: 1}, false).line, '✓ explore · 2 steps · 1.0s');

    const html = app._subagentHandoffHtml(app._subagentHandoffView({agent_type: 'build', handoff: {
        changed_files: ['a"b<c>.txt', ...Array.from({length: 13}, (_, i) => `f${i}.py`)],
        blockers: ['<img src=x onerror=alert(1)>']}}, true));
    for (const unsafe of ['<img', 'a"b', '<c>']) assert.ok(!html.includes(unsafe), unsafe);
    assert.match(html, /data-file-path="a&quot;b&lt;c&gt;\.txt"/);
    assert.equal(html.match(/class="subagent-handoff-file"/g).length, 12);
    assert.match(html, /2 more/);
    assert.match(html, /<summary class="subagent-result is-error">✗ build · 0 steps · 0\.0s · 14 files changed<\/summary>/);
});

test('replay rebuilds changed files from saved results, not saved calls', () => {
    const app = toolEventApp();
    const rejected = editCall('call_1', 'notes.txt'), accepted = editCall('call_2', 'notes.txt');
    const interrupted = editCall('call_3', 'late.txt');
    app.replayDisplayEvents([rejected, toolResult(rejected, {denied: true}), interrupted]);
    assert.deepEqual(app.changes(), []);
    app.replayDisplayEvents([rejected, toolResult(rejected, {denied: true}), accepted, toolResult(accepted)]);
    assert.deepEqual(app.changes(), ['notes.txt: Diff +1 −1']);
});

// The application account must never inherit another provider's identity.
function accountView(settings = {}, sonnAccount, document = {}) {
    const context = vm.createContext({window: {}, document});
    const mixin = fs.readFileSync(path.join(__dirname, '../lumi/gui/static/settings_view.js'), 'utf8');
    vm.runInContext(mixin + '\nthis.View = LumiSettingsView;', context);
    const app = Object.create(context.View.prototype);
    app.settings = settings;
    app.sonnAccount = sonnAccount;
    app.providerConnections = {codex: {account: {type:'chatgpt',email:'other@example.test',planType:'pro'}}};
    return app;
}

test('a Codex connection never supplies the SONN account identity', () => {
    const app = accountView();
    assert.equal(app._accountSummary().name, 'SONN account');
    assert.equal(app._accountSummary().detail, 'Not connected to SONN');
    assert.equal(app._accountSummary().initials, 'S');
});

test('local display name preserves the authenticated SONN identifier and prepaid status', () => {
    const app = accountView({general: {display_name: 'Alex Morgan'}}, {
        user: 'user-fixture', billing: {enabled:true},
    });
    const summary = app._accountSummary();
    assert.equal(summary.name, 'Alex Morgan');
    assert.equal(summary.initials, 'AM');
    assert.equal(summary.detail, 'SONN · Prepaid credits');
    assert.equal(summary.status, 'Account: user-fixture');
    app.settings.general.display_name = ' ';
    assert.equal(app._accountSummary().name, 'user-fixture');
});

test('billing-off and failed SONN discovery never appear as a paid subscription', () => {
    const app = accountView({}, {user:'user-fixture',billing:{enabled:false}});
    assert.equal(app._accountSummary().detail, 'SONN · Billing off');
    app.sonnAccount = {error:'Account unavailable'};
    assert.equal(app._accountSummary().detail, 'Not connected to SONN');
    assert.equal(app._accountSummary().name, 'SONN account');
});

test('Settings say device management updates an MSI or PKG copy', () => {
    const app = accountView();
    app.escapeHtml = value => String(value).replace(/[&<>"']/g, c => `&#${c.charCodeAt(0)};`);
    app.updateStatus = {version: '0.20.0', mode: 'off', describe: 'the stable channel', installed_by: 'pkg',
        available: false, problems: []};
    assert.match(app._renderUpdateStatus(),
        /installed from the macOS installer package, so your organization’s device management updates it/);
    app.updateStatus.installed_by = 'msi';
    assert.match(app._renderUpdateStatus(), /installed from the MSI package, so/);
    app.updateStatus = {...app.updateStatus, installed_by: 'someday', managed_by: 'Acme'};
    assert.match(app._renderUpdateStatus(), /managed by Acme/);
    app.aboutInfo = {version: '0.20.0', license: 'MIT', installed_by: 'pkg'};
    assert.match(app._renderAbout(), /Installed by your organization’s device management\./);
});

test('Settings shortcut works from a composer draft without sending or clearing it', () => {
    const app = setup();
    app.userInput.value = 'Keep this draft';
    app._closeAccountMenu = () => {};
    const views = [];
    app.switchView = view => views.push(view);
    let prevented = false;
    app._handleKeyboardShortcut({target: {tagName: 'TEXTAREA'}, ctrlKey: true, key: ',', preventDefault() {prevented = true;}});
    assert.equal(prevented, true);
    assert.deepEqual(views, ['settings']);
    assert.equal(app.userInput.value, 'Keep this draft');
});

test('browser folder actions never block on a desktop server picker', () => {
    for (const consumer of ['register', 'new-session', null]) {
        const app = setup(() => {});
        app.send = () => assert.fail('Browser must not request a native dialog');
        const choices = [];
        app.registerProjectFolder = path => choices.push(['register', path]);
        app.startNewSession = path => choices.push(['new-session', path]);
        app.selectProjectFolder = path => choices.push([null, path]);
        app._promptForProjectPath = (_label, callback) => callback('D:/warehouse');
        app.openProjectFolder(consumer);
        assert.deepEqual(choices, [[consumer, 'D:/warehouse']]);
        assert.equal(app._pendingFolderPickConsumer, null);
    }
});

test('native page still opens the native picker and retains intent', () => {
    const app = setup(() => {}, {pywebview: {api: {}}});
    app.currentCwd = 'D:/current';
    const sent = [];
    app.send = message => sent.push(JSON.parse(JSON.stringify(message)));
    app.openProjectFolder('new-session');
    assert.deepEqual(sent, [{command: 'folder_dialog', directory: 'D:/current'}]);
    assert.equal(app._pendingFolderPickConsumer, 'new-session');
});

test('settings search finds field help and keeps stored values out of the index', () => {
    const app = accountView({api_keys:{sonn:'private-fixture-value'}});
    const pages = app._settingsPages(), sections = app._settingsSections();
    const matches = query => Array.from(app._matchingSettingsPages(pages, sections, query), page => page.id);
    assert.deepEqual(matches('private-fixture-value'), []);
    assert.deepEqual(matches('permission'), ['general']);
    assert.deepEqual(matches('Blender'), ['creative_editors']);
    assert.deepEqual(matches('SONN API key'), ['provider_connections']);
    assert.deepEqual(matches('Echo'), ['pets']);
});

test('all editable settings remain reachable through the category pages', () => {
    const app = accountView();
    for (const section of app._settingsSections()) {
        for (const field of section.fields || []) {
            assert.ok(app._settingsPages().some(page => page.sections.includes(section.id)
                && (!page.fields || page.fields.includes(field.key))), `${section.id}.${field.key}`);
        }
    }
});

test('background settings refresh defers while a field is being edited', () => {
    const editable = {value:'unfinished draft', matches:() => true};
    const app = accountView({}, null, {activeElement:editable});
    app._initSettingsNavigation = () => {};
    app.settingsBody = {contains:element => element === editable};
    app.renderSettingsView();
    assert.equal(app._settingsRenderPending, true);
    assert.equal(editable.value, 'unfinished draft');
});

test('settings requests are limited to the page being visited', () => {
    const app = accountView();
    const sent = [];
    app.send = message => sent.push(message.command);
    app._loadSettingsPage('general');
    assert.deepEqual(sent, []);
    app._loadSettingsPage('creative_editors');
    app._loadSettingsPage('creative_editors');
    assert.deepEqual(sent, ['editor_list']);
    app._loadSettingsPage('cost_tracking');
    assert.deepEqual(sent, ['editor_list','get_costs']);
});

test('opening another project during a run preserves view and sends no navigation', () => {
    const app = setup();
    app.isRunning = true;
    app.currentCwd = 'D:/original';
    app.currentSessionId = 'active';
    const notices = [];
    app.showToastMessage = message => notices.push(message);
    app.send = () => { throw new Error('must not navigate'); };
    app.selectProjectFolder('D:/other');
    assert.equal(app.currentCwd, 'D:/original');
    assert.equal(app.currentSessionId, 'active');
    assert.match(notices[0], /Finish or stop/);
});

// ── Launch access (static/local_access.js) ─────────────────────────────
const accessSource = fs.readFileSync(path.join(__dirname, '../lumi/gui/static/local_access.js'), 'utf8');

function loadAccess({hash = '', stored = null, fetch = async () => { throw new Error('unexpected fetch'); }} = {}) {
    const storage = new Map(stored ? [['lumi:access', stored]] : []);
    const replaced = [];
    const listeners = {};
    const reloads = [];
    const location = {hash, pathname: '/', search: '', reload: () => reloads.push(location.hash)};
    const context = vm.createContext({
        URLSearchParams, console, fetch, location,
        history: {state: null, replaceState: (state, title, url) => replaced.push(url)},
        localStorage: {getItem: key => storage.get(key) ?? null, setItem: (key, value) => storage.set(key, String(value))},
        addEventListener: (type, listener) => { listeners[type] = listener; },
    });
    context.window = context;
    vm.runInContext(accessSource, context);
    return {access: context.LumiLocalAccess, storage, replaced, listeners, location, reloads};
}

test('a launch link pasted into an open tab reloads so its code is redeemed', () => {
    const page = loadAccess({stored: 'stale'});
    page.location.hash = '#view=settings';
    page.listeners.hashchange();
    assert.deepEqual(page.reloads, []);
    page.location.hash = '#lumi-launch=fresh';
    page.listeners.hashchange();
    assert.deepEqual(page.reloads, ['#lumi-launch=fresh']);
});

test('a launch code leaves the address bar and is redeemed once for a stored token', async () => {
    const requests = [];
    const {access, storage, replaced} = loadAccess({hash: '#lumi-launch=abc', fetch: async (url, options) => {
        requests.push([url, options]);
        return {ok: true, json: async () => ({token: 'T1'})};
    }});
    assert.deepEqual(replaced, ['/']);
    assert.equal(await access.ready, 'T1');
    assert.equal(requests.length, 1);
    assert.equal(requests[0][0], '/api/access');
    assert.equal(requests[0][1].method, 'POST');
    assert.deepEqual(JSON.parse(requests[0][1].body), {code: 'abc'});
    assert.equal(storage.get('lumi:access'), 'T1');
    assert.deepEqual({...access.headers({'Content-Type': 'application/json'})},
        {'Content-Type': 'application/json', 'X-Lumi-Access': 'T1'});
    assert.deepEqual([...access.protocols()], ['lumi.v1', 'lumi.access.T1']);
});

test('a reopened launch link falls back to the token stored when it was first used', async () => {
    const {access} = loadAccess({hash: '#lumi-launch=spent', stored: 'T0', fetch: async () => ({ok: false, status: 403})});
    assert.equal(await access.ready, 'T0');
    const offline = loadAccess({hash: '#lumi-launch=abc', stored: 'T0', fetch: async () => { throw new TypeError('Failed to fetch'); }});
    assert.equal(await offline.access.ready, 'T0');
});

test('pages without a launch link keep other fragments and use the stored token', async () => {
    const plain = loadAccess({hash: '#view=settings', stored: 'T0'});
    assert.deepEqual(plain.replaced, []);
    assert.equal(await plain.access.ready, 'T0');
    const mixed = loadAccess({hash: '#lumi-launch=abc&view=settings', fetch: async () => ({ok: true, json: async () => ({token: 'T2'})})});
    assert.deepEqual(mixed.replaced, ['/#view=settings']);
    const none = loadAccess();
    assert.equal(await none.access.ready, '');
    assert.deepEqual([...none.access.protocols()], ['lumi.v1']);
});

test('the access check tells a refused token from an unreachable server', async () => {
    let respond;
    const {access} = loadAccess({stored: 'T0', fetch: (url, options) => respond(url, options)});
    respond = async (url, options) => {
        assert.equal(url, '/api/access');
        assert.equal(options.headers['X-Lumi-Access'], 'T0');
        return {status: 204};
    };
    assert.equal(await access.check(), true);
    respond = async () => ({status: 403});
    assert.equal(await access.check(), false);
    respond = async () => { throw new TypeError('Failed to fetch'); };
    assert.equal(await access.check(), null);
});

test('private requests wait for the launch token and carry it', async () => {
    let release;
    const calls = [];
    const LumiLocalAccess = {
        ready: new Promise(resolve => { release = resolve; }),
        headers: extra => ({...(extra || {}), 'X-Lumi-Access': 'T1'}),
    };
    const app = setup((url, options) => { calls.push([url, options]); return Promise.resolve({ok: true}); }, {LumiLocalAccess});
    const pending = app._localFetch('/api/ui-state', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}'});
    await tick();
    assert.equal(calls.length, 0);
    release('T1');
    await pending;
    assert.equal(calls[0][1].method, 'POST');
    assert.deepEqual(calls[0][1].headers, {'Content-Type': 'application/json', 'X-Lumi-Access': 'T1'});
});

test('a refused socket explains how to reconnect instead of retrying forever', async () => {
    const app = setup(() => {});
    const calls = [];
    app._showAccessRequired = () => calls.push('access');
    app.scheduleReconnect = () => calls.push('retry');
    await app._reconnectUnlessRefused({check: async () => false});
    await app._reconnectUnlessRefused({check: async () => null});
    await app._reconnectUnlessRefused({check: async () => true});
    await app._reconnectUnlessRefused(undefined);
    assert.deepEqual(calls, ['access', 'retry', 'retry', 'retry']);
});

test('capability pack review escapes repository text and approves only what it showed', () => {
    const app = accountView();
    const sent = [];
    app.send = message => sent.push(message.command);
    app._loadSettingsPage('capability_packs');
    assert.deepEqual(sent, ['capability_pack_list']);
    app.capabilityPacks = {packs: [
        {id: 'quality', name: 'Quality" onmouseover="alert(1)', version: '1.0', status: 'needs_approval',
         description: '<img src=x onerror=alert(1)>', path: "D:/repo/.resonant/packs/it's", scope: 'project',
         digest: 'a'.repeat(64), agents: [], skills: ['skill.md'], pinned_files: ['scripts/check.py'],
         hooks: [{hook_type: 'pre_tool_use', matcher: 'bash', command: 'python check.py "</pre><script>"'}],
         mcp_servers: {docs: {command: 'node', args: ['server.js', '--port=1']}}},
        {id: 'moved', name: 'Moved', version: '2', status: 'unverifiable', scope: 'project', digest: '',
         path: 'D:/repo/.resonant/packs/moved', problem: 'The pack cannot be verified because it contains a link: x.'},
    ]};
    const html = app._renderCapabilityPacks();
    for (const unsafe of ['onmouseover="', '<img', '<script>', "packs/it's"]) {
        assert.ok(!html.includes(unsafe), unsafe);
    }
    assert.match(html, /data-pack-digest="a{64}"/);
    assert.match(html, /python check\.py &quot;&lt;\/pre&gt;&lt;script&gt;&quot;/);
    assert.match(html, /node server\.js --port=1/);
    assert.match(html, /scripts\/check\.py/);
    // One approvable pack; an unverifiable pack offers nothing to approve.
    assert.equal(html.match(/data-pack-action="approve"/g).length, 1);
    assert.match(html, /contains a link/);
});

test('a prompt keeps amounts and version numbers in its question', () => {
    const app = setup(() => Promise.resolve({ok: true}));
    assert.equal(app._conciseAwaitUserQuestion("Today's spend is $0.27, past your $0.20 limit \u2014 continue anyway?"),
        "Today's spend is $0.27, past your $0.20 limit \u2014 continue anyway?");
    assert.equal(app._conciseAwaitUserQuestion('The tests pass. Upgrade to Python 3.12 now?'), 'Upgrade to Python 3.12 now?');
});

test('an edit approval goes in the conversation, where a running turn cannot hide it', () => {
    // A running task hides its activity rows, and a worker's block with them,
    // until the user opens the live status (styles.css), so an approval the
    // run waits on must not render there.
    const element = () => ({
        children: [], listeners: {}, buttons: {},
        appendChild(child) { this.children.push(child); return child; },
        addEventListener(type, handler) { this.listeners[type] = handler; },
        querySelector(selector) { return (this.buttons[selector] ||= element()); },
        querySelectorAll() { return Object.values(this.buttons); },
        replaceWith(next) { this.replacement = next; },
    });
    const app = setup(() => Promise.resolve({ok: true}), {document: {getElementById: () => null, createElement: element}});
    const sent = [];
    app.send = message => sent.push({...message});
    app.scrollToBottom = () => {};
    app._setSessionActivity = () => {};
    app.chatMessages = element();
    app._activeTask = {card: {isConnected: true}, activityEl: element()};
    app.subagentContainer = element();
    const review = {file_path: 'notes.txt', hunks: [{old_start: 1, old_count: 1, new_start: 1, new_count: 1,
                                                     lines: ['-old line', '+new line']}]};

    app._renderInlineDiffPermission('file_edit', {path: 'notes.txt'}, review, 'request-1');

    assert.equal(app.chatMessages.children.length, 1);
    assert.equal(app._activeTask.activityEl.children.length, 0);
    assert.equal(app.subagentContainer.children.length, 0);
    app.chatMessages.children[0].querySelector('[data-action="accept"]').listeners.click();
    assert.deepEqual(sent, [{command: 'approve', approved: true, request_id: 'request-1'}]);
});

test('escaped text is safe inside attribute values', () => {
    const app = setup(() => Promise.resolve({ok: true}));
    // A check command with quotes used to end value="..." early on re-render.
    assert.equal(app.escapeHtml(`python -c "print('x')" <&>`), 'python -c &quot;print(&#39;x&#39;)&quot; &lt;&amp;&gt;');
    assert.equal(app.escapeHtml(null), '');
    assert.equal(app.escapeHtml(undefined), '');
    assert.equal(app.escapeHtml(3), '3');
});


// ── Untrusted text renders as text ────────────────────────────────────
// Repository contents (commit messages, branch and file names), model output
// (tool arguments, plan goals) and MCP tool names all reach innerHTML. Each
// must stay text: no new elements, and no way out of an attribute value.

const UNTRUSTED = 'x" onmouseover="window.pwned=1"><img src=x onerror="window.pwned=1">';
const UNTRUSTED_AS_TEXT = 'x&quot; onmouseover=&quot;window.pwned=1&quot;&gt;&lt;img src=x onerror=&quot;window.pwned=1&quot;&gt;';

function assertRenderedAsText(html, where) {
    assert.ok(!/<img/i.test(html), `${where}: untrusted text became an element`);
    assert.ok(!html.includes('" onmouseover="') && !html.includes('onerror="'), `${where}: untrusted text became an attribute`);
    assert.ok(html.includes(UNTRUSTED_AS_TEXT), `${where}: untrusted text should still be shown`);
}

// Serializes like a browser's text node: & < > only, never quotes.
function browserTextElement() {
    let text = '';
    return {
        set textContent(value) { text = String(value); },
        get innerHTML() { return text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;'); },
    };
}

test('repository text in the Git panel and hook commands renders as text', () => {
    const body = {innerHTML: ''};
    const app = accountView({}, null, {getElementById: id => (id === 'git-popover-body' ? body : null)});
    // The settings view is mixed into the app, which supplies escapeHtml.
    app.escapeHtml = setup(() => {}).escapeHtml;
    app.gitData = {
        is_repo: true,
        branch: UNTRUSTED,
        changes: [{status: 'M', file: UNTRUSTED}],
        commits: [{hash: UNTRUSTED, message: UNTRUSTED}],
    };

    app._renderGitPopoverTab('changes');
    assertRenderedAsText(body.innerHTML, 'changed file');
    app._renderGitPopoverTab('commits');
    assert.equal(body.innerHTML.split(UNTRUSTED_AS_TEXT).length - 1, 2, 'commit hash and message');
    assertRenderedAsText(body.innerHTML, 'commit');
    assertRenderedAsText(app._gitPopoverHtml(app.gitData), 'branch');
    assertRenderedAsText(app._renderHooksList([{hook_type: 'pre_tool_use', name: UNTRUSTED, command: UNTRUSTED, enabled: true}]), 'hook');
});

test('tool names and arguments from a model or MCP server render as text', () => {
    const rows = [];
    const document = {
        getElementById: () => null,
        createElement: () => {
            const element = {innerHTML: '', className: '', setAttribute() {}};
            rows.push(element);
            return element;
        },
    };
    const app = setup(() => {}, {document});
    app.getRenderTarget = () => ({appendChild() {}});
    app.scrollToBottom = () => {};

    app.renderToolCall({name: UNTRUSTED, arguments: {}});
    app.renderToolCall({name: 'computer_click', arguments: {x: UNTRUSTED, y: UNTRUSTED}});
    app.renderToolCall({name: 'computer_scroll', arguments: {direction: UNTRUSTED, amount: UNTRUSTED}});
    assert.equal(rows.length, 3);
    rows.forEach((row, index) => assertRenderedAsText(row.innerHTML, `native tool row ${index}`));

    // CLI providers report tool names too, including their MCP tools.
    const context = vm.createContext({console, document, window: {}});
    vm.runInContext(source + '\nthis.App = LumiApp;', context);
    vm.runInContext(fs.readFileSync(path.join(__dirname, '../lumi/gui/static/run_cards.js'), 'utf8'), context);
    const cards = Object.create(context.window.LumiRunCards.prototype);
    cards.escapeHtml = context.App.prototype.escapeHtml;
    cards.scrollToBottom = () => {};
    cards.activeToolGroupCount = 0;
    cards.activeToolGroupCounts = {};
    cards.activeToolGroup = {querySelector: () => ({appendChild() {}})};
    rows.length = 0;
    cards.addToToolActivityGroup({name: UNTRUSTED, arguments: {}});
    assertRenderedAsText(rows[0].innerHTML, 'CLI tool activity');
});

test('model-written plan goals stay inside their attributes', () => {
    const canvas = {innerHTML: '', style: {}, querySelectorAll: () => []};
    const document = {
        getElementById: id => (id === 'plan-graph-canvas' ? canvas : null),
        createElement: () => browserTextElement(),
    };
    const window = {};
    vm.runInContext(
        fs.readFileSync(path.join(__dirname, '../lumi/gui/static/plan_graph_view.js'), 'utf8'),
        vm.createContext({window, document, console}),
    );

    window.PlanGraphView.render({intent: 'Fix it', intent_id: 'i1', nodes: [
        {id: 'n1', goal: UNTRUSTED, status: 'running', specialization: 'implement'},
    ]});

    assertRenderedAsText(canvas.innerHTML, 'plan node');
    assert.equal(canvas.innerHTML.split(UNTRUSTED_AS_TEXT).length - 1, 2, 'goal title and text');
});

// Worker transcripts and controls: the runtime pane that offered them left in
// v0.14.0. A running worker's controls sit in the run's Sub-tasks list; a
// stopped worker's block offers its transcript and, unless it completed, a restart.
const WORKER = 'agt_0123456789ab';

test('a worker offers only the controls its status allows', () => {
    const app = setup(() => {});
    assert.deepEqual(Array.from(app._workerLiveActions('running')), ['pause', 'cancel', 'steer']);
    assert.deepEqual(Array.from(app._workerLiveActions('paused')), ['resume', 'cancel', 'steer']);
    assert.deepEqual(Array.from(app._workerLiveActions('stuck')), []);
    // A stopped worker has no live thread to pause or steer.
    assert.deepEqual(Array.from(app._workerBlockActions('completed')), ['transcript']);
    for (const status of ['failed', 'cancelled', 'stuck']) {
        assert.deepEqual(Array.from(app._workerBlockActions(status)), ['transcript', 'restart'], status);
    }
    assert.deepEqual(Array.from(app._workerBlockActions('running')), []);
    assert.equal(app._workerStatusNote({status: 'stuck', steps: 1}), 'Interrupted when Lumi closed after 1 step');
    assert.equal(app._workerStatusNote({status: 'cancelled', steps: 3}), 'Stopped after 3 steps');
    assert.equal(app._workerStatusNote({status: 'completed', steps: 3}), '');
    // Only the agent registry's workers can be controlled.
    assert.equal(app._isRegistryWorker(WORKER), true);
    assert.equal(app._isRegistryWorker('task:call_1'), false);
});

test('a running worker\'s Sub-tasks row carries its controls', () => {
    const app = setup(() => {});
    const html = app._workerLiveControlsHtml({agentId: WORKER, status: 'running', label: 'build "x"'});
    for (const action of ['pause', 'cancel', 'steer']) assert.match(html, new RegExp(`data-worker-action="${action}"`));
    assert.match(html, /aria-label="Stop the build &quot;x&quot; worker"/);
    const paused = app._workerLiveControlsHtml({agentId: WORKER, status: 'running', paused: true, label: 'build'});
    assert.match(paused, /data-worker-action="resume"/);
    assert.doesNotMatch(paused, /data-worker-action="pause"/);
    // Nothing once it finished or is stopping, and nothing without a registry id.
    assert.equal(app._workerLiveControlsHtml({agentId: WORKER, status: 'done', label: 'build'}), '');
    assert.equal(app._workerLiveControlsHtml({agentId: WORKER, status: 'running', stopping: true}), '');
    assert.equal(app._workerLiveControlsHtml({agentId: 'task:call_1', status: 'running'}), '');
});

test('a worker control sends its command, and a restart waits for the current run', () => {
    const app = setup(() => {}, {setTimeout: () => 0});
    const sent = [], messages = [], opened = [], marked = [];
    app.send = (message) => sent.push(`${message.command}:${message.action || message.agent_id}`);
    app.showStatusMessage = (text) => messages.push(text);
    app.openWorkerTranscript = (id) => opened.push(`transcript:${id}`);
    app.openWorkerSteer = (id, label) => opened.push(`steer:${id}:${label}`);
    app._markLiveWorker = (id, patch) => marked.push(`${id}:${patch.stopping}`);
    const button = (action, agentId = WORKER) => ({
        dataset: {workerAction: action, agentId, workerLabel: 'build'}, disabled: false, textContent: action,
    });

    for (const action of ['pause', 'resume', 'cancel', 'transcript', 'steer']) app._onWorkerAction(button(action));
    app._onWorkerAction(button('pause', 'task:call_1'));  // not the registry's: ignored
    assert.deepEqual(sent, ['agent_runtime_control:pause', 'agent_runtime_control:resume', 'agent_runtime_control:cancel']);
    assert.deepEqual(marked, [`${WORKER}:true`]);
    assert.deepEqual(opened, [`transcript:${WORKER}`, `steer:${WORKER}:build`]);

    app.isRunning = true;
    const blocked = button('restart');
    app._onWorkerAction(blocked);
    assert.equal(sent.length, 3);
    assert.match(messages.at(-1), /Finish or stop the current run/);
    assert.equal(blocked.disabled, false);

    app.isRunning = false;
    const restart = button('restart');
    app._onWorkerAction(restart);
    assert.equal(sent.at(-1), `agent_restart:${WORKER}`);
    assert.equal(restart.disabled, true);
});

test('a worker transcript pairs each call with its own result', () => {
    const app = setup(() => {});
    const call = (id, name, label, location, args = {}) => ({
        event: 'tool.call', name, call_id: id, arguments: args, presentation: {label, locations: location ? [location] : []},
    });
    const entries = app._workerTranscriptEntries([
        {event: 'text.delta', delta: 'Look'},
        call('c1', 'file_edit', 'Edit file', 'notes.txt'),
        call('c2', 'bash', 'Run command', '', {command: 'pytest -q'}),
        {event: 'tool.result', name: 'bash', call_id: 'c2', output: '1 failed', is_error: true, denied: false},
        {event: 'tool.result', name: 'file_edit', call_id: 'c1', output: 'Tool execution denied by user.', is_error: false, denied: true},
        {event: 'steer.applied', text: 'Keep the rest unchanged'},
        call('c3', 'file_write', 'Write file', 'new.txt'),
        {event: 'text.done', text: 'Done here.'},
        {event: 'error', message: 'Interrupted'},
    ]);
    assert.deepEqual(Array.from(entries, (e) => [e.kind, e.label || e.text, e.target || '', e.outcome || ''].join('|')), [
        'tool|Edit file|notes.txt|denied',
        'tool|Run command|pytest -q|failed',
        'steer|Keep the rest unchanged||',
        'tool|Write file|new.txt|no result',
        'text|Done here.||',
        'error|Interrupted||',
    ]);
});

test('a restart shows as a turn of its own', () => {
    const app = setup(() => {});
    const calls = [];
    app._prepareTurnUI = (text) => calls.push(`turn:${text}`);
    app.setRunning = (running) => calls.push(`running:${running}`);
    app.handleEvent({event: 'agent.restarted', source_agent_id: WORKER,
        display_text: 'Restarting build agent (interrupted after 2 steps)'});
    assert.deepEqual(calls, ['turn:Restarting build agent (interrupted after 2 steps)', 'running:true']);
});

test('a replayed turn that never ended is shown stopped, with its work reachable', () => {
    const app = setup(() => {});
    const collapsed = [];
    app._collapseTaskActivity = () => collapsed.push('collapsed');
    // The replayed step's ticking "thinking" row goes before the collapse.
    app.removeThinking = () => collapsed.push('thinking');
    const card = (classes, connected = true) => {
        const set = new Set(classes);
        return {isConnected: connected, classList: {
            contains: (name) => set.has(name), remove: (...names) => names.forEach((n) => set.delete(n)),
            add: (...names) => names.forEach((n) => set.add(n)), list: () => Array.from(set).sort().join(' ')}};
    };
    const task = (c) => ({card: c, stateEl: {className: '', textContent: 'Running'}});

    const running = task(card(['task-card', 'task-card-running']));
    app._activeTask = running;
    app._settleInterruptedCard();
    assert.equal(running.card.classList.list(), 'task-card task-card-stopped');
    assert.equal(running.stateEl.textContent, 'Interrupted');
    app._activeTask = task(card(['task-card', 'task-card-running']));
    app._settleInterruptedCard('Paused');
    assert.equal(app._activeTask.stateEl.textContent, 'Paused');
    assert.deepEqual(collapsed, ['thinking', 'collapsed', 'thinking', 'collapsed']);

    // A finished card, or one no longer on the page, is left alone.
    for (const other of [task(card(['task-card', 'task-card-done'])), task(card(['task-card', 'task-card-running'], false))]) {
        app._activeTask = other;
        app._settleInterruptedCard();
        assert.equal(other.stateEl.textContent, 'Running');
    }
    assert.equal(collapsed.length, 4);
});

// The Timeline: the open conversation's checkpoints, and what each restores.
test('a checkpoint is named by what it was saved before', () => {
    const app = setup(() => {});
    const label = (tool_name, target = '') => app._timelineLabel({tool_name, target});
    assert.equal(label('file_write', 'docs/notes.md'), 'Before writing docs/notes.md');
    assert.equal(label('file_edit', 'notes.txt'), 'Before editing notes.txt');
    assert.equal(label('bash', 'npm test'), 'Before running npm test');
    assert.equal(label('bash', 'x'.repeat(80)), `Before running ${'x'.repeat(57)}…`);
    assert.equal(label('git_commit', 'Fix it'), 'Before committing');
    assert.equal(label('git_branch_create', 'feature'), 'Before creating branch feature');
    assert.equal(label('batch'), 'Before a batch of changes');
    assert.equal(app._timelineLabel({tool_name: 'mystery', reason: 'Before mystery'}), 'Before mystery');
});

test('the Timeline never promises checkpoints for a CLI connection\'s own changes', () => {
    const app = setup(() => {});
    for (const [value, expected] of [['ollama:qwen3', /^Before each change it makes, Lumi saves a checkpoint/],
        ['codex:gpt-5.3-codex', /^Codex changes files with its own tools, so Lumi saves no checkpoints/],
        ['claude-code:sonnet', /^Claude Code changes files with its own tools/]]) {
        app.modelSelector = {value};
        assert.match(app._timelineIntro(), expected);
    }
});

test('a checkpoint restores only what it holds, and says so first', () => {
    const app = setup(() => {});
    const modes = (item) => Array.from(app._timelineRestoreModes(item));
    assert.deepEqual(modes({snapshot: 'git'}), ['files', 'conversation', 'both']);
    // A worker's checkpoint holds the worker's conversation: files only.
    assert.deepEqual(modes({snapshot: 'archive', subagent: true}), ['files']);
    assert.deepEqual(modes({snapshot: ''}), ['conversation']);
    assert.match(app._timelineRestoreNote({snapshot: 'git'}, 'files'), /kept first, on a lumi-recovery branch\. The conversation doesn't change/);
    assert.match(app._timelineRestoreNote({snapshot: 'archive'}, 'both'), /recovery archive\. The conversation goes back to this point/);
    assert.match(app._timelineRestoreNote({snapshot: 'git'}, 'conversation'), /later messages leave it\. Your files don't change/);
    assert.equal(app._timelineRestoredMessage({mode: 'files', checkpoint: {tool_name: 'file_edit', target: 'notes.txt'},
        workspace: {recovery_branch: 'lumi-recovery/20260925'}}),
        'Files restored to before editing notes.txt. Your previous files are on lumi-recovery/20260925.');
    assert.equal(app._timelineRestoredMessage({mode: 'conversation', checkpoint: {tool_name: 'bash', target: 'make'}}),
        'Conversation restored to before running make.');
});

test('restoring asks first, waits for the current run, and recovers from a refusal', () => {
    // Focus moves within the dialog; the rows under test aren't rendered here.
    const app = setup(() => {}, {document: {getElementById: () => null, querySelector: () => null},
        CSS: {escape: (value) => value}});
    const sent = [], messages = [];
    app.send = (message) => sent.push(`${message.command}:${message.checkpoint_id || ''}:${message.mode || ''}`);
    app.showStatusMessage = (text) => messages.push(text);
    app._renderTimeline = () => {};
    app.runtimeTimeline = [{id: 'cp_1', tool_name: 'file_edit', target: 'notes.txt', snapshot: 'git'},
        {id: 'cp_2', tool_name: 'file_write', target: 'w.txt', snapshot: 'archive', subagent: true}];
    const button = (action, id) => ({dataset: {timelineAction: action}, closest: () => ({dataset: {checkpointId: id}})});

    app._onTimelineAction(button('restore', 'cp_2'));
    assert.deepEqual({...app._timelineConfirm}, {id: 'cp_2', mode: 'files'});
    app._onTimelineAction(button('cancel', 'cp_2'));
    assert.equal(app._timelineConfirm, null);
    app._onTimelineAction(button('compare', 'cp_1'));
    app._onTimelineAction(button('restore', 'cp_1'));
    assert.equal(sent.length, 1);  // choosing isn't restoring

    app.isRunning = true;
    app._onTimelineAction(button('confirm', 'cp_1'));
    assert.match(messages.at(-1), /Stop the current run/);
    app.isRunning = false;
    app._onTimelineAction(button('confirm', 'cp_1'));
    assert.deepEqual(sent, ['session_timeline_compare:cp_1:', 'session_timeline_restore:cp_1:files']);
    assert.equal(app._timelinePending, 'cp_1');

    // A refusal (an error event) lets the user try again; showing the error
    // itself is handleError's job.
    app.handleError = () => {};
    app.handleEvent({event: 'error', message: 'Stop the active run before restoring a checkpoint'});
    assert.equal(app._timelinePending, '');
});

test('a restored conversation stops at its checkpoint instead of replaying as a crash', () => {
    const notes = [];
    const app = setup(() => {}, {document: {getElementById: () => null, createElement: () => ({})}});
    app.chatMessages = {appendChild: (el) => notes.push(el)};
    // Saved before the turn's first change: the turn had only started.
    const turn = [{event: 'user_message', text: 'write the notes'}, {event: 'step.start', step: 1}];
    const marker = {event: 'timeline.restored', mode: 'both', checkpoint: {tool_name: 'file_write', target: 'notes.txt'}};
    assert.equal(app._interruptedReplayRecovery(turn).kind, 'not_started');
    assert.equal(app._interruptedReplayRecovery([...turn, marker]), null);
    // A later turn that stopped still offers its Retry.
    const later = [...turn, marker, {event: 'user_message', text: 'again'}, {event: 'step.start', step: 1}];
    assert.equal(app._interruptedReplayRecovery(later).kind, 'not_started');

    app._addTimelineRestoredNote(marker);
    assert.equal(notes[0].className, 'timeline-restored-note');
    assert.equal(notes[0].textContent, 'Files and conversation restored to before writing notes.txt.');
});

// A run's trace and saved files, at the end of its card's work details.
test('a trace row says what happened, never what a call contained', () => {
    const app = setup(() => {});
    const label = (row) => app._traceRowLabel(row);
    assert.equal(label({event: 'tool.call', name: 'file_write', target: 'notes.txt'}), 'Called file_write: notes.txt');
    assert.equal(label({event: 'tool.result', name: 'bash', elapsed: 0.25, chars: 600, artifact: 'bash result'}),
        'bash finished in 250 ms · 600 characters · saved “bash result”');
    assert.equal(label({event: 'tool.result', name: 'bash', is_error: true, elapsed: 1.5}), 'bash failed in 1.50s');
    assert.equal(label({event: 'tool.result', name: 'task', denied: true}), 'task was refused');
    assert.equal(label({event: 'status', tokens: [120, 30]}), 'Model call: 120 tokens in, 30 out');
    assert.equal(label({event: 'status', tokens: [1, 1]}), 'Model call: 1 token in, 1 out');
    assert.equal(label({event: 'step.start', step: 2, detail: 'after file_write'}), 'Step 2 (after file_write)');
    assert.equal(label({event: 'session.end', outcome: 'changed_unverified'}), 'Finished: changed, not verified');
    assert.equal(label({event: 'mystery.event'}), 'mystery.event');
    assert.equal(app._traceTime(0.5), '+0.50s');
    assert.equal(app._traceTime(75), '+1m 15s');
});

test('a run keeps the files it saved, a worker\'s included', () => {
    const app = setup(() => {});
    app._liveRun = null;
    app.renderToolResult = () => {};
    app._activeTask = {};
    const result = (metadata) => ({event: 'tool.result', name: 'bash', _subagent: true, metadata});
    app.handleToolResult(result({artifact: {id: 'art_1', label: 'bash result', size: 61000}}));
    app.handleToolResult(result({}));
    assert.deepEqual(Array.from(app._activeTask.artifacts, (saved) => saved.id), ['art_1']);
    assert.equal(app._artifactName(app._activeTask.artifacts[0]), 'bash result · 60 KB');
    assert.equal(app._formatBytes(0), '');
    assert.equal(app._formatBytes(512), '512 B');
    assert.equal(app._formatBytes(3.5 * 1024 * 1024), '3.5 MB');
});

test('a trace or saved file that can\'t be read says so in its own dialog', () => {
    const app = setup(() => {});
    const renders = [];
    app._renderRunTrace = () => renders.push('trace');
    app._renderArtifact = () => renders.push('artifact');
    app.handleError = () => renders.push('chat');
    app._runTrace = {turn_id: 'turn_1', data: null};
    app.handleEvent({event: 'error', source: 'trace', turn_id: 'turn_1', message: "This run's trace is no longer saved."});
    assert.equal(app._runTrace.error, "This run's trace is no longer saved.");
    // A failed save keeps the trace on show.
    app._runTrace = {turn_id: 'turn_1', data: {rows: []}, exporting: true};
    app.handleEvent({event: 'error', source: 'trace', turn_id: 'turn_1', message: 'The disk is full.'});
    assert.equal(app._runTrace.exportError, 'The disk is full.');
    assert.equal(app._runTrace.exporting, false);
    assert.equal(app._runTrace.error, undefined);
    app._artifactView = {id: 'art_1', loading: true};
    app.handleEvent({event: 'error', source: 'artifact', artifact_id: 'art_1', message: 'This file is no longer saved.'});
    assert.equal(app._artifactView.error, 'This file is no longer saved.');
    assert.deepEqual(renders, ['trace', 'trace', 'artifact']);  // never a chat error
});

test('the saved-file viewer adds each page it is shown, and only its own', () => {
    const app = setup(() => {});
    app._renderArtifact = () => {};
    app._artifactView = {id: 'art_1', artifact: {id: 'art_1'}, text: null, next: null, loading: true};
    app._receiveArtifactView({artifact: {id: 'art_1', label: 'bash result'}, text: 'aaa', offset: 0, next_offset: 3});
    app._receiveArtifactView({artifact: {id: 'art_2'}, text: 'zzz', offset: 3});
    app._receiveArtifactView({artifact: {id: 'art_1'}, text: 'bbb', offset: 3, next_offset: null});
    assert.equal(app._artifactView.text, 'aaabbb');
    assert.equal(app._artifactView.next, null);
    assert.equal(app._artifactView.artifact.label, 'bash result');
});

test('a turn\'s trace is saved for OpenTelemetry once, from its own dialog', () => {
    const app = setup(() => {});
    const sent = [];
    app.send = (message) => sent.push(`${message.command}:${message.run_id}:${message.turn_id}`);
    app._renderRunTrace = () => {};
    app._runTrace = {run_id: 'run_1', turn_id: 'turn_1', data: {rows: []}};
    app._onRunTraceAction('export');
    app._onRunTraceAction('export');
    assert.deepEqual(sent, ['flight_recorder_export:run_1:turn_1']);
    app.handleEvent({event: 'artifact.created', turn_id: 'turn_2', artifact: {id: 'art_8'}});
    assert.equal(app._runTrace.exported, undefined);
    app.handleEvent({event: 'artifact.created', turn_id: 'turn_1', artifact: {id: 'art_9', path: 'C:/trace.json'}});
    assert.equal(app._runTrace.exported.id, 'art_9');
    assert.equal(app._runTrace.exporting, false);
});

// ── A refused call says why ───────────────────────────────────────────
// Its row shows the reason the model was told: a hook's message, a policy
// rule, an approval nobody could answer. The person's own Deny needs none.

// Enough DOM for tool rows. innerHTML is parsed into elements and every
// write is kept, so a test can check untrusted text never became markup.
// querySelector reads only the compound selectors the rows use (tag,
// .class, [attr], [attr="value"], :not([attr])) and throws on anything
// else, so a selector built from an unescaped name fails.
function fakeDom() {
    const htmlWrites = [];
    const entities = {amp: '&', lt: '<', gt: '>', quot: '"', '#39': "'"};
    const decode = text => text.replace(/&(amp|lt|gt|quot|#39);/g, (_, name) => entities[name]);
    const unescapeCss = text => text.replace(/\\([0-9a-f]{1,6} ?|[^0-9a-f])/gi,
        (_, code) => (/^[0-9a-f]/i.test(code) ? String.fromCodePoint(parseInt(code, 16)) : code));
    const compile = selector => {
        const checks = [];
        let rest = selector;
        const take = pattern => {
            const match = pattern.exec(rest);
            if (match) rest = rest.slice(match[0].length);
            return match;
        };
        let match = take(/^[a-z]+/i);
        if (match) {
            const tag = match[0].toUpperCase();
            checks.push(el => el.tagName === tag);
        }
        while (rest) {
            if ((match = take(/^\.([\w-]+)/))) {
                const name = match[1];
                checks.push(el => el.classList.contains(name));
            } else if ((match = take(/^\[([\w-]+)(?:="((?:\\.|[^"\\])*)")?\]/))) {
                const [, attr, raw] = match;
                checks.push(el => el.hasAttribute(attr) && (raw === undefined || el.getAttribute(attr) === unescapeCss(raw)));
            } else if ((match = take(/^:not\(\[([\w-]+)\]\)/))) {
                const attr = match[1];
                checks.push(el => !el.hasAttribute(attr));
            } else {
                throw new Error(`fake DOM cannot read the selector ${JSON.stringify(selector)}`);
            }
        }
        return el => checks.every(check => check(el));
    };
    const detach = node => {
        if (node.parentNode) node.parentNode.childNodes.splice(node.parentNode.childNodes.indexOf(node), 1);
        node.parentNode = null;
    };
    class Text {
        constructor(text) { this.nodeType = 3; this.textContent = text; this.parentNode = null; }
    }
    class Element {
        constructor(tag) {
            this.tagName = tag.toUpperCase();
            this.nodeType = 1;
            this.childNodes = [];
            this.parentNode = null;
            this.attributes = new Map();
            this.style = {};
            this.listeners = {};
            const attr = key => `data-${key.replace(/[A-Z]/g, c => `-${c.toLowerCase()}`)}`;
            this.dataset = new Proxy({}, {
                get: (_, key) => (typeof key === 'string' && this.hasAttribute(attr(key)) ? this.getAttribute(attr(key)) : undefined),
                set: (_, key, value) => { this.setAttribute(attr(key), value); return true; },
            });
            const classes = () => this.className.split(/\s+/).filter(Boolean);
            this.classList = {
                contains: name => classes().includes(name),
                add: (...names) => { this.className = [...new Set([...classes(), ...names])].join(' '); },
                remove: (...names) => { this.className = classes().filter(name => !names.includes(name)).join(' '); },
                toggle: name => {
                    const on = !classes().includes(name);
                    if (on) this.classList.add(name); else this.classList.remove(name);
                    return on;
                },
            };
        }
        get className() { return this.getAttribute('class') || ''; }
        set className(value) { this.setAttribute('class', value); }
        get children() { return this.childNodes.filter(node => node.nodeType === 1); }
        get nextElementSibling() {
            const siblings = this.parentNode ? this.parentNode.children : [];
            return siblings[siblings.indexOf(this) + 1] || null;
        }
        get textContent() { return this.childNodes.map(node => node.textContent).join(''); }
        set textContent(value) {
            this.childNodes.slice().forEach(detach);
            if (String(value)) this.appendChild(new Text(String(value)));
        }
        set innerHTML(html) {
            html = String(html);
            htmlWrites.push(html);
            this.childNodes.slice().forEach(detach);
            const open = [this];
            let at = 0;
            for (const token of html.matchAll(/<\/([a-z]+)\s*>|<([a-z]+)((?:\s+[\w-]+(?:="[^"]*")?)*)\s*>|[^<]+/gi)) {
                if (token.index !== at) break;
                at += token[0].length;
                const parent = open[open.length - 1];
                if (token[1]) {
                    if (parent.tagName !== token[1].toUpperCase()) break;
                    open.pop();
                } else if (token[2]) {
                    const el = parent.appendChild(new Element(token[2]));
                    for (const [, name, value = ''] of token[3].matchAll(/([\w-]+)(?:="([^"]*)")?/g)) {
                        el.setAttribute(name, decode(value));
                    }
                    open.push(el);
                } else if (token[0].trim()) {
                    parent.appendChild(new Text(decode(token[0])));
                }
            }
            if (at !== html.length || open.length !== 1) throw new Error(`fake DOM cannot parse ${JSON.stringify(html)}`);
        }
        setAttribute(name, value) { this.attributes.set(name, String(value)); }
        getAttribute(name) { return this.attributes.has(name) ? this.attributes.get(name) : null; }
        hasAttribute(name) { return this.attributes.has(name); }
        addEventListener(type, handler) { this.listeners[type] = handler; }
        appendChild(node) { return this.insertBefore(node, null); }
        insertBefore(node, reference) {
            detach(node);
            const index = reference ? this.childNodes.indexOf(reference) : this.childNodes.length;
            assert.ok(index >= 0, 'insertBefore needs a child of this element');
            this.childNodes.splice(index, 0, node);
            node.parentNode = this;
            return node;
        }
        remove() { detach(this); }
        matches(selector) { return compile(selector)(this); }
        querySelectorAll(selector) {
            const matches = compile(selector);
            const found = [];
            const visit = el => el.children.forEach(child => {
                if (matches(child)) found.push(child);
                visit(child);
            });
            visit(this);
            return found;
        }
        querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
    }
    return {
        htmlWrites,
        document: {createElement: tag => new Element(tag), getElementById: () => null, querySelector: () => null},
        // Like the browser's CSS.escape for these values: every character
        // that can't stand bare in an identifier becomes a hex escape.
        CSS: {escape: value => String(value).replace(/[^\w\u00a0-\uffff-]/g, c => `\\${c.codePointAt(0).toString(16)} `)},
    };
}

// The real tool-call and tool-result handlers, drawing into a task card's
// activity, with the Evidence group mixed in from run_cards.js.
function toolRowApp() {
    const dom = fakeDom();
    const context = vm.createContext({console, document: dom.document, CSS: dom.CSS, window: {}});
    vm.runInContext(source + '\nthis.App = LumiApp;', context);
    vm.runInContext(fs.readFileSync(path.join(__dirname, '../lumi/gui/static/run_cards.js'), 'utf8'), context);
    const cards = context.window.LumiRunCards.prototype;
    const app = Object.create(context.App.prototype);
    const noop = () => {};
    const activity = dom.document.createElement('div');
    Object.assign(app, {
        _appendToLiveCollapsedGroup: cards._appendToLiveCollapsedGroup,
        _finalizeLiveCollapsedGroup: cards._finalizeLiveCollapsedGroup,
        removeThinking: noop, addThinking: noop, _setLiveRunPhase: noop, _advanceLiveMilestone: noop,
        ensureStepRendered: noop, scrollToBottom: noop,
        trackTerminalStart: noop, trackTerminalEnd: noop,
        _liveRunToolActivity: () => ({active: 'Working', completed: 'Worked'}),
        _ensureTaskCard: () => ({activityEl: activity}),
        activeTerminals: new Map(), subagentContainers: new Map(), subagentStreams: new Map(),
        _blockToolRows: new Map(), stepToolCalls: [], stepToolResults: [], stepIsInlineOnly: false,
        collapsedGroup: [], handlesTools: false, _agentRunSummary: {title: '', fileChanges: [], todos: null},
    });
    app.activity = activity;
    app.htmlWrites = dom.htmlWrites;
    app.element = tag => dom.document.createElement(tag);
    app.play = (...events) => events.forEach(event => app.handleEvent(event));
    return app;
}

const refusal = (call, output, fields = {}) => ({event: 'tool.result', name: call.name, call_id: call.call_id,
    output, is_error: false, denied: true, elapsed: 0, ...fields});
const HOOK_TIMEOUT = 'Blocked by hook: pre_tool_use hook `slow-guard` timed out after 2 s; gate hooks block when '
    + 'they give no answer. Raise its timeout_seconds if it needs longer.';
const NO_PROMPT = 'Tool execution requires approval, but no approval prompt is available for this run, so grep '
    + 'was not executed. Continue without it, or ask the user to switch to a permission mode that allows it.';

test('a refused command says why in its own row; the person\'s own Deny only says denied', () => {
    const app = toolRowApp();
    const bash = (id, command) => ({event: 'tool.call', name: 'bash', call_id: id, arguments: {command}});
    const guarded = bash('call_1', 'rm -rf build');
    const policed = bash('call_2', 'rm -rf dist');
    const declined = bash('call_3', 'make deploy');
    app.play(guarded, refusal(guarded, `${HOOK_TIMEOUT}\n`),
        policed, refusal(policed, 'Blocked by policy: Recursive delete blocked', {is_error: true}),
        declined, refusal(declined, 'Tool execution denied by user.'));

    // The refusal settles the call's own row; no second "denied" line.
    const rows = app.activity.children;
    assert.equal(rows.length, 3);
    for (const row of rows) {
        const status = row.querySelector('[data-status]');
        assert.equal(status.textContent, '✗');
        assert.ok(status.classList.contains('denied') && !status.classList.contains('pending'));
        assert.ok(row.classList.contains('denied'));
    }
    const [hookRow, policyRow, deniedRow] = rows;
    assert.equal(hookRow.querySelector('[data-meta]').textContent, 'not run');
    const why = hookRow.querySelector('.tool-denial-reason');
    assert.equal(why.textContent, HOOK_TIMEOUT);
    assert.equal(why.nextElementSibling, hookRow.querySelector('[data-detail]'), 'above the expandable detail');
    assert.equal(policyRow.querySelector('.tool-denial-reason').textContent, 'Blocked by policy: Recursive delete blocked');
    assert.equal(deniedRow.querySelector('[data-meta]').textContent, 'denied');
    assert.equal(deniedRow.querySelector('.tool-denial-reason'), null);

    // Expanded, a command that never ran has no output to show.
    app._toggleBlockRowDetail(hookRow);
    assert.equal(hookRow.querySelector('code').textContent, 'rm -rf build');
    assert.equal(hookRow.querySelector('pre').textContent, '(not run)');
});

test('refusal reasons from hooks, policies and models render as text on their own call\'s row', () => {
    const app = toolRowApp();
    // A model or MCP server names the tool; a hook writes the reason.
    const first = {event: 'tool.call', name: UNTRUSTED, call_id: `${UNTRUSTED}:1`, arguments: {}};
    const second = {event: 'tool.call', name: UNTRUSTED, call_id: `${UNTRUSTED}:2`, arguments: {}};
    const reason = `Blocked by hook: ${UNTRUSTED}`;
    app.play(first, second,
        {event: 'tool.result', name: UNTRUSTED, call_id: first.call_id, output: 'fetched', is_error: false, denied: false},
        refusal(second, reason));

    const [done, refused] = app.activity.children;
    assert.equal(app.activity.children.length, 2);
    assert.equal(done.querySelector('.tool-status').textContent, '✓');
    assert.equal(done.querySelector('.tool-denial-reason'), null);
    assert.equal(refused.querySelector('.tool-status').textContent, '✗ not run');
    assert.ok(refused.querySelector('.tool-status').classList.contains('denied'));
    assert.ok(refused.classList.contains('is-denied'));
    const why = refused.querySelector('.tool-denial-reason');
    assert.equal(why.textContent, reason);
    assert.equal(why.children.length, 0);

    // A refusal whose call has no row still shows, on a line of its own.
    app.play(refusal({name: 'web_fetch', call_id: 'call_9'}, `Blocked by policy: ${UNTRUSTED}`));
    const line = app.activity.children[2];
    assert.ok(line.classList.contains('is-denied'));
    assert.equal(line.querySelector('.tool-status').textContent, '✗ not run');
    assert.equal(line.querySelector('.tool-denial-reason').textContent, `Blocked by policy: ${UNTRUSTED}`);

    // Only the two call rows were markup, and the name in them stayed text.
    assert.equal(app.htmlWrites.length, 2);
    app.htmlWrites.forEach((html, index) => assertRenderedAsText(html, `tool row ${index}`));
});

test('a refused evidence call reads ✗ with its reason, never ✓, and keeps the group open', () => {
    const app = toolRowApp();
    app.stepIsInlineOnly = true;
    const grep = (id, pattern) => ({event: 'tool.call', name: 'grep', call_id: id, arguments: {pattern, path: '.'}});
    const found = grep('g1', 'TODO'), unanswered = grep('g2', 'FIXME'), declined = grep('g3', 'XXX');
    app.play(found, unanswered, declined,
        {event: 'tool.result', name: 'grep', call_id: 'g1', output: 'a.py:1: TODO', is_error: false, denied: false,
         metadata: {count: 1}},
        refusal(unanswered, NO_PROMPT),
        refusal(declined, 'Tool execution denied by user.'));

    const [ok, blocked, denied] = app.activity.querySelectorAll('.evidence-item');
    assert.equal(ok.querySelector('.tool-status').textContent, '✓');
    // A refusal the person didn't make is open, with its reason as the output.
    assert.equal(blocked.querySelector('.tool-status').textContent, '✗');
    assert.equal(blocked.querySelector('.tool-status').style.color, 'var(--warn)');
    assert.equal(blocked.querySelector('.tool-meta').textContent, 'not run');
    assert.equal(blocked.querySelector('.tool-evidence-output').textContent, NO_PROMPT);
    assert.ok(blocked.classList.contains('is-denied') && blocked.classList.contains('show-output'));
    assert.equal(blocked.getAttribute('aria-expanded'), 'true');
    assert.equal(ok.getAttribute('aria-expanded'), 'false');
    // The person's own Deny: marked, with nothing to open.
    assert.equal(denied.querySelector('.tool-status').textContent, '✗');
    assert.equal(denied.querySelector('.tool-meta').textContent, 'denied');
    assert.equal(denied.querySelector('.tool-evidence-output'), null);
    assert.ok(!denied.classList.contains('show-output'));

    const group = app.activity.querySelector('.collapsed-group');
    assert.match(group.querySelector('.collapsed-summary').textContent, / · 2 not run$/);
    app._finalizeLiveCollapsedGroup();
    assert.ok(group.classList.contains('expanded'), 'refusals stay in view');
    assert.ok(!group.classList.contains('has-errors'), 'a refusal is not a failure');
});

test('a worker\'s refused call shows its reason in the worker\'s own rows', () => {
    const app = toolRowApp();
    const parentTask = {event: 'tool.call', name: 'task', call_id: 'call_2', arguments: {prompt: 'Explore'}};
    app.play(parentTask);
    // The worker it starts draws in its own lane, inside the parent's activity.
    const lane = app.activity.appendChild(app.element('div')).appendChild(app.element('div'));
    app.subagentContainers.set('w1', lane);
    const worker = event => ({...event, _subagent: true, _agent_id: 'w1', _agent_type: 'explore'});
    const allowlist = tool => `Tool '${tool}' is not in this session's allowlist. Allowed tools: ['file_read', 'grep']`;
    const write = worker({event: 'tool.call', name: 'file_write', call_id: 'call_1', arguments: {path: 'a.txt', content: 'x'}});
    const workerTask = worker({event: 'tool.call', name: 'task', call_id: 'call_2', arguments: {prompt: 'Build'}});
    app.play(write, workerTask,
        worker(refusal(write, allowlist('file_write'), {is_error: true})),
        worker(refusal(workerTask, allowlist('task'), {is_error: true})),
        {event: 'tool.result', name: 'task', call_id: 'call_2', output: 'handoff', is_error: false, denied: false});

    const [writeRow, taskRow] = lane.children;
    assert.equal(writeRow.querySelector('[data-meta]').textContent, 'not run');
    assert.equal(writeRow.querySelector('.tool-denial-reason').textContent, allowlist('file_write'));
    assert.equal(taskRow.querySelector('.tool-status').textContent, '✗ not run');
    assert.equal(taskRow.querySelector('.tool-denial-reason').textContent, allowlist('task'));
    // The parent's call with the same id is answered by its own result.
    const parentRow = app.activity.children[0];
    assert.equal(parentRow.getAttribute('data-call-id'), 'call_2');
    assert.equal(parentRow.querySelector('.tool-status').textContent, '✓');
    assert.equal(parentRow.querySelector('.tool-denial-reason'), null);
});

// ── Evidence answered after its group closed ──────────────────────────
// The engine announces every call of a response before it runs any. A
// command or an edit after an Evidence call therefore closes the group while
// that call still waits to run, and its result arrives afterwards. It still
// belongs on its own item.

const stepStart = step => ({event: 'step.start', step});
const stepEnd = step => ({event: 'step.end', step, elapsed: 0.5});
const grepCall = (id, pattern) => ({event: 'tool.call', name: 'grep', call_id: id, arguments: {pattern, path: '.'}});
const bashCall = (id, command) => ({event: 'tool.call', name: 'bash', call_id: id, arguments: {command}});
const answer = (call, output, fields = {}) => ({event: 'tool.result', name: call.name, call_id: call.call_id,
    output, is_error: false, denied: false, elapsed: 0.2, ...fields});

test('an Evidence call answered after a command closed its group settles its own item', () => {
    const app = toolRowApp();
    const early = grepCall('g1', 'TODO'), late = grepCall('g2', 'FIXME');
    const build = bashCall('b1', 'make build');
    app.play(stepStart(1), early, answer(early, 'a.py:1: TODO', {metadata: {count: 1}}), stepEnd(1),
        stepStart(2), late, build);
    const [group, buildRow] = app.activity.children;
    assert.ok(!group.classList.contains('running'), 'the command closed the group');

    app.play(answer(late, 'b.py:4: FIXME\nc.py:9: FIXME', {metadata: {count: 2}}),
        answer(build, 'built', {metadata: {exit_code: 0}}), stepEnd(2));
    const item = group.querySelectorAll('.evidence-item')[1];
    assert.equal(item.querySelector('.tool-status').textContent, '✓');
    assert.ok(!item.classList.contains('pending'));
    assert.equal(item.querySelector('.tool-meta').textContent, '2 matches');
    assert.equal(item.querySelector('.tool-evidence-output').textContent, 'b.py:4: FIXME\nc.py:9: FIXME');
    assert.ok(item.classList.contains('has-output') && !item.classList.contains('show-output'));
    // The header still counts both steps' calls and no failures, and stays closed.
    assert.equal(group.querySelector('.collapsed-meta').textContent, 'steps 1–2 · 2 calls');
    assert.doesNotMatch(group.querySelector('.collapsed-summary').textContent, /failed|not run/);
    assert.ok(!group.classList.contains('expanded'));
    assert.equal(buildRow.querySelector('[data-status]').textContent, '✓');
    assert.equal(app.activity.children.length, 2, 'the result drew no line of its own');
});

test('a late Evidence command never lends its result to another command\'s row', () => {
    const app = toolRowApp();
    const install = bashCall('b1', 'npm install');
    app.play(stepStart(1), install, answer(install, 'added 12 packages', {metadata: {exit_code: 0}}), stepEnd(1));
    const checks = bashCall('b2', 'pytest -q');  // Evidence
    const deploy = bashCall('b3', 'make deploy');  // a command row of its own
    app.play(stepStart(2), checks, deploy,
        answer(checks, '1 failed, 3 passed', {is_error: true, metadata: {exit_code: 1}}));

    const [installRow, group, deployRow] = app.activity.children;
    assert.equal(installRow.dataset.fullOutput, 'added 12 packages');
    assert.match(installRow.querySelector('[data-meta]').textContent, /^exit 0/);
    assert.ok(deployRow.querySelector('[data-status]').classList.contains('pending'), 'still waiting to run');
    assert.equal(deployRow.dataset.fullOutput, undefined);
    // It failed on its own item, which opens with its output, and the closed
    // group opens again and counts it.
    const item = group.querySelector('.evidence-item');
    assert.equal(item.querySelector('.tool-status').textContent, '✗');
    assert.ok(item.classList.contains('is-error') && item.classList.contains('show-output'));
    assert.equal(item.getAttribute('aria-expanded'), 'true');
    assert.equal(item.querySelector('.tool-evidence-output').textContent, '1 failed, 3 passed');
    assert.match(group.querySelector('.collapsed-summary').textContent, / · 1 failed$/);
    assert.ok(group.classList.contains('expanded') && group.classList.contains('has-errors'));
    assert.equal(group.querySelector('.collapsed-icon').textContent, '▾');

    app.play(answer(deploy, 'deployed', {metadata: {exit_code: 0}}));
    assert.equal(deployRow.dataset.fullOutput, 'deployed');
    assert.equal(deployRow.querySelector('[data-status]').textContent, '✓');
});

test('a refusal answered after its group closed opens its reason on its item, not on a line of its own', () => {
    const app = toolRowApp();
    const unanswered = grepCall('g1', 'FIXME'), declined = grepCall('g2', 'XXX');
    const deploy = bashCall('b1', 'make deploy');
    app.play(stepStart(1), unanswered, declined, deploy,
        refusal(unanswered, NO_PROMPT), refusal(declined, 'Tool execution denied by user.'),
        answer(deploy, 'deployed', {metadata: {exit_code: 0}}));

    const rows = app.activity.children;
    assert.equal(rows.length, 2);
    assert.ok(!rows.some(row => row.classList.contains('is-denied')));
    const group = rows[0];
    const [blocked, denied] = group.querySelectorAll('.evidence-item');
    assert.equal(blocked.querySelector('.tool-status').textContent, '✗');
    assert.equal(blocked.querySelector('.tool-status').style.color, 'var(--warn)');
    assert.equal(blocked.querySelector('.tool-meta').textContent, 'not run');
    assert.equal(blocked.querySelector('.tool-evidence-output').textContent, NO_PROMPT);
    assert.ok(blocked.classList.contains('is-denied') && blocked.classList.contains('show-output'));
    assert.equal(blocked.getAttribute('aria-expanded'), 'true');
    // The person's own Deny: marked, with nothing to open.
    assert.equal(denied.querySelector('.tool-status').textContent, '✗');
    assert.equal(denied.querySelector('.tool-meta').textContent, 'denied');
    assert.equal(denied.querySelector('.tool-evidence-output'), null);
    // The group counts both, opens again, and doesn't call them failures.
    assert.match(group.querySelector('.collapsed-summary').textContent, / · 2 not run$/);
    assert.ok(group.classList.contains('expanded') && !group.classList.contains('has-errors'));
    assert.equal(group.querySelector('.collapsed-icon').textContent, '▾');
});

test('a screenshot settles its item when its image closes the group, or a later call already did', () => {
    const app = toolRowApp();
    const images = [];
    app.renderScreenshotImage = (data, type, name) => images.push(`${name}:${data}`);
    const shot = id => ({event: 'tool.call', name: 'browser_screenshot', call_id: id, arguments: {}});
    const image = data => ({image: {data, media_type: 'image/png'}});
    const alone = shot('s1');
    app.play(stepStart(1), alone, answer(alone, 'Captured the page', image('AAAA')), stepEnd(1));
    const followed = shot('s2');
    const script = {event: 'tool.call', name: 'browser_js', call_id: 'j1', arguments: {code: 'document.title'}};
    app.play(stepStart(2), followed, script, answer(followed, 'Captured the page', image('BBBB')));

    const items = app.activity.querySelectorAll('.evidence-item');
    assert.equal(items.length, 2);
    for (const item of items) {
        assert.equal(item.querySelector('.tool-status').textContent, '✓');
        assert.ok(!item.classList.contains('pending'));
    }
    assert.equal(images.join(' '), 'browser_screenshot:AAAA browser_screenshot:BBBB');
    assert.equal(app.activity.children.length, 3, 'two groups and the script\'s row, no other lines');
});

test('a closed group\'s waiting item answers only results drawn in its own turn', () => {
    const app = toolRowApp();
    // A turn that never finished (Lumi closed while its search waited to run),
    // as a replay shows it: no session end cleared its closed group.
    const stale = grepCall('call_5f2a9c01', 'TODO');
    app.play(stepStart(1), stale, bashCall('b1', 'make build'));
    const staleItem = app.activity.querySelector('.evidence-item');
    // The next turn draws in a new task card. A backend that derives ids from
    // the call gives the same search the same id there.
    const nextActivity = app.element('div');
    app._ensureTaskCard = () => ({activityEl: nextActivity});
    const again = grepCall('call_5f2a9c01', 'TODO');
    app.play(stepStart(1), bashCall('b2', 'make lint'), again,
        answer(again, 'a.py:1: TODO', {metadata: {count: 1}}));

    const row = nextActivity.children.find(el => el.getAttribute('data-call-id') === 'call_5f2a9c01');
    assert.equal(row.querySelector('.tool-status').textContent, '1 matches');
    assert.ok(staleItem.classList.contains('pending'));
    assert.equal(staleItem.querySelector('.tool-status').textContent, '…');
});
