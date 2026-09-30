/* Session-scoped team controls. Runtime state is authoritative; no model calls here. */
window.LumiSwarmView = class LumiSwarmView {
    static DECISIONS = new Set(['review_read_result', 'reject_result', 'retry_work', 'accept_writer', 'decide_proposal']);

    bindSwarmPanel() {
        this._swarmDialog = null;
        this._swarmRequestCounter = 0;
        this._swarmStopRefresh = null;
        document.getElementById('swarm-team-button')?.addEventListener('click', () => this.openSwarmPanel());
    }

    /**
     * The chat's `/team <objective>`: the panel with that objective, planned
     * and run by the orchestrator. The panel first learns this conversation's
     * latest team: a finished one gives way to a new team's form, as New team
     * does, and a team still working keeps the panel, which says so.
     */
    openSwarmWithObjective(objective) {
        this.openSwarmPanel();
        if (!this._swarmNodes?.form) return;
        this._swarmObjectiveDraft = {objective};
        // Otherwise the view the panel asked for applies it (receiveSwarmState).
        if (!this._swarmPending) this._swarmApplyObjectiveDraft();
    }

    _swarmApplyObjectiveDraft() {
        const draft = this._swarmObjectiveDraft;
        if (!draft) return;
        this._swarmObjectiveDraft = null;
        const nodes = this._swarmNodes;
        const run = this._swarmState?.run?.run;
        if (run && !['completed', 'cancelled', 'failed'].includes(run.state)) {
            nodes.notice.textContent = 'This conversation’s team is still working. Stop it, or wait until it finishes, then use /team again or choose New team.';
            nodes.notice.dataset.error = 'true';
            nodes.notice.scrollIntoView?.({block: 'nearest'});
            return;
        }
        if (run) {
            // As New team does; it is off only while another change is pending.
            nodes['new-team'].click();
            if (nodes.form.hidden) {
                nodes.notice.textContent = 'Choose New team to start another team in this conversation.';
                return;
            }
        }
        nodes['plan-mode'].value = 'coordinator';
        if (draft.objective) nodes.objective.value = draft.objective;
        nodes.autonomous.checked = true;
        this._swarmRenumberTasks();
        // The person still reviews the limits and presses Start.
        (draft.objective ? nodes.rounds : nodes.objective).focus();
    }

    openSwarmPanel() {
        if (this._swarmDialog?.open) return;
        this._swarmScope = Object.freeze({project: this.currentCwd || '', session_id: this.currentSessionId || '', run_id: ''});
        this._swarmState = null;
        this._swarmExecutionMode = 'personal';
        this._swarmCursor = 0;
        this._swarmActivity = new Map();
        this._swarmWorkers = new Map();
        this._swarmProposals = new Map();
        this._swarmRecoveryRows = new Map();
        this._swarmManagedRows = new Map();
        this._swarmRetryRows = new Map();
        this._swarmWriterRows = new Map();
        this._swarmCandidateRows = new Map();
        this._swarmLeftRows = new Map();
        this._swarmConcurrencyDirty = false;
        this._swarmConcurrencyCap = null;
        this._swarmPending = null;
        this._swarmPlanningNew = false;
        this._swarmPreviousRunId = null;
        this._swarmRunCache = new Map();
        this._swarmFollowupDrafts = new Map();
        this._swarmFollowupRun = null;
        this._swarmHistory = null;
        this._swarmHistoryCursors = [null];
        this._swarmHistoryPage = 0;
        this._swarmArtifactState = null;
        this._swarmSelectingRun = false;
        const dialog = document.createElement('dialog');
        dialog.className = 'swarm-panel';
        dialog.id = 'swarm-team-dialog';
        dialog.setAttribute('aria-labelledby', 'swarm-title');
        dialog.innerHTML = `<header class="swarm-heading"><div><p class="swarm-eyebrow">SESSION TEAM</p><h2 id="swarm-title">Work together</h2></div>
            <button type="button" data-swarm="close" class="swarm-icon" aria-label="Close team panel" title="Close team panel">×</button></header>
            <div class="swarm-body"><p class="swarm-scope" data-swarm="scope"></p>
            <p class="swarm-notice" data-swarm="notice" role="status" aria-live="polite">Loading team…</p>
            <details class="swarm-history"><summary>Saved teams in this conversation</summary>
            <form data-swarm="history-form"><label>Saved team<select data-swarm="history-select" aria-label="Saved team" required><option value="">Loading saved teams…</option></select></label>
            <button type="submit" data-swarm="history-open">Open saved team</button></form>
            <p data-swarm="history-status" class="swarm-help" role="status"></p><div class="swarm-controls">
            <button type="button" data-swarm="history-newer">Newer teams</button><button type="button" data-swarm="history-older">Older teams</button>
            <button type="button" data-swarm="history-refresh">Refresh history</button></div>
            <p data-swarm="kept-everywhere" class="swarm-help" role="status" hidden></p><button type="button" data-swarm="discard-all" hidden>Discard all kept work</button></details>
            <section class="swarm-setup" aria-labelledby="swarm-setup-title"><h3 id="swarm-setup-title">Team preview</h3>
            <p>Assign scoped work to a small team. Start with read-only investigations, or explicitly allow isolated file changes. Results still need verification.</p>
            <label class="swarm-switch"><input type="checkbox" data-swarm="enabled"> Enable team preview</label>
            <label>Execution ownership<select data-swarm="execution-mode" aria-label="Execution ownership"><option value="personal">Personal team</option><option value="managed" disabled>Organization managed team (set up by your organization)</option></select></label>
            <p data-swarm="managed-status" class="swarm-help" role="status">Personal work stays on this computer.</p>
            <p class="swarm-model"><span>Saved session model</span><strong data-swarm="model">Loading…</strong></p>
            <button type="button" data-swarm="previous-team" hidden>Back to saved team</button>
            <form data-swarm="form"><label>Planning approach<select data-swarm="plan-mode"><option value="manual">Assign investigations myself</option><option value="coordinator">Ask a coordinator to propose a plan</option></select></label>
            <label>Team objective<textarea data-swarm="objective" rows="2" maxlength="8000" required placeholder="What should the team investigate?"></textarea></label>
            <div class="swarm-fields"><label>Worker slots<select data-swarm="slots"><option value="1">1 worker</option><option value="2" selected>2 workers</option><option value="3">3 workers</option><option value="4">4 workers</option><option value="5">5 workers</option><option value="6">6 workers</option><option value="7">7 workers</option><option value="8">8 workers</option></select></label>
            <label>Total model requests<input data-swarm="allowance" type="number" min="1" max="1000" step="1" value="12" required></label></div>
            <p class="swarm-help">The request allowance includes each worker’s planning and compression calls. Uncertain calls keep their allowance.</p>
            <label>Worker model<select data-swarm="worker-model"><option value="">Same as this session</option></select></label>
            <p class="swarm-help">Workers use this model; the coordinator or orchestrator always uses this session’s. A faster or local model can do the work while a stronger one plans.</p>
            <label class="swarm-switch"><input type="checkbox" data-swarm="allow-writes"> Allow scoped file changes</label>
            <p class="swarm-help" data-swarm="git-note" hidden></p>
            <fieldset data-swarm="writer-setup" class="swarm-task" hidden disabled><legend>File changes and verification</legend>
            <p class="swarm-help">Start from a clean, committed Git checkout. Its base and branch are captured when the team starts. Writers use isolated worktrees. Your checkout changes only after verified changes are explicitly applied.</p>
            <label>Writable project folders<input data-swarm="write-roots" required placeholder="src, tests"></label>
            <label data-swarm="manual-worker-label">Allowance per writer<input data-swarm="manual-worker-requests" type="number" min="1" max="1000" step="1" value="4" required></label>
            <p class="swarm-help">Declare trusted verification commands before starting. They execute locally with your account’s permissions; choose commands you understand.</p>
            <div data-swarm="check-catalog"></div><button type="button" data-swarm="add-check">Add verification check</button></fieldset>
            <div data-swarm="coordinator-setup" hidden><p class="swarm-help">The coordinator can read project files and propose investigations. Review its scope and dependencies before approving any workers.</p>
            <div class="swarm-fields"><label>Coordinator request allowance<input data-swarm="coordinator-requests" type="number" min="1" max="1000" step="1" value="3" required></label>
            <label>Allowance per worker<input data-swarm="worker-requests" type="number" min="1" max="1000" step="1" value="4" required></label></div>
            <label class="swarm-switch"><input type="checkbox" data-swarm="autonomous"> Let the orchestrator run the team</label>
            <p class="swarm-help" data-swarm="autonomy-mode" hidden>The orchestrator decides for you, so it needs Full-auto. This conversation isn’t in Full-auto: starting asks first, and offers to run just this team in Full-auto. The conversation keeps its mode.</p>
            <div class="swarm-fields" data-swarm="autonomy-fields" hidden><label>Orchestrator rounds<input data-swarm="rounds" type="number" min="1" max="8" step="1" value="3" required></label></div>
            <label class="swarm-switch" data-swarm="auto-apply-label" hidden><input type="checkbox" data-swarm="auto-apply"> Apply changes that pass every check</label>
            <p class="swarm-help" data-swarm="autonomy-help" hidden>The orchestrator plans, starts workers, reads their findings and plans again for up to this many rounds, then writes a final report. It doesn’t wait for you at each step: findings it uses are marked accepted by the orchestrator, not reviewed by you. <span data-swarm="apply-help-manual">File changes still wait for you.</span><span data-swarm="apply-help-auto" hidden>Writers’ changes are combined and applied to your checkout once every declared check passes, without your review; a failing check sends the writers back once with its output. Every check runs on each round’s combined change, so declare checks the project should pass after every round. Keep the checkout clean and on its branch while the team runs.</span> You can pause or stop the team at any time.</p></div>
            <div data-swarm="tasks" class="swarm-tasks"></div><button type="button" data-swarm="add-task" class="swarm-secondary">Add investigation</button>
            <button type="submit" data-swarm="start" class="swarm-primary">Start read-only team</button></form></section>
            <section data-swarm="run-section" hidden aria-labelledby="swarm-run-title"><div class="swarm-section-heading"><h3 id="swarm-run-title">Selected team</h3><span class="swarm-badge" data-swarm="run-state"></span></div>
            <p data-swarm="run-objective"></p><p data-swarm="worker-model-note" class="swarm-help" hidden></p><p data-swarm="accounting" class="swarm-help"></p>
            <div class="swarm-controls"><button type="button" data-swarm="pause">Pause new work</button><button type="button" data-swarm="resume">Resume</button>
            <button type="button" data-swarm="stop" class="swarm-stop">Stop team</button><button type="button" data-swarm="recover">Take over expired team</button>
            <button type="button" data-swarm="complete">Complete team</button>
            <button type="button" data-swarm="export-report">Export run report</button>
            <button type="button" data-swarm="use-in-chat" title="Add this team's report and accepted results to your message as context. Nothing is sent.">Use in chat</button>
            <button type="button" data-swarm="new-team">New team</button></div>
            <form data-swarm="concurrency-form" class="swarm-concurrency" hidden><label>Active worker limit<select data-swarm="worker-limit" aria-label="Active worker limit" required></select></label>
            <p data-swarm="worker-count" class="swarm-help" role="status"></p><p class="swarm-help">Existing workers finish; this limits new assignments. The coordinator is counted separately.</p>
            <button type="submit" data-swarm="apply-worker-limit">Apply worker limit</button></form>
            <section data-swarm="orchestrator" class="swarm-orchestrator" hidden aria-labelledby="swarm-orchestrator-title"><h4 id="swarm-orchestrator-title">Orchestrator</h4>
            <p data-swarm="orchestrator-status" role="status"></p>
            <section data-swarm="orchestrator-report" class="swarm-report" hidden aria-labelledby="swarm-report-title"><h5 id="swarm-report-title">Final report</h5>
            <p data-swarm="orchestrator-report-text" class="swarm-report-text"></p>
            <p data-swarm="orchestrator-record" class="swarm-help"></p></section></section>
            <form data-swarm="followup-form" class="swarm-task" hidden aria-labelledby="swarm-followup-title"><h4 id="swarm-followup-title">Follow-up planning</h4>
            <p class="swarm-help">Ask the coordinator to propose additional work from retained findings. Existing workers continue. Review the new proposal before approving any additional workers.</p>
            <div class="swarm-fields"><label>Follow-up coordinator allowance<input data-swarm="followup-requests" type="number" min="1" max="1000" step="1" required></label>
            <label>Follow-up readable folders<input data-swarm="followup-roots" placeholder="Leave empty for retained findings only"></label></div>
            <p class="swarm-help">Separate project paths with commas. An empty field permits no file reads. The selected model and allowance per worker stay as originally configured.</p>
            <p data-swarm="followup-status" class="swarm-help" role="status"></p><button type="submit" data-swarm="request-plan">Request follow-up plan</button></form>
            <section data-swarm="recovery-section" hidden aria-labelledby="swarm-recovery-title"><h3 id="swarm-recovery-title">Review interrupted work</h3>
            <p class="swarm-help">Taking over prevents the previous supervisor from admitting more work. It starts no workers and does not prove that a process stopped or a request was unused.</p>
            <p data-swarm="recovery-status" role="status"></p><div data-swarm="recovery-records"></div>
            <section data-swarm="managed-recovery" hidden aria-labelledby="swarm-managed-recovery-title"><h4 id="swarm-managed-recovery-title">Organization recovery observations</h4>
            <p class="swarm-help">Outcomes are derived from the exact retained local records. Cleanup must be acknowledged by the organization service before another worker can start. Unknown request usage remains held.</p>
            <p data-swarm="managed-recovery-status" role="status"></p>
            <div class="swarm-fields"><label>Retained epoch<select data-swarm="managed-epoch" aria-label="Retained epoch"></select></label>
            <label>Retained record type<select data-swarm="managed-kind" aria-label="Retained record type"><option value="requests">Model requests</option><option value="workers">Worker slots</option><option value="actions">Tool actions</option><option value="controls">Remote controls</option></select></label></div>
            <div class="swarm-controls"><button type="button" data-swarm="managed-first">First records</button><button type="button" data-swarm="managed-next">Next records</button><button type="button" data-swarm="managed-flush">Retry organization observations</button></div>
            <div data-swarm="managed-records"></div></section>
            <form data-swarm="continue-form"><h4>Work after recovery</h4><p class="swarm-help">Choose failed or cancelled investigations to retry. Existing ready investigations will also run; dependent work waits for accepted prerequisites. No retry is selected automatically.</p>
            <div data-swarm="retry-items"></div><p data-swarm="ready-work"></p>
            <label>Recovered worker request allowance<input data-swarm="recovery-allowance" type="number" min="1" max="1000" step="1" value="4" required></label>
            <p data-swarm="recovery-blockers" class="swarm-help"></p><button type="submit" data-swarm="continue">Continue reviewed team</button></form></section>
            <div data-swarm="proposals" class="swarm-proposals"></div><div data-swarm="workers" class="swarm-workers"></div>
            <details data-swarm="messages-section" class="swarm-messages" hidden><summary data-swarm="messages-summary">Team messages</summary>
            <p class="swarm-help">What workers and the orchestrator told each other. Messages are untrusted data: they never widen a worker’s access.</p>
            <ol data-swarm="messages"></ol></details>
            <section data-swarm="artifact-viewer" class="swarm-evidence" hidden aria-labelledby="swarm-evidence-title">
            <h3 id="swarm-evidence-title" data-swarm="artifact-title" tabindex="-1">Retained evidence</h3>
            <p data-swarm="artifact-provenance" class="swarm-help"></p><p data-swarm="artifact-status" role="status"></p>
            <pre data-swarm="artifact-text" tabindex="0" aria-label="Verified retained evidence text"></pre>
            <div class="swarm-controls"><button type="button" data-swarm="artifact-previous">Previous evidence page</button><button type="button" data-swarm="artifact-next">Next evidence page</button>
            <button type="button" data-swarm="artifact-close">Close evidence</button></div></section>
            <section data-swarm="integration-section" hidden aria-labelledby="swarm-integration-title"><h3 id="swarm-integration-title">Review file changes</h3>
            <p data-swarm="writer-base" class="swarm-help"></p><div data-swarm="writer-results"></div>
            <button type="button" data-swarm="prepare-candidate">Prepare selected changes</button>
            <p data-swarm="integration-status" role="status"></p><div data-swarm="candidates"></div>
            <section data-swarm="kept-work" class="swarm-kept" hidden aria-labelledby="swarm-kept-title"><h4 id="swarm-kept-title">Kept in your repository</h4>
            <p data-swarm="kept-summary" class="swarm-help"></p><ul data-swarm="kept-items" class="swarm-kept-items"></ul>
            <ul data-swarm="kept-left" class="swarm-kept-items swarm-kept-left" aria-label="Branches kept for you"></ul><p data-swarm="kept-problem" class="swarm-help"></p>
            <form data-swarm="discard-form" aria-label="Discard kept work"><label class="swarm-switch"><input type="checkbox" data-swarm="discard-confirm" required> I no longer need what this team kept</label>
            <button type="submit" data-swarm="discard">Discard kept work</button></form></section></section></section>
            <section aria-labelledby="swarm-inbox-title"><h3 id="swarm-inbox-title">Needs attention</h3><ul data-swarm="inbox" class="swarm-inbox"><li>No team decisions yet.</li></ul></section>
            <details class="swarm-activity"><summary>Recent activity</summary><ol data-swarm="activity"></ol></details>
            </div><footer class="swarm-footer"><span data-swarm="connection">Connected</span><button type="button" data-swarm="refresh">Refresh team</button></footer>`;
        this._swarmDialog = dialog;
        this._swarmNodes = Object.fromEntries([...dialog.querySelectorAll('[data-swarm]')].map(node => [node.dataset.swarm, node]));
        const nodes = this._swarmNodes;
        this._mountCollaboration?.();
        this._mountManagedCollaboration?.();
        nodes.scope.textContent = `${this._swarmScope.project || 'No project selected'} · ${this._swarmScope.session_id ? 'Saved conversation' : 'Save a conversation to start a team'}`;
        nodes.close.addEventListener('click', () => dialog.close());
        dialog.addEventListener('close', () => {
            // Native close delivery may trail a fast keyboard reopen. Cleanup
            // owns this dialog, never the replacement panel or its request.
            clearInterval(dialog._swarmPoll);
            dialog.remove();
            if (this._swarmDialog === dialog) {
                this._swarmPoll = null;
                this._swarmPending = null;
                this._swarmDialog = null;
                (this._swarmReturnFocus || document.getElementById('swarm-team-button'))?.focus();
            }
            this._swarmReturnFocus = null;
        });
        nodes.enabled.addEventListener('change', () => this.requestSwarm('configure', {enabled: nodes.enabled.checked}));
        nodes['execution-mode'].addEventListener('change', () => {
            if (this._swarmPending || this._swarmState?.run?.run) return;
            this._swarmExecutionMode = nodes['execution-mode'].value;
            this._swarmPreviousRunId = null;
            this._swarmState = null;
            this._swarmCursor = 0;
            this._swarmHistory = null;
            this._swarmHistoryCursors = [null];
            this._swarmHistoryPage = 0;
            this._swarmRunCache.clear();
            this._swarmActivity.clear();
            this.requestSwarm('view');
        });
        nodes['history-form'].addEventListener('submit', event => {
            event.preventDefault();
            if (nodes['history-form'].reportValidity()) this._swarmOpenRun(nodes['history-select'].value);
        });
        nodes['history-select'].addEventListener('change', () => this._renderSwarmControls());
        nodes['history-newer'].addEventListener('click', () => this._swarmLoadHistory(this._swarmHistoryPage - 1));
        nodes['history-older'].addEventListener('click', () => this._swarmLoadHistory(this._swarmHistoryPage + 1));
        nodes['history-refresh'].addEventListener('click', () => { this._swarmHistoryCursors = [null]; this._swarmLoadHistory(0); });
        nodes['artifact-close'].addEventListener('click', () => { this._swarmArtifactState = null; this._renderSwarmArtifact(); });
        nodes['artifact-next'].addEventListener('click', () => {
            const state = this._swarmArtifactState;
            if (Number.isInteger(state?.page?.next_offset)) this._swarmReadArtifact(state.target, state.page.next_offset);
        });
        nodes['artifact-previous'].addEventListener('click', () => {
            const state = this._swarmArtifactState;
            const index = state?.offsets.indexOf(state.page?.offset);
            if (index > 0) this._swarmReadArtifact(state.target, state.offsets[index - 1]);
        });
        nodes['add-task'].addEventListener('click', () => this._swarmAddTask());
        nodes.slots.addEventListener('change', () => this._swarmRenumberTasks());
        nodes['allow-writes'].addEventListener('change', () => this._swarmRenumberTasks());
        nodes['add-check'].addEventListener('click', () => this._swarmAddCheck());
        for (const name of ['plan-mode', 'coordinator-requests', 'worker-requests', 'manual-worker-requests', 'autonomous', 'auto-apply']) {
            nodes[name].addEventListener('input', () => this._swarmRenumberTasks());
        }
        nodes.form.addEventListener('submit', event => {
            event.preventDefault();
            this._swarmValidateChecks();
            if (!nodes.form.reportValidity()) return;
            const coordinator = nodes['plan-mode'].value === 'coordinator';
            const setup = {objective: nodes.objective.value.trim(), plan_mode: nodes['plan-mode'].value,
                request_limit: Number(nodes.allowance.value), max_workers: Number(nodes.slots.value)};
            if (nodes['allow-writes'].checked) {
                setup.write_roots = nodes['write-roots'].value.split(',').map(value => value.trim()).filter(Boolean);
                setup.worker_requests = Number(nodes['manual-worker-requests'].value);
                setup.checks = [...nodes['check-catalog'].children].map(row => ({key: row.querySelector('[data-check-key]').value.trim(),
                    argv: [row.querySelector('[data-check-executable]').value.trim(), ...row.querySelector('[data-check-arguments]').value.split(/\r?\n/).filter(value => value.length)],
                    timeout_seconds: Number(row.querySelector('[data-check-timeout]').value)}));
            }
            if (coordinator) {
                setup.coordinator_requests = Number(nodes['coordinator-requests'].value);
                setup.worker_requests = Number(nodes['worker-requests'].value);
                if (nodes['worker-model'].value) setup.worker_model = JSON.parse(nodes['worker-model'].value);
                if (nodes.autonomous.checked) {
                    setup.autonomy = {rounds: Number(nodes.rounds.value)};
                    if (nodes['allow-writes'].checked && nodes['auto-apply'].checked) setup.autonomy.apply = true;
                }
                this.requestSwarm('start', setup);
                return;
            }
            const tasks = [...nodes.tasks.children].map(row => ({
                objective: row.querySelector('[data-task-objective]').value.trim(),
                read_roots: row.querySelector('[data-task-roots]').value.split(',').map(root => root.trim()).filter(Boolean),
                ...(nodes['allow-writes'].checked && row.querySelector('[data-task-role]').value === 'implement' ? {role: 'implement',
                    write_roots: row.querySelector('[data-task-writes]').value.split(',').map(root => root.trim()).filter(Boolean),
                    criteria: row.querySelector('[data-task-criteria]').value.split(',').map(value => value.trim()).filter(Boolean)} : {}),
            }));
            if (tasks.some(task => !task.objective || !task.read_roots.length)) return;
            if (nodes['worker-model'].value) setup.worker_model = JSON.parse(nodes['worker-model'].value);
            this.requestSwarm('start', {...setup, tasks});
        });
        for (const action of ['pause', 'resume', 'stop', 'recover', 'complete']) {
            nodes[action].addEventListener('click', () => this.requestSwarm(action));
        }
        nodes['export-report'].addEventListener('click', () => this.requestSwarm('export_report'));
        nodes['use-in-chat'].addEventListener('click', () => this._swarmUseInChat());
        nodes['worker-limit'].addEventListener('input', () => {
            this._swarmConcurrencyDirty = true;
            this._renderSwarmControls();
        });
        nodes['concurrency-form'].addEventListener('submit', event => {
            event.preventDefault();
            if (nodes['concurrency-form'].reportValidity()) this.requestSwarm('set_concurrency', {max_workers: Number(nodes['worker-limit'].value)});
        });
        for (const name of ['followup-requests', 'followup-roots']) nodes[name].addEventListener('input', () => {
            const runId = this._swarmScope.run_id;
            if (runId) this._swarmFollowupDrafts.set(runId, {requests: nodes['followup-requests'].value, roots: nodes['followup-roots'].value});
        });
        nodes['followup-form'].addEventListener('submit', event => {
            event.preventDefault();
            if (!this._swarmState?.coordinator_planning?.available || !nodes['followup-form'].reportValidity()) return;
            this.requestSwarm('request_plan', {coordinator_requests: Number(nodes['followup-requests'].value),
                read_roots: nodes['followup-roots'].value.split(',').map(value => value.trim()).filter(Boolean)});
        });
        nodes['continue-form'].addEventListener('submit', event => {
            event.preventDefault();
            if (!nodes['continue-form'].reportValidity()) return;
            const stopped = Boolean(this._swarmState?.run?.run?.stop_requested);
            const retry_work_items = stopped ? [] : [...this._swarmRetryRows].filter(([, row]) => row.querySelector('input').checked).map(([id]) => id);
            this.requestSwarm('continue_recovered', {retry_work_items, worker_requests: Number(nodes['recovery-allowance'].value)});
        });
        const managedPage = after => this.requestSwarm('managed_recovery_inspect', {
            managed_epoch: Number(nodes['managed-epoch'].value), kind: nodes['managed-kind'].value, after, limit: 25});
        nodes['managed-epoch'].addEventListener('change', () => managedPage(null));
        nodes['managed-kind'].addEventListener('change', () => managedPage(null));
        nodes['managed-first'].addEventListener('click', () => managedPage(null));
        nodes['managed-next'].addEventListener('click', () => managedPage(this._swarmState?.run?.managed_recovery?.next_cursor));
        nodes['managed-flush'].addEventListener('click', () => this.requestSwarm('managed_retry_observations'));
        nodes['prepare-candidate'].addEventListener('click', () => {
            const writer_ids = [...this._swarmWriterRows].filter(([, row]) => row.querySelector('input').checked && !row.querySelector('input').disabled).map(([id]) => id);
            if (writer_ids.length) this.requestSwarm('prepare_candidate', {writer_ids});
        });
        // An ended team's unapplied work stays until the person discards it
        // (engine/swarming/cleanup.py); the checkbox is the confirmation.
        nodes['discard-confirm'].addEventListener('change', () => this._renderSwarmControls());
        nodes['discard-form'].addEventListener('submit', event => {
            event.preventDefault();
            if (nodes['discard-form'].reportValidity()) this.requestSwarm('discard_kept_work');
        });
        nodes['discard-all'].addEventListener('click', () => {
            if (!window.confirm('Discard what every ended team in this conversation kept?\n\nTheir worktrees and branches with changes nobody applied are removed, and so are applied candidates kept for inspection. Branches someone committed to stay.')) return;
            this.requestSwarm('discard_all_kept_work');
        });
        nodes.refresh.addEventListener('click', () => this.requestSwarm('view'));
        nodes['new-team'].addEventListener('click', () => {
            this._swarmRememberRun();
            this._swarmPreviousRunId = this._swarmScope.run_id;
            this._swarmScope = Object.freeze({...this._swarmScope, run_id: ''});
            this._swarmPlanningNew = true;
            this._swarmPending = null;
            this._swarmState = {...this._swarmState, run: null};
            nodes['run-section'].hidden = true;
            this._swarmActivity.clear();
            this._swarmCursor = 0;
            this._swarmWorkers.clear();
            this._swarmProposals.clear();
            this._swarmRecoveryRows.clear();
            this._swarmRetryRows.clear();
            this._swarmWriterRows.clear();
            this._swarmCandidateRows.clear();
            this._swarmLeftRows.clear();
            nodes['kept-left'].replaceChildren();
            this._swarmConcurrencyDirty = false;
            this._swarmConcurrencyCap = null;
            this._swarmArtifactState = null;
            this._renderSwarmArtifact();
            nodes.workers.replaceChildren();
            nodes.proposals.replaceChildren();
            nodes['recovery-records'].replaceChildren();
            nodes['retry-items'].replaceChildren();
            nodes['recovery-section'].hidden = true;
            nodes['writer-results'].replaceChildren();
            nodes.candidates.replaceChildren();
            nodes['integration-section'].hidden = true;
            nodes.activity.replaceChildren();
            this._renderSwarmInbox(null);
            this._renderSwarmControls();
            nodes.objective.focus();
        });
        nodes['previous-team'].addEventListener('click', () => {
            this._swarmOpenRun(this._swarmPreviousRunId);
        });
        document.body.appendChild(dialog);
        this._swarmAddTask();
        this._swarmAddTask();
        this._swarmAddCheck();
        dialog.showModal();
        nodes.close.focus();
        this._renderSwarmControls();
        this.requestSwarm('view');
        // Durable snapshots also cover events missed during a disconnect. Never
        // overlap a pending command or replay a mutation after a lost reply.
        dialog._swarmPoll = this._swarmPoll = setInterval(() => {
            if (dialog.open && !this._swarmPending && this.ws?.readyState === WebSocket.OPEN) {
                this.requestSwarm('view', {}, {quiet: true});
            }
        }, 1000);
    }

    _swarmAddTask() {
        const nodes = this._swarmNodes;
        if (!nodes || nodes.tasks.children.length >= 8) return;
        const row = document.createElement('fieldset');
        row.className = 'swarm-task';
        row.innerHTML = `<legend>Investigation</legend><label>Task<textarea data-task-objective rows="2" required maxlength="4000" placeholder="A focused question this worker can answer"></textarea></label>
            <label>Readable folders<input data-task-roots value="." required placeholder="src, tests"></label>
            <label data-task-role-label hidden>Assignment type<select data-task-role><option value="explore">Read-only investigation</option><option value="implement">Implement file changes</option></select></label>
            <div data-task-writer-fields hidden><label>Writable folders for this task<input data-task-writes placeholder="src/backend" required></label>
            <label>Required check names<input data-task-criteria placeholder="csv-tests" required></label></div>
            <div class="swarm-task-footer"><span class="swarm-help">Separate folders with commas; . means this project.</span><button type="button" data-remove-task>Remove</button></div>`;
        row.querySelector('[data-task-role]').addEventListener('change', () => this._swarmRenumberTasks());
        row.querySelector('[data-task-criteria]').addEventListener('input', () => this._swarmValidateChecks());
        row.querySelector('[data-remove-task]').addEventListener('click', () => {
            if (nodes.tasks.children.length <= 1) return;
            row.remove();
            this._swarmRenumberTasks();
            nodes['add-task'].focus();
        });
        nodes.tasks.appendChild(row);
        this._swarmRenumberTasks();
        if (this._swarmDialog.open) row.querySelector('textarea').focus();
    }

    _swarmRenumberTasks() {
        const nodes = this._swarmNodes;
        const coordinator = nodes['plan-mode'].value === 'coordinator';
        // Writer teams need Git (lumi/git_support.py); read-only teams don't.
        const git = this._gitMissingCopy?.();
        nodes['git-note'].hidden = !git;
        if (git) {
            nodes['git-note'].textContent = `Writer teams need ${git.product}, which isn’t installed on this computer. Read-only teams work without it. `;
            const link = document.createElement('a');
            link.href = git.url;
            link.target = '_blank';
            link.rel = 'noopener noreferrer';
            link.textContent = `Get ${git.product}`;
            nodes['git-note'].appendChild(link);
            nodes['allow-writes'].checked = false;
        }
        nodes['allow-writes'].disabled = Boolean(git);
        const writers = nodes['allow-writes'].checked;
        nodes['writer-setup'].hidden = !writers;
        nodes['writer-setup'].disabled = !writers;
        nodes['coordinator-setup'].hidden = !coordinator;
        nodes['manual-worker-label'].hidden = coordinator;
        nodes['manual-worker-requests'].disabled = coordinator || !writers;
        nodes.tasks.hidden = coordinator;
        nodes['add-task'].hidden = coordinator;
        const autonomous = coordinator && nodes.autonomous.checked;
        nodes.start.textContent = autonomous ? 'Start orchestrated team' : coordinator ? 'Request a proposed plan'
            : writers ? 'Start scoped team' : 'Start read-only team';
        for (const name of ['coordinator-requests', 'worker-requests', 'autonomous']) nodes[name].disabled = !coordinator;
        nodes['autonomy-fields'].hidden = !autonomous;
        nodes['autonomy-help'].hidden = !autonomous;
        nodes['autonomy-mode'].hidden = !autonomous || this.permissionMode === 'bypass';
        nodes.rounds.disabled = !autonomous;
        // Applying needs writer access: its checks are what decide.
        const canApply = autonomous && writers;
        nodes['auto-apply-label'].hidden = !canApply;
        nodes['auto-apply'].disabled = !canApply;
        const applies = canApply && nodes['auto-apply'].checked;
        nodes['apply-help-manual'].hidden = applies;
        nodes['apply-help-auto'].hidden = !applies;
        [...nodes.tasks.children].forEach((row, index) => {
            row.disabled = coordinator;
            row.querySelector('[data-task-role-label]').hidden = !writers;
            const writes = writers && row.querySelector('[data-task-role]').value === 'implement';
            row.querySelector('[data-task-writer-fields]').hidden = !writes;
            for (const input of row.querySelectorAll('[data-task-writes],[data-task-criteria]')) input.disabled = !writes;
            const noun = writes ? 'Change' : 'Investigation';
            row.querySelector('legend').textContent = `${noun} ${index + 1}`;
            row.querySelector('[data-remove-task]').setAttribute('aria-label', `Remove ${noun.toLowerCase()} ${index + 1}`);
            row.querySelector('[data-remove-task]').disabled = nodes.tasks.children.length <= 1;
        });
        nodes['add-task'].disabled = nodes.tasks.children.length >= 8;
        nodes.slots.setCustomValidity(!coordinator && nodes.tasks.children.length > Number(nodes.slots.value)
            ? 'Choose at least as many worker slots as investigations for this preview.' : '');
        nodes.allowance.min = String(coordinator ? Number(nodes['coordinator-requests'].value) + Number(nodes['worker-requests'].value)
            : writers ? Number(nodes['manual-worker-requests'].value) * nodes.tasks.children.length : nodes.tasks.children.length);
        this._swarmValidateChecks();
    }

    _swarmAddCheck() {
        const parent = this._swarmNodes['check-catalog'];
        if (parent.children.length >= 8) return;
        const row = document.createElement('fieldset');
        row.className = 'swarm-task';
        row.innerHTML = `<legend>Verification check</legend><label>Check name<input data-check-key required maxlength="80" placeholder="csv-tests"></label>
            <label>Executable<input data-check-executable required placeholder="python"></label>
            <label>Arguments, one per line<textarea data-check-arguments rows="3" placeholder="-m&#10;pytest&#10;-q"></textarea></label>
            <p class="swarm-help">Each line is one argument. Do not add shell quoting, pipes or redirects.</p>
            <label>Check timeout in seconds<input data-check-timeout type="number" min="1" max="1200" step="1" value="120" required></label>
            <button type="button">Remove check</button>`;
        row.querySelector('button').addEventListener('click', () => {
            if (parent.children.length > 1) row.remove();
            this._swarmValidateChecks();
            this._swarmNodes['add-check'].focus();
        });
        parent.appendChild(row);
        row.querySelector('[data-check-key]').addEventListener('input', () => this._swarmValidateChecks());
        this._swarmValidateChecks();
    }

    _swarmValidateChecks() {
        const nodes = this._swarmNodes;
        const rows = [...nodes['check-catalog'].children];
        const keys = rows.map(row => row.querySelector('[data-check-key]').value.trim());
        for (const row of rows) {
            const input = row.querySelector('[data-check-key]');
            input.setCustomValidity(input.value && !/^[A-Za-z0-9][A-Za-z0-9._-]*$/.test(input.value.trim()) ? 'Use letters, numbers, dots, underscores or hyphens for the check name.'
                : keys.filter(key => key && key === input.value.trim()).length > 1 ? 'Each verification check needs a unique name.' : '');
            row.querySelector('button').disabled = rows.length <= 1;
        }
        nodes['add-check'].disabled = rows.length >= 8;
        for (const task of nodes.tasks.children) {
            const input = task.querySelector('[data-task-criteria]');
            const criteria = input.value.split(',').map(value => value.trim()).filter(Boolean);
            input.setCustomValidity(!input.disabled && criteria.some(name => !keys.includes(name)) ? 'Use the names of verification checks declared above.' : '');
        }
    }

    requestSwarm(action, extra = {}, {quiet = false, retried = false} = {}) {
        if (!this._swarmDialog?.open) return;
        if (action === 'stop' && this._swarmPending?.action === 'request_plan') {
            // The planner may already have committed a newer revision while its
            // launch reply is delayed. Refresh this captured run, then send one
            // Stop. Never retry a mutation or reuse another panel's ownership.
            if (!this.ws || this.ws.readyState !== WebSocket.OPEN) return;
            const request_id = `swarm-${++this._swarmRequestCounter}-${crypto.randomUUID()}`;
            const target = {...this._swarmScope, execution_mode: this._swarmExecutionMode || 'personal', request_id};
            this._swarmStopRefresh = Object.freeze(target);
            this._swarmPending = {request_id, action: 'view', quiet: true};
            this._swarmNodes.notice.textContent = 'Refreshing this team before sending Stop…';
            this._renderSwarmControls();
            this.send({command: 'swarm', action: 'view', ...target});
            return;
        }
        if (this._swarmStopRefreshVisible() && !this._swarmIsRead(action)) return;
        if (this._swarmPending && !this._swarmIsRead(this._swarmPending.action)) return;
        if (!this.ws || this.ws.readyState !== WebSocket.OPEN) {
            this._swarmNodes.notice.textContent = 'Reconnecting. Team controls will return when the connection is restored.';
            this._renderSwarmControls();
            return;
        }
        const scope = this._swarmScope;
        if (!scope.project || !scope.session_id) {
            this._swarmNodes.notice.textContent = 'Open a saved conversation in a project to use teams.';
            return;
        }
        const request_id = `swarm-${++this._swarmRequestCounter}-${crypto.randomUUID()}`;
        const request = {command: 'swarm', action, execution_mode: this._swarmExecutionMode || 'personal',
            ...this._collaborationReadFields?.(action), ...this._managedSharingReadFields?.(action), ...extra, ...scope, request_id};
        if (!scope.run_id) delete request.run_id;
        // The owner chose "Run this team in Full-auto" (or Continue) for this request only (_swarmOfferFullAuto).
        if (this._swarmFullAutoGrant && this._swarmFullAutoGrant === action) {
            request.full_auto = true;
            this._swarmFullAutoGrant = null;
            this._swarmFullAutoGranted = true;
        }
        if (['view', 'events'].includes(action)) request.after = this._swarmCursor;
        const revision = this._swarmState?.run?.run?.revision;
        if (Number.isInteger(revision) && !['view', 'events', 'configure', 'history', 'read_artifact'].includes(action)) request.expected_revision = revision;
        const decision = LumiSwarmView.DECISIONS.has(action) ? {extra, retried} : null;
        this._swarmPending = {request_id, action, quiet, decision, participant: extra.attempt_id
            ? {attempt_id: extra.attempt_id, attempt_epoch: extra.attempt_epoch, text: extra.text} : null,
            historyPage: action === 'history' ? this._swarmRequestedHistoryPage : null,
            artifact: action === 'read_artifact' ? {artifact_id: extra.artifact_id, offset: extra.offset || 0, run_id: scope.run_id} : null};
        if (!quiet) this._swarmNodes.notice.textContent = ['view', 'events', 'history', 'read_artifact'].includes(action) ? 'Loading retained team information…' : 'Applying team change…';
        this._renderSwarmControls();
        this.send(request);
    }

    /**
     * Offer to send the refused Start or Continue again with the owner's
     * Full-auto grant for this one team (gui/swarming.py): the conversation
     * keeps its mode. The grant rides only on the request that button sends
     * (requestSwarm reads it while the form submits), never on a later one.
     * Focus never moves onto the button, so a second Enter can't press it;
     * the notice is a status, and takes focus itself only if focus was lost.
     */
    _swarmOfferFullAuto(action) {
        const nodes = this._swarmNodes;
        const button = document.createElement('button');
        button.type = 'button';
        button.className = 'swarm-full-auto-grant';
        button.textContent = action === 'start' ? 'Run this team in Full-auto' : 'Continue this team in Full-auto';
        button.addEventListener('click', () => {
            button.disabled = true;
            this._swarmFullAutoGrant = action;
            this._swarmFullAutoGranted = false;
            try {
                (action === 'start' ? nodes.form : nodes['continue-form']).requestSubmit();
            } finally {
                this._swarmFullAutoGrant = null;
            }
            // Nothing was sent (the form needs a fix first): the offer stays.
            if (!this._swarmFullAutoGranted) button.disabled = false;
        });
        nodes.notice.append(' ', button);
        nodes.notice.tabIndex = -1;
        if (this._focusNotice) this._focusNotice(nodes.notice);
    }

    _swarmStopRefreshVisible() {
        const target = this._swarmStopRefresh;
        return Boolean(target && this._swarmDialog?.open && this._swarmScope?.project === target.project
            && this._swarmScope?.session_id === target.session_id && this._swarmScope?.run_id === target.run_id
            && (this._swarmExecutionMode || 'personal') === target.execution_mode);
    }

    _receiveSwarmStopRefresh(event) {
        const target = this._swarmStopRefresh;
        if (!target || target.request_id !== event.request_id) return false;
        const visible = this._swarmStopRefreshVisible();
        this._swarmStopRefresh = null;
        const run = event.run?.run;
        const valid = event.project === target.project && event.session_id === target.session_id
            && event.execution_mode === target.execution_mode && run?.id === target.run_id
            && Number.isInteger(run.revision) && run.revision >= 0 && !event.error;
        if (visible) {
            this._swarmPending = {request_id: target.request_id, action: 'view', quiet: true};
            this.receiveSwarmState(valid ? event : {...event, run: null, project: target.project, session_id: target.session_id,
                error: event.error || 'Stop was not sent because the captured team could not be refreshed. Refresh and try again.'});
        }
        if (!valid || ['completed', 'cancelled', 'failed', 'stopping'].includes(run.state) || run.stop_requested) return true;
        if (!this.ws || this.ws.readyState !== WebSocket.OPEN) return true;
        const request_id = `swarm-${++this._swarmRequestCounter}-${crypto.randomUUID()}`;
        // A closed/replaced panel does not redirect this already requested Stop.
        // Its reply is only displayed if the original captured team is visible.
        if (visible) {
            this._swarmPending = {request_id, action: 'stop', quiet: false};
            this._swarmNodes.notice.textContent = 'Sending Stop to this team…';
            this._renderSwarmControls();
        }
        this.send({command: 'swarm', action: 'stop', ...target, request_id, expected_revision: run.revision});
        return true;
    }

    receiveSwarmState(event) {
        if (this._receiveSwarmStopRefresh(event)) return;
        if (!this._swarmDialog?.open || !this._swarmPending || event.request_id !== this._swarmPending.request_id) return;
        const scope = this._swarmScope;
        if (event.project !== scope.project || event.session_id !== scope.session_id) return;
        const {action, quiet, participant, historyPage, artifact, decision} = this._swarmPending;
        // A read of the session's latest run must not replace an explicit new
        // team draft. Only its own successful start may bind that draft.
        if (this._swarmPlanningNew && !['start', 'collaboration_prepare', 'managed_sharing_prepare'].includes(action)) event = {...event, run: null, events: []};
        const incomingRun = event.run?.run;
        if (scope.run_id && incomingRun?.id && incomingRun.id !== scope.run_id) return;
        this._swarmPending = null;
        if (action === 'view') this._swarmSelectingRun = false;
        if (event.error && decision && !decision.retried && /^Run revision changed/.test(String(event.error))) {
            // Another change landed first: an earlier acceptance, or a worker
            // the team started. The decision names its own immutable target
            // (attempt and candidate, or proposal), so after a refresh it is
            // sent once more; the server still checks that target.
            this._swarmRetryDecision = {action, extra: decision.extra};
            this.requestSwarm('view', {}, {quiet: true});
            return;
        }
        if (action === 'view' && this._swarmRetryDecision) {
            const retry = this._swarmRetryDecision;
            this._swarmRetryDecision = null;
            if (!event.error) setTimeout(() => this.requestSwarm(retry.action, retry.extra, {retried: true}), 0);
        }
        if (event.error) {
            this._swarmNodes.notice.textContent = typeof event.error === 'string' ? event.error : 'The team change could not be applied. Refresh to inspect its current state.';
            this._swarmNodes.notice.dataset.error = 'true';
            // Start and Continue are far below the notice; bring it into view.
            if (!quiet) this._swarmNodes.notice.scrollIntoView?.({block: 'nearest'});
            // A team the orchestrator runs needs Full-auto (gui/swarming.py): one click runs this team in it.
            if (event.code === 'needs_full_auto' && event.can_grant && ['start', 'continue_recovered'].includes(action)) {
                this._swarmOfferFullAuto(action);
            }
            this._swarmNodes.enabled.checked = Boolean(this._swarmState?.enabled);
            if (action === 'read_artifact' && artifact?.artifact_id === this._swarmArtifactState?.target.artifact_id) {
                this._swarmArtifactState.page = null;
                this._swarmArtifactState.error = this._swarmNodes.notice.textContent;
                this._renderSwarmArtifact();
            }
            this._renderSwarmControls();
            return;
        }
        const currentRevision = this._swarmState?.run?.run?.revision ?? -1;
        if (incomingRun && incomingRun.revision < currentRevision) { this._renderSwarmControls(); return; }
        this._swarmState = event;
        if (action === 'history' && event.history) {
            this._swarmHistory = event.history;
            this._swarmHistoryPage = historyPage ?? 0;
            this._swarmHistoryCursors[this._swarmHistoryPage + 1] = event.history.next_before_run_id;
            this._swarmHistoryCursors.length = this._swarmHistoryPage + 2;
            this._renderSwarmHistory();
        }
        if (action === 'read_artifact' && artifact && event.artifact_page) {
            const page = event.artifact_page;
            const target = this._swarmArtifactState?.target;
            if (target && artifact.run_id === scope.run_id && page.run_id === scope.run_id && page.artifact?.id === target.artifact_id
                && artifact.artifact_id === target.artifact_id && page.offset === artifact.offset && page.verified_sha256 === target.sha256) {
                this._swarmArtifactState.page = page;
                this._swarmArtifactState.error = null;
                if (!this._swarmArtifactState.offsets.includes(page.offset)) this._swarmArtifactState.offsets.push(page.offset);
                this._renderSwarmArtifact();
            }
        }
        if (action === 'steer_worker' && participant) {
            const node = this._swarmWorkers.get(participant.attempt_id);
            const input = node?.querySelector('[data-guidance-text]');
            if (node?._controlTarget?.attempt_epoch === participant.attempt_epoch && input?.value.trim() === participant.text) input.value = '';
        }
        if (action === 'set_concurrency') this._swarmConcurrencyDirty = false;
        if (action === 'discard_kept_work') this._swarmNodes['discard-confirm'].checked = false;
        if (action === 'export_report' && event.report && typeof event.report === 'object' && !Array.isArray(event.report)) {
            const contents = JSON.stringify(event.report, null, 2) + '\n';
            const url = URL.createObjectURL(new Blob([contents], {type: 'application/json;charset=utf-8'}));
            const link = document.createElement('a');
            link.href = url;
            link.download = `SONN-swarm-${String(scope.run_id).replace(/[^A-Za-z0-9._-]/g, '_').slice(0, 100)}.json`;
            link.hidden = true;
            this._swarmDialog.appendChild(link);
            link.click();
            link.remove();
            setTimeout(() => URL.revokeObjectURL(url), 1000);
        }
        if (['start', 'collaboration_prepare', 'managed_sharing_prepare'].includes(action) && incomingRun) { this._swarmPlanningNew = false; this._swarmHistory = null; this._swarmHistoryCursors = [null]; }
        if (incomingRun && !scope.run_id) this._swarmScope = Object.freeze({...scope, run_id: incomingRun.id});
        const nodes = this._swarmNodes;
        if (!quiet || !nodes.notice.dataset.error) {
            delete nodes.notice.dataset.error;
            nodes.notice.textContent = event.message || (event.available ? 'Team preview is available. Submitted results still need verification and owner acceptance.' : 'Teams are unavailable for this conversation.');
        }
        nodes.enabled.checked = Boolean(event.enabled);
        if (event.execution_mode) this._swarmExecutionMode = event.execution_mode;
        nodes['execution-mode'].value = this._swarmExecutionMode;
        nodes['execution-mode'].querySelector('[value="managed"]').disabled = !event.managed?.available;
        const managed = event.managed;
        const policy = managed?.effective_policy;
        nodes['managed-status'].textContent = this._swarmExecutionMode === 'managed'
            ? `Organization ${managed?.tenant_id || 'unavailable'} · ${managed?.connection || 'not contacted'} · No offline execution. Metadata and request/action counts are reported; prompts and file contents are not uploaded.`
                + (policy?.authenticated ? ` ${policy.valid ? 'Current' : 'Expired'} policy: ${policy.policy?.max_workers ?? '—'} worker slots; ${policy.policy?.request_limit ?? '—'} shared model requests.` : ' Effective policy has not been obtained.')
            : managed?.available ? 'Personal work stays local. Choose organization ownership explicitly for a new team.'
                : 'Organization managed teams run under your organization’s Lumi Cloud, with a setup your administrator gives Lumi when it starts. Personal teams need nothing more.';
        nodes.model.textContent = event.model ? `${event.model.label || event.model.provider} · ${event.model.model}` : 'No supported model selected';
        this._swarmWorkerModels(event.model);
        const snapshot = event.run;
        nodes['run-section'].hidden = !incomingRun;
        const workerModel = snapshot?.worker_model;
        nodes['worker-model-note'].hidden = !(incomingRun && workerModel);
        nodes['worker-model-note'].textContent = workerModel
            ? `Workers use ${workerModel.label || workerModel.provider} · ${workerModel.model}; the ${this._swarmCoordinatorName().toLowerCase()} uses this session’s model.` : '';
        if (incomingRun) {
            nodes['run-state'].textContent = this._swarmStateLabel(incomingRun.state);
            nodes['run-objective'].textContent = incomingRun.objective;
            const reservations = snapshot.reservations || [];
            const known = reservations.reduce((sum, row) => sum + (Number.isInteger(row.used) ? row.used : 0), 0);
            const held = reservations.filter(row => row.state !== 'settled').reduce((sum, row) => sum + row.amount, 0);
            nodes.accounting.textContent = `${known} requests accounted for · ${held} held or uncertain · ${snapshot.remaining_requests ?? '—'} available from ${incomingRun.request_limit}`;
            this._renderSwarmConcurrency(snapshot);
            this._renderSwarmWorkers(snapshot);
            this._renderSwarmProposals(snapshot);
            this._renderSwarmRecovery(snapshot);
            this._renderSwarmIntegration(snapshot);
            this._renderSwarmMessages(snapshot);
        }
        this._renderSwarmOrchestrator(incomingRun ? event.autonomy : null);
        this._renderSwarmInbox(snapshot);
        this._renderSwarmKeptEverywhere(event.kept_everywhere);
        for (const record of event.events || []) {
            if (!Number.isInteger(record.sequence)) continue;
            this._swarmActivity.set(record.sequence, record);
            this._swarmCursor = Math.max(this._swarmCursor, record.sequence);
        }
        const activity = [...this._swarmActivity.values()].sort((a, b) => a.sequence - b.sequence).slice(-30);
        for (const sequence of [...this._swarmActivity.keys()].sort((a, b) => a - b).slice(0, -100)) this._swarmActivity.delete(sequence);
        nodes.activity.replaceChildren(...activity.map(record => {
            const item = document.createElement('li');
            item.textContent = this._swarmActivityLabel(record.kind);
            return item;
        }));
        if (!activity.length) { const item = document.createElement('li'); item.textContent = 'No recorded activity yet.'; nodes.activity.appendChild(item); }
        this._renderSwarmControls();
        if (action === 'view') this._swarmApplyObjectiveDraft();
        if (!this._swarmHistory && action !== 'history') this._swarmLoadHistory(0);
    }

    _swarmLoadHistory(page) {
        if (page < 0 || (page > 0 && !this._swarmHistoryCursors[page])) return;
        this._swarmRequestedHistoryPage = page;
        this.requestSwarm('history', {limit: 20, ...(this._swarmHistoryCursors[page] ? {before_run_id: this._swarmHistoryCursors[page]} : {})});
    }

    _swarmIsRead(action) {
        return ['view', 'events', 'history', 'read_artifact', 'collaboration_inspect', 'managed_recovery_inspect'].includes(action);
    }

    _renderSwarmHistory() {
        const nodes = this._swarmNodes;
        const prior = nodes['history-select'].value;
        const items = Array.isArray(this._swarmHistory?.items) ? this._swarmHistory.items : [];
        const placeholder = document.createElement('option'); placeholder.value = ''; placeholder.textContent = items.length ? 'Choose a retained team' : 'No retained teams';
        nodes['history-select'].replaceChildren(placeholder, ...items.map(item => {
            const option = document.createElement('option'); option.value = item.run_id;
            option.textContent = `${String(item.objective || 'Untitled team').slice(0, 90)}${item.objective_truncated ? '…' : ''} · ${this._swarmStateLabel(item.state)}`;
            option.title = `${item.objective || ''}${item.started_at ? ' · ' + new Date(item.started_at * 1000).toLocaleString() : ''}`;
            return option;
        }));
        const selected = items.some(item => item.run_id === prior) ? prior : items.some(item => item.run_id === this._swarmScope.run_id) ? this._swarmScope.run_id : '';
        nodes['history-select'].value = selected;
        nodes['history-status'].textContent = `${items.length} saved teams on page ${this._swarmHistoryPage + 1}. Opening a team keeps controls in this conversation.`;
    }

    _swarmRememberRun() {
        if (!this._swarmScope.run_id || !this._swarmState?.run) return;
        const stored = {state: this._swarmState, cursor: this._swarmCursor, artifact: this._swarmArtifactState,
            concurrencyDirty: this._swarmConcurrencyDirty, concurrencyValue: this._swarmNodes['worker-limit'].value,
            recoveryAllowance: this._swarmNodes['recovery-allowance'].value};
        for (const name of ['Activity', 'Workers', 'Proposals', 'RecoveryRows', 'RetryRows', 'WriterRows', 'CandidateRows']) stored[name] = this[`_swarm${name}`];
        this._swarmRunCache.set(this._swarmScope.run_id, stored);
    }

    _swarmOpenRun(runId) {
        if (!runId || (this._swarmPending && !this._swarmIsRead(this._swarmPending.action))) return;
        this._swarmRememberRun();
        const cached = this._swarmRunCache.get(runId);
        this._swarmScope = Object.freeze({...this._swarmScope, run_id: runId});
        this._swarmPlanningNew = false;
        this._swarmSelectingRun = true;
        this._swarmPending = null;
        this._swarmState = cached?.state || {...this._swarmState, run: null, events: []};
        this._swarmCursor = cached?.cursor || 0;
        this._swarmConcurrencyDirty = cached?.concurrencyDirty || false;
        this._swarmConcurrencyCap = null;
        this._swarmNodes['recovery-allowance'].value = cached?.recoveryAllowance || '4';
        for (const [name, container] of [['Activity', null], ['Workers', 'workers'], ['Proposals', 'proposals'], ['RecoveryRows', 'recovery-records'],
            ['RetryRows', 'retry-items'], ['WriterRows', 'writer-results'], ['CandidateRows', 'candidates']]) {
            this[`_swarm${name}`] = cached?.[name] || new Map();
            if (container) this._swarmNodes[container].replaceChildren(...this[`_swarm${name}`].values());
        }
        this._swarmArtifactState = cached?.artifact || null;
        this._renderSwarmArtifact();
        this._swarmNodes['run-section'].hidden = true;
        this._swarmNodes.activity.replaceChildren();
        if (cached?.state.run) {
            this._renderSwarmConcurrency(cached.state.run);
            if (cached.concurrencyDirty) this._swarmNodes['worker-limit'].value = cached.concurrencyValue;
        }
        this.requestSwarm('view');
    }

    _swarmReadArtifact(target, offset = 0) {
        if (!target || target.run_id !== this._swarmScope.run_id || (this._swarmPending && !this._swarmIsRead(this._swarmPending.action))) return;
        if (this._swarmArtifactState?.target.artifact_id !== target.artifact_id) this._swarmArtifactState = {target, page: null, offsets: [0]};
        this._renderSwarmArtifact();
        this._swarmNodes['artifact-viewer'].hidden = false;
        if (!this._swarmArtifactState.page) this._swarmNodes['artifact-title'].focus();
        this.requestSwarm('read_artifact', {artifact_id: target.artifact_id, offset, limit: 8000});
    }

    _renderSwarmArtifact() {
        const nodes = this._swarmNodes;
        const state = this._swarmArtifactState;
        nodes['artifact-viewer'].hidden = !state;
        if (!state) { nodes['artifact-text'].textContent = ''; return; }
        const page = state.page;
        nodes['artifact-title'].textContent = state.target.label || 'Retained evidence';
        nodes['artifact-provenance'].textContent = `SHA-256: ${state.target.sha256}${page ? ` · ${page.artifact.size} bytes · ${page.artifact.origin}` : ''}`;
        nodes['artifact-status'].textContent = page ? (page.format === 'text'
            ? `Characters ${page.offset + (page.total_characters ? 1 : 0)}–${page.next_offset ?? page.total_characters} of ${page.total_characters}. ${page.message}` : page.message) : (state.error || 'Verifying the complete retained content…');
        nodes['artifact-text'].textContent = page?.format === 'text' ? page.text : '';
        nodes['artifact-text'].hidden = page?.format !== 'text';
        nodes['artifact-previous'].hidden = !page || state.offsets.indexOf(page.offset) <= 0;
        nodes['artifact-next'].hidden = !Number.isInteger(page?.next_offset);
    }

    _renderSwarmConcurrency(snapshot) {
        const nodes = this._swarmNodes;
        let policy = {};
        try { policy = JSON.parse(snapshot.run.policy_json); } catch (_) { /* Missing limits cannot authorize a change. */ }
        const cap = policy.max_workers;
        const limit = snapshot.run.worker_limit;
        const known = Number.isInteger(cap) && cap >= 1 && cap <= 8 && Number.isInteger(limit) && limit >= 1 && limit <= cap;
        nodes['concurrency-form'].hidden = !known;
        if (!known) { this._swarmConcurrencyCap = null; return; }
        if (this._swarmConcurrencyCap !== cap) {
            nodes['worker-limit'].replaceChildren(...Array.from({length: cap}, (_, index) => {
                const option = document.createElement('option');
                option.value = String(index + 1);
                option.textContent = `${index + 1} worker${index ? 's' : ''}`;
                return option;
            }));
            this._swarmConcurrencyDirty = false;
            this._swarmConcurrencyCap = cap;
        }
        if (!this._swarmConcurrencyDirty) nodes['worker-limit'].value = String(limit);
        const active = (snapshot.attempts || []).filter(attempt => ['leased', 'running', 'uncertain'].includes(attempt.state)
            || attempt.process_state !== 'stopped');
        const workers = active.filter(attempt => attempt.kind !== 'coordinator').length;
        const coordinators = active.filter(attempt => attempt.kind === 'coordinator').length;
        nodes['worker-count'].textContent = `${workers} active worker${workers === 1 ? '' : 's'} · Assignment limit: ${limit} of ${cap} allowed · ${coordinators} active coordinator${coordinators === 1 ? '' : 's'}`;
    }

    _renderSwarmWorkers(snapshot) {
        const parent = this._swarmNodes.workers;
        const items = new Map((snapshot.work_items || []).map(item => [item.id, item]));
        const attempts = snapshot.attempts || [];
        // Every outstanding review remains reachable even when a coordinator
        // has proposed more tasks than the bounded accepted-history window.
        const active = attempts.filter(attempt => ['leased', 'running', 'uncertain'].includes(attempt.state)
            || (attempt.state === 'submitted' && items.get(attempt.work_item_id)?.state !== 'accepted'));
        const historyLimit = Math.max(0, 20 - active.length);
        const history = historyLimit ? attempts.filter(attempt => !active.includes(attempt)).slice(-historyLimit) : [];
        const shown = new Set([...active, ...history].map(attempt => attempt.id));
        for (const [id, node] of this._swarmWorkers) {
            if (!shown.has(id)) { node.remove(); this._swarmWorkers.delete(id); }
        }
        const numbers = new Map(attempts.filter(attempt => attempt.kind !== 'coordinator').map((attempt, index) => [attempt.id, index + 1]));
        for (const attempt of attempts.filter(attempt => shown.has(attempt.id))) {
            const name = attempt.kind === 'coordinator' ? this._swarmCoordinatorName() : `Worker ${numbers.get(attempt.id)}`;
            let node = this._swarmWorkers.get(attempt.id);
            if (!node) {
                node = document.createElement('details');
                node.className = 'swarm-worker';
                node.innerHTML = `<summary></summary><dl></dl><section data-worker-evidence class="swarm-worker-evidence" hidden>
                    <label>Retained evidence<select data-evidence-select></select></label><button type="button" data-evidence-read>Read evidence</button></section><form class="swarm-review" hidden>
                    <h4>Owner review</h4><p class="swarm-help">Accepting records your review of these findings. It does not run automated checks.</p>
                    <label>Review notes<textarea rows="3" maxlength="8000" required placeholder="Explain what you reviewed and why the findings satisfy this task."></textarea></label>
                    <p class="swarm-help" data-review-paused hidden>Resume the team before accepting findings.</p>
                    <button type="submit">Accept findings</button></form>
                    <form data-result-reject class="swarm-review" hidden><h4>Request another attempt</h4><label>Rejection notes<textarea rows="2" maxlength="8000" required placeholder="Describe what must be corrected in this result."></textarea></label><button type="submit">Reject result</button></form>
                    <form data-work-retry class="swarm-review" hidden><label>Retry notes<textarea rows="2" maxlength="8000" required placeholder="Explain why another attempt is appropriate."></textarea></label><p class="swarm-help">Retry admits another worker using the existing scope, model and remaining allowance.</p><button type="submit">Retry task</button></form>
                    <section data-participant-controls class="swarm-participant" hidden><p data-participant-status role="status"></p>
                    <div data-participant-buttons class="swarm-controls"><button type="button" data-worker-action="pause_worker">Pause</button><button type="button" data-worker-action="resume_worker">Resume</button><button type="button" data-worker-action="cancel_worker" class="swarm-stop">Stop</button></div>
                    <form data-participant-guidance><label>Task guidance<textarea data-guidance-text rows="3" maxlength="8000" required placeholder="Clarify this task within its existing file access and tools."></textarea></label>
                    <p class="swarm-help">Guidance cannot expand this participant’s task permissions. Queued and prepared input receipts do not prove the model understood or followed it.</p><button type="submit">Send guidance</button></form>
                    <div data-guidance-history hidden><h4>Guidance receipts</h4><p data-guidance-history-limit class="swarm-help"></p><ul></ul></div></section>`;
                node.querySelector('form').addEventListener('submit', event => {
                    event.preventDefault();
                    const form = node.querySelector('form');
                    const evidence = form.querySelector('textarea').value.trim();
                    if (!form.reportValidity() || !evidence || !node._reviewTarget) return;
                    this.requestSwarm('review_read_result', {...node._reviewTarget, evidence});
                });
                for (const [selector, action] of [['[data-result-reject]', 'reject_result'], ['[data-work-retry]', 'retry_work']]) {
                    const form = node.querySelector(selector);
                    form.addEventListener('submit', event => {
                        event.preventDefault();
                        const evidence = form.querySelector('textarea').value.trim();
                        if (form.reportValidity() && evidence && form._target) this.requestSwarm(action, {...form._target, evidence});
                    });
                }
                for (const button of node.querySelectorAll('[data-worker-action]')) {
                    button.addEventListener('click', () => {
                        if (node._controlTarget) this.requestSwarm(button.dataset.workerAction, {...node._controlTarget});
                    });
                }
                node.querySelector('[data-participant-guidance]').addEventListener('submit', event => {
                    event.preventDefault();
                    const form = node.querySelector('[data-participant-guidance]');
                    const text = form.querySelector('textarea').value.trim();
                    if (form.reportValidity() && text && node._controlTarget) this.requestSwarm('steer_worker', {...node._controlTarget, text});
                });
                node.querySelector('[data-evidence-read]').addEventListener('click', () => {
                    const selected = node._artifacts?.get(node.querySelector('[data-evidence-select]').value);
                    if (selected) this._swarmReadArtifact({run_id: this._swarmScope.run_id, artifact_id: selected.id,
                        sha256: selected.sha256, label: selected.label || selected.kind});
                });
                this._swarmWorkers.set(attempt.id, node);
                parent.appendChild(node);
            }
            const work = items.get(attempt.work_item_id);
            let grant = {};
            try { grant = typeof attempt.grant_json === 'string' ? JSON.parse(attempt.grant_json) : (attempt.grant || {}); } catch (_) { /* Unreadable records remain visibly unknown. */ }
            const runtime = (snapshot.workers || []).find(row => row.attempt_id === attempt.id);
            const displayState = attempt.state === 'submitted' ? (work?.state || attempt.state)
                : ['leased', 'running'].includes(attempt.state) && ['paused', 'pausing', 'stopping'].includes(runtime?.state) ? runtime.state : attempt.state;
            node.querySelector('summary').textContent = `${name} · ${this._swarmStateLabel(displayState)}`;
            const rows = [['Task', work?.objective || (attempt.kind === 'coordinator' ? this._swarmTurnPurpose(attempt) : 'Task details unavailable')], ['Model', grant.model ? `${grant.model.provider} · ${grant.model.model}` : 'Unavailable'],
                ['Read access', (grant.read_roots || []).join(', ') || 'No file access'], ['Write access', (grant.write_roots || []).join(', ') || 'Read only'], ['Execution', this._swarmStateLabel(attempt.process_state)]];
            if (runtime) {
                rows.push(['Worker activity', `${this._swarmStateLabel(runtime.state)} · ${runtime.alive ? 'still active' : (runtime.termination_recorded ? 'exit confirmed' : 'exit not confirmed')}`]);
                if (runtime.error) rows.push(['Runtime observation', String(runtime.error).slice(0, 2000)]);
            }
            const submission = (snapshot.submissions || []).find(row => row.attempt_id === attempt.id);
            // The server bounds a complete handoff to 64 KiB. Show all of that
            // admitted text so owner review cannot silently miss its ending.
            if (submission) rows.push(['Submitted findings', String(submission.handoff || '').slice(0, 65536)],
                ['Candidate revision', String(submission.candidate_revision || '').slice(0, 200)]);
            const artifacts = (snapshot.artifact_refs || []).filter(row => row.attempt_id === attempt.id);
            const evidenceSelect = node.querySelector('[data-evidence-select]');
            if (JSON.stringify([...node._artifacts?.keys() || []]) !== JSON.stringify(artifacts.map(row => row.id))) {
                const prior = evidenceSelect.value;
                evidenceSelect.replaceChildren(...artifacts.map(artifact => {
                    const option = document.createElement('option'); option.value = artifact.id;
                    option.textContent = `${String(artifact.label || artifact.kind).slice(0, 160)} · ${artifact.kind}`;
                    option.title = `SHA-256: ${artifact.sha256}`;
                    return option;
                }));
                evidenceSelect.value = artifacts.some(row => row.id === prior) ? prior : (artifacts.at(-1)?.id || '');
            }
            node._artifacts = new Map(artifacts.map(row => [row.id, row]));
            evidenceSelect.setAttribute('aria-label', `Retained evidence for ${name}`);
            node.querySelector('[data-evidence-read]').setAttribute('aria-label', `Read evidence for ${name}`);
            node.querySelector('[data-worker-evidence]').hidden = !artifacts.length;
            node.querySelector('dl').replaceChildren(...rows.flatMap(([key, value]) => {
                const term = document.createElement('dt'); term.textContent = key;
                const detail = document.createElement('dd'); detail.textContent = value;
                return [term, detail];
            }));
            const form = node.querySelector('form');
            const target = submission ? {attempt_id: attempt.id, attempt_epoch: attempt.epoch, candidate_revision: submission.candidate_revision} : null;
            // A different candidate invalidates a draft review. Polling the
            // same immutable submission leaves focused notes untouched.
            if (node._reviewTarget && node._reviewTarget.candidate_revision !== target?.candidate_revision) form.querySelector('textarea').value = '';
            node._reviewTarget = target;
            form.setAttribute('aria-label', `Review ${name}`);
            form.querySelector('textarea').setAttribute('aria-label', `Review notes for ${name}`);
            form.hidden = !submission || work?.state !== 'submitted' || attempt.process_state !== 'stopped'
                || !Array.isArray(grant.write_roots) || grant.write_roots.length > 0;
            const reject = node.querySelector('[data-result-reject]');
            reject._target = {attempt_id: attempt.id, attempt_epoch: attempt.epoch};
            reject.hidden = !submission || work?.state !== 'submitted' || attempt.state !== 'submitted' || attempt.process_state !== 'stopped';
            reject.setAttribute('aria-label', `Reject ${name}`);
            reject.querySelector('textarea').setAttribute('aria-label', `Rejection notes for ${name}`);
            const retry = node.querySelector('[data-work-retry]');
            retry._target = {work_item_id: attempt.work_item_id};
            retry.hidden = !['failed', 'cancelled'].includes(work?.state) || attempt.kind === 'coordinator'
                || attempts.filter(row => row.work_item_id === attempt.work_item_id).at(-1)?.id !== attempt.id;
            retry.setAttribute('aria-label', `Retry ${name}`);
            retry.querySelector('textarea').setAttribute('aria-label', `Retry notes for ${name}`);
            this._renderSwarmParticipant(node, attempt, runtime, snapshot, name);
        }
    }

    _renderSwarmParticipant(node, attempt, runtime, snapshot, name) {
        const section = node.querySelector('[data-participant-controls]');
        const input = node.querySelector('[data-guidance-text]');
        if (node._controlTarget && node._controlTarget.attempt_epoch !== attempt.epoch) input.value = '';
        node._controlTarget = {attempt_id: attempt.id, attempt_epoch: attempt.epoch};
        node._controlActive = ['leased', 'running'].includes(attempt.state) && attempt.process_state !== 'stopped';
        node._pauseRequested = Boolean(attempt.pause_requested);
        node._cancelRequested = Boolean(attempt.cancel_requested);
        const directives = (snapshot.owner_directives || []).filter(row => row.attempt_id === attempt.id && row.epoch === attempt.epoch);
        section.hidden = !node._controlActive && !directives.length;
        node.querySelector('[data-participant-buttons]').hidden = !node._controlActive;
        node.querySelector('[data-participant-guidance]').hidden = !node._controlActive;
        node.querySelector('[data-participant-guidance]').setAttribute('aria-label', `Guide ${name}`);
        input.setAttribute('aria-label', `Guidance for ${name}`);
        node.querySelector('[data-participant-guidance] button').setAttribute('aria-label', `Send guidance to ${name}`);
        for (const [action, label] of [['pause_worker', 'Pause'], ['resume_worker', 'Resume'], ['cancel_worker', 'Stop']]) {
            node.querySelector(`[data-worker-action="${action}"]`).setAttribute('aria-label', `${label} ${name}`);
        }
        node.querySelector('[data-participant-status]').textContent = attempt.process_state === 'stopped'
            ? 'Termination is recorded. Existing guidance receipts remain available.'
            : node._cancelRequested ? 'Stop requested. Termination is not yet confirmed.'
            : runtime?.state === 'paused' ? 'Paused at an observed checkpoint.'
            : node._pauseRequested ? 'Pause requested. Waiting for the current activity to reach a checkpoint.'
            : ['pausing', 'paused'].includes(snapshot.run.state) ? 'The team is paused or pausing. Resume the team before resuming this participant.'
            : 'Controls affect only this participant. Stopping it cannot be undone.';
        const receipts = new Map((snapshot.owner_directive_receipts || []).filter(row => row.attempt_id === attempt.id && row.epoch === attempt.epoch)
            .map(row => [row.directive_id, row]));
        const pending = directives.filter(row => !receipts.has(row.id));
        const prepared = directives.filter(row => receipts.has(row.id)).slice(-10);
        const shown = new Set([...pending, ...prepared].map(row => row.id));
        node.querySelector('[data-guidance-history]').hidden = !directives.length;
        node.querySelector('[data-guidance-history-limit]').textContent = 'All queued guidance and the latest ten prepared inputs are shown. Prepared input is distinct from model comprehension.';
        node.querySelector('[data-guidance-history] ul').replaceChildren(...directives.filter(row => shown.has(row.id)).map(row => {
            const item = document.createElement('li');
            const state = document.createElement('strong');
            const body = document.createElement('p');
            state.textContent = receipts.has(row.id) ? 'Included in prepared model input' : 'Queued for this participant';
            body.textContent = String(row.text || '').slice(0, 8192);
            item.append(state, body);
            return item;
        }));
    }

    _renderSwarmProposals(snapshot) {
        const proposals = (snapshot.coordinator_proposals || []).slice(-8);
        const shown = new Set(proposals.map(proposal => proposal.id));
        for (const [id, node] of this._swarmProposals) {
            if (!shown.has(id)) { node.remove(); this._swarmProposals.delete(id); }
        }
        for (const proposal of proposals) {
            let node = this._swarmProposals.get(proposal.id);
            if (!node) {
                node = document.createElement('article');
                node.className = 'swarm-proposal';
                node.innerHTML = `<h4>Proposed investigations</h4><p data-proposal-state></p><p data-proposal-summary></p><p data-proposal-approach class="swarm-help"></p><p data-proposal-decision class="swarm-help" hidden></p>
                    <ol data-proposal-tasks></ol><form aria-label="Review proposed plan"><p class="swarm-help">Approval starts the listed work within the team’s remaining allowance. Findings still need your separate review.</p>
                    <label>Plan review notes<textarea rows="3" maxlength="8000" required placeholder="Explain why you approve or reject these investigations and their access."></textarea></label>
                    <p data-proposal-wait class="swarm-help"></p><div class="swarm-controls"><button type="submit" value="approve">Approve plan</button><button type="submit" value="reject">Reject plan</button></div></form>`;
                node.querySelector('form').addEventListener('submit', event => {
                    event.preventDefault();
                    const form = node.querySelector('form');
                    const evidence = form.querySelector('textarea').value.trim();
                    if (!form.reportValidity() || !evidence || !node._proposalTarget) return;
                    this.requestSwarm('decide_proposal', {...node._proposalTarget, evidence, accept: event.submitter?.value === 'approve'});
                });
                this._swarmProposals.set(proposal.id, node);
                this._swarmNodes.proposals.appendChild(node);
            }
            // A changed immutable identity invalidates notes written for the old
            // proposal. Ordinary snapshots preserve text, selection and focus.
            if (node._proposalTarget?.sha256 !== proposal.sha256) {
                node.querySelector('textarea').value = '';
                node._proposalTarget = {proposal_id: proposal.id, sha256: proposal.sha256};
                let plan;
                try { plan = JSON.parse(proposal.payload_json).plan; } catch (_) { plan = null; }
                node._readableProposal = Boolean(plan && typeof plan.summary === 'string' && Array.isArray(plan.work_items));
                node.querySelector('[data-proposal-summary]').textContent = node._readableProposal ? plan.summary : 'Plan details are unavailable. Refresh before making a decision.';
                const items = node._readableProposal ? plan.work_items.slice(0, 256) : [];
                // A follow-up with no work is the orchestrator's final report.
                const report = node._readableProposal && !items.length;
                node.querySelector('h4').textContent = report ? 'Orchestrator report'
                    : items.some(item => item.role === 'implement') ? 'Proposed work' : 'Proposed investigations';
                node.querySelector('[data-proposal-approach]').textContent = !node._readableProposal ? ''
                    : report ? 'No more work proposed: this is the final report.'
                    : plan.use_team ? 'Tasks can run as their dependencies are accepted and worker slots become available.'
                    : `The ${this._swarmCoordinatorName().toLowerCase()} recommends one focused assignment.`;
                // One numbering; a writer's task is a change and a verifier's a check.
                const noun = item => ({implement: 'Change', verify: 'Check'}[item.role] || 'Investigation');
                const labels = new Map(items.map((item, index) => [item.id, `${noun(item)} ${index + 1}`]));
                node.querySelector('[data-proposal-tasks]').replaceChildren(...items.map((item, index) => {
                    const entry = document.createElement('li');
                    const title = document.createElement('strong');
                    title.textContent = `${labels.get(item.id)}: ${item.objective}`;
                    const detail = document.createElement('p');
                    const dependencies = (item.dependencies || []).map(id => labels.get(id) || 'Unavailable task');
                    detail.textContent = `Role: ${{explore: 'Investigate', verify: 'Verify', implement: 'Implement'}[item.role] || 'Unknown'}. Read access: ${(item.read_roots || []).join(', ') || 'None'}. Write access: ${(item.write_roots || []).join(', ') || 'None'}. Depends on: ${dependencies.join(', ') || 'None'}. Review criteria: ${(item.criteria || []).map(name => name === 'owner_review' ? 'Owner review of findings' : name).join(', ')}.`;
                    entry.append(title, detail);
                    return entry;
                }));
            }
            node.querySelector('[data-proposal-state]').textContent = {pending: this._swarmState?.autonomy ? 'Being decided by the orchestrator' : 'Awaiting your plan review', accepted: 'Plan approved', rejected: 'Plan rejected'}[proposal.state] || 'Plan state unavailable';
            node.querySelector('[data-proposal-decision]').hidden = !proposal.decision_evidence;
            node.querySelector('[data-proposal-decision]').textContent = proposal.decision_evidence
                ? `${this._swarmState?.autonomy ? 'Decision' : 'Owner decision'}: ${proposal.decision_evidence}` : '';
            const attempt = (snapshot.attempts || []).find(row => row.id === proposal.attempt_id);
            node._proposalReady = proposal.state === 'pending' && attempt?.state === 'completed' && attempt?.process_state === 'stopped';
            node.querySelector('form').hidden = proposal.state !== 'pending';
            node.querySelector('[data-proposal-wait]').textContent = node._proposalReady ? '' : 'Waiting for coordinator cleanup before review.';
        }
    }

    _renderSwarmIntegration(snapshot) {
        const nodes = this._swarmNodes;
        const setup = snapshot.writer_setup;
        nodes['integration-section'].hidden = !setup;
        if (!setup) return;
        nodes['writer-base'].textContent = `Captured branch: ${setup.target_branch || 'Unavailable'} · Base: ${setup.base_revision || 'Unavailable'} · Writable folders: ${(setup.write_roots || []).join(', ')}`;
        const writers = snapshot.writer_worktrees || [];
        const attempts = new Map((snapshot.attempts || []).map(row => [row.id, row]));
        const work = new Map((snapshot.work_items || []).map(row => [row.id, row]));
        const writerIds = new Set(writers.map(row => row.id));
        for (const [id, row] of this._swarmWriterRows) {
            if (!writerIds.has(id)) { row.remove(); this._swarmWriterRows.delete(id); }
        }
        for (const writer of writers) {
            let row = this._swarmWriterRows.get(writer.id);
            if (!row) {
                row = document.createElement('label');
                row.className = 'swarm-writer-result';
                row.innerHTML = '<input type="checkbox"><span><strong></strong><span data-writer-state></span><span data-writer-files></span></span>';
                row.querySelector('input').addEventListener('change', () => this._renderSwarmControls());
                this._swarmWriterRows.set(writer.id, row);
                nodes['writer-results'].appendChild(row);
            }
            const attempt = attempts.get(writer.attempt_id);
            const item = work.get(attempt?.work_item_id);
            let manifest = {};
            try { manifest = JSON.parse(writer.manifest_json); } catch (_) { /* Unreadable details cannot authorize selection. */ }
            row.querySelector('strong').textContent = item?.objective || 'Writer result';
            row.querySelector('[data-writer-state]').textContent = `Result: ${writer.state} · Revision: ${writer.result_revision || 'Not finalized'}`;
            row.querySelector('[data-writer-files]').textContent = `Changed files: ${(manifest.changed_paths || []).join(', ') || 'None recorded'}`;
            row._eligible = writer.state === 'ready' && attempt?.state === 'submitted' && attempt?.process_state === 'stopped' && item?.state === 'submitted';
        }
        const operations = snapshot.integration_operations || [];
        nodes['integration-status'].textContent = operations.slice(-5).map(row => `${({prepare_candidate: 'Preparing changes', run_check: 'Verification check', apply: 'Applying changes', reconcile_application: 'Inspecting application'})[row.kind] || 'File integration'}: ${row.state}${row.waiting ? ' · waiting for another step on this repository' : ''}${row.error ? ' · ' + row.error : ''}`).join(' · ');
        const candidates = snapshot.integration_candidates || [];
        const shown = new Set(candidates.map(row => row.id));
        for (const [id, row] of this._swarmCandidateRows) {
            if (!shown.has(id)) { row.remove(); this._swarmCandidateRows.delete(id); }
        }
        for (const candidate of candidates) {
            let row = this._swarmCandidateRows.get(candidate.id);
            if (!row) {
                row = document.createElement('article');
                row.className = 'swarm-candidate';
                row.innerHTML = `<h4>Combined changes</h4><p data-candidate-state></p><p data-candidate-revisions class="swarm-help"></p>
                    <button type="button" data-candidate-inspect>Inspect candidate</button><details><summary>Review changed files and diff</summary><p data-candidate-files></p><p data-candidate-limit class="swarm-help"></p><pre data-candidate-diff></pre></details>
                    <div data-candidate-checks></div><form data-candidate-apply aria-label="Apply verified changes"><label>Application review notes<textarea rows="3" required maxlength="8000" placeholder="Explain what you reviewed in these exact changes and verification results."></textarea></label>
                    <p class="swarm-help">Apply updates the captured project branch only if its base is unchanged and the checkout is clean. It does not record task acceptance.</p><button type="submit">Apply reviewed changes</button></form>
                    <div data-candidate-acceptances></div>`;
                row.querySelector('[data-candidate-apply]').addEventListener('submit', event => {
                    event.preventDefault();
                    const form = row.querySelector('[data-candidate-apply]');
                    const evidence = form.querySelector('textarea').value.trim();
                    if (form.reportValidity() && evidence && row._candidateTarget) this.requestSwarm('apply_candidate', {...row._candidateTarget, evidence});
                });
                row.querySelector('[data-candidate-inspect]').addEventListener('click', () => this.requestSwarm('inspect_candidate', {candidate_id: candidate.id}));
                row._checks = new Map();
                row._acceptances = new Map();
                this._swarmCandidateRows.set(candidate.id, row);
                nodes.candidates.appendChild(row);
            }
            if (row._candidateTarget?.target_revision !== candidate.result_revision) row.querySelector('[data-candidate-apply] textarea').value = '';
            row._candidateTarget = {candidate_id: candidate.id, expected_base: candidate.base_revision, target_revision: candidate.result_revision};
            row._candidateState = candidate.state;
            row.querySelector('[data-candidate-state]').textContent = ({ready: 'Ready for verification', verified: 'Checks passed for this exact revision', applied: 'Changes applied; task acceptance remains separate', failed: 'Verification needs review', conflict: 'Changes conflict; repair is required', superseded: 'Replaced by a newer attempt', uncertain: 'Integration outcome needs reconciliation', preparing: 'Preparing combined changes'})[candidate.state] || 'Candidate state unavailable';
            row.querySelector('[data-candidate-revisions]').textContent = `Base: ${candidate.base_revision} · Candidate: ${candidate.result_revision || 'Not ready'}`;
            const details = (snapshot.candidate_details || []).find(item => (item.candidate_id || item.id) === candidate.id);
            const matchingDetails = details?.base_revision === candidate.base_revision && details?.result_revision === candidate.result_revision;
            const truncated = details?.diff_truncated || details?.changed_paths_truncated;
            const inspectionErrors = Array.isArray(details?.errors) ? details.errors : [];
            row._diffReady = Boolean(details?.state === 'ready' && matchingDetails && typeof details.diff === 'string'
                && Array.isArray(details.changed_paths) && Array.isArray(details.errors) && !inspectionErrors.length
                && details.diff_truncated === false && details.changed_paths_truncated === false);
            row._inspecting = details?.state === 'loading';
            row.querySelector('[data-candidate-files]').textContent = `Files: ${(details?.changed_paths || []).join(', ') || 'Details not loaded'}`;
            row.querySelector('[data-candidate-diff]').textContent = details?.diff || '';
            row.querySelector('[data-candidate-limit]').textContent = details?.state === 'loading' ? 'Loading the exact candidate diff…'
                : details?.state === 'failed' ? `Inspection failed: ${details.error || 'Refresh and inspect again.'}`
                : inspectionErrors.length ? `Inspection needs review: ${inspectionErrors.join(' · ')}`
                : truncated ? 'The preview is truncated. Applying is unavailable because this preview cannot show the complete retained changes.'
                : row._diffReady ? 'This complete diff belongs to the candidate revision shown above.' : 'Inspect the exact candidate diff before application.';
            let manifest = {};
            try { manifest = JSON.parse(candidate.manifest_json); } catch (_) { /* Keep controls closed for unreadable contracts. */ }
            for (const check of manifest.checks || []) {
                let checkNode = row._checks.get(check.key);
                if (!checkNode) {
                    checkNode = document.createElement('div');
                    checkNode.className = 'swarm-candidate-check';
                    checkNode.innerHTML = '<h5></h5><p data-check-command class="swarm-help"></p><p data-check-status></p><details><summary>Check output</summary><pre></pre></details><button type="button">Run check</button>';
                    checkNode.querySelector('button').addEventListener('click', () => this.requestSwarm('run_check', {candidate_id: candidate.id, check_key: check.key}));
                    row._checks.set(check.key, checkNode);
                    row.querySelector('[data-candidate-checks]').appendChild(checkNode);
                }
                const receipt = (snapshot.integration_checks || []).filter(item => item.candidate_id === candidate.id && item.check_key === check.key).at(-1);
                checkNode.querySelector('h5').textContent = check.key;
                checkNode.querySelector('button').textContent = `Run ${check.key}`;
                checkNode.querySelector('[data-check-command]').textContent = `Arguments: ${JSON.stringify(check.argv)} · Timeout: ${check.timeout_seconds}s`;
                checkNode.querySelector('[data-check-status]').textContent = receipt ? `Observed: ${receipt.state} · Exit status: ${receipt.exit_code ?? 'Unknown'}${receipt.candidate_revision !== candidate.result_revision ? ' · Evidence is for an older revision' : ''}` : 'No executed check recorded.';
                checkNode.querySelector('pre').textContent = receipt?.output || 'No output recorded.';
                checkNode._receiptState = receipt?.state;
            }
            row.querySelector('[data-candidate-apply]').hidden = candidate.state === 'applied' || candidate.state === 'superseded';
            for (const source of manifest.writers || []) {
                const attempt = attempts.get(source.attempt_id);
                const item = work.get(attempt?.work_item_id);
                let form = row._acceptances.get(source.attempt_id);
                if (!form) {
                    form = document.createElement('form');
                    form.className = 'swarm-review';
                    form.innerHTML = '<h5></h5><label>Acceptance notes<textarea rows="2" required maxlength="8000" placeholder="Explain how the applied changes and recorded checks satisfy this task."></textarea></label><button type="submit">Accept applied result</button>';
                    form.addEventListener('submit', event => {
                        event.preventDefault();
                        const evidence = form.querySelector('textarea').value.trim();
                        if (form.reportValidity() && evidence && form._target) this.requestSwarm('accept_writer', {...form._target, evidence});
                    });
                    row._acceptances.set(source.attempt_id, form);
                    row.querySelector('[data-candidate-acceptances]').appendChild(form);
                }
                form._target = {attempt_id: source.attempt_id, attempt_epoch: attempt?.epoch, candidate_id: candidate.id};
                form.setAttribute('aria-label', `Accept applied result: ${item?.objective || 'Unavailable task'}`);
                form.querySelector('h5').textContent = item?.objective || 'Unavailable task';
                form.hidden = candidate.state !== 'applied' || item?.state === 'accepted';
            }
        }
        this._renderSwarmKeptWork(snapshot.kept_work);
    }

    /** A size in bytes as people read it: "840 MB". */
    _swarmBytes(bytes) {
        const units = ['bytes', 'KB', 'MB', 'GB', 'TB'];
        let value = Number(bytes) || 0, unit = 0;
        while (value >= 1024 && unit < units.length - 1) { value /= 1024; unit++; }
        return unit ? `${value < 10 ? value.toFixed(1) : Math.round(value)} ${units[unit]}` : `${value} bytes`;
    }

    /** How much disk kept folders take, measured in the background (cleanup.FolderSizes). */
    _swarmKeptSize(kept, owner) {
        if (kept?.size_pending) return kept.size ? ` ${owner} take at least ${this._swarmBytes(kept.size)} (still measuring).` : ` Measuring how much space ${owner.toLowerCase()} take…`;
        return kept?.size ? ` ${owner} take ${this._swarmBytes(kept.size)}.` : '';
    }

    /** What an ended team keeps in the repository until the person discards it (engine/swarming/cleanup.py). */
    _renderSwarmKeptWork(kept) {
        const nodes = this._swarmNodes;
        const items = kept?.items || [];
        const applied = kept?.applied || [];
        const left = kept?.left || [];
        // Shown once Stop is asked for (what Stop keeps), with Discard once the team has ended.
        const run = this._swarmState?.run?.run;
        const stopping = !kept?.ended && Boolean(run?.stop_requested || run?.state === 'stopping');
        nodes['kept-work'].hidden = !(kept?.ended || stopping) || !(items.length || applied.length || left.length || kept?.problem);
        if (nodes['kept-work'].hidden) return;
        const plural = (count, one, many) => `${count} ${count === 1 ? one : many}`;
        const writers = items.filter(item => item.kind === 'writer').length;
        const parts = [writers && plural(writers, 'writer branch', 'writer branches'),
            items.length - writers && plural(items.length - writers, 'combined candidate', 'combined candidates')].filter(Boolean);
        const inspectable = applied.length ? ` The combined candidate${applied.length === 1 ? ' you applied stays' : 's you applied stay'} in Lumi’s folder so you can inspect ${applied.length === 1 ? 'it' : 'them'}, until you discard ${applied.length === 1 ? 'it' : 'them'}.` : '';
        nodes['kept-summary'].textContent = (!items.length ? (applied.length ? 'Everything this team changed that you applied is in your project.' : 'Nothing else this team kept is waiting to be discarded.')
            : stopping ? `Stopping keeps this team’s changes nobody applied: ${parts.join(' and ')}. They stay in your repository and Lumi’s folder until you discard them, once the team has stopped.`
            : `This team ended with changes nobody applied: ${parts.join(' and ')}. They stay in your repository and Lumi’s folder until you discard them.`)
            + inspectable + this._swarmKeptSize(kept, 'Its folders');
        const size = item => (Number.isInteger(item.size) ? ` · ${this._swarmBytes(item.size)}` : '');
        nodes['kept-items'].replaceChildren(...items.map(item => {
            const row = document.createElement('li');
            row.textContent = (item.kind === 'writer'
                ? `${item.branch || 'Writer worktree'} · ${item.changed ? 'committed changes nobody applied' : 'may hold edits nobody committed'}`
                : `Combined candidate · ${this._swarmStateLabel(item.state)}`) + size(item);
            return row;
        }), ...applied.map(item => {
            const row = document.createElement('li');
            row.textContent = `Applied combined candidate · kept so you can inspect it${size(item)}`;
            return row;
        }));
        // A branch Lumi won't delete stays, with the folder left with it until
        // the person removes that folder here: Lumi removes it without following
        // links out of it, which deleting it by hand might not.
        const reasons = {moved: 'it has commits the team didn’t make', in_use: 'it is checked out, or being rebased or bisected',
            another_repository: 'it belongs to another repository now'};
        const shown = new Set();
        for (const item of left) {
            const key = item.writer_id || item.branch;
            shown.add(key);
            let row = this._swarmLeftRows.get(key);
            if (!row) {
                row = document.createElement('li');
                row.append(document.createElement('span'), ' ');
                const button = document.createElement('button');
                button.type = 'button';
                button.dataset.leftRemove = '';
                button.textContent = 'Remove folder';
                button.addEventListener('click', () => {
                    const target = row._leftTarget;
                    if (!target?.worktree) return;
                    if (!window.confirm(`Remove ${target.worktree}?\n\nThe branch ${target.branch} and its commits stay in your repository. Changes not committed in that folder are lost.`)) return;
                    this.requestSwarm('remove_left_worktree', {writer_id: target.writer_id});
                });
                row.append(button);
                this._swarmLeftRows.set(key, row);
                nodes['kept-left'].appendChild(row);
            }
            row._leftTarget = item;
            row.firstChild.textContent = `Kept for you: ${item.branch} (${reasons[item.reason] || 'Lumi left it'})` + (item.worktree
                ? `, with its worktree ${item.worktree}${size(item)}. Remove the folder here when you no longer need it; the branch and its commits stay.`
                : item.folder_removed ? '. Its folder was removed; the branch and its commits stay in your repository.'
                : '. The branch and its commits stay in your repository.');
            const button = row.querySelector('[data-left-remove]');
            button.hidden = !item.worktree;
            button.setAttribute('aria-label', `Remove the folder kept with ${item.branch}`);
            button.title = item.worktree ? `Remove ${item.worktree}` : '';
        }
        for (const [key, row] of this._swarmLeftRows) if (!shown.has(key)) { row.remove(); this._swarmLeftRows.delete(key); }
        nodes['kept-left'].hidden = !left.length;
        nodes['kept-problem'].textContent = kept.problem ? `Cleanup couldn’t finish: ${kept.problem}` : '';
        nodes['discard-form'].hidden = !(items.length || applied.length) || stopping;
    }

    /** What every ended team of this conversation keeps, and Discard all kept work (cleanup.kept_across). */
    _renderSwarmKeptEverywhere(across) {
        const nodes = this._swarmNodes;
        const teams = across?.teams || 0;
        const folders = (across?.items || 0) + (across?.applied || 0);
        nodes['kept-everywhere'].hidden = nodes['discard-all'].hidden = !teams;
        nodes['kept-everywhere'].textContent = teams ? `${teams} ended team${teams === 1 ? '' : 's'} in this conversation keep${teams === 1 ? 's' : ''} ${folders} worktree${folders === 1 ? '' : 's'} in Lumi’s folder.`
            + this._swarmKeptSize(across, 'They') : '';
    }

    _swarmRecoveryBlockers(snapshot) {
        const blockers = [];
        if ((snapshot.attempts || []).some(row => row.process_state !== 'stopped')
            || (snapshot.process_observations || []).some(row => row.state !== 'stopped')) blockers.push('Process termination is not fully recorded.');
        if ((snapshot.model_requests || []).some(row => ['reserved', 'started', 'uncertain'].includes(row.state))) blockers.push('Request accounting needs reconciliation.');
        if ((snapshot.action_receipts || []).some(row => row.state !== 'completed')) blockers.push('Tool outcomes need reconciliation.');
        if ((snapshot.reservations || []).some(row => row.state !== 'settled')) blockers.push('An assignment allowance remains held.');
        if ((snapshot.integration_operations || []).some(row => ['queued', 'running', 'uncertain'].includes(row.state))) blockers.push('File integration operations need reconciliation.');
        for (const [name, states] of [['writer_worktrees', ['creating', 'finalizing', 'uncertain']], ['integration_candidates', ['preparing', 'uncertain']],
            ['integration_checks', ['running', 'uncertain']], ['integration_applications', ['applying', 'uncertain']]]) {
            if ((snapshot[name] || []).some(row => states.includes(row.state))) blockers.push('Retained file changes or checks require host inspection.');
        }
        if (this._swarmExecutionMode === 'managed' && !snapshot.managed_recovery?.resume_ready) blockers.push('Organization recovery needs retained proof and cleanup acknowledgements.');
        if (snapshot.managed_recovery?.operation?.state === 'running') blockers.push('An organization observation is being reconciled.');
        return [...new Set(blockers)];
    }

    _renderManagedRecovery(snapshot) {
        const nodes = this._swarmNodes;
        const managed = snapshot.managed_recovery;
        nodes['managed-recovery'].hidden = !managed;
        if (!managed) return;
        const epochs = (managed.epochs || []).map(row => String(row.epoch));
        if ([...nodes['managed-epoch'].options].map(option => option.value).join(',') !== epochs.join(',')) {
            nodes['managed-epoch'].replaceChildren(...epochs.map(epoch => new Option(`Epoch ${epoch}`, epoch)));
        }
        nodes['managed-epoch'].value = String(managed.history_epoch);
        nodes['managed-kind'].value = managed.kind || 'requests';
        const operation = managed.operation;
        nodes['managed-recovery-status'].textContent = `${managed.worker_cleanup_pending || 0} worker cleanup acknowledgement(s) pending. ${managed.effect_cleanup_pending || 0} owner-process cleanup acknowledgement(s) pending. ${managed.unknown_request_units || 0} request unit(s) held or awaiting confirmation.`
            + (operation?.state === 'running' ? ' Reconciling in the background; Stop remains available.' : operation?.error ? ` ${operation.error}` : operation?.delivery?.unavailable ? ' Some observations could not be delivered. Retry when the service is available.' : '')
            + (managed.missing_effect_epochs?.length ? ' Owner-process history is missing; continuation remains blocked.' : '');
        const selected = managed.kind || 'requests';
        const records = (managed.records || []).map(record => ({...record, type: selected}));
        for (const epoch of managed.owner_effects || []) {
            for (const record of epoch.effects || []) records.push({...record, epoch: epoch.epoch, local_id: record.process_id, type: 'effects'});
        }
        const kept = new Set();
        for (const record of records) {
            const key = `${record.epoch}:${record.type}:${record.local_id}`;
            kept.add(key);
            let row = this._swarmManagedRows.get(key);
            if (!row) {
                row = document.createElement('article');
                row.className = 'swarm-recovery-record';
                row.innerHTML = '<h5></h5><p data-managed-id class="swarm-help"></p><p data-managed-proof class="swarm-help"></p><div class="swarm-controls"><button type="button">Derive and report retained observation</button><button type="button" data-managed-fence>Check and fence missing admission</button></div>';
                row.querySelector('button').addEventListener('click', () => {
                    const target = row._target;
                    const action = {requests: ['request', 'model_request_id'], workers: ['worker', 'attempt_id'], actions: ['action', 'action_id'], effects: ['effect', 'process_id']}[target.type];
                    if (action) this.requestSwarm(`managed_reconcile_${action[0]}`, {managed_epoch: target.epoch, [action[1]]: target.local_id});
                });
                row.querySelector('[data-managed-fence]').addEventListener('click', () => {
                    const target = row._target;
                    this.requestSwarm('managed_fence_absent', {managed_epoch: target.epoch, kind: target.type, local_id: target.local_id});
                });
                this._swarmManagedRows.set(key, row);
                nodes['managed-records'].appendChild(row);
            }
            row._target = record;
            row.querySelector('h5').textContent = `Epoch ${record.epoch} · ${record.type} · ${record.outcome || record.phase}`;
            row.querySelector('[data-managed-id]').textContent = `Local: ${record.local_id} · Organization: ${record.remote_id || 'Unavailable'}`;
            row.querySelector('[data-managed-proof]').textContent = [record.native_state && `Native outcome: ${record.native_state}`, record.native_process_state && `Process: ${record.native_process_state}`,
                record.identity_matches === false && 'Receipt identity does not match.', record.has_recovery_evidence && 'Retained reconciliation evidence is recorded.', record.type === 'controls' && 'Remote control uncertainty is retained; this control will not be replayed.'].filter(Boolean).join(' ');
            row.querySelector('button').hidden = record.type === 'controls';
            row.querySelector('[data-managed-fence]').hidden = !record.can_fence_absent;
        }
        for (const [key, row] of this._swarmManagedRows) if (!kept.has(key)) { row.remove(); this._swarmManagedRows.delete(key); }
    }

    _renderSwarmRecovery(snapshot) {
        const nodes = this._swarmNodes;
        nodes['recovery-section'].hidden = !snapshot.recovery_needed;
        if (!snapshot.recovery_needed) return;
        this._renderManagedRecovery(snapshot);
        const recovery = snapshot.recovery;
        const leaseWait = this._swarmLeaseWait(snapshot.run);
        nodes['recovery-status'].textContent = recovery?.owns_lease
            ? 'This host owns recovery. Check execution, then reconcile each uncertain observation.'
            : leaseWait > 0 ? `The previous supervisor lease expires in about ${leaseWait} seconds. This refreshes automatically; the server decides when takeover is available.`
                : 'Take over after the previous supervisor lease expires. The server checks whether ownership is available.';
        if (recovery?.renewal_error) nodes['recovery-status'].textContent = 'Recovery ownership needs inspection. Refresh before making further decisions. ' + String(recovery.renewal_error).slice(0, 2000);
        const attempts = snapshot.attempts || [];
        const labels = new Map(attempts.filter(row => row.kind !== 'coordinator').map((row, index) => [row.id, `Worker ${index + 1}`]));
        const label = id => attempts.find(row => row.id === id)?.kind === 'coordinator' ? 'Coordinator' : (labels.get(id) || 'Retained worker');
        const kept = new Set();
        for (const attempt of attempts) {
            const key = 'process:' + attempt.id;
            kept.add(key);
            let row = this._swarmRecoveryRows.get(key);
            if (!row) {
                row = document.createElement('article');
                row.className = 'swarm-recovery-record';
                row.innerHTML = '<h4></h4><p data-process-observation></p><p data-process-detail class="swarm-help"></p><div class="swarm-controls"><button type="button" data-process-check>Check process</button><button type="button" data-process-record>Record host observation</button></div>';
                row.querySelector('[data-process-check]').addEventListener('click', () => this.requestSwarm('inspect_process', {attempt_id: attempt.id}));
                row.querySelector('[data-process-record]').addEventListener('click', () => this.requestSwarm('reconcile_process', {attempt_id: attempt.id}));
                this._swarmRecoveryRows.set(key, row);
                nodes['recovery-records'].appendChild(row);
            }
            row.querySelector('h4').textContent = `${label(attempt.id)} execution`;
            const observation = (snapshot.recovery_observations || []).find(item => item.attempt_id === attempt.id);
            row.querySelector('[data-process-observation]').textContent = attempt.process_state === 'stopped' ? 'Termination recorded.'
                : observation?.observation === 'stopped' ? 'The host observed that the captured process stopped. Record that observation before continuing.'
                : observation?.observation === 'running' ? 'The captured process is still running. Termination is not confirmed.' : 'Process termination is unknown. Check the captured process on its original host.';
            row.querySelector('[data-process-detail]').textContent = [observation?.reason, observation?.next_step].filter(Boolean).join(' ');
            row._canRecord = attempt.process_state !== 'stopped' && observation?.observation === 'stopped';
        }
        for (const [kind, records] of [['request', snapshot.model_requests || []], ['action', snapshot.action_receipts || []]]) {
            for (const record of records.filter(item => item.state === 'uncertain')) {
                const key = `${kind}:${record.id}`;
                kept.add(key);
                let row = this._swarmRecoveryRows.get(key);
                if (!row) {
                    row = document.createElement('form');
                    row.className = 'swarm-recovery-record';
                    row.innerHTML = `<h4></h4><p data-record-identity class="swarm-help"></p><p class="swarm-help">${kind === 'request'
                        ? 'Use provider records or other attributable observations. A stopped process does not prove that its request was unused.'
                        : 'Inspect retained outputs and actual effects. Model claims and process exit alone do not establish the tool outcome.'}</p>
                        <label>Observed outcome<select required><option value="">Choose an observed outcome</option><option value="completed">Completed${kind === 'request' ? ' — count one request' : ''}</option><option value="failed">Failed${kind === 'request' ? ' — count one request' : ''}</option><option value="not_started">Did not start${kind === 'request' ? ' — count zero requests' : ''}</option></select></label>
                        <label>Observation evidence<textarea rows="3" maxlength="8000" required placeholder="Identify the evidence you inspected and how it establishes this exact outcome."></textarea></label>
                        <p data-record-wait class="swarm-help"></p><button type="submit">Record ${kind === 'request' ? 'request accounting' : 'tool outcome'}</button>`;
                    row.addEventListener('submit', event => {
                        event.preventDefault();
                        const outcome = row.querySelector('select').value;
                        const evidence = row.querySelector('textarea').value.trim();
                        if (!row.reportValidity() || !outcome || !evidence) return;
                        const payload = kind === 'request' ? {model_request_id: record.id, used: outcome === 'not_started' ? 0 : 1} : {action_id: record.id};
                        this.requestSwarm(`reconcile_${kind}`, {...payload, outcome, evidence});
                    });
                    this._swarmRecoveryRows.set(key, row);
                    nodes['recovery-records'].appendChild(row);
                }
                const title = `${label(record.attempt_id)} ${kind === 'request' ? 'request accounting' : 'tool outcome'}`;
                row.setAttribute('aria-label', title);
                row.querySelector('h4').textContent = title;
                row.querySelector('[data-record-identity]').textContent = kind === 'request' ? `Request: ${record.id} · ${record.purpose === 'auxiliary' ? 'Planning or compression' : 'Main model request'}` : `Tool: ${record.tool_name || 'Unknown'} · Action: ${record.id}`;
                row._canReconcile = attempts.find(attempt => attempt.id === record.attempt_id)?.process_state === 'stopped';
                row.querySelector('[data-record-wait]').textContent = row._canReconcile ? '' : 'Record process termination before settling this observation.';
            }
        }
        const retainedEffects = [
            ...['writer', 'candidate', 'check'].flatMap(kind => {
                const table = {writer: 'writer_worktrees', candidate: 'integration_candidates', check: 'integration_checks'}[kind];
                return (snapshot[table] || []).filter(item => item.state === 'uncertain').map(item => ({
                    key: `effect:${kind}:${item.id}`, title: `Interrupted ${kind === 'writer' ? 'writer checkout' : kind === 'candidate' ? 'combined changes' : 'verification check'}`,
                    identity: item.id, action: 'reconcile_effect', payload: {effect_kind: kind, effect_id: item.id},
                    explanation: 'The host inspects retained process ownership. Confirmed termination can close interrupted work as failed or cancelled; it cannot establish a passed check. Missing or live process evidence keeps the outcome unresolved.',
                }));
            }),
            ...(snapshot.integration_operations || []).filter(item => item.state === 'uncertain').map(item => ({
                key: `operation:${item.id}`, title: 'Interrupted integration operation', identity: item.id,
                action: 'reconcile_operation', payload: {operation_id: item.id},
                explanation: 'The host reconciles this operation from its exact retained effect and process records. Your notes cannot override missing evidence or choose an outcome.',
            })),
            ...(snapshot.integration_applications || []).filter(item => item.state === 'uncertain').map(item => ({
                key: `application:${item.id}`, title: 'Interrupted branch application', identity: item.id,
                action: 'reconcile_application', payload: {approval_id: item.id},
                explanation: 'The host checks the captured branch and exact before/after revisions. This does not replay Git commands or accept a task.',
            })),
        ];
        for (const effect of retainedEffects) {
            kept.add(effect.key);
            let row = this._swarmRecoveryRows.get(effect.key);
            if (!row) {
                row = document.createElement('form');
                row.className = 'swarm-recovery-record';
                row.innerHTML = '<h4></h4><p data-effect-id class="swarm-help"></p><p data-effect-help class="swarm-help"></p><label>Recovery notes<textarea rows="2" required maxlength="8000" placeholder="Record why you are inspecting this retained operation."></textarea></label><button type="submit">Inspect and reconcile</button>';
                row.addEventListener('submit', event => {
                    event.preventDefault();
                    const evidence = row.querySelector('textarea').value.trim();
                    if (row.reportValidity() && evidence) this.requestSwarm(row._effect.action, {...row._effect.payload, evidence});
                });
                this._swarmRecoveryRows.set(effect.key, row);
                nodes['recovery-records'].appendChild(row);
            }
            row._effect = effect;
            row._canReconcile = true;
            row.setAttribute('aria-label', `${effect.title}: ${effect.identity}`);
            row.querySelector('h4').textContent = effect.title;
            row.querySelector('[data-effect-id]').textContent = effect.identity;
            row.querySelector('[data-effect-help]').textContent = effect.explanation;
        }
        for (const [key, row] of this._swarmRecoveryRows) {
            if (!kept.has(key)) { row.remove(); this._swarmRecoveryRows.delete(key); }
        }
        const retryable = (snapshot.work_items || []).filter(item => ['failed', 'cancelled'].includes(item.state));
        const retryIds = new Set(retryable.map(item => item.id));
        for (const [id, row] of this._swarmRetryRows) {
            if (!retryIds.has(id)) { row.remove(); this._swarmRetryRows.delete(id); }
        }
        for (const item of retryable) {
            let row = this._swarmRetryRows.get(item.id);
            if (!row) {
                row = document.createElement('label');
                row.className = 'swarm-retry-option';
                row.append(document.createElement('input'), document.createElement('span'));
                row.querySelector('input').type = 'checkbox';
                this._swarmRetryRows.set(item.id, row);
                nodes['retry-items'].appendChild(row);
            }
            row.querySelector('span').textContent = `Retry: ${item.objective}`;
        }
        const ready = (snapshot.work_items || []).filter(item => item.state === 'ready');
        nodes['ready-work'].textContent = ready.length ? `Already ready to run: ${ready.map(item => item.objective).join('; ')}` : 'No investigations are currently ready to run.';
        nodes['recovery-blockers'].textContent = this._swarmRecoveryBlockers(snapshot).join(' ') || 'All recorded execution and accounting is settled. The server will recheck before continuation.';
    }

    _renderSwarmInbox(snapshot) {
        const messages = [];
        // An orchestrator the owner let run the team takes their plan, result and
        // retry decisions (autopilot.py) until it hands the team back to them.
        const autonomy = this._swarmState?.autonomy;
        const orchestrated = Boolean(autonomy?.active) && autonomy.phase !== 'needs_owner';
        if (autonomy?.active && autonomy.phase === 'needs_owner') messages.push(`The orchestrator handed the team back: ${String(autonomy.detail || '').slice(0, 2000)}`);
        const plans = (snapshot?.coordinator_proposals || []).filter(row => row.state === 'pending').length;
        if (plans && !orchestrated) messages.push(`${plans} proposed plan${plans === 1 ? ' needs' : 's need'} your review before its investigations can start.`);
        const scheduling = snapshot?.scheduling;
        if (scheduling?.error) messages.push(`Scheduling needs attention: ${String(scheduling.error).slice(0, 2000)}`);
        const blocked = Object.values(scheduling?.blocked || {});
        if (blocked.length) messages.push(`${blocked.length} investigation${blocked.length === 1 ? ' is' : 's are'} waiting: ${[...new Set(blocked)].join('; ').slice(0, 2000)}`);
        if (snapshot?.recovery_needed || snapshot?.run?.state === 'recovery_required') messages.push('Execution was interrupted. Take over the expired team, check captured processes, and reconcile uncertain outcomes before continuing.');
        const unknown = (snapshot?.model_requests || []).filter(row => row.state === 'uncertain').length;
        if (unknown) messages.push(`${unknown} model request${unknown === 1 ? ' needs' : 's need'} reconciliation. Usage is still uncertain.`);
        const unknownActions = (snapshot?.action_receipts || []).filter(row => row.state === 'uncertain').length;
        if (unknownActions) messages.push(`${unknownActions} tool action${unknownActions === 1 ? ' needs' : 's need'} reconciliation. Its effects are not confirmed.`);
        const pending = (snapshot?.work_items || []).filter(row => row.state === 'submitted').length;
        if (pending && !orchestrated) messages.push(`${pending} investigation${pending === 1 ? ' has' : 's have'} submitted findings awaiting independent verification.`);
        const failed = (snapshot?.work_items || []).filter(row => ['failed', 'uncertain'].includes(row.state)).length;
        if (failed && !orchestrated) messages.push(`${failed} investigation${failed === 1 ? ' needs' : 's need'} review before another attempt.`);
        const kept = snapshot?.kept_work;
        if (kept?.ended && kept.items?.length) messages.push('Changes nobody applied are kept in your repository. Discard them under Review file changes when you no longer need them.');
        const across = this._swarmState?.kept_everywhere;
        if (across?.teams > 1 || (across?.teams && !(kept?.ended && kept.items?.length))) messages.push(`Ended teams in this conversation keep worktrees in Lumi’s folder.${this._swarmKeptSize(across, 'They')} Discard all kept work is under Saved teams.`);
        if (!messages.length) messages.push(orchestrated ? 'The orchestrator is making the team’s decisions under your grant. You’ll see here if it hands the team back.'
            : 'No decisions need your attention.');
        this._swarmNodes.inbox.replaceChildren(...messages.map(text => { const item = document.createElement('li'); item.textContent = text; return item; }));
    }

    _renderSwarmControls() {
        if (!this._swarmDialog) return;
        const nodes = this._swarmNodes;
        const online = Boolean(this.ws && this.ws.readyState === WebSocket.OPEN);
        const busy = Boolean(this._swarmSelectingRun || this._swarmStopRefreshVisible()
            || (this._swarmPending && !this._swarmIsRead(this._swarmPending.action)));
        const available = Boolean(this._swarmState?.available);
        const run = this._swarmState?.run?.run;
        const recoveryNeeded = Boolean(this._swarmState?.run?.recovery_needed || run?.state === 'recovery_required');
        const recoveryOwned = Boolean(this._swarmState?.run?.recovery?.owns_lease);
        const work = this._swarmState?.run?.work_items || [];
        const planning = this._swarmState?.coordinator_planning;
        nodes['followup-form'].hidden = !run || !planning || Boolean(this._swarmState?.autonomy?.active);
        if (run && planning && this._swarmFollowupRun !== run.id) {
            this._swarmFollowupRun = run.id;
            const draft = this._swarmFollowupDrafts.get(run.id) || {requests: String(planning.default_requests || 1), roots: (planning.read_roots || []).join(', ')};
            this._swarmFollowupDrafts.set(run.id, draft);
            nodes['followup-requests'].value = draft.requests;
            nodes['followup-roots'].value = draft.roots;
        }
        nodes['followup-requests'].max = String(Math.max(1, Math.min(1000, planning?.remaining_requests ?? 1000)));
        nodes['followup-status'].textContent = planning?.available
            ? `${planning.remaining_requests} requests available · ${planning.worker_requests} requests per additional worker. Requesting a plan consumes coordinator allowance.`
            : planning?.reason || 'Follow-up planning is unavailable.';
        nodes['request-plan'].disabled = !online || busy || !planning?.available;
        for (const name of ['followup-requests', 'followup-roots']) nodes[name].disabled = !online || busy || !planning?.available;
        // Startup/discovery events can spell the same Windows path with either
        // separator. Ownership remains server-validated; this only gates setup.
        const projectKey = path => {
            const value = String(path || '').replace(/\\/g, '/').replace(/\/+$/, '');
            return /^(?:[a-z]:\/|\/\/)/i.test(value) ? value.toLowerCase() : value;
        };
        const selectedElsewhere = projectKey(this.currentCwd) !== projectKey(this._swarmScope.project)
            || this.currentSessionId !== this._swarmScope.session_id;
        nodes.scope.textContent = `${this._swarmScope.project || 'No project selected'} · ${selectedElsewhere ? 'Viewing the originally opened conversation; controls stay with this team' : (this._swarmScope.session_id ? 'Saved conversation' : 'Save a conversation to start a team')}`;
        nodes.connection.textContent = online ? 'Connected' : 'Reconnecting…';
        nodes.form.hidden = Boolean(run);
        nodes['previous-team'].hidden = !this._swarmPlanningNew || !this._swarmPreviousRunId;
        nodes['previous-team'].disabled = !online || busy;
        nodes['history-open'].disabled = !online || busy || !nodes['history-select'].value;
        nodes['history-select'].disabled = !online || busy;
        nodes['history-newer'].disabled = !online || busy || this._swarmHistoryPage <= 0;
        nodes['history-older'].disabled = !online || busy || !this._swarmHistory?.next_before_run_id;
        nodes['history-refresh'].disabled = !online || busy;
        nodes['artifact-next'].disabled = !online || busy;
        nodes['artifact-previous'].disabled = !online || busy;
        nodes.enabled.disabled = !online || busy || !available;
        nodes.start.disabled = !online || busy || !available || !this._swarmState?.enabled || Boolean(run) || selectedElsewhere;
        nodes['execution-mode'].disabled = !online || busy || Boolean(run) || selectedElsewhere;
        // Writer teams need Git (_swarmRenumberTasks says so beside the switch).
        nodes['allow-writes'].disabled = !online || busy || Boolean(run) || Boolean(this._gitMissingCopy?.());
        nodes.pause.hidden = !run || recoveryNeeded || run.state !== 'running';
        nodes.resume.hidden = !run || recoveryNeeded || run.state !== 'paused';
        nodes.stop.hidden = !run || (recoveryNeeded && !recoveryOwned) || Boolean(run.stop_requested) || ['completed', 'cancelled', 'failed', 'stopping'].includes(run.state);
        nodes.recover.hidden = !run || !recoveryNeeded || recoveryOwned;
        nodes.complete.hidden = !run || recoveryNeeded || ['completed', 'cancelled', 'failed', 'stopping'].includes(run.state)
            || !work.length || work.some(item => item.state !== 'accepted');
        nodes.complete.disabled = !online || busy || run?.state !== 'running';
        nodes['new-team'].hidden = !run || !['completed', 'cancelled', 'failed'].includes(run.state);
        nodes['export-report'].hidden = !run;
        nodes['export-report'].disabled = !online || busy;
        // Only this conversation's own personal teams can be attached (gui/swarming.py).
        nodes['use-in-chat'].hidden = !run || this._swarmExecutionMode === 'managed';
        const concurrencyDisabled = !online || busy || recoveryNeeded || !['running', 'pausing', 'paused'].includes(run?.state)
            || !this._swarmConcurrencyCap;
        nodes['worker-limit'].disabled = concurrencyDisabled;
        nodes['apply-worker-limit'].disabled = concurrencyDisabled || Number(nodes['worker-limit'].value) === run?.worker_limit;
        nodes['new-team'].disabled = !online || busy || selectedElsewhere;
        for (const action of ['pause', 'resume', 'stop', 'recover']) nodes[action].disabled = !online || busy;
        if (this._swarmPending?.action === 'request_plan' && !this._swarmSelectingRun) nodes.stop.disabled = !online;
        nodes.recover.disabled ||= this._swarmLeaseWait(run) > 0;
        for (const node of this._swarmWorkers.values()) {
            node.querySelector('[data-evidence-read]').disabled = !online || busy;
            const form = node.querySelector('form');
            for (const button of node.querySelectorAll('.swarm-review button')) button.disabled = !online || busy || run?.state !== 'running' || recoveryNeeded;
            form.querySelector('[data-review-paused]').hidden = run?.state !== 'paused';
            const closed = !online || busy || recoveryNeeded || !node._controlActive || node._cancelRequested
                || Boolean(run?.stop_requested) || !['running', 'pausing', 'paused'].includes(run?.state);
            const pause = node.querySelector('[data-worker-action="pause_worker"]');
            const resume = node.querySelector('[data-worker-action="resume_worker"]');
            pause.hidden = node._pauseRequested;
            resume.hidden = !node._pauseRequested;
            pause.disabled = closed;
            resume.disabled = closed || run?.state !== 'running';
            node.querySelector('[data-worker-action="cancel_worker"]').disabled = closed;
            node.querySelector('[data-participant-guidance] button').disabled = closed;
            node.querySelector('[data-guidance-text]').disabled = closed && (!online || recoveryNeeded || !node._controlActive || node._cancelRequested || Boolean(run?.stop_requested));
        }
        for (const node of this._swarmProposals.values()) {
            for (const button of node.querySelectorAll('button')) button.disabled = !online || busy || run?.state !== 'running'
                || recoveryNeeded || !node._proposalReady || !node._readableProposal;
            if (node._proposalReady) node.querySelector('[data-proposal-wait]').textContent = run?.state === 'paused' ? 'Resume the team before deciding on this plan.' : '';
        }
        const recoveryOperating = (this._swarmState?.run?.integration_operations || []).some(row =>
            ['reconcile_effect', 'reconcile_operation', 'reconcile_application'].includes(row.kind) && ['queued', 'running'].includes(row.state));
        const recoveryDisabled = !online || busy || !recoveryOwned || recoveryOperating;
        const managedDisabled = recoveryDisabled || this._swarmState?.run?.managed_recovery?.operation?.state === 'running';
        for (const name of ['managed-epoch', 'managed-kind', 'managed-first', 'managed-flush']) nodes[name].disabled = managedDisabled;
        nodes['managed-next'].disabled = managedDisabled || !this._swarmState?.run?.managed_recovery?.next_cursor;
        for (const row of this._swarmManagedRows.values()) for (const button of row.querySelectorAll('button')) button.disabled = managedDisabled;
        for (const [key, row] of this._swarmRecoveryRows) {
            if (key.startsWith('process:')) {
                row.querySelector('[data-process-check]').disabled = recoveryDisabled;
                row.querySelector('[data-process-record]').disabled = recoveryDisabled || !row._canRecord;
            } else row.querySelector('button').disabled = recoveryDisabled || !row._canReconcile;
        }
        nodes['continue-form'].hidden = !recoveryOwned;
        nodes.continue.disabled = recoveryDisabled || this._swarmRecoveryBlockers(this._swarmState?.run || {}).length > 0;
        nodes.continue.textContent = run?.stop_requested ? 'Finish stopped team' : 'Continue reviewed team';
        nodes['recovery-allowance'].disabled = Boolean(run?.stop_requested);
        for (const row of this._swarmRetryRows.values()) {
            row.querySelector('input').disabled = recoveryDisabled || Boolean(run?.stop_requested);
            if (run?.stop_requested) row.querySelector('input').checked = false;
        }
        const integrationBusy = (this._swarmState?.run?.integration_operations || []).some(row => ['queued', 'running', 'uncertain'].includes(row.state));
        const integrationDisabled = !online || busy || recoveryNeeded || run?.state !== 'running' || integrationBusy;
        let selectedWriters = 0;
        for (const row of this._swarmWriterRows.values()) {
            const input = row.querySelector('input');
            input.disabled = integrationDisabled || !row._eligible;
            if (input.checked && !input.disabled) selectedWriters++;
        }
        nodes['prepare-candidate'].disabled = integrationDisabled || !selectedWriters;
        const kept = this._swarmState?.run?.kept_work;
        const discardClosed = !online || busy || !kept?.ended || !((kept.items || []).length || (kept.applied || []).length);
        nodes['discard-confirm'].disabled = discardClosed;
        nodes.discard.disabled = discardClosed || !nodes['discard-confirm'].checked;
        for (const row of this._swarmLeftRows.values()) row.querySelector('[data-left-remove]').disabled = !online || busy || !kept?.ended;
        nodes['discard-all'].disabled = !online || busy || !this._swarmState?.kept_everywhere?.teams;
        for (const row of this._swarmCandidateRows.values()) {
            row.querySelector('[data-candidate-inspect]').disabled = !online || busy || row._inspecting || !row._candidateTarget?.target_revision;
            for (const check of row._checks.values()) check.querySelector('button').disabled = integrationDisabled || !['ready', 'failed', 'verified'].includes(row._candidateState);
            row.querySelector('[data-candidate-apply] button').disabled = integrationDisabled || row._candidateState !== 'verified' || !row._diffReady;
            for (const form of row._acceptances.values()) form.querySelector('button').disabled = integrationDisabled || row._candidateState !== 'applied';
        }
        nodes.refresh.disabled = !online || busy;
        this._renderCollaboration?.();
        this._renderManagedCollaboration?.();
    }

    _swarmLeaseWait(run) {
        return Number.isFinite(run?.lease_until) ? Math.max(0, Math.ceil(run.lease_until - Date.now() / 1000)) : 0;
    }

    swarmConnectionChanged(connected) {
        const stopUnsent = Boolean(this._swarmStopRefresh);
        if (!connected) this._swarmStopRefresh = null;
        if (!this._swarmDialog?.open) return;
        this._swarmPending = null;
        this._renderSwarmControls();
        if (connected) this.requestSwarm('view');
        else this._swarmNodes.notice.textContent = stopUnsent
            ? 'Connection lost before Stop was sent. Existing workers may still be running; refresh and send Stop after reconnecting.'
            : 'Reconnecting. Existing workers may still be running; refresh after the connection returns.';
    }

    _swarmStateLabel(state) {
        return ({running: 'Working', leased: 'Starting', pending: 'Waiting', ready: 'Ready', pausing: 'Pausing', paused: 'Paused', stopping: 'Stopping', stopped: 'Stopped',
            finalizing: 'Committing its changes', waiting_for_repository: 'Waiting for another step on this repository',
            submitted: 'Awaiting verification', accepted: 'Accepted', completed: 'Complete', cancelled: 'Stopped', failed: 'Needs review', uncertain: 'Needs reconciliation', reconciliation_required: 'Needs reconciliation', recovery_required: 'Recovery needed', unknown: 'Not confirmed'})[state] || 'Not confirmed';
    }

    /** Worker model choices: each Team-capable provider's models (native or a connection). */
    _swarmWorkerModels(session) {
        const select = this._swarmNodes['worker-model'];
        const choices = [];
        for (const [provider, info] of Object.entries(this.backends || {})) {
            if (!/^(ollama|exo|kimi|openrouter|sonn|conn-[a-z0-9][a-z0-9-]*)$/.test(provider)) continue;
            for (const model of info?.models || []) {
                if (session && provider === session.provider && model === session.model) continue;
                choices.push({provider, model, label: `${info.label || provider} · ${model}`});
            }
        }
        const key = JSON.stringify(choices);
        if (select.dataset.choices === key) return;
        const kept = select.value;
        select.dataset.choices = key;
        select.replaceChildren(new Option('Same as this session', ''),
            ...choices.map(choice => new Option(choice.label, JSON.stringify({provider: choice.provider, model: choice.model}))));
        select.value = [...select.options].some(option => option.value === kept) ? kept : '';
    }

    /** Add the selected team to the message as ``@team:<run>`` context (engine/context_broker.py); nothing is sent. */
    _swarmUseInChat() {
        const run = this._swarmState?.run?.run;
        if (!run?.id || !this.userInput) return;
        const mention = `@team:${run.id}`;
        const current = this.userInput.value;
        if (!current.split(/\s+/).includes(mention)) {
            const joiner = current && !/\s$/.test(current) ? ' ' : '';
            // The trailing space keeps the @-file picker from opening on the mention.
            this.userInput.value = `${current}${joiner}${mention} `;
            this.userInput.dispatchEvent(new Event('input', {bubbles: true}));
        }
        this._swarmReturnFocus = this.userInput;
        this._swarmDialog?.close();
        const end = this.userInput.value.length;
        this.userInput.setSelectionRange(end, end);
        this.showToastMessage?.('Added this team’s report and accepted results to your message. Send when you’re ready.');
    }

    /** What a coordinator attempt is for: an owner-reviewed proposal, an orchestrator's plan, or its answers. */
    _swarmTurnPurpose(attempt) {
        if (!this._swarmState?.autonomy) return 'Propose investigations for owner review';
        return String(attempt.worker_id || '').startsWith('orchestrator-answer-')
            ? 'Answer workers’ questions' : 'Plan the team’s work, or write its report';
    }

    _swarmCoordinatorName() {
        return this._swarmState?.autonomy ? 'Orchestrator' : 'Coordinator';
    }

    _renderSwarmOrchestrator(autonomy) {
        const nodes = this._swarmNodes;
        nodes.orchestrator.hidden = !autonomy;
        if (!autonomy) return;
        const turn = autonomy.closing ? 'Closing turn' : `Round ${autonomy.round} of ${autonomy.rounds}`;
        const applies = autonomy.apply ? ' · Applies checked changes' : '';
        nodes['orchestrator-status'].textContent = `${turn}${applies} · ${autonomy.detail || ''}`;
        const report = autonomy.final_report || '';
        nodes['orchestrator-report'].hidden = !report;
        // What Lumi recorded, beside what the model wrote: a live report claimed
        // questions nobody asked (chat_context.team_record).
        const record = this._swarmState?.team_record;
        const plural = (count, word) => `${count} ${word}${count === 1 ? '' : 's'}`;
        nodes['orchestrator-record'].textContent = record
            ? `Recorded by Lumi: ${record.accepted} of ${plural(record.tasks, 'task')} accepted, ${plural(record.questions, 'question')} to the orchestrator and ${plural(record.answers, 'answer')}, ${plural(record.applied, 'change')} applied. The report above is the orchestrator’s own words.`
            : '';
        if (report === nodes['orchestrator-report-text'].dataset.source) return;
        nodes['orchestrator-report-text'].dataset.source = report;
        // The report is model output: render its Markdown only through the
        // chat's sanitizer, and fall back to plain text without it.
        if (report && typeof marked !== 'undefined' && typeof DOMPurify !== 'undefined') {
            nodes['orchestrator-report-text'].innerHTML = this.sanitizeMarkdownHtml(marked.parse(report));
        } else {
            nodes['orchestrator-report-text'].textContent = report;
        }
    }

    _renderSwarmMessages(snapshot) {
        const nodes = this._swarmNodes;
        const messages = snapshot.messages || [];
        nodes['messages-section'].hidden = !messages.length;
        const attempts = snapshot.attempts || [];
        const numbers = new Map(attempts.filter(attempt => attempt.kind !== 'coordinator').map((attempt, index) => [attempt.id, index + 1]));
        const coordinators = new Set(attempts.filter(attempt => attempt.kind === 'coordinator').map(attempt => attempt.id));
        const name = id => coordinators.has(id) ? this._swarmCoordinatorName() : numbers.has(id) ? `Worker ${numbers.get(id)}` : 'Another participant';
        const kinds = {finding: 'Finding', question: 'Question', answer: 'Answer', blocker: 'Blocker',
            change_proposal: 'Change proposal', handoff_reference: 'Handoff'};
        nodes['messages-summary'].textContent = `Team messages (${messages.length})`;
        nodes.messages.replaceChildren(...messages.slice(-50).map(message => {
            const item = document.createElement('li');
            const heading = document.createElement('p');
            heading.className = 'swarm-message-heading';
            heading.textContent = `${name(message.sender_attempt_id)} → ${name(message.recipient_attempt_id)} · ${kinds[message.kind] || 'Message'}`;
            const body = document.createElement('p');
            body.className = 'swarm-message-body';
            body.textContent = message.body;
            item.append(heading, body);
            return item;
        }));
    }

    _swarmActivityLabel(kind) {
        return ({run_created: 'Team created', command_plan: 'Investigations planned', command_assign: 'Worker assigned', command_worker_started: 'Worker started', command_worker_stopped: 'Worker stopped',
            message_accepted: 'A finding or question was shared', message_context: 'A shared message was prepared for a worker', artifact_published: 'Evidence retained', artifact_shared: 'Evidence shared',
            command_pause: 'Pause requested', command_resume: 'Team resumed', command_stop: 'Stop requested', recovery_required: 'Recovery needed', command_recover: 'Recovery reviewed',
            command_submit: 'Findings submitted for review', command_review_read_result: 'Owner accepted findings', command_accept: 'Investigation accepted', command_complete: 'Team completed', command_set_concurrency: 'Worker assignment limit updated',
            command_accept_under_grant: 'The orchestrator accepted findings (not reviewed by you)', command_decide_proposal: 'A plan was decided',
            command_accept_writer: 'Owner accepted applied changes', command_accept_writer_under_grant: 'The orchestrator accepted applied changes (not reviewed by you)',
            command_reject: 'A result was sent back', command_retry: 'A task was retried', candidate_applied: 'Checked changes were applied to the project',
            candidate_check_observed: 'A check finished on combined changes',
            leftovers_removed: 'Worktrees and branches nothing needed were removed', kept_work_discarded: 'Kept work was discarded',
            left_worktree_removed: 'A folder kept with a branch was removed',
            coordinator_proposed: 'A plan was proposed', tool_refused: 'A worker’s call was refused: outside its assignment'})[kind] || 'Team state updated';
    }
};
