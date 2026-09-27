/* Lumi's panel bridge (docs/extensions.md#panels).
 *
 * Lumi adds this script to every HTML file of a capability pack's panel,
 * before anything else in it (gui/extension_panels.py). It runs inside the
 * panel's sandboxed frame with the panel's own permissions and no others, and
 * wraps the postMessage protocol that Lumi's page checks (static/panels_view.js):
 *
 *   lumi.context()      resolves to {project, theme}
 *   lumi.insert(text)   adds text to the message box; it is never sent
 *   lumi.toast(text)    shows a short notice, marked as the panel's
 *   lumi.onContext(fn)  calls fn with the context when the panel loads and
 *                       when the theme changes
 *
 * It also marks <html> with data-lumi-theme="dark" or "light" for the
 * panel's styles, and closes the panel when the person presses Escape in it.
 *
 * Closing goes over a private channel (a MessagePort) that Lumi's page hands
 * this script when the panel loads, and only for an Escape key press the
 * browser reports as real (isTrusted). Running first, this script registers
 * the first listener and takes the port before the panel's scripts can see
 * it, and it keeps its own copies of what it calls, so a panel can neither
 * send on the channel nor fake the key. Lumi's page takes no close request
 * any other way: a panel can't close itself to move the person's focus.
 */
(function () {
    'use strict';

    if (window.lumi) return;
    const PROTOCOL = 1;
    const TIMEOUT_MS = 10000;
    // Taken before any panel script runs, so later changes to them don't reach
    // here. The close channel may use nothing a panel could replace afterwards.
    const parentWindow = window.parent;
    const portPost = Function.prototype.call.bind(MessagePort.prototype.postMessage);
    const portsOf = Function.prototype.call.bind(Object.getOwnPropertyDescriptor(MessageEvent.prototype, 'ports').get);
    const sourceOf = Function.prototype.call.bind(Object.getOwnPropertyDescriptor(MessageEvent.prototype, 'source').get);
    const dataOf = Function.prototype.call.bind(Object.getOwnPropertyDescriptor(MessageEvent.prototype, 'data').get);
    const stopEvent = Function.prototype.call.bind(Event.prototype.stopImmediatePropagation);
    const keyOf = Function.prototype.call.bind(Object.getOwnPropertyDescriptor(KeyboardEvent.prototype, 'key').get);
    const prevented = Function.prototype.call.bind(Object.getOwnPropertyDescriptor(Event.prototype, 'defaultPrevented').get);
    const pending = new Map();
    const listeners = [];
    let next = 1;
    let channel = null;

    function send(message) {
        parentWindow.postMessage(Object.assign({lumi: PROTOCOL}, message), '*');
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

    // The private channel: first in line (capture, registered before any other
    // script), and hidden from every listener after this one.
    window.addEventListener('message', event => {
        if (sourceOf(event) !== parentWindow) return;
        const data = dataOf(event);
        if (!data || data.lumi !== PROTOCOL || data.event !== 'port') return;
        stopEvent(event);
        // By index, not by destructuring, which a panel could redirect through Array.prototype.
        const port = portsOf(event)[0];
        if (port) channel = port;
    }, true);

    window.addEventListener('message', event => {
        const data = event.data;
        if (event.source !== parentWindow || !data || data.lumi !== PROTOCOL) return;
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
        if (!event.isTrusted || keyOf(event) !== 'Escape' || prevented(event) || !channel) return;
        portPost(channel, {lumi: PROTOCOL, method: 'close'});
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
