// The page's side of the panel bridge (lumi/gui/static/panels_view.js): which
// messages from a capability pack's sandboxed panel count, what they may do,
// and what they may not. tests/extension_panels.browser.cjs runs the real
// frame in a browser; this checks the rules message by message.
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../lumi/gui/static/panels_view.js'), 'utf8');

/** The view's code in a context of its own, with just enough of a page. */
function load(document = {}) {
    const context = vm.createContext({console, Event, Date, setTimeout, clearTimeout});
    context.window = context;
    context.addEventListener = () => {};
    context.document = {
        activeElement: null,
        body: {},
        documentElement: {getAttribute: () => null},
        querySelector: () => null,
        getElementById: () => null,
        ...document,
    };
    vm.runInContext(source, context);
    return context;
}

const plain = value => JSON.parse(JSON.stringify(value));

test('only requests in the bridge shape count, and each is bounded', () => {
    const {LumiPanelBridge: bridge} = load();
    const check = data => plain(bridge.checkRequest(data));

    assert.deepEqual(check({lumi: 1, id: 1, method: 'context'}), {id: 1, method: 'context'});
    assert.deepEqual(check({lumi: 1, id: 'a', method: 'composer.insert', text: 'Explain the build'}),
        {id: 'a', method: 'composer.insert', text: 'Explain the build'});
    assert.deepEqual(check({lumi: 1, method: 'close'}), {method: 'close'});

    // Not the bridge's at all: ignored, not answered.
    for (const data of [null, 'context', 7, [1], {method: 'context'}, {lumi: 2, method: 'context'},
        {lumi: '1', method: 'context'}, {lumi: 1, id: {}, method: 'context'}, {lumi: 1, id: 1.5, method: 'context'},
        {lumi: 1, id: 'x'.repeat(65), method: 'context'}]) {
        assert.equal(bridge.checkRequest(data), null, JSON.stringify(data));
    }

    // Recognised but refused: answered with why.
    const refused = data => bridge.checkRequest(data).error;
    assert.match(refused({lumi: 1, id: 2, method: 'send'}), /no method "send"/);
    assert.match(refused({lumi: 1, id: 2, method: 'settings.read'}), /no method/);
    assert.match(refused({lumi: 1, id: 2, method: 'x'.repeat(5000)}), /no method "x{40}"\./);
    assert.match(refused({lumi: 1, id: 2, method: 'toast', a: 1, b: 2, c: 3, d: 4, e: 5, f: 6}), /too many fields/);
    assert.match(refused({lumi: 1, id: 2, method: 'composer.insert'}), /up to 8000 characters/);
    assert.match(refused({lumi: 1, id: 2, method: 'composer.insert', text: 'x'.repeat(8001)}), /up to 8000/);
    assert.match(refused({lumi: 1, id: 2, method: 'toast', text: 'x'.repeat(201)}), /up to 200/);
    assert.match(refused({lumi: 1, id: 2, method: 'toast', text: ['a']}), /must be a string/);
    assert.match(refused({lumi: 1, id: 2, method: 'composer.insert', text: ' \u200b\u2060 '}), /text is empty/);
});

test('text reaches the message box as the person sees it', () => {
    const {LumiPanelBridge: bridge} = load();
    // Zero-width, bidirectional, tag and control characters could hide words
    // the model reads but the person never sees; tabs and line breaks stay.
    const hidden = 'Summarize\u200b the build\u202e\u2066 now\u0007\u0000 \u{E0041}\u{E0042}';
    assert.equal(bridge.visibleText(hidden), 'Summarize the build now ');
    assert.equal(bridge.visibleText('a\r\nb\rc\u2028d\te'), 'a\nb\nc\nd\te');
    assert.equal(bridge.checkRequest({lumi: 1, method: 'toast', text: ' Saved\n\n  twice\u200b '}).text, 'Saved twice');
});

test('a rate limit holds requests to a window', () => {
    const {LumiPanelBridge: bridge} = load();
    let now = 1000;
    const limit = bridge.rateLimit(2, 1000, () => now);
    assert.deepEqual([limit.take(), limit.take(), limit.take()], [true, true, false]);
    now += 999;
    assert.equal(limit.take(), false);
    now += 1;
    assert.equal(limit.take(), true);
});

/** A page with an open panel: the frame's window records what the page tells it. */
function openPanel({draft = true, composer = 'Existing draft'} = {}) {
    const context = load();
    const frameWindow = {received: [], postMessage(message, origin) { this.received.push({message: plain(message), origin}); }};
    const frame = {contentWindow: frameWindow};
    const app = Object.create(context.LumiPanelsView.prototype);
    Object.assign(app, {
        sent: [], toasts: [], saved: 0, inputEvents: 0,
        currentCwd: 'D:/work/acme-api', currentSessionId: 's1',
        _currentSessionSummary: () => ({id: 's1', title: 'Fix the flaky test'}),
        _projectNameFromPath: p => p.split('/').pop(),
        _draftScope: draft ? {key: 'draft'} : null,
        inputBar: {style: {display: 'flex'}},
        userInput: {
            value: composer,
            setSelectionRange(start, end) { this.selection = [start, end]; },
            dispatchEvent: event => { app.inputEvents += event.type === 'input' ? 1 : 0; },
        },
        send(data) { this.sent.push(plain(data)); },
        showToastMessage(message) { this.toasts.push(message); },
        _markDraftEdited() {},
        _saveDraft() { this.saved += 1; },
    });
    const overlay = {removed: false, remove() { this.removed = true; }};
    app._extensionPanel = {pack: 'demo', panel: 'stats', title: 'Build stats', packName: 'Demo', token: 'T'.repeat(32),
        overlay, frame, opener: null,
        limits: {all: context.LumiPanelBridge.rateLimit(20, 1000), toast: context.LumiPanelBridge.rateLimit(1, 1000)}};
    const post = (data, {source = frameWindow, origin = 'null'} = {}) => {
        const before = frameWindow.received.length;
        app._receiveExtensionPanelMessage({data, source, origin});
        return frameWindow.received.slice(before).map(entry => entry.message);
    };
    return {context, app, frame, frameWindow, overlay, post};
}

test('messages count only from the open panel\'s own frame with its opaque origin', () => {
    const {app, post} = openPanel();
    const insert = {lumi: 1, id: 1, method: 'composer.insert', text: 'Injected'};
    assert.deepEqual(post(insert, {source: {postMessage() {}}}), []);          // another frame or window
    assert.deepEqual(post(insert, {origin: 'http://127.0.0.1:48123'}), []);    // not the sandbox's origin
    assert.deepEqual(post(insert, {origin: 'https://evil.example'}), []);
    assert.equal(app.userInput.value, 'Existing draft');
    app._extensionPanel = null;
    assert.deepEqual(post(insert), []);                                         // no panel open
    assert.equal(app.userInput.value, 'Existing draft');
});

test('a panel reads only the project name, the session title and the theme', () => {
    const {app, post} = openPanel();
    assert.deepEqual(post({lumi: 1, id: 7, method: 'context'}),
        [{lumi: 1, id: 7, ok: true, result: {project: 'acme-api', session: 'Fix the flaky test', theme: 'dark'}}]);
    app.currentSessionId = '';
    assert.equal(post({lumi: 1, id: 8, method: 'context'})[0].result.session, '');
});

test('a panel adds text to the message box and never sends it', () => {
    const {app, post} = openPanel();
    const [reply] = post({lumi: 1, id: 2, method: 'composer.insert', text: 'Explain the\u200b failure'});
    assert.deepEqual(reply, {lumi: 1, id: 2, ok: true, result: null});
    assert.equal(app.userInput.value, 'Existing draft\nExplain the failure');
    assert.deepEqual(app.userInput.selection, [34, 34], 'the caret goes after the added text');
    assert.equal(app.inputEvents, 1);
    assert.equal(app.saved, 1);
    assert.deepEqual(app.sent, [], 'nothing reaches the server: no message is sent');
    assert.deepEqual(app.toasts, ['Build stats added text to your message. Nothing is sent until you send it.']);

    // After whitespace the text follows directly.
    app.userInput.value = 'Draft ';
    post({lumi: 1, id: 3, method: 'composer.insert', text: 'more'});
    assert.equal(app.userInput.value, 'Draft more');

    // No draft to add to, or a message that would grow too long, is refused.
    const empty = openPanel({draft: false});
    assert.match(empty.post({lumi: 1, id: 4, method: 'composer.insert', text: 'x'})[0].error, /no message box/);
    assert.equal(empty.app.userInput.value, 'Existing draft');
    const long = openPanel({composer: 'y'.repeat(99995)});
    assert.match(long.post({lumi: 1, id: 5, method: 'composer.insert', text: 'x'.repeat(10)})[0].error, /too long/);
});

test('a notice always names its panel, one a second', () => {
    const {app, post} = openPanel();
    assert.equal(post({lumi: 1, id: 1, method: 'toast', text: 'Saved'})[0].ok, true);
    assert.match(post({lumi: 1, id: 2, method: 'toast', text: 'Again'})[0].error, /one notice a second/);
    assert.deepEqual(app.toasts, ['Build stats: Saved']);
});

test('refused and unknown requests are answered, and requests are rate limited', () => {
    const {app, post} = openPanel();
    assert.deepEqual(post({lumi: 1, id: 9, method: 'bash', command: 'rm -rf /'}),
        [{lumi: 1, id: 9, ok: false, error: 'The panel bridge has no method "bash".'}]);
    const replies = [];
    for (let i = 0; i < 25; i++) replies.push(...post({lumi: 1, id: i, method: 'context'}));
    // One request above already counted toward the 20 a second.
    assert.equal(replies.filter(reply => reply.ok).length, 19);
    assert.match(replies.at(-1).error, /Too many requests/);
    assert.deepEqual(app.sent, []);
});

test('a panel closes itself only for Escape pressed in it', () => {
    const {context, app, frame, overlay, post} = openPanel();
    assert.match(post({lumi: 1, method: 'close'})[0].error, /presses Escape/);
    assert.equal(overlay.removed, false);
    context.document.activeElement = frame;
    assert.deepEqual(post({lumi: 1, method: 'close'}), []);
    assert.equal(overlay.removed, true);
    assert.equal(app._extensionPanel, null);
    assert.deepEqual(app.sent, [{command: 'extension_panel_close', token: 'T'.repeat(32)}]);
});

test('the page frames only a panel URL from its own server', () => {
    const {app} = openPanel();
    const shown = [];
    app._showExtensionPanel = info => shown.push(plain(info));
    const opened = url => ({event: 'extension_panel_opened', request_id: 'r', pack: 'demo', pack_name: 'Demo',
        panel: 'stats', title: 'Build stats', token: 'T'.repeat(32), url});
    for (const url of ['https://evil.example/panels/x', '/static/app.js', 'javascript:alert(1)',
        '//evil.example/panels/' + 'T'.repeat(32) + '/index.html', '/panels/short/index.html',
        '/panels/' + 'T'.repeat(32) + '/index.html?x=1', undefined]) {
        app._extensionPanelOpening = {requestId: 'r', opener: null};
        app.receiveExtensionPanelOpened(opened(url));
    }
    assert.deepEqual(shown, []);
    // Only an answer to this page's latest request; an earlier one's token is withdrawn.
    app._extensionPanelOpening = {requestId: 'mine', opener: null};
    app.receiveExtensionPanelOpened(opened(`/panels/${'T'.repeat(32)}/index.html`));
    assert.deepEqual(shown, []);
    assert.deepEqual(app.sent.at(-1), {command: 'extension_panel_close', token: 'T'.repeat(32)});
    app._extensionPanelOpening = {requestId: 'r', opener: null};
    app.receiveExtensionPanelOpened(opened(`/panels/${'T'.repeat(32)}/index.html`));
    assert.equal(shown.length, 1);
    assert.equal(shown[0].url, `/panels/${'T'.repeat(32)}/index.html`);
    // A refusal is shown, and the list asked for again.
    app._extensionPanelOpening = {requestId: 'r2', opener: null};
    app.receiveExtensionPanelOpened({event: 'extension_panel_opened', request_id: 'r2', error: 'Approve the Demo pack.'});
    assert.equal(app.toasts.at(-1), 'Approve the Demo pack.');
    assert.deepEqual(app.sent.at(-1), {command: 'extension_panels'});
});

test('a panel whose pack leaves the list closes at once', () => {
    const {app, overlay} = openPanel();
    app.receiveExtensionPanels({event: 'extension_panels', enabled: true,
        panels: [{pack: 'demo', pack_name: 'Demo', panel: 'stats', title: 'Build stats'}, {pack: 1}]});
    assert.equal(overlay.removed, false);
    assert.deepEqual(plain(app._extensionPanels), [{pack: 'demo', pack_name: 'Demo', panel: 'stats', title: 'Build stats'}]);
    app.receiveExtensionPanels({event: 'extension_panels', enabled: true, panels: []});
    assert.equal(overlay.removed, true);
    assert.match(app.toasts.at(-1), /^Build stats closed\. Its pack is no longer approved/);
    // Turned off: nothing is listed, whatever the rows say.
    app.receiveExtensionPanels({event: 'extension_panels', enabled: false, reason: 'Off.',
        panels: [{pack: 'demo', pack_name: 'Demo', panel: 'stats', title: 'Build stats'}]});
    assert.deepEqual(plain(app._extensionPanels), []);
});

test('the command palette lists panels and asks for a fresh list at most every few seconds', () => {
    const {app} = openPanel();
    app._extensionPanels = [{pack: 'demo', pack_name: 'Demo', panel: 'stats', title: 'Build stats'}];
    const commands = app.extensionPanelCommands();
    assert.deepEqual(commands.map(c => [c.id, c.label, c.hint]), [['panel:demo/stats', 'Open panel: Build stats', 'Demo']]);
    app.extensionPanelCommands();
    assert.deepEqual(app.sent, [{command: 'extension_panels'}]);
});

test('a window whose native bridge reaches every frame opens no panel', () => {
    const {context, app} = openPanel();
    context.webkit = {messageHandlers: {jsBridge: {}}};
    app.openExtensionPanel('demo', 'stats');
    assert.deepEqual(app.sent, []);
    assert.match(app.toasts.at(-1), /Open in Browser/);
    delete context.webkit;
    app.openExtensionPanel('demo', 'stats');
    assert.deepEqual(app.sent.map(message => message.command), ['extension_panel_open']);
});
