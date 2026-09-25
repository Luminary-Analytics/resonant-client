// The autonomous session launch card (lumi/gui/static/autonomous_view.js).
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

const source = fs.readFileSync(path.join(__dirname, '../lumi/gui/static/autonomous_view.js'), 'utf8');
const context = vm.createContext({console, window: {}, document: {}});
vm.runInContext(source, context);
const view = context.window.LumiAutonomousView.prototype;

const spec = (criteria) => `## Final spec

**Refined intent:** Build a counter.

**Time budget:** 4h

**Acceptance criteria:**
${criteria}

**Open risks:**
- none
`;

test('the card accepts criteria in the form the grill prompt asks for', () => {
    // grill_me asks for "- `[bash]` ..."; this was refused before.
    const result = view._validateAutonomousSpec(spec('- `[bash]` `npm test` exits 0\n- `[chrome]` Button increments'));
    assert.equal(result.ok, true);
    assert.equal(result.criteriaCount, 2);
});

test('the roadmap checkbox form is still accepted', () => {
    const result = view._validateAutonomousSpec(spec('- [ ] `[bash]` `npm test` exits 0\n- [x] `[vision]` Centered'));
    assert.equal(result.ok, true);
    assert.equal(result.criteriaCount, 2);
});

test('untyped criteria are refused with a readable reason', () => {
    const result = view._validateAutonomousSpec(spec('- It should work'));
    assert.equal(result.ok, false);
    assert.match(result.reason, /no typed criteria/);
    assert.equal(view._validateAutonomousSpec('no spec here').ok, false);
});

test('stop reasons read as words', () => {
    assert.equal(view._autonomousStopReason('spend_limit_reached'), 'spending limit reached');
    assert.equal(view._autonomousStopReason('time_budget_exhausted'), 'time budget used up');
    assert.equal(view._autonomousStopReason('mode_not_allowed'), 'not allowed by policy');
    assert.equal(view._autonomousStopReason('something_new'), 'something_new');
});

test('a refused dispatch puts its Build button back, once', () => {
    // e.g. the organization's policy doesn't allow Full-auto (source "mission_dispatch").
    let restored = 0;
    const app = {_pendingMissionDispatch: () => { restored += 1; }};
    view._missionDispatchRefused.call(app);
    view._missionDispatchRefused.call(app);
    assert.equal(restored, 1);
    assert.equal(app._pendingMissionDispatch, null);
});

test('a dispatch that went through keeps its dispatched state', () => {
    let restored = 0;
    const app = {
        _pendingMissionDispatch: () => { restored += 1; },
        _currentSessionSummary: () => null,
        _refreshMissionBadge: () => {},
    };
    view.handleMissionPhaseChanged.call(app, {phase: 'planning_dispatched'});
    view._missionDispatchRefused.call(app);  // a later, unrelated refusal
    assert.equal(restored, 0);
});
