// The Send feedback dialog's side (lumi/gui/static/feedback_view.js): the form's checks and
// wording, and the dialog's flow against a small stand-in page. lumi/feedback.py builds and
// sends the report (tests/test_feedback.py); tests/feedback.browser.cjs runs the real page.
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../lumi/gui/static/feedback_view.js'), 'utf8');

class El {
    constructor(page, id, props = {}) {
        Object.assign(this, {page, id, value: '', checked: false, hidden: false, disabled: false, textContent: '',
            type: '', name: '', isConnected: true, parent: null, dataset: {}, style: {}, attributes: {},
            listeners: {}, children: [], classes: new Set()}, props);
        const el = this;
        this.classList = {add: name => el.classes.add(name), remove: name => el.classes.delete(name),
            toggle: (name, on) => (on ? el.classes.add(name) : el.classes.delete(name)), contains: name => el.classes.has(name)};
    }
    addEventListener(type, handler) { (this.listeners[type] ||= []).push(handler); }
    dispatch(type, extra = {}) {
        const event = {type, target: extra.target || this, currentTarget: this, key: extra.key, shiftKey: !!extra.shiftKey,
            ctrlKey: !!extra.ctrlKey, metaKey: false, defaultPrevented: false,
            preventDefault() { this.defaultPrevented = true; }, stopPropagation() { this.stopped = true; }};
        for (const handler of this.listeners[type] || []) handler(event);
        return event;
    }
    focus() { this.page.document.activeElement = this; }
    select() { this.selected = true; }
    setAttribute(name, value) { this.attributes[name] = String(value); if (name === 'hidden') this.hidden = true; }
    removeAttribute(name) { delete this.attributes[name]; }
    getAttribute(name) { return this.attributes[name] ?? null; }
    replaceChildren() { this.children = []; }
    appendChild(child) { child.parent = this; this.children.push(child); }
    closest(selector) {
        for (let node = this; node; node = node.parent) {
            if (selector === '[hidden]' && node.hidden) return node;
            if (selector === 'button[data-held-action]' && node.tag === 'button' && node.dataset.heldAction) return node;
        }
        return null;
    }
    getClientRects() { return this.closest('[hidden]') ? [] : [{}]; }
    contains(node) { return this.page.inDialog.has(node); }
    get text() { return [this.textContent, ...this.children.map(child => child.text)].join(' ').trim(); }
}

/** The dialog's markup, as stand-ins, and a LumiFeedbackView on it. */
function page({clipboard = 'ok', confirm = true} = {}) {
    const document = {activeElement: null};
    const timers = [];
    const context = vm.createContext({console, setTimeout: (fn) => { timers.push(fn); return timers.length; },
        clearTimeout: (id) => { if (id) timers[id - 1] = null; }});
    const p = {document, inDialog: new Set(), elements: {}, sent: [], copied: [], toasts: [], confirmed: []};
    const make = (id, props = {}) => {
        const element = new El(p, id, props);
        p.elements[id] = element;
        return element;
    };
    const dialog = make('feedback-dialog', {style: {display: 'none'}});
    const form = make('feedback-form');
    const preview = make('feedback-preview', {hidden: true});
    const queue = make('feedback-queue', {hidden: true});
    const done = make('feedback-done', {hidden: true});
    const inside = (parent, id, props) => { const element = make(id, props); element.parent = parent; return element; };
    // In page order, as the Tab key meets them.
    const stops = [
        make('feedback-dialog-close'),
        inside(form, 'feedback-kind-bug', {type: 'radio', name: 'feedback-kind', value: 'bug', checked: true}),
        inside(form, 'feedback-kind-idea', {type: 'radio', name: 'feedback-kind', value: 'idea'}),
        inside(form, 'feedback-kind-other', {type: 'radio', name: 'feedback-kind', value: 'other'}),
        inside(form, 'feedback-message', {type: 'textarea'}),
        inside(form, 'feedback-reply-to', {type: 'email'}),
        inside(form, 'feedback-diagnostics', {type: 'checkbox'}),
        inside(preview, 'feedback-preview-body'),
        inside(queue, 'feedback-flush'),
        inside(queue, 'feedback-discard'),
        inside(form, 'feedback-copy-text', {type: 'textarea', hidden: true}),
        inside(form, 'feedback-copy', {hidden: true}),
        inside(form, 'feedback-cancel'),
        inside(form, 'feedback-send', {textContent: 'Send'}),
        inside(done, 'feedback-another'),
        inside(done, 'feedback-done-close'),
    ];
    preview.parent = form;
    queue.parent = form;
    for (const id of ['feedback-kind-error', 'feedback-message-error', 'feedback-reply-to-error', 'feedback-alert',
        'feedback-disabled', 'feedback-diagnostics-off']) {
        inside(form, id, {hidden: true});
    }
    inside(queue, 'feedback-held', {hidden: true});
    for (const id of ['feedback-message-count', 'feedback-destination', 'feedback-always', 'feedback-progress',
        'feedback-queue-text', 'feedback-preview-meta', 'feedback-preview-notices', 'feedback-done-text',
        'feedback-done-notices']) make(id);
    for (const element of Object.values(p.elements)) p.inDialog.add(element);
    make('feedback-announcer');  // outside the dialog
    dialog.querySelectorAll = () => stops;
    document.body = new El(p, 'body');
    document.listeners = [];
    document.addEventListener = (type, handler) => document.listeners.push({type, handler});
    document.dispatch = (type, extra = {}) => {
        const event = {type, key: extra.key, target: extra.target || document.body, defaultPrevented: false,
            preventDefault() { this.defaultPrevented = true; }, stopPropagation() {}};
        for (const listener of document.listeners) if (listener.type === type) listener.handler(event);
        return event;
    };
    document.getElementById = id => p.elements[id] || null;
    document.getElementsByName = name => stops.filter(element => element.name === name);
    document.createElement = tag => new El(p, '', {tag});
    context.document = document;
    context.window = context;
    context.window.confirm = message => { p.confirmed.push(message); return confirm; };
    context.navigator = {clipboard: {writeText: async text => {
        if (clipboard !== 'ok') throw new Error('refused');
        p.copied.push(text);
    }}};
    vm.runInContext(source, context);
    const app = Object.create(context.LumiFeedbackView.prototype);
    app.send = message => p.sent.push(JSON.parse(JSON.stringify(message)));
    app.showToastMessage = text => p.toasts.push(text);
    p.app = app;
    p.$ = id => p.elements[id];
    p.runTimers = () => { const due = timers.splice(0); for (const fn of due) if (fn) fn(); };
    p.type = (id, value) => {
        const field = p.elements[id];
        if (field.type === 'checkbox' || field.type === 'radio') field.checked = value;
        else field.value = value;
        form.dispatch('input', {target: field});
        form.dispatch('change', {target: field});
    };
    p.choose = kind => {
        for (const other of ['bug', 'idea', 'other']) p.elements[`feedback-kind-${other}`].checked = other === kind;
        form.dispatch('input', {target: p.elements[`feedback-kind-${kind}`]});
        form.dispatch('change', {target: p.elements[`feedback-kind-${kind}`]});
    };
    p.api = context.LumiFeedback;
    return p;
}

const plain = value => JSON.parse(JSON.stringify(value));
const STATUS = {destination: 'cloud.example.com', destination_url: 'https://cloud.example.com', source: 'cloud',
    account: '', waiting: 0, sendable: 0, reports: [], diagnostics: {allowed: true, reason: ''}};

test('the form is checked as the app checks it', () => {
    const {api} = page();
    const ok = api.check({kind: 'idea', message: '  Add dark mode to reports ', reply_to: ' ada@example.com ',
        include_diagnostics: true});
    assert.deepEqual(plain(ok), {form: {kind: 'idea', message: '  Add dark mode to reports ', reply_to: 'ada@example.com',
        include_diagnostics: true}, errors: {}, ok: true});
    const bad = api.check({kind: 'praise', message: '   ', reply_to: 'ada@example', include_diagnostics: 'true'});
    assert.deepEqual(Object.keys(bad.errors).sort(), ['kind', 'message', 'reply_to']);
    assert.equal(bad.form.include_diagnostics, false, 'only a real true includes diagnostics');
    for (const address of ['a..b@example.com', '.ada@example.com', 'ada.@example.com', 'ada@example.com?cc=eve@x.com',
        'ada eve@example.com', 'ada@@example.com', 'ada@example.c', `${'a'.repeat(65)}@example.com`]) {
        assert.ok(api.check({kind: 'bug', message: 'x', reply_to: address}).errors.reply_to, address);
    }
    for (const address of ["o'brien+lumi@mail.example.co.uk", 'A.B-C_d%e@EXAMPLE.org']) {
        assert.equal(api.check({kind: 'bug', message: 'x', reply_to: address}).ok, true, address);
    }
});

test('characters are counted as lumi/feedback.py counts what it sends', () => {
    const {api} = page();
    const emoji = String.fromCodePoint(0x1F600);
    // An emoji is one character, not two UTF-16 units.
    assert.equal(api.check({kind: 'bug', message: emoji.repeat(5000)}).ok, true);
    assert.match(api.check({kind: 'bug', message: 'x'.repeat(5001)}).errors.message, /5,000 characters/);
    assert.equal(api.countLabel(emoji + 'ab'), '3 / 5,000');
    // Control characters, bidirectional controls and space at either end don't count: they aren't sent.
    const rlo = String.fromCharCode(0x202E);
    const nul = String.fromCharCode(0);
    assert.equal(api.cleanText(`  a${nul}b${rlo}c\r\nd\t `), 'abc\nd');
    assert.equal(api.characters(`  ${'x'.repeat(5000)}${rlo}\n `), 5000);
    assert.equal(api.check({kind: 'bug', message: `${'x'.repeat(5000)}${rlo}  `}).ok, true);
    assert.match(api.check({kind: 'bug', message: `${nul}${rlo} `}).errors.message, /Write what happened/);
});

test('the wording says where a report goes, who reads it there, as whom, and what waits', () => {
    const {api} = page();
    assert.equal(api.destination({destination: 'cloud.example.com', account: 'ada@example.com'}),
        'It goes to cloud.example.com as ada@example.com.');
    assert.equal(api.destination({destination: 'cloud.example.com'}), 'It goes to cloud.example.com, without your account.');
    assert.equal(api.destination({destination: 'cloud.example.com', source: 'policy'},
        {destination: 'cloud.example.com', operator: 'Luminary Analytics', accepting: true}),
    'It goes to cloud.example.com (read by Luminary Analytics), set by your organization, without your account.');
    assert.equal(api.destination({destination: 'c.test', source: 'policy', account: 'ada@example.com'}),
        'It goes to c.test, set by your organization, as ada@example.com.');
    assert.match(api.destination({destination: 'x.test'}, {destination: 'x.test', accepting: false, operator: ''}),
        /doesn’t accept feedback now/);
    // Another destination's answer isn't shown beside this one.
    assert.equal(api.destination({destination: 'a.test'}, {destination: 'b.test', operator: 'B'}), 'It goes to a.test, without your account.');
    assert.match(api.destination({}), /No feedback address is set/);
    assert.equal(api.destination({destination: 'x', offline: 'Offline mode: no.'}), 'Offline mode: no.');
    assert.equal(api.destination({destination: 'x', disabled: 'Off.'}), '');
    assert.equal(api.previewMeta({destination: 'cloud.example.com'}), 'To cloud.example.com, without your account.');
    assert.match(api.previewMeta({destination: 'c.test', provisional: true}), /data loss prevention service checks it when you send it/);
    assert.equal(api.alwaysSent({version: '0.19.2', channel: 'beta', os: 'Windows 11', arch: 'amd64'}),
        'Always sent: Lumi 0.19.2 (beta), Windows 11, amd64, and a random id for this install.');
    assert.equal(api.queueText(0), '');
    assert.equal(api.queueText(1), '1 report waiting on this computer.');
    assert.equal(api.queueText(2, 'c.test'), '2 reports waiting on this computer to go to c.test.');
    assert.equal(api.flushText({sent: 2, held: 1, waiting: 1}), 'Sent 2 reports. 1 report couldn’t be delivered: see below.');
    assert.equal(api.flushText({sent: 0, held: 0, waiting: 0}), 'Nothing is waiting.');
    assert.equal(api.flushText({sent: 0, held: 0, waiting: 2}), 'Nothing could be sent now.');
    assert.equal(api.flushText({busy: true}), 'Lumi is already sending the waiting reports.');
    assert.equal(api.flushText({failed: true}), 'Lumi couldn’t send the waiting reports. It will try again later.');
    assert.equal(api.flushText({error: 'Where reports go changed.'}), 'Where reports go changed.');
    assert.equal(api.previewText({kind: 'bug'}), '{\n  "kind": "bug"\n}');
});

test('each waiting report says what it waits for', () => {
    const {api} = page();
    const status = {destination: 'cloud.example.com'};
    const say = report => api.heldText({kind: 'bug', written: 0, ...report}, status);
    assert.match(say({reason: 'no_destination', state: 'held'}), /^Bug report: written before a feedback address was set\. It goes only if you send it to cloud\.example\.com\.$/);
    assert.match(api.heldText({kind: 'idea', reason: 'no_destination', state: 'held'}, {}), /stays here until one is/);
    assert.match(say({state: 'sign_in', reason: 'sign_in', destination: 'cloud.example.com'}), /didn’t accept your sign-in\. Sign in again/);
    assert.match(say({state: 'sign_in', reason: 'unauthorized', destination: 'cloud.example.com'}), /waiting for you to sign in/);
    assert.match(say({state: 'waiting', reason: 'unreachable', destination: 'other.test', here: false}), /waiting for other\.test, where it was written to go/);
    assert.match(say({state: 'held', reason: 'not_accepting', destination: 'c.test', here: true}), /doesn’t accept feedback\. Send now tries again/);
    assert.match(say({state: 'held', reason: 'too_large', destination: 'c.test', here: true}), /too large for c\.test/);
    assert.match(say({state: 'held', reason: 'dlp', here: true}), /data loss prevention rules now keep it/);
    assert.match(say({state: 'held', reason: 'expired', here: true}), /waited 30 days/);
    assert.match(say({state: 'held', reason: 'refused', destination: 'c.test', detail: 'No.', here: true}), /c\.test couldn’t take it: No\./);
});

test('the dialog opens, asks where reports go, and gives focus back when it closes', () => {
    const p = page();
    const opener = new El(p, 'opener');
    p.app.openFeedbackDialog(opener);
    assert.equal(p.$('feedback-dialog').style.display, 'flex');
    assert.deepEqual(p.sent, [{command: 'feedback_status', open: true}]);
    assert.equal(p.document.activeElement, p.$('feedback-message'));
    p.app.handleFeedbackStatus({data: {...STATUS, app: {version: '0.19.2', channel: 'stable', os: 'Windows 11', arch: 'amd64'}}});
    assert.equal(p.$('feedback-destination').textContent, 'It goes to cloud.example.com, without your account.');
    p.app.handleFeedbackInfo({data: {destination: 'cloud.example.com', accepting: true, operator: 'Luminary Analytics'}});
    assert.equal(p.$('feedback-destination').textContent,
        'It goes to cloud.example.com (read by Luminary Analytics), without your account.');
    assert.match(p.$('feedback-always').textContent, /^Always sent: Lumi 0\.19\.2 \(stable\)/);
    assert.equal(p.$('feedback-queue').hidden, true);
    const escape = p.$('feedback-dialog').dispatch('keydown', {key: 'Escape'});
    assert.ok(escape.defaultPrevented && escape.stopped, 'Escape is the dialog’s, not the page’s');
    assert.equal(p.$('feedback-dialog').style.display, 'none');
    assert.equal(p.document.activeElement, opener);
});

test('a refusal from the last time is gone when the dialog opens again', () => {
    const p = page();
    p.app.openFeedbackDialog();
    p.type('feedback-message', 'Offline here.');
    p.$('feedback-form').dispatch('submit');
    p.app.handleFeedbackResult({ok: false, code: 'offline', message: 'Offline mode: no.', copy_text: 'text'});
    assert.equal(p.$('feedback-alert').hidden, false);
    assert.equal(p.$('feedback-copy').hidden, false);
    p.app.closeFeedbackDialog();
    p.app.openFeedbackDialog();
    assert.equal(p.$('feedback-alert').hidden, true);
    assert.equal(p.$('feedback-copy').hidden, true);
    assert.equal(p.$('feedback-progress').textContent, '');
    assert.equal(p.$('feedback-message').value, 'Offline here.', 'the draft stays');
});

test('an organization’s switches are said plainly, and Send and diagnostics follow them', () => {
    const p = page();
    p.app.openFeedbackDialog();
    p.app.handleFeedbackStatus({data: {...STATUS, disabled: 'Your organization turned off sending feedback from Lumi.'}});
    assert.equal(p.$('feedback-disabled').hidden, false);
    assert.equal(p.$('feedback-disabled').textContent, 'Your organization turned off sending feedback from Lumi.');
    assert.equal(p.$('feedback-send').disabled, true);
    assert.equal(p.$('feedback-destination').textContent, '');
    assert.equal(p.$('feedback-preview').hidden, true);
    p.type('feedback-message', 'Hello');
    p.$('feedback-form').dispatch('submit');
    assert.equal(p.sent.filter(m => m.command === 'feedback_send').length, 0);

    const q = page();
    q.app.openFeedbackDialog();
    q.$('feedback-diagnostics').checked = true;
    q.app.handleFeedbackStatus({data: {...STATUS, diagnostics: {allowed: false, reason: 'Your organization doesn’t allow diagnostics in feedback.'}}});
    assert.equal(q.$('feedback-diagnostics').disabled, true);
    assert.equal(q.$('feedback-diagnostics').checked, false);
    assert.equal(q.$('feedback-diagnostics-off').hidden, false);
    assert.match(q.$('feedback-diagnostics-off').textContent, /doesn’t allow diagnostics/);
    assert.equal(q.$('feedback-send').disabled, false);
});

test('Send checks the form first and says what to fix', () => {
    const p = page();
    p.app.openFeedbackDialog();
    p.type('feedback-reply-to', 'ada@');
    p.$('feedback-form').dispatch('submit');
    assert.equal(p.sent.filter(m => m.command === 'feedback_send').length, 0);
    assert.equal(p.$('feedback-message-error').hidden, false);
    assert.match(p.$('feedback-message-error').textContent, /Write what happened/);
    assert.equal(p.$('feedback-message').getAttribute('aria-invalid'), 'true');
    assert.equal(p.$('feedback-reply-to').getAttribute('aria-invalid'), 'true');
    assert.equal(p.document.activeElement, p.$('feedback-message'), 'focus goes to the first field to fix');
    // An error clears as soon as its field is right.
    p.type('feedback-message', 'The build button does nothing.');
    assert.equal(p.$('feedback-message-error').hidden, true);
    assert.equal(p.$('feedback-message').getAttribute('aria-invalid'), null);
    assert.equal(p.$('feedback-message-count').textContent, '30 / 5,000');
    assert.equal(p.$('feedback-reply-to-error').hidden, false, 'still wrong, still shown');
    // Over the limit, the counter says so.
    p.type('feedback-message', 'x'.repeat(5001));
    assert.ok(p.$('feedback-message-count').classes.has('is-over'));
});

test('a report without diagnostics is sent, and the dialog says what happened and what the checks changed', () => {
    const p = page();
    p.app.openFeedbackDialog();
    p.choose('idea');
    p.type('feedback-message', 'Let me pin sessions.');
    p.$('feedback-message').dispatch('keydown', {key: 'Enter', ctrlKey: true});  // Ctrl+Enter sends
    assert.deepEqual(p.sent.at(-1), {command: 'feedback_send', preview_id: '',
        form: {kind: 'idea', message: 'Let me pin sessions.', reply_to: '', include_diagnostics: false}});
    assert.equal(p.$('feedback-send').disabled, true);
    assert.equal(p.$('feedback-send').textContent, 'Sending…');
    p.$('feedback-form').dispatch('submit');
    assert.equal(p.sent.filter(m => m.command === 'feedback_send').length, 1, 'one report per Send');

    p.app.handleFeedbackResult({ok: true, status: 'sent', message: 'Thanks. Your feedback is in Lumi Cloud (reference fbk_1).',
        notices: ['Removed 1 secret (GitHub token) from the report.', 'The reply-to address looked like a secret, so it was left out.']});
    assert.equal(p.$('feedback-form').hidden, true);
    assert.equal(p.$('feedback-done').hidden, false);
    assert.equal(p.$('feedback-done-text').textContent, 'Thanks. Your feedback is in Lumi Cloud (reference fbk_1).');
    assert.deepEqual(p.$('feedback-done-notices').children.map(item => item.textContent),
        ['Removed 1 secret (GitHub token) from the report.', 'The reply-to address looked like a secret, so it was left out.']);
    assert.equal(p.document.activeElement, p.$('feedback-done-close'));
    assert.equal(p.$('feedback-message').value, '', 'a sent report leaves an empty form');
    assert.equal(p.$('feedback-send').disabled, false);
    p.$('feedback-another').dispatch('click');
    assert.equal(p.$('feedback-form').hidden, false);
    assert.equal(p.document.activeElement, p.$('feedback-message'));
});

test('with diagnostics, Send sends only the report the person saw', () => {
    const p = page();
    p.app.openFeedbackDialog();
    p.type('feedback-message', 'It crashed.');
    p.type('feedback-diagnostics', true);
    assert.equal(p.$('feedback-preview').hidden, false);
    assert.equal(p.$('feedback-preview-meta').textContent, 'Updating the report…');
    assert.equal(p.sent.filter(m => m.command === 'feedback_preview').length, 0, 'waits for typing to stop');
    p.runTimers();
    const asked = p.sent.filter(m => m.command === 'feedback_preview');
    assert.equal(asked.length, 1);
    assert.deepEqual(asked[0].form, {kind: 'bug', message: 'It crashed.', reply_to: '', include_diagnostics: true});

    // Send before the report arrives: nothing goes; it waits to be reviewed.
    p.$('feedback-form').dispatch('submit');
    assert.equal(p.sent.filter(m => m.command === 'feedback_send').length, 0);
    const request = p.sent.at(-1).request;
    assert.equal(p.sent.at(-1).command, 'feedback_preview');
    // An answer for an older form is ignored.
    p.app.handleFeedbackPreview({request: asked[0].request, data: {preview_id: 'old', body: {message: 'old'}}});
    assert.equal(p.$('feedback-preview-body').textContent, '');
    const body = {kind: 'bug', message: 'It crashed.', diagnostics: {log_tail: 'line'}};
    p.app.handleFeedbackPreview({request, data: {preview_id: 'p1', body, destination: 'cloud.example.com',
        account: 'ada@example.com', notices: ['Removed 1 secret (GitHub token) from the report.']}});
    assert.equal(p.$('feedback-preview-body').textContent, JSON.stringify(body, null, 2));
    assert.equal(p.$('feedback-preview-meta').textContent, 'To cloud.example.com, as ada@example.com.');
    assert.deepEqual(p.$('feedback-preview-notices').children.map(item => item.textContent),
        ['Removed 1 secret (GitHub token) from the report.']);
    assert.match(p.$('feedback-progress').textContent, /Review it, then choose Send/);

    // Leaving a field without changing it keeps the report shown.
    p.$('feedback-form').dispatch('change', {target: p.$('feedback-message')});
    p.$('feedback-form').dispatch('submit');
    assert.deepEqual(p.sent.at(-1), {command: 'feedback_send', preview_id: 'p1',
        form: {kind: 'bug', message: 'It crashed.', reply_to: '', include_diagnostics: true}});
    p.app.handleFeedbackResult({ok: false, code: 'preview', message: 'The report changed since you reviewed it.'});
    assert.equal(p.sent.at(-1).command, 'feedback_preview', 'a stale report is shown again, not sent');

    // Changing the form makes the shown report stale: Send asks for a new one.
    p.app.handleFeedbackPreview({request: p.sent.at(-1).request, data: {preview_id: 'p2', body}});
    p.type('feedback-message', 'It crashed twice.');
    p.$('feedback-form').dispatch('submit');
    assert.equal(p.sent.at(-1).command, 'feedback_preview');
    assert.equal(p.sent.filter(m => m.command === 'feedback_send').length, 1);
    // Unchecking hides the report and forgets it.
    p.type('feedback-diagnostics', false);
    assert.equal(p.$('feedback-preview').hidden, true);
    p.$('feedback-form').dispatch('submit');
    assert.deepEqual(p.sent.at(-1), {command: 'feedback_send', preview_id: '',
        form: {kind: 'bug', message: 'It crashed twice.', reply_to: '', include_diagnostics: false}});
});

test('a report the check changed at Send is shown as it is now, and sent only when chosen again', () => {
    const p = page();
    p.app.openFeedbackDialog();
    p.type('feedback-message', 'Falcon broke.');
    p.type('feedback-diagnostics', true);
    p.runTimers();
    p.app.handleFeedbackPreview({request: p.sent.at(-1).request, data: {preview_id: 'p1', provisional: true,
        body: {message: 'Falcon broke.'}, destination: 'c.test'}});
    assert.match(p.$('feedback-preview-meta').textContent, /service checks it when you send it/);
    p.$('feedback-form').dispatch('submit');
    assert.equal(p.sent.at(-1).preview_id, 'p1');
    p.app.handleFeedbackResult({ok: false, code: 'review', message: 'Your organization’s data loss prevention check changed the report.',
        preview: {preview_id: 'p2', provisional: false, body: {message: '[REDACTED:dlp-service] broke.'}, destination: 'c.test'}});
    assert.match(p.$('feedback-preview-body').textContent, /REDACTED:dlp-service/);
    assert.equal(p.document.activeElement, p.$('feedback-preview-body'));
    assert.match(p.$('feedback-progress').textContent, /check changed the report/);
    assert.equal(p.sent.filter(m => m.command === 'feedback_send').length, 1, 'nothing more sent by itself');
    p.$('feedback-form').dispatch('submit');
    assert.equal(p.sent.at(-1).preview_id, 'p2');
});

test('an answer for the form as it was is dropped as soon as the form changes', () => {
    const p = page();
    p.app.openFeedbackDialog();
    p.type('feedback-message', 'It crashed.');
    p.type('feedback-diagnostics', true);
    p.runTimers();
    const asked = p.sent.filter(m => m.command === 'feedback_preview').at(-1);
    // An edit while that answer is on its way, before the next request goes (typing hasn't stopped).
    p.type('feedback-message', 'It crashed at startup.');
    p.app.handleFeedbackPreview({request: asked.request, data: {preview_id: 'old', body: {message: 'It crashed.'}}});
    assert.equal(p.$('feedback-preview-body').textContent, '');
    p.$('feedback-form').dispatch('submit');
    assert.equal(p.sent.filter(m => m.command === 'feedback_send').length, 0, 'the old report is never sent');
    const fresh = p.sent.at(-1);
    assert.equal(fresh.command, 'feedback_preview');
    assert.equal(fresh.form.message, 'It crashed at startup.');
    p.app.handleFeedbackPreview({request: fresh.request, data: {preview_id: 'new', body: {message: 'It crashed at startup.'}}});
    p.$('feedback-form').dispatch('submit');
    assert.equal(p.sent.at(-1).preview_id, 'new');
});

test('a refusal says why and offers the report to copy', async () => {
    const p = page();
    p.app.openFeedbackDialog();
    p.type('feedback-message', 'Offline here.');
    p.$('feedback-form').dispatch('submit');
    p.app.handleFeedbackResult({ok: false, code: 'offline', message: 'Offline mode: sending feedback needs cloud.example.com.',
        copy_text: 'Lumi feedback: Bug\n\nOffline here.\n'});
    assert.equal(p.$('feedback-alert').hidden, false);
    assert.equal(p.$('feedback-alert').textContent, 'Offline mode: sending feedback needs cloud.example.com.');
    assert.equal(p.$('feedback-copy').hidden, false);
    assert.equal(p.document.activeElement, p.$('feedback-copy'));
    assert.equal(p.$('feedback-message').value, 'Offline here.', 'a refused report stays in the form');
    p.$('feedback-copy').dispatch('click');
    await new Promise(resolve => setImmediate(resolve));
    assert.deepEqual(p.copied, ['Lumi feedback: Bug\n\nOffline here.\n']);
    assert.match(p.$('feedback-progress').textContent, /^Copied\./);
    p.type('feedback-message', 'Offline here, still.');
    assert.equal(p.$('feedback-progress').textContent, '', '"Copied." was about the form as it was');

    // Without a clipboard, the text is shown selected, to copy by hand.
    const q = page({clipboard: 'refused'});
    q.app.openFeedbackDialog();
    q.app.handleFeedbackResult({ok: false, code: 'rate_limited', message: 'Too many.', copy_text: 'report text'});
    q.$('feedback-copy').dispatch('click');
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(q.$('feedback-copy-text').hidden, false);
    assert.equal(q.$('feedback-copy-text').value, 'report text');
    assert.ok(q.$('feedback-copy-text').selected);
    assert.equal(q.document.activeElement, q.$('feedback-copy-text'));

    // A field Lumi refused is marked on the field, with no copy offered.
    q.app.handleFeedbackResult({ok: false, code: 'invalid', field: 'reply_to', message: 'Enter an email address.'});
    assert.equal(q.$('feedback-reply-to-error').textContent, 'Enter an email address.');
    assert.equal(q.document.activeElement, q.$('feedback-reply-to'));
});

test('a field to fix is announced, and the announcement goes once it is fixed', () => {
    const p = page();
    p.app.openFeedbackDialog();
    p.type('feedback-message', 'Hi');
    p.type('feedback-reply-to', 'ada@');
    p.$('feedback-form').dispatch('submit');
    assert.match(p.$('feedback-progress').textContent, /Enter an email address/);
    assert.equal(p.document.activeElement, p.$('feedback-reply-to'));
    p.type('feedback-message', 'Hi there');
    assert.match(p.$('feedback-progress').textContent, /Enter an email address/, 'still wrong, still said');
    p.type('feedback-reply-to', 'ada@example.com');
    assert.equal(p.$('feedback-progress').textContent, '');
});

test('what happens after the dialog closed is announced, not only shown for a moment', () => {
    const p = page();
    p.app.openFeedbackDialog();
    p.type('feedback-message', 'Offline here.');
    p.$('feedback-form').dispatch('submit');
    p.app.closeFeedbackDialog();
    p.app.handleFeedbackResult({ok: false, code: 'offline', message: 'Offline mode: sending feedback needs cloud.example.com.',
        copy_text: 'text'});
    p.runTimers();
    assert.equal(p.$('feedback-announcer').textContent, 'Offline mode: sending feedback needs cloud.example.com.');
    assert.deepEqual(p.toasts, ['Offline mode: sending feedback needs cloud.example.com.']);
    assert.equal(p.$('feedback-alert').hidden, true);
    assert.equal(p.$('feedback-message').value, 'Offline here.', 'the draft is there when it opens again');
    p.app.handleFeedbackResult({ok: true, status: 'queued', message: 'Saved on this computer.', notices: ['Removed 1 secret.']});
    p.runTimers();
    assert.equal(p.$('feedback-announcer').textContent, 'Saved on this computer. Removed 1 secret.');
    assert.equal(p.$('feedback-message').value, '');
});

test('reports waiting on this computer: sent now, copied, sent where shown, or discarded', async () => {
    const p = page();
    p.app.openFeedbackDialog();
    const reports = [
        {id: 'a', kind: 'bug', state: 'waiting', reason: 'unreachable', destination: 'cloud.example.com', here: true, copy: true, send: false},
        {id: 'b', kind: 'idea', state: 'held', reason: 'no_destination', destination: '', here: false, copy: true, send: true},
        {id: 'c', kind: 'bug', state: 'waiting', reason: 'unreachable', destination: 'other.test', here: false, copy: true, send: false},
        {id: 'd', kind: 'other', state: 'held', reason: 'dlp', destination: 'cloud.example.com', here: true, copy: false, send: false},
    ];
    p.app.handleFeedbackStatus({data: {...STATUS, waiting: 4, sendable: 1, reports}});
    assert.equal(p.$('feedback-queue').hidden, false);
    assert.equal(p.$('feedback-queue-text').textContent, '1 report waiting on this computer to go to cloud.example.com.');
    const items = p.$('feedback-held').children;
    assert.equal(items.length, 3, 'the one for this address is counted above, the others listed');
    const buttons = item => item.children[1].children.map(button => `${button.dataset.heldAction}:${button.textContent}`);
    assert.deepEqual(buttons(items[0]), ['send:Send to cloud.example.com', 'copy:Copy', 'discard:Discard']);
    assert.deepEqual(buttons(items[1]), ['copy:Copy', 'discard:Discard']);
    assert.deepEqual(buttons(items[2]), ['discard:Discard'], 'no copy of what DLP keeps here');
    assert.match(items[1].children[0].textContent, /waiting for other\.test/);
    assert.equal(items[0].children[1].children[0].getAttribute('aria-label'), 'Send the idea report to cloud.example.com');

    const click = button => p.$('feedback-held').dispatch('click', {target: button});
    click(items[0].children[1].children[0]);
    assert.deepEqual(p.sent.at(-1), {command: 'feedback_send_held', id: 'b', destination: 'https://cloud.example.com'});
    click(items[1].children[1].children[0]);
    assert.deepEqual(p.sent.at(-1), {command: 'feedback_copy_held', id: 'c'});
    p.app.handleFeedbackCopy({id: 'c', text: 'Lumi feedback: Bug\n\nkept\n'});
    await new Promise(resolve => setImmediate(resolve));
    assert.deepEqual(p.copied, ['Lumi feedback: Bug\n\nkept\n']);
    click(items[2].children[1].children[0]);
    assert.match(p.confirmed.at(-1), /Delete this report/);
    assert.deepEqual(p.sent.at(-1), {command: 'feedback_discard', ids: ['d']});
    p.app.handleFeedbackStatus({data: {...STATUS, waiting: 3, sendable: 1, reports: reports.slice(0, 3), discarded: 1}});
    assert.equal(p.$('feedback-progress').textContent, 'Deleted 1 waiting report.');

    // Send now, for the ones due here.
    p.$('feedback-flush').focus();
    p.$('feedback-flush').dispatch('click');
    p.$('feedback-flush').dispatch('click');
    assert.equal(p.sent.filter(m => m.command === 'feedback_flush').length, 1, 'one round at a time');
    assert.equal(p.$('feedback-flush').getAttribute('aria-busy'), 'true');
    assert.equal(p.$('feedback-flush').disabled, false, 'disabling it would drop the focus out of the dialog');
    p.app.handleFeedbackStatus({data: {...STATUS, waiting: 0, sendable: 0, reports: [], flushed: {sent: 1, held: 0, waiting: 0}}});
    assert.equal(p.$('feedback-progress').textContent, 'Sent 1 report.');
    assert.equal(p.$('feedback-queue').hidden, true);
    assert.equal(p.$('feedback-flush').getAttribute('aria-busy'), null);
    // Its button is gone, so the focus goes to the message box, still in the dialog.
    assert.equal(p.document.activeElement, p.$('feedback-message'));
    // Escape from wherever focus fell still closes the dialog.
    p.document.activeElement = p.document.body;
    assert.ok(p.document.dispatch('keydown', {key: 'Escape'}).defaultPrevented);
    assert.equal(p.$('feedback-dialog').style.display, 'none');

    // Discard all: what was on its way can't be taken back, and the note says so.
    p.app.openFeedbackDialog();
    p.app.handleFeedbackStatus({data: {...STATUS, waiting: 2, sendable: 2, reports: reports.slice(0, 1)}});
    p.$('feedback-discard').dispatch('click');
    assert.match(p.confirmed.at(-1), /Delete 2 reports waiting on this computer/);
    assert.deepEqual(p.sent.at(-1), {command: 'feedback_discard'});
    p.app.handleFeedbackStatus({data: {...STATUS, waiting: 1, sendable: 1, reports: reports.slice(0, 1), discarded: 1}});
    assert.equal(p.$('feedback-progress').textContent, 'Deleted 1 waiting report. 1 report was already being sent.');
    const q = page({confirm: false});
    q.app.openFeedbackDialog();
    q.app.handleFeedbackStatus({data: {...STATUS, waiting: 1, sendable: 1, reports: reports.slice(0, 1)}});
    q.$('feedback-discard').dispatch('click');
    assert.equal(q.sent.filter(m => m.command === 'feedback_discard').length, 0);
});

test('a report Lumi couldn’t make leaves no empty box to tab to', () => {
    const p = page();
    p.app.openFeedbackDialog();
    p.type('feedback-message', 'Offline here.');
    p.type('feedback-diagnostics', true);
    p.runTimers();
    p.app.handleFeedbackPreview({request: p.sent.at(-1).request, error: {code: 'offline', message: 'Offline mode: no.'},
        copy_text: 'text'});
    assert.equal(p.$('feedback-preview-body').hidden, true);
    assert.equal(p.$('feedback-preview-meta').textContent, 'Nothing to show: this report can’t be sent now.');
    assert.equal(p.$('feedback-alert').textContent, 'Offline mode: no.');
    assert.equal(p.$('feedback-copy').hidden, false);
    // Once one can be made, it's shown.
    p.type('feedback-message', 'Online again.');
    p.runTimers();
    p.app.handleFeedbackPreview({request: p.sent.at(-1).request, data: {preview_id: 'p', body: {message: 'Online again.'}}});
    assert.equal(p.$('feedback-preview-body').hidden, false);
    assert.match(p.$('feedback-preview-body').textContent, /Online again\./);
});

test('Tab and Shift+Tab stay inside the dialog', () => {
    const p = page();
    p.app.openFeedbackDialog();
    const dialog = p.$('feedback-dialog');
    // The last stop is Send: the Done panel, the hidden preview and the queue are skipped.
    p.$('feedback-send').focus();
    const forward = dialog.dispatch('keydown', {key: 'Tab'});
    assert.ok(forward.defaultPrevented);
    assert.equal(p.document.activeElement, p.$('feedback-dialog-close'));
    const back = dialog.dispatch('keydown', {key: 'Tab', shiftKey: true});
    assert.ok(back.defaultPrevented);
    assert.equal(p.document.activeElement, p.$('feedback-send'));
    // In between, the browser moves focus itself.
    p.$('feedback-message').focus();
    assert.equal(dialog.dispatch('keydown', {key: 'Tab'}).defaultPrevented, false);
    // After success the Done panel's buttons are the stops.
    p.app.handleFeedbackResult({ok: true, message: 'Sent.'});
    p.$('feedback-done-close').focus();
    dialog.dispatch('keydown', {key: 'Tab'});
    assert.equal(p.document.activeElement, p.$('feedback-dialog-close'));
});
