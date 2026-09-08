const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../resonant_client/gui/static/app.js'), 'utf8').split('function applyMixin(')[0];
const tick = () => new Promise(resolve => setImmediate(resolve));

function setup(fetch) {
    const context = vm.createContext({fetch, URLSearchParams, Blob, console});
    vm.runInContext(source + '\nthis.App = ResonantApp;', context);
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
