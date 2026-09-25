'use strict';
// The VS Code extension (lumi/code_editors/vscode) against a simulated VS Code
// API and a stand-in for Lumi's editor bridge. Nothing here starts VS Code.

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const http = require('node:http');
const Module = require('node:module');
const os = require('node:os');
const path = require('node:path');

const EXTENSION_DIR = path.join(__dirname, '..', 'lumi', 'code_editors', 'vscode');
const bridge = require(path.join(EXTENSION_DIR, 'bridge.js'));

class Uri {
    constructor(scheme, uriPath, query = '', fsPath = '') {
        this.scheme = scheme;
        this.path = uriPath;
        this.query = query;
        this.fsPath = fsPath || uriPath;
    }

    static file(fsPath) {
        return new Uri('file', fsPath.replace(/\\/g, '/'), '', fsPath);
    }

    static from({ scheme, path: uriPath, query = '' }) {
        return new Uri(scheme, uriPath, query);
    }

    toString() {
        return `${this.scheme}:${this.path}${this.query ? `?${this.query}` : ''}`;
    }
}

function fakeVscode() {
    const fake = {
        commands: {}, providers: {}, messages: [], executed: [], answers: {},
        Uri,
        window: {
            activeTextEditor: null,
            tabGroups: { all: [] },
            showInformationMessage: async (message) => { fake.messages.push(['info', message]); },
            showWarningMessage: async (message, ...items) => {
                fake.messages.push(['warning', message]);
                return items.length ? fake.answers.warning : undefined;
            },
            showErrorMessage: async (message) => { fake.messages.push(['error', message]); },
            showInputBox: async () => fake.answers.input,
            showQuickPick: async (items, options) => {
                fake.quickPick = { items, options };
                return fake.answers.pick ? fake.answers.pick(items) : undefined;
            },
        },
        workspace: {
            textDocuments: [],
            registerTextDocumentContentProvider: (scheme, provider) => {
                fake.providers[scheme] = provider;
                return { dispose() {} };
            },
        },
    };
    fake.commands = {
        registered: {},
        registerCommand: (id, handler) => {
            fake.commands.registered[id] = handler;
            return { dispose() {} };
        },
        executeCommand: async (...args) => { fake.executed.push(args); },
    };
    return fake;
}

/** Load extension.js with ``vscode`` resolved to the fake, and activate it. */
function loadExtension(fake) {
    const original = Module._load;
    Module._load = function load(request, ...rest) {
        return request === 'vscode' ? fake : original.call(this, request, ...rest);
    };
    try {
        const file = path.join(EXTENSION_DIR, 'extension.js');
        delete require.cache[require.resolve(file)];
        const extension = require(file);
        extension.activate({ subscriptions: [] });
        return extension;
    } finally {
        Module._load = original;
    }
}

function document(fsPath, { dirty = false } = {}) {
    const doc = { uri: Uri.file(fsPath), isDirty: dirty, saved: 0 };
    doc.save = async () => { doc.saved += 1; doc.isDirty = false; return true; };
    return doc;
}

function selection(startLine, startCharacter, endLine, endCharacter) {
    return {
        start: { line: startLine, character: startCharacter },
        end: { line: endLine, character: endCharacter },
        isEmpty: startLine === endLine && startCharacter === endCharacter,
    };
}

/** A stand-in for Lumi's /api/editor endpoints that records what it was sent. */
async function fakeLumi(t, token = 'bridge-token') {
    const received = [];
    const project = path.join(os.tmpdir(), 'lumi-vscode-project');
    const server = http.createServer((req, res) => {
        const chunks = [];
        req.on('data', (chunk) => chunks.push(chunk));
        req.on('end', () => {
            const url = new URL(req.url, 'http://127.0.0.1');
            const body = chunks.length ? JSON.parse(Buffer.concat(chunks).toString('utf8')) : null;
            received.push({ method: req.method, path: url.pathname, query: Object.fromEntries(url.searchParams), body,
                            headers: req.headers });
            const send = (status, value, type = 'application/json') => {
                res.writeHead(status, { 'Content-Type': type });
                res.end(type === 'application/json' ? JSON.stringify(value) : value);
            };
            if (req.headers.authorization !== `Bearer ${token}`) return send(403, { error: 'Forbidden' });
            if (url.pathname === '/api/editor/status') {
                return send(200, { ok: true, project, version: '0.20.0', window: true });
            }
            if (url.pathname === '/api/editor/context') {
                const outside = body.items.filter((item) => item.path.includes('outside'));
                if (outside.length === body.items.length) {
                    return send(409, { error: "outside.py: It isn't in the project Lumi has open." });
                }
                return send(200, {
                    ok: true,
                    attached: body.items.filter((item) => !outside.includes(item)).map((item) => path.basename(item.path)),
                    skipped: outside.map((item) => ({ path: item.path, reason: "It isn't in the project Lumi has open." })),
                });
            }
            if (url.pathname === '/api/editor/changes') {
                return send(200, {
                    project,
                    turn: { before: 'cp_00003_abcdef12', prompt: 'Fix the parser', finished: true,
                            compared_with: "the snapshot taken before the turn's first change" },
                    files: [
                        { path: 'src/app.py', status: 'modified', before_path: 'src/app.py' },
                        { path: 'src/new.py', status: 'added', before_path: '' },
                        { path: 'old.txt', status: 'deleted', before_path: 'old.txt' },
                    ],
                    more: 0,
                });
            }
            if (url.pathname === '/api/editor/before') {
                return send(200, `before: ${url.searchParams.get('path')}\n`, 'application/octet-stream');
            }
            return send(404, { error: 'Unknown editor request.' });
        });
    });
    await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
    t.after(() => server.close());
    const home = fs.mkdtempSync(path.join(os.tmpdir(), 'lumi-vscode-state-'));
    t.after(() => fs.rmSync(home, { recursive: true, force: true }));
    fs.writeFileSync(path.join(home, 'editor-bridge.json'),
                     JSON.stringify({ version: 1, url: `http://127.0.0.1:${server.address().port}`, token, pid: process.pid }));
    const previous = process.env.LUMI_STATE_HOME;
    process.env.LUMI_STATE_HOME = home;
    t.after(() => {
        if (previous === undefined) delete process.env.LUMI_STATE_HOME;
        else process.env.LUMI_STATE_HOME = previous;
    });
    return { received, project, home, port: server.address().port };
}

test('the bridge file is found where Lumi keeps its state, and only for this computer', (t) => {
    const home = fs.mkdtempSync(path.join(os.tmpdir(), 'lumi-home-'));
    t.after(() => fs.rmSync(home, { recursive: true, force: true }));
    assert.equal(bridge.stateHome({ LUMI_STATE_HOME: '/custom' }, home), '/custom');
    assert.equal(bridge.stateHome({}, home), path.join(home, '.lumi'));
    fs.mkdirSync(path.join(home, '.resonant'));
    assert.equal(bridge.stateHome({}, home), path.join(home, '.resonant')); // not moved yet
    fs.mkdirSync(path.join(home, '.lumi'));
    assert.equal(bridge.stateHome({}, home), path.join(home, '.lumi'));

    assert.throws(() => bridge.readBridge({}, home), { message: bridge.NOT_RUNNING });
    const file = path.join(home, '.lumi', 'editor-bridge.json');
    fs.writeFileSync(file, JSON.stringify({ url: 'http://127.0.0.1:8123/', token: 't' }));
    assert.deepEqual(bridge.readBridge({}, home), { url: 'http://127.0.0.1:8123', token: 't' });
    fs.writeFileSync(file, JSON.stringify({ url: 'http://127.0.0.1:8123', token: 't', pid: process.pid }));
    assert.equal(bridge.readBridge({}, home).token, 't');
    // Left behind by a launch that was killed: its process is gone.
    const finished = require('node:child_process').spawnSync(process.execPath, ['-e', '']);
    fs.writeFileSync(file, JSON.stringify({ url: 'http://127.0.0.1:8123', token: 't', pid: finished.pid }));
    assert.throws(() => bridge.readBridge({}, home), { message: bridge.NOT_RUNNING });
    fs.writeFileSync(file, JSON.stringify({ url: 'http://example.com:8123', token: 't' }));
    assert.throws(() => bridge.readBridge({}, home), /doesn't point at this computer/);
    fs.writeFileSync(file, '{"url": 5}');
    assert.throws(() => bridge.readBridge({}, home), /unreadable/);
});

test('selections become 1-based line ranges', () => {
    const items = bridge.selectionItems('/p/a.py', [
        selection(9, 4, 12, 0), // ends at the start of line 13: lines 10-12
        selection(9, 0, 11, 7), // the same lines
        selection(20, 0, 21, 3),
        selection(3, 2, 3, 2), // empty
    ]);
    assert.deepEqual(items, [
        { path: '/p/a.py', start_line: 10, end_line: 12 },
        { path: '/p/a.py', start_line: 21, end_line: 22 },
    ]);
    assert.deepEqual(bridge.selectionItems('/p/a.py', [selection(4, 1, 4, 9)]),
                     [{ path: '/p/a.py', start_line: 5, end_line: 5 }]);
    assert.deepEqual(bridge.selectionItems('/p/a.py', [selection(3, 2, 3, 2)]), [{ path: '/p/a.py' }]);
    assert.equal(bridge.statusLabel({ status: 'renamed', before_path: 'a.py' }), 'renamed from a.py');
    assert.equal(bridge.statusLabel({ status: 'added' }), 'new file');
});

test('requests carry the token, and failures say what went wrong', async (t) => {
    const lumi = await fakeLumi(t);
    const connection = bridge.readBridge();
    const status = await bridge.request(connection, 'GET', '/api/editor/status');
    assert.equal(status.version, '0.20.0');
    const seen = lumi.received.at(-1);
    assert.equal(seen.headers.authorization, 'Bearer bridge-token');
    assert.equal(seen.headers.origin, undefined);
    await assert.rejects(bridge.request({ ...connection, token: 'wrong' }, 'GET', '/api/editor/status'),
                         /Restart Lumi/);
    await assert.rejects(bridge.request(connection, 'POST', '/api/editor/context', { items: [{ path: '/x/outside.py' }] }),
                         (error) => error.status === 409 && /isn't in the project/.test(error.message));
    const closed = http.createServer();
    await new Promise((resolve) => closed.listen(0, '127.0.0.1', resolve));
    const port = closed.address().port;
    await new Promise((resolve) => closed.close(resolve));
    await assert.rejects(bridge.request({ url: `http://127.0.0.1:${port}`, token: 't' }, 'GET', '/api/editor/status'),
                         { message: bridge.NOT_RUNNING });
});

test('Send Selection adds the selected lines, saving first when asked', async (t) => {
    const lumi = await fakeLumi(t);
    const vscode = fakeVscode();
    loadExtension(vscode);
    const file = path.join(lumi.project, 'src', 'app.py');
    const doc = document(file, { dirty: true });
    vscode.window.activeTextEditor = { document: doc, selections: [selection(1, 0, 3, 0)] };

    await vscode.commands.registered['lumi.sendSelection']();  // the warning is dismissed
    assert.equal(lumi.received.length, 0);
    assert.match(vscode.messages.at(-1)[1], /unsaved changes/);

    vscode.answers.warning = 'Save and Send';
    await vscode.commands.registered['lumi.sendSelection']();
    assert.equal(doc.saved, 1);
    const sent = lumi.received.at(-1);
    assert.equal(sent.method, 'POST');
    assert.deepEqual(sent.body, { items: [{ path: file, start_line: 2, end_line: 3 }], text: '', source: 'VS Code' });
    assert.match(vscode.messages.at(-1)[1], /Added to your message in Lumi: app\.py\. Send it from Lumi\./);

    vscode.answers.input = 'Why does this fail?';
    await vscode.commands.registered['lumi.askAboutSelection']();
    assert.equal(lumi.received.at(-1).body.text, 'Why does this fail?');
    vscode.answers.input = undefined;  // the question box was closed
    const before = lumi.received.length;
    await vscode.commands.registered['lumi.askAboutSelection']();
    assert.equal(lumi.received.length, before);

    vscode.window.activeTextEditor = { document: { uri: new Uri('untitled', 'Untitled-1'), isDirty: true }, selections: [] };
    await vscode.commands.registered['lumi.sendSelection']();
    assert.match(vscode.messages.at(-1)[1], /only files saved on this computer/);
});

test('Send File and Send Open Files add whole files, and report what was left out', async (t) => {
    const lumi = await fakeLumi(t);
    const vscode = fakeVscode();
    loadExtension(vscode);
    const one = Uri.file(path.join(lumi.project, 'one.py'));
    const two = Uri.file(path.join(lumi.project, 'two.py'));
    const outside = Uri.file(path.join(os.tmpdir(), 'outside.py'));
    await vscode.commands.registered['lumi.sendFile'](one, [one, two, new Uri('git', '/x')]);
    assert.deepEqual(lumi.received.at(-1).body.items, [{ path: one.fsPath }, { path: two.fsPath }]);

    vscode.window.tabGroups.all = [
        { tabs: [{ input: { uri: one } }, { input: { uri: outside } }] },
        { tabs: [{ input: { uri: one } }, { input: {} }, { input: { uri: new Uri('untitled', 'Untitled-2') } }] },
    ];
    await vscode.commands.registered['lumi.sendOpenFiles']();
    assert.deepEqual(lumi.received.at(-1).body.items, [{ path: one.fsPath }, { path: outside.fsPath }]);
    const kinds = vscode.messages.slice(-2).map(([kind]) => kind);
    assert.deepEqual(kinds, ['warning', 'info']);
    assert.match(vscode.messages.at(-2)[1], /Not added: outside\.py \(It isn't in the project/);

    vscode.window.tabGroups.all = [];
    const count = lumi.received.length;
    await vscode.commands.registered['lumi.sendOpenFiles']();
    assert.equal(lumi.received.length, count);
    assert.match(vscode.messages.at(-1)[1], /no saved files to send/);
});

test("Review Lumi's Changes opens each file beside its earlier version", async (t) => {
    const lumi = await fakeLumi(t);
    const vscode = fakeVscode();
    loadExtension(vscode);
    vscode.answers.pick = (items) => items[0];  // Open all
    await vscode.commands.registered['lumi.reviewChanges']();
    assert.equal(vscode.quickPick.options.title, "Lumi's changes: Fix the parser");
    assert.deepEqual(vscode.quickPick.items.map((item) => item.label), ['Open all', 'src/app.py', 'src/new.py', 'old.txt']);
    assert.equal(vscode.executed.length, 3);
    const [changed, added, deleted] = vscode.executed;
    assert.equal(changed[0], 'vscode.diff');
    assert.equal(changed[1].scheme, 'lumi-before');
    assert.equal(new URLSearchParams(changed[1].query).get('before'), 'cp_00003_abcdef12');
    assert.equal(changed[2].fsPath, path.join(lumi.project, 'src', 'app.py'));
    assert.equal(changed[3], 'app.py (before Lumi ↔ now)');
    assert.deepEqual(changed[4], { preview: false });
    assert.equal(added[1].query, 'empty=1');  // nothing before
    assert.equal(deleted[2].query, 'empty=1');  // nothing now

    const provider = vscode.providers['lumi-before'];
    assert.equal(await provider.provideTextDocumentContent(changed[1]), 'before: src/app.py\n');
    assert.deepEqual(lumi.received.at(-1).query, { before: 'cp_00003_abcdef12', path: 'src/app.py' });
    assert.equal(await provider.provideTextDocumentContent(added[1]), '');

    vscode.answers.pick = (items) => items.find((item) => item.label === 'src/app.py');
    vscode.executed.length = 0;
    await vscode.commands.registered['lumi.reviewChanges']();
    assert.equal(vscode.executed.length, 1);
    assert.deepEqual(vscode.executed[0][4], { preview: true });

    await vscode.commands.registered['lumi.status']();
    assert.match(vscode.messages.at(-1)[1], /Lumi 0\.20\.0 is running with .*lumi-vscode-project open\./);
});

test('without Lumi running, each command says so', async (t) => {
    const home = fs.mkdtempSync(path.join(os.tmpdir(), 'lumi-none-'));
    t.after(() => fs.rmSync(home, { recursive: true, force: true }));
    const previous = process.env.LUMI_STATE_HOME;
    process.env.LUMI_STATE_HOME = home;
    t.after(() => {
        if (previous === undefined) delete process.env.LUMI_STATE_HOME;
        else process.env.LUMI_STATE_HOME = previous;
    });
    const vscode = fakeVscode();
    loadExtension(vscode);
    await vscode.commands.registered['lumi.reviewChanges']();
    assert.deepEqual(vscode.messages.at(-1), ['error', `Lumi: ${bridge.NOT_RUNNING}`]);
    await vscode.commands.registered['lumi.sendFile'](Uri.file(path.join(home, 'a.py')));
    assert.deepEqual(vscode.messages.at(-1), ['error', `Lumi: ${bridge.NOT_RUNNING}`]);
});

test('the manifest declares every command the extension registers', () => {
    const manifest = JSON.parse(fs.readFileSync(path.join(EXTENSION_DIR, 'package.json'), 'utf8'));
    const vscode = fakeVscode();
    loadExtension(vscode);
    const declared = manifest.contributes.commands.map((item) => item.command).sort();
    assert.deepEqual(Object.keys(vscode.commands.registered).sort(), declared);
    for (const menu of Object.values(manifest.contributes.menus)) {
        for (const item of menu) assert.ok(declared.includes(item.command), item.command);
    }
    assert.equal(manifest.main, './extension.js');
});
