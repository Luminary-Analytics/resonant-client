/**
 * Resonant GUI — Frontend Application
 *
 * Handles WebSocket communication, event rendering, streaming markdown,
 * tool call display, and step collapsing.
 */

// ═══════════════════════════════════════════════════════════════════
//  Tool Display Config (mirrors TUI's TOOL_DISPLAY)
// ═══════════════════════════════════════════════════════════════════

const TOOL_DISPLAY = {
    file_read:  { icon: '→', label: 'Read',  color: 'tool' },
    file_write: { icon: '←', label: 'Write', color: 'ok' },
    file_edit:  { icon: '~', label: 'Edit',  color: 'warn' },
    bash:       { icon: '$', label: 'Shell', color: 'tool' },
    glob:       { icon: '✱', label: 'Glob',  color: 'tool' },
    grep:       { icon: '/', label: 'Grep',  color: 'tool' },
    task:       { icon: '│', label: 'Task',  color: 'brand2' },
    batch:      { icon: '⚡', label: 'Batch', color: 'brand' },
    browser_navigate:   { icon: '⊕', label: 'Navigate',   color: 'brand2', category: 'browser' },
    browser_click:      { icon: '◎', label: 'Click',      color: 'brand2', category: 'browser' },
    browser_type:       { icon: '⌨', label: 'Type',       color: 'brand2', category: 'browser' },
    browser_read:       { icon: '◫', label: 'Read Page',  color: 'brand2', category: 'browser' },
    browser_screenshot: { icon: '◰', label: 'Screenshot', color: 'brand2', category: 'browser' },
    browser_js:         { icon: '⟐', label: 'JavaScript', color: 'brand2', category: 'browser' },
    computer_screenshot: { icon: '▣', label: 'Desktop Screenshot', color: 'warn', category: 'desktop' },
    computer_click:      { icon: '◎', label: 'Desktop Click',      color: 'warn', category: 'desktop' },
    computer_type:       { icon: '⌨', label: 'Desktop Type',       color: 'warn', category: 'desktop' },
    computer_scroll:     { icon: '↕', label: 'Desktop Scroll',     color: 'warn', category: 'desktop' },
};

const COLLAPSIBLE_TOOLS = new Set([
    'file_read', 'glob', 'grep', 'browser_read', 'computer_screenshot', 'browser_screenshot'
]);

const BLOCK_TOOLS = new Set(['bash', 'file_write', 'file_edit', 'browser_js']);

// v0.5.4a4 — phases that should activate the sidebar roadmap inspector.
// Originally `autonomous_running` only (v0.5.3a3); extended to include
// terminal phases so users can review the final state of completed /
// paused / failed missions without opening roadmap.md.
const _AUTONOMOUS_PHASES = new Set([
    'autonomous_running',
    'autonomous_complete',
    'autonomous_paused',
    'autonomous_failed',
]);

const MAX_OUTPUT_LINES = 5;

function getToolInfo(name) {
    return TOOL_DISPLAY[name] || { icon: '⚙', label: name, color: 'tool' };
}

/**
 * Infer a human-readable action label from a set of tool counts.
 * Returns a string like "Exploring codebase", "Editing files", etc.
 */
function inferActionLabel(toolCounts) {
    const reads = (toolCounts.file_read || 0);
    const writes = (toolCounts.file_write || 0);
    const edits = (toolCounts.file_edit || 0);
    const shells = (toolCounts.bash || 0);
    const greps = (toolCounts.grep || 0);
    const globs = (toolCounts.glob || 0);
    const tasks = (toolCounts.task || 0);
    const batches = (toolCounts.batch || 0);

    const browserKeys = ['browser_navigate', 'browser_click', 'browser_type',
                         'browser_read', 'browser_screenshot', 'browser_js'];
    const desktopKeys = ['computer_screenshot', 'computer_click',
                         'computer_type', 'computer_scroll'];
    const browserTotal = browserKeys.reduce((s, k) => s + (toolCounts[k] || 0), 0);
    const desktopTotal = desktopKeys.reduce((s, k) => s + (toolCounts[k] || 0), 0);

    const searchTotal = greps + globs;
    const writeTotal = writes + edits;
    const total = Object.values(toolCounts).reduce((s, v) => s + v, 0);

    // Browser / desktop dominant
    if (browserTotal > 0 && browserTotal >= total * 0.5) return 'Browsing';
    if (desktopTotal > 0 && desktopTotal >= total * 0.5) return 'Using desktop';

    // Task / batch dominant
    if (tasks + batches > 0 && tasks + batches >= total * 0.5) return 'Running sub-tasks';

    // Write/edit dominant
    if (writeTotal > 0 && writeTotal >= total * 0.4) return 'Editing files';
    if (writes > 0 && writes >= edits) return 'Writing files';

    // Shell dominant
    if (shells > 0 && shells >= total * 0.5) return 'Running commands';

    // Search dominant
    if (searchTotal > 0 && searchTotal >= total * 0.4) return 'Searching codebase';

    // Read dominant (exploration)
    if (reads > 0 && reads + searchTotal >= total * 0.5) return 'Exploring codebase';

    // Mixed reads + writes → implementing
    if (reads > 0 && writeTotal > 0) return 'Implementing changes';

    // Fallback
    if (total === 0) return 'Working...';
    return 'Processing';
}


// ═══════════════════════════════════════════════════════════════════
//  Resonant App Class
// ═══════════════════════════════════════════════════════════════════

class ResonantApp {
    constructor() {
        this.ws = null;
        this.reconnectAttempts = 0;
        this.isRunning = false;
        this.planMode = false;

        // Streaming state
        this.streamBuffer = '';
        this.isStreaming = false;
        this.currentMessageEl = null;
        this._renderTimer = null;          // setTimeout handle for throttled re-parse
        this._lastStreamParseAt = 0;       // perf timestamp of last successful parse
        this._userScrolledUp = false;      // true → don't auto-scroll, show "↓ new" pill

        // @-file fuzzy autocomplete state. Files are fetched once per session
        // (lazy-loaded on first @ trigger) and filtered client-side.
        this._fuzzyFiles = null;           // string[] of project-relative paths, or null = unloaded
        this._fuzzyFilesLoading = false;
        this._fuzzyFilesPending = false;   // true → reopen popup once load completes
        this._fuzzyOpen = false;
        this._fuzzyAtPos = -1;             // index of the `@` that triggered the popup
        this._fuzzyQuery = '';
        this._fuzzyMatches = [];
        this._fuzzyIdx = 0;
        this._fuzzyPopupEl = null;

        // Step collapsing state
        this.currentStepEvent = null;
        this.stepToolCalls = [];
        this.stepToolResults = [];
        this.stepIsInlineOnly = true;
        this.stepRendered = false;
        this.collapsedGroup = [];
        this._liveCollapsedGroup = null;  // live-rendering DOM tracker for inline-only streaks
        this.lastModel = '';
        this.lastStats = null;

        // Per-turn aggregate — replaces the per-step footers (model · tokens ·
        // elapsed). Stats accumulate across step.end events; we render one
        // dim line below the assistant's prose at session.end.
        this._currentTurn = this._freshTurnAggregate();
        // Map of in-flight block tool call_id → DOM row, so tool.result can
        // update the same row in place rather than rendering a separate one.
        this._blockToolRows = new Map();

        // CLI backend tool activity group
        this.handlesTools = false;
        this.isReplaying = false;
        this.activeToolGroup = null;      // current DOM container
        this.activeToolGroupCount = 0;
        this.activeToolGroupCounts = {};  // {name: count}

        this.sessionRole = 'generator';     // active session role for new sessions
        this.currentSessionRole = 'generator'; // loaded session's role

        // Terminal tracking — active bash/tool executions
        this.activeTerminals = new Map(); // call_id → {name, command, startTime}
        this._terminalTimer = null;

        // Attached images for multimodal input
        this.attachedImages = [];

        // Subagent nesting
        this.subagentDepth = 0;
        this.subagentContainer = null;

        // Preview panel state
        this.previewOpen = false;
        this.previewImages = []; // {src, toolName, timestamp}
        this._currentPreviewPane = '';  // C2 — drives the plan-tab unread indicator
        this._previewResizing = false;

        // Session management state
        this.sessions = [];
        this.allSessions = [];
        this.currentSessionId = '';
        this.recentProjects = [];
        this.harnessState = null;
        this.harnessCycles = [];
        this.harnessCyclePoller = null;

        // View state
        this.currentView = 'agents';
        this.settings = {};

        // Per-turn agent run summary (Cursor-style card on session.end)
        this._agentRunSummary = { title: '', fileChanges: [], todos: null };
        this._liveAgentTodoEl = null;

        // Git state
        this.gitData = null;
        this.gitPopoverOpen = false;
        this.harnessPopoverOpen = false;

        // RESONANT.md state
        this.resonantMd = null;

        // DOM refs
        this.chatMessages = document.getElementById('chat-messages');
        this.chatContainer = document.getElementById('chat-container');
        this.welcomeScreen = document.getElementById('welcome-screen');
        this.inputBar = document.getElementById('input-bar');
        this.userInput = document.getElementById('user-input');
        this.sendBtn = document.getElementById('send-btn');
        this.stopBtn = document.getElementById('stop-btn');
        this.modelSelector = document.getElementById('model-selector');
        this.thinkingModeSelector = document.getElementById('thinking-mode-selector');
        this.headerStatus = document.getElementById('header-status');
        this.headerProject = document.getElementById('header-project');
        // Sidebar project switcher pill — opens the same dropdown the titlebar used to
        // and additionally filters the sidebar session tree to the selected project.
        this.sidebarProjectSwitch = document.getElementById('sidebar-project-switch');
        this.sidebarProjectSwitchLabel = document.getElementById('sidebar-project-switch-label');
        this.sidebarProjectSwitch?.addEventListener('click', (ev) => {
            ev.stopPropagation();
            this._openProjectSwitcher(this.sidebarProjectSwitch);
        });
        this.sidebarCwd = document.getElementById('sidebar-cwd');
        this.sidebarProjectName = document.getElementById('sidebar-project-name');
        this.sessionList = document.getElementById('agent-list');
        this.agentPanel = document.getElementById('agent-panel');
        this.chatScrollEndBtn = document.getElementById('chat-scroll-end');
        this.tokenInfo = document.getElementById('token-info');

        // Terminal bar DOM refs
        this.terminalBar = document.getElementById('terminal-bar');
        this.terminalBarLabel = document.getElementById('terminal-bar-label');
        this.terminalBarList = document.getElementById('terminal-bar-list');
        this.terminalBarToggle = document.getElementById('terminal-bar-toggle');
        this.terminalStopAll = document.getElementById('terminal-stop-all');

        // Preview panel DOM refs
        this.previewPanel = document.getElementById('preview-panel');
        this.previewViewport = document.getElementById('preview-viewport');
        this.previewToggle = document.getElementById('preview-toggle');
        this.previewResize = document.getElementById('preview-resize');
        this.previewClose = document.getElementById('preview-close');
        this.previewUrlText = document.getElementById('preview-url-text');
        this.previewTabName = document.getElementById('preview-tab-name');
        this.previewConsoleBody = document.getElementById('preview-console-body');
        this.previewCurrentIndex = -1; // current screenshot index for back/forward

        // Feature view refs
        this.settingsView = document.getElementById('settings-view');
        this.settingsBody = document.getElementById('settings-body');

        // Header indicator refs
        this.gitBadge = document.getElementById('git-badge');
        this.gitBranchName = document.getElementById('git-branch-name');
        this.gitChangesCount = document.getElementById('git-changes-count');
        this.harnessBadge = document.getElementById('harness-badge');
        this.harnessBadgeText = document.getElementById('harness-badge-text');
        this.resonantMdBadge = document.getElementById('resonant-md-badge');

        // Configure marked
        if (typeof marked !== 'undefined') {
            marked.setOptions({
                gfm: true,
                breaks: true,
                highlight: function(code, lang) {
                    if (typeof hljs !== 'undefined' && lang && hljs.getLanguage(lang)) {
                        return hljs.highlight(code, { language: lang }).value;
                    }
                    return code;
                }
            });
        }

        this.bindEvents();
        this._restoreAppearance();
        this._bindMenuBar();
        this.showSessionSkeletons();
        this.connect();
    }

    // ── WebSocket ───────────────────────────────────────────────

    connect() {
        const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
        this.ws = new WebSocket(`${protocol}//${location.host}/ws`);

        this.ws.onopen = () => {
            this.reconnectAttempts = 0;
            this.headerStatus.textContent = 'Connected';
            this.ws.send(JSON.stringify({ command: 'init' }));
        };

        this.ws.onmessage = (e) => {
            try {
                this.handleEvent(JSON.parse(e.data));
            } catch (err) {
                console.error('Event parse error:', err, e.data);
            }
        };

        this.ws.onclose = () => {
            // Show "Reconnecting..." during retry attempts to avoid flickering
            if (this.reconnectAttempts < 10) {
                this.headerStatus.textContent = 'Reconnecting...';
            } else {
                this.headerStatus.textContent = 'Disconnected';
            }
            this.scheduleReconnect();
        };

        this.ws.onerror = (err) => {
            console.error('WebSocket error:', err);
        };
    }

    scheduleReconnect() {
        if (this.reconnectAttempts < 10) {
            const delay = Math.min(1000 * Math.pow(2, this.reconnectAttempts), 10000);
            this.reconnectAttempts++;
            setTimeout(() => this.connect(), delay);
        } else {
            this.headerStatus.textContent = 'Disconnected';
        }
    }

    send(data) {
        if (this.ws && this.ws.readyState === WebSocket.OPEN) {
            this.ws.send(JSON.stringify(data));
        }
    }

    // ── Event Binding ───────────────────────────────────────────

    bindEvents() {
        // Send message
        this.sendBtn.addEventListener('click', () => this.sendMessage());

        // Mission toggle in chat header — opens the composer.
        const missionToggle = document.getElementById('mission-toggle');
        if (missionToggle) {
            missionToggle.addEventListener('click', () => this.openMissionComposer());
        }
        this.userInput.addEventListener('keydown', (e) => {
            // Fuzzy file picker hijacks navigation/select keys when open so
            // it can act like a real autocomplete instead of moving the
            // textarea cursor. Order matters: handle this BEFORE the Enter
            // → sendMessage shortcut so picking with Enter doesn't fire send.
            if (this._fuzzyOpen && this._handleFuzzyKeydown(e)) {
                return;
            }
            if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault();
                this.sendMessage();
            }
        });

        // Auto-resize textarea + drive the @-file fuzzy popup off the same
        // input event (avoids needing a second listener that could race).
        this.userInput.addEventListener('input', () => {
            this.userInput.style.height = 'auto';
            this.userInput.style.height = Math.min(this.userInput.scrollHeight, 200) + 'px';
            this._updateFileFuzzyState();
        });
        // Caret moves on click/arrow without firing 'input' — keep the popup
        // in sync so it closes when the user navigates away from the @-token.
        this.userInput.addEventListener('keyup', (e) => {
            if (e.key.startsWith('Arrow') || e.key === 'Home' || e.key === 'End') {
                this._updateFileFuzzyState();
            }
        });
        this.userInput.addEventListener('blur', () => {
            // Close on blur, but with a tiny delay so clicking a popup item
            // doesn't get cancelled by the focus loss.
            setTimeout(() => this._closeFileFuzzy(), 120);
        });

        if (this.chatContainer && this.chatScrollEndBtn) {
            this.chatContainer.addEventListener('scroll', () => {
                // Track whether the user has manually scrolled up to read older
                // content. While scrolled up, scrollToBottom() is a no-op so
                // we don't yank them away from what they're reading; the
                // existing scroll-end pill becomes the "↓ new messages" cue.
                this._userScrolledUp = !this._isAtBottom();
                if (!this._userScrolledUp) this._clearScrollEndPillNew();
                this._syncChatScrollEndBtn();
            }, { passive: true });
            // Click the pill → force scroll to bottom regardless of state.
            this.chatScrollEndBtn.addEventListener('click', () => this.forceScrollToBottom());
        }

        // Stop button — cancel current turn. If the user is in the
        // drafting phase of a Mission, a Cancel mid-question is usually a
        // signal of "I want out of this mission", not just "retry". Show
        // a small inline prompt right after cancel, asking whether they
        // want to exit the mission entirely (A1 fix).
        this.stopBtn.addEventListener('click', () => {
            this.send({ command: 'cancel' });
            this._maybeOfferMissionExitOnCancel();
        });

        // Terminal bar — header click toggles expand/collapse
        document.getElementById('terminal-bar-header').addEventListener('click', (e) => {
            // Don't toggle if clicking the stop-all button
            if (e.target.closest('.terminal-bar-stop')) return;
            this.terminalBar.classList.toggle('expanded');
        });

        // Terminal bar — stop all
        this.terminalStopAll.addEventListener('click', (e) => {
            e.stopPropagation();
            this.send({ command: 'cancel' });
        });

        // Terminal bar — event delegation for individual stop buttons
        this.terminalBarList.addEventListener('click', (e) => {
            const stopBtn = e.target.closest('.terminal-entry-stop');
            if (stopBtn) {
                e.stopPropagation();
                this.send({ command: 'cancel' });
            }
        });

        // Model selector — value is "backend:model" (model may contain colons like "nemotron:cloud")
        this.modelSelector.addEventListener('change', () => {
            const val = this.modelSelector.value;
            const idx = val.indexOf(':');
            if (idx > 0) {
                const backend = val.substring(0, idx);
                const model = val.substring(idx + 1);
                this.send({ command: 'switch_model', backend, model });
            }
            this._refreshThinkingModeVisibility();
        });

        // Voice input (push-to-talk via Web Speech API)
        this._setupVoiceInput();

        // "Plan this" button — sends the current input as an intent.
        document.getElementById('plan-this-btn')?.addEventListener('click', () => {
            const text = this.userInput.value.trim();
            if (!text) {
                this.showStatusMessage('Type a goal first, then click Plan this.');
                this.userInput.focus();
                return;
            }
            this.startIntent(text);
            this.userInput.value = '';
            this.userInput.style.height = 'auto';
        });

        // Thinking-mode selector (deepseek-v* only)
        if (this.thinkingModeSelector) {
            this.thinkingModeSelector.addEventListener('change', () => {
                const mode = this.thinkingModeSelector.value || '';
                if (mode && !confirm('Changing thinking mode reloads the model (~30–90s on large MoE models). Continue?')) {
                    // Revert visually
                    this.thinkingModeSelector.value = this._lastThinkingMode || '';
                    return;
                }
                this._lastThinkingMode = mode;
                this.send({ command: 'set_thinking_mode', mode });
            });
        }

        // Permission dropdown
        this.permissionMode = 'bypass'; // default: bypass permissions
        const permToggle = document.getElementById('permission-toggle');
        const permMenu = document.getElementById('permission-menu');

        permToggle.addEventListener('click', (e) => {
            e.stopPropagation();
            permMenu.classList.toggle('open');
        });

        document.addEventListener('click', (e) => {
            if (!permMenu.contains(e.target) && e.target !== permToggle) {
                permMenu.classList.remove('open');
            }
        });

        permMenu.addEventListener('click', (e) => {
            const option = e.target.closest('.perm-option');
            if (!option) return;
            const mode = option.dataset.mode;
            this.setPermissionMode(mode);
            permMenu.classList.remove('open');
        });

        // Sidebar toggle
        document.getElementById('sidebar-toggle').addEventListener('click', () => {
            document.getElementById('sidebar').classList.toggle('collapsed');
        });

        // Permission dialog
        document.getElementById('permission-allow').addEventListener('click', () => {
            this.send({ command: 'approve', approved: true });
            document.getElementById('permission-dialog').style.display = 'none';
        });
        document.getElementById('permission-deny').addEventListener('click', () => {
            this.send({ command: 'approve', approved: false });
            document.getElementById('permission-dialog').style.display = 'none';
        });

        // New session — show project picker / welcome screen
        document.getElementById('new-agent-btn').addEventListener('click', () => {
            this.showNewSessionSetup();
        });

        // Add project button (next to project filter dropdown)
        document.getElementById('pf-add-project')?.addEventListener('click', () => {
            this.send({ command: 'folder_dialog' });
        });

        // Image paste (Ctrl+V)
        this.userInput.addEventListener('paste', (e) => {
            const items = e.clipboardData?.items;
            if (!items) return;
            for (const item of items) {
                if (item.type.startsWith('image/')) {
                    e.preventDefault();
                    const file = item.getAsFile();
                    if (file) this.addImageAttachment(file);
                }
            }
        });

        // Drag and drop images
        const inputWrapper = document.querySelector('.input-wrapper');
        inputWrapper.addEventListener('dragover', (e) => {
            e.preventDefault();
            inputWrapper.classList.add('dragover');
        });
        inputWrapper.addEventListener('dragleave', () => {
            inputWrapper.classList.remove('dragover');
        });
        inputWrapper.addEventListener('drop', (e) => {
            e.preventDefault();
            inputWrapper.classList.remove('dragover');
            const files = e.dataTransfer?.files;
            if (files) {
                for (const file of files) {
                    if (file.type.startsWith('image/')) {
                        this.addImageAttachment(file);
                    }
                }
            }
        });

        // Preview panel toggle
        this.previewToggle.addEventListener('click', () => {
            this.togglePreviewPanel();
        });
        this.previewClose.addEventListener('click', () => {
            this.closePreviewPanel();
        });

        // Preview tab toggle (Browser ↔ Plan)
        document.querySelectorAll('.preview-tab[data-pane]').forEach((tab) => {
            tab.addEventListener('click', () => {
                this.switchPreviewPane(tab.dataset.pane);
            });
        });

        // Plan-graph toolbar buttons
        document.getElementById('plan-graph-pause')?.addEventListener('click', () => {
            const id = this._currentIntentId;
            if (!id) { this.showStatusMessage('No active intent to pause.'); return; }
            this.send({ command: 'intent_pause', intent_id: id });
        });
        document.getElementById('plan-graph-history')?.addEventListener('click', () => {
            const id = this._currentIntentId;
            if (!id) { this.showStatusMessage('No active intent — nothing to show history for.'); return; }
            this.send({ command: 'intent_list_snapshots', intent_id: id });
        });
        document.getElementById('plan-graph-branch')?.addEventListener('click', () => {
            this.showStatusMessage('Branch from a node by clicking it in the viz, then choosing Restore from here.');
        });

        // Wire host-app hooks for the plan-graph view: per-node Restore / Re-run.
        if (window.PlanGraphView) {
            window.PlanGraphView.onAction = (kind, nodeId) => {
                const id = this._currentIntentId;
                if (!id) return;
                if (kind === 'restore_from') {
                    this.send({ command: 'intent_list_snapshots', intent_id: id });
                    this.showStatusMessage('Pick a snapshot to restore from the history list.');
                } else if (kind === 'rerun') {
                    this.showStatusMessage('Re-run is not wired yet — restart the intent or restore a snapshot.');
                }
            };
        }

        // Preview back/forward navigation
        document.getElementById('preview-back').addEventListener('click', () => {
            this.previewNavigate(-1);
        });
        document.getElementById('preview-forward').addEventListener('click', () => {
            this.previewNavigate(1);
        });

        // Preview console tab switching
        document.querySelectorAll('.preview-console-tab').forEach(tab => {
            tab.addEventListener('click', () => {
                document.querySelectorAll('.preview-console-tab').forEach(t => t.classList.remove('active'));
                tab.classList.add('active');
                this.filterPreviewConsole(tab.dataset.filter);
            });
        });

        // Preview console search
        document.getElementById('preview-console-search').addEventListener('input', (e) => {
            this.filterPreviewConsole(
                document.querySelector('.preview-console-tab.active')?.dataset.filter || 'all',
                e.target.value
            );
        });

        // Preview panel resize handle
        this.previewResize.addEventListener('mousedown', (e) => {
            e.preventDefault();
            this._previewResizing = true;
            this.previewResize.classList.add('dragging');
            document.body.style.cursor = 'col-resize';
            document.body.style.userSelect = 'none';

            const onMove = (ev) => {
                if (!this._previewResizing) return;
                const mainEl = document.getElementById('main');
                const mainRect = mainEl.getBoundingClientRect();
                const newWidth = mainRect.right - ev.clientX;
                const minW = 280;
                const maxW = mainRect.width * 0.7;
                const clamped = Math.max(minW, Math.min(maxW, newWidth));
                this.previewPanel.style.width = clamped + 'px';
                this.previewPanel.style.minWidth = clamped + 'px';
            };

            const onUp = () => {
                this._previewResizing = false;
                this.previewResize.classList.remove('dragging');
                document.body.style.cursor = '';
                document.body.style.userSelect = '';
                document.removeEventListener('mousemove', onMove);
                document.removeEventListener('mouseup', onUp);
                if (this.previewPanel.style.width) {
                    localStorage.setItem('resonant:preview-width', this.previewPanel.style.width);
                }
            };

            document.addEventListener('mousemove', onMove);
            document.addEventListener('mouseup', onUp);
        });

        // Lightbox close on Escape
        document.addEventListener('keydown', (e) => {
            if (e.key === 'Escape') {
                const lb = document.querySelector('.lightbox-overlay');
                if (lb) lb.remove();
                const pp = document.querySelector('.project-picker-overlay');
                if (pp) pp.remove();
                // Close preview panel on Escape too
                if (this.previewOpen) this.closePreviewPanel();
            }
        });

        // Close context menu on click anywhere
        document.addEventListener('click', () => {
            document.querySelector('.agent-context-menu')?.remove();
        });

        // ── Sidebar Navigation ──
        document.querySelectorAll('.sidebar-icon-btn[data-view], .sidebar-nav-item[data-view], .sidebar-action[data-view]').forEach(item => {
            item.addEventListener('click', () => {
                this.switchView(item.dataset.view);
            });
        });

        // ── Keyboard Shortcuts ──
        document.addEventListener('keydown', (e) => {
            this._handleKeyboardShortcut(e);
        });

        // Shortcuts overlay close
        document.getElementById('shortcuts-close')?.addEventListener('click', () => {
            document.getElementById('shortcuts-overlay').style.display = 'none';
        });

        // Sidebar search → live filter
        document.getElementById('search-input')?.addEventListener('input', () => {
            this.renderFilteredSessions();
        });

        // Git badge click → popover
        this.gitBadge?.addEventListener('click', () => this.toggleGitPopover());

        // Harness badge click → popover
        this.harnessBadge?.addEventListener('click', (e) => {
            e.stopPropagation();
            this.toggleHarnessPopover();
        });

        // RESONANT.md badge click → show popover
        this.resonantMdBadge?.addEventListener('click', (e) => {
            e.stopPropagation();
            // Fetch latest content, then toggle popover
            if (this.ws && this.ws.readyState === WebSocket.OPEN) {
                this.ws.send(JSON.stringify({ command: 'get_resonant_md' }));
            }
            this.toggleResonantMdPopover();
        });

    }

    // ── Image Attachments ────────────────────────────────────────

    addImageAttachment(file) {
        const reader = new FileReader();
        reader.onload = (e) => {
            const dataUrl = e.target.result;
            // Extract base64 part
            const [header, data] = dataUrl.split(',');
            const mediaType = header.match(/:(.*?);/)?.[1] || 'image/png';

            this.attachedImages.push({ data, media_type: mediaType, dataUrl });
            this.renderAttachedImages();
        };
        reader.readAsDataURL(file);
    }

    renderAttachedImages() {
        let container = document.getElementById('attached-images');
        if (!container) {
            container = document.createElement('div');
            container.id = 'attached-images';
            container.className = 'attached-images';
            const inputWrapper = document.querySelector('.input-wrapper');
            inputWrapper.parentNode.insertBefore(container, inputWrapper.nextSibling);
        }

        container.innerHTML = '';
        if (this.attachedImages.length === 0) {
            container.style.display = 'none';
            return;
        }

        container.style.display = 'flex';
        this.attachedImages.forEach((img, idx) => {
            const el = document.createElement('div');
            el.className = 'attached-image';
            el.innerHTML = `
                <img src="${img.dataUrl}" alt="Attached image">
                <button class="remove-btn" data-idx="${idx}">&times;</button>
            `;
            el.querySelector('.remove-btn').addEventListener('click', () => {
                this.attachedImages.splice(idx, 1);
                this.renderAttachedImages();
            });
            container.appendChild(el);
        });
    }

    // ── Send Message ────────────────────────────────────────────

    sendMessage() {
        const text = this.userInput.value.trim();
        if (!text) return;

        // Pi-style shell shortcuts: `!cmd` runs the command and feeds output
        // back to the model; `!!cmd` runs and shows output without involving
        // the model. Lets you hand quick context to the agent (or just check
        // git status mid-flow) without burning a full model turn.
        // Order matters — check `!!` before `!` since strings starting with
        // `!!` also start with `!`.
        if (text.startsWith('!!') && text.length > 2) {
            const cmd = text.slice(2).trim();
            if (cmd) {
                this._runShellShortcut(cmd, /* feedToLlm= */ false);
                this.userInput.value = '';
                this.userInput.style.height = 'auto';
                return;
            }
        } else if (text.startsWith('!') && text.length > 1 && !text.startsWith('!!')) {
            const cmd = text.slice(1).trim();
            if (cmd) {
                if (this.isRunning) {
                    this.showStatusMessage('Cannot feed shell output to model while a session is running. Use !!cmd to peek without involving the model.');
                    return;
                }
                this._runShellShortcut(cmd, /* feedToLlm= */ true);
                this.userInput.value = '';
                this.userInput.style.height = 'auto';
                return;
            }
        }

        if (this.isRunning) return;

        // /plan slash-prefix routes the message to the intent flow instead of
        // a one-shot Session.run. Strip the prefix and forward.
        if (text.startsWith('/plan ')) {
            this.startIntent(text.slice('/plan '.length).trim());
            this.userInput.value = '';
            this.userInput.style.height = 'auto';
            return;
        }
        if (text === '/plan') {
            this.showStatusMessage('Type your goal after /plan, e.g. /plan add dark mode toggle.');
            return;
        }

        if (text === '/pin') {
            this.send({ command: 'pin_session' });
            this.userInput.value = '';
            this.userInput.style.height = 'auto';
            return;
        }

        // Mission entry is now a UI toggle (chat-header) — slash command
        // dropped. If a stale `/grill` muscle-memory shows up, point them
        // at the toggle so they don't get silently confused.
        if (text.startsWith('/grill') || text.startsWith('/mission')) {
            this.showStatusMessage('Missions are now started from the 🎯 toggle in the chat header.');
            return;
        }

        // Add user message to chat (with image thumbnails if attached)
        this.addUserMessage(text, this.attachedImages);

        this._resetAgentRunSummary(text);

        // Reset streaming state
        if (this._renderTimer) {
            clearTimeout(this._renderTimer);
            this._renderTimer = null;
        }
        this._lastStreamParseAt = 0;
        this.streamBuffer = '';
        this.isStreaming = false;
        this.currentMessageEl = null;
        this.currentStepEvent = null;
        this.stepToolCalls = [];
        this.stepToolResults = [];
        this.stepIsInlineOnly = true;
        this.stepRendered = false;
        this.collapsedGroup = [];
        this._liveCollapsedGroup = null;
        this._currentTurn = this._freshTurnAggregate();
        this._blockToolRows = new Map();
        this.subagentDepth = 0;
        this.subagentContainer = null;
        this.clearTerminals();
        this._removeLiveAgentTodoStrip();

        // Send to server (include images if attached)
        const msg = { command: 'message', text };
        if (this.attachedImages.length > 0) {
            msg.images = this.attachedImages.map(img => ({
                data: img.data,
                media_type: img.media_type,
            }));
        }
        this.send(msg);

        // Clear input and attachments
        this.userInput.value = '';
        this.userInput.style.height = 'auto';
        this.attachedImages = [];
        this.renderAttachedImages();
        this.setRunning(true);
    }

    /**
     * Render a list of plan-graph snapshots in a small modal so the user can
     * pick one to restore. Modal is dismissible; selecting fires intent_restore_snapshot.
     */
    _renderSnapshotList(intentId, snapshots) {
        const existing = document.getElementById('snapshot-list-modal');
        if (existing) existing.remove();
        const modal = document.createElement('div');
        modal.id = 'snapshot-list-modal';
        modal.className = 'snapshot-list-modal';
        const rows = (snapshots || []).map(s => `
            <div class="snap-row" data-ts="${s.ts_ms}">
                <span class="snap-time">${this.escapeHtml(s.ts_iso)}</span>
                <span class="snap-meta">${s.node_count} nodes</span>
                <button class="snap-restore" data-ts="${s.ts_ms}">Restore</button>
            </div>
        `).join('');
        modal.innerHTML = `
            <div class="snap-modal-card">
                <div class="snap-modal-header">
                    <span>Plan history (${snapshots.length})</span>
                    <button class="snap-modal-close" id="snap-modal-close">&times;</button>
                </div>
                <div class="snap-modal-body">${rows || '<div class="snap-empty">No snapshots yet for this intent.</div>'}</div>
            </div>`;
        document.body.appendChild(modal);
        modal.querySelector('#snap-modal-close')?.addEventListener('click', () => modal.remove());
        modal.addEventListener('click', (e) => { if (e.target === modal) modal.remove(); });
        modal.querySelectorAll('.snap-restore').forEach((btn) => {
            btn.addEventListener('click', () => {
                const ts = parseInt(btn.dataset.ts, 10);
                this.send({ command: 'intent_restore_snapshot', intent_id: intentId, ts_ms: ts });
                this.showStatusMessage('Restoring snapshot…');
                modal.remove();
            });
        });
    }

    /**
     * v0.5.6a4 — Modal text-input fallback for project-path entry when
     * the native folder picker is unavailable (browser mode, missing
     * pywebview/tkinter, locked-down kiosk, etc.). Linux-bridge field-
     * observation #3: clicking the project switcher hung indefinitely
     * in browser mode and the user had to drop into devtools and run
     * `app.send({command:'set_project',path:'...'})` by hand.
     *
     * The modal:
     *   - pre-fills with the current cwd so the common case is "tweak
     *     the trailing folder name" rather than "type the whole path"
     *   - accepts Enter to submit, Esc / backdrop-click to cancel
     *   - on submit, calls back with the trimmed path so the caller
     *     can route to selectProjectFolder / mission composer / etc.
     *
     * `consumer` is a freeform label shown in the modal header so the
     * user knows what they're picking a folder for ("Switch project",
     * "Pick mission folder", etc.). The callback receives the chosen
     * path; modal auto-closes on success.
     */
    _promptForProjectPath(consumer, onPick) {
        const existing = document.getElementById('project-path-modal');
        if (existing) existing.remove();
        const modal = document.createElement('div');
        modal.id = 'project-path-modal';
        modal.className = 'snapshot-list-modal';  // reuse existing overlay styles
        const cur = (this.currentCwd || '').replace(/\\/g, '/');
        const label = consumer || 'Switch project';
        modal.innerHTML = `
            <div class="snap-modal-card project-path-card">
                <div class="snap-modal-header">
                    <span>${this.escapeHtml(label)}</span>
                    <button class="snap-modal-close" id="project-path-close">&times;</button>
                </div>
                <div class="snap-modal-body">
                    <div class="project-path-hint">
                        Native folder picker is unavailable here. Type or
                        paste an absolute folder path:
                    </div>
                    <input id="project-path-input" type="text"
                           class="project-path-input"
                           placeholder="C:\\Repos\\my-project or /Users/me/code/my-project"
                           value="${this.escapeHtml(cur)}" />
                    <div class="project-path-actions">
                        <button class="project-path-cancel">Cancel</button>
                        <button class="project-path-open">Open</button>
                    </div>
                </div>
            </div>`;
        document.body.appendChild(modal);

        const input = modal.querySelector('#project-path-input');
        const openBtn = modal.querySelector('.project-path-open');
        const cancelBtn = modal.querySelector('.project-path-cancel');
        const closeBtn = modal.querySelector('#project-path-close');

        const close = () => modal.remove();
        const submit = () => {
            const path = (input.value || '').trim();
            if (!path) {
                input.focus();
                input.classList.add('project-path-input-error');
                return;
            }
            close();
            try { onPick(path); } catch (e) { /* swallow */ }
        };

        openBtn.addEventListener('click', submit);
        cancelBtn.addEventListener('click', close);
        closeBtn.addEventListener('click', close);
        modal.addEventListener('click', (e) => { if (e.target === modal) close(); });
        input.addEventListener('keydown', (e) => {
            if (e.key === 'Enter') submit();
            else if (e.key === 'Escape') close();
            else input.classList.remove('project-path-input-error');
        });
        // Highlight the trailing path segment so the most-likely edit
        // (typing a new folder name on the end) is one keystroke away.
        input.focus();
        const slashIdx = Math.max(cur.lastIndexOf('/'), cur.lastIndexOf('\\'));
        if (slashIdx >= 0 && slashIdx < cur.length - 1) {
            input.setSelectionRange(slashIdx + 1, cur.length);
        } else {
            input.select();
        }
    }

    /**
     * Send the user text as an intent — kicks off the orchestrator pipeline,
     * not a one-shot Session.run. The plan-graph viz auto-opens; status events
     * land in the chat as small status messages.
     */
    startIntent(text) {
        text = (text || '').trim();
        if (!text) return;
        this.send({ command: 'intent_start', text });
        this.addUserMessage('/plan ' + text);
        this.showStatusMessage('Intent dispatched — plan-graph populating in the preview panel.');
        this.openPlanTab(true);
    }

    /**
     * Start a Mission. Always creates a fresh chat session on the backend
     * (the grill phase needs a clean slate; mixing it with prior chat
     * history pollutes the interview). The new session is flagged with
     * `mission_state: {phase: "drafting", ...}` and the first assistant
     * turn streams the interviewer's first question. When the model
     * emits its `## Final spec` block, the backend fires a
     * `mission.spec_ready` event and we render a "Build this roadmap"
     * affordance beneath that assistant message.
     */
    startMission(feature, projectPath, options) {
        feature = (feature || '').trim();
        projectPath = (projectPath || '').trim();
        const autonomous = !!(options && options.autonomous);
        if (!feature) {
            this.showStatusMessage('Describe the feature or product first.');
            return;
        }
        if (this.isRunning) {
            this.showStatusMessage('A session is already running — wait for it to finish or cancel.');
            return;
        }
        // v0.3.2 — second-line double-submit guard. The composer button is
        // already disabled on click, but the Cmd+Enter handler can still
        // race that. A short-lived in-flight flag (cleared by the
        // session_cleared response or after a 6s safety timeout) catches
        // the gap.
        if (this._missionStartInflight) {
            this.showStatusMessage('Mission is already starting — give it a second.');
            return;
        }
        this._missionStartInflight = true;
        if (this._missionStartInflightTimer) clearTimeout(this._missionStartInflightTimer);
        this._missionStartInflightTimer = setTimeout(() => {
            this._missionStartInflight = false;
        }, 6000);
        if (!this.chatMessages) return;

        // Mission lives in a fresh session, so we wipe local turn state
        // before sending. The backend will follow up with a session_cleared
        // event that re-renders chat — but we want the user message to
        // appear immediately for responsive feel.
        this.chatMessages.innerHTML = '';
        this.addUserMessage(feature);
        this._resetAgentRunSummary(feature);

        if (this._renderTimer) {
            clearTimeout(this._renderTimer);
            this._renderTimer = null;
        }
        this._lastStreamParseAt = 0;
        this.streamBuffer = '';
        this.isStreaming = false;
        this.currentMessageEl = null;
        this.currentStepEvent = null;
        this.stepToolCalls = [];
        this.stepToolResults = [];
        this.stepIsInlineOnly = true;
        this.stepRendered = false;
        this.collapsedGroup = [];
        this._liveCollapsedGroup = null;
        if (this._currentTurn !== undefined) {
            this._currentTurn = this._freshTurnAggregate();
        }
        if (this._blockToolRows && this._blockToolRows.clear) {
            this._blockToolRows.clear();
        }
        this.subagentDepth = 0;
        this.subagentContainer = null;
        this.clearTerminals();
        this._removeLiveAgentTodoStrip();

        // Stash so the chat-header badge knows we're in drafting before
        // session_cleared lands and overwrites currentSessionId.
        this._pendingMissionFeature = feature;
        this._refreshMissionBadge('drafting', feature);

        // v0.3.3 — only ship project_path when it actually differs from
        // the current cwd, so re-running a mission inside an existing
        // project doesn't churn the project context.
        const payload = { command: 'mission_start', feature };
        const cur = (this.currentCwd || '').replace(/\\/g, '/').toLowerCase();
        const chosen = projectPath.replace(/\\/g, '/').toLowerCase();
        if (projectPath && chosen !== cur) {
            payload.project_path = projectPath;
        }
        // v0.5.0a7 — opt-in to the rigorous-grill / autonomous-loop
        // flow. Backend reads this on mission_start, swaps in the
        // rigorous-grill prompt, and stashes the flag in mission_state
        // so the spec card later renders the right "Build" CTA.
        if (autonomous) {
            payload.autonomous = true;
            this._pendingMissionAutonomous = true;
        } else {
            this._pendingMissionAutonomous = false;
        }
        this.send(payload);
    }

    /**
     * Backend signaled that the assistant just emitted a `## Final spec`
     * block in a drafting Mission. Render a "Build this roadmap" button
     * beneath the most-recent assistant message.
     */
    /**
     * Backend signals that the mission has advanced from one phase to
     * the next (drafting → planning_dispatched, etc.). Sync the header
     * badge accordingly. The session's mission_state is the source of
     * truth — we just mirror it.
     */
    handleMissionPhaseChanged(event) {
        const phase = (event && event.phase) || '';
        if (!phase) return;
        // Find the seed feature from the current session record so the
        // badge can keep showing the original intent text.
        const sess = this._currentSessionSummary();
        const seed = sess?.mission_state?.seed_feature || this._pendingMissionFeature || '';
        this._refreshMissionBadge(phase, seed);
    }

    handleMissionExited(event) {
        // Mirror the sessions_updated update path so the per-project
        // session list, the active-session pointer, and the chat-header
        // chrome all converge on the new (exited) state. The mission
        // session stays in the sidebar under "Missions" with its dim
        // inactive style — we don't kick the user back to a regular
        // session automatically; they can decide where to go next.
        if (event && Array.isArray(event.sessions)) {
            this.sessions = event.sessions;
        }
        // v0.3.2: backend now ships all_sessions on mission_* events too.
        // Without this update the cross-project sidebar reads from a stale
        // snapshot (mission stuck in 'drafting' even after exit).
        if (event && Array.isArray(event.all_sessions)) {
            this.allSessions = event.all_sessions;
        }
        if (event && event.current_session_id !== undefined) {
            this.currentSessionId = event.current_session_id || '';
        }
        this.renderFilteredSessions();
        this._syncMissionUI();
        this.showStatusMessage('Mission exited.');
    }

    // ── Autonomous Mission events (v0.5.0a7) ───────────────────────

    /**
     * Per-AppState live state for the active autonomous mission.
     * Cleared on autonomous_mission_complete / paused / failed.
     */
    _ensureAutonomousState(event) {
        if (!this._autonomousState) {
            this._autonomousState = {
                intentId: '',
                iterCount: 0,
                // v0.5.7a2 — track in-flight state so the header badge
                // can disambiguate "iter N (running)" from "iter N
                // completed". Linux-bridge field-observation #5: the
                // header counted the in-flight iter while the sidebar
                // inspector counted completed iters, so during iter 5
                // the header showed "iter 5" and the inspector showed
                // "iter 4" — both correct under their own definitions
                // but visually disagreeing.
                iterInFlight: false,
                startedAt: 0,
                timeBudgetSeconds: null,
                lastVerdict: 'continue',
                lastReflection: null,
                acceptanceSummary: null,
            };
        }
        if (event && event.intent_id) {
            this._autonomousState.intentId = event.intent_id;
        }
        return this._autonomousState;
    }

    handleAutonomousMissionStarted(event) {
        const s = this._ensureAutonomousState(event);
        // started_iso → epoch seconds; the daemon sends ISO + budget
        // up front, then per-iter events update iter_count + elapsed.
        const isoStr = event && event.started_iso;
        s.startedAt = isoStr
            ? Math.floor(new Date(isoStr).getTime() / 1000)
            : Math.floor(Date.now() / 1000);
        s.timeBudgetSeconds = event && typeof event.time_budget_seconds === 'number'
            ? event.time_budget_seconds
            : null;
        s.iterCount = 0;
        s.lastVerdict = 'continue';

        this._renderAutonomousBanner('start', event);
        this._refreshMissionBadge('autonomous_running', '');
        // Repaint the badge once a second while the run is live so
        // "1h 23m left" / cost stay current without waiting for the
        // next event.
        if (!this._autonomousBadgeTimer) {
            this._autonomousBadgeTimer = setInterval(
                () => this._updateAutonomousBadgeState(), 1000,
            );
        }
        // v0.5.3a3 — mission just started; sidebar inspector should
        // surface its initial state. Roadmap may not be fully written
        // yet on the first iteration — the inspector renders a
        // graceful "Bootstrapping…" placeholder if so.
        this._requestAutonomousMissionRoadmap();
        // v0.5.5a2 — new mission appeared; refresh the browser so
        // the user sees it in the list.
        this._requestAutonomousMissionsList();
    }

    handleAutonomousIterationStarted(event) {
        const s = this._ensureAutonomousState(event);
        s.iterCount = (event && event.iter_count) || s.iterCount;
        // v0.5.7a2 — flag the in-flight iter so the badge can render
        // "iter N (running)" while the inspector continues to show
        // the count of COMPLETED iters from the persisted log.
        s.iterInFlight = true;
        this._updateAutonomousBadgeState();
        this._renderIterationCard(event, /*complete=*/false);
    }

    handleAutonomousIterationComplete(event) {
        const s = this._ensureAutonomousState(event);
        s.iterCount = (event && event.iter_count) || s.iterCount;
        // v0.5.7a2 — iter shipped; badge converges with inspector.
        s.iterInFlight = false;
        this._updateAutonomousBadgeState();
        this._upgradeIterationCardToComplete(event);
        // v0.5.3a3 — REFLECT may have ticked criteria off + appended
        // an iteration log entry; sidebar inspector should re-fetch.
        this._requestAutonomousMissionRoadmap();
    }

    handleAutonomousIterationFailed(event) {
        const s = this._ensureAutonomousState(event);
        s.iterCount = (event && event.iter_count) || s.iterCount;
        // v0.5.7a2 — failed iters don't append to the iteration log
        // (no SHA to record), so the badge converges with the
        // inspector at iterCount-1, BUT the badge still shows the
        // attempted iter number (iterCount). Drop the in-flight flag
        // so the badge stops advertising "running".
        s.iterInFlight = false;
        this._updateAutonomousBadgeState();
        this._upgradeIterationCardToFailed(event);
        this._requestAutonomousMissionRoadmap();
    }

    handleAutonomousReflection(event) {
        const s = this._ensureAutonomousState(event);
        s.iterCount = (event && event.iter_count) || s.iterCount;
        s.lastVerdict = (event && event.verdict) || s.lastVerdict;
        s.acceptanceSummary = (event && event.acceptance_summary) || s.acceptanceSummary;
        s.lastReflection = event;
        // v0.5.7a2 — REFLECT runs after an iter has completed (or on
        // an empty roadmap), so by the time we see this event the
        // iter is no longer "in flight" from the user's POV.
        s.iterInFlight = false;
        this._updateAutonomousBadgeState();
        this._renderReflectionCard(event);
        // REFLECT just ran — the persisted roadmap is freshly mutated
        // (criteria flipped, items checked, log entry appended).
        this._requestAutonomousMissionRoadmap();
    }

    handleAutonomousMissionEnded(event, isComplete) {
        const s = this._ensureAutonomousState(event);
        s.iterCount = (event && event.iter_count) || s.iterCount;
        s.lastVerdict = isComplete ? 'satisfied' : (event && event.stop_reason) || 'paused';
        // v0.5.9a1 — clear the activity line so the post-run badge
        // doesn't show "running REFLECT · 8m elapsed" forever.
        s.activity = null;

        // Stop the live-update tick and dismiss the badge.
        if (this._autonomousBadgeTimer) {
            clearInterval(this._autonomousBadgeTimer);
            this._autonomousBadgeTimer = null;
        }
        const newPhase = isComplete ? 'autonomous_complete' : 'autonomous_paused';
        this._refreshMissionBadge(newPhase, '');

        this._renderAutonomousBanner(isComplete ? 'complete' : 'paused', event);
        // v0.5.4a4 — refresh inspector once more so it shows the final
        // criteria state. The session's `mission_state.phase` may not
        // have transitioned yet (depends on whether the server has
        // emitted sessions_updated); _renderAutonomousRoadmapInspector
        // re-checks the phase and either keeps showing or hides.
        this._requestAutonomousMissionRoadmap();
        // v0.5.5a2 — mission just transitioned to a terminal phase;
        // mission browser should re-render with the new phase icon.
        this._requestAutonomousMissionsList();
    }

    handleAutonomousMissionFailed(event) {
        if (this._autonomousBadgeTimer) {
            clearInterval(this._autonomousBadgeTimer);
            this._autonomousBadgeTimer = null;
        }
        this._refreshMissionBadge('autonomous_paused', '');
        this._renderAutonomousBanner('failed', event);
        this._requestAutonomousMissionRoadmap();
        this._requestAutonomousMissionsList();
    }

    /**
     * v0.5.8a2 — REFLECT emitted a decision_request and the daemon
     * is parked waiting for the user to pick an option. Render an
     * inline card in the chat with the question, options as
     * radio-button-equivalent clickable rows, an optional notes
     * textarea, and a Submit button.
     *
     * Linux-bridge field-observation #10: when a [bash] criterion
     * had a wrong path (e.g. `recipes/` vs `src-tauri/recipes/`),
     * REFLECT correctly diagnosed the mismatch but couldn't decide
     * autonomously between (a) move file or (b) update criterion.
     * Daemon went stuck. This card surfaces the decision so the
     * user can pick, and the daemon retries REFLECT with the choice
     * folded into the prompt.
     */
    handleAutonomousHumanDecisionRequired(event) {
        const request = event && event.request;
        if (!request || !request.options || !request.options.length) {
            return;
        }
        const intentId = (this._autonomousState && this._autonomousState.intentId)
            || (event && event.intent_id) || '';
        if (!intentId) {
            return;
        }
        // Track active decision card so we can dismiss / replace it
        // cleanly (e.g. if the daemon emits a SECOND request in the
        // same iter). One card at a time per mission.
        const existing = document.getElementById('autonomous-decision-card');
        if (existing) existing.remove();

        const card = document.createElement('div');
        card.id = 'autonomous-decision-card';
        card.className = 'autonomous-decision-card';
        card.dataset.intentId = intentId;

        const optsHTML = request.options.map((o, i) => `
            <label class="autonomous-decision-option" data-id="${this.escapeHtml(o.id || '')}">
                <input type="radio" name="autonomous-decision-${this.escapeHtml(intentId)}"
                       value="${this.escapeHtml(o.id || '')}"
                       ${i === 0 ? 'checked' : ''} />
                <span class="autonomous-decision-option-label">${this.escapeHtml(o.label || '')}</span>
                ${o.detail ? `<span class="autonomous-decision-option-detail">${this.escapeHtml(o.detail)}</span>` : ''}
            </label>
        `).join('');

        const contextHTML = request.context
            ? `<div class="autonomous-decision-context">${this.escapeHtml(request.context)}</div>`
            : '';

        card.innerHTML = `
            <div class="autonomous-decision-head">
                <span class="autonomous-decision-icon" aria-hidden="true">⏸</span>
                <span class="autonomous-decision-title">Mission paused — your decision needed</span>
            </div>
            <div class="autonomous-decision-question">${this.escapeHtml(request.question || '')}</div>
            ${contextHTML}
            <div class="autonomous-decision-options">${optsHTML}</div>
            <textarea class="autonomous-decision-notes"
                      placeholder="Optional notes (e.g. 'use the criterion path AND clean up the dupe')"
                      rows="2"></textarea>
            <div class="autonomous-decision-actions">
                <button type="button" class="autonomous-decision-submit">Apply choice & resume</button>
            </div>
        `;

        const submitBtn = card.querySelector('.autonomous-decision-submit');
        submitBtn.addEventListener('click', () => {
            const checked = card.querySelector('input[type=radio]:checked');
            const optionId = checked ? checked.value : '';
            if (!optionId) return;
            const notes = (card.querySelector('.autonomous-decision-notes').value || '').trim();
            this.send({
                command: 'autonomous_mission_decision',
                intent_id: intentId,
                option_id: optionId,
                response_text: notes,
            });
            // Optimistic UI: disable the controls + flip the title
            // so the user sees their click landed. The daemon's
            // `autonomous_human_decision_received` event later
            // converts the card into a chip via the receive handler.
            submitBtn.disabled = true;
            submitBtn.textContent = 'Sending…';
            card.querySelectorAll('input[type=radio]').forEach(i => { i.disabled = true; });
            card.querySelector('.autonomous-decision-notes').disabled = true;
        });

        if (this.chatMessages) {
            this.chatMessages.appendChild(card);
            this.scrollToBottom?.();
        }
    }

    /**
     * v0.5.9a2 — per-iter cost + model attribution. Fires right
     * after iter_complete / iter_failed; we attach the cost data
     * to the matching iter card's footer so each iter shows what
     * it actually cost. The breakdown surfaces v0.5.8a1's per-
     * specialist routing: pro for REFLECT, flash for IMPLEMENT
     * appears as two lines under the iter card.
     */
    handleAutonomousIterationCost(event) {
        const iter = (event && event.iter_count) || 0;
        if (!iter || !this.chatMessages) return;
        // Find the matching iter card. May be inside a v0.5.8a3
        // fold wrapper — querySelector descends through both.
        const card = this.chatMessages.querySelector(
            `.autonomous-iter-card[data-iter-count="${iter}"]`,
        );
        if (!card) return;

        // Don't double-render if a previous cost line is already
        // there (defensive; in practice each iter fires once).
        const existing = card.querySelector('.autonomous-iter-cost');
        if (existing) existing.remove();

        const tokensIn = event.tokens_in || 0;
        const tokensOut = event.tokens_out || 0;
        const totalCost = event.cost_usd || 0;
        const byModel = Array.isArray(event.by_model) ? event.by_model : [];

        const cost = document.createElement('div');
        cost.className = 'autonomous-iter-cost';
        // The total + tokens line. Always shown.
        const totalLine = `
            <div class="autonomous-iter-cost-total">
                <span class="autonomous-iter-cost-label">cost</span>
                <span class="autonomous-iter-cost-value">$${totalCost.toFixed(4)}</span>
                <span class="autonomous-iter-cost-tokens">${this._fmtTokens(tokensIn)} in / ${this._fmtTokens(tokensOut)} out</span>
            </div>
        `;
        // Per-model breakdown. Only show if 2+ models contributed
        // (multi-model = the v0.5.8a1 routing case worth surfacing).
        let breakdownLine = '';
        if (byModel.length > 1) {
            const items = byModel.map(m => `
                <span class="autonomous-iter-cost-model">
                    <code>${this.escapeHtml(m.model || '')}</code>
                    <span class="autonomous-iter-cost-model-cost">$${(m.cost_usd || 0).toFixed(4)}</span>
                </span>
            `).join('');
            breakdownLine = `<div class="autonomous-iter-cost-breakdown">${items}</div>`;
        }
        cost.innerHTML = totalLine + breakdownLine;
        card.appendChild(cost);
    }

    /**
     * v0.6.3a3 — the skill loader (v0.6.3a2) surfaced N skills into
     * this iter's planner context. Render a chip on the iter card so
     * the READ side of the self-improvement loop is visible: the
     * user sees which prior-mission skills fed this iter, and each
     * skill id is clickable → opens the v0.6.2a3 detail modal.
     *
     * Correlation: `skill_context_loaded` carries no iter_count (the
     * loader runs inside dispatch_item, a layer below the iter
     * loop). But it fires immediately after `autonomous_iteration_
     * started` created the card, so the most-recent iter card is the
     * right target.
     */
    handleSkillContextLoaded(event) {
        if (!this.chatMessages) return;
        const skillIds = Array.isArray(event && event.skill_ids) ? event.skill_ids : [];
        if (!skillIds.length) return;

        // Most-recent iter card = the one this skill context belongs to.
        const cards = this.chatMessages.querySelectorAll('.autonomous-iter-card');
        const card = cards.length ? cards[cards.length - 1] : null;
        if (!card) return;

        // Idempotent — don't double-render if the event somehow repeats.
        const existing = card.querySelector('.autonomous-iter-skills');
        if (existing) existing.remove();

        const chip = document.createElement('div');
        chip.className = 'autonomous-iter-skills';
        const label = document.createElement('span');
        label.className = 'autonomous-iter-skills-label';
        label.textContent = `🛠 ${skillIds.length} skill${skillIds.length === 1 ? '' : 's'}`;
        label.title = 'Skills surfaced into this iteration’s planner context';
        chip.appendChild(label);

        skillIds.forEach((id, idx) => {
            const pill = document.createElement('button');
            pill.type = 'button';
            pill.className = 'autonomous-iter-skill-pill';
            pill.textContent = id;
            pill.title = `View skill: ${id}`;
            pill.addEventListener('click', () => this.openSkillDetail(id));
            chip.appendChild(pill);
        });
        card.appendChild(chip);
    }

    /**
     * v0.5.9a2 — pretty-print token counts. 12,847 → "12.8k".
     */
    _fmtTokens(n) {
        n = Math.max(0, Math.floor(n || 0));
        if (n < 1000) return String(n);
        if (n < 1_000_000) return `${(n / 1000).toFixed(1)}k`;
        return `${(n / 1_000_000).toFixed(2)}M`;
    }

    /**
     * v0.5.9a1 — live daemon activity. The daemon transitions
     * through 6+ phases per iteration; this handler stashes the
     * latest phase in `_autonomousState.activity` and updates the
     * badge so the user can see "Currently: reflecting · 12s" in
     * real time. Solves the "is it stuck or just slow?" diagnostic
     * during long-running missions.
     */
    handleAutonomousActivity(event) {
        const s = this._ensureAutonomousState(event);
        // Per-phase fields the daemon emits.
        s.activity = {
            phase: (event && event.phase) || '',
            detail: (event && event.detail) || '',
            specialist: (event && event.specialist) || '',
            started_iso: (event && event.started_iso) || '',
            iter_count: (event && event.iter_count) || s.iterCount,
        };
        // Compute a client-side "started_at_epoch" so the periodic
        // badge re-paint can show "12s" elapsed without re-fetching.
        if (s.activity.started_iso) {
            const epoch = Math.floor(
                new Date(s.activity.started_iso).getTime() / 1000,
            );
            if (Number.isFinite(epoch)) {
                s.activity.started_at = epoch;
            }
        }
        this._updateAutonomousBadgeState();
    }

    /**
     * v0.5.8a2 — daemon picked up the decision and is retrying
     * REFLECT. Convert the active card into a one-line "resolved"
     * chip so the chat retains a marker of the user's choice.
     */
    handleAutonomousHumanDecisionReceived(event) {
        const card = document.getElementById('autonomous-decision-card');
        if (!card) return;
        const optionId = (event && event.option_id) || '';
        const time = new Date();
        const hh = String(time.getHours()).padStart(2, '0');
        const mm = String(time.getMinutes()).padStart(2, '0');
        const chip = document.createElement('div');
        chip.className = 'autonomous-decision-chip';
        chip.innerHTML = `
            <span class="autonomous-decision-chip-icon" aria-hidden="true">✓</span>
            <span>Decision applied at ${hh}:${mm} — option <code>${this.escapeHtml(optionId)}</code></span>
        `;
        card.replaceWith(chip);
    }

    /**
     * v0.5.3a2 — Render the "resume orphaned autonomous missions"
     * banner. Triggered by the `autonomous_orphans` WS event AND by
     * the `autonomous_orphans` field included in `init` payloads.
     *
     * An orphan is a session whose `mission_state.phase` is
     * `autonomous_running` but whose intent_id has no live daemon
     * (server restart / app crash / laptop sleep killed it). The
     * roadmap is still on disk; clicking Resume picks up where the
     * mission left off, preserving any progress already made.
     *
     * Banner is hidden when the orphan list is empty (the steady-
     * state happy path).
     */
    handleAutonomousOrphans(event) {
        const orphans = (event && Array.isArray(event.orphans)) ? event.orphans : [];
        this._autonomousOrphans = orphans;
        this._renderAutonomousOrphansBanner();
    }

    _renderAutonomousOrphansBanner() {
        const banner = document.getElementById('autonomous-orphans-banner');
        if (!banner) return;
        const orphans = this._autonomousOrphans || [];
        if (!orphans.length) {
            banner.hidden = true;
            banner.innerHTML = '';
            return;
        }
        banner.hidden = false;
        banner.innerHTML = orphans.map(o => this._renderOrphanCardHTML(o)).join('');
        // Wire click handlers per card. We build the inner HTML as
        // strings (cleaner) and attach listeners after — this is the
        // pattern the rest of the file uses for dynamically-rendered
        // session cards.
        banner.querySelectorAll('.autonomous-orphan-resume').forEach(btn => {
            btn.addEventListener('click', (e) => {
                e.preventDefault();
                const intentId = btn.dataset.intentId || '';
                const sessionId = btn.dataset.sessionId || '';
                this._handleResumeOrphanClick(intentId, sessionId, btn);
            });
        });
        banner.querySelectorAll('.autonomous-orphan-dismiss').forEach(btn => {
            btn.addEventListener('click', (e) => {
                e.preventDefault();
                const intentId = btn.dataset.intentId || '';
                this._handleDismissOrphanClick(intentId);
            });
        });
    }

    _renderOrphanCardHTML(orphan) {
        const intentId = orphan.intent_id || '';
        const sessionId = orphan.session_id || '';
        const feature = orphan.feature || '(unnamed mission)';
        const startedAt = typeof orphan.autonomous_started_at === 'number'
            ? orphan.autonomous_started_at : null;
        const ageLabel = startedAt
            ? this._formatAutonomousAge(Date.now() / 1000 - startedAt)
            : '';
        const roadmapMissing = orphan.roadmap_exists === false;
        const subtitleParts = [];
        if (ageLabel) subtitleParts.push(`Started ${ageLabel} ago`);
        if (roadmapMissing) subtitleParts.push('roadmap.md missing — resume will fail');
        const subtitle = subtitleParts.join(' · ');

        // Mark the resume button disabled if the roadmap is gone — the
        // server will reject anyway, but disabling it up front saves the
        // round trip and tells the user why.
        const resumeAttrs = roadmapMissing
            ? 'disabled aria-disabled="true" title="Roadmap file is missing — cannot resume"'
            : 'title="Resume this mission from where it stopped"';

        return `
            <div class="autonomous-orphan-card" data-intent-id="${this.escapeHtml(intentId)}">
                <span class="autonomous-orphan-icon" aria-hidden="true">∞</span>
                <div class="autonomous-orphan-text">
                    <span class="autonomous-orphan-title">${this.escapeHtml(feature)}</span>
                    ${subtitle ? `<span class="autonomous-orphan-subtitle">${this.escapeHtml(subtitle)}</span>` : ''}
                </div>
                <div class="autonomous-orphan-actions">
                    <button type="button" class="autonomous-orphan-resume"
                            data-intent-id="${this.escapeHtml(intentId)}"
                            data-session-id="${this.escapeHtml(sessionId)}"
                            ${resumeAttrs}>Resume</button>
                    <button type="button" class="autonomous-orphan-dismiss"
                            data-intent-id="${this.escapeHtml(intentId)}"
                            title="Hide this — does NOT stop the mission's session record">Dismiss</button>
                </div>
            </div>
        `;
    }

    _formatAutonomousAge(seconds) {
        if (!Number.isFinite(seconds) || seconds < 0) return '';
        if (seconds < 60) return `${Math.round(seconds)}s`;
        if (seconds < 3600) return `${Math.round(seconds / 60)}m`;
        if (seconds < 86400) return `${Math.round(seconds / 360) / 10}h`;
        return `${Math.round(seconds / 8640) / 10}d`;
    }

    _handleResumeOrphanClick(intentId, sessionId, btn) {
        if (!intentId) return;
        // Disable the buttons in this card so a double-click doesn't
        // race two resume requests through. The server rejects the
        // second one (RuntimeError "already running"), but the UX is
        // cleaner if we don't even send it.
        const card = btn ? btn.closest('.autonomous-orphan-card') : null;
        if (card) {
            card.querySelectorAll('button').forEach(b => { b.disabled = true; });
            const resumeBtn = card.querySelector('.autonomous-orphan-resume');
            if (resumeBtn) resumeBtn.textContent = 'Resuming…';
        }
        this.send({
            command: 'autonomous_mission_resume',
            intent_id: intentId,
            session_id: sessionId || undefined,
        });
    }

    _handleDismissOrphanClick(intentId) {
        // Local-only dismissal — does NOT modify the session record on
        // disk. The orphan reappears on next page reload / connect.
        // This is by design: dismiss is a "not now" action, not a
        // "permanently forget about this".
        if (!Array.isArray(this._autonomousOrphans)) return;
        this._autonomousOrphans = this._autonomousOrphans.filter(
            o => (o.intent_id || '') !== intentId
        );
        this._renderAutonomousOrphansBanner();
    }

    /**
     * v0.5.5a2 — Sidebar mission browser. Lists every autonomous
     * mission for the project (running + complete + paused + failed),
     * each row clickable to switch to that session. Hidden when no
     * autonomous missions exist (steady state for a fresh project).
     */
    handleAutonomousMissions(event) {
        const missions = (event && Array.isArray(event.missions))
            ? event.missions : [];
        this._autonomousMissions = missions;
        this._renderAutonomousMissionBrowser();
    }

    _renderAutonomousMissionBrowser() {
        const root = document.getElementById('autonomous-mission-browser');
        if (!root) return;
        const missions = this._autonomousMissions || [];
        if (!missions.length) {
            root.hidden = true;
            root.innerHTML = '';
            return;
        }
        root.hidden = false;
        const itemsHTML = missions.map(m => this._renderMissionBrowserItem(m)).join('');
        root.innerHTML = `
            <div class="amb-header">
                <span>Missions (${missions.length})</span>
            </div>
            <div class="amb-list">${itemsHTML}</div>
        `;
        root.querySelectorAll('.amb-item').forEach(btn => {
            btn.addEventListener('click', (e) => {
                e.preventDefault();
                const sessionId = btn.dataset.sessionId || '';
                if (sessionId) this._handleMissionBrowserClick(sessionId);
            });
        });
    }

    _renderMissionBrowserItem(mission) {
        const sessionId = mission.session_id || '';
        const intentId = mission.intent_id || '';
        const phase = mission.phase || '';
        const feature = mission.feature || '(unnamed mission)';
        const isCurrent = sessionId === this.currentSessionId;
        const startedAt = typeof mission.autonomous_started_at === 'number'
            ? mission.autonomous_started_at : null;
        const ageLabel = startedAt
            ? this._formatAutonomousAge(Date.now() / 1000 - startedAt)
            : '';

        // Phase icon: ∞ live, ✓ complete, ⏸ paused, ✗ failed.
        let icon, iconClass;
        switch (phase) {
            case 'autonomous_running':
                icon = '∞'; iconClass = 'amb-phase-running'; break;
            case 'autonomous_complete':
                icon = '✓'; iconClass = 'amb-phase-complete'; break;
            case 'autonomous_paused':
                icon = '⏸'; iconClass = 'amb-phase-paused'; break;
            case 'autonomous_failed':
                icon = '✗'; iconClass = 'amb-phase-failed'; break;
            default:
                icon = '·'; iconClass = '';
        }

        // The orphan flag distinguishes "running" sessions whose
        // daemon is dead — clicking still switches to the session,
        // but the user knows they need to resume.
        const orphanFlag = mission.is_orphan
            ? `<span class="amb-orphan-flag" title="No live daemon — resume from the banner">orphan</span>`
            : '';

        const currentClass = isCurrent ? ' amb-item-current' : '';
        const ageHTML = ageLabel
            ? `<span class="amb-item-age">${this.escapeHtml(ageLabel)}</span>`
            : '';

        return `
            <button type="button" class="amb-item${currentClass}"
                    data-session-id="${this.escapeHtml(sessionId)}"
                    data-intent-id="${this.escapeHtml(intentId)}"
                    title="${this.escapeHtml(feature)} (${phase})">
                <span class="amb-item-phase-icon ${iconClass}" aria-hidden="true">${icon}</span>
                <span class="amb-item-text">${this.escapeHtml(feature)}${orphanFlag}</span>
                ${ageHTML}
            </button>
        `;
    }

    _handleMissionBrowserClick(sessionId) {
        // Use the existing session-switch path so backend recreates
        // the backend + emits session_loaded (which triggers the
        // inspector refresh via _syncMissionUI). Same code path as
        // clicking a regular session in the agent list.
        if (!sessionId || sessionId === this.currentSessionId) return;
        this.send({ command: 'switch_session', session_id: sessionId });
    }

    /**
     * Convenience — request a fresh mission-browser snapshot. Used
     * when an autonomous_* event arrives that may have changed the
     * mission roster (started, ended, failed).
     */
    _requestAutonomousMissionsList() {
        this.send({ command: 'autonomous_missions_list' });
    }

    /**
     * v0.5.3a3 — Sidebar roadmap inspector. The frontend keeps the
     * latest roadmap snapshot per intent_id and re-renders the inline
     * inspector whenever the data refreshes. The backend re-parses
     * roadmap.md on every request — we never trust a cached copy
     * because REFLECT mutates the file asynchronously.
     */
    handleAutonomousMissionRoadmap(event) {
        if (!event || !event.intent_id) return;
        if (!this._autonomousRoadmaps) this._autonomousRoadmaps = {};
        this._autonomousRoadmaps[event.intent_id] = event;
        this._renderAutonomousRoadmapInspector();
    }

    /**
     * Request a fresh roadmap snapshot for the current session's
     * autonomous mission. Safe to call when no autonomous mission is
     * active (no-op). Used as a "refresh trigger" hooked into the
     * autonomous_* events that change roadmap state.
     */
    _requestAutonomousMissionRoadmap(intentId) {
        const id = intentId || this._currentAutonomousIntentId();
        if (!id) return;
        this.send({ command: 'autonomous_mission_roadmap', intent_id: id });
    }

    _currentAutonomousIntentId() {
        // v0.5.4a4 — also surface intent_id for terminal autonomous
        // phases (complete / paused / failed) so the inspector can
        // render the FINAL roadmap state, not just live-running ones.
        // The reader can revisit a finished mission and see which
        // criteria passed without opening roadmap.md.
        const sess = this._currentSessionSummary();
        const ms = sess?.mission_state || {};
        if (!_AUTONOMOUS_PHASES.has(ms.phase)) return '';
        return ms.intent_id || '';
    }

    _currentAutonomousPhase() {
        const sess = this._currentSessionSummary();
        const ms = sess?.mission_state || {};
        return _AUTONOMOUS_PHASES.has(ms.phase) ? ms.phase : '';
    }

    _renderAutonomousRoadmapInspector() {
        const inspector = document.getElementById('autonomous-roadmap-inspector');
        if (!inspector) return;
        const intentId = this._currentAutonomousIntentId();
        if (!intentId) {
            inspector.hidden = true;
            inspector.innerHTML = '';
            return;
        }
        const data = this._autonomousRoadmaps && this._autonomousRoadmaps[intentId];
        if (!data) {
            // We have an active autonomous mission but no snapshot yet —
            // fire one off and show a placeholder.
            this._requestAutonomousMissionRoadmap(intentId);
            inspector.hidden = false;
            inspector.innerHTML = `
                <div class="arm-inspector-header">
                    <span class="arm-inspector-icon" aria-hidden="true">∞</span>
                    <span class="arm-inspector-title">Loading roadmap…</span>
                </div>
            `;
            return;
        }
        if (data.roadmap_exists === false) {
            // The mission hasn't persisted its roadmap yet (very early
            // in the run, or the daemon failed before the first save).
            // Show a minimal placeholder rather than hiding entirely so
            // the user knows the mission IS active, just not yet
            // inspectable.
            inspector.hidden = false;
            inspector.innerHTML = `
                <div class="arm-inspector-header">
                    <span class="arm-inspector-icon" aria-hidden="true">∞</span>
                    <span class="arm-inspector-title">Roadmap not yet on disk</span>
                </div>
                <div class="arm-inspector-summary">
                    <span class="arm-inspector-summary-pill">Bootstrapping…</span>
                </div>
            `;
            return;
        }
        inspector.hidden = false;
        inspector.innerHTML = this._renderRoadmapInspectorHTML(data);
        // v0.5.5a4 — wire the copy-path button. Uses navigator.clipboard
        // (Promise-based; falls back to a status message on failure or
        // when running outside a secure context).
        const copyBtn = inspector.querySelector('.arm-copy-path-btn');
        if (copyBtn) {
            copyBtn.addEventListener('click', (e) => {
                e.preventDefault();
                const path = copyBtn.dataset.path || '';
                if (!path) return;
                this._copyToClipboard(path).then(ok => {
                    if (ok) {
                        const orig = copyBtn.textContent;
                        copyBtn.textContent = 'Copied!';
                        copyBtn.classList.add('arm-copy-path-btn-copied');
                        setTimeout(() => {
                            copyBtn.textContent = orig;
                            copyBtn.classList.remove('arm-copy-path-btn-copied');
                        }, 1500);
                    } else {
                        this.showStatusMessage(
                            'Could not copy to clipboard — path: ' + path
                        );
                    }
                });
            });
        }
    }

    /**
     * v0.5.5a4 — promise-based clipboard write with a graceful fallback
     * for non-secure contexts (which lack navigator.clipboard). Returns
     * a Promise<bool> indicating success.
     */
    _copyToClipboard(text) {
        if (navigator.clipboard && window.isSecureContext) {
            return navigator.clipboard.writeText(text)
                .then(() => true)
                .catch(() => false);
        }
        // Fallback: synchronous selection + execCommand. Older API but
        // still works in non-secure contexts (e.g. file:// or some
        // pywebview environments).
        try {
            const ta = document.createElement('textarea');
            ta.value = text;
            ta.style.position = 'fixed';
            ta.style.opacity = '0';
            document.body.appendChild(ta);
            ta.select();
            const ok = document.execCommand('copy');
            document.body.removeChild(ta);
            return Promise.resolve(ok);
        } catch (_e) {
            return Promise.resolve(false);
        }
    }

    _renderRoadmapInspectorHTML(data) {
        const feature = data.feature || '(unnamed mission)';
        const summary = data.acceptance_summary || {};
        const passed = summary.passed || 0;
        const total = summary.total_blocking || 0;
        const isConverged = data.is_converged === true;
        const iterCount = typeof data.iteration_count === 'number'
            ? data.iteration_count : 0;
        // v0.5.4a4 — terminal phase markers. Rendered as a small badge
        // next to the feature title so the user can tell at a glance
        // whether they're looking at a live mission or a finished one.
        const phase = this._currentAutonomousPhase();
        const phaseBadge = this._renderInspectorPhaseBadge(phase);
        const isTerminal = phase && phase !== 'autonomous_running';

        const summaryPill = isConverged
            ? `<span class="arm-inspector-summary-pill arm-pill-converged">${passed}/${total} met · converged</span>`
            : `<span class="arm-inspector-summary-pill">${passed}/${total} criteria met</span>`;

        const criteria = Array.isArray(summary.criteria) ? summary.criteria : [];
        const criteriaHTML = criteria.map(c => {
            let statusIcon, statusClass;
            if (c.is_blocking === false) {
                // Manual criterion — advisory, not a gate.
                statusIcon = '○';
                statusClass = 'arm-status-manual';
            } else if (c.passed === true) {
                statusIcon = '✓';
                statusClass = 'arm-status-pass';
            } else if (c.passed === false) {
                statusIcon = '✗';
                statusClass = 'arm-status-fail';
            } else {
                statusIcon = '·';
                statusClass = 'arm-status-pending';
            }
            return `
                <li class="arm-criterion">
                    <span class="arm-criterion-status ${statusClass}">${statusIcon}</span>
                    <span class="arm-criterion-text">
                        <span class="arm-criterion-type">[${this.escapeHtml(c.type || '')}]</span>${this.escapeHtml(c.text || '')}
                    </span>
                </li>
            `;
        }).join('');

        const nextItem = data.next_item;
        // v0.5.4a4 — hide "Next item" for terminal-phase missions.
        // Showing the unchecked item that the user "should do next"
        // is misleading when the mission is paused / failed / done;
        // there's no "next" — the daemon stopped.
        const nextItemHTML = (nextItem && !isTerminal)
            ? `
                <div class="arm-next-item">
                    <span class="arm-next-item-label">Next: ${this.escapeHtml(nextItem.id || '')}</span>
                    <span>${this.escapeHtml(nextItem.title || '')}</span>
                </div>
            `
            : '';

        const reflection = (data.reflection_summary || '').trim();
        const reflectionHTML = reflection
            ? `
                <div class="arm-reflection">
                    <span class="arm-reflection-label">Latest reflection</span>
                    ${this.escapeHtml(reflection)}
                </div>
            `
            : '';

        // v0.5.5a4 — timing line for terminal-phase missions. Live
        // missions hide it (the chat-header autonomous badge already
        // shows live elapsed); terminal missions get "Ran for 3m 12s"
        // since they're frozen. Also a copy-path footer that lets the
        // user paste the roadmap.md path into their editor.
        const elapsedSeconds = (typeof data.elapsed_seconds === 'number')
            ? data.elapsed_seconds : null;
        const timingHTML = (isTerminal && elapsedSeconds !== null)
            ? `
                <div class="arm-timing">
                    <span class="arm-timing-label">Ran for</span>
                    <span class="arm-timing-value">${this.escapeHtml(this._formatAutonomousAge(elapsedSeconds))}</span>
                </div>
            `
            : '';

        const roadmapPath = data.roadmap_path || '';
        const footerHTML = roadmapPath
            ? `
                <div class="arm-footer">
                    <button type="button" class="arm-copy-path-btn"
                            data-path="${this.escapeHtml(roadmapPath)}"
                            title="${this.escapeHtml(roadmapPath)}">
                        Copy roadmap.md path
                    </button>
                </div>
            `
            : '';

        return `
            <div class="arm-inspector-header">
                <span class="arm-inspector-icon" aria-hidden="true">∞</span>
                <span class="arm-inspector-title" title="${this.escapeHtml(feature)}">${this.escapeHtml(feature)}</span>
                ${phaseBadge}
            </div>
            <div class="arm-inspector-summary">
                ${summaryPill}
                <span title="Iterations recorded in roadmap.md (each entry = one shipped sub-mission). The chat-header badge counts the in-flight iter as well, so during a running iter the header may show one ahead.">iter ${iterCount} completed</span>
            </div>
            ${criteriaHTML ? `<ul class="arm-criteria-list">${criteriaHTML}</ul>` : ''}
            ${nextItemHTML}
            ${timingHTML}
            ${reflectionHTML}
            ${footerHTML}
        `;
    }

    _renderInspectorPhaseBadge(phase) {
        // v0.5.4a4 — small status pill that sits next to the feature
        // title. Live (autonomous_running) gets no badge — the inspector
        // being visible is signal enough. Terminal phases get a colored
        // badge so the user instantly sees the final state.
        switch (phase) {
            case 'autonomous_complete':
                return `<span class="arm-phase-badge arm-phase-complete" title="Mission converged">complete</span>`;
            case 'autonomous_paused':
                return `<span class="arm-phase-badge arm-phase-paused" title="Mission paused (user stop / budget / stuck)">paused</span>`;
            case 'autonomous_failed':
                return `<span class="arm-phase-badge arm-phase-failed" title="Mission ended in failure">failed</span>`;
            default:
                return '';
        }
    }

    /**
     * Render an iteration card in the chat panel. Stays in
     * "in-progress" state (spinner + "iter N — picked T1.x") until
     * the matching `iteration_complete` / `_failed` event upgrades
     * the trailing label.
     */
    _renderIterationCard(event, _complete) {
        if (!this.chatMessages) return;
        const iter = (event && event.iter_count) || 0;
        const itemId = (event && event.item_id) || '';
        const itemTitle = (event && event.item_title) || '';

        const card = document.createElement('div');
        card.className = 'autonomous-iter-card autonomous-iter-card-running';
        card.dataset.iterCount = String(iter);
        card.innerHTML = `
            <div class="autonomous-iter-head">
                <span class="autonomous-iter-icon" aria-hidden="true">∞</span>
                <span class="autonomous-iter-label">Iteration ${iter}</span>
                <span class="autonomous-iter-spinner" aria-hidden="true"></span>
            </div>
            <div class="autonomous-iter-body">
                <span class="autonomous-iter-action">picked</span>
                <code class="autonomous-iter-item-id">${this.escapeHtml(itemId)}</code>:
                <span class="autonomous-iter-item-title">${this.escapeHtml(itemTitle)}</span>
            </div>
            <div class="autonomous-iter-footer" data-final="">
                <span class="autonomous-iter-status">in flight…</span>
            </div>
        `;
        this.chatMessages.appendChild(card);
        this.scrollToBottom();

        // v0.5.8a3 — chat virtualization for long-running missions.
        // When a new iter starts, fold any iter older than
        // `currentIter - AUTONOMOUS_KEEP_RECENT_ITERS` into a one-line
        // collapsible affordance. Linux-bridge field-observation #7:
        // ~2h of accumulated chat (388 messages) was timing out
        // Chrome's screenshot capture; the DOM had grown too large
        // for layout. Folding past iters shrinks the live render
        // tree to ~3 expanded iters at any time without losing
        // information (clicking the affordance restores visibility).
        try {
            this._foldOlderIterationsIfNeeded(iter);
        } catch (e) {
            console.warn('iter virtualization raised', e);
        }
    }

    /**
     * v0.5.8a3 — collapse older iter blocks into one-line affordances.
     *
     * Strategy: each iter's block in the DOM is bounded by the iter
     * card with `data-iter-count=N` on the leading edge and the next
     * iter card (count=N+1) on the trailing edge. We wrap that block
     * in a `<details>` element with a one-line summary; the body
     * stays in the DOM but is hidden until the user clicks to expand.
     * `<details>` is the right primitive: native browser folding,
     * keyboard-accessible, no JS state to manage.
     *
     * Triggers when the latest iter's count exceeds the keep-recent
     * window. Idempotent — re-running on already-folded iters is a
     * no-op (we check for `data-folded`).
     */
    _foldOlderIterationsIfNeeded(latestIter) {
        const KEEP_RECENT = 2;  // 2 most recent iters + live = 3 expanded
        const cutoff = latestIter - KEEP_RECENT;
        if (cutoff < 1) return;
        if (!this.chatMessages) return;

        // Find every iter card whose count is at-or-below cutoff and
        // hasn't been folded yet.
        const cards = this.chatMessages.querySelectorAll(
            '.autonomous-iter-card:not([data-folded="1"])',
        );
        for (const card of cards) {
            const n = parseInt(card.dataset.iterCount || '0', 10);
            if (!Number.isFinite(n) || n < 1 || n > cutoff) continue;
            this._foldSingleIterBlock(card, n);
        }
    }

    _foldSingleIterBlock(card, iterCount) {
        // Scan forward from `card` to either the next iter card OR
        // the end of chatMessages. Collect those siblings into the
        // fold block. The card itself goes inside the fold so the
        // entire visual block becomes the affordance.
        const nodes = [];
        let cursor = card;
        while (cursor) {
            const next = cursor.nextElementSibling;
            nodes.push(cursor);
            // Stop when we hit the next iter card.
            if (next && next.classList && next.classList.contains('autonomous-iter-card')) {
                break;
            }
            cursor = next;
        }
        if (!nodes.length) return;

        // Build the <details> wrapper. The summary line surfaces the
        // most useful one-line digest: iter number, item id, status.
        const status = card.classList.contains('autonomous-iter-card-failed')
            ? 'failed'
            : card.classList.contains('autonomous-iter-card-complete')
                ? 'shipped'
                : 'in flight';
        const itemIdEl = card.querySelector('.autonomous-iter-item-id');
        const itemId = itemIdEl ? itemIdEl.textContent : '';
        const durEl = card.querySelector('.autonomous-iter-duration');
        const dur = durEl ? durEl.textContent : '';

        const wrap = document.createElement('details');
        wrap.className = 'autonomous-iter-fold';
        const summary = document.createElement('summary');
        summary.className = 'autonomous-iter-fold-summary';
        summary.innerHTML = `
            <span class="autonomous-iter-fold-icon" aria-hidden="true">▸</span>
            <span class="autonomous-iter-fold-label">Iter ${iterCount}</span>
            ${itemId ? `<code class="autonomous-iter-fold-itemid">${this.escapeHtml(itemId)}</code>` : ''}
            <span class="autonomous-iter-fold-status autonomous-iter-fold-status-${status.replace(/\s+/g, '-')}">${this.escapeHtml(status)}</span>
            ${dur ? `<span class="autonomous-iter-fold-dur">${this.escapeHtml(dur)}</span>` : ''}
            <span class="autonomous-iter-fold-hint">click to expand</span>
        `;
        wrap.appendChild(summary);

        // Mark the card as folded BEFORE moving so subsequent calls
        // skip it. The dataset attr survives the move.
        card.dataset.folded = '1';

        // Insert the wrapper in the card's slot, then move all the
        // collected nodes inside it.
        card.parentNode.insertBefore(wrap, card);
        for (const node of nodes) {
            wrap.appendChild(node);
        }
    }

    _upgradeIterationCardToComplete(event) {
        const iter = (event && event.iter_count) || 0;
        const card = this.chatMessages.querySelector(
            `.autonomous-iter-card[data-iter-count="${iter}"]`,
        );
        if (!card) return;
        card.classList.remove('autonomous-iter-card-running');
        card.classList.add('autonomous-iter-card-complete');
        const sha = (event && event.commit_sha) || '';
        const dur = (event && event.duration_seconds) || 0;
        const footer = card.querySelector('.autonomous-iter-footer');
        if (footer) {
            footer.dataset.final = '1';
            const shaPart = sha
                ? `shipped at <code>${this.escapeHtml(sha.slice(0, 7))}</code>`
                : 'shipped <em>&lt;no commit recorded&gt;</em>';
            footer.innerHTML = `
                <span class="autonomous-iter-status autonomous-iter-status-ok">✓</span>
                ${shaPart}
                <span class="autonomous-iter-duration">${this._fmtDuration(dur)}</span>
            `;
        }
        const spinner = card.querySelector('.autonomous-iter-spinner');
        if (spinner) spinner.remove();
    }

    _upgradeIterationCardToFailed(event) {
        const iter = (event && event.iter_count) || 0;
        const card = this.chatMessages.querySelector(
            `.autonomous-iter-card[data-iter-count="${iter}"]`,
        );
        if (!card) return;
        card.classList.remove('autonomous-iter-card-running');
        card.classList.add('autonomous-iter-card-failed');
        const err = (event && event.error) || '(no error message)';
        const footer = card.querySelector('.autonomous-iter-footer');
        if (footer) {
            footer.dataset.final = '1';
            footer.innerHTML = `
                <span class="autonomous-iter-status autonomous-iter-status-fail">✗</span>
                <span class="autonomous-iter-error">${this.escapeHtml(err)}</span>
            `;
        }
        const spinner = card.querySelector('.autonomous-iter-spinner');
        if (spinner) spinner.remove();
    }

    /**
     * Render a reflection card from a full-pass result. Shows
     * verdict, acceptance summary (X/Y), bash/vision/chrome tally,
     * added items, blocked items, manual pending, model summary.
     */
    _renderReflectionCard(event) {
        if (!this.chatMessages) return;
        const verdict = (event && event.verdict) || 'continue';
        const modelVerdict = (event && event.model_verdict) || verdict;
        const overridden = !!(event && event.verdict_overridden);
        const overrideReason = (event && event.override_reason) || '';
        const unpassed = (event && event.unpassed_criteria) || [];
        const summary = (event && event.summary) || '';
        const accept = (event && event.acceptance_summary) || { passed: 0, total: 0 };
        const tally = (event && event.pass_tally) || {};
        const added = (event && event.added) || [];
        const blocked = (event && event.blocked) || [];
        const manual = (event && event.manual_pending) || [];
        const iter = (event && event.iter_count) || 0;

        const verdictClass = `autonomous-reflect-verdict-${verdict}`;
        const tallyParts = [];
        if (typeof tally.bash_passed === 'number') {
            tallyParts.push(`bash ${tally.bash_passed} pass / ${tally.bash_failed || 0} fail`);
        }
        if (typeof tally.vision_passed === 'number' && (tally.vision_passed + (tally.vision_failed || 0)) > 0) {
            tallyParts.push(`vision ${tally.vision_passed} pass / ${tally.vision_failed || 0} fail`);
        }
        if (typeof tally.chrome_pending === 'number' && tally.chrome_pending > 0) {
            tallyParts.push(`chrome ${tally.chrome_pending} pending`);
        }

        const addedHTML = added.length ? `
            <div class="autonomous-reflect-section">
                <div class="autonomous-reflect-section-title">Added items</div>
                <ul class="autonomous-reflect-list">
                    ${added.map((it) => `<li><strong>T${this.escapeHtml(it.tier || '?')}</strong> ${this.escapeHtml(it.title || '')}</li>`).join('')}
                </ul>
            </div>` : '';

        const blockedHTML = blocked.length ? `
            <div class="autonomous-reflect-section">
                <div class="autonomous-reflect-section-title">Blocked</div>
                <ul class="autonomous-reflect-list">
                    ${blocked.map((it) => `<li><code>${this.escapeHtml(it.id || '?')}</code> — ${this.escapeHtml(it.reason || '')}</li>`).join('')}
                </ul>
            </div>` : '';

        const manualHTML = manual.length ? `
            <div class="autonomous-reflect-section">
                <div class="autonomous-reflect-section-title">Manual verification (handoff)</div>
                <ul class="autonomous-reflect-list">
                    ${manual.map((m) => `<li>${this.escapeHtml(m)}</li>`).join('')}
                </ul>
            </div>` : '';

        // v0.5.9a3 — verdict-override provenance. When the daemon
        // downgraded `satisfied` → `continue` because the roadmap
        // didn't actually agree, surface that as a structured
        // "model said X / daemon said Y" badge with the unpassed
        // criteria list. The user shouldn't have to parse the
        // summary prose to figure out why the model's claim got
        // overridden.
        const overrideHTML = overridden ? `
            <div class="autonomous-reflect-override">
                <div class="autonomous-reflect-override-head">
                    <span class="autonomous-reflect-override-icon" aria-hidden="true">!</span>
                    <span class="autonomous-reflect-override-title">Daemon override</span>
                </div>
                <div class="autonomous-reflect-override-line">
                    Model said <code>${this.escapeHtml(modelVerdict)}</code> · daemon downgraded to <code>${this.escapeHtml(verdict)}</code>
                </div>
                ${overrideReason ? `<div class="autonomous-reflect-override-reason">${this.escapeHtml(overrideReason)}</div>` : ''}
                ${unpassed.length ? `
                    <div class="autonomous-reflect-override-criteria">
                        <div class="autonomous-reflect-override-criteria-title">Unpassed criteria:</div>
                        <ul>${unpassed.map(c => `<li>${this.escapeHtml(c)}</li>`).join('')}</ul>
                    </div>
                ` : ''}
            </div>` : '';

        const card = document.createElement('div');
        card.className = `autonomous-reflect-card ${verdictClass}`;
        if (overridden) card.classList.add('autonomous-reflect-card-overridden');
        card.innerHTML = `
            <div class="autonomous-reflect-head">
                <span class="autonomous-reflect-icon" aria-hidden="true">∞</span>
                <span class="autonomous-reflect-label">Reflection · iter ${iter}</span>
                <span class="autonomous-reflect-verdict">${this.escapeHtml(verdict)}</span>
            </div>
            <div class="autonomous-reflect-body">
                ${overrideHTML}
                <div class="autonomous-reflect-acceptance">
                    Acceptance: <strong>${accept.passed}/${accept.total}</strong> blocking criteria passed
                </div>
                ${tallyParts.length ? `<div class="autonomous-reflect-tally">${tallyParts.join(' · ')}</div>` : ''}
                ${summary ? `<div class="autonomous-reflect-summary">${this.escapeHtml(summary)}</div>` : ''}
                ${addedHTML}
                ${blockedHTML}
                ${manualHTML}
            </div>
        `;
        this.chatMessages.appendChild(card);
        this.scrollToBottom();
    }

    /**
     * Render mission start / complete / paused / failed banners.
     * Visually distinct from iteration cards — they're terminal
     * messages that bookmark the run.
     */
    _renderAutonomousBanner(kind, event) {
        if (!this.chatMessages) return;

        let icon = '∞';
        let titleText = '';
        let subText = '';
        let cls = 'autonomous-banner';

        if (kind === 'start') {
            const budget = event && typeof event.time_budget_seconds === 'number'
                ? `time budget: ${this._fmtDuration(event.time_budget_seconds)}`
                : 'full auto (no time cap)';
            const cap = event && event.max_iterations ? `, iteration cap: ${event.max_iterations}` : '';
            titleText = 'Autonomous Mission started';
            subText = `${budget}${cap}.`;
            cls += ' autonomous-banner-start';
        } else if (kind === 'complete') {
            const reason = (event && event.stop_reason) || 'satisfied';
            const elapsed = event && typeof event.elapsed_seconds === 'number'
                ? this._fmtDuration(event.elapsed_seconds)
                : '';
            titleText = 'Mission complete · all acceptance criteria passed';
            subText = `Stop reason: ${this.escapeHtml(reason)}${elapsed ? ` · ${elapsed} elapsed` : ''}.`;
            cls += ' autonomous-banner-complete';
        } else if (kind === 'paused') {
            const reason = (event && event.stop_reason) || 'paused';
            const message = (event && event.stop_message) || '';
            const elapsed = event && typeof event.elapsed_seconds === 'number'
                ? this._fmtDuration(event.elapsed_seconds)
                : '';
            titleText = `Mission paused · ${this.escapeHtml(reason)}`;
            subText = `${this.escapeHtml(message)}${elapsed ? ` · ${elapsed} elapsed` : ''}.`;
            cls += ' autonomous-banner-paused';
        } else if (kind === 'failed') {
            const err = (event && event.error) || 'unknown failure';
            titleText = 'Mission failed';
            subText = `${this.escapeHtml(err)}`;
            cls += ' autonomous-banner-failed';
            icon = '✗';
        }

        const banner = document.createElement('div');
        banner.className = cls;
        banner.innerHTML = `
            <span class="autonomous-banner-icon" aria-hidden="true">${icon}</span>
            <div class="autonomous-banner-text">
                <div class="autonomous-banner-title">${titleText}</div>
                ${subText ? `<div class="autonomous-banner-sub">${subText}</div>` : ''}
            </div>
        `;
        this.chatMessages.appendChild(banner);
        this.scrollToBottom();
    }

    /**
     * v0.3.3 — Bug #25 surface. The chat-header now shows the active
     * project path so misconfigurations (the install dir is the project!
     * permission denied!) are visible BEFORE the agent does damage.
     * Click swaps projects via the native picker.
     */
    _updateHeaderProjectPath(cwd) {
        const btn = document.getElementById('header-project-path');
        const text = document.getElementById('header-project-path-text');
        if (!btn || !text) return;
        const path = (cwd || '').replace(/\\/g, '/');
        // v0.3.4 — empty-state branch dropped. apply_project_context
        // always falls through to a non-empty path (safe-default chain
        // guarantees this since v0.3.3), so the empty rendering was
        // unreachable in practice.
        // Surface the install-dir foot-gun directly in the header so the
        // user sees it before they ever start a session.
        const lower = path.toLowerCase();
        const isUnsafe = lower.includes('/program files') ||
                         lower.startsWith('c:/windows') ||
                         lower.startsWith('/applications/');
        btn.classList.toggle('header-project-path-unsafe', isUnsafe);
        // Show the trailing folder + parent for context (full path on hover).
        const parts = path.split('/');
        const tail = parts.slice(-2).join('/') || path;
        text.textContent = tail;
        btn.title = isUnsafe
            ? `⚠ Project is in a system / install folder: ${path}\nClick to switch.`
            : `Project: ${path}\nClick to switch.`;
        if (!btn._wired) {
            btn._wired = true;
            btn.addEventListener('click', (e) => {
                this._pendingFolderPickConsumer = null;  // global project switch
                // v0.5.6a4 — Shift-click bypasses the native picker
                // and goes straight to the text-input modal. For users
                // who already know the picker won't work in their
                // environment (browser mode, kiosk, etc.) this avoids
                // the picker-fails-then-modal-appears round-trip.
                if (e.shiftKey) {
                    this._promptForProjectPath('Switch project', (path) => {
                        this.selectProjectFolder(path);
                    });
                    return;
                }
                this.send({ command: 'folder_dialog' });
            });
            // Right-click also opens the text-input modal (discoverable
            // alternative for users without a Shift key, e.g. some
            // tablet keyboards).
            btn.addEventListener('contextmenu', (e) => {
                e.preventDefault();
                this._pendingFolderPickConsumer = null;
                this._promptForProjectPath('Switch project', (path) => {
                    this.selectProjectFolder(path);
                });
            });
        }
    }

    /** Look up the current session summary. Tries `sessions` (per-project,
     *  always fresh after session_cleared) first, falls back to
     *  `allSessions` (cross-project, sometimes stale immediately after a
     *  session_cleared since that event only carries per-project data). */
    _currentSessionSummary() {
        if (!this.currentSessionId) return null;
        if (Array.isArray(this.sessions)) {
            const hit = this.sessions.find(s => s && s.id === this.currentSessionId);
            if (hit) return hit;
        }
        if (Array.isArray(this.allSessions)) {
            return this.allSessions.find(s => s && s.id === this.currentSessionId) || null;
        }
        return null;
    }

    /**
     * Sync the chat-header chrome with the current session's mission state.
     * Called whenever sessions_updated or session_cleared lands.
     *
     * Three visible states:
     * - Regular session (no mission_state)        → show "🎯 Mission" toggle
     * - Active mission (drafting/planning/etc.)   → hide toggle, show badge
     * - Past mission (exited / completed)         → hide toggle, show muted
     *                                                "viewing past mission"
     *                                                indicator (B1 fix)
     */
    _syncMissionUI() {
        const sess = this._currentSessionSummary();
        const ms = sess && sess.mission_state;
        const phase = ms && ms.phase;
        const seed = (ms && ms.seed_feature) || '';
        const active = phase && phase !== 'exited' && phase !== 'completed';
        const past = phase === 'exited' || phase === 'completed';

        const toggle = document.getElementById('mission-toggle');
        if (toggle) {
            // Hide the toggle whenever we're inside any mission session
            // (active OR past) — for past missions, the past-mission
            // indicator takes the toggle's slot.
            toggle.style.display = (active || past) ? 'none' : '';
        }
        this._refreshMissionBadge(active ? phase : '', seed);
        this._refreshPastMissionIndicator(past ? phase : '', seed);
        // v0.5.3a3 — refresh the sidebar roadmap inspector. When the
        // current session is in autonomous_running phase, this both
        // ensures the inspector is visible AND triggers a fetch if we
        // don't have a snapshot yet. Hidden cleanly otherwise.
        this._renderAutonomousRoadmapInspector();
        // v0.5.5a2 — re-render mission browser so the "current"
        // highlight follows the active session as the user switches.
        this._renderAutonomousMissionBrowser();

        // Clear the pending feature once the real session arrives so we
        // don't keep showing it on stale switches.
        if (active && this._pendingMissionFeature && seed) {
            this._pendingMissionFeature = '';
        }
    }

    /**
     * The past-mission indicator — sibling of `mission-badge` in the
     * chat header. Shown only when the current session is a mission
     * whose phase is 'exited' or 'completed'. Pure read-only — no
     * exit/cancel action; the only interaction is the optional Resume
     * affordance (B4) on the sidebar row.
     */
    _refreshPastMissionIndicator(phase, seedFeature) {
        const header = document.getElementById('chat-header') ||
                       document.querySelector('.chat-header');
        if (!header) return;

        let el = document.getElementById('mission-past-indicator');
        if (!phase) {
            if (el) el.remove();
            return;
        }
        if (!el) {
            el = document.createElement('div');
            el.id = 'mission-past-indicator';
            el.className = 'mission-past-indicator';
            header.appendChild(el);
        }
        const phaseLabel = phase === 'completed' ? 'completed' : 'exited';
        el.dataset.phase = phase;
        el.innerHTML = `
            <span class="mission-past-icon" aria-hidden="true">🎯</span>
            <span class="mission-past-text">
                <span class="mission-past-label">Mission</span>
                <span class="mission-past-phase">${this.escapeHtml(phaseLabel)}</span>
            </span>
        `;
    }

    /**
     * Build / update the chat-header Mission badge. Pure DOM work — the
     * source of truth lives in the session's `mission_state`. Called from
     * mission_start, session switch, mission_phase_changed, mission_exited.
     *
     * B2 fix — when the badge's phase changes (e.g. drafting → planning),
     * trigger a brief pulse animation so the transition is visually
     * acknowledged rather than silently swapping a label.
     */
    _refreshMissionBadge(phase, seedFeature) {
        const header = document.getElementById('chat-header') ||
                       document.querySelector('.chat-header') ||
                       document.querySelector('header.app-titlebar');
        if (!header) return;

        let badge = document.getElementById('mission-badge');
        // v0.5.0a7 — autonomous_complete is a new "satisfied" state we
        // still want to show briefly (so the user sees the ∞ done
        // banner). autonomous_paused / exited / completed all hide
        // the badge; the chat-card banner conveys the outcome.
        const isActive = phase && phase !== 'exited' && phase !== 'completed' &&
                         phase !== 'autonomous_complete' && phase !== 'autonomous_paused';

        if (!isActive) {
            if (badge) badge.remove();
            return;
        }

        const previousPhase = badge ? badge.dataset.phase : '';

        // v0.5.0a7 — autonomous_running gets a richer badge:
        // ∞ glyph + live iter / time remaining / cost. Render via the
        // dedicated method; non-autonomous phases keep the original
        // 🎯 + phase-name layout below.
        if (phase === 'autonomous_running') {
            this._renderAutonomousBadge(header, badge, previousPhase);
            return;
        }

        if (!badge) {
            badge = document.createElement('button');
            badge.id = 'mission-badge';
            badge.className = 'mission-badge';
            badge.type = 'button';
            badge.title = 'Mission in progress — click to exit';
            badge.addEventListener('click', () => this._handleMissionBadgeClick());
            header.appendChild(badge);
        }

        const phaseLabel = {
            drafting: 'Drafting',
            planning_dispatched: 'Planning',
            executing: 'Executing',
            reviewing: 'Reviewing',
            completed: 'Done',
        }[phase] || phase;

        badge.dataset.phase = phase;
        badge.innerHTML = `
            <span class="mission-badge-icon" aria-hidden="true">🎯</span>
            <span class="mission-badge-text">
                <span class="mission-badge-label">Mission</span>
                <span class="mission-badge-phase">${this.escapeHtml(phaseLabel)}</span>
            </span>
            <span class="mission-badge-exit" title="Exit mission" aria-label="Exit mission">×</span>
        `;
        const exitBtn = badge.querySelector('.mission-badge-exit');
        if (exitBtn) {
            exitBtn.addEventListener('click', (e) => {
                e.stopPropagation();
                this._handleMissionExitClick();
            });
        }

        // Phase-transition pulse (B2). Only fire when the phase actually
        // changed (not on first render — initial appearance already
        // animates via collapsed-group-enter). Forces an animation
        // restart by removing → reflowing → re-adding the class.
        if (previousPhase && previousPhase !== phase) {
            badge.classList.remove('mission-badge-pulse');
            void badge.offsetWidth;
            badge.classList.add('mission-badge-pulse');
        }
    }

    /**
     * v0.5.0a7 — autonomous-mission badge with live iter / time-left /
     * cost / Stop button. Updated on every autonomous_* event via
     * `_updateAutonomousBadgeState`. Replaces the standard 🎯 badge
     * while phase=autonomous_running.
     */
    _renderAutonomousBadge(header, badge, previousPhase) {
        if (!badge || !badge.classList.contains('mission-badge-autonomous')) {
            // Wrong type of badge in place — recreate.
            if (badge) badge.remove();
            badge = document.createElement('div');
            badge.id = 'mission-badge';
            badge.className = 'mission-badge mission-badge-autonomous';
            badge.dataset.phase = 'autonomous_running';
            // v0.5.9a4 — Pause + Stop are now separate affordances.
            // Pause = graceful "finish current iter then stop", no
            // tool calls cancelled mid-flight. Stop = abrupt cancel.
            // Pause is the lighter touch users want for "I just
            // need to look at this before continuing"; Stop is for
            // "kill it now".
            badge.innerHTML = `
                <span class="mission-badge-icon" aria-hidden="true">∞</span>
                <span class="mission-badge-text">
                    <span class="mission-badge-label">Autonomous</span>
                    <span class="mission-badge-phase">starting…</span>
                </span>
                <button type="button" class="mission-badge-pause"
                    title="Finish the current iteration then pause cleanly">Pause</button>
                <button type="button" class="mission-badge-stop"
                    title="Stop now — cancels any in-flight tool calls">Stop</button>
            `;
            header.appendChild(badge);
            const stopBtn = badge.querySelector('.mission-badge-stop');
            stopBtn.addEventListener('click', () => this._handleAutonomousStopClick());
            const pauseBtn = badge.querySelector('.mission-badge-pause');
            pauseBtn.addEventListener('click', () => this._handleAutonomousPauseClick());
        }

        // Re-paint from cached state (set by autonomous_* handlers).
        this._updateAutonomousBadgeState();

        if (previousPhase && previousPhase !== 'autonomous_running') {
            badge.classList.remove('mission-badge-pulse');
            void badge.offsetWidth;
            badge.classList.add('mission-badge-pulse');
        }
    }

    /**
     * v0.5.0a7 — paint the autonomous-badge text from `_autonomousState`.
     * Called on every event that mutates the state (badge updates live
     * even between full re-renders).
     */
    _updateAutonomousBadgeState() {
        const badge = document.getElementById('mission-badge');
        if (!badge || !badge.classList.contains('mission-badge-autonomous')) return;
        const phaseEl = badge.querySelector('.mission-badge-phase');
        if (!phaseEl) return;

        const s = this._autonomousState || {};
        const parts = [];

        if (typeof s.iterCount === 'number') {
            // v0.5.7a2 — qualify "iter N" with "(running)" when the
            // daemon is mid-iteration, so the user knows the inspector's
            // "iter N completed" line refers to the previous iter (N-1).
            // Linux-bridge field-observation #5: badge counted in-flight,
            // inspector counted completed, both correct but visually
            // disagreeing.
            const iterLabel = s.iterInFlight
                ? `iter ${s.iterCount} (running)`
                : `iter ${s.iterCount}`;
            parts.push(iterLabel);
        }
        // Time remaining or elapsed depending on whether we have a budget.
        if (typeof s.startedAt === 'number') {
            const elapsed = Math.max(0, (Date.now() / 1000) - s.startedAt);
            if (s.timeBudgetSeconds && s.timeBudgetSeconds > 0) {
                const left = Math.max(0, s.timeBudgetSeconds - elapsed);
                parts.push(`${this._fmtDuration(left)} left`);
            } else {
                parts.push(`${this._fmtDuration(elapsed)} elapsed`);
            }
        }
        // Cost / burn-rate from the existing CostTracker state if
        // available — exposed via getCostsSummary() if wired.
        const costStr = this._fmtAutonomousCost();
        if (costStr) parts.push(costStr);

        // v0.5.9a1 — live activity. When the daemon emits a phase
        // transition, the badge surfaces "running REFLECT · 12s" so
        // the user can tell at a glance what's happening RIGHT NOW
        // — the killer "is it stuck or just slow" diagnostic for
        // multi-hour runs.
        const activityStr = this._fmtAutonomousActivity();
        if (activityStr) parts.push(activityStr);

        phaseEl.textContent = parts.join(' · ') || 'starting…';
    }

    /**
     * v0.5.9a1 — short label for the daemon's current phase, with
     * elapsed time on this phase. Returns "" if no activity has
     * been received yet.
     */
    _fmtAutonomousActivity() {
        const s = this._autonomousState || {};
        const a = s.activity;
        if (!a || !a.phase) return '';
        // Friendly labels for each phase. Open-vocabulary on the
        // backend side, but the common cases get a tidy mapping;
        // unknown phases pass through as-is.
        const PHASE_LABELS = {
            picking: 'picking item',
            dispatching: 'dispatching',
            waiting_dispatch: 'running sub-mission',
            reflecting: 'running REFLECT',
            tick_pause: 'between iters',
            parked: 'awaiting your decision',
            idle: '',
        };
        const friendly = PHASE_LABELS[a.phase] !== undefined
            ? PHASE_LABELS[a.phase] : a.phase;
        if (!friendly) return '';
        // Elapsed-on-phase, computed client-side from the started_at
        // epoch so the badge ticks live without re-fetching.
        let elapsedStr = '';
        if (typeof a.started_at === 'number') {
            const elapsed = Math.max(0, (Date.now() / 1000) - a.started_at);
            elapsedStr = ` ${this._fmtDuration(elapsed)}`;
        }
        return `${friendly}${elapsedStr}`;
    }

    /**
     * v0.5.0a7 — compact duration (e.g. "1h 23m" / "47m" / "12s").
     */
    _fmtDuration(seconds) {
        seconds = Math.max(0, Math.floor(seconds));
        if (seconds < 60) return `${seconds}s`;
        const m = Math.floor(seconds / 60);
        if (m < 60) return `${m}m`;
        const h = Math.floor(m / 60);
        return `${h}h ${m % 60}m`;
    }

    /**
     * v0.5.0a7 — format the cost / burn-rate for the autonomous badge.
     * Reads from the chat-header's existing cost display if present;
     * empty string when cost tracking isn't wired up yet for this run.
     */
    _fmtAutonomousCost() {
        // CostTracker integration is best-effort here. If the existing
        // chat-header cost element holds a recent total, we surface it
        // alongside burn-rate. Otherwise return empty.
        const costEl = document.querySelector('.chat-cost, .cost-display');
        if (!costEl) return '';
        const txt = (costEl.textContent || '').trim();
        if (!txt || /^\$0(\.0+)?$/.test(txt)) return '';
        return txt;
    }

    /**
     * v0.5.0a7 — Stop-button click on the autonomous badge.
     * Confirms before stopping so a fat-finger doesn't kill an
     * in-flight run.
     */
    _handleAutonomousStopClick() {
        const s = this._autonomousState || {};
        if (!s.intentId) return;
        // v0.5.9a4 — clarified copy: Stop is the abrupt one. Pause
        // is the graceful affordance for the lighter touch.
        const ok = confirm(
            'Stop the autonomous mission NOW? In-flight tool calls ' +
            'will be cancelled. For a graceful "finish current iter ' +
            'then stop", click Pause instead.'
        );
        if (!ok) return;
        this.send({
            command: 'autonomous_mission_stop',
            intent_id: s.intentId,
        });
        // Optimistic UI: dim the stop button + relabel so the user
        // sees their click landed. The actual phase transition fires
        // when the daemon emits autonomous_mission_paused.
        const stopBtn = document.querySelector('.mission-badge-stop');
        if (stopBtn) {
            stopBtn.disabled = true;
            stopBtn.textContent = 'Stopping…';
        }
        const pauseBtn = document.querySelector('.mission-badge-pause');
        if (pauseBtn) pauseBtn.disabled = true;
    }

    /**
     * v0.5.9a4 — graceful pause. Distinct from Stop. The daemon
     * completes the current iteration + reflection cycle then
     * exits. Useful for "I want to look at what just shipped
     * before continuing" without losing the in-flight work.
     */
    _handleAutonomousPauseClick() {
        const s = this._autonomousState || {};
        if (!s.intentId) return;
        const ok = confirm(
            'Pause after the current iteration completes? In-flight ' +
            'tool calls will finish naturally; the daemon stops at ' +
            'the next safe point. You can resume from the orphan ' +
            'banner.'
        );
        if (!ok) return;
        this.send({
            command: 'autonomous_mission_pause',
            intent_id: s.intentId,
        });
        // Optimistic UI.
        const pauseBtn = document.querySelector('.mission-badge-pause');
        if (pauseBtn) {
            pauseBtn.disabled = true;
            pauseBtn.textContent = 'Pausing…';
        }
    }

    /**
     * Inline post-cancel affordance — A1 fix. When the user cancels a
     * turn during a Mission's drafting phase, surface a small banner
     * inviting them to exit the mission too. We don't auto-exit because
     * the user might just want to redo the current question; the banner
     * lets them opt in. Auto-dismisses after 12s.
     */
    _maybeOfferMissionExitOnCancel() {
        const sess = this._currentSessionSummary();
        const phase = sess && sess.mission_state && sess.mission_state.phase;
        if (phase !== 'drafting' && phase !== 'planning_dispatched') return;
        // Only one banner at a time.
        if (document.getElementById('mission-cancel-exit-banner')) return;

        const banner = document.createElement('div');
        banner.id = 'mission-cancel-exit-banner';
        banner.className = 'mission-cancel-exit-banner';
        banner.innerHTML = `
            <span class="mission-cancel-exit-msg">Mission paused. Exit it entirely, or stay in drafting?</span>
            <button type="button" class="mission-cancel-exit-btn-stay">Stay</button>
            <button type="button" class="mission-cancel-exit-btn-exit">Exit mission</button>
        `;
        const cleanup = () => banner.remove();
        banner.querySelector('.mission-cancel-exit-btn-stay').addEventListener('click', cleanup);
        banner.querySelector('.mission-cancel-exit-btn-exit').addEventListener('click', () => {
            cleanup();
            this._handleMissionExitClick();
        });
        this.chatMessages.appendChild(banner);
        this.scrollToBottom();
        setTimeout(cleanup, 12000);
    }

    _handleMissionBadgeClick() {
        // Click on the badge itself — show a small confirm (no popover
        // library, just a confirm() for v1) so the user doesn't lose work
        // on a fat-finger click.
        const ok = confirm('Exit this mission?\n\nThe session stays in your sidebar under "Missions" and you can review the conversation, but no new work will be dispatched.');
        if (ok) this._handleMissionExitClick();
    }

    _handleMissionExitClick() {
        this.send({ command: 'mission_exit' });
    }

    /**
     * Open the Mission composer — small inline modal that asks
     * "What do you want to build?" and dispatches mission_start on submit.
     * Triggered by the "🎯 Mission" button (off state) in the chat header
     * or the "+ Mission" sidebar button.
     */
    openMissionComposer() {
        let overlay = document.getElementById('mission-composer-overlay');
        if (overlay) {
            // Already open — just focus the input.
            overlay.querySelector('textarea')?.focus();
            return;
        }

        // macOS users see ⌘, everyone else sees Ctrl. Surfacing the
        // shortcut directly so the affordance is discoverable (B5 fix —
        // the keybinding existed before but was invisible).
        const isMac = /Mac|iPhone|iPad/i.test(navigator.platform || navigator.userAgent || '');
        const submitHintKey = isMac ? '⌘' : 'Ctrl';

        // v0.3.3 — Bug #25 fix. Mission must let the user pick where the
        // agent is allowed to write. Without this, the mission inherits
        // whatever os.getcwd() landed on at app launch — which for a
        // Start-Menu shortcut on Windows is `C:\Program Files\Resonant
        // Client`. Permission-denied storms followed. The composer now
        // shows the chosen path inline with a picker + manual-edit
        // fallback. The chosen path is only applied on Start, not Cancel.
        this._missionComposerPath = (this.currentCwd || '').replace(/\\/g, '/');

        // v0.4.0 — Ollama-only model hints. The original v0.3.2 codex /
        // claude-code recommendations are gone (those backends were cut).
        // Model-specific advice now centers on the deepseek tier choice
        // and known quirks of other open models on Ollama.
        const modelVal = (this.modelSelector && this.modelSelector.value) || '';
        const backendType = modelVal.indexOf(':') > 0 ? modelVal.substring(0, modelVal.indexOf(':')) : '';
        const modelName = modelVal.indexOf(':') > 0 ? modelVal.substring(modelVal.indexOf(':') + 1) : '';
        let modelHintHTML = '';
        if (!backendType) {
            modelHintHTML = `
                <div class="mission-composer-hint mission-composer-hint-warn">
                    <span aria-hidden="true">⚠</span>
                    Pick a model first — the Mission needs Ollama to run the interview.
                </div>`;
        } else if (backendType === 'ollama' && /qwen/i.test(modelName)) {
            modelHintHTML = `
                <div class="mission-composer-hint mission-composer-hint-info">
                    <span aria-hidden="true">ℹ</span>
                    Heads up: Qwen sometimes formats the spec loosely. If the
                    "Build this roadmap" button doesn't appear, exit and retry,
                    or switch to <strong>deepseek-v4-pro:cloud</strong> for more
                    reliable spec emission.
                </div>`;
        } else if (backendType === 'ollama' && /deepseek-v4-flash/i.test(modelName)) {
            modelHintHTML = `
                <div class="mission-composer-hint mission-composer-hint-info">
                    <span aria-hidden="true">⚡</span>
                    Flash is fast — great for short missions. For multi-specialist
                    work, <strong>deepseek-v4-pro:cloud</strong> usually produces a
                    more thorough spec.
                </div>`;
        }

        overlay = document.createElement('div');
        overlay.id = 'mission-composer-overlay';
        overlay.className = 'mission-composer-overlay';
        overlay.innerHTML = `
            <div class="mission-composer">
                <div class="mission-composer-header">
                    <span class="mission-composer-icon" aria-hidden="true">🎯</span>
                    <span class="mission-composer-title">Start a Mission</span>
                    <button type="button" class="mission-composer-close" aria-label="Close">×</button>
                </div>
                <p class="mission-composer-blurb">
                    Describe a feature or product you want built. The agent will
                    interview you to nail down the spec, then dispatch a
                    plan-graph of work to deliver it.
                </p>
                ${modelHintHTML}
                <label class="mission-composer-field-label" for="mission-composer-path">
                    Build it at
                </label>
                <div class="mission-composer-path-row">
                    <input type="text" id="mission-composer-path" class="mission-composer-path-input"
                        placeholder="C:\\Dev\\my-roguelite" spellcheck="false" autocomplete="off">
                    <button type="button" class="mission-composer-path-pick" title="Browse for folder">📁 Browse</button>
                </div>
                <div class="mission-composer-path-hint" id="mission-composer-path-hint"></div>
                <textarea class="mission-composer-input" rows="4"
                    placeholder="Add a /export command that exports the current chat to markdown…"></textarea>
                <label class="mission-composer-autonomous-row" for="mission-composer-autonomous">
                    <input type="checkbox" id="mission-composer-autonomous"
                        class="mission-composer-autonomous-toggle">
                    <span class="mission-composer-autonomous-label">
                        <span class="mission-composer-autonomous-icon" aria-hidden="true">∞</span>
                        Run autonomously
                    </span>
                    <span class="mission-composer-autonomous-hint">
                        Rigorous grill (10–25 questions, binary acceptance criteria) → multi-iteration
                        autonomous loop. I'll ask for a time budget before launching.
                    </span>
                </label>
                <div class="mission-composer-actions">
                    <span class="mission-composer-shortcut">
                        <kbd>${submitHintKey}</kbd> <kbd>Enter</kbd> to start
                    </span>
                    <button type="button" class="mission-composer-cancel">Cancel</button>
                    <button type="button" class="mission-composer-start" disabled>Start mission</button>
                </div>
            </div>
        `;
        document.body.appendChild(overlay);

        // Pre-fill the path with the current project (or the safe default
        // backend resolution). The user can pick a different folder via
        // the Browse button or just edit the field directly.
        const pathInput = overlay.querySelector('#mission-composer-path');
        const pathHint = overlay.querySelector('#mission-composer-path-hint');
        pathInput.value = this._missionComposerPath || '';
        const updatePathHint = () => {
            const v = pathInput.value.trim();
            if (!v) {
                pathHint.className = 'mission-composer-path-hint mission-composer-path-hint-warn';
                pathHint.textContent = 'Pick or type a folder — the agent will write files here.';
                return;
            }
            // Surface the install-dir / system-dir foot-gun BEFORE the
            // user invests in writing a feature description.
            const lower = v.toLowerCase().replace(/\\/g, '/');
            if (lower.includes('/program files') || lower.startsWith('c:/windows') ||
                lower.startsWith('/applications/') || lower.startsWith('/usr/')) {
                pathHint.className = 'mission-composer-path-hint mission-composer-path-hint-warn';
                pathHint.textContent = '⚠ This is a system / install directory. Pick somewhere under your home folder instead.';
                return;
            }
            pathHint.className = 'mission-composer-path-hint mission-composer-path-hint-ok';
            pathHint.textContent = 'Folder will be created if it doesn\'t exist yet.';
        };
        updatePathHint();
        pathInput.addEventListener('input', updatePathHint);

        overlay.querySelector('.mission-composer-path-pick').addEventListener('click', () => {
            // Mark that the next folder_picked event belongs to the
            // mission composer, not the welcome screen flow.
            this._pendingFolderPickConsumer = 'mission';
            this.send({ command: 'folder_dialog' });
        });

        const textarea = overlay.querySelector('textarea');
        const startBtn = overlay.querySelector('.mission-composer-start');
        const autonomousToggle = overlay.querySelector('#mission-composer-autonomous');
        // A2 fix — backdrop click discards work. If the textarea has
        // substantial input, treat backdrop click as a no-op so a
        // fat-finger doesn't lose the user's typed feature description.
        // The Cancel button and Esc still dismiss intentionally.
        const SUBSTANTIAL_INPUT_THRESHOLD = 20;  // chars
        const close = () => overlay.remove();

        // v0.5.0a7 — Start button label tracks the autonomous toggle
        // so the user knows which flow they're committing to before
        // they hit it.
        const updateStartLabel = () => {
            startBtn.textContent = autonomousToggle.checked
                ? '∞ Start autonomous mission'
                : 'Start mission';
        };
        updateStartLabel();
        autonomousToggle.addEventListener('change', updateStartLabel);

        textarea.addEventListener('input', () => {
            startBtn.disabled = textarea.value.trim().length === 0;
        });
        textarea.addEventListener('keydown', (e) => {
            if (e.key === 'Enter' && (e.metaKey || e.ctrlKey) && !startBtn.disabled) {
                e.preventDefault();
                startBtn.click();
            } else if (e.key === 'Escape') {
                close();
            }
        });
        overlay.querySelector('.mission-composer-close').addEventListener('click', close);
        overlay.querySelector('.mission-composer-cancel').addEventListener('click', close);
        overlay.addEventListener('click', (e) => {
            if (e.target !== overlay) return;
            const len = textarea.value.trim().length;
            if (len >= SUBSTANTIAL_INPUT_THRESHOLD) {
                // User has real input — flash the modal as a hint that
                // backdrop click was ignored, but don't close.
                const composer = overlay.querySelector('.mission-composer');
                composer.classList.remove('mission-composer-flash');
                // Force reflow so the next class addition retriggers the animation.
                void composer.offsetWidth;
                composer.classList.add('mission-composer-flash');
                return;
            }
            close();
        });
        startBtn.addEventListener('click', () => {
            const feature = textarea.value.trim();
            if (!feature) return;
            // v0.3.2 — double-submit guard. Disable + relabel the button
            // before delegating so a second click within the same animation
            // frame can't fire a second mission_start.
            if (startBtn.disabled) return;
            // v0.3.3 — capture the chosen project path. Empty falls back
            // to whatever the backend already has (currentCwd). A non-empty
            // path that differs from currentCwd triggers a project switch
            // before the session is created.
            const chosenPath = pathInput.value.trim();
            // v0.5.0a7 — capture the autonomous-flow opt-in. Backend
            // uses this to switch the grill prompt into rigorous mode
            // and stash mission_state.autonomous so the spec card later
            // renders the right CTA.
            const autonomous = autonomousToggle.checked;
            startBtn.disabled = true;
            startBtn.textContent = 'Starting…';
            close();
            this.startMission(feature, chosenPath, { autonomous });
        });

        setTimeout(() => textarea.focus(), 50);
    }

    handleMissionSpecReady(event) {
        const refined = (event && event.refined_intent) || '';
        const specMd = (event && event.spec_markdown) || '';
        const sessionId = (event && event.session_id) || '';
        if (!refined && !specMd) return;

        // Anchor to the latest assistant message.
        const messages = this.chatMessages.querySelectorAll('.msg-assistant');
        const target = messages[messages.length - 1];
        if (!target) return;
        // Idempotent — multiple text.done events on the same message
        // shouldn't stack buttons.
        if (target.querySelector('.mission-build-action')) return;

        // v0.5.0a7 — branch on mission_state.autonomous. The backend
        // sets that flag on mission_start when the composer toggle was
        // on; the rigorous grill produces a typed-criteria spec with
        // a `**Time budget:**` line. We render different CTAs for
        // each flow so the user knows which loop they're committing
        // to (one-shot planner vs hours of autonomous iteration).
        const isAutonomous = this._currentMissionIsAutonomous();
        if (isAutonomous) {
            // v0.5.6a2 — spec-validity gate. The linux-bridge field-
            // observation run had the model emit a Final-spec block
            // that was truncated mid-sentence (no Acceptance criteria
            // section). The build card rendered anyway with the Build
            // button enabled — clicking it would have hit the backend
            // with malformed spec_markdown that fails extract_spec()
            // with a ValueError. Gate the dispatch card on the spec
            // actually being parseable + carrying ≥1 typed criterion.
            const validity = this._validateAutonomousSpec(specMd);
            if (!validity.ok) {
                target.appendChild(
                    this._buildSpecIncompleteCard(sessionId, validity)
                );
                return;
            }
            const card = this._buildAutonomousBuildCard(sessionId, specMd, refined);
            target.appendChild(card);
        } else {
            const wrap = document.createElement('div');
            wrap.className = 'mission-build-action';
            wrap.innerHTML = `
                <button type="button" class="mission-build-btn" title="Hand this spec to the planner">
                    <span class="mission-build-icon" aria-hidden="true">▸</span>
                    <span class="mission-build-label">Build this roadmap</span>
                </button>
                <span class="mission-build-hint">Spec captured. Click to dispatch the planner with the full spec.</span>
            `;
            wrap.querySelector('.mission-build-btn').addEventListener('click', () => {
                // Tier-1 fix #1: hand the FULL spec markdown over, not
                // just the refined-intent paragraph — the planner
                // needs the assumptions / scope / acceptance criteria
                // too. Backend owns the intent_start + phase
                // transition.
                this.send({
                    command: 'mission_dispatch_roadmap',
                    session_id: sessionId,
                    spec_markdown: specMd,
                    refined_intent: refined,
                });
                const btn = wrap.querySelector('.mission-build-btn');
                btn.disabled = true;
                btn.querySelector('.mission-build-label').textContent = 'Roadmap dispatched';
                // Surface the planner UI proactively so the user sees
                // the graph populate as it builds.
                this.openPlanTab(true);
            });
            target.appendChild(wrap);
        }
        this.scrollToBottom();
    }

    /**
     * v0.5.0a7 — does the current Mission session have the
     * autonomous flag set? Reads mission_state from the loaded
     * SessionRecord first, falls back to the in-flight start flag
     * we stashed in `startMission`.
     */
    _currentMissionIsAutonomous() {
        if (this._pendingMissionAutonomous) return true;
        const sessions = this.sessions || [];
        const cur = sessions.find((s) => s && s.id === this.currentSessionId);
        if (!cur || !cur.mission_state) return false;
        return Boolean(cur.mission_state.autonomous);
    }

    /**
     * v0.5.6a2 — gate the autonomous-mission dispatch card on the
     * spec actually being parseable. The autonomous_session backend
     * calls `extract_spec()` then enforces a non-empty acceptance-
     * criteria list before constructing a roadmap; if either check
     * fails the dispatch raises ValueError. Mirror those gates in
     * the frontend so we don't render a Build button that's going
     * to detonate when clicked.
     *
     * The two real-world failure modes this catches (both observed
     * during the linux-bridge field-observation run):
     *   1. Spec generation truncated mid-sentence — In-scope bullets
     *      are present but no Acceptance criteria section.
     *   2. Model emitted a `## Final spec` heading but no body
     *      (rare; possible on stream interruption).
     *
     * Returns: { ok: bool, reason: string, sectionsFound: object }.
     */
    _validateAutonomousSpec(specMd) {
        const md = (specMd || '').trim();
        const sectionsFound = {
            finalSpec: /^##\s+Final spec/im.test(md),
            acceptanceCriteria: /\*\*Acceptance criteria:\*\*/i.test(md),
            timeBudget: /\*\*Time budget:\*\*/i.test(md),
        };
        if (!sectionsFound.finalSpec) {
            return {
                ok: false,
                reason: 'Spec is missing the `## Final spec` heading.',
                sectionsFound,
            };
        }
        if (!sectionsFound.acceptanceCriteria) {
            return {
                ok: false,
                reason: 'Spec has no `**Acceptance criteria:**` section. The model may have been cut off mid-generation.',
                sectionsFound,
            };
        }
        // Acceptance criteria section exists — count typed criteria.
        // Mirror the regex shape `roadmap.py._CRITERION_LINE_RE` uses
        // (`- [ ] \`[bash|chrome|vision|manual]\` <text>`).
        const criteriaMatches = md.match(
            /^-\s*\[[\s x]\]\s*`\[(?:bash|chrome|vision|manual)\]`/gim,
        );
        const criteriaCount = criteriaMatches ? criteriaMatches.length : 0;
        if (criteriaCount === 0) {
            return {
                ok: false,
                reason: 'Spec has the Acceptance criteria heading but no typed criteria (looking for lines like ``- [ ] `[bash]` ...``).',
                sectionsFound,
                criteriaCount,
            };
        }
        return {
            ok: true,
            reason: '',
            sectionsFound,
            criteriaCount,
        };
    }

    /**
     * v0.5.6a2 — render an "incomplete spec" card in place of the
     * dispatch card. Tells the user what's missing and offers a
     * "Continue spec" button that asks the model to finish.
     *
     * Better than letting the user click Build and hit a backend
     * ValueError they can't easily understand.
     */
    _buildSpecIncompleteCard(sessionId, validity) {
        const wrap = document.createElement('div');
        wrap.className = 'mission-build-action mission-spec-incomplete';
        wrap.innerHTML = `
            <div class="mission-spec-incomplete-head">
                <span class="mission-spec-incomplete-icon" aria-hidden="true">⚠</span>
                <span class="mission-spec-incomplete-title">Spec is incomplete</span>
            </div>
            <p class="mission-spec-incomplete-reason">
                ${this.escapeHtml(validity.reason)}
            </p>
            <p class="mission-spec-incomplete-detail">
                Cannot dispatch the autonomous loop until the spec includes
                at least one typed acceptance criterion (
                <code>[bash]</code> / <code>[chrome]</code> / <code>[vision]</code> / <code>[manual]</code>
                ).
            </p>
            <div class="mission-spec-incomplete-actions">
                <button type="button" class="mission-spec-incomplete-continue">
                    Ask the model to complete the spec
                </button>
            </div>
        `;
        const continueBtn = wrap.querySelector('.mission-spec-incomplete-continue');
        continueBtn.addEventListener('click', () => {
            // Send a clarifying user message that prompts the model
            // to fill in the missing sections. Same idea as my manual
            // intervention during the linux-bridge run; codifying it
            // here means users don't have to figure out the magic
            // phrasing themselves.
            const prompt = "Your spec was incomplete — please continue and emit the remaining sections. I need a `## Final spec` block with at least: **Refined intent**, **In scope**, **Out of scope**, **Time budget**, **Technical constraints**, **Acceptance criteria** (≥4 typed binary criteria using `[bash]` / `[chrome]` / `[vision]` / `[manual]` tags), and **Open risks**. Just continue from where you stopped.";
            // Reuse the regular composer-submit path so the model
            // sees this as a normal user message.
            if (this.userInput) {
                this.userInput.value = prompt;
                if (this.sendBtn) this.sendBtn.click();
            }
            continueBtn.disabled = true;
            continueBtn.textContent = 'Asked — waiting for model…';
        });
        return wrap;
    }

    /**
     * v0.5.0a7 — extract `**Time budget:** <label>` from the spec
     * markdown so the budget UI can pre-fill the model's
     * recommendation. Empty string when absent (e.g. legacy or
     * non-rigorous spec).
     */
    _extractTimeBudget(specMd) {
        if (!specMd) return '';
        const m = specMd.match(/\*\*Time budget:\*\*\s*(.+?)\s*$/im);
        return m ? m[1].trim() : '';
    }

    /**
     * v0.5.0a7 — render the spec-card with the budget confirmation
     * presets. User picks a preset (or accepts the model's recommend-
     * ation pre-filled), then clicks "∞ Build autonomously" to fire
     * mission_dispatch_autonomous.
     */
    _buildAutonomousBuildCard(sessionId, specMd, refined) {
        const recommended = this._extractTimeBudget(specMd) || '4h';
        const wrap = document.createElement('div');
        wrap.className = 'mission-build-action mission-autonomous-card';

        // Budget preset buttons. The labels match what the rigorous
        // grill is told to produce (§11.5 of the design doc).
        const presets = [
            { label: '1h', sub: 'lunch break' },
            { label: '4h', sub: '' },
            { label: '6h', sub: '' },
            { label: '8h', sub: '' },
            { label: '12h', sub: '' },
            { label: '24h', sub: '' },
            { label: '48h', sub: '' },
            { label: 'Full auto', sub: 'no time cap' },
        ];

        const presetHTML = presets.map((p) => `
            <button type="button"
                class="mission-budget-preset"
                data-budget="${p.label}"
                ${p.label.toLowerCase() === recommended.toLowerCase() ? 'data-default="1"' : ''}>
                <span class="mission-budget-preset-label">${p.label}</span>
                ${p.sub ? `<span class="mission-budget-preset-sub">${p.sub}</span>` : ''}
            </button>
        `).join('');

        wrap.innerHTML = `
            <div class="mission-autonomous-head">
                <span class="mission-autonomous-icon" aria-hidden="true">∞</span>
                <span class="mission-autonomous-title">Autonomous Mission</span>
            </div>
            <p class="mission-autonomous-blurb">
                Acceptance criteria from the spec drive the convergence check. The mission stops
                when ALL criteria are met (regardless of budget remaining), the budget runs out,
                or you click Stop. <strong>Pick a time budget:</strong>
            </p>
            <div class="mission-budget-presets">${presetHTML}</div>
            <div class="mission-autonomous-actions">
                <span class="mission-autonomous-budget-label">
                    Selected: <strong class="mission-autonomous-budget-display">${recommended}</strong>
                </span>
                <button type="button" class="mission-build-btn mission-build-btn-autonomous">
                    <span class="mission-build-icon" aria-hidden="true">∞</span>
                    <span class="mission-build-label">Build autonomously</span>
                </button>
            </div>
            <p class="mission-autonomous-fullauto-note" style="display: none;">
                Full auto skips the time ceiling. Mission stops only on convergence, blocking,
                or your Stop click. A 100-iteration cap is always enforced as a defensive backstop.
            </p>
        `;

        // Wire preset selection. Default selection from the spec's
        // `**Time budget:**` line (or "4h" if absent).
        let chosen = recommended;
        const presetButtons = wrap.querySelectorAll('.mission-budget-preset');
        const budgetDisplay = wrap.querySelector('.mission-autonomous-budget-display');
        const fullAutoNote = wrap.querySelector('.mission-autonomous-fullauto-note');
        const updateSelection = (label) => {
            chosen = label;
            budgetDisplay.textContent = label;
            presetButtons.forEach((b) => {
                b.classList.toggle(
                    'mission-budget-preset-selected',
                    b.dataset.budget.toLowerCase() === label.toLowerCase(),
                );
            });
            fullAutoNote.style.display = /full/i.test(label) ? 'block' : 'none';
        };
        presetButtons.forEach((b) => {
            b.addEventListener('click', () => updateSelection(b.dataset.budget));
            if (b.dataset.default === '1') {
                updateSelection(b.dataset.budget);
            }
        });
        // Defensive — if no preset matched the recommendation, default to 4h.
        if (!wrap.querySelector('.mission-budget-preset-selected')) {
            updateSelection('4h');
        }

        const buildBtn = wrap.querySelector('.mission-build-btn-autonomous');
        buildBtn.addEventListener('click', () => {
            // The chosen budget is included in the spec the daemon
            // reads — overwrite the `**Time budget:**` line in the
            // spec markdown so the user's pick wins over the model's
            // recommendation.
            const finalSpec = this._patchTimeBudget(specMd, chosen);
            this.send({
                command: 'mission_dispatch_autonomous',
                session_id: sessionId,
                spec_markdown: finalSpec,
                refined_intent: refined,
                time_budget: chosen,
            });
            // v0.5.7a4 — collapse the dispatch card into a one-line
            // confirmation chip after click. Linux-bridge field-
            // observation #11: keeping the full card around (just
            // greyed out) was visual clutter for the next 2 hours
            // of the run. The chip stays in the chat as a permanent
            // marker of WHEN the daemon was dispatched + what budget,
            // and exposes a Stop affordance that's still useful.
            this._collapseDispatchCardToChip(wrap, chosen);
            this.openPlanTab(true);
        });

        return wrap;
    }

    /**
     * v0.5.7a4 — Replace the in-place dispatch card with a compact
     * one-line chip so the chat doesn't carry the full card forward
     * for the rest of the run. The chip records the dispatch
     * timestamp (so the user can scroll back and see when the
     * daemon was kicked off) + the chosen budget + a Stop button
     * that proxies to the existing autonomous-stop flow.
     */
    _collapseDispatchCardToChip(card, budget) {
        if (!card || !card.parentNode) return;
        const time = new Date();
        const hh = String(time.getHours()).padStart(2, '0');
        const mm = String(time.getMinutes()).padStart(2, '0');
        const ss = String(time.getSeconds()).padStart(2, '0');
        const chip = document.createElement('div');
        chip.className = 'mission-dispatch-chip';
        chip.innerHTML = `
            <span class="mission-dispatch-chip-icon" aria-hidden="true">∞</span>
            <span class="mission-dispatch-chip-text">
                Mission dispatched at ${hh}:${mm}:${ss}
                <span class="mission-dispatch-chip-budget">· ${this.escapeHtml(budget || '')}</span>
            </span>
            <button type="button" class="mission-dispatch-chip-stop" title="Stop the autonomous mission after the current iteration">Stop</button>
        `;
        const stopBtn = chip.querySelector('.mission-dispatch-chip-stop');
        stopBtn.addEventListener('click', () => {
            // Reuse the existing stop click handler so the confirm
            // dialog + WS command + UI update all match the badge's
            // Stop affordance.
            this._handleAutonomousStopClick();
        });
        card.parentNode.replaceChild(chip, card);
    }

    /**
     * v0.5.0a7 — overwrite (or insert) the `**Time budget:** <X>`
     * line in a spec block so the user's pick replaces the model's
     * recommendation before the spec hits the autonomous daemon.
     */
    _patchTimeBudget(specMd, label) {
        if (!specMd) return specMd;
        if (/\*\*Time budget:\*\*/i.test(specMd)) {
            return specMd.replace(
                /\*\*Time budget:\*\*\s*.+?\s*$/im,
                `**Time budget:** ${label}`,
            );
        }
        // No existing line — insert before **Acceptance criteria:**
        // (which the rigorous grill always emits) so the daemon picks
        // it up cleanly. Falls through to a plain append if neither
        // anchor exists.
        if (/\*\*Acceptance criteria:\*\*/i.test(specMd)) {
            return specMd.replace(
                /(\*\*Acceptance criteria:\*\*)/i,
                `**Time budget:** ${label}\n\n$1`,
            );
        }
        return `${specMd}\n\n**Time budget:** ${label}\n`;
    }

    setRunning(running) {
        this.isRunning = running;
        this.sendBtn.style.display = running ? 'none' : 'flex';
        this.stopBtn.style.display = running ? 'flex' : 'none';
        this.userInput.disabled = running;
        if (!running) {
            this.userInput.focus();
        }
    }

    // ── Terminal Bar ─────────────────────────────────────────────

    trackTerminalStart(callId, name, args) {
        const command = name.toLowerCase() === 'bash' ? (args.command || '') : name;
        this.activeTerminals.set(callId, {
            name,
            command,
            startTime: Date.now(),
        });
        this.updateTerminalBar();
        this.startTerminalTimer();
    }

    trackTerminalEnd(callId) {
        if (!this.activeTerminals.has(callId)) return; // guard against duplicate results
        this.activeTerminals.delete(callId);

        // Update the list entry to show done state
        const el = document.querySelector(`.terminal-entry[data-call-id="${callId}"]`);
        if (el) {
            const spinner = el.querySelector('.terminal-entry-spinner');
            if (spinner) {
                spinner.outerHTML = '<span class="terminal-entry-done">✓</span>';
            }
            const stopBtn = el.querySelector('.terminal-entry-stop');
            if (stopBtn) stopBtn.remove();
            // Remove after brief delay to show completion
            setTimeout(() => {
                if (el.parentNode) el.remove();
                this.updateTerminalBar();
            }, 800);
        } else {
            this.updateTerminalBar();
        }
    }

    updateTerminalBar() {
        const count = this.activeTerminals.size;
        if (count === 0) {
            this.terminalBar.style.display = 'none';
            this.terminalBar.classList.remove('expanded');
            return;
        }

        this.terminalBar.style.display = 'block';
        this.terminalBarLabel.textContent = `Running ${count} terminal${count !== 1 ? 's' : ''}`;

        // Rebuild list entries
        this.terminalBarList.innerHTML = '';
        for (const [callId, info] of this.activeTerminals) {
            const displayCmd = info.command.length > 60
                ? info.command.slice(0, 57) + '...'
                : info.command;
            const elapsed = ((Date.now() - info.startTime) / 1000).toFixed(1);

            const entry = document.createElement('div');
            entry.className = 'terminal-entry';
            entry.setAttribute('data-call-id', callId);
            entry.innerHTML = `
                <div class="terminal-entry-left">
                    <span class="terminal-entry-spinner"></span>
                    <span class="terminal-entry-cmd" title="${this.escapeHtml(info.command)}">$ ${this.escapeHtml(displayCmd)}</span>
                </div>
                <div style="display:flex;align-items:center;gap:6px;">
                    <span class="terminal-entry-elapsed">${elapsed}s</span>
                    <button class="terminal-entry-stop" title="Cancel current run" data-call-id="${callId}">
                        <svg width="10" height="10" viewBox="0 0 10 10" fill="none">
                            <rect x="1.5" y="1.5" width="7" height="7" rx="1" fill="currentColor"/>
                        </svg>
                    </button>
                </div>
            `;

            this.terminalBarList.appendChild(entry);
        }
    }

    clearTerminals() {
        this.activeTerminals.clear();
        this.updateTerminalBar();
        if (this._terminalTimer) {
            clearInterval(this._terminalTimer);
            this._terminalTimer = null;
        }
    }

    startTerminalTimer() {
        if (this._terminalTimer) return;
        this._terminalTimer = setInterval(() => {
            if (this.activeTerminals.size === 0) {
                clearInterval(this._terminalTimer);
                this._terminalTimer = null;
                return;
            }
            // Update elapsed times in the list
            for (const [callId, info] of this.activeTerminals) {
                const el = document.querySelector(`.terminal-entry[data-call-id="${callId}"] .terminal-entry-elapsed`);
                if (el) {
                    el.textContent = ((Date.now() - info.startTime) / 1000).toFixed(1) + 's';
                }
            }
        }, 500);
    }

    applySessionRoleUI(role) {
        this.currentSessionRole = role || 'generator';
        document.body.dataset.sessionRole = this.currentSessionRole;
        this.updateHarnessBadge();
    }

    formatSessionRole(role) {
        // Note: 'chat' is a legacy role from the pre-refocus Ask mode. We
        // now hide it instead of rendering "Ask" — chat mode is gone.
        const labels = { planner: 'Planner', generator: 'Generator', evaluator: 'Evaluator' };
        if (role === 'chat') return ''; // suppress legacy tag
        return labels[role] || 'Generator';
    }

    setHarnessSprint(payload) {
        this.send({ command: 'set_harness_sprint', ...payload });
    }

    setHarnessContractStatus(status) {
        this.send({
            command: 'set_harness_contract_status',
            status,
            session_role: this.currentSessionRole || 'planner',
        });
    }

    setEvaluatorVerdict(payload) {
        this.send({ command: 'set_evaluator_verdict', ...payload });
    }

    requestHarnessCycleList() {
        this.send({ command: 'harness_cycle_list' });
    }

    getActiveHarnessCycle() {
        return (this.harnessCycles || []).find(run => run.status === 'running' || run.status === 'pending') || null;
    }

    updateHarnessCyclePolling() {
        const active = this.getActiveHarnessCycle();
        if (active && !this.harnessCyclePoller) {
            this.harnessCyclePoller = setInterval(() => {
                this.requestHarnessCycleList();
                this.send({ command: 'get_harness_state' });
            }, 3000);
            return;
        }
        if (!active && this.harnessCyclePoller) {
            clearInterval(this.harnessCyclePoller);
            this.harnessCyclePoller = null;
        }
    }

    rerenderHarnessPopoverIfOpen() {
        if (!this.harnessPopoverOpen) return;
        const existing = document.querySelector('.harness-popover');
        if (!existing) return;
        existing.remove();
        this.harnessPopoverOpen = false;
        this.toggleHarnessPopover();
    }

    startHarnessCycle(payload) {
        this.send({ command: 'harness_cycle_start', ...payload });
    }

    requestHarnessTeacherRecovery(payload) {
        this.send({ command: 'harness_teacher_recover', ...payload });
    }

    promptHarnessCycle(kind = 'cycle') {
        const active = this.getActiveHarnessCycle();
        if (active) {
            this.showStatusMessage(`Harness cycle already ${active.status}`);
            return;
        }

        const current = this.harnessState || {};
        const defaultObjective = current.contract_objective || current.summary || '';
        let objective = defaultObjective;
        if (kind === 'cycle' || !current.active_sprint_id) {
            objective = prompt('Top-level objective:', defaultObjective);
            if (objective === null) return;
        }

        let maxLoops = kind === 'step' ? 1 : 6;
        if (kind === 'cycle') {
            const rawLoops = prompt('Max automated steps:', '6');
            if (rawLoops === null) return;
            const parsed = Number.parseInt(rawLoops, 10);
            if (!Number.isNaN(parsed) && parsed > 0) {
                maxLoops = parsed;
            }
        }

        this.startHarnessCycle({
            name: kind === 'step' ? 'Harness Step' : 'Harness Cycle',
            objective: (objective || '').trim(),
            max_loops: maxLoops,
        });
        this.requestHarnessCycleList();
    }

    cancelActiveHarnessCycle() {
        const active = this.getActiveHarnessCycle();
        if (!active) {
            this.showStatusMessage('No active harness cycle');
            return;
        }
        this.send({ command: 'harness_cycle_cancel', run_id: active.id });
    }

    promptHarnessTeacherRecovery() {
        const current = this.harnessState || {};
        const defaultReason = current.evaluator_verdict === 'blocked' ? 'manual_blocked_recovery' : 'manual_recovery';
        const reason = prompt('Teacher recovery reason:', defaultReason);
        if (reason === null) return;
        const failedRole = prompt(
            'Failed role:',
            current.active_role || this.currentSessionRole || 'generator'
        );
        if (failedRole === null) return;
        const objective = prompt(
            'Objective override:',
            current.contract_objective || current.summary || ''
        );
        if (objective === null) return;
        this.requestHarnessTeacherRecovery({
            reason: reason.trim() || defaultReason,
            failed_role: (failedRole || '').trim() || 'generator',
            objective: (objective || '').trim(),
        });
    }

    requestHarnessResumePrompt() {
        this.send({
            command: 'get_harness_resume_prompt',
            session_mode: 'code',
            session_role: this.currentSessionRole || 'generator',
        });
    }

    applyResumePrompt(prompt) {
        if (!prompt) return;
        this.userInput.value = prompt;
        this.userInput.style.height = 'auto';
        this.userInput.style.height = Math.min(this.userInput.scrollHeight, 200) + 'px';
        this.userInput.focus();
        this.showStatusMessage('Loaded resume prompt from harness state');
    }

    updateHarnessBadge() {
        if (!this.harnessBadge || !this.harnessBadgeText) return;
        // Master gate first: when sprint workflow is off, the badge never appears.
        if (this.harnessEnabled === false) {
            this.harnessBadge.style.display = 'none';
            return;
        }
        // Only surface the harness badge when the workflow is actually being used:
        // - an active sprint exists, OR
        // - a harness cycle is running.
        // Otherwise the badge "no sprint · proposed" is just visual noise for
        // users who don't use harness mode.
        const activeCycle = this.getActiveHarnessCycle();
        const hasActiveSprint = this.harnessState && (this.harnessState.active_sprint_id || activeCycle);
        if (!hasActiveSprint) {
            this.harnessBadge.style.display = 'none';
            return;
        }
        this.harnessBadge.style.display = 'flex';

        const sprint = this.harnessState.active_sprint_id || 'cycle';
        if (activeCycle) {
            const cycleRole = activeCycle.current_role || activeCycle.active_step?.role || activeCycle.status;
            this.harnessBadgeText.textContent = `${sprint} · auto:${cycleRole}`;
            return;
        }
        const verdict = this.harnessState.evaluator_verdict || 'unknown';
        const status = this.harnessState.contract_status || 'idle';
        this.harnessBadgeText.textContent = `${sprint} · ${verdict !== 'unknown' ? verdict : status}`;
    }

    toggleHarnessPopover() {
        const existing = document.querySelector('.harness-popover');
        if (existing) {
            existing.remove();
            this.harnessPopoverOpen = false;
            return;
        }
        if (!this.harnessState) {
            this.send({ command: 'get_harness_state' });
            return;
        }
        this.requestHarnessCycleList();

        this.harnessPopoverOpen = true;
        const popover = document.createElement('div');
        popover.className = 'harness-popover';

        const sprint = this.harnessState.active_sprint_id || 'No active sprint';
        const role = this.formatSessionRole(this.currentSessionRole || 'generator');
        const objective = this.harnessState.contract_objective || this.harnessState.summary || 'No objective yet.';
        const revisions = (this.harnessState.required_revisions || []).slice(0, 4);
        const checks = (this.harnessState.acceptance_checks || []).slice(0, 4);
        const teacherEscalations = (this.harnessState.recent_teacher_escalations || []).slice().reverse().slice(0, 2);
        const recentEvaluatorEvents = (this.harnessState.recent_run_events || [])
            .slice()
            .reverse()
            .filter(item => item?.event === 'cycle_step_completed' && item?.payload?.role === 'evaluator')
            .slice(0, 3);
        const activeCycle = this.getActiveHarnessCycle();
        const cycleText = activeCycle
            ? `${activeCycle.status} · ${activeCycle.current_role || activeCycle.active_step?.role || 'waiting'} · ${activeCycle.current_loop}/${activeCycle.max_loops}`
            : 'idle';

        popover.innerHTML = `
            <div class="git-popover-header">
                <span>Harness · ${this.escapeHtml(role)}</span>
                <button class="icon-btn harness-popover-close">&times;</button>
            </div>
            <div class="harness-popover-body">
                <div class="harness-popover-row"><span class="harness-label">Sprint</span><span>${this.escapeHtml(sprint)}</span></div>
                <div class="harness-popover-row"><span class="harness-label">Contract</span><span>${this.escapeHtml(this.harnessState.contract_status || 'unknown')}</span></div>
                <div class="harness-popover-row"><span class="harness-label">Verdict</span><span>${this.escapeHtml(this.harnessState.evaluator_verdict || 'unknown')}</span></div>
                <div class="harness-popover-row"><span class="harness-label">Automation</span><span>${this.escapeHtml(cycleText)}</span></div>
                <div class="harness-popover-block">
                    <div class="harness-label">Objective</div>
                    <div class="harness-text">${this.escapeHtml(objective)}</div>
                </div>
                <div class="harness-popover-block">
                    <div class="harness-label">Checks</div>
                    <div class="harness-list">${checks.length ? checks.map(c => `<div>• ${this.escapeHtml(c)}</div>`).join('') : '<div>• none</div>'}</div>
                </div>
                <div class="harness-popover-block">
                    <div class="harness-label">Required Revisions</div>
                    <div class="harness-list">${revisions.length ? revisions.map(c => `<div>• ${this.escapeHtml(c)}</div>`).join('') : '<div>• none</div>'}</div>
                </div>
                <div class="harness-popover-block">
                    <div class="harness-label">Recent Teacher Recovery</div>
                    <div class="harness-list">${
                        teacherEscalations.length
                            ? teacherEscalations.map(item => {
                                const provider = item.teacher_provider || 'teacher';
                                const model = item.teacher_model || '';
                                const roleName = item.recommended_role || item.response?.recommended_role || 'unknown';
                                const status = item.status || 'unknown';
                                const kind = item.response?.recovery_kind || '';
                                const label = `${provider}${model ? `/${model}` : ''} → ${roleName}`;
                                const detail = [status, kind].filter(Boolean).join(' · ');
                                return `<div>• <strong>${this.escapeHtml(label)}</strong>${detail ? ` <span class="harness-inline-meta">${this.escapeHtml(detail)}</span>` : ''}</div>`;
                            }).join('')
                            : '<div>• none</div>'
                    }</div>
                </div>
                <div class="harness-popover-block">
                    <div class="harness-label">Recent Evaluator Path</div>
                    <div class="harness-list">${
                        recentEvaluatorEvents.length
                            ? recentEvaluatorEvents.map(item => {
                                const payload = item.payload || {};
                                const backend = payload.backend_type || 'unknown';
                                const model = payload.model || '';
                                const verdict = payload.evaluator_verdict || 'unknown';
                                const mode = payload.evaluation_mode || '';
                                const route = payload.prechecked ? 'precheck' : 'model';
                                const label = `${backend}${model ? `/${model}` : ''} → ${verdict}`;
                                const detail = [mode, route].filter(Boolean).join(' · ');
                                return `<div>• <strong>${this.escapeHtml(label)}</strong>${detail ? ` <span class="harness-inline-meta">${this.escapeHtml(detail)}</span>` : ''}</div>`;
                            }).join('')
                            : '<div>• none</div>'
                    }</div>
                </div>
                <div class="harness-popover-actions">
                    <button class="harness-action-btn" data-action="refresh">Refresh</button>
                    <button class="harness-action-btn" data-action="resume">Resume</button>
                    <button class="harness-action-btn" data-action="teacher-recover">Teacher</button>
                    <button class="harness-action-btn" data-action="run-step">Run Step</button>
                    <button class="harness-action-btn" data-action="run-cycle">Auto Cycle</button>
                    <button class="harness-action-btn" data-action="stop-cycle">Stop</button>
                    <button class="harness-action-btn" data-action="approve-contract">Approve</button>
                    <button class="harness-action-btn" data-action="set-sprint">Set Sprint</button>
                    <button class="harness-action-btn" data-action="pass">Pass</button>
                    <button class="harness-action-btn" data-action="revise">Revise</button>
                    <button class="harness-action-btn" data-action="blocked">Blocked</button>
                </div>
            </div>
        `;

        document.getElementById('main').appendChild(popover);
        popover.querySelector('.harness-popover-close').addEventListener('click', () => this.toggleHarnessPopover());
        popover.addEventListener('click', (e) => {
            const btn = e.target.closest('.harness-action-btn');
            if (!btn) return;
            const action = btn.dataset.action;
            if (action === 'refresh') {
                this.send({ command: 'get_harness_state' });
                this.requestHarnessCycleList();
                return;
            }
            if (action === 'resume') {
                this.requestHarnessResumePrompt();
                return;
            }
            if (action === 'teacher-recover') {
                this.promptHarnessTeacherRecovery();
                return;
            }
            if (action === 'run-step') {
                this.promptHarnessCycle('step');
                return;
            }
            if (action === 'run-cycle') {
                this.promptHarnessCycle('cycle');
                return;
            }
            if (action === 'stop-cycle') {
                this.cancelActiveHarnessCycle();
                return;
            }
            if (action === 'approve-contract') {
                this.setHarnessContractStatus('approved');
                return;
            }
            if (action === 'set-sprint') {
                this.promptHarnessSprint();
                return;
            }
            this.promptHarnessVerdict(action);
        });

        // Close on click outside
        setTimeout(() => {
            const clickHandler = (e) => {
                if (!popover.contains(e.target) && !this.harnessBadge.contains(e.target)) {
                    this.toggleHarnessPopover();
                    document.removeEventListener('click', clickHandler);
                    document.removeEventListener('keydown', escHandler);
                }
            };
            // Close on Escape key
            const escHandler = (e) => {
                if (e.key === 'Escape') {
                    this.toggleHarnessPopover();
                    document.removeEventListener('click', clickHandler);
                    document.removeEventListener('keydown', escHandler);
                }
            };
            document.addEventListener('click', clickHandler);
            document.addEventListener('keydown', escHandler);
        }, 100);
    }

    promptHarnessSprint() {
        const current = this.harnessState || {};
        const sprintId = prompt('Sprint ID:', current.active_sprint_id || 'sprint-001');
        if (!sprintId) return;
        const featureName = prompt('Feature name:', current.contract_feature_name || '');
        if (featureName === null) return;
        const objective = prompt('Objective:', current.contract_objective || current.summary || '');
        if (!objective) return;
        const acceptance = prompt(
            'Acceptance checks (separate with |):',
            (current.acceptance_checks || []).join(' | ')
        );
        if (acceptance === null) return;
        const evaluatorFocus = prompt(
            'Evaluator focus (separate with |):',
            (current.evaluator_focus || []).join(' | ')
        );
        if (evaluatorFocus === null) return;
        this.setHarnessSprint({
            sprint_id: sprintId.trim(),
            feature_name: featureName.trim(),
            objective: objective.trim(),
            acceptance_checks: acceptance.split('|').map(s => s.trim()).filter(Boolean),
            evaluator_focus: evaluatorFocus.split('|').map(s => s.trim()).filter(Boolean),
            status: 'approved',
            session_role: this.currentSessionRole || 'planner',
        });
    }

    promptHarnessVerdict(action) {
        const verdict = action;
        const sprintId = this.harnessState?.active_sprint_id || prompt('Sprint ID:', '');
        if (!sprintId) return;
        const findings = prompt('Findings (separate with |):', (this.harnessState?.findings || []).join(' | '));
        if (findings === null) return;
        const revisionsDefault = verdict === 'revise'
            ? (this.harnessState?.required_revisions || []).join(' | ')
            : '';
        const revisions = prompt('Required revisions (separate with |):', revisionsDefault);
        if (revisions === null) return;
        this.setEvaluatorVerdict({
            sprint_id: sprintId.trim(),
            verdict,
            findings: findings.split('|').map(s => s.trim()).filter(Boolean),
            required_revisions: revisions.split('|').map(s => s.trim()).filter(Boolean),
        });
    }

    // ── Event Handler ───────────────────────────────────────────

    handleEvent(event) {
        const type = event.event;

        switch (type) {
            case 'init':
                this.handleInit(event);
                break;
            case 'session.start':
                this.handleSessionStart(event);
                break;
            case 'step.start':
                this.handleStepStart(event);
                break;
            case 'text.delta':
                this.handleTextDelta(event);
                break;
            case 'text.done':
                this.handleTextDone(event);
                break;
            case 'todos.updated':
                this.handleTodosUpdated(event);
                break;
            case 'tool.call':
                this.handleToolCall(event);
                break;
            case 'tool.result':
                this.handleToolResult(event);
                break;
            case 'status':
                this.handleStatus(event);
                break;
            case 'step.end':
                this.handleStepEnd(event);
                break;
            case 'session.end':
                this.handleSessionEnd(event);
                break;
            case 'error':
                this.handleError(event);
                break;
            case 'subagent.start':
                this.handleSubagentStart(event);
                break;
            case 'subagent.end':
                this.handleSubagentEnd(event);
                break;
            case 'shell_exec_result':
                this.handleShellExecResult(event);
                break;
            case 'project_files':
                this.handleProjectFiles(event);
                break;
            case 'mission.spec_ready':
                this.handleMissionSpecReady(event);
                break;
            case 'mission_phase_changed':
                this.handleMissionPhaseChanged(event);
                break;
            case 'mission_exited':
                this.handleMissionExited(event);
                break;
            // v0.5.0a7 — autonomous-mission events from
            // AutonomousMissionDaemon. See docs/long-running-agents-
            // phase-2-implementation.md §4.5 for the contract.
            case 'autonomous_mission_started':
                this.handleAutonomousMissionStarted(event);
                break;
            case 'autonomous_iteration_started':
                this.handleAutonomousIterationStarted(event);
                break;
            case 'autonomous_iteration_complete':
                this.handleAutonomousIterationComplete(event);
                break;
            case 'autonomous_iteration_failed':
                this.handleAutonomousIterationFailed(event);
                break;
            case 'autonomous_reflection':
                this.handleAutonomousReflection(event);
                break;
            case 'autonomous_mission_complete':
                this.handleAutonomousMissionEnded(event, true);
                break;
            case 'autonomous_mission_paused':
                this.handleAutonomousMissionEnded(event, false);
                break;
            case 'autonomous_mission_failed':
                this.handleAutonomousMissionFailed(event);
                break;
            // v0.5.8a2 — REFLECT emitted a decision_request; the
            // daemon is parked waiting for the user's pick. Render an
            // inline card with the options + a Submit button. Once the
            // user picks, the daemon retries REFLECT with the choice
            // folded into the prompt.
            case 'autonomous_human_decision_required':
                this.handleAutonomousHumanDecisionRequired(event);
                break;
            case 'autonomous_human_decision_received':
                this.handleAutonomousHumanDecisionReceived(event);
                break;
            case 'autonomous_decision_dispatched':
                // No-op surface event — confirms the WS round-trip.
                // The daemon-side `_received` event arrives separately
                // and is the actual signal that work resumed.
                break;
            // v0.5.9a1 — live daemon activity. Fires at every phase
            // transition (picking → dispatching → waiting_dispatch →
            // reflecting → tick_pause → parked). The frontend renders
            // a "Currently: <phase> · <ago>" line so the user can tell
            // whether the daemon is actively working, blocked on the
            // sub-mission, parked waiting for them, or just between iters.
            case 'autonomous_activity':
                this.handleAutonomousActivity(event);
                break;
            // v0.5.9a2 — per-iter cost + model attribution. Fires
            // right after autonomous_iteration_complete /
            // _failed; carries tokens, cost, by-model breakdown.
            // Frontend attaches it to the matching iter card so
            // each iter's footer shows what the iter actually cost
            // (with v0.5.8a1's per-specialist routing made visible).
            case 'autonomous_iteration_cost':
                this.handleAutonomousIterationCost(event);
                break;
            // v0.6.3a3 — the skill loader surfaced N skills into this
            // iter's planner context. Render a chip on the iter card
            // so the self-improvement loop's READ side is visible:
            // the user can see which prior-mission skills fed this
            // iter. Fires right after autonomous_iteration_started.
            case 'skill_context_loaded':
                this.handleSkillContextLoaded(event);
                break;
            // v0.5.3a2 — orphan list refresh from server. Sent both
            // automatically (via init / after resume) and on demand
            // (`autonomous_orphans_list` command). Empty array is the
            // happy path; banner hides itself.
            case 'autonomous_orphans':
                this.handleAutonomousOrphans(event);
                break;
            // v0.5.3a3 — sidebar roadmap inspector data. Sent in
            // response to an `autonomous_mission_roadmap` command.
            case 'autonomous_mission_roadmap':
                this.handleAutonomousMissionRoadmap(event);
                break;
            // v0.5.5a2 — sidebar mission browser refresh. Sent on
            // init AND in response to `autonomous_missions_list`.
            case 'autonomous_missions':
                this.handleAutonomousMissions(event);
                break;
            // v0.5.6a1 — backend self-reports operational status (Ollama
            // 503 retries, etc). Distinct from `error` (terminal); the
            // user wants to know "still alive, retrying" rather than
            // staring at a stalled "thinking N s" counter.
            case 'backend.status':
                this.handleBackendStatus(event);
                break;
            case 'await_user':
                // v0.3.5 — agent paused with `await_user` tool, asking
                // a focused question. Render an inline prompt with
                // optional quick-reply chips; the user's reply goes
                // back via the `user_input` WS command and unblocks
                // the agent.
                this.handleAwaitUser(event);
                break;
            case 'tool_permission':
                this.handleToolPermission(event);
                break;
            case 'choices':
                this.handleChoices(event);
                break;
            case 'status_msg':
                this.showStatusMessage(event.message);
                break;
            case 'resume_prompt':
                this.applyResumePrompt(event.prompt || '');
                break;
            case 'sessions_updated':
                this.sessions = event.sessions || [];
                if (event.all_sessions) this.allSessions = event.all_sessions;
                this.currentSessionId = event.current_session_id || '';
                this.renderFilteredSessions();
                this._syncMissionUI();
                break;
            case 'session_cleared':
                // Mission flow: the frontend already rendered the seed
                // feature as a user message before sending mission_start.
                // Wiping the chat here would visually delete it, leaving
                // an empty chat until the model's first reply streams.
                // Skip the wipe in that case — the new (empty) session
                // is already what we're showing.
                if (!event.mission_started) {
                    this.chatMessages.innerHTML = '';
                }
                this.sessions = event.sessions || [];
                if (Array.isArray(event.all_sessions)) {
                    this.allSessions = event.all_sessions;
                }
                this.currentSessionId = event.current_session_id || '';
                // v0.3.4 — when the backend swapped the project context
                // (mission_start with explicit project_path, or any
                // other path-changing path), the new cwd rides on the
                // session_cleared event. Keep `currentCwd` and the
                // chat-header path display in sync. Without this, the
                // header lied for the rest of the app's lifetime
                // (Bug A from v0.3.3 E2E).
                if (event.cwd) {
                    this.currentCwd = event.cwd.replace(/\\/g, '/');
                    this._updateHeaderProjectPath(this.currentCwd);
                }
                this.applySessionRoleUI(event.session_role || this.sessionRole);
                this.renderFilteredSessions();
                this.showChatInterface();
                this._syncMissionUI();
                // v0.3.2 — release the mission_start guard. session_cleared
                // is the success ack for mission_start, so it's safe to let
                // a future click through.
                if (this._missionStartInflight) {
                    this._missionStartInflight = false;
                    if (this._missionStartInflightTimer) {
                        clearTimeout(this._missionStartInflightTimer);
                        this._missionStartInflightTimer = null;
                    }
                }
                break;
            case 'session_forked':
                this.showStatusMessage(`Forked: kept ${event.user_messages_kept} message(s)`);
                break;
            case 'session_replay_events':
                if (event.error) {
                    this.showStatusMessage(`Replay failed: ${event.error}`);
                } else {
                    this.enterReplayMode(event.events || [], event.title || '');
                }
                break;
            case 'session_loaded':
                this.chatMessages.innerHTML = '';
                this.currentSessionId = event.current_session_id || '';
                this.sessions = event.sessions || [];
                this.applySessionRoleUI(event.session_role || 'generator');
                this.sessionRole = event.session_role || this.sessionRole;
                this.renderFilteredSessions();
                this.showChatInterface();
                // Clear preview panel for loaded session
                this.clearPreviewPanel();
                // Replay display events to rebuild the conversation in the UI
                if (event.display_events && event.display_events.length > 0) {
                    this.replayDisplayEvents(event.display_events);
                }
                // Scroll to bottom after replay
                this.chatMessages.scrollTop = this.chatMessages.scrollHeight;
                break;
            case 'harness_state':
                this.harnessState = event.data || null;
                this.updateHarnessBadge();
                this.rerenderHarnessPopoverIfOpen();
                break;
            // ── Plan-graph (organic orchestration) ────────────────────
            case 'plan.snapshot':
                if (window.PlanGraphView) {
                    window.PlanGraphView.render(event.snapshot || event.data);
                    this.openPlanTab(false);  // open without stealing focus
                    this._markPlanTabUnread();
                }
                break;
            case 'plan.event':
                if (window.PlanGraphView) {
                    window.PlanGraphView.applyEvent(event.event_payload || event);
                    this._markPlanTabUnread();
                }
                break;
            case 'plan.checkpoint':
                if (window.PlanGraphView) {
                    window.PlanGraphView.showCheckpoint(event.payload || event);
                    // Checkpoints DO grab focus (an explicit user-attention
                    // moment). The mark-unread is moot since the switch
                    // immediately clears it, but keep it for symmetry in
                    // case the focus call ever becomes optional.
                    this.openPlanTab(true);
                    this._markPlanTabUnread();
                }
                break;
            case 'plan.snapshot_list':
                this._renderSnapshotList(event.intent_id, event.snapshots || []);
                break;
            case 'intent.accepted':
                this._currentIntentId = event.intent_id;
                break;
            case 'intent.started':
                this.showStatusMessage(`Intent started: ${event.text || ''}`);
                break;
            case 'intent.complete':
                this.showStatusMessage(
                    event.extracted_skill_id
                        ? `Intent complete \u00B7 skill saved: ${event.extracted_skill_id}`
                        : 'Intent complete'
                );
                break;
            case 'intent.cancelled':
                this.showStatusMessage('Intent cancelled.');
                break;
            case 'intent.failed':
                this.showStatusMessage(`Intent failed: ${event.error || 'unknown error'}`);
                break;
            case 'intent.paused':
                this.showStatusMessage('Intent paused.');
                break;
            case 'intent.resumed':
                this.showStatusMessage('Intent resumed.');
                break;
            case 'intent.cancel_ack':
            case 'intent.pause_ack':
            case 'intent.resume_ack':
            case 'intent.restore_ack':
                // Acks are silent — the followup intent.* event surfaces the user-visible message.
                break;
            case 'harness_cycle_started':
                this.showStatusMessage(`Started ${event.run?.name || 'harness cycle'}`);
                this.requestHarnessCycleList();
                break;
            case 'harness_cycle_list':
                this.harnessCycles = event.runs || [];
                this.updateHarnessBadge();
                this.updateHarnessCyclePolling();
                if (!this.getActiveHarnessCycle()) {
                    this.send({ command: 'get_harness_state' });
                }
                this.rerenderHarnessPopoverIfOpen();
                break;
            case 'harness_cycle_result':
                this.showStatusMessage(`Harness cycle ${event.run?.status || 'updated'}`);
                break;
            case 'harness_cycle_cancelled':
                this.showStatusMessage(event.success ? 'Cancelled harness cycle' : 'No active harness cycle to cancel');
                break;
            case 'harness_teacher_recovered':
                this.showStatusMessage(
                    event.data?.status_message
                        || `Teacher recovery applied via ${event.data?.teacher_provider || 'teacher'}`
                );
                this.send({ command: 'get_harness_state' });
                break;
            case 'dir_list':
                this.handleDirList(event);
                break;
            case 'folder_picked':
                // Native folder dialog returned a path. v0.3.3 — when
                // the mission composer asked for the picker, route the
                // result there instead of switching the global project
                // (the global switch only happens on Start).
                if (event.path) {
                    if (this._pendingFolderPickConsumer === 'mission') {
                        this._pendingFolderPickConsumer = null;
                        const pathInput = document.getElementById('mission-composer-path');
                        if (pathInput) {
                            pathInput.value = event.path;
                            pathInput.dispatchEvent(new Event('input'));
                            pathInput.focus();
                        }
                        break;
                    }
                    const folderInput = document.getElementById('welcome-folder-input');
                    if (folderInput) folderInput.value = event.path;
                    this.selectProjectFolder(event.path);
                }
                break;
            case 'folder_picker_unavailable':
                // v0.5.6a4 \u2014 replace the old "kick the user to the
                // welcome screen" fallback with an in-place modal text
                // input. Linux-bridge field-observation #3: bouncing
                // back to showNewSessionSetup() abandoned the user's
                // current session and was a dead end in browser mode.
                // Route the typed path through the same consumer
                // dispatcher the native folder_picked handler uses, so
                // mission-composer pickers stay scoped to the composer.
                {
                    const consumer = this._pendingFolderPickConsumer;
                    const label = consumer === 'mission'
                        ? 'Pick mission folder'
                        : 'Switch project';
                    if (event.message) this.showStatusMessage(event.message);
                    this._promptForProjectPath(label, (path) => {
                        if (consumer === 'mission') {
                            this._pendingFolderPickConsumer = null;
                            const pathInput = document.getElementById('mission-composer-path');
                            if (pathInput) {
                                pathInput.value = path;
                                pathInput.dispatchEvent(new Event('input'));
                                pathInput.focus();
                            }
                            return;
                        }
                        this.selectProjectFolder(path);
                    });
                }
                break;
            case 'ollama_probe_result':
                // v0.4.3 (T1.3) — real-time feedback for the Ollama
                // setup wizard's URL probe. Lands BEFORE the init
                // payload that re-renders the welcome flow, so the
                // wizard's hint area can show "✓ Reachable, N models"
                // or "✗ No models / unreachable" before the wizard
                // gets replaced (success) or re-renders (failure).
                this._handleOllamaProbeResult(event);
                break;
            case 'diagnostics_saved':
                // v0.3.4 \u2014 Help \u2192 Save Diagnostics result. Show the path
                // and size so the user knows what to attach to a GitHub
                // issue. The size confirms the bundle isn't empty.
                this._showDiagnosticsToast(event.path || '', event.size_bytes || 0);
                break;
            case 'model_warmup_started':
                // Big cloud / MoE models can take 30-90s to load on first call.
                // Show a banner so the user knows the upcoming "thinking" delay
                // is model-load, not the model being slow.
                this._showWarmupBanner(event.model || event.backend || 'model');
                break;
            case 'model_warmup_complete':
                this._hideWarmupBanner(event.elapsed_s);
                break;
            case 'settings':
                this.settings = event.data || {};
                if (!this.isRunning) {
                    this.setPermissionMode(
                        this.settings.general?.default_permission_mode || this.permissionMode || 'bypass',
                        false
                    );
                }
                this.renderSettingsView();
                break;
            case 'costs':
                // Update cost display
                break;
            case 'git_status':
                this.handleGitStatus(event.data);
                break;
            case 'git_result':
                this.handleGitResult(event);
                break;
            case 'resonant_md':
                this.resonantMd = event.info;
                this.resonantMdContent = event.content || '';
                this.updateResonantMdBadge();
                this._updateResonantMdPopoverContent();
                break;
            case 'context.compression':
                this.handleCompression(event);
                break;
            case 'mcp_list':
                // Refresh settings view if open
                if (this.currentView === 'settings') {
                    this.renderSettingsView();
                }
                break;
            case 'rag_indexed':
                this.ragStats = event;
                this.showStatusMessage(`Indexed ${event.total_files} files in ${event.elapsed_ms}ms`);
                if (this.currentView === 'settings') this.renderSettingsView();
                break;
            case 'rag_results':
                this.handleRagResults(event);
                break;
            case 'rag_stats':
                this.ragStats = event;
                break;
            case 'skill_list':
                this.skills = event.skills || [];
                this.renderFilteredSessions();
                if (this._skillDetailOpenId) {
                    // Refresh open detail dialog if it's still relevant.
                    const stillExists = this.skills.some(s => s.id === this._skillDetailOpenId);
                    if (!stillExists) this.closeSkillDetail();
                }
                break;
            case 'skill_view_data':
                this.handleSkillViewData(event);
                break;
            case 'skill_pin_changed':
                // No-op — list refresh follows immediately.
                break;
            case 'skill_archived':
                this.showStatusMessage(`Archived skill ${event.skill_id}`);
                this.closeSkillDetail();
                break;
            case 'skill_error':
                this.showStatusMessage(event.message || 'Skill operation failed', 'error');
                break;
        }
    }

    handleRagResults(event) {
        const results = event.results || [];
        if (!results.length) {
            this.showStatusMessage('No matching files found');
            return;
        }
        // Show results in a brief status
        const top = results.slice(0, 3).map(r => r.path.split('/').pop()).join(', ');
        this.showStatusMessage(`Found ${results.length} relevant files: ${top}`);
    }

    // ── Init ────────────────────────────────────────────────────

    handleInit(event) {
        const {
            backends,
            current_backend,
            current_model,
            cwd,
            sessions,
            all_sessions,
            current_session_id,
            current_session_mode,
            current_session_role,
            recent_projects,
            refresh_only,
            harness_enabled,
            harness_cycles,
        } = event;

        // Update project info
        if (cwd) {
            const short = cwd.split('/').pop();
            this.headerProject.textContent = short;
            this.headerProject.title = `Project: ${cwd}`;
            if (this.sidebarProjectName) this.sidebarProjectName.textContent = short;
            if (this.sidebarCwd) this.sidebarCwd.textContent = cwd;
            this.currentCwd = cwd;
            this._updateHeaderProjectPath(cwd);
            // Default the sidebar filter to the current project so users immediately
            // see only that project's sessions; clearing it via "All projects" still works.
            if (this.sidebarProjectSwitchLabel && !this._projectFilterUserCleared) {
                this._projectFilter = cwd.replace(/\\/g, '/');
                this.sidebarProjectSwitchLabel.textContent = short;
            }
        }

        // Store backends for later use
        this.backends = backends || {};
        this.handlesTools = event.handles_tools || false;

        if (recent_projects) {
            this.recentProjects = recent_projects;
        }
        // Master switch: when sprint workflow is off, the role selector + harness
        // badge stay hidden and we don't even fetch cycle state. Defaults to false
        // so legacy clients (no field present) match the new opt-in behavior.
        this.harnessEnabled = harness_enabled === true;
        document.body.dataset.harnessEnabled = this.harnessEnabled ? '1' : '0';
        // Surface the one-time migration notice when legacy .resonant-harness/
        // gets copied to ~/.resonant/projects/<hash>/harness/.
        const migrationNotice = (event.harness_migration_notice || '').trim();
        if (migrationNotice && migrationNotice !== this._lastShownMigrationNotice) {
            this._lastShownMigrationNotice = migrationNotice;
            this.showStatusMessage(migrationNotice);
        }
        if (event.harness) {
            this.harnessState = event.harness;
            this.updateHarnessBadge();
        }
        if (harness_cycles) {
            this.harnessCycles = harness_cycles;
            this.updateHarnessBadge();
            this.updateHarnessCyclePolling();
        }

        // Store settings
        if (event.settings) {
            this.settings = event.settings;
        }
        this.setPermissionMode(
            event.permission_mode || this.settings.general?.default_permission_mode || 'bypass',
            false
        );

        // RESONANT.md indicator
        if (event.resonant_md) {
            this.resonantMd = event.resonant_md;
            this.updateResonantMdBadge();
        }

        // RAG index status
        if (event.rag) {
            this.ragStats = event.rag;
        }

        // Fetch git status
        this.requestGitStatus();

        // v0.6.2a3 — Fetch skill list so the Skills sidebar group populates.
        this.requestSkillList();

        if (sessions) {
            this.sessions = sessions;
            this.allSessions = all_sessions || [];
            this.currentSessionId = current_session_id || '';
            this.applySessionRoleUI(current_session_role || this.currentSessionRole || 'generator');
            this.sessionRole = current_session_role || this.sessionRole;
            this.renderFilteredSessions();
        }

        // v0.5.3a2 — Resume orphaned autonomous missions. Server sends
        // the orphan list as part of init (and again on session-switch
        // refresh). Render the banner if any are present.
        if (Array.isArray(event.autonomous_orphans)) {
            this.handleAutonomousOrphans({ orphans: event.autonomous_orphans });
        }

        // v0.5.5a2 — Sidebar mission browser. Server includes the full
        // list (running + complete + paused + failed) on init.
        if (Array.isArray(event.autonomous_missions)) {
            this.handleAutonomousMissions({ missions: event.autonomous_missions });
        }

        if (current_backend) {
            if (!refresh_only) {
                this.showChatInterface();
            }
            this.headerStatus.textContent = `${current_backend} · ${current_model}`;
            this.populateModelSelector(backends, current_backend, current_model);
            this.setThinkingMode(event.current_thinking_mode || '');
            return;
        }

        if (refresh_only) {
            const backendStep = document.getElementById('backend-step');
            if (backendStep && backendStep.style.display !== 'none') {
                this.showBackendSelector(backends);
            }
            return;
        }

        const preferred = this._getPreferredBackendSelection(backends);
        const configuredBackend = this.settings?.general?.default_backend || '';
        if (configuredBackend && preferred?.backend === configuredBackend) {
            this.selectBackend(preferred.backend, preferred.model);
            return;
        }

        // If we're on the backend step (project already selected), refresh backend cards
        const backendStep = document.getElementById('backend-step');
        if (backendStep && backendStep.style.display !== 'none') {
            this.showBackendSelector(backends);
            return;
        }

        // Show new session setup (project picker first)
        this.showNewSessionSetup();
    }

    /**
     * v0.4.0 — Ollama-only welcome flow. Two states:
     *   1. Ollama reachable + has models → flat model picker (deepseek
     *      flagship pinned to top).
     *   2. Ollama unreachable / empty → setup wizard with URL config,
     *      install link, "pull a model" hint.
     * Pre-v0.4.0 this rendered cards for Anthropic / OpenAI / Claude
     * Code / Codex / LM Studio / MLX — all cut.
     */
    showBackendSelector(backends) {
        const list = document.getElementById('backend-list');
        const label = document.querySelector('.backend-label');
        list.innerHTML = '';

        const ollamaInfo = backends && backends.ollama;
        if (!ollamaInfo) {
            this._renderOllamaSetupWizard(list, label);
            return;
        }

        const models = Array.isArray(ollamaInfo.models) ? ollamaInfo.models.slice() : [];
        if (models.length === 0) {
            this._renderOllamaSetupWizard(list, label, {
                reason: 'connected-but-empty',
                url: ollamaInfo.url,
            });
            return;
        }

        label.textContent = 'Pick a model';

        // Pin DeepSeek flagship variants to the top.
        const flagshipOrder = [
            'deepseek-v4-pro:cloud',
            'deepseek-v4-flash:cloud',
            'deepseek-v4:cloud',
        ];
        const pinned = flagshipOrder.filter(m => models.includes(m));
        const others = models.filter(m => !pinned.includes(m));
        const ordered = [...pinned, ...others];

        const card = document.createElement('div');
        card.className = 'backend-group-cards single';

        for (const model of ordered) {
            const isFlagship = flagshipOrder.includes(model);
            const row = document.createElement('div');
            row.className = 'backend-card';
            row.dataset.backend = 'ollama';
            row.dataset.model = model;
            const pills = [];
            if (isFlagship) pills.push('<span class="backend-pill backend-pill-rec">Flagship</span>');
            if (model.endsWith(':cloud')) pills.push('<span class="backend-pill backend-pill-ok">Cloud</span>');
            else pills.push('<span class="backend-pill backend-pill-ok">Local</span>');
            row.innerHTML = `
                <div class="backend-card-icon">🦙</div>
                <div class="backend-card-info">
                    <div class="backend-card-name">${this.escapeHtml(model)}</div>
                    <div class="backend-card-detail">${this.escapeHtml(ollamaInfo.url || 'Ollama')}</div>
                    <div class="backend-card-pills">${pills.join('')}</div>
                </div>
                <div class="backend-card-dot"></div>
            `;
            row.addEventListener('click', () => {
                this.selectBackend('ollama', model);
            });
            card.appendChild(row);
        }
        list.appendChild(card);
    }

    /**
     * v0.4.0 — Ollama setup wizard. URL field persists via
     * `update_settings` and triggers a re-detect, so the user never
     * has to leave the window to get unstuck.
     */
    _renderOllamaSetupWizard(list, label, opts = {}) {
        const reason = opts.reason || 'unreachable';
        const triedUrl = opts.url
            || (this.settings && this.settings.network && this.settings.network.ollama_url)
            || 'http://10.0.0.133:11434';

        label.textContent = 'Set up Ollama';

        const headline = reason === 'connected-but-empty'
            ? 'Ollama is reachable but no models are pulled yet.'
            : 'Resonant needs Ollama. We couldn\'t reach it.';

        const wizard = document.createElement('div');
        wizard.className = 'ollama-wizard';
        wizard.innerHTML = `
            <div class="ollama-wizard-headline">
                <span class="ollama-wizard-icon" aria-hidden="true">🦙</span>
                <span>${this.escapeHtml(headline)}</span>
            </div>
            <p class="ollama-wizard-blurb">
                Resonant Client v0.4.0 is purpose-built for DeepSeek (and other
                open-source models) running through Ollama. If you want
                Anthropic models, reach for <strong>Claude Code</strong>; for
                OpenAI, reach for <strong>Codex</strong>.
            </p>

            <div class="ollama-wizard-step">
                <div class="ollama-wizard-step-title">1. Ollama URL</div>
                <div class="ollama-wizard-row">
                    <input type="text" class="ollama-wizard-url" value="${this.escapeHtml(triedUrl)}"
                        placeholder="http://10.0.0.133:11434" spellcheck="false" autocomplete="off">
                    <button type="button" class="ollama-wizard-test">Test</button>
                </div>
                <div class="ollama-wizard-quick-row">
                    <span class="ollama-wizard-quick-label">Quick fill:</span>
                    <button type="button" class="ollama-wizard-quick" data-url="http://10.0.0.133:11434"
                        title="Mac Studio (canonical Resonant deployment)">Mac Studio</button>
                    <button type="button" class="ollama-wizard-quick" data-url="http://localhost:11434"
                        title="Ollama on this machine (dev / single-host setups)">localhost</button>
                </div>
                <div class="ollama-wizard-hint" id="ollama-wizard-hint">
                    Default: <code>http://10.0.0.133:11434</code> (Mac Studio).
                    Override via <code>OLLAMA_HOST</code> env or fill above.
                </div>
            </div>

            <div class="ollama-wizard-step">
                <div class="ollama-wizard-step-title">2. Install Ollama (if you haven't)</div>
                <div class="ollama-wizard-cmd">
                    <a href="https://ollama.com/download" target="_blank" rel="noopener">Download from ollama.com</a>
                    &middot; then run <code>ollama serve</code>
                </div>
            </div>

            <div class="ollama-wizard-step">
                <div class="ollama-wizard-step-title">3. Pull DeepSeek</div>
                <div class="ollama-wizard-cmd">
                    <code>ollama pull deepseek-v4-pro:cloud</code>
                    <span class="ollama-wizard-cmd-note">— flagship, autonomous-mission default</span>
                </div>
                <div class="ollama-wizard-cmd">
                    <code>ollama pull deepseek-v4-flash:cloud</code>
                    <span class="ollama-wizard-cmd-note">— faster per-call, good for quick chat</span>
                </div>
            </div>
        `;

        // Probe a URL: persist it to settings and re-detect. Used by both
        // the "Test" button (current input value) and the quick-fill chips
        // (the chip's URL is filled into the input first so the user can
        // see what got tried, then probed).
        //
        // v0.4.3 (T1.3) — wires up real-time feedback. Sets
        // `_ollamaProbeInflight` so the `ollama_probe_result` event
        // handler knows to update this wizard's hint, and arms a 7s
        // safety timeout that surfaces "no response" if the backend
        // somehow never emits the event (network stack hung).
        const probeUrl = (newUrl) => {
            const urlInput = wizard.querySelector('.ollama-wizard-url');
            const hint = wizard.querySelector('#ollama-wizard-hint');
            const trimmed = (newUrl || '').trim();
            if (!trimmed) {
                hint.textContent = '⚠ URL is empty.';
                hint.className = 'ollama-wizard-hint ollama-wizard-hint-warn';
                return;
            }
            urlInput.value = trimmed;  // visible feedback for quick-fill
            hint.innerHTML = `<span class="ollama-wizard-spinner" aria-hidden="true"></span>Probing <code>${this.escapeHtml(trimmed)}</code>…`;
            hint.className = 'ollama-wizard-hint';
            this.send({
                command: 'update_settings',
                section: 'network',
                values: { ollama_url: trimmed },
            });
            // Stash a reference to this wizard's hint + a generation token
            // so a stale probe (user clicked twice fast) can't overwrite a
            // fresher result.
            const generation = (this._ollamaProbeGeneration || 0) + 1;
            this._ollamaProbeGeneration = generation;
            this._ollamaProbeInflight = { hint, generation, url: trimmed };
            // Arm a safety timeout — the backend's httpx connect+read
            // budget is ~6s, so 7s is a generous "no response at all"
            // catch. Fires only if `ollama_probe_result` never arrives.
            if (this._ollamaProbeTimeout) clearTimeout(this._ollamaProbeTimeout);
            this._ollamaProbeTimeout = setTimeout(() => {
                if (this._ollamaProbeInflight && this._ollamaProbeInflight.generation === generation) {
                    hint.innerHTML = `✗ No response from <code>${this.escapeHtml(trimmed)}</code> after 7s. Is the URL correct? Is <code>ollama serve</code> running?`;
                    hint.className = 'ollama-wizard-hint ollama-wizard-hint-warn';
                    this._ollamaProbeInflight = null;
                }
            }, 7000);
            setTimeout(() => {
                this.send({ command: 'redetect_backends' });
            }, 400);
        };

        wizard.querySelector('.ollama-wizard-test').addEventListener('click', () => {
            probeUrl(wizard.querySelector('.ollama-wizard-url').value);
        });

        // T1.2 (v0.4.x roadmap): quick-fill chips. Pre-fills the URL field
        // and immediately re-probes — one click gets the user unstuck if
        // the canonical Mac Studio URL didn't work but Ollama is on this
        // machine (or vice versa).
        wizard.querySelectorAll('.ollama-wizard-quick').forEach(btn => {
            btn.addEventListener('click', () => {
                probeUrl(btn.dataset.url);
            });
        });

        list.appendChild(wizard);
    }

    /**
     * v0.4.3 (T1.3) — handle the structured probe result the backend
     * emits BEFORE the init payload. Updates the wizard's hint with
     * a real-time success/failure message. On success the wizard gets
     * replaced by the model picker shortly after (init payload land);
     * on failure the wizard stays put with this hint surfaced.
     *
     * Generation token guards against a stale event landing after
     * the user kicked off a fresher probe — only the inflight
     * generation gets to update the hint.
     */
    _handleOllamaProbeResult(event) {
        if (this._ollamaProbeTimeout) {
            clearTimeout(this._ollamaProbeTimeout);
            this._ollamaProbeTimeout = null;
        }
        const inflight = this._ollamaProbeInflight;
        if (!inflight) return;  // no wizard listening — probably timed out already
        const hint = inflight.hint;
        // Confirm the hint is still in the DOM (wizard might have been
        // re-rendered for a different reason).
        if (!hint || !hint.isConnected) {
            this._ollamaProbeInflight = null;
            return;
        }
        const ok = !!(event && event.ok);
        const url = (event && event.url) || inflight.url || '';
        const count = (event && event.models_count) || 0;
        if (ok && count > 0) {
            hint.innerHTML = `✓ Reachable at <code>${this.escapeHtml(url)}</code> — found ${count} model${count === 1 ? '' : 's'}.`;
            hint.className = 'ollama-wizard-hint ollama-wizard-hint-ok';
        } else if (ok) {
            hint.innerHTML = `✓ Reachable at <code>${this.escapeHtml(url)}</code>, but no models pulled yet. Try <code>ollama pull deepseek-v4-pro:cloud</code>.`;
            hint.className = 'ollama-wizard-hint ollama-wizard-hint-warn';
        } else {
            hint.innerHTML = `✗ <code>${this.escapeHtml(url)}</code> unreachable. Is <code>ollama serve</code> running on that host?`;
            hint.className = 'ollama-wizard-hint ollama-wizard-hint-warn';
        }
        this._ollamaProbeInflight = null;
    }

    showModelPicker(backendType, models, container, card, modelLabels) {
        // Remove existing pickers and deselect cards
        document.querySelectorAll('.model-picker').forEach(el => el.remove());
        document.querySelectorAll('.backend-card.selected').forEach(el => el.classList.remove('selected'));

        card.classList.add('selected');

        const picker = document.createElement('div');
        picker.className = 'model-picker visible';
        const labels = modelLabels || {};

        const row = document.createElement('div');
        row.className = 'model-picker-row';

        const select = document.createElement('select');
        for (const m of models) {
            const opt = document.createElement('option');
            opt.value = m;
            opt.textContent = labels[m] || m;
            select.appendChild(opt);
        }

        const btn = document.createElement('button');
        btn.className = 'connect-btn';
        btn.textContent = 'Connect';
        btn.addEventListener('click', () => {
            this.selectBackend(backendType, select.value);
        });

        // Allow Enter in select to connect
        select.addEventListener('keydown', (e) => {
            if (e.key === 'Enter') this.selectBackend(backendType, select.value);
        });

        row.appendChild(select);
        row.appendChild(btn);
        picker.appendChild(row);
        container.appendChild(picker);
        select.focus();
    }

    selectBackend(backendType, model) {
        document.querySelector('.backend-label').textContent = 'Connecting...';
        const sessionRole = document.getElementById('setup-session-role')?.value || this.sessionRole || 'generator';
        this.sessionRole = sessionRole;
        this.send({
            command: 'select_backend',
            backend: backendType,
            model,
            session_mode: 'code',
            session_role: sessionRole,
        });
    }

    showChatInterface() {
        this.welcomeScreen.style.display = 'none';
        if (this.agentPanel) this.agentPanel.style.display = 'flex';
        else this.chatContainer.style.display = 'flex';
        this.inputBar.style.display = 'flex';
        if (this.settingsView) this.settingsView.style.display = 'none';
        this.currentView = 'agents';
        document.querySelectorAll('.sidebar-icon-btn[data-view], .sidebar-nav-item[data-view], .sidebar-action[data-view]').forEach(el =>
            el.classList.toggle('active', el.dataset.view === 'agents'));
        this._maybeRenderChatEmptyState();
        this.userInput.focus();
    }

    /** First-run onboarding card on the welcome screen. Dismissed permanently via localStorage. */
    _maybeRenderOnboardingCard() {
        try {
            if (localStorage.getItem('resonant_onboarding_seen') === '1') return;
        } catch (_) { /* private mode, etc. — show once anyway */ }

        // Don't render twice
        if (document.getElementById('onboarding-card')) return;

        const projectStep = document.getElementById('project-step');
        if (!projectStep) return;

        const card = document.createElement('div');
        card.id = 'onboarding-card';
        card.className = 'onboarding-card';
        card.innerHTML = `
            <div class="onboarding-header">
                <span class="onboarding-pill">Welcome</span>
                <button class="onboarding-dismiss" aria-label="Dismiss" title="Dismiss">&times;</button>
            </div>
            <h3 class="onboarding-title">A laser-focused agentic IDE</h3>
            <p class="onboarding-sub">Resonant is built around <strong>deepseek-v4 on Ollama</strong> &mdash; pro for autonomous missions, flash for quick chat. High-quality coding without sending your code to the cloud.</p>
            <ul class="onboarding-list">
                <li><span class="onboarding-bullet">⚡</span><span><strong>Batch + sub-agents</strong> &mdash; ask the model to fan out reads or spawn isolated investigations</span></li>
                <li><span class="onboarding-bullet">🔍</span><span><strong>Auto-lint &amp; auto-test on edit</strong> &mdash; toggle in Settings &rarr; General</span></li>
                <li><span class="onboarding-bullet">🛡</span><span><strong>Inline diff review</strong> &mdash; accept/reject edits without a popup</span></li>
                <li><span class="onboarding-bullet">↪</span><span><strong>Fork from any message</strong> &mdash; explore alternate paths without losing your thread</span></li>
            </ul>
            <p class="onboarding-cta">Pick a workspace folder below to get started.</p>
        `;
        card.querySelector('.onboarding-dismiss').addEventListener('click', () => {
            try { localStorage.setItem('resonant_onboarding_seen', '1'); } catch (_) {}
            card.remove();
        });
        projectStep.parentNode.insertBefore(card, projectStep);
    }

    /** Render an empty-state card in the chat panel when there are no messages yet. */
    _maybeRenderChatEmptyState() {
        if (!this.chatMessages) return;
        // Only render when chat is genuinely empty (no messages, no replays)
        if (this.chatMessages.children.length > 0) return;
        const empty = document.createElement('div');
        empty.className = 'chat-empty-state';
        const projectShort = (this.currentCwd || '').replace(/\\/g, '/').split('/').pop() || 'this project';
        empty.innerHTML = `
            <div class="chat-empty-glyph">┃</div>
            <h2 class="chat-empty-title">Ready when you are.</h2>
            <p class="chat-empty-sub">Type your request below. <kbd>Enter</kbd> to send, <kbd>Shift+Enter</kbd> for newline.</p>
            <div class="chat-empty-suggestions">
                <button class="chat-suggestion" data-prompt="Summarize the structure of this codebase in 5 bullets.">
                    <span class="chat-suggestion-label">Explore the codebase</span>
                    <span class="chat-suggestion-hint">Quick architecture summary of ${this.escapeHtml(projectShort)}</span>
                </button>
                <button class="chat-suggestion" data-prompt="Use git_status to show what's currently changed in this repo.">
                    <span class="chat-suggestion-label">Check git status</span>
                    <span class="chat-suggestion-hint">First-class git tool, structured output</span>
                </button>
                <button class="chat-suggestion" data-prompt="Run the test suite and tell me what failed.">
                    <span class="chat-suggestion-label">Run the tests</span>
                    <span class="chat-suggestion-hint">Then iterate on any failures</span>
                </button>
            </div>
            <div class="chat-empty-mission">
                <button type="button" class="chat-empty-mission-btn" data-action="start-mission">
                    <span class="chat-empty-mission-icon" aria-hidden="true">🎯</span>
                    <span class="chat-empty-mission-text">
                        <span class="chat-empty-mission-label">Or start a Mission</span>
                        <span class="chat-empty-mission-hint">Get interviewed about a feature, then dispatch a plan-graph to build it.</span>
                    </span>
                </button>
            </div>
        `;
        empty.addEventListener('click', (ev) => {
            // v0.3.2 — Mission entry from empty state. Discoverable surface
            // for users who don't know about the chat-header toggle yet.
            if (ev.target.closest('[data-action="start-mission"]')) {
                this.openMissionComposer();
                return;
            }
            const btn = ev.target.closest('.chat-suggestion');
            if (!btn) return;
            this.userInput.value = btn.dataset.prompt || '';
            this.userInput.focus();
        });
        this.chatMessages.appendChild(empty);
    }

    /** Remove the empty-state card the moment a real message lands. */
    _removeChatEmptyState() {
        const el = this.chatMessages?.querySelector('.chat-empty-state');
        if (el) el.remove();
    }

    setPermissionMode(mode, notifyServer = true) {
        this.permissionMode = mode;

        // Use a shield (🛡) for the safe sandboxed default instead of a triangle —
        // the triangle reads as a warning glyph and looks alarming on the safe path.
        const icons = { ask: '⚙', 'auto-edit': '✎', plan: '☰', bypass: '🛡' };
        const labels = { ask: 'Ask', 'auto-edit': 'Auto-edit', plan: 'Plan', bypass: 'Full-auto' };

        document.getElementById('perm-icon').textContent = icons[mode] || '🛡';
        document.getElementById('perm-label').textContent = labels[mode] || mode;
        // Tooltip on the toggle reflects the current mode + a one-line explanation
        const tooltips = {
            ask: 'Ask permissions — confirm before every change',
            'auto-edit': 'Auto-edit — files OK, shell asks',
            plan: 'Plan mode — propose a plan before acting',
            bypass: 'Full-auto (sandboxed) — accept all changes inside the project',
        };
        const toggle = document.getElementById('permission-toggle');
        if (toggle) toggle.title = tooltips[mode] || 'Permission mode';

        // Update active state + checkmark
        document.querySelectorAll('.perm-option').forEach(opt => {
            const isActive = opt.dataset.mode === mode;
            opt.classList.toggle('active', isActive);
            // Remove existing checkmarks
            const existingCheck = opt.querySelector('.perm-check');
            if (existingCheck) existingCheck.remove();
            // Add checkmark to active
            if (isActive) {
                const check = document.createElement('span');
                check.className = 'perm-check';
                check.textContent = '✓';
                opt.appendChild(check);
            }
        });

        // Apply mode effects
        this.planMode = (mode === 'plan');

        // Notify server of permission mode change
        if (notifyServer) {
            this.send({ command: 'set_permission_mode', mode });
        }
    }

    _getModelGroups() {
        // v0.4.0 — single backend, single group. Old multi-backend
        // grouping (Local / Subscriptions / APIs) is gone.
        return {
            ollama: {
                label: 'Ollama',
                backends: ['ollama'],
            },
        };
    }

    _getBackendLabels() {
        return { ollama: 'Ollama' };
    }

    _getPreferredBackendSelection(backends) {
        // v0.4.0 — Ollama-only. Pick the configured default model if
        // it's pulled, otherwise the first deepseek flagship variant
        // we can find, otherwise the first available model.
        if (!backends?.ollama?.models?.length) return null;
        const models = backends.ollama.models;
        const configuredModel = this.settings?.general?.default_model || '';
        if (configuredModel && models.includes(configuredModel)) {
            return { backend: 'ollama', model: configuredModel };
        }
        // v0.6.2 — pro is the autonomous-mission flagship (see
        // network_defaults.py:get_default_model). Flash is the fast-iter
        // fallback when pro isn't available.
        for (const flagship of ['deepseek-v4-pro:cloud', 'deepseek-v4-flash:cloud', 'deepseek-v4:cloud']) {
            if (models.includes(flagship)) return { backend: 'ollama', model: flagship };
        }
        return { backend: 'ollama', model: models[0] };
    }

    _populateSelectWithGroupedModels(selectEl, backends, currentBackend, currentModel) {
        selectEl.innerHTML = '';
        const groups = this._getModelGroups();
        const backendLabels = this._getBackendLabels();
        const placed = new Set();
        const preferred = this._getPreferredBackendSelection(backends);
        const effectiveBackend = currentBackend || preferred?.backend || '';
        const effectiveModel = currentModel || preferred?.model || '';

        for (const group of Object.values(groups)) {
            // Collect all models in this category
            const categoryOptions = [];
            for (const key of group.backends) {
                const info = backends[key];
                if (!info || !info.models || info.models.length === 0) continue;
                placed.add(key);
                const labels = info.model_labels || {};
                const bLabel = backendLabels[key] || key;
                for (const m of info.models) {
                    categoryOptions.push({
                        value: `${key}:${m}`,
                        text: `${labels[m] || m}`,
                        prefix: bLabel,
                        isSelected: key === effectiveBackend && m === effectiveModel,
                    });
                }
            }
            if (categoryOptions.length === 0) continue;

            const optgroup = document.createElement('optgroup');
            optgroup.label = group.label;
            for (const o of categoryOptions) {
                const opt = document.createElement('option');
                opt.value = o.value;
                // Show backend prefix if multiple backends in this group
                const backendsInGroup = group.backends.filter(k => backends[k]?.models?.length > 0);
                opt.textContent = backendsInGroup.length > 1 ? `${o.prefix} · ${o.text}` : o.text;
                if (o.isSelected) opt.selected = true;
                optgroup.appendChild(opt);
            }
            selectEl.appendChild(optgroup);
        }

        // Any backends not categorized above
        for (const key of Object.keys(backends)) {
            if (placed.has(key)) continue;
            const info = backends[key];
            if (!info || !info.models || info.models.length === 0) continue;
            const labels = info.model_labels || {};
            const bLabel = backendLabels[key] || key;
            const optgroup = document.createElement('optgroup');
            optgroup.label = bLabel;
            for (const m of info.models) {
                const opt = document.createElement('option');
                opt.value = `${key}:${m}`;
                opt.textContent = labels[m] || m;
                if (key === effectiveBackend && m === effectiveModel) opt.selected = true;
                optgroup.appendChild(opt);
            }
            selectEl.appendChild(optgroup);
        }
    }

    populateModelSelector(backends, currentBackend, currentModel) {
        this._populateSelectWithGroupedModels(this.modelSelector, backends, currentBackend, currentModel);
        this._refreshThinkingModeVisibility();
    }

    /** Show the thinking-mode selector only for deepseek-v* models on Ollama. */
    _refreshThinkingModeVisibility() {
        if (!this.thinkingModeSelector) return;
        const val = (this.modelSelector && this.modelSelector.value) || '';
        const colonIdx = val.indexOf(':');
        const backend = colonIdx > 0 ? val.substring(0, colonIdx) : '';
        const model = colonIdx > 0 ? val.substring(colonIdx + 1) : '';
        const supports = backend === 'ollama' && /^deepseek-v\d/i.test(model || '');
        this.thinkingModeSelector.style.display = supports ? '' : 'none';
    }

    /**
     * Push-to-talk voice input via the browser SpeechRecognition API.
     *
     * Hold the mic button → start recognition; show interim results in
     * the textarea (greyed); release → final transcript replaces the
     * grey text. User can edit before submitting.
     *
     * Falls back gracefully when SpeechRecognition isn't available
     * (e.g. desktop pywebview without WebView2 speech support).
     */
    _setupVoiceInput() {
        const btn = document.getElementById('mic-btn');
        if (!btn) return;

        const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
        if (!SpeechRecognition) {
            btn.disabled = true;
            btn.title = 'Voice input not supported (try a Chromium browser, or wire whisper.cpp on the desktop)';
            btn.style.opacity = 0.4;
            return;
        }

        let recognition = null;
        let active = false;
        let baseText = '';
        let interim = '';

        const start = (e) => {
            e.preventDefault();
            if (active) return;
            active = true;
            btn.classList.add('recording');
            baseText = this.userInput.value;
            interim = '';

            recognition = new SpeechRecognition();
            recognition.continuous = false;
            recognition.interimResults = true;
            recognition.lang = navigator.language || 'en-US';

            recognition.addEventListener('result', (event) => {
                let finalT = '';
                let interimT = '';
                for (let i = event.resultIndex; i < event.results.length; i++) {
                    const res = event.results[i];
                    if (res.isFinal) finalT += res[0].transcript;
                    else interimT += res[0].transcript;
                }
                if (finalT) {
                    baseText = (baseText + (baseText && !baseText.endsWith(' ') ? ' ' : '') + finalT).trimStart();
                }
                interim = interimT;
                this.userInput.value = baseText + (interim ? (baseText && !baseText.endsWith(' ') ? ' ' : '') + interim : '');
                this.userInput.style.height = 'auto';
                this.userInput.style.height = Math.min(this.userInput.scrollHeight, 200) + 'px';
            });

            recognition.addEventListener('error', (event) => {
                this.showStatusMessage(`Speech: ${event.error || 'error'}`);
                stop();
            });

            recognition.addEventListener('end', () => {
                // Browser may end on its own (silence). Settle final text.
                if (interim) {
                    baseText = (baseText + (baseText && !baseText.endsWith(' ') ? ' ' : '') + interim).trimStart();
                    interim = '';
                    this.userInput.value = baseText;
                }
                stop();
            });

            try {
                recognition.start();
            } catch (err) {
                this.showStatusMessage(`Speech start failed: ${err}`);
                stop();
            }
        };

        const stop = () => {
            if (!active) return;
            active = false;
            btn.classList.remove('recording');
            if (recognition) {
                try { recognition.stop(); } catch (_) {}
                recognition = null;
            }
            this.userInput.focus();
        };

        // Push-to-talk: mousedown / touchstart starts; mouseup / leave / touchend stops.
        btn.addEventListener('mousedown', start);
        btn.addEventListener('touchstart', start, { passive: false });
        btn.addEventListener('mouseup', stop);
        btn.addEventListener('mouseleave', stop);
        btn.addEventListener('touchend', stop);
        btn.addEventListener('touchcancel', stop);
    }

    /** Sync the thinking-mode selector with server state (called from init/session_loaded). */
    setThinkingMode(mode) {
        if (!this.thinkingModeSelector) return;
        const value = mode || '';
        this.thinkingModeSelector.value = value;
        this._lastThinkingMode = value;
    }

    // ── Step Handling ────────────────────────────────────────────

    handleSessionStart(event) {
        // Show tool mode indicator for adaptive backends
        const toolMode = event.tool_mode || 'native';
        if (toolMode === 'text') {
            this.showStatusMessage(`⚡ Using text-based tool calling for ${event.model || 'this model'}`);
        }
        // Store tool mode for potential UI use
        this.currentToolMode = toolMode;
    }

    handleStepStart(event) {
        this.currentStepEvent = event;
        this.stepToolCalls = [];
        this.stepToolResults = [];
        this.stepIsInlineOnly = true;
        this.stepRendered = false;
        this.isStreaming = false;
        this._currentStepHeaderEl = null;
        this._currentStepToolCounts = {};

        // Skip the thinking indicator if a live tool group is already on screen —
        // its spinner already signals the agent is working. Re-adding here causes
        // a flicker between every consecutive inline-only step.
        if (this._liveCollapsedGroup) return;

        this.removeThinking();
        this.addThinking();
    }

    /** Fresh aggregator object for a new turn — accumulates step stats so we
     *  can render a single dim "▣ model · tokens · elapsed" footer beneath
     *  the assistant's prose instead of repeating one per step. */
    _freshTurnAggregate() {
        return {
            totalElapsed: 0,
            totalInputTokens: 0,
            totalOutputTokens: 0,
            stepCount: 0,
            toolCallCount: 0,    // UX fix #7 — honest tool-call count in run-card
            footerEl: null,      // set lazily on session.end render
        };
    }

    ensureStepRendered() {
        // Step headers no longer render at all in the default view — agent
        // activity is presented as a flat list of tool rows + assistant
        // prose. We still need to flush any pending inline-tool group when
        // a block tool / text starts, so non-inline-only steps don't get
        // visually fused with the prior inline streak.
        if (!this.stepRendered && this.currentStepEvent) {
            this.flushCollapsedGroup();
            // Buffered inline tools (rare — they'd only buffer if their
            // group hasn't been live-rendered yet) flush now too.
            for (const tc of this.stepToolCalls) {
                this.renderToolCall(tc);
            }
            for (const tr of this.stepToolResults) {
                this.renderToolResult(tr);
            }
            this.stepRendered = true;
        }
    }

    /** No-op (kept as an empty stub for replay paths that still call it). */
    renderStepHeader(_event) { /* step decoration removed in compact view */ }

    /** No-op (kept as an empty stub — labels live on the live-group header now). */
    updateStepActionLabel() { /* step decoration removed in compact view */ }

    handleStepEnd(event) {
        // Accumulate per-step stats into the per-turn aggregate. The single
        // dim footer at session.end uses these instead of repeating a
        // ▣ model · tokens · elapsed line after every step.
        const t = this._currentTurn;
        if (t) {
            t.totalElapsed += (event.elapsed || 0);
            t.stepCount += 1;
            if (this.lastStats) {
                t.totalInputTokens += (this.lastStats.input_tokens || 0);
                t.totalOutputTokens += (this.lastStats.output_tokens || 0);
            }
        }

        if (this.stepIsInlineOnly && this.stepToolCalls.length > 0) {
            // Inline-only step → keep data buffer in sync (used on session-end /
            // replay). The live group is already on screen; just bump the step
            // range in its header so "step 2" becomes "steps 2–3" as more
            // inline-only steps complete.
            this.collapsedGroup.push({
                stepEvent: this.currentStepEvent,
                toolCalls: [...this.stepToolCalls],
                toolResults: [...this.stepToolResults],
                endEvent: event,
                model: this.lastModel,
                stats: this.lastStats,
            });
            if (this._liveCollapsedGroup) {
                this._liveCollapsedGroup.lastStep = event.step ?? this._liveCollapsedGroup.lastStep;
                this._updateLiveCollapsedHeader();
            }
            // Don't yank the thinking indicator (or the live spinner) — the
            // model is about to start the next step.
        } else if (this.stepRendered) {
            // Per-step footer no longer renders — the single per-turn footer
            // at session.end carries the aggregated stats.
            this.removeThinking();
        } else {
            // Step had no rendered output (no tools, no text) — clear thinking
            this.removeThinking();
        }
    }

    /** No-op stub kept so any older replay code paths don't break. */
    renderStepFooter(_event) { /* per-step footer removed in compact view */ }

    /**
     * Render the single dim per-turn footer beneath the assistant's prose.
     * Format: `▣ model · 1234→340 tok · 4.7s`. Called once at session.end
     * (after the last text.done has flushed the assistant message), so it
     * sits between prose and the run-card.
     */
    _renderTurnFooter() {
        const t = this._currentTurn;
        if (!t || (t.stepCount === 0 && !this.lastModel)) return;

        const parts = [];
        if (this.lastModel) parts.push(this.lastModel);
        if (t.totalInputTokens || t.totalOutputTokens) {
            parts.push(`${t.totalInputTokens}→${t.totalOutputTokens} tok`);
        }
        if (t.totalElapsed > 0) parts.push(`${t.totalElapsed.toFixed(1)}s`);
        if (parts.length === 0) return;

        const el = document.createElement('div');
        el.className = 'turn-footer';
        el.innerHTML = `▣ ${parts.map((p) => `<span>${this.escapeHtml(p)}</span>`).join('<span class="sep">·</span>')}`;
        this.chatMessages.appendChild(el);
        t.footerEl = el;
    }

    // ── Collapsed Group ─────────────────────────────────────────
    //
    // Inline-only tool calls (file_read / glob / grep / etc.) used to be
    // buffered and rendered as a single collapsed card the *next* time a
    // step needed to render — which made the card pop in late and caused
    // visible flicker as the thinking indicator and step headers cycled
    // around it. We now render the card immediately when the first inline
    // tool arrives and append items live as more come in. flushCollapsedGroup
    // therefore just finalizes (removes the .running class) — the legacy
    // bulk-render path is kept as a fallback for any edge case where a
    // collapsedGroup data buffer somehow ends up populated without a
    // matching live DOM (defensive only).

    flushCollapsedGroup() {
        if (this._liveCollapsedGroup) {
            this._finalizeLiveCollapsedGroup();
            return;
        }
        if (this.collapsedGroup.length === 0) return;
        // Defensive fallback: bulk-render from buffered data.
        for (const g of this.collapsedGroup) {
            for (let i = 0; i < g.toolCalls.length; i++) {
                this._appendToLiveCollapsedGroup(g.toolCalls[i], g.stepEvent);
                const r = g.toolResults[i];
                if (r) this._updateLiveCollapsedItemResult(r);
            }
            if (this._liveCollapsedGroup && g.endEvent) {
                this._liveCollapsedGroup.lastStep = g.endEvent.step ?? this._liveCollapsedGroup.lastStep;
            }
        }
        this._updateLiveCollapsedHeader();
        this._finalizeLiveCollapsedGroup();
    }

    /**
     * Append (or create) the live collapsed-group DOM for an inline tool
     * call. The card is marked .running while in flight and finalized when
     * a block tool, text streaming, error, or session end arrives.
     */
    _appendToLiveCollapsedGroup(callEvent, stepEvent) {
        const stepRef = stepEvent || this.currentStepEvent || {};
        const stepNum = stepRef.step ?? 0;

        if (!this._liveCollapsedGroup) {
            // Start expanded so the user can SEE what the agent is doing in
            // real time. We auto-collapse it the moment the assistant starts
            // streaming prose (signal that tool work is winding down) — that
            // happens in _finalizeLiveCollapsedGroup, which is called from
            // text.delta / block-tool / session-end / error paths.
            const container = document.createElement('div');
            container.className = 'collapsed-group running expanded';

            const header = document.createElement('div');
            header.className = 'collapsed-header';
            header.innerHTML = `
                <span class="collapsed-icon">▾</span>
                <span class="collapsed-summary">◆ Working...</span>
                <span class="collapsed-meta">step ${stepNum}</span>
            `;

            const items = document.createElement('div');
            items.className = 'collapsed-items';

            header.addEventListener('click', () => {
                container.classList.toggle('expanded');
                header.querySelector('.collapsed-icon').textContent =
                    container.classList.contains('expanded') ? '▾' : '▸';
            });

            container.appendChild(header);
            container.appendChild(items);
            this.getRenderTarget().appendChild(container);

            this._liveCollapsedGroup = {
                container, header, items,
                firstStep: stepNum,
                lastStep: stepNum,
                toolCounts: {},
                callIdToItem: new Map(),
                callCount: 0,
            };
        }

        const live = this._liveCollapsedGroup;
        live.lastStep = Math.max(live.lastStep, stepNum);
        live.callCount++;

        const name = callEvent.name || '';
        const args = callEvent.arguments || {};
        const info = getToolInfo(name);
        live.toolCounts[name] = (live.toolCounts[name] || 0) + 1;

        let desc = '';
        if (name === 'file_read') {
            const p = args.path || '';
            desc = `<span style="color:var(--file)">${this.escapeHtml(this.shortenPath(p))}</span>`;
        } else if (name === 'glob') {
            desc = this.escapeHtml(args.pattern || '');
        } else if (name === 'grep') {
            desc = `'${this.escapeHtml(args.pattern || '')}'`;
        } else {
            desc = this.escapeHtml(info.label);
        }

        const line = document.createElement('div');
        line.className = 'tool-inline pending';
        line.innerHTML = `
            <span class="tool-icon" style="color:var(--${info.color})">${info.icon}</span>
            <span class="tool-desc">${desc}</span>
            <span class="tool-meta"></span>
            <span class="tool-status" style="color:var(--muted)">…</span>
        `;
        live.items.appendChild(line);

        const callId = callEvent.call_id || '';
        if (callId) {
            live.callIdToItem.set(callId, line);
        } else {
            // Fallback: track latest item for tools that don't emit a call_id
            live._lastItem = line;
            live._lastItemTool = name;
        }

        this._updateLiveCollapsedHeader();
        this.scrollToBottom();
    }

    /** Update an in-flight live item with result metadata (line counts, error state). */
    _updateLiveCollapsedItemResult(resultEvent) {
        if (!this._liveCollapsedGroup) return;
        const live = this._liveCollapsedGroup;
        const name = resultEvent.name || '';
        const callId = resultEvent.call_id || '';

        let line = null;
        if (callId && live.callIdToItem.has(callId)) {
            line = live.callIdToItem.get(callId);
            live.callIdToItem.delete(callId);
        } else if (live._lastItem && live._lastItemTool === name) {
            line = live._lastItem;
            live._lastItem = null;
            live._lastItemTool = null;
        }
        if (!line) return;

        const meta = resultEvent.metadata || {};
        const isError = resultEvent.is_error || false;

        let metaText = '';
        if (name === 'file_read' && meta.lines) metaText = `${meta.lines} lines`;
        else if (name === 'glob' && meta.count != null) metaText = `${meta.count} files`;
        else if (name === 'grep' && meta.count != null) metaText = `${meta.count} matches`;

        const metaEl = line.querySelector('.tool-meta');
        if (metaEl) metaEl.textContent = metaText;

        const statusEl = line.querySelector('.tool-status');
        if (statusEl) {
            statusEl.textContent = isError ? '✗' : '✓';
            statusEl.style.color = isError ? 'var(--err)' : 'var(--ok)';
        }
        line.classList.remove('pending');
    }

    /** Refresh the live group's summary label, step range, and call count. */
    _updateLiveCollapsedHeader() {
        if (!this._liveCollapsedGroup) return;
        const live = this._liveCollapsedGroup;
        const summaryEl = live.header.querySelector('.collapsed-summary');
        const metaEl = live.header.querySelector('.collapsed-meta');
        if (summaryEl) {
            const action = inferActionLabel(live.toolCounts);
            summaryEl.textContent = `◆ ${action}`;
        }
        if (metaEl) {
            const stepMeta = live.firstStep === live.lastStep
                ? `step ${live.firstStep}`
                : `steps ${live.firstStep}–${live.lastStep}`;
            metaEl.textContent = `${stepMeta} · ${live.callCount} call${live.callCount === 1 ? '' : 's'}`;
        }
    }

    /**
     * Finalize the live group: drop the .running class so its spinner stops,
     * and auto-collapse it (the .expanded class) so the chat doesn't keep
     * the now-static tool list taking up vertical space. The user can
     * still re-expand it with a click. Called from text.delta (assistant
     * is now writing prose), block-tool starts, session.end, and error.
     */
    _finalizeLiveCollapsedGroup() {
        if (this._liveCollapsedGroup) {
            const live = this._liveCollapsedGroup;
            live.container.classList.remove('running');
            live.container.classList.remove('expanded');
            const icon = live.header && live.header.querySelector('.collapsed-icon');
            if (icon) icon.textContent = '▸';
            this._liveCollapsedGroup = null;
        }
        this.collapsedGroup = [];
    }

    // ── Tool Activity Group (CLI backends) ─────────────────────

    addToToolActivityGroup(event) {
        const name = event.name || '';
        const args = event.arguments || {};
        const info = getToolInfo(name);

        // Stop any active streaming cursor — tool calls arrived mid-stream
        if (this.isStreaming && this.currentMessageEl) {
            this.isStreaming = false;
            this.renderMarkdown(this.currentMessageEl, this.streamBuffer);
            this.currentMessageEl.querySelector('.message-content')?.classList.remove('streaming-cursor');
            this.currentMessageEl = null;
        }

        // Create group container if not yet present
        if (!this.activeToolGroup) {
            this.activeToolGroupCount = 0;
            this.activeToolGroupCounts = {};

            const container = document.createElement('div');
            container.className = 'tool-activity-group running';

            const header = document.createElement('div');
            header.className = 'tool-activity-header';
            header.innerHTML = `
                <span class="tool-activity-chevron">▸</span>
                <span class="tool-activity-spinner"></span>
                <span class="tool-activity-done-icon">✓</span>
                <span class="tool-activity-label">Working...</span>
                <span class="tool-activity-count">0</span>
            `;

            const items = document.createElement('div');
            items.className = 'tool-activity-items';

            header.addEventListener('click', () => {
                container.classList.toggle('expanded');
                header.querySelector('.tool-activity-chevron').textContent =
                    container.classList.contains('expanded') ? '▾' : '▸';
            });

            container.appendChild(header);
            container.appendChild(items);
            this.getRenderTarget().appendChild(container);
            this.activeToolGroup = container;
        }

        // Add item to the group
        this.activeToolGroupCount++;
        this.activeToolGroupCounts[name] = (this.activeToolGroupCounts[name] || 0) + 1;

        // Build detail text
        let detail = '';
        if (name === 'bash') {
            const cmd = args.command || '';
            detail = cmd.length > 80 ? cmd.slice(0, 77) + '...' : cmd;
        } else if (name === 'file_read') {
            detail = args.path || '';
        } else if (name === 'file_edit') {
            detail = args.path || '';
        } else if (name === 'file_write') {
            detail = args.path || '';
        } else if (name === 'grep') {
            detail = `'${args.pattern || ''}' in ${args.path || '.'}`;
        } else if (name === 'glob') {
            detail = args.pattern || '';
        } else {
            detail = JSON.stringify(args).slice(0, 60);
        }

        const item = document.createElement('div');
        item.className = 'tool-activity-item';
        item.innerHTML = `
            <span class="ta-icon" style="color:var(--${info.color})">${info.icon}</span>
            <span class="ta-name">${info.label}</span>
            <span class="ta-detail">${this.escapeHtml(detail)}</span>
        `;

        const itemsContainer = this.activeToolGroup.querySelector('.tool-activity-items');
        itemsContainer.appendChild(item);

        // Update header summary
        const summaryParts = [];
        const order = ['bash', 'file_read', 'file_edit', 'file_write', 'grep', 'glob'];
        const shown = new Set();
        for (const k of order) {
            if (this.activeToolGroupCounts[k]) {
                const i = getToolInfo(k);
                summaryParts.push(`${i.label} ×${this.activeToolGroupCounts[k]}`);
                shown.add(k);
            }
        }
        for (const [k, c] of Object.entries(this.activeToolGroupCounts)) {
            if (!shown.has(k)) {
                const i = getToolInfo(k);
                summaryParts.push(`${i.label} ×${c}`);
            }
        }

        const actionLabel = inferActionLabel(this.activeToolGroupCounts);
        this.activeToolGroup.querySelector('.tool-activity-label').textContent = actionLabel;
        this.activeToolGroup.querySelector('.tool-activity-count').textContent =
            summaryParts.join(' · ');

        // Scroll to keep visible
        this.scrollToBottom();
    }

    finalizeToolActivityGroup() {
        if (!this.activeToolGroup) return;

        this.activeToolGroup.classList.remove('running');
        this.activeToolGroup = null;
        this.activeToolGroupCount = 0;
        this.activeToolGroupCounts = {};
    }

    // ── Text Streaming ──────────────────────────────────────────

    handleTextDelta(event) {
        this.removeThinking();
        this.stepIsInlineOnly = false;

        // Text streaming means tools are done — clear terminal bar
        if (this.activeTerminals.size > 0) {
            this.clearTerminals();
        }

        // Finalize tool activity group before text appears
        this.finalizeToolActivityGroup();

        if (!this.isStreaming) {
            this.isStreaming = true;
            this.streamBuffer = '';
            this.ensureStepRendered();
            this.currentMessageEl = this.addAssistantMessage();
        }

        this.streamBuffer += (event.delta || '');
        this.scheduleRender();
    }

    handleTextDone(event) {
        // Cancel any pending throttled re-parse — we're about to do the
        // final, fully-formatted parse and we don't want a stale partial
        // re-parse to land after it.
        if (this._renderTimer) {
            clearTimeout(this._renderTimer);
            this._renderTimer = null;
        }
        if (this.isStreaming && this.currentMessageEl) {
            this.isStreaming = false;
            const finalText = (event.text || this.streamBuffer || '').trim();
            this.streamBuffer = finalText;
            // streaming=false on the final parse runs hljs + decorates code
            // blocks with copy buttons (skipped during streaming for perf).
            this.renderMarkdown(this.currentMessageEl, finalText);
            this.currentMessageEl.querySelector('.message-content')?.classList.remove('streaming-cursor');
        }
    }

    scheduleRender() {
        // Re-parsing the entire markdown buffer on every animation frame is
        // O(n²) for long responses — a 50KB stream would re-parse 50KB on
        // every frame ~60×/sec. Throttle to ~12fps; the final fully-formatted
        // parse runs unconditionally on text.done.
        if (this._renderTimer) return;
        const now = performance.now();
        const elapsed = now - (this._lastStreamParseAt || 0);
        const wait = Math.max(16, 80 - elapsed);
        this._renderTimer = setTimeout(() => {
            this._renderTimer = null;
            this._lastStreamParseAt = performance.now();
            if (this.currentMessageEl) {
                this.renderMarkdown(this.currentMessageEl, this.streamBuffer, true);
            }
        }, wait);
    }

    renderMarkdown(el, text, streaming = false) {
        const contentEl = el.querySelector('.message-content');
        if (!contentEl) return;

        try {
            let html = '';
            if (typeof marked !== 'undefined') {
                html = marked.parse(text);
            } else {
                html = text.replace(/\n/g, '<br>');
            }

            if (typeof DOMPurify !== 'undefined') {
                html = DOMPurify.sanitize(html);
            }

            contentEl.innerHTML = html;

            if (streaming) {
                contentEl.classList.add('streaming-cursor');
            } else {
                contentEl.classList.remove('streaming-cursor');
            }

            // Defer expensive syntax highlighting + copy-button decoration
            // until streaming completes — running hljs on every code block
            // on every throttled re-parse still adds up on long responses,
            // and the user can't read or copy the code until it's stable
            // anyway.
            if (!streaming) {
                contentEl.querySelectorAll('pre code').forEach(block => {
                    if (typeof hljs !== 'undefined') {
                        hljs.highlightElement(block);
                    }
                });
                this._decorateCodeBlocks(contentEl);
                this._decorateMissionSpec(contentEl);
            }
        } catch (err) {
            contentEl.textContent = text;
        }

        this.scrollToBottom();
    }

    /**
     * Add a hover-revealed copy button to each `<pre>` block in a rendered
     * message. Idempotent — if the button already exists, leaves it alone.
     */
    _decorateCodeBlocks(contentEl) {
        const copyIcon = '<svg width="13" height="13" viewBox="0 0 14 14" fill="none" aria-hidden="true">'
            + '<rect x="4" y="4" width="8" height="8" rx="1.2" stroke="currentColor" stroke-width="1.1"/>'
            + '<path d="M10 4V2.8A.8.8 0 0 0 9.2 2H2.8a.8.8 0 0 0-.8.8v6.4a.8.8 0 0 0 .8.8H4" stroke="currentColor" stroke-width="1.1"/>'
            + '</svg>';
        contentEl.querySelectorAll('pre').forEach((pre) => {
            if (pre.querySelector(':scope > .code-copy-btn')) return;
            const btn = document.createElement('button');
            btn.type = 'button';
            btn.className = 'code-copy-btn';
            btn.title = 'Copy code';
            btn.setAttribute('aria-label', 'Copy code');
            btn.innerHTML = copyIcon;
            btn.addEventListener('click', (e) => {
                e.stopPropagation();
                const code = pre.querySelector('code')?.textContent || pre.textContent || '';
                if (!navigator.clipboard?.writeText) return;
                navigator.clipboard.writeText(code).then(() => {
                    btn.classList.add('copied');
                    btn.innerHTML = '✓';
                    setTimeout(() => {
                        btn.classList.remove('copied');
                        btn.innerHTML = copyIcon;
                    }, 1200);
                }).catch(() => {});
            });
            pre.appendChild(btn);
        });
    }

    /**
     * B3 fix — when an assistant message contains a `## Final spec`
     * heading (Mission drafting phase output), wrap the heading + all
     * following content into a `.mission-spec-card` container. CSS then
     * styles the result as a structured spec card instead of a wall of
     * bold-on-newline labels. Idempotent: if the card already exists
     * (re-render path), do nothing.
     */
    _decorateMissionSpec(contentEl) {
        const headings = contentEl.querySelectorAll('h2');
        let specHeading = null;
        for (const h of headings) {
            if (h.textContent.trim() === 'Final spec') {
                specHeading = h;
                break;
            }
        }
        if (!specHeading) return;
        // Guard: already wrapped (post-streaming re-render shouldn't double-wrap).
        if (specHeading.parentElement && specHeading.parentElement.classList.contains('mission-spec-card-body')) {
            return;
        }

        const card = document.createElement('div');
        card.className = 'mission-spec-card';
        const head = document.createElement('div');
        head.className = 'mission-spec-card-head';
        head.innerHTML = '<span class="mission-spec-card-icon" aria-hidden="true">📋</span><span class="mission-spec-card-title">Final spec</span>';
        const body = document.createElement('div');
        body.className = 'mission-spec-card-body';
        card.appendChild(head);
        card.appendChild(body);

        // Insert the card in place of specHeading, then move the
        // heading + every following sibling into card.body until the
        // next h2 (or end of content).
        const parent = specHeading.parentNode;
        parent.insertBefore(card, specHeading);
        let node = specHeading;
        while (node) {
            const next = node.nextSibling;
            if (node.nodeType === 1 && node.tagName === 'H2' && node !== specHeading) break;
            // Skip the heading itself — we already rendered it in the card head.
            if (node === specHeading) {
                node.remove();
            } else {
                body.appendChild(node);
            }
            node = next;
        }
    }

    // ── Tool Calls ──────────────────────────────────────────────

    handleToolCall(event) {
        this.removeThinking();
        const name = event.name || '';
        const callId = event.call_id || '';
        const nameLower = name.toLowerCase();

        // UX fix #7 — accumulate tool-call count for the per-turn aggregate
        // so the run-card can report something honest like "3 steps · 7 tools"
        // instead of just "3 agent steps".
        if (this._currentTurn) {
            this._currentTurn.toolCallCount = (this._currentTurn.toolCallCount || 0) + 1;
        }

        // Track in terminal bar (bash commands — works for all backends and modes)
        // For CLI backends: a new tool call means the previous one finished
        if (!this.isReplaying && this.activeTerminals.size > 0) {
            this.clearTerminals();
        }
        if (!this.isReplaying && nameLower === 'bash' && callId) {
            this.trackTerminalStart(callId, name, event.arguments || {});
        }

        if (name === 'file_edit' || name === 'file_write') {
            this._recordAgentFileChange(name, event.arguments || {}, event);
        }

        // CLI backends: group ALL tool calls into a collapsible activity panel
        if (this.handlesTools) {
            this.addToToolActivityGroup(event);
            return;
        }

        // Track tool counts for action-based step labels
        if (this._currentStepToolCounts) {
            this._currentStepToolCounts[name] = (this._currentStepToolCounts[name] || 0) + 1;
            this.updateStepActionLabel();
        }

        if (COLLAPSIBLE_TOOLS.has(name) && this.stepIsInlineOnly) {
            // Live-render into the running collapsed group + keep the data
            // buffer in sync (used by step.end / replay paths).
            this._appendToLiveCollapsedGroup(event);
            this.stepToolCalls.push(event);
        } else {
            // Block tool → finalize any live group first, then render the
            // step header and the block tool. The buffered inline calls (if
            // any) are already on screen inside the now-finalized live group,
            // so we drop them from the local buffers to avoid double-render.
            this._finalizeLiveCollapsedGroup();
            this.stepIsInlineOnly = false;
            this.stepToolCalls = [];
            this.stepToolResults = [];
            this.ensureStepRendered();
            this.renderToolCall(event);
        }
    }

    handleToolResult(event) {
        const name = event.name || '';
        const callId = event.call_id || '';
        const nameLower = name.toLowerCase();
        const hasImage = event.image && event.image.data;

        // Remove from terminal bar (works for all backends and modes)
        if (!this.isReplaying && nameLower === 'bash' && callId) {
            this.trackTerminalEnd(callId);
        }

        // If a screenshot comes back with an image, force step to render (don't collapse)
        if (hasImage && this.stepIsInlineOnly) {
            this._finalizeLiveCollapsedGroup();
            this.stepIsInlineOnly = false;
            this.stepToolCalls = [];
            this.stepToolResults = [];
            this.ensureStepRendered();
            this.renderToolResult(event);
            return;
        }

        if (this.stepIsInlineOnly && COLLAPSIBLE_TOOLS.has(name)) {
            // Update the live item with status + metadata (e.g. "5 matches")
            this._updateLiveCollapsedItemResult(event);
            this.stepToolResults.push(event);
        } else {
            if (!this.stepRendered) this.ensureStepRendered();
            this.renderToolResult(event);
        }
    }

    renderToolCall(event) {
        const name = event.name || '';
        const args = event.arguments || {};
        const info = getToolInfo(name);
        const category = info.category || '';

        if (BLOCK_TOOLS.has(name)) {
            this.renderBlockToolCall(name, args, info, category, event);
        } else {
            this.renderInlineToolCall(name, args, info, category);
        }
        this.scrollToBottom();
    }

    getRenderTarget() {
        return this.subagentContainer || this.chatMessages;
    }

    renderInlineToolCall(name, args, info, category) {
        let desc = '';
        let meta = '';

        switch (name) {
            case 'file_read':
                desc = `<span style="color:var(--file)">${this.escapeHtml(args.path || '')}</span>`;
                break;
            case 'glob':
                desc = this.escapeHtml(args.pattern || '');
                meta = args.path || '.';
                break;
            case 'grep':
                desc = `'${this.escapeHtml(args.pattern || '')}'`;
                meta = args.path || '.';
                break;
            case 'browser_navigate':
                desc = `<span style="color:var(--file)">${this.escapeHtml(args.url || '')}</span>`;
                // Update preview panel URL bar
                if (args.url) this.updatePreviewUrl(args.url);
                break;
            case 'browser_click':
                const target = args.text || args.selector || `(${args.x || '?'}, ${args.y || '?'})`;
                desc = `Click ${this.escapeHtml(target)}`;
                break;
            case 'browser_type':
                const t = args.text || '';
                desc = `Type '${this.escapeHtml(t.length > 40 ? t.slice(0, 37) + '...' : t)}'`;
                break;
            case 'browser_read':
                desc = 'Read page';
                meta = args.mode || 'text';
                break;
            case 'browser_screenshot':
                desc = 'Page screenshot';
                meta = args.full_page ? 'full page' : 'viewport';
                break;
            case 'computer_screenshot':
                desc = 'Desktop screenshot';
                meta = args.region ? `${args.region.width}×${args.region.height}` : 'full screen';
                break;
            case 'computer_click':
                const ct = args.clicks === 2 ? 'Double-click' : 'Click';
                desc = `${ct} (${args.x}, ${args.y})`;
                meta = args.button || 'left';
                break;
            case 'computer_type':
                const key = args.key || args.hotkey || '';
                if (key) {
                    desc = `Press ${this.escapeHtml(key)}`;
                } else {
                    const tx = args.text || '';
                    desc = `Type '${this.escapeHtml(tx.length > 40 ? tx.slice(0, 37) + '...' : tx)}'`;
                }
                break;
            case 'computer_scroll':
                desc = `Scroll ${args.direction || 'down'} ×${args.amount || 3}`;
                break;
            default:
                desc = info.label;
        }

        const el = document.createElement('div');
        el.className = `tool-inline ${category}`;
        el.setAttribute('data-tool', name);
        el.innerHTML = `
            <span class="tool-icon" style="color:var(--${info.color})">${info.icon}</span>
            <span class="tool-desc">${desc}</span>
            ${meta ? `<span class="tool-meta">(${this.escapeHtml(meta)})</span>` : ''}
        `;
        this.getRenderTarget().appendChild(el);
    }

    /**
     * Render a block tool (bash / file_edit / file_write / browser_js) as a
     * single compact row that sits in the flat per-turn list. The tool's
     * full output (diff / file content / shell output) is stashed on the
     * row's dataset and rendered lazily into a hidden detail panel that
     * the user expands by clicking the chevron — or that auto-expands when
     * the result event reports an error.
     *
     * Row anatomy:
     *   ◯ ~ foo.py · pending …                ← while running
     *   ✓ ~ foo.py · +12 −3              ▸    ← after success result
     *   ✗ $ npm test · exit 1 · 1.2s    ▾    ← after error result (auto-expanded)
     */
    renderBlockToolCall(name, args, info, category, event) {
        const callId = event.call_id || `__nocallid_${Date.now()}_${Math.random().toString(36).slice(2, 6)}`;

        const el = document.createElement('div');
        el.className = `tool-row ${category}`;
        el.dataset.tool = name;
        el.dataset.callId = callId;

        // Per-tool args summary + the icon glyph that goes in the second slot.
        let glyph = info.icon;
        let glyphColor = info.color;
        let summary = '';
        let detailKind = ''; // what we'll render when expanded

        if (name === 'bash') {
            glyph = '$';
            glyphColor = info.color;
            const cmd = args.command || '';
            summary = cmd;
            el.dataset.fullCommand = cmd;
            detailKind = 'bash';
            this.pushPreviewConsole(`$ ${cmd}`, 'stdout');
        } else if (name === 'file_write') {
            glyph = '+';
            glyphColor = 'ok';
            summary = args.path || '';
            const content = args.content || '';
            el.dataset.fullContent = content;
            detailKind = 'file_write';
        } else if (name === 'file_edit') {
            glyph = '~';
            glyphColor = 'warn';
            summary = args.path || '';
            // Stash diff lines as JSON so the lazy expand can re-render them
            // with proper +/- coloring without re-fetching anything.
            const diffLines = event.diff_lines || [];
            el.dataset.diffLines = JSON.stringify(diffLines);
            detailKind = 'file_edit';
        } else if (name === 'browser_js') {
            glyph = '>';
            glyphColor = 'brand2';
            const code = args.code || '';
            summary = code.length > 100 ? code.slice(0, 97) + '...' : code;
            el.dataset.fullCode = code;
            detailKind = 'browser_js';
        } else {
            summary = info.label;
        }

        el.dataset.detailKind = detailKind;

        el.innerHTML = `
            <span class="tool-row-status pending" data-status>◯</span>
            <span class="tool-row-glyph" style="color:var(--${glyphColor})" data-glyph>${glyph}</span>
            <code class="tool-row-summary" data-summary></code>
            <span class="tool-row-meta" data-meta>running…</span>
            <button type="button" class="tool-row-toggle" data-toggle aria-expanded="false" tabindex="-1">▸</button>
            <div class="tool-row-detail" data-detail hidden></div>
        `;
        // Use textContent for summary so paths / commands aren't HTML-injected.
        el.querySelector('[data-summary]').textContent = summary;

        // Click anywhere on the row (except the toggle button which stops
        // propagation) → expand. Idempotent: clicking again toggles.
        el.addEventListener('click', () => this._toggleBlockRowDetail(el));

        this.getRenderTarget().appendChild(el);
        if (this._blockToolRows) this._blockToolRows.set(callId, el);
    }

    /**
     * Lazy-render the detail panel for a block tool row. Called the first
     * time the user expands the row (or auto-triggered on error in
     * renderBlockToolResult).
     */
    _renderBlockRowDetail(el) {
        const detail = el.querySelector('[data-detail]');
        if (!detail || detail.dataset.rendered === 'true') return;
        const kind = el.dataset.detailKind || '';

        if (kind === 'bash') {
            const cmd = el.dataset.fullCommand || '';
            const output = el.dataset.fullOutput || '';
            const isError = el.dataset.isError === 'true';
            detail.innerHTML = `
                <div class="tool-row-detail-cmd">$ <code></code></div>
                <pre class="tool-row-detail-output ${isError ? 'err' : ''}"></pre>
            `;
            detail.querySelector('code').textContent = cmd;
            detail.querySelector('pre').textContent = output || '(no output)';
        } else if (kind === 'file_write') {
            const content = el.dataset.fullContent || '';
            detail.innerHTML = `<pre class="tool-row-detail-content"></pre>`;
            detail.querySelector('pre').textContent = content;
        } else if (kind === 'file_edit') {
            let lines = [];
            try { lines = JSON.parse(el.dataset.diffLines || '[]'); } catch (_e) { /* keep [] */ }
            const rendered = lines.length > 2
                ? lines.slice(2).map((line) => {
                    if (line.startsWith('+')) return `<span class="diff-line add">+ ${this.escapeHtml(line.slice(1))}</span>`;
                    if (line.startsWith('-')) return `<span class="diff-line del">- ${this.escapeHtml(line.slice(1))}</span>`;
                    if (line.startsWith('@@')) return `<span class="diff-line hunk">${this.escapeHtml(line)}</span>`;
                    return `<span class="diff-line ctx">  ${this.escapeHtml(line)}</span>`;
                }).join('')
                : '<span class="tool-row-detail-empty">(no visible diff)</span>';
            detail.innerHTML = `<div class="tool-row-detail-diff">${rendered}</div>`;
        } else if (kind === 'browser_js') {
            const code = el.dataset.fullCode || '';
            const output = el.dataset.fullOutput || '';
            detail.innerHTML = `
                <pre class="tool-row-detail-content"></pre>
                <pre class="tool-row-detail-output"></pre>
            `;
            detail.querySelector('.tool-row-detail-content').textContent = code;
            detail.querySelector('.tool-row-detail-output').textContent = output || '(no output)';
        }

        detail.dataset.rendered = 'true';
    }

    _toggleBlockRowDetail(el) {
        const expanded = el.classList.toggle('expanded');
        const detail = el.querySelector('[data-detail]');
        const toggle = el.querySelector('[data-toggle]');
        if (expanded) {
            this._renderBlockRowDetail(el);
            if (detail) detail.hidden = false;
            if (toggle) {
                toggle.textContent = '▾';
                toggle.setAttribute('aria-expanded', 'true');
            }
        } else {
            if (detail) detail.hidden = true;
            if (toggle) {
                toggle.textContent = '▸';
                toggle.setAttribute('aria-expanded', 'false');
            }
        }
    }

    renderToolResult(event) {
        const name = event.name || '';
        const output = event.output || '';
        const isError = event.is_error || false;
        const elapsed = event.elapsed || 0;
        const meta = event.metadata || {};
        const denied = event.denied || false;
        const image = event.image || null;

        if (denied) {
            this.appendToolStatus(name, '✗ denied', 'warn');
            return;
        }

        // Splice call_id into meta so the block-row lookup can match the
        // exact call (multiple parallel calls of the same tool exist with
        // tool batching). Inline result path doesn't need this.
        const metaWithId = event.call_id ? { ...meta, _call_id: event.call_id } : meta;

        if (BLOCK_TOOLS.has(name)) {
            this.renderBlockToolResult(name, output, isError, elapsed, metaWithId);
        } else {
            this.renderInlineToolResult(name, output, isError, elapsed, meta);
        }

        // Render screenshot image if present
        if (image && image.data) {
            this.renderScreenshotImage(image.data, image.media_type || 'image/png', name);
        }

        this.scrollToBottom();
    }

    renderInlineToolResult(name, output, isError, elapsed, meta) {
        // Find the last matching inline tool and add status
        const target = this.getRenderTarget();
        const tools = target.querySelectorAll(`.tool-inline[data-tool="${name}"]`);
        const last = tools[tools.length - 1];
        if (!last) return;

        let statusText = '';
        let statusClass = isError ? 'err' : 'ok';

        switch (name) {
            case 'file_read':
                statusText = isError ? '✗' : (meta.lines ? `${meta.lines} lines` : '✓');
                break;
            case 'glob':
                statusText = `${meta.count || 0} files`;
                break;
            case 'grep':
                statusText = `${meta.count || 0} matches`;
                break;
            case 'browser_navigate':
                statusText = isError ? '✗' : (meta.title || '✓');
                // Update preview tab name with page title
                if (meta.title) this.updatePreviewUrl(null, meta.title);
                break;
            case 'browser_read':
                statusText = isError ? '✗' : `${(meta.chars || 0).toLocaleString()} chars`;
                break;
            case 'browser_screenshot':
            case 'computer_screenshot': {
                const kb = meta.size_bytes ? Math.round(meta.size_bytes / 1024) : 0;
                const dims = meta.width && meta.height ? `${meta.width}×${meta.height} · ` : '';
                statusText = isError ? '✗' : `${dims}${kb}KB`;
                break;
            }
            default:
                statusText = isError ? '✗' : '✓';
        }

        const status = document.createElement('span');
        status.className = `tool-status ${statusClass}`;
        status.textContent = statusText;

        // Remove existing status if any
        const existing = last.querySelector('.tool-status');
        if (existing) existing.remove();
        last.appendChild(status);
    }

    /**
     * Update the in-place tool row created by renderBlockToolCall with the
     * result. Sets the status glyph (✓/✗), populates the meta column with
     * a one-line summary (`+12 −3` / `exit 0 · 1.2s` / `145 lines`), and
     * stashes the full output on the row so the lazy detail render can
     * find it. Auto-expands on error so the user immediately sees what
     * went wrong without an extra click.
     */
    renderBlockToolResult(name, output, isError, elapsed, meta) {
        // Find the most recent row for this tool. We try call_id first
        // (precise — multiple parallel calls of the same tool are kept
        // distinct) then fall back to "last row of this tool name" so
        // backends that don't emit call_ids still get correct updates.
        const callId = meta && meta._call_id;
        let row = null;
        if (callId && this._blockToolRows && this._blockToolRows.has(callId)) {
            row = this._blockToolRows.get(callId);
            this._blockToolRows.delete(callId);
        } else {
            const all = this.getRenderTarget().querySelectorAll(`.tool-row[data-tool="${name}"]`);
            row = all[all.length - 1] || null;
        }
        if (!row) return;

        // Status glyph + class
        const statusEl = row.querySelector('[data-status]');
        const metaEl = row.querySelector('[data-meta]');
        if (statusEl) {
            statusEl.classList.remove('pending');
            statusEl.classList.add(isError ? 'err' : 'ok');
            statusEl.textContent = isError ? '✗' : '✓';
        }
        row.dataset.isError = isError ? 'true' : 'false';
        // Stamp the row with the .errored class so the whole row picks up
        // the warn-tinted styling (red left-border + faint bg). The status
        // glyph alone is too easy to miss when skimming a long turn.
        if (isError) {
            row.classList.add('errored');
        } else {
            row.classList.remove('errored');
        }

        // Per-tool meta summary (the right-aligned dim line on the row)
        let metaText = '';
        if (name === 'bash') {
            const parts = [];
            if (meta && meta.exit_code !== undefined && meta.exit_code !== null) {
                parts.push(`exit ${meta.exit_code}`);
            }
            if (elapsed > 0) parts.push(`${elapsed.toFixed(1)}s`);
            if (meta && meta.timed_out) parts.push('timeout');
            metaText = parts.join(' · ');
            row.dataset.fullOutput = output || '';
            // Push to preview console regardless of expand state.
            if (output && output.trim()) {
                this.pushPreviewConsole(output.trim(), isError ? 'stderr' : 'stdout');
            }
        } else if (name === 'file_write') {
            const lineCount = (meta && meta.lines) || 0;
            metaText = isError ? (output || 'error').slice(0, 80) : `${lineCount} line${lineCount === 1 ? '' : 's'}`;
        } else if (name === 'file_edit') {
            // Count +/- from the diff_lines we stashed on the row.
            let add = 0, del = 0;
            try {
                const lines = JSON.parse(row.dataset.diffLines || '[]');
                for (const ln of lines) {
                    if (ln.startsWith('+') && !ln.startsWith('+++')) add++;
                    else if (ln.startsWith('-') && !ln.startsWith('---')) del++;
                }
            } catch (_e) { /* keep zero counts */ }
            metaText = isError
                ? (output || 'error').slice(0, 80)
                : (add || del ? `+${add} −${del}` : 'applied');
        } else if (name === 'browser_js') {
            row.dataset.fullOutput = output || '';
            metaText = elapsed > 0 ? `${elapsed.toFixed(1)}s` : '';
        }
        if (metaEl) metaEl.textContent = metaText;

        // Auto-expand on error so the user sees the failure immediately.
        if (isError && !row.classList.contains('expanded')) {
            this._toggleBlockRowDetail(row);
        }

        // If detail panel is already open (user expanded before result
        // arrived), invalidate the cache so the next render picks up the
        // now-populated dataset fields.
        const detail = row.querySelector('[data-detail]');
        if (detail && detail.dataset.rendered === 'true' && row.classList.contains('expanded')) {
            detail.dataset.rendered = '';
            detail.innerHTML = '';
            this._renderBlockRowDetail(row);
        }
    }

    appendToolStatus(name, text, color) {
        const el = document.createElement('div');
        el.className = 'tool-inline';
        el.innerHTML = `<span class="tool-status" style="color:var(--${color})">${text}</span>`;
        this.getRenderTarget().appendChild(el);
    }

    // ── Screenshot Image ────────────────────────────────────────

    renderScreenshotImage(base64Data, mediaType, toolName) {
        const category = toolName.startsWith('computer_') ? 'desktop' : '';
        const container = document.createElement('div');
        container.className = `screenshot-container ${category}`;

        const img = document.createElement('img');
        img.className = 'screenshot-thumb';
        img.src = `data:${mediaType};base64,${base64Data}`;
        img.alt = 'Screenshot';
        img.loading = 'lazy';

        img.addEventListener('click', () => {
            this.showLightbox(img.src);
        });

        container.appendChild(img);

        // Append to current render target (subagent container or chat)
        const target = this.subagentContainer || this.chatMessages;
        target.appendChild(container);

        // Also push to preview panel
        this.pushPreviewImage(base64Data, mediaType, toolName);
    }

    showLightbox(src) {
        const overlay = document.createElement('div');
        overlay.className = 'lightbox-overlay';
        overlay.innerHTML = `
            <img class="lightbox-img" src="${src}" alt="Screenshot">
            <button class="lightbox-close">&times;</button>
        `;
        overlay.addEventListener('click', (e) => {
            if (e.target === overlay || e.target.classList.contains('lightbox-close')) {
                overlay.remove();
            }
        });
        document.body.appendChild(overlay);
    }

    // ── Preview Panel ────────────────────────────────────────────

    togglePreviewPanel() {
        if (this.previewOpen) {
            this.closePreviewPanel();
        } else {
            this.openPreviewPanel();
        }
    }

    openPreviewPanel() {
        this.previewOpen = true;
        this.previewPanel.classList.add('open');
        this.previewResize.style.display = 'block';
        this.previewToggle.classList.add('active');
        this.previewToggle.classList.remove('has-update');
        const savedW = localStorage.getItem('resonant:preview-width');
        if (savedW) {
            this.previewPanel.style.width = savedW;
            this.previewPanel.style.minWidth = '320px';
        }
        localStorage.setItem('resonant:preview-open', '1');
    }

    closePreviewPanel() {
        this.previewOpen = false;
        if (this.previewPanel.style.width) {
            localStorage.setItem('resonant:preview-width', this.previewPanel.style.width);
        }
        this.previewPanel.classList.remove('open');
        this.previewPanel.style.width = '';
        this.previewPanel.style.minWidth = '';
        this.previewResize.style.display = 'none';
        this.previewToggle.classList.remove('active');
        localStorage.setItem('resonant:preview-open', '0');
    }

    /**
     * Switch the preview panel between the Browser pane and the Plan pane.
     * Browser-related elements stay in their existing IDs (preview-chrome,
     * preview-viewport, preview-console). Plan elements live under
     * #plan-graph-pane. Mutually exclusive display toggle.
     */
    switchPreviewPane(pane) {
        const isPlan = pane === 'plan';
        document.querySelectorAll('.preview-tab[data-pane]').forEach((t) => {
            t.classList.toggle('active', t.dataset.pane === pane);
        });
        const browserChrome = document.querySelector('#preview-panel .preview-chrome');
        const browserViewport = document.getElementById('preview-viewport');
        const browserConsole = document.getElementById('preview-console');
        const planPane = document.getElementById('plan-graph-pane');
        if (browserChrome) browserChrome.style.display = isPlan ? 'none' : '';
        if (browserViewport) browserViewport.style.display = isPlan ? 'none' : '';
        if (browserConsole) browserConsole.style.display = isPlan ? 'none' : '';
        if (planPane) planPane.style.display = isPlan ? 'flex' : 'none';

        // C2 — track the active pane so plan-event handlers can decide
        // whether to flash the unread-update indicator. Switching TO the
        // plan pane clears any pending indicator.
        this._currentPreviewPane = pane;
        if (isPlan) this._clearPlanTabUnread();
    }

    /**
     * Mark the plan tab as having unread updates. Called when a
     * plan.event / plan.snapshot / plan.checkpoint arrives while the
     * user is on a different preview pane (or the panel is closed but
     * not focused on plan). Pure DOM affordance — never grabs focus.
     */
    _markPlanTabUnread() {
        if (this._currentPreviewPane === 'plan') return;
        const tab = document.querySelector('.preview-tab[data-pane="plan"]');
        if (tab) tab.classList.add('has-unread');
    }

    _clearPlanTabUnread() {
        const tab = document.querySelector('.preview-tab[data-pane="plan"]');
        if (tab) tab.classList.remove('has-unread');
    }

    /**
     * Convenience: open the preview panel + switch to the Plan pane.
     * `focus=true` ensures the user actually sees it (used for checkpoints);
     * `focus=false` quietly populates the badge without stealing attention.
     */
    openPlanTab(focus = true) {
        if (!this.previewOpen) this.openPreviewPanel();
        if (focus) this.switchPreviewPane('plan');
    }

    /** Update the preview URL bar when a browser_navigate tool is called */
    updatePreviewUrl(url, title) {
        this._previewUrl = url || this._previewUrl || '';
        this._previewTitle = title || this._previewTitle || '';
        if (this.previewUrlText) {
            this.previewUrlText.textContent = this._previewUrl;
        }
        if (this.previewTabName && this._previewTitle) {
            this.previewTabName.textContent = this._previewTitle;
        } else if (this.previewTabName && this._previewUrl) {
            try {
                const u = new URL(this._previewUrl);
                this.previewTabName.textContent = u.hostname + u.pathname;
            } catch {
                this.previewTabName.textContent = this._previewUrl;
            }
        }
    }

    /** Push a screenshot image to the preview panel viewport */
    pushPreviewImage(base64Data, mediaType, toolName) {
        const src = `data:${mediaType};base64,${base64Data}`;
        const now = new Date();

        // Store in history
        this.previewImages.push({
            src, toolName, timestamp: now,
            url: this._previewUrl || '', title: this._previewTitle || ''
        });
        this.previewCurrentIndex = this.previewImages.length - 1;

        // Show the latest screenshot in the viewport
        this._renderPreviewScreenshot(this.previewCurrentIndex);

        // Update nav button states
        this._updatePreviewNav();

        // Auto-open panel on first screenshot (if closed)
        if (!this.previewOpen) {
            this.openPreviewPanel();
        } else {
            this.previewToggle.classList.remove('has-update');
        }
    }

    /** Render the screenshot at the given index in the viewport */
    _renderPreviewScreenshot(index) {
        const item = this.previewImages[index];
        if (!item) return;

        // Clear viewport
        const empty = document.getElementById('preview-empty');
        if (empty) empty.remove();

        // Find or create the img element
        let img = this.previewViewport.querySelector('img.preview-screenshot');
        if (!img) {
            img = document.createElement('img');
            img.className = 'preview-screenshot';
            img.addEventListener('click', () => {
                const current = this.previewImages[this.previewCurrentIndex];
                if (current) this.showLightbox(current.src);
            });
            this.previewViewport.appendChild(img);
        }
        img.src = item.src;
        img.alt = 'Screenshot';

        // Update URL bar to match this screenshot's context
        if (item.url) this.previewUrlText.textContent = item.url;
        if (item.title) {
            this.previewTabName.textContent = item.title;
        }
    }

    /** Navigate back/forward through screenshots */
    previewNavigate(delta) {
        const newIndex = this.previewCurrentIndex + delta;
        if (newIndex < 0 || newIndex >= this.previewImages.length) return;
        this.previewCurrentIndex = newIndex;
        this._renderPreviewScreenshot(newIndex);
        this._updatePreviewNav();
    }

    /** Update back/forward button enabled states */
    _updatePreviewNav() {
        const back = document.getElementById('preview-back');
        const fwd = document.getElementById('preview-forward');
        if (back) back.disabled = this.previewCurrentIndex <= 0;
        if (fwd) fwd.disabled = this.previewCurrentIndex >= this.previewImages.length - 1;
    }

    /** Push a log line to the preview console */
    pushPreviewConsole(text, type = 'stdout') {
        if (!this.previewConsoleBody) return;

        // Remove "waiting" placeholder
        const waiting = this.previewConsoleBody.querySelector('.preview-console-waiting');
        if (waiting) waiting.remove();

        const line = document.createElement('div');
        line.className = `preview-console-line ${type}`;
        line.dataset.type = type;
        line.textContent = text;
        this.previewConsoleBody.appendChild(line);
        this.previewConsoleBody.scrollTop = this.previewConsoleBody.scrollHeight;
    }

    /** Filter console lines by type and search text */
    filterPreviewConsole(filter = 'all', search = '') {
        if (!this.previewConsoleBody) return;
        const lines = this.previewConsoleBody.querySelectorAll('.preview-console-line');
        const searchLower = search.toLowerCase();
        lines.forEach(line => {
            const typeMatch = filter === 'all' || line.dataset.type === filter;
            const textMatch = !search || line.textContent.toLowerCase().includes(searchLower);
            line.style.display = (typeMatch && textMatch) ? '' : 'none';
        });
    }

    /** Clear preview panel (e.g. on new session) */
    clearPreviewPanel() {
        this.previewImages = [];
        this.previewCurrentIndex = -1;
        this._previewUrl = '';
        this._previewTitle = '';

        // Reset viewport
        if (this.previewViewport) {
            this.previewViewport.innerHTML = `
                <div class="preview-empty" id="preview-empty">
                    <div class="preview-empty-icon">
                        <svg width="32" height="32" viewBox="0 0 32 32" fill="none">
                            <rect x="2" y="4" width="28" height="20" rx="2" stroke="currentColor" stroke-width="1.5"/>
                            <line x1="2" y1="10" x2="30" y2="10" stroke="currentColor" stroke-width="1" opacity="0.3"/>
                            <circle cx="6" cy="7" r="1.2" fill="currentColor" opacity="0.3"/>
                            <circle cx="10" cy="7" r="1.2" fill="currentColor" opacity="0.3"/>
                            <circle cx="14" cy="7" r="1.2" fill="currentColor" opacity="0.3"/>
                            <rect x="8" y="26" width="16" height="2" rx="1" fill="currentColor" opacity="0.2"/>
                        </svg>
                    </div>
                    <span>Waiting for output...</span>
                </div>
            `;
        }

        // Reset URL bar & tab
        if (this.previewUrlText) this.previewUrlText.textContent = '';
        if (this.previewTabName) this.previewTabName.textContent = 'Preview';

        // Reset console
        if (this.previewConsoleBody) {
            this.previewConsoleBody.innerHTML = '<div class="preview-console-waiting">Waiting for output...</div>';
        }

        // Reset nav buttons
        this._updatePreviewNav();
    }

    // ── View Switching ────────────────────────────────────────────

    switchView(viewName) {
        this.currentView = viewName;

        // Hide all views
        this.welcomeScreen.style.display = 'none';
        if (this.agentPanel) this.agentPanel.style.display = 'none';
        else this.chatContainer.style.display = 'none';
        this.inputBar.style.display = 'none';
        if (this.settingsView) this.settingsView.style.display = 'none';

        const sessionList = document.getElementById('agent-list');
        if (sessionList) sessionList.style.display = viewName === 'agents' ? '' : 'none';

        document.querySelectorAll('.sidebar-icon-btn[data-view], .sidebar-nav-item[data-view], .sidebar-action[data-view]').forEach(el =>
            el.classList.toggle('active', el.dataset.view === viewName));

        // Header title swap — make it obvious you're on a non-agent screen
        if (this.headerProject) {
            if (viewName === 'agents') {
                // Restore project name
                const proj = (this.currentCwd || '').replace(/\\/g, '/').split('/').pop() || '';
                this.headerProject.textContent = proj;
            } else if (viewName === 'settings') {
                this.headerProject.textContent = 'Settings';
            }
        }
        // Hide the header indicators on Settings — they apply to a session, not Settings
        const indicators = document.querySelector('.header-indicators');
        if (indicators) indicators.style.visibility = (viewName === 'agents') ? '' : 'hidden';

        switch (viewName) {
            case 'agents':
                if (this.currentSessionId || (this.backends && Object.keys(this.backends).length > 0)) {
                    if (this.agentPanel) this.agentPanel.style.display = 'flex';
                    else this.chatContainer.style.display = 'flex';
                    this.inputBar.style.display = 'flex';
                } else {
                    this.welcomeScreen.style.display = 'flex';
                }
                break;
            case 'settings':
                this.settingsView.style.display = 'flex';
                if (!this.settings || !Object.keys(this.settings).length) {
                    this.send({ command: 'get_settings' });
                } else {
                    this.renderSettingsView();
                }
                break;
        }
    }

    // ── Settings View ────────────────────────────────────────────

    renderSettingsView() {
        if (!this.settingsBody) return;

        const sections = [
            {
                id: 'general', title: 'General', open: true,
                fields: [
                    // v0.4.0 — single backend; the Auto option is the
                    // only sensible value. Kept the select for schema
                    // compatibility with older settings.json.
                    { key: 'default_backend', label: 'Default backend', type: 'select',
                      options: [
                          { value: 'ollama', label: 'Ollama' },
                          { value: '', label: 'Auto' },
                      ]
                    },
                    { key: 'default_model', label: 'Default model', type: 'text',
                      placeholder: 'e.g. deepseek-v4-pro:cloud',
                      hint: 'Leave blank to use the first model the chosen backend reports. Pro is the v0.6.2 default for autonomous missions; flash is the fast-iter alternative for chat.' },
                    { key: 'default_permission_mode', label: 'Default permission mode', type: 'select',
                      options: [
                          { value: 'bypass', label: 'Full-auto (sandboxed)' },
                          { value: 'ask', label: 'Suggest (read-only)' },
                          { value: 'auto-edit', label: 'Auto-edit (files OK, shell asks)' },
                          { value: 'plan', label: 'Plan mode' },
                      ]
                    },
                    { key: 'auto_lint_after_edits', label: 'Auto-lint after edits', type: 'toggle',
                      hint: 'After every file_edit/file_write, run the project linter (ruff/eslint/flake8) on the changed file. Errors are injected back as a follow-up turn.' },
                    { key: 'auto_test_after_edits', label: 'Auto-test after edits', type: 'toggle',
                      hint: 'After every file_edit/file_write, run the test command on the matching test file. Failures are injected back as a follow-up turn.' },
                    { key: 'auto_test_command', label: 'Auto-test command', type: 'text',
                      hint: 'Default: "pytest -x". For JS/TS: "npx jest" or "npx vitest run".' },
                    { key: 'big_context_profile', label: 'Big-context profile (deepseek-v4-flash 1M)', type: 'toggle',
                      hint: 'Bumps Ollama context to 131072 tokens and batch to 2048. Best for large-repo sessions. Restart the app for the change to take effect on the next backend connection.' },
                    { key: 'harness_enabled', label: 'Sprint workflow (planner / generator / evaluator)', type: 'toggle',
                      hint: 'Off by default. Enable to use Resonant\u2019s structured planner\u2192generator\u2192evaluator pattern with sprint contracts and an autonomous cycle. State lives in ~/.resonant/, not in your repo.' },
                    { key: 'session_max_steps', label: 'Session step budget', type: 'text',
                      hint: 'Maximum agentic loop steps before the session caps out. Default 200 (effectively unlimited for normal tasks). Set to 0 to disable entirely \u2014 doom-loop detection still catches real runaways. Local-model users can safely raise this; cloud users may want a smaller cap for cost.' },
                ]
            },
            {
                id: 'appearance', title: 'Appearance', open: false,
                fields: [
                    { key: 'theme', label: 'Theme', type: 'select',
                      options: [{ value: 'dark', label: 'Dark' }, { value: 'light', label: 'Light' }]
                    },
                    { key: 'density', label: 'Density', type: 'select',
                      options: [{ value: 'comfortable', label: 'Comfortable' }, { value: 'compact', label: 'Compact' }]
                    },
                    { key: 'font_size', label: 'Base font size', type: 'select',
                      options: [
                          { value: '12', label: '12px' },
                          { value: '13', label: '13px' },
                          { value: '13.5', label: '13.5px (default)' },
                          { value: '14', label: '14px' },
                          { value: '15', label: '15px' },
                      ]
                    },
                ]
            },
            {
                id: 'local_backends', title: 'Local Backends (Ollama / LM Studio)', open: false,
                fields: [
                    { key: 'ollama_host', label: 'Ollama host (OLLAMA_HOST)', type: 'text' },
                    { key: 'ollama_num_ctx', label: 'Ollama context window (num_ctx)', type: 'number' },
                    { key: 'ollama_keep_alive', label: 'Ollama keep-alive duration', type: 'text' },
                ]
            },
            {
                id: 'network', title: 'Network',
                fields: [
                    // v0.4.0 — Ollama is the only backend. Default Mac Studio
                    // location is 10.0.0.133:11434; leave blank to use the
                    // OLLAMA_HOST env var or auto-detect.
                    { key: 'ollama_url', label: 'Ollama URL (e.g. http://10.0.0.133:11434)', type: 'text' },
                ]
            },
            // v0.4.0 — `api_keys` section dropped (Anthropic / OpenAI
            // backends were cut). Settings still loads old JSONs that
            // had this section; the runtime just ignores them.
            {
                id: 'cost_tracking', title: 'Cost Tracking',
                fields: [
                    { key: 'enabled', label: 'Enable cost tracking', type: 'toggle' },
                    { key: 'budget_alert_usd', label: 'Daily budget alert ($)', type: 'number' },
                ]
            },
            {
                id: 'engram', title: 'Memory (Engram)',
                fields: [
                    { key: 'enabled', label: 'Enable memory', type: 'toggle' },
                    { key: 'server_url', label: 'Engram server URL', type: 'text' },
                ]
            },
            {
                id: 'rag', title: 'Codebase Index (RAG)', custom: true },
            {
                id: 'hooks', title: 'Hooks', custom: true },
            {
                id: 'mcp_servers', title: 'MCP Servers', custom: true },
        ];

        this.settingsBody.innerHTML = '';

        for (const section of sections) {
            const data = this.settings[section.id] || {};
            const el = document.createElement('div');
            el.className = `settings-section${section.open ? ' open' : ''}`;

            let bodyHtml = '';
            if (section.id === 'rag') {
                const rag = this.ragStats || {};
                const indexed = rag.total_files > 0;
                bodyHtml = `
                    <div class="settings-row">
                        <span class="settings-row-label">Status</span>
                        <span style="color:${indexed ? 'var(--ok)' : 'var(--muted)'}">${indexed ? `${rag.total_files} files indexed (${rag.total_lines || 0} lines)` : 'Not indexed'}</span>
                    </div>
                `;
                if (rag.languages) {
                    const langs = Object.entries(rag.languages).sort((a,b) => b[1]-a[1]).slice(0,5);
                    bodyHtml += `<div class="settings-row"><span class="settings-row-label">Languages</span><span style="color:var(--muted);font-size:12px">${langs.map(([l,c]) => `${l}: ${c}`).join(', ')}</span></div>`;
                }
                bodyHtml += `
                    <div class="settings-row" style="margin-top:8px;gap:8px">
                        <button class="btn-sm rag-index-btn" style="font-size:12px">${indexed ? 'Re-index' : 'Index Codebase'}</button>
                        <button class="btn-sm rag-force-btn" style="font-size:12px">Force Re-index</button>
                    </div>
                    <div class="settings-row" style="margin-top:4px"><span class="settings-row-label" style="color:var(--dim);font-size:11px">Index enables semantic file search for better context in prompts</span></div>
                `;
            } else if (section.id === 'hooks') {
                const hooks = Array.isArray(data) ? data : [];
                if (hooks.length === 0) {
                    bodyHtml = `<div class="settings-row"><span class="settings-row-label" style="color:var(--dim)">No hooks configured</span></div>`;
                } else {
                    bodyHtml = hooks.map((h, i) => `
                        <div class="settings-row">
                            <span class="settings-row-label">${h.name || h.hook_type}: <code style="font-size:11px">${h.command}</code></span>
                            <span style="color:${h.enabled ? 'var(--ok)' : 'var(--muted)'}">${h.enabled ? '●' : '○'}</span>
                        </div>
                    `).join('');
                }
                bodyHtml += `<div class="settings-row" style="margin-top:8px"><span class="settings-row-label" style="color:var(--dim);font-size:11px">Edit hooks in ~/.resonant/settings.json</span></div>`;
            } else if (section.id === 'mcp_servers') {
                const servers = typeof data === 'object' && !Array.isArray(data) ? Object.entries(data) : [];
                if (servers.length === 0) {
                    bodyHtml = `<div class="settings-row"><span class="settings-row-label" style="color:var(--dim)">No MCP servers configured</span></div>`;
                } else {
                    bodyHtml = servers.map(([name, cfg]) => `
                        <div class="settings-row">
                            <span class="settings-row-label">${name}: <code style="font-size:11px">${cfg.command || ''}</code></span>
                            <button class="btn-sm mcp-connect-btn" data-server="${name}" style="font-size:11px">Connect</button>
                        </div>
                    `).join('');
                }
                bodyHtml += `<div class="settings-row" style="margin-top:8px"><span class="settings-row-label" style="color:var(--dim);font-size:11px">Edit MCP servers in ~/.resonant/settings.json</span></div>`;
            } else if (section.custom) {
                bodyHtml = `<div class="settings-row"><span class="settings-row-label" style="color:var(--dim)">Configure in settings.json</span></div>`;
            } else if (section.fields) {
                for (const field of section.fields) {
                    const val = data[field.key] ?? '';
                    let input = '';
                    if (field.type === 'select') {
                        const opts = field.options.map(o =>
                            `<option value="${o.value}" ${val === o.value ? 'selected' : ''}>${o.label}</option>`
                        ).join('');
                        input = `<select class="settings-select" data-section="${section.id}" data-key="${field.key}">${opts}</select>`;
                    } else if (field.type === 'toggle') {
                        const checked = val ? 'checked' : '';
                        input = `<label style="cursor:pointer"><input type="checkbox" ${checked} data-section="${section.id}" data-key="${field.key}" style="cursor:pointer" /> ${val ? 'On' : 'Off'}</label>`;
                    } else if (field.type === 'password') {
                        const hasSecret = Boolean(this.settings._meta?.api_keys_present?.[field.key]);
                        input = `
                            <div style="display:flex;align-items:center;gap:8px;">
                                <input class="settings-input" type="password" value="" data-section="${section.id}" data-key="${field.key}" data-secret-field="true" placeholder="${hasSecret ? 'Stored key' : 'Enter key'}" style="flex:1" />
                                <span style="color:var(--muted);font-size:11px;white-space:nowrap">${hasSecret ? 'Stored' : 'Not set'}</span>
                                ${hasSecret ? `<button class="btn-sm settings-clear-secret" data-section="${section.id}" data-key="${field.key}" style="font-size:11px">Clear</button>` : ''}
                            </div>
                        `;
                    } else if (field.type === 'password') {
                        input = `<input class="settings-input" type="password" value="${this.escapeHtml(String(val))}" data-section="${section.id}" data-key="${field.key}" placeholder="••••" />`;
                    } else if (field.type === 'number') {
                        input = `<input class="settings-input" type="number" value="${val || ''}" data-section="${section.id}" data-key="${field.key}" placeholder="None" style="width:80px" />`;
                    } else {
                        const ph = field.placeholder ? ` placeholder="${this.escapeHtml(field.placeholder)}"` : '';
                        input = `<input class="settings-input" type="text" value="${this.escapeHtml(String(val))}" data-section="${section.id}" data-key="${field.key}"${ph} />`;
                    }
                    const hint = field.hint ? `<div class="settings-row-hint">${this.escapeHtml(field.hint)}</div>` : '';
                    bodyHtml += `<div class="settings-row"><span class="settings-row-label">${field.label}</span><div class="settings-row-value">${input}${hint}</div></div>`;
                }
            }

            el.innerHTML = `
                <div class="settings-section-header">
                    <span class="settings-section-title">${section.title}</span>
                    <span class="settings-section-arrow">▶</span>
                </div>
                <div class="settings-section-body">${bodyHtml}</div>
            `;

            // Toggle open/close — click anywhere on header toggles the section
            const header = el.querySelector('.settings-section-header');
            header.addEventListener('click', (e) => {
                // Don't toggle if clicking on an input/select inside the header
                if (e.target.closest('input, select, textarea, button')) return;
                el.classList.toggle('open');
            });
            // Also handle clicks on the arrow and title directly
            header.addEventListener('mousedown', (e) => {
                e.preventDefault(); // Prevent text selection on double-click
            });

            this.settingsBody.appendChild(el);
        }

        // Bind change events for settings inputs
        this.settingsBody.querySelectorAll('select, input').forEach(input => {
            const eventType = input.type === 'checkbox' ? 'change' : 'blur';
            input.addEventListener(eventType, () => {
                const section = input.dataset.section;
                const key = input.dataset.key;
                if (!section || !key) return;
                let value;
                if (input.type === 'checkbox') {
                    value = input.checked;
                    const label = input.parentElement;
                    if (label) label.lastChild.textContent = value ? ' On' : ' Off';
                } else if (input.type === 'number') {
                    value = input.value ? Number(input.value) : null;
                } else if (input.type === 'password') {
                    value = input.value;
                    if (!value) return;
                } else {
                    value = input.value;
                }
                this.send({ command: 'update_settings', section, key, value });

                if (section === 'appearance') this._applyAppearance(key, value);
            });
        });

        this.settingsBody.querySelectorAll('.settings-clear-secret').forEach(btn => {
            btn.addEventListener('click', () => {
                this.send({
                    command: 'update_settings',
                    section: btn.dataset.section,
                    key: btn.dataset.key,
                    value: '',
                    clear_secret: true,
                });
            });
        });

        // MCP connect buttons
        this.settingsBody.querySelectorAll('.mcp-connect-btn').forEach(btn => {
            btn.addEventListener('click', () => {
                const serverName = btn.dataset.server;
                this.send({ command: 'mcp_connect', name: serverName });
                btn.textContent = 'Connecting...';
                btn.disabled = true;
            });
        });

        // RAG index buttons
        const ragIndexBtn = this.settingsBody.querySelector('.rag-index-btn');
        if (ragIndexBtn) {
            ragIndexBtn.addEventListener('click', () => {
                this.send({ command: 'rag_index' });
                ragIndexBtn.textContent = 'Indexing...';
                ragIndexBtn.disabled = true;
            });
        }
        const ragForceBtn = this.settingsBody.querySelector('.rag-force-btn');
        if (ragForceBtn) {
            ragForceBtn.addEventListener('click', () => {
                this.send({ command: 'rag_index', force: true });
                ragForceBtn.textContent = 'Indexing...';
                ragForceBtn.disabled = true;
            });
        }
    }

    // ── Menu Bar ──────────────────────────────────────────────

    _bindMenuBar() {
        // Window controls (pywebview frameless mode)
        const hasNativeAPI = () => typeof pywebview !== 'undefined' && pywebview.api;
        const controls = document.getElementById('window-controls');
        const maxBtn = document.getElementById('win-maximize');

        const maximizeIconSvg = '<svg width="10" height="10" viewBox="0 0 10 10"><rect x="1" y="1" width="8" height="8" stroke="currentColor" stroke-width="1.2" fill="none"/></svg>';
        const restoreIconSvg = '<svg width="10" height="10" viewBox="0 0 10 10"><rect x="3" y="1" width="6" height="6" stroke="currentColor" stroke-width="1.1" fill="none"/><rect x="1" y="3" width="6" height="6" stroke="currentColor" stroke-width="1.1" fill="none"/></svg>';

        const updateMaxIcon = (isMaximized) => {
            if (maxBtn) maxBtn.innerHTML = isMaximized ? restoreIconSvg : maximizeIconSvg;
        };

        const doToggleMaximize = async () => {
            if (!hasNativeAPI()) return;
            const result = await pywebview.api.toggle_maximize();
            updateMaxIcon(result);
        };

        const wireControls = () => {
            if (hasNativeAPI()) {
                document.getElementById('win-minimize')?.addEventListener('click', () => pywebview.api.minimize());
                maxBtn?.addEventListener('click', doToggleMaximize);
                document.getElementById('win-close')?.addEventListener('click', () => pywebview.api.close());
                // Double-click title bar to toggle maximize
                document.getElementById('app-titlebar')?.addEventListener('dblclick', (e) => {
                    if (e.target.closest('.titlebar-menus') || e.target.closest('.titlebar-window-controls')
                        || e.target.closest('.titlebar-icon-btn') || e.target.closest('.header-badge')) return;
                    doToggleMaximize();
                });
            } else if (controls) {
                controls.style.display = 'none';
            }
        };

        if (hasNativeAPI()) wireControls();
        else setTimeout(wireControls, 1000);

        document.querySelectorAll('.menubar-action[data-action]').forEach(el => {
            el.addEventListener('click', () => {
                const action = el.dataset.action;
                switch (action) {
                    case 'new-agent': document.getElementById('new-agent-btn')?.click(); break;
                    case 'open-folder': document.getElementById('project-selector')?.click(); break;
                    case 'settings': this.switchView('settings'); break;
                    case 'cmd-palette': this.openCommandPalette(); break;
                    case 'toggle-sidebar': document.getElementById('sidebar-toggle')?.click(); break;
                    case 'toggle-preview': document.getElementById('preview-toggle')?.click(); break;
                    case 'shortcuts': this.toggleShortcutsOverlay(); break;
                    case 'save-diagnostics':
                        // v0.3.4 — Help → Save Diagnostics. Bundles
                        // redacted logs / intent audits / settings into
                        // a ZIP under ~/Downloads. The user attaches
                        // that to a GitHub issue. The result event
                        // (`diagnostics_saved`) shows the path so the
                        // user knows where it landed.
                        this.showStatusMessage('Bundling diagnostics…');
                        this.send({ command: 'save_diagnostics' });
                        break;
                    case 'about': this.showStatusMessage('Resonant Client — Build software with agents'); break;
                }
            });
        });
    }

    // ── Appearance ─────────────────────────────────────────────

    _applyAppearance(key, value) {
        if (key === 'theme') {
            document.documentElement.setAttribute('data-theme', value === 'light' ? 'light' : '');
            localStorage.setItem('resonant:theme', value);
        } else if (key === 'density') {
            document.documentElement.setAttribute('data-density', value === 'compact' ? 'compact' : '');
            localStorage.setItem('resonant:density', value);
        } else if (key === 'font_size') {
            const px = parseFloat(value) || 13.5;
            document.documentElement.style.setProperty('--text-base', px + 'px');
            localStorage.setItem('resonant:font-size', String(px));
        }
    }

    _restoreAppearance() {
        const theme = localStorage.getItem('resonant:theme');
        if (theme === 'light') document.documentElement.setAttribute('data-theme', 'light');
        const density = localStorage.getItem('resonant:density');
        if (density === 'compact') document.documentElement.setAttribute('data-density', 'compact');
        const fontSize = localStorage.getItem('resonant:font-size');
        if (fontSize) document.documentElement.style.setProperty('--text-base', fontSize + 'px');
    }

    // ── Keyboard Shortcuts ──────────────────────────────────────

    _handleKeyboardShortcut(e) {
        // Don't intercept when typing in inputs (except specific combos)
        const tag = e.target.tagName.toLowerCase();
        const inInput = tag === 'input' || tag === 'textarea' || tag === 'select';

        // Ctrl/Cmd+K → command palette
        if ((e.ctrlKey || e.metaKey) && e.key === 'k') {
            e.preventDefault();
            const overlay = document.getElementById('command-palette');
            if (overlay && overlay.style.display !== 'none') this.closeCommandPalette();
            else this.openCommandPalette();
            return;
        }

        // Ctrl+/ or Ctrl+? → shortcuts help
        if ((e.ctrlKey || e.metaKey) && (e.key === '/' || e.key === '?')) {
            e.preventDefault();
            this.toggleShortcutsOverlay();
            return;
        }

        // Escape → close overlays (already handled elsewhere, but also close shortcuts)
        if (e.key === 'Escape') {
            const cp = document.getElementById('command-palette');
            if (cp && cp.style.display !== 'none') {
                this.closeCommandPalette();
                e.preventDefault();
                return;
            }
            const so = document.getElementById('shortcuts-overlay');
            if (so && so.style.display !== 'none') {
                so.style.display = 'none';
                e.preventDefault();
                return;
            }
        }

        // Don't intercept other shortcuts when in input
        if (inInput) return;

        // Ctrl+N → new session
        if ((e.ctrlKey || e.metaKey) && e.key === 'n') {
            e.preventDefault();
            document.getElementById('new-agent-btn')?.click();
            return;
        }

        // Ctrl+, → settings
        if ((e.ctrlKey || e.metaKey) && e.key === ',') {
            e.preventDefault();
            this.switchView('settings');
            return;
        }

        // Ctrl+Shift+D → toggle sidebar
        if ((e.ctrlKey || e.metaKey) && e.shiftKey && e.key === 'D') {
            e.preventDefault();
            document.getElementById('sidebar-toggle')?.click();
            return;
        }

        // Alt+1 / Alt+2 → switch between the two remaining views (Agents / Settings)
        if (e.altKey && (e.key === '1' || e.key === '2')) {
            e.preventDefault();
            const views = ['agents', 'settings'];
            this.switchView(views[parseInt(e.key) - 1]);
            return;
        }
    }

    toggleShortcutsOverlay() {
        const overlay = document.getElementById('shortcuts-overlay');
        if (!overlay) return;

        const visible = overlay.style.display !== 'none';
        overlay.style.display = visible ? 'none' : 'flex';

        if (!visible) {
            // Render shortcuts
            const body = document.getElementById('shortcuts-body');
            if (!body) return;
            const shortcuts = [
                { label: 'Command palette', keys: ['Ctrl', 'K'] },
                { label: 'New agent', keys: ['Ctrl', 'N'] },
                { label: 'Settings', keys: ['Ctrl', ','] },
                { label: 'Shortcuts help', keys: ['Ctrl', '/'] },
                { label: 'Toggle sidebar', keys: ['Ctrl', 'Shift', 'D'] },
                { label: 'Switch to Agent', keys: ['Alt', '1'] },
                { label: 'Switch to Automations', keys: ['Alt', '2'] },
                { label: 'Switch to Background', keys: ['Alt', '3'] },
                { label: 'Switch to Settings', keys: ['Alt', '4'] },
                { label: 'Close overlay', keys: ['Escape'] },
                { label: 'Send message', keys: ['Enter'] },
                { label: 'New line in message', keys: ['Shift', 'Enter'] },
            ];
            body.innerHTML = shortcuts.map(s => `
                <div class="shortcut-row">
                    <span class="shortcut-label">${s.label}</span>
                    <span class="shortcut-keys">${s.keys.map(k => `<span class="shortcut-key">${k}</span>`).join('')}</span>
                </div>
            `).join('');
        }
    }

    // ── Command Palette ─────────────────────────────────────────

    _cmdPaletteCommands() {
        return [
            { id: 'new-agent',  icon: '+', label: 'New agent',          hint: 'Ctrl+N',       action: () => document.getElementById('new-agent-btn')?.click() },
            { id: 'settings',   icon: '\u2699', label: 'Open Settings',            hint: 'Ctrl+,', action: () => this.switchView('settings') },
            { id: 'preview',    icon: '\u25A1', label: 'Toggle preview panel',     hint: '',        action: () => document.getElementById('preview-toggle')?.click() },
            { id: 'sidebar',    icon: '\u2261', label: 'Toggle sidebar',           hint: 'Ctrl+Shift+D', action: () => document.getElementById('sidebar-toggle')?.click() },
            { id: 'shortcuts',  icon: '\u2328', label: 'Keyboard shortcuts',       hint: 'Ctrl+/', action: () => this.toggleShortcutsOverlay() },
        ];
    }

    openCommandPalette() {
        const overlay = document.getElementById('command-palette');
        if (!overlay) return;
        overlay.style.display = 'flex';
        const input = document.getElementById('cmd-palette-input');
        if (input) { input.value = ''; input.focus(); }
        this._cmdPaletteIdx = 0;
        this._renderCommandPaletteResults('');

        const onKey = (e) => {
            if (e.key === 'Escape') {
                e.preventDefault();
                this.closeCommandPalette();
                document.removeEventListener('keydown', onKey, true);
                return;
            }
            if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
                e.preventDefault();
                const items = overlay.querySelectorAll('.cmd-palette-item');
                if (!items.length) return;
                items[this._cmdPaletteIdx]?.classList.remove('active');
                if (e.key === 'ArrowDown') this._cmdPaletteIdx = (this._cmdPaletteIdx + 1) % items.length;
                else this._cmdPaletteIdx = (this._cmdPaletteIdx - 1 + items.length) % items.length;
                items[this._cmdPaletteIdx]?.classList.add('active');
                items[this._cmdPaletteIdx]?.scrollIntoView({ block: 'nearest' });
                return;
            }
            if (e.key === 'Enter') {
                e.preventDefault();
                const items = overlay.querySelectorAll('.cmd-palette-item');
                if (items[this._cmdPaletteIdx]) items[this._cmdPaletteIdx].click();
                return;
            }
        };
        this._cmdPaletteKeyHandler = onKey;
        document.addEventListener('keydown', onKey, true);

        if (input) {
            input.addEventListener('input', () => {
                this._cmdPaletteIdx = 0;
                this._renderCommandPaletteResults(input.value);
            });
        }

        overlay.addEventListener('click', (e) => {
            if (e.target === overlay) this.closeCommandPalette();
        }, { once: true });
    }

    closeCommandPalette() {
        const overlay = document.getElementById('command-palette');
        if (overlay) overlay.style.display = 'none';
        if (this._cmdPaletteKeyHandler) {
            document.removeEventListener('keydown', this._cmdPaletteKeyHandler, true);
            this._cmdPaletteKeyHandler = null;
        }
    }

    _renderCommandPaletteResults(query) {
        const container = document.getElementById('cmd-palette-results');
        if (!container) return;
        const q = (query || '').toLowerCase().trim();
        let cmds = this._cmdPaletteCommands();
        if (q) cmds = cmds.filter(c => c.label.toLowerCase().includes(q));

        if (!cmds.length) {
            container.innerHTML = '<div class="cmd-palette-empty">No matching commands</div>';
            return;
        }
        container.innerHTML = cmds.map((c, i) => `
            <div class="cmd-palette-item${i === 0 ? ' active' : ''}" data-cmd-id="${c.id}">
                <span class="cmd-palette-item-icon">${this.escapeHtml(c.icon)}</span>
                <span class="cmd-palette-item-label">${this.escapeHtml(c.label)}</span>
                ${c.hint ? `<span class="cmd-palette-item-hint">${this.escapeHtml(c.hint)}</span>` : ''}
            </div>
        `).join('');

        container.querySelectorAll('.cmd-palette-item').forEach(el => {
            el.addEventListener('click', () => {
                const cmd = cmds.find(c => c.id === el.dataset.cmdId);
                if (cmd) { this.closeCommandPalette(); cmd.action(); }
            });
        });
    }

    // ── Status ──────────────────────────────────────────────────

    handleStatus(event) {
        this.lastModel = event.model || this.lastModel;
        this.lastStats = event.stats || this.lastStats;

        // Update header subtitle with model context
        if (this.lastModel) {
            const parts = [this.lastModel];
            if (this.lastStats) {
                const inp = this.lastStats.input_tokens;
                const out = this.lastStats.output_tokens;
                if (inp && out) parts.push(`${inp}\u2192${out} tok`);
                const sessionCost = this.lastStats.session_cost_usd;
                if (sessionCost) {
                    parts.push(`$${Number(sessionCost).toFixed(4)}`);
                }
            }
            this.tokenInfo.textContent = parts.join(' \u00B7 ');
        }

        // Enrich header status line
        if (this.headerStatus && this.lastModel) {
            this.headerStatus.textContent = this.lastModel;
        }
    }

    // ── Session End ─────────────────────────────────────────────

    _resetAgentRunSummary(userText) {
        this._agentRunSummary = {
            title: this._truncateAgentRunTitle(userText || ''),
            fileChanges: [],
            todos: null,
        };
        this._agentRunErrored = false;
        this._agentRunErrorMessage = '';
    }

    _removeLiveAgentTodoStrip() {
        if (this._liveAgentTodoEl && this._liveAgentTodoEl.parentNode) {
            this._liveAgentTodoEl.parentNode.removeChild(this._liveAgentTodoEl);
        }
        this._liveAgentTodoEl = null;
    }

    handleTodosUpdated(event) {
        const raw = event.todos || [];
        const done = typeof event.done === 'number'
            ? event.done
            : raw.filter((t) => t && t.done).length;
        const total = typeof event.total === 'number' ? event.total : raw.length;
        if (!this._agentRunSummary) {
            this._agentRunSummary = { title: '', fileChanges: [], todos: null };
        }
        this._agentRunSummary.todos = {
            items: raw,
            done,
            total,
        };
        this._syncLiveAgentTodoStrip(done, total, raw);
    }

    _syncLiveAgentTodoStrip(done, total, items) {
        if (this.isReplaying || total <= 0) return;
        const target = this.getRenderTarget();
        if (!target) return;

        const pct = Math.min(100, Math.round((done / total) * 100));
        const preview = (items || []).slice(0, 4).map((t) => {
            const mark = t.done ? '\u2713' : '\u25CB';
            const label = this.escapeHtml((t.text || '').slice(0, 64));
            return `<span class="agent-live-todo-item">${mark} ${label || '…'}</span>`;
        }).join('');

        if (!this._liveAgentTodoEl) {
            const el = document.createElement('div');
            el.className = 'agent-live-todo-strip';
            el.setAttribute('role', 'status');
            target.appendChild(el);
            this._liveAgentTodoEl = el;
        }
        this._liveAgentTodoEl.innerHTML = `
            <div class="agent-live-todo-bar" style="--agent-todo-pct:${pct}%"></div>
            <div class="agent-live-todo-row">
                <span class="agent-live-todo-count">${done} of ${total} to-dos</span>
                <span class="agent-live-todo-preview">${preview}</span>
            </div>
        `;
        this.scrollToBottom();
    }

    _truncateAgentRunTitle(text) {
        const t = String(text).replace(/\s+/g, ' ').trim();
        if (!t) return '';
        if (t.length <= 72) return t;
        return t.slice(0, 69) + '…';
    }

    _recordAgentFileChange(name, args, event) {
        if (!this._agentRunSummary) {
            this._agentRunSummary = { title: '', fileChanges: [], todos: null };
        }
        const path = String(args.path || '').replace(/\\/g, '/').trim();
        if (!path) return;

        let detail = '';
        if (name === 'file_write') {
            const lines = String(args.content || '').split('\n').length;
            detail = lines ? `Wrote ${lines} line${lines === 1 ? '' : 's'}` : 'Wrote file';
        } else if (name === 'file_edit') {
            const dl = event.diff_lines || [];
            let add = 0;
            let del = 0;
            for (const line of dl) {
                if (line.startsWith('+') && !line.startsWith('+++')) add++;
                if (line.startsWith('-') && !line.startsWith('---')) del++;
            }
            detail = add || del ? `Diff +${add} −${del}` : 'Edited';
        }

        const list = this._agentRunSummary.fileChanges;
        const idx = list.findIndex(c => c.path === path);
        const entry = { path, tool: name, detail };
        if (idx >= 0) list[idx] = entry;
        else list.push(entry);
    }

    _formatRunDuration(seconds) {
        const s = Math.max(0, Math.floor(seconds || 0));
        if (s < 60) return `${s}s`;
        const m = Math.floor(s / 60);
        const r = s % 60;
        return r ? `${m}m ${r}s` : `${m}m`;
    }

    _renderAgentRunCompleteCard(totalElapsed, totalSteps) {
        const summary = this._agentRunSummary || { title: '', fileChanges: [], todos: null };
        const n = Math.max(0, Math.floor(totalSteps || 0));
        const files = summary.fileChanges || [];
        const td = summary.todos;
        const errored = !!this._agentRunErrored;
        const errorMsg = this._agentRunErrorMessage || '';

        // UX fix #6 — when the active session is a Mission, anchor the
        // run-card title to the original feature description rather than
        // the user's last reply. Without this, every grill round produces
        // a card titled "Backend, agreed. New ClientCommand.EXPORT…" or
        // similar — which is confusing for a multi-turn mission.
        const sess = this._currentSessionSummary && this._currentSessionSummary();
        const ms = sess && sess.mission_state;
        const missionSeed = ms && ms.seed_feature ? ms.seed_feature : '';
        const title = missionSeed || summary.title || 'Agent task';

        // UX fix #7 — pull tool-call count from the per-turn aggregate
        // (which counts every tool.call event) so the progress label can
        // honestly say "3 steps · 7 tool calls" instead of "1 agent step"
        // when the model fanned out 7 inline tools across 3 step events.
        const tools = (this._currentTurn && this._currentTurn.toolCallCount) || 0;
        const stepsToolsLabel = (steps) => {
            const stepPart = `${steps} step${steps === 1 ? '' : 's'}`;
            if (tools > 0 && tools !== steps) {
                return `${stepPart} · ${tools} tool call${tools === 1 ? '' : 's'}`;
            }
            return stepPart;
        };

        let progressLabel;
        if (errored) {
            progressLabel = n > 0
                ? `Stopped after ${stepsToolsLabel(n)}`
                : 'Stopped';
        } else if (td && td.total > 0) {
            progressLabel = `${td.done} of ${td.total} to-dos completed`;
        } else if (n > 0) {
            progressLabel = stepsToolsLabel(n);
        } else {
            progressLabel = 'Completed';
        }

        const el = document.createElement('div');
        // Default to `compact` — click the banner to expand to the full panel
        // (file changes list, blurb, Review/Commit buttons). One-line by
        // default keeps completed turns from accumulating visual weight.
        const compactClass = errored ? 'agent-run-card agent-run-stopped compact' : 'agent-run-card compact';
        el.className = compactClass;

        // For HTML files, expose a "Preview" affordance — closes the loop so
        // the user doesn't have to spin up their own HTTP server to see the
        // result of a "build me a web app" task.
        const isPreviewable = (p) => /\.(html?|htm)$/i.test(p || '');
        const changesHtml = files.length
            ? `<ol class="agent-changes-list">${files.map((c) => {
                const previewable = isPreviewable(c.path);
                const preview = previewable
                    ? `<button type="button" class="agent-file-preview-btn" data-preview-path="${this.escapeHtml(c.path)}" title="Open this file in a new browser tab">Preview \u25B6</button>`
                    : '';
                return `
                <li class="agent-change-item">
                    <code class="agent-file-path" data-file-path="${this.escapeHtml(c.path)}" title="${this.escapeHtml(c.path)}">${this.escapeHtml(this.shortenPath(c.path))}</code>
                    <span class="agent-change-detail">${this.escapeHtml(c.detail || '')}</span>
                    ${preview}
                </li>`;
            }).join('')}
            </ol>`
            : '';

        const kicker = errored ? 'Stopped' : 'Build';
        const checkGlyph = errored ? '⚠' : '✓';

        // Hide Review / Commit actions when there is nothing actionable —
        // either the run errored out, or zero files were edited (a pure
        // exploration / Q&A turn shouldn't pretend it has changes to ship).
        const showActions = !errored && files.length > 0;
        const actionsHtml = showActions
            ? `<div class="agent-run-actions">
                    <button type="button" class="agent-run-btn agent-run-btn-primary" data-agent-run-action="review">Review</button>
                    <button type="button" class="agent-run-btn" data-agent-run-action="commit" disabled title="Not wired yet">Create branch &amp; commit</button>
                </div>`
            : '';

        const blurbHtml = errored
            ? `<p class="agent-run-blurb agent-run-error-blurb">${this.escapeHtml(errorMsg || 'Run stopped before completion.')}</p>`
            : `<p class="agent-run-blurb">Worked for ${this._formatRunDuration(totalElapsed)}.${files.length ? ' Edits below.' : ''}</p>`;

        // Compact summary line — always visible. The expanded sections
        // (todo strip, blurb, changes list, action buttons) live inside
        // .agent-run-card-detail and toggle on click of the banner.
        const summaryParts = [];
        if (files.length) summaryParts.push(`${files.length} file${files.length === 1 ? '' : 's'}`);
        if (totalElapsed > 0) summaryParts.push(this._formatRunDuration(totalElapsed));
        const summaryLine = summaryParts.length
            ? `<span class="agent-run-summary-meta">${this.escapeHtml('· ' + summaryParts.join(' · '))}</span>`
            : '';

        el.innerHTML = `
            <button type="button" class="agent-run-banner" data-agent-run-toggle aria-expanded="false">
                <span class="agent-run-kicker">${kicker}</span>
                <span class="agent-run-title">${this.escapeHtml(title)}</span>
                ${summaryLine}
                <span class="agent-run-banner-chevron" aria-hidden="true">▸</span>
            </button>
            <div class="agent-run-card-detail" hidden>
                <div class="agent-run-card-inner">
                    <div class="agent-todo-strip">
                        <span class="agent-todo-check" aria-hidden="true">${checkGlyph}</span>
                        <span class="agent-todo-text">${this.escapeHtml(progressLabel)}</span>
                    </div>
                    ${blurbHtml}
                    ${files.length ? `<div class="agent-changes-heading">Summary of changes</div>${changesHtml}` : ''}
                    ${actionsHtml}
                </div>
            </div>
        `;

        // Banner toggles the detail panel.
        const banner = el.querySelector('[data-agent-run-toggle]');
        const detail = el.querySelector('.agent-run-card-detail');
        const chev = el.querySelector('.agent-run-banner-chevron');
        if (banner && detail) {
            banner.addEventListener('click', () => {
                el.classList.toggle('compact');
                const isExpanded = !el.classList.contains('compact');
                detail.hidden = !isExpanded;
                if (chev) chev.textContent = isExpanded ? '▾' : '▸';
                banner.setAttribute('aria-expanded', isExpanded ? 'true' : 'false');
            });
        }

        // Wire the "Review" button (only present in code mode — actionsHtml is non-empty)
        const reviewBtn = el.querySelector('[data-agent-run-action="review"]');
        if (reviewBtn) {
            reviewBtn.addEventListener('click', (e) => {
                // Don't bubble into the banner toggle.
                e.stopPropagation();
                const gitBadge = document.getElementById('git-badge');
                if (gitBadge && gitBadge.style.display !== 'none') gitBadge.click();
                else this.showStatusMessage('Use the git status control in the header when available.');
            });
        }

        // Wire per-file "Preview" buttons for previewable artifacts (HTML).
        // window.open with file:// works in pywebview and most desktop Chrome
        // setups; if the browser blocks it we still show a clear status message
        // with the absolute path so the user can copy/paste.
        el.querySelectorAll('[data-preview-path]').forEach((btn) => {
            btn.addEventListener('click', () => {
                const p = btn.dataset.previewPath || '';
                if (!p) return;
                const url = 'file:///' + p.replace(/\\\\/g, '/').replace(/^\//, '');
                let win = null;
                try { win = window.open(url, '_blank', 'noopener'); } catch (e) {}
                if (!win) {
                    this.showStatusMessage(`Browser blocked file:// navigation. Open this manually: ${p}`);
                }
            });
        });

        // Make file paths copyable on click
        el.querySelectorAll('[data-file-path]').forEach((codeEl) => {
            codeEl.style.cursor = 'pointer';
            codeEl.addEventListener('click', () => {
                const p = codeEl.dataset.filePath || '';
                if (!p) return;
                if (navigator.clipboard?.writeText) {
                    navigator.clipboard.writeText(p);
                    this.showStatusMessage(`Copied: ${p}`);
                }
            });
        });

        this.chatMessages.appendChild(el);
        this._removeLiveAgentTodoStrip();
    }

    handleSessionEnd(event) {
        this.removeThinking();
        this._removeLiveAgentTodoStrip();

        // Clear terminal bar
        this.clearTerminals();

        // Finalize CLI tool activity group
        this.finalizeToolActivityGroup();

        // Flush collapsed group
        this.flushCollapsedGroup();

        const totalElapsed = event.total_elapsed || 0;
        const totalSteps = event.total_steps || 0;

        // Per-turn footer — single dim line below the assistant's prose,
        // replaces the per-step "▣ model · tokens · 1.2s" footer that used
        // to repeat after every step.
        this._renderTurnFooter();

        const fileCount = (this._agentRunSummary && this._agentRunSummary.fileChanges)
            ? this._agentRunSummary.fileChanges.length
            : 0;
        const todoTotal = (this._agentRunSummary && this._agentRunSummary.todos)
            ? (this._agentRunSummary.todos.total || 0)
            : 0;
        const showRunCard = (
            totalSteps >= 1 || fileCount > 0 || todoTotal > 0
        );

        if (showRunCard) {
            const stepsForCard = totalSteps > 0 ? totalSteps : (fileCount > 0 ? 1 : 0);
            this._renderAgentRunCompleteCard(totalElapsed, stepsForCard);
        } else if (totalSteps > 1) {
            const el = document.createElement('div');
            el.className = 'session-end';
            el.innerHTML = `<span class="check">✓</span> Done · ${totalSteps} steps · ${totalElapsed.toFixed(1)}s`;
            this.chatMessages.appendChild(el);
        }

        this.setRunning(false);

        // Follow-up chips (not during replay)
        if (!this.isReplaying) {
            this._renderFollowUpChips();
        }

        this.scrollToBottom();

        // Refresh git status after session (files may have changed)
        if (!this.isReplaying) {
            this.requestGitStatus();
        }
    }

    _renderFollowUpChips() {
        const suggestions = [];
        const fc = (this._agentRunSummary && this._agentRunSummary.fileChanges) || [];
        if (fc.length > 0) {
            suggestions.push('Run tests');
            suggestions.push('Explain the changes');
        }
        suggestions.push('Continue');
        if (!suggestions.length) return;

        const el = document.createElement('div');
        el.className = 'follow-up-chips';
        el.innerHTML = suggestions.map(s =>
            `<button class="follow-up-chip">${this.escapeHtml(s)}</button>`
        ).join('');
        el.querySelectorAll('.follow-up-chip').forEach(btn => {
            btn.addEventListener('click', () => {
                el.remove();
                this.userInput.value = btn.textContent;
                this.sendMessage();
            });
        });
        this.chatMessages.appendChild(el);
    }

    // ── Subagents ───────────────────────────────────────────────

    handleSubagentStart(event) {
        this.removeThinking();
        this.ensureStepRendered();

        const agentType = event.agent_type || '';
        const prompt = event.prompt || '';
        const display = prompt.length > 100 ? prompt.slice(0, 97) + '...' : prompt;

        const el = document.createElement('div');
        el.className = 'subagent-block';
        el.setAttribute('data-agent-type', agentType);

        const header = document.createElement('div');
        header.className = 'subagent-header';
        header.innerHTML = `
            <span class="subagent-toggle">▸</span>
            <span class="subagent-label">Task</span>
            <span style="color:var(--muted);font-size:12px">${this.escapeHtml(agentType)}</span>
            <span class="subagent-prompt">"${this.escapeHtml(display)}"</span>
        `;

        const children = document.createElement('div');
        children.className = 'subagent-children';

        header.addEventListener('click', () => {
            el.classList.toggle('expanded');
            header.querySelector('.subagent-toggle').textContent =
                el.classList.contains('expanded') ? '▾' : '▸';
        });

        el.appendChild(header);
        el.appendChild(children);

        // Append to current render target
        const target = this.subagentContainer || this.chatMessages;
        target.appendChild(el);

        // Push nesting — child events render into this subagent's children div
        this.subagentDepth++;
        this.subagentContainer = children;

        this.scrollToBottom();
    }

    handleSubagentEnd(event) {
        const agentType = event.agent_type || '';
        const steps = event.steps || 0;
        const elapsed = event.elapsed || 0;

        // Find the current subagent block and add result footer
        if (this.subagentContainer) {
            const block = this.subagentContainer.closest('.subagent-block');
            if (block) {
                const result = document.createElement('div');
                result.className = 'subagent-result';
                result.textContent = `✓ ${agentType} · ${steps} steps · ${elapsed.toFixed(1)}s`;
                block.appendChild(result);

                // If subagent had content, auto-expand it
                if (this.subagentContainer.children.length > 0) {
                    block.classList.add('expanded');
                    const toggle = block.querySelector('.subagent-toggle');
                    if (toggle) toggle.textContent = '▾';
                }
            }
        }

        // Pop nesting
        this.subagentDepth = Math.max(0, this.subagentDepth - 1);
        if (this.subagentDepth === 0) {
            this.subagentContainer = null;
        } else {
            // Find parent subagent container
            const parentBlock = this.subagentContainer?.closest('.subagent-block')?.parentElement?.closest('.subagent-block');
            this.subagentContainer = parentBlock ? parentBlock.querySelector('.subagent-children') : null;
        }
    }

    // ── Error ───────────────────────────────────────────────────

    handleError(event) {
        this.removeThinking();
        this._finalizeLiveCollapsedGroup();
        this.ensureStepRendered();

        // v0.3.2 — release the mission_start in-flight guard on error so a
        // failed mission_start doesn't leave the user locked out of retries
        // for the full 6s safety timeout.
        if (this._missionStartInflight) {
            this._missionStartInflight = false;
            if (this._missionStartInflightTimer) {
                clearTimeout(this._missionStartInflightTimer);
                this._missionStartInflightTimer = null;
            }
        }

        // Track error state so the run-card can drop the "Build" framing and
        // hide Review/Commit actions when there's nothing successfully to act on.
        this._agentRunErrored = true;
        this._agentRunErrorMessage = event.message || '';

        const el = document.createElement('div');
        el.className = 'error-block';
        el.textContent = `✗ ${event.message || 'Unknown error'}`;
        this.getRenderTarget().appendChild(el);
        this.scrollToBottom();

        // If it was a fatal-ish error, stop running and clean up terminals
        if (event.message && (
            event.message.includes('step limit') ||
            event.message.includes('No backend') ||
            event.message.includes('Cancelled')
        )) {
            this.clearTerminals();
            this.setRunning(false);
        }
    }

    // ── Permission ──────────────────────────────────────────────

    handleToolPermission(event) {
        const name = event.name || '';
        const args = event.arguments || {};
        const review = event.review || null;

        // For file edits, render inline in the conversation rather than modal —
        // less interruption, persists in the chat history. Other tools (bash etc.)
        // still get the modal because they're more consequential.
        if ((name === 'file_edit' || name === 'file_write') && review && review.hunks) {
            this._renderInlineDiffPermission(name, args, review);
            return;
        }

        const titleEl = document.getElementById('permission-title');
        const riskEl = document.getElementById('permission-risk');
        const textEl = document.getElementById('permission-text');
        const warningsEl = document.getElementById('permission-warnings');
        const diffEl = document.getElementById('permission-diff');
        const detailsEl = document.getElementById('permission-details');

        // Reset
        warningsEl.style.display = 'none';
        warningsEl.innerHTML = '';
        diffEl.style.display = 'none';
        diffEl.innerHTML = '';
        detailsEl.style.display = 'block';
        riskEl.className = 'risk-badge';
        riskEl.textContent = '';

        if (review) {
            // Rich diff review mode
            titleEl.textContent = review.action === 'execute' ? 'Command Review' : 'Change Review';
            textEl.textContent = review.summary || `Allow ${name}?`;

            // Risk badge
            if (review.risk_level) {
                riskEl.textContent = review.risk_level;
                riskEl.className = `risk-badge risk-${review.risk_level}`;
            }

            // Warnings
            if (review.warnings && review.warnings.length > 0) {
                warningsEl.style.display = 'block';
                warningsEl.innerHTML = review.warnings
                    .map(w => `<div class="review-warning">${this.escapeHtml(w)}</div>`)
                    .join('');
            }

            // Bash command
            if (name === 'bash' && review.command) {
                diffEl.style.display = 'block';
                diffEl.innerHTML = `
                    <div class="review-command">
                        <div class="review-command-label">Command</div>
                        <div class="review-command-text">${this.escapeHtml(review.command)}</div>
                    </div>
                `;
                detailsEl.style.display = 'none';
            }
            // File diff
            else if (review.hunks && review.hunks.length > 0) {
                diffEl.style.display = 'block';
                let diffHtml = '';

                if (review.file_path) {
                    diffHtml += `<div class="diff-header">${this.escapeHtml(review.file_path)}</div>`;
                }

                for (const hunk of review.hunks) {
                    const hunkHeader = `@@ -${hunk.old_start},${hunk.old_count} +${hunk.new_start},${hunk.new_count} @@`;
                    diffHtml += `<div class="diff-hunk-header">${this.escapeHtml(hunkHeader)}${hunk.context ? ' ' + this.escapeHtml(hunk.context) : ''}</div>`;

                    for (const line of hunk.lines) {
                        let cls = 'diff-context';
                        if (line.startsWith('+')) cls = 'diff-add';
                        else if (line.startsWith('-')) cls = 'diff-remove';
                        diffHtml += `<div class="diff-line ${cls}">${this.escapeHtml(line)}</div>`;
                    }
                }

                diffEl.innerHTML = diffHtml;
                detailsEl.style.display = 'none'; // Hide raw JSON when we have a diff
            }
            // No diff, show raw args
            else {
                detailsEl.textContent = JSON.stringify(args, null, 2);
            }
        } else {
            // Basic permission mode (no review data)
            titleEl.textContent = 'Tool Approval';
            textEl.textContent = `Allow ${name}?`;
            detailsEl.textContent = JSON.stringify(args, null, 2);
        }

        document.getElementById('permission-dialog').style.display = 'flex';
    }

    /** Render an inline diff card with accept/reject buttons in the chat stream. */
    _renderInlineDiffPermission(toolName, args, review) {
        const block = document.createElement('div');
        block.className = 'inline-diff';

        const filePath = review.file_path || args.path || '(no path)';
        const additions = (review.hunks || []).reduce(
            (sum, h) => sum + (h.lines || []).filter(l => l.startsWith('+') && !l.startsWith('+++')).length, 0);
        const deletions = (review.hunks || []).reduce(
            (sum, h) => sum + (h.lines || []).filter(l => l.startsWith('-') && !l.startsWith('---')).length, 0);

        let diffHtml = '';
        const hunks = review.hunks || [];
        const showAll = hunks.length <= 5;
        const visibleHunks = showAll ? hunks : hunks.slice(0, 5);
        for (const hunk of visibleHunks) {
            const hunkHeader = `@@ -${hunk.old_start},${hunk.old_count} +${hunk.new_start},${hunk.new_count} @@`;
            diffHtml += `<div class="diff-hunk-header">${this.escapeHtml(hunkHeader)}${hunk.context ? ' ' + this.escapeHtml(hunk.context) : ''}</div>`;
            for (const line of hunk.lines) {
                let cls = 'diff-context';
                if (line.startsWith('+') && !line.startsWith('+++')) cls = 'diff-add';
                else if (line.startsWith('-') && !line.startsWith('---')) cls = 'diff-remove';
                diffHtml += `<div class="diff-line ${cls}">${this.escapeHtml(line)}</div>`;
            }
        }
        if (!showAll) {
            diffHtml += `<div class="diff-truncated">…(${hunks.length - 5} more hunks — accept to apply all)</div>`;
        }

        const verb = toolName === 'file_write' ? 'Write' : 'Edit';
        block.innerHTML = `
            <div class="inline-diff-header">
                <span class="inline-diff-verb">${verb}</span>
                <code class="inline-diff-path" title="${this.escapeHtml(filePath)}">${this.escapeHtml(filePath)}</code>
                <span class="inline-diff-stats">+${additions} -${deletions}</span>
            </div>
            <div class="inline-diff-body">${diffHtml}</div>
            <div class="inline-diff-actions">
                <button class="inline-diff-btn inline-diff-reject" data-action="reject">Reject</button>
                <button class="inline-diff-btn inline-diff-accept" data-action="accept">Accept</button>
            </div>
        `;

        const onDecide = (approved) => {
            this.send({ command: 'approve', approved });
            block.querySelectorAll('button').forEach(b => b.disabled = true);
            const summary = document.createElement('div');
            summary.className = 'inline-diff-summary ' + (approved ? 'accepted' : 'rejected');
            summary.textContent = approved
                ? `✓ Accepted ${verb.toLowerCase()}: ${filePath} (+${additions} -${deletions})`
                : `✗ Rejected ${verb.toLowerCase()}: ${filePath}`;
            block.replaceWith(summary);
        };
        block.querySelector('[data-action="accept"]').addEventListener('click', () => onDecide(true));
        block.querySelector('[data-action="reject"]').addEventListener('click', () => onDecide(false));

        const target = this.getRenderTarget ? this.getRenderTarget() : this.chatMessages;
        target.appendChild(block);
        this.scrollToBottom();
    }

    // ── Choices ─────────────────────────────────────────────────

    handleChoices(event) {
        // For now, auto-select first choice
        const options = event.options || [];
        if (options.length > 0) {
            this.send({ command: 'choice_select', selected: options[0] });
        }
    }

    // ── await_user (v0.3.5) ─────────────────────────────────────

    /**
     * Render an inline prompt for the agent's `await_user` tool. The
     * agent is blocked on the backend's `state.user_input_response`
     * threading.Event; replying via the `user_input` command unblocks
     * it. We render *inline in the chat* (not as a modal overlay) so
     * the question reads as part of the conversation flow — modals
     * yank focus and feel adversarial here. Quick-reply chips appear
     * when the tool was called with options; otherwise a plain
     * textarea with a Send button.
     */
    handleAwaitUser(event) {
        const question = (event && event.question) || '';
        const options = Array.isArray(event && event.options) ? event.options : [];
        if (!question) return;

        const wrap = document.createElement('div');
        wrap.className = 'await-user-prompt';

        // Chips for options. Plain textarea + Send for free-text.
        const chipsHTML = options.length > 0
            ? `<div class="await-user-chips">${
                options.map(o => `<button type="button" class="await-user-chip">${this.escapeHtml(o)}</button>`).join('')
              }</div>`
            : '';

        wrap.innerHTML = `
            <div class="await-user-header">
                <span class="await-user-icon" aria-hidden="true">❓</span>
                <span class="await-user-label">Agent needs your input</span>
            </div>
            <div class="await-user-question">${this.escapeHtml(question)}</div>
            ${chipsHTML}
            <div class="await-user-input-row">
                <textarea class="await-user-input" rows="2"
                    placeholder="${options.length > 0 ? 'Or type a different answer…' : 'Type your answer…'}"></textarea>
                <button type="button" class="await-user-send">Send</button>
            </div>
        `;

        const reply = (text) => {
            // One-shot — disable the whole prompt after answering so
            // the user can't accidentally double-send. The backend
            // immediately resumes; we don't need an "answer received"
            // animation.
            wrap.classList.add('await-user-answered');
            wrap.querySelectorAll('button, textarea').forEach(el => el.disabled = true);
            const textarea = wrap.querySelector('.await-user-input');
            if (textarea && text !== textarea.value) textarea.value = text;
            this.send({ command: 'user_input', response: text });
        };

        wrap.querySelectorAll('.await-user-chip').forEach(btn => {
            btn.addEventListener('click', () => reply(btn.textContent || ''));
        });

        const textarea = wrap.querySelector('.await-user-input');
        const sendBtn = wrap.querySelector('.await-user-send');
        sendBtn.addEventListener('click', () => {
            const v = textarea.value.trim();
            if (v) reply(v);
        });
        textarea.addEventListener('keydown', (e) => {
            if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) {
                e.preventDefault();
                sendBtn.click();
            }
        });

        this.chatMessages.appendChild(wrap);
        this.scrollToBottom();
        setTimeout(() => textarea.focus(), 100);
    }

    // ── DOM Helpers ─────────────────────────────────────────────

    /**
     * Pi-style shell shortcut: render a "running" snippet in chat and ask the
     * server to execute the command. Output lands via handleShellExecResult.
     *
     * `feedToLlm=true` (single-bang `!cmd`) — the server feeds the output back
     * to the model after the command finishes, kicking off a normal assistant
     * turn. The snippet stays in chat as the visible representation of the
     * "user message" the model is responding to.
     *
     * `feedToLlm=false` (double-bang `!!cmd`) — pure side-channel. Output is
     * displayed in chat for the user only; the model is not involved.
     */
    _runShellShortcut(cmd, feedToLlm) {
        this._removeChatEmptyState();
        const reqId = `shell-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;

        if (!this._pendingShellExec) this._pendingShellExec = new Map();
        const el = this._renderShellSnippetRunning(cmd, feedToLlm);
        this._pendingShellExec.set(reqId, { cmd, feedToLlm, el });

        this.send({
            command: 'shell_exec',
            cmd,
            feed_to_llm: feedToLlm,
            request_id: reqId,
        });

        if (feedToLlm) {
            // Reset agent-run state — the LLM turn that follows is a fresh turn.
            this._resetAgentRunSummary(`!${cmd}`);
            if (this._renderTimer) {
                clearTimeout(this._renderTimer);
                this._renderTimer = null;
            }
            this._lastStreamParseAt = 0;
            this.streamBuffer = '';
            this.isStreaming = false;
            this.currentMessageEl = null;
            this.currentStepEvent = null;
            this.stepToolCalls = [];
            this.stepToolResults = [];
            this.stepIsInlineOnly = true;
            this.stepRendered = false;
            this.collapsedGroup = [];
            this._liveCollapsedGroup = null;
        }
    }

    _renderShellSnippetRunning(cmd, feedToLlm) {
        const el = document.createElement('div');
        el.className = `shell-snippet ${feedToLlm ? 'fed' : 'silent'} running`;
        el.innerHTML = `
            <div class="shell-snippet-header">
                <span class="shell-snippet-prompt">${feedToLlm ? '!' : '!!'}</span>
                <code class="shell-snippet-cmd"></code>
                <span class="shell-snippet-status">running…</span>
            </div>
            <pre class="shell-snippet-output" data-empty="true">running…</pre>
        `;
        el.querySelector('.shell-snippet-cmd').textContent = cmd;
        this.chatMessages.appendChild(el);
        this.scrollToBottom();
        return el;
    }

    handleShellExecResult(event) {
        const reqId = event.request_id || '';
        const pending = this._pendingShellExec ? this._pendingShellExec.get(reqId) : null;
        if (!pending) return;
        this._pendingShellExec.delete(reqId);

        const el = pending.el;
        if (!el) return;

        el.classList.remove('running');
        if (!event.ok) el.classList.add('errored');

        const elapsed = typeof event.elapsed === 'number' ? event.elapsed.toFixed(2) : '0.00';
        const statusText = event.ok
            ? `done · ${elapsed}s`
            : `failed (exit ${event.exit_code ?? '?'}) · ${elapsed}s`;
        el.querySelector('.shell-snippet-status').textContent = statusText;

        const out = event.output || '(no output)';
        const outputEl = el.querySelector('.shell-snippet-output');
        outputEl.textContent = out;
        outputEl.dataset.empty = out ? 'false' : 'true';

        // For `!cmd` (feed_to_llm), the backend automatically kicks off a
        // session.run after this event; the streaming events that follow
        // arrive on the same websocket. We've already cleared streaming
        // state in _runShellShortcut, so the assistant message will render
        // normally below the snippet.
        this.scrollToBottom();
    }

    // ── @-file Fuzzy Autocomplete ─────────────────────────────────────
    //
    // Inspired by pi's `@`-prefixed file references. Type `@` in the input
    // → popup opens with project files; continue typing to filter; arrow
    // keys to navigate, Tab/Enter to insert, Escape to dismiss. The path
    // is inserted as-is (project-relative) so the model can resolve it via
    // its existing path-resolution logic.

    handleProjectFiles(event) {
        // The backend may emit project_files for other unrelated requests
        // in the future, but for now there's only the autocomplete consumer.
        this._fuzzyFiles = Array.isArray(event.files) ? event.files : [];
        this._fuzzyFilesLoading = false;
        if (this._fuzzyFilesPending) {
            this._fuzzyFilesPending = false;
            // User triggered `@` while we were still loading — open the popup now.
            this._updateFileFuzzyState();
        }
    }

    _ensureFuzzyFilesLoaded() {
        if (this._fuzzyFiles !== null) return true;
        if (!this._fuzzyFilesLoading) {
            this._fuzzyFilesLoading = true;
            this.send({ command: 'list_project_files', request_id: 'fuzzy-init' });
        }
        return false;
    }

    /**
     * Read the current input value + caret position. If the caret is
     * immediately after an `@` token (no whitespace between `@` and the
     * caret), open or update the fuzzy popup with the token as the query.
     * Otherwise close the popup. Called from input/keyup handlers.
     */
    _updateFileFuzzyState() {
        if (!this.userInput) return;
        const value = this.userInput.value;
        const caret = this.userInput.selectionStart || 0;

        // Walk backwards from caret to find an `@` that's preceded by
        // whitespace or start-of-string and has no whitespace after it.
        let atPos = -1;
        for (let i = caret - 1; i >= 0; i--) {
            const ch = value[i];
            if (ch === '@') {
                const before = i === 0 ? ' ' : value[i - 1];
                if (/\s/.test(before)) atPos = i;
                break;
            }
            if (/\s/.test(ch)) break;
        }

        if (atPos < 0) {
            this._closeFileFuzzy();
            return;
        }

        const query = value.slice(atPos + 1, caret);
        if (!this._ensureFuzzyFilesLoaded()) {
            this._fuzzyFilesPending = true;
            return;
        }

        this._fuzzyAtPos = atPos;
        this._fuzzyQuery = query;
        this._fuzzyMatches = this._computeFuzzyMatches(query);
        this._fuzzyIdx = 0;
        this._renderFileFuzzy();
    }

    _computeFuzzyMatches(query) {
        const files = this._fuzzyFiles || [];
        if (!query) {
            return files.slice(0, 10);
        }
        const q = query.toLowerCase();
        const scored = [];
        for (const f of files) {
            const score = this._fuzzyScore(q, f.toLowerCase());
            if (score > 0) scored.push({ f, score });
        }
        // Higher score first. Within equal scores, shorter paths win — the
        // closest match in a deeply nested project is usually what you want.
        scored.sort((a, b) => b.score - a.score || a.f.length - b.f.length);
        return scored.slice(0, 10).map((s) => s.f);
    }

    /**
     * Subsequence fuzzy score: every char of query must appear in path in
     * order. Bonuses for: matching the basename, consecutive char runs,
     * matching at a path-segment boundary. Returns 0 if no match.
     */
    _fuzzyScore(query, path) {
        let qi = 0;
        let score = 0;
        let consecutive = 0;
        const slashIdx = path.lastIndexOf('/');
        const baseStart = slashIdx >= 0 ? slashIdx + 1 : 0;
        for (let i = 0; i < path.length && qi < query.length; i++) {
            if (path[i] === query[qi]) {
                let bonus = 1;
                if (i === 0 || path[i - 1] === '/' || path[i - 1] === '-' || path[i - 1] === '_' || path[i - 1] === '.') bonus += 3;
                if (i >= baseStart) bonus += 2;
                consecutive = path[i - 1] === query[qi - 1] ? consecutive + 1 : 0;
                score += bonus + consecutive;
                qi++;
            }
        }
        return qi === query.length ? score : 0;
    }

    _renderFileFuzzy() {
        if (!this._fuzzyPopupEl) {
            const el = document.createElement('div');
            el.className = 'file-fuzzy-popup';
            // Position relative to the input bar (parent of userInput).
            const anchor = this.userInput.closest('.input-bar') || this.userInput.parentElement;
            if (anchor && getComputedStyle(anchor).position === 'static') {
                anchor.style.position = 'relative';
            }
            (anchor || document.body).appendChild(el);
            this._fuzzyPopupEl = el;
        }
        const el = this._fuzzyPopupEl;
        el.innerHTML = '';

        if (this._fuzzyMatches.length === 0) {
            el.innerHTML = `
                <div class="file-fuzzy-empty">No files match "${this.escapeHtml(this._fuzzyQuery)}"</div>
            `;
            this._fuzzyOpen = true;
            el.style.display = 'block';
            return;
        }

        const list = document.createElement('div');
        list.className = 'file-fuzzy-list';
        this._fuzzyMatches.forEach((path, idx) => {
            const row = document.createElement('div');
            row.className = 'file-fuzzy-item' + (idx === this._fuzzyIdx ? ' selected' : '');
            const slashIdx = path.lastIndexOf('/');
            const dir = slashIdx >= 0 ? path.slice(0, slashIdx + 1) : '';
            const base = slashIdx >= 0 ? path.slice(slashIdx + 1) : path;
            row.innerHTML = `
                <span class="file-fuzzy-base"></span><span class="file-fuzzy-dir"></span>
            `;
            row.querySelector('.file-fuzzy-base').textContent = base;
            row.querySelector('.file-fuzzy-dir').textContent = dir ? ` · ${dir}` : '';
            row.addEventListener('mousedown', (e) => {
                // mousedown so we win the race against blur → close.
                e.preventDefault();
                this._fuzzyIdx = idx;
                this._selectFileFuzzy();
            });
            list.appendChild(row);
        });
        el.appendChild(list);

        if (this._fuzzyFiles && this._fuzzyFiles.length > this._fuzzyMatches.length) {
            const hint = document.createElement('div');
            hint.className = 'file-fuzzy-hint';
            hint.textContent = `${this._fuzzyMatches.length} of ${this._fuzzyFiles.length} files · Tab/Enter to insert · Esc to dismiss`;
            el.appendChild(hint);
        }

        this._fuzzyOpen = true;
        el.style.display = 'block';
    }

    _handleFuzzyKeydown(e) {
        if (!this._fuzzyOpen) return false;
        if (e.key === 'ArrowDown') {
            e.preventDefault();
            this._fuzzyIdx = Math.min(this._fuzzyMatches.length - 1, this._fuzzyIdx + 1);
            this._renderFileFuzzy();
            return true;
        }
        if (e.key === 'ArrowUp') {
            e.preventDefault();
            this._fuzzyIdx = Math.max(0, this._fuzzyIdx - 1);
            this._renderFileFuzzy();
            return true;
        }
        if (e.key === 'Tab' || (e.key === 'Enter' && !e.shiftKey)) {
            if (this._fuzzyMatches.length > 0) {
                e.preventDefault();
                this._selectFileFuzzy();
                return true;
            }
        }
        if (e.key === 'Escape') {
            e.preventDefault();
            this._closeFileFuzzy();
            return true;
        }
        return false;
    }

    _selectFileFuzzy() {
        const path = this._fuzzyMatches[this._fuzzyIdx];
        if (!path) {
            this._closeFileFuzzy();
            return;
        }
        const value = this.userInput.value;
        const caret = this.userInput.selectionStart || 0;
        const before = value.slice(0, this._fuzzyAtPos);
        const after = value.slice(caret);
        // Insert as `@path ` so the model has a clear delimiter and the user
        // can keep typing. We don't try to be clever about quoting — paths
        // with spaces are rare enough; the user can wrap manually.
        const inserted = `@${path} `;
        this.userInput.value = before + inserted + after;
        const newCaret = before.length + inserted.length;
        this.userInput.selectionStart = newCaret;
        this.userInput.selectionEnd = newCaret;
        this.userInput.focus();
        this._closeFileFuzzy();
    }

    _closeFileFuzzy() {
        if (this._fuzzyPopupEl) this._fuzzyPopupEl.style.display = 'none';
        this._fuzzyOpen = false;
        this._fuzzyAtPos = -1;
        this._fuzzyQuery = '';
        this._fuzzyMatches = [];
        this._fuzzyIdx = 0;
    }

    addUserMessage(text, images = []) {
        this._removeChatEmptyState();
        const el = document.createElement('div');
        el.className = 'msg-user';

        // Build the content node with textContent (no template-literal
        // whitespace bleeding into the rendered output — A3 fix). Images
        // and the action row stay as innerHTML since their structure is
        // shape-fixed.
        const content = document.createElement('div');
        content.className = 'msg-user-content';
        if (images && images.length > 0) {
            const wrap = document.createElement('div');
            wrap.style.cssText = 'display:flex;gap:8px;margin-bottom:8px;flex-wrap:wrap';
            for (const img of images) {
                const thumb = document.createElement('img');
                thumb.src = img.dataUrl || `data:${img.media_type};base64,${img.data}`;
                thumb.alt = 'Attached';
                thumb.style.cssText = 'max-width:120px;max-height:80px;border-radius:4px;border:1px solid var(--border);cursor:pointer';
                thumb.addEventListener('click', () => this.showLightbox(thumb.src));
                wrap.appendChild(thumb);
            }
            content.appendChild(wrap);
        }
        const textNode = document.createElement('span');
        textNode.textContent = (text || '').trim();
        content.appendChild(textNode);
        el.appendChild(content);

        const actions = document.createElement('div');
        actions.className = 'msg-actions msg-actions-user';
        actions.innerHTML = `
            <button class="msg-action-btn" data-action="fork" title="Fork from this message">
                <svg width="14" height="14" viewBox="0 0 14 14" fill="none">
                    <circle cx="3" cy="3" r="1.5" stroke="currentColor" stroke-width="1.1"/>
                    <circle cx="11" cy="3" r="1.5" stroke="currentColor" stroke-width="1.1"/>
                    <circle cx="7" cy="11" r="1.5" stroke="currentColor" stroke-width="1.1"/>
                    <path d="M3 4.5V7c0 1 .8 1.8 1.8 1.8h4.4c1 0 1.8-.8 1.8-1.8V4.5" stroke="currentColor" stroke-width="1.1" fill="none"/>
                    <path d="M7 8.8v.7" stroke="currentColor" stroke-width="1.1"/>
                </svg>
            </button>`;
        actions.querySelector('[data-action="fork"]').addEventListener('click', () => {
            this._forkFromUserMessage(el);
        });
        el.appendChild(actions);

        this.chatMessages.appendChild(el);
        this.scrollToBottom();
    }

    /** Compute the index of `el` among .msg-user blocks, then fork. */
    _forkFromUserMessage(el) {
        if (!this.currentSessionId) return;
        const rows = Array.from(this.chatMessages.querySelectorAll('.msg-user'));
        const idx = rows.indexOf(el);
        if (idx < 0) return;
        if (!confirm(`Fork from this message? A new session will be created with the conversation up through this exchange.`)) return;
        this.send({
            command: 'fork_session',
            session_id: this.currentSessionId,
            user_message_index: idx,
        });
    }

    addAssistantMessage() {
        const el = document.createElement('div');
        el.className = 'msg-assistant';
        el.innerHTML = `
            <div class="message-content streaming-cursor"></div>
            <div class="msg-actions">
                <button class="msg-action-btn" data-action="copy" title="Copy to clipboard">
                    <svg width="14" height="14" viewBox="0 0 14 14" fill="none"><rect x="4" y="4" width="8" height="8" rx="1.2" stroke="currentColor" stroke-width="1.1"/><path d="M10 4V2.8A.8.8 0 009.2 2H2.8a.8.8 0 00-.8.8v6.4a.8.8 0 00.8.8H4" stroke="currentColor" stroke-width="1.1"/></svg>
                </button>
            </div>
        `;
        el.querySelector('[data-action="copy"]').addEventListener('click', () => {
            const text = el.querySelector('.message-content')?.textContent || '';
            navigator.clipboard.writeText(text).then(() => {
                const btn = el.querySelector('[data-action="copy"]');
                if (btn) { btn.innerHTML = '\u2713'; setTimeout(() => { btn.innerHTML = '<svg width="14" height="14" viewBox="0 0 14 14" fill="none"><rect x="4" y="4" width="8" height="8" rx="1.2" stroke="currentColor" stroke-width="1.1"/><path d="M10 4V2.8A.8.8 0 009.2 2H2.8a.8.8 0 00-.8.8v6.4a.8.8 0 00.8.8H4" stroke="currentColor" stroke-width="1.1"/></svg>'; }, 1500); }
            });
        });
        this.getRenderTarget().appendChild(el);
        return el;
    }

    addThinking() {
        const el = document.createElement('div');
        el.className = 'thinking-indicator';
        el.setAttribute('data-thinking', 'true');
        el.innerHTML = `
            <div class="thinking-dots">
                <span></span><span></span><span></span>
            </div>
            <span class="thinking-label">thinking</span>
            <span class="thinking-elapsed" data-elapsed></span>
        `;
        this.getRenderTarget().appendChild(el);
        this.scrollToBottom();
        // Surface elapsed time after 5s — users panic when "thinking" sits
        // silent for 60+s on a cold model load. Showing seconds proves it's alive.
        const startedAt = Date.now();
        const elapsedEl = el.querySelector('[data-elapsed]');
        const tick = () => {
            if (!el.isConnected) return;
            const s = Math.floor((Date.now() - startedAt) / 1000);
            if (s >= 5 && elapsedEl) elapsedEl.textContent = ` ${s}s`;
        };
        el._elapsedTimer = setInterval(tick, 1000);
    }

    removeThinking() {
        // Remove from current target or anywhere in chat
        const target = this.getRenderTarget();
        const el = target.querySelector('[data-thinking]') ||
                   this.chatMessages.querySelector('[data-thinking]');
        if (el) {
            if (el._elapsedTimer) clearInterval(el._elapsedTimer);
            el.remove();
        }
    }

    /**
     * Show a banner above the input bar saying "Loading <model> ...". Cloud /
     * big MoE models often take 30-90s to load on first use; this prevents the
     * user from staring at a silent "thinking" indicator and giving up.
     */
    _showWarmupBanner(modelLabel) {
        let banner = document.getElementById('model-warmup-banner');
        if (!banner) {
            banner = document.createElement('div');
            banner.id = 'model-warmup-banner';
            banner.className = 'model-warmup-banner';
            this.inputBar.parentNode.insertBefore(banner, this.inputBar);
        }
        banner.innerHTML = `
            <span class="warmup-spinner"></span>
            <span class="warmup-text">Warming up <code>${this.escapeHtml(modelLabel)}</code>
                <span class="warmup-hint">first load can take 30\u201390s on cloud / big-MoE models</span>
            </span>
            <span class="warmup-elapsed" data-warmup-elapsed>0s</span>
        `;
        const startedAt = Date.now();
        if (banner._timer) clearInterval(banner._timer);
        banner._timer = setInterval(() => {
            const elapsedEl = banner.querySelector('[data-warmup-elapsed]');
            if (elapsedEl) elapsedEl.textContent = `${Math.floor((Date.now() - startedAt) / 1000)}s`;
        }, 1000);
    }

    _hideWarmupBanner(elapsedS) {
        const banner = document.getElementById('model-warmup-banner');
        if (!banner) return;
        if (banner._timer) clearInterval(banner._timer);
        if (typeof elapsedS === 'number' && elapsedS > 1) {
            // Briefly show completion before fading
            banner.innerHTML = `<span class="warmup-text">Model loaded \u2014 ready in ${elapsedS}s.</span>`;
            banner.classList.add('warmup-complete');
            setTimeout(() => banner.remove(), 2000);
        } else {
            banner.remove();
        }
    }

    showStatusMessage(message) {
        // Brief toast-like status message
        const el = document.createElement('div');
        el.style.cssText = 'text-align:center;color:var(--muted);font-size:12px;padding:8px;';
        el.textContent = message;
        this.chatMessages.appendChild(el);
        this.scrollToBottom();
    }

    /**
     * v0.5.6a1 — render a dedicated banner for backend operational
     * status (e.g. Ollama 503 retry). Different from showStatusMessage
     * because:
     *   1. It shows ABOVE the in-flight "thinking" indicator so it's
     *      contextually attached to "why is this taking so long"
     *   2. It auto-fades after backoff_seconds + grace so the user
     *      gets the signal but it doesn't pollute the chat indefinitely
     *   3. Distinct visual treatment (warning yellow) so users know
     *      this isn't a terminal error
     *
     * Without this banner the user sees only a "thinking 90 s" counter
     * during a 503 retry storm — same as a real hang. Visible signal
     * is the difference between "Backend is alive, just busy" and
     * "Should I cancel and try again?"
     */
    handleBackendStatus(event) {
        if (!event || !event.kind) return;
        if (event.kind === 'ollama_retry') {
            this._renderOllamaRetryBanner(event);
        } else if (event.kind === 'ollama_exhausted') {
            this._renderOllamaExhaustedChip(event);
        }
        // Future kinds get their own renderers; swallow unknown kinds
        // silently rather than confuse the user with unfamiliar text.
    }

    /**
     * v0.6.4 (F2) — the backend exhausted its retry budget on a
     * transient 5xx (typically Ollama Cloud's 503 "Server
     * overloaded"). Unlike the per-retry banner, this chip is
     * PERSISTENT — it does NOT auto-fade. The v0.6.2 field run found
     * that when retries gave up, the transient banner cleared and
     * the chat went silent, leaving the user unable to tell whether
     * the agent was thinking, retrying, or fully stalled.
     *
     * The chip names the model, the attempt count, and offers two
     * affordances: jump to the model selector (the alt deepseek tier
     * hits a different cloud quota), and dismiss.
     */
    _renderOllamaExhaustedChip(event) {
        if (!this.chatMessages) return;
        const status = event.status_code || 503;
        const model = event.model || 'the model';
        const attempts = event.attempts || 4;

        // Suggest the other deepseek tier — pro and flash hit
        // separate cloud quotas, so switching often dodges the storm.
        let altSuggestion = 'a different model';
        if (/pro/i.test(model)) altSuggestion = 'deepseek-v4-flash:cloud';
        else if (/flash/i.test(model)) altSuggestion = 'deepseek-v4-pro:cloud';

        const reason = (status === 503)
            ? 'rate-limited (HTTP 503 — cloud overloaded)'
            : `returning transient ${status} errors`;

        const chip = document.createElement('div');
        chip.className = 'backend-status-banner backend-status-exhausted';
        chip.innerHTML = `
            <span class="backend-status-icon" aria-hidden="true">⛔</span>
            <span class="backend-status-text">
                <strong>${this.escapeHtml(model)}</strong> is ${this.escapeHtml(reason)} —
                gave up after ${attempts} attempts. The current step is stalled.
                <span class="backend-status-hint">Try switching to ${this.escapeHtml(altSuggestion)} (a separate cloud quota), or wait for the backend to recover.</span>
            </span>
            <span class="backend-status-actions">
                <button type="button" class="backend-status-btn backend-status-switch">Switch model</button>
                <button type="button" class="backend-status-btn backend-status-dismiss" aria-label="Dismiss">×</button>
            </span>
        `;
        chip.querySelector('.backend-status-switch').addEventListener('click', () => {
            // Guide the user to the model control rather than auto-
            // switching: a mid-mission model swap has correctness
            // caveats the user should own.
            if (this.modelSelector) {
                this.modelSelector.scrollIntoView({ behavior: 'smooth', block: 'center' });
                this.modelSelector.classList.add('model-selector-flash');
                setTimeout(() => this.modelSelector.classList.remove('model-selector-flash'), 1600);
                try { this.modelSelector.focus(); } catch (e) { /* non-fatal */ }
            }
        });
        chip.querySelector('.backend-status-dismiss').addEventListener('click', () => chip.remove());
        this.chatMessages.appendChild(chip);
        this.scrollToBottom();
    }

    _renderOllamaRetryBanner(event) {
        if (!this.chatMessages) return;
        const attempt = event.attempt || 1;
        const max = event.max || 4;
        const status = event.status_code || 0;
        const backoff = typeof event.backoff_seconds === 'number'
            ? event.backoff_seconds : 1.5;

        // Phrase the message based on the actual upstream status.
        // 503 is the common case (cloud overloaded); 502/504 are
        // gateway errors that look the same to the user.
        const reason = (status === 503)
            ? 'rate-limited (HTTP 503)'
            : `transient ${status} error`;

        const banner = document.createElement('div');
        banner.className = 'backend-status-banner backend-status-retry';
        banner.innerHTML = `
            <span class="backend-status-icon" aria-hidden="true">⚠</span>
            <span class="backend-status-text">
                Backend ${this.escapeHtml(reason)} — retrying in ${backoff.toFixed(1)}s
                <span class="backend-status-attempt">attempt ${attempt}/${max}</span>
            </span>
        `;
        this.chatMessages.appendChild(banner);
        this.scrollToBottom();

        // Fade out after the backoff completes + 1.5s grace so the
        // user catches it. We don't remove it instantly because the
        // user may have scrolled away; better to leave a fading trace.
        const fadeAfterMs = (backoff + 1.5) * 1000;
        setTimeout(() => {
            banner.classList.add('backend-status-banner-fading');
            setTimeout(() => banner.remove(), 400);
        }, fadeAfterMs);
    }

    /**
     * v0.3.4 — confirmation toast after Help → Save Diagnostics. Shows
     * the on-disk path + size, with a "copy path" button so the user
     * can paste straight into a GitHub issue.
     */
    _showDiagnosticsToast(zipPath, sizeBytes) {
        if (!zipPath) {
            this.showStatusMessage('Diagnostics saved (path unknown).');
            return;
        }
        const sizeKB = Math.max(1, Math.round((sizeBytes || 0) / 1024));
        const wrap = document.createElement('div');
        wrap.className = 'diagnostics-toast';
        wrap.innerHTML = `
            <div class="diagnostics-toast-row">
                <span class="diagnostics-toast-icon" aria-hidden="true">📦</span>
                <span class="diagnostics-toast-text">
                    Diagnostics ZIP saved
                    <span class="diagnostics-toast-meta">${sizeKB} KB · attach to a GitHub issue</span>
                </span>
            </div>
            <code class="diagnostics-toast-path">${this.escapeHtml(zipPath)}</code>
            <div class="diagnostics-toast-actions">
                <button type="button" class="diagnostics-toast-copy">Copy path</button>
                <button type="button" class="diagnostics-toast-dismiss">Dismiss</button>
            </div>
        `;
        wrap.querySelector('.diagnostics-toast-copy').addEventListener('click', async () => {
            try {
                await navigator.clipboard.writeText(zipPath);
                this.showStatusMessage('Path copied to clipboard.');
            } catch {
                this.showStatusMessage('Copy failed — select the path manually.');
            }
        });
        wrap.querySelector('.diagnostics-toast-dismiss').addEventListener('click', () => wrap.remove());
        this.chatMessages.appendChild(wrap);
        this.scrollToBottom();
    }

    /**
     * Auto-scroll the chat to the bottom — but ONLY if the user hasn't
     * deliberately scrolled up. Yanking someone's viewport back down while
     * they're trying to read older context is one of the most disorienting
     * things a chat UI can do; instead we mark the existing "↓" pill as
     * having new content and let them scroll on their own terms.
     */
    scrollToBottom() {
        if (this._userScrolledUp) {
            this._markScrollEndPillNew();
            return;
        }
        requestAnimationFrame(() => {
            if (!this.chatContainer) return;
            this.chatContainer.scrollTop = this.chatContainer.scrollHeight;
            this._syncChatScrollEndBtn();
        });
    }

    /** Force-scroll regardless of user state — used when the user clicks the pill. */
    forceScrollToBottom() {
        this._userScrolledUp = false;
        this._clearScrollEndPillNew();
        requestAnimationFrame(() => {
            if (!this.chatContainer) return;
            this.chatContainer.scrollTop = this.chatContainer.scrollHeight;
            this._syncChatScrollEndBtn();
        });
    }

    _isAtBottom() {
        if (!this.chatContainer) return true;
        const el = this.chatContainer;
        // 80px threshold = "close enough to bottom that incremental
        // streaming still feels like it's anchored to the live edge"
        return (el.scrollHeight - el.scrollTop - el.clientHeight) < 80;
    }

    _markScrollEndPillNew() {
        if (!this.chatScrollEndBtn) return;
        if (!this.chatScrollEndBtn.classList.contains('has-new')) {
            this.chatScrollEndBtn.dataset.defaultText =
                this.chatScrollEndBtn.dataset.defaultText || this.chatScrollEndBtn.textContent || 'Scroll';
            this.chatScrollEndBtn.textContent = '↓ New messages';
            this.chatScrollEndBtn.classList.add('has-new');
        }
        // Make sure it's visible even if our usual sync logic would hide it
        // (it shouldn't, since user is scrolled up by definition, but guard anyway).
        this.chatScrollEndBtn.style.display = 'flex';
    }

    _clearScrollEndPillNew() {
        if (!this.chatScrollEndBtn) return;
        if (this.chatScrollEndBtn.classList.contains('has-new')) {
            this.chatScrollEndBtn.classList.remove('has-new');
            this.chatScrollEndBtn.textContent = this.chatScrollEndBtn.dataset.defaultText || 'Scroll';
        }
    }

    _syncChatScrollEndBtn() {
        if (!this.chatScrollEndBtn || !this.chatContainer) return;
        const el = this.chatContainer;
        const room = el.scrollHeight - el.scrollTop - el.clientHeight;
        // Hide the pill when we're effectively at the bottom; if there's
        // pending "new" content we still keep it visible.
        const hasNew = this.chatScrollEndBtn.classList.contains('has-new');
        this.chatScrollEndBtn.style.display = (room < 120 && !hasNew) ? 'none' : 'flex';
    }

    // ── Session Replay ──────────────────────────────────────────

    /**
     * Enter replay mode: shows a scrubber over the conversation, lets the user
     * drag through events to "time-travel" through what the agent did.
     */
    enterReplayMode(events, title = '') {
        if (!Array.isArray(events) || events.length === 0) {
            this.showStatusMessage('No replay events for this session');
            return;
        }
        // Stash live state so we can restore on exit
        if (!this._replay) {
            this._replay = {
                stashedHTML: this.chatMessages.innerHTML,
                inputBarDisplay: this.inputBar.style.display,
                events: events,
                index: 0,
                playing: false,
                speed: 1,
                playTimer: null,
                title,
            };
        } else {
            this._replay.events = events;
            this._replay.index = 0;
        }

        // Build/show scrubber
        let scrubber = document.getElementById('replay-scrubber');
        if (!scrubber) {
            scrubber = document.createElement('div');
            scrubber.id = 'replay-scrubber';
            scrubber.className = 'replay-scrubber';
            scrubber.innerHTML = `
                <button id="replay-play" title="Play / pause">▶</button>
                <input id="replay-range" type="range" min="0" value="0" />
                <span id="replay-time">0/0</span>
                <select id="replay-speed">
                    <option value="1">1×</option>
                    <option value="2">2×</option>
                    <option value="4">4×</option>
                </select>
                <span class="replay-title-label" id="replay-title">${this.escapeHtml(title)}</span>
                <button id="replay-exit" title="Exit replay">✕ Exit</button>
            `;
            // Insert above input bar
            this.inputBar.parentNode.insertBefore(scrubber, this.inputBar);

            scrubber.querySelector('#replay-play').addEventListener('click', () => this._toggleReplayPlay());
            scrubber.querySelector('#replay-range').addEventListener('input', (ev) => {
                this._renderReplayUpTo(parseInt(ev.target.value, 10));
            });
            scrubber.querySelector('#replay-speed').addEventListener('change', (ev) => {
                this._replay.speed = parseInt(ev.target.value, 10) || 1;
                if (this._replay.playing) {
                    this._stopReplayTimer();
                    this._startReplayTimer();
                }
            });
            scrubber.querySelector('#replay-exit').addEventListener('click', () => this.exitReplayMode());
        } else {
            scrubber.querySelector('#replay-title').textContent = title;
        }

        const range = scrubber.querySelector('#replay-range');
        range.max = events.length;
        range.value = 0;
        scrubber.style.display = 'flex';
        this.inputBar.style.display = 'none';

        this._renderReplayUpTo(0);
    }

    exitReplayMode() {
        if (!this._replay) return;
        this._stopReplayTimer();
        this.chatMessages.innerHTML = this._replay.stashedHTML;
        this.inputBar.style.display = this._replay.inputBarDisplay || '';
        const scrubber = document.getElementById('replay-scrubber');
        if (scrubber) scrubber.style.display = 'none';
        this._replay = null;
        this.scrollToBottom();
    }

    _renderReplayUpTo(idx) {
        if (!this._replay) return;
        const events = this._replay.events;
        const clamped = Math.max(0, Math.min(events.length, idx));
        this._replay.index = clamped;
        // Wipe + replay events 0..clamped
        this.chatMessages.innerHTML = '';
        if (clamped > 0) this.replayDisplayEvents(events.slice(0, clamped));
        // Update label
        const time = document.getElementById('replay-time');
        const range = document.getElementById('replay-range');
        if (time) time.textContent = `${clamped}/${events.length}`;
        if (range && Number(range.value) !== clamped) range.value = clamped;
        this.scrollToBottom();
    }

    _toggleReplayPlay() {
        if (!this._replay) return;
        if (this._replay.playing) {
            this._stopReplayTimer();
        } else {
            this._startReplayTimer();
        }
    }

    _startReplayTimer() {
        if (!this._replay || this._replay.playing) return;
        this._replay.playing = true;
        const btn = document.getElementById('replay-play');
        if (btn) btn.textContent = '⏸';
        const tick = () => {
            if (!this._replay) return;
            if (this._replay.index >= this._replay.events.length) {
                this._stopReplayTimer();
                return;
            }
            this._renderReplayUpTo(this._replay.index + 1);
        };
        const period = Math.max(80, 600 / this._replay.speed);
        this._replay.playTimer = setInterval(tick, period);
    }

    _stopReplayTimer() {
        if (!this._replay) return;
        if (this._replay.playTimer) {
            clearInterval(this._replay.playTimer);
            this._replay.playTimer = null;
        }
        this._replay.playing = false;
        const btn = document.getElementById('replay-play');
        if (btn) btn.textContent = '▶';
    }

    replayDisplayEvents(events) {
        /**
         * Replay saved display events to rebuild the conversation UI.
         * This handles user_message, text.done, tool.call, tool.result,
         * step.start, step.end, session.end, subagent.start/end, and error events.
         *
         * We skip streaming deltas (text.delta, thinking.delta) — instead we
         * use text.done to render the final text in one shot.
         */

        // Reset all rendering state
        this.streamBuffer = '';
        this.isStreaming = false;
        this.currentMessageEl = null;
        this.currentStepEvent = null;
        this.stepToolCalls = [];
        this.stepToolResults = [];
        this.stepIsInlineOnly = true;
        this.stepRendered = false;
        this.collapsedGroup = [];
        this._liveCollapsedGroup = null;
        this._currentTurn = this._freshTurnAggregate();
        this._blockToolRows = new Map();
        this.subagentDepth = 0;
        this.subagentContainer = null;

        // Skip these event types during replay (streaming deltas, markers)
        const SKIP_REPLAY = new Set([
            'text.delta', 'thinking.delta', 'session.start', 'status',
            'init', 'status_msg', 'sessions_updated',
        ]);

        this.isReplaying = true;
        this._agentRunSummary = { title: '', fileChanges: [], todos: null };
        try {
            for (const event of events) {
                const type = event.event;

            // Skip ephemeral events
            if (SKIP_REPLAY.has(type)) continue;

            if (type === 'user_message') {
                this._resetAgentRunSummary(event.text || '');
                // Replay user message bubble
                this.addUserMessage(event.text);
                continue;
            }

            // For text.done — render the full text as a completed message
            if (type === 'text.done') {
                this.handleTextDoneReplay(event);
                continue;
            }

            // Step start/end, tool call/result, subagent — use normal handlers
            if (type === 'step.start') {
                this.handleStepStart(event);
            } else if (type === 'tool.call') {
                this.handleToolCall(event);
            } else if (type === 'tool.result') {
                this.handleToolResult(event);
            } else if (type === 'step.end') {
                this.handleStepEnd(event);
            } else if (type === 'session.end') {
                this.handleSessionEnd(event);
            } else if (type === 'todos.updated') {
                this.handleTodosUpdated(event);
            } else if (type === 'subagent.start') {
                this.handleSubagentStart(event);
            } else if (type === 'subagent.end') {
                this.handleSubagentEnd(event);
            } else if (type === 'error') {
                this.handleError(event);
            }
        }
        } finally {
            this.isReplaying = false;
        }

        // Flush any pending collapsed groups
        this.flushCollapsedGroup();

        // Ensure we're not in a running state after replay
        this.setRunning(false);
        this.clearTerminals();

        // Detect interrupted session — if last event isn't session.end,
        // the model was cut off mid-response
        if (events.length > 0) {
            const lastEvent = events[events.length - 1];
            const lastType = lastEvent.event;
            if (lastType !== 'session.end' && lastType !== 'error') {
                this.showResumeButton();
            }
        }

        // Scroll to bottom
        this.scrollToBottom();
    }

    showResumeButton() {
        const el = document.createElement('div');
        el.className = 'resume-banner';
        el.innerHTML = `
            <span class="resume-text">Session was interrupted</span>
            <button class="resume-btn">Resume</button>
        `;
        el.querySelector('.resume-btn').addEventListener('click', () => {
            el.remove();
            this.userInput.value = 'Continue where you left off.';
            this.sendMessage();
        });
        this.chatMessages.appendChild(el);
    }

    handleTextDoneReplay(event) {
        /**
         * Render a completed text block (used during replay instead of
         * streaming delta-by-delta).
         */
        const text = event.text || '';
        if (!text.trim()) return;

        // Ensure the step is rendered if needed
        this.ensureStepRendered();

        // Create a message element
        const el = document.createElement('div');
        el.className = 'message assistant';

        // Render markdown
        let html = text;
        if (typeof marked !== 'undefined') {
            html = marked.parse(text);
            if (typeof DOMPurify !== 'undefined') {
                html = DOMPurify.sanitize(html);
            }
        }

        el.innerHTML = `<div class="message-content markdown-body">${html}</div>`;

        // Syntax highlighting on code blocks
        el.querySelectorAll('pre code').forEach((block) => {
            if (typeof hljs !== 'undefined') hljs.highlightElement(block);
        });

        const container = this.subagentContainer || this.chatMessages;
        container.appendChild(el);
    }

    // ── Session List ─────────────────────────────────────────────

    _renderPinnedGroup(sessions) {
        const wrap = document.createElement('div');
        wrap.className = 'pinned-group';

        const header = document.createElement('div');
        header.className = 'pinned-group-header';
        header.innerHTML = `
            <span class="pinned-group-icon" aria-hidden="true">📌</span>
            <span class="pinned-group-title">Pinned</span>
            <span class="pinned-group-count">${sessions.length}</span>
        `;
        wrap.appendChild(header);

        const sortByUpdated = (a, b) => (b.updated_at || 0) - (a.updated_at || 0);
        [...sessions].sort(sortByUpdated).forEach(s => {
            wrap.appendChild(this._createTreeSessionRow(s));
        });

        this.sessionList.appendChild(wrap);
    }

    /**
     * Render the "Missions" sidebar group — split into Active and
     * Completed subsections (B6 fix). Active = drafting / planning /
     * executing / reviewing. Completed = exited / completed. Each
     * subsection has its own subheader; the Completed subsection is
     * collapsed by default to keep the sidebar compact when a project
     * accumulates archived missions.
     */
    _renderMissionsGroup(missions) {
        const ACTIVE_PHASES = new Set(['drafting', 'planning_dispatched', 'executing', 'reviewing']);
        const active = missions.filter(s => ACTIVE_PHASES.has(s.mission_state?.phase || ''));
        const inactive = missions.filter(s => !ACTIVE_PHASES.has(s.mission_state?.phase || ''));
        const sortByUpdated = (a, b) => (b.updated_at || 0) - (a.updated_at || 0);
        active.sort(sortByUpdated);
        inactive.sort(sortByUpdated);

        const wrap = document.createElement('div');
        wrap.className = 'mission-group';

        const header = document.createElement('div');
        header.className = 'mission-group-header';
        header.innerHTML = `
            <span class="mission-group-icon" aria-hidden="true">🎯</span>
            <span class="mission-group-title">Missions</span>
            <span class="mission-group-count">${missions.length}</span>
            <button type="button" class="mission-group-add" title="Start a Mission" aria-label="New mission">+</button>
        `;
        header.querySelector('.mission-group-add').addEventListener('click', (e) => {
            e.stopPropagation();
            this.openMissionComposer();
        });
        wrap.appendChild(header);

        if (active.length > 0) {
            const sub = this._createMissionSubsection('Active', active, /* defaultExpanded= */ true);
            wrap.appendChild(sub);
        }
        if (inactive.length > 0) {
            const sub = this._createMissionSubsection('Completed', inactive, /* defaultExpanded= */ false);
            wrap.appendChild(sub);
        }
        this.sessionList.appendChild(wrap);
    }

    // ── v0.6.2a3 — Skills sidebar group + detail dialog ──────────────

    requestSkillList() {
        if (!this.ws || this.ws.readyState !== WebSocket.OPEN) return;
        this.ws.send(JSON.stringify({ command: 'skill_list' }));
    }

    /**
     * Render a "Skills" group in the sidebar — same shape as the Missions
     * group. Pinned skills float to the top; the rest sort most-recently-
     * used. Click a row → opens the detail modal.
     *
     * Designed to gracefully no-op when no skills exist (the list is
     * usually empty until the autonomous loop has run a few iters and
     * the bundled skills auto-install on first CLI use, but a fresh GUI
     * boot may still see zero skills).
     */
    _renderSkillsGroup(skills) {
        if (!skills || !skills.length) return;

        const wrap = document.createElement('div');
        wrap.className = 'mission-group skills-group';

        const header = document.createElement('div');
        header.className = 'mission-group-header';
        header.innerHTML = `
            <span class="mission-group-icon" aria-hidden="true">🛠️</span>
            <span class="mission-group-title">Skills</span>
            <span class="mission-group-count">${skills.length}</span>
        `;
        wrap.appendChild(header);

        const items = document.createElement('div');
        items.className = 'mission-subsection-items skills-list';
        for (const s of skills) {
            items.appendChild(this._createSkillRow(s));
        }
        wrap.appendChild(items);
        this.sessionList.appendChild(wrap);
    }

    _createSkillRow(s) {
        const el = document.createElement('div');
        el.className = 'mission-row skill-row' + (s.pinned ? ' pinned' : '');
        el.setAttribute('role', 'button');
        el.setAttribute('tabindex', '0');
        el.dataset.skillId = s.id;

        const pinMark = s.pinned ? '<span class="skill-row-pin" title="Pinned (curator-exempt)">[PIN]</span>' : '';
        const scopeChip = `<span class="skill-row-scope skill-scope-${this.escapeHtml(s.scope || 'global')}">${this.escapeHtml(s.scope || 'global')}</span>`;
        const provChip = `<span class="skill-row-prov skill-prov-${this.escapeHtml(s.created_by || 'agent')}">${this.escapeHtml(s.created_by || 'agent')}</span>`;
        const desc = (s.description || '').slice(0, 70);

        el.innerHTML = `
            <div class="mission-row-body skill-row-body">
                <div class="mission-row-title skill-row-title">${pinMark} ${this.escapeHtml(s.name || s.id)}</div>
                <div class="mission-row-meta skill-row-meta">${scopeChip} ${provChip} <span class="skill-row-desc">${this.escapeHtml(desc)}</span></div>
            </div>
        `;
        const open = () => this.openSkillDetail(s.id);
        el.addEventListener('click', open);
        el.addEventListener('keydown', (e) => {
            if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); open(); }
        });
        return el;
    }

    openSkillDetail(skillId) {
        if (!this.ws || this.ws.readyState !== WebSocket.OPEN) return;
        this._skillDetailOpenId = skillId;
        // Show the dialog with a placeholder — fill in once the view data lands.
        const dialog = document.getElementById('skill-detail-dialog');
        if (!dialog) return;
        dialog.style.display = 'flex';
        document.getElementById('skill-detail-name').textContent = skillId;
        document.getElementById('skill-detail-meta').textContent = 'Loading…';
        document.getElementById('skill-detail-description').textContent = '';
        document.getElementById('skill-detail-procedure').textContent = '';
        // Wire close + buttons (idempotent — addEventListener with same handler is fine for reopens).
        if (!this._skillDetailWired) {
            document.getElementById('skill-detail-close').addEventListener('click', () => this.closeSkillDetail());
            document.getElementById('skill-detail-pin').addEventListener('click', () => this.toggleSkillPin());
            document.getElementById('skill-detail-archive').addEventListener('click', () => this.archiveCurrentSkill());
            this._skillDetailWired = true;
        }
        this.ws.send(JSON.stringify({ command: 'skill_view', skill_id: skillId }));
    }

    closeSkillDetail() {
        const dialog = document.getElementById('skill-detail-dialog');
        if (dialog) dialog.style.display = 'none';
        this._skillDetailOpenId = null;
        this._skillDetailData = null;
    }

    handleSkillViewData(event) {
        if (!event || !event.skill) {
            if (event && event.error) this.showStatusMessage(event.error, 'error');
            this.closeSkillDetail();
            return;
        }
        if (event.skill.id !== this._skillDetailOpenId) return;
        this._skillDetailData = event.skill;
        const s = event.skill;
        document.getElementById('skill-detail-name').textContent = s.name || s.id;
        const meta = [
            s.scope, s.created_by,
            s.pinned ? 'pinned' : null,
            s.deprecated ? 'deprecated' : null,
            `used ${s.success_count}× (${s.fail_count} fails)`,
            `v${s.version}`,
        ].filter(Boolean).join(' · ');
        document.getElementById('skill-detail-meta').textContent = meta;
        document.getElementById('skill-detail-description').textContent = s.description || '';
        document.getElementById('skill-detail-procedure').textContent = s.procedure_md || '(no procedure body)';
        // Update button labels to reflect current state.
        const pinBtn = document.getElementById('skill-detail-pin');
        if (pinBtn) pinBtn.textContent = s.pinned ? 'Unpin' : 'Pin';
        const archBtn = document.getElementById('skill-detail-archive');
        if (archBtn) {
            // Disable archive if refused (bundled / user / pinned).
            const blocked = s.created_by === 'bundled' || s.created_by === 'user' || s.pinned;
            archBtn.disabled = !!blocked;
            archBtn.title = blocked
                ? `Cannot archive ${s.created_by} or pinned skills`
                : 'Move this skill to the archive (reversible)';
        }
    }

    toggleSkillPin() {
        if (!this._skillDetailOpenId) return;
        if (!this.ws || this.ws.readyState !== WebSocket.OPEN) return;
        this.ws.send(JSON.stringify({ command: 'skill_pin_toggle', skill_id: this._skillDetailOpenId }));
        // Refresh the detail view too.
        this.ws.send(JSON.stringify({ command: 'skill_view', skill_id: this._skillDetailOpenId }));
    }

    archiveCurrentSkill() {
        if (!this._skillDetailOpenId) return;
        if (!this.ws || this.ws.readyState !== WebSocket.OPEN) return;
        if (!confirm('Archive this skill? It will be moved to the archive folder (reversible).')) return;
        this.ws.send(JSON.stringify({ command: 'skill_archive', skill_id: this._skillDetailOpenId, reason: 'archived via GUI' }));
    }

    _createMissionSubsection(label, missions, defaultExpanded) {
        const sub = document.createElement('div');
        sub.className = 'mission-subsection' + (defaultExpanded ? ' expanded' : '');

        const subhead = document.createElement('div');
        subhead.className = 'mission-subsection-header';
        subhead.innerHTML = `
            <span class="mission-subsection-chevron">${defaultExpanded ? '▾' : '▸'}</span>
            <span class="mission-subsection-label">${this.escapeHtml(label)}</span>
            <span class="mission-subsection-count">${missions.length}</span>
        `;
        const items = document.createElement('div');
        items.className = 'mission-subsection-items';
        for (const m of missions) items.appendChild(this._createMissionRow(m));
        subhead.addEventListener('click', () => {
            const expanded = sub.classList.toggle('expanded');
            subhead.querySelector('.mission-subsection-chevron').textContent = expanded ? '▾' : '▸';
        });
        sub.appendChild(subhead);
        sub.appendChild(items);
        return sub;
    }

    _createMissionRow(session) {
        const phase = (session.mission_state && session.mission_state.phase) || '';
        const seed = (session.mission_state && session.mission_state.seed_feature) || session.title || '';
        const phaseLabel = {
            drafting: 'drafting',
            planning_dispatched: 'planning',
            executing: 'executing',
            reviewing: 'reviewing',
            completed: 'done',
            exited: 'exited',
        }[phase] || phase;

        const isActive = session.id === this.currentSessionId;
        const isInactive = phase === 'exited' || phase === 'completed';

        const el = document.createElement('div');
        el.className = 'mission-row' + (isActive ? ' active' : '') + (isInactive ? ' inactive' : '');
        el.setAttribute('role', 'button');
        el.setAttribute('tabindex', '0');
        el.dataset.sessionId = session.id;

        // B4 fix — Resume affordance on inactive rows. Hover-revealed so
        // it doesn't compete with the row's primary "click to switch"
        // affordance, but prominent on hover.
        const resumeButtonHtml = isInactive
            ? `<button type="button" class="mission-row-resume" title="Resume this mission" aria-label="Resume mission">↻</button>`
            : '';

        el.innerHTML = `
            <span class="mission-row-dot" data-phase="${this.escapeHtml(phase)}"></span>
            <span class="mission-row-body">
                <span class="mission-row-title">${this.escapeHtml(seed.slice(0, 80))}</span>
                <span class="mission-row-meta">${this.escapeHtml(phaseLabel)}</span>
            </span>
            ${resumeButtonHtml}
        `;
        el.addEventListener('click', () => {
            if (session.id !== this.currentSessionId) {
                this.send({ command: 'switch_session', session_id: session.id });
            }
        });
        const resumeBtn = el.querySelector('.mission-row-resume');
        if (resumeBtn) {
            resumeBtn.addEventListener('click', (e) => {
                e.stopPropagation();
                this.send({ command: 'mission_resume', session_id: session.id });
            });
        }
        return el;
    }

    showSessionSkeletons() {
        if (!this.sessionList) return;
        let html = '';
        for (let i = 0; i < 5; i++) {
            html += '<div class="agent-skeleton"><div class="skeleton-line"></div><div class="skeleton-line"></div></div>';
        }
        this.sessionList.innerHTML = html;
    }

    renderSessionList() {
        if (!this.sessionList) return;
        this.sessionList.innerHTML = '';

        if (this.sessions.length === 0) {
            this.sessionList.innerHTML = '<div class="agent-empty">No agents yet</div>';
            return;
        }

        for (const session of this.sessions) {
            const el = document.createElement('div');
            el.className = 'agent-row' + (session.id === this.currentSessionId ? ' active' : '');
            el.setAttribute('role', 'button');
            el.setAttribute('tabindex', '0');
            el.dataset.sessionId = session.id;

            const date = new Date(session.updated_at * 1000);
            const timeStr = this.formatRelativeTime(date);

            const projectTag = session.project_name
                ? `<span class="session-project-tag">${this.escapeHtml(session.project_name)}</span> \u00B7 `
                : '';
            const roleLabel = this.formatSessionRole(session.session_role || 'generator');
            const roleTag = roleLabel
                ? `<span class="session-project-tag">${this.escapeHtml(roleLabel)}</span> \u00B7 `
                : '';

            const fullTitle = session.title || 'New agent';
            el.title = fullTitle;  // native tooltip on hover for truncated rows
            el.innerHTML = `
                <div class="agent-row-title">${this.escapeHtml(fullTitle)}</div>
                <div class="agent-row-date">${projectTag}${roleTag}${session.model || ''} \u00B7 ${timeStr}</div>
                <div class="agent-row-actions">
                    <button class="agent-menu-btn" title="More actions">&#8943;</button>
                </div>
            `;

            const switchToSession = (e) => {
                if (e && e.target && e.target.closest('.agent-menu-btn')) return;
                if (session.id !== this.currentSessionId) {
                    el.style.opacity = '0.6';
                    const msg = { command: 'switch_session', session_id: session.id };
                    if (session.project_path) msg.project_path = session.project_path;
                    this.send(msg);
                }
            };
            el.addEventListener('click', switchToSession);
            el.addEventListener('keydown', (e) => {
                if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); switchToSession(); }
            });

            // Context menu button (three dots)
            el.querySelector('.agent-menu-btn').addEventListener('click', (e) => {
                e.stopPropagation();
                this.showSessionContextMenu(e, session);
            });

            // Right-click context menu
            el.addEventListener('contextmenu', (e) => {
                e.preventDefault();
                this.showSessionContextMenu(e, session);
            });

            this.sessionList.appendChild(el);
        }
    }

    renderFilteredSessions() {
        const allSessions = this.allSessions && this.allSessions.length
            ? this.allSessions
            : this.sessions;

        const searchVal = (document.getElementById('search-input')?.value || '').toLowerCase().trim();
        const projFilter = (this._projectFilter || '').replace(/\\/g, '/');

        let filtered = allSessions;
        if (projFilter) {
            filtered = filtered.filter(s => (s.project_path || '').replace(/\\/g, '/') === projFilter);
        }
        if (searchVal) {
            filtered = filtered.filter(s => (s.title || '').toLowerCase().includes(searchVal));
        }

        this._renderProjectTree(filtered);
    }

    _renderProjectTree(sessions) {
        if (!this.sessionList) return;
        this.sessionList.innerHTML = '';

        if (!sessions.length) {
            this.sessionList.innerHTML = '<div class="agent-empty">No agents yet</div>';
            return;
        }

        // Pinned sessions float to the top in their own group, removed from
        // their original section to avoid double-listing.
        const pinned = sessions.filter(s => s && s.pinned);
        const unpinned = sessions.filter(s => !s || !s.pinned);

        if (pinned.length > 0) {
            this._renderPinnedGroup(pinned);
        }

        // Split missions out into their own top-level group so they read as
        // first-class entities (per the long-running-agents design). They
        // are also filtered out of the per-project tree below to avoid
        // double-listing.
        const missions = unpinned.filter(s => s && s.mission_state);
        const nonMissions = unpinned.filter(s => !s || !s.mission_state);

        if (missions.length > 0) {
            this._renderMissionsGroup(missions);
        }

        // v0.6.2a3 — Skills group below missions. No-ops when empty.
        if (this.skills && this.skills.length) {
            this._renderSkillsGroup(this.skills);
        }

        if (!nonMissions.length) {
            // All filtered sessions are missions — nothing more to render
            // beneath the Missions group.
            return;
        }

        // Group regular sessions by project.
        const projectMap = new Map();
        for (const s of nonMissions) {
            const path = (s.project_path || this.currentCwd || '').replace(/\\/g, '/');
            const name = s.project_name || path.split('/').pop() || 'Unknown';
            if (!projectMap.has(path)) {
                projectMap.set(path, { name, path, sessions: [] });
            }
            projectMap.get(path).sessions.push(s);
        }

        // Track expanded state (default: current project expanded, others collapsed)
        if (!this._expandedProjects) this._expandedProjects = new Set();
        const curPath = (this.currentCwd || '').replace(/\\/g, '/');
        if (!this._expandedProjectsInited) {
            this._expandedProjects.add(curPath);
            this._expandedProjectsInited = true;
        }
        // When the tree contains a single project (e.g. filtered down), auto-expand it
        // so users immediately see the sessions instead of having to click the chevron.
        if (projectMap.size === 1) {
            const onlyPath = projectMap.keys().next().value;
            this._expandedProjects.add(onlyPath);
        }

        for (const [path, proj] of projectMap) {
            const isExpanded = this._expandedProjects.has(path);
            const isCurrent = path === curPath;

            const header = document.createElement('div');
            header.className = 'proj-tree-header' + (isCurrent ? ' current' : '');
            header.innerHTML = `
                <span class="proj-tree-chevron">${isExpanded ? '\u25BE' : '\u25B8'}</span>
                <span class="proj-tree-name">${this.escapeHtml(proj.name)}</span>
                <span class="proj-tree-count">${proj.sessions.length}</span>
            `;
            header.addEventListener('click', () => {
                if (this._expandedProjects.has(path)) {
                    this._expandedProjects.delete(path);
                } else {
                    this._expandedProjects.add(path);
                }
                this.renderFilteredSessions();
            });
            this.sessionList.appendChild(header);

            if (isExpanded) {
                const container = document.createElement('div');
                container.className = 'proj-tree-sessions';
                for (const session of proj.sessions) {
                    container.appendChild(this._createTreeSessionRow(session));
                }
                this.sessionList.appendChild(container);
            }
        }
    }

    _createTreeSessionRow(session) {
        const el = document.createElement('div');
        el.className = 'agent-row' + (session.id === this.currentSessionId ? ' active' : '');
        el.setAttribute('role', 'button');
        el.setAttribute('tabindex', '0');
        el.dataset.sessionId = session.id;

        const date = new Date(session.updated_at * 1000);
        const timeStr = this.formatRelativeTime(date);
        const roleLabel = (session.session_role && session.session_role !== 'generator')
            ? this.formatSessionRole(session.session_role) : '';
        const roleTag = roleLabel
            ? `<span class="session-project-tag">${this.escapeHtml(roleLabel)}</span> \u00B7 `
            : '';

        const pinIcon = session.pinned ? '<span class="session-pin-icon" aria-label="Pinned">📌</span>' : '';
        el.innerHTML = `
            <div class="agent-row-title">${pinIcon}${this.escapeHtml(session.title || 'New session')}</div>
            <div class="agent-row-date">${roleTag}${session.model || ''} \u00B7 ${timeStr}</div>
            <div class="agent-row-actions">
                <button class="agent-menu-btn" title="More actions">&#8943;</button>
            </div>
        `;

        const switchToSession = (e) => {
            if (e && e.target && e.target.closest('.agent-menu-btn')) return;
            if (session.id !== this.currentSessionId) {
                el.style.opacity = '0.6';
                const msg = { command: 'switch_session', session_id: session.id };
                if (session.project_path) msg.project_path = session.project_path;
                this.send(msg);
            }
        };
        el.addEventListener('click', switchToSession);
        el.addEventListener('keydown', (e) => {
            if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); switchToSession(); }
        });

        el.querySelector('.agent-menu-btn').addEventListener('click', (e) => {
            e.stopPropagation();
            this.showSessionContextMenu(e, session);
        });

        el.addEventListener('contextmenu', (e) => {
            e.preventDefault();
            this.showSessionContextMenu(e, session);
        });

        return el;
    }

    showSessionContextMenu(e, session) {
        // Remove any existing menu
        document.querySelector('.agent-context-menu')?.remove();

        const menu = document.createElement('div');
        menu.className = 'agent-context-menu';

        const pinLabel = session.pinned ? '📌 Unpin' : '📌 Pin to top';
        menu.innerHTML = `
            <div class="ctx-item" data-action="pin">${pinLabel}</div>
            <div class="ctx-item" data-action="rename">&#9998; Rename</div>
            <div class="ctx-item" data-action="replay">&#9654; Replay</div>
            <div class="ctx-separator"></div>
            <div class="ctx-item danger" data-action="delete">&#128465; Delete</div>
        `;

        // Position near the click
        menu.style.left = `${e.clientX}px`;
        menu.style.top = `${e.clientY}px`;

        // Handle actions
        menu.addEventListener('click', (ev) => {
            const action = ev.target.closest('.ctx-item')?.dataset.action;
            if (action === 'pin') {
                this.send({ command: 'pin_session', session_id: session.id });
            } else if (action === 'delete') {
                this.send({ command: 'delete_session', session_id: session.id });
            } else if (action === 'rename') {
                const newTitle = prompt('Rename agent:', session.title);
                if (newTitle && newTitle.trim()) {
                    this.send({ command: 'rename_session', session_id: session.id, title: newTitle.trim() });
                }
            } else if (action === 'replay') {
                this.send({
                    command: 'get_session_replay_events',
                    session_id: session.id,
                    project_path: session.project_path || '',
                });
            }
            menu.remove();
        });

        document.body.appendChild(menu);

        // Keep menu in viewport
        const rect = menu.getBoundingClientRect();
        if (rect.right > window.innerWidth) {
            menu.style.left = `${window.innerWidth - rect.width - 8}px`;
        }
        if (rect.bottom > window.innerHeight) {
            menu.style.top = `${window.innerHeight - rect.height - 8}px`;
        }
    }

    formatRelativeTime(date) {
        const now = new Date();
        const diffMs = now - date;
        const diffMins = Math.floor(diffMs / 60000);
        if (diffMins < 1) return 'just now';
        if (diffMins < 60) return `${diffMins}m ago`;
        const diffHours = Math.floor(diffMins / 60);
        if (diffHours < 24) return `${diffHours}h ago`;
        const diffDays = Math.floor(diffHours / 24);
        if (diffDays < 7) return `${diffDays}d ago`;
        // Beyond a week, show a compact absolute date in a consistent format ("Mar 23")
        // or include the year if it's not the current year ("Mar 23, 2025").
        const opts = (date.getFullYear() === now.getFullYear())
            ? { month: 'short', day: 'numeric' }
            : { month: 'short', day: 'numeric', year: 'numeric' };
        return date.toLocaleDateString(undefined, opts);
    }

    // ── Project switcher dropdown ──────────────────────────────────────

    _openProjectSwitcher(anchorEl) {
        // Toggle: if already open, close it.
        const existing = document.getElementById('project-switcher-menu');
        if (existing) {
            existing.remove();
            return;
        }
        const anchor = anchorEl || this.sidebarProjectSwitch || this.headerProject;
        if (!anchor) return;

        const cur = (this.currentCwd || '').replace(/\\/g, '/');
        const filter = (this._projectFilter || '').replace(/\\/g, '/');
        const recents = (this.recentProjects || []);

        const menu = document.createElement('div');
        menu.id = 'project-switcher-menu';
        menu.className = 'project-switcher-menu';
        menu.setAttribute('role', 'menu');

        const itemHtml = (icon, label, sub, opts = {}) => `
            <div class="psw-item${opts.checked ? ' is-current' : ''}${opts.cls ? ' ' + opts.cls : ''}" role="menuitem" tabindex="0"${opts.dataAttr || ''}>
                <span class="psw-icon">${icon}</span>
                <span class="psw-text">
                    <span class="psw-label">${this.escapeHtml(label)}</span>
                    ${sub ? `<span class="psw-sub">${this.escapeHtml(sub)}</span>` : ''}
                </span>
                ${opts.checked ? '<span class="psw-check">&#10003;</span>' : ''}
            </div>`;

        let html = '';
        // Filter section: All projects + per-project filter is implicit via clicking a recent.
        html += itemHtml(
            '&#9776;',
            'All projects',
            'Show every session in the sidebar',
            { checked: !filter, cls: 'psw-filter-all' }
        );
        html += '<div class="psw-divider"></div>';
        // Quick actions
        if (cur) {
            html += itemHtml('&#43;', 'New session here', this._shortenForMenu(cur), { cls: 'psw-new-session' });
        }
        html += itemHtml('&#128193;', 'Open another project\u2026', '', { cls: 'psw-open-other' });
        if (recents.length) {
            html += `<div class="psw-divider"></div><div class="psw-heading">Recent projects</div>`;
            for (const p of recents) {
                const norm = (p.path || '').replace(/\\/g, '/');
                const isCurrent = norm === cur;
                const isFilter = norm === filter;
                html += `<div class="psw-item psw-project${isFilter ? ' is-current' : ''}" role="menuitem" tabindex="0" data-path="${this.escapeHtml(p.path || '')}">
                    <span class="psw-icon">&#128193;</span>
                    <span class="psw-text">
                        <span class="psw-label">${this.escapeHtml(p.name || '')}${isCurrent ? ' <span class="psw-pill">active</span>' : ''}</span>
                        <span class="psw-sub">${this.escapeHtml(this._shortenForMenu(p.path || ''))}</span>
                    </span>
                    ${isFilter ? '<span class="psw-check">&#10003;</span>' : ''}
                </div>`;
            }
        }
        menu.innerHTML = html;

        // Position under (or beside) the anchor element.
        const rect = anchor.getBoundingClientRect();
        const isSidebarAnchor = anchor === this.sidebarProjectSwitch;
        menu.style.position = 'fixed';
        if (isSidebarAnchor) {
            // Sidebar pill: drop down with the same width as the sidebar (matches anchor width)
            menu.style.left = `${Math.round(rect.left)}px`;
            menu.style.top = `${Math.round(rect.bottom + 4)}px`;
            menu.style.minWidth = `${Math.max(220, Math.round(rect.width))}px`;
        } else {
            menu.style.left = `${Math.round(rect.left)}px`;
            menu.style.top = `${Math.round(rect.bottom + 4)}px`;
            menu.style.minWidth = `${Math.max(260, Math.round(rect.width))}px`;
        }
        document.body.appendChild(menu);
        anchor.setAttribute('aria-expanded', 'true');

        // Wire up actions by class (more robust than positional indexing).
        menu.querySelector('.psw-filter-all')?.addEventListener('click', () => {
            this._closeProjectSwitcher();
            this._setProjectFilter('');
        });
        menu.querySelector('.psw-new-session')?.addEventListener('click', () => {
            this._closeProjectSwitcher();
            if (cur) this.selectProjectFolder(cur);
        });
        menu.querySelector('.psw-open-other')?.addEventListener('click', () => {
            this._closeProjectSwitcher();
            this.send({ command: 'folder_dialog' });
        });
        menu.querySelectorAll('.psw-project').forEach((row) => {
            row.addEventListener('click', () => {
                const path = row.dataset.path;
                this._closeProjectSwitcher();
                if (!path) return;
                const norm = path.replace(/\\/g, '/');
                // Filter the sidebar to this project. Only swap the active backend/session
                // if the user picked a different project than the currently-loaded one.
                this._setProjectFilter(norm);
                if (norm !== cur) this.selectProjectFolder(path);
            });
        });

        // Close on outside click / Escape. Guard against the click that just opened
        // the menu re-firing through document (synthetic-click sequences sometimes do this).
        const openedAt = Date.now();
        const onDocClick = (e) => {
            if (Date.now() - openedAt < 100) return;
            if (anchor.contains(e.target)) return;
            if (!menu.contains(e.target)) this._closeProjectSwitcher();
        };
        const onKey = (e) => { if (e.key === 'Escape') this._closeProjectSwitcher(); };
        document.addEventListener('click', onDocClick);
        document.addEventListener('keydown', onKey);
        menu._cleanup = () => {
            document.removeEventListener('click', onDocClick);
            document.removeEventListener('keydown', onKey);
            anchor.setAttribute('aria-expanded', 'false');
        };
    }

    /**
     * Set or clear the project filter applied to the sidebar session tree.
     * Empty string clears the filter (show all projects).
     */
    _setProjectFilter(path) {
        const norm = (path || '').replace(/\\/g, '/');
        this._projectFilter = norm;
        // Remember explicit "all projects" choice so init events don't re-apply the filter.
        this._projectFilterUserCleared = !norm;
        if (this.sidebarProjectSwitchLabel) {
            if (!norm) {
                this.sidebarProjectSwitchLabel.textContent = 'All projects';
            } else {
                const name = (this.recentProjects || [])
                    .find(p => (p.path || '').replace(/\\/g, '/') === norm)?.name
                    || norm.split('/').pop();
                this.sidebarProjectSwitchLabel.textContent = name;
            }
        }
        this.renderFilteredSessions();
    }

    _closeProjectSwitcher() {
        const menu = document.getElementById('project-switcher-menu');
        if (!menu) return;
        if (typeof menu._cleanup === 'function') menu._cleanup();
        menu.remove();
    }

    _shortenForMenu(path, max = 48) {
        const norm = (path || '').replace(/\\/g, '/');
        if (norm.length <= max) return norm;
        return '\u2026' + norm.slice(-(max - 1));
    }

    // ── New Session Setup ──────────────────────────────────────

    showNewSessionSetup() {
        if (this.agentPanel) this.agentPanel.style.display = 'none';
        else this.chatContainer.style.display = 'none';
        this.inputBar.style.display = 'none';
        if (this.settingsView) this.settingsView.style.display = 'none';

        this.welcomeScreen.style.display = 'flex';
        this.currentView = 'agents';
        document.querySelectorAll('.sidebar-icon-btn[data-view], .sidebar-nav-item[data-view], .sidebar-action[data-view]').forEach(el =>
            el.classList.toggle('active', el.dataset.view === 'agents'));

        this.clearPreviewPanel();
        this.closePreviewPanel();
        this._maybeRenderOnboardingCard();

        const projectStep = document.getElementById('project-step');
        const backendStep = document.getElementById('backend-step');
        const roleSelect = document.getElementById('setup-session-role');
        projectStep.style.display = 'block';
        backendStep.style.display = 'none';
        if (roleSelect) {
            // Sprint workflow is opt-in. Hide the role picker entirely when off so
            // users aren't forced to think about planner/generator/evaluator for a
            // plain conversation.
            const wrapper = roleSelect.closest('.chat-welcome-footer');
            if (wrapper) wrapper.style.display = this.harnessEnabled ? '' : 'none';
            roleSelect.value = this.sessionRole || 'generator';
            roleSelect.onchange = () => {
                this.sessionRole = roleSelect.value || 'generator';
            };
        }

        const input = document.getElementById('welcome-folder-input');
        // Don't pre-fill — user is starting fresh. Show the current cwd as a
        // placeholder hint instead so they know what would be the default.
        input.value = '';
        const hint = (this.currentCwd || '').trim();
        input.placeholder = hint ? `Enter folder path or pick from Recent (default: ${hint})` : 'Enter folder path...';

        // Bind folder open
        const openBtn = document.getElementById('welcome-folder-open');
        openBtn.onclick = () => {
            const path = (input.value.trim() || (this.currentCwd || '').trim());
            if (path) this.selectProjectFolder(path);
        };

        // Bind native folder browse button
        const browseBtn = document.getElementById('welcome-folder-browse');
        browseBtn.onclick = () => {
            this.send({ command: 'folder_dialog' });
        };

        input.onkeydown = (e) => {
            if (e.key === 'Enter') {
                const path = input.value.trim();
                if (path) this.selectProjectFolder(path);
            }
        };

        // Dir browsing on input change
        input.oninput = () => {
            const val = input.value.trim();
            if (val.length > 2) {
                this._dirBrowserTarget = 'welcome-dir-browser';
                this.send({ command: 'list_dirs', path: val });
            }
        };

        // Render recent projects
        const recentSection = document.getElementById('welcome-recent-projects');
        recentSection.innerHTML = '';

        if (this.recentProjects.length > 0) {
            recentSection.innerHTML = '<div class="recent-projects-label">Recent</div>';
            for (const proj of this.recentProjects) {
                const item = document.createElement('div');
                item.className = 'recent-project-item';

                // Check if this is the current project
                const isCurrent = proj.path.replace(/\\/g, '/') === (this.currentCwd || '').replace(/\\/g, '/');

                item.innerHTML = `
                    <span class="proj-icon">&#128193;</span>
                    <div style="flex:1;min-width:0">
                        <div class="proj-name">${this.escapeHtml(proj.name || '')}</div>
                        <div class="proj-path">${this.escapeHtml(proj.path || '')}</div>
                    </div>
                    ${isCurrent ? '<span style="color:var(--ok)">&#10003;</span>' : ''}
                `;
                item.addEventListener('click', () => {
                    this.selectProjectFolder(proj.path);
                });
                recentSection.appendChild(item);
            }

            // "Choose a different folder" option — opens native folder picker
            const chooseItem = document.createElement('div');
            chooseItem.className = 'recent-project-item';
            chooseItem.innerHTML = `
                <span class="proj-icon" style="font-size:12px">&#10133;</span>
                <div style="flex:1"><div class="proj-name">Choose a different folder</div></div>
            `;
            chooseItem.addEventListener('click', () => {
                this.send({ command: 'folder_dialog' });
            });
            recentSection.appendChild(chooseItem);

            // v0.5.6a4 — explicit "type a path" affordance so users
            // who already know the picker won't work in their setup
            // (browser mode, headless, kiosk) don't have to wait for
            // the picker to fail before getting the modal text input.
            const typeItem = document.createElement('div');
            typeItem.className = 'recent-project-item';
            typeItem.innerHTML = `
                <span class="proj-icon" style="font-size:12px">&#9000;</span>
                <div style="flex:1"><div class="proj-name">Type a folder path…</div></div>
            `;
            typeItem.addEventListener('click', () => {
                this._promptForProjectPath('Switch project', (path) => {
                    this.selectProjectFolder(path);
                });
            });
            recentSection.appendChild(typeItem);
        }

        input.focus();
    }

    selectProjectFolder(path) {
        this.send({ command: 'set_project', path });

        const short = path.replace(/\\/g, '/').split('/').pop();
        this.currentCwd = path.replace(/\\/g, '/');
        this.headerProject.textContent = short;
        this.sidebarProjectName.textContent = short;
        this.sidebarCwd.textContent = path;

        // Bug #7+#8 fix: project switch was leaving the chat panel and the
        // git pill showing the previous project's state. The set_project
        // command above gets the backend ready, but doesn't tell the
        // frontend to refresh dependent UI components.
        //
        // 1. Clear chat-panel messages immediately. The session_loaded event
        //    that follows set_project will re-render whatever's appropriate
        //    for the new project, but until then we want a clean slate
        //    rather than the previous project's last conversation lingering.
        if (this.chatMessages) this.chatMessages.innerHTML = '';
        this.clearPreviewPanel?.();
        // 2. Request a fresh git_status for the new project so the bottom
        //    git pill reflects the new branch / dirty count instead of the
        //    previous project's. The backend will respond with a git_status
        //    event that handleGitStatus consumes.
        this.send({ command: 'git_status' });

        // Show backend step
        const projectStep = document.getElementById('project-step');
        const backendStep = document.getElementById('backend-step');
        projectStep.style.display = 'none';
        backendStep.style.display = 'block';

        // Show project badge (clickable to go back)
        const badge = document.getElementById('setup-project-badge');
        badge.innerHTML = `
            <span class="badge-icon">&#128193;</span>
            ${this.escapeHtml(short)}
            <span class="badge-change">change</span>
        `;
        badge.onclick = () => {
            projectStep.style.display = 'block';
            backendStep.style.display = 'none';
        };

        // Populate backend cards with what we have (will be refreshed by init event)
        const backends = this.backends || {};
        if (Object.keys(backends).length > 0) {
            this.showBackendSelector(backends);
        } else {
            // Show scanning message — init event will refresh
            const label = document.querySelector('.backend-label');
            if (label) label.textContent = 'Scanning backends...';
        }
    }

    handleDirList(event) {
        const browserId = this._dirBrowserTarget || 'welcome-dir-browser';
        const browser = document.getElementById(browserId);
        if (!browser) return;

        const dirs = event.dirs || [];
        if (dirs.length === 0) {
            browser.style.display = 'none';
            return;
        }

        browser.style.display = 'block';
        browser.innerHTML = '';

        for (const dir of dirs) {
            const item = document.createElement('div');
            item.className = 'dir-item';
            const name = dir.split(/[/\\]/).filter(Boolean).pop() || dir;
            item.innerHTML = `<span class="dir-icon">&#128193;</span> ${this.escapeHtml(name)}`;
            item.addEventListener('click', () => {
                // Update the folder input
                const input = document.getElementById('welcome-folder-input');
                if (input) {
                    input.value = dir;
                    this._dirBrowserTarget = browserId;
                    this.send({ command: 'list_dirs', path: dir });
                }
            });
            item.addEventListener('dblclick', () => {
                this.selectProjectFolder(dir);
            });
            browser.appendChild(item);
        }
    }

    escapeHtml(str) {
        const div = document.createElement('div');
        div.textContent = str;
        return div.innerHTML;
    }

    shortenPath(path) {
        const p = path.replace(/\\/g, '/');
        if (p.length > 50) {
            const parts = p.split('/');
            return '…/' + parts.slice(-2).join('/');
        }
        return p;
    }

    // ── Git Integration ─────────────────────────────────────────

    requestGitStatus() {
        if (this.ws && this.ws.readyState === WebSocket.OPEN) {
            this.ws.send(JSON.stringify({ command: 'git_status' }));
        }
    }

    handleGitStatus(data) {
        this.gitData = data;
        this._applyGitBadgeFromData();
    }

    /** Git badge is Agent/workspace context; hidden on Ask so chat feels repo-agnostic. */
    _applyGitBadgeFromData() {
        if (!this.gitBadge) return;
        const data = this.gitData;
        if (!data || !data.is_repo) {
            this.gitBadge.style.display = 'none';
            return;
        }
        this.gitBadge.style.display = 'flex';
        this.gitBranchName.textContent = data.branch || 'unknown';
        if (data.change_count > 0) {
            this.gitChangesCount.style.display = 'flex';
            this.gitChangesCount.textContent = data.change_count;
        } else {
            this.gitChangesCount.style.display = 'none';
        }
    }

    handleGitResult(event) {
        // Handle results from git_quick actions
        const data = event.data || {};
        if (this.gitPopoverOpen) {
            this.requestGitStatus(); // Refresh after actions
        }
    }

    toggleGitPopover() {
        if (this.gitPopoverOpen) {
            const existing = document.querySelector('.git-popover');
            if (existing) existing.remove();
            this.gitPopoverOpen = false;
            return;
        }
        if (!this.gitData || !this.gitData.is_repo) return;

        this.gitPopoverOpen = true;
        const popover = document.createElement('div');
        popover.className = 'git-popover';

        const data = this.gitData;
        popover.innerHTML = `
            <div class="git-popover-header">
                <span>${data.branch}</span>
                <button class="icon-btn git-popover-close">&times;</button>
            </div>
            <div class="git-popover-tabs">
                <button class="git-popover-tab active" data-tab="changes">Changes (${data.changes.length})</button>
                <button class="git-popover-tab" data-tab="commits">Commits</button>
            </div>
            <div class="git-popover-body" id="git-popover-body"></div>
        `;

        document.getElementById('main').appendChild(popover);

        // Close button
        popover.querySelector('.git-popover-close').addEventListener('click', () => this.toggleGitPopover());

        // Tab switching
        popover.querySelectorAll('.git-popover-tab').forEach(tab => {
            tab.addEventListener('click', () => {
                popover.querySelectorAll('.git-popover-tab').forEach(t => t.classList.remove('active'));
                tab.classList.add('active');
                this._renderGitPopoverTab(tab.dataset.tab);
            });
        });

        // Close on click outside
        setTimeout(() => {
            const handler = (e) => {
                if (!popover.contains(e.target) && !this.gitBadge.contains(e.target)) {
                    this.toggleGitPopover();
                    document.removeEventListener('click', handler);
                }
            };
            document.addEventListener('click', handler);
        }, 100);

        this._renderGitPopoverTab('changes');
    }

    _renderGitPopoverTab(tab) {
        const body = document.getElementById('git-popover-body');
        if (!body || !this.gitData) return;

        if (tab === 'changes') {
            if (this.gitData.changes.length === 0) {
                body.innerHTML = '<div style="padding:16px;color:var(--muted);text-align:center">No changes</div>';
                return;
            }
            body.innerHTML = this.gitData.changes.map(c => {
                let statusClass = 'modified';
                if (c.status === '??' || c.status === 'A') statusClass = 'added';
                if (c.status === 'D') statusClass = 'deleted';
                if (c.status === '??') statusClass = 'untracked';
                return `<div class="git-file-item">
                    <span class="git-status-code ${statusClass}">${c.status}</span>
                    <span>${c.file}</span>
                </div>`;
            }).join('');
        } else {
            body.innerHTML = (this.gitData.commits || []).map(c =>
                `<div class="git-commit-item">
                    <span class="git-commit-hash">${c.hash}</span>
                    <span class="git-commit-msg">${c.message}</span>
                </div>`
            ).join('');
        }
    }

    // ── RESONANT.md Badge ───────────────────────────────────────

    updateResonantMdBadge() {
        if (!this.resonantMdBadge) return;
        if (this.resonantMd && this.resonantMd.exists) {
            this.resonantMdBadge.style.display = 'flex';
        } else {
            this.resonantMdBadge.style.display = 'none';
        }
    }

    toggleResonantMdPopover() {
        const existing = document.querySelector('.resonant-md-popover');
        if (existing) {
            existing.remove();
            this.resonantMdPopoverOpen = false;
            return;
        }

        this.resonantMdPopoverOpen = true;
        const popover = document.createElement('div');
        popover.className = 'resonant-md-popover git-popover';

        const content = this.resonantMdContent || '';
        const exists = this.resonantMd?.exists;

        popover.innerHTML = `
            <div class="git-popover-header">
                <span>RESONANT.md</span>
                <button class="icon-btn resonant-md-popover-close">&times;</button>
            </div>
            <div class="resonant-md-popover-body" style="padding:12px;display:flex;flex-direction:column;gap:8px;">
                <textarea class="settings-input" id="resonant-md-editor" rows="12"
                    style="font-family:monospace;font-size:12px;resize:vertical;min-height:120px;"
                    placeholder="Add project instructions for the AI assistant...">${this.escapeHtml(content)}</textarea>
                <div style="display:flex;gap:8px;justify-content:flex-end;">
                    <button class="btn-primary btn-sm" id="resonant-md-save-btn">Save</button>
                </div>
            </div>
        `;

        document.getElementById('main').appendChild(popover);

        // Close button
        popover.querySelector('.resonant-md-popover-close').addEventListener('click', () =>
            this.toggleResonantMdPopover());

        // Save button
        popover.querySelector('#resonant-md-save-btn')?.addEventListener('click', () => {
            const editor = document.getElementById('resonant-md-editor');
            if (editor && this.ws && this.ws.readyState === WebSocket.OPEN) {
                this.ws.send(JSON.stringify({ command: 'save_resonant_md', content: editor.value }));
            }
        });

        // Close on click outside + Escape
        setTimeout(() => {
            const clickHandler = (e) => {
                if (!popover.contains(e.target) && !this.resonantMdBadge.contains(e.target)) {
                    this.toggleResonantMdPopover();
                    document.removeEventListener('click', clickHandler);
                    document.removeEventListener('keydown', escHandler);
                }
            };
            const escHandler = (e) => {
                if (e.key === 'Escape') {
                    this.toggleResonantMdPopover();
                    document.removeEventListener('click', clickHandler);
                    document.removeEventListener('keydown', escHandler);
                }
            };
            document.addEventListener('click', clickHandler);
            document.addEventListener('keydown', escHandler);
        }, 100);
    }

    _updateResonantMdPopoverContent() {
        const editor = document.getElementById('resonant-md-editor');
        if (editor && this.resonantMdContent !== undefined) {
            editor.value = this.resonantMdContent;
        }
    }

}


// ═══════════════════════════════════════════════════════════════════
//  Initialize
// ═══════════════════════════════════════════════════════════════════

document.addEventListener('DOMContentLoaded', () => {
    window.app = new ResonantApp();
});
