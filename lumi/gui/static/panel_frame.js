/* Lumi's panel bridge (docs/extensions.md#panels).
 *
 * Lumi adds this script to the start of every HTML file of a capability pack's
 * panel (gui/extension_panels.py). It runs inside the panel's sandboxed frame
 * with the panel's own permissions and no others, and only wraps the
 * postMessage protocol that Lumi's page checks (static/panels_view.js):
 *
 *   lumi.context()      resolves to {project, session, theme}
 *   lumi.insert(text)   adds text to the message box; it is never sent
 *   lumi.toast(text)    shows a short notice that names the panel
 *   lumi.onContext(fn)  calls fn with the context when the panel loads and
 *                       when the theme changes
 *
 * It also tells Lumi when the person presses Escape in the panel and the
 * panel didn't handle the key itself, so the panel closes, and marks <html>
 * with data-lumi-theme="dark" or "light" for the panel's styles.
 */
(function () {
    'use strict';

    if (window.lumi) return;
    const PROTOCOL = 1;
    const TIMEOUT_MS = 10000;
    const pending = new Map();
    const listeners = [];
    let next = 1;

    function send(message) {
        window.parent.postMessage(Object.assign({lumi: PROTOCOL}, message), '*');
    }

    function call(method, fields) {
        const id = next++;
        return new Promise((resolve, reject) => {
            const timer = setTimeout(() => {
                if (pending.delete(id)) reject(new Error('Lumi did not answer.'));
            }, TIMEOUT_MS);
            pending.set(id, {resolve, reject, timer});
            send(Object.assign({id, method}, fields));
        });
    }

    function applyTheme(context) {
        const theme = context && context.theme === 'light' ? 'light' : 'dark';
        document.documentElement.setAttribute('data-lumi-theme', theme);
        document.documentElement.style.colorScheme = theme;
    }

    window.addEventListener('message', event => {
        const data = event.data;
        if (event.source !== window.parent || !data || data.lumi !== PROTOCOL) return;
        if (data.event === 'context') {
            applyTheme(data.context);
            for (const listener of listeners) {
                try {
                    listener(data.context);
                } catch (error) {
                    console.error(error);
                }
            }
            return;
        }
        const waiting = pending.get(data.id);
        if (!waiting) return;
        pending.delete(data.id);
        clearTimeout(waiting.timer);
        if (data.ok) waiting.resolve(data.result);
        else waiting.reject(new Error(String(data.error || 'Lumi refused the request.')));
    });

    // On window, so it runs after the panel's own handlers: a panel that
    // handles Escape itself (to close a menu of its own) keeps the key.
    window.addEventListener('keydown', event => {
        if (event.key === 'Escape' && !event.defaultPrevented) send({method: 'close'});
    });

    window.lumi = Object.freeze({
        context: () => call('context'),
        insert: text => call('composer.insert', {text: String(text)}),
        toast: text => call('toast', {text: String(text)}),
        onContext(listener) {
            if (typeof listener === 'function') listeners.push(listener);
        },
    });
})();
