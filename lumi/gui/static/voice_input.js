/*
 * Dictation in the composer (docs/voice-input.md).
 *
 * window.LumiDictation.create(options) returns a controller that app.js
 * wires to the microphone button, the composer and the socket. The
 * decisions live here, so tests can drive them without a page.
 *
 * Two engines listen; lumi/voice.py says which Settings and the
 * organization's policy allow (settings._meta.voice):
 *   browser  the webview's SpeechRecognition, restarted after the pauses
 *            that end it, so dictation lasts until the person stops;
 *   service  a MediaRecorder recording, sent to Lumi when dictation stops
 *            and transcribed by the service chosen in Settings > Voice.
 *
 * press() and release() come from the button (pointer, Space, Enter) and
 * from Ctrl+Shift+Space. Held for HOLD_MS or longer, release stops; a quick
 * press keeps listening until the next press. cancel() (Escape) stops
 * without inserting anything and leaves the composer as it was.
 */
(function () {
    'use strict';

    const HOLD_MS = 350;
    // The first of these MediaRecorder supports is used; lumi/voice.py accepts them all.
    const RECORDING_TYPES = ['audio/webm;codecs=opus', 'audio/webm', 'audio/mp4', 'audio/ogg;codecs=opus', 'audio/ogg'];
    // A recognizer ending on its own this many times in a row without a word means it can't hear.
    const MAX_EMPTY_RESTARTS = 5;
    // Recognizer errors that end dictation, and what to do about them. A pause
    // ("no-speech") and our own abort ("aborted") aren't errors.
    const BROWSER_ERRORS = {
        'not-allowed': 'Allow microphone access for Lumi to dictate.',
        'service-not-allowed': 'This window’s speech recognition is turned off. Choose a transcription service in Settings > Voice.',
        'audio-capture': 'No microphone was found.',
        'network': 'Speech recognition needs an online service this window can’t reach. Choose a transcription service in Settings > Voice.',
        'language-not-supported': 'Speech recognition doesn’t support this language. Change it in Settings > Voice.',
    };

    /**
     * Which engine may listen: {engine: 'browser' | 'service' | null, reason}.
     * A service someone set up wins over the browser's recognizer in auto.
     */
    function chooseEngine(status, hasRecognizer, canRecord) {
        const s = status || {};
        const engine = s.engine || 'auto';
        if (engine === 'off') return {engine: null, reason: s.reason || 'Dictation is off in Settings > Voice.'};
        if (engine !== 'browser' && s.service_ready) {
            if (canRecord) return {engine: 'service', reason: ''};
            if (engine === 'service') return {engine: null, reason: 'This window can’t record audio.'};
        }
        if (engine !== 'service' && s.browser !== false && hasRecognizer) return {engine: 'browser', reason: ''};
        if (engine === 'service') return {engine: null, reason: s.reason || 'Choose a transcription service in Settings > Voice.'};
        if (s.browser === false) return {engine: null, reason: s.browser_reason || s.reason || 'Dictation isn’t allowed here.'};
        const fix = engine === 'auto' && s.reason ? s.reason : 'Choose a transcription service in Settings > Voice.';
        return {engine: null, reason: `This window can’t recognize speech. ${fix}`};
    }

    /** `before` and `addition` joined by one space where they meet. */
    function joinText(before, addition) {
        const base = String(before || '');
        const add = String(addition || '').trim();
        if (!add) return base;
        return base && !/\s$/.test(base) ? `${base} ${add}` : base + add;
    }

    function recordingType(Recorder) {
        if (!Recorder || typeof Recorder.isTypeSupported !== 'function') return '';
        return RECORDING_TYPES.find(type => Recorder.isTypeSupported(type)) || '';
    }

    function microphoneProblem(error) {
        const name = error && error.name;
        if (name === 'NotAllowedError' || name === 'SecurityError') return 'Allow microphone access for Lumi to dictate.';
        if (name === 'NotFoundError' || name === 'OverconstrainedError') return 'No microphone was found.';
        if (name === 'NotReadableError') return 'Another app is using the microphone.';
        return `The microphone couldn’t start: ${(error && error.message) || error}`;
    }

    /**
     * options:
     *   env         {SpeechRecognition, mediaDevices, MediaRecorder, Blob, now, setTimeout, clearTimeout}
     *   getStatus() settings._meta.voice
     *   getText(), setText(text, {interim})  the composer
     *   language()  the recognizer's language (BCP 47)
     *   transcribe(blob, type) -> Promise<string>  the socket round trip
     *   onState(state, detail)  idle | starting | listening | stopping | transcribing
     *   onError(message)
     */
    function create(options) {
        const env = options.env || {};
        const now = env.now || (() => Date.now());
        const later = env.setTimeout || setTimeout;
        const clearLater = env.clearTimeout || clearTimeout;
        let state = 'idle';
        let engine = null;
        let pressedAt = 0;
        let latched = false;       // keep listening until the next press
        let stopWanted = false;    // stop as soon as the microphone is open
        let session = 0;           // bumped by every start and cancel; stale callbacks compare it
        let original = '';         // the composer before a browser dictation
        let committed = '';        // browser: text from recognizer runs that have ended
        let heard = '';            // browser: final results of the current run
        let interim = '';
        let recognition = null;
        let recorder = null;
        let stream = null;
        let timer = null;          // the length limit, or the stop fallback
        let pending = 0;           // service: the session whose transcript is awaited

        function emit(next, detail) {
            state = next;
            if (options.onState) options.onState(next, Object.assign({engine, latched}, detail || {}));
        }

        function fail(message) {
            if (message && options.onError) options.onError(message);
        }

        function current() {
            const canRecord = Boolean(env.mediaDevices && env.mediaDevices.getUserMedia && env.MediaRecorder);
            return chooseEngine(options.getStatus ? options.getStatus() : null, Boolean(env.SpeechRecognition), canRecord);
        }

        function maxMilliseconds() {
            const status = options.getStatus ? options.getStatus() : null;
            return Math.max(10, Number(status && status.max_seconds) || 300) * 1000;
        }

        function clearTimer() {
            if (timer !== null) clearLater(timer);
            timer = null;
        }

        function stopTracks(media) {
            if (media && media.getTracks) media.getTracks().forEach(track => track.stop());
        }

        function stopStream() {
            stopTracks(stream);
            stream = null;
        }

        function finishIdle(detail) {
            clearTimer();
            recognition = null;
            recorder = null;
            stopStream();
            latched = false;
            stopWanted = false;
            emit('idle', detail);
            engine = null;
        }

        // ── The browser's recognizer ────────────────────────────────────────
        function render() {
            options.setText(joinText(original, joinText(joinText(committed, heard), interim)), {interim: Boolean(interim)});
        }

        function startBrowser(id) {
            let fatal = false;
            let emptyRuns = 0;
            try {
                recognition = new env.SpeechRecognition();
            } catch (error) {
                fail(`Speech recognition couldn’t start: ${error}`);
                finishIdle();
                return;
            }
            const rec = recognition;
            rec.continuous = true;
            rec.interimResults = true;
            rec.lang = (options.language && options.language()) || 'en-US';
            rec.onresult = event => {
                if (id !== session) return;
                // Rebuilt from every result each time, so a result that turns
                // final is counted once however the events arrive.
                let finals = '';
                let partial = '';
                for (let i = 0; i < event.results.length; i++) {
                    const result = event.results[i];
                    if (result.isFinal) finals += result[0].transcript;
                    else partial += result[0].transcript;
                }
                heard = finals.trim();
                interim = partial.trim();
                if (heard || interim) emptyRuns = 0;
                render();
            };
            rec.onerror = event => {
                if (id !== session || event.error === 'no-speech' || event.error === 'aborted') return;
                fatal = true;
                fail(BROWSER_ERRORS[event.error] || `Speech recognition stopped (${event.error || 'error'}).`);
            };
            rec.onend = () => {
                if (id !== session) return;
                const said = joinText(heard, interim);
                if (!said) emptyRuns += 1;
                committed = joinText(committed, said);
                heard = '';
                interim = '';
                // The recognizer ends by itself after a pause or about a
                // minute; keep listening while the person is dictating.
                if (state === 'listening' && !fatal) {
                    if (emptyRuns >= MAX_EMPTY_RESTARTS) {
                        fail('Dictation stopped after a long silence.');
                    } else {
                        try {
                            rec.start();
                            return;
                        } catch (_) { /* fall through and finish */ }
                    }
                }
                options.setText(joinText(original, committed), {interim: false});
                finishIdle({inserted: committed});
            };
            try {
                rec.start();
            } catch (error) {
                fail(`Speech recognition couldn’t start: ${error}`);
                finishIdle();
                return;
            }
            emit('listening');
            timer = later(() => { if (id === session && state === 'listening') finish(); }, maxMilliseconds());
        }

        function stopBrowser() {
            const id = session;
            emit('stopping');
            clearTimer();
            try { recognition.stop(); } catch (_) { /* ended already */ }
            // Some recognizers never send 'end' after stop(); settle anyway.
            timer = later(() => {
                if (id !== session || state !== 'stopping') return;
                try { recognition && recognition.abort(); } catch (_) { /* gone */ }
                session += 1;
                committed = joinText(committed, joinText(heard, interim));
                options.setText(joinText(original, committed), {interim: false});
                finishIdle({inserted: committed});
            }, 3000);
        }

        // ── A recording for the transcription service ───────────────────────
        function startService(id) {
            emit('starting');
            let opening;
            try {
                opening = env.mediaDevices.getUserMedia({audio: true});
            } catch (error) {
                opening = Promise.reject(error);
            }
            Promise.resolve(opening).then(media => {
                if (id !== session) {  // cancelled while the microphone opened
                    stopTracks(media);
                    return;
                }
                stream = media;
                const type = recordingType(env.MediaRecorder);
                const chunks = [];
                try {
                    recorder = type ? new env.MediaRecorder(media, {mimeType: type}) : new env.MediaRecorder(media);
                } catch (error) {
                    fail(`Recording couldn’t start: ${error}`);
                    finishIdle();
                    return;
                }
                const rec = recorder;
                rec.ondataavailable = event => { if (event.data && event.data.size) chunks.push(event.data); };
                rec.onstop = () => {
                    // This recording's microphone only: a new dictation may have opened another.
                    stopTracks(media);
                    if (stream === media) stream = null;
                    if (id !== session) return;
                    const kind = rec.mimeType || type || 'audio/webm';
                    const blob = new env.Blob(chunks, {type: kind});
                    if (!blob.size) {
                        fail('Nothing was recorded. Check your microphone.');
                        finishIdle();
                        return;
                    }
                    send(id, blob, kind);
                };
                rec.start();
                emit('listening');
                timer = later(() => { if (id === session && state === 'listening') finish(); }, maxMilliseconds());
                if (stopWanted) finish();
            }, error => {
                if (id !== session) return;
                fail(microphoneProblem(error));
                finishIdle();
            });
        }

        function stopService() {
            clearTimer();
            emit('stopping');
            try { recorder.stop(); } catch (_) { finishIdle(); }
        }

        function send(id, blob, kind) {
            pending = id;
            emit('transcribing');
            Promise.resolve().then(() => {
                if (pending !== id) return '';
                return options.transcribe(blob, kind);
            }).then(text => {
                if (pending !== id) return;
                pending = 0;
                const said = String(text || '').trim();
                if (said) options.setText(joinText(options.getText(), said), {interim: false});
                else fail('Nothing was heard. Try again, closer to the microphone.');
                finishIdle({inserted: said});
            }, error => {
                if (pending !== id) return;
                pending = 0;
                fail(String((error && error.message) || error || 'Transcription failed.'));
                finishIdle();
            });
        }

        // ── What the button, the shortcut and Escape call ───────────────────
        function finish() {
            if (state === 'starting') { stopWanted = true; return; }
            if (state !== 'listening') return;
            if (engine === 'browser') stopBrowser();
            else stopService();
        }

        function press() {
            if (state === 'transcribing' || state === 'stopping') return;
            if (state === 'listening' || state === 'starting') {
                if (latched) finish();
                return;
            }
            const choice = current();
            if (!choice.engine) {
                fail(choice.reason);
                return;
            }
            session += 1;
            engine = choice.engine;
            pressedAt = now();
            latched = false;
            stopWanted = false;
            original = options.getText();
            committed = '';
            heard = '';
            interim = '';
            if (engine === 'browser') startBrowser(session);
            else startService(session);
        }

        function release() {
            if (latched || (state !== 'listening' && state !== 'starting')) return;
            // A quick press, or one that waited on the microphone permission
            // prompt, keeps listening until the next press.
            if (state === 'starting' || now() - pressedAt < HOLD_MS) {
                latched = true;
                emit(state);
                return;
            }
            finish();
        }

        function cancel({restore = true, focus = true} = {}) {
            if (state === 'idle') return false;
            const was = engine;
            session += 1;
            pending = 0;
            clearTimer();
            if (recognition) {
                try { recognition.abort(); } catch (_) { /* ended already */ }
            }
            if (recorder && recorder.state !== 'inactive') {
                try { recorder.stop(); } catch (_) { /* stopped already */ }
            }
            if (was === 'browser' && restore) options.setText(original, {interim: false});
            finishIdle(focus ? {cancelled: true} : {cancelled: true, focus: false});
            return true;
        }

        return {
            press, release, cancel, finish,
            get state() { return state; },
            get engine() { return engine; },
            get latched() { return latched; },
            available: () => current(),
        };
    }

    window.LumiDictation = {create, chooseEngine, joinText, recordingType, HOLD_MS, MAX_EMPTY_RESTARTS};
})();
