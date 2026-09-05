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

function shouldGroupAsEvidence(name, args = {}) {
    if (COLLAPSIBLE_TOOLS.has(name)) return true;
    if (name !== 'bash') return false;
    const command = String(args.command || '').trim();
    return /^(?:(?:python\s+-m\s+)?pytest\b|(?:python\s+-m\s+)?ruff\b|(?:npm|pnpm|yarn)\s+(?:(?:run\s+)?(?:test|lint|build|check))\b|cargo\s+(?:test|check|clippy)\b|go\s+test\b|git\s+(?:status|diff|log|show)\b)/i.test(command);
}

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

// Events whose handling is a single delegation. A table rather than 45 more
// switch arms: "what handles X" becomes a lookup instead of a scroll through a
// 750-line function, and an event with no handler is now a findable gap rather
// than a silent fall-through.
//
// Cases with inline logic deliberately stay in the switch below — hoisting
// those mechanically would produce a hundred badly-named methods.
const RESONANT_EVENT_DELEGATES = {
    'autonomous_heartbeat': 'handleAutonomousHeartbeat',
    'autonomous_human_decision_received': 'handleAutonomousHumanDecisionReceived',
    'autonomous_human_decision_required': 'handleAutonomousHumanDecisionRequired',
    'autonomous_iteration_complete': 'handleAutonomousIterationComplete',
    'autonomous_iteration_started': 'handleAutonomousIterationStarted',
    'autonomous_iteration_timeout': 'handleAutonomousIterationTimeout',
    'autonomous_mission_started': 'handleAutonomousMissionStarted',
    'autonomous_reflection': 'handleAutonomousReflection',
    'autonomous_resume_recovery': 'handleAutonomousResumeRecovery',
    'backend.status': 'handleBackendStatus',
    'cancel.completed': 'handleCancelCompleted',
    'cancel.requested': 'handleCancelRequested',
    'choices': 'handleChoices',
    'context.compression': 'handleCompression',
    'dir_list': 'handleDirList',
    'git_result': 'handleGitResult',
    'init': 'handleInit',
    'message.queue_cleared': 'handleMessageQueueCleared',
    'message.queued': 'handleMessageQueued',
    'message.remove_failed': 'handleMessageRemoveFailed',
    'message.removed': 'handleMessageRemoved',
    'message.started': 'handleMessageStarted',
    'mission.spec_ready': 'handleMissionSpecReady',
    'mission_phase_changed': 'handleMissionPhaseChanged',
    'project_files': 'handleProjectFiles',
    'rag_results': 'handleRagResults',
    'session.end': 'handleSessionEnd',
    'session_history_page': 'handleSessionHistoryPage',
    'session.start': 'handleSessionStart',
    'shell_exec_result': 'handleShellExecResult',
    'skill_view_data': 'handleSkillViewData',
    'status': 'handleStatus',
    'status.update_queued': 'handleStatusUpdateQueued',
    'status.update_rejected': 'handleStatusUpdateRejected',
    'steer.applied': 'handleSteerApplied',
    'step.end': 'handleStepEnd',
    'step.start': 'handleStepStart',
    'subagent.end': 'handleSubagentEnd',
    'subagent.start': 'handleSubagentStart',
    'text.delta': 'handleTextDelta',
    'text.done': 'handleTextDone',
    'todos.updated': 'handleTodosUpdated',
    'tool.call': 'handleToolCall',
    'tool.result': 'handleToolResult',
    'tool_permission': 'handleToolPermission',
    'user_input_received': 'handleUserInputReceived',
};


class ResonantApp {
    constructor() {
        this.ws = null;
        this.reconnectAttempts = 0;
        this.isRunning = false;
        // Selection is not activity: the selected session can be idle,
        // working, or blocked on the user. Keep transient live state here.
        this._sessionActivity = new Map();
        this._queuedMessages = new Map();
        this._cancelInFlight = null;
        this._cancelInterrupted = false;
        this._cancelWatchdog = null;
        this._cancelCardBaseline = null;
        this._newSessionInflight = false;
        this._newSessionRequestId = '';
        this._newSessionInflightTimer = null;
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
        this._activeTask = null;
        this._liveRun = null;
        this._liveRunTimer = null;
        this.activeTaskCard = null;
        this.activeTaskActivityEl = null;
        this.activeTaskResultEl = null;
        this.activeTaskFooterEl = null;
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
        // Parallel workers emit interleaved events. Keep a render lane per
        // worker instead of relying on one global "current" subagent.
        this.subagentContainers = new Map();
        this.subagentStreams = new Map();
        this._activeRenderEvent = null;
        this.agentActivities = new Map();
        // Runtime bookkeeping still powers compact in-chat task summaries.
        // It is intentionally not exposed as a separate agent hierarchy.
        this.runtimeView = 'agents';
        this.runtimeAgents = [];
        this.runtimeTimeline = [];
        this.runtimeTraces = [];
        this.runtimeArtifacts = [];
        this.runtimePacks = [];
        this.agentActivityOrder = [];
        this.agentActivityStack = [];
        this.contextState = null;
        this.contextProviders = [];

        // Preview panel state
        this.previewOpen = false;
        this.previewImages = []; // {src, toolName, timestamp}
        this._currentPreviewPane = '';  // C2 — drives the plan-tab unread indicator
        this._previewResizing = false;

        // Session management state
        this.sessions = [];
        this.allSessions = [];
        this.currentSessionId = '';
        this._historyPage = null;
        this._loadedHistoryEvents = [];
        this._historyLoading = false;
        this._historyWindowDroppedTail = false;
        this.recentProjects = [];
        this.playgroundProject = null;
        this._projectRailOrder = [];
        this._projectSwitchSequence = 0;
        this._latestProjectSwitchId = '';
        this._pendingProjectSwitchId = '';
        this._pendingProjectPath = '';
        this._projectSwitchTimer = null;
        this.harnessState = null;
        this.harnessCycles = [];
        this.harnessCyclePoller = null;

        // View state
        this.currentView = 'agents';
        this.settings = {};
        this.costData = null;
        this.promptInspector = null;
        this.evaluationDashboard = null;
        this.iterationCheckpoints = [];
        this.checkpointComparison = null;

        // Per-turn agent run summary (Cursor-style card on session.end)
        this._agentRunSummary = { title: '', fileChanges: [], todos: null };
        this._autoFallbackDepth = 0;
        this._liveAgentTodoEl = null;

        // Git state
        this.gitData = null;
        this.gitPopoverOpen = false;
        this.harnessPopoverOpen = false;

        // RESONANT.md state
        this.resonantMd = null;

        // Runtime status popover state
        this.systemStatus = 'disconnected';
        this.systemStatusLabel = 'Disconnected';
        this.statusPopoverOpen = false;
        this.statusPopoverTab = 'servers';
        this.mcpServers = [];
        this.mcpHealth = {};
        this.lspItems = [];
        this.resonantPlugins = [];
        this.pluginSummary = {};

        // DOM refs
        this.chatMessages = document.getElementById('chat-messages');
        this.chatContainer = document.getElementById('chat-container');
        this.welcomeScreen = document.getElementById('welcome-screen');
        this.inputBar = document.getElementById('input-bar');
        this.liveRunSurface = document.getElementById('live-run-surface');
        this.userInput = document.getElementById('user-input');
        this.sendBtn = document.getElementById('send-btn');
        this.stopBtn = document.getElementById('stop-btn');
        this.composerQueue = document.getElementById('composer-queue');
        this.modelSelector = document.getElementById('model-selector');
        this.thinkingModeSelector = document.getElementById('thinking-mode-selector');
        this.headerStatus = document.getElementById('header-status');
        this.headerProject = document.getElementById('header-project');
        this.statusPopoverTrigger = document.getElementById('status-popover-trigger');
        this.statusPopover = document.getElementById('status-popover');
        this.statusPopoverBody = document.getElementById('status-popover-body');
        this.railProjects = document.getElementById('rail-projects');
        // Sidebar project switcher pill — opens the same dropdown the titlebar used to
        // and additionally filters the sidebar session tree to the selected project.
        this.sidebarProjectSwitch = document.getElementById('sidebar-project-switch');
        this.sidebarProjectSwitchLabel = document.getElementById('sidebar-project-switch-label');
        this.sidebarProjectSwitch?.addEventListener('click', (ev) => {
            ev.stopPropagation();
            this._openProjectSwitcher(this.sidebarProjectSwitch);
        });
        // v0.6.7 — composer-footer folder chip opens the same project
        // switcher (switch project / open another folder / recent).
        const _footerProjectBtn = document.getElementById('footer-project-btn');
        _footerProjectBtn?.addEventListener('click', (ev) => {
            ev.stopPropagation();
            this._openProjectSwitcher(_footerProjectBtn);
        });
        // Project-card "..." (Project actions) — same switcher menu.
        const _projectMenuBtn = document.querySelector('.sidebar-project-menu');
        _projectMenuBtn?.addEventListener('click', (ev) => {
            ev.stopPropagation();
            this._openProjectSwitcher(_projectMenuBtn);
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

        // Configure marked.
        //
        // The `highlight` callback that used to live here was dead: marked
        // removed that option in v5, and the page loaded an unpinned `latest`
        // build, so it had been silently ignored for a long time. Syntax
        // highlighting is done by hljs.highlightElement over the rendered
        // `pre code` blocks once streaming settles — see renderMarkdown. It
        // is kept out of the parse step deliberately, because re-highlighting
        // every block on every throttled re-parse is expensive on long
        // responses.
        if (typeof marked !== 'undefined') {
            marked.setOptions({ gfm: true, breaks: true });
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
            this._setSystemStatus('connected', 'Connected');
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
                this._setSystemStatus('warning', 'Reconnecting...');
            } else {
                this._setSystemStatus('disconnected', 'Disconnected');
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
            this._setSystemStatus('disconnected', 'Disconnected');
        }
    }

    send(data) {
        if (this.ws && this.ws.readyState === WebSocket.OPEN) {
            this.ws.send(JSON.stringify(data));
        }
    }

    _setSystemStatus(state, label) {
        this.systemStatus = state || 'connected';
        this.systemStatusLabel = label || 'Connected';
        if (this.headerStatus) {
            this.headerStatus.textContent = '';
            this.headerStatus.classList.remove('is-connected', 'is-warning', 'is-error', 'is-disconnected');
            const mapped = this.systemStatus === 'connected'
                ? 'is-connected'
                : this.systemStatus === 'warning'
                    ? 'is-warning'
                    : this.systemStatus === 'error'
                        ? 'is-error'
                        : 'is-disconnected';
            this.headerStatus.classList.add(mapped);
        }
        if (this.statusPopoverTrigger) {
            this.statusPopoverTrigger.title = this.systemStatusLabel;
            this.statusPopoverTrigger.setAttribute('aria-label', `Runtime status: ${this.systemStatusLabel}`);
        }
        this._renderStatusPopover();
    }

    requestMcpList() {
        if (!this.ws || this.ws.readyState !== WebSocket.OPEN) return;
        this.ws.send(JSON.stringify({ command: 'mcp_list' }));
    }

    requestLspList() {
        if (!this.ws || this.ws.readyState !== WebSocket.OPEN) return;
        this.ws.send(JSON.stringify({ command: 'lsp_list' }));
    }

    requestPluginList() {
        if (!this.ws || this.ws.readyState !== WebSocket.OPEN) return;
        this.ws.send(JSON.stringify({ command: 'plugin_list' }));
    }

    _statusPill(status) {
        const normalized = String(status || '').toLowerCase();
        const klass = normalized.includes('error') || normalized.includes('fail') || normalized.includes('unreachable') || normalized === 'offline'
            ? 'bad'
            : normalized.includes('warn') || normalized.includes('pending') || normalized.includes('idle') || normalized.includes('attention') || normalized.includes('missing')
                ? 'warn'
                : normalized.includes('connected') || normalized.includes('ready') || normalized.includes('ok') || normalized.includes('available')
                    ? 'ok'
                    : 'muted';
        return `<span class="status-pill ${klass}">${this.escapeHtml(status || 'unknown')}</span>`;
    }

    _statusRow({ dot = 'muted', title = '', detail = '', meta = '', action = '' }) {
        return `
            <div class="status-row">
                <span class="status-row-dot ${dot}" aria-hidden="true"></span>
                <span class="status-row-main">
                    <span class="status-row-title">${this.escapeHtml(title)}</span>
                    ${detail ? `<span class="status-row-detail">${this.escapeHtml(detail)}</span>` : ''}
                </span>
                ${meta ? `<span class="status-row-meta">${meta}</span>` : ''}
                ${action}
            </div>`;
    }

    _renderStatusServers() {
        const backends = this.backends || {};
        const backendLabels = this._getBackendLabels();
        const rows = [];
        for (const [key, info] of Object.entries(backends)) {
            const models = Array.isArray(info?.models) ? info.models : [];
            const active = key === this.currentBackendName;
            const detail = info?.url || (key === 'ollama' ? 'Ollama endpoint' : 'Model server');
            const providerLabel = backendLabels[key] || key;
            const label = active && this.currentModelName ? `${providerLabel} / ${this.currentModelName}` : providerLabel;
            rows.push(this._statusRow({
                dot: active ? 'ok' : 'muted',
                title: label,
                detail,
                meta: this._statusPill(`${models.length} models`),
            }));
        }
        if (!rows.length) {
            rows.push(this._statusRow({
                dot: 'bad',
                title: 'Ollama',
                detail: this.settings?.network?.ollama_url || 'No reachable model server',
                meta: this._statusPill('offline'),
            }));
        }
        return rows.join('');
    }

    _renderStatusMcp() {
        const servers = Array.isArray(this.mcpServers) ? this.mcpServers : [];
        if (!servers.length) {
            return this._statusRow({
                dot: 'muted',
                title: 'No MCP servers configured',
                detail: 'Add servers in Settings when MCP tools are needed.',
                meta: this._statusPill('empty'),
            });
        }
        return servers.map((server) => this._statusRow({
            dot: server.connected ? 'ok' : server.error ? 'bad' : server.enabled === false ? 'muted' : 'warn',
            title: server.name || 'MCP server',
            detail: server.error || server.endpoint || server.url || server.command || '',
            meta: this._statusPill(server.connected ? `${server.tools || 0} tools` : (server.error ? 'error' : server.enabled === false ? 'disabled' : 'disconnected')),
            action: !server.connected && server.enabled !== false
                ? `<button class="status-row-action" type="button" data-status-mcp="${this.escapeHtml(server.name || '')}">Connect</button>`
                : '',
        })).join('');
    }

    _renderStatusLsp() {
        const items = Array.isArray(this.lspItems) ? this.lspItems : [];
        if (!items.length) {
            return this._statusRow({
                dot: 'muted',
                title: 'No LSP servers detected',
                detail: 'Configured servers and installed language servers will appear here.',
                meta: this._statusPill('empty'),
            });
        }
        return items.map((item) => this._statusRow({
            dot: item.status === 'available' || item.status === 'connected' ? 'ok' : item.status === 'disabled' ? 'muted' : 'warn',
            title: item.name || item.id || 'Language server',
            detail: item.detail || item.command || (Array.isArray(item.languages) ? item.languages.join(', ') : ''),
            meta: this._statusPill(item.status || 'unknown'),
        })).join('');
    }

    _renderStatusPlugins() {
        const plugins = Array.isArray(this.resonantPlugins) ? this.resonantPlugins : [];
        if (!plugins.length) {
            return this._statusRow({
                dot: 'muted',
                title: 'No Resonant plugins installed',
                detail: 'Plugin packages will appear here when they are enabled.',
                meta: this._statusPill('empty'),
            });
        }
        return plugins.slice(0, 12).map((plugin) => {
            const status = String(plugin.status || (plugin.enabled === false ? 'disabled' : 'available')).toLowerCase();
            const ok = ['available', 'enabled', 'connected', 'ready', 'ok'].includes(status);
            const disabled = status === 'disabled' || plugin.enabled === false;
            const detail = plugin.description || plugin.detail || plugin.path || plugin.source || '';
            const meta = plugin.version ? `${status} ${plugin.version}` : status;
            return this._statusRow({
                dot: ok ? 'ok' : disabled ? 'muted' : 'warn',
                title: plugin.name || plugin.id || 'Resonant plugin',
                detail,
                meta: this._statusPill(meta),
            });
        }).join('') + (plugins.length > 12
            ? `<div class="status-popover-more">${plugins.length - 12} more plugins configured</div>`
            : '');
    }

    _renderStatusSkills() {
        const skills = Array.isArray(this.skills) ? this.skills : [];
        if (!skills.length) {
            return this._statusRow({
                dot: 'muted',
                title: 'No skills installed',
                detail: 'Reusable skills will appear here when available.',
                meta: this._statusPill('empty'),
            });
        }
        const rows = skills.slice(0, 18).map((skill) => {
            const id = skill.id || '';
            const title = skill.name || id || 'Skill';
            const desc = (skill.description || '').slice(0, 96);
            const meta = skill.pinned ? 'pinned' : (skill.scope || 'skill');
            const detail = [skill.scope, skill.created_by, desc].filter(Boolean).join(' · ');
            return `
                <button class="status-row status-skill-row" type="button" data-status-skill-id="${this.escapeHtml(id)}">
                    <span class="status-row-dot ${skill.pinned ? 'ok' : 'muted'}" aria-hidden="true"></span>
                    <span class="status-row-main">
                        <span class="status-row-title">${this.escapeHtml(title)}</span>
                        ${detail ? `<span class="status-row-detail">${this.escapeHtml(detail)}</span>` : ''}
                    </span>
                    <span class="status-row-meta">${this._statusPill(meta)}</span>
                </button>`;
        }).join('');
        return rows + (skills.length > 18
            ? `<div class="status-popover-more">${skills.length - 18} more skills configured</div>`
            : '');
    }

    _bindWindowResizeHandles() {
        if (this._resizeHandlesBound) return;
        const handles = Array.from(document.querySelectorAll('[data-resize-edge]'));
        if (!handles.length) return;
        const hasResizeApi = () => typeof pywebview !== 'undefined'
            && pywebview.api
            && typeof pywebview.api.resize_window === 'function';
        if (!hasResizeApi()) {
            // Register the ready-listener ONCE (desktop mode injects
            // pywebview late), and cap the poll — in plain-browser mode
            // pywebview never appears, and the old retry loop leaked a
            // fresh once-listener + timer every second forever.
            if (!this._resizeApiWaitBound) {
                this._resizeApiWaitBound = true;
                window.addEventListener('pywebviewready', () => this._bindWindowResizeHandles(), { once: true });
            }
            this._resizeApiPolls = (this._resizeApiPolls || 0) + 1;
            if (this._resizeApiPolls <= 15) {
                setTimeout(() => this._bindWindowResizeHandles(), 1000);
            }
            return;
        }
        this._resizeHandlesBound = true;
        document.body.classList.add('native-resize-ready');

        handles.forEach((handle) => {
            handle.addEventListener('mousedown', (event) => {
                if (!hasResizeApi()) return;
                event.preventDefault();
                const edge = handle.dataset.resizeEdge || 'bottom-right';
                const startX = event.screenX;
                const startY = event.screenY;
                const startW = window.outerWidth || window.innerWidth;
                const startH = window.outerHeight || window.innerHeight;
                const startWindowX = window.screenX;
                const startWindowY = window.screenY;
                const minW = 800;
                const minH = 600;
                let queued = false;
                let nextW = startW;
                let nextH = startH;
                let nextX = startWindowX;
                let nextY = startWindowY;

                const flush = () => {
                    queued = false;
                    if (edge.includes('left') || edge.includes('top')) {
                        pywebview.api.resize_window(
                            Math.round(nextW),
                            Math.round(nextH),
                            Math.round(nextX),
                            Math.round(nextY),
                        );
                    } else {
                        pywebview.api.resize_window(Math.round(nextW), Math.round(nextH));
                    }
                };
                const onMove = (moveEvent) => {
                    const dx = moveEvent.screenX - startX;
                    const dy = moveEvent.screenY - startY;
                    if (edge.includes('right')) {
                        nextW = Math.max(minW, startW + dx);
                    }
                    if (edge.includes('bottom')) {
                        nextH = Math.max(minH, startH + dy);
                    }
                    if (edge.includes('left')) {
                        nextW = Math.max(minW, startW - dx);
                        nextX = startWindowX + Math.min(dx, startW - minW);
                    }
                    if (edge.includes('top')) {
                        nextH = Math.max(minH, startH - dy);
                        nextY = startWindowY + Math.min(dy, startH - minH);
                    }
                    if (!queued) {
                        queued = true;
                        requestAnimationFrame(flush);
                    }
                };
                const onUp = () => {
                    document.removeEventListener('mousemove', onMove);
                    document.removeEventListener('mouseup', onUp);
                    document.body.classList.remove('is-window-resizing');
                };
                document.body.classList.add('is-window-resizing');
                document.addEventListener('mousemove', onMove);
                document.addEventListener('mouseup', onUp);
            });
        });
    }

    _bindComposerGutterSync() {
        if (!this.inputBar || this._composerGutterBound) return;
        this._composerGutterBound = true;

        const sync = () => {
            const visible = this.inputBar.style.display !== 'none';
            const height = visible ? Math.ceil(this.inputBar.getBoundingClientRect().height || 0) : 0;
            const gutter = Math.max(118, height + 34);
            document.documentElement.style.setProperty('--composer-gutter', `${gutter}px`);
        };

        this._syncComposerGutter = sync;
        sync();
        if (typeof ResizeObserver !== 'undefined') {
            this._composerGutterObserver = new ResizeObserver(sync);
            this._composerGutterObserver.observe(this.inputBar);
        }
        window.addEventListener('resize', sync);
    }

    _bindWindowDragFallback() {
        if (this._dragFallbackBound) return;
        const titlebar = document.getElementById('app-titlebar');
        if (!titlebar) return;
        const hasMoveApi = () => typeof pywebview !== 'undefined'
            && pywebview.api
            && typeof pywebview.api.move_window === 'function';
        if (!hasMoveApi()) {
            // Same once-listener + capped-poll shape as
            // _bindWindowResizeHandles — see the comment there.
            if (!this._moveApiWaitBound) {
                this._moveApiWaitBound = true;
                window.addEventListener('pywebviewready', () => this._bindWindowDragFallback(), { once: true });
            }
            this._moveApiPolls = (this._moveApiPolls || 0) + 1;
            if (this._moveApiPolls <= 15) {
                setTimeout(() => this._bindWindowDragFallback(), 1000);
            }
            return;
        }
        this._dragFallbackBound = true;
        document.body.classList.add('native-drag-ready');

        titlebar.addEventListener('mousedown', (event) => {
            if (event.button !== 0 || event.detail > 1) return;
            const target = event.target instanceof Element ? event.target : event.target?.parentElement;
            if (target?.closest('button,input,select,textarea,a,.titlebar-menus,.titlebar-window-controls,.titlebar-command,.status-popover,.header-badge,[role="button"]')) {
                return;
            }
            if (!hasMoveApi()) return;

            event.preventDefault();
            const startMouseX = event.screenX;
            const startMouseY = event.screenY;
            const startWindowX = window.screenX;
            const startWindowY = window.screenY;
            let queued = false;
            let nextX = startWindowX;
            let nextY = startWindowY;

            const flush = () => {
                queued = false;
                pywebview.api.move_window(Math.round(nextX), Math.round(nextY));
            };
            const onMove = (moveEvent) => {
                nextX = startWindowX + (moveEvent.screenX - startMouseX);
                nextY = startWindowY + (moveEvent.screenY - startMouseY);
                if (!queued) {
                    queued = true;
                    requestAnimationFrame(flush);
                }
            };
            const onUp = () => {
                document.removeEventListener('mousemove', onMove);
                document.removeEventListener('mouseup', onUp);
                document.body.classList.remove('is-window-dragging');
            };

            document.body.classList.add('is-window-dragging');
            document.addEventListener('mousemove', onMove);
            document.addEventListener('mouseup', onUp);
        });
    }

    // ── Event Binding ───────────────────────────────────────────

    /**
     * Wire every DOM listener.
     *
     * Was one 467-line block of 62 addEventListener calls, which made
     * "where is the composer wired up" a scrolling exercise. Split by
     * surface; each group below is independently readable and the order
     * here is the only coupling between them.
     */
    bindEvents() {
        document.getElementById('managed-previews-button')?.addEventListener('click', () => {
            this._showManagedPreviews = true;
            this.send({command: 'preview_list'});
        });
        document.getElementById('project-memory-button')?.addEventListener('click', () => this.send({command: 'memory_list'}));
        this._bindComposer();
        this._bindTerminalBar();
        this._bindRunControls();
        this._bindSidebarChrome();
        this._bindAttachments();
        this._bindPreviewPanel();
        this._bindGlobalSurfaces();
    }

    /** Wires the message composer: send, mission toggle, textarea
     * behaviour, and the stop button. */
    _bindComposer() {
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
            this._syncComposerGutter?.();
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
            this._requestCancel();
            this._maybeOfferMissionExitOnCancel();
        });
    }

    /** Wires the managed-process terminal bar. */
    _bindTerminalBar() {
        // Terminal bar — header click toggles expand/collapse
        document.getElementById('terminal-bar-header').addEventListener('click', (e) => {
            // Don't toggle if clicking the stop-all button
            if (e.target.closest('.terminal-bar-stop')) return;
            this.terminalBar.classList.toggle('expanded');
        });

        // Terminal bar — stop all
        this.terminalStopAll.addEventListener('click', (e) => {
            e.stopPropagation();
            this._requestCancel();
        });

        // Terminal bar — event delegation for individual stop buttons
        this.terminalBarList.addEventListener('click', (e) => {
            const stopBtn = e.target.closest('.terminal-entry-stop');
            if (stopBtn) {
                e.stopPropagation();
                this._requestCancel();
            }
        });
    }

    /** Wires run configuration: model selector, voice input, reasoning
     * depth, and the permission mode dropdown. */
    _bindRunControls() {
        // Model selector — value is "backend:model" (model may contain colons like "nemotron:cloud")
        this.modelSelector.addEventListener('change', () => {
            const val = this.modelSelector.value;
            const idx = val.indexOf(':');
            if (idx > 0) {
                const backend = val.substring(0, idx);
                const model = val.substring(idx + 1);
                this.currentBackendName = backend;
                this.currentModelName = model;
                // Optimistically claiming 'connected' here was half of the
                // "model selected but no backend" contradiction: the status
                // went green before the server had tried to build anything,
                // and stayed green when the build failed. Report the attempt;
                // the init payload that follows reports the outcome.
                this._setSystemStatus('warning', `Starting ${model}...`);
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

        // Reasoning-depth selector for models that expose thinking controls.
        if (this.thinkingModeSelector) {
            this.thinkingModeSelector.addEventListener('change', () => {
                const mode = this.thinkingModeSelector.value || '';
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
    }

    /** Wires sidebar chrome: collapse toggle, activity rail, permission
     * dialog, new session, and the add-project button. */
    _bindSidebarChrome() {
        // Sidebar toggle
        document.getElementById('sidebar-toggle').addEventListener('click', () => {
            document.getElementById('sidebar').classList.toggle('collapsed');
        });
        const sidebar = document.getElementById('sidebar');
        const mobileSidebarQuery = window.matchMedia('(max-width: 820px)');
        const syncMobileSidebar = () => {
            if (!sidebar) return;
            sidebar.classList.toggle('collapsed', mobileSidebarQuery.matches);
        };
        syncMobileSidebar();
        mobileSidebarQuery.addEventListener?.('change', syncMobileSidebar);
        document.getElementById('titlebar-sidebar-toggle')?.addEventListener('click', () => {
            document.getElementById('sidebar-toggle')?.click();
        });
        document.getElementById('titlebar-command')?.addEventListener('click', () => {
            this.openCommandPalette();
        });
        document.getElementById('titlebar-settings')?.addEventListener('click', () => {
            // Navigation controls must be idempotent. Treating the gear as a
            // toggle meant a quick second click (or an impatient double-click
            // while the settings payload rendered) immediately closed the
            // view again. The explicit Back button owns the close action.
            this.switchView('settings');
        });
        document.getElementById('settings-back')?.addEventListener('click', () => {
            this.switchView('agents');
        });
        document.getElementById('rail-open-project')?.addEventListener('click', () => {
            this.openProjectFolder('register');
        });
        // Scope to the activity rail: the bare [data-action="shortcuts"]
        // selector grabbed the Help-menu item first, double-binding it
        // (open+close per click) while this rail button got no handler.
        document.querySelector('.rail-btn[data-action="shortcuts"]')?.addEventListener('click', () => {
            this.toggleShortcutsOverlay();
        });

        this.statusPopoverTrigger?.addEventListener('click', (e) => {
            e.stopPropagation();
            this.toggleStatusPopover();
        });
        this.statusPopover?.addEventListener('click', (e) => {
            e.stopPropagation();
            const tab = e.target.closest('.status-tab');
            if (tab && tab.dataset.statusTab) {
                this.statusPopoverTab = tab.dataset.statusTab;
                this._renderStatusPopover();
                return;
            }
            const skillRow = e.target.closest('[data-status-skill-id]');
            if (skillRow?.dataset.statusSkillId) {
                this.openSkillDetail(skillRow.dataset.statusSkillId);
                this.closeStatusPopover();
                return;
            }
            const mcpConnect = e.target.closest('[data-status-mcp]');
            if (mcpConnect?.dataset.statusMcp) {
                this.send({ command: 'mcp_connect', name: mcpConnect.dataset.statusMcp });
                this.closeStatusPopover();
                return;
            }
            const action = e.target.closest('[data-status-action]');
            if (action?.dataset.statusAction === 'open-settings') {
                this.switchView('settings');
                this.closeStatusPopover();
            }
        });
        document.addEventListener('click', () => this.closeStatusPopover());
        this._bindWindowResizeHandles();
        this._bindWindowDragFallback();
        this._bindComposerGutterSync();

        // Permission dialog
        document.getElementById('permission-allow').addEventListener('click', () => {
            this.send({ command: 'approve', approved: true });
            this._setSessionActivity('working');
            document.getElementById('permission-dialog').style.display = 'none';
        });
        document.getElementById('permission-deny').addEventListener('click', () => {
            this.send({ command: 'approve', approved: false });
            this._setSessionActivity('working');
            document.getElementById('permission-dialog').style.display = 'none';
        });

        // New session — show project picker / welcome screen
        document.getElementById('new-agent-btn').addEventListener('click', () => {
            this.startNewSession();
        });

        // Add project button (next to project filter dropdown)
        document.getElementById('pf-add-project')?.addEventListener('click', () => {
            this.openProjectFolder('register');
        });
    }

    /** Wires image attachment: clipboard paste and drag-and-drop. */
    _bindAttachments() {
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
    }

    /** Wires the preview panel: visibility, tabs, plan-graph toolbar,
     * navigation, console, and the resize handle. */
    _bindPreviewPanel() {
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
        document.getElementById('context-cockpit-refresh')?.addEventListener('click', () => {
            this.send({ command: 'get_context_state' });
            this.send({ command: 'context_catalog' });
        });
        document.querySelectorAll('.runtime-view-tab').forEach((button) => {
            button.addEventListener('click', () => this.switchRuntimeView(button.dataset.runtimeView));
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
    }

    /** Wires everything global or badge-driven: lightbox escape, context
     * menu dismissal, sidebar navigation, keyboard shortcuts, search, and
     * the git / harness / RESONANT.md badges. */
    _bindGlobalSurfaces() {
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
        document.querySelectorAll('.sidebar-icon-btn[data-view], .sidebar-nav-item[data-view], .sidebar-action[data-view], .rail-btn[data-view]').forEach(item => {
            item.addEventListener('click', () => {
                // Views are destinations, not toggles. Their own Back/Close
                // affordances own navigation away from them.
                const view = item.dataset.view;
                this.switchView(view);
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
            this._syncComposerGutter?.();
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
        this._syncComposerGutter?.();
    }

    // ── Send Message ────────────────────────────────────────────

    _prepareTurnUI(text, images = []) {
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
        this._resetTaskCardState();
        this.addUserMessage(text, images);
        if (this._historyPage && this.currentSessionId) {
            this._loadedHistoryEvents.push({ event: 'user_message', text });
            this._historyPage = {
                ...this._historyPage,
                total_events: Number(this._historyPage.total_events || 0) + 1,
            };
        }
        this._resetAgentRunSummary(text);
        this._blockToolRows = new Map();
        this.subagentDepth = 0;
        this.subagentContainer = null;
        this.subagentContainers.clear();
        this.subagentStreams.clear();
        this.clearTerminals();
        this._removeLiveAgentTodoStrip();
    }

    _clearComposerAfterSend() {
        this.userInput.value = '';
        this.userInput.style.height = 'auto';
        this._syncComposerGutter?.();
        this.attachedImages = [];
        this.renderAttachedImages();
    }

    _queueFollowUpMessage(text) {
        const messageId = (globalThis.crypto?.randomUUID?.() || `followup-${Date.now()}-${Math.random()}`);
        const images = this.attachedImages.map((image) => ({ ...image }));
        const msg = { command: 'message', text, message_id: messageId };
        if (images.length) {
            msg.images = images.map((image) => ({
                data: image.data,
                media_type: image.media_type,
            }));
        }
        this._renderQueuedMessage(messageId, text, images);
        this.send(msg);
        this._clearComposerAfterSend();
        this.userInput.focus();
    }

    _renderQueuedMessage(messageId, text, images = []) {
        const item = document.createElement('section');
        item.className = 'steer-queue-item';
        item.dataset.messageId = messageId;
        item.innerHTML = `
            <span class="steer-queue-icon" aria-hidden="true"><i></i></span>
            <span class="steer-queue-copy"><strong>Follow-up queued</strong><small>${this.escapeHtml(text)}</small></span>
            <span class="steer-queue-actions">
                <span class="steer-queue-position">Queued</span>
                <button type="button" class="steer-queue-promote">Steer</button>
                <button type="button" class="steer-queue-remove" aria-label="Remove queued follow-up" title="Remove queued follow-up">&times;</button>
            </span>
        `;
        item.querySelector('.steer-queue-promote')?.addEventListener('click', () => {
            this._promoteQueuedMessage(messageId);
        });
        item.querySelector('.steer-queue-remove')?.addEventListener('click', () => {
            this._removeQueuedMessage(messageId);
        });
        (this.composerQueue || this.chatMessages).appendChild(item);
        if (this.composerQueue) this.composerQueue.hidden = false;
        this._queuedMessages.set(messageId, { el: item, text, images, steer: false });
        this._syncComposerGutter?.();
    }

    _promoteQueuedMessage(messageId) {
        const queued = this._queuedMessages.get(messageId);
        if (!queued || queued.steer) return;
        queued.steer = true;
        queued.el.classList.add('is-promoting');
        const button = queued.el.querySelector('.steer-queue-promote');
        if (button) {
            button.disabled = true;
            button.textContent = 'Steering';
        }
        this.send({ command: 'steer_queued', message_id: messageId });
    }

    _removeQueuedMessage(messageId) {
        const queued = this._queuedMessages.get(messageId);
        if (!queued || queued.removing) return;
        queued.removing = true;
        queued.el.classList.add('is-removing');
        queued.el.querySelectorAll('button').forEach(button => { button.disabled = true; });
        const label = queued.el.querySelector('.steer-queue-position');
        if (label) label.textContent = 'Removing';
        this.send({ command: 'remove_queued', message_id: messageId });
    }

    _syncComposerQueue() {
        if (!this.composerQueue) return;
        this.composerQueue.hidden = this.composerQueue.children.length === 0;
        this._syncComposerGutter?.();
    }

    handleMessageQueued(event) {
        const queued = this._queuedMessages.get(event.message_id);
        if (!queued) return;
        const label = queued.el.querySelector('.steer-queue-position');
        const heading = queued.el.querySelector('.steer-queue-copy strong');
        if (heading) heading.textContent = event.steering ? 'Steering current run' : 'Follow-up queued';
        if (label) label.textContent = event.steering
            ? 'Waiting for next step'
            : `Queued ${event.position || ''}`.trim();
        queued.el.classList.add('is-acknowledged');
        queued.el.classList.toggle('is-steering', !!event.steering);
    }

    handleSteerApplied(event) {
        const run = this._liveRun;
        if (run && run.statusRequestId === event.message_id) {
            if (run.statusRequestTimer) clearTimeout(run.statusRequestTimer);
            run.statusRequestTimer = null;
            run.statusRequestState = 'applied';
            run.statusNote = 'Agent received the update request and will report in this run';
            this._renderLiveRun();
            return;
        }
        const queued = this._queuedMessages.get(event.message_id);
        if (!this.chatMessages.querySelector(`[data-steer-note-id="${CSS.escape(event.message_id || '')}"]`)) {
            const note = document.createElement('div');
            note.className = 'live-steer-note';
            note.dataset.steerNoteId = event.message_id || '';
            note.innerHTML = `
                <span><small>You steered</small>${this.escapeHtml(event.text || '')}</span>
                <strong>Applied to current run</strong>
            `;
            this.chatMessages.appendChild(note);
        }
        if (queued) {
            const heading = queued.el.querySelector('.steer-queue-copy strong');
            const label = queued.el.querySelector('.steer-queue-position');
            if (heading) heading.textContent = 'Steer applied';
            if (label) label.textContent = 'In current context';
            queued.el.classList.remove('is-promoting');
            queued.el.classList.add('is-applied');
            setTimeout(() => {
                queued.el.remove();
                this._queuedMessages.delete(event.message_id);
                this._syncComposerQueue();
            }, 900);
        }
        this._setLiveRunPhase('Steered', 'Working with your added direction', event.step || null);
        this.scrollToBottom();
    }

    handleMessageStarted(event) {
        const queued = this._queuedMessages.get(event.message_id);
        const text = queued?.text || event.text || 'Continue';
        const images = queued?.images || [];
        queued?.el?.remove();
        this._queuedMessages.delete(event.message_id);
        this._syncComposerQueue();
        this._prepareTurnUI(text, images);
        this.setRunning(true);
    }

    handleMessageQueueCleared(event) {
        (event.message_ids || []).forEach((messageId) => {
            this._queuedMessages.get(messageId)?.el?.remove();
            this._queuedMessages.delete(messageId);
        });
        this._syncComposerQueue();
    }

    handleMessageRemoved(event) {
        const queued = this._queuedMessages.get(event.message_id);
        queued?.el?.remove();
        this._queuedMessages.delete(event.message_id);
        this._syncComposerQueue();
    }

    handleMessageRemoveFailed(event) {
        const queued = this._queuedMessages.get(event.message_id);
        if (queued) {
            queued.removing = false;
            queued.el.classList.remove('is-removing');
            queued.el.querySelectorAll('button').forEach(button => { button.disabled = false; });
            const label = queued.el.querySelector('.steer-queue-position');
            if (label) label.textContent = queued.steer ? 'Waiting for next step' : 'Queued';
        }
        this.showToastMessage(event.message || 'That follow-up could not be removed.');
    }

    sendMessage(options = {}) {
        const autoRetry = !!(options && options.autoRetry === true);
        if (!autoRetry) this._autoFallbackDepth = 0;
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
                this._syncComposerGutter?.();
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
                this._syncComposerGutter?.();
                return;
            }
        }

        if (this.isRunning) {
            this._queueFollowUpMessage(text);
            return;
        }

        // /plan slash-prefix routes the message to the intent flow instead of
        // a one-shot Session.run. Strip the prefix and forward.
        if (text.startsWith('/plan ')) {
            this.startIntent(text.slice('/plan '.length).trim());
            this.userInput.value = '';
            this.userInput.style.height = 'auto';
            this._syncComposerGutter?.();
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
            this._syncComposerGutter?.();
            return;
        }

        if (text.startsWith('/grill') || text.startsWith('/mission')) {
            this.showStatusMessage('That workflow is hidden in the redesigned UI. Start a normal session and ask Resonant directly.');
            return;
        }

        this._prepareTurnUI(text, this.attachedImages);

        // Send to server (include images if attached)
        const msg = { command: 'message', text };
        if (this.attachedImages.length > 0) {
            msg.images = this.attachedImages.map(img => ({
                data: img.data,
                media_type: img.media_type,
            }));
        }
        this.send(msg);

        this._clearComposerAfterSend();
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

    _renderOrphanCardHTML(orphan) {
        const intentId = orphan.intent_id || '';
        const sessionId = orphan.session_id || '';
        const feature = orphan.feature || '(unnamed session)';
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
            : 'title="Resume this autonomous session from where it stopped"';

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
                            title="Hide this — does NOT stop the autonomous session">Dismiss</button>
                </div>
            </div>
        `;
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

    _renderInspectorPhaseBadge(phase) {
        // v0.5.4a4 — small status pill that sits next to the feature
        // title. Live (autonomous_running) gets no badge — the inspector
        // being visible is signal enough. Terminal phases get a colored
        // badge so the user instantly sees the final state.
        switch (phase) {
            case 'autonomous_complete':
                return `<span class="arm-phase-badge arm-phase-complete" title="Autonomous session converged">complete</span>`;
            case 'autonomous_paused':
                return `<span class="arm-phase-badge arm-phase-paused" title="Autonomous session paused (user stop / budget / stuck)">paused</span>`;
            case 'autonomous_failed':
                return `<span class="arm-phase-badge arm-phase-failed" title="Autonomous session ended in failure">failed</span>`;
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
     * v0.3.3 — Bug #25 surface. The chat-header now shows the active
     * project path so misconfigurations (the install dir is the project!
     * permission denied!) are visible BEFORE the agent does damage.
     * Click swaps projects via the native picker.
     */
    _updateHeaderProjectPath(cwd) {
        // v0.6.7 — keep the composer-footer folder chip in sync (always,
        // even if the header path element happens to be absent).
        const _footerName = document.getElementById('footer-project-name');
        if (_footerName) {
            const _p = (cwd || '').replace(/\\/g, '/').split('/').filter(Boolean);
            _footerName.textContent = _p[_p.length - 1] || (cwd || 'project');
        }
        const shellName = document.getElementById('sidebar-shell-project-name');
        const shellPath = document.getElementById('sidebar-shell-project-path');
        if (shellName || shellPath) {
            const path = (cwd || '').replace(/\\/g, '/');
            const parts = path.split('/').filter(Boolean);
            const short = parts[parts.length - 1] || 'Resonant';
            if (shellName) shellName.textContent = short;
            if (shellPath) {
                shellPath.textContent = path || 'Open a workspace';
                shellPath.title = path || 'Open a workspace';
            }
        }
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
                this.openProjectFolder();
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

    _syncSessionTitle(sess = null) {
        const el = document.getElementById('chat-session-title');
        if (!el) return;
        const session = sess || this._currentSessionSummary();
        el.textContent = (session && session.title) || 'New session';
        el.title = el.textContent;
    }

    openProjectFolder(consumer = null) {
        this._pendingFolderPickConsumer = consumer;
        this.send({
            command: 'folder_dialog',
            directory: (this.currentCwd || '').trim(),
        });
    }

    showCurrentProjectBackendSetup() {
        const cwd = (this.currentCwd || '').trim();
        if (!cwd) {
            this.openProjectFolder();
            return;
        }

        if (this.agentPanel) this.agentPanel.style.display = 'none';
        else this.chatContainer.style.display = 'none';
        this.inputBar.style.display = 'none';
        if (this.settingsView) this.settingsView.style.display = 'none';

        this.welcomeScreen.style.display = 'flex';
        this.currentView = 'agents';
        document.querySelectorAll('.sidebar-icon-btn[data-view], .sidebar-nav-item[data-view], .sidebar-action[data-view], .rail-btn[data-view]').forEach(el =>
            el.classList.toggle('active', el.dataset.view === 'agents'));

        this.clearPreviewPanel();
        this.closePreviewPanel();
        this._maybeRenderOnboardingCard();

        const projectStep = document.getElementById('project-step');
        const backendStep = document.getElementById('backend-step');
        const roleSelect = document.getElementById('setup-session-role');
        if (projectStep) projectStep.style.display = 'none';
        if (backendStep) backendStep.style.display = 'block';
        if (roleSelect) {
            const wrapper = roleSelect.closest('.chat-welcome-footer');
            if (wrapper) wrapper.style.display = this.harnessEnabled ? '' : 'none';
            roleSelect.value = this.sessionRole || 'generator';
            roleSelect.onchange = () => {
                this.sessionRole = roleSelect.value || 'generator';
            };
        }

        const short = cwd.replace(/\\/g, '/').split('/').pop() || cwd;
        const badge = document.getElementById('setup-project-badge');
        if (badge) {
            badge.innerHTML = `
                <span class="badge-icon">&#128193;</span>
                ${this.escapeHtml(short)}
                <span class="badge-change">change</span>
            `;
            badge.onclick = () => this.openProjectFolder();
        }

        const backends = this.backends || {};
        if (Object.keys(backends).length > 0) {
            this.showBackendSelector(backends);
        } else {
            const label = document.querySelector('.backend-label');
            if (label) label.textContent = 'Scanning backends...';
            this.send({ command: 'redetect_backends' });
        }
    }

    /**
     * v0.6.5 — render the heartbeat liveness token for the badge.
     * Gated on `iterInFlight`: the daemon only heartbeats while a
     * sub-mission is dispatched, and that flag is cleared the instant
     * the iter completes/fails/times out — so a stale heartbeat can
     * never linger as a false "live"/"stalled" between iterations.
     *   • fresh beat  → "♥ live"  (proof the wait is genuinely alive)
     *   • stale beat  → "⚠ no heartbeat 1m 30s"  (likely frozen)
     */
    _fmtHeartbeatToken() {
        const s = this._autonomousState || {};
        const hb = s.heartbeat;
        if (!hb || typeof hb.at !== 'number' || !s.iterInFlight) return '';
        const sinceLast = (Date.now() / 1000) - hb.at;
        // 2.5 × the daemon's ~30s heartbeat — slack for network jitter
        // and a momentarily busy event loop before we cry stall.
        const STALE_AFTER = 75;
        if (sinceLast <= STALE_AFTER) return '♥ live';
        return `⚠ no heartbeat ${this._fmtDuration(sinceLast)}`;
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
                Autonomous session dispatched at ${hh}:${mm}:${ss}
                <span class="mission-dispatch-chip-budget">· ${this.escapeHtml(budget || '')}</span>
            </span>
            <button type="button" class="mission-dispatch-chip-stop" title="Stop the autonomous session after the current iteration">Stop</button>
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

    _requestCancel() {
        if (!this.isRunning || this._cancelInFlight) return;
        const cancelId = (globalThis.crypto?.randomUUID?.() || `cancel-${Date.now()}-${Math.random()}`);
        this._cancelInFlight = cancelId;
        this._cancelCardBaseline = new Set(this.chatMessages.querySelectorAll('.task-card'));
        this.stopBtn.disabled = true;
        this.stopBtn.classList.add('is-stopping');
        this.stopBtn.title = 'Stopping agent and running tools...';
        this.stopBtn.setAttribute('aria-label', 'Stopping agent and running tools');
        this._setLiveRunPhase('Stopping', 'Cancelling the model and running tools');
        this.send({ command: 'cancel', cancel_id: cancelId });
        clearTimeout(this._cancelWatchdog);
        this._cancelWatchdog = setTimeout(() => {
            if (this._cancelInFlight === cancelId) {
                this._setLiveRunPhase('Still stopping', 'Waiting for a running tool to exit safely');
                this.showStatusMessage('Still stopping the active tool safely...');
            }
        }, 5000);
    }

    handleCancelRequested(event) {
        if (this._cancelInFlight && event.cancel_id !== this._cancelInFlight) return;
        this._queuedMessages.forEach((queued) => queued.el?.remove());
        this._queuedMessages.clear();
        this._syncComposerQueue();
        this._setLiveRunPhase('Stopping', 'Draining the active session');
    }

    _finishCancelledTask() {
        const task = this._activeTask;
        if (task) {
            task.card.classList.remove('task-card-running');
            task.card.classList.add('task-card-stopped');
            task.stateEl.className = 'task-card-state is-stopped';
            task.stateEl.textContent = 'Stopped';
            this._completeLiveRun(true);
            this._collapseTaskActivity({});
            this._setActiveTask(null);
        }
        this.removeThinking();
        this.finalizeToolActivityGroup();
        this.clearTerminals();
        this.isStreaming = false;
        this.currentMessageEl = null;
        this.setRunning(false);
    }

    _removeEmptyCancelArtifacts() {
        const baseline = this._cancelCardBaseline || new Set();
        this.chatMessages.querySelectorAll('.task-card[data-user-message="synthetic"]').forEach((card) => {
            if (baseline.has(card)) return;
            const activity = card.querySelector('.task-activity')?.textContent?.trim() || '';
            const result = card.querySelector('.task-result')?.textContent?.trim() || '';
            const footer = card.querySelector('.task-card-footer')?.textContent?.trim() || '';
            if (!activity && !result && !footer) card.remove();
        });
        this._cancelCardBaseline = null;
    }

    handleCancelCompleted(event) {
        if (this._cancelInFlight && event.cancel_id !== this._cancelInFlight) return;
        clearTimeout(this._cancelWatchdog);
        this._cancelWatchdog = null;
        this._finishCancelledTask();
        this._removeEmptyCancelArtifacts();
        this._cancelInFlight = null;
        this._cancelInterrupted = false;
        this.stopBtn.disabled = false;
        this.stopBtn.classList.remove('is-stopping');
        this.stopBtn.title = 'Stop the agent';
        this.stopBtn.setAttribute('aria-label', 'Stop the agent');
    }

    setRunning(running) {
        this.isRunning = running;
        this._setSessionActivity(running ? 'working' : 'idle');
        this.sendBtn.style.display = 'flex';
        this.stopBtn.style.display = running ? 'flex' : 'none';
        this.userInput.disabled = false;
        this.userInput.placeholder = running
            ? 'Write a follow-up for the running agent...'
            : 'Message Resonant';
        const sendLabel = running
            ? 'Queue follow-up (Enter) — Shift+Enter for newline'
            : 'Send message (Enter) — Shift+Enter for newline';
        this.sendBtn.title = sendLabel;
        this.sendBtn.setAttribute('aria-label', sendLabel);
        this.userInput.closest('.input-wrapper')?.classList.toggle('is-running', running);
        if (running) this._startLiveRun();
        else this._stopLiveRun();
        this.userInput.focus();
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
        const labels = { planner: 'Plan', generator: 'Build', evaluator: 'Review' };
        if (role === 'chat') return ''; // suppress legacy tag
        return labels[role] || 'Build';
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

    _withRenderEvent(event, callback) {
        const previous = this._activeRenderEvent;
        this._activeRenderEvent = event;
        try {
            return callback();
        } finally {
            this._activeRenderEvent = previous;
        }
    }

    handleEvent(event) {
        const type = event.event;
        const projectSwitchId = String(event?.project_switch_id || '');

        // A full project initialization can outlive a newer rail click.
        // Older acknowledgements must never repaint the newer selection.
        if (
            projectSwitchId
            && this._latestProjectSwitchId
            && projectSwitchId !== this._latestProjectSwitchId
        ) {
            return;
        }

        if (
            this._liveRun?.active
            && [
                'step.start', 'step.end', 'thinking.delta', 'text.delta', 'text.done',
                'tool.call', 'tool.result', 'subagent.start', 'subagent.end', 'backend_status',
            ].includes(type)
        ) {
            this._liveRun.lastEventAt = Date.now();
        }

        // Only ledger-backed stream events carry this cursor. Retain them so
        // loading an older page during a live continuation cannot repaint a
        // stale tail and make the new turn disappear.
        if (
            this._historyPage
            && this.currentSessionId
            && !this.isReplaying
            && Number.isInteger(event?._ledger_seq)
        ) {
            this._loadedHistoryEvents.push({ ...event });
            this._historyPage = {
                ...this._historyPage,
                end_seq: event._ledger_seq,
                as_of_seq: event._ledger_seq,
                total_events: Number(this._historyPage.total_events || 0) + 1,
            };
        }

        // Single-delegation events resolve here; see RESONANT_EVENT_DELEGATES.
        // Checked before the switch so the table is the first place to look,
        // and so a mistyped handler name fails loudly instead of falling
        // through to the default arm and being swallowed.
        const delegate = RESONANT_EVENT_DELEGATES[type];
        if (delegate) {
            if (typeof this[delegate] !== 'function') {
                console.error(`No handler ${delegate} for event "${type}"`);
                return;
            }
            this._withRenderEvent(event, () => this[delegate](event));
            return;
        }

        switch (type) {
            case 'previews_updated':
                if (this._normalizeProjectPath(event.project) !== this._normalizeProjectPath(this.currentCwd)) break;
                this._managedPreviews = event.previews || [];
                if (this._showManagedPreviews) this._renderProjectResources('previews');
                break;
            case 'project_memory_updated':
                if (this._normalizeProjectPath(event.project) !== this._normalizeProjectPath(this.currentCwd)) break;
                this._projectNotes = event.memories || [];
                this._renderProjectResources('notes');
                break;
            case 'error':
                if (
                    projectSwitchId
                    && projectSwitchId === this._pendingProjectSwitchId
                ) {
                    this._pendingProjectSwitchId = '';
                    this._pendingProjectPath = '';
                    this._restoreConfirmedProjectSelection();
                }
                this._withRenderEvent(event, () => {
                    if (event._subagent) this.handleSubagentError(event);
                    else this.handleError(event);
                });
                break;
            case 'mission_exited':
                this.handleMissionExited(event);
                break;
            // v0.5.0a7 — autonomous-mission events from
            // AutonomousMissionDaemon. See docs/long-running-agents-
            // phase-2-implementation.md §4.5 for the contract.
            case 'autonomous_iteration_failed':
                this.handleAutonomousIterationFailed(event);
                break;
            // v0.6.5 (task #7) — long-running session health signals.
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
            case 'await_user':
                // v0.3.5 — agent paused with `await_user` tool, asking
                // a focused question. Render an inline prompt with
                // optional quick-reply chips; the user's reply goes
                // back via the `user_input` WS command and unblocks
                // the agent.
                this.handleAwaitUser(event);
                break;
            case 'status_msg':
                this.showStatusMessage(event.message);
                break;
            case 'ui_notice':
                this.showToastMessage(event.message);
                break;
            case 'project_registered':
                if (Array.isArray(event.recent_projects)) {
                    this.recentProjects = event.recent_projects;
                }
                if (event.playground_project) {
                    this.playgroundProject = event.playground_project;
                }
                {
                    const registeredPath = this._normalizeProjectPath(event.path || '');
                    const hasRegistered = registeredPath && (this.recentProjects || [])
                        .some(project => this._normalizeProjectPath(project?.path || '') === registeredPath);
                    if (registeredPath && !hasRegistered) {
                        this.recentProjects = [
                            { name: this._projectNameFromPath(registeredPath), path: registeredPath },
                            ...(this.recentProjects || []),
                        ];
                    }
                }
                if (Array.isArray(event.all_sessions)) {
                    this.allSessions = event.all_sessions;
                }
                this.renderProjectRail();
                if (event.open_after_add && event.path) this.selectProjectFolder(event.path);
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
                this._syncSessionTitle();
                break;
            case 'session_cleared':
                if (
                    this._newSessionInflight
                    && event.request_id
                    && event.request_id !== this._newSessionRequestId
                ) {
                    break;
                }
                // Mission flow: the frontend already rendered the seed
                // feature as a user message before sending mission_start.
                // Wiping the chat here would visually delete it, leaving
                // an empty chat until the model's first reply streams.
                // Skip the wipe in that case — the new (empty) session
                // is already what we're showing.
                if (!event.mission_started) {
                    this.chatMessages.innerHTML = '';
                    this._resetTaskCardState();
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
                    this._projectFilter = this.currentCwd;
                    this._projectFilterUserCleared = false;
                    this._pinnedOnly = false;
                    if (this.sidebarProjectSwitchLabel) {
                        this.sidebarProjectSwitchLabel.textContent = this._projectNameFromPath(this.currentCwd);
                    }
                }
                this.applySessionRoleUI(event.session_role || this.sessionRole);
                this.renderFilteredSessions();
                // A new conversation must not inherit screenshots, console
                // output, or an open side panel from the previous session.
                this.clearPreviewPanel();
                this.closePreviewPanel();
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
                if (this._newSessionInflight) {
                    this._releaseNewSessionGuard();
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
                this._resetTaskCardState();
                this.currentSessionId = event.current_session_id || '';
                this._historyPage = event.history_page || null;
                this._loadedHistoryEvents = Array.isArray(event.display_events)
                    ? event.display_events.slice()
                    : [];
                this._historyLoading = false;
                this._historyWindowDroppedTail = false;
                this.sessions = event.sessions || [];
                this.applySessionRoleUI(event.session_role || 'generator');
                this.sessionRole = event.session_role || this.sessionRole;
                this.renderFilteredSessions();
                this._syncSessionTitle();
                this.showChatInterface();
                // Clear preview panel for loaded session
                this.clearPreviewPanel();
                this.closePreviewPanel();
                // Replay display events to rebuild the conversation in the UI
                if (event.display_events && event.display_events.length > 0) {
                    this.replayDisplayEvents(event.display_events);
                }
                this._renderHistoryPageControl();
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
                this.trackPlanAgentEvent(event);
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
                    if (this._pendingFolderPickConsumer === 'register') {
                        this._pendingFolderPickConsumer = null;
                        this.registerProjectFolder(event.path);
                        break;
                    }
                    this._pendingFolderPickConsumer = null;
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
                        ? 'Pick session folder'
                        : consumer === 'register'
                            ? 'Add project'
                        : 'Open project';
                    this._pendingFolderPickConsumer = null;
                    if (event.message) this.showToastMessage(event.message);
                    this._promptForProjectPath(label, (path) => {
                        if (consumer === 'mission') {
                            const pathInput = document.getElementById('mission-composer-path');
                            if (pathInput) {
                                pathInput.value = path;
                                pathInput.dispatchEvent(new Event('input'));
                                pathInput.focus();
                            }
                            return;
                        }
                        if (consumer === 'register') {
                            this.registerProjectFolder(path);
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
            case 'prompt_inspector':
                this.promptInspector = event.data || null;
                if (this.currentView === 'settings') this.renderSettingsView();
                break;
            case 'evaluation_dashboard':
                this.evaluationDashboard = event.data || null;
                if (this.currentView === 'settings') this.renderSettingsView();
                break;
            case 'evaluation_started':
                this.showStatusMessage('Model evaluation started in the background');
                this.send({ command: 'evaluation_list' });
                break;
            case 'checkpoint_list':
                this.iterationCheckpoints = event.checkpoints || [];
                if (this.currentView === 'settings') this.renderSettingsView();
                break;
            case 'checkpoint_comparison':
                this.checkpointComparison = event.data || null;
                if (this.currentView === 'settings') this.renderSettingsView();
                break;
            case 'checkpoint_restored':
                this.showStatusMessage(`Checkpoint restored; failed state preserved on ${event.data?.recovery_branch || 'a recovery branch'}`);
                this.send({ command: 'checkpoint_list' });
                this.send({ command: 'git_status' });
                break;
            case 'autonomous_iteration_checkpoint':
                this.showStatusMessage(`Saved recovery checkpoint for iteration ${event.iter_count}`);
                break;
            case 'autonomous_iteration_checkpoint_failed':
                this.showStatusMessage(`Checkpoint unavailable: ${event.error || 'unknown error'}`);
                break;
            case 'costs':
                this.costData = event.data || null;
                if (this.currentView === 'settings') this.renderSettingsView();
                break;
            case 'git_status':
                this.handleGitStatus(event.data);
                break;
            case 'resonant_md':
                this.resonantMd = event.info;
                this.resonantMdContent = event.content || '';
                this.updateResonantMdBadge();
                this._updateResonantMdPopoverContent();
                break;
            case 'context.state':
                this.contextState = event;
                this.renderContextCockpit();
                break;
            case 'context.catalog':
                this.contextProviders = event.providers || [];
                this.renderContextCockpit();
                break;
            case 'agent.created':
            case 'agent.updated':
            case 'agent.completed':
            case 'agent.steered':
            case 'agent.control_ack':
                if (event.agent) this.upsertRuntimeAgent(event.agent);
                this.renderRuntimeView();
                break;
            case 'agent.runtime_list':
                this.runtimeAgents = event.agents || [];
                this.syncRuntimeAgents();
                this.renderRuntimeView();
                break;
            case 'agent.runtime_detail':
                this.showRuntimeAgentDetail(event.agent, event.transcript || []);
                break;
            case 'session.timeline_list':
                this.runtimeTimeline = event.checkpoints || [];
                this.renderRuntimeView();
                break;
            case 'session.timeline_comparison':
                this.showRuntimePayload('Checkpoint comparison', event.data || {});
                break;
            case 'session.timeline_restored':
                this.showStatusMessage('Checkpoint restored');
                this._historyPage = event.history_page || null;
                this._loadedHistoryEvents = Array.isArray(event.display_events)
                    ? event.display_events.slice()
                    : [];
                this._historyLoading = false;
                this._historyWindowDroppedTail = false;
                if (event.display_events) {
                    this.chatMessages.innerHTML = '';
                    this._resetTaskCardState();
                    this.replayDisplayEvents(event.display_events);
                }
                this._renderHistoryPageControl();
                this.send({ command: 'session_timeline_list' });
                break;
            case 'flight.recorder_list':
                this.runtimeTraces = event.runs || [];
                this.renderRuntimeView();
                break;
            case 'flight.recorder_detail':
                this.showRuntimePayload('Run trace', { manifest: event.manifest, events: event.events });
                break;
            case 'flight.recorder_comparison':
                this.showRuntimePayload('Trajectory comparison', event.data || {});
                break;
            case 'artifact.list':
                this.runtimeArtifacts = event.artifacts || [];
                this.renderRuntimeView();
                break;
            case 'artifact.created':
                if (event.artifact) this.runtimeArtifacts.unshift(event.artifact);
                this.renderRuntimeView();
                break;
            case 'capability.pack_list':
                this.runtimePacks = event.packs || [];
                this.renderRuntimeView();
                break;
            case 'mcp_list':
                this.mcpServers = event.servers || [];
                this.mcpHealth = event.health || {};
                this._renderStatusPopover();
                // Refresh settings view if open
                if (this.currentView === 'settings') {
                    this.renderSettingsView();
                }
                break;
            case 'lsp_list':
                this.lspItems = event.servers || event.items || [];
                this._renderStatusPopover();
                break;
            case 'plugin_list':
                this.resonantPlugins = event.plugins || [];
                this.pluginSummary = event.summary || {};
                this._renderStatusPopover();
                break;
            case 'rag_indexed':
                this.ragStats = event;
                this.showStatusMessage(`Indexed ${event.total_files} files in ${event.elapsed_ms}ms`);
                if (this.currentView === 'settings') this.renderSettingsView();
                break;
            case 'rag_stats':
                this.ragStats = event;
                break;
            case 'skill_list':
                this.skills = event.skills || [];
                this._renderStatusPopover();
                if (this._skillDetailOpenId) {
                    // Refresh open detail dialog if it's still relevant.
                    const stillExists = this.skills.some(s => s.id === this._skillDetailOpenId);
                    if (!stillExists) this.closeSkillDetail();
                }
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
        const projectSwitchId = String(event?.project_switch_id || '');
        if (this._pendingProjectSwitchId) {
            // While changing projects, only the latest selection may replace
            // project-scoped state. Untagged refreshes can already be in flight.
            if (projectSwitchId !== this._pendingProjectSwitchId) return;
            this._pendingProjectSwitchId = '';
            this._pendingProjectPath = '';
        }

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
            current_display_events,
            current_history_page,
            run_active,
            run_started_at,
            queued_messages,
            recent_projects,
            playground_project,
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
        this.currentBackendName = current_backend || '';
        this.currentModelName = current_model || '';
        this.handlesTools = event.handles_tools || false;

        if (recent_projects) {
            this.recentProjects = recent_projects;
        }
        if (playground_project) {
            this.playgroundProject = playground_project;
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
        this.requestMcpList();
        this.requestLspList();

        if (sessions) {
            this.sessions = sessions;
            this.allSessions = all_sessions || [];
            this.currentSessionId = current_session_id || '';
            this.applySessionRoleUI(current_session_role || this.currentSessionRole || 'generator');
            this.sessionRole = current_session_role || this.sessionRole;
            this.renderFilteredSessions();
            this._syncSessionTitle();
            if (
                current_session_id
                && Array.isArray(current_display_events)
                && !refresh_only
            ) {
                this.chatMessages.innerHTML = '';
                this._resetTaskCardState();
                this._historyPage = current_history_page || null;
                this._loadedHistoryEvents = current_display_events.slice();
                this._historyLoading = false;
                this._historyWindowDroppedTail = false;
                this.showChatInterface();
                if (current_display_events.length) {
                    this.replayDisplayEvents(current_display_events, { activeRun: run_active === true });
                }
                if (run_active === true) {
                    this.chatMessages.querySelector('.resume-banner')?.remove();
                    this.setRunning(true);
                    this._startLiveRun({
                        model: current_model || '',
                        provider: current_backend || '',
                        started_at: run_started_at,
                    });
                    this._setLiveRunPhase(
                        'Continuing',
                        'Reconnected to the active run · waiting for the next update',
                    );

                    // Replay builds the completed conversation surface but it
                    // intentionally does not mutate the live-run counters.
                    // Seed those counters from the unfinished turn so refresh
                    // does not make a 30-tool run look as though it just began.
                    const lastUserIndex = current_display_events.reduce(
                        (found, item, index) => item?.event === 'user_message' ? index : found,
                        -1,
                    );
                    const activeEvents = current_display_events.slice(lastUserIndex + 1);
                    const callsById = new Map(
                        activeEvents
                            .filter(item => item?.event === 'tool.call' && item.call_id)
                            .map(item => [item.call_id, item]),
                    );
                    const completed = activeEvents.filter(item => item?.event === 'tool.result');
                    if (this._liveRun) {
                        this._liveRun.completedTools = completed.length;
                        const latest = completed.at(-1);
                        if (latest) {
                            const call = callsById.get(latest.call_id) || {};
                            const activity = this._liveRunToolActivity(
                                latest.name,
                                call.arguments || {},
                            );
                            const failed = Boolean(latest.is_error || latest.denied);
                            this._liveRun.lastCompleted = {
                                text: failed
                                    ? `${activity.completed} — needs attention`
                                    : activity.completed,
                                elapsed: Number(latest.elapsed || 0),
                                failed,
                            };
                        }
                        this._renderLiveRun();
                    }
                }

                // The queue is owned by the server-side run loop. Recreate its
                // controls after reconnect so the user can still promote or
                // remove a follow-up they queued before refreshing.
                if (this.composerQueue) this.composerQueue.replaceChildren();
                this._queuedMessages.clear();
                for (const queued of (queued_messages || [])) {
                    this._renderQueuedMessage(queued.message_id, queued.text || '');
                    this.handleMessageQueued(queued);
                }
                this._syncComposerQueue();
            }
        } else {
            this.renderProjectRail();
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
            // `runtime_ready` is the authority on whether a message can be
            // sent. A backend can be selected while its session failed to
            // build (a dead MCP server is the usual cause), and reporting
            // "connected" there is what produced the contradiction users hit:
            // the composer named a model and every send came back refused.
            if (event.runtime_ready === false) {
                this._setSystemStatus('warning', `${current_model} — not running`);
            } else {
                this._setSystemStatus('connected', `${current_backend} / ${current_model}`);
            }
            this._modelCapabilities = event.model_capabilities || {};
            this.populateModelSelector(backends, current_backend, current_model);
            this.setThinkingMode(event.current_thinking_mode || '');
            this._applyRuntimeError(event);
            return;
        }

        // No backend loaded. Note this branch previously left the model
        // selector untouched, so it kept displaying whichever model was last
        // shown — the visible half of the same contradiction.
        this.populateModelSelector(backends, '', '', { unloaded: true });
        const haveProviders = Object.keys(this.backends || {}).length > 0;
        this._setSystemStatus('warning',
            haveProviders ? 'No model loaded' : 'No model server');
        this._applyRuntimeError(event);

        if (refresh_only) {
            const backendStep = document.getElementById('backend-step');
            if (backendStep && backendStep.style.display !== 'none') {
                this.showBackendSelector(backends);
            }
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
            const alternatives = ['exo', 'kimi', 'codex']
                .flatMap(backend => (backends?.[backend]?.models || []).map(model => ({ backend, model })));
            if (alternatives.length) {
                label.textContent = 'Pick a model';
                const card = document.createElement('div');
                card.className = 'backend-group-cards single';
                for (const item of alternatives) {
                    const row = document.createElement('div');
                    row.className = 'backend-card';
                    row.dataset.backend = item.backend;
                    row.dataset.model = item.model;
                    const provider = this._getBackendLabels()[item.backend] || item.backend;
                    const detail = backends[item.backend]?.url || `${provider} provider`;
                    row.innerHTML = `
                        <div class="backend-card-icon">${item.backend === 'exo' ? 'E' : (item.backend === 'kimi' ? 'K' : 'C')}</div>
                        <div class="backend-card-info">
                            <div class="backend-card-name">${this.escapeHtml(item.model)}</div>
                            <div class="backend-card-detail">${this.escapeHtml(detail)}</div>
                            <div class="backend-card-pills"><span class="backend-pill backend-pill-ok">${provider}</span></div>
                        </div>
                        <div class="backend-card-dot"></div>`;
                    row.addEventListener('click', () => this.selectBackend(item.backend, item.model));
                    card.appendChild(row);
                }
                list.appendChild(card);
                return;
            }
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

        label.textContent = 'Pick an Ollama model';

        const configuredDefault = (this.settings?.general?.default_model || '').trim();
        const ordered = models;

        const card = document.createElement('div');
        card.className = 'backend-group-cards single';

        for (const model of ordered) {
            const isConfiguredDefault = configuredDefault === model;
            const row = document.createElement('div');
            row.className = 'backend-card';
            row.dataset.backend = 'ollama';
            row.dataset.model = model;
            const pills = [];
            if (isConfiguredDefault) pills.push('<span class="backend-pill backend-pill-rec">Default</span>');
            if (model.endsWith(':cloud')) pills.push('<span class="backend-pill backend-pill-ok">Cloud</span>');
            else pills.push('<span class="backend-pill backend-pill-ok">Local</span>');
            row.innerHTML = `
                <div class="backend-card-icon">🦙</div>
                <div class="backend-card-info">
                    <div class="backend-card-name">${this.escapeHtml(model)}</div>
                    <div class="backend-card-detail">${this.escapeHtml(ollamaInfo.url || 'Ollama endpoint')}</div>
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
            hint.innerHTML = `✓ Reachable at <code>${this.escapeHtml(url)}</code>, but no models are installed yet.`;
            hint.className = 'ollama-wizard-hint ollama-wizard-hint-warn';
        } else {
            hint.innerHTML = `✗ <code>${this.escapeHtml(url)}</code> unreachable. Is <code>ollama serve</code> running on that host?`;
            hint.className = 'ollama-wizard-hint ollama-wizard-hint-warn';
        }
        this._ollamaProbeInflight = null;
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

    showChatInterface({ force = false } = {}) {
        // Runtime/session events can arrive after the user has navigated to a
        // feature view. Update their underlying session state, but do not let
        // a late init/session_loaded/session_cleared event steal navigation
        // and make Settings appear to close itself.
        if (!force && this.currentView !== 'agents') return;

        this.welcomeScreen.style.display = 'none';
        if (this.agentPanel) this.agentPanel.style.display = 'flex';
        else this.chatContainer.style.display = 'flex';
        this.inputBar.style.display = 'flex';
        this._syncComposerGutter?.();
        if (this.settingsView) this.settingsView.style.display = 'none';
        // Un-hide the sidebar session list too — switchView('settings')
        // hides it, and arriving here via Ctrl+N used to leave it hidden.
        const sessionList = document.getElementById('agent-list');
        if (sessionList) sessionList.style.display = '';
        this.currentView = 'agents';
        document.querySelectorAll('.sidebar-icon-btn[data-view], .sidebar-nav-item[data-view], .sidebar-action[data-view], .rail-btn[data-view]').forEach(el =>
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
            <p class="onboarding-sub">Resonant adapts its coding harness to the capabilities of your configured model and provider.</p>
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
        this.agentPanel?.classList.remove('has-empty-state');
        // Only render when chat is genuinely empty (no messages, no replays)
        if (this.chatMessages.children.length > 0) return;
        const empty = document.createElement('div');
        empty.className = 'chat-empty-state';
        empty.innerHTML = `
            <img class="chat-empty-logo" src="/static/resonant.png" alt="" aria-hidden="true">
            <h2 class="chat-empty-title">Resonant</h2>
            <p class="chat-empty-sub">Local-first multimodal coding agent for open-source models</p>
        `;
        this.chatMessages.appendChild(empty);
        this.agentPanel?.classList.add('has-empty-state');
    }

    /** Remove the empty-state card the moment a real message lands. */
    _removeChatEmptyState() {
        const el = this.chatMessages?.querySelector('.chat-empty-state');
        if (el) el.remove();
        this.agentPanel?.classList.remove('has-empty-state');
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
            exo: {
                label: 'EXO',
                backends: ['exo'],
            },
            kimi: {
                label: 'Kimi API',
                backends: ['kimi'],
            },
            codex: {
                label: 'Codex',
                backends: ['codex'],
            },
            ollama: {
                label: 'Ollama',
                backends: ['ollama'],
            },
        };
    }

    _getBackendLabels() {
        return { codex: 'Codex', exo: 'EXO', kimi: 'Kimi API', ollama: 'Ollama' };
    }

    _getPreferredBackendSelection(backends) {
        const preferredConfiguredBackend = (this.settings?.general?.default_backend || '').trim();
        const preferredConfiguredModel = this.settings?.general?.default_model || '';
        const backendOrder = [];
        if (preferredConfiguredBackend && backends?.[preferredConfiguredBackend]?.models?.length) {
            backendOrder.push(preferredConfiguredBackend);
        }
        for (const candidate of ['ollama', 'exo', 'kimi', 'codex']) {
            if (!backendOrder.includes(candidate)) backendOrder.push(candidate);
        }
        for (const backend of backendOrder) {
            const modelsForBackend = backends?.[backend]?.models || [];
            if (!modelsForBackend.length) continue;
            if (preferredConfiguredModel && modelsForBackend.includes(preferredConfiguredModel)) {
                return { backend, model: preferredConfiguredModel };
            }
            return { backend, model: modelsForBackend[0] };
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
        const currentIsDiscovered = Boolean(
            effectiveBackend
            && effectiveModel
            && (backends[effectiveBackend]?.models || []).includes(effectiveModel)
        );
        if (effectiveBackend && effectiveModel && !currentIsDiscovered) {
            const unavailableGroup = document.createElement('optgroup');
            unavailableGroup.label = 'Current selection';
            const unavailable = document.createElement('option');
            unavailable.value = `${effectiveBackend}:${effectiveModel}`;
            unavailable.textContent = `${effectiveModel} · temporarily unavailable`;
            unavailable.selected = true;
            unavailableGroup.appendChild(unavailable);
            selectEl.appendChild(unavailableGroup);
        }

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

    populateModelSelector(backends, currentBackend, currentModel, { unloaded = false } = {}) {
        this._populateSelectWithGroupedModels(this.modelSelector, backends, currentBackend, currentModel);
        // With no runtime loaded, the builder still falls back to a "preferred"
        // model so the list has a sensible default highlighted. That is fine
        // when a runtime exists and actively wrong when one doesn't: the
        // composer ends up naming a model nobody selected, directly
        // contradicting the banner telling the user to select one. Put an
        // explicit placeholder in front and select it.
        if (unloaded && this.modelSelector) {
            const placeholder = document.createElement('option');
            placeholder.value = '';
            placeholder.textContent = 'Select a model';
            placeholder.selected = true;
            this.modelSelector.prepend(placeholder);
            this.modelSelector.value = '';
        }
        this._refreshThinkingModeVisibility();
    }

    /** Show the thinking-mode selector for Ollama reasoning models
     * (deepseek-v*, glm-*). Other models hide it. */
    _refreshThinkingModeVisibility() {
        if (!this.thinkingModeSelector) return;
        const val = (this.modelSelector && this.modelSelector.value) || '';
        const colonIdx = val.indexOf(':');
        const backend = colonIdx > 0 ? val.substring(0, colonIdx) : '';
        const model = colonIdx > 0 ? val.substring(colonIdx + 1) : '';
        const profile = this._modelCapabilities || {};
        const matchingProfile = profile.model === model;
        const levels = (profile.reasoning_levels || []).map(level => level === 'medium' ? 'med' : level);
        const supports = ['ollama', 'kimi'].includes(backend) && (matchingProfile
            ? levels.length > 0 : /^(deepseek-v\d|glm-)/i.test(model || ''));
        for (const option of this.thinkingModeSelector.options) {
            option.disabled = matchingProfile && (option.value === 'off'
                ? profile.reasoning_can_disable === false
                : option.value !== 'default' && !levels.includes(option.value));
        }
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
        const value = mode || 'default';
        this.thinkingModeSelector.value = value;
        this._lastThinkingMode = value;
    }

    // ── Step Handling ────────────────────────────────────────────

    handleSessionStart(event) {
        this._setSessionActivity('working');
        // Show tool mode indicator for adaptive backends
        const toolMode = event.tool_mode || 'native';
        if (toolMode === 'text') {
            this.showStatusMessage(`⚡ Using text-based tool calling for ${event.model || 'this model'}`);
        }
        // Store tool mode for potential UI use
        this.currentToolMode = toolMode;
        this._startLiveRun(event);
        this._setLiveRunPhase('Reasoning', `Preparing ${event.model || 'the model'}`);
    }

    handleStepStart(event) {
        if (event._subagent) {
            const worker = event._agent_type || 'Sub-agent';
            this._setLiveRunPhase(
                'Delegating',
                `${worker} sub-agent is reasoning${event.step ? ` · step ${event.step}` : ''}`,
            );
            return;
        }
        this.currentStepEvent = event;
        this.stepToolCalls = [];
        this.stepToolResults = [];
        this.stepIsInlineOnly = true;
        this.stepRendered = false;
        this.isStreaming = false;
        this._currentStepHeaderEl = null;
        this._currentStepToolCounts = {};
        if ((event.step || 1) > 1) {
            this._advanceLiveMilestone('reason', 'Reason through the next agent step');
        }
        const liveContext = this._liveRun?.currentAction || '';
        this._setLiveRunPhase(
            event.step > 1 ? 'Continuing' : 'Reasoning',
            event.label || liveContext || `Agent step ${event.step || 1}`,
            event.step || 1,
        );

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
        const footerTarget = (this._activeTask && this._activeTask.footerEl) || this.chatMessages;
        if (this._activeTask && this._activeTask.footerEl) this._activeTask.footerEl.hidden = false;
        footerTarget.appendChild(el);
        t.footerEl = el;
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
        const output = String(resultEvent.output || '');

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
        if (output) {
            const preview = document.createElement('pre');
            preview.className = 'tool-evidence-output';
            preview.textContent = output.length > 8000
                ? output.slice(0, 8000) + '\n… output truncated in chat'
                : output;
            line.appendChild(preview);
            line.classList.add('has-output');
        }
        if (isError) {
            live.errorCount += 1;
            line.classList.add('is-error', 'show-output');
            const errorIcon = live.header.querySelector('.collapsed-icon');
            if (errorIcon) errorIcon.textContent = '\u25be';
            live.container.classList.add('has-errors', 'expanded');
            const icon = live.header.querySelector('.collapsed-icon');
            if (icon) icon.textContent = 'â–¾';
        }
        this._updateLiveCollapsedHeader();
    }

    /** Refresh the live group's summary label, step range, and call count. */
    _updateLiveCollapsedHeader() {
        if (!this._liveCollapsedGroup) return;
        const live = this._liveCollapsedGroup;
        const summaryEl = live.header.querySelector('.collapsed-summary');
        const metaEl = live.header.querySelector('.collapsed-meta');
        if (summaryEl) {
            const action = inferActionLabel(live.toolCounts);
            summaryEl.textContent = live.errorCount
                ? `Evidence · ${action} · ${live.errorCount} failed`
                : `Evidence · ${action}`;
            summaryEl.textContent = `◆ ${action}`;
        }
        if (summaryEl) {
            const action = inferActionLabel(live.toolCounts);
            summaryEl.textContent = live.errorCount
                ? `Evidence · ${action} · ${live.errorCount} failed`
                : `Evidence · ${action}`;
        }
        if (summaryEl) {
            const action = inferActionLabel(live.toolCounts);
            const parts = ['Evidence', action];
            if (live.errorCount) parts.push(`${live.errorCount} failed`);
            summaryEl.textContent = parts.join(' \u00b7 ');
        }
        if (metaEl) {
            const stepMeta = live.firstStep === live.lastStep
                ? `step ${live.firstStep}`
                : `steps ${live.firstStep}–${live.lastStep}`;
            metaEl.textContent = `${stepMeta} · ${live.callCount} call${live.callCount === 1 ? '' : 's'}`;
        }
    }

    // ── Text Streaming ──────────────────────────────────────────

    handleTextDelta(event) {
        this.removeThinking();
        this._advanceLiveMilestone('report', 'Communicate progress');
        const run = this._liveRun;
        if (run && run.provider === 'exo') {
            run.lastProgressAt = Date.now();
            run.lastTransportAt = Date.now();
        }
        const activeModel = run?.model || this.currentModelName || '';
        if (event._subagent) {
            this._setLiveRunPhase(
                'Delegating',
                `${event._agent_type || 'Sub-agent'} is preparing its handoff`,
            );
            this._handleSubagentTextDelta(event);
            return;
        } else {
            this._setLiveRunPhase(
                'Composing',
                activeModel
                    ? `Receiving output from ${activeModel}`
                    : 'Streaming the latest update',
            );
        }
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
        if (event._subagent) {
            this._handleSubagentTextDone(event);
            return;
        }
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

            let sanitized = false;
            if (typeof DOMPurify !== 'undefined') {
                html = DOMPurify.sanitize(html);
                sanitized = true;
            }

            // marked passes raw inline HTML straight through, and what lands
            // here is model output and tool output — including file contents
            // the agent just read. The sanitizer is therefore not optional.
            // It used to come from a CDN and now comes from static/vendor/,
            // which is absent when running from source without first running
            // packaging/fetch_web_assets.ps1. Degrade to plain text rather
            // than injecting unsanitized markup.
            if (sanitized) {
                contentEl.innerHTML = html;
            } else {
                contentEl.textContent = text;
            }

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

    // ── Tool Calls ──────────────────────────────────────────────

    handleToolCall(event) {
        this.removeThinking();
        const name = event.name || '';
        const callId = event.call_id || '';
        const nameLower = name.toLowerCase();
        const readTools = new Set(['file_read', 'glob', 'grep', 'git_status', 'git_diff']);
        const writeTools = new Set(['file_write', 'file_edit', 'apply_patch', 'git_commit']);
        const validationTools = new Set(['check_run']);
        if (readTools.has(nameLower)) {
            this._advanceLiveMilestone('inspect', 'Inspect the project');
        } else if (writeTools.has(nameLower)) {
            this._advanceLiveMilestone('change', 'Implement the changes');
        } else if (validationTools.has(nameLower)) {
            this._advanceLiveMilestone('verify', 'Running acceptance checks');
        } else if (nameLower === 'bash') {
            this._advanceLiveMilestone('command', 'Run a command');
        }
        const toolActivity = this._liveRunToolActivity(name, event.arguments || {});
        const run = this._liveRun;
        if (run && run.active) {
            const activity = {
                callId,
                name,
                active: toolActivity.active,
                completed: toolActivity.completed,
                startedAt: Date.now(),
            };
            run.activeTool = activity;
            run.currentAction = activity.active;
            if (callId) run.toolActivities.set(callId, activity);
        }
        this._setLiveRunPhase(
            event._subagent ? 'Delegating' : 'Using tools',
            event._subagent
                ? `${event._agent_type || 'Sub-agent'}: ${toolActivity.active}`
                : toolActivity.active,
        );
        if (event._subagent) {
            this.renderToolCall(event);
            return;
        }

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

        const renderKind = event.presentation?.kind || '';
        if (renderKind === 'edit' || renderKind === 'write' || name === 'file_edit' || name === 'file_write') {
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

        if (shouldGroupAsEvidence(name, event.arguments || {}) && this.stepIsInlineOnly) {
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
        if (event.metadata?.preview && !this.isReplaying) this.send({command: 'preview_list'});
        const check = event.metadata?.check;
        if (check && this._liveRun) {
            const run = this._liveRun;
            run.checks = run.checks || new Map();
            run.checks.set(JSON.stringify([check.requirement, check.command]), check);
            const failed = [...run.checks.values()].some(c => c.status !== 'passed');
            const item = run.milestones.find(m => m.id === 'verify');
            if (item) { item.status = failed ? 'error' : 'done'; item.text = failed ? 'Acceptance checks need attention' : 'Named checks passed'; }
        }
        const name = event.name || '';
        const callId = event.call_id || '';
        const nameLower = name.toLowerCase();
        const hasImage = event.image && event.image.data;

        // Remove from terminal bar (works for all backends and modes)
        if (!this.isReplaying && nameLower === 'bash' && callId) {
            this.trackTerminalEnd(callId);
        }

        const run = this._liveRun;
        if (run && run.active) {
            const activity = (callId && run.toolActivities.get(callId))
                || (run.activeTool?.name === name ? run.activeTool : null);
            const failed = Boolean(event.is_error || event.denied);
            const elapsed = Number(event.elapsed || 0);
            const completedText = activity?.completed
                || this._liveRunToolActivity(name, {}).completed;
            run.completedTools += 1;
            run.lastCompleted = {
                text: failed ? `${completedText} — needs attention` : completedText,
                elapsed,
                failed,
            };
            const activeText = activity?.active || name || 'the tool';
            const activePhrase = `${activeText.charAt(0).toLowerCase()}${activeText.slice(1)}`;
            run.currentAction = failed
                ? `Investigating why ${activePhrase} failed`
                : `Reviewing the result of ${activePhrase}`;
            if (callId) run.toolActivities.delete(callId);
            if (!callId || run.activeTool?.callId === callId || run.activeTool?.name === name) {
                run.activeTool = null;
            }
            this._setLiveRunPhase(
                failed ? 'Recovering' : (event._subagent ? 'Delegating' : 'Reasoning'),
                event._subagent
                    ? `${event._agent_type || 'Sub-agent'}: ${run.currentAction}`
                    : run.currentAction,
            );
        }
        if (event._subagent) {
            this.renderToolResult(event);
            return;
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

        const liveOwnsResult = Boolean(
            this._liveCollapsedGroup && (
                (callId && this._liveCollapsedGroup.callIdToItem.has(callId))
                || (this._liveCollapsedGroup._lastItemTool === name)
            )
        );
        if (this.stepIsInlineOnly && liveOwnsResult) {
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
        const agentId = String(this._activeRenderEvent?._agent_id || '');
        const parallelTarget = agentId ? this.subagentContainers.get(agentId) : null;
        if (parallelTarget) return parallelTarget;
        if (this.subagentContainer) return this.subagentContainer;
        const task = this._ensureTaskCard('Task');
        return (task && task.activityEl) || this.chatMessages;
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

    _screenshotGallery(target) {
        let details = Array.from(target.children || []).find((child) =>
            child.classList?.contains('screenshot-gallery')
        );
        if (details) return details;

        details = document.createElement('details');
        details.className = 'screenshot-gallery';
        details.innerHTML = `
            <summary>
                <span class="screenshot-gallery-caret" aria-hidden="true"></span>
                <span class="screenshot-gallery-title">Screenshots</span>
                <span class="screenshot-gallery-count">0</span>
                <span class="screenshot-gallery-hint">Open gallery</span>
            </summary>
            <div class="screenshot-gallery-grid"></div>
        `;
        // Deliberately omit the `open` attribute. Screenshots are evidence,
        // while the assistant's succinct result is the primary handoff.
        target.appendChild(details);
        return details;
    }

    renderScreenshotImage(base64Data, mediaType, toolName) {
        const category = toolName.startsWith('computer_') ? 'desktop' : '';
        const target = this.getRenderTarget();
        const gallery = this._screenshotGallery(target);
        const grid = gallery.querySelector('.screenshot-gallery-grid');
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
        grid.appendChild(container);

        const count = grid.childElementCount;
        const countEl = gallery.querySelector('.screenshot-gallery-count');
        if (countEl) countEl.textContent = `${count} image${count === 1 ? '' : 's'}`;

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
        const isContext = pane === 'context';
        const isBrowser = !isPlan && !isContext;
        document.querySelectorAll('.preview-tab[data-pane]').forEach((t) => {
            t.classList.toggle('active', t.dataset.pane === pane);
        });
        const browserChrome = document.querySelector('#preview-panel .preview-chrome');
        const browserViewport = document.getElementById('preview-viewport');
        const browserConsole = document.getElementById('preview-console');
        const planPane = document.getElementById('plan-graph-pane');
        if (browserChrome) browserChrome.style.display = isBrowser ? '' : 'none';
        if (browserViewport) browserViewport.style.display = isBrowser ? '' : 'none';
        if (browserConsole) browserConsole.style.display = isBrowser ? '' : 'none';
        if (planPane) planPane.style.display = isPlan ? 'flex' : 'none';
        const contextPane = document.getElementById('context-cockpit-pane');
        if (contextPane) contextPane.style.display = isContext ? 'flex' : 'none';

        // C2 — track the active pane so plan-event handlers can decide
        // whether to flash the unread-update indicator. Switching TO the
        // plan pane clears any pending indicator.
        this._currentPreviewPane = pane;
        if (isPlan) this._clearPlanTabUnread();
        if (isContext) {
            this.send({ command: 'get_context_state' });
            this.send({ command: 'context_catalog' });
        }
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

    _markAgentTabUnread() {
        if (this._currentPreviewPane === 'agents') return;
        document.querySelector('.preview-tab[data-pane="agents"]')?.classList.add('has-unread');
    }

    _clearAgentTabUnread() {
        document.querySelector('.preview-tab[data-pane="agents"]')?.classList.remove('has-unread');
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
        this.agentActivities.clear();
        this.agentActivityOrder = [];
        this.agentActivityStack = [];
        this.contextState = null;
        this.runtimeAgents = [];
        this.runtimeTimeline = [];
        this.runtimeTraces = [];
        this.runtimeArtifacts = [];
        this.runtimePacks = [];
        this.renderAgentActivityTree();
        this.renderContextCockpit();

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
        this._syncComposerGutter?.();
        if (this.settingsView) this.settingsView.style.display = 'none';

        const sessionList = document.getElementById('agent-list');
        if (sessionList) sessionList.style.display = viewName === 'agents' ? '' : 'none';

        document.querySelectorAll('.sidebar-icon-btn[data-view], .sidebar-nav-item[data-view], .sidebar-action[data-view], .rail-btn[data-view]').forEach(el =>
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
                    this._syncComposerGutter?.();
                } else {
                    this.welcomeScreen.style.display = 'flex';
                }
                break;
            case 'settings':
                this.settingsView.style.display = 'flex';
                this.send({ command: 'get_costs' });
                this.send({ command: 'evaluation_list' });
                this.send({ command: 'checkpoint_list' });
                if (!this.settings || !Object.keys(this.settings).length) {
                    this.send({ command: 'get_settings' });
                } else {
                    this.renderSettingsView();
                }
                break;
        }
    }

    // ── Settings View ────────────────────────────────────────────

    _formatUsageTokens(value) {
        const tokens = Math.max(0, Number(value) || 0);
        if (tokens >= 1_000_000) return `${(tokens / 1_000_000).toFixed(tokens >= 10_000_000 ? 1 : 2)}M`;
        if (tokens >= 1_000) return `${(tokens / 1_000).toFixed(tokens >= 100_000 ? 0 : 1)}K`;
        return Math.round(tokens).toLocaleString();
    }

    _formatUsageCost(value) {
        const cost = Math.max(0, Number(value) || 0);
        if (cost > 0 && cost < 0.01) return `$${cost.toFixed(4)}`;
        return `$${cost.toFixed(2)}`;
    }

    _renderCostDashboard(data) {
        const costs = this.costData;
        const enabled = data.enabled !== false;
        const budget = Math.max(0, Number(data.budget_alert_usd) || 0);
        const controls = `
            <div class="settings-row">
                <span class="settings-row-label">Enable cost tracking</span>
                <div class="settings-row-value">
                    <label class="cost-tracking-toggle"><input type="checkbox" ${enabled ? 'checked' : ''} data-section="cost_tracking" data-key="enabled" /> ${enabled ? 'On' : 'Off'}</label>
                </div>
            </div>
            <div class="settings-row">
                <span class="settings-row-label">Daily budget alert ($)</span>
                <div class="settings-row-value">
                    <input class="settings-input" type="number" min="0" step="0.01" value="${budget || ''}" data-section="cost_tracking" data-key="budget_alert_usd" placeholder="None" />
                    <div class="settings-row-hint">Shows an alert after tracked daily spend crosses this amount.</div>
                </div>
            </div>
        `;
        if (!costs) {
            return `${controls}<div class="cost-dashboard-loading"><span></span>Loading usage history&hellip;</div>`;
        }

        const emptyUsage = { input_tokens: 0, output_tokens: 0, cost_usd: 0 };
        const today = costs.today || emptyUsage;
        const session = costs.session || emptyUsage;
        const dailyEntries = Object.entries(costs.daily || {}).sort(([a], [b]) => b.localeCompare(a));
        const total = costs.total || dailyEntries.reduce((sum, [, value]) => ({
            input_tokens: sum.input_tokens + Number(value.input_tokens || 0),
            output_tokens: sum.output_tokens + Number(value.output_tokens || 0),
            cost_usd: sum.cost_usd + Number(value.cost_usd || 0),
        }), { ...emptyUsage });
        const totalTokens = item => Number(item.input_tokens || 0) + Number(item.output_tokens || 0);
        const statCard = (label, item, detail) => `
            <article class="cost-stat-card">
                <span class="cost-stat-label">${label}</span>
                <strong>${this._formatUsageCost(item.cost_usd)}</strong>
                <span class="cost-stat-tokens">${this._formatUsageTokens(totalTokens(item))} tokens</span>
                <small>${this._formatUsageTokens(item.input_tokens)} in &middot; ${this._formatUsageTokens(item.output_tokens)} out${detail ? ` &middot; ${detail}` : ''}</small>
            </article>
        `;
        const budgetPercent = budget > 0 ? Math.min(100, (Number(today.cost_usd || 0) / budget) * 100) : 0;
        const budgetHtml = budget > 0 ? `
            <div class="cost-budget" aria-label="Daily budget usage">
                <div><span>Daily alert usage</span><strong>${Math.round(budgetPercent)}% of ${this._formatUsageCost(budget)}</strong></div>
                <div class="cost-budget-track"><span style="width:${budgetPercent}%"></span></div>
            </div>
        ` : '';
        const historyRows = dailyEntries.slice(0, 14).map(([day, item]) => `
            <div class="cost-history-row">
                <time datetime="${this.escapeHtml(day)}">${this.escapeHtml(day)}</time>
                <span>${this._formatUsageTokens(item.input_tokens)}</span>
                <span>${this._formatUsageTokens(item.output_tokens)}</span>
                <strong>${this._formatUsageCost(item.cost_usd)}</strong>
            </div>
        `).join('');

        return `
            ${controls}
            <div class="cost-dashboard${enabled ? '' : ' is-paused'}">
                <div class="cost-dashboard-head">
                    <div><strong>Usage overview</strong><span>${enabled ? 'Tracking active' : 'Tracking paused; history is preserved'}</span></div>
                    <button type="button" class="btn-sm cost-refresh-btn">Refresh</button>
                </div>
                <div class="cost-stat-grid">
                    ${statCard('Today', today, '')}
                    ${statCard('Current session', session, '')}
                    ${statCard('Tracked total', total, `${dailyEntries.length} day${dailyEntries.length === 1 ? '' : 's'}`)}
                </div>
                ${budgetHtml}
                <div class="cost-history">
                    <div class="cost-history-title"><strong>Recent daily usage</strong><span>Input</span><span>Output</span><span>Cost</span></div>
                    ${historyRows || '<div class="cost-history-empty">No token usage has been recorded yet.</div>'}
                </div>
                <p class="cost-dashboard-note">Local and Ollama-hosted models still report tokens when available, but show $0 unless a per-token price is configured.</p>
            </div>
        `;
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

        const menuButton = document.querySelector('.titlebar-menu-button');
        const appMenu = document.querySelector('.titlebar-menus');
        const closeAppMenu = () => {
            if (!appMenu) return;
            appMenu.classList.remove('is-open');
            menuButton?.setAttribute('aria-expanded', 'false');
        };
        menuButton?.addEventListener('click', (e) => {
            e.stopPropagation();
            const isOpen = appMenu?.classList.toggle('is-open') || false;
            menuButton.setAttribute('aria-expanded', isOpen ? 'true' : 'false');
        });
        document.addEventListener('click', (e) => {
            if (!appMenu || !appMenu.classList.contains('is-open')) return;
            if (appMenu.contains(e.target) || menuButton?.contains(e.target)) return;
            closeAppMenu();
        });
        document.addEventListener('keydown', (e) => {
            if (e.key === 'Escape') closeAppMenu();
        });

        document.querySelectorAll('.menubar-action[data-action]').forEach(el => {
            el.addEventListener('click', () => {
                const action = el.dataset.action;
                switch (action) {
                    case 'new-agent': document.getElementById('new-agent-btn')?.click(); break;
                    case 'open-folder': this.openProjectFolder(); break;
                    case 'project-switch': this._openProjectSwitcher(this.sidebarProjectSwitch || document.getElementById('footer-project-btn')); break;
                    case 'settings': this.switchView('settings'); break;
                    case 'cmd-palette': this.openCommandPalette(); break;
                    case 'toggle-sidebar': document.getElementById('sidebar-toggle')?.click(); break;
                    case 'toggle-preview': document.getElementById('preview-toggle')?.click(); break;
                    case 'shortcuts': this.toggleShortcutsOverlay(); break;
                    case 'check-updates':
                        this.showStatusMessage('Checking for updates...');
                        this.send({ command: 'check_updates' });
                        break;
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
                    case 'about': this.showStatusMessage('Resonant Client - local-first multimodal coding agent'); break;
                }
                closeAppMenu();
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

    // ── Command Palette ─────────────────────────────────────────

    _cmdPaletteCommands() {
        return [
            { id: 'new-agent',  icon: '+', label: 'New session',        hint: 'Ctrl+N',       action: () => document.getElementById('new-agent-btn')?.click() },
            { id: 'settings',   icon: '\u2699', label: 'Open Settings',            hint: 'Ctrl+,', action: () => this.switchView('settings') },
            { id: 'sessions',   icon: '\u2190', label: 'Back to Sessions',         hint: 'Alt+1',  action: () => this.switchView('agents') },
            { id: 'git',        icon: '\u2387', label: 'Git changes & commits',    hint: '',       action: () => { if (this.gitData?.is_repo) this.toggleGitPopover(); else this.showStatusMessage('Not a git repository.'); } },
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
            this._setSystemStatus('connected', this.lastModel);
        }
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
        this._setLiveRunTodos(raw, done, total);
        this._syncLiveAgentTodoStrip(done, total, raw);
    }

    _syncLiveAgentTodoStrip(done, total, items) {
        if (this._liveRun && this._liveRun.active) return;
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
        const presented = Array.isArray(event.presentation?.locations)
            ? event.presentation.locations
            : [];
        const paths = (presented.length ? presented : [args.path])
            .map((value) => String(value || '').replace(/\\/g, '/').trim())
            .filter((value, index, values) => value && values.indexOf(value) === index);
        if (!paths.length) return;

        let detail = '';
        const renderKind = event.presentation?.kind || '';
        if (name === 'file_write' || renderKind === 'write') {
            const lines = String(args.content || '').split('\n').length;
            detail = lines ? `Wrote ${lines} line${lines === 1 ? '' : 's'}` : 'Wrote file';
        } else if (name === 'file_edit' || renderKind === 'edit') {
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
        for (const path of paths) {
            const idx = list.findIndex(c => c.path === path);
            const entry = { path, tool: name, detail };
            if (idx >= 0) list[idx] = entry;
            else list.push(entry);
        }
    }

    _handleSubagentTextDelta(event) {
        const agentId = String(event._agent_id || '');
        if (!agentId) return;
        let state = this.subagentStreams.get(agentId);
        if (!state) {
            state = { buffer: '', element: this.addAssistantMessage(), timer: null };
            this.subagentStreams.set(agentId, state);
        }
        state.buffer += event.delta || '';
        if (state.timer) return;
        state.timer = setTimeout(() => {
            state.timer = null;
            if (state.element?.isConnected) {
                this.renderMarkdown(state.element, state.buffer, true);
            }
        }, 80);
    }

    _handleSubagentTextDone(event) {
        const agentId = String(event._agent_id || '');
        if (!agentId) return;
        let state = this.subagentStreams.get(agentId);
        const finalText = String(event.text || state?.buffer || '').trim();
        if (!state && finalText) {
            state = { buffer: finalText, element: this.addAssistantMessage(), timer: null };
        }
        if (!state) return;
        if (state.timer) clearTimeout(state.timer);
        state.timer = null;
        state.buffer = finalText;
        this.renderMarkdown(state.element, finalText);
        state.element.querySelector('.message-content')?.classList.remove('streaming-cursor');
        this.subagentStreams.delete(agentId);
    }

    _openWorkspacePath(path) {
        const value = String(path || '').trim();
        if (!value) return;
        this.send({ command: 'open_workspace_path', path: value });
        this.showStatusMessage(`Opening ${this.shortenPath(value)}â€¦`);
    }

    _selectAlternateModelValue() {
        if (!this.modelSelector) return false;
        const current = this.modelSelector.value || '';
        const options = Array.from(this.modelSelector.options || []);
        const currentBackend = current.split(':', 1)[0];
        const target = options.find((option) =>
            option.value !== current && option.value.split(':', 1)[0] === currentBackend
        ) || options.find((option) => option.value !== current);
        if (!target) return false;
        this.modelSelector.value = target.value;
        this.modelSelector.dispatchEvent(new Event('change'));
        return true;
    }

    _collapseTaskActivity(event = {}) {
        const task = this._activeTask;
        const activity = task && task.activityEl;
        // Live progress is transient. Keep only the concrete work rows in the
        // completed disclosure so expanding Activity never reveals an empty,
        // faded run dashboard.
        activity?.querySelector(':scope > .live-run-surface')?.remove();
        if (!activity || activity.children.length === 0) return;
        if (activity.querySelector(':scope > .task-activity-details')) return;

        const t = this._currentTurn || {};
        const steps = event.total_steps || t.stepCount || 0;
        const tools = t.toolCallCount || 0;
        const pieces = [];
        const actions = tools || steps;
        if (actions > 0) pieces.push(`${actions} action${actions === 1 ? '' : 's'}`);
        const elapsed = Number(event.total_elapsed || t.totalElapsed || 0);
        const activityTitle = elapsed > 0
            ? `Worked for ${this._formatRunDuration(elapsed)}`
            : 'Work details';

        const details = document.createElement('details');
        details.className = 'task-activity-details';
        const summary = document.createElement('summary');
        summary.innerHTML = `
            <span class="task-activity-caret" aria-hidden="true"></span>
            <span class="task-activity-title">${this.escapeHtml(activityTitle)}</span>
            ${pieces.length ? `<span class="task-activity-meta">${this.escapeHtml(pieces.join(' · '))}</span>` : ''}
        `;
        details.appendChild(summary);
        while (activity.firstChild) {
            details.appendChild(activity.firstChild);
        }
        activity.appendChild(details);
    }

    _finishActiveTask(event = {}) {
        if (!this._activeTask) return;
        this._completeLiveRun(!!this._agentRunErrored);
        this._renderTaskCompletionSummary(event);
        this._collapseTaskActivity(event);
        this._setActiveTask(null);
    }

    handleSessionEnd(event) {
        const finishedTask = this._activeTask;
        this.removeThinking();
        this._removeLiveAgentTodoStrip();

        // Clear terminal bar
        this.clearTerminals();

        // Finalize CLI tool activity group
        this.finalizeToolActivityGroup();

        // Flush collapsed group
        this.flushCollapsedGroup();

        if (this._cancelInFlight || this._cancelInterrupted) {
            this._finishCancelledTask();
            this._cancelInterrupted = false;
            this.scrollToBottom();
            return;
        }

        const totalElapsed = event.total_elapsed || 0;
        const totalSteps = event.total_steps || 0;

        // Per-turn footer — single dim line below the assistant's prose,
        // replaces the per-step "▣ model · tokens · 1.2s" footer that used
        // to repeat after every step.
        this._renderTurnFooter();
        this._finishActiveTask(event);
        this.setRunning(false);
        this.scrollToBottom();

        if (!this.isReplaying) {
            this.requestGitStatus();
        }
        const emptyFailure = event.outcome === 'failed'
            && Number(event.evidence?.empty_response_attempts || 0) >= 3;
        if (
            !this.isReplaying
            && emptyFailure
            && this.permissionMode === 'bypass'
            && this._autoFallbackDepth === 0
        ) {
            this._autoFallbackDepth = 1;
            this.showStatusMessage('Primary model returned empty responses; retrying once with an alternate model.');
            setTimeout(() => {
                this._retryTask(finishedTask, { mode: 'retry', alternate: true, auto: true });
            }, 250);
        }
        return;

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
                this._syncComposerGutter?.();
                this.sendMessage();
            });
        });
        this.chatMessages.appendChild(el);
    }

    // ── Subagents ───────────────────────────────────────────────

    handleCompression(event) {
        const target = this.getRenderTarget?.() || this.chatMessages;
        if (target) {
            const banner = document.createElement('div');
            banner.className = 'compression-banner';
            banner.innerHTML = `
                <span>Context compressed</span>
                <span>${Number(event.old_tokens || 0).toLocaleString()} → ${Number(event.new_tokens || 0).toLocaleString()} est. tokens</span>
            `;
            target.appendChild(banner);
            this.scrollToBottom();
        }
        this.send({ command: 'get_context_state' });
    }

    renderContextCockpit() {
        const body = document.getElementById('context-cockpit-body');
        if (!body) return;
        const state = this.contextState;
        if (!state) {
            body.innerHTML = '<div class="agent-activity-empty">Context telemetry appears when a session starts.</div>';
            return;
        }
        const total = Number(state.estimated_total_tokens || 0);
        const windowTokens = Number(state.context_window || 0);
        const threshold = Number(state.compression_threshold || 0);
        const utilization = Math.max(0, Math.min(1, Number(state.utilization || 0)));
        const pct = Math.round(utilization * 100);
        const history = state.history || {};
        const prompt = state.system_prompt || {};
        const roles = Object.entries(history.role_tokens || {}).map(([role, tokens]) =>
            `<span>${this.escapeHtml(role)} <strong>${Number(tokens).toLocaleString()}</strong></span>`
        ).join('');
        const sources = Object.entries(state.sources || {}).map(([name, data]) =>
            `<span>${this.escapeHtml(name)} <strong>${Number(data.estimated_tokens || 0).toLocaleString()}</strong></span>`
        ).join('');
        const layers = (prompt.layers || []).map(layer => `
            <div class="context-list-row"><span>${this.escapeHtml(layer.label || layer.id)}</span><strong>${Number(layer.estimated_tokens || 0).toLocaleString()}</strong></div>
        `).join('');
        const payloads = (state.largest_tool_payloads || []).map(item => `
            <div class="context-list-row"><span>${this.escapeHtml(item.name || 'tool')} <small>#${item.index}</small></span><strong>${Number(item.estimated_tokens || 0).toLocaleString()}</strong></div>
        `).join('');
        const todos = state.todos || [];
        const providers = (this.contextProviders || []).map(item =>
            `<button class="context-provider-chip" type="button" data-syntax="${this.escapeHtml(item.syntax || '')}" title="Insert into the composer">${this.escapeHtml(item.syntax || item.name)}</button>`
        ).join('');
        body.innerHTML = `
            <div class="context-hero">
                <div><strong>${total.toLocaleString()}</strong><span>estimated tokens</span></div>
                <div><strong>${windowTokens.toLocaleString()}</strong><span>effective window</span></div>
                <div><strong>${Number(state.compression_count || 0)}</strong><span>compressions</span></div>
            </div>
            <div class="context-meter ${pct >= 75 ? 'is-warning' : ''}"><span style="width:${pct}%"></span></div>
            <div class="context-meter-label"><span>${pct}% used</span><span>compress near ${threshold.toLocaleString()}</span></div>
            <section class="context-card">
                <h4>Composition</h4>
                <div class="context-stat-chips">
                    <span>system <strong>${Number(prompt.estimated_tokens || 0).toLocaleString()}</strong></span>
                    <span>history <strong>${Number(history.estimated_tokens || 0).toLocaleString()}</strong></span>
                    ${sources}
                </div>
                <div class="context-stat-chips">${roles || '<span>No history entries</span>'}</div>
            </section>
            <section class="context-card"><h4>Prompt layers</h4>${layers || '<div class="context-empty-row">No prompt layers</div>'}</section>
            <section class="context-card"><h4>Largest tool payloads</h4>${payloads || '<div class="context-empty-row">No tool results</div>'}</section>
            <section class="context-card"><h4>Durable task state</h4><div class="context-empty-row">${todos.length ? `${todos.filter(item => item.done).length}/${todos.length} todos complete` : 'No active todo ledger'}</div></section>
            <section class="context-card"><h4>Explicit attachments</h4><div class="context-provider-list">${providers || '<span class="context-empty-row">No providers available</span>'}</div><p class="context-provider-help">Insert a provider, replace <code>selector</code>, and send. Attachments carry provenance into the model context.</p></section>
        `;
        body.querySelectorAll('.context-provider-chip').forEach((button) => button.addEventListener('click', () => {
            const syntax = button.dataset.syntax || '';
            this.userInput.value = `${this.userInput.value}${this.userInput.value ? ' ' : ''}${syntax}`;
            this.userInput.focus();
            this._syncComposerGutter?.();
        }));
    }

    trackPlanAgentEvent(event) {
        const wrapped = event.event_payload || event;
        const payload = wrapped.payload || {};
        const nodeId = wrapped.node_id || '';
        if (!nodeId || !['node.start', 'node.done'].includes(wrapped.kind)) return;
        const id = `specialist:${event.intent_id || 'intent'}:${nodeId}`;
        const existing = this.agentActivities.get(id) || {
            id,
            kind: 'specialist',
            label: payload.specialization || 'specialist',
            prompt: payload.goal || '',
            parentId: '',
            startedAt: (wrapped.ts || Date.now() / 1000) * 1000,
        };
        if (!this.agentActivities.has(id)) this.agentActivityOrder.push(id);
        if (wrapped.kind === 'node.start') {
            existing.status = 'running';
            existing.label = payload.specialization || existing.label;
            existing.prompt = payload.goal || existing.prompt;
        } else {
            existing.status = payload.status || 'done';
            existing.finishedAt = (wrapped.ts || Date.now() / 1000) * 1000;
            existing.handoff = payload.summary || payload.error || '';
            existing.confidence = payload.confidence;
            existing.verdict = payload.verdict || '';
        }
        this.agentActivities.set(id, existing);
        this.renderAgentActivityTree();
        this._markAgentTabUnread();
    }

    switchRuntimeView(view) {
        this.runtimeView = view || 'agents';
        document.querySelectorAll('.runtime-view-tab').forEach((button) => {
            button.classList.toggle('active', button.dataset.runtimeView === this.runtimeView);
        });
        this.refreshRuntimeView();
    }

    refreshRuntimeView() {
        const commands = {
            agents: 'agent_runtime_list',
            timeline: 'session_timeline_list',
            traces: 'flight_recorder_list',
            artifacts: 'artifact_list',
            packs: 'capability_pack_list',
        };
        this.send({ command: commands[this.runtimeView] || commands.agents });
        this.renderRuntimeView();
    }

    upsertRuntimeAgent(agent) {
        const index = this.runtimeAgents.findIndex((item) => item.id === agent.id);
        if (index >= 0) this.runtimeAgents[index] = agent;
        else this.runtimeAgents.unshift(agent);
        this.syncRuntimeAgents();
    }

    syncRuntimeAgents() {
        this.runtimeAgents.forEach((agent) => {
            const existing = this.agentActivities.get(agent.id) || {};
            if (!this.agentActivities.has(agent.id)) this.agentActivityOrder.push(agent.id);
            this.agentActivities.set(agent.id, {
                ...existing,
                id: agent.id,
                kind: agent.role || 'agent',
                label: agent.agent_type || agent.role || 'agent',
                prompt: agent.prompt || '',
                parentId: agent.parent_id || '',
                status: agent.status || 'queued',
                startedAt: Number(agent.created_at || 0) * 1000,
                finishedAt: ['completed', 'failed', 'cancelled', 'stuck'].includes(agent.status)
                    ? Number(agent.updated_at || 0) * 1000 : 0,
                steps: agent.steps,
                handoff: agent.handoff ? JSON.stringify(agent.handoff, null, 2) : agent.error || '',
                runtime: agent,
            });
        });
    }

    renderRuntimeView() {
        const tree = document.getElementById('agent-activity-tree');
        const count = document.getElementById('agent-activity-count');
        if (!tree) return;
        if (this.runtimeView === 'agents') {
            this.renderAgentActivityTree();
            return;
        }
        const collections = {
            timeline: this.runtimeTimeline,
            traces: this.runtimeTraces,
            artifacts: this.runtimeArtifacts,
            packs: this.runtimePacks,
        };
        const items = collections[this.runtimeView] || [];
        if (count) count.textContent = `${items.length} ${this.runtimeView}`;
        if (!items.length) {
            tree.innerHTML = `<div class="agent-activity-empty">No ${this.escapeHtml(this.runtimeView)} recorded yet.</div>`;
            return;
        }
        if (this.runtimeView === 'timeline') {
            tree.innerHTML = items.map((item) => `
                <article class="runtime-card" data-checkpoint-id="${this.escapeHtml(item.id)}">
                    <div><strong>${this.escapeHtml(item.reason || item.id)}</strong><small>#${Number(item.sequence || 0)} · ${new Date(Number(item.created_at || 0) * 1000).toLocaleString()}</small></div>
                    <span>${this.escapeHtml(item.tool_name || (item.workspace_ref ? 'git snapshot' : 'archive snapshot'))}</span>
                    <div class="runtime-actions"><button data-action="compare">Compare</button><button data-action="conversation">Restore chat</button><button data-action="files">Restore files</button><button data-action="both">Restore both</button></div>
                </article>`).join('');
            tree.querySelectorAll('.runtime-card').forEach((card) => {
                card.querySelectorAll('[data-action]').forEach((button) => button.addEventListener('click', () => {
                    const action = button.dataset.action;
                    if (action === 'compare') this.send({ command: 'session_timeline_compare', checkpoint_id: card.dataset.checkpointId });
                    else this.send({ command: 'session_timeline_restore', checkpoint_id: card.dataset.checkpointId, mode: action });
                }));
            });
            return;
        }
        if (this.runtimeView === 'traces') {
            tree.innerHTML = items.map((item) => `
                <article class="runtime-card" data-run-id="${this.escapeHtml(item.run_id)}">
                    <div><strong>${this.escapeHtml(item.model || item.run_id)}</strong><small>${this.escapeHtml(item.model_role || 'primary')} · ${this.escapeHtml(item.status || '')}</small></div>
                    <span>${new Date(Number(item.updated_at || 0) * 1000).toLocaleString()}</span>
                    <div class="runtime-actions"><button data-action="inspect">Inspect</button><button data-action="export">Export OTLP</button></div>
                </article>`).join('');
            tree.querySelectorAll('.runtime-card').forEach((card) => {
                card.querySelector('[data-action="inspect"]')?.addEventListener('click', () => this.send({ command: 'flight_recorder_detail', run_id: card.dataset.runId }));
                card.querySelector('[data-action="export"]')?.addEventListener('click', () => this.send({ command: 'flight_recorder_export', run_id: card.dataset.runId }));
            });
            return;
        }
        if (this.runtimeView === 'artifacts') {
            tree.innerHTML = items.map((item) => `
                <article class="runtime-card">
                    <div><strong>${this.escapeHtml(item.label || item.id)}</strong><small>${this.escapeHtml(item.kind || '')} · ${Number(item.size || 0).toLocaleString()} bytes</small></div>
                    <span title="${this.escapeHtml(item.path || '')}">${this.escapeHtml((item.path || '').split(/[\\/]/).pop() || '')}</span>
                </article>`).join('');
            return;
        }
        tree.innerHTML = items.map((item) => `
            <article class="runtime-card">
                <div><strong>${this.escapeHtml(item.name || item.id)}</strong><small>v${this.escapeHtml(item.version || '0.0.0')}</small></div>
                <span>${this.escapeHtml(item.description || '')}</span>
                <div class="runtime-badges"><b class="${item.enabled ? 'is-on' : ''}">${item.enabled ? 'enabled' : 'disabled'}</b><b class="${item.trusted ? 'is-on' : ''}">${item.trusted ? 'trusted' : 'untrusted'}</b><b>${(item.agents || []).length} agents</b><b>${(item.skills || []).length} skills</b></div>
            </article>`).join('');
    }

    showRuntimePayload(title, payload) {
        const detail = document.getElementById('agent-handoff-detail');
        if (!detail) return;
        detail.innerHTML = `<div class="agent-handoff-header"><strong>${this.escapeHtml(title)}</strong><button class="agent-handoff-close" type="button">×</button></div><pre>${this.escapeHtml(JSON.stringify(payload, null, 2))}</pre>`;
        detail.style.display = 'block';
        detail.querySelector('.agent-handoff-close')?.addEventListener('click', () => { detail.style.display = 'none'; });
    }

    showRuntimeAgentDetail(agent, transcript) {
        if (!agent) return;
        this.showRuntimePayload(agent.agent_type || 'Agent', { agent, transcript });
        const detail = document.getElementById('agent-handoff-detail');
        const controls = document.createElement('div');
        controls.className = 'runtime-control-bar';
        controls.innerHTML = `<button data-action="pause">Pause</button><button data-action="resume">Resume</button><button data-action="cancel" class="is-danger">Cancel</button><input aria-label="Steer agent" placeholder="Add direction without cancelling"><button data-action="steer">Steer</button>`;
        // A terminal agent has no live thread behind it. Offering Pause /
        // Resume / Steer there invites a control that can only ever come back
        // as an error — and `resume` in particular used to flip a restart-
        // orphaned agent to "running", leaving the UI waiting on a worker that
        // no longer existed.
        const TERMINAL_STATUSES = ['completed', 'failed', 'cancelled', 'stuck'];
        const status = String(agent.status || '');
        if (TERMINAL_STATUSES.includes(status)) {
            controls.querySelectorAll('button, input').forEach((element) => {
                element.disabled = true;
                element.title = `Agent is ${status} — no live run to control.`;
            });
            // Restart is the one control that DOES apply to a dead worker: its
            // assignment outlived its thread. Not offered for `completed` —
            // there is nothing to redo.
            if (status !== 'completed') {
                const restart = document.createElement('button');
                restart.type = 'button';
                restart.dataset.action = 'restart';
                restart.textContent = 'Restart';
                restart.title = 'Re-run this agent from its saved assignment.';
                restart.addEventListener('click', () => {
                    restart.disabled = true;
                    restart.textContent = 'Restarting…';
                    this.send({ command: 'agent_restart', agent_id: agent.id });
                });
                controls.appendChild(restart);
            }
        }
        detail?.appendChild(controls);
        controls.querySelectorAll('button').forEach((button) => button.addEventListener('click', () => {
            const input = controls.querySelector('input');
            this.send({ command: 'agent_runtime_control', agent_id: agent.id, action: button.dataset.action, text: input?.value || '' });
            if (button.dataset.action === 'steer' && input) input.value = '';
        }));
    }

    renderAgentActivityTree() {
        if (this.runtimeView !== 'agents') {
            this.renderRuntimeView();
            return;
        }
        const tree = document.getElementById('agent-activity-tree');
        const count = document.getElementById('agent-activity-count');
        const badge = document.getElementById('agents-tab-badge');
        if (!tree) return;
        const activities = this.agentActivityOrder
            .map(id => this.agentActivities.get(id))
            .filter(Boolean);
        if (count) count.textContent = `${activities.length} worker${activities.length === 1 ? '' : 's'}`;
        if (badge) {
            badge.textContent = String(activities.filter(item => item.status === 'running').length || activities.length);
            badge.style.display = activities.length ? '' : 'none';
        }
        if (!activities.length) {
            tree.innerHTML = '<div class="agent-activity-empty">Sub-agents and specialists will appear here.</div>';
            const detail = document.getElementById('agent-handoff-detail');
            if (detail) detail.style.display = 'none';
            return;
        }
        tree.innerHTML = activities.map(item => {
            const depth = item.parentId ? 1 : 0;
            const elapsed = item.finishedAt && item.startedAt
                ? `${((item.finishedAt - item.startedAt) / 1000).toFixed(1)}s`
                : item.status === 'running' ? 'live' : '';
            return `
                <button class="agent-activity-node status-${this.escapeHtml(item.status || 'queued')}" data-activity-id="${this.escapeHtml(item.id)}" style="--agent-depth:${depth}">
                    <span class="agent-activity-state"></span>
                    <span class="agent-activity-main">
                        <strong>${this.escapeHtml(item.label || item.kind || 'worker')}</strong>
                        <small>${this.escapeHtml((item.prompt || '').slice(0, 90) || item.kind || '')}</small>
                    </span>
                    <span class="agent-activity-elapsed">${this.escapeHtml(elapsed)}</span>
                </button>
            `;
        }).join('');
        tree.querySelectorAll('.agent-activity-node').forEach(node => {
            node.addEventListener('click', () => {
                const item = this.agentActivities.get(node.dataset.activityId);
                if (item?.runtime) this.send({ command: 'agent_runtime_detail', agent_id: item.runtime.id });
                else this.showAgentHandoff(node.dataset.activityId);
            });
        });
    }

    showAgentHandoff(id) {
        const item = this.agentActivities.get(id);
        const detail = document.getElementById('agent-handoff-detail');
        if (!item || !detail) return;
        const metadata = [
            item.status,
            item.steps != null ? `${item.steps} steps` : '',
            item.confidence != null ? `${Math.round(item.confidence * 100)}% confidence` : '',
            item.verdict || '',
        ].filter(Boolean).join(' · ');
        detail.innerHTML = `
            <div class="agent-handoff-header">
                <strong>${this.escapeHtml(item.label || 'Worker')}</strong>
                <button class="agent-handoff-close" type="button" aria-label="Close handoff">×</button>
            </div>
            <div class="agent-handoff-meta">${this.escapeHtml(metadata)}</div>
            ${item.prompt ? `<div class="agent-handoff-section"><span>Assignment</span><pre>${this.escapeHtml(item.prompt)}</pre></div>` : ''}
            <div class="agent-handoff-section"><span>Handoff</span><pre>${this.escapeHtml(item.handoff || 'No handoff was returned.')}</pre></div>
        `;
        detail.style.display = 'block';
        detail.querySelector('.agent-handoff-close')?.addEventListener('click', () => {
            detail.style.display = 'none';
        });
    }

    handleSubagentStart(event) {
        this.removeThinking();
        this.ensureStepRendered();

        const agentType = event.agent_type || '';
        const prompt = event.prompt || '';
        const activityId = `task:${event.call_id || Date.now()}`;
        this._updateLiveSubtask(activityId, {
            label: agentType || 'Sub-task',
            prompt,
            status: 'running',
            startedAt: Date.now(),
        });
        this._advanceLiveMilestone('delegate', 'Coordinate sub-tasks');
        this._setLiveRunPhase(
            'Delegating',
            prompt
                ? `Working on ${this._liveRunCompactValue(prompt, 68)}`
                : (agentType || 'Sub-task running'),
        );
        this.agentActivities.set(activityId, {
            id: activityId,
            agentId: event.agent_id || '',
            kind: 'subagent',
            label: agentType || 'task',
            prompt,
            parentId: Array.from(this.agentActivities.values()).find(
                (item) => item.agentId && item.agentId === event.parent_agent_id,
            )?.id || '',
            status: 'running',
            startedAt: Date.now(),
        });
        this.agentActivityOrder.push(activityId);
        this.renderAgentActivityTree();
        this._markAgentTabUnread();
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

        // Append beside sibling workers, or inside an explicit parent worker.
        // A global stack cannot represent task_batch events because they arrive
        // interleaved from concurrent threads.
        const parentTarget = this.subagentContainers.get(event.parent_agent_id || '');
        const task = this._ensureTaskCard('Task');
        const target = parentTarget || (task && task.activityEl) || this.chatMessages;
        target.appendChild(el);

        // Child events include _agent_id, which selects this render lane.
        if (event.agent_id) this.subagentContainers.set(event.agent_id, children);
        this.subagentDepth = this.subagentContainers.size;

        this.scrollToBottom();
    }

    handleSubagentEnd(event) {
        const agentType = event.agent_type || '';
        const steps = event.steps || 0;
        const elapsed = event.elapsed || 0;
        const activityId = `task:${event.call_id || ''}`;
        const workerOutcome = String(event.handoff?.outcome || '').toLowerCase();
        const workerFailed = ['failed', 'blocked', 'interrupted'].includes(workerOutcome);
        this._updateLiveSubtask(activityId, {
            label: agentType || 'Sub-task',
            status: workerFailed ? 'failed' : 'done',
            steps,
            elapsed,
        });
        const activity = this.agentActivities.get(activityId);
        if (activity) {
            activity.status = workerFailed ? 'failed' : 'done';
            activity.steps = steps;
            activity.finishedAt = activity.startedAt + elapsed * 1000;
            activity.handoff = event.handoff
                ? JSON.stringify(event.handoff, null, 2)
                : event.result || event.result_preview || '';
            this.agentActivities.set(activityId, activity);
            this.renderAgentActivityTree();
            this._markAgentTabUnread();
        }

        // Find this worker's own block even when sibling events interleave.
        const workerContainer = this.subagentContainers.get(event.agent_id || '')
            || this.subagentContainer;
        if (workerContainer) {
            const block = workerContainer.closest('.subagent-block');
            if (block) {
                const result = document.createElement('div');
                result.className = `subagent-result${workerFailed ? ' is-error' : ''}`;
                result.textContent = `${workerFailed ? '✗' : '✓'} ${agentType} · ${steps} steps · ${elapsed.toFixed(1)}s`;
                block.appendChild(result);

                // If subagent had content, auto-expand it
                if (workerContainer.children.length > 0) {
                    block.classList.add('expanded');
                    const toggle = block.querySelector('.subagent-toggle');
                    if (toggle) toggle.textContent = '▾';
                }
            }
        }

        if (event.agent_id) this.subagentContainers.delete(event.agent_id);
        const stream = this.subagentStreams.get(event.agent_id || '');
        if (stream?.timer) clearTimeout(stream.timer);
        if (event.agent_id) this.subagentStreams.delete(event.agent_id);
        this.subagentDepth = this.subagentContainers.size;
        this.subagentContainer = null;
        this._setLiveRunPhase(
            workerFailed ? 'Recovering' : 'Reasoning',
            workerFailed
                ? `${agentType || 'Sub-agent'} stopped before completing its handoff`
                : `Reviewing the ${agentType || 'sub-agent'} handoff`,
        );
    }

    handleSubagentError(event) {
        // Worker failures are activity within the current user turn. Promoting
        // them through handleError() creates a second top-level failure card,
        // then the parent error creates a third. Keep the status attached to
        // the worker; subagent.end carries the durable handoff and result.
        const agentId = String(event._agent_id || '');
        const activityId = [...this.agentActivityOrder].reverse().find((id) => {
            const candidate = this.agentActivities.get(id);
            return candidate?.status === 'running'
                && (!agentId || candidate.agentId === agentId);
        }) || '';
        const activity = activityId ? this.agentActivities.get(activityId) : null;
        if (activity) {
            activity.status = 'failed';
            activity.error = event.message || 'Worker stopped';
            activity.finishedAt = Date.now();
            this.agentActivities.set(activityId, activity);
            this._updateLiveSubtask(activityId, { status: 'failed' });
            this.renderAgentActivityTree();
        }
    }

    // ── Error ───────────────────────────────────────────────────

    handleError(event) {
        this.removeThinking();
        this._finalizeLiveCollapsedGroup();
        this.ensureStepRendered();

        // A provider can identify a streamed partial as malformed after enough
        // evidence accumulates. Remove that partial message instead of leaving
        // token soup in the conversation; the error card below remains as the
        // durable, actionable record of what happened.
        if (event.discard_partial_output) {
            if (this._renderTimer) {
                clearTimeout(this._renderTimer);
                this._renderTimer = null;
            }
            if (this.currentMessageEl) {
                this.currentMessageEl.remove();
            }
            this.currentMessageEl = null;
            this.streamBuffer = '';
            this.isStreaming = false;
        }

        if (event.message === 'Interrupted' && this._cancelInFlight) {
            this._cancelInterrupted = true;
            this._setLiveRunPhase('Stopping', 'The active run has been interrupted');
            return;
        }

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
        if (this._newSessionInflight) {
            this._releaseNewSessionGuard();
        }

        // Track error state so the run-card can drop the "Build" framing and
        // hide Review/Commit actions when there's nothing successfully to act on.
        this._agentRunErrored = true;
        this._agentRunErrorMessage = event.message || '';

        // A task card already has a dedicated failure summary with recovery
        // actions and expandable detail. Rendering a raw error block beside it
        // duplicates the same failure and makes one turn look like two.
        if (!this._activeTask) {
            const el = document.createElement('div');
            el.className = 'error-block';
            el.textContent = `✗ ${event.message || 'Unknown error'}`;
            this.getRenderTarget().appendChild(el);
        }
        this.scrollToBottom();
        this._finishActiveTask({
            total_elapsed: (this._currentTurn && this._currentTurn.totalElapsed) || 0,
            total_steps: (this._currentTurn && this._currentTurn.stepCount) || 0,
        });

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
        this._setSessionActivity('needs-input');
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
            this._setSessionActivity('working');
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
    _conciseAwaitUserQuestion(question) {
        const normalized = String(question || '')
            .replace(/\r\n?/g, '\n')
            .replace(/[ \t]+/g, ' ')
            .trim();
        if (!normalized) return '';

        // Models occasionally put rationale and duplicated option descriptions
        // into `question`. Keep only the paragraph that actually asks one.
        const blocks = normalized.split(/\n{2,}/).map((block) => block.trim()).filter(Boolean);
        const questionBlock = blocks.find((block) => block.includes('?'));
        let concise = questionBlock || blocks[0] || normalized;
        if (questionBlock) {
            const sentences = questionBlock.match(/[^.!?]+[.!?]+|[^.!?]+$/g) || [];
            const directQuestions = sentences.filter((sentence) => sentence.includes('?'));
            if (directQuestions.length > 0) concise = directQuestions.join(' ');
        } else {
            concise = concise.split(/(?<=[.!])\s+/)[0];
        }

        concise = concise
            .replace(/^\s*(?:q(?:uestion)?\s*)?(?:[-:\u2013\u2014]+\s*)/i, '')
            .replace(/\s+/g, ' ')
            .trim();
        if (concise.length > 240) {
            concise = `${concise.slice(0, 236).replace(/\s+\S*$/, '')}\u2026`;
        }
        return concise;
    }

    handleAwaitUser(event) {
        const question = (event && event.question) || '';
        const options = Array.isArray(event && event.options) ? event.options : [];
        if (!question) return;
        this._setSessionActivity('needs-input');

        const conciseQuestion = this._conciseAwaitUserQuestion(question);

        const normalizedOptions = options.map((option, index) => {
            const raw = String(option || '').trim();
            const recommended = /\s*\(recommended\)\s*$/i.test(raw);
            const value = raw.replace(/\s*\(recommended\)\s*$/i, '').trim();
            const markerMatch = value.match(/^\s*(?:option\s+)?([a-z]|\d+)\s*(?:[.)]|[-:\u2013\u2014])\s*/i);
            return {
                value,
                label: markerMatch ? value.slice(markerMatch[0].length).trim() : value,
                marker: markerMatch ? markerMatch[1].toUpperCase() : String.fromCharCode(65 + index),
                recommended,
            };
        }).filter((option) => option.value);

        const wrap = document.createElement('div');
        wrap.className = 'await-user-prompt';
        wrap.dataset.awaitUserState = 'waiting';

        // Chips for options. Plain textarea + Send for free-text.
        const chipsHTML = normalizedOptions.length > 0
            ? `<div class="await-user-chips">${
                normalizedOptions.map((option, index) => `
                    <button type="button" class="await-user-chip${option.recommended ? ' is-recommended' : ''}" data-option-index="${index}">
                        <span class="await-user-option-key" aria-hidden="true">${this.escapeHtml(option.marker)}</span>
                        <span class="await-user-option-label">${this.escapeHtml(option.label)}</span>
                        ${option.recommended ? '<span class="await-user-recommended">Recommended</span>' : ''}
                    </button>
                `).join('')
              }</div>`
            : '';

        wrap.innerHTML = `
            <div class="await-user-header">
                <span class="await-user-icon" aria-hidden="true">❓</span>
                <span class="await-user-label">Agent needs your input</span>
            </div>
            <div class="await-user-question">${this.escapeHtml(conciseQuestion)}</div>
            ${chipsHTML}
            <div class="await-user-input-row">
                <textarea class="await-user-input" rows="2"
                    placeholder="${normalizedOptions.length > 0 ? 'Or type a different answer…' : 'Type your answer…'}"></textarea>
                <button type="button" class="await-user-send">Send</button>
            </div>
        `;

        let answered = false;
        const reply = (text) => {
            if (answered) return;
            answered = true;
            // One-shot — disable the whole prompt after answering so
            // the user can't accidentally double-send. The backend
            // immediately resumes; we don't need an "answer received"
            // animation.
            wrap.dataset.awaitUserState = 'submitting';
            wrap.classList.add('await-user-answered');
            wrap.querySelectorAll('button, textarea').forEach(el => el.disabled = true);
            const controls = wrap.querySelector('.await-user-chips');
            const inputRow = wrap.querySelector('.await-user-input-row');
            if (controls) controls.hidden = true;
            if (inputRow) inputRow.hidden = true;
            const confirmation = document.createElement('div');
            confirmation.className = 'await-user-confirmation';
            confirmation.innerHTML = `
                <span class="await-user-confirmation-check" aria-hidden="true">&#10003;</span>
                <span><small>Selected</small><strong>${this.escapeHtml(text)}</strong></span>
                <span class="await-user-confirmation-status">Resuming agent&hellip;</span>
            `;
            wrap.appendChild(confirmation);
            this._setLiveRunPhase('Continuing', 'Applying your selection');
            this._setSessionActivity('working');
            this.send({ command: 'user_input', response: text });
        };

        wrap.querySelectorAll('.await-user-chip').forEach(btn => {
            btn.addEventListener('click', () => {
                const option = normalizedOptions[Number(btn.dataset.optionIndex)];
                if (option) reply(option.value);
            });
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
        setTimeout(() => {
            if (!answered) textarea.focus();
        }, 100);
    }

    handleUserInputReceived(_event) {
        this._setSessionActivity('working');
        const pending = Array.from(this.chatMessages.querySelectorAll(
            '.await-user-prompt[data-await-user-state="submitting"]',
        )).at(-1);
        if (!pending) return;
        pending.dataset.awaitUserState = 'received';
        const status = pending.querySelector('.await-user-confirmation-status');
        if (status) status.textContent = 'Agent resumed';
        this._setLiveRunPhase('Continuing', 'Working from your selection');
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

    _setActiveTask(task) {
        this._activeTask = task || null;
        this.activeTaskCard = task ? task.card : null;
        this.activeTaskActivityEl = task ? task.activityEl : null;
        this.activeTaskResultEl = task ? task.resultEl : null;
        this.activeTaskFooterEl = task ? task.footerEl : null;
    }

    _appendTaskImages(container, images = []) {
        if (!images || images.length === 0) return;
        const wrap = document.createElement('div');
        wrap.className = 'task-attachments';
        for (const img of images) {
            const thumb = document.createElement('img');
            thumb.src = img.dataUrl || `data:${img.media_type};base64,${img.data}`;
            thumb.alt = 'Attached';
            thumb.className = 'task-attachment-thumb';
            thumb.addEventListener('click', () => this.showLightbox(thumb.src));
            wrap.appendChild(thumb);
        }
        container.appendChild(wrap);
    }

    _getTaskResultTarget() {
        const task = this._ensureTaskCard('Agent response');
        if (!task || !task.resultEl) return null;
        task.resultEl.hidden = false;
        return task.resultEl;
    }

    addUserMessage(text, images = []) {
        return this._beginTaskCard(text, images);
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
        const rows = Array.from(this.chatMessages.querySelectorAll('.task-card[data-user-message="true"], .msg-user'));
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
        const subagentTarget = this.getRenderTarget();
        const isSubagentTarget = Boolean(
            this._activeRenderEvent?._agent_id
            && this.subagentContainers.has(this._activeRenderEvent._agent_id),
        );
        const target = isSubagentTarget
            ? subagentTarget
            : (this._getTaskResultTarget() || this.chatMessages);
        if (!isSubagentTarget && target.classList?.contains('task-result')) {
            target.querySelectorAll(':scope > .msg-assistant').forEach((msg) => {
                msg.classList.add('task-progress-note');
            });
        }
        target.appendChild(el);
        return el;
    }

    _advanceLiveMilestone(id, text) {
        const run = this._liveRun;
        if (!run || !run.active || run.modelTodos) return;
        const current = run.milestones.find((item) => item.status === 'running');
        if (current && current.id !== id && current.id !== 'verify') current.status = 'done';
        if (id === 'change') {
            const verification = run.milestones.find(entry => entry.id === 'verify');
            if (verification) { verification.status = 'pending'; verification.text = 'Checks need rerunning after edits'; }
            if (run.checks) for (const check of run.checks.values()) if (check.status === 'passed') check.status = 'stale';
        }
        let item = run.milestones.find((entry) => entry.id === id);
        if (!item) {
            item = { id, text, status: 'running' };
            run.milestones.push(item);
        } else {
            item.text = text || item.text;
            item.status = 'running';
        }
        this._renderLiveRun();
    }

    _updateLiveSubtask(id, patch) {
        const run = this._liveRun;
        if (!run || !run.active) return;
        const previous = run.subtasks.get(id) || { id };
        run.subtasks.set(id, { ...previous, ...patch, id });
        this._renderLiveRun();
    }

    handleStatusUpdateQueued(event) {
        const run = this._liveRun;
        if (!run || run.statusRequestId !== event.message_id) return;
        if (run.statusRequestTimer) clearTimeout(run.statusRequestTimer);
        run.statusRequestTimer = null;
        run.statusRequestState = 'queued';
        run.statusNote = event.message || 'Agent update queued for the next safe step';
        this._renderLiveRun();
    }

    handleStatusUpdateRejected(event) {
        const run = this._liveRun;
        if (!run || run.statusRequestId !== event.message_id) return;
        if (run.statusRequestTimer) clearTimeout(run.statusRequestTimer);
        run.statusRequestTimer = null;
        run.statusRequestState = 'failed';
        run.statusNote = event.message || 'The active run could not accept an update request';
        this._renderLiveRun();
    }

    addThinking() {
        if (this._liveRun && this._liveRun.active) {
            const run = this._liveRun;
            const detail = run.currentAction
                || (run.lastCompleted ? `Reviewing ${run.lastCompleted.text}` : '')
                || 'Planning the next action';
            this._setLiveRunPhase('Reasoning', detail);
            return;
        }
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
        // Removal must never create a render target. In particular, an error
        // finishes the active task before session.end arrives; calling
        // getRenderTarget() there manufactured an empty synthetic task card,
        // which session.end then finalized as a duplicate failure.
        const activeAgentId = String(this._activeRenderEvent?._agent_id || '');
        const target = this.subagentContainers.get(activeAgentId)
            || this.subagentContainer
            || this._activeTask?.activityEl
            || this.chatMessages;
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

    /**
     * Raise an OS notification for something that happened while the user was
     * elsewhere.
     *
     * Long autonomous runs are measured in tens of minutes; a toast only
     * reaches someone already looking at the window. The three moments that
     * genuinely need a person — the run finished, it failed, or it parked
     * waiting on a decision — should reach them anywhere on the desktop.
     *
     * Deliberately silent when the window is already focused: an alert for
     * something visible on screen is noise, and noise gets permission revoked.
     */
    notifyDesktop(title, body, { tag = '', force = false } = {}) {
        try {
            if (!force && document.visibilityState === 'visible' && document.hasFocus()) return false;
            if (typeof Notification === 'undefined') return false;
            if (Notification.permission !== 'granted') return false;
            const note = new Notification(title, {
                body: String(body || '').slice(0, 240),
                // A tag collapses repeats: a run that parks twice replaces its
                // own notification instead of stacking.
                tag: tag || 'resonant-run',
                icon: '/static/resonant.png',
            });
            note.onclick = () => { window.focus(); note.close(); };
            return true;
        } catch (err) {
            console.debug('desktop notification failed', err);
            return false;
        }
    }

    /**
     * Ask for notification permission the first time a long run could need it.
     *
     * Not requested at startup: an unprompted permission dialog on first launch
     * is the fastest way to get permanently denied. This is called when the
     * user actually starts autonomous work, where the payoff is obvious.
     */
    ensureNotificationPermission() {
        try {
            if (typeof Notification === 'undefined') return;
            if (Notification.permission !== 'default') return;
            if (this._notificationPromptShown) return;
            this._notificationPromptShown = true;
            Notification.requestPermission().catch(() => {});
        } catch (err) {
            console.debug('notification permission request failed', err);
        }
    }

    showToastMessage(message) {
        if (!message) return;
        let el = document.getElementById('ui-toast-message');
        if (!el) {
            el = document.createElement('div');
            el.id = 'ui-toast-message';
            el.className = 'ui-toast-message';
            document.body.appendChild(el);
        }
        el.textContent = message;
        el.classList.add('visible');
        clearTimeout(el._hideTimer);
        el._hideTimer = setTimeout(() => {
            el.classList.remove('visible');
        }, 2200);
    }

    showStatusMessage(message) {
        if (!message) return;
        if (typeof this.showToastMessage === 'function' && !this.isReplaying) {
            this.showToastMessage(message);
            return;
        }
        const el = document.createElement('div');
        el.className = 'status-inline';
        el.style.cssText = 'text-align:center;color:var(--muted);font-size:12px;padding:8px;';
        el.textContent = message;
        const target = (this._activeTask && this._activeTask.activityEl) || this.chatMessages;
        target.appendChild(el);
        this.scrollToBottom();
    }

    /**
     * Show or clear the persistent "runtime unavailable" notice above the
     * composer.
     *
     * Deliberately not a toast. The reason a runtime failed to start was
     * already being computed and sent, but only as a transient status message
     * — so it was gone by the time the user typed something, and all that
     * remained was a model name in the composer plus an error insisting no
     * model was selected. This state persists until the runtime actually
     * works, because the condition itself persists.
     */
    _applyRuntimeError(event) {
        const el = document.getElementById('runtime-banner');
        if (!el) return;

        const reason = (event && event.runtime_ready === false)
            ? (event.runtime_error || '') : '';
        // An MCP server being down does not block chat (its tools just
        // disappear), but it silently removes capabilities the user asked
        // for. With BrowserOS down, "open a browser and search" fails in a
        // way that reads as the agent ignoring the request.
        const down = (event && Array.isArray(event.mcp_unavailable)) ? event.mcp_unavailable : [];
        let mcpNote = (event && event.mcp_load_error) ? event.mcp_load_error : '';
        if (!mcpNote && down.length) {
            mcpNote = down
                .map(s => `${s.name} not connected${s.endpoint ? ` (${s.endpoint})` : ''}${s.error ? ` — ${s.error}` : ''}`)
                .join('; ');
        }

        if (!reason && !mcpNote) {
            el.hidden = true;
            el.textContent = '';
            this._dismissedRuntimeNotice = '';
            return;
        }

        // Dismissal is keyed to the message, not the element. A sticky banner
        // with no way to close it is just noise once the user has read it —
        // but silencing it forever would hide a *different*, later problem, so
        // a changed message brings it back.
        const signature = `${reason}||${mcpNote}`;
        if (this._dismissedRuntimeNotice === signature) {
            el.hidden = true;
            return;
        }

        el.replaceChildren();
        if (reason) {
            const line = document.createElement('div');
            line.className = 'runtime-banner-reason';
            line.textContent = reason;
            el.appendChild(line);
        }
        if (mcpNote) {
            const line = document.createElement('div');
            line.className = 'runtime-banner-mcp';
            line.textContent = `Tool server unavailable: ${mcpNote}`;
            el.appendChild(line);
        }

        const close = document.createElement('button');
        close.className = 'runtime-banner-close';
        close.type = 'button';
        close.setAttribute('aria-label', 'Dismiss');
        close.title = 'Dismiss';
        close.textContent = '×';
        close.addEventListener('click', () => {
            this._dismissedRuntimeNotice = signature;
            el.hidden = true;
        });
        el.appendChild(close);

        el.hidden = false;
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
        if (event.reset_partial_output && this.isStreaming) {
            if (this._renderTimer) {
                clearTimeout(this._renderTimer);
                this._renderTimer = null;
            }
            if (this.currentMessageEl) this.currentMessageEl.remove();
            this.currentMessageEl = null;
            this.streamBuffer = '';
            this.isStreaming = false;
        }
        if (event.kind === 'exo_instance_check') {
            this._setLiveRunPhase(
                'Starting',
                `Checking ${event.model || 'the selected model'} on EXO`,
            );
        } else if (event.kind === 'exo_instance_ready') {
            this._setLiveRunPhase(
                'Ready',
                `${event.model || 'The selected model'} is loaded on EXO`,
            );
        } else if (event.kind === 'exo_prefill_progress') {
            const envelope = event.progress || {};
            const progress = envelope.PrefillProgressChunk || envelope;
            const rawPercent = Number(
                progress.progress
                ?? progress.percent
                ?? progress.percentage
                ?? progress.fraction
                ?? (
                    Number(progress.total_tokens) > 0
                        ? Number(progress.processed_tokens || 0) / Number(progress.total_tokens)
                        : NaN
                ),
            );
            const percent = Number.isFinite(rawPercent)
                ? Math.round(rawPercent <= 1 ? rawPercent * 100 : rawPercent)
                : null;
            if (this._liveRun) {
                this._liveRun.provider = 'exo';
                this._liveRun.model = event.model || this._liveRun.model;
                this._liveRun.lastProgressAt = Date.now();
                this._liveRun.lastTransportAt = Date.now();
            }
            this._setLiveRunPhase(
                'Reading context',
                percent === null
                    ? `EXO is preparing the prompt for ${event.model || 'the model'}`
                    : `EXO prompt prefill ${Math.max(0, Math.min(100, percent))}%`,
            );
        } else if (event.kind === 'exo_generation_started') {
            const idleSeconds = Number(event.idle_timeout_seconds || 0);
            const warningSeconds = Number(event.progress_warning_seconds || 120);
            if (this._liveRun) {
                this._liveRun.provider = 'exo';
                this._liveRun.model = event.model || this._liveRun.model;
                this._liveRun.idleTimeoutSeconds = idleSeconds;
                this._liveRun.progressWarningSeconds = warningSeconds;
                this._liveRun.lastProgressAt = Date.now();
                this._liveRun.lastTransportAt = Date.now();
            }
            const safeguard = idleSeconds > 0
                ? ` · ${idleSeconds}s operator idle limit`
                : ' · no automatic time limit';
            this._setLiveRunPhase(
                'Reasoning',
                `Generating with ${event.model || 'EXO'}${safeguard}`,
            );
        } else if (event.kind === 'exo_keepalive') {
            if (this._liveRun) {
                this._liveRun.provider = 'exo';
                this._liveRun.model = event.model || this._liveRun.model;
                this._liveRun.lastTransportAt = Date.now();
                if (Number(event.idle_timeout_seconds || 0) > 0) {
                    this._liveRun.idleTimeoutSeconds = Number(
                        event.idle_timeout_seconds,
                    );
                }
            }
        } else if (event.kind === 'ollama_retry' || event.kind === 'ollama_timeout' || event.kind === 'kimi_retry' || event.kind === 'exo_retry') {
            // v0.6.4 (F6) — ollama_timeout (a slow open-phase call
            // being retried) shares the transient retry banner; the
            // renderer phrases it differently from a 5xx retry.
            if (event.kind === 'exo_retry' && event.reason === 'runner_restart') {
                if (this._liveRun) {
                    this._liveRun.provider = 'exo';
                    this._liveRun.lastProgressAt = Date.now();
                    this._liveRun.lastTransportAt = Date.now();
                }
                this._setLiveRunPhase(
                    'Recovering',
                    `EXO runner stopped; replaying the uncommitted step safely (attempt ${(event.attempt || 1) + 1}/${event.max || 3})`,
                );
            }
            this._renderOllamaRetryBanner(event);
        } else if (event.kind === 'generation_progress') {
            if (this._liveRun) {
                this._liveRun.lastProgressAt = Date.now();
                this._liveRun.lastTransportAt = Date.now();
            }
            const label = {generating_code: 'Generating code', reasoning: 'Reasoning', responding: 'Writing response'}[event.phase] || 'Generating';
            this._setLiveRunPhase(label, `${label} with ${event.model || 'the model'}`);
        } else if (event.kind === 'empty_response_retry') {
            if (!this._cancelInFlight && !this._cancelInterrupted) this._renderEmptyResponseRetryBanner(event);
        } else if (event.kind === 'action_promise_continuation') {
            this._renderActionContinuationBanner(event);
        } else if (event.kind === 'ollama_exhausted') {
            this._renderOllamaExhaustedChip(event);
        }
        // Future kinds get their own renderers; swallow unknown kinds
        // silently rather than confuse the user with unfamiliar text.
    }

    _renderEmptyResponseRetryBanner(event) {
        if (!this.chatMessages) return;
        const attempt = event.attempt || 1;
        const max = event.max || 2;
        const model = event.model || 'The model';
        const banner = document.createElement('div');
        banner.className = 'backend-status-banner backend-status-retry';
        banner.innerHTML = `
            <span class="backend-status-icon" aria-hidden="true">↻</span>
            <span class="backend-status-text">
                ${this.escapeHtml(model)} returned no usable response — retrying automatically
                <span class="backend-status-attempt">retry ${attempt}/${max}</span>
            </span>
        `;
        this.chatMessages.appendChild(banner);
        this.scrollToBottom();
        setTimeout(() => {
            banner.classList.add('backend-status-banner-fading');
            setTimeout(() => banner.remove(), 400);
        }, 3500);
    }

    _renderActionContinuationBanner(event) {
        if (!this.chatMessages) return;
        const banner = document.createElement('div');
        banner.className = 'backend-status-banner backend-status-retry';
        banner.innerHTML = `
            <span class="backend-status-icon" aria-hidden="true">→</span>
            <span class="backend-status-text">
                The agent promised an action without taking it — continuing automatically
                <span class="backend-status-attempt">continuation ${event.attempt || 1}/${event.max || 2}</span>
            </span>
        `;
        this.chatMessages.appendChild(banner);
        this.scrollToBottom();
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

        const altSuggestion = 'another configured model';

        // v0.6.4 (F6) — status_code 0 + reason "timeout" is the
        // timeout-exhausted flavor (the open-phase retries all timed
        // out); otherwise it's the 5xx-exhausted flavor.
        let reason;
        if (event.reason === 'timeout' || status === 0) {
            reason = 'not responding (read timeout)';
        } else if (status === 503) {
            reason = 'rate-limited (HTTP 503 — cloud overloaded)';
        } else {
            reason = `returning transient ${status} errors`;
        }

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
        // gateway errors that look the same to the user. v0.6.4 (F6):
        // an ollama_timeout event carries no status_code — it's a
        // slow open-phase call, not an error response.
        let reason;
        if (event.kind === 'exo_retry' && event.reason === 'runner_restart') {
            reason = 'lost an EXO runner before the step committed';
        } else if (event.kind === 'ollama_timeout') {
            reason = 'slow to respond';
        } else if (status === 503) {
            reason = 'rate-limited (HTTP 503)';
        } else {
            reason = `transient ${status} error`;
        }

        const banner = document.createElement('div');
        banner.className = 'backend-status-banner backend-status-retry';
        banner.innerHTML = `
            <span class="backend-status-icon" aria-hidden="true">⚠</span>
            <span class="backend-status-text">
                Backend ${this.escapeHtml(reason)} — retrying safely in ${backoff.toFixed(1)}s
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
        // History replay can invoke normal event handlers hundreds of times in
        // one tick. Queueing a scroll frame for every saved event turns one
        // session click into hundreds of redundant layout reads and writes.
        // replayDisplayEvents() performs one final scroll after replay ends.
        if (this.isReplaying) return;
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
    _renderHistoryPageControl() {
        this.chatMessages?.querySelector('.session-history-page-control')?.remove();
        const page = this._historyPage;
        if (!this.chatMessages || !page || (!page.has_more && !this._historyWindowDroppedTail)) return;

        const control = document.createElement('div');
        control.className = 'session-history-page-control';
        const button = document.createElement('button');
        button.type = 'button';
        button.disabled = this._historyLoading;
        if (this._historyWindowDroppedTail) {
            button.textContent = 'Return to latest activity';
            button.addEventListener('click', () => {
                if (!this.currentSessionId) return;
                this.send({ command: 'switch_session', session_id: this.currentSessionId });
            });
        } else {
            const loaded = this._loadedHistoryEvents.length;
            const total = Number(page.total_events || loaded);
            button.textContent = this._historyLoading
                ? 'Loading earlier activity…'
                : `Load earlier activity · ${loaded} of ${total} events`;
            button.addEventListener('click', () => this._loadOlderHistory());
        }
        control.appendChild(button);
        this.chatMessages.prepend(control);
    }

    _loadOlderHistory() {
        if (this._historyLoading || !this._historyPage?.has_more || !this.currentSessionId) return;
        this._historyLoading = true;
        this._renderHistoryPageControl();
        this.send({
            command: 'get_session_history_page',
            session_id: this.currentSessionId,
            before_seq: this._historyPage.start_seq,
            limit: 240,
        });
    }

    handleSessionHistoryPage(event) {
        if (event.session_id !== this.currentSessionId) return;
        this._historyLoading = false;
        if (event.error) {
            this.showStatusMessage(`Could not load earlier activity: ${event.error}`);
            this._renderHistoryPageControl();
            return;
        }

        const page = event.page || {};
        const older = Array.isArray(page.events) ? page.events : [];
        const existingSeqs = new Set(
            this._loadedHistoryEvents.map((item) => item && item._ledger_seq)
                .filter((value) => value !== undefined),
        );
        const merged = [
            ...older.filter((item) => !existingSeqs.has(item && item._ledger_seq)),
            ...this._loadedHistoryEvents,
        ];

        // A bounded event window keeps the DOM genuinely bounded. Loading far
        // into the past drops the newer tail from this temporary inspection
        // window; one click restores the authoritative latest page.
        const MAX_MOUNTED_HISTORY_EVENTS = 1200;
        this._historyWindowDroppedTail = merged.length > MAX_MOUNTED_HISTORY_EVENTS;
        this._loadedHistoryEvents = merged.slice(0, MAX_MOUNTED_HISTORY_EVENTS);
        const lastLoaded = this._loadedHistoryEvents[this._loadedHistoryEvents.length - 1];
        this._historyPage = {
            ...page,
            end_seq: lastLoaded?._ledger_seq ?? page.end_seq,
        };

        const scrollSurface = this.chatContainer || this.chatMessages;
        const previousHeight = scrollSurface.scrollHeight;
        const previousTop = scrollSurface.scrollTop;
        this.chatMessages.innerHTML = '';
        this._resetTaskCardState();
        this.replayDisplayEvents(this._loadedHistoryEvents);
        this._renderHistoryPageControl();
        requestAnimationFrame(() => {
            const addedHeight = Math.max(0, scrollSurface.scrollHeight - previousHeight);
            scrollSurface.scrollTop = previousTop + addedHeight;
        });
    }

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
        this._syncComposerGutter?.();

        this._renderReplayUpTo(0);
    }

    exitReplayMode() {
        if (!this._replay) return;
        this._stopReplayTimer();
        this.chatMessages.innerHTML = this._replay.stashedHTML;
        this._resetTaskCardState();
        this.inputBar.style.display = this._replay.inputBarDisplay || '';
        this._syncComposerGutter?.();
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
        this._resetTaskCardState();
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

    replayDisplayEvents(events, { activeRun = false } = {}) {
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
        this.subagentContainers.clear();
        this.subagentStreams.clear();

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
                this._withRenderEvent(event, () => this.handleTextDoneReplay(event));
                continue;
            }

            // Step start/end, tool call/result, subagent — use normal handlers
            this._withRenderEvent(event, () => {
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
                    if (event._subagent) this.handleSubagentError(event);
                    else this.handleError(event);
                }
            });
        }
        } finally {
            this.isReplaying = false;
        }

        // Flush any pending collapsed groups
        this.flushCollapsedGroup();

        // A reconnect can replay an unfinished turn while its server-owned run
        // is still active. Keep the composer and recovery UI aligned with that.
        if (!activeRun) this.setRunning(false);
        this.clearTerminals();

        const replayRecovery = activeRun ? null : this._interruptedReplayRecovery(events);
        if (replayRecovery) this.showResumeButton(replayRecovery);

        // Scroll to bottom
        this.scrollToBottom();
    }

    _interruptedReplayRecovery(events = []) {
        // Inspect only the unfinished turn after the most recent clean end.
        // A bare step.start means inference never produced a response, so the
        // correct action is to retry the user's request rather than ask the
        // model to "continue" work that never began.
        const lastEnd = events.reduce(
            (index, event, candidate) => event?.event === 'session.end' ? candidate : index,
            -1,
        );
        const tail = events.slice(lastEnd + 1);
        const userIndex = tail.reduce(
            (index, event, candidate) => event?.event === 'user_message' ? candidate : index,
            -1,
        );
        if (userIndex < 0) return null;

        const turn = tail.slice(userIndex + 1);
        const terminal = new Set(['session.end', 'text.done', 'error', 'await_user']);
        if (turn.some((event) => terminal.has(event?.event))) return null;

        const started = turn.some((event) => event?.event === 'step.start');
        const partial = turn.some((event) => [
            'tool.call', 'tool.result', 'text.delta', 'thinking.delta',
        ].includes(event?.event));
        if (!started && !partial) return null;
        return {
            kind: partial ? 'paused' : 'not_started',
            prompt: String(tail[userIndex]?.text || '').trim(),
        };
    }

    showResumeButton(recovery = {}) {
        this.chatMessages.querySelector('.resume-banner')?.remove();
        const notStarted = recovery.kind === 'not_started';
        const label = notStarted ? 'Response didn\'t start' : 'Response paused';
        const action = notStarted ? 'Retry' : 'Continue';
        const el = document.createElement('div');
        el.className = 'resume-banner';
        el.innerHTML = `
            <span class="resume-text">${label}</span>
            <button class="resume-btn">${action}</button>
        `;
        el.querySelector('.resume-btn').addEventListener('click', () => {
            el.remove();
            this.userInput.value = notStarted && recovery.prompt
                ? recovery.prompt
                : 'Continue the previous request from where it paused. Preserve completed work and verify the result.';
            this.sendMessage();
        });
        const task = this._activeTask;
        if (task && task.footerEl) {
            task.footerEl.hidden = false;
            task.footerEl.appendChild(el);
        } else {
            this.chatMessages.appendChild(el);
        }
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
        el.className = 'msg-assistant';

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

        const activeAgentId = String(this._activeRenderEvent?._agent_id || '');
        const subagentTarget = this.subagentContainers.get(activeAgentId);
        const container = subagentTarget || this._getTaskResultTarget() || this.chatMessages;
        if (!subagentTarget && container.classList?.contains('task-result')) {
            container.querySelectorAll(':scope > .msg-assistant').forEach((msg) => {
                msg.classList.add('task-progress-note');
            });
        }
        container.appendChild(el);
    }

    // ── Session List ─────────────────────────────────────────────

    _renderPinnedGroup(sessions) {
        const wrap = document.createElement('div');
        wrap.className = 'mission-group pinned-group';

        const header = document.createElement('div');
        header.className = 'mission-group-header';
        header.innerHTML = `
            <span class="mission-group-icon" aria-hidden="true"><svg width="13" height="13" viewBox="0 0 14 14" fill="none"><path d="M7 1.4l1.6 3.3 3.6.5-2.6 2.5.6 3.6L7 9.7 3.8 11.4l.6-3.6L1.8 5.2l3.6-.5L7 1.4z" stroke="currentColor" stroke-width="1" stroke-linejoin="round"/></svg></span>
            <span class="mission-group-title">Pinned</span>
            <span class="mission-group-count">${sessions.length}</span>
        `;
        wrap.appendChild(header);

        const sortByUpdated = (a, b) => (b.updated_at || 0) - (a.updated_at || 0);
        [...sessions].sort(sortByUpdated).forEach(s => {
            wrap.appendChild(this._createTreeSessionRow(s));
        });

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
            <span class="mission-group-icon" aria-hidden="true"><svg width="13" height="13" viewBox="0 0 14 14" fill="none"><path d="M8 1.5L3.5 8h3l-.5 4.5L10.5 6h-3L8 1.5z" stroke="currentColor" stroke-width="1" stroke-linejoin="round"/></svg></span>
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
            this.sessionList.innerHTML = '<div class="agent-empty">No sessions yet</div>';
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

            const fullTitle = session.title || 'New session';
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
        if (this._pinnedOnly) {
            // v0.6.6 — "Pinned" quick-filter: only pinned sessions, across all projects.
            filtered = filtered.filter(s => s && s.pinned);
        } else if (projFilter) {
            filtered = filtered.filter(s => (s.project_path || '').replace(/\\/g, '/') === projFilter);
        }
        if (searchVal) {
            filtered = filtered.filter(s => (s.title || '').toLowerCase().includes(searchVal));
        }

        this._renderProjectTree(filtered);
        this.renderProjectRail();
    }

    _renderProjectTree(sessions) {
        if (!this.sessionList) return;
        this.sessionList.innerHTML = '';
        const visibleSessions = Array.isArray(sessions) ? sessions.filter(Boolean) : [];

        // Pinned sessions float to the top in their own group, removed from
        // their original section to avoid double-listing.
        const pinned = visibleSessions.filter(s => s && s.pinned);
        const unpinned = visibleSessions.filter(s => s && !s.pinned);

        if (pinned.length > 0) {
            this._renderPinnedGroup(pinned);
        }

        // Projects live on the rail; the sidebar body stays a flat session list.
        const sortByUpdated = (a, b) => (b.updated_at || 0) - (a.updated_at || 0);
        for (const session of [...unpinned].sort(sortByUpdated)) {
            this.sessionList.appendChild(this._createTreeSessionRow(session));
        }

        if (!pinned.length && !unpinned.length) {
            this.sessionList.innerHTML = '<div class="agent-empty">No sessions yet</div>';
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

        const indicator = this._sessionIndicator(session);
        const autoBadge = '';
        el.innerHTML = `
            <div class="agent-row-title"><span class="agent-row-status is-${indicator.state}" role="img" aria-label="${indicator.label}" title="${indicator.label}"><span aria-hidden="true"></span></span>${this.escapeHtml(session.title || 'New session')}</div>
            <div class="agent-row-date">${autoBadge}${roleTag}${session.model || ''} \u00B7 ${timeStr}</div>
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

    /** Resolve the semantic lifecycle shown beside a session title. */
    _sessionIndicator(session) {
        const explicit = this._sessionActivity.get(session.id);
        if (explicit === 'needs-input') return { state: explicit, label: 'Needs your input' };
        if (explicit === 'working') return { state: explicit, label: 'Resonant is working' };

        const phase = String(session?.mission_state?.phase || '').toLowerCase();
        const isOrphan = (this._autonomousOrphans || []).some((item) =>
            item && (item.session_id === session.id || item.id === session.id)
        );
        if (isOrphan) return { state: 'needs-input', label: 'Needs your attention' };
        if (['planning_dispatched', 'executing', 'reviewing', 'autonomous_running'].includes(phase)) {
            return { state: 'working', label: 'Resonant is working' };
        }
        return { state: 'idle', label: 'Idle' };
    }

    /** Update one row in place so activity changes never rebuild the list. */
    _setSessionActivity(state, sessionId = this.currentSessionId) {
        if (!sessionId) return;
        const normalized = state === 'needs-input' ? state : (state === 'working' ? state : 'idle');
        const previous = this._sessionActivity.get(sessionId) || 'idle';
        if (normalized === 'idle') this._sessionActivity.delete(sessionId);
        else this._sessionActivity.set(sessionId, normalized);
        if (previous === normalized) return;

        const row = this.sessionList?.querySelector(
            `.agent-row[data-session-id="${CSS.escape(sessionId)}"]`,
        );
        const marker = row?.querySelector('.agent-row-status');
        if (!marker) return;
        const labels = {
            working: 'Resonant is working',
            'needs-input': 'Needs your input',
            idle: 'Idle',
        };
        marker.className = `agent-row-status is-${normalized}`;
        marker.setAttribute('aria-label', labels[normalized]);
        marker.title = labels[normalized];
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
                const newTitle = prompt('Rename session:', session.title);
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

    _normalizeProjectPath(path) {
        return (path || '').replace(/\\/g, '/').replace(/\/+$/, '');
    }

    _projectKey(path) {
        return this._normalizeProjectPath(path).toLowerCase();
    }

    _projectNameFromPath(path) {
        const parts = this._normalizeProjectPath(path).split('/').filter(Boolean);
        return parts[parts.length - 1] || 'Project';
    }

    _projectInitials(name) {
        const clean = String(name || '').trim();
        if (!clean) return 'P';
        if (clean.toLowerCase() === 'playground') return 'P';
        const words = clean.split(/[\s._-]+/).filter(Boolean);
        if (words.length >= 2) return `${words[0][0] || ''}${words[1][0] || ''}`.toUpperCase();
        return clean.slice(0, 2).toUpperCase();
    }

    _projectRailPalette() {
        return [
            ['#5b3b95', '#2b2240', 'rgba(182,157,255,.62)'],
            ['#2d7a65', '#153c34', 'rgba(95,231,195,.54)'],
            ['#8a4d2b', '#442615', 'rgba(255,171,105,.55)'],
            ['#2c5f9a', '#142c49', 'rgba(111,183,255,.52)'],
            ['#8a2f55', '#421727', 'rgba(255,119,169,.52)'],
            ['#66702a', '#313614', 'rgba(215,232,99,.48)'],
            ['#6a3c8c', '#311d42', 'rgba(202,139,255,.52)'],
            ['#9a3c35', '#451b18', 'rgba(255,128,119,.52)'],
            ['#276f84', '#12343e', 'rgba(94,218,244,.48)'],
            ['#7a6424', '#3a3012', 'rgba(240,205,87,.48)'],
            ['#355f32', '#182d17', 'rgba(135,218,127,.48)'],
            ['#6b4674', '#332138', 'rgba(218,157,232,.48)'],
        ];
    }

    _projectRailColor(index) {
        const palette = this._projectRailPalette();
        return palette[index % palette.length];
    }

    _getProjectRailItems() {
        const byKey = new Map();
        const candidateOrder = [];
        const sessionPool = (this.allSessions && this.allSessions.length)
            ? this.allSessions
            : this.sessions;
        const counts = new Map();
        for (const session of (sessionPool || [])) {
            const key = this._projectKey(session?.project_path || this.currentCwd || '');
            if (!key) continue;
            counts.set(key, (counts.get(key) || 0) + 1);
        }
        const addProject = (pathValue, nameValue = '') => {
            const path = this._normalizeProjectPath(pathValue);
            if (!path) return;
            const key = this._projectKey(path);
            if (!key) return;
            if (!byKey.has(key)) {
                byKey.set(key, {
                    key,
                    path,
                    name: nameValue || this._projectNameFromPath(path),
                    count: counts.get(key) || 0,
                });
                candidateOrder.push(key);
                return;
            }
            const existing = byKey.get(key);
            if (!existing.name && nameValue) existing.name = nameValue;
            existing.count = counts.get(key) || existing.count || 0;
        };

        const playgroundKey = this.playgroundProject?.path
            ? this._projectKey(this.playgroundProject.path)
            : '';
        for (const project of (this.recentProjects || [])) {
            if (this._projectKey(project?.path || '') === playgroundKey) continue;
            addProject(project?.path || '', project?.name || '');
        }

        const allKeys = new Set(candidateOrder);
        const previousOrder = Array.isArray(this._projectRailOrder) ? this._projectRailOrder : [];
        const orderedKeys = [];
        const pushKey = (key) => {
            if (key && allKeys.has(key) && !orderedKeys.includes(key)) orderedKeys.push(key);
        };

        previousOrder.forEach(pushKey);
        candidateOrder.forEach(pushKey);
        this._projectRailOrder = orderedKeys;
        return orderedKeys.map(key => byKey.get(key)).filter(Boolean);
    }

    renderProjectRail() {
        if (!this.railProjects) return;
        const projects = this._getProjectRailItems();
        const currentPath = this._normalizeProjectPath(
            this._pendingProjectPath || this.currentCwd || '',
        );
        const currentKey = this._projectKey(currentPath);
        this.railProjects.innerHTML = '';

        for (const [index, project] of projects.entries()) {
            const isActive = project.key === currentKey;
            const [bg, bg2, border] = this._projectRailColor(index);
            const btn = document.createElement('button');
            btn.type = 'button';
            btn.className = `rail-project-icon${isActive ? ' active' : ''}`;
            btn.title = `${project.name}\n${project.path}`;
            btn.setAttribute('aria-label', isActive
                ? `${project.name} project, active`
                : `Open project ${project.name}`);
            btn.dataset.path = project.path;
            btn.style.setProperty('--rail-project-bg', bg);
            btn.style.setProperty('--rail-project-bg-2', bg2);
            btn.style.setProperty('--rail-project-border', border);
            btn.innerHTML = `<span class="rail-project-initials">${this.escapeHtml(this._projectInitials(project.name))}</span>`;
            if (project.count > 0) {
                const badge = document.createElement('span');
                badge.className = 'rail-project-count';
                badge.textContent = project.count > 99 ? '99+' : String(project.count);
                btn.appendChild(badge);
            }
            btn.addEventListener('click', (ev) => {
                ev.stopPropagation();
                this._selectRailProject(project.path);
            });
            btn.addEventListener('contextmenu', (ev) => {
                ev.preventDefault();
                ev.stopPropagation();
                this.showProjectContextMenu(ev, project);
            });
            this.railProjects.appendChild(btn);
        }
    }

    /**
     * Right-click menu for a sidebar project tile.
     *
     * Reuses the session context menu's markup and positioning so both feel
     * the same. Two actions:
     *
     *  - Rename: a display name for the sidebar only. The folder is not
     *    touched, and the name survives re-opening the project.
     *  - Remove: stops tracking the project. Deliberately NOT "Delete" — the
     *    folder and its sessions stay on disk and re-opening restores them.
     *    Labelled and confirmed in those terms so nobody reads it as
     *    destructive, and so nobody expects it to free disk space.
     *
     * The Playground project is permanent and cannot be removed.
     */
    showProjectContextMenu(e, project) {
        document.querySelector('.agent-context-menu')?.remove();
        if (!project || !project.path) return null;

        const menu = document.createElement('div');
        menu.className = 'agent-context-menu';
        const permanent = !!project.permanent;
        menu.innerHTML = `
            <div class="ctx-item" data-action="open">&#128194; Open</div>
            <div class="ctx-item" data-action="rename">&#9998; Rename</div>
            <div class="ctx-separator"></div>
            <div class="ctx-item${permanent ? ' is-disabled' : ' danger'}" data-action="forget">
                &#10006; Remove from sidebar
            </div>
        `;
        menu.style.left = `${e.clientX}px`;
        menu.style.top = `${e.clientY}px`;

        menu.addEventListener('click', (ev) => {
            const action = ev.target.closest('.ctx-item')?.dataset.action;
            if (action === 'open') {
                this._selectRailProject(project.path);
            } else if (action === 'rename') {
                const next = prompt('Rename project (sidebar label only):', project.name || '');
                if (next && next.trim() && next.trim() !== project.name) {
                    this.send({ command: 'rename_project', path: project.path, name: next.trim() });
                }
            } else if (action === 'forget') {
                if (permanent) {
                    this.showToastMessage('The Playground project cannot be removed.');
                } else if (confirm(
                    `Remove "${project.name}" from the sidebar?\n\n`
                    + 'The folder and its sessions stay on disk — reopening it brings them back.'
                )) {
                    this.send({ command: 'forget_project', path: project.path });
                }
            }
            menu.remove();
        });

        document.body.appendChild(menu);

        // Keep the menu inside the viewport — the rail sits at the left edge,
        // so the bottom overflow is the one that actually bites.
        const rect = menu.getBoundingClientRect();
        if (rect.right > window.innerWidth) {
            menu.style.left = `${window.innerWidth - rect.width - 8}px`;
        }
        if (rect.bottom > window.innerHeight) {
            menu.style.top = `${window.innerHeight - rect.height - 8}px`;
        }
        return menu;
    }

    _selectRailProject(path) {
        const norm = this._normalizeProjectPath(path);
        if (!norm) return;
        const confirmed = this._normalizeProjectPath(this.currentCwd || '');
        const cur = this._normalizeProjectPath(
            this._pendingProjectPath || this.currentCwd || '',
        );
        if (norm === cur) {
            this.renderProjectRail();
            return;
        }
        if (norm === confirmed && !this._pendingProjectSwitchId) {
            if (this._projectSwitchTimer) clearTimeout(this._projectSwitchTimer);
            this._projectSwitchTimer = null;
            this._pendingProjectPath = '';
            this._setProjectFilter(norm);
            this.renderProjectRail();
            return;
        }

        // Keep the last of a burst of rail clicks. The pending path makes the
        // highlight respond immediately without launching redundant indexing
        // and model-discovery work for every intermediate project.
        this._pendingProjectPath = norm;
        this._setProjectFilter(norm);
        this.renderProjectRail();
        if (this._projectSwitchTimer) clearTimeout(this._projectSwitchTimer);
        this._projectSwitchTimer = setTimeout(() => {
            this._projectSwitchTimer = null;
            this.selectProjectFolder(path);
        }, 120);
    }

    /**
     * Set or clear the project filter applied to the sidebar session tree.
     * Empty string clears the filter (show all projects).
     */
    _setProjectFilter(path) {
        const norm = (path || '').replace(/\\/g, '/');
        this._projectFilter = norm;
        this._pinnedOnly = false;  // picking a project clears the Pinned quick-filter
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

    /**
     * v0.6.6 — "Pinned" quick-filter from the sidebar filter dropdown.
     * Mutually exclusive with the per-project filter: turning it on clears
     * any project filter and shows only pinned sessions across all projects.
     */
    _setPinnedFilter(on) {
        this._pinnedOnly = !!on;
        this._projectFilter = '';
        // Treat this like an explicit user choice so init/cwd events don't
        // clobber the label or re-apply a project filter underneath it.
        this._projectFilterUserCleared = true;
        if (this.sidebarProjectSwitchLabel) {
            this.sidebarProjectSwitchLabel.textContent = on ? 'Pinned' : 'All projects';
        }
        this.renderFilteredSessions();
    }

    _shortenForMenu(path, max = 48) {
        const norm = (path || '').replace(/\\/g, '/');
        if (norm.length <= max) return norm;
        return '\u2026' + norm.slice(-(max - 1));
    }

    // ── New Session Setup ──────────────────────────────────────

    /**
     * New session is scoped to the active project. Project add/switch lives on
     * the rail + / Open Folder affordance, matching OpenCode's split.
     */
    startNewSession() {
        // This is an explicit navigation action, unlike background runtime
        // events guarded by showChatInterface().
        if (this.currentView !== 'agents') this.switchView('agents');
        if (this._newSessionInflight) {
            // Ignore click bursts while the first request is in flight. The
            // disabled primary button handles normal clicks; this also covers
            // keyboard/project-switcher paths without adding noisy toasts.
            return;
        }

        const selectedBackend = this.currentBackendName || ((this.modelSelector?.value || '').split(':')[0] || '');
        if (this.currentSessionId || selectedBackend) {
            this._newSessionInflight = true;
            this._newSessionRequestId = (
                globalThis.crypto?.randomUUID?.()
                || `new-${Date.now()}-${Math.random().toString(16).slice(2)}`
            );
            const button = document.getElementById('new-agent-btn');
            if (button) {
                button.disabled = true;
                button.setAttribute('aria-busy', 'true');
            }
            if (this._newSessionInflightTimer) clearTimeout(this._newSessionInflightTimer);
            this._newSessionInflightTimer = setTimeout(
                () => this._releaseNewSessionGuard(),
                10000,
            );
            this.send({
                command: 'clear',
                session_role: this.sessionRole || 'generator',
                request_id: this._newSessionRequestId,
            });
        } else if (this.currentCwd) {
            this.showCurrentProjectBackendSetup();
        } else {
            this.openProjectFolder();
        }
    }

    showNewSessionSetup() {
        if (this.agentPanel) this.agentPanel.style.display = 'none';
        else this.chatContainer.style.display = 'none';
        this.inputBar.style.display = 'none';
        if (this.settingsView) this.settingsView.style.display = 'none';

        this.welcomeScreen.style.display = 'flex';
        this.currentView = 'agents';
        document.querySelectorAll('.sidebar-icon-btn[data-view], .sidebar-nav-item[data-view], .sidebar-action[data-view], .rail-btn[data-view]').forEach(el =>
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
            this.openProjectFolder();
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
                this.openProjectFolder();
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

    _renderProjectResources(kind) {
        let dialog = document.getElementById('project-resources-dialog');
        const wasOpen = !!dialog?.open;
        if (!dialog) {
            dialog = document.createElement('dialog');
            dialog.id = 'project-resources-dialog';
            dialog.setAttribute('aria-labelledby', 'project-resources-title');
            document.body.appendChild(dialog);
        }
        const esc = value => this.escapeHtml(String(value || ''));
        const previews = kind === 'previews';
        dialog.innerHTML = `<header><h2 id="project-resources-title">${previews ? 'Project previews' : 'Project notes'}</h2><button data-close>Close</button></header>`;
        if (previews) {
            dialog.innerHTML += '<p>Previews stay available across tasks and project switches until stopped or Resonant closes.</p>';
            if (!this._managedPreviews?.length) dialog.innerHTML += '<p>No previews started for this project.</p>';
            for (const p of this._managedPreviews || []) {
                const section = document.createElement('section');
                section.innerHTML = `<h3>${esc(p.state)}</h3><a href="${esc(p.url)}" target="_blank" rel="noopener noreferrer">Open preview</a> <button data-stop ${p.state === 'stopped' ? 'disabled' : ''}>Stop preview</button><details><summary>Recent logs</summary><pre style="white-space:pre-wrap">${esc(p.logs) || 'No output yet'}</pre></details>`;
                section.querySelector('[data-stop]').onclick = () => this.send({command: 'preview_stop', id: p.id});
                dialog.appendChild(section);
            }
            const refresh = document.createElement('button'); refresh.textContent = 'Refresh status';
            refresh.onclick = () => this.send({command: 'preview_list'}); dialog.appendChild(refresh);
        } else {
            dialog.innerHTML += '<p>Small project facts and decisions with sources. Stale notes are excluded from recall. Model assertions still need verification.</p>';
            for (const note of this._projectNotes || []) {
                const section = document.createElement('section');
                section.innerHTML = `<p>${esc(note.text)}</p><small>${esc(note.kind)} · ${esc(note.confidence)} · ${note.stale ? 'Stale' : 'Source unchanged'} · ${esc(note.source)}</small><p><button data-edit>Edit</button> <button data-delete>Delete</button></p>`;
                section.querySelector('[data-edit]').onclick = () => {
                    const form = dialog.querySelector('form');
                    form.elements.id.value = note.id; form.elements.text.value = note.text;
                    form.elements.source.value = note.source; form.elements.kind.value = note.kind;
                    form.elements.sources.value = (note.sources || []).join(', '); form.elements.text.focus();
                };
                section.querySelector('[data-delete]').onclick = () => this.send({command: 'memory_delete', id: note.id});
                dialog.appendChild(section);
            }
            const form = document.createElement('form');
            form.innerHTML = '<input type="hidden" name="id"><p><label>Note <textarea name="text" required maxlength="1000" rows="3" style="width:100%"></textarea></label></p><p><label>Source <input name="source" required maxlength="300" placeholder="Decision in this task, or file and line"></label></p><p><label>Kind <select name="kind"><option value="decision">Decision</option><option value="fact">Fact</option><option value="constraint">Constraint</option><option value="procedure">Procedure</option></select></label></p><p><label>Source files <input name="sources" placeholder="Relative paths, separated by commas"></label></p><button type="submit">Save note</button>';
            form.onsubmit = e => { e.preventDefault(); const values = Object.fromEntries(new FormData(form)); this.send({command: 'memory_save', ...values, sources: values.sources.split(',').map(s => s.trim()).filter(Boolean)}); };
            dialog.appendChild(form);
        }
        dialog.querySelector('[data-close]').onclick = () => { this._showManagedPreviews = false; dialog.close(); };
        dialog.oncancel = () => { this._showManagedPreviews = false; };
        if (!dialog.open) dialog.showModal();
        else if (wasOpen) (dialog.querySelector('form textarea') || dialog.querySelector('[data-close]'))?.focus();
    }

    registerProjectFolder(path) {
        const cleanPath = (path || '').trim();
        if (!cleanPath) return;
        this.send({ command: 'register_project', path: cleanPath, open_after_add: true });
    }

    selectProjectFolder(path) {
        this._showManagedPreviews = false;
        document.getElementById('project-resources-dialog')?.remove();
        const title = document.getElementById('chat-session-title');
        if (title) title.textContent = 'New session';
        if (this._projectSwitchTimer) clearTimeout(this._projectSwitchTimer);
        this._projectSwitchTimer = null;
        const projectSwitchId = `project-${Date.now()}-${++this._projectSwitchSequence}`;
        this._latestProjectSwitchId = projectSwitchId;
        this._pendingProjectSwitchId = projectSwitchId;
        this._pendingProjectPath = this._normalizeProjectPath(path);
        this.send({
            command: 'set_project',
            path,
            project_switch_id: projectSwitchId,
        });

        const short = path.replace(/\\/g, '/').split('/').pop();
        this.headerProject.textContent = short;
        this.sidebarProjectName.textContent = short;
        this.sidebarCwd.textContent = path;
        this._updateHeaderProjectPath(this._pendingProjectPath);
        this._projectFilter = this._pendingProjectPath;
        this._pinnedOnly = false;
        this._projectFilterUserCleared = false;
        if (this.sidebarProjectSwitchLabel) this.sidebarProjectSwitchLabel.textContent = short;
        this.renderFilteredSessions();

        // Bug #7+#8 fix: project switch was leaving the chat panel and the
        // git pill showing the previous project's state. The set_project
        // command above gets the backend ready, but doesn't tell the
        // frontend to refresh dependent UI components.
        //
        // 1. Clear chat-panel messages immediately. The session_loaded event
        //    that follows set_project will re-render whatever's appropriate
        //    for the new project, but until then we want a clean slate
        //    rather than the previous project's last conversation lingering.
        if (this.chatMessages) {
            this.chatMessages.innerHTML = '';
            this._resetTaskCardState();
        }
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

    _releaseNewSessionGuard() {
        this._newSessionInflight = false;
        this._newSessionRequestId = '';
        if (this._newSessionInflightTimer) {
            clearTimeout(this._newSessionInflightTimer);
            this._newSessionInflightTimer = null;
        }
        const button = document.getElementById('new-agent-btn');
        if (button) {
            button.disabled = false;
            button.removeAttribute('aria-busy');
        }
    }

    _restoreConfirmedProjectSelection() {
        const path = this._normalizeProjectPath(this.currentCwd || '');
        if (path) {
            const short = this._projectNameFromPath(path);
            this.headerProject.textContent = short;
            this.sidebarProjectName.textContent = short;
            this.sidebarCwd.textContent = path;
            this._updateHeaderProjectPath(path);
            this._projectFilter = path;
            if (this.sidebarProjectSwitchLabel) {
                this.sidebarProjectSwitchLabel.textContent = short;
            }
        }
        this.renderFilteredSessions();
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

    // ── RESONANT.md Badge ───────────────────────────────────────

    updateResonantMdBadge() {
        if (!this.resonantMdBadge) return;
        if (this.resonantMd && this.resonantMd.exists) {
            this.resonantMdBadge.style.display = 'flex';
        } else {
            this.resonantMdBadge.style.display = 'none';
        }
    }

}


// ═══════════════════════════════════════════════════════════════════
//  Mixins
// ═══════════════════════════════════════════════════════════════════

/**
 * Fold a mixin class's methods onto ResonantApp.prototype.
 *
 * `Object.assign` is the obvious choice and the wrong one: class methods are
 * non-enumerable, so it would copy nothing and every mixed-in call would fail
 * at runtime with no import error to point at the cause. Descriptors copy
 * regardless of enumerability.
 *
 * Throws on a name collision rather than letting a mixin silently shadow a
 * real method — that failure is otherwise near-impossible to spot in review.
 */
function applyMixin(target, MixinClass, label) {
    if (!MixinClass) {
        throw new Error(`Mixin "${label}" is missing — check the script order in index.html.`);
    }
    const descriptors = Object.getOwnPropertyDescriptors(MixinClass.prototype);
    delete descriptors.constructor;
    for (const name of Object.keys(descriptors)) {
        if (Object.prototype.hasOwnProperty.call(target, name)) {
            throw new Error(`Mixin "${label}" would overwrite ResonantApp.${name}`);
        }
    }
    Object.defineProperties(target, descriptors);
}

applyMixin(ResonantApp.prototype, window.ResonantAutonomousView, 'autonomous-view');
applyMixin(ResonantApp.prototype, window.ResonantSettingsView, 'settings-view');
applyMixin(ResonantApp.prototype, window.ResonantRunCards, 'run-cards');


// ═══════════════════════════════════════════════════════════════════
//  Initialize
// ═══════════════════════════════════════════════════════════════════

document.addEventListener('DOMContentLoaded', () => {
    window.app = new ResonantApp();
});
