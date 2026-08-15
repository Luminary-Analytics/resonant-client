/*
 * Run-card and live-run rendering for ResonantApp.
 *
 * Everything that draws an agent turn while it happens and after it finishes:
 * the task card lifecycle, the live-run panel (clock, phase, todos, health,
 * status requests), collapsed tool-activity grouping, and the completion
 * summary including the expandable failure detail.
 *
 * Mixed into ResonantApp.prototype by applyMixin in app.js — see
 * autonomous_view.js for why a prototype mixin rather than an ES module, and
 * why Object.assign would silently copy nothing here.
 *
 * Load order matters: this file must load BEFORE app.js.
 */

class ResonantRunCards {


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
                errorCount: 0,
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
        } else if (name === 'bash') {
            const command = String(args.command || '');
            desc = `<code>${this.escapeHtml(command.length > 180 ? command.slice(0, 177) + '...' : command)}</code>`;
        } else {
            desc = this.escapeHtml(info.label);
        }

        const line = document.createElement('div');
        line.className = 'tool-inline evidence-item pending';
        line.setAttribute('role', 'button');
        line.setAttribute('tabindex', '0');
        line.innerHTML = `
            <span class="tool-icon" style="color:var(--${info.color})">${info.icon}</span>
            <span class="tool-desc">${desc}</span>
            <span class="tool-meta"></span>
            <span class="tool-status" style="color:var(--muted)">…</span>
        `;
        const toggleOutput = () => {
            if (!line.classList.contains('has-output')) return;
            line.classList.toggle('show-output');
            line.setAttribute(
                'aria-expanded', line.classList.contains('show-output') ? 'true' : 'false'
            );
        };
        line.addEventListener('click', toggleOutput);
        line.addEventListener('keydown', (event) => {
            if (event.key === 'Enter' || event.key === ' ') {
                event.preventDefault();
                toggleOutput();
            }
        });
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
            if (live.errorCount) live.container.classList.add('expanded');
            const icon = live.header && live.header.querySelector('.collapsed-icon');
            if (icon && live.errorCount) icon.textContent = '\u25be';
            if (icon) icon.textContent = '▸';
            if (icon) icon.textContent = live.errorCount ? '\u25be' : '\u25b8';
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


    _formatRunDuration(seconds) {
        const s = Math.max(0, Math.floor(seconds || 0));
        if (s < 60) return `${s}s`;
        const m = Math.floor(s / 60);
        const r = s % 60;
        return r ? `${m}m ${r}s` : `${m}m`;
    }


    /**
     * Attach the full failure text to a run summary as an expandable block.
     *
     * `.task-run-detail` is a single-line ellipsis clamp, which suits a status
     * line but silently eats the diagnostic half of a provider error — the
     * message names the failing constraint at the END ("...must be followed by
     * tool messages responding to each tool_call_id"), so the clamp removes
     * exactly the part worth reading. The text is already here in full; only
     * the presentation was lossy.
     *
     * Collapsed by default so a healthy transcript stays scannable.
     */
    _attachFailureDetail(summary, detail) {
        const text = String(detail || '');
        const wrap = document.createElement('div');
        wrap.className = 'task-failure-detail';

        const toggle = document.createElement('button');
        toggle.type = 'button';
        toggle.className = 'task-failure-toggle';
        toggle.setAttribute('aria-expanded', 'false');
        toggle.textContent = 'Show full error';

        const body = document.createElement('pre');
        body.className = 'task-failure-body';
        body.hidden = true;
        body.textContent = text;

        const copy = document.createElement('button');
        copy.type = 'button';
        copy.className = 'task-failure-copy';
        copy.textContent = 'Copy';
        copy.addEventListener('click', (e) => {
            e.stopPropagation();
            // Errors get pasted into issues and searched for; make that one click.
            navigator.clipboard?.writeText(text).then(() => {
                copy.textContent = 'Copied';
                setTimeout(() => { copy.textContent = 'Copy'; }, 1200);
            }).catch(() => this.showToastMessage('Could not copy to clipboard.'));
        });

        toggle.addEventListener('click', (e) => {
            e.stopPropagation();
            const open = body.hidden;
            body.hidden = !open;
            toggle.setAttribute('aria-expanded', String(open));
            toggle.textContent = open ? 'Hide full error' : 'Show full error';
        });

        wrap.append(toggle, copy, body);
        summary.insertAdjacentElement('afterend', wrap);
        return wrap;
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
                // Open the git popover directly — the header badge it used
                // to click was removed in v0.6.7.
                if (this.gitData && this.gitData.is_repo) this.toggleGitPopover();
                else this.showStatusMessage('Not a git repository — nothing to review.');
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

        // Produced files are first-class deliverables: open them directly.
        el.querySelectorAll('[data-file-path]').forEach((codeEl) => {
            codeEl.style.cursor = 'pointer';
            codeEl.addEventListener('click', () => {
                const p = codeEl.dataset.filePath || '';
                this._openWorkspacePath(p);
            });
        });

        this.chatMessages.appendChild(el);
        this._removeLiveAgentTodoStrip();
    }


    _renderTaskCompletionSummary(event = {}) {
        const task = this._activeTask;
        if (!task || !task.card) return;

        const errored = !!this._agentRunErrored;
        const t = this._currentTurn || {};
        const elapsed = event.total_elapsed || t.totalElapsed || 0;
        const steps = event.total_steps || t.stepCount || 0;
        const tools = t.toolCallCount || 0;
        const todos = (this._agentRunSummary && this._agentRunSummary.todos) || null;
        const evidence = event.evidence || {};
        const files = (this._agentRunSummary && this._agentRunSummary.fileChanges) || [];
        for (const rawPath of (evidence.changed_files || [])) {
            const path = String(rawPath || '').replace(/\\/g, '/').trim();
            if (path && !files.some((entry) => entry.path === path)) {
                files.push({ path, tool: 'evidence', detail: 'Changed' });
            }
        }
        const hasVisibleResult = !!(task.resultEl && task.resultEl.textContent.trim());
        const legacyOutcome = files.length > 0
            ? 'changed_unverified'
            : (hasVisibleResult ? 'answered' : 'incomplete');
        const outcome = errored ? 'failed' : (event.outcome || legacyOutcome);
        const outcomeMeta = {
            answered: { label: 'Answered', mark: 'OK', state: 'is-done', card: 'task-card-done' },
            changed_verified: { label: 'Changed & verified', mark: 'OK', state: 'is-done', card: 'task-card-done' },
            changed_unverified: { label: 'Changed — verify', mark: '!', state: 'is-warning', card: 'task-card-warning' },
            no_changes_needed: { label: 'No changes needed', mark: 'OK', state: 'is-done', card: 'task-card-done' },
            needs_input: { label: 'Needs input', mark: '?', state: 'is-warning', card: 'task-card-warning' },
            incomplete: { label: 'Needs attention', mark: '!', state: 'is-warning', card: 'task-card-warning' },
            failed: { label: 'Failed', mark: '!', state: 'is-error', card: 'task-card-error' },
        }[outcome] || { label: 'Completed', mark: 'OK', state: 'is-done', card: 'task-card-done' };

        task.card.classList.remove('task-card-running', 'task-card-done', 'task-card-error', 'task-card-warning');
        task.card.classList.add(outcomeMeta.card);
        task.card.dataset.outcome = outcome;
        if (task.stateEl) {
            task.stateEl.className = `task-card-state ${outcomeMeta.state}`;
            task.stateEl.textContent = outcomeMeta.label;
        }

        const parts = [];
        if (steps > 0) parts.push(`${steps} step${steps === 1 ? '' : 's'}`);
        if (tools > 0) parts.push(`${tools} tool${tools === 1 ? '' : 's'}`);
        if (files.length > 0) parts.push(`${files.length} file${files.length === 1 ? '' : 's'}`);
        if ((evidence.validation_tools || []).length > 0) {
            parts.push(`validated with ${evidence.validation_tools.join(', ')}`);
        }
        if (todos && todos.total > 0) parts.push(`${todos.done || 0}/${todos.total} to-dos`);
        if (elapsed > 0) parts.push(this._formatRunDuration(elapsed));

        const summary = document.createElement('div');
        summary.className = `task-run-summary ${outcomeMeta.state}`;
        const outcomeDetails = {
            failed: this._agentRunErrorMessage || 'The turn stopped before producing a usable result.',
            incomplete: evidence.requires_workspace_change
                ? 'The request asked for a workspace change, but no successful edit was recorded.'
                : 'The turn ended without a visible result.',
            changed_unverified: 'Files changed, but no successful validation ran afterward.',
            needs_input: 'The agent needs a decision before it can continue.',
        };
        const detail = outcomeDetails[outcome] || (parts.length ? parts.join(' | ') : outcomeMeta.label);
        summary.innerHTML = `
            <span class="task-run-mark">${outcomeMeta.mark}</span>
            <span class="task-run-label">${this.escapeHtml(outcomeMeta.label)}</span>
            <span class="task-run-detail">${this.escapeHtml(detail)}</span>
        `;

        // A one-line ellipsis is right for "3 steps | 2 tools" and actively
        // harmful for a provider error: the part that says WHY is always the
        // part that gets cut. Failures get an expandable, copyable full text.
        if (outcome === 'failed' && detail) {
            this._attachFailureDetail(summary, detail);
        }

        if (files.length > 0 && outcome !== 'failed') {
            const review = document.createElement('button');
            review.type = 'button';
            review.className = 'task-review-btn';
            review.textContent = 'Review';
            review.addEventListener('click', (e) => {
                e.stopPropagation();
                if (this.gitData && this.gitData.is_repo) this.toggleGitPopover();
                else this.showStatusMessage('Not a git repository; nothing to review.');
            });
            summary.appendChild(review);
        }

        if (['incomplete', 'failed', 'changed_unverified'].includes(outcome) && !this._replay) {
            const actions = document.createElement('span');
            actions.className = 'task-recovery-actions';
            actions.innerHTML = `
                <button type="button" class="task-review-btn" data-recovery="retry">Retry</button>
                <button type="button" class="task-review-btn" data-recovery="alternate">Retry another model</button>
                <button type="button" class="task-review-btn" data-recovery="continue">${outcome === 'changed_unverified' ? 'Verify changes' : 'Continue'}</button>
            `;
            actions.querySelector('[data-recovery="retry"]')?.addEventListener('click', () => {
                this._retryTask(task, { mode: 'retry' });
            });
            actions.querySelector('[data-recovery="alternate"]')?.addEventListener('click', () => {
                this._retryTask(task, { mode: 'retry', alternate: true });
            });
            actions.querySelector('[data-recovery="continue"]')?.addEventListener('click', () => {
                this._retryTask(task, { mode: 'continue' });
            });
            summary.appendChild(actions);
        }

        task.footerEl.hidden = false;
        task.footerEl.prepend(summary);

        if (files.length > 0) {
            const changes = document.createElement('details');
            changes.className = 'task-change-list';
            changes.innerHTML = `
                <summary>Changed files</summary>
                <ol>${files.slice(0, 12).map((f) => `
                    <li>
                        <code class="task-change-path" data-file-path="${this.escapeHtml(f.path)}" title="Open ${this.escapeHtml(f.path)}">${this.escapeHtml(this.shortenPath(f.path))}</code>
                        ${f.detail ? `<span>${this.escapeHtml(f.detail)}</span>` : ''}
                    </li>
                `).join('')}</ol>
                ${files.length > 12 ? `<div class="task-change-more">${files.length - 12} more</div>` : ''}
            `;
            changes.querySelectorAll('[data-file-path]').forEach((codeEl) => {
                codeEl.addEventListener('click', (clickEvent) => {
                    clickEvent.stopPropagation();
                    this._openWorkspacePath(codeEl.dataset.filePath || '');
                });
            });
            task.footerEl.appendChild(changes);
        }
    }


    _retryTask(task, { mode = 'retry', alternate = false, auto = false } = {}) {
        if (this.isRunning || !task) return;
        if (alternate && !this._selectAlternateModelValue()) {
            this.showStatusMessage('No alternate model is currently available.');
            if (auto) return;
        }
        const original = (task.requestText || '').trim();
        const prompt = mode === 'continue'
            ? 'Continue the previous request. Verify any changes already made and report concrete evidence.'
            : original;
        if (!prompt) return;
        this.userInput.value = prompt;
        this.userInput.style.height = 'auto';
        this.sendMessage({ autoRetry: auto });
    }


    _resetTaskCardState() {
        this._activeTask = null;
        this.activeTaskCard = null;
        this.activeTaskActivityEl = null;
        this.activeTaskResultEl = null;
        this.activeTaskFooterEl = null;
    }


    _beginTaskCard(text, images = [], options = {}) {
        this._removeChatEmptyState();

        const requestText = (text || '').trim() || 'Task';
        const card = document.createElement('article');
        card.className = 'task-card task-card-running';
        card.dataset.userMessage = options.synthetic ? 'synthetic' : 'true';

        const header = document.createElement('div');
        header.className = 'task-card-header';

        const state = document.createElement('span');
        state.className = 'task-card-state is-running';
        state.textContent = 'Running';

        const main = document.createElement('div');
        main.className = 'task-card-main';

        const label = document.createElement('div');
        label.className = 'task-card-label';
        label.textContent = options.synthetic ? 'Resonant' : 'You';

        const request = document.createElement('div');
        request.className = 'task-request-text';
        request.textContent = requestText;

        main.appendChild(label);
        main.appendChild(request);
        this._appendTaskImages(main, images);

        const actions = document.createElement('div');
        actions.className = 'task-card-actions';
        if (!options.synthetic) {
            actions.innerHTML = `
                <button class="msg-action-btn task-fork-btn" data-action="fork" title="Fork from this task" aria-label="Fork from this task">
                    <svg width="14" height="14" viewBox="0 0 14 14" fill="none" aria-hidden="true">
                        <circle cx="3" cy="3" r="1.5" stroke="currentColor" stroke-width="1.1"/>
                        <circle cx="11" cy="3" r="1.5" stroke="currentColor" stroke-width="1.1"/>
                        <circle cx="7" cy="11" r="1.5" stroke="currentColor" stroke-width="1.1"/>
                        <path d="M3 4.5V7c0 1 .8 1.8 1.8 1.8h4.4c1 0 1.8-.8 1.8-1.8V4.5" stroke="currentColor" stroke-width="1.1" fill="none"/>
                        <path d="M7 8.8v.7" stroke="currentColor" stroke-width="1.1"/>
                    </svg>
                </button>`;
            actions.querySelector('[data-action="fork"]')?.addEventListener('click', () => {
                this._forkFromUserMessage(card);
            });
        }

        header.appendChild(state);
        header.appendChild(main);
        header.appendChild(actions);

        const activity = document.createElement('div');
        activity.className = 'task-activity';

        const result = document.createElement('div');
        result.className = 'task-result';
        result.hidden = true;

        const footer = document.createElement('div');
        footer.className = 'task-card-footer';
        footer.hidden = true;

        card.appendChild(header);
        card.appendChild(activity);
        card.appendChild(result);
        card.appendChild(footer);
        this.chatMessages.appendChild(card);

        const task = {
            card,
            stateEl: state,
            activityEl: activity,
            resultEl: result,
            liveEl: this.liveRunSurface,
            footerEl: footer,
            requestText,
            startedAt: performance.now(),
        };
        this._setActiveTask(task);
        this.scrollToBottom();
        return task;
    }


    _ensureTaskCard(label = 'Task') {
        if (this._activeTask && this._activeTask.card && this._activeTask.card.isConnected) {
            return this._activeTask;
        }
        return this._beginTaskCard(label, [], { synthetic: true });
    }


    _startLiveRun(event = {}) {
        if (this.isReplaying) return;
        const task = this._activeTask;
        if (!task || !task.liveEl) return;
        if (this._liveRun && this._liveRun.active && this._liveRun.el === task.liveEl) {
            if (event.model) this._liveRun.model = event.model;
            this._renderLiveRun();
            return;
        }
        if (this._liveRunTimer) clearInterval(this._liveRunTimer);
        this._liveRun = {
            active: true,
            el: task.liveEl,
            startedAt: Date.now(),
            phase: 'Starting',
            detail: 'Preparing the workspace and model',
            step: 0,
            model: event.model || this.lastModel || '',
            milestones: [{ id: 'analyze', text: 'Understand the request', status: 'running' }],
            modelTodos: false,
            subtasks: new Map(),
            toolActivities: new Map(),
            activeTool: null,
            currentAction: 'Preparing the workspace and model',
            lastCompleted: null,
            completedTools: 0,
            detailsOpen: false,
            provider: '',
            lastProgressAt: null,
            lastTransportAt: null,
            idleTimeoutSeconds: 0,
            progressWarningSeconds: 120,
            statusVisible: false,
            statusRequestId: '',
            statusRequestState: '',
            statusRequestTimer: null,
            statusNote: '',
        };
        task.liveEl.hidden = false;
        task.liveEl.classList.remove('is-finishing', 'is-error');
        this._renderLiveRun();
        this._liveRunTimer = setInterval(() => this._updateLiveRunClock(), 1000);
        this.scrollToBottom();
    }


    _stopLiveRun() {
        if (this._liveRunTimer) clearInterval(this._liveRunTimer);
        this._liveRunTimer = null;
        if (this._liveRun?.statusRequestTimer) {
            clearTimeout(this._liveRun.statusRequestTimer);
        }
        if (this._liveRun && this._liveRun.active) {
            this._liveRun.active = false;
            if (this._liveRun.el) this._liveRun.el.hidden = true;
        }
        this._liveRun = null;
    }


    _completeLiveRun(errored = false) {
        const run = this._liveRun;
        if (!run || !run.el) return;
        if (this._liveRunTimer) clearInterval(this._liveRunTimer);
        this._liveRunTimer = null;
        if (run.statusRequestTimer) clearTimeout(run.statusRequestTimer);
        run.active = false;
        run.phase = errored ? 'Stopped' : 'Complete';
        run.detail = errored ? 'The run ended before completion' : 'Finalizing the result';
        run.milestones = run.milestones.map((item) => ({
            ...item,
            status: errored && item.status === 'running' ? 'error' : 'done',
        }));
        if (errored) run.el.classList.add('is-error');
        this._renderLiveRun();
        run.el.classList.add('is-finishing');
        setTimeout(() => {
            if (run.el) run.el.hidden = true;
            if (this._liveRun === run) this._liveRun = null;
        }, 420);
    }


    _liveRunFallbackTask(phase, detail = '') {
        const normalized = String(phase || '').toLowerCase();
        if (normalized.includes('stop')) return 'Stop the active run';
        if (normalized === 'delegating') return 'Coordinate sub-tasks';
        if (normalized === 'steered') return 'Apply your new direction';
        if (normalized === 'composing') return 'Finish the response';
        if (normalized === 'continuing') return 'Reason through the next agent step';
        if (normalized === 'reasoning' || normalized === 'starting') return 'Reason through the next action';
        return detail || phase || 'Continue the task';
    }


    _liveRunCompactValue(value, limit = 72) {
        const compact = String(value || '').replace(/\s+/g, ' ').trim();
        if (compact.length <= limit) return compact;
        return `${compact.slice(0, Math.max(1, limit - 1)).trimEnd()}…`;
    }


    _liveRunPathLabel(value) {
        const normalized = String(value || '').replace(/\\/g, '/').replace(/\/$/, '');
        return this._liveRunCompactValue(normalized.split('/').pop() || normalized || 'file', 54);
    }


    _liveRunToolActivity(name, args = {}) {
        const tool = String(name || '').toLowerCase();
        const path = this._liveRunPathLabel(args.path || args.file_path || args.filename || '');
        const target = this._liveRunCompactValue(args.path || args.directory || '.', 42);
        const pattern = this._liveRunCompactValue(args.pattern || args.query || '', 44);
        const command = this._liveRunCompactValue(args.command || args.cmd || '', 70);
        const quoted = (value) => value ? `“${value}”` : '';
        const activities = {
            file_read: [`Reading ${path}`, `Read ${path}`],
            file_write: [`Writing ${path}`, `Wrote ${path}`],
            file_edit: [`Editing ${path}`, `Edited ${path}`],
            apply_patch: ['Applying a workspace patch', 'Applied a workspace patch'],
            glob: [`Finding files matching ${quoted(pattern) || 'the requested pattern'}`, `Found files matching ${quoted(pattern) || 'the requested pattern'}`],
            grep: [`Searching ${target} for ${quoted(pattern) || 'matching code'}`, `Searched ${target} for ${quoted(pattern) || 'matching code'}`],
            git_status: ['Checking repository status', 'Checked repository status'],
            git_diff: ['Reviewing workspace changes', 'Reviewed workspace changes'],
            git_commit: ['Creating a Git commit', 'Created a Git commit'],
            bash: [`Running ${command || 'a terminal command'}`, `Ran ${command || 'a terminal command'}`],
            browser_navigate: [`Opening ${this._liveRunCompactValue(args.url || 'a page', 58)}`, `Opened ${this._liveRunCompactValue(args.url || 'a page', 58)}`],
            browser_read: ['Inspecting the current page', 'Inspected the current page'],
            browser_screenshot: ['Capturing the current page', 'Captured the current page'],
            computer_screenshot: ['Capturing the desktop', 'Captured the desktop'],
            search_tools: [`Finding a tool for ${quoted(pattern) || 'the next action'}`, `Found tools for ${quoted(pattern) || 'the next action'}`],
            task: [
                args.prompt
                    ? `Delegating ${quoted(this._liveRunCompactValue(args.prompt, 54))}`
                    : 'Starting a delegated sub-task',
                'Finished a delegated sub-task',
            ],
            task_batch: [
                Array.isArray(args.tasks)
                    ? `Starting ${args.tasks.length} delegated sub-tasks`
                    : 'Starting delegated sub-tasks',
                'Finished delegated sub-tasks',
            ],
            director_plan: ['Building the Director task graph', 'Built the Director task graph'],
            director_status: ['Reviewing team progress', 'Reviewed team progress'],
            director_validate: ['Validating worker evidence', 'Validated worker evidence'],
            director_decide: ['Reviewing a worker result', 'Reviewed a worker result'],
            director_complete: ['Finalizing the Director run', 'Finalized the Director run'],
        };
        const pair = activities[tool];
        if (pair) return { active: pair[0], completed: pair[1] };
        const label = this._liveRunCompactValue(
            (getToolInfo(name).label || name || 'tool').replace(/\s+/g, ' '),
            64,
        );
        return { active: `Using ${label}`, completed: `Finished ${label}` };
    }


    _setLiveRunPhase(phase, detail = '', step = null) {
        const run = this._liveRun;
        if (!run || !run.active) return;
        run.phase = phase || run.phase;
        run.detail = detail || run.detail;
        run.currentAction = run.detail;
        if (step !== null) run.step = step;
        // When evidence-derived milestones are complete, keep one fallback
        // task active and synchronize its label with the real phase above.
        // Previously this was permanently named "Finish the response", even
        // when the agent had returned to reasoning on a later step.
        if (!run.modelTodos && run.milestones.length) {
            const fallbackText = this._liveRunFallbackTask(run.phase, run.detail);
            const evidenceMilestones = run.milestones.filter((item) => item.id !== 'finalize');
            const hasActiveEvidence = evidenceMilestones.some((item) => item.status === 'running');
            let finalStep = run.milestones.find((item) => item.id === 'finalize');
            if (finalStep && finalStep.status === 'running') {
                finalStep.text = fallbackText;
            } else if (!hasActiveEvidence
                && evidenceMilestones.every((item) => item.status === 'done')) {
                if (!finalStep) {
                    finalStep = { id: 'finalize', text: fallbackText, status: 'running' };
                    run.milestones.push(finalStep);
                } else {
                    finalStep.text = fallbackText;
                    finalStep.status = 'running';
                }
            }
        }
        this._renderLiveRun();
    }


    _setLiveRunTodos(items, done, total) {
        const run = this._liveRun;
        if (!run || !run.active || total <= 0) return;
        run.modelTodos = true;
        run.milestones = (items || []).map((item, index) => ({
            id: `todo-${index}`,
            text: item.text || `Task ${index + 1}`,
            status: item.done ? 'done' : (index === done ? 'running' : 'pending'),
        }));
        this._renderLiveRun();
    }


    _liveRunHealthText(run = this._liveRun) {
        if (!run) return 'No active run';
        const now = Date.now();
        const parts = [];
        if (run.provider === 'exo') {
            const transportAge = run.lastTransportAt
                ? Math.max(0, Math.floor((now - run.lastTransportAt) / 1000))
                : null;
            parts.push(
                transportAge !== null && transportAge <= 15
                    ? 'EXO connection active'
                    : transportAge === null
                        ? 'Waiting for first EXO stream signal'
                        : `No EXO stream signal for ${transportAge}s`,
            );
            if (run.lastProgressAt) {
                const progressAge = Math.max(
                    0,
                    Math.floor((now - run.lastProgressAt) / 1000),
                );
                parts.push(`last model progress ${progressAge}s ago`);
            }
        } else {
            parts.push(`${run.phase || 'Working'} now`);
        }
        if (run.step) parts.push(`step ${run.step}`);
        parts.push(`${run.completedTools} tool${run.completedTools === 1 ? '' : 's'} finished`);
        parts.push(`running ${this._formatRunDuration((now - run.startedAt) / 1000)}`);
        return parts.join(' · ');
    }


    _requestLiveRunStatus() {
        const run = this._liveRun;
        if (!run || !run.active) {
            this.showStatusMessage('There is no active run to check.');
            return;
        }

        // This snapshot is synchronous and remains useful even if the model
        // is blocked inside a long provider generation.
        run.statusVisible = true;
        run.statusNote = 'Local health refreshed';
        if (['sending', 'queued'].includes(run.statusRequestState)) {
            this._renderLiveRun();
            return;
        }

        const messageId = (
            globalThis.crypto?.randomUUID?.()
            || `status-${Date.now()}-${Math.random()}`
        );
        run.statusRequestId = messageId;
        run.statusRequestState = 'sending';
        run.statusNote = 'Requesting a concise agent update';
        this._renderLiveRun();
        this.send({ command: 'status_update', message_id: messageId });

        if (run.statusRequestTimer) clearTimeout(run.statusRequestTimer);
        run.statusRequestTimer = setTimeout(() => {
            if (
                this._liveRun === run
                && run.statusRequestId === messageId
                && run.statusRequestState === 'sending'
            ) {
                run.statusRequestState = 'failed';
                run.statusNote = 'Agent update was not acknowledged; local health is still live';
                this._renderLiveRun();
            }
        }, 8000);
    }


    _updateLiveRunClock() {
        const run = this._liveRun;
        if (!run || !run.el) return;
        const elapsed = run.el.querySelector('[data-live-elapsed]');
        if (elapsed) elapsed.textContent = this._formatRunDuration((Date.now() - run.startedAt) / 1000);
        if (
            run.provider === 'exo'
            && run.lastProgressAt
            && ['Starting', 'Ready', 'Reading context', 'Reasoning', 'Composing', 'Recovering']
                .includes(run.phase)
        ) {
            const now = Date.now();
            const idleFor = Math.max(0, Math.floor((now - run.lastProgressAt) / 1000));
            const model = this._liveRunCompactValue(run.model || 'EXO', 44);
            const transportAge = run.lastTransportAt
                ? Math.max(0, Math.floor((now - run.lastTransportAt) / 1000))
                : null;
            const connection = transportAge !== null && transportAge <= 15
                ? 'EXO connection active'
                : 'waiting for EXO stream data';
            const hardLimit = run.idleTimeoutSeconds > 0
                ? ` · automatic stop in ${Math.max(0, run.idleTimeoutSeconds - idleFor)}s`
                : '';
            const activity = idleFor < 2
                ? `${model} is producing output`
                : `${run.currentAction || run.detail} · ${connection} · last model progress ${idleFor}s ago${hardLimit}`;
            const nowEl = run.el.querySelector('[data-live-now]');
            if (nowEl) {
                const warningAt = Math.max(15, run.progressWarningSeconds || 120);
                const phase = idleFor >= warningAt && run.phase !== 'Recovering'
                    ? 'Still working'
                    : run.phase;
                const nowText = `${phase} · ${activity}`;
                nowEl.textContent = nowText;
                nowEl.title = nowText;
            }
        }
        const healthEl = run.el.querySelector('[data-live-health]');
        if (healthEl && run.statusVisible) {
            healthEl.textContent = [
                this._liveRunHealthText(run),
                run.statusNote,
            ].filter(Boolean).join(' · ');
            healthEl.title = healthEl.textContent;
        }
        run.el.querySelectorAll('[data-subtask-elapsed]').forEach((el) => {
            const item = run.subtasks.get(el.dataset.subtaskElapsed);
            if (item && item.status === 'running') {
                el.textContent = this._formatRunDuration((Date.now() - item.startedAt) / 1000);
            }
        });
    }


    _renderLiveRun() {
        const run = this._liveRun;
        if (!run || !run.el) return;
        // Streaming models can emit hundreds of text deltas while the visible
        // run state remains unchanged. Replacing the entire dock for each one
        // restarts its animations and makes the surface flash. Only rebuild
        // when user-visible state has actually changed; elapsed clocks update
        // their own text nodes in _updateLiveRunClock().
        const renderKey = JSON.stringify({
            phase: run.phase,
            detail: run.detail,
            step: run.step,
            currentAction: run.currentAction,
            lastCompleted: run.lastCompleted,
            completedTools: run.completedTools,
            milestones: run.milestones,
            subtasks: Array.from(run.subtasks.values()),
            detailsOpen: run.detailsOpen,
            statusVisible: run.statusVisible,
            statusRequestState: run.statusRequestState,
            statusNote: run.statusNote,
        });
        if (run.renderKey === renderKey) return;
        run.renderKey = renderKey;
        const elapsedSeconds = Math.max(0, (Date.now() - run.startedAt) / 1000);
        const complete = run.milestones.filter((item) => item.status === 'done').length;
        const total = run.milestones.length;
        const pct = total ? Math.round((complete / total) * 100) : 0;
        const subtasks = Array.from(run.subtasks.values());
        const activeSubtasks = subtasks.filter((item) => item.status === 'running').length;
        const milestoneOrder = {
            analyze: 0, inspect: 1, change: 2, verify: 3,
            reason: 4, delegate: 5, report: 6, finalize: 7,
        };
        const orderedMilestones = [...run.milestones].sort((a, b) => (
            (milestoneOrder[a.id] ?? 99) - (milestoneOrder[b.id] ?? 99)
        ));
        const milestoneHtml = orderedMilestones.map((item) => {
            const glyph = item.status === 'done' ? '\u2713' : item.status === 'error' ? '!' : item.status === 'running' ? '' : '\u00b7';
            return `<li class="live-run-todo is-${item.status}"><span class="live-run-check">${glyph}</span><span>${this.escapeHtml(item.text)}</span></li>`;
        }).join('');
        const subtaskHtml = subtasks.map((item) => {
            const meta = item.status === 'done'
                ? `${item.steps || 0} steps \u00b7 ${this._formatRunDuration(item.elapsed || 0)}`
                : this._formatRunDuration((Date.now() - (item.startedAt || Date.now())) / 1000);
            return `<li class="live-run-subtask is-${item.status}">
                <span class="live-run-subtask-pulse"></span>
                <span class="live-run-subtask-copy"><strong>${this.escapeHtml(item.label || 'Sub-task')}</strong><small>${this.escapeHtml((item.prompt || '').slice(0, 96))}</small></span>
                <span class="live-run-subtask-meta" data-subtask-elapsed="${this.escapeHtml(item.id)}">${meta}</span>
            </li>`;
        }).join('');
        // Keep the animated shell mounted for the entire run. Replacing this
        // subtree on every phase/tool event restarts its CSS animations and
        // presents as hard flicker. Updates below patch only changing nodes.
        if (!run.domReady || !run.el.querySelector('.live-run-head')) {
            run.el.innerHTML = `
                <div class="live-run-head">
                    <button type="button" class="live-run-toggle" aria-expanded="false" aria-label="Show run details">
                        <span class="live-run-orbit" aria-hidden="true"><i></i><b></b></span>
                        <span class="live-run-copy">
                            <strong>Resonant is working</strong>
                            <small data-live-now></small>
                            <span class="live-run-latest" data-live-latest hidden></span>
                            <span class="live-run-health" data-live-health hidden></span>
                        </span>
                        <span class="live-run-meta"><span data-live-step></span><span data-live-elapsed></span></span>
                        <span class="live-run-chevron" aria-hidden="true"></span>
                    </button>
                    <button type="button" class="live-run-status-check" title="Show live health and ask the agent for a concise update">Check status</button>
                </div>
                <div class="live-run-body" hidden>
                    <div class="live-run-progress"><span></span></div>
                    <div class="live-run-details">
                        <div class="live-run-details-summary"><span>Run details</span><span data-live-counts></span></div>
                        <div class="live-run-detail-grid">
                            <section><h4>Task list</h4><ol class="live-run-todos"></ol></section>
                            <section data-live-subtasks-section hidden><h4>Sub-tasks</h4><ol class="live-run-subtasks"></ol></section>
                        </div>
                    </div>
                </div>
            `;
            const toggle = run.el.querySelector('.live-run-toggle');
            const body = run.el.querySelector('.live-run-body');
            const setDetailsOpen = (open) => {
                run.detailsOpen = open;
                toggle.setAttribute('aria-expanded', String(open));
                toggle.setAttribute('aria-label', open ? 'Hide run details' : 'Show run details');
                body.hidden = !open;
            };
            setDetailsOpen(run.detailsOpen);
            toggle.addEventListener('click', () => {
                setDetailsOpen(!run.detailsOpen);
            });
            run.el.querySelector('.live-run-status-check')?.addEventListener(
                'click',
                () => this._requestLiveRunStatus(),
            );
            run.domReady = true;
        }

        const nowEl = run.el.querySelector('[data-live-now]');
        const nowText = `${run.phase} \u00b7 ${run.currentAction || run.detail}`;
        nowEl.textContent = nowText;
        nowEl.title = nowText;
        const latestEl = run.el.querySelector('[data-live-latest]');
        if (run.lastCompleted) {
            const duration = run.lastCompleted.elapsed > 0
                ? ` \u00b7 ${this._formatRunDuration(run.lastCompleted.elapsed)}`
                : '';
            const toolCount = ` \u00b7 ${run.completedTools} tool${run.completedTools === 1 ? '' : 's'} finished`;
            latestEl.textContent = `Latest \u00b7 ${run.lastCompleted.text}${duration}${toolCount}`;
            latestEl.title = latestEl.textContent;
            latestEl.hidden = false;
            latestEl.classList.toggle('is-error', Boolean(run.lastCompleted.failed));
        } else {
            latestEl.hidden = true;
            latestEl.textContent = '';
            latestEl.classList.remove('is-error');
        }
        const healthEl = run.el.querySelector('[data-live-health]');
        healthEl.hidden = !run.statusVisible;
        healthEl.textContent = run.statusVisible
            ? [this._liveRunHealthText(run), run.statusNote].filter(Boolean).join(' · ')
            : '';
        healthEl.title = healthEl.textContent;
        const statusButton = run.el.querySelector('.live-run-status-check');
        const statusPending = ['sending', 'queued'].includes(run.statusRequestState);
        statusButton.disabled = statusPending;
        statusButton.textContent = run.statusRequestState === 'sending'
            ? 'Checking…'
            : run.statusRequestState === 'queued'
                ? 'Update queued'
                : run.statusVisible
                    ? 'Refresh status'
                    : 'Check status';
        run.el.querySelector('[data-live-step]').textContent = run.step ? `Step ${run.step} \u00b7 ` : '';
        run.el.querySelector('[data-live-elapsed]').textContent = this._formatRunDuration(elapsedSeconds);
        run.el.querySelector('.live-run-progress').style.setProperty('--live-run-progress', `${pct}%`);
        run.el.querySelector('[data-live-counts]').textContent = `${complete}/${total} tasks \u00b7 ${run.completedTools} tools${subtasks.length ? ` \u00b7 ${activeSubtasks} active sub-task${activeSubtasks === 1 ? '' : 's'}` : ''}`;

        const milestoneKey = JSON.stringify(orderedMilestones);
        if (run.milestoneRenderKey !== milestoneKey) {
            run.el.querySelector('.live-run-todos').innerHTML = milestoneHtml;
            run.milestoneRenderKey = milestoneKey;
        }
        const subtaskKey = JSON.stringify(subtasks);
        if (run.subtaskRenderKey !== subtaskKey) {
            run.el.querySelector('.live-run-subtasks').innerHTML = subtaskHtml;
            run.subtaskRenderKey = subtaskKey;
        }
        run.el.querySelector('[data-live-subtasks-section]').hidden = subtasks.length === 0;
        run.el.hidden = false;
    }

}

window.ResonantRunCards = ResonantRunCards;
