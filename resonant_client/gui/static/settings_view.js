/*
 * Settings and overlay surfaces for ResonantApp.
 *
 * Everything that opens *over* the main view: the settings page, the Ollama
 * setup wizard, the model picker, the project switcher, the shortcuts overlay,
 * and the status / harness / git / RESONANT.md popovers.
 *
 * Grouped by what they are, not by name. `_handleKeyboardShortcut` and
 * `_runShellShortcut` deliberately stayed in app.js — the first is global key
 * dispatch and the second runs a `!cmd` shell shortcut; neither is an overlay,
 * they just share a word.
 *
 * Mixed into ResonantApp.prototype by applyMixin in app.js — see
 * autonomous_view.js for why a prototype mixin rather than an ES module, and
 * why Object.assign would silently copy nothing here.
 *
 * Load order matters: this file must load BEFORE app.js.
 */

class ResonantSettingsView {
    _accountSummary() {
        const account = this.sonnAccount;
        const text = value => typeof value === 'string' ? value.trim().slice(0, 160) : '';
        const connected = !!account?.user && !account.error;
        const name = text(this.settings?.general?.display_name) || (connected ? text(account.user) : 'SONN account');
        const detail = connected ? (account.billing?.enabled ? 'SONN · Prepaid credits' : 'SONN · Billing off') : 'Not connected to SONN';
        const status = this._sonnAccountPending ? 'Checking SONN account…' : account?.error || (connected
            ? `Account: ${text(account.user)}` : 'Connect with your SONN private invitation');
        const initials = name === 'SONN account' ? 'S' : name.includes('@') ? Array.from(name)[0].toUpperCase()
            : name.split(/\s+/).slice(0, 2).map(word => Array.from(word)[0]).join('').toUpperCase();
        return {name, detail, status, initials};
    }

    _requestSonnAccount() {
        if (this._sonnAccountPending) return;
        this._sonnAccountPending = true;
        this._renderAccountMenu();
        this.send({command: 'sonn_account'});
    }

    _renderSonnAccount() {
        const account = this.sonnAccount;
        const summary = this._accountSummary();
        const escape = value => this.escapeHtml(String(value ?? ''));
        const money = value => Number.isSafeInteger(value)
            ? new Intl.NumberFormat('en-US', {style: 'currency', currency: 'USD'}).format(value / 1000000) : 'Unavailable';
        const billing = account?.billing;
        return `<div class="provider-connection">
            <strong>${escape(summary.name)}</strong><p>${escape(summary.detail)}</p><p role="status">${escape(summary.status)}</p>
            ${account?.user && !account.error ? `<div class="sonn-account-balances">
                <div><small>Available credits</small><strong>${billing?.enabled ? money(billing.available_credit_microusd) : 'Billing off'}</strong></div>
                <div><small>Reserved credits</small><strong>${money(billing?.reserved_credit_microusd)}</strong></div>
                <div><small>Total charged</small><strong>${money(billing?.total_charged_microusd)}</strong></div>
            </div><p class="provider-note">${account.checkout_mode === 'test' ? 'Test checkout mode. ' : ''}Prepaid account credits, not a subscription. Provider usage outside SONN is separate.</p>
            <p class="provider-note">Last checked: ${escape(new Date(account.checked_at).toLocaleString())}</p>` : ''}
            <button class="btn-sm" id="sonn-account-refresh" ${this._sonnAccountPending ? 'disabled' : ''}>${this._sonnAccountPending ? 'Checking…' : 'Connect / refresh SONN account'}</button>
            <a href="https://getsonn.com/workspace" target="_blank" rel="noopener noreferrer">Open SONN workspace</a>
            <p class="provider-note">Uses your SONN project URL and private invitation in Connections → Network and API keys. SONN currently reports an account identifier; Display name in Profile is a local label. Manage credits in the SONN workspace.</p>
        </div>`;
    }

    _renderAccountMenu() {
        const summary = this._accountSummary();
        for (const [id, value] of Object.entries({
            'account-name': summary.name, 'account-menu-name': summary.name,
            'account-detail': summary.detail, 'account-menu-detail': summary.detail,
            'account-menu-status': summary.status, 'account-avatar': summary.initials,
        })) {
            const element = document.getElementById(id);
            if (element) { element.textContent = value; element.title = value; }
        }
        const visible = this.settings?.general?.show_companion === true;
        const pet = document.getElementById('sidebar-companion');
        if (pet) { pet.hidden = !visible; pet.classList.toggle('working', !!this.isRunning); }
        const status = document.getElementById('echo-status');
        if (status) status.textContent = this.isRunning ? 'Keeping you company…' : 'Ready when you are';
        const toggle = document.getElementById('account-pet');
        if (toggle) {
            toggle.textContent = visible ? 'Hide Echo' : 'Show Echo';
            toggle.setAttribute('aria-pressed', String(visible));
        }
    }

    _setCompanion(visible) {
        this.settings ||= {};
        this.settings.general ||= {};
        this.settings.general.show_companion = visible;
        this._renderAccountMenu();
        this.send({command: 'update_settings', section: 'general', key: 'show_companion', value: visible});
    }

    _closeAccountMenu(restoreFocus = false) {
        const popover = document.getElementById('account-popover');
        const trigger = document.getElementById('sidebar-account');
        if (popover) popover.hidden = true;
        trigger?.setAttribute('aria-expanded', 'false');
        if (restoreFocus) trigger?.focus();
    }

    _initAccountMenu() {
        const trigger = document.getElementById('sidebar-account');
        const popover = document.getElementById('account-popover');
        if (!trigger || !popover) return;
        trigger.addEventListener('click', () => {
            if (!popover.hidden) { this._closeAccountMenu(); return; }
            this._renderAccountMenu();
            popover.hidden = false;
            trigger.setAttribute('aria-expanded', 'true');
            popover.querySelector('button')?.focus();
            // Account reads never enter the startup or generation path.
            if (!this.sonnAccount) this._requestSonnAccount();
        });
        document.addEventListener('pointerdown', event => {
            if (!popover.hidden && !popover.contains(event.target) && !trigger.contains(event.target)) this._closeAccountMenu();
        });
        document.addEventListener('focusin', event => {
            if (!popover.hidden && !popover.contains(event.target) && !trigger.contains(event.target)) this._closeAccountMenu();
        });
        popover.addEventListener('keydown', event => {
            if (event.key === 'Escape') {
                event.preventDefault(); event.stopPropagation(); this._closeAccountMenu(true);
            } else if (['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) {
                const buttons = [...popover.querySelectorAll('button')];
                let index = buttons.indexOf(document.activeElement);
                index = event.key === 'Home' ? 0 : event.key === 'End' ? buttons.length - 1
                    : (index + (event.key === 'ArrowDown' ? 1 : -1) + buttons.length) % buttons.length;
                event.preventDefault(); buttons[index]?.focus();
            }
        });
        const openSettings = sectionId => {
            this._closeAccountMenu();
            this._settingsActivePage = sectionId || this._settingsActivePage || 'general';
            this._settingsQuery = '';
            const search = document.getElementById('settings-search');
            if (search) search.value = '';
            this.switchView('settings');
            document.getElementById('settings-back')?.focus();
        };
        document.getElementById('account-settings')?.addEventListener('click', () => openSettings(false));
        document.getElementById('account-connections')?.addEventListener('click', () => openSettings('provider_connections'));
        document.getElementById('account-usage')?.addEventListener('click', () => openSettings('sonn_account'));
        document.getElementById('account-pet')?.addEventListener('click', () => this._setCompanion(!this.settings?.general?.show_companion));
        document.getElementById('echo-hide')?.addEventListener('click', () => {
            this._setCompanion(false); trigger.focus();
        });
        this._renderAccountMenu();
    }

    _renderEditorIntegrations() {
        const editors = this.editorIntegrations || [];
        if (!editors.length) return '<p class="editor-help">Loading editor setup…</p>';
        return `<p class="editor-help">Connect a running editor to work on scenes and assets from chat. Setup uses community MCP bridges. Reconnect here after restarting SONN Client. Codex and Claude Code receive enabled bridges on their next Full-auto turn; other modes keep editor tools disabled in those CLIs.</p>
            <div class="editor-grid">${editors.map(editor => {
                const escape = value => this.escapeHtml(String(value ?? ''));
                const busy = this._editorBusy === editor.id;
                const status = editor.connected ? `Bridge connected · ${editor.tools} tools` : editor.enabled ? 'Configured · not connected' : editor.configured ? 'Disabled' : 'Not configured';
                const value = this._editorDrafts?.[editor.id] ?? editor.value;
                return `<article class="editor-card" aria-label="${escape(editor.title)} integration">
                    <div><h3>${escape(editor.title)}</h3><p class="editor-help">${escape(editor.summary)}</p></div>
                    <p class="editor-connection-status" role="status">${escape(status)}</p>
                    <details><summary>Setup ${escape(editor.title)}</summary>
                        <ol>${editor.steps.map(step => `<li>${escape(step)}</li>`).join('')}</ol>
                        <a href="${escape(editor.url)}" target="_blank" rel="noopener noreferrer">Open setup guide</a>
                        <p class="editor-help">The editor and its add-on must be installed separately. ${editor.id === 'blender' ? 'Connecting may download the pinned bridge using uv. ' : ''}Editor actions use the editor's filesystem access, outside SONN Client's project sandbox.</p>
                    </details>
                    <label class="editor-field">${escape(editor.label)}
                        <input class="settings-input" data-editor-input="${editor.id}" value="${escape(value)}" ${editor.field === 'port' ? 'inputmode="numeric"' : ''} />
                    </label>
                    <div class="editor-actions">
                        <button class="btn-sm" data-editor="${editor.id}" data-editor-action="connect" ${busy ? 'disabled' : ''}>${editor.connected ? 'Reconnect' : 'Connect'}</button>
                        <button class="btn-sm" data-editor="${editor.id}" data-editor-action="check" ${busy || !editor.connected ? 'disabled' : ''}>Check editor</button>
                        <button class="btn-sm" data-editor="${editor.id}" data-editor-action="disconnect" ${busy || !editor.enabled ? 'disabled' : ''}>Disable</button>
                    </div>
                    ${editor.error ? `<p class="editor-error">${escape(editor.error)}</p>` : ''}
                    ${this._editorMessages?.[editor.id] ? `<p role="status" class="editor-help">${escape(this._editorMessages[editor.id])}</p>` : ''}
                    ${this._editorChecks?.[editor.id] ? `<details open><summary>Last editor check</summary><pre class="editor-check-output">${escape(this._editorChecks[editor.id])}</pre></details>` : ''}
                </article>`;
            }).join('')}</div>`;
    }


    _renderProviderConnections() {
        const connections = this.providerConnections || {};
        const codex = connections.codex || {};
        const router = connections.openrouter || {};
        const sonn = connections.sonn || {};
        const account = codex.account;
        const subscription = account?.type?.startsWith('chatgpt');
        const accountLabel = subscription ? `ChatGPT ${account.planType || ''} · ${account.email || 'Connected'}`
            : account ? 'Codex API-key access (billed separately)' : 'Connect your ChatGPT subscription through Codex';
        const limits = codex.rate_limits?.rateLimitsByLimitId || (codex.rate_limits?.rateLimits ? { codex: codex.rate_limits.rateLimits } : {});
        const quota = Object.entries(limits).flatMap(([name, bucket]) => ['primary', 'secondary'].map(key => {
            const window = bucket[key];
            if (typeof window?.usedPercent !== 'number') return '';
            const reset = window.resetsAt ? ` · resets ${new Date(window.resetsAt * 1000).toLocaleString()}` : '';
            return `<div class="provider-note">${this.escapeHtml(bucket.limitName || name)}: ${Math.max(0, 100 - window.usedPercent).toFixed(0)}% remaining${this.escapeHtml(reset)}</div>`;
        })).join('');
        let loginLink = '';
        try {
            const url = new URL(codex.auth_url);
            if (url.protocol === 'https:' && ['chatgpt.com', 'openai.com'].some(domain => url.hostname === domain || url.hostname.endsWith(`.${domain}`))) {
                loginLink = `<a href="${this.escapeHtml(url.href)}" target="_blank" rel="noopener noreferrer">Continue sign-in in your browser</a><button class="btn-sm" data-provider="codex" data-provider-action="cancel">Cancel sign-in</button>`;
            }
        } catch (_) { /* No pending browser login. */ }
        return `<div class="provider-connection">
            <strong>ChatGPT / Codex</strong><p>${this.escapeHtml(codex.error || accountLabel)}</p>
            ${quota}${subscription && !quota ? '<p class="provider-note">Usage limits unavailable. Refresh to try again.</p>' : ''}
            <div class="provider-actions"><button class="btn-sm" data-provider="codex" data-provider-action="login">Sign in with ChatGPT</button>
            <button class="btn-sm" data-provider="codex" data-provider-action="status">Refresh account & models</button>${loginLink}</div>
            <p class="provider-note">Uses your installed Codex CLI and its sign-in. After signing in, refresh the account. <a href="https://developers.openai.com/codex/cli" target="_blank" rel="noopener noreferrer">Install Codex CLI</a></p>
            </div><div class="provider-connection"><strong>OpenRouter</strong>
            <p>${this.escapeHtml(router.error || (router.status === 'ready' ? 'Connected · API usage is billed through OpenRouter' : 'Add an OpenRouter key in API keys below, then check the connection.'))}</p>
            ${typeof router.usage === 'number' ? `<p class="provider-note">Key usage: $${router.usage.toFixed(4)}${typeof router.limit_remaining === 'number' ? ` · key allowance remaining: $${router.limit_remaining.toFixed(2)}` : ''}</p>` : ''}
            <button class="btn-sm" data-provider="openrouter" data-provider-action="status">Check connection & refresh models</button></div>
            <div class="provider-connection"><strong>SONN</strong>
            <p>${this.escapeHtml(sonn.error || (sonn.status === 'ready' ? `Connected · ${sonn.model_count} models available` : 'Set your project API base URL in Network and your SONN key in API keys below.'))}</p>
            <button class="btn-sm" data-provider="sonn" data-provider-action="status">Check SONN connection & refresh models</button></div>`;
    }

    openProviderPicker() {
        document.getElementById('provider-picker')?.remove();
        const dialog = document.createElement('dialog');
        dialog.id = 'provider-picker';
        dialog.className = 'provider-picker';
        dialog.setAttribute('aria-labelledby', 'provider-picker-title');
        dialog.innerHTML = `<div class="provider-picker-heading"><h2 id="provider-picker-title">Choose a model</h2><button type="button" class="btn-sm" data-close aria-label="Close model picker">Close</button></div>
            <input type="search" placeholder="Search providers or models" aria-label="Search providers or models" autofocus>
            <label class="provider-remember"><input type="checkbox" data-remember> Use for new sessions in this project</label>
            <div class="provider-model-list"></div><div class="provider-picker-footer"><button class="btn-sm" data-connections>Manage connections</button><span data-results></span></div>`;
        const search = dialog.querySelector('input[type=search]');
        const list = dialog.querySelector('.provider-model-list');
        const labels = this._getBackendLabels();
        const render = () => {
            const favorites = this.settings?.model_favorites?.models || [];
            const query = search.value.trim().toLowerCase();
            const models = Object.entries(this.backends || {}).flatMap(([backend, info]) => (info.models || []).map(model => ({
                backend, model, id: `${backend}:${model}`, label: info.model_labels?.[model] || model,
                details: info.model_details?.[model] || {},
            }))).filter(row => `${labels[row.backend] || row.backend} ${row.label} ${row.model}`.toLowerCase().includes(query))
                .sort((a, b) => Number(favorites.includes(b.id)) - Number(favorites.includes(a.id)) || Number(b.id === `${this.currentBackendName}:${this.currentModelName}`) - Number(a.id === `${this.currentBackendName}:${this.currentModelName}`) || a.backend.localeCompare(b.backend) || a.label.localeCompare(b.label));
            list.replaceChildren();
            for (const row of models.slice(0, 80)) {
                const item = document.createElement('div');
                item.className = 'provider-model-row';
                const active = row.backend === this.currentBackendName && row.model === this.currentModelName;
                const price = row.details.pricing;
                const pricing = price && price.prompt != null && price.completion != null && Number.isFinite(Number(price.prompt)) && Number.isFinite(Number(price.completion)) && Number(price.prompt) >= 0 && Number(price.completion) >= 0
                    ? ` · $${(Number(price.prompt) * 1e6).toFixed(2)} in / $${(Number(price.completion) * 1e6).toFixed(2)} out per 1M tokens` : '';
                item.innerHTML = `<button type="button" class="provider-model-choice" ${active ? 'aria-current="true"' : ''}><strong>${this.escapeHtml(row.label)}${active ? ' ✓' : ''}</strong><span>${this.escapeHtml(labels[row.backend] || row.backend)}${this.escapeHtml(pricing)}</span></button>
                    <button type="button" class="provider-star" aria-label="${favorites.includes(row.id) ? 'Unfavorite' : 'Favorite'} ${this.escapeHtml(row.label)}" aria-pressed="${favorites.includes(row.id)}">${favorites.includes(row.id) ? '★' : '☆'}</button>`;
                item.querySelector('.provider-model-choice').onclick = () => {
                    this.send({command: 'switch_model', backend: row.backend, model: row.model, remember_project: dialog.querySelector('[data-remember]').checked});
                    dialog.close();
                };
                item.querySelector('.provider-star').onclick = () => {
                    const updated = favorites.includes(row.id) ? favorites.filter(id => id !== row.id) : [...favorites, row.id];
                    this.settings ||= {};
                    this.settings.model_favorites = {models: updated};
                    this.send({command: 'update_settings', section: 'model_favorites', key: 'models', value: updated});
                    render();
                    [...list.querySelectorAll('.provider-star')].find(btn => btn.getAttribute('aria-label').endsWith(` ${row.label}`))?.focus();
                };
                list.appendChild(item);
            }
            if (!models.length) list.textContent = 'No matching models. Connect a provider or refresh its models in Settings.';
            dialog.querySelector('[data-results]').textContent = models.length > 80 ? `Showing 80 of ${models.length} · narrow your search` : `${models.length} models`;
        };
        search.addEventListener('input', render);
        dialog.querySelector('[data-close]').onclick = () => dialog.close();
        dialog.querySelector('[data-connections]').onclick = () => { dialog.close(); this.switchView('settings'); };
        dialog.addEventListener('close', () => dialog.remove());
        document.body.appendChild(dialog);
        render();
        dialog.showModal();
    }


    toggleStatusPopover() {
        this.statusPopoverOpen = !this.statusPopoverOpen;
        if (this.statusPopover) this.statusPopover.hidden = !this.statusPopoverOpen;
        if (this.statusPopoverTrigger) {
            this.statusPopoverTrigger.setAttribute('aria-expanded', this.statusPopoverOpen ? 'true' : 'false');
        }
        if (this.statusPopoverOpen) {
            this.requestMcpList();
            this.requestLspList();
            this.requestPluginList();
            this._renderStatusPopover();
        }
    }


    closeStatusPopover() {
        if (!this.statusPopoverOpen) return;
        this.statusPopoverOpen = false;
        if (this.statusPopover) this.statusPopover.hidden = true;
        if (this.statusPopoverTrigger) this.statusPopoverTrigger.setAttribute('aria-expanded', 'false');
    }


    _renderStatusPopover() {
        if (!this.statusPopoverBody) return;
        const tab = this.statusPopoverTab || 'servers';
        this.statusPopover?.querySelectorAll('.status-tab').forEach((btn) => {
            btn.classList.toggle('active', btn.dataset.statusTab === tab);
        });
        const renderers = {
            servers: () => this._renderStatusServers(),
            mcp: () => this._renderStatusMcp(),
            lsp: () => this._renderStatusLsp(),
            plugins: () => this._renderStatusPlugins(),
            skills: () => this._renderStatusSkills(),
        };
        this.statusPopoverBody.innerHTML = `
            <div class="status-popover-summary">
                <span class="status-summary-dot ${this.systemStatus === 'connected' ? 'ok' : this.systemStatus === 'warning' ? 'warn' : 'bad'}"></span>
                <span>${this.escapeHtml(this.systemStatusLabel || 'Runtime status')}</span>
            </div>
            <div class="status-popover-list">${(renderers[tab] || renderers.servers)()}</div>
            <div class="status-popover-footer">
                <button type="button" data-status-action="open-settings">Settings</button>
            </div>`;
    }


    rerenderHarnessPopoverIfOpen() {
        if (!this.harnessPopoverOpen) return;
        const existing = document.querySelector('.harness-popover');
        if (!existing) return;
        existing.remove();
        this.harnessPopoverOpen = false;
        this.toggleHarnessPopover();
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


    /**
     * v0.4.0 — Ollama setup wizard. URL field persists via
     * `update_settings` and triggers a re-detect, so the user never
     * has to leave the window to get unstuck.
     */
    _renderOllamaSetupWizard(list, label, opts = {}) {
        const reason = opts.reason || 'unreachable';
        const triedUrl = opts.url
            || (this.settings && this.settings.network && this.settings.network.ollama_url)
            || 'http://127.0.0.1:11434';

        label.textContent = 'Set up Ollama';

        const headline = reason === 'connected-but-empty'
            ? 'Ollama is reachable but no models are pulled yet.'
            : 'SONN Client needs Ollama. We couldn\'t reach it.';

        const wizard = document.createElement('div');
        wizard.className = 'ollama-wizard';
        wizard.innerHTML = `
            <div class="ollama-wizard-headline">
                <span class="ollama-wizard-icon" aria-hidden="true">🦙</span>
                <span>${this.escapeHtml(headline)}</span>
            </div>
            <p class="ollama-wizard-blurb">
                SONN Client uses the models exposed by your configured Ollama
                endpoint. Model capabilities are detected at runtime.
            </p>

            <div class="ollama-wizard-step">
                <div class="ollama-wizard-step-title">1. Ollama URL</div>
                <div class="ollama-wizard-row">
                    <input type="text" class="ollama-wizard-url" value="${this.escapeHtml(triedUrl)}"
                        placeholder="http://127.0.0.1:11434" spellcheck="false" autocomplete="off">
                    <button type="button" class="ollama-wizard-test">Test</button>
                </div>
                <div class="ollama-wizard-quick-row">
                    <span class="ollama-wizard-quick-label">Quick fill:</span>
                    <button type="button" class="ollama-wizard-quick" data-url="http://127.0.0.1:11434"
                        title="Ollama on this machine">localhost</button>
                </div>
                <div class="ollama-wizard-hint" id="ollama-wizard-hint">
                    Default: <code>http://127.0.0.1:11434</code>.
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
                <div class="ollama-wizard-step-title">3. Pull a model</div>
                <div class="ollama-wizard-cmd">
                    Browse <a href="https://ollama.com/search" target="_blank" rel="noopener">Ollama models</a>,
                    then run <code>ollama pull &lt;model&gt;</code>
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


    _settingsPages() {
        return [
            {id:'general', title:'General', group:'Personal', icon:'settings', description:'Choose how the agent works and which models new sessions use.', sections:['general'], fields:['default_permission_mode','default_backend','default_model','auto_lint_after_edits','auto_test_after_edits','auto_test_command','big_context_profile','harness_enabled'], keywords:'permissions approval workflow'},
            {id:'profile', title:'Profile', group:'Personal', icon:'person', description:'Personalize your local workspace identity.', sections:['general'], fields:['display_name']},
            {id:'appearance', title:'Appearance', group:'Personal', icon:'sun', description:'Make the workspace feel right for you.', sections:['appearance']},
            {id:'pets', title:'Pets', group:'Personal', icon:'pet', description:'A little company while you build.', sections:['general'], fields:['show_companion'], keywords:'Echo companion'},
            {id:'sonn_account', title:'SONN account & credits', group:'Personal', icon:'person', description:'Your authenticated SONN identity and prepaid credit balance.', sections:['sonn_account'], keywords:'billing invitation balance'},
            {id:'cost_tracking', title:'Usage & cost', group:'Personal', icon:'chart', description:'Review tracked model usage and local spending alerts.', sections:['cost_tracking'], keywords:'tokens budget'},
            {id:'provider_connections', title:'Connections', group:'Integrations', icon:'globe', description:'Connect model providers and manage their endpoints and API keys.', sections:['provider_connections','network','api_keys'], keywords:'ChatGPT Codex OpenRouter SONN login authentication'},
            {id:'creative_editors', title:'Creative editors', group:'Integrations', icon:'cube', description:'Work with Blender, Unity, and Unreal Engine 5.', sections:['creative_editors']},
            {id:'mcp_servers', title:'MCP servers', group:'Integrations', icon:'plug', description:'Connect tools supplied by external servers.', sections:['mcp_servers']},
            {id:'engram', title:'Memory', group:'Coding', icon:'book', description:'Configure the optional Engram memory service.', sections:['engram']},
            {id:'rag', title:'Codebase index', group:'Coding', icon:'book', description:'Index your project for semantic code search.', sections:['rag'], keywords:'RAG files repository'},
            {id:'hooks', title:'Hooks', group:'Coding', icon:'plug', description:'Inspect commands that run at lifecycle events.', sections:['hooks']},
            {id:'local_backends', title:'Ollama runtime', group:'Advanced', icon:'cube', description:'Tune your local model runtime.', sections:['local_backends']},
            {id:'prompt_inspector', title:'Prompt inspector', group:'Advanced', icon:'book', description:'Inspect the instructions used by the active model.', sections:['prompt_inspector']},
            {id:'model_evaluations', title:'Model evaluations', group:'Advanced', icon:'chart', description:'Review model quality and runtime diagnostics.', sections:['model_evaluations']},
            {id:'iteration_checkpoints', title:'Checkpoints & recovery', group:'Advanced', icon:'history', description:'Inspect saved iterations and recovery options.', sections:['iteration_checkpoints']},
        ];
    }

    _matchingSettingsPages(pages, sections, query) {
        const words = query.toLowerCase().trim().split(/\s+/).filter(Boolean);
        return pages.filter(page => {
            // Search labels and help, never stored values, account data, or secrets.
            const fields = sections.filter(section => page.sections.includes(section.id))
                .flatMap(section => section.fields || []).filter(field => !page.fields || page.fields.includes(field.key));
            const headings = page.fields ? [] : sections.filter(section => page.sections.includes(section.id)).map(section => section.title);
            const text = [page.title, page.description, page.keywords, ...headings, ...fields.flatMap(field => [field.label, field.hint])].join(' ').toLowerCase();
            return words.every(word => text.includes(word));
        });
    }

    _initSettingsNavigation() {
        if (this._settingsNavigationReady) return;
        this._settingsNavigationReady = true;
        const search = document.getElementById('settings-search');
        const applySearch = () => {
            this._settingsQuery = search.value;
            this.renderSettingsView({force:true});
            document.getElementById('settings-content').scrollTop = 0;
        };
        search.addEventListener('input', applySearch);
        search.addEventListener('keydown', event => {
            if (event.key === 'Escape' && search.value) {
                event.preventDefault(); event.stopPropagation(); search.value = ''; applySearch();
            } else if (event.key === 'ArrowDown') {
                event.preventDefault(); document.querySelector('#settings-nav button')?.focus();
            }
        });
        document.getElementById('settings-clear-search').addEventListener('click', () => {
            search.value = ''; applySearch(); search.focus();
        });
        document.getElementById('settings-nav').addEventListener('keydown', event => {
            if (!['ArrowDown','ArrowUp','Home','End'].includes(event.key)) return;
            const buttons = [...document.querySelectorAll('#settings-nav button')];
            const index = buttons.indexOf(document.activeElement);
            const next = event.key === 'Home' ? 0 : event.key === 'End' ? buttons.length - 1
                : (index + (event.key === 'ArrowDown' ? 1 : -1) + buttons.length) % buttons.length;
            event.preventDefault(); buttons[next]?.focus();
        });
        this.settingsBody.addEventListener('focusout', () => queueMicrotask(() => {
            if (this._settingsRenderPending) this.renderSettingsView();
        }));
    }

    _renderSettingsNavigation(pages) {
        const icons = {
            settings:'M6 2h4l1 3 3 1v4l-3 1-1 3H6l-1-3-3-1V6l3-1z M6 8a2 2 0 1 0 4 0 2 2 0 0 0-4 0',
            person:'M5 5a3 3 0 1 0 6 0 3 3 0 0 0-6 0 M2 14c0-6 12-6 12 0',
            sun:'M5 8a3 3 0 1 0 6 0 3 3 0 0 0-6 0 M8 1v1m0 12v1M1 8h1m12 0h1M3 3l1 1m8 8 1 1M3 13l1-1m8-8 1-1',
            pet:'M3 6 2 2l5 2h2l5-2-1 4c4 10-14 10-10 0z M5 8h.1M11 8h.1M6 11h4',
            chart:'M2 2v12h12M5 10V7m4 3V4m4 6V6',
            globe:'M1 8a7 7 0 1 0 14 0A7 7 0 1 0 1 8M1 8h14M8 1c-4 4-4 10 0 14 4-4 4-10 0-14',
            cube:'m8 1 6 3v8l-6 3-6-3V4z M2 4l6 4 6-4M8 8v7',
            plug:'M5 1v4m6-4v4M3 5h10v3a5 5 0 0 1-10 0z M8 13v2',
            book:'M8 3C5 1 2 1 1 2v11c3-1 5 0 7 1 2-1 4-2 7-1V2c-1-1-4-1-7 1z M8 3v11',
            history:'M2 5a6 6 0 1 1 0 6M2 1v4h4M8 4v4l3 2',
        };
        const nav = document.getElementById('settings-nav');
        const scrollTop = nav.scrollTop, scrollLeft = nav.scrollLeft;
        nav.innerHTML = ['Personal','Integrations','Coding','Advanced'].map(group => {
            const items = pages.filter(page => page.group === group);
            return items.length ? `<div class="settings-nav-group"><h3>${group}</h3>${items.map(page => `<button type="button" id="settings-nav-${page.id}" data-settings-page="${page.id}" ${page.id === this._settingsActivePage ? 'aria-current="page"' : ''}><svg width="16" height="16" viewBox="0 0 16 16" fill="none" aria-hidden="true"><path d="${icons[page.icon]}" stroke="currentColor" stroke-width="1.15" stroke-linecap="round" stroke-linejoin="round"/></svg><span>${this.escapeHtml(page.title)}</span></button>`).join('')}</div>` : '';
        }).join('');
        nav.scrollTop = scrollTop;
        nav.scrollLeft = scrollLeft;
        nav.querySelectorAll('[data-settings-page]').forEach(button => button.addEventListener('click', () => {
            this._settingsActivePage = button.dataset.settingsPage;
            this.renderSettingsView({force:true});
            document.getElementById('settings-content').scrollTop = 0;
        }));
    }

    _loadSettingsPage(page) {
        if (this._settingsLoadedPage === page) return;
        this._settingsLoadedPage = page;
        if (page === 'sonn_account' && !this.sonnAccount) this._requestSonnAccount();
        if (page === 'provider_connections' && !this.providerConnections?.codex) this.send({command:'provider_connection', provider:'codex', action:'status'});
        const command = {creative_editors:'editor_list', cost_tracking:'get_costs', model_evaluations:'evaluation_list', iteration_checkpoints:'checkpoint_list'}[page];
        if (command) this.send({command});
    }

    _settingsSections() {
        return [
            { id: "sonn_account", title: "SONN account & credits", open: true, custom: true },
            { id: "provider_connections", title: "Connections", open: true, custom: true },
            { id: 'creative_editors', title: 'Creative editors', open: true, custom: true },
            {
                id: 'cost_tracking', title: 'Usage & Cost', open: true,
                fields: [
                    { key: 'enabled', label: 'Enable cost tracking', type: 'toggle' },
                    { key: 'budget_alert_usd', label: 'Daily budget alert ($)', type: 'number' },
                ]
            },
            {
                id: 'general', title: 'General', open: true,
                fields: [
                    { key: 'display_name', label: 'Display name', type: 'text',
                      placeholder: 'Your name', hint: 'Local sidebar label. Leave blank to use your SONN account identifier. This does not change your SONN account.' },
                    { key: 'show_companion', label: 'Show Echo, your sidebar companion', type: 'toggle',
                      hint: 'A quiet little companion. No model calls or notifications; respects reduced motion.' },
                    // v0.4.0 — single backend; the Auto option is the
                    // only sensible value. Kept the select for schema
                    // compatibility with older settings.json.
                    { key: 'default_backend', label: 'Default backend', type: 'select',
                      options: [
                          { value: 'ollama', label: 'Ollama' },
                          { value: 'exo', label: 'EXO' },
                          { value: 'kimi', label: 'Kimi API' },
                          { value: 'codex', label: 'ChatGPT / Codex' },
                          { value: 'openrouter', label: 'OpenRouter' },
                          { value: 'sonn', label: 'SONN' },
                          { value: '', label: 'Auto' },
                      ]
                    },
                    { key: 'default_model', label: 'Default model', type: 'text',
                      placeholder: 'Model identifier from your provider',
                      hint: 'Leave blank to use the first model reported by the chosen backend.' },
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
                    { key: 'big_context_profile', label: 'Large-context profile', type: 'toggle',
                      hint: 'Bumps Ollama context to 131072 tokens and batch to 2048. Best for large-repo sessions. Restart the app for the change to take effect on the next backend connection.' },
                    { key: 'harness_enabled', label: 'Sprint workflow (planner / generator / evaluator)', type: 'toggle',
                      hint: 'Off by default. Enable to use SONN Client\u2019s structured planner\u2192generator\u2192evaluator pattern with sprint contracts and an autonomous cycle. State lives in ~/.resonant/, not in your repo.' },
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
                    { key: 'font_size', label: 'Base font size', type: 'select', default: '13.5',
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
                id: 'local_backends', title: 'Ollama Runtime', open: false,
                fields: [
                    { key: 'ollama_host', label: 'Ollama host (OLLAMA_HOST)', type: 'text' },
                    { key: 'ollama_num_ctx', label: 'Ollama context window (num_ctx)', type: 'number' },
                    { key: 'ollama_keep_alive', label: 'Ollama keep-alive duration', type: 'text' },
                ]
            },
            {
                id: 'prompt_inspector', title: 'Active Prompt Inspector', custom: true,
            },
            {
                id: 'model_evaluations', title: 'GLM / DeepSeek Evaluations', custom: true,
            },
            {
                id: 'iteration_checkpoints', title: 'Iteration Checkpoints & Recovery', custom: true,
            },
            {
                id: 'network', title: 'Network',
                fields: [
                    // v0.4.0 — Ollama is the only backend. Default Mac Studio
                    // location is 10.0.0.133:11434; leave blank to use the
                    // OLLAMA_HOST env var or auto-detect.
                    { key: 'ollama_url', label: 'Ollama URL (e.g. http://127.0.0.1:11434)', type: 'text' },
                    { key: 'exo_url', label: 'EXO OpenAI API URL', type: 'text',
                      hint: 'Default: http://127.0.0.1:52415/v1. EXO_API_URL and EXO_BASE_URL are also supported.' },
                    { key: 'sonn_url', label: 'SONN API base URL', type: 'text',
                      placeholder: 'https://getsonn.com/v1/workspace/projects/<project-id>/openai/v1',
                      hint: 'Paste your project connection URL ending in /openai/v1. SONN_API_URL is also supported. Model: sonn-auto.' },
                ]
            },
            {
                id: 'api_keys', title: 'API keys', open: false,
                fields: [
                    { key: 'sonn', label: 'SONN API key', type: 'password',
                      hint: 'Enter your private invitation key. Stored locally in ~/.resonant/settings.json and hidden after saving. SONN_API_KEY is also supported.' },
                    { key: 'openrouter', label: 'OpenRouter API key', type: 'password',
                      hint: 'Stored locally in ~/.resonant/settings.json. OPENROUTER_API_KEY is also supported. API calls use your OpenRouter credits.' },
                    { key: 'kimi', label: 'Moonshot API key', type: 'password',
                      hint: 'Stored locally in ~/.resonant/settings.json. MOONSHOT_API_KEY is also supported and takes effect when no stored key exists.' },
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

    }

    renderSettingsView({force = false} = {}) {
        if (!this.settingsBody) return;
        this._initSettingsNavigation();
        const focused = document.activeElement;
        if (!force && this.settingsBody.contains(focused) && focused.matches('input, select, textarea')) {
            this._settingsRenderPending = true;
            return;
        }
        this._settingsRenderPending = false;
        const focusId = focused?.id;
        const sections = this._settingsSections();
        const pages = this._settingsPages();
        const matches = this._matchingSettingsPages(pages, sections, this._settingsQuery || '');
        const page = matches.find(item => item.id === this._settingsActivePage) || matches[0];
        if (page) this._settingsActivePage = page.id;
        this._renderSettingsNavigation(matches);
        document.getElementById('settings-page-title').textContent = page?.title || 'Search settings';
        document.getElementById('settings-page-description').textContent = page?.description || '';
        document.getElementById('settings-empty').hidden = !!page;
        const scroll = document.getElementById('settings-content');
        const scrollTop = scroll.scrollTop;
        const visibleSections = page ? sections.filter(section => page.sections.includes(section.id)).map(section => ({
            ...section, fields: section.fields?.filter(field => !page.fields || page.fields.includes(field.key)),
        })).flatMap(section => page.id === 'general' ? [
            {heading:'Permissions', keys:['default_permission_mode']},
            {heading:'Models', keys:['default_backend','default_model','big_context_profile']},
            {heading:'Workflow', keys:['auto_lint_after_edits','auto_test_after_edits','auto_test_command','harness_enabled']},
        ].map(group => ({...section, heading:group.heading, fields:section.fields.filter(field => group.keys.includes(field.key))})) : [section]) : [];
        this.settingsBody.innerHTML = '';

        for (const section of visibleSections) {
            const data = this.settings[section.id] || {};
            const el = document.createElement('div');
            el.className = 'settings-section open';
            el.dataset.settingsSection = section.id;

            let bodyHtml = '';
            if (section.id === 'sonn_account') {
                bodyHtml = this._renderSonnAccount();
            } else if (section.id === 'provider_connections') {
                bodyHtml = this._renderProviderConnections();
            } else if (section.id === 'creative_editors') {
                bodyHtml = this._renderEditorIntegrations();
            } else if (section.id === 'cost_tracking') {
                bodyHtml = this._renderCostDashboard(data);
            } else if (section.id === 'rag') {
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
            } else if (section.id === 'prompt_inspector') {
                const inspector = this.promptInspector;
                if (!inspector) {
                    bodyHtml = `
                        <div class="settings-row"><span class="settings-row-label">Inspect the exact layered system prompt for the active model and session.</span></div>
                        <div class="settings-row"><button class="btn-sm prompt-inspector-refresh">Load active prompt</button></div>
                    `;
                } else {
                    const layers = (inspector.layers || []).map(layer => `
                        <details class="prompt-layer">
                            <summary><span>${this.escapeHtml(layer.label || layer.id)}</span><span>${layer.estimated_tokens || 0} est. tokens</span></summary>
                            <pre>${this.escapeHtml(layer.content || '')}</pre>
                        </details>
                    `).join('');
                    bodyHtml = `
                        <div class="prompt-inspector-meta">
                            <span class="prompt-profile-badge">${this.escapeHtml(inspector.profile || inspector.family || 'generic')}</span>
                            <span>${this.escapeHtml(inspector.model || 'default model')}</span>
                            <span>${inspector.estimated_tokens || 0} est. tokens</span>
                            <span title="${this.escapeHtml(inspector.sha256 || '')}">${this.escapeHtml((inspector.sha256 || '').slice(0, 12))}</span>
                            <button class="btn-sm prompt-inspector-refresh">Refresh</button>
                        </div>
                        <div class="prompt-layer-list">${layers}</div>
                    `;
                }
            } else if (section.id === 'model_evaluations') {
                const dashboard = this.evaluationDashboard || {};
                const models = dashboard.models || [];
                const specs = dashboard.specs || ['minimal'];
                const records = dashboard.records || [];
                const turnSummary = dashboard.turn_summary || {};
                const turnModels = Object.entries(turnSummary.by_model || {});
                const active = Boolean(dashboard.active_id);
                const telemetryHtml = turnModels.map(([model, metrics]) => `
                    <div class="evaluation-record">
                        <div class="evaluation-record-head">
                            <strong>${this.escapeHtml(model)}</strong>
                            <span>${metrics.turns || 0} interactive turns</span>
                        </div>
                        <div class="evaluation-metrics">
                            <span>${Math.round((metrics.empty_response_rate || 0) * 100)}% empty-response turns</span>
                            <span>${Math.round((metrics.incomplete_rate || 0) * 100)}% incomplete</span>
                            <span>${Number(metrics.avg_elapsed_seconds || 0).toFixed(1)}s average</span>
                            <span>${metrics.promise_continuations || 0} promise continuations</span>
                        </div>
                    </div>
                `).join('');
                const recordHtml = records.slice(0, 8).map(record => {
                    const result = record.result || {};
                    const rate = result.convergence_rate == null
                        ? '' : `${Math.round(result.convergence_rate * 100)}% convergence`;
                    const timing = result.total_elapsed_seconds || {};
                    const median = timing.median == null ? '' : `${Number(timing.median).toFixed(1)}s median`;
                    const baseline = record.baseline_diff;
                    const delta = baseline?.delta_total_elapsed_median;
                    const baselineText = baseline
                        ? `${baseline.has_regressions ? 'Regression' : 'Baseline OK'}${delta == null ? '' : ` (${delta >= 0 ? '+' : ''}${Number(delta).toFixed(1)}s)`}`
                        : 'No project baseline';
                    return `
                        <div class="evaluation-record status-${this.escapeHtml(record.status || 'unknown')}">
                            <div class="evaluation-record-head">
                                <strong>${this.escapeHtml(record.model_id || record.model_label || '')}</strong>
                                <span>${this.escapeHtml(record.spec_name || '')} × ${record.n || 1}</span>
                                <span class="evaluation-status">${this.escapeHtml(record.status || '')}</span>
                            </div>
                            <div class="evaluation-metrics">
                                <span>${record.completed_runs || 0}/${record.n || 1} runs</span>
                                ${rate ? `<span>${rate}</span>` : ''}
                                ${median ? `<span>${median}</span>` : ''}
                                <span>${this.escapeHtml(baselineText)}</span>
                            </div>
                            ${record.error ? `<div class="evaluation-error">${this.escapeHtml(record.error)}</div>` : ''}
                        </div>
                    `;
                }).join('');
                bodyHtml = `
                    <div class="evaluation-controls">
                        <label>Model<select class="settings-select evaluation-model">${models.map(item => `<option value="${this.escapeHtml(item.label)}">${this.escapeHtml(item.model)}</option>`).join('')}</select></label>
                        <label>Spec<select class="settings-select evaluation-spec">${specs.map(name => `<option value="${this.escapeHtml(name)}">${this.escapeHtml(name)}</option>`).join('')}</select></label>
                        <label>Runs<select class="settings-select evaluation-n"><option value="1">1 quick</option><option value="3">3 variance</option><option value="5">5 release</option></select></label>
                        <button class="btn-sm evaluation-start" ${active ? 'disabled' : ''}>${active ? 'Evaluation running…' : 'Run evaluation'}</button>
                    </div>
                    <div class="settings-row-hint evaluation-hint">Runs use fresh temporary projects and the live Ollama models. Results persist under ~/.resonant/evaluations.</div>
                    <div class="settings-row-hint evaluation-hint"><strong>Interactive provider health</strong> — redacted outcomes, retries, and latency; prompts and responses are never stored.</div>
                    <div class="evaluation-records">${telemetryHtml || '<div class="settings-row"><span class="settings-row-label">No interactive telemetry yet.</span></div>'}</div>
                    <div class="evaluation-records">${recordHtml || '<div class="settings-row"><span class="settings-row-label">No evaluations yet.</span></div>'}</div>
                `;
            } else if (section.id === 'iteration_checkpoints') {
                const checkpoints = this.iterationCheckpoints || [];
                const comparison = this.checkpointComparison;
                bodyHtml = `
                    <div class="settings-row-hint checkpoint-hint">Each autonomous iteration snapshots tracked and untracked work without moving HEAD. Restore first preserves the failed state on a resonant-recovery/* branch.</div>
                    <div class="checkpoint-list">
                        ${checkpoints.length ? checkpoints.map(item => `
                            <div class="checkpoint-record">
                                <div><strong>${this.escapeHtml((item.message || 'Iteration checkpoint').replace('Resonant checkpoint ', ''))}</strong><small>${this.escapeHtml(item.commit?.slice(0, 10) || '')} · ${this.escapeHtml(item.created_at || '')}</small></div>
                                <button class="btn-sm checkpoint-compare" data-ref="${this.escapeHtml(item.ref)}">Compare</button>
                                <button class="btn-sm checkpoint-restore" data-ref="${this.escapeHtml(item.ref)}">Restore</button>
                            </div>
                        `).join('') : '<div class="settings-row"><span class="settings-row-label">No iteration checkpoints yet.</span></div>'}
                    </div>
                    ${comparison ? `<div class="checkpoint-comparison"><strong>Changes since checkpoint</strong><pre>${this.escapeHtml(comparison.name_status || 'No changes')}</pre><pre>${this.escapeHtml(comparison.stat || '')}</pre></div>` : ''}
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
                const servers = typeof data === 'object' && !Array.isArray(data)
                    ? Object.entries(data).filter(([name, cfg]) => !(cfg?.editor_integration && name === `resonant_${cfg.editor_integration}`)) : [];
                if (servers.length === 0) {
                    bodyHtml = `<div class="settings-row"><span class="settings-row-label" style="color:var(--dim)">No MCP servers configured</span></div>`;
                } else {
                    bodyHtml = servers.map(([name, rawCfg]) => {
                        const cfg = rawCfg && typeof rawCfg === 'object' ? rawCfg : {};
                        const transport = cfg.transport || (cfg.url ? 'http' : 'stdio');
                        const runtime = (this.mcpServers || []).find(server => server.name === name);
                        const connected = Boolean(runtime?.connected);
                        const error = runtime?.error || '';
                        const endpoint = transport === 'http'
                            ? `<input class="settings-input mcp-url-input" type="url" data-server="${this.escapeHtml(name)}" value="${this.escapeHtml(cfg.url || '')}" aria-label="${this.escapeHtml(name)} MCP server URL" />`
                            : `<code style="font-size:11px">${this.escapeHtml([cfg.command, ...(cfg.args || [])].filter(Boolean).join(' '))}</code>`;
                        return `
                            <div class="settings-row mcp-settings-row">
                                <span class="settings-row-label"><strong>${this.escapeHtml(name)}</strong><small style="display:block;color:var(--dim)">${this.escapeHtml(transport)}</small></span>
                                <div class="settings-row-value" style="display:flex;flex-direction:column;align-items:stretch;gap:4px;min-width:0;flex:1">${endpoint}${error ? `<small style="color:var(--danger)">${this.escapeHtml(error)}</small>` : ''}</div>
                                <button class="btn-sm mcp-connect-btn" data-server="${this.escapeHtml(name)}" style="font-size:11px" ${connected ? 'disabled' : ''}>${connected ? `${runtime.tools || 0} tools` : 'Connect'}</button>
                            </div>`;
                    }).join('');
                }
                bodyHtml += `
                    <div class="settings-row" style="margin-top:8px">
                        <span class="settings-row-label" style="color:var(--dim);font-size:11px">BrowserOS: copy the Server URL from <code>chrome://browseros/mcp</code>. Other MCP servers remain user configurable.</span>
                        <button class="btn-sm mcp-add-http-btn" type="button">Add HTTP MCP</button>
                    </div>`;
            } else if (section.custom) {
                bodyHtml = `<div class="settings-row"><span class="settings-row-label" style="color:var(--dim)">Configure in settings.json</span></div>`;
            } else if (section.fields) {
                for (const field of section.fields) {
                    const val = data[field.key] ?? field.default ?? '';
                    let input = '';
                    if (field.type === 'select') {
                        const opts = field.options.map(o =>
                            `<option value="${o.value}" ${val === o.value ? 'selected' : ''}>${o.label}</option>`
                        ).join('');
                        input = `<select class="settings-select" data-section="${section.id}" data-key="${field.key}" aria-label="${this.escapeHtml(field.label)}">${opts}</select>`;
                    } else if (field.type === 'toggle') {
                        const checked = val ? 'checked' : '';
                        input = `<label class="settings-toggle"><input type="checkbox" ${checked} data-section="${section.id}" data-key="${field.key}" aria-label="${this.escapeHtml(field.label)}" /><span class="settings-toggle-track" aria-hidden="true"></span></label>`;
                    } else if (field.type === 'password') {
                        const hasSecret = Boolean(this.settings._meta?.api_keys_present?.[field.key]);
                        input = `
                            <div style="display:flex;align-items:center;gap:8px;">
                                <input class="settings-input" type="password" value="" data-section="${section.id}" data-key="${field.key}" aria-label="${this.escapeHtml(field.label)}" data-secret-field="true" placeholder="${hasSecret ? 'Stored key' : 'Enter key'}" style="flex:1" />
                                <span style="color:var(--muted);font-size:11px;white-space:nowrap">${hasSecret ? 'Stored' : 'Not set'}</span>
                                ${hasSecret ? `<button class="btn-sm settings-clear-secret" data-section="${section.id}" data-key="${field.key}" aria-label="Clear ${this.escapeHtml(field.label)}" style="font-size:11px">Clear</button>` : ''}
                            </div>
                        `;
                    } else if (field.type === 'number') {
                        input = `<input class="settings-input" type="number" value="${val || ''}" data-section="${section.id}" data-key="${field.key}" aria-label="${this.escapeHtml(field.label)}" placeholder="None" style="width:80px" />`;
                    } else {
                        const ph = field.placeholder ? ` placeholder="${this.escapeHtml(field.placeholder)}"` : '';
                        input = `<input class="settings-input" type="text" value="${this.escapeHtml(String(val))}" data-section="${section.id}" data-key="${field.key}" aria-label="${this.escapeHtml(field.label)}"${ph} />`;
                    }
                    const hint = field.hint ? `<div class="settings-row-hint">${this.escapeHtml(field.hint)}</div>` : '';
                    bodyHtml += `<div class="settings-row"><div class="settings-row-copy"><span class="settings-row-label">${this.escapeHtml(field.label)}</span>${hint}</div><div class="settings-row-value">${input}</div></div>`;
                }
            }

            el.innerHTML = `<h3 class="settings-section-header"><span class="settings-section-title">${this.escapeHtml(section.heading || (section.id === 'general' ? page.title : section.title))}</span></h3><div class="settings-section-body">${bodyHtml}</div>`;
            this.settingsBody.appendChild(el);
        }

        scroll.scrollTop = scrollTop;
        if (page) this._loadSettingsPage(page.id);
        if (focusId && focused !== document.getElementById(focusId)) document.getElementById(focusId)?.focus({preventScroll: true});
        document.getElementById('sonn-account-refresh')?.addEventListener('click', () => {
            this._requestSonnAccount();
            this.renderSettingsView();
        });
        this.settingsBody.querySelectorAll('[data-provider-action]').forEach(btn => {
            btn.addEventListener('click', () => {
                btn.disabled = true;
                btn.textContent = 'Connecting…';
                this.send({command: 'provider_connection', provider: btn.dataset.provider, action: btn.dataset.providerAction});
            });
        });
        // Bind change events for settings inputs
        this.settingsBody.querySelectorAll('select, input').forEach(input => {
            const eventType = input.type === 'checkbox' || input.tagName === 'SELECT' ? 'change' : 'blur';
            input.addEventListener(eventType, () => {
                const section = input.dataset.section;
                const key = input.dataset.key;
                if (!section || !key) return;
                let value;
                if (input.type === 'checkbox') {
                    value = input.checked;
                    const label = input.parentElement;
                    if (label && !label.classList.contains('settings-toggle')) label.lastChild.textContent = value ? ' On' : ' Off';
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

        this.settingsBody.querySelectorAll('.cost-refresh-btn').forEach(btn => {
            btn.addEventListener('click', () => {
                btn.disabled = true;
                btn.textContent = 'Refreshing...';
                this.send({ command: 'get_costs' });
            });
        });

        // MCP connect buttons
        this.settingsBody.querySelectorAll('[data-editor-action]').forEach(btn => {
            btn.addEventListener('click', () => {
                const editor = btn.dataset.editor;
                const action = btn.dataset.editorAction;
                const input = this.settingsBody.querySelector(`[data-editor-input="${editor}"]`);
                this._editorDrafts = this._editorDrafts || {};
                if (input) this._editorDrafts[editor] = input.value;
                this._editorBusy = editor;
                this.send({ command: action === 'check' ? 'editor_check' : 'editor_connect',
                    editor, action, value: input?.value || '' });
                btn.disabled = true;
                btn.textContent = action === 'check' ? 'Checking…' : 'Connecting…';
            });
        });
        this.settingsBody.querySelectorAll('[data-editor-input]').forEach(input => {
            input.addEventListener('input', () => {
                this._editorDrafts = this._editorDrafts || {};
                this._editorDrafts[input.dataset.editorInput] = input.value;
            });
        });
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
        this.settingsBody.querySelectorAll('.prompt-inspector-refresh').forEach(btn => {
            btn.addEventListener('click', () => {
                btn.disabled = true;
                btn.textContent = 'Loading...';
                this.send({ command: 'get_prompt_inspector' });
            });
        });
        this.settingsBody.querySelectorAll('.mcp-url-input').forEach(input => {
            input.addEventListener('change', () => {
                const serverName = input.dataset.server;
                const current = this.settings?.mcp_servers?.[serverName] || {};
                this.send({
                    command: 'update_settings',
                    section: 'mcp_servers',
                    key: serverName,
                    value: { ...current, transport: 'http', url: input.value.trim(), enabled: true },
                });
            });
        });
        this.settingsBody.querySelector('.mcp-add-http-btn')?.addEventListener('click', () => {
            const requestedName = window.prompt('MCP server name');
            if (!requestedName) return;
            const serverName = requestedName.trim().toLowerCase().replace(/[^a-z0-9_-]+/g, '-');
            if (!serverName) return;
            const url = window.prompt('Streamable HTTP MCP URL', 'http://127.0.0.1:3000/mcp');
            if (!url) return;
            this.send({
                command: 'update_settings',
                section: 'mcp_servers',
                key: serverName,
                value: { transport: 'http', url: url.trim(), enabled: true },
            });
        });
        const evaluationStart = this.settingsBody.querySelector('.evaluation-start');
        if (evaluationStart) {
            evaluationStart.addEventListener('click', () => {
                const model = this.settingsBody.querySelector('.evaluation-model')?.value || 'glm';
                const spec = this.settingsBody.querySelector('.evaluation-spec')?.value || 'minimal';
                const n = Number(this.settingsBody.querySelector('.evaluation-n')?.value || 1);
                evaluationStart.disabled = true;
                evaluationStart.textContent = 'Starting…';
                this.send({ command: 'evaluation_start', model, spec, n });
            });
        }
        this.settingsBody.querySelectorAll('.checkpoint-compare').forEach(btn => {
            btn.addEventListener('click', () => {
                btn.disabled = true;
                this.send({ command: 'checkpoint_compare', ref: btn.dataset.ref });
            });
        });
        this.settingsBody.querySelectorAll('.checkpoint-restore').forEach(btn => {
            btn.addEventListener('click', () => {
                const ref = btn.dataset.ref;
                if (!confirm('Restore this checkpoint? Your current files will be preserved on a resonant-recovery/* branch first.')) return;
                btn.disabled = true;
                btn.textContent = 'Restoring…';
                this.send({ command: 'checkpoint_restore', ref });
            });
        });
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
                { label: 'New session', keys: ['Ctrl', 'N'] },
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
        // v0.6.6 — typeahead box: live-filter the project list as you type.
        html += `<div class="psw-search-wrap"><input type="text" class="psw-search" placeholder="Filter projects…" autocomplete="off" spellcheck="false" aria-label="Filter projects" /></div>`;
        // Quick filters: every session, or only the pinned ones.
        html += itemHtml(
            '&#9776;',
            'All projects',
            'Show every session in the sidebar',
            { checked: !filter && !this._pinnedOnly, cls: 'psw-filter-all' }
        );
        html += itemHtml(
            '&#9733;',
            'Pinned',
            'Only pinned sessions',
            { checked: !!this._pinnedOnly, cls: 'psw-filter-pinned' }
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
        // v0.6.7 — anchors low in the viewport (e.g. the composer-footer
        // folder chip) would otherwise open off the bottom edge; flip the
        // menu to open upward when there isn't room below.
        const _menuH = menu.offsetHeight;
        if (rect.bottom + _menuH + 8 > window.innerHeight) {
            menu.style.top = `${Math.max(8, Math.round(rect.top - _menuH - 4))}px`;
        }
        anchor.setAttribute('aria-expanded', 'true');

        // Wire up actions by class (more robust than positional indexing).
        menu.querySelector('.psw-filter-all')?.addEventListener('click', () => {
            this._closeProjectSwitcher();
            this._setProjectFilter('');
        });
        menu.querySelector('.psw-filter-pinned')?.addEventListener('click', () => {
            this._closeProjectSwitcher();
            this._setPinnedFilter(true);
        });
        menu.querySelector('.psw-new-session')?.addEventListener('click', () => {
            this._closeProjectSwitcher();
            this.startNewSession();
        });
        menu.querySelector('.psw-open-other')?.addEventListener('click', () => {
            this._closeProjectSwitcher();
            this.openProjectFolder();
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

        // v0.6.6 — typeahead: live-filter the recent-project rows as the user
        // types. The fixed quick-filters (All / Pinned / actions) stay put; only
        // the project list narrows. Enter selects the first visible match.
        const typeahead = menu.querySelector('.psw-search');
        if (typeahead) {
            const heading = menu.querySelector('.psw-heading');
            const applyTypeahead = () => {
                const q = typeahead.value.toLowerCase().trim();
                let anyVisible = false;
                menu.querySelectorAll('.psw-project').forEach((row) => {
                    const label = (row.querySelector('.psw-label')?.textContent || '').toLowerCase();
                    const sub = (row.querySelector('.psw-sub')?.textContent || '').toLowerCase();
                    const match = !q || label.includes(q) || sub.includes(q);
                    row.style.display = match ? '' : 'none';
                    if (match) anyVisible = true;
                });
                if (heading) heading.style.display = anyVisible ? '' : 'none';
            };
            typeahead.addEventListener('input', applyTypeahead);
            typeahead.addEventListener('keydown', (e) => {
                if (e.key === 'Enter') {
                    e.preventDefault();
                    const first = [...menu.querySelectorAll('.psw-project')]
                        .find(r => r.style.display !== 'none');
                    first?.click();
                } else if (e.key === 'Escape') {
                    this._closeProjectSwitcher();
                }
            });
            // Focus immediately so the user can start typing without a click.
            setTimeout(() => { try { typeahead.focus(); } catch (e) { /* non-fatal */ } }, 0);
        }

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


    _closeProjectSwitcher() {
        const menu = document.getElementById('project-switcher-menu');
        if (!menu) return;
        if (typeof menu._cleanup === 'function') menu._cleanup();
        menu.remove();
    }


    toggleGitPopover() {
        if (this.gitPopoverOpen) {
            const existing = document.querySelector('.git-popover');
            if (existing) existing.remove();
            this.gitPopoverOpen = false;
            // Deregister the outside-click handler no matter HOW the
            // popover was closed (× button, Review re-click, outside
            // click) — a stale handler resurrects the popover on the
            // next unrelated click.
            if (this._gitPopoverOutsideHandler) {
                document.removeEventListener('click', this._gitPopoverOutsideHandler);
                this._gitPopoverOutsideHandler = null;
            }
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

        // Close on click outside. gitBadge is optional — the header badge
        // was removed in v0.6.7; the popover now opens from the Review
        // button and the command palette. The handler is stored on the
        // instance so the close branch above can always deregister it.
        setTimeout(() => {
            const handler = (e) => {
                if (!popover.contains(e.target) && !this.gitBadge?.contains(e.target)) {
                    this.toggleGitPopover();
                }
            };
            this._gitPopoverOutsideHandler = handler;
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

window.ResonantSettingsView = ResonantSettingsView;
