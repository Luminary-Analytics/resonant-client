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
            listeners: {}, children: []}, props);
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
    setAttribute(name, value) { this.attributes[name] = String(value); }
    removeAttribute(name) { delete this.attributes[name]; }
    getAttribute(name) { return this.attributes[name] ?? null; }
    replaceChildren() { this.children = []; }
    appendChild(child) { this.children.push(child); }
    closest(selector) {
        if (selector !== '[hidden]') return null;
        for (let node = this; node; node = node.parent) if (node.hidden) return node;
        return null;
    }
    getClientRects() { return this.closest('[hidden]') ? [] : [{}]; }
    contains(node) { return this.page.inDialog.has(node); }
}

/** The dialog's markup, as stand-ins, and a LumiFeedbackView on it. */
function page({clipboard = 'ok', confirm = true} = {}) {
    const document = {activeElement: null};
    const context = vm.createContext({console, setTimeout: (fn) => { timers.push(fn); return timers.length; },
        clearTimeout: (id) => { if (id) timers[id - 1] = null; }});
    const timers = [];
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
    for (const id of ['feedback-kind-error', 'feedback-message-error', 'feedback-reply-to-error', 'feedback-alert']) {
        inside(form, id, {hidden: true});
    }
    for (const id of ['feedback-message-count', 'feedback-destination', 'feedback-always', 'feedback-progress',
        'feedback-queue-text', 'feedback-preview-meta', 'feedback-preview-notices', 'feedback-done-text']) make(id);
    for (const element of Object.values(p.elements)) p.inDialog.add(element);
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
    // Characters as Python counts them: an emoji is one, not two UTF-16 units.
    const emoji = String.fromCodePoint(0x1F600);
    assert.equal(api.check({kind: 'bug', message: emoji.repeat(5000)}).ok, true);
    assert.match(api.check({kind: 'bug', message: 'x'.repeat(5001)}).errors.message, /5,000 characters/);
    assert.equal(api.countLabel(emoji + 'ab'), '3 / 5,000');
});

test('the wording says where a report goes, as whom, and what waits', () => {
    const {api} = page();
    assert.equal(api.destination({destination: 'cloud.example.com', account: 'ada@example.com'}),
        'It goes to cloud.example.com as ada@example.com.');
    assert.equal(api.destination({destination: 'cloud.example.com'}), 'It goes to cloud.example.com, without your account.');
    assert.match(api.destination({}), /No Lumi Cloud address is set/);
    assert.equal(api.destination({destination: 'x', offline: 'Offline mode: no.'}), 'Offline mode: no.');
    assert.equal(api.previewMeta({destination: 'cloud.example.com'}), 'To cloud.example.com, without your account.');
    assert.equal(api.previewMeta({account: 'ada@example.com'}),
        'Kept on this computer until a Lumi Cloud address is set, as ada@example.com.');
    assert.equal(api.alwaysSent({version: '0.19.2', channel: 'beta', os: 'Windows 11', arch: 'amd64'}),
        'Always sent: Lumi 0.19.2 (beta), Windows 11, amd64, and a random id for this install.');
    assert.equal(api.queueText(0), '');
    assert.equal(api.queueText(1), '1 report waiting on this computer.');
    assert.equal(api.flushText({sent: 2, dropped: 1, waiting: 1}),
        'Sent 2 reports. 1 report couldn’t be sent and was removed. 1 report still waiting: Lumi Cloud can’t take it now.');
    assert.equal(api.flushText({sent: 0, dropped: 0, waiting: 0}), 'Nothing is waiting.');
    assert.equal(api.previewText({kind: 'bug'}), '{\n  "kind": "bug"\n}');
});

test('the dialog opens, asks where reports go, and gives focus back when it closes', () => {
    const p = page();
    const opener = new El(p, 'opener');
    p.app.openFeedbackDialog(opener);
    assert.equal(p.$('feedback-dialog').style.display, 'flex');
    assert.deepEqual(p.sent, [{command: 'feedback_status'}]);
    assert.equal(p.document.activeElement, p.$('feedback-message'));
    p.app.handleFeedbackStatus({data: {destination: 'cloud.example.com', account: '', waiting: 0,
        app: {version: '0.19.2', channel: 'stable', os: 'Windows 11', arch: 'amd64'}}});
    assert.equal(p.$('feedback-destination').textContent, 'It goes to cloud.example.com, without your account.');
    assert.match(p.$('feedback-always').textContent, /^Always sent: Lumi 0\.19\.2 \(stable\)/);
    assert.equal(p.$('feedback-queue').hidden, true);
    const escape = p.$('feedback-dialog').dispatch('keydown', {key: 'Escape'});
    assert.ok(escape.defaultPrevented && escape.stopped, 'Escape is the dialog’s, not the page’s');
    assert.equal(p.$('feedback-dialog').style.display, 'none');
    assert.equal(p.document.activeElement, opener);
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
});

test('a report without diagnostics is sent, and the dialog says what happened', () => {
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

    p.app.handleFeedbackResult({ok: true, status: 'sent', message: 'Thanks. Your feedback is in Lumi Cloud (reference fbk_1).'});
    assert.equal(p.$('feedback-form').hidden, true);
    assert.equal(p.$('feedback-done').hidden, false);
    assert.equal(p.$('feedback-done-text').textContent, 'Thanks. Your feedback is in Lumi Cloud (reference fbk_1).');
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

test('reports waiting on this computer can be sent now or discarded', () => {
    const p = page();
    p.app.openFeedbackDialog();
    p.app.handleFeedbackStatus({data: {destination: '', waiting: 2}});
    assert.equal(p.$('feedback-queue').hidden, false);
    assert.equal(p.$('feedback-queue-text').textContent, '2 reports waiting on this computer.');
    assert.equal(p.$('feedback-flush').disabled, true, 'nowhere to send them yet');
    p.app.handleFeedbackStatus({data: {destination: 'cloud.example.com', waiting: 2}});
    assert.equal(p.$('feedback-flush').disabled, false);
    p.$('feedback-flush').focus();
    p.$('feedback-flush').dispatch('click');
    p.$('feedback-flush').dispatch('click');
    assert.deepEqual(p.sent.filter(m => m.command === 'feedback_flush').length, 1, 'one round at a time');
    assert.equal(p.$('feedback-flush').getAttribute('aria-busy'), 'true');
    assert.equal(p.$('feedback-flush').disabled, false, 'disabling it would drop the focus out of the dialog');
    p.app.handleFeedbackStatus({data: {destination: 'cloud.example.com', waiting: 0, flushed: {sent: 2, dropped: 0, waiting: 0}}});
    assert.equal(p.$('feedback-progress').textContent, 'Sent 2 reports.');
    assert.equal(p.$('feedback-queue').hidden, true);
    assert.equal(p.$('feedback-flush').getAttribute('aria-busy'), null);
    // Its button is gone, so the focus goes to the message box, still in the dialog.
    assert.equal(p.document.activeElement, p.$('feedback-message'));
    // Escape from wherever focus fell still closes the dialog.
    p.document.activeElement = p.document.body;
    assert.ok(p.document.dispatch('keydown', {key: 'Escape'}).defaultPrevented);
    assert.equal(p.$('feedback-dialog').style.display, 'none');
    p.app.openFeedbackDialog();

    p.app.handleFeedbackStatus({data: {destination: 'cloud.example.com', waiting: 1}});
    p.$('feedback-discard').dispatch('click');
    assert.match(p.confirmed[0], /Delete 1 report waiting on this computer/);
    assert.equal(p.sent.at(-1).command, 'feedback_discard');
    const q = page({confirm: false});
    q.app.openFeedbackDialog();
    q.app.handleFeedbackStatus({data: {destination: 'cloud.example.com', waiting: 1}});
    q.$('feedback-discard').dispatch('click');
    assert.equal(q.sent.filter(m => m.command === 'feedback_discard').length, 0);
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

test('what happens after the dialog closed still reaches the person', () => {
    const p = page();
    p.app.openFeedbackDialog();
    p.type('feedback-message', 'Offline here.');
    p.$('feedback-form').dispatch('submit');
    p.app.closeFeedbackDialog();
    p.app.handleFeedbackResult({ok: false, code: 'offline', message: 'Offline mode: sending feedback needs cloud.example.com.',
        copy_text: 'text'});
    assert.deepEqual(p.toasts, ['Offline mode: sending feedback needs cloud.example.com.']);
    assert.equal(p.$('feedback-alert').hidden, true);
    assert.equal(p.$('feedback-message').value, 'Offline here.', 'the draft is there when it opens again');
    p.app.handleFeedbackResult({ok: true, status: 'queued', message: 'Saved on this computer.'});
    assert.equal(p.toasts.at(-1), 'Saved on this computer.');
    assert.equal(p.$('feedback-message').value, '');
});

test('Discard says what it deleted, and what was already on its way', () => {
    const p = page();
    p.app.openFeedbackDialog();
    p.app.handleFeedbackStatus({data: {destination: 'cloud.example.com', waiting: 1, discarded: 2}});
    assert.equal(p.$('feedback-progress').textContent, 'Deleted 2 waiting reports. 1 report was already being sent.');
    p.app.handleFeedbackStatus({data: {destination: 'cloud.example.com', waiting: 0, discarded: 0}});
    assert.equal(p.$('feedback-progress').textContent, 'Nothing was deleted.');
    assert.equal(p.api.flushText({sent: 0, dropped: 0, waiting: 2, failed: true}),
        'Lumi couldn’t send the waiting reports. It will try again later.');
    assert.equal(p.api.flushText({busy: true}), 'Lumi is already sending the waiting reports.');
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
