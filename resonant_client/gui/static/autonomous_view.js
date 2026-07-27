/*
 * Autonomous-session view methods for ResonantApp.
 *
 * app.js reached 14,500 lines as a single class, the point at which "where
 * does this behaviour live" stops having a findable answer. This file holds
 * the autonomous-session half: mission lifecycle, roadmap inspector, decision
 * cards, health signals, and run banners.
 *
 * Mixed into ResonantApp.prototype rather than converted to an ES module. The
 * page loads classic scripts (see index.html) and moving the whole app to
 * modules is a separate change with its own risk. A prototype mixin needs no
 * build step, preserves `this`, and matches how plan_graph_view.js already
 * loads.
 *
 * Class body rather than an object literal so the comments between methods
 * travel with the code they describe. Class methods are non-enumerable, so
 * app.js copies them with getOwnPropertyDescriptors — Object.assign would
 * silently copy nothing.
 *
 * Load order matters: this file must load BEFORE app.js.
 */

class ResonantAutonomousView {


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
            this.showStatusMessage('Autonomous session is already starting — give it a second.');
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
        this._resetTaskCardState();
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
        this.showStatusMessage('Autonomous session exited.');
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
        // Ask here rather than at startup: the payoff for allowing
        // notifications is obvious at the moment you kick off work that runs
        // for tens of minutes, and an unprompted dialog on first launch is the
        // fastest route to a permanent denial.
        this.ensureNotificationPermission();
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
        this._setSessionActivity('working');

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


    // ── v0.6.5 — long-running session health (task #7) ────────────
    // The daemon emits three health signals during multi-hour/-day
    // autonomous runs. Surface them so the user can read liveness at a
    // glance and is told when a step stalls or is recovered after a
    // crash. Backend: autonomous_loop.py (_wait_with_monitor) and
    // autonomous_session.py (resume path).

    handleAutonomousHeartbeat(event) {
        const s = this._ensureAutonomousState(event);
        // Server-originated proof of life while waiting on a sub-mission.
        // The badge's activity counter advances client-side even if the
        // daemon froze, so this records the last REAL contact — that's
        // what distinguishes "alive but slow" from "stuck". The token is
        // painted by _fmtHeartbeatToken, gated on iterInFlight.
        s.heartbeat = {
            at: Date.now() / 1000,
            elapsed: typeof event.elapsed_seconds === 'number'
                ? event.elapsed_seconds : null,
            iter: event.iter_count,
            phase: event.phase || '',
        };
        this._updateAutonomousBadgeState();
    }


    handleAutonomousIterationTimeout(event) {
        const s = this._ensureAutonomousState(event);
        // The sub-mission blew past the dispatch ceiling and was
        // cancelled as stalled; the daemon moves on to the next item.
        // Drop the in-flight + heartbeat state so the badge stops
        // advertising the dead wait, then leave a persistent chip.
        s.iterInFlight = false;
        s.heartbeat = null;
        this._updateAutonomousBadgeState();
        this._renderAutonomousHealthChip('timeout', event);
    }


    handleAutonomousResumeRecovery(event) {
        // The app stopped mid-iteration; on resume the daemon re-ran the
        // interrupted step. Warn so the user can reconcile any partial
        // work from the previous run. One-shot notice — no badge state
        // needed (the mission's own start event re-seeds the badge).
        this._renderAutonomousHealthChip('recovery', event);
    }


    /**
     * v0.6.5 — persistent, dismissable chip for a discrete autonomous
     * health event: a stalled step cancelled (`timeout`), or a step
     * re-run after a post-crash resume (`recovery`). Reuses the
     * backend-status-banner styling so it reads like the other
     * health/retry chips.
     */
    _renderAutonomousHealthChip(kind, event) {
        if (!this.chatMessages) return;
        const chip = document.createElement('div');
        let icon, body;
        if (kind === 'timeout') {
            const secs = event && event.timeout_seconds;
            const limit = (typeof secs === 'number' && secs > 0)
                ? this._fmtDuration(secs) : 'its time limit';
            const iter = (event && event.iter_count != null)
                ? ` (iter ${this.escapeHtml(String(event.iter_count))})` : '';
            icon = '⏱';
            chip.className = 'backend-status-banner autonomous-health-chip autonomous-health-timeout';
            body = `<span class="backend-status-text">
                A step${iter} ran past ${this.escapeHtml(limit)} with no progress and was
                <strong>cancelled as stalled</strong> — the session moves on to the next item.
                <span class="backend-status-hint">If this repeats, the step may be blocked (e.g. waiting on input it can't get). Check that roadmap item.</span>
            </span>`;
        } else {
            const iter = (event && event.iter != null)
                ? ` (iter ${this.escapeHtml(String(event.iter))})` : '';
            icon = '↻';
            chip.className = 'backend-status-banner autonomous-health-chip autonomous-health-recovery';
            body = `<span class="backend-status-text">
                Resumed after an interruption — re-running the step${iter} that was in flight when the app stopped.
                <span class="backend-status-hint">Check for partial work from the previous run (e.g. an uncommitted change) so nothing is duplicated.</span>
            </span>`;
        }
        chip.innerHTML = `
            <span class="backend-status-icon" aria-hidden="true">${icon}</span>
            ${body}
            <span class="backend-status-actions">
                <button type="button" class="backend-status-btn backend-status-dismiss" aria-label="Dismiss">×</button>
            </span>`;
        chip.querySelector('.backend-status-dismiss')
            .addEventListener('click', () => chip.remove());
        this.chatMessages.appendChild(chip);
        this.scrollToBottom();
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
        this._setSessionActivity('idle');

        // Stop the live-update tick and dismiss the badge.
        if (this._autonomousBadgeTimer) {
            clearInterval(this._autonomousBadgeTimer);
            this._autonomousBadgeTimer = null;
        }
        const newPhase = isComplete ? 'autonomous_complete' : 'autonomous_paused';
        this._refreshMissionBadge(newPhase, '');

        this._renderAutonomousBanner(isComplete ? 'complete' : 'paused', event);
        this.notifyDesktop(
            isComplete ? 'Autonomous session complete' : 'Autonomous session stopped',
            isComplete
                ? `Finished after ${s.iterCount || 0} step${s.iterCount === 1 ? '' : 's'}.`
                : `Stopped: ${(event && event.stop_reason) || 'paused'}.`,
            { tag: 'resonant-autonomous' },
        );
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
        this._setSessionActivity('idle');
        if (this._autonomousBadgeTimer) {
            clearInterval(this._autonomousBadgeTimer);
            this._autonomousBadgeTimer = null;
        }
        this._refreshMissionBadge('autonomous_paused', '');
        this._renderAutonomousBanner('failed', event);
        this.notifyDesktop(
            'Autonomous session failed',
            event.message || event.reason || 'The session stopped before finishing.',
            { tag: 'resonant-autonomous' },
        );
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
        this._setSessionActivity('needs-input');
        // The run is now blocked on a person. Everything else in the session
        // keeps working without one, so this is the moment most worth an
        // interrupt — an unnoticed park is dead wall-clock time.
        this.notifyDesktop(
            'Resonant needs a decision',
            request.question || 'The autonomous session is waiting on your input.',
            { tag: 'resonant-autonomous' },
        );
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
                <span class="autonomous-decision-title">Autonomous session paused — your decision needed</span>
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
            this._setSessionActivity('working');
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
        this._setSessionActivity(s.activity.phase === 'parked' ? 'needs-input' : 'working');
        this._updateAutonomousBadgeState();
    }


    /**
     * v0.5.8a2 — daemon picked up the decision and is retrying
     * REFLECT. Convert the active card into a one-line "resolved"
     * chip so the chat retains a marker of the user's choice.
     */
    handleAutonomousHumanDecisionReceived(event) {
        this._setSessionActivity('working');
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


    _formatAutonomousAge(seconds) {
        if (!Number.isFinite(seconds) || seconds < 0) return '';
        if (seconds < 60) return `${Math.round(seconds)}s`;
        if (seconds < 3600) return `${Math.round(seconds / 60)}m`;
        if (seconds < 86400) return `${Math.round(seconds / 360) / 10}h`;
        return `${Math.round(seconds / 8640) / 10}d`;
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
        // v0.6.5 — folded into the unified per-project sessions list, where
        // autonomous sessions are badged inline. The separate browser is
        // hidden so they aren't listed twice.
        const root = document.getElementById('autonomous-mission-browser');
        if (root) { root.hidden = true; root.innerHTML = ''; }
    }


    _renderMissionBrowserItem(mission) {
        const sessionId = mission.session_id || '';
        const intentId = mission.intent_id || '';
        const phase = mission.phase || '';
        const feature = mission.feature || '(unnamed session)';
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


    _renderRoadmapInspectorHTML(data) {
        const feature = data.feature || '(unnamed session)';
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
                <span title="Iterations recorded in roadmap.md (each entry = one shipped step). The chat-header badge counts the in-flight iter as well, so during a running iter the header may show one ahead.">iter ${iterCount} completed</span>
            </div>
            ${criteriaHTML ? `<ul class="arm-criteria-list">${criteriaHTML}</ul>` : ''}
            ${nextItemHTML}
            ${timingHTML}
            ${reflectionHTML}
            ${footerHTML}
        `;
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
            titleText = 'Autonomous session started';
            subText = `${budget}${cap}.`;
            cls += ' autonomous-banner-start';
        } else if (kind === 'complete') {
            const reason = (event && event.stop_reason) || 'satisfied';
            const elapsed = event && typeof event.elapsed_seconds === 'number'
                ? this._fmtDuration(event.elapsed_seconds)
                : '';
            titleText = 'Autonomous session complete · all acceptance criteria passed';
            subText = `Stop reason: ${this.escapeHtml(reason)}${elapsed ? ` · ${elapsed} elapsed` : ''}.`;
            cls += ' autonomous-banner-complete';
        } else if (kind === 'paused') {
            const reason = (event && event.stop_reason) || 'paused';
            const message = (event && event.stop_message) || '';
            const elapsed = event && typeof event.elapsed_seconds === 'number'
                ? this._fmtDuration(event.elapsed_seconds)
                : '';
            titleText = `Autonomous session paused · ${this.escapeHtml(reason)}`;
            subText = `${this.escapeHtml(message)}${elapsed ? ` · ${elapsed} elapsed` : ''}.`;
            cls += ' autonomous-banner-paused';
        } else if (kind === 'failed') {
            const err = (event && event.error) || 'unknown failure';
            titleText = 'Autonomous session failed';
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
        this._syncSessionTitle(sess);
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
                <span class="mission-past-label">Autonomous</span>
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
            badge.title = 'Autonomous session running — click to exit';
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
                <span class="mission-badge-label">Autonomous</span>
                <span class="mission-badge-phase">${this.escapeHtml(phaseLabel)}</span>
            </span>
            <span class="mission-badge-exit" title="Exit autonomous session" aria-label="Exit autonomous session">×</span>
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

        // v0.6.5 (task #7) — heartbeat liveness. The activity counter
        // above ticks client-side even if the daemon froze, so this
        // server-originated token is what actually tells slow from stuck.
        const hbToken = this._fmtHeartbeatToken();
        if (hbToken) parts.push(hbToken);

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
            waiting_dispatch: 'running step',
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
            'Stop the autonomous session NOW? In-flight tool calls ' +
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
            <span class="mission-cancel-exit-msg">Autonomous session paused. Exit it entirely, or stay in drafting?</span>
            <button type="button" class="mission-cancel-exit-btn-stay">Stay</button>
            <button type="button" class="mission-cancel-exit-btn-exit">Exit session</button>
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
        const ok = confirm('Exit this autonomous session?\n\nIt stays in your sidebar and you can review the conversation, but no new work will be dispatched.');
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
        let modelHintHTML = '';
        if (!backendType) {
            modelHintHTML = `
                <div class="mission-composer-hint mission-composer-hint-warn">
                    <span aria-hidden="true">⚠</span>
                    Pick a model first — the autonomous session needs Ollama to run the interview.
                </div>`;
        }

        overlay = document.createElement('div');
        overlay.id = 'mission-composer-overlay';
        overlay.className = 'mission-composer-overlay';
        overlay.innerHTML = `
            <div class="mission-composer">
                <div class="mission-composer-header">
                    <span class="mission-composer-icon" aria-hidden="true">🎯</span>
                    <span class="mission-composer-title">Start an autonomous session</span>
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
                    <button type="button" class="mission-composer-start" disabled>Start session</button>
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
                ? '∞ Start autonomous session'
                : 'Start session';
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

        // How long the session may sit parked on a decision before proceeding
        // with the option REFLECT nominated. "Wait for me" is the default and
        // preserves the historical behaviour — a deadline only makes sense if
        // you are willing to have it decide without you.
        const decisionPresets = [
            { label: 'Wait for me', value: '', sub: 'never proceeds alone' },
            { label: '15m', value: '15m', sub: '' },
            { label: '30m', value: '30m', sub: '' },
            { label: '1h', value: '1h', sub: '' },
            { label: '4h', value: '4h', sub: '' },
        ];
        const decisionHTML = decisionPresets.map((p) => `
            <button type="button"
                class="mission-budget-preset mission-decision-preset"
                data-decision="${p.value}"
                ${p.value === '' ? 'data-default="1"' : ''}>
                <span class="mission-budget-preset-label">${p.label}</span>
                ${p.sub ? `<span class="mission-budget-preset-sub">${p.sub}</span>` : ''}
            </button>
        `).join('');

        wrap.innerHTML = `
            <div class="mission-autonomous-head">
                <span class="mission-autonomous-icon" aria-hidden="true">∞</span>
                <span class="mission-autonomous-title">Autonomous session</span>
            </div>
            <p class="mission-autonomous-blurb">
                Acceptance criteria from the spec drive the convergence check. The session stops
                when ALL criteria are met (regardless of budget remaining), the budget runs out,
                or you click Stop. <strong>Pick a time budget:</strong>
            </p>
            <div class="mission-budget-presets">${presetHTML}</div>
            <p class="mission-autonomous-blurb">
                <strong>If it needs a decision and you are away:</strong> the session parks and
                waits. Bound that wait so an unattended run cannot stall on you indefinitely.
            </p>
            <div class="mission-decision-presets">${decisionHTML}</div>
            <div class="mission-autonomous-actions">
                <span class="mission-autonomous-budget-label">
                    Selected: <strong class="mission-autonomous-budget-display">${recommended}</strong>
                    <span class="mission-autonomous-decision-display"></span>
                </span>
                <button type="button" class="mission-build-btn mission-build-btn-autonomous">
                    <span class="mission-build-icon" aria-hidden="true">∞</span>
                    <span class="mission-build-label">Build autonomously</span>
                </button>
            </div>
            <p class="mission-autonomous-fullauto-note" style="display: none;">
                Full auto skips the time ceiling. The session stops only on convergence, blocking,
                or your Stop click. A 100-iteration cap is always enforced as a defensive backstop.
            </p>
        `;

        // Wire preset selection. Default selection from the spec's
        // `**Time budget:**` line (or "4h" if absent).
        let chosen = recommended;
        // Scoped to the budget row on purpose: the decision-deadline buttons
        // reuse `.mission-budget-preset` for styling, and an unscoped query
        // would sweep them in and read a `data-budget` they do not carry.
        const presetButtons = wrap.querySelectorAll(
            '.mission-budget-presets .mission-budget-preset',
        );
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

        // Park deadline. Empty string means wait indefinitely.
        let chosenDecision = '';
        const decisionButtons = wrap.querySelectorAll(
            '.mission-decision-presets .mission-decision-preset',
        );
        const decisionDisplay = wrap.querySelector('.mission-autonomous-decision-display');
        const updateDecision = (value) => {
            chosenDecision = value || '';
            decisionDisplay.textContent = chosenDecision
                ? ` · decides alone after ${chosenDecision}`
                : ' · waits for you';
            decisionButtons.forEach((b) => {
                b.classList.toggle(
                    'mission-budget-preset-selected',
                    (b.dataset.decision || '') === chosenDecision,
                );
            });
        };
        decisionButtons.forEach((b) => {
            b.addEventListener('click', () => updateDecision(b.dataset.decision || ''));
        });
        updateDecision('');
        // Defensive — if no preset matched the recommendation, default to 4h.
        // Scoped to the budget row: the decision row's default is always
        // selected by now, and an unscoped query would see that and conclude a
        // budget had been chosen when none had.
        if (!wrap.querySelector('.mission-budget-presets .mission-budget-preset-selected')) {
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
                decision_timeout: chosenDecision,
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

}

window.ResonantAutonomousView = ResonantAutonomousView;
