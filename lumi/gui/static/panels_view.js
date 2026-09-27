/* Panels from capability packs (docs/extensions.md#panels).
 *
 * An approved pack's panel is a page made of the pack's own files. The server
 * serves them under /panels/<panel token>/ with a Content-Security-Policy of
 * their own (gui/extension_panels.py), and this view shows them in
 * <iframe sandbox="allow-scripts">: an opaque origin, with no access to this
 * page, its storage (which holds the launch's access token) or its socket.
 *
 * The panel reaches the app only by postMessage, checked here: a message
 * counts only from that exact frame (event.source) with the sandbox's opaque
 * origin ("null"), in the bridge's shape, within size and rate limits. A panel
 * can read the project's name, the session's title and the theme; add text to
 * the message box without sending it; show a notice that names the panel; and
 * close itself when the person presses Escape in it. Nothing else.
 *
 * Panels are listed under View in the application menu and in the command
 * palette, only when an approved pack has one, and open in a dialog.
 */
(function () {
    'use strict';

    const PROTOCOL = 1;
    const MAX_FIELDS = 8;
    const MAX_ID = 64;
    const MAX_INSERT = 8000;
    const MAX_TOAST = 200;
    const MAX_COMPOSER = 100000;
    const MAX_LISTED = 50;
    const REFRESH_MS = 3000;
    const METHODS = new Set(['context', 'composer.insert', 'toast', 'close']);
    const PANEL_URL = /^\/panels\/[A-Za-z0-9_-]{32}\/[^?#\s]*$/;

    /**
     * Text as the person will see it in the message box: line breaks as \n,
     * and no format or control characters but tab and newline. Invisible
     * characters (zero-width, bidirectional and tag characters) could carry
     * words the model reads but the person never sees.
     */
    function visibleText(text) {
        return String(text)
            .replace(/\r\n?|\p{Zl}|\p{Zp}/gu, '\n')
            .replace(/(?![\t\n])[\p{Cc}\p{Cf}]/gu, '');
    }

    /**
     * A panel's message as the bridge takes it: {id, method, text?}, or
     * {id, error} for a request it refuses (answered), or null for anything
     * that isn't a bridge request at all (ignored).
     */
    function checkRequest(data) {
        if (!data || typeof data !== 'object' || Array.isArray(data) || data.lumi !== PROTOCOL) return null;
        const id = data.id;
        const idOk = id === undefined
            || (typeof id === 'number' && Number.isSafeInteger(id))
            || (typeof id === 'string' && id.length <= MAX_ID);
        if (!idOk) return null;
        if (Object.keys(data).length > MAX_FIELDS) return {id, error: 'The request has too many fields.'};
        const method = data.method;
        if (typeof method !== 'string' || !METHODS.has(method)) {
            return {id, error: `The panel bridge has no method ${JSON.stringify(String(method).slice(0, 40))}.`};
        }
        if (method !== 'composer.insert' && method !== 'toast') return {id, method};
        const limit = method === 'toast' ? MAX_TOAST : MAX_INSERT;
        if (typeof data.text !== 'string' || data.text.length > limit) {
            return {id, error: `text must be a string of up to ${limit} characters.`};
        }
        let text = visibleText(data.text);
        if (method === 'toast') text = text.replace(/\s+/g, ' ').trim();
        if (!text.trim()) return {id, error: 'text is empty.'};
        return {id, method, text};
    }

    /** Allows at most `limit` take() calls in any `windowMs`. */
    function rateLimit(limit, windowMs, now = () => Date.now()) {
        const times = [];
        return {
            take() {
                const at = now();
                while (times.length && at - times[0] >= windowMs) times.shift();
                if (times.length >= limit) return false;
                times.push(at);
                return true;
            },
        };
    }

    /** A row of the server's list, or null. */
    function listedPanel(row) {
        if (!row || typeof row !== 'object') return null;
        const {pack, pack_name: packName, panel, title} = row;
        if (![pack, packName, panel, title].every(value => typeof value === 'string' && value)) return null;
        return {pack, pack_name: packName, panel, title};
    }

    function element(tag, className, text) {
        const node = document.createElement(tag);
        if (className) node.className = className;
        if (text !== undefined) node.textContent = text;
        return node;
    }

    window.LumiPanelBridge = Object.freeze({PROTOCOL, MAX_INSERT, MAX_TOAST, checkRequest, visibleText, rateLimit});

    window.LumiPanelsView = class LumiPanelsView {
        /** Called once from bindEvents: the bridge's listener, View's entries and theme changes. */
        bindExtensionPanels() {
            this._extensionPanels = null;
            this._extensionPanelsReason = '';
            this._extensionPanel = null;
            this._extensionPanelOpening = null;
            this._extensionPanelsAsked = 0;
            window.addEventListener('message', event => this._receiveExtensionPanelMessage(event));
            // The application menu is opened from this button; View lists what it finds.
            document.querySelector('.titlebar-menu-button')?.addEventListener('click', () => this._requestExtensionPanels(true));
            // Coming back to the window (from Settings in another window, or an editor)
            // checks that an open panel's pack is still approved and unchanged.
            window.addEventListener('focus', () => {
                if (this._extensionPanel) this._requestExtensionPanels();
            });
            if (typeof MutationObserver === 'function') {
                new MutationObserver(() => this._postExtensionPanelContext())
                    .observe(document.documentElement, {attributes: true, attributeFilter: ['data-theme']});
            }
        }

        /** Command palette entries, one per panel; asks for a fresh list at most every few seconds. */
        extensionPanelCommands() {
            this._requestExtensionPanels();
            return (this._extensionPanels || []).map(panel => ({
                id: `panel:${panel.pack}/${panel.panel}`,
                icon: '▣',
                label: `Open panel: ${panel.title}`,
                hint: panel.pack_name,
                action: () => this.openExtensionPanel(panel.pack, panel.panel),
            }));
        }

        _requestExtensionPanels(force = false) {
            const now = Date.now();
            if (!force && now - (this._extensionPanelsAsked || 0) < REFRESH_MS) return;
            this._extensionPanelsAsked = now;
            this.send({command: 'extension_panels'});
        }

        /** The server's list (gui/extension_panels.listing). */
        receiveExtensionPanels(event) {
            const before = JSON.stringify(this._extensionPanels);
            const rows = Array.isArray(event.panels) ? event.panels.map(listedPanel).filter(Boolean) : [];
            this._extensionPanels = event.enabled === false ? [] : rows.slice(0, MAX_LISTED);
            this._extensionPanelsReason = typeof event.reason === 'string' ? event.reason : '';
            this._renderExtensionPanelMenu();
            const open = this._extensionPanel;
            if (open && !this._extensionPanels.some(row => row.pack === open.pack && row.panel === open.panel)) {
                this.closeExtensionPanel({notice: `${open.title} closed. ${this._extensionPanelsReason
                    || 'Its pack is no longer approved, or it changed.'}`});
            }
            if (before !== JSON.stringify(this._extensionPanels)) this._refreshPaletteForPanels();
        }

        _refreshPaletteForPanels() {
            const overlay = document.getElementById('command-palette');
            const input = document.getElementById('cmd-palette-input');
            if (!overlay || overlay.style.display === 'none' || !input
                || typeof this._renderCommandPaletteResults !== 'function') return;
            this._cmdPaletteIdx = 0;
            this._renderCommandPaletteResults(input.value);
        }

        /** View's "Panels" entries, after the menu's own items. */
        _renderExtensionPanelMenu() {
            const dropdown = document.querySelector('.menubar-item[data-menu="view"] .menubar-dropdown');
            if (!dropdown) return;
            dropdown.querySelectorAll('[data-extension-panel-menu]').forEach(node => node.remove());
            const panels = this._extensionPanels || [];
            if (!panels.length) return;
            const separator = element('div', 'menubar-sep');
            const heading = element('div', 'extension-panel-menu-heading', 'Panels');
            separator.dataset.extensionPanelMenu = '';
            heading.dataset.extensionPanelMenu = '';
            dropdown.append(separator, heading);
            for (const panel of panels) {
                const item = element('div', 'menubar-action extension-panel-menu-item');
                item.dataset.extensionPanelMenu = '';
                item.title = `Open the ${panel.title} panel from the ${panel.pack_name} pack`;
                item.append(element('span', '', panel.title), element('span', 'menubar-shortcut', panel.pack_name));
                item.addEventListener('click', () => {
                    this._closeAppMenuForPanel();
                    this.openExtensionPanel(panel.pack, panel.panel);
                });
                dropdown.appendChild(item);
            }
        }

        _closeAppMenuForPanel() {
            document.querySelector('.titlebar-menus')?.classList.remove('is-open');
            document.querySelector('.titlebar-menu-button')?.setAttribute('aria-expanded', 'false');
        }

        /**
         * Whether this window could let a panel reach more than the bridge.
         * WebKit (the macOS and Linux desktop window) gives the window's
         * script message handler to every frame in it, so a panel there could
         * reach the window's own controls. Such a window opens panels only in
         * the browser (File > Open in Browser).
         */
        _extensionPanelsBlockedHere() {
            return Boolean(window.webkit && window.webkit.messageHandlers);
        }

        openExtensionPanel(packId, panelId) {
            if (this._extensionPanelsBlockedHere()) {
                this.showToastMessage('Open panels in your browser: File > Open in Browser. This window can’t isolate them yet.');
                return;
            }
            const active = document.activeElement;
            const opener = active && active !== document.body && typeof active.focus === 'function' ? active : null;
            const requestId = typeof crypto !== 'undefined' && crypto.randomUUID
                ? crypto.randomUUID() : `${Date.now()}-${Math.random()}`;
            this._extensionPanelOpening = {requestId, opener};
            this.send({command: 'extension_panel_open', pack_id: packId, panel_id: panelId, request_id: requestId});
        }

        /** The server's answer to extension_panel_open: a token and URL, or why not. */
        receiveExtensionPanelOpened(event) {
            const pending = this._extensionPanelOpening;
            if (!pending || event.request_id !== pending.requestId) {
                // An answer to an earlier click: its token isn't wanted, so withdraw it.
                if (typeof event.token === 'string' && event.token) {
                    this.send({command: 'extension_panel_close', token: event.token});
                }
                return;
            }
            this._extensionPanelOpening = null;
            if (event.error) {
                this.showToastMessage(String(event.error));
                this._requestExtensionPanels(true);
                return;
            }
            const info = listedPanel(event);
            if (!info || typeof event.url !== 'string' || !PANEL_URL.test(event.url) || typeof event.token !== 'string') return;
            this._showExtensionPanel({...info, url: event.url, token: event.token}, pending.opener);
        }

        _showExtensionPanel(info, opener) {
            this.closeExtensionPanel({restoreFocus: false});
            const overlay = element('div', 'dialog-overlay extension-panel-overlay');
            overlay.id = 'extension-panel-dialog';
            overlay.setAttribute('role', 'dialog');
            overlay.setAttribute('aria-modal', 'true');
            overlay.setAttribute('aria-labelledby', 'extension-panel-title');
            overlay.setAttribute('aria-describedby', 'extension-panel-source');
            // Focus wraps between the close button and the panel: keys typed in
            // the frame never reach this page, so Tab past its last control lands here.
            const start = element('span', 'extension-panel-sentinel');
            const end = element('span', 'extension-panel-sentinel');
            start.tabIndex = 0;
            end.tabIndex = 0;
            const dialog = element('div', 'dialog extension-panel-dialog');
            const header = element('div', 'dialog-header extension-panel-header');
            const heading = element('div', 'extension-panel-heading');
            const title = element('h2', '', info.title);
            title.id = 'extension-panel-title';
            const source = element('p', 'extension-panel-source',
                `From the ${info.pack_name} capability pack. It runs apart from Lumi: it can add text to your message but not send it.`);
            source.id = 'extension-panel-source';
            heading.append(title, source);
            const close = element('button', 'dialog-btn-close extension-panel-close', '×');
            close.type = 'button';
            close.title = 'Close (Esc)';
            close.setAttribute('aria-label', `Close ${info.title}`);
            header.append(heading, close);
            const frame = element('iframe', 'extension-panel-frame');
            // Set before the source: a frame's sandbox applies from its next navigation.
            frame.setAttribute('sandbox', 'allow-scripts');
            frame.setAttribute('referrerpolicy', 'no-referrer');
            frame.title = `${info.title}, a panel from the ${info.pack_name} pack`;
            dialog.append(header, frame);
            overlay.append(start, dialog, end);
            document.body.appendChild(overlay);
            this._extensionPanel = {
                pack: info.pack, panel: info.panel, title: info.title, packName: info.pack_name, token: info.token,
                overlay, frame, opener,
                limits: {all: rateLimit(20, 1000), toast: rateLimit(1, 1000)},
            };
            frame.addEventListener('load', () => this._postExtensionPanelContext());
            close.addEventListener('click', () => this.closeExtensionPanel());
            overlay.addEventListener('keydown', event => {
                if (event.key !== 'Escape') return;
                event.preventDefault();
                event.stopPropagation();
                this.closeExtensionPanel();
            });
            overlay.addEventListener('click', event => {
                if (event.target === overlay) this.closeExtensionPanel();
            });
            start.addEventListener('focus', () => frame.focus());
            end.addEventListener('focus', () => close.focus());
            frame.src = info.url;
            close.focus();
        }

        /** Close the open panel, withdraw its token and give focus back to what opened it. */
        closeExtensionPanel({restoreFocus = true, notice = ''} = {}) {
            const panel = this._extensionPanel;
            if (!panel) return;
            this._extensionPanel = null;
            panel.overlay.remove();
            this.send({command: 'extension_panel_close', token: panel.token});
            if (notice) this.showToastMessage(notice);
            if (!restoreFocus) return;
            const visible = node => node && node.isConnected && typeof node.focus === 'function'
                && (typeof node.getClientRects !== 'function' || node.getClientRects().length > 0);
            const target = [panel.opener, this.userInput].find(visible);
            if (target) target.focus();
        }

        /** What a panel may read: the project's folder name, the session's title, the theme. */
        _extensionPanelContext() {
            const session = typeof this._currentSessionSummary === 'function' ? this._currentSessionSummary() : null;
            const theme = window.LumiAppearance?.theme
                || (document.documentElement.getAttribute('data-theme') === 'light' ? 'light' : 'dark');
            return {
                project: this.currentCwd && typeof this._projectNameFromPath === 'function'
                    ? this._projectNameFromPath(this.currentCwd) : '',
                session: this.currentSessionId && session && typeof session.title === 'string'
                    ? session.title.slice(0, 200) : '',
                theme: theme === 'light' ? 'light' : 'dark',
            };
        }

        _postToExtensionPanel(panel, message) {
            // The frame's origin is opaque, so no other target origin can name it;
            // the message goes to that frame's window only and carries only what
            // the panel may read.
            panel.frame.contentWindow?.postMessage(message, '*');
        }

        _postExtensionPanelContext() {
            const panel = this._extensionPanel;
            if (panel) this._postToExtensionPanel(panel, {lumi: PROTOCOL, event: 'context', context: this._extensionPanelContext()});
        }

        _receiveExtensionPanelMessage(event) {
            const panel = this._extensionPanel;
            // Only the open panel's own frame, which its sandbox gives the opaque origin "null".
            if (!panel || !panel.frame.contentWindow || event.source !== panel.frame.contentWindow
                || event.origin !== 'null') return;
            const request = checkRequest(event.data);
            if (!request) return;
            const answer = (ok, fields) => this._postToExtensionPanel(panel, {lumi: PROTOCOL, id: request.id, ok, ...fields});
            // Refused requests count too: every one gets an answer, so all are bounded.
            if (!panel.limits.all.take()) return answer(false, {error: 'Too many requests. Wait a moment.'});
            if (request.error) return answer(false, {error: request.error});
            switch (request.method) {
            case 'context':
                return answer(true, {result: this._extensionPanelContext()});
            case 'composer.insert': {
                const error = this._insertFromExtensionPanel(panel, request.text);
                return error ? answer(false, {error}) : answer(true, {result: null});
            }
            case 'toast':
                if (!panel.limits.toast.take()) return answer(false, {error: 'A panel can show one notice a second.'});
                // Always named, so a notice can't pass for one of Lumi's own.
                this.showToastMessage(`${panel.title}: ${request.text}`);
                return answer(true, {result: null});
            case 'close':
                // The bridge sends it for Escape pressed in the panel, which then has focus.
                if (document.activeElement !== panel.frame) {
                    return answer(false, {error: 'A panel closes when the person presses Escape in it.'});
                }
                return this.closeExtensionPanel();
            default:
                return undefined;
            }
        }

        /** Add a panel's text after the draft, like the editor bridge: it is never sent. */
        _insertFromExtensionPanel(panel, text) {
            const input = this.userInput;
            const bar = this.inputBar;
            if (!input || !this._draftScope || this.isReplaying || (bar && bar.style.display === 'none')) {
                return 'There is no message box to add to. Open a session first.';
            }
            const current = input.value;
            const joiner = current && !/\s$/.test(current) ? '\n' : '';
            if (current.length + joiner.length + text.length > MAX_COMPOSER) return 'The message would be too long.';
            input.value = `${current}${joiner}${text}`;
            const end = input.value.length;
            if (typeof input.setSelectionRange === 'function') input.setSelectionRange(end, end);
            if (typeof this._markDraftEdited === 'function') this._markDraftEdited();
            if (typeof this._saveDraft === 'function') this._saveDraft();
            input.dispatchEvent(new Event('input', {bubbles: true}));
            // Text ending in an @mention would otherwise open the file picker behind the panel.
            if (typeof this._closeFileFuzzy === 'function') this._closeFileFuzzy();
            this.showToastMessage(`${panel.title} added text to your message. Nothing is sent until you send it.`);
            return '';
        }
    };
})();
