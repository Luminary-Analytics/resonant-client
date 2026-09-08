const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../resonant_client/gui/static/app.js'), 'utf8').split('function applyMixin(')[0];
const tick = () => new Promise(resolve => setImmediate(resolve));

function setup(fetch) {
    const context = vm.createContext({fetch, URLSearchParams, Blob, console, WebSocket: {OPEN: 1}});
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
