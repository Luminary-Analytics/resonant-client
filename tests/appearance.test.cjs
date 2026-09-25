const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../lumi/gui/static/appearance.js'), 'utf8');

/** Runs appearance.js against a fake <html> rendered with `setting` and an OS preference. */
function load(setting, prefersLight = false) {
    const attrs = new Map(setting === null ? [] : [['data-theme-setting', setting]]);
    const root = {
        getAttribute: name => (attrs.has(name) ? attrs.get(name) : null),
        setAttribute: (name, value) => attrs.set(name, String(value)),
        removeAttribute: name => attrs.delete(name),
    };
    const listeners = [];
    const query = {matches: prefersLight, addEventListener: (type, fn) => listeners.push(fn)};
    const context = vm.createContext({document: {documentElement: root}});
    context.window = context;
    context.window.matchMedia = () => query;
    vm.runInContext(source, context);
    return {
        api: context.LumiAppearance,
        theme: () => root.getAttribute('data-theme'),
        setting: () => root.getAttribute('data-theme-setting'),
        osChanges: prefers => { query.matches = prefers; listeners.forEach(fn => fn()); },
    };
}

test('the rendered setting applies at load; anything unknown shows dark', () => {
    assert.equal(load('light').theme(), 'light');
    assert.equal(load('dark').theme(), null);
    const unknown = load('sepia', true);
    assert.equal(unknown.theme(), null);
    assert.equal(unknown.setting(), 'dark');
    assert.equal(load(null, true).api.theme, 'dark');
});

test('match system follows the OS preference as it changes', () => {
    const page = load('system', true);
    assert.equal(page.theme(), 'light');
    page.osChanges(false);
    assert.equal(page.theme(), null);
    assert.equal(page.api.theme, 'dark');
    page.osChanges(true);
    assert.equal(page.theme(), 'light');
});

test('a fixed choice ignores later OS changes until system is chosen again', () => {
    const page = load('system', false);
    assert.equal(page.api.setTheme('light'), 'light');
    page.osChanges(false);
    assert.equal(page.theme(), 'light');
    assert.equal(page.api.setTheme('system'), 'dark');
    assert.equal(page.api.setting, 'system');
    page.osChanges(true);
    assert.equal(page.theme(), 'light');
});
