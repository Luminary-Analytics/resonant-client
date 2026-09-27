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
 * can read the project's name and the theme, add text to the message box
 * without sending it, and show a notice marked as its own. Nothing else.
 *
 * Text a panel adds is held to what the person can review: no invisible
 * characters, no padding, at most 20 lines, no @mention that would attach a
 * file, and never a message that would start with ! or / and run as a
 * command. Its pack is checked again on the server before each addition.
 *
 * A panel can't close itself. Escape pressed in it reaches this page only
 * over a private channel that Lumi's bridge script holds and uses for a real
 * key press; a panel that closed itself could move the person's focus to the
 * message box. When a panel closes, focus goes back to the menu or the
 * command palette's button, never to the message box.
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
    const MAX_INSERT_LINES = 20;
    const MAX_INDENT = 8;
    const MAX_TOAST = 200;
    const MAX_COMPOSER = 100000;
    const MAX_LISTED = 50;
    const MAX_CHECKS = 3;
    const CHECK_TIMEOUT_MS = 8000;
    const NOTICE_MS = 4000;
    const REFRESH_MS = 3000;
    const METHODS = new Set(['context', 'composer.insert', 'toast']);
    const PANEL_URL = /^\/panels\/[A-Za-z0-9_-]{32}\/[^?#\s]*$/;
    // What the server reads as an attachment (engine/context_broker.MENTION_RE),
    // widened to any letters so no case-folding trick slips through.
    const MENTION = /@(?=[\p{L}\p{N}_-]{1,40}:)/gu;

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
     * A panel's text for the message box: no padding to push words out of
     * view (runs of spaces become one, indentation at most 8, at most one
     * blank line in a row), and @mentions split apart ("@ file:") so they
     * attach nothing. Returns {text, lines, mentions}.
     */
    function tidyInsert(text) {
        const kept = [];
        for (const line of visibleText(text).split('\n')) {
            const indent = line.match(/^[\p{Zs}\t]*/u)[0];
            const body = line.slice(indent.length).replace(/[\p{Zs}\t]+/gu, ' ').trimEnd();
            if (!body) {
                if (kept.length && kept[kept.length - 1]) kept.push('');
                continue;
            }
            const columns = [...indent].reduce((sum, ch) => sum + (ch === '\t' ? 4 : 1), 0);
            kept.push(' '.repeat(Math.min(columns, MAX_INDENT)) + body);
        }
        while (kept.length && !kept[kept.length - 1]) kept.pop();
        let mentions = 0;
        const joined = kept.join('\n').replace(MENTION, () => { mentions += 1; return '@ '; });
        return {text: joined, lines: kept.length, mentions};
    }

    /**
     * A panel's message as the bridge takes it: {id, method, text?, mentions?},
     * or {id, error} for a request it refuses (answered), or null for anything
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
        if (method === 'close') {
            return {id, error: 'A panel closes when the person presses Escape in it or chooses Close.'};
        }
        if (typeof method !== 'string' || !METHODS.has(method)) {
            return {id, error: `The panel bridge has no method ${JSON.stringify(String(method).slice(0, 40))}.`};
        }
        if (method === 'context') return {id, method};
        const limit = method === 'toast' ? MAX_TOAST : MAX_INSERT;
        if (typeof data.text !== 'string' || data.text.length > limit) {
            return {id, error: `text must be a string of up to ${limit} characters.`};
        }
        if (method === 'toast') {
            const text = visibleText(data.text).replace(/\s+/g, ' ').trim();
            return text ? {id, method, text} : {id, error: 'text is empty.'};
        }
        const tidy = tidyInsert(data.text);
        if (!tidy.text) return {id, error: 'text is empty.'};
        if (tidy.lines > MAX_INSERT_LINES) return {id, error: `text may have at most ${MAX_INSERT_LINES} lines.`};
        return {id, method, text: tidy.text, mentions: tidy.mentions};
    }

    /** What the message box would hold after `text` is added after `current`: {joiner, next}. */
    function composed(current, text) {
        const joiner = current && !/\s$/.test(current) ? '\n' : '';
        return {joiner, next: `${current}${joiner}${text}`};
    }

    /** The command a message starting this way would run instead of being sent (! and !! run shell commands, / runs Lumi's). */
    function commandStart(message) {
        const first = message.trimStart().charAt(0);
        return first === '!' || first === '/' ? first : '';
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

    window.LumiPanelBridge = Object.freeze({
        PROTOCOL, MAX_INSERT, MAX_INSERT_LINES, MAX_TOAST, checkRequest, visibleText, tidyInsert, composed,
        commandStart, rateLimit,
    });

    window.LumiPanelsView = class LumiPanelsView {
        /** Called once from bindEvents: the bridge's listener, View's entries and theme changes. */
        bindExtensionPanels() {
            this._extensionPanels = null;
            this._extensionPanelsReason = '';
            this._extensionPanel = null;
            this._extensionPanelOpening = null;
            this._extensionPanelsAsked = 0;
            this._extensionPanelChecks = new Map();
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
                action: () => this.openExtensionPanel(panel.pack, panel.panel, 'palette'),
            }));
        }

        _requestExtensionPanels(force = false) {
            const now = Date.now();
            if (!force && now - (this._extensionPanelsAsked || 0) < REFRESH_MS) return;
            this._extensionPanelsAsked = now;
            this.send({command: 'extension_panels'});
        }

        /** The server's list (gui/extension_panels.listing), also sent after anything that may change it. */
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

        /** The app socket closed: the server has withdrawn the panel's token, so the panel goes too. */
        extensionPanelsDisconnected() {
            if (this._extensionPanel) {
                this.closeExtensionPanel({notice: `${this._extensionPanel.title} closed: Lumi's connection dropped.`});
            }
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
                    this.openExtensionPanel(panel.pack, panel.panel, 'menu');
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
         * In the desktop window, pywebview's own bridge must answer only this
         * page. On Windows (WebView2, "edgechromium") a frame's messages don't
         * reach it; WebKit (macOS, Linux) and Qt give it to every frame. So
         * panels open only in a browser or WebView2; other desktop windows
         * send the person to File > Open in Browser.
         */
        _extensionPanelsBlockedHere() {
            const bridge = window.pywebview;
            if (bridge && typeof bridge === 'object' && typeof bridge.platform === 'string') {
                return bridge.platform !== 'edgechromium';
            }
            return Boolean((window.webkit && window.webkit.messageHandlers) || window.qt);
        }

        /** Open a panel; `launcher` ('menu' or 'palette') is where focus goes back to when it closes. */
        openExtensionPanel(packId, panelId, launcher = 'menu') {
            if (this._extensionPanelsBlockedHere()) {
                this.showToastMessage('Open panels in your browser: File > Open in Browser. This window can’t isolate them yet.');
                return;
            }
            const requestId = typeof crypto !== 'undefined' && crypto.randomUUID
                ? crypto.randomUUID() : `${Date.now()}-${Math.random()}`;
            this._extensionPanelOpening = {requestId, launcher: launcher === 'palette' ? 'palette' : 'menu'};
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
            this._showExtensionPanel({...info, url: event.url, token: event.token}, pending.launcher);
        }

        _showExtensionPanel(info, launcher) {
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
            // The panel's own notices, apart from Lumi's, and always marked as the panel's.
            const notice = element('p', 'extension-panel-notice');
            notice.setAttribute('role', 'status');
            notice.hidden = true;
            dialog.append(header, frame, notice);
            overlay.append(start, dialog, end);
            document.body.appendChild(overlay);
            const panel = {
                pack: info.pack, panel: info.panel, title: info.title, packName: info.pack_name, token: info.token,
                overlay, frame, notice, launcher, port: null, noticeTimer: null,
                limits: {all: rateLimit(20, 1000), toast: rateLimit(1, 1000)},
            };
            this._extensionPanel = panel;
            frame.addEventListener('load', () => {
                this._openExtensionPanelChannel(panel);
                this._postExtensionPanelContext();
            });
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

        /**
         * A private channel for each page the panel loads, handed to Lumi's
         * bridge script there. It carries only "close", which the bridge sends
         * for a real Escape key press in the panel.
         */
        _openExtensionPanelChannel(panel) {
            if (typeof MessageChannel !== 'function' || !panel.frame.contentWindow) return;
            if (panel.port) panel.port.close();
            const channel = new MessageChannel();
            panel.port = channel.port1;
            channel.port1.onmessage = event => {
                const data = event.data;
                if (this._extensionPanel !== panel || panel.port !== channel.port1) return;
                if (!data || data.lumi !== PROTOCOL || data.method !== 'close') return;
                // Escape was pressed in the panel, which therefore has focus.
                if (document.activeElement !== panel.frame) return;
                this.closeExtensionPanel();
            };
            panel.frame.contentWindow.postMessage({lumi: PROTOCOL, event: 'port'}, '*', [channel.port2]);
        }

        /** Close the open panel, withdraw its token and give focus back to where it was opened from. */
        closeExtensionPanel({restoreFocus = true, notice = ''} = {}) {
            const panel = this._extensionPanel;
            if (!panel) return;
            this._extensionPanel = null;
            if (panel.port) panel.port.close();
            clearTimeout(panel.noticeTimer);
            panel.overlay.remove();
            this.send({command: 'extension_panel_close', token: panel.token});
            const checks = this._extensionPanelChecksMap();
            for (const [requestId, check] of checks) {
                if (check.panel === panel) {
                    clearTimeout(check.timer);
                    checks.delete(requestId);
                }
            }
            if (notice) this.showToastMessage(notice);
            if (restoreFocus) this._focusExtensionPanelLauncher(panel.launcher);
        }

        /**
         * Focus goes back to the control that opens panels (the application
         * menu's button, or the command palette's), never to the message box:
         * the next Enter there would send whatever a panel put in it.
         */
        _focusExtensionPanelLauncher(launcher) {
            const order = launcher === 'palette'
                ? ['#titlebar-command', '.titlebar-menu-button'] : ['.titlebar-menu-button', '#titlebar-command'];
            for (const selector of order) {
                const node = document.querySelector(selector);
                const shown = node && (typeof node.getClientRects !== 'function' || node.getClientRects().length > 0);
                if (shown && typeof node.focus === 'function') {
                    node.focus();
                    return;
                }
            }
        }

        /** What a panel may read: the project's folder name and the theme. */
        _extensionPanelContext() {
            const theme = window.LumiAppearance?.theme
                || (document.documentElement.getAttribute('data-theme') === 'light' ? 'light' : 'dark');
            return {
                project: this.currentCwd && typeof this._projectNameFromPath === 'function'
                    ? this._projectNameFromPath(this.currentCwd) : '',
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
            case 'composer.insert':
                return this._checkExtensionPanelInsert(panel, request, answer);
            case 'toast':
                if (!panel.limits.toast.take()) return answer(false, {error: 'A panel can show one notice a second.'});
                this._showExtensionPanelNotice(panel, request.text);
                return answer(true, {result: null});
            default:
                return undefined;
            }
        }

        /** A panel's notice, in its dialog, marked with the pack's name; never in Lumi's own notices. */
        _showExtensionPanelNotice(panel, text) {
            panel.notice.textContent = `Panel · ${panel.packName}: ${text}`;
            panel.notice.hidden = false;
            clearTimeout(panel.noticeTimer);
            panel.noticeTimer = setTimeout(() => { panel.notice.hidden = true; }, NOTICE_MS);
        }

        /** Why text can't go into the message box now, or ''. */
        _extensionPanelInsertRefusal(panel, text) {
            const input = this.userInput;
            const bar = this.inputBar;
            if (!input || !this._draftScope || this.isReplaying || (bar && bar.style.display === 'none')) {
                return 'There is no message box to add to. Open a session first.';
            }
            const {next} = composed(input.value, text);
            if (next.length > MAX_COMPOSER) return 'The message would be too long.';
            const command = commandStart(next);
            if (command) {
                return `Lumi didn’t add ${panel.title}’s text: your message would start with ${command} and run as a command.`;
            }
            return '';
        }

        /** Additions waiting for the server's check, by request id. */
        _extensionPanelChecksMap() {
            if (!this._extensionPanelChecks) this._extensionPanelChecks = new Map();
            return this._extensionPanelChecks;
        }

        /** Refuse what can be refused here, then ask the server whether the pack is still as approved. */
        _checkExtensionPanelInsert(panel, request, answer) {
            const refusal = this._extensionPanelInsertRefusal(panel, request.text);
            if (refusal) {
                this.showToastMessage(refusal);
                return answer(false, {error: refusal});
            }
            const checks = this._extensionPanelChecksMap();
            if (checks.size >= MAX_CHECKS) {
                return answer(false, {error: 'Lumi is still checking the last text. Wait a moment.'});
            }
            const requestId = typeof crypto !== 'undefined' && crypto.randomUUID
                ? crypto.randomUUID() : `${Date.now()}-${Math.random()}`;
            const timer = setTimeout(() => {
                if (checks.delete(requestId)) answer(false, {error: 'Lumi couldn’t check the panel’s pack.'});
            }, CHECK_TIMEOUT_MS);
            checks.set(requestId, {panel, request, answer, timer});
            this.send({command: 'extension_panel_check', token: panel.token, request_id: requestId});
            return undefined;
        }

        /** The server's answer to extension_panel_check. */
        receiveExtensionPanelChecked(event) {
            const checks = this._extensionPanelChecksMap();
            const check = checks.get(event.request_id);
            if (!check) return;
            checks.delete(event.request_id);
            clearTimeout(check.timer);
            const {panel, request, answer} = check;
            if (this._extensionPanel !== panel) return;
            if (!event.ok) {
                const why = String(event.error || 'Its pack is no longer approved, or it changed.');
                answer(false, {error: why});
                this.closeExtensionPanel({notice: `${panel.title} closed. ${why}`});
                return;
            }
            // Checked again: the message box may have changed while the server answered.
            const refusal = this._extensionPanelInsertRefusal(panel, request.text);
            if (refusal) {
                this.showToastMessage(refusal);
                answer(false, {error: refusal});
                return;
            }
            this._insertFromExtensionPanel(panel, request.text, request.mentions);
            answer(true, {result: null});
        }

        /** Add a panel's text after the draft, like the editor bridge: it is never sent. */
        _insertFromExtensionPanel(panel, text, mentions = 0) {
            const input = this.userInput;
            const {next} = composed(input.value, text);
            const start = next.length - text.length;
            input.value = next;
            if (typeof this._markDraftEdited === 'function') this._markDraftEdited();
            if (typeof this._saveDraft === 'function') this._saveDraft();
            input.dispatchEvent(new Event('input', {bubbles: true}));
            // Text ending in an @mention would otherwise open the file picker behind the panel.
            if (typeof this._closeFileFuzzy === 'function') this._closeFileFuzzy();
            this._revealComposerAt(start);
            const split = mentions ? ' Mentions in it (such as @file:) were split apart, so they attach nothing.' : '';
            this.showToastMessage(`${panel.title} (${panel.packName} pack) added text to your message.${split} Nothing is sent until you send it.`);
        }

        /**
         * Put the caret where the added text starts and scroll the message box
         * so that line shows, whatever came before it: a long draft can't hide
         * the start of what a panel added.
         */
        _revealComposerAt(start) {
            const input = this.userInput;
            if (typeof input.setSelectionRange === 'function') input.setSelectionRange(start, start);
            if (typeof getComputedStyle !== 'function' || !input.isConnected) return;
            const style = getComputedStyle(input);
            const full = input.value;
            const {height, minHeight} = input.style;
            // The height of the text before the addition is where the addition begins.
            input.value = full.slice(0, start);
            input.style.height = '0px';
            input.style.minHeight = '0px';
            const before = input.scrollHeight;
            input.value = full;
            input.style.height = height;
            input.style.minHeight = minHeight;
            input.setSelectionRange?.(start, start);
            const line = parseFloat(style.lineHeight) || parseFloat(style.fontSize) * 1.4 || 20;
            const padding = (parseFloat(style.paddingTop) || 0) + (parseFloat(style.paddingBottom) || 0);
            input.scrollTop = Math.max(0, before - padding - line);
        }
    };
})();
