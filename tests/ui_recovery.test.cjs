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
