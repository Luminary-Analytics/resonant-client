const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../lumi/gui/static/appearance.js'), 'utf8');

/** An element the page starts with hidden (data-start-hidden in index.html). */
function hiddenAtStart() {
    const attrs = new Set(['data-start-hidden']);
    return {
        style: {display: ''},
        hasAttribute: name => attrs.has(name),
        removeAttribute: name => attrs.delete(name),
    };
}

/**
 * Runs appearance.js against a fake <html> rendered with `setting` and any
 * other `attributes`, an OS preference, and the page's start-hidden elements.
 */
function load(setting, prefersLight = false, attributes = {}, startHidden = []) {
    const attrs = new Map(setting === null ? [] : [['data-theme-setting', setting]]);
    for (const [name, value] of Object.entries(attributes)) attrs.set(name, value);
    const properties = new Map();
    const root = {
        getAttribute: name => (attrs.has(name) ? attrs.get(name) : null),
        setAttribute: (name, value) => attrs.set(name, String(value)),
        removeAttribute: name => attrs.delete(name),
        style: {setProperty: (name, value) => properties.set(name, value)},
    };
    const parsed = [];
    const document = {
        documentElement: root,
        addEventListener: (type, fn) => { if (type === 'DOMContentLoaded') parsed.push(fn); },
        querySelectorAll: selector => {
            assert.equal(selector, '[data-start-hidden]');
            return startHidden.filter(el => el.hasAttribute('data-start-hidden'));
        },
    };
    const listeners = [];
    const query = {matches: prefersLight, addEventListener: (type, fn) => listeners.push(fn)};
    const context = vm.createContext({document});
    context.window = context;
    context.window.matchMedia = () => query;
    vm.runInContext(source, context);
    return {
        api: context.LumiAppearance,
        theme: () => root.getAttribute('data-theme'),
        setting: () => root.getAttribute('data-theme-setting'),
        property: name => properties.get(name),
        osChanges: prefers => { query.matches = prefers; listeners.forEach(fn => fn()); },
        parsed: () => parsed.forEach(fn => fn()),
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

test('the rendered font size applies at load, through element.style', () => {
    // The page's CSP refuses style="" on <html>, so the server renders data-font-size.
    assert.equal(load('dark', false, {'data-font-size': '14'}).property('--text-base'), '14px');
    assert.equal(load('light', false, {'data-font-size': '13.5'}).property('--text-base'), '13.5px');
    // None chosen, or nothing usable: the stylesheet's size stays.
    assert.equal(load('dark').property('--text-base'), undefined);
    assert.equal(load('dark', false, {'data-font-size': 'huge'}).property('--text-base'), undefined);
});

test('elements that start hidden get an inline display: none once the page is parsed', () => {
    const dialog = hiddenAtStart();
    const palette = hiddenAtStart();
    const page = load('dark', false, {}, [dialog, palette]);
    // Until then styles.css hides the marker.
    assert.equal(dialog.style.display, '');
    page.parsed();
    // Scripts show these with style.display = '' or test it for 'none', as they
    // did when the markup said style="display:none".
    for (const el of [dialog, palette]) {
        assert.equal(el.style.display, 'none');
        assert.equal(el.hasAttribute('data-start-hidden'), false);
    }
});
