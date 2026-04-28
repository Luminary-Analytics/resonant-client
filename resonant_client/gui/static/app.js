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
        this._renderScheduled = false;

        // Step collapsing state
        this.currentStepEvent = null;
        this.stepToolCalls = [];
        this.stepToolResults = [];
        this.stepIsInlineOnly = true;
        this.stepRendered = false;
        this.collapsedGroup = [];
        this.lastModel = '';
        this.lastStats = null;

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
        this.userInput.addEventListener('keydown', (e) => {
            if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault();
                this.sendMessage();
            }
        });

        // Auto-resize textarea
        this.userInput.addEventListener('input', () => {
            this.userInput.style.height = 'auto';
            this.userInput.style.height = Math.min(this.userInput.scrollHeight, 200) + 'px';
        });

        if (this.chatContainer && this.chatScrollEndBtn) {
            this.chatContainer.addEventListener('scroll', () => this._syncChatScrollEndBtn(), { passive: true });
            this.chatScrollEndBtn.addEventListener('click', () => this.scrollToBottom());
        }

        // Stop button
        this.stopBtn.addEventListener('click', () => {
            this.send({ command: 'cancel' });
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
        if (!text || this.isRunning) return;

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

        // Add user message to chat (with image thumbnails if attached)
        this.addUserMessage(text, this.attachedImages);

        this._resetAgentRunSummary(text);

        // Reset streaming state
        this.streamBuffer = '';
        this.isStreaming = false;
        this.currentMessageEl = null;
        this.currentStepEvent = null;
        this.stepToolCalls = [];
        this.stepToolResults = [];
        this.stepIsInlineOnly = true;
        this.stepRendered = false;
        this.collapsedGroup = [];
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
                break;
            case 'session_cleared':
                this.chatMessages.innerHTML = '';
                this.sessions = event.sessions || [];
                this.currentSessionId = event.current_session_id || '';
                this.applySessionRoleUI(event.session_role || this.sessionRole);
                this.renderFilteredSessions();
                this.showChatInterface();
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
                }
                break;
            case 'plan.event':
                if (window.PlanGraphView) {
                    window.PlanGraphView.applyEvent(event.event_payload || event);
                }
                break;
            case 'plan.checkpoint':
                if (window.PlanGraphView) {
                    window.PlanGraphView.showCheckpoint(event.payload || event);
                    this.openPlanTab(true);
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
                // Native folder dialog returned a path
                if (event.path) {
                    const folderInput = document.getElementById('welcome-folder-input');
                    if (folderInput) folderInput.value = event.path;
                    this.selectProjectFolder(event.path);
                }
                break;
            case 'folder_picker_unavailable':
                // No native picker (typically browser-only mode) \u2014 redirect the user
                // to the welcome screen's text input so the click isn't a dead end.
                this.showStatusMessage(event.message || 'Folder picker unavailable. Type a path in the welcome screen.');
                this.showNewSessionSetup();
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

        if (sessions) {
            this.sessions = sessions;
            this.allSessions = all_sessions || [];
            this.currentSessionId = current_session_id || '';
            this.applySessionRoleUI(current_session_role || this.currentSessionRole || 'generator');
            this.sessionRole = current_session_role || this.sessionRole;
            this.renderFilteredSessions();
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

    showBackendSelector(backends) {
        const list = document.getElementById('backend-list');
        list.innerHTML = '';

        const label = document.querySelector('.backend-label');
        const keys = Object.keys(backends);
        if (keys.length === 0) {
            label.textContent = 'No backends found. Start Ollama, set ANTHROPIC_API_KEY, or set OPENAI_API_KEY.';
            return;
        }

        label.textContent = 'Select a backend';

        const backendLabels = {
            resonant: 'Resonant Engine',
            mlx: 'MLX Local',
            ollama: 'Ollama',
            claude: 'Claude',
            openai: 'OpenAI',
            lmstudio: 'LM Studio',
            'claude-code': 'Claude Code',
            codex: 'Codex',
        };

        const backendIcons = {
            'claude-code': '⌘',
            codex: '>_',
            resonant: '◈',
            mlx: '◉',
            ollama: '🦙',
            claude: '◆',
            openai: '◎',
            lmstudio: '⬡',
        };

        const backendDescs = {
            'claude-code': 'CLI agent',
            codex: 'CLI agent',
            resonant: 'Cognitive engine',
            mlx: 'Routed local adapters',
            ollama: 'Local models',
            claude: 'Anthropic API',
            openai: 'OpenAI API',
            lmstudio: 'Local server',
        };

        // Group backends by category
        const groups = [
            { label: 'Agents', keys: ['claude-code', 'codex', 'resonant'] },
            { label: 'Cloud APIs', keys: ['claude', 'openai'] },
            { label: 'Local', keys: ['mlx', 'ollama', 'lmstudio'] },
        ];
        const preferred = this._getPreferredBackendSelection(backends);

        for (const group of groups) {
            const available = group.keys.filter(k => backends[k]);
            if (available.length === 0) continue;

            const section = document.createElement('div');
            section.className = 'backend-group';

            const groupLabel = document.createElement('div');
            groupLabel.className = 'backend-group-label';
            groupLabel.textContent = group.label;
            section.appendChild(groupLabel);

            const cardsContainer = document.createElement('div');
            cardsContainer.className = 'backend-group-cards' + (available.length === 1 ? ' single' : '');

            for (const key of available) {
                const info = backends[key];
                const card = document.createElement('div');
                card.className = 'backend-card';
                card.dataset.backend = key;

                const modelCount = info.models ? info.models.length : 0;
                const detail = backendDescs[key] || (info.patterns
                    ? info.patterns.toLocaleString() + ' patterns'
                    : modelCount + (modelCount === 1 ? ' model' : ' models'));
                const isPreferred = preferred && preferred.backend === key;

                // Status pills — visible signal of "is this ready to use".
                const pills = [];
                if (isPreferred) pills.push('<span class="backend-pill backend-pill-rec">Recommended</span>');
                if (modelCount > 0) {
                    pills.push(`<span class="backend-pill backend-pill-ok">\u2713 ${modelCount} model${modelCount === 1 ? '' : 's'}</span>`);
                } else if (info.patterns) {
                    pills.push('<span class="backend-pill backend-pill-ok">\u2713 Available</span>');
                } else {
                    pills.push('<span class="backend-pill backend-pill-warn">No models</span>');
                }

                card.innerHTML = `
                    <div class="backend-card-icon">${backendIcons[key] || '●'}</div>
                    <div class="backend-card-info">
                        <div class="backend-card-name">${backendLabels[key] || key}</div>
                        <div class="backend-card-detail">${detail}</div>
                        <div class="backend-card-pills">${pills.join('')}</div>
                    </div>
                    <div class="backend-card-dot"></div>
                `;

                card.addEventListener('click', () => {
                    if (info.models && info.models.length > 1) {
                        this.showModelPicker(key, info.models, cardsContainer, card, info.model_labels);
                    } else {
                        const model = info.models ? info.models[0] : '';
                        this.selectBackend(key, model);
                    }
                });

                cardsContainer.appendChild(card);
            }

            section.appendChild(cardsContainer);
            list.appendChild(section);
        }
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
            <p class="onboarding-sub">Resonant is built around <strong>deepseek-v4-flash on Ollama</strong> &mdash; high-quality coding without sending your code to the cloud.</p>
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
        `;
        empty.addEventListener('click', (ev) => {
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
        // Categorize backends into display groups
        return {
            local: {
                label: 'Local',
                backends: ['mlx', 'ollama', 'lmstudio', 'resonant'],
            },
            subscriptions: {
                label: 'Subscriptions',
                backends: ['claude-code', 'codex'],
            },
            apis: {
                label: 'APIs',
                backends: ['claude', 'openai'],
            },
        };
    }

    _getBackendLabels() {
        return {
            'claude-code': 'Claude Code',
            codex: 'Codex',
            resonant: 'Resonant',
            mlx: 'MLX Local',
            ollama: 'Ollama',
            claude: 'Anthropic API',
            openai: 'OpenAI API',
            lmstudio: 'LM Studio',
        };
    }

    _getPreferredBackendSelection(backends) {
        const configuredBackend = this.settings?.general?.default_backend || '';
        const configuredModel = this.settings?.general?.default_model || '';
        if (configuredBackend && backends?.[configuredBackend]?.models?.length > 0) {
            const models = backends[configuredBackend].models;
            const preferredModel = configuredModel && models.includes(configuredModel)
                ? configuredModel
                : models[0];
            return { backend: configuredBackend, model: preferredModel };
        }
        if (backends?.mlx?.models?.length > 0) {
            const preferredModel = backends.mlx.models.includes('adapter-router')
                ? 'adapter-router'
                : backends.mlx.models[0];
            return { backend: 'mlx', model: preferredModel };
        }
        if (backends?.ollama?.models?.length > 0) {
            return { backend: 'ollama', model: backends.ollama.models[0] };
        }
        if (backends?.lmstudio?.models?.length > 0) {
            return { backend: 'lmstudio', model: backends.lmstudio.models[0] };
        }
        return null;
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

        // Add thinking indicator
        this.removeThinking();
        this.addThinking();
    }

    ensureStepRendered() {
        if (!this.stepRendered && this.currentStepEvent) {
            // Flush collapsed group first
            this.flushCollapsedGroup();

            // Render step header
            this.renderStepHeader(this.currentStepEvent);

            // Flush any buffered inline tools
            for (const tc of this.stepToolCalls) {
                this.renderToolCall(tc);
            }
            for (const tr of this.stepToolResults) {
                this.renderToolResult(tr);
            }

            this.stepRendered = true;
        }
    }

    renderStepHeader(event) {
        const step = event.step || 0;

        const el = document.createElement('div');
        el.className = 'step-header';
        el.dataset.step = step;
        el.innerHTML = `
            <div class="step-divider"></div>
            <span class="step-label">◆ Working...</span>
            <span class="step-meta">step ${step}</span>
            <div class="step-divider"></div>
        `;
        this.getRenderTarget().appendChild(el);
        this._currentStepHeaderEl = el;
        this._currentStepToolCounts = {};
    }

    /** Update the current step header label based on tools used so far. */
    updateStepActionLabel() {
        if (!this._currentStepHeaderEl || !this._currentStepToolCounts) return;
        const total = Object.values(this._currentStepToolCounts).reduce((s, v) => s + v, 0);
        if (total === 0) return;
        const action = inferActionLabel(this._currentStepToolCounts);
        const step = this._currentStepHeaderEl.dataset.step || '0';
        const labelEl = this._currentStepHeaderEl.querySelector('.step-label');
        if (labelEl) labelEl.textContent = `◆ ${action}`;
        const metaEl = this._currentStepHeaderEl.querySelector('.step-meta');
        if (metaEl) metaEl.textContent = `step ${step}`;
    }

    handleStepEnd(event) {
        this.removeThinking();

        if (this.stepIsInlineOnly && this.stepToolCalls.length > 0) {
            // Inline-only step → add to collapsed group
            this.collapsedGroup.push({
                stepEvent: this.currentStepEvent,
                toolCalls: [...this.stepToolCalls],
                toolResults: [...this.stepToolResults],
                endEvent: event,
                model: this.lastModel,
                stats: this.lastStats,
            });
        } else if (this.stepRendered) {
            // Fully rendered step → show footer
            this.renderStepFooter(event);
        }
    }

    renderStepFooter(event) {
        const elapsed = event.elapsed || 0;
        if (elapsed <= 0 && !this.lastModel) return;

        const parts = [];
        if (this.lastModel) parts.push(this.lastModel);
        if (this.lastStats) {
            const inp = this.lastStats.input_tokens;
            const out = this.lastStats.output_tokens;
            if (inp && out) parts.push(`${inp}→${out} tok`);
        }
        if (elapsed > 0) parts.push(`${elapsed.toFixed(1)}s`);

        const el = document.createElement('div');
        el.className = 'step-footer';
        el.innerHTML = `▣ ${parts.map(p => `<span>${p}</span>`).join('<span class="sep">·</span>')}`;
        this.getRenderTarget().appendChild(el);
    }

    // ── Collapsed Group ─────────────────────────────────────────

    flushCollapsedGroup() {
        if (this.collapsedGroup.length === 0) return;

        const group = this.collapsedGroup;
        const firstStep = group[0].stepEvent.step || 0;
        const lastStep = group[group.length - 1].stepEvent.step || 0;

        // Count tools
        const toolCounts = {};
        const allCalls = [];
        for (const g of group) {
            for (let i = 0; i < g.toolCalls.length; i++) {
                const tc = g.toolCalls[i];
                const name = tc.name || '';
                toolCounts[name] = (toolCounts[name] || 0) + 1;
                allCalls.push({
                    call: tc,
                    result: g.toolResults[i] || {},
                });
            }
        }

        // Summary
        const summaryParts = [];
        for (const [name, count] of Object.entries(toolCounts)) {
            const info = getToolInfo(name);
            summaryParts.push(count > 1 ? `${info.label} ×${count}` : info.label);
        }

        const actionLabel = inferActionLabel(toolCounts);
        const stepMeta = firstStep === lastStep
            ? `step ${firstStep}`
            : `steps ${firstStep}–${lastStep}`;

        const container = document.createElement('div');
        container.className = 'collapsed-group';

        const header = document.createElement('div');
        header.className = 'collapsed-header';
        header.innerHTML = `
            <span class="collapsed-icon">▸</span>
            <span class="collapsed-summary">◆ ${actionLabel}</span>
            <span class="collapsed-meta">${stepMeta} · ${allCalls.length} calls</span>
        `;

        const items = document.createElement('div');
        items.className = 'collapsed-items';

        for (const { call, result } of allCalls) {
            const name = call.name || '';
            const args = call.arguments || {};
            const info = getToolInfo(name);
            const meta = result.metadata || {};
            const isError = result.is_error || false;

            let desc = '';
            let metaText = '';

            if (name === 'file_read') {
                const p = args.path || '';
                desc = `<span style="color:var(--file)">${this.shortenPath(p)}</span>`;
                metaText = meta.lines ? `${meta.lines} lines` : '';
            } else if (name === 'glob') {
                desc = args.pattern || '';
                metaText = meta.count != null ? `${meta.count} files` : '';
            } else if (name === 'grep') {
                desc = `'${args.pattern || ''}'`;
                metaText = meta.count != null ? `${meta.count} matches` : '';
            } else {
                desc = info.label;
            }

            const statusIcon = isError ? '✗' : '✓';
            const statusColor = isError ? 'var(--err)' : 'var(--ok)';

            const line = document.createElement('div');
            line.className = 'tool-inline';
            line.innerHTML = `
                <span class="tool-icon" style="color:var(--${info.color})">${info.icon}</span>
                <span class="tool-desc">${desc}</span>
                <span class="tool-meta">${metaText}</span>
                <span class="tool-status" style="color:${statusColor}">${statusIcon}</span>
            `;
            items.appendChild(line);
        }

        header.addEventListener('click', () => {
            container.classList.toggle('expanded');
            header.querySelector('.collapsed-icon').textContent =
                container.classList.contains('expanded') ? '▾' : '▸';
        });

        container.appendChild(header);
        container.appendChild(items);
        this.getRenderTarget().appendChild(container);

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
        if (this.isStreaming && this.currentMessageEl) {
            this.isStreaming = false;
            const finalText = (event.text || this.streamBuffer || '').trim();
            this.streamBuffer = finalText;
            this.renderMarkdown(this.currentMessageEl, finalText);
            this.currentMessageEl.querySelector('.message-content')?.classList.remove('streaming-cursor');
        }
    }

    scheduleRender() {
        if (!this._renderScheduled) {
            this._renderScheduled = true;
            requestAnimationFrame(() => {
                this._renderScheduled = false;
                if (this.currentMessageEl) {
                    this.renderMarkdown(this.currentMessageEl, this.streamBuffer, true);
                }
            });
        }
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

            // Syntax highlight code blocks
            contentEl.querySelectorAll('pre code').forEach(block => {
                if (typeof hljs !== 'undefined') {
                    hljs.highlightElement(block);
                }
            });
        } catch (err) {
            contentEl.textContent = text;
        }

        this.scrollToBottom();
    }

    // ── Tool Calls ──────────────────────────────────────────────

    handleToolCall(event) {
        this.removeThinking();
        const name = event.name || '';
        const callId = event.call_id || '';
        const nameLower = name.toLowerCase();

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
            // Buffer inline tool
            this.stepToolCalls.push(event);
        } else {
            // Block tool → render immediately
            this.stepIsInlineOnly = false;
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
            this.stepIsInlineOnly = false;
            this.ensureStepRendered();
            this.renderToolResult(event);
            return;
        }

        if (this.stepIsInlineOnly && COLLAPSIBLE_TOOLS.has(name)) {
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

    renderBlockToolCall(name, args, info, category, event) {
        const el = document.createElement('div');
        el.className = `tool-block ${category}`;
        el.setAttribute('data-tool', name);
        el.style.borderLeftColor = `var(--${info.color})`;

        let headerContent = '';
        let bodyContent = '';

        if (name === 'bash') {
            const cmd = args.command || '';
            const displayCmd = cmd.length > 100 ? cmd.slice(0, 97) + '...' : cmd;
            headerContent = `<span class="tool-icon" style="color:var(--${info.color})">${info.icon}</span>
                             <span class="tool-name" style="color:var(--${info.color})">${info.label}</span>`;
            bodyContent = `<span style="color:var(--muted)">$ </span>${this.escapeHtml(displayCmd)}`;

            // Push command to preview console
            this.pushPreviewConsole(`$ ${cmd}`, 'stdout');

        } else if (name === 'file_write') {
            const fpath = args.path || '';
            const content = args.content || '';
            const lines = content.split('\n');
            const lineCount = lines.length;
            const preview = lines.slice(0, 15).join('\n');

            headerContent = `<span class="tool-icon" style="color:var(--ok)">${info.icon}</span>
                             <span class="tool-name" style="color:var(--ok)">${info.label}</span>
                             <span class="tool-file">${this.escapeHtml(fpath)}</span>`;
            bodyContent = this.escapeHtml(preview);
            if (lineCount > 15) bodyContent += `\n<span style="color:var(--dim)"># ... (${lineCount - 15} more lines)</span>`;

        } else if (name === 'file_edit') {
            const fpath = args.path || '';
            const diffLines = event.diff_lines || [];

            headerContent = `<span class="tool-icon" style="color:var(--warn)">${info.icon}</span>
                             <span class="tool-name" style="color:var(--warn)">${info.label}</span>
                             <span class="tool-file">${this.escapeHtml(fpath)}</span>`;

            if (diffLines.length > 2) {
                const rendered = diffLines.slice(2, 20).map(line => {
                    if (line.startsWith('+')) return `<span class="diff-line add">+ ${this.escapeHtml(line.slice(1))}</span>`;
                    if (line.startsWith('-')) return `<span class="diff-line del">- ${this.escapeHtml(line.slice(1))}</span>`;
                    if (line.startsWith('@@')) return `<span class="diff-line hunk">${this.escapeHtml(line)}</span>`;
                    return `<span class="diff-line ctx">  ${this.escapeHtml(line)}</span>`;
                }).join('');
                bodyContent = rendered;
                if (diffLines.length > 20) bodyContent += `<span class="diff-line ctx">  ⋯ ${diffLines.length - 20} more lines</span>`;
            } else {
                bodyContent = '<span style="color:var(--dim)">(no visible diff)</span>';
            }

        } else if (name === 'browser_js') {
            const code = args.code || '';
            const display = code.length > 200 ? code.slice(0, 197) + '...' : code;
            headerContent = `<span class="tool-icon" style="color:var(--brand2)">${info.icon}</span>
                             <span class="tool-name" style="color:var(--brand2)">${info.label}</span>`;
            bodyContent = this.escapeHtml(display);
        }

        el.innerHTML = `
            <div class="tool-block-header">${headerContent}</div>
            <div class="tool-block-body">${bodyContent}</div>
            <div class="tool-block-footer" data-tool-footer="${name}"></div>
        `;

        this.getRenderTarget().appendChild(el);
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

        if (BLOCK_TOOLS.has(name)) {
            this.renderBlockToolResult(name, output, isError, elapsed, meta);
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

    renderBlockToolResult(name, output, isError, elapsed, meta) {
        const target = this.getRenderTarget();
        const footers = target.querySelectorAll(`[data-tool-footer="${name}"]`);
        const footer = footers[footers.length - 1];
        if (!footer) return;

        if (name === 'bash') {
            const lines = output.split('\n');
            const displayLines = lines.length > MAX_OUTPUT_LINES
                ? [...lines.slice(0, MAX_OUTPUT_LINES), `⋯ +${lines.length - MAX_OUTPUT_LINES} lines`]
                : lines;

            // Add output to block body
            const body = footer.previousElementSibling;
            if (body) {
                const outputEl = document.createElement('div');
                outputEl.style.marginTop = '8px';
                outputEl.style.borderTop = '1px solid var(--border)';
                outputEl.style.paddingTop = '6px';
                outputEl.style.color = isError ? 'var(--err)' : 'var(--dim)';
                outputEl.textContent = displayLines.join('\n');
                body.appendChild(outputEl);
            }

            const parts = [`${elapsed.toFixed(1)}s`];
            if (meta.timed_out) parts.push('timeout');
            if (meta.exit_code && meta.exit_code !== 0) parts.push(`exit ${meta.exit_code}`);
            footer.innerHTML = `<span class="tool-status ${isError ? 'err' : 'ok'}">${isError ? '✗' : '✓'}</span> ${parts.join(' · ')}`;

            // Push to preview console
            if (output && output.trim()) {
                const type = isError ? 'stderr' : 'stdout';
                this.pushPreviewConsole(output.trim(), type);
            }

        } else if (name === 'file_write') {
            const lineCount = meta.lines || 0;
            const chars = meta.chars || 0;
            const icon = isError ? '✗' : '✓';
            footer.innerHTML = `<span class="tool-status ${isError ? 'err' : 'ok'}">${icon}</span> ${lineCount} lines, ${chars} chars`;

        } else if (name === 'file_edit') {
            const icon = isError ? '✗' : '✓';
            const msg = isError ? output : 'applied';
            footer.innerHTML = `<span class="tool-status ${isError ? 'err' : 'ok'}">${icon}</span> ${msg}`;

        } else if (name === 'browser_js') {
            const lines = output.split('\n');
            const display = lines.length > MAX_OUTPUT_LINES
                ? [...lines.slice(0, MAX_OUTPUT_LINES), `⋯ +${lines.length - MAX_OUTPUT_LINES} lines`].join('\n')
                : output;
            footer.innerHTML = `<span style="color:${isError ? 'var(--err)' : 'var(--dim)'}">${this.escapeHtml(display)}</span>`;
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
                    { key: 'default_backend', label: 'Default backend', type: 'select',
                      options: [
                          { value: '', label: 'Auto' },
                          { value: 'resonant', label: 'Resonant Engine' },
                          { value: 'mlx', label: 'MLX Local' },
                          { value: 'ollama', label: 'Ollama' },
                          { value: 'lmstudio', label: 'LM Studio' },
                          { value: 'claude-code', label: 'Claude Code' },
                          { value: 'codex', label: 'Codex' },
                          { value: 'claude', label: 'Anthropic API' },
                          { value: 'openai', label: 'OpenAI API' },
                      ]
                    },
                    { key: 'default_model', label: 'Default model', type: 'text',
                      placeholder: 'e.g. deepseek-v4-flash:cloud',
                      hint: 'Leave blank to use the first model the chosen backend reports.' },
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
                    { key: 'lmstudio_url', label: 'LM Studio URL (LMSTUDIO_URL)', type: 'text' },
                    { key: 'ollama_num_ctx', label: 'Ollama context window (num_ctx)', type: 'number' },
                    { key: 'ollama_keep_alive', label: 'Ollama keep-alive duration', type: 'text' },
                ]
            },
            {
                id: 'network', title: 'Network',
                fields: [
                    { key: 'resonant_api_url', label: 'Resonant API URL', type: 'text' },
                    { key: 'remote_engine_ws_url', label: 'Remote engine WebSocket URL', type: 'text' },
                ]
            },
            {
                id: 'api_keys', title: 'API Keys',
                fields: [
                    { key: 'anthropic', label: 'Anthropic API Key', type: 'password' },
                    { key: 'openai', label: 'OpenAI API Key', type: 'password' },
                ]
            },
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
        const title = summary.title || 'Agent task';
        const n = Math.max(0, Math.floor(totalSteps || 0));
        const files = summary.fileChanges || [];
        const td = summary.todos;
        let progressLabel;
        if (td && td.total > 0) {
            progressLabel = `${td.done} of ${td.total} to-dos completed`;
        } else if (n > 0) {
            progressLabel = `${n} agent step${n === 1 ? '' : 's'}`;
        } else {
            progressLabel = 'Completed';
        }

        const el = document.createElement('div');
        el.className = 'agent-run-card';

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

        const kicker = 'Build';
        const actionsHtml = `<div class="agent-run-actions">
                    <button type="button" class="agent-run-btn agent-run-btn-primary" data-agent-run-action="review">Review</button>
                    <button type="button" class="agent-run-btn" data-agent-run-action="commit" disabled title="Not wired yet">Create branch &amp; commit</button>
                </div>`;
        el.innerHTML = `
            <div class="agent-run-card-inner">
                <div class="agent-run-kicker">${kicker}</div>
                <div class="agent-run-title">${this.escapeHtml(title)}</div>
                <div class="agent-todo-strip">
                    <span class="agent-todo-check" aria-hidden="true">✓</span>
                    <span class="agent-todo-text">${this.escapeHtml(progressLabel)}</span>
                </div>
                <p class="agent-run-blurb">Worked for ${this._formatRunDuration(totalElapsed)}.${files.length ? ' Edits below.' : ''}</p>
                ${files.length ? `<div class="agent-changes-heading">Summary of changes</div>${changesHtml}` : ''}
                ${actionsHtml}
            </div>
        `;

        // Wire the "Review" button (only present in code mode — actionsHtml is non-empty)
        const reviewBtn = el.querySelector('[data-agent-run-action="review"]');
        if (reviewBtn) {
            reviewBtn.addEventListener('click', () => {
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
        this.ensureStepRendered();

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

    // ── DOM Helpers ─────────────────────────────────────────────

    addUserMessage(text, images = []) {
        this._removeChatEmptyState();
        const el = document.createElement('div');
        el.className = 'msg-user';

        let imagesHtml = '';
        if (images && images.length > 0) {
            const thumbs = images.map(img =>
                `<img src="${img.dataUrl || `data:${img.media_type};base64,${img.data}`}"
                      style="max-width:120px;max-height:80px;border-radius:4px;border:1px solid var(--border);cursor:pointer"
                      onclick="app.showLightbox(this.src)"
                      alt="Attached">`
            ).join('');
            imagesHtml = `<div style="display:flex;gap:8px;margin-bottom:8px;flex-wrap:wrap">${thumbs}</div>`;
        }

        el.innerHTML = `
            <div class="msg-user-content">${imagesHtml}${this.escapeHtml(text)}</div>
            <div class="msg-actions msg-actions-user">
                <button class="msg-action-btn" data-action="fork" title="Fork from this message">
                    <svg width="14" height="14" viewBox="0 0 14 14" fill="none">
                        <circle cx="3" cy="3" r="1.5" stroke="currentColor" stroke-width="1.1"/>
                        <circle cx="11" cy="3" r="1.5" stroke="currentColor" stroke-width="1.1"/>
                        <circle cx="7" cy="11" r="1.5" stroke="currentColor" stroke-width="1.1"/>
                        <path d="M3 4.5V7c0 1 .8 1.8 1.8 1.8h4.4c1 0 1.8-.8 1.8-1.8V4.5" stroke="currentColor" stroke-width="1.1" fill="none"/>
                        <path d="M7 8.8v.7" stroke="currentColor" stroke-width="1.1"/>
                    </svg>
                </button>
            </div>
        `;
        el.querySelector('[data-action="fork"]')?.addEventListener('click', () => {
            this._forkFromUserMessage(el);
        });
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

    scrollToBottom() {
        requestAnimationFrame(() => {
            this.chatContainer.scrollTop = this.chatContainer.scrollHeight;
            this._syncChatScrollEndBtn();
        });
    }

    _syncChatScrollEndBtn() {
        if (!this.chatScrollEndBtn || !this.chatContainer) return;
        const el = this.chatContainer;
        const room = el.scrollHeight - el.scrollTop - el.clientHeight;
        this.chatScrollEndBtn.style.display = room < 120 ? 'none' : 'flex';
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

        // Group by project
        const projectMap = new Map();
        for (const s of sessions) {
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

        el.innerHTML = `
            <div class="agent-row-title">${this.escapeHtml(session.title || 'New session')}</div>
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

        menu.innerHTML = `
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
            if (action === 'delete') {
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
