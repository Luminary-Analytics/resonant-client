/**
 * SONN Client — launch access for this page.
 *
 * The server refuses the app WebSocket and its private HTTP endpoints unless a
 * request carries this launch's access token (see gui/local_access.py). The
 * desktop window and browser-mode links open the page with a one-time code in
 * the URL fragment. This script takes the code out of the address bar before
 * anything else can copy it, redeems it once for the token, and keeps the token
 * in this origin's localStorage. Origin storage is isolated per port, unlike a
 * cookie, so no other local server ever receives it; reloads, reconnects and
 * new tabs on this origin keep working while the app runs.
 *
 * Classic script, loaded in <head>: it starts the exchange before app.js
 * constructs the app, and exposes window.LumiLocalAccess.
 */
(function () {
    'use strict';

    const STORAGE_KEY = 'lumi:access';
    const FRAGMENT_KEY = 'lumi-launch';
    const HEADER = 'X-Lumi-Access';
    let memoryToken = '';

    function storedToken() {
        try {
            return localStorage.getItem(STORAGE_KEY) || memoryToken;
        } catch (_) {
            return memoryToken;
        }
    }

    function store(token) {
        memoryToken = token;
        try {
            localStorage.setItem(STORAGE_KEY, token);
        } catch (_) {
            // Storage can be disabled; the token then lasts for this page only.
        }
    }

    function takeLaunchCode() {
        const params = new URLSearchParams(location.hash.slice(1));
        const code = params.get(FRAGMENT_KEY);
        if (code === null) return '';
        params.delete(FRAGMENT_KEY);
        const rest = params.toString();
        // Replace rather than push, so neither the address bar nor this
        // history entry keeps a link that a reload or copy would reuse.
        history.replaceState(history.state, '',
            location.pathname + location.search + (rest ? `#${rest}` : ''));
        return code;
    }

    async function redeem(code) {
        const response = await fetch('/api/access', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({code}),
            cache: 'no-store',
        });
        if (!response.ok) return '';
        const data = await response.json();
        return typeof data.token === 'string' ? data.token : '';
    }

    // Pasting a launch link into a tab already showing this origin changes
    // only the fragment, which does not reload the page. Reload so the new
    // code is taken and redeemed like any other launch.
    window.addEventListener('hashchange', () => {
        if (new URLSearchParams(location.hash.slice(1)).has(FRAGMENT_KEY)) location.reload();
    });

    const code = takeLaunchCode();
    const ready = (code ? redeem(code).catch(() => '') : Promise.resolve(''))
        .then(token => {
            if (token) store(token);
            // A reopened link has already been used, but the token stored when
            // it was first opened still works for this launch.
            return token || storedToken();
        });

    window.LumiLocalAccess = {
        /** Resolves with the token ('' when this page has none) once any launch code is redeemed. */
        ready,

        token: storedToken,

        /** Headers for a request to a private HTTP endpoint. */
        headers(extra) {
            const token = storedToken();
            return token ? {...(extra || {}), [HEADER]: token} : {...(extra || {})};
        },

        /** WebSocket subprotocols: a browser WebSocket cannot send other headers. */
        protocols() {
            const token = storedToken();
            return token ? ['lumi.v1', `lumi.access.${token}`] : ['lumi.v1'];
        },

        /**
         * Whether the server accepts this page's token: true, false, or null
         * when the server cannot be reached. A refused WebSocket handshake and
         * a stopped server both look like close code 1006 to the page.
         */
        async check() {
            try {
                const response = await fetch('/api/access', {headers: this.headers(), cache: 'no-store'});
                if (response.status === 204) return true;
                if (response.status === 403) return false;
            } catch (_) {
                // Unreachable: fall through.
            }
            return null;
        },
    };
})();
