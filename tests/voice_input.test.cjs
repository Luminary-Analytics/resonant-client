const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../lumi/gui/static/voice_input.js'), 'utf8');

function loadApi() {
    const context = vm.createContext({});
    context.window = context;
    vm.runInContext(source, context);
    return context.LumiDictation;
}

const flush = () => new Promise(resolve => setImmediate(resolve));

/** A SpeechRecognition the test speaks into. */
class FakeRecognition {
    constructor() {
        FakeRecognition.made.push(this);
        this.starts = 0;
        this.stopped = false;
        this.aborted = false;
    }
    start() { this.starts += 1; }
    stop() { this.stopped = true; }
    abort() { this.aborted = true; }
    /** results: [[text, isFinal], ...], every result of the current run. */
    hear(results) {
        this.onresult({results: results.map(([text, isFinal]) => Object.assign([{transcript: text}], {isFinal}))});
    }
    end() { this.onend(); }
    fail(code) { this.onerror({error: code}); }
}

class FakeBlob {
    constructor(parts, options) {
        this.parts = parts;
        this.type = (options && options.type) || '';
        this.size = parts.reduce((total, part) => total + (part.size || 0), 0);
    }
}

/** getUserMedia and MediaRecorder; `deny` names the error getUserMedia rejects with. */
function microphone({deny = null, recorded = {size: 5}} = {}) {
    const tracks = [];
    const recorders = [];
    class Recorder {
        static isTypeSupported(type) { return type === 'audio/webm;codecs=opus'; }
        constructor(stream, options) {
            this.mimeType = (options && options.mimeType) || '';
            this.state = 'inactive';
            recorders.push(this);
        }
        start() { this.state = 'recording'; }
        stop() {
            this.state = 'inactive';
            if (this.ondataavailable) this.ondataavailable({data: recorded});
            if (this.onstop) this.onstop();
        }
    }
    const devices = {
        getUserMedia() {
            if (deny) return Promise.reject(Object.assign(new Error('refused'), {name: deny}));
            const track = {stopped: false, stop() { this.stopped = true; }};
            tracks.push(track);
            return Promise.resolve({getTracks: () => [track]});
        },
    };
    return {devices, Recorder, tracks, recorders};
}

function harness({status = {engine: 'auto'}, recognizer = true, mic = null, text = ''} = {}) {
    const api = loadApi();
    let clock = 1000;
    const timers = new Map();
    let nextTimer = 1;
    const composer = {value: text};
    const states = [];
    const errors = [];
    const sent = [];
    FakeRecognition.made = [];
    const dictation = api.create({
        env: {
            SpeechRecognition: recognizer ? FakeRecognition : undefined,
            mediaDevices: mic ? mic.devices : undefined,
            MediaRecorder: mic ? mic.Recorder : undefined,
            Blob: FakeBlob,
            now: () => clock,
            setTimeout: (fn, ms) => { timers.set(nextTimer, {fn, at: clock + ms}); return nextTimer++; },
            clearTimeout: id => timers.delete(id),
        },
        getStatus: () => status,
        getText: () => composer.value,
        setText: value => { composer.value = value; },
        language: () => 'en-GB',
        transcribe: (blob, type) => new Promise((resolve, reject) => sent.push({blob, type, resolve, reject})),
        onState: (state, detail) => states.push([state, Object.assign({}, detail)]),
        onError: message => errors.push(message),
    });
    return {
        api, dictation, composer, states, errors, sent,
        advance(ms) {
            clock += ms;
            for (const [id, timer] of [...timers]) {
                if (timer.at <= clock) { timers.delete(id); timer.fn(); }
            }
        },
        get recognition() { return FakeRecognition.made[FakeRecognition.made.length - 1]; },
        last: () => states[states.length - 1],
    };
}

test('the engine: off, a ready service, the browser, or why neither', () => {
    const {chooseEngine} = loadApi();
    const plain = value => JSON.parse(JSON.stringify(value));
    assert.deepEqual(plain(chooseEngine({engine: 'off', reason: 'Acme turned dictation off.'}, true, true)),
        {engine: null, reason: 'Acme turned dictation off.'});
    assert.equal(chooseEngine({engine: 'auto', service_ready: true}, true, true).engine, 'service');
    // A window that can't record falls back to its recognizer in auto, but not when the service was chosen.
    assert.equal(chooseEngine({engine: 'auto', service_ready: true}, true, false).engine, 'browser');
    assert.match(chooseEngine({engine: 'service', service_ready: true}, true, false).reason, /can’t record audio/);
    assert.equal(chooseEngine({engine: 'browser', service_ready: true}, true, true).engine, 'browser');
    assert.deepEqual(plain(chooseEngine({engine: 'auto', browser: false, browser_reason: 'Acme allows only services that keep no data.'}, true, true)),
        {engine: null, reason: 'Acme allows only services that keep no data.'});
    assert.equal(chooseEngine({engine: 'auto', reason: 'Add your OpenAI API key.'}, false, true).reason,
        'This window can’t recognize speech. Add your OpenAI API key.');
    assert.equal(chooseEngine({engine: 'browser'}, false, true).reason,
        'This window can’t recognize speech. Choose a transcription service in Settings > Voice.');
    assert.equal(chooseEngine({engine: 'service', reason: 'Add the key for Office Whisper.'}, true, true).reason,
        'Add the key for Office Whisper.');
});

test('dictated text joins what is there with one space', () => {
    const {joinText} = loadApi();
    assert.equal(joinText('', ' hello '), 'hello');
    assert.equal(joinText('Fix', 'the bug'), 'Fix the bug');
    assert.equal(joinText('Fix ', 'it'), 'Fix it');
    assert.equal(joinText('Keep', '  '), 'Keep');
});

test('holding dictates through the recognizer until release', () => {
    const page = harness({text: 'Please'});
    page.dictation.press();
    const rec = page.recognition;
    assert.equal(rec.continuous, true);
    assert.equal(rec.interimResults, true);
    assert.equal(rec.lang, 'en-GB');
    assert.equal(page.last()[0], 'listening');
    rec.hear([[' run the', false]]);
    assert.equal(page.composer.value, 'Please run the');
    rec.hear([[' run the tests', true]]);
    assert.equal(page.composer.value, 'Please run the tests');
    page.advance(600);
    page.dictation.release();
    assert.equal(rec.stopped, true);
    assert.equal(page.last()[0], 'stopping');
    rec.end();
    assert.equal(page.composer.value, 'Please run the tests');
    assert.deepEqual(page.last(), ['idle', {engine: 'browser', latched: false, inserted: 'run the tests'}]);
    assert.equal(rec.starts, 1);
});

test('a quick press keeps listening across pauses until the next press', () => {
    const page = harness();
    page.dictation.press();
    page.advance(100);
    page.dictation.release();
    assert.equal(page.dictation.latched, true);
    assert.equal(page.recognition.stopped, false);
    const rec = page.recognition;
    rec.hear([['first part', true]]);
    rec.end();  // the recognizer stops by itself after a pause
    assert.equal(rec.starts, 2);
    rec.hear([['second part', false]]);
    assert.equal(page.composer.value, 'first part second part');
    page.dictation.release();  // releasing the second press doesn't matter while latched
    page.dictation.press();
    assert.equal(rec.stopped, true);
    rec.end();
    assert.equal(page.composer.value, 'first part second part');
    assert.equal(page.last()[0], 'idle');
});

test('a long silence ends dictation, and a pause is not an error', () => {
    const page = harness();
    page.dictation.press();
    page.dictation.release();
    const rec = page.recognition;
    rec.fail('no-speech');
    for (let i = 0; i < page.api.MAX_EMPTY_RESTARTS; i++) rec.end();
    assert.equal(rec.starts, page.api.MAX_EMPTY_RESTARTS);
    assert.deepEqual(page.errors, ['Dictation stopped after a long silence.']);
    assert.equal(page.last()[0], 'idle');
});

test('a recognizer that can’t reach its service stops and says what to do', () => {
    const page = harness({text: 'Draft'});
    page.dictation.press();
    page.recognition.fail('network');
    page.recognition.end();
    assert.equal(page.recognition.starts, 1);
    assert.match(page.errors[0], /Choose a transcription service in Settings > Voice/);
    assert.equal(page.composer.value, 'Draft');
    assert.equal(page.last()[0], 'idle');
});

test('Escape cancels and leaves the composer as it was', () => {
    const page = harness({text: 'Draft'});
    page.dictation.press();
    const rec = page.recognition;
    rec.hear([['something I did not mean', false]]);
    assert.equal(page.dictation.cancel(), true);
    assert.equal(rec.aborted, true);
    assert.equal(page.composer.value, 'Draft');
    assert.deepEqual(page.last(), ['idle', {engine: 'browser', latched: false, cancelled: true}]);
    rec.hear([['late words', true]]);
    rec.end();
    assert.equal(page.composer.value, 'Draft');
    assert.equal(page.dictation.cancel(), false);
});

test('dictation stops at the length limit', () => {
    const page = harness({status: {engine: 'auto', max_seconds: 60}});
    page.dictation.press();
    page.dictation.release();
    page.advance(59_000);
    assert.equal(page.recognition.stopped, false);
    page.advance(1_000);
    assert.equal(page.recognition.stopped, true);
});

const SERVICE = {engine: 'auto', service_ready: true, service_name: 'OpenAI', max_seconds: 300};

test('with a service, the recording is sent when dictation stops and the text added', async () => {
    const mic = microphone();
    const page = harness({status: SERVICE, mic, text: 'Draft'});
    page.dictation.press();
    assert.equal(page.last()[0], 'starting');
    await flush();
    assert.equal(page.last()[0], 'listening');
    assert.equal(mic.recorders[0].mimeType, 'audio/webm;codecs=opus');
    page.advance(800);
    page.dictation.release();
    assert.equal(page.last()[0], 'transcribing');
    await flush();
    assert.equal(page.sent.length, 1);
    assert.equal(page.sent[0].type, 'audio/webm;codecs=opus');
    assert.equal(page.sent[0].blob.size, 5);
    assert.equal(mic.tracks[0].stopped, true);
    page.composer.value = 'Draft, edited meanwhile';
    page.sent[0].resolve(' run it ');
    await flush();
    assert.equal(page.composer.value, 'Draft, edited meanwhile run it');
    assert.deepEqual(page.last(), ['idle', {engine: 'service', latched: false, inserted: 'run it'}]);
});

test('a release while the microphone opens keeps listening until the next press', async () => {
    const mic = microphone();
    const page = harness({status: SERVICE, mic});
    page.dictation.press();
    page.advance(2_000);  // a permission prompt
    page.dictation.release();
    await flush();
    assert.equal(page.last()[0], 'listening');
    assert.equal(mic.recorders[0].state, 'recording');
    page.dictation.press();
    await flush();
    assert.equal(page.sent.length, 1);
});

test('cancelling a transcription drops its late answer', async () => {
    const mic = microphone();
    const page = harness({status: SERVICE, mic, text: 'Draft'});
    page.dictation.press();
    await flush();
    page.advance(800);
    page.dictation.release();
    await flush();
    page.dictation.cancel();
    assert.equal(page.last()[1].cancelled, true);
    page.sent[0].resolve('too late');
    await flush();
    assert.equal(page.composer.value, 'Draft');
});

test('microphone and service problems are explained', async () => {
    const denied = harness({status: SERVICE, mic: microphone({deny: 'NotAllowedError'})});
    denied.dictation.press();
    await flush();
    assert.deepEqual(denied.errors, ['Allow microphone access for Lumi to dictate.']);
    assert.equal(denied.last()[0], 'idle');

    const silent = harness({status: SERVICE, mic: microphone({recorded: {size: 0}})});
    silent.dictation.press();
    await flush();
    silent.advance(800);
    silent.dictation.release();
    assert.deepEqual(silent.errors, ['Nothing was recorded. Check your microphone.']);
    assert.equal(silent.sent.length, 0);

    const refused = harness({status: SERVICE, mic: microphone()});
    refused.dictation.press();
    await flush();
    refused.advance(800);
    refused.dictation.release();
    await flush();
    refused.sent[0].reject(new Error('OpenAI refused the key (401). Check it in Settings > Connections.'));
    await flush();
    assert.deepEqual(refused.errors, ['OpenAI refused the key (401). Check it in Settings > Connections.']);
    assert.equal(refused.last()[0], 'idle');

    const unset = harness({status: {engine: 'service', reason: 'Add your OpenAI API key.'}, mic: microphone()});
    unset.dictation.press();
    assert.deepEqual(unset.errors, ['Add your OpenAI API key.']);
    assert.deepEqual(unset.states, []);
});
