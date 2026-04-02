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

        // Mode tabs — Chat vs Code
        this.sessionMode = 'code';          // active tab (affects new session creation)
        this.currentSessionMode = 'code';   // loaded session's mode (affects rendering)
        this.sessionRole = 'generator';     // active code-session role for new sessions
        this.currentSessionRole = 'generator'; // loaded session's role
        this.chatGroups = [];               // ordered list of group names
        this.expandedGroups = new Set();     // which groups are expanded in sidebar

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
        this.projectFilter = 'all'; // 'all' = all projects (default), null = current project, or a path
        this.harnessState = null;
        this.harnessCycles = [];
        this.harnessCyclePoller = null;

        // View state
        this.currentView = 'chat';
        this.settings = {};

        // Command center state
        this.commandPanel = 'fleet';
        this.commandAgents = [];
        this.commandTasks = [];
        this.commandFeed = [];
        this.commandProjects = [];
        this.cmdSelectedProject = null;
        this.cmdDashTab = 'plan';
        this.commandCenter = document.getElementById('command-center');

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
        this.headerStatus = document.getElementById('header-status');
        this.headerProject = document.getElementById('header-project');
        this.sidebarCwd = document.getElementById('sidebar-cwd');
        this.sidebarProjectName = document.getElementById('sidebar-project-name');
        this.sessionList = document.getElementById('session-list');
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
        this.scheduleView = document.getElementById('schedule-view');
        this.dispatchView = document.getElementById('dispatch-view');
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
        // Mode tabs (Chat / Code)
        document.querySelectorAll('.mode-tab').forEach(tab => {
            tab.addEventListener('click', () => this.setSessionMode(tab.dataset.mode));
        });

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
        });

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
        document.getElementById('new-session-btn').addEventListener('click', () => {
            this.showNewSessionSetup();
        });

        // Add project button (next to project filter dropdown)
        document.getElementById('pf-add-project')?.addEventListener('click', () => {
            this.send({ command: 'folder_dialog' });
        });

        // Chat welcome screen — send button and Enter key
        const chatWelcomeSend = document.getElementById('chat-welcome-send');
        const chatWelcomeTextarea = document.getElementById('chat-welcome-textarea');
        if (chatWelcomeSend && chatWelcomeTextarea) {
            chatWelcomeSend.addEventListener('click', () => this._sendChatWelcomeMessage());
            chatWelcomeTextarea.addEventListener('keydown', (e) => {
                if (e.key === 'Enter' && !e.shiftKey) {
                    e.preventDefault();
                    this._sendChatWelcomeMessage();
                }
            });
            chatWelcomeTextarea.addEventListener('input', () => {
                chatWelcomeTextarea.style.height = 'auto';
                chatWelcomeTextarea.style.height = Math.min(chatWelcomeTextarea.scrollHeight, 200) + 'px';
            });
        }

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
            document.querySelector('.session-context-menu')?.remove();
        });

        // ── Sidebar Navigation ──
        document.querySelectorAll('.sidebar-nav-item[data-view]').forEach(item => {
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

        // Dispatch + Schedule "New" buttons
        document.getElementById('dispatch-new-btn')?.addEventListener('click', () => this.showDispatchDialog());
        document.getElementById('schedule-new-btn')?.addEventListener('click', () => this.showScheduleDialog());

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

        // ── Command Center ──
        document.getElementById('cmd-new-project')?.addEventListener('click', () => {
            this.cmdSelectedProject = null;
            this.renderCommandSidebar();
            this.renderNewProjectScreen();
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

        // Add user message to chat (with image thumbnails if attached)
        this.addUserMessage(text, this.attachedImages);

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

    // ── Mode Tabs (Chat / Code) ─────────────────────────────────

    setSessionMode(mode) {
        this.sessionMode = mode;
        document.querySelectorAll('.mode-tab').forEach(tab =>
            tab.classList.toggle('active', tab.dataset.mode === mode));

        // Command mode: show command center, hide everything else
        if (this.commandCenter) {
            this.commandCenter.style.display = mode === 'command' ? 'flex' : 'none';
        }
        if (mode === 'command') {
            // Hide all chat/feature views
            this.chatContainer.style.display = 'none';
            this.inputBar.style.display = 'none';
            this.welcomeScreen.style.display = 'none';
            const chatWelcome = document.getElementById('chat-welcome-screen');
            if (chatWelcome) chatWelcome.style.display = 'none';
            if (this.settingsView) this.settingsView.style.display = 'none';
            if (this.scheduleView) this.scheduleView.style.display = 'none';
            if (this.dispatchView) this.dispatchView.style.display = 'none';
            // Hide the entire main sidebar — Command Center has its own project sidebar
            const sidebar = document.getElementById('sidebar');
            if (sidebar) sidebar.style.display = 'none';
            this.currentView = 'command';
            this.initCommandCenter();
            return;
        }

        // Restore the main sidebar when leaving command mode
        const sidebar = document.getElementById('sidebar');
        if (sidebar) sidebar.style.display = '';

        // Toggle sidebar content — project filter vs chat groups
        const projectFilter = document.getElementById('sidebar-project-filter');
        if (projectFilter) projectFilter.style.display = mode === 'code' ? '' : 'none';

        // Update "New Session" / "New Chat" label
        const newBtn = document.getElementById('new-session-btn');
        if (newBtn) {
            newBtn.style.display = '';
            const label = newBtn.querySelector('span');
            if (label) label.textContent = mode === 'chat' ? 'New Chat' : 'New Session';
        }

        const roleSelect = document.getElementById('setup-session-role');
        if (roleSelect) roleSelect.style.display = mode === 'chat' ? 'none' : '';

        this.updateHarnessBadge();
        this.renderFilteredSessions();
    }

    applySessionModeUI(mode) {
        this.currentSessionMode = mode || 'code';
        document.body.classList.toggle('chat-mode', this.currentSessionMode === 'chat');
        this.updateHarnessBadge();
    }

    applySessionRoleUI(role) {
        this.currentSessionRole = role || 'generator';
        document.body.dataset.sessionRole = this.currentSessionRole;
        this.updateHarnessBadge();
    }

    formatSessionRole(role) {
        const labels = { planner: 'Planner', generator: 'Generator', evaluator: 'Evaluator', chat: 'Chat' };
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
            session_mode: this.currentSessionMode || 'code',
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
        const show = this.currentSessionMode !== 'chat' && !!this.harnessState;
        this.harnessBadge.style.display = show ? 'flex' : 'none';
        if (!show) return;

        const sprint = this.harnessState.active_sprint_id || 'no sprint';
        const activeCycle = this.getActiveHarnessCycle();
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

    _populateChatWelcomeModels() {
        const select = document.getElementById('chat-welcome-model');
        if (!select || !this.backends) return;
        const currentVal = this.modelSelector?.value || '';
        const idx = currentVal.indexOf(':');
        const curBackend = idx > 0 ? currentVal.substring(0, idx) : '';
        const curModel = idx > 0 ? currentVal.substring(idx + 1) : '';
        this._populateSelectWithGroupedModels(select, this.backends, curBackend, curModel);
    }

    _sendChatWelcomeMessage() {
        const textarea = document.getElementById('chat-welcome-textarea');
        const select = document.getElementById('chat-welcome-model');
        if (!textarea || !select) return;
        const text = textarea.value.trim();
        if (!text) return;

        // Parse backend:model from select
        const val = select.value;
        const idx = val.indexOf(':');
        if (idx <= 0) return;
        const backend = val.substring(0, idx);
        const model = val.substring(idx + 1);

        // Select backend (creates session), then send message once connected
        this._pendingChatMessage = text;
        this.send({ command: 'select_backend', backend, model, session_mode: 'chat', session_role: 'chat' });

        // Clear and hide welcome
        textarea.value = '';
        const chatWelcome = document.getElementById('chat-welcome-screen');
        if (chatWelcome) chatWelcome.style.display = 'none';
        this.chatContainer.style.display = 'flex';
        this.inputBar.style.display = 'flex';
        this.applySessionModeUI('chat');
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
                this.applySessionModeUI(this.sessionMode);
                this.applySessionRoleUI(event.session_role || this.sessionRole);
                this.renderFilteredSessions();
                this.showChatInterface();
                break;
            case 'session_loaded':
                this.chatMessages.innerHTML = '';
                this.currentSessionId = event.current_session_id || '';
                this.sessions = event.sessions || [];
                // Apply saved session mode before replay
                this.applySessionModeUI(event.session_mode || 'code');
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
            case 'chat_groups':
                this.chatGroups = event.groups || [];
                if (this.sessionMode === 'chat') this.renderFilteredSessions();
                break;
            case 'harness_state':
                this.harnessState = event.data || null;
                this.updateHarnessBadge();
                this.rerenderHarnessPopoverIfOpen();
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
            case 'dispatch_submitted':
            case 'dispatch_cancelled':
                this.requestDispatchList();
                break;
            case 'dispatch_list':
                this.renderDispatchList(event.tasks || []);
                break;
            case 'dispatch_result':
                this.renderDispatchResult(event.task);
                break;
            case 'schedule_created':
                this.requestScheduleList();
                break;
            case 'schedule_list':
                this.renderScheduleList(event.schedules || []);
                break;
            // ── Command Center Events ──
            case 'command_project_list':
                this.commandProjects = event.projects || [];
                if (this.sessionMode === 'command') {
                    this.renderCommandSidebar();
                }
                break;
            case 'command_project_created':
                this.commandProjects = event.projects || this.commandProjects;
                this.cmdSelectedProject = event.project?.id || this.cmdSelectedProject;
                if (this.sessionMode === 'command') {
                    this.renderCommandSidebar();
                    if (event.project) this.renderProjectDashboard(event.project);
                }
                break;
            case 'command_project_status':
                // Update project in list and render dashboard
                if (event.project) {
                    const idx = this.commandProjects.findIndex(p => p.id === event.project.id);
                    if (idx >= 0) this.commandProjects[idx] = event.project;
                    if (this.sessionMode === 'command') {
                        this.renderCommandSidebar();
                        if (this.cmdSelectedProject === event.project.id) {
                            this.renderProjectDashboard(event.project);
                        }
                    }
                }
                break;
            case 'command_project_chat_response':
                this._handleProjectChatResponse(event);
                break;
            case 'command_project_chat_delta':
                // Streaming text delta — update the thinking indicator with partial text
                this._handleProjectChatDelta(event);
                break;
            case 'command_feed_posted':
                this.commandFeed.push(event.message);
                break;
            case 'command_project_files':
                this.cmdProjectFiles = event.files || [];
                if (this.sessionMode === 'command' && this.cmdDashTab === 'results') {
                    const proj = this.commandProjects.find(p => p.id === event.project_id);
                    if (proj) this.renderProjectDashboard(proj);
                }
                break;
            case 'command_project_preview':
                if (event.url) {
                    window.open(event.url, '_blank');
                } else if (event.error) {
                    alert(event.error);
                }
                break;
            case 'command_org_chart':
                this._orgChartData = event;
                if (this.sessionMode === 'command' && this.cmdDashTab === 'org_chart') {
                    const proj = this.commandProjects.find(p => p.id === event.project_id);
                    if (proj) {
                        proj.org_chart = event.nodes;
                        this._renderDashOrgChart(document.getElementById('project-dash-content'), proj);
                    }
                }
                break;
            case 'command_project_file_content':
                {
                    const codeEl = document.getElementById('results-code');
                    const viewEl = document.getElementById('results-file-content');
                    const nameEl = document.getElementById('results-viewing-file');
                    if (codeEl && viewEl) {
                        codeEl.textContent = event.content || '(empty file)';
                        if (nameEl) nameEl.textContent = event.path || '';
                        viewEl.style.display = 'block';
                    }
                }
                break;
            case 'command_fleet':
                this.commandAgents = event.agents || [];
                break;
            case 'command_spawn_ok':
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
            harness_cycles,
        } = event;

        // Update project info
        if (cwd) {
            const short = cwd.split('/').pop();
            this.headerProject.textContent = short;
            this.sidebarProjectName.textContent = short;
            this.sidebarCwd.textContent = cwd;
            this.currentCwd = cwd;
        }

        // Store backends for later use
        this.backends = backends || {};
        this.handlesTools = event.handles_tools || false;

        // Store recent projects and chat groups
        if (recent_projects) {
            this.recentProjects = recent_projects;
        }
        if (event.chat_groups) {
            this.chatGroups = event.chat_groups;
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

        // Update sessions list
        if (sessions) {
            this.sessions = sessions;
            this.allSessions = all_sessions || [];
            this.currentSessionId = current_session_id || '';
            this.applySessionModeUI(current_session_mode || this.currentSessionMode || 'code');
            this.applySessionRoleUI(current_session_role || this.currentSessionRole || 'generator');
            this.sessionRole = current_session_role || this.sessionRole;
            this.buildProjectFilter();
            this.renderFilteredSessions();
        }

        // If already connected to a backend, show chat
        if (current_backend) {
            if (!refresh_only) {
                this.showChatInterface();
            }
            this.headerStatus.textContent = `${current_backend} · ${current_model}`;
            this.populateModelSelector(backends, current_backend, current_model);

            // Send pending chat message (from chat welcome screen)
            if (this._pendingChatMessage) {
                const text = this._pendingChatMessage;
                this._pendingChatMessage = null;
                // Render user message and send
                this.addUserMessage(text);
                this.send({ command: 'message', text });
                this.setRunning(true);
            }
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
                const detail = info.patterns
                    ? info.patterns.toLocaleString() + ' patterns'
                    : modelCount + (modelCount === 1 ? ' model' : ' models');
                const isPreferred = preferred && preferred.backend === key;
                const detailText = isPreferred
                    ? `${backendDescs[key] || detail} · Recommended`
                    : (backendDescs[key] || detail);

                card.innerHTML = `
                    <div class="backend-card-icon">${backendIcons[key] || '●'}</div>
                    <div class="backend-card-info">
                        <div class="backend-card-name">${backendLabels[key] || key}</div>
                        <div class="backend-card-detail">${detailText}</div>
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
        const sessionRole = this.sessionMode === 'chat'
            ? 'chat'
            : (document.getElementById('setup-session-role')?.value || this.sessionRole || 'generator');
        this.sessionRole = sessionRole;
        this.send({
            command: 'select_backend',
            backend: backendType,
            model,
            session_mode: this.sessionMode,
            session_role: sessionRole,
        });
    }

    showChatInterface() {
        this.welcomeScreen.style.display = 'none';
        const chatWelcome = document.getElementById('chat-welcome-screen');
        if (chatWelcome) chatWelcome.style.display = 'none';
        this.chatContainer.style.display = 'flex';
        this.inputBar.style.display = 'flex';
        // Hide other views if they were visible
        if (this.settingsView) this.settingsView.style.display = 'none';
        if (this.scheduleView) this.scheduleView.style.display = 'none';
        if (this.dispatchView) this.dispatchView.style.display = 'none';
        // Update nav
        this.currentView = 'chat';
        document.querySelectorAll('.sidebar-nav-item[data-view]').forEach(el =>
            el.classList.toggle('active', el.dataset.view === 'chat'));
        this.userInput.focus();
    }

    setPermissionMode(mode, notifyServer = true) {
        this.permissionMode = mode;

        const icons = { ask: '⚙', 'auto-edit': '</>', plan: '☰', bypass: '△' };
        const labels = { ask: 'Suggest (read-only)', 'auto-edit': 'Auto-edit (files OK, shell asks)', plan: 'Plan mode', bypass: 'Full-auto (sandboxed)' };

        document.getElementById('perm-icon').textContent = icons[mode] || '△';
        document.getElementById('perm-label').textContent = labels[mode] || mode;

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
        if (this.currentSessionMode === 'chat') return;
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
        if (this.currentSessionMode === 'chat') return;
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

        if (this.currentSessionMode === 'chat') return;

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

        if (this.currentSessionMode === 'chat') return;

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
    }

    closePreviewPanel() {
        this.previewOpen = false;
        this.previewPanel.classList.remove('open');
        this.previewPanel.style.width = '';
        this.previewPanel.style.minWidth = '';
        this.previewResize.style.display = 'none';
        this.previewToggle.classList.remove('active');
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

        // If coming from command mode, switch mode tab back
        if (this.sessionMode === 'command') {
            this.sessionMode = 'code';
            document.querySelectorAll('.mode-tab').forEach(tab =>
                tab.classList.toggle('active', tab.dataset.mode === 'code'));
        }

        // Hide all views
        this.welcomeScreen.style.display = 'none';
        this.chatContainer.style.display = 'none';
        this.inputBar.style.display = 'none';
        if (this.commandCenter) this.commandCenter.style.display = 'none';
        if (this.settingsView) this.settingsView.style.display = 'none';
        if (this.scheduleView) this.scheduleView.style.display = 'none';
        if (this.dispatchView) this.dispatchView.style.display = 'none';

        // Show session list only in chat view
        const sessionList = document.getElementById('session-list');
        if (sessionList) sessionList.style.display = viewName === 'chat' ? '' : 'none';

        // Show project filter only in chat view + code mode
        const pf = document.getElementById('sidebar-project-filter');
        if (pf) pf.style.display = (viewName === 'chat' && this.sessionMode === 'code') ? '' : 'none';

        // Show search only in chat view
        const search = document.querySelector('.sidebar-search');
        if (search) search.style.display = viewName === 'chat' ? '' : 'none';

        // Update nav active state
        document.querySelectorAll('.sidebar-nav-item[data-view]').forEach(el =>
            el.classList.toggle('active', el.dataset.view === viewName));

        // Show the requested view
        switch (viewName) {
            case 'chat':
                // Restore chat or welcome based on whether a session is active or backend is connected
                if (this.currentSessionId || (this.backends && Object.keys(this.backends).length > 0)) {
                    this.chatContainer.style.display = 'flex';
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
            case 'schedule':
                this.scheduleView.style.display = 'flex';
                this.requestScheduleList();
                break;
            case 'dispatch':
                this.dispatchView.style.display = 'flex';
                this.requestDispatchList();
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
                    { key: 'default_model', label: 'Default model', type: 'text' },
                    { key: 'default_permission_mode', label: 'Default permission mode', type: 'select',
                      options: [
                          { value: 'bypass', label: 'Full-auto (sandboxed)' },
                          { value: 'ask', label: 'Suggest (read-only)' },
                          { value: 'auto-edit', label: 'Auto-edit (files OK, shell asks)' },
                          { value: 'plan', label: 'Plan mode' },
                      ]
                    },
                    { key: 'theme', label: 'Theme', type: 'select',
                      options: [{ value: 'dark', label: 'Dark' }, { value: 'light', label: 'Light (coming soon)' }]
                    },
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
                        input = `<input class="settings-input" type="text" value="${this.escapeHtml(String(val))}" data-section="${section.id}" data-key="${field.key}" />`;
                    }
                    bodyHtml += `<div class="settings-row"><span class="settings-row-label">${field.label}</span><div class="settings-row-value">${input}</div></div>`;
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

    // ── Keyboard Shortcuts ──────────────────────────────────────

    _handleKeyboardShortcut(e) {
        // Don't intercept when typing in inputs (except specific combos)
        const tag = e.target.tagName.toLowerCase();
        const inInput = tag === 'input' || tag === 'textarea' || tag === 'select';

        // Ctrl+/ or Ctrl+? → shortcuts help
        if ((e.ctrlKey || e.metaKey) && (e.key === '/' || e.key === '?')) {
            e.preventDefault();
            this.toggleShortcutsOverlay();
            return;
        }

        // Escape → close overlays (already handled elsewhere, but also close shortcuts)
        if (e.key === 'Escape') {
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
            document.getElementById('new-session-btn')?.click();
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

        // 1-4 keys → switch views (when not in input)
        if (e.altKey && e.key >= '1' && e.key <= '4') {
            e.preventDefault();
            const views = ['chat', 'schedule', 'dispatch', 'settings'];
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
                { label: 'New session', keys: ['Ctrl', 'N'] },
                { label: 'Settings', keys: ['Ctrl', ','] },
                { label: 'Shortcuts help', keys: ['Ctrl', '/'] },
                { label: 'Toggle sidebar', keys: ['Ctrl', 'Shift', 'D'] },
                { label: 'Switch to Chat', keys: ['Alt', '1'] },
                { label: 'Switch to Scheduled', keys: ['Alt', '2'] },
                { label: 'Switch to Dispatch', keys: ['Alt', '3'] },
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

    // ── Status ──────────────────────────────────────────────────

    handleStatus(event) {
        this.lastModel = event.model || this.lastModel;
        this.lastStats = event.stats || this.lastStats;

        // Update header
        if (this.lastModel) {
            const parts = [this.lastModel];
            if (this.lastStats) {
                const inp = this.lastStats.input_tokens;
                const out = this.lastStats.output_tokens;
                if (inp && out) parts.push(`${inp}→${out} tok`);
                const sessionCost = this.lastStats.session_cost_usd;
                if (sessionCost) {
                    parts.push(`$${Number(sessionCost).toFixed(4)}`);
                }
            }
            this.tokenInfo.textContent = parts.join(' · ');
        }
    }

    // ── Session End ─────────────────────────────────────────────

    handleSessionEnd(event) {
        this.removeThinking();

        // Clear terminal bar
        this.clearTerminals();

        // Finalize CLI tool activity group
        this.finalizeToolActivityGroup();

        // Flush collapsed group
        this.flushCollapsedGroup();

        const totalElapsed = event.total_elapsed || 0;
        const totalSteps = event.total_steps || 0;

        if (totalSteps > 1) {
            const el = document.createElement('div');
            el.className = 'session-end';
            el.innerHTML = `<span class="check">✓</span> Done · ${totalSteps} steps · ${totalElapsed.toFixed(1)}s`;
            this.chatMessages.appendChild(el);
        }

        this.setRunning(false);
        this.scrollToBottom();

        // Refresh git status after session (files may have changed)
        if (!this.isReplaying) {
            this.requestGitStatus();
        }
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

        el.innerHTML = `<div class="msg-user-content">${imagesHtml}${this.escapeHtml(text)}</div>`;
        this.chatMessages.appendChild(el);
        this.scrollToBottom();
    }

    addAssistantMessage() {
        const el = document.createElement('div');
        el.className = 'msg-assistant';
        el.innerHTML = `<div class="message-content streaming-cursor"></div>`;
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
            <span>thinking</span>
        `;
        this.getRenderTarget().appendChild(el);
        this.scrollToBottom();
    }

    removeThinking() {
        // Remove from current target or anywhere in chat
        const target = this.getRenderTarget();
        const el = target.querySelector('[data-thinking]') ||
                   this.chatMessages.querySelector('[data-thinking]');
        if (el) el.remove();
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
        });
    }

    // ── Session Replay ──────────────────────────────────────────

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
        try {
            for (const event of events) {
                const type = event.event;

            // Skip ephemeral events
            if (SKIP_REPLAY.has(type)) continue;

            if (type === 'user_message') {
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
            html += '<div class="session-skeleton"><div class="skeleton-line"></div><div class="skeleton-line"></div></div>';
        }
        this.sessionList.innerHTML = html;
    }

    renderSessionList() {
        if (!this.sessionList) return;
        this.sessionList.innerHTML = '';

        if (this.sessions.length === 0) {
            this.sessionList.innerHTML = '<div class="session-empty">No previous sessions</div>';
            return;
        }

        for (const session of this.sessions) {
            const el = document.createElement('div');
            el.className = 'session-item' + (session.id === this.currentSessionId ? ' active' : '');

            const date = new Date(session.updated_at * 1000);
            const timeStr = this.formatRelativeTime(date);

            // Show project name when viewing all projects or a different project
            const showProject = this.projectFilter === 'all' || (this.projectFilter && this.projectFilter !== this.currentCwd);
            const projectTag = showProject && session.project_name
                ? `<span class="session-project-tag">${this.escapeHtml(session.project_name)}</span> · `
                : '';
            const roleTag = session.session_mode === 'chat'
                ? ''
                : `<span class="session-project-tag">${this.escapeHtml(this.formatSessionRole(session.session_role || 'generator'))}</span> · `;

            el.innerHTML = `
                <div class="session-item-title">${this.escapeHtml(session.title || 'New session')}</div>
                <div class="session-item-date">${projectTag}${roleTag}${session.model || ''} · ${timeStr}</div>
                <div class="session-item-actions">
                    <button class="session-menu-btn" title="More actions">&#8943;</button>
                </div>
            `;

            // Click to switch session (include project_path for cross-project sessions)
            el.addEventListener('click', (e) => {
                if (e.target.closest('.session-menu-btn')) return; // handled below
                if (session.id !== this.currentSessionId) {
                    const msg = { command: 'switch_session', session_id: session.id };
                    if (session.project_path) msg.project_path = session.project_path;
                    this.send(msg);
                }
            });

            // Context menu button (three dots)
            el.querySelector('.session-menu-btn').addEventListener('click', (e) => {
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

    // ── Project Filter ──────────────────────────────────────────

    buildProjectFilter() {
        const selected = document.getElementById('project-filter-selected');
        const dropdown = document.getElementById('project-filter-dropdown');
        const searchInput = document.getElementById('pf-search');
        const optionsContainer = document.getElementById('pf-options');
        if (!selected || !dropdown) return;

        // Build unique project list from allSessions
        const projectMap = new Map();
        for (const s of this.allSessions) {
            const path = (s.project_path || '').replace(/\\/g, '/');
            const name = s.project_name || path.split('/').pop() || path;
            if (path && !projectMap.has(path)) {
                projectMap.set(path, { name, path, count: 0 });
            }
            if (projectMap.has(path)) {
                projectMap.get(path).count++;
            }
        }
        this._projectOptions = projectMap;

        // Set the selected label
        this._updateFilterLabel();

        // Toggle dropdown
        selected.onclick = () => {
            const isOpen = dropdown.style.display !== 'none';
            dropdown.style.display = isOpen ? 'none' : 'flex';
            document.getElementById('sidebar-project-filter').classList.toggle('open', !isOpen);
            if (!isOpen) {
                searchInput.value = '';
                this._renderProjectOptions('');
                searchInput.focus();
            }
        };

        // Search filtering
        searchInput.oninput = () => {
            this._renderProjectOptions(searchInput.value.trim().toLowerCase());
        };

        // Close dropdown when clicking outside
        document.addEventListener('click', (e) => {
            if (!e.target.closest('#sidebar-project-filter')) {
                dropdown.style.display = 'none';
                document.getElementById('sidebar-project-filter')?.classList.remove('open');
            }
        });

        this._renderProjectOptions('');
    }

    _updateFilterLabel() {
        const nameEl = document.getElementById('pf-selected-name');
        if (!nameEl) return;
        if (this.projectFilter === 'all') {
            nameEl.textContent = 'All Projects';
        } else if (this.projectFilter) {
            const name = this.projectFilter.replace(/\\/g, '/').split('/').pop();
            nameEl.textContent = name;
        } else {
            const name = (this.currentCwd || '').split('/').pop() || 'Current Project';
            nameEl.textContent = name;
        }
    }

    _renderProjectOptions(filter) {
        const container = document.getElementById('pf-options');
        if (!container) return;
        container.innerHTML = '';

        const currentPath = (this.currentCwd || '').replace(/\\/g, '/');

        // "All Projects" option (always first)
        if (!filter || 'all projects'.includes(filter)) {
            const opt = document.createElement('div');
            opt.className = 'pf-option' + (this.projectFilter === 'all' ? ' active' : '');
            opt.innerHTML = `<span class="pf-opt-name">All Projects</span><span class="pf-opt-count">${this.allSessions.length}</span>`;
            opt.addEventListener('click', () => this._selectProjectFilter('all'));
            container.appendChild(opt);
        }

        // "Current Project" option
        const currentName = currentPath.split('/').pop() || 'Current';
        if (!filter || currentName.toLowerCase().includes(filter)) {
            const opt = document.createElement('div');
            opt.className = 'pf-option' + (!this.projectFilter ? ' active' : '');
            opt.innerHTML = `<span class="pf-opt-name">${this.escapeHtml(currentName)}</span><span class="pf-opt-count">${this.sessions.length}</span>`;
            opt.addEventListener('click', () => this._selectProjectFilter(null));
            container.appendChild(opt);
        }

        // Individual projects
        for (const [path, info] of this._projectOptions || []) {
            const normPath = path.replace(/\\/g, '/');
            if (normPath === currentPath) continue; // already shown as "Current"
            if (filter && !info.name.toLowerCase().includes(filter)) continue;

            const opt = document.createElement('div');
            opt.className = 'pf-option' + (this.projectFilter === path ? ' active' : '');
            opt.innerHTML = `<span class="pf-opt-name">${this.escapeHtml(info.name)}</span><span class="pf-opt-count">${info.count}</span>`;
            opt.addEventListener('click', () => this._selectProjectFilter(path));
            container.appendChild(opt);
        }
    }

    _selectProjectFilter(value) {
        this.projectFilter = value;
        this._updateFilterLabel();
        this.renderFilteredSessions();
        // Close dropdown
        const dropdown = document.getElementById('project-filter-dropdown');
        if (dropdown) dropdown.style.display = 'none';
        document.getElementById('sidebar-project-filter')?.classList.remove('open');
    }

    renderFilteredSessions() {
        let sessionsToShow;
        if (this.projectFilter === 'all') {
            sessionsToShow = this.allSessions;
        } else if (this.projectFilter) {
            // Specific project path
            const norm = this.projectFilter.replace(/\\/g, '/').toLowerCase();
            sessionsToShow = this.allSessions.filter(s =>
                (s.project_path || '').replace(/\\/g, '/').toLowerCase() === norm
            );
        } else {
            // Current project (default)
            sessionsToShow = this.sessions;
        }

        // Filter by session mode (Chat vs Code tab)
        sessionsToShow = sessionsToShow.filter(s =>
            (s.session_mode || 'code') === this.sessionMode
        );

        // Chat mode: render grouped sidebar
        if (this.sessionMode === 'chat') {
            this.renderChatSidebar(sessionsToShow);
            return;
        }

        // Code mode: flat list
        const saved = this.sessions;
        this.sessions = sessionsToShow;
        this.renderSessionList();
        this.sessions = saved;
    }

    // ── Chat Sidebar (grouped sessions) ────────────────────────

    renderChatSidebar(sessions) {
        if (!this.sessionList) return;
        this.sessionList.innerHTML = '';

        if (sessions.length === 0 && this.chatGroups.length === 0) {
            this.sessionList.innerHTML = '<div class="session-empty">No chat sessions</div>';
            return;
        }

        // Partition sessions by group
        const grouped = {};
        const ungrouped = [];
        for (const s of sessions) {
            const g = s.chat_group || '';
            if (g) {
                (grouped[g] = grouped[g] || []).push(s);
            } else {
                ungrouped.push(s);
            }
        }

        // Render ungrouped sessions first (no header)
        for (const s of ungrouped) {
            this.sessionList.appendChild(this._createChatSessionItem(s));
        }

        // Render each group
        for (const groupName of this.chatGroups) {
            const groupSessions = grouped[groupName] || [];
            const isExpanded = this.expandedGroups.has(groupName);

            const header = document.createElement('div');
            header.className = 'chat-group-header' + (isExpanded ? ' expanded' : '');
            header.innerHTML = `
                <span class="chat-group-chevron">${isExpanded ? '▾' : '▸'}</span>
                <span class="chat-group-name">${this.escapeHtml(groupName)}</span>
                <span class="chat-group-count">${groupSessions.length}</span>
                <button class="chat-group-menu-btn" title="Group options">&#8943;</button>
            `;

            header.addEventListener('click', (e) => {
                if (e.target.closest('.chat-group-menu-btn')) return;
                if (this.expandedGroups.has(groupName)) {
                    this.expandedGroups.delete(groupName);
                } else {
                    this.expandedGroups.add(groupName);
                }
                this.renderFilteredSessions();
            });

            header.querySelector('.chat-group-menu-btn').addEventListener('click', (e) => {
                e.stopPropagation();
                this.showGroupContextMenu(e, groupName);
            });

            this.sessionList.appendChild(header);

            if (isExpanded) {
                const container = document.createElement('div');
                container.className = 'chat-group-sessions';
                for (const s of groupSessions) {
                    container.appendChild(this._createChatSessionItem(s));
                }
                this.sessionList.appendChild(container);
            }
        }

        // "New group" button
        const addBtn = document.createElement('div');
        addBtn.className = 'chat-group-add';
        addBtn.innerHTML = '<span>+</span> New group';
        addBtn.addEventListener('click', () => {
            const name = prompt('Group name:');
            if (name && name.trim()) {
                this.send({ command: 'create_chat_group', name: name.trim() });
            }
        });
        this.sessionList.appendChild(addBtn);
    }

    _createChatSessionItem(session) {
        const el = document.createElement('div');
        el.className = 'session-item' + (session.id === this.currentSessionId ? ' active' : '');

        const date = new Date(session.updated_at * 1000);
        const timeStr = this.formatRelativeTime(date);
        const roleTag = session.session_mode === 'chat'
            ? ''
            : `<span class="session-project-tag">${this.escapeHtml(this.formatSessionRole(session.session_role || 'generator'))}</span> · `;

        el.innerHTML = `
            <div class="session-item-title">${this.escapeHtml(session.title || 'New session')}</div>
            <div class="session-item-date">${roleTag}${session.model || ''} · ${timeStr}</div>
            <div class="session-item-actions">
                <button class="session-menu-btn" title="More actions">&#8943;</button>
            </div>
        `;

        el.addEventListener('click', (e) => {
            if (e.target.closest('.session-menu-btn')) return;
            if (session.id !== this.currentSessionId) {
                const msg = { command: 'switch_session', session_id: session.id };
                if (session.project_path) msg.project_path = session.project_path;
                this.send(msg);
            }
        });

        el.querySelector('.session-menu-btn').addEventListener('click', (e) => {
            e.stopPropagation();
            this.showChatSessionContextMenu(e, session);
        });

        return el;
    }

    showGroupContextMenu(e, groupName) {
        document.querySelector('.session-context-menu')?.remove();

        const menu = document.createElement('div');
        menu.className = 'session-context-menu';
        menu.innerHTML = `
            <div class="ctx-item" data-action="rename">&#9998; Rename</div>
            <div class="ctx-separator"></div>
            <div class="ctx-item danger" data-action="delete">&#128465; Delete</div>
        `;
        menu.style.left = `${e.clientX}px`;
        menu.style.top = `${e.clientY}px`;

        menu.addEventListener('click', (ev) => {
            const action = ev.target.closest('.ctx-item')?.dataset.action;
            if (action === 'rename') {
                const newName = prompt('Rename group:', groupName);
                if (newName && newName.trim()) {
                    this.send({ command: 'rename_chat_group', old_name: groupName, new_name: newName.trim() });
                }
            } else if (action === 'delete') {
                this.send({ command: 'delete_chat_group', name: groupName });
            }
            menu.remove();
        });

        document.body.appendChild(menu);
        setTimeout(() => {
            const close = (e2) => { if (!menu.contains(e2.target)) { menu.remove(); document.removeEventListener('click', close); } };
            document.addEventListener('click', close);
        }, 0);
    }

    showChatSessionContextMenu(e, session) {
        document.querySelector('.session-context-menu')?.remove();

        const menu = document.createElement('div');
        menu.className = 'session-context-menu';

        // Build group submenu items
        const groupItems = this.chatGroups.map(g =>
            `<div class="ctx-item ctx-sub" data-action="move" data-group="${this.escapeHtml(g)}">${this.escapeHtml(g)}${(session.chat_group === g) ? ' ✓' : ''}</div>`
        ).join('');

        menu.innerHTML = `
            <div class="ctx-item" data-action="rename">&#9998; Rename</div>
            ${this.chatGroups.length > 0 ? `
                <div class="ctx-separator"></div>
                <div class="ctx-label">Move to group</div>
                ${groupItems}
                ${session.chat_group ? `<div class="ctx-item ctx-sub" data-action="ungroup">Remove from group</div>` : ''}
            ` : ''}
            <div class="ctx-separator"></div>
            <div class="ctx-item danger" data-action="delete">&#128465; Delete</div>
        `;
        menu.style.left = `${e.clientX}px`;
        menu.style.top = `${e.clientY}px`;

        menu.addEventListener('click', (ev) => {
            const item = ev.target.closest('.ctx-item');
            if (!item) return;
            const action = item.dataset.action;
            if (action === 'rename') {
                const newTitle = prompt('Rename session:', session.title);
                if (newTitle && newTitle.trim()) {
                    this.send({ command: 'rename_session', session_id: session.id, title: newTitle.trim() });
                }
            } else if (action === 'delete') {
                this.send({ command: 'delete_session', session_id: session.id });
            } else if (action === 'move') {
                this.send({ command: 'set_session_group', session_id: session.id, group: item.dataset.group });
            } else if (action === 'ungroup') {
                this.send({ command: 'set_session_group', session_id: session.id, group: '' });
            }
            menu.remove();
        });

        document.body.appendChild(menu);
        setTimeout(() => {
            const close = (e2) => { if (!menu.contains(e2.target)) { menu.remove(); document.removeEventListener('click', close); } };
            document.addEventListener('click', close);
        }, 0);
    }

    showSessionContextMenu(e, session) {
        // Remove any existing menu
        document.querySelector('.session-context-menu')?.remove();

        const menu = document.createElement('div');
        menu.className = 'session-context-menu';

        menu.innerHTML = `
            <div class="ctx-item" data-action="rename">&#9998; Rename</div>
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
                const newTitle = prompt('Rename session:', session.title);
                if (newTitle && newTitle.trim()) {
                    this.send({ command: 'rename_session', session_id: session.id, title: newTitle.trim() });
                }
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
        return date.toLocaleDateString();
    }

    // ── New Session Setup ──────────────────────────────────────

    showNewSessionSetup() {
        // Hide all main views
        this.welcomeScreen.style.display = 'none';
        this.chatContainer.style.display = 'none';
        this.inputBar.style.display = 'none';
        const chatWelcome = document.getElementById('chat-welcome-screen');
        if (chatWelcome) chatWelcome.style.display = 'none';
        if (this.settingsView) this.settingsView.style.display = 'none';
        if (this.scheduleView) this.scheduleView.style.display = 'none';
        if (this.dispatchView) this.dispatchView.style.display = 'none';

        // Chat mode: show simple chat welcome
        if (this.sessionMode === 'chat') {
            if (chatWelcome) {
                chatWelcome.style.display = 'flex';
                this._populateChatWelcomeModels();
                document.getElementById('chat-welcome-textarea')?.focus();
            }
            this.currentView = 'chat';
            document.querySelectorAll('.sidebar-nav-item[data-view]').forEach(el =>
                el.classList.toggle('active', el.dataset.view === 'chat'));
            return;
        }

        // Code mode: show project picker
        this.welcomeScreen.style.display = 'flex';
        // Update nav
        this.currentView = 'chat';
        document.querySelectorAll('.sidebar-nav-item[data-view]').forEach(el =>
            el.classList.toggle('active', el.dataset.view === 'chat'));

        // Clear and close preview panel for new session
        this.clearPreviewPanel();
        this.closePreviewPanel();

        const projectStep = document.getElementById('project-step');
        const backendStep = document.getElementById('backend-step');
        const roleSelect = document.getElementById('setup-session-role');
        projectStep.style.display = 'block';
        backendStep.style.display = 'none';
        if (roleSelect) {
            roleSelect.value = this.sessionRole || 'generator';
            roleSelect.style.display = this.sessionMode === 'chat' ? 'none' : '';
            roleSelect.onchange = () => {
                this.sessionRole = roleSelect.value || 'generator';
            };
        }

        const input = document.getElementById('welcome-folder-input');
        input.value = this.currentCwd || '';

        // Bind folder open
        const openBtn = document.getElementById('welcome-folder-open');
        openBtn.onclick = () => {
            const path = input.value.trim();
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
        // Set the project and move to backend selection step
        this.send({ command: 'set_project', path });

        // Update UI immediately
        const short = path.replace(/\\/g, '/').split('/').pop();
        this.currentCwd = path.replace(/\\/g, '/');
        this.headerProject.textContent = short;
        this.sidebarProjectName.textContent = short;
        this.sidebarCwd.textContent = path;
        // Reset filter to current project
        this.projectFilter = null;
        this._updateFilterLabel();

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

    // ── Command Center v2 — Project-Centric ────────────────────

    initCommandCenter() {
        // Called when entering command mode
        this.send({ command: 'command_project_list' });
        this.renderCommandSidebar();
        if (!this.cmdSelectedProject) {
            this.renderNewProjectScreen();
        }
    }

    renderCommandSidebar() {
        const list = document.getElementById('cmd-project-list');
        if (!list) return;

        const projects = this.commandProjects || [];
        if (projects.length === 0) {
            list.innerHTML = '<div style="padding:16px;color:var(--dim);font-size:12px;text-align:center">No projects yet</div>';
            return;
        }

        list.innerHTML = projects.map(p => `
            <div class="cmd-project-item ${p.id === this.cmdSelectedProject ? 'active' : ''}" data-id="${p.id}">
                <span class="cmd-project-dot ${p.status || 'idle'}"></span>
                <div class="cmd-project-info">
                    <div class="cmd-project-name">${this.escapeHtml(p.name)}</div>
                    <div class="cmd-project-status">${p.status || 'idle'}</div>
                </div>
            </div>
        `).join('');

        list.querySelectorAll('.cmd-project-item').forEach(item => {
            item.addEventListener('click', () => {
                this.cmdSelectedProject = item.dataset.id;
                this.renderCommandSidebar();
                this.send({ command: 'command_project_status', project_id: item.dataset.id });
            });
        });
    }

    renderNewProjectScreen() {
        const main = document.getElementById('cmd-main');
        if (!main) return;

        // Pre-fill with current project path
        const currentPath = this.currentCwd || '';

        main.innerHTML = `
            <div class="cmd-new-project">
                <h2>New Project</h2>
                <div class="cmd-subtitle">Set a high-level strategy and launch an AI coordinator to orchestrate the work.</div>

                <div class="cmd-form-group">
                    <label>Project Path</label>
                    <input type="text" class="settings-input" id="cmd-project-path" value="${this.escapeHtml(currentPath)}" placeholder="/path/to/your/project" />
                </div>

                <div class="cmd-form-group">
                    <label>Project Name</label>
                    <input type="text" class="settings-input" id="cmd-project-name" placeholder="e.g., Auth System Refactor" />
                </div>

                <div class="cmd-form-group">
                    <label>Strategy</label>
                    <textarea class="settings-input" id="cmd-project-strategy" rows="6"
                        placeholder="Describe the high-level objective. The AI coordinator will break this into tasks, spawn worker agents, and manage the execution.&#10;&#10;Example: Build a complete user authentication system with JWT tokens. Implement signup, login, password reset endpoints. Add auth middleware. Write tests for all endpoints."></textarea>
                </div>

                <div class="cmd-form-group">
                    <label>Coordinator Model</label>
                    <select class="settings-input" id="cmd-coordinator-model"></select>
                </div>

                <div style="display:flex;gap:10px;margin-top:8px">
                    <button class="btn-primary" id="cmd-launch-btn" style="padding:10px 24px;font-size:14px">
                        Launch Coordinator
                    </button>
                </div>
            </div>
        `;

        // Populate model selector dropdown
        const modelSelect = document.getElementById('cmd-coordinator-model');
        if (modelSelect) {
            this._populateCommandModelSelector(modelSelect);
        }

        document.getElementById('cmd-launch-btn')?.addEventListener('click', () => {
            const path = document.getElementById('cmd-project-path')?.value?.trim();
            const name = document.getElementById('cmd-project-name')?.value?.trim();
            const strategy = document.getElementById('cmd-project-strategy')?.value?.trim();
            if (!strategy) return;
            // Use the selected model from dropdown
            const selectedValue = document.getElementById('cmd-coordinator-model')?.value || '';
            const [backendType, backendModel] = selectedValue.includes(':') ? selectedValue.split(':') : ['', ''];
            this.send({
                command: 'command_project_create',
                path: path || this.currentCwd,
                name: name || (path || 'Project').split(/[/\\]/).pop(),
                strategy,
                backend: backendType,
                model: backendModel,
            });
        });
    }

    _populateCommandModelSelector(selectEl, selectedValue) {
        selectEl.innerHTML = '';
        const backendLabels = this._getBackendLabels ? this._getBackendLabels() : {};
        for (const [key, info] of Object.entries(this.backends || {})) {
            if (!info || !info.models || info.models.length === 0) continue;
            const labels = info.model_labels || {};
            const bLabel = backendLabels[key] || key;
            const optgroup = document.createElement('optgroup');
            optgroup.label = bLabel;
            for (const m of info.models) {
                const opt = document.createElement('option');
                opt.value = `${key}:${m}`;
                opt.textContent = labels[m] || m;
                if (selectedValue && opt.value === selectedValue) opt.selected = true;
                optgroup.appendChild(opt);
            }
            selectEl.appendChild(optgroup);
        }
        // If nothing selected, try to pick a sensible default
        if (!selectedValue && selectEl.options.length > 0) {
            // Prefer codex or claude-code
            for (const opt of selectEl.options) {
                if (opt.value.startsWith('codex:') || opt.value.startsWith('claude-code:')) {
                    opt.selected = true;
                    break;
                }
            }
        }
    }

    renderProjectDashboard(project) {
        const main = document.getElementById('cmd-main');
        if (!main) return;

        // Auto-refresh: poll every 5s while project has active agents
        if (this._cmdRefreshTimer) clearInterval(this._cmdRefreshTimer);
        const hasActive = (project.agents || []).some(a => a.status === 'running');
        if (hasActive && this.sessionMode === 'command') {
            this._cmdRefreshTimer = setInterval(() => {
                if (this.sessionMode !== 'command' || this.cmdSelectedProject !== project.id) {
                    clearInterval(this._cmdRefreshTimer);
                    return;
                }
                this.send({ command: 'command_project_status', project_id: project.id });
            }, 5000);
        }

        const tasks = project.tasks || [];
        const completedTasks = tasks.filter(t => t.status === 'completed').length;
        const totalTasks = tasks.length;
        const activeAgents = (project.agents || []).filter(a => a.status === 'running').length;
        const progressPct = totalTasks > 0 ? Math.round((completedTasks / totalTasks) * 100) : 0;
        const statusLabel = {
            idle: 'Idle', planning: 'Planning...', running: 'Running',
            completed: 'Completed', failed: 'Failed'
        }[project.status] || project.status;

        const dashTab = this.cmdDashTab || 'chat';

        main.innerHTML = `
            <div class="project-dashboard">
                <div class="project-dash-header">
                    <div class="project-dash-title">${this.escapeHtml(project.name)}</div>
                    <div class="project-dash-strategy">${this.escapeHtml(project.strategy)}</div>
                    <div class="project-dash-stats">
                        <span>Status: <span class="stat-value">${statusLabel}</span></span>
                        <span>Tasks: <span class="stat-value">${completedTasks}/${totalTasks}</span></span>
                        <span>Agents: <span class="stat-value">${activeAgents} active</span></span>
                        <span class="project-dash-actions">
                            <button class="btn-sm" id="dash-preview-btn" title="Preview in browser">▶ Preview</button>
                            <button class="btn-sm" id="dash-files-btn" title="View generated files">📁 Files</button>
                        </span>
                    </div>
                    <div class="project-dash-progress">
                        <div class="project-dash-progress-bar" style="width:${progressPct}%"></div>
                    </div>
                </div>
                <div class="project-dash-tabs">
                    <button class="project-dash-tab ${dashTab === 'chat' ? 'active' : ''}" data-tab="chat">Chat</button>
                    <button class="project-dash-tab ${dashTab === 'plan' ? 'active' : ''}" data-tab="plan">Plan</button>
                    <button class="project-dash-tab ${dashTab === 'agents' ? 'active' : ''}" data-tab="agents">Agents</button>
                    <button class="project-dash-tab ${dashTab === 'activity' ? 'active' : ''}" data-tab="activity">Activity</button>
                    <button class="project-dash-tab ${dashTab === 'results' ? 'active' : ''}" data-tab="results">Results</button>
                    <button class="project-dash-tab ${dashTab === 'org_chart' ? 'active' : ''}" data-tab="org_chart">Org Chart</button>
                </div>
                <div class="project-dash-content" id="project-dash-content"></div>
            </div>
        `;

        // Preview button — open project's index.html or serve files
        document.getElementById('dash-preview-btn')?.addEventListener('click', () => {
            this.send({ command: 'command_project_preview', project_id: project.id });
        });

        // Files button — request file listing
        document.getElementById('dash-files-btn')?.addEventListener('click', () => {
            this.cmdDashTab = 'results';
            this.send({ command: 'command_project_files', project_id: project.id });
        });

        // Tab switching
        main.querySelectorAll('.project-dash-tab').forEach(tab => {
            tab.addEventListener('click', () => {
                this.cmdDashTab = tab.dataset.tab;
                if (tab.dataset.tab === 'results') {
                    this.send({ command: 'command_project_files', project_id: project.id });
                }
                this.renderProjectDashboard(project);
            });
        });

        // Render active tab content
        const content = document.getElementById('project-dash-content');
        if (!content) return;

        switch (dashTab) {
            case 'chat': this._renderDashChat(content, project); break;
            case 'plan': this._renderDashPlan(content, project); break;
            case 'agents': this._renderDashAgents(content, project); break;
            case 'activity': this._renderDashActivity(content, project); break;
            case 'results': this._renderDashResults(content, project); break;
            case 'org_chart': this._renderDashOrgChart(content, project); break;
        }
    }

    _renderDashChat(el, project) {
        // Initialize chat history for this project if not exists
        if (!this._projectChatHistory) this._projectChatHistory = {};
        const history = this._projectChatHistory[project.id] || [];

        el.innerHTML = `
            <div class="project-chat">
                <div class="project-chat-messages" id="project-chat-messages">
                    ${history.length === 0 ? `
                        <div class="project-chat-welcome">
                            <div class="project-chat-welcome-icon">🤖</div>
                            <h3>Project Coordinator</h3>
                            <p>Chat with the AI coordinator for this project. You can ask it to:</p>
                            <ul>
                                <li>Break down the strategy into tasks</li>
                                <li>Spawn worker agents for specific tasks</li>
                                <li>Check on agent progress</li>
                                <li>Modify the plan or priorities</li>
                                <li>Get status updates</li>
                            </ul>
                        </div>
                    ` : history.map(m => `
                        <div class="project-chat-msg project-chat-${m.role}">
                            <div class="project-chat-msg-avatar">${m.role === 'user' ? '👤' : '🤖'}</div>
                            <div class="project-chat-msg-body">
                                <div class="project-chat-msg-content">${this.escapeHtml(m.content)}</div>
                            </div>
                        </div>
                    `).join('')}
                </div>
                <div class="project-chat-input-bar">
                    <div class="project-chat-model-row">
                        <span class="project-chat-model-label">Model:</span>
                        <select class="settings-input" id="project-chat-model" style="width:200px;font-size:12px"></select>
                    </div>
                    <div class="project-chat-input-row">
                        <textarea class="settings-input" id="project-chat-input" rows="2"
                            placeholder="Tell the coordinator what to do..."></textarea>
                        <button class="btn-primary btn-sm" id="project-chat-send">Send</button>
                    </div>
                </div>
            </div>
        `;

        // Scroll to bottom
        const messagesEl = document.getElementById('project-chat-messages');
        if (messagesEl) messagesEl.scrollTop = messagesEl.scrollHeight;

        // Populate model selector
        const chatModelSelect = document.getElementById('project-chat-model');
        if (chatModelSelect) this._populateCommandModelSelector(chatModelSelect);

        // Send handler
        const sendMessage = () => {
            const input = document.getElementById('project-chat-input');
            const content = input?.value?.trim();
            if (!content) return;

            // Add user message to history
            history.push({ role: 'user', content });
            this._projectChatHistory[project.id] = history;

            // Add user message to UI immediately
            const msgEl = document.createElement('div');
            msgEl.className = 'project-chat-msg project-chat-user';
            msgEl.innerHTML = `
                <div class="project-chat-msg-avatar">👤</div>
                <div class="project-chat-msg-body">
                    <div class="project-chat-msg-content">${this.escapeHtml(content)}</div>
                </div>
            `;

            // Remove welcome screen if present
            const welcome = messagesEl?.querySelector('.project-chat-welcome');
            if (welcome) welcome.remove();

            messagesEl?.appendChild(msgEl);

            // Add "thinking" indicator
            const thinkingEl = document.createElement('div');
            thinkingEl.className = 'project-chat-msg project-chat-assistant';
            thinkingEl.id = 'project-chat-thinking';
            thinkingEl.innerHTML = `
                <div class="project-chat-msg-avatar">🤖</div>
                <div class="project-chat-msg-body">
                    <div class="project-chat-msg-content project-chat-thinking">Thinking...</div>
                </div>
            `;
            messagesEl?.appendChild(thinkingEl);
            if (messagesEl) messagesEl.scrollTop = messagesEl.scrollHeight;

            // Send to backend
            this.send({
                command: 'command_project_chat',
                project_id: project.id,
                message: content,
            });

            input.value = '';
        };

        document.getElementById('project-chat-send')?.addEventListener('click', sendMessage);
        document.getElementById('project-chat-input')?.addEventListener('keydown', (e) => {
            if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault();
                sendMessage();
            }
        });

        // Focus input
        document.getElementById('project-chat-input')?.focus();
    }

    _handleProjectChatResponse(event) {
        const messagesEl = document.getElementById('project-chat-messages');
        if (!messagesEl) return;

        // Remove thinking indicator
        const thinking = document.getElementById('project-chat-thinking');
        if (thinking) thinking.remove();

        const content = event.response || event.text || '(no response)';

        // Add to history
        if (!this._projectChatHistory) this._projectChatHistory = {};
        const history = this._projectChatHistory[event.project_id] || [];
        history.push({ role: 'assistant', content });
        this._projectChatHistory[event.project_id] = history;

        // Add assistant message to UI
        const msgEl = document.createElement('div');
        msgEl.className = 'project-chat-msg project-chat-assistant';
        msgEl.innerHTML = `
            <div class="project-chat-msg-avatar">🤖</div>
            <div class="project-chat-msg-body">
                <div class="project-chat-msg-content">${this.escapeHtml(content)}</div>
            </div>
        `;
        messagesEl.appendChild(msgEl);
        messagesEl.scrollTop = messagesEl.scrollHeight;
    }

    _handleProjectChatDelta(event) {
        const messagesEl = document.getElementById('project-chat-messages');
        if (!messagesEl) return;

        // Find or create streaming message element
        let streamEl = document.getElementById('project-chat-streaming');
        if (!streamEl) {
            // Remove thinking indicator
            const thinking = document.getElementById('project-chat-thinking');
            if (thinking) thinking.remove();

            streamEl = document.createElement('div');
            streamEl.className = 'project-chat-msg project-chat-assistant';
            streamEl.id = 'project-chat-streaming';
            streamEl.innerHTML = `
                <div class="project-chat-msg-avatar">🤖</div>
                <div class="project-chat-msg-body">
                    <div class="project-chat-msg-content" id="project-chat-stream-text"></div>
                </div>
            `;
            messagesEl.appendChild(streamEl);
        }

        const textEl = document.getElementById('project-chat-stream-text');
        if (textEl) {
            textEl.textContent += event.text || '';
            messagesEl.scrollTop = messagesEl.scrollHeight;
        }
    }

    _renderDashPlan(el, project) {
        const tasks = project.tasks || [];
        if (tasks.length === 0) {
            const isPlanning = project.status === 'planning';
            el.innerHTML = `<div class="feature-empty" style="padding:40px">
                <span>${isPlanning ? 'Coordinator is analyzing the project and creating a plan...' : 'No tasks yet. Launch the coordinator to generate a plan.'}</span>
            </div>`;
            return;
        }

        const statusIcon = { todo: '○', running: '◉', completed: '✓', failed: '✗', assigned: '◎' };
        const statusClass = { todo: 'task-status-todo', running: 'task-status-running', completed: 'task-status-completed', failed: 'task-status-failed', assigned: 'task-status-assigned' };

        el.innerHTML = `<div class="task-list">${tasks.map((t, i) => `
            <div class="task-item">
                <span class="task-priority" style="font-size:16px;min-width:20px;text-align:center">${statusIcon[t.status] || '○'}</span>
                <div class="task-info">
                    <div class="task-title">${this.escapeHtml(t.title || t.name || `Task ${i + 1}`)}</div>
                    ${t.description ? `<div class="task-desc">${this.escapeHtml(t.description).substring(0, 120)}</div>` : ''}
                </div>
                <span class="task-status-badge ${statusClass[t.status] || ''}">${t.status || 'todo'}</span>
                ${t.agent_id ? `<span class="task-agent-id">${t.agent_id}</span>` : ''}
            </div>
        `).join('')}</div>`;
    }

    _renderDashAgents(el, project) {
        const agents = project.agents || [];
        if (agents.length === 0) {
            el.innerHTML = `<div class="feature-empty" style="padding:40px"><span>No agents spawned yet.</span></div>`;
            return;
        }

        // Group: coordinator first, then workers
        const coordinator = agents.find(a => a.role === 'coordinator');
        const workers = agents.filter(a => a.role !== 'coordinator');

        el.innerHTML = `
            <div style="display:flex;flex-direction:column;gap:8px">
                ${coordinator ? `
                    <div class="agent-card" style="border-left:3px solid var(--brand)">
                        <div class="agent-card-header">
                            <div class="agent-card-status">
                                <span class="agent-status-dot ${coordinator.status}"></span>
                                Coordinator
                            </div>
                            <span class="agent-card-id">${coordinator.id || ''}</span>
                        </div>
                        <div class="agent-card-name">${this.escapeHtml(coordinator.name || 'Project Coordinator')}</div>
                        <div class="agent-card-meta">
                            <span>${coordinator.model || ''}</span>
                            <span>${coordinator.steps || 0} steps</span>
                        </div>
                    </div>
                    ${workers.length > 0 ? '<div style="margin-left:24px;border-left:2px solid var(--border);padding-left:16px;display:flex;flex-direction:column;gap:8px">' : ''}
                ` : ''}
                ${workers.map(a => `
                    <div class="agent-card">
                        <div class="agent-card-header">
                            <div class="agent-card-status">
                                <span class="agent-status-dot ${a.status}"></span>
                                Worker
                            </div>
                            <span class="agent-card-id">${a.id || ''}</span>
                        </div>
                        <div class="agent-card-name">${this.escapeHtml(a.name || 'Worker')}</div>
                        <div class="agent-card-meta">
                            <span>${a.model || ''}</span>
                            <span>${a.steps || 0} steps</span>
                            <span>${a.elapsed ? Math.round(a.elapsed) + 's' : ''}</span>
                        </div>
                    </div>
                `).join('')}
                ${coordinator && workers.length > 0 ? '</div>' : ''}
            </div>
        `;
    }

    _renderDashActivity(el, project) {
        const activity = project.activity || this.commandFeed || [];
        if (activity.length === 0) {
            el.innerHTML = `<div class="feature-empty" style="padding:40px"><span>No activity yet.</span></div>`;
            return;
        }

        const senderIcon = (type) => type === 'user' ? '👤' : type === 'agent' ? '🤖' : 'ℹ️';

        el.innerHTML = `
            <div class="comms-feed" style="padding:0">
                ${activity.map(m => `
                    <div class="comms-message">
                        <span class="comms-icon">${senderIcon(m.sender_type)}</span>
                        <div class="comms-body">
                            <div class="comms-header">
                                <span class="comms-sender">${this.escapeHtml(m.sender_name || m.sender_id || '')}</span>
                                <span class="comms-time">${m.timestamp ? new Date(m.timestamp).toLocaleTimeString() : ''}</span>
                            </div>
                            <div class="comms-content">${this.escapeHtml(m.content || '')}</div>
                        </div>
                    </div>
                `).join('')}
            </div>
        `;
        el.scrollTop = el.scrollHeight;
    }

    _renderDashOrgChart(el, project) {
        const nodes = project.org_chart || [];

        // Request latest org chart from server
        this.send({ command: 'command_org_chart_get', project_id: project.id });

        const _renderTree = (nodeList, depth = 0) => {
            return nodeList.map(node => {
                const statusDot = { idle: '⚪', running: '🟢', completed: '🔵', failed: '🔴' }[node.status] || '⚪';
                const modelLabel = node.model ? node.model.split(':').pop() : 'no model';
                const indent = depth * 28;
                const childrenHtml = (node.children && node.children.length > 0)
                    ? _renderTree(node.children, depth + 1)
                    : '';
                return `
                    <div class="org-node" style="margin-left:${indent}px" data-id="${node.id}">
                        <div class="org-node-content">
                            <span class="org-node-connector">${depth > 0 ? '└── ' : ''}</span>
                            <span class="org-node-status">${statusDot}</span>
                            <span class="org-node-role">${this.escapeHtml(node.role)}</span>
                            <span class="org-node-model">${this.escapeHtml(modelLabel)}</span>
                            <button class="org-node-edit" data-id="${node.id}" title="Edit">&#9998;</button>
                            <button class="org-node-delete" data-id="${node.id}" title="Delete">&times;</button>
                        </div>
                        ${node.description ? `<div class="org-node-desc" style="margin-left:${indent + 48}px">${this.escapeHtml(node.description)}</div>` : ''}
                        ${childrenHtml}
                    </div>`;
            }).join('');
        };

        // Build tree from flat nodes
        const tree = this._orgChartData?.tree || this._buildOrgTree(nodes);
        const hasNodes = nodes.length > 0;

        el.innerHTML = `
            <div class="org-chart-header">
                <button class="btn-primary btn-sm" id="org-add-role-btn">+ Add Role</button>
                ${hasNodes ? `<button class="btn-sm" id="org-activate-btn" style="background:var(--ok);color:#000;border-color:var(--ok)">&#9654; Activate All</button>` : ''}
            </div>
            <div class="org-tree">
                <div class="org-node org-user-node">
                    <div class="org-node-content">
                        <span class="org-node-status">👤</span>
                        <span class="org-node-role" style="font-weight:700">You</span>
                    </div>
                </div>
                ${hasNodes ? _renderTree(tree, 1) : `
                    <div class="feature-empty" style="padding:30px">
                        <span>No roles defined yet. Click <strong>+ Add Role</strong> to build your agent hierarchy.</span>
                    </div>
                `}
            </div>
        `;

        // Bind buttons
        document.getElementById('org-add-role-btn')?.addEventListener('click', () => this._showOrgAddForm(el, project));
        document.getElementById('org-activate-btn')?.addEventListener('click', () => {
            this.send({ command: 'command_org_chart_activate', project_id: project.id });
        });
        el.querySelectorAll('.org-node-edit').forEach(btn => {
            btn.addEventListener('click', (e) => {
                e.stopPropagation();
                this._showOrgEditForm(el, project, btn.dataset.id);
            });
        });
        el.querySelectorAll('.org-node-delete').forEach(btn => {
            btn.addEventListener('click', (e) => {
                e.stopPropagation();
                this.send({ command: 'command_org_chart_remove_node', project_id: project.id, node_id: btn.dataset.id });
            });
        });
    }

    _buildOrgTree(nodes) {
        // Convert flat node list to nested tree
        const map = {};
        const roots = [];
        for (const n of nodes) { map[n.id] = { ...n, children: [] }; }
        for (const n of nodes) {
            if (n.parent_id && map[n.parent_id]) {
                map[n.parent_id].children.push(map[n.id]);
            } else {
                roots.push(map[n.id]);
            }
        }
        return roots;
    }

    _showOrgAddForm(el, project) {
        const nodes = project.org_chart || [];
        const parentOptions = nodes.map(n =>
            `<option value="${n.id}">${this.escapeHtml(n.role)}</option>`
        ).join('');

        const content = document.getElementById('project-dash-content');
        if (!content) return;

        content.innerHTML = `
            <div class="spawn-agent-form">
                <h3 style="margin:0 0 16px;font-size:16px;color:var(--text)">Add Role to Org Chart</h3>
                <div class="settings-row"><label>Role</label>
                    <input type="text" class="settings-input" id="org-role-name" placeholder="e.g., Backend Developer" /></div>
                <div class="settings-row"><label>Description</label>
                    <textarea class="settings-input" id="org-role-desc" rows="2" placeholder="What does this agent do?"></textarea></div>
                <div class="settings-row"><label>Model</label>
                    <select class="settings-input" id="org-role-model"></select></div>
                <div class="settings-row"><label>Reports to</label>
                    <select class="settings-input" id="org-role-parent">
                        <option value="">You (top level)</option>
                        ${parentOptions}
                    </select></div>
                <div style="display:flex;gap:8px;margin-top:12px">
                    <button class="btn-primary btn-sm" id="org-add-submit">Add Role</button>
                    <button class="btn-sm" id="org-add-cancel">Cancel</button>
                </div>
            </div>
        `;

        const modelSelect = document.getElementById('org-role-model');
        if (modelSelect) this._populateCommandModelSelector(modelSelect);

        document.getElementById('org-add-submit')?.addEventListener('click', () => {
            const role = document.getElementById('org-role-name')?.value?.trim();
            if (!role) return;
            this.send({
                command: 'command_org_chart_add_node',
                project_id: project.id,
                role,
                description: document.getElementById('org-role-desc')?.value || '',
                model: document.getElementById('org-role-model')?.value || '',
                parent_id: document.getElementById('org-role-parent')?.value || null,
            });
            // Switch back to org chart view
            this.cmdDashTab = 'org_chart';
            this.renderProjectDashboard(project);
        });
        document.getElementById('org-add-cancel')?.addEventListener('click', () => {
            this.cmdDashTab = 'org_chart';
            this.renderProjectDashboard(project);
        });
    }

    _showOrgEditForm(el, project, nodeId) {
        const nodes = project.org_chart || [];
        const node = nodes.find(n => n.id === nodeId);
        if (!node) return;

        const parentOptions = nodes
            .filter(n => n.id !== nodeId)
            .map(n => `<option value="${n.id}" ${n.id === node.parent_id ? 'selected' : ''}>${this.escapeHtml(n.role)}</option>`)
            .join('');

        const content = document.getElementById('project-dash-content');
        if (!content) return;

        content.innerHTML = `
            <div class="spawn-agent-form">
                <h3 style="margin:0 0 16px;font-size:16px;color:var(--text)">Edit Role: ${this.escapeHtml(node.role)}</h3>
                <div class="settings-row"><label>Role</label>
                    <input type="text" class="settings-input" id="org-edit-name" value="${this.escapeHtml(node.role)}" /></div>
                <div class="settings-row"><label>Description</label>
                    <textarea class="settings-input" id="org-edit-desc" rows="2">${this.escapeHtml(node.description || '')}</textarea></div>
                <div class="settings-row"><label>Model</label>
                    <select class="settings-input" id="org-edit-model"></select></div>
                <div class="settings-row"><label>Reports to</label>
                    <select class="settings-input" id="org-edit-parent">
                        <option value="">You (top level)</option>
                        ${parentOptions}
                    </select></div>
                <div style="display:flex;gap:8px;margin-top:12px">
                    <button class="btn-primary btn-sm" id="org-edit-submit">Save</button>
                    <button class="btn-sm" id="org-edit-cancel">Cancel</button>
                </div>
            </div>
        `;

        const modelSelect = document.getElementById('org-edit-model');
        if (modelSelect) {
            this._populateCommandModelSelector(modelSelect, node.model);
        }

        document.getElementById('org-edit-submit')?.addEventListener('click', () => {
            this.send({
                command: 'command_org_chart_update_node',
                project_id: project.id,
                node_id: nodeId,
                role: document.getElementById('org-edit-name')?.value?.trim() || node.role,
                description: document.getElementById('org-edit-desc')?.value || '',
                model: document.getElementById('org-edit-model')?.value || '',
                parent_id: document.getElementById('org-edit-parent')?.value || null,
            });
            this.cmdDashTab = 'org_chart';
            this.renderProjectDashboard(project);
        });
        document.getElementById('org-edit-cancel')?.addEventListener('click', () => {
            this.cmdDashTab = 'org_chart';
            this.renderProjectDashboard(project);
        });
    }

    _renderDashResults(el, project) {
        const files = this.cmdProjectFiles || [];
        const projectPath = project.path || '';

        el.innerHTML = `
            <div class="results-panel">
                <div class="results-header">
                    <h3 style="margin:0;font-size:15px;color:var(--text)">Generated Files</h3>
                    <div class="results-actions">
                        <button class="btn-primary btn-sm" id="results-preview-btn">▶ Preview in Browser</button>
                        <button class="btn-sm" id="results-refresh-btn">↻ Refresh</button>
                    </div>
                </div>
                <div class="results-path" style="font-size:12px;color:var(--dim);margin:8px 0">${this.escapeHtml(projectPath)}</div>
                ${files.length === 0 ? `
                    <div class="feature-empty" style="padding:30px">
                        <span>No files generated yet. Launch an initiative to start building.</span>
                    </div>
                ` : `
                    <div class="results-file-list">
                        ${files.map(f => `
                            <div class="results-file-item" data-path="${this.escapeHtml(f.path)}">
                                <span class="results-file-icon">${f.is_dir ? '📁' : this._fileIcon(f.name)}</span>
                                <span class="results-file-name">${this.escapeHtml(f.name)}</span>
                                <span class="results-file-size">${f.is_dir ? '' : this._formatSize(f.size)}</span>
                                <span class="results-file-time">${f.modified ? new Date(f.modified * 1000).toLocaleTimeString() : ''}</span>
                                ${!f.is_dir ? `<button class="btn-sm results-view-btn" data-path="${this.escapeHtml(f.path)}">View</button>` : ''}
                            </div>
                        `).join('')}
                    </div>
                `}
                <div class="results-file-content" id="results-file-content" style="display:none">
                    <div class="results-file-content-header">
                        <span id="results-viewing-file"></span>
                        <button class="btn-sm" id="results-close-file">Close</button>
                    </div>
                    <pre class="results-code" id="results-code"></pre>
                </div>
            </div>
        `;

        // Preview button
        el.querySelector('#results-preview-btn')?.addEventListener('click', () => {
            this.send({ command: 'command_project_preview', project_id: project.id });
        });

        // Refresh
        el.querySelector('#results-refresh-btn')?.addEventListener('click', () => {
            this.send({ command: 'command_project_files', project_id: project.id });
        });

        // View file content
        el.querySelectorAll('.results-view-btn').forEach(btn => {
            btn.addEventListener('click', (e) => {
                e.stopPropagation();
                this.send({ command: 'command_project_read_file', project_id: project.id, path: btn.dataset.path });
            });
        });

        // Close file view
        el.querySelector('#results-close-file')?.addEventListener('click', () => {
            document.getElementById('results-file-content').style.display = 'none';
        });
    }

    _fileIcon(name) {
        const ext = (name || '').split('.').pop()?.toLowerCase();
        const icons = { html: '🌐', css: '🎨', js: '⚡', json: '📋', md: '📝', py: '🐍', ts: '💠' };
        return icons[ext] || '📄';
    }

    _formatSize(bytes) {
        if (!bytes) return '';
        if (bytes < 1024) return bytes + ' B';
        if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
        return (bytes / (1024 * 1024)).toFixed(1) + ' MB';
    }

    switchCommandPanel(panel) {
        this.commandPanel = panel;
        document.querySelectorAll('.command-tab').forEach(tab =>
            tab.classList.toggle('active', tab.dataset.panel === panel));
        document.querySelectorAll('.command-panel').forEach(p => p.style.display = 'none');
        const target = document.getElementById(`command-${panel}`);
        if (target) target.style.display = 'flex';

        // Refresh data for the panel
        switch (panel) {
            case 'fleet': this.requestCommandFleet(); break;
            case 'tasks': this.send({ command: 'command_task_list' }); break;
            case 'monitor': this.renderCommandMonitor(); break;
            case 'comms': this.send({ command: 'command_feed_list' }); break;
        }
    }

    requestCommandFleet() {
        if (this.ws && this.ws.readyState === WebSocket.OPEN) {
            this.ws.send(JSON.stringify({ command: 'command_fleet' }));
        }
    }

    renderCommandFleet() {
        const panel = document.getElementById('command-fleet');
        if (!panel) return;

        const agents = this.commandAgents;
        if (!agents || agents.length === 0) {
            panel.innerHTML = `
                <div class="feature-empty">
                    <svg width="32" height="32" viewBox="0 0 32 32" fill="none"><circle cx="16" cy="16" r="10" stroke="currentColor" stroke-width="1.5"/><path d="M16 11v6M13 20h6" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>
                    <span>No agents running. Click <strong>+ Spawn Agent</strong> to start one.</span>
                </div>`;
            return;
        }

        panel.innerHTML = `<div class="fleet-grid">${agents.map(a => this._renderAgentCard(a)).join('')}</div>`;

        // Bind cancel buttons
        panel.querySelectorAll('.agent-cancel-btn').forEach(btn => {
            btn.addEventListener('click', (e) => {
                e.stopPropagation();
                const id = btn.dataset.id;
                this.send({ command: 'dispatch_cancel', task_id: id });
                setTimeout(() => this.requestCommandFleet(), 500);
            });
        });

        // Bind card clicks → switch to monitor panel
        panel.querySelectorAll('.agent-card').forEach(card => {
            card.addEventListener('click', () => {
                this.commandMonitorAgent = card.dataset.id;
                this.switchCommandPanel('monitor');
            });
        });
    }

    _renderAgentCard(agent) {
        const status = agent.status || 'pending';
        const statusLabel = status.charAt(0).toUpperCase() + status.slice(1);
        const name = agent.name || agent.prompt?.substring(0, 60) || 'Unnamed agent';
        const model = agent.model || '';
        const steps = agent.steps || 0;
        const elapsed = agent.elapsed ? `${Math.round(agent.elapsed)}s` : '—';
        const isRunning = status === 'running' || status === 'pending';

        return `
            <div class="agent-card" data-id="${agent.id}">
                <div class="agent-card-header">
                    <div class="agent-card-status">
                        <span class="agent-status-dot ${status}"></span>
                        ${statusLabel}
                    </div>
                    <span class="agent-card-id">${agent.id}</span>
                </div>
                <div class="agent-card-name">${this.escapeHtml(name)}</div>
                <div class="agent-card-meta">
                    <span>${this.escapeHtml(model)}</span>
                    <span>${steps} steps</span>
                    <span>${elapsed}</span>
                </div>
                ${isRunning ? `<div class="agent-card-actions"><button class="agent-cancel-btn cancel-btn" data-id="${agent.id}">Cancel</button></div>` : ''}
            </div>`;
    }

    showSpawnAgentDialog() {
        const panel = document.getElementById('command-fleet');
        if (!panel) return;

        panel.innerHTML = `
            <div class="spawn-agent-form">
                <h3 style="margin:0 0 16px;font-size:16px;color:var(--text)">Spawn New Agent</h3>
                <div class="settings-row"><label>Name</label>
                    <input type="text" class="settings-input" id="spawn-name" placeholder="Agent name (optional)" /></div>
                <div class="settings-row"><label>Prompt</label>
                    <textarea class="settings-input" id="spawn-prompt" rows="5" placeholder="What should this agent work on?"></textarea></div>
                <div class="settings-row"><label>Role</label>
                    <select class="settings-input" id="spawn-role">
                        <option value="generator">Generator</option>
                        <option value="evaluator">Evaluator</option>
                        <option value="planner">Planner</option>
                    </select></div>
                <div style="display:flex;gap:8px;margin-top:12px">
                    <button class="btn-primary btn-sm" id="spawn-submit-btn">Launch Agent</button>
                    <button class="btn-sm" id="spawn-cancel-btn">Cancel</button>
                </div>
            </div>
        `;

        document.getElementById('spawn-submit-btn')?.addEventListener('click', () => {
            const name = document.getElementById('spawn-name')?.value || '';
            const prompt = document.getElementById('spawn-prompt')?.value || '';
            const role = document.getElementById('spawn-role')?.value || 'generator';
            if (prompt.trim()) {
                this.send({ command: 'command_spawn', name, prompt, session_role: role });
                this.requestCommandFleet();
            }
        });
        document.getElementById('spawn-cancel-btn')?.addEventListener('click', () => this.renderCommandFleet());
    }

    // ── Command Center: Task Board ────────────────────────────────

    renderCommandTasks() {
        const panel = document.getElementById('command-tasks');
        if (!panel) return;

        const tasks = this.commandTasks;
        if (!tasks || tasks.length === 0) {
            panel.innerHTML = `
                <div style="display:flex;justify-content:flex-end;margin-bottom:12px">
                    <button class="btn-primary btn-sm" id="task-create-btn">+ New Task</button>
                </div>
                <div class="feature-empty">
                    <svg width="32" height="32" viewBox="0 0 32 32" fill="none"><rect x="8" y="6" width="16" height="20" rx="2" stroke="currentColor" stroke-width="1.5"/><path d="M12 12h8M12 16h8M12 20h5" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>
                    <span>No tasks yet. Create tasks and assign them to agents.</span>
                </div>`;
            panel.querySelector('#task-create-btn')?.addEventListener('click', () => this.showCommandTaskDialog());
            return;
        }

        const priorityIcon = { high: '🔴', medium: '🟡', low: '🟢' };
        const statusBadge = (s) => `<span class="task-status-badge task-status-${s}">${s}</span>`;

        panel.innerHTML = `
            <div style="display:flex;justify-content:flex-end;margin-bottom:12px">
                <button class="btn-primary btn-sm" id="task-create-btn">+ New Task</button>
            </div>
            <div class="task-list">
                ${tasks.map(t => `
                    <div class="task-item" data-id="${t.id}">
                        <span class="task-priority">${priorityIcon[t.priority] || '⚪'}</span>
                        <div class="task-info">
                            <div class="task-title">${this.escapeHtml(t.title)}</div>
                            ${t.description ? `<div class="task-desc">${this.escapeHtml(t.description).substring(0, 80)}${t.description.length > 80 ? '...' : ''}</div>` : ''}
                        </div>
                        ${statusBadge(t.status)}
                        <div class="task-actions">
                            ${t.status === 'todo' ? `<button class="btn-sm task-assign-btn" data-id="${t.id}">Assign</button>` : ''}
                            ${t.assigned_agent_id ? `<span class="task-agent-id">${t.assigned_agent_id}</span>` : ''}
                            <button class="btn-sm task-delete-btn" data-id="${t.id}" title="Delete">&times;</button>
                        </div>
                    </div>
                `).join('')}
            </div>`;

        panel.querySelector('#task-create-btn')?.addEventListener('click', () => this.showCommandTaskDialog());
        panel.querySelectorAll('.task-assign-btn').forEach(btn => {
            btn.addEventListener('click', (e) => {
                e.stopPropagation();
                this.send({ command: 'command_task_assign', task_id: btn.dataset.id });
            });
        });
        panel.querySelectorAll('.task-delete-btn').forEach(btn => {
            btn.addEventListener('click', (e) => {
                e.stopPropagation();
                this.send({ command: 'command_task_delete', id: btn.dataset.id });
            });
        });
    }

    showCommandTaskDialog() {
        const panel = document.getElementById('command-tasks');
        if (!panel) return;

        panel.innerHTML = `
            <div class="spawn-agent-form">
                <h3 style="margin:0 0 16px;font-size:16px;color:var(--text)">Create Task</h3>
                <div class="settings-row"><label>Title</label>
                    <input type="text" class="settings-input" id="task-title" placeholder="Task title" /></div>
                <div class="settings-row"><label>Description</label>
                    <textarea class="settings-input" id="task-desc" rows="4" placeholder="Detailed instructions for the agent"></textarea></div>
                <div class="settings-row"><label>Priority</label>
                    <select class="settings-input" id="task-priority">
                        <option value="high">High</option>
                        <option value="medium" selected>Medium</option>
                        <option value="low">Low</option>
                    </select></div>
                <div style="display:flex;gap:8px;margin-top:12px">
                    <button class="btn-primary btn-sm" id="task-submit-btn">Create Task</button>
                    <button class="btn-sm" id="task-cancel-btn">Cancel</button>
                </div>
            </div>
        `;

        document.getElementById('task-submit-btn')?.addEventListener('click', () => {
            const title = document.getElementById('task-title')?.value?.trim();
            if (title) {
                this.send({
                    command: 'command_task_create',
                    title,
                    description: document.getElementById('task-desc')?.value || '',
                    priority: document.getElementById('task-priority')?.value || 'medium',
                });
            }
        });
        document.getElementById('task-cancel-btn')?.addEventListener('click', () => {
            this.send({ command: 'command_task_list' });
        });
    }

    // ── Command Center: Live Monitor ────────────────────────────

    renderCommandMonitor() {
        const panel = document.getElementById('command-monitor');
        if (!panel) return;

        const agents = this.commandAgents.filter(a => a.status === 'running' || a.status === 'pending');
        const allAgents = this.commandAgents;

        if (allAgents.length === 0) {
            panel.innerHTML = `
                <div class="feature-empty">
                    <svg width="32" height="32" viewBox="0 0 32 32" fill="none"><rect x="4" y="6" width="24" height="16" rx="2" stroke="currentColor" stroke-width="1.5"/><path d="M10 26h12M16 22v4" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/><path d="M8 14l4-3 4 5 4-4 4 3" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>
                    <span>No agents to monitor. Spawn agents from the Fleet panel first.</span>
                </div>`;
            return;
        }

        const selected = this.commandMonitorAgent || (agents[0] || allAgents[0])?.id || '';

        panel.innerHTML = `
            <div class="monitor-layout">
                <div class="monitor-sidebar">
                    <div class="monitor-sidebar-title">Agents</div>
                    ${allAgents.map(a => `
                        <div class="monitor-agent-item ${a.id === selected ? 'active' : ''}" data-id="${a.id}">
                            <span class="agent-status-dot ${a.status}"></span>
                            <span class="monitor-agent-name">${this.escapeHtml(a.name || a.id)}</span>
                        </div>
                    `).join('')}
                </div>
                <div class="monitor-feed" id="monitor-feed">
                    <div class="monitor-stats" id="monitor-stats"></div>
                    <div class="monitor-events" id="monitor-events">
                        <div style="color:var(--dim);padding:20px;text-align:center">
                            ${selected ? 'Loading events...' : 'Select an agent to monitor'}
                        </div>
                    </div>
                </div>
            </div>`;

        // Bind agent selection
        panel.querySelectorAll('.monitor-agent-item').forEach(item => {
            item.addEventListener('click', () => {
                // Unsubscribe from previous
                if (this.commandMonitorAgent) {
                    this.send({ command: 'command_monitor_unsubscribe', task_id: this.commandMonitorAgent });
                }
                this.commandMonitorAgent = item.dataset.id;
                this.renderCommandMonitor();
                // Subscribe to new
                this.send({ command: 'command_monitor_subscribe', task_id: item.dataset.id });
            });
        });

        // Auto-subscribe if selected
        if (selected) {
            this.commandMonitorAgent = selected;
            this.send({ command: 'command_monitor_subscribe', task_id: selected });
        }
    }

    handleCommandAgentEvent(event) {
        const eventsEl = document.getElementById('monitor-events');
        if (!eventsEl || event.task_id !== this.commandMonitorAgent) return;

        // Clear "Loading events..." placeholder
        if (eventsEl.querySelector('[style*="text-align:center"]')) {
            eventsEl.innerHTML = '';
        }

        const eventType = event.event || '';
        let html = '';

        if (eventType === 'tool_call') {
            const name = event.name || 'tool';
            const args = event.args ? JSON.stringify(event.args).substring(0, 120) : '';
            html = `<div class="monitor-event tool-call"><span class="me-label">⚡ ${this.escapeHtml(name)}</span><span class="me-detail">${this.escapeHtml(args)}</span></div>`;
        } else if (eventType === 'tool_result') {
            const result = (event.result || '').substring(0, 150);
            html = `<div class="monitor-event tool-result"><span class="me-label">↩ result</span><span class="me-detail">${this.escapeHtml(result)}</span></div>`;
        } else if (eventType === 'text.done') {
            const text = (event.text || '').substring(0, 200);
            html = `<div class="monitor-event text-done"><span class="me-label">💬</span><span class="me-detail">${this.escapeHtml(text)}</span></div>`;
        } else if (eventType === 'step.start') {
            html = `<div class="monitor-event step-marker">── Step ${event.step || ''} ──</div>`;
        } else if (eventType === 'step.end') {
            html = `<div class="monitor-event step-marker">── Step complete ──</div>`;
        } else if (eventType === 'error') {
            html = `<div class="monitor-event error-event">✗ ${this.escapeHtml(event.message || 'Error')}</div>`;
        }

        if (html) {
            eventsEl.insertAdjacentHTML('beforeend', html);
            eventsEl.scrollTop = eventsEl.scrollHeight;
        }
    }

    handleCommandAgentHistory(event) {
        const eventsEl = document.getElementById('monitor-events');
        const statsEl = document.getElementById('monitor-stats');
        if (!eventsEl || event.task_id !== this.commandMonitorAgent) return;

        // Update stats
        if (statsEl) {
            statsEl.innerHTML = `
                <span>Status: <strong>${event.status || 'unknown'}</strong></span>
                <span>Steps: <strong>${event.steps || 0}</strong></span>
                <span>Elapsed: <strong>${event.elapsed || 0}s</strong></span>
            `;
        }

        // Replay recent events
        eventsEl.innerHTML = '';
        const events = event.events || [];
        // Only show last 50 events for performance
        const recent = events.slice(-50);
        for (const evt of recent) {
            this.handleCommandAgentEvent({ ...evt, task_id: event.task_id });
        }
    }

    // ── Command Center: Comms Feed ──────────────────────────────

    renderCommandComms() {
        const panel = document.getElementById('command-comms');
        if (!panel) return;

        const messages = this.commandFeed;

        const senderIcon = (type) => {
            if (type === 'user') return '👤';
            if (type === 'agent') return '🤖';
            return 'ℹ️';
        };

        panel.innerHTML = `
            <div class="comms-feed" id="comms-feed">
                ${messages.length === 0 ? '<div style="color:var(--dim);text-align:center;padding:40px">No messages yet. Send a broadcast to your agents.</div>' : ''}
                ${messages.map(m => `
                    <div class="comms-message comms-${m.sender_type}">
                        <span class="comms-icon">${senderIcon(m.sender_type)}</span>
                        <div class="comms-body">
                            <div class="comms-header">
                                <span class="comms-sender">${this.escapeHtml(m.sender_name || m.sender_id)}</span>
                                <span class="comms-time">${new Date(m.timestamp).toLocaleTimeString()}</span>
                                ${m.target !== 'all' ? `<span class="comms-target">→ ${m.target}</span>` : ''}
                            </div>
                            <div class="comms-content">${this.escapeHtml(m.content)}</div>
                        </div>
                    </div>
                `).join('')}
            </div>
            <div class="comms-input-bar">
                <textarea class="settings-input" id="comms-input" rows="2" placeholder="Broadcast a message to all agents..."></textarea>
                <button class="btn-primary btn-sm" id="comms-send-btn">Send</button>
            </div>`;

        // Scroll feed to bottom
        const feed = document.getElementById('comms-feed');
        if (feed) feed.scrollTop = feed.scrollHeight;

        // Send button
        document.getElementById('comms-send-btn')?.addEventListener('click', () => {
            const input = document.getElementById('comms-input');
            const content = input?.value?.trim();
            if (content) {
                this.send({ command: 'command_feed_post', content, target: 'all' });
                input.value = '';
            }
        });

        // Enter to send (Shift+Enter for newline)
        document.getElementById('comms-input')?.addEventListener('keydown', (e) => {
            if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault();
                document.getElementById('comms-send-btn')?.click();
            }
        });
    }

    // ── Context Compression ─────────────────────────────────────

    handleCompression(event) {
        const el = document.createElement('div');
        el.className = 'compression-banner';
        el.innerHTML = `
            <svg width="14" height="14" viewBox="0 0 14 14" fill="none"><path d="M3 4h8M5 7h4M3 10h8" stroke="currentColor" stroke-width="1.2" stroke-linecap="round"/></svg>
            <span>Context compressed: ${event.old_entries} → ${event.new_entries} entries (~${Math.round((event.old_tokens - event.new_tokens)/1000)}k tokens saved)</span>
        `;
        this.chatMessages.appendChild(el);
        this.scrollToBottom();
    }

    // ── Dispatch (Background Tasks) ─────────────────────────

    requestDispatchList() {
        if (this.ws && this.ws.readyState === WebSocket.OPEN) {
            this.ws.send(JSON.stringify({ command: 'dispatch_list' }));
        }
    }

    submitDispatch(name, prompt) {
        if (this.ws && this.ws.readyState === WebSocket.OPEN) {
            this.ws.send(JSON.stringify({ command: 'dispatch', name, prompt }));
        }
    }

    renderDispatchList(tasks) {
        const body = document.getElementById('dispatch-body');
        if (!body) return;

        if (tasks.length === 0) {
            body.innerHTML = `
                <div class="feature-empty">
                    <svg width="32" height="32" viewBox="0 0 32 32" fill="none"><path d="M16 4v16M10 14l6 6 6-6" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/><path d="M4 22v4h24v-4" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>
                    <span>No dispatched tasks yet.</span>
                </div>`;
            return;
        }

        body.innerHTML = tasks.map(t => {
            const statusClass = t.status === 'completed' ? 'ok' : t.status === 'failed' ? 'err' : t.status === 'running' ? 'brand' : 'muted';
            const statusIcon = t.status === 'completed' ? '✓' : t.status === 'failed' ? '✗' : t.status === 'running' ? '⟳' : '○';
            return `<div class="dispatch-item" data-task-id="${t.id}">
                <div class="dispatch-item-header">
                    <span class="dispatch-status" style="color:var(--${statusClass})">${statusIcon}</span>
                    <span class="dispatch-name">${t.name}</span>
                    <span class="dispatch-meta">${t.model || ''} · ${t.steps} steps · ${t.elapsed}s</span>
                </div>
                <div class="dispatch-prompt">${t.prompt.substring(0, 100)}${t.prompt.length > 100 ? '...' : ''}</div>
                ${t.error ? `<div class="dispatch-error">${t.error}</div>` : ''}
            </div>`;
        }).join('');

        body.querySelectorAll('.dispatch-item').forEach(el => {
            el.addEventListener('click', () => {
                const taskId = el.dataset.taskId;
                this.ws.send(JSON.stringify({ command: 'dispatch_result', task_id: taskId }));
            });
        });
    }

    renderDispatchResult(task) {
        const body = document.getElementById('dispatch-body');
        if (!body || !task) return;

        const resultHtml = task.result ? this.renderMarkdown(task.result) : '<em>No output</em>';
        body.innerHTML = `
            <div class="dispatch-result-view">
                <button class="btn-sm dispatch-back-btn">&larr; Back to list</button>
                <h3>${task.name}</h3>
                <div class="dispatch-result-meta">${task.status} · ${task.model} · ${task.steps} steps · ${task.elapsed}s</div>
                <div class="dispatch-result-content markdown-body">${resultHtml}</div>
            </div>
        `;
        body.querySelector('.dispatch-back-btn')?.addEventListener('click', () => this.requestDispatchList());
    }

    showDispatchDialog() {
        const body = document.getElementById('dispatch-body');
        if (!body) return;

        body.innerHTML = `
            <div class="dispatch-form">
                <div class="settings-row"><label>Name</label>
                    <input type="text" class="settings-input" id="dispatch-name" placeholder="Task name (optional)" /></div>
                <div class="settings-row"><label>Prompt</label>
                    <textarea class="settings-input" id="dispatch-prompt" rows="4" placeholder="What should the agent do?"></textarea></div>
                <div style="display:flex;gap:8px;margin-top:12px">
                    <button class="btn-primary btn-sm" id="dispatch-submit-btn">Submit</button>
                    <button class="btn-sm" id="dispatch-cancel-btn">Cancel</button>
                </div>
            </div>
        `;

        document.getElementById('dispatch-submit-btn')?.addEventListener('click', () => {
            const name = document.getElementById('dispatch-name')?.value || '';
            const prompt = document.getElementById('dispatch-prompt')?.value || '';
            if (prompt.trim()) {
                this.submitDispatch(name, prompt);
                this.requestDispatchList();
            }
        });
        document.getElementById('dispatch-cancel-btn')?.addEventListener('click', () => this.requestDispatchList());
    }

    // ── Scheduled Tasks ─────────────────────────────────────

    requestScheduleList() {
        if (this.ws && this.ws.readyState === WebSocket.OPEN) {
            this.ws.send(JSON.stringify({ command: 'schedule_list' }));
        }
    }

    renderScheduleList(schedules) {
        const body = document.getElementById('schedule-body');
        if (!body) return;

        if (schedules.length === 0) {
            body.innerHTML = `
                <div class="feature-empty">
                    <svg width="32" height="32" viewBox="0 0 32 32" fill="none"><circle cx="16" cy="16" r="12" stroke="currentColor" stroke-width="1.5"/><path d="M16 8v9l6 3" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>
                    <span>No scheduled tasks yet.</span>
                </div>`;
            return;
        }

        body.innerHTML = schedules.map(s => {
            const statusDot = s.enabled ? '<span style="color:var(--ok)">●</span>' : '<span style="color:var(--muted)">○</span>';
            const taskKind = s.task_kind === 'harness_cycle' ? 'harness cycle' : 'session';
            const metaSuffix = s.task_kind === 'harness_cycle'
                ? `${taskKind} · loops:${s.max_loops || 6}`
                : `${taskKind}`;
            return `<div class="schedule-item" data-schedule-id="${s.id}">
                <div class="dispatch-item-header">
                    ${statusDot}
                    <span class="dispatch-name">${s.name}</span>
                    <span class="dispatch-meta">${s.schedule} · ${s.run_count} runs · ${metaSuffix}</span>
                    <label class="toggle-switch">
                        <input type="checkbox" ${s.enabled ? 'checked' : ''} data-id="${s.id}" class="schedule-toggle" />
                        <span class="toggle-slider"></span>
                    </label>
                </div>
                <div class="dispatch-prompt">${s.prompt.substring(0, 100)}${s.prompt.length > 100 ? '...' : ''}</div>
                ${s.next_run ? `<div class="schedule-next">Next: ${new Date(s.next_run).toLocaleTimeString()}</div>` : ''}
            </div>`;
        }).join('');

        body.querySelectorAll('.schedule-toggle').forEach(toggle => {
            toggle.addEventListener('change', (e) => {
                const id = e.target.dataset.id;
                this.ws.send(JSON.stringify({ command: 'schedule_update', task_id: id, enabled: e.target.checked }));
            });
        });
    }

    showScheduleDialog() {
        const body = document.getElementById('schedule-body');
        if (!body) return;

        body.innerHTML = `
            <div class="dispatch-form">
                <div class="settings-row"><label>Name</label>
                    <input type="text" class="settings-input" id="schedule-name" placeholder="Task name" /></div>
                <div class="settings-row"><label>Type</label>
                    <select class="settings-input" id="schedule-kind">
                        <option value="session">Prompt Session</option>
                        <option value="harness_cycle">Harness Cycle</option>
                    </select></div>
                <div class="settings-row"><label>Prompt</label>
                    <textarea class="settings-input" id="schedule-prompt" rows="4" placeholder="Prompt or top-level objective"></textarea></div>
                <div class="settings-row"><label>Schedule</label>
                    <input type="text" class="settings-input" id="schedule-interval" placeholder="every:5m, every:1h, every:30s" /></div>
                <div class="settings-row" id="schedule-max-loops-row"><label>Max Loops</label>
                    <input type="number" min="1" class="settings-input" id="schedule-max-loops" value="6" /></div>
                <div style="display:flex;gap:8px;margin-top:12px">
                    <button class="btn-primary btn-sm" id="schedule-submit-btn">Create</button>
                    <button class="btn-sm" id="schedule-cancel-btn">Cancel</button>
                </div>
            </div>
        `;

        const kindSelect = document.getElementById('schedule-kind');
        const maxLoopsRow = document.getElementById('schedule-max-loops-row');
        const updateVisibility = () => {
            if (!maxLoopsRow || !kindSelect) return;
            maxLoopsRow.style.display = kindSelect.value === 'harness_cycle' ? 'flex' : 'none';
        };
        kindSelect?.addEventListener('change', updateVisibility);
        updateVisibility();

        document.getElementById('schedule-submit-btn')?.addEventListener('click', () => {
            const name = document.getElementById('schedule-name')?.value || '';
            const taskKind = document.getElementById('schedule-kind')?.value || 'session';
            const prompt = document.getElementById('schedule-prompt')?.value || '';
            const schedule = document.getElementById('schedule-interval')?.value || '';
            const maxLoops = Number.parseInt(document.getElementById('schedule-max-loops')?.value || '6', 10) || 6;
            if (prompt.trim() && schedule.trim()) {
                this.ws.send(JSON.stringify({
                    command: 'schedule_create',
                    name,
                    prompt,
                    schedule,
                    task_kind: taskKind,
                    max_loops: maxLoops,
                }));
            }
        });
        document.getElementById('schedule-cancel-btn')?.addEventListener('click', () => this.requestScheduleList());
    }
}


// ═══════════════════════════════════════════════════════════════════
//  Initialize
// ═══════════════════════════════════════════════════════════════════

document.addEventListener('DOMContentLoaded', () => {
    window.app = new ResonantApp();
});
