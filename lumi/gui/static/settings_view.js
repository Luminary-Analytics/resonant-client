/*
 * Settings and overlay surfaces for LumiApp.
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
 * Mixed into LumiApp.prototype by applyMixin in app.js — see
 * autonomous_view.js for why a prototype mixin rather than an ES module, and
 * why Object.assign would silently copy nothing here.
 *
 * Load order matters: this file must load BEFORE app.js.
 */

class LumiSettingsView {
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
        return `<p class="editor-help">Connect a running editor to work on scenes and assets from chat. Setup uses community MCP bridges. Reconnect here after restarting Lumi. Codex and Claude Code receive enabled bridges on their next Full-auto turn; other modes keep editor tools disabled in those CLIs.</p>
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
                        <p class="editor-help">The editor and its add-on must be installed separately. ${editor.id === 'blender' ? 'Connecting may download the pinned bridge using uv. ' : ''}Editor actions use the editor's filesystem access, outside Lumi's project sandbox.</p>
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


    _renderCapabilityPacks() {
        const data = this.capabilityPacks;
        if (!data) return '<p class="editor-help">Loading capability packs…</p>';
        // Pack names, descriptions and commands come from repository files, so
        // everything is escaped for attribute and text contexts alike.
        const entities = {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'};
        const esc = value => String(value ?? '').replace(/[&<>"']/g, ch => entities[ch]);
        const draft = this._packInstallDraft || {};
        const install = `<form class="pack-install" id="pack-install-form" novalidate>
                <h4>Install from Git</h4>
                <p class="editor-help">A public https repository with a lumi-pack.json, pinned to one commit. A tag or branch is resolved to the commit it names now. The pack installs turned off; review what it would run below, then approve it.</p>
                <div class="pack-install-grid">
                    <label for="pack-install-url"><span>Repository</span><input class="settings-input" id="pack-install-url" data-pack-install="url" value="${esc(draft.url || '')}" placeholder="https://github.com/owner/repo" spellcheck="false" autocomplete="off"></label>
                    <label for="pack-install-ref"><span>Commit, tag or branch</span><input class="settings-input" id="pack-install-ref" data-pack-install="ref" value="${esc(draft.ref || '')}" placeholder="v1.2.0" spellcheck="false" autocomplete="off"></label>
                    <label for="pack-install-subdir"><span>Folder (optional)</span><input class="settings-input" id="pack-install-subdir" data-pack-install="subdir" value="${esc(draft.subdir || '')}" placeholder="packs/review" spellcheck="false" autocomplete="off"></label>
                </div>
                <div class="editor-actions"><button type="submit" class="btn-sm"${this._packInstalling ? ' disabled' : ''}>${this._packInstalling ? 'Installing…' : 'Install'}</button></div>
            </form>`;
        const intro = '<p class="editor-help">A capability pack can add lifecycle hooks (shell commands), MCP servers, skills and agents. Nothing in a pack runs until you approve it here; a pack cannot approve itself. An approval covers this pack at this location with exactly the files it has now. If any of them change, the pack turns off until you review it again.</p>'
            + (data.error ? `<p class="editor-error" role="alert">${esc(data.error)}</p>` : '') + install;
        const packs = Array.isArray(data.packs) ? data.packs : [];
        if (!packs.length) {
            return `${intro}<div class="settings-row"><span class="settings-row-label" style="color:var(--dim)">No capability packs in this project's .lumi/packs or in ~/.lumi/packs.</span></div>`;
        }
        const statusText = {
            approved: 'Approved · active',
            disabled: 'Approved · disabled',
            needs_approval: 'Not approved · off',
            changed: 'Changed since approval · off',
            unverifiable: 'Cannot be verified · off',
        };
        return `${intro}<div class="pack-list">${packs.map(pack => {
            const hooks = (pack.hooks || []).map(hook => {
                const target = hook.matcher || hook.tool_name;
                return `<li><code>${esc(hook.hook_type || 'pre_tool_use')}${target ? ` · ${esc(target)}` : ''}</code><pre class="pack-command">${esc(hook.command)}</pre></li>`;
            }).join('');
            const servers = Object.entries(pack.mcp_servers || {}).map(([name, config]) => {
                const args = Array.isArray(config?.args) ? config.args : [];
                const endpoint = config?.url || [config?.command, ...args].filter(Boolean).join(' ');
                return `<li><code>${esc(name)}</code><pre class="pack-command">${esc(endpoint)}</pre></li>`;
            }).join('');
            const pinned = (pack.pinned_files || []).map(file => `<li><code>${esc(file)}</code></li>`).join('');
            const canApprove = Boolean(pack.digest) && ['needs_approval', 'changed', 'disabled'].includes(pack.status);
            const canRevoke = ['approved', 'disabled', 'changed'].includes(pack.status);
            const target = `data-pack-id="${esc(pack.id)}" data-pack-path="${esc(pack.path)}"`;
            return `<article class="pack-card status-${esc(pack.status)}" aria-label="${esc(pack.name)} capability pack">
                <div class="pack-card-head"><h3>${esc(pack.name)} <small>v${esc(pack.version)}</small></h3><span class="pack-status" role="status">${esc(statusText[pack.status] || pack.status)}</span></div>
                ${pack.description ? `<p class="editor-help">${esc(pack.description)}</p>` : ''}
                <p class="pack-meta">${pack.scope === 'project' ? 'From this repository' : 'Personal pack'} · <code>${esc(pack.path)}</code></p>
                ${pack.source?.type === 'git' ? `<p class="pack-meta">Installed from <code>${esc(pack.source.url)}</code>${pack.source.subdir ? ` (<code>${esc(pack.source.subdir)}</code>)` : ''} at commit <code>${esc(String(pack.source.commit || '').slice(0, 12))}</code></p>` : ''}
                ${pack.problem ? `<p class="editor-error">${esc(pack.problem)}</p>` : ''}
                <details ${pack.status === 'approved' ? '' : 'open'}><summary>What this pack would run</summary>
                    ${hooks ? `<h4>Hooks (shell commands)</h4><ul>${hooks}</ul>` : '<p class="editor-help">No hooks.</p>'}
                    ${servers ? `<h4>MCP servers</h4><ul>${servers}</ul>` : '<p class="editor-help">No MCP servers.</p>'}
                    ${pinned ? `<h4>Repository files its commands run</h4><ul>${pinned}</ul>` : ''}
                    <p class="editor-help">${(pack.agents || []).length} agents · ${(pack.skills || []).length} skills · content digest <code>${esc((pack.digest || '').slice(0, 12))}</code></p>
                </details>
                <div class="editor-actions">
                    ${canApprove ? `<button type="button" class="btn-sm" data-pack-action="approve" ${target} data-pack-digest="${esc(pack.digest)}">Approve and enable</button>` : ''}
                    ${canRevoke ? `<button type="button" class="btn-sm" data-pack-action="revoke" ${target}>Revoke approval</button>` : ''}
                    ${pack.source?.type === 'git' ? `<button type="button" class="btn-sm" data-pack-action="remove" ${target}>Remove</button>` : ''}
                </div>
            </article>`;
        }).join('')}</div>`;
    }

    _renderAuditStatus() {
        const status = this.auditStatus;
        if (!status) return '<p class="editor-help">Loading…</p>';
        const esc = value => this.escapeHtml(String(value ?? ''));
        const chain = !status.verified
            ? `Verification failed: ${esc(status.problem)}`
            : status.records
                ? `Verified: ${esc(status.records)} records, none changed or out of order.`
                : 'No records yet.';
        const exported = status.export
            ? `${esc(status.export.sent)} sent, ${esc(status.export.queued)} waiting, ${esc(status.export.dropped)} dropped${status.export.last_error ? ` · last error ${esc(status.export.last_error)}` : ''}`
            : 'Not exporting';
        const where = status.enabled === false
            ? `The audit log is off. Records already in <code>${esc(status.path)}</code> stay until their retention ends.`
            : `<code>${esc(status.path)}</code>`;
        return `<div class="settings-row"><div class="settings-row-copy"><span class="settings-row-label">Records</span><div class="settings-row-hint">${where}</div></div></div>
            <div class="settings-row"><div class="settings-row-copy"><span class="settings-row-label">Hash chain</span><div class="settings-row-hint${status.verified ? '' : ' editor-error'}" role="status">${chain}</div></div>
                <div class="settings-row-value"><button type="button" class="btn-sm" id="audit-verify">Verify again</button></div></div>
            <div class="settings-row"><div class="settings-row-copy"><span class="settings-row-label">OpenTelemetry export</span><div class="settings-row-hint">${exported}</div></div></div>`;
    }

    _renderAbout() {
        const info = this.aboutInfo;
        if (!info) return '<p class="editor-help">Loading…</p>';
        const esc = value => this.escapeHtml(String(value ?? ''));
        const managed = info.installed_by === 'msi'
            ? 'Installed by your organization’s device management.'
            : info.organization ? `Managed by ${esc(info.organization)}.` : '';
        const row = (label, body) => `<div class="settings-row"><div class="settings-row-copy"><span class="settings-row-label">${label}</span><div class="settings-row-hint">${body}</div></div></div>`;
        return [
            row(`Lumi ${esc(info.version)}`, `The coding agent by Luminary Analytics. ${managed}`),
            row('Free for individuals', 'The whole agent, every tool, provider and feature in this app, works without an account: with your own API keys, your ChatGPT sign-in, or models on your own computer.'),
            row('What leaves this computer', 'Your prompts, code and keys go only to the model providers you choose. Luminary Analytics receives only the update check, which you can turn off in Updates.'),
            row('For teams and organizations', 'Lumi Cloud adds central policy, members, devices and usage reporting; if your organization uses it, sign in from Lumi account. Without it, organizations set policy on each computer (see docs/enterprise-policy.md).'),
            row('License', `Lumi’s source is available under the ${esc(info.license)} license.${info.notices ? ` Third-party components and their licenses: <code>${esc(info.notices)}</code>` : ''}`),
        ].join('');
    }

    _renderLumiAccount() {
        const s = this.cloudStatus;
        if (!s) return '<p class="editor-help">Loading…</p>';
        const esc = value => this.escapeHtml(String(value ?? ''));
        const row = (label, hint, value = '') => `<div class="settings-row"><div class="settings-row-copy"><span class="settings-row-label">${label}</span><div class="settings-row-hint">${hint}</div></div>${value ? `<div class="settings-row-value">${value}</div>` : ''}</div>`;
        const button = (action, label, extra = '') => `<button type="button" class="btn-sm" data-cloud-action="${action}" ${extra}>${label}</button>`;
        const parts = [];
        if (s.error) parts.push(`<p class="editor-error" role="alert">${esc(s.error)}</p>`);
        if (s.cloud_error) parts.push(`<p class="editor-error" role="alert">${esc(s.cloud_error)}</p>`);
        const device = s.device && s.device.id ? s.device : null;
        if (s.signing_in) {
            parts.push(row('Signing in…', 'Finish signing in in your browser. This page updates when you’re done.', button('cancel', 'Cancel')));
        } else if (s.signed_in && s.account && s.account.email) {
            parts.push(row(`Signed in as ${esc(s.account.email)}`,
                `Lumi Cloud at <code>${esc(s.url)}</code>. This account is separate from your display name and from SONN or ChatGPT.`,
                `${button('refresh', 'Refresh')} ${button('sign_out', 'Sign out')}`));
            const orgs = Array.isArray(s.account.organizations) ? s.account.organizations : [];
            if (!orgs.length) parts.push(row('Organizations', 'You aren’t in an organization yet. Accept an invitation, or create one in Lumi Cloud.'));
            for (const org of orgs) {
                const here = device && device.organization_id === org.id;
                const canEnroll = !device && org.has_seat && !s.managed_organization;
                const note = here ? 'This computer uses its policy.'
                    : !org.has_seat ? 'You don’t have a seat. Ask an administrator for one.'
                    : device ? `This computer is enrolled in ${esc(device.organization_name)}.`
                    : 'Use it on this computer to apply its policy here.';
                parts.push(row(esc(org.name), `${esc(org.role)} · ${note}`,
                    canEnroll ? button('enroll', 'Use on this computer', `data-org="${esc(org.id)}" aria-label="Use ${esc(org.name)} on this computer"`) : ''));
            }
        } else {
            const address = this._cloudUrlDraft ?? s.url ?? '';
            const hint = s.url_locked ? 'Set by your organization’s policy.' : 'Your organization’s Lumi Cloud, such as https://cloud.example.com.';
            parts.push(`<div class="settings-row"><div class="settings-row-copy"><label class="settings-row-label" for="cloud-url">Lumi Cloud address</label><div class="settings-row-hint">${hint}</div></div>
                <div class="settings-row-value"><input id="cloud-url" type="url" class="settings-input" value="${esc(address)}" placeholder="https://" autocomplete="url"${s.url_locked ? ' readonly' : ''}> ${button('sign_in', 'Sign in with your browser')}</div></div>`);
        }
        if (device) {
            const how = device.how === 'managed' ? 'managed by your organization' : 'joined in this app';
            const seen = s.last_checkin ? new Date(s.last_checkin).toLocaleString() : 'not yet';
            const version = s.policy_version ? `policy version ${esc(s.policy_version)} is in force` : 'no policy is published yet';
            parts.push(row(`This computer: ${esc(device.organization_name)}`,
                `Enrolled, ${how}. Last check-in: ${esc(seen)}; ${version}.`,
                `${button('check_in', 'Check in now')}${device.how === 'managed' ? '' : ` ${button('unenroll', 'Leave on this computer')}`}`));
        } else if (s.managed_organization) {
            parts.push(row('This computer', 'Your organization’s policy enrolls this computer in Lumi Cloud automatically.'));
        }
        const remote = s.remote_tasks;
        if (device && device.how !== 'managed' && remote) {
            const draft = this._remoteTasksDraft || {project: remote.project || this.currentCwd || '', mode: remote.mode || 'ask'};
            const modes = {ask: 'Ask before changes', 'auto-edit': 'Edit files, ask about the rest', bypass: 'Ask about nothing'};
            const state = !remote.enabled ? 'Off.'
                : remote.blocked ? esc(remote.blocked)
                : remote.running ? 'On. Running a request now.'
                : 'On. Checks for your requests every 20 seconds while Lumi is open.';
            const last = remote.last && remote.last.status
                ? ` Last request: ${esc(remote.last.status)}, ${esc(new Date(remote.last.at * 1000).toLocaleString())}.` : '';
            const options = Object.entries(modes).map(([value, label]) =>
                `<option value="${value}"${draft.mode === value ? ' selected' : ''}>${label}</option>`).join('');
            parts.push(`<div class="settings-row" data-remote-tasks><div class="settings-row-copy"><span class="settings-row-label" id="remote-tasks-label">Tasks from Slack and Teams</span>
                <div class="settings-row-hint">Requests you send to Lumi in your organization’s Slack or Microsoft Teams run here, one at a time, in this project with your default model. Lumi asks you in the chat before any action the mode doesn’t allow.</div>
                <div class="settings-row-hint" role="status">${state}${last}</div></div>
                <div class="settings-row-value"><label class="settings-toggle"><input type="checkbox" id="remote-tasks-enabled" aria-labelledby="remote-tasks-label"${remote.enabled ? ' checked' : ''}><span class="settings-toggle-track" aria-hidden="true"></span></label></div></div>
                <div class="settings-row"><div class="settings-row-copy"><label class="settings-row-label" for="remote-tasks-project">Project folder</label></div>
                <div class="settings-row-value"><input id="remote-tasks-project" class="settings-input" value="${esc(draft.project)}" spellcheck="false"></div></div>
                <div class="settings-row"><div class="settings-row-copy"><label class="settings-row-label" for="remote-tasks-mode">Permission mode</label></div>
                <div class="settings-row-value"><select id="remote-tasks-mode" class="settings-select">${options}</select> ${button('remote_tasks', 'Save')}</div></div>`);
        }
        parts.push('<p class="editor-help">An enrolled computer checks in hourly with its Lumi version, the policy in force and usage totals per model (requests, tokens and cost). Prompts, code and file names never go to Lumi Cloud.</p>');
        return parts.join('');
    }

    _bindLumiAccount() {
        const section = this.settingsBody?.querySelector('[data-settings-section="lumi_account"]');
        if (!section) return;
        section.querySelector('#cloud-url')?.addEventListener('input', event => { this._cloudUrlDraft = event.target.value; });
        section.querySelector('#cloud-url')?.addEventListener('keydown', event => {
            if (event.key === 'Enter') section.querySelector('[data-cloud-action="sign_in"]')?.click();
        });
        const remoteDraft = () => {
            this._remoteTasksDraft = {project: section.querySelector('#remote-tasks-project')?.value || '',
                                      mode: section.querySelector('#remote-tasks-mode')?.value || 'ask'};
        };
        section.querySelector('#remote-tasks-project')?.addEventListener('input', remoteDraft);
        section.querySelector('#remote-tasks-mode')?.addEventListener('change', remoteDraft);
        // The switch saves at once, with the folder and mode as they stand.
        section.querySelector('#remote-tasks-enabled')?.addEventListener('change', () => {
            section.querySelector('[data-cloud-action="remote_tasks"]')?.click();
        });
        section.querySelectorAll('[data-cloud-action]').forEach(control => control.addEventListener('click', () => {
            const action = control.dataset.cloudAction;
            const organization = this.cloudStatus?.device?.organization_name || 'the organization';
            if (action === 'unenroll' && !window.confirm(`Leave ${organization} on this computer? Its policy stops applying here.`)) return;
            control.disabled = true;
            const message = {command: `cloud_${action}`};
            if (action === 'sign_in') message.url = (section.querySelector('#cloud-url')?.value || '').trim();
            if (action === 'enroll') message.organization_id = control.dataset.org;
            if (action === 'remote_tasks') {
                message.enabled = Boolean(section.querySelector('#remote-tasks-enabled')?.checked);
                message.project = (section.querySelector('#remote-tasks-project')?.value || '').trim();
                message.mode = section.querySelector('#remote-tasks-mode')?.value || 'ask';
                // Kept, so a refused folder stays in the field beside the error.
                this._remoteTasksDraft = {project: message.project, mode: message.mode};
            }
            this.send(message);
        }));
    }

    /** Redraw the Lumi account section in place; typing in the address field is never interrupted. */
    refreshLumiAccount() {
        const body = this.settingsBody?.querySelector('[data-settings-section="lumi_account"] .settings-section-body');
        if (!body) return false;
        // Don't pull the address field out from under someone typing in it. Once
        // signing in starts (Enter keeps focus there) the field is gone anyway.
        const s = this.cloudStatus || {};
        const addressStays = !s.signing_in && !(s.signed_in && s.account?.email);
        if (addressStays && document.activeElement?.id === 'cloud-url') return true;
        // Nor the task folder or mode while they're being edited (the draft is kept either way).
        if (['remote-tasks-project', 'remote-tasks-mode'].includes(document.activeElement?.id)) return true;
        body.innerHTML = this._renderLumiAccount();
        this._bindLumiAccount();
        return true;
    }

    _bindUpdateCheck() {
        document.getElementById('update-check')?.addEventListener('click', () => {
            this.showStatusMessage('Checking for updates...');
            this.send({command: 'check_updates'});
        });
    }

    /** Redraw only the status block, so it stays current while a field keeps focus. */
    refreshUpdateStatus() {
        const body = this.settingsBody?.querySelector('[data-settings-section="update_status"] .settings-section-body');
        if (!body) return false;
        body.innerHTML = this._renderUpdateStatus();
        this._bindUpdateCheck();
        return true;
    }

    _renderUpdateStatus() {
        const status = this.updateStatus;
        if (!status) return '<p class="editor-help">Loading…</p>';
        const esc = value => this.escapeHtml(String(value ?? ''));
        const modes = { automatic: 'Automatic', manual: 'Only when you check', off: 'Off' };
        const describe = s => `${esc(modes[s.mode] || s.mode)}${s.mode === 'off' ? '' : `, from ${esc(s.describe)}`}`;
        const managed = status.installed_by === 'msi'
            ? ' · installed from the MSI package, so your organization’s device management updates it'
            : status.managed_by ? ` · managed by ${esc(status.managed_by)}` : '';
        const checking = status.mode !== 'off' && status.available;
        const hints = [
            status.mode !== 'off' && !status.available ? 'This copy of Lumi doesn’t update itself: it runs from source or outside Windows.' : '',
            ...(status.problems || []),
        ].filter(Boolean).map(text => `<div class="settings-row-hint">${esc(text)}</div>`).join('');
        const lastCheck = status.last_check ? new Date(status.last_check * 1000).toLocaleString() : 'Not yet';
        return `<div class="settings-row"><div class="settings-row-copy"><span class="settings-row-label">Lumi ${esc(status.version)}</span><div class="settings-row-hint">${describe(status)}${managed}</div>${hints}</div>
                <div class="settings-row-value"><button type="button" class="btn-sm" id="update-check"${checking ? '' : ' disabled'}>Check for updates</button></div></div>
            ${checking ? `<div class="settings-row"><div class="settings-row-copy"><span class="settings-row-label">Last checked</span><div class="settings-row-hint">${esc(lastCheck)}</div></div></div>` : ''}
            ${status.pending ? `<div class="settings-row"><div class="settings-row-copy"><span class="settings-row-label">After Lumi restarts</span><div class="settings-row-hint" role="status">${describe(status.pending)}</div></div></div>` : ''}`;
    }

    _renderOrgPolicy() {
        const meta = this.settings?._meta?.policy || {};
        const esc = value => this.escapeHtml(String(value ?? ''));
        if (meta.error) {
            return `<p class="editor-error" role="alert">${esc(meta.error)} Lumi won’t send model requests until it’s fixed.</p>`;
        }
        const policy = meta.summary;
        if (!policy) {
            return '<p class="editor-help">No organization policy is installed on this computer. An administrator can set one with Group Policy, a configuration profile or a policy file; see docs/enterprise-policy.md.</p>';
        }
        const list = (items, none) => items === null || items === undefined ? none
            : items.length ? items.map(item => `<code>${esc(item)}</code>`).join(', ') : 'none';
        const expiry = !policy.expires_at ? 'Doesn’t expire'
            : policy.expiry === 'grace' ? `Expired ${esc(policy.expires_at)}; still enforced during the offline grace period`
            : `Valid until ${esc(policy.expires_at)}`;
        const rows = [
            ['Managed by', `${esc(policy.organization)}${policy.signed ? ' · signed' : ''}`],
            ['Source', esc(policy.source)],
            ['Validity', expiry],
            ['Locked settings', list(policy.locked_settings, 'none')],
            ['Permission modes', list(policy.allowed_modes, 'all')],
            ['Models allowed', list(policy.models_allowed, 'all')],
            ['Models blocked', list(policy.models_blocked, 'none')],
            ['Zero data retention', policy.require_zero_retention ? `Required: local models, connections marked zero retention${(policy.zero_retention_providers || []).length ? ', and ' + esc(policy.zero_retention_providers.join(', ')) : ''}` : 'Not required'],
            ['Files never read', list(policy.exclude, 'none')],
            ['Shell rules', String(policy.shell_rules || 0)],
            ['MCP servers', `${list(policy.mcp_allowed, 'all')}${policy.mcp_allow_stdio ? '' : ' · command-based servers off'}`],
            ['Capability packs', list(policy.packs_allowed, 'all')],
        ];
        return rows.map(([label, value]) => `<div class="settings-row"><div class="settings-row-copy"><span class="settings-row-label">${label}</span></div><div class="settings-row-value settings-policy-value">${value}</div></div>`).join('');
    }

    _renderModelComparisons() {
        const esc = value => this.escapeHtml(String(value ?? ''));
        const data = this.modelEvals;
        if (!data) return '<p class="editor-help">Loading…</p>';
        const money = value => typeof value === 'number' ? `$${value.toFixed(value < 1 ? 4 : 2)}` : '—';
        const statusText = {ready: 'Not run yet', running: 'Running…', done: 'Finished', stopped: 'Stopped',
            failed: 'Failed', interrupted: 'Interrupted (Lumi closed)'};
        const items = (data.items || []).map(item => {
            const running = data.running === item.id;
            const table = (item.summary || []).map(row => `<tr><th scope="row"><code>${esc(row.model)}</code></th>
                <td>${row.passed}/${row.tasks}</td><td>${money(row.cost_usd)}</td>
                <td>${row.median_seconds == null ? '—' : `${esc(row.median_seconds)}s`}</td></tr>`).join('');
            const details = (item.tasks || []).map((task, index) => {
                const cells = (item.results || []).filter(r => r.task === index).map(r => `<li>
                    <span class="${r.passed ? 'eval-pass' : 'eval-fail'}">${r.passed ? 'Passed' : 'Failed'}</span>
                    <code>${esc(r.model)}</code> · ${esc(r.status)}${typeof r.elapsed === 'number' ? ` · ${esc(r.elapsed)}s` : ''} · ${money(r.cost_usd)} · ${esc(r.changed_files)} file${r.changed_files === 1 ? '' : 's'} changed
                    ${r.error ? `<div class="settings-row-hint">${esc(String(r.error).slice(0, 300))}</div>` : ''}
                    ${r.check_output ? `<pre class="eval-check-output">${esc(r.check_output)}</pre>` : ''}</li>`).join('');
                return `<details class="eval-task"><summary>Task ${index + 1}: ${esc(task.prompt.split('\n')[0].slice(0, 120))}</summary>
                    <div class="settings-row-hint">Check: <code>${esc(task.check)}</code></div>
                    ${cells ? `<ul class="eval-results">${cells}</ul>` : '<p class="settings-row-hint">No runs yet.</p>'}</details>`;
            }).join('');
            const id = esc(item.id);
            const action = running
                ? `<button type="button" class="btn-sm" id="eval-stop-${id}" data-eval-action="stop" data-eval-id="${id}">Stop</button>`
                : `<button type="button" class="btn-sm" id="eval-run-${id}" data-eval-action="run" data-eval-id="${id}" ${data.running ? 'aria-disabled="true"' : ''}>${item.results?.length ? 'Run again' : 'Run'}</button>`;
            const progress = running ? ` · ${(item.results || []).length} of ${item.tasks.length * item.models.length} runs` : '';
            return `<div class="settings-row eval-row">
                <div class="settings-row-copy"><span class="settings-row-label">${esc(item.name)}</span>
                    <div class="settings-row-hint">${esc(statusText[item.status] || item.status)}${progress} · ${item.tasks.length} task${item.tasks.length === 1 ? '' : 's'} · ${esc(item.mode)} · up to ${esc(item.max_minutes)} min a run · <code>${esc(item.project)}</code></div>
                    ${item.error ? `<div class="settings-row-hint">${esc(item.error)}</div>` : ''}
                    <table class="eval-summary"><thead><tr><th scope="col">Model</th><th scope="col">Passed</th><th scope="col">Cost</th><th scope="col">Median time</th></tr></thead><tbody>${table}</tbody></table>
                    ${details}
                </div>
                <div class="settings-row-value schedule-actions">${action}
                    <button type="button" class="btn-sm" id="eval-remove-${id}" data-eval-action="remove" data-eval-id="${id}" aria-label="Remove ${esc(item.name)}" ${running ? 'aria-disabled="true"' : ''}>Remove</button>
                </div></div>`;
        }).join('');
        const draft = this._evalDraft || (this._evalDraft = {
            name: '', project: this.currentCwd || '', models: [], tasks: [{prompt: '', check: ''}], mode: 'auto-edit',
            max_minutes: 10});
        const labels = this._getBackendLabels ? this._getBackendLabels() : {};
        const modelBoxes = Object.entries(this.backends || {})
            .filter(([, info]) => (info?.models || []).length)
            .map(([backend, info]) => `<fieldset class="eval-model-group"><legend>${esc(labels[backend] || backend)}</legend>${info.models.map(model => {
                const value = `${backend}:${model}`;
                return `<label><input type="checkbox" name="models" value="${esc(value)}" ${draft.models.includes(value) ? 'checked' : ''}> ${esc(model)}</label>`;
            }).join('')}</fieldset>`).join('');
        const taskRows = draft.tasks.map((task, index) => `<div class="eval-task-row" data-task-row="${index}">
                <label>Task ${index + 1} <textarea class="settings-input" id="eval-task-${index}" data-task-field="prompt" rows="2" placeholder="Fix the failing test in tests/test_api.py">${esc(task.prompt)}</textarea></label>
                <label>Check (passes with exit code 0) <input class="settings-input" id="eval-check-${index}" data-task-field="check" value="${esc(task.check)}" placeholder="python -m pytest tests/test_api.py -q" spellcheck="false"></label>
                ${draft.tasks.length > 1 ? `<button type="button" class="btn-sm" data-remove-task="${index}" aria-label="Remove task ${index + 1}">Remove task</button>` : ''}
            </div>`).join('');
        const error = this.modelEvalError ? `<div class="settings-error-banner" role="alert">${esc(this.modelEvalError)}</div>` : '';
        const modes = {ask: 'Read only (ask)', 'auto-edit': 'Edit files (auto-edit)', bypass: 'Everything (bypass)'};
        return `<p class="editor-help">Run the same tasks with each model and see which passes your checks, what it costs and how long it takes. Each run starts from the project's last commit in its own copy, so it never touches your work, and is an unattended <code>lumi run</code>: nobody is asked anything, your organization's policy and budgets apply, and its requests count in Usage &amp; cost.</p>
            ${items || '<p class="editor-help">No comparisons yet.</p>'}
            <h4 class="settings-subheading">New comparison</h4>
            ${error}
            <form class="schedule-form eval-form" data-eval-form>
                <label>Name <input class="settings-input" id="eval-name" name="name" maxlength="80" required value="${esc(draft.name)}" placeholder="Test fixes: default vs. cheaper model"></label>
                <label>Project (a git repository) <input class="settings-input" id="eval-project" name="project" value="${esc(draft.project)}" required spellcheck="false"></label>
                <div class="eval-models"><span class="eval-models-label">Models to compare (2 to 6)</span>${modelBoxes || '<p class="settings-row-hint">No models are available yet.</p>'}</div>
                ${taskRows}
                <div><button type="button" class="btn-sm" data-add-task ${draft.tasks.length >= 20 ? 'disabled' : ''}>Add task</button></div>
                <div class="schedule-when">
                    <label>What it may do <select class="settings-select" id="eval-mode" name="mode">${Object.entries(modes).map(([value, label]) => `<option value="${value}" ${draft.mode === value ? 'selected' : ''}>${label}</option>`).join('')}</select></label>
                    <label>Stop each run after (minutes) <input class="settings-input" id="eval-minutes" name="max_minutes" type="number" min="1" max="60" value="${esc(draft.max_minutes)}" required></label>
                </div>
                <div><button type="submit" class="btn-sm">Create comparison</button></div>
            </form>`;
    }

    _bindModelComparisons() {
        const root = this.settingsBody;
        root?.querySelectorAll('[data-eval-action]').forEach(button => button.addEventListener('click', () => {
            if (button.getAttribute('aria-disabled') === 'true') return;
            const action = button.dataset.evalAction;
            if (action === 'remove' && !window.confirm('Remove this comparison and its results?')) return;
            button.setAttribute('aria-disabled', 'true');
            this.modelEvalError = '';
            this.send({command: 'model_eval_change', id: button.dataset.evalId, action});
        }));
        const form = root?.querySelector('[data-eval-form]');
        if (!form) return;
        const read = () => {
            const fields = new FormData(form);
            const tasks = [...form.querySelectorAll('[data-task-row]')].map(row => ({
                prompt: row.querySelector('[data-task-field="prompt"]').value,
                check: row.querySelector('[data-task-field="check"]').value}));
            this._evalDraft = {name: fields.get('name') || '', project: fields.get('project') || '',
                models: fields.getAll('models'), tasks, mode: fields.get('mode') || 'auto-edit',
                max_minutes: fields.get('max_minutes') || ''};
            return this._evalDraft;
        };
        form.addEventListener('input', read);
        form.addEventListener('change', read);
        form.querySelector('[data-add-task]')?.addEventListener('click', () => {
            const draft = read();
            draft.tasks.push({prompt: '', check: ''});
            this.renderSettingsView({force: true});
            document.getElementById(`eval-task-${draft.tasks.length - 1}`)?.focus();
        });
        form.querySelectorAll('[data-remove-task]').forEach(button => button.addEventListener('click', () => {
            const draft = read();
            draft.tasks.splice(Number(button.dataset.removeTask), 1);
            this.renderSettingsView({force: true});
        }));
        form.addEventListener('submit', event => {
            event.preventDefault();
            const draft = read();
            this.modelEvalError = '';
            form.querySelector('[type="submit"]').disabled = true;
            this.send({command: 'model_eval_save', comparison: {...draft, max_minutes: Number(draft.max_minutes)}});
        });
    }

    _renderScheduledTasks() {
        const esc = value => this.escapeHtml(String(value ?? ''));
        const data = this.schedules;
        if (!data) return '<p class="editor-help">Loading…</p>';
        const days = ['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun'];
        const modes = {ask: 'Read only (ask)', 'auto-edit': 'Edit files (auto-edit)', bypass: 'Everything (bypass)'};
        const when = value => esc(String(value || '').replace('T', ' ').replace(/\+00:00$/, ' UTC'));
        const rows = (data.items || []).map(item => {
            const last = item.last_run || {};
            const cost = typeof last.cost_usd === 'number' ? ` · $${last.cost_usd.toFixed(2)}` : '';
            const status = item.running ? 'Running now'
                : last.started_at ? `Last run ${when(last.started_at)}: ${esc(last.status)}${cost}`
                : 'Not run yet';
            const model = item.model ? esc(item.provider ? `${item.provider}: ${item.model}` : item.model) : 'default model';
            const changed = Array.isArray(last.changed_files) && last.changed_files.length && !item.running
                ? ` · changed ${esc(last.changed_files.slice(0, 3).join(', '))}${last.changed_files.length > 3 ? ` and ${last.changed_files.length - 3} more` : ''}` : '';
            const failure = (last.error && !item.running ? ` · ${esc(String(last.error).slice(0, 200))}` : '') + changed;
            const answer = last.answer && !item.running
                ? `<details class="schedule-answer"><summary>Last answer</summary><div class="schedule-answer-body message-content" data-plain>${esc(last.answer)}</div></details>` : '';
            const name = esc(item.name);
            const id = esc(item.id);
            return `<div class="settings-row schedule-row">
                <div class="settings-row-copy"><span class="settings-row-label">${name}${item.enabled ? '' : ' <span class="connection-badge">Paused</span>'}</span>
                <div class="settings-row-hint">${esc(item.when)} · ${esc(modes[item.mode] || item.mode)} · ${model} · up to ${esc(item.max_minutes)} min</div>
                <div class="settings-row-hint"><code>${esc(item.project)}</code></div>
                <div class="settings-row-hint">${status}${failure}</div>
                ${answer}</div>
                <div class="settings-row-value schedule-actions">
                    <button type="button" class="btn-sm" id="schedule-run-${id}" data-schedule-action="run" data-schedule-id="${id}" aria-disabled="${item.running ? 'true' : 'false'}" aria-label="${item.running ? `${name} is running` : `Run ${name} now`}">${item.running ? 'Running…' : 'Run now'}</button>
                    <button type="button" class="btn-sm" id="schedule-toggle-${id}" data-schedule-action="${item.enabled ? 'pause' : 'resume'}" data-schedule-id="${id}" aria-label="${item.enabled ? 'Pause' : 'Resume'} ${name}">${item.enabled ? 'Pause' : 'Resume'}</button>
                    <button type="button" class="btn-sm" id="schedule-remove-${id}" data-schedule-action="remove" data-schedule-id="${id}" aria-label="Remove ${name}">Remove</button>
                </div></div>`;
        }).join('');
        // What's typed survives the list refreshing (a run finishing) and a failed save.
        const draft = this._scheduleDraft || (this._scheduleDraft = {
            name: '', prompt: '', project: this.currentCwd || '', time: '02:00', days: [], mode: 'ask',
            model: '', max_minutes: 60});
        const labels = this._getBackendLabels ? this._getBackendLabels() : {};
        const modelOptions = Object.entries(this.backends || {})
            .filter(([, info]) => (info?.models || []).length)
            .map(([backend, info]) => `<optgroup label="${esc(labels[backend] || backend)}">${info.models.map(model => {
                const value = `${backend}|${model}`;
                return `<option value="${esc(value)}" ${draft.model === value ? 'selected' : ''}>${esc(model)}</option>`;
            }).join('')}</optgroup>`).join('');
        const error = this.scheduleError ? `<div class="settings-error-banner" role="alert">${esc(this.scheduleError)}</div>` : '';
        const off = data.turned_off ? `<div class="settings-error-banner">${esc(data.turned_off)} Schedules can still be paused or removed.</div>` : '';
        const dayBoxes = days.map(day => `<label><input type="checkbox" name="days" value="${day}" ${draft.days.includes(day) ? 'checked' : ''}> ${day.charAt(0).toUpperCase() + day.slice(1)}</label>`).join('');
        const modeOptions = Object.entries(modes).map(([value, label]) => `<option value="${value}" ${draft.mode === value ? 'selected' : ''}>${label}</option>`).join('');
        return `<p class="editor-help">A saved task that runs at set times through ${esc(data.scheduler)} (this computer's time), even when Lumi is closed. Each run is an unattended <code>lumi run</code>: your organization's policy and budgets apply, repository instructions apply only if you trust the project, and nobody is asked anything, so choose what it may do with care.</p>
            ${off}
            ${rows || '<p class="editor-help">No schedules yet.</p>'}
            <h4 class="settings-subheading">Add a schedule</h4>
            ${error}
            <form class="schedule-form" data-schedule-form>
                <label>Name <input class="settings-input" id="schedule-name" name="name" maxlength="80" required value="${esc(draft.name)}" placeholder="Nightly dependency check"></label>
                <label>Task <textarea class="settings-input" id="schedule-prompt" name="prompt" rows="3" required placeholder="Check for outdated dependencies and summarize what to update.">${esc(draft.prompt)}</textarea></label>
                <label>Project folder <input class="settings-input" id="schedule-project" name="project" value="${esc(draft.project)}" required spellcheck="false"></label>
                <div class="schedule-when">
                    <label>Time <input class="settings-input" id="schedule-time" name="time" type="time" value="${esc(draft.time)}" required></label>
                    <label>Stop after (minutes) <input class="settings-input" id="schedule-minutes" name="max_minutes" type="number" min="1" max="720" value="${esc(draft.max_minutes)}" required></label>
                </div>
                <fieldset class="schedule-days"><legend>Days (none: every day)</legend>${dayBoxes}</fieldset>
                <label>What it may do <select class="settings-select" id="schedule-mode" name="mode">${modeOptions}</select></label>
                <label>Model <select class="settings-select" id="schedule-model" name="model"><option value="">Your default model</option>${modelOptions}</select></label>
                <div><button type="submit" class="btn-sm">Add schedule</button></div>
            </form>`;
    }

    _bindScheduledTasks() {
        const root = this.settingsBody;
        // The answer is model output: Markdown through the chat's sanitizer,
        // or left as plain text when either library is missing.
        if (typeof marked !== 'undefined' && typeof DOMPurify !== 'undefined') {
            root?.querySelectorAll('.schedule-answer-body[data-plain]').forEach(body => {
                body.innerHTML = DOMPurify.sanitize(marked.parse(body.textContent));
                body.removeAttribute('data-plain');
            });
        }
        root?.querySelectorAll('[data-schedule-action]').forEach(button => button.addEventListener('click', () => {
            const action = button.dataset.scheduleAction;
            // aria-disabled rather than disabled, so keyboard focus stays put.
            if (button.getAttribute('aria-disabled') === 'true') return;
            if (action === 'remove' && !window.confirm('Remove this schedule and its kept results?')) return;
            button.setAttribute('aria-disabled', 'true');
            if (action === 'run') button.textContent = 'Starting…';
            this.scheduleError = '';
            this.send({command: 'schedule_change', id: button.dataset.scheduleId, action});
        }));
        const form = root?.querySelector('[data-schedule-form]');
        if (!form) return;
        const read = () => {
            const fields = new FormData(form);
            this._scheduleDraft = {
                name: fields.get('name') || '', prompt: fields.get('prompt') || '', project: fields.get('project') || '',
                time: fields.get('time') || '', days: fields.getAll('days'), mode: fields.get('mode') || 'ask',
                model: fields.get('model') || '', max_minutes: fields.get('max_minutes') || ''};
            return this._scheduleDraft;
        };
        form.addEventListener('input', read);
        form.addEventListener('change', read);
        form.addEventListener('submit', event => {
            event.preventDefault();
            const draft = read();
            const [provider, ...model] = draft.model ? draft.model.split('|') : ['', ''];
            this.scheduleError = '';
            form.querySelector('[type="submit"]').disabled = true;
            this.send({command: 'schedule_save', schedule: {
                name: draft.name, prompt: draft.prompt, project: draft.project, time: draft.time, days: draft.days,
                mode: draft.mode, provider, model: model.join('|'), max_minutes: Number(draft.max_minutes)}});
        });
    }

    _renderCodeEditors() {
        const esc = value => this.escapeHtml(String(value ?? ''));
        const data = this.codeEditors;
        if (!data) return '<p class="editor-help">Loading…</p>';
        const result = data.result
            ? `<p class="${data.result.ok ? 'editor-help' : 'settings-error-banner'}" role="status">${esc(data.result.message)}</p>` : '';
        const off = data.enabled ? '' : '<div class="settings-error-banner">Code editors are turned off in Privacy &amp; security, so editors can’t reach Lumi.</div>';
        const vscode = (data.vscode || []).map(item =>
            `<button type="button" class="btn-sm" data-code-editor-install="vscode" data-editor="${esc(item.command)}">Install in ${esc(item.name)}</button>`).join('');
        const jetbrains = data.jetbrains || [];
        return `<p class="editor-help">Send the selection or files from your editor into your message here, and open what Lumi changed beside your files. Editors reach Lumi only on this computer while Lumi is open, and only files in the project Lumi has open. You send the message from Lumi.</p>
            ${off}${result}
            <h4 class="settings-subheading">VS Code</h4>
            <p class="editor-help">Adds Send Selection to Lumi, Ask Lumi About Selection, Send File to Lumi and Review Lumi’s Changes to the editor’s menus and Command Palette. Works in Cursor, Windsurf and VSCodium too.</p>
            ${vscode ? `<div class="editor-actions">${vscode}</div>` : '<p class="editor-help">No <code>code</code> command was found on PATH. In VS Code, run “Shell Command: Install ‘code’ command in PATH” (macOS), or reinstall with “Add to PATH” (Windows), then reopen this page.</p>'}
            <p class="editor-help">Or in a terminal: <code>${esc(data.commands?.vscode)}</code></p>
            <h4 class="settings-subheading">JetBrains IDEs</h4>
            <p class="editor-help">Adds External Tools that send the file or selection, ask about the selection, and show Lumi’s changes: Tools &gt; External Tools &gt; Lumi, and the editor’s right-click menu. ${jetbrains.length ? `Found: ${jetbrains.map(esc).join(', ')}. Restart an IDE that is open.` : 'No JetBrains IDE settings were found on this computer.'}</p>
            ${jetbrains.length ? '<div class="editor-actions"><button type="button" class="btn-sm" data-code-editor-install="jetbrains">Add to JetBrains IDEs</button></div>' : ''}
            <p class="editor-help">Or in a terminal: <code>${esc(data.commands?.jetbrains)}</code></p>`;
    }

    _bindCodeEditors() {
        this.settingsBody?.querySelectorAll('[data-code-editor-install]').forEach(button => button.addEventListener('click', () => {
            // aria-disabled rather than disabled, so keyboard focus stays put.
            if (button.getAttribute('aria-disabled') === 'true') return;
            button.setAttribute('aria-disabled', 'true');
            button.textContent = 'Installing…';
            this.send({command: 'code_editor_install', target: button.dataset.codeEditorInstall, editor: button.dataset.editor || ''});
        }));
    }

    _renderFileExclusions() {
        const saved = this.settings?.privacy?.excluded_paths || [];
        const managed = this.settings?._meta?.policy?.summary?.exclude || [];
        const org = this.settings?._meta?.policy?.summary?.organization || '';
        const managedHtml = managed.length
            ? `<p class="editor-help settings-managed">Managed by ${this.escapeHtml(org)}: ${managed.map(item => `<code>${this.escapeHtml(item)}</code>`).join(', ')}</p>`
            : '';
        const draft = this._exclusionDraft ?? saved.join('\n');
        return `${managedHtml}<p class="editor-help">Gitignore-style patterns, one per line, for every project: <code>.env</code> matches that name in any folder, and <code>secrets/**</code> is anchored at the project root. Lumi won’t read or change these files and leaves them out of searches, git diffs, the codebase index and attachments. A project’s <code>.lumiignore</code> adds its own patterns. Shell commands can still open excluded files, so keep command approval on for sensitive work.</p>
            <textarea id="settings-exclusions" class="settings-input settings-textarea" rows="6" spellcheck="false" aria-label="Excluded files, one pattern per line" placeholder=".env&#10;*.pem&#10;secrets/**">${this.escapeHtml(draft)}</textarea>
            <div class="editor-actions">
                <button type="button" class="btn-sm" id="settings-exclusions-save">Save exclusions</button>
                <button type="button" class="btn-sm" id="settings-exclusions-common">Add common secret files</button>
            </div>`;
    }

    _renderProjectTrust() {
        const data = this.projectTrust;
        if (!data) return '<p class="editor-help">Loading…</p>';
        const esc = value => this.escapeHtml(String(value ?? ''));
        const current = data.current || {};
        const brings = [...(current.instructions || [])];
        if (current.notes) brings.push('project notes (.lumi/memory.json)');
        const one = current.policy_allows === 1;
        if (current.policy_file) brings.push(current.policy_allows ? `${current.policy_file} (${current.policy_allows} rule${one ? '' : 's'} that skip${one ? 's' : ''} approval in Auto-edit)` : current.policy_file);
        // Trust also lets Lumi run the project's code on its own (language
        // servers, automatic lint and tests), so it's offered for every
        // project, not only ones that bring instructions.
        const state = current.policy_changed ? `Trusted, but you haven’t reviewed this version of its policy.${current.policy_allows ? ' Its rules that skip approval in Auto-edit are off until you trust it.' : ''}`
            : current.decision === 'trusted' ? 'Trusted: Lumi uses what it brings and may run its code for language servers and automatic checks.'
            : current.decision === 'restricted' ? 'Restricted: Lumi ignores what it brings and doesn’t run its code on its own.'
            : 'Not decided yet: Lumi ignores what it brings and doesn’t run its code on its own until you trust it.';
        const path = esc(current.project_path);
        const actions = current.project_path ? `<div class="editor-actions">
                <button type="button" class="btn-sm" data-trust-decision="trusted" data-trust-path="${path}">Trust this project</button>
                <button type="button" class="btn-sm" data-trust-decision="restricted" data-trust-path="${path}">Restrict</button>
            </div>` : '';
        const rows = (data.decisions || []).map(item => `<div class="settings-row">
                <div class="settings-row-copy"><span class="settings-row-label"><code>${esc(item.path)}</code></span>
                <div class="settings-row-hint">${item.decision === 'trusted' ? 'Trusted' : 'Restricted'} since ${esc(item.at)}${item.note ? ` · ${esc(item.note)}` : ''}</div></div>
                <div class="settings-row-value"><button type="button" class="btn-sm" data-trust-decision="forget" data-trust-path="${esc(item.path)}" aria-label="Forget the decision for ${esc(item.path)}">Forget</button></div>
            </div>`).join('');
        return `<p class="editor-help">A project’s instruction files (AGENTS.md, LUMI.md, CLAUDE.md and similar), its notes and codebase summary, and the allow rules in its lumi-policy.json, which skip approval in Auto-edit, apply only after you trust it, and language servers (code intelligence) and automatic lint and test runs wait for trust because they execute the project’s code. Its deny and ask rules always apply, because they only make Lumi more careful. Capability packs keep their own approval.</p>
            <div class="settings-row"><div class="settings-row-copy"><span class="settings-row-label">This project</span>
                <div class="settings-row-hint">${esc(`${brings.length ? `Brings ${brings.join(', ')}.` : 'Brings no instructions or policy.'} ${state}`)}</div></div></div>
            ${actions}
            <h4 class="settings-subheading">Remembered decisions</h4>
            ${rows || '<p class="editor-help">None yet.</p>'}`;
    }

    openSettingsPage(page) {
        this._settingsActivePage = page || this._settingsActivePage || 'general';
        this._settingsQuery = '';
        const search = document.getElementById('settings-search');
        if (search) search.value = '';
        this.switchView('settings');
    }

    _renderApiProviderCard(provider, label, keyHint) {
        const info = (this.providerConnections || {})[provider] || {};
        const text = info.error || (info.status === 'ready'
            ? `Connected · ${info.model_count ?? (info.models || []).length} models available · billed to your ${label} account`
            : `Add your ${label} key in API keys below, then check the connection. ${keyHint}`);
        return `<div class="provider-connection"><strong>${this.escapeHtml(label)}</strong>
            <p>${this.escapeHtml(text)}</p>
            <button class="btn-sm" data-provider="${provider}" data-provider-action="status">Check connection & refresh models</button></div>`;
    }

    _connectionTypeHints() {
        return {
            'openai-compatible': 'Base URL ending in /v1, for LiteLLM, vLLM, Ollama\u2019s /v1 or an internal gateway.',
            'openai': 'Leave the URL blank for api.openai.com, or point at a proxy that speaks the Responses API.',
            'azure-openai': 'https://NAME.openai.azure.com/openai/v1 \u2014 list your deployment names as models.',
            'anthropic': 'Leave the URL blank for api.anthropic.com, or point at a proxy that speaks the Messages API.',
            'anthropic-bedrock': 'Uses your AWS credentials (environment, profile or SSO) or a Bedrock API key. List model or inference-profile ids.',
            'anthropic-vertex': 'Uses Google Application Default Credentials. Set the project and region, and list model ids.',
        };
    }

    _connectionAuthOptions(type) {
        const byType = {
            'openai-compatible': ['bearer', 'header', 'oauth', 'none'],
            'openai': ['bearer', 'header', 'oauth', 'none'],
            'azure-openai': ['header', 'bearer', 'entra', 'oauth'],
            'anthropic': ['header', 'bearer', 'oauth', 'none'],
            'anthropic-bedrock': ['aws', 'bearer'],
            'anthropic-vertex': ['google'],
        };
        const labels = { bearer: 'Bearer token', header: 'Key in a header', none: 'No key', aws: 'AWS credentials', google: 'Google credentials',
                         entra: 'Microsoft Entra ID', oauth: 'OAuth client credentials' };
        return (byType[type] || ['bearer']).map(value => ({ value, label: labels[value] }));
    }

    _blankConnectionDraft() {
        return { name: '', type: 'openai-compatible', base_url: '', auth: 'bearer', auth_header: '', models: '',
                 region: '', project: '', api_version: '', aws_profile: '', context_window: '', vision: false,
                 headers: '', max_tokens_param: 'max_tokens', reasoning_effort: '', api_key: '', zero_retention: false,
                 tenant_id: '', client_id: '', token_url: '', scope: '', audience: '', client_cert: '', client_key: '' };
    }

    _connectionDraftFrom(item) {
        return { ...this._blankConnectionDraft(), ...item,
                 models: (item.models || []).join(', '),
                 headers: Object.entries(item.headers || {}).map(([k, v]) => `${k}: ${v}`).join('\n'),
                 context_window: item.context_window || '', vision: Boolean(item.vision),
                 zero_retention: Boolean(item.zero_retention), api_key: '' };
    }

    _connectionSecret(draft) {
        // Entra ID without a client id signs in as this computer; a secret
        // typed before the id was cleared isn't sent or stored.
        if (draft.auth === 'entra' && !String(draft.client_id || '').trim()) return undefined;
        return draft.api_key || undefined;
    }

    _connectionPayload(draft) {
        const headers = {};
        for (const line of String(draft.headers || '').split('\n')) {
            const index = line.indexOf(':');
            if (index > 0) headers[line.slice(0, index).trim()] = line.slice(index + 1).trim();
        }
        const payload = { name: draft.name, type: draft.type, base_url: draft.base_url, auth: draft.auth,
                          auth_header: draft.auth_header, models: draft.models, headers,
                          region: draft.region, project: draft.project, api_version: draft.api_version,
                          aws_profile: draft.aws_profile, max_tokens_param: draft.max_tokens_param,
                          reasoning_effort: draft.reasoning_effort, tenant_id: draft.tenant_id,
                          client_id: draft.client_id, token_url: draft.token_url, scope: draft.scope,
                          audience: draft.audience, client_cert: draft.client_cert, client_key: draft.client_key };
        if (this._connectionEdit?.originalId) payload.id = this._connectionEdit.originalId;
        if (String(draft.context_window || '').trim()) payload.context_window = Number(draft.context_window);
        if (draft.vision) payload.vision = true;
        payload.zero_retention = Boolean(draft.zero_retention);
        return payload;
    }

    _renderCustomConnections() {
        const data = this.connectionsData || { items: [], types: {} };
        const types = data.types || {};
        const edit = this._connectionEdit;
        const status = this._connectionStatus;
        const statusHtml = status ? `<p class="connection-status ${status.ok ? 'ok' : 'err'}" role="status">${this.escapeHtml(status.message)}</p>` : '';
        const rows = (data.items || []).map(item => {
            const confirm = this._connectionDeleteId === item.id;
            return `<li class="connection-row">
                <div class="connection-row-main"><strong>${this.escapeHtml(item.name)}</strong>${item.zero_retention ? ' <span class="connection-badge">Zero retention</span>' : ''}
                <span class="connection-meta">${this.escapeHtml(types[item.type] || item.type)}${item.base_url ? ' · ' + this.escapeHtml(item.base_url) : ''}</span>
                <span class="connection-meta">${item.models?.length ? this.escapeHtml(item.models.slice(0, 4).join(', ')) + (item.models.length > 4 ? ` +${item.models.length - 4}` : '') : 'Models discovered from the endpoint'}${item.auth === 'none' || item.auth === 'aws' || item.auth === 'google' || (item.auth === 'entra' && !item.client_id) ? '' : item.has_key ? ` · ${['oauth', 'entra'].includes(item.auth) ? 'secret' : 'key'} stored` : ` · no ${['oauth', 'entra'].includes(item.auth) ? 'secret' : 'key'} yet`}</span></div>
                <div class="connection-row-actions">${confirm
                    ? `<span class="connection-confirm">Remove ${this.escapeHtml(item.name)}?</span><button class="btn-sm" data-conn-action="delete-confirm" data-conn-id="${this.escapeHtml(item.id)}">Remove</button><button class="btn-sm" data-conn-action="delete-cancel">Keep</button>`
                    : `<button class="btn-sm" data-conn-action="edit" data-conn-id="${this.escapeHtml(item.id)}" aria-label="Edit ${this.escapeHtml(item.name)}">Edit</button><button class="btn-sm" data-conn-action="delete" data-conn-id="${this.escapeHtml(item.id)}" aria-label="Remove ${this.escapeHtml(item.name)}">Remove</button>`}</div>
            </li>`;
        }).join('');
        let form = '';
        if (edit) {
            const d = edit.draft;
            const typeOptions = Object.entries(types).map(([value, label]) => `<option value="${value}" ${d.type === value ? 'selected' : ''}>${this.escapeHtml(label)}</option>`).join('');
            const authOptions = this._connectionAuthOptions(d.type).map(o => `<option value="${o.value}" ${d.auth === o.value ? 'selected' : ''}>${o.label}</option>`).join('');
            const field = (id, label, input, hint = '', attrs = '') => `<label class="connection-field" for="conn-f-${id}" ${attrs}><span>${label}</span>${input}${hint ? `<small>${hint}</small>` : ''}</label>`;
            const text = (id, value, attrs = '') => `<input class="settings-input" id="conn-f-${id}" data-conn-field="${id}" value="${this.escapeHtml(String(value ?? ''))}" ${attrs}>`;
            const showKey = (!['none', 'aws', 'google', 'entra'].includes(d.auth) || (d.auth === 'entra' && String(d.client_id || '').trim()))
                || (d.type === 'anthropic-bedrock' && d.auth === 'bearer');
            const secretLabel = ['oauth', 'entra'].includes(d.auth) ? 'Client secret' : 'Key';
            const signIn = d.auth === 'entra' ? `
                ${field('tenant_id', 'Tenant id', text('tenant_id', d.tenant_id, 'placeholder="contoso.onmicrosoft.com or a GUID" spellcheck="false"'))}
                ${field('client_id', 'Client id (optional)', text('client_id', d.client_id, 'spellcheck="false"'), 'Leave blank to use this computer’s Azure sign-in: azure-identity when installed (a managed identity, say), else the Azure CLI (az login). With a client id, enter its client secret below.')}
                ${field('scope', 'Scope (optional)', text('scope', d.scope, 'placeholder="https://cognitiveservices.azure.com/.default" spellcheck="false"'))}`
                : d.auth === 'oauth' ? `
                ${field('token_url', 'Token URL', text('token_url', d.token_url, 'placeholder="https://login.example.com/oauth2/token" spellcheck="false"'))}
                ${field('client_id', 'Client id', text('client_id', d.client_id, 'spellcheck="false"'))}
                ${field('scope', 'Scope (optional)', text('scope', d.scope, 'spellcheck="false"'))}
                ${field('audience', 'Audience (optional)', text('audience', d.audience, 'spellcheck="false"'))}` : '';
            const clientCert = ['anthropic-bedrock', 'anthropic-vertex'].includes(d.type) ? '' : `
                ${field('client_cert', 'Client certificate (optional)', text('client_cert', d.client_cert, 'placeholder="Path to a PEM file" spellcheck="false"'), 'For gateways that require mutual TLS. The file stays where it is.')}
                ${field('client_key', 'Client key (optional)', text('client_key', d.client_key, 'placeholder="Path, if not in the certificate file" spellcheck="false"'))}`;
            const secretWord = secretLabel === 'Key' ? 'key' : 'client secret';
            const storedKey = edit.hasKey ? `Stored ${secretWord} — leave blank to keep it` : `Enter the ${secretWord}`;
            form = `<form class="connection-form" id="connection-form" novalidate>
                <h4>${edit.originalId ? 'Edit connection' : 'Add a connection'}</h4>
                <div class="connection-grid">
                ${field('name', 'Name', text('name', d.name, 'maxlength="60" placeholder="Company gateway" autocomplete="off"'))}
                ${field('type', 'Type', `<select class="settings-select" id="conn-f-type" data-conn-field="type">${typeOptions}</select>`, this.escapeHtml(this._connectionTypeHints()[d.type] || ''))}
                ${['anthropic-bedrock', 'anthropic-vertex'].includes(d.type) ? '' : field('base_url', 'Endpoint URL', text('base_url', d.base_url, 'placeholder="https://" autocomplete="off" spellcheck="false"'))}
                ${field('auth', 'Authentication', `<select class="settings-select" id="conn-f-auth" data-conn-field="auth">${authOptions}</select>`)}
                ${d.auth === 'header' ? field('auth_header', 'Key header', text('auth_header', d.auth_header, 'placeholder="api-key" spellcheck="false"')) : ''}
                ${signIn}
                ${showKey || d.auth === 'entra' ? field('api_key', secretLabel, `<input class="settings-input" type="password" id="conn-f-api_key" data-conn-field="api_key" value="${this.escapeHtml(d.api_key || '')}" placeholder="${storedKey}" autocomplete="off">`, 'Stored locally with your other API keys; never shown again.', showKey ? '' : 'hidden') : ''}
                ${field('models', d.type === 'azure-openai' ? 'Deployments' : 'Models', text('models', d.models, 'placeholder="Comma-separated ids" spellcheck="false"'), d.type === 'openai-compatible' || d.type === 'openai' || d.type === 'anthropic' ? 'Leave blank to list the models the endpoint reports.' : '')}
                ${['anthropic-bedrock', 'anthropic-vertex'].includes(d.type) ? field('region', 'Region', text('region', d.region, 'placeholder="us-east-1" spellcheck="false"')) : ''}
                ${d.type === 'anthropic-vertex' ? field('project', 'Google Cloud project', text('project', d.project, 'spellcheck="false"')) : ''}
                ${d.type === 'anthropic-bedrock' && d.auth === 'aws' ? field('aws_profile', 'AWS profile (optional)', text('aws_profile', d.aws_profile, 'placeholder="default" spellcheck="false"')) : ''}
                ${d.type === 'azure-openai' ? field('api_version', 'API version (optional)', text('api_version', d.api_version, 'placeholder="Only for pre-v1 endpoints" spellcheck="false"')) : ''}
                ${clientCert}
                ${field('context_window', 'Context window (optional)', text('context_window', d.context_window, 'inputmode="numeric" placeholder="Tokens, e.g. 128000"'))}
                ${d.type === 'openai-compatible' ? field('max_tokens_param', 'Output limit parameter', `<select class="settings-select" id="conn-f-max_tokens_param" data-conn-field="max_tokens_param"><option value="max_tokens" ${d.max_tokens_param !== 'max_completion_tokens' ? 'selected' : ''}>max_tokens</option><option value="max_completion_tokens" ${d.max_tokens_param === 'max_completion_tokens' ? 'selected' : ''}>max_completion_tokens</option></select>`) : ''}
                ${d.type === 'openai-compatible' ? field('reasoning_effort', 'Reasoning effort', `<select class="settings-select" id="conn-f-reasoning_effort" data-conn-field="reasoning_effort">${['', 'low', 'medium', 'high'].map(v => `<option value="${v}" ${d.reasoning_effort === v ? 'selected' : ''}>${v || 'Don\u2019t send'}</option>`).join('')}</select>`) : ''}
                ${field('headers', 'Extra headers (optional)', `<textarea class="settings-input" id="conn-f-headers" data-conn-field="headers" rows="3" placeholder="Header-Name: value" spellcheck="false">${this.escapeHtml(d.headers || '')}</textarea>`, 'One per line. Put secrets in the key field instead.')}
                <label class="connection-check"><input type="checkbox" id="conn-f-vision" data-conn-field="vision" ${d.vision ? 'checked' : ''}> Models accept images</label>
                <label class="connection-check"><input type="checkbox" id="conn-f-zero_retention" data-conn-field="zero_retention" ${d.zero_retention ? 'checked' : ''}> This endpoint keeps no prompts or responses (a zero data retention agreement)</label>
                </div>
                <div class="provider-actions"><button class="btn-sm" type="button" data-conn-action="test">Test connection</button>
                <button class="btn-sm connection-save" type="submit">${edit.originalId ? 'Save changes' : 'Add connection'}</button>
                <button class="btn-sm" type="button" data-conn-action="cancel">Cancel</button></div>
            </form>`;
        }
        return `<div class="provider-connection custom-connections"><strong>Custom connections</strong>
            <p>Gateways such as LiteLLM or vLLM, Azure OpenAI, Claude on Amazon Bedrock or Google Vertex AI, or any OpenAI-compatible endpoint. Each appears in the model menu under its name.</p>
            ${rows ? `<ul class="connection-list">${rows}</ul>` : ''}
            ${statusHtml}
            ${form || '<button class="btn-sm" type="button" data-conn-action="add">Add connection</button>'}
        </div>`;
    }

    _bindCustomConnections() {
        const root = this.settingsBody?.querySelector('.custom-connections');
        if (!root) return;
        root.querySelectorAll('[data-conn-field]').forEach(input => {
            input.addEventListener(input.tagName === 'SELECT' || input.type === 'checkbox' ? 'change' : 'input', () => {
                const draft = this._connectionEdit?.draft;
                if (!draft) return;
                const field = input.dataset.connField;
                draft[field] = input.type === 'checkbox' ? input.checked : input.value;
                // Type and authentication decide which fields exist. The form
                // re-renders from the draft, so forcing past the edit guard
                // loses nothing and keeps focus on this control.
                if (field === 'type') {
                    const allowed = this._connectionAuthOptions(draft.type).map(o => o.value);
                    if (!allowed.includes(draft.auth)) draft.auth = allowed[0];
                    this.renderSettingsView({force: true});
                } else if (field === 'auth') {
                    this.renderSettingsView({force: true});
                }
            });
        });
        // With Entra ID, a client id needs its client secret. Show that field
        // as the id is typed; re-rendering here would move focus away and
        // could swallow a click on the button that took it.
        const clientId = root.querySelector('[data-conn-field="client_id"]');
        clientId?.addEventListener('input', () => {
            const secret = root.querySelector('[data-conn-field="api_key"]')?.closest('label');
            if (secret && this._connectionEdit?.draft?.auth === 'entra') secret.hidden = !clientId.value.trim();
        });
        root.querySelectorAll('[data-conn-action]').forEach(btn => btn.addEventListener('click', () => {
            const action = btn.dataset.connAction;
            const id = btn.dataset.connId;
            const item = (this.connectionsData?.items || []).find(entry => entry.id === id);
            if (action === 'add') {
                this._connectionEdit = { originalId: '', hasKey: false, draft: this._blankConnectionDraft() };
                this._connectionStatus = null;
            } else if (action === 'edit' && item) {
                this._connectionEdit = { originalId: item.id, hasKey: item.has_key, draft: this._connectionDraftFrom(item) };
                this._connectionStatus = null;
            } else if (action === 'cancel') {
                this._connectionEdit = null;
                this._connectionStatus = null;
            } else if (action === 'delete') {
                this._connectionDeleteId = id;
            } else if (action === 'delete-cancel') {
                this._connectionDeleteId = null;
            } else if (action === 'delete-confirm' && id) {
                this._connectionDeleteId = null;
                this.send({ command: 'connection_delete', id });
                return;
            } else if (action === 'test' && this._connectionEdit) {
                const draft = this._connectionEdit.draft;
                this._connectionStatus = { ok: true, message: 'Testing the connection…' };
                this.send({ command: 'connection_test', connection: this._connectionPayload(draft),
                            api_key: this._connectionSecret(draft), original_id: this._connectionEdit.originalId || undefined });
            }
            this.renderSettingsView();
            if (action === 'add' || action === 'edit') document.getElementById('conn-f-name')?.focus();
        }));
        root.querySelector('#connection-form')?.addEventListener('submit', event => {
            event.preventDefault();
            const edit = this._connectionEdit;
            if (!edit) return;
            this._connectionStatus = { ok: true, message: 'Saving…' };
            this.send({ command: 'connection_save', connection: this._connectionPayload(edit.draft),
                        api_key: this._connectionSecret(edit.draft), original_id: edit.originalId || undefined });
            this.renderSettingsView();
        });
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
        return this._renderApiProviderCard('anthropic', 'Anthropic', 'ANTHROPIC_API_KEY also works.')
            + this._renderApiProviderCard('openai', 'OpenAI', 'OPENAI_API_KEY also works.')
            + `<div class="provider-connection">
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
            <button class="btn-sm" data-provider="sonn" data-provider-action="status">Check SONN connection & refresh models</button></div>`
            + this._renderCustomConnections();
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
            : 'Lumi needs Ollama. We couldn\'t reach it.';

        const wizard = document.createElement('div');
        wizard.className = 'ollama-wizard';
        wizard.innerHTML = `
            <div class="ollama-wizard-headline">
                <span class="ollama-wizard-icon" aria-hidden="true">🦙</span>
                <span>${this.escapeHtml(headline)}</span>
            </div>
            <p class="ollama-wizard-blurb">
                Lumi uses the models exposed by your configured Ollama
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


    _shellSandboxNote() {
        const sandbox = this.settings?._meta?.shell_sandbox;
        if (!sandbox) return '';
        if (sandbox.available === null) return 'Checking whether a sandbox can run on this computer…';
        if (sandbox.available) {
            return `A sandbox can run here (${sandbox.kind === 'seatbelt' ? 'macOS Seatbelt' : 'bubblewrap'}).`;
        }
        const note = `No sandbox can run here: ${sandbox.reason || 'unknown reason.'}`;
        return sandbox.mode === 'project' ? `${note} While this is on, the agent can’t run commands.` : note;
    }

    _secretStorageNote() {
        const storage = this.settings?._meta?.secret_storage;
        if (!storage) return '';
        if (storage.keychain) return `Keys you save here are kept in ${storage.store || 'your system credential store'}; settings.json holds only a placeholder.`;
        return `Keys you save here are kept in ~/.lumi/settings.json. ${storage.reason || ''}`.trim();
    }

    _settingsPages() {
        return [
            {id:'general', title:'General', group:'Personal', icon:'settings', description:'Choose how the agent works and which models new sessions use.', sections:['general'], fields:['default_permission_mode','default_backend','default_model','fallback_models','role_models','auto_lint_after_edits','auto_test_after_edits','auto_test_command','max_model_requests','big_context_profile','harness_enabled','autonomous_sessions'], keywords:'permissions approval workflow fallback failover roles summarize autonomous mission unattended'},
            {id:'profile', title:'Profile', group:'Personal', icon:'person', description:'Personalize your local workspace identity.', sections:['general'], fields:['display_name']},
            {id:'appearance', title:'Appearance', group:'Personal', icon:'sun', description:'Make the workspace feel right for you.', sections:['appearance']},
            {id:'pets', title:'Pets', group:'Personal', icon:'pet', description:'A little company while you build.', sections:['general'], fields:['show_companion'], keywords:'Echo companion'},
            {id:'sonn_account', title:'SONN account & credits', group:'Personal', icon:'person', description:'Your authenticated SONN identity and prepaid credit balance.', sections:['sonn_account'], keywords:'billing invitation balance'},
            {id:'cost_tracking', title:'Usage & cost', group:'Personal', icon:'chart', description:'Review tracked model usage and local spending alerts.', sections:['cost_tracking'], keywords:'tokens budget'},
            {id:'provider_connections', title:'Connections', group:'Integrations', icon:'globe', description:'Connect model providers and manage their endpoints and API keys.', sections:['provider_connections','network','api_keys'], keywords:'ChatGPT Codex OpenRouter SONN login authentication proxy certificates TLS keychain'},
            {id:'code_editors', title:'Code editors', group:'Integrations', icon:'plug', description:'Use Lumi from VS Code and JetBrains IDEs: send the selection, see Lumi’s changes.', sections:['code_editors'], keywords:'vs code vscode cursor windsurf vscodium jetbrains intellij pycharm webstorm rider goland ide extension plugin selection diff'},
            {id:'creative_editors', title:'Creative editors', group:'Integrations', icon:'cube', description:'Work with Blender, Unity, and Unreal Engine 5.', sections:['creative_editors']},
            {id:'mcp_servers', title:'MCP servers', group:'Integrations', icon:'plug', description:'Connect tools supplied by external servers.', sections:['mcp_servers']},
            {id:'engram', title:'Memory', group:'Coding', icon:'book', description:'Configure the optional Engram memory service.', sections:['engram']},
            {id:'rag', title:'Codebase index', group:'Coding', icon:'book', description:'Index your project for semantic code search.', sections:['rag'], keywords:'RAG files repository'},
            {id:'hooks', title:'Hooks', group:'Coding', icon:'plug', description:'Inspect commands that run at lifecycle events.', sections:['hooks']},
            {id:'capability_packs', title:'Capability packs', group:'Coding', icon:'cube', description:'Review what a pack would run, then approve or revoke it. Nothing in a pack runs until you approve it.', sections:['capability_packs'], keywords:'plugins extensions trust approve repository pack'},
            {id:'scheduled_tasks', title:'Scheduled tasks', group:'Coding', icon:'clock', description:'Run a saved task at set times, even when Lumi is closed.', sections:['scheduled_tasks'], keywords:'schedule cron nightly recurring automation task scheduler launchd unattended'},
            {id:'privacy', title:'Privacy & security', group:'Security', icon:'shield', description:'Control what Lumi reads, keeps and sends, and which tools it may use.', sections:['org_policy','privacy','file_exclusions','transcripts','audit_log','audit','audit_status','security','shell_sandbox','project_trust'], keywords:'audit log opentelemetry otlp tamper evidence secrets redact scan credentials DLP exclude ignore lumiignore env retention delete trust AGENTS.md policy codex claude computer gateway organization managed group policy MDM sandbox seatbelt bubblewrap bwrap shell commands'},
            {id:'local_backends', title:'Ollama runtime', group:'Advanced', icon:'cube', description:'Tune your local model runtime.', sections:['local_backends']},
            {id:'prompt_inspector', title:'Prompt inspector', group:'Advanced', icon:'book', description:'Inspect the instructions used by the active model.', sections:['prompt_inspector']},
            {id:'model_evaluations', title:'Model evaluations', group:'Advanced', icon:'chart', description:'Compare models on your own tasks, and review model quality and runtime diagnostics.', sections:['model_comparisons', 'model_evaluations'], keywords:'compare comparison benchmark evaluate models tasks switch pass rate'},
            {id:'iteration_checkpoints', title:'Checkpoints & recovery', group:'Advanced', icon:'history', description:'Inspect saved iterations and recovery options.', sections:['iteration_checkpoints']},
            {id:'lumi_account', title:'Lumi account', group:'Personal', icon:'person', description:'Sign in to Lumi Cloud and use your organization’s policy on this computer.', sections:['lumi_account'], keywords:'lumi cloud organization team company sign in enroll device computer managed policy seat slack teams microsoft chat tasks remote requests'},
            {id:'about', title:'About Lumi', group:'Personal', icon:'book', description:'What Lumi is, what it costs and what it sends where.', sections:['about'], keywords:'version license free plan pricing account privacy telemetry notices MIT'},
            {id:'updates', title:'Updates', group:'Advanced', icon:'history', description:'Choose how Lumi updates itself and which releases it takes.', sections:['updates','update_status'], keywords:'update upgrade version release beta channel pin stable automatic manual off'},
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
        // Flush a deferred render once focus has settled. A microtask runs
        // during the focus change, before the clicked field receives focus, so
        // it rebuilt the form under the click and dropped what was typed next
        // (click a toggle, then click into a key field). The render still
        // waits while the new focus is another field.
        this.settingsBody.addEventListener('focusout', () => setTimeout(() => {
            if (this._settingsRenderPending) this.renderSettingsView();
        }, 0));
    }

    _renderSettingsNavigation(pages) {
        const icons = {
            settings:'M6 2h4l1 3 3 1v4l-3 1-1 3H6l-1-3-3-1V6l3-1z M6 8a2 2 0 1 0 4 0 2 2 0 0 0-4 0',
            clock:'M8 2a6 6 0 1 0 0 12A6 6 0 0 0 8 2 M8 5v3.5l2.5 1.5',
            person:'M5 5a3 3 0 1 0 6 0 3 3 0 0 0-6 0 M2 14c0-6 12-6 12 0',
            sun:'M5 8a3 3 0 1 0 6 0 3 3 0 0 0-6 0 M8 1v1m0 12v1M1 8h1m12 0h1M3 3l1 1m8 8 1 1M3 13l1-1m8-8 1-1',
            pet:'M3 6 2 2l5 2h2l5-2-1 4c4 10-14 10-10 0z M5 8h.1M11 8h.1M6 11h4',
            chart:'M2 2v12h12M5 10V7m4 3V4m4 6V6',
            globe:'M1 8a7 7 0 1 0 14 0A7 7 0 1 0 1 8M1 8h14M8 1c-4 4-4 10 0 14 4-4 4-10 0-14',
            cube:'m8 1 6 3v8l-6 3-6-3V4z M2 4l6 4 6-4M8 8v7',
            plug:'M5 1v4m6-4v4M3 5h10v3a5 5 0 0 1-10 0z M8 13v2',
            book:'M8 3C5 1 2 1 1 2v11c3-1 5 0 7 1 2-1 4-2 7-1V2c-1-1-4-1-7 1z M8 3v11',
            history:'M2 5a6 6 0 1 1 0 6M2 1v4h4M8 4v4l3 2',
            shield:'M8 1 2 3v5c0 4 3 6 6 7 3-1 6-3 6-7V3z M5.5 8l2 2 3-3.5',
        };
        const nav = document.getElementById('settings-nav');
        const scrollTop = nav.scrollTop, scrollLeft = nav.scrollLeft;
        nav.innerHTML = ['Personal','Integrations','Coding','Security','Advanced'].map(group => {
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
        if (page === 'provider_connections') this.send({command: 'connections_list'});
        if (page === 'privacy') {
            this.send({command: 'project_trust_list'});
            this.send({command: 'audit_status'});
        }
        if (page === 'scheduled_tasks') this.send({command: 'schedules_list'});
        if (page === 'code_editors') this.send({command: 'code_editors_list'});
        if (page === 'model_evaluations') this.send({command: 'model_evals_list'});
        const command = {creative_editors:'editor_list', capability_packs:'capability_pack_list', cost_tracking:'get_costs', model_evaluations:'evaluation_list', iteration_checkpoints:'checkpoint_list', updates:'update_status', about:'about_info', lumi_account:'cloud_status'}[page];
        if (command) this.send({command});
    }

    _settingsSections() {
        return [
            { id: "sonn_account", title: "SONN account & credits", open: true, custom: true },
            { id: 'lumi_account', title: 'Lumi account', open: true, custom: true },
            { id: "provider_connections", title: "Connections", open: true, custom: true },
            { id: 'code_editors', title: 'Code editors', open: true, custom: true },
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
                          { value: 'anthropic', label: 'Anthropic' },
                          { value: 'openai', label: 'OpenAI' },
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
                          { value: 'ask', label: 'Ask permissions (ask before every change)' },
                          { value: 'auto-edit', label: 'Auto-edit (file edits OK, other actions ask)' },
                          { value: 'plan', label: 'Plan mode' },
                      ]
                    },
                    { key: 'auto_lint_after_edits', label: 'Auto-lint after edits', type: 'toggle',
                      hint: 'After every file_edit/file_write, run the project linter (ruff/eslint/flake8) on the changed file. Errors are injected back as a follow-up turn.' },
                    { key: 'auto_test_after_edits', label: 'Auto-test after edits', type: 'toggle',
                      hint: 'After every file_edit/file_write, run the test command on the matching test file. Failures are injected back as a follow-up turn.' },
                    { key: 'auto_test_command', label: 'Auto-test command', type: 'text',
                      hint: 'Default: "pytest -x". For JS/TS: "npx jest" or "npx vitest run".' },
                    { key: 'max_model_requests', label: 'Model requests per turn', type: 'number',
                      hint: '0 means unlimited. Native coding runs pause at this limit and retain work; send Continue to resume. Includes recovery attempts, excludes auxiliary summaries and delegated workers. This is not a dollar limit or a CLI-provider limit.' },
                    { key: 'fallback_models', label: 'If the model fails, continue with', type: 'lines',
                      placeholder: 'anthropic:claude-sonnet-5\nollama:qwen3:32b',
                      hint: 'One provider:model per line, tried in order when a request fails before answering. The next turn tries your chosen model first again.' },
                    { key: 'role_models', label: 'Models for roles', type: 'lines',
                      placeholder: 'summarize ollama:qwen3:8b\nreview anthropic:claude-opus-5-5',
                      hint: 'One "role provider:model" per line. summarize names sessions and compacts long conversations; plan, explore, implement, test, review and vision are used for delegated work.' },
                    { key: 'big_context_profile', label: 'Large-context profile', type: 'toggle',
                      hint: 'Bumps Ollama context to 131072 tokens and batch to 2048. Best for large-repo sessions. Restart the app for the change to take effect on the next backend connection.' },
                    { key: 'harness_enabled', label: 'Sprint workflow (planner / generator / evaluator)', type: 'toggle',
                      hint: 'Off by default. Enable to use Lumi\u2019s structured planner\u2192generator\u2192evaluator pattern with sprint contracts and an autonomous cycle. State lives in ~/.lumi/, not in your repo.' },
                    { key: 'autonomous_sessions', label: 'Autonomous sessions (experimental)', type: 'toggle',
                      hint: 'Off by default. Shows the Autonomous button: Lumi drafts a spec with you, then works through it unattended within a time budget and an optional spending limit, checking the acceptance criteria as it goes.' },
                ]
            },
            {
                id: 'appearance', title: 'Appearance', open: false,
                fields: [
                    { key: 'theme', label: 'Theme', type: 'select', default: 'dark',
                      options: [
                          { value: 'dark', label: 'Dark' },
                          { value: 'light', label: 'Light' },
                          { value: 'system', label: 'Match system' },
                      ],
                      hint: 'Match system follows your computer’s light or dark setting.'
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
            { id: 'model_comparisons', title: 'Compare models on your tasks', custom: true },
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
                    { key: 'proxy_url', label: 'Proxy', type: 'text',
                      placeholder: 'http://proxy.example.com:8080',
                      hint: 'Sends model, account and update traffic through this proxy. Local addresses always connect directly. Leave blank to use HTTPS_PROXY from your environment.' },
                    { key: 'no_proxy', label: 'Connect directly to', type: 'text',
                      placeholder: 'internal.example.com, models.corp.example.com',
                      hint: 'Comma-separated hosts that bypass the proxy. localhost, 127.0.0.1 and ::1 always do.' },
                    { key: 'system_certificates', label: 'Use the system certificate store', type: 'toggle', default: true,
                      hint: 'Trusts the certificates your operating system trusts, including a company root certificate for TLS inspection. Turn off to use only the certificates bundled with Lumi.' },
                ]
            },
            {
                id: 'api_keys', title: 'API keys', open: false,
                note: this._secretStorageNote(),
                fields: [
                    { key: 'anthropic', label: 'Anthropic API key', type: 'password',
                      hint: 'ANTHROPIC_API_KEY is also supported. Usage is billed to your Anthropic account.' },
                    { key: 'openai', label: 'OpenAI API key', type: 'password',
                      hint: 'OPENAI_API_KEY is also supported. Usage is billed to your OpenAI account.' },
                    { key: 'sonn', label: 'SONN API key', type: 'password',
                      hint: 'Enter your private invitation key; it is hidden after saving. SONN_API_KEY is also supported.' },
                    { key: 'openrouter', label: 'OpenRouter API key', type: 'password',
                      hint: 'OPENROUTER_API_KEY is also supported. API calls use your OpenRouter credits.' },
                    { key: 'kimi', label: 'Moonshot API key', type: 'password',
                      hint: 'MOONSHOT_API_KEY is also supported and takes effect when no stored key exists.' },
                    { key: 'github', label: 'GitHub token', type: 'password',
                      hint: 'Lets the agent read pull request reviews and checks, and open, update and comment on pull requests. GITHUB_TOKEN or GH_TOKEN also work.' },
                    { key: 'gitlab', label: 'GitLab token', type: 'password',
                      hint: 'The same for merge requests on GitLab (api scope). GITLAB_TOKEN also works.' },
                    { key: 'bitbucket', label: 'Bitbucket token', type: 'password',
                      hint: 'An access token, or username:app-password, for Bitbucket Cloud pull requests. BITBUCKET_TOKEN also works.' },
                    { key: 'azure_devops', label: 'Azure DevOps token', type: 'password',
                      hint: 'A personal access token (Code: read & write; Build: read). AZURE_DEVOPS_TOKEN also works.' },
                    { key: 'otlp', label: 'OpenTelemetry collector token', type: 'password',
                      hint: 'Sent in the header set under Privacy & security when audit records are exported.' },
                ]
            },
            { id: 'org_policy', title: 'Organization policy', custom: true },
            {
                id: 'privacy', title: 'Before each model request',
                note: 'Commands the agent runs, hooks and MCP servers never receive Lumi’s model keys, and your saved keys are removed from tool output before it reaches a model.',
                fields: [
                    { key: 'secret_scan', label: 'Scan for secrets', type: 'toggle',
                      hint: 'Also removes well-known credentials — cloud and platform keys, tokens, private keys, passwords in connection strings and .env lines — from tool output and your messages. The model sees [REDACTED …] in their place. Codex and Claude Code read files through their own tools and are not scanned.' },
                ]
            },
            { id: 'file_exclusions', title: 'Files Lumi never reads', custom: true },
            {
                id: 'transcripts', title: 'Transcripts', store: 'privacy',
                fields: [
                    { key: 'transcript_retention_days', label: 'Delete transcripts after (days)', type: 'number',
                      hint: 'Empty or 0 keeps them. Lumi deletes saved sessions and session logs this many days after their last activity, at startup and daily. The open session is never deleted.' },
                ]
            },
            {
                id: 'security', title: 'Tools outside Lumi’s own loop',
                fields: [
                    { key: 'cli_adapters', label: 'Codex and Claude Code', type: 'toggle', default: true,
                      hint: 'They run their own tool loops, so Lumi’s approvals, file exclusions and secret scan don’t apply inside them. Off removes them from Models.' },
                    { key: 'computer_use', label: 'Computer use', type: 'toggle', default: true,
                      hint: 'Screenshots and mouse and keyboard control of this computer. Off removes these tools from every session.' },
                    { key: 'chat_gateway', label: 'Chat gateway', type: 'toggle', default: true,
                      hint: 'Lets `lumi gateway` answer messages from Telegram. Off makes it refuse to start.' },
                    { key: 'scheduled_tasks', label: 'Scheduled tasks', type: 'toggle', default: true,
                      hint: 'Saved tasks that run unattended at set times (Settings > Scheduled tasks). Off stops them running and stops new ones being added.' },
                    { key: 'editor_bridge', label: 'Code editors', type: 'toggle', default: true,
                      hint: 'Lets the VS Code extension and JetBrains tools on this computer add files to your message and show what Lumi changed (Settings > Code editors).' },
                ]
            },
            {
                id: 'shell_sandbox', title: 'Shell sandbox', store: 'security',
                note: this._shellSandboxNote(),
                fields: [
                    { key: 'shell_sandbox', label: 'Where the agent’s commands can write', type: 'select', default: 'off',
                      options: [
                          { value: 'off', label: 'Anywhere you can' },
                          { value: 'project', label: 'Only the project and temporary folders' },
                      ],
                      hint: 'Commands, checks, jobs and previews the agent starts run in an operating-system sandbox (macOS and Linux). Reading files and the network work as before; Git’s own folder stays read-only, so commit with the agent’s Git tools. Where no sandbox can run, the agent can’t run commands while this is on. Commands that wipe a drive or your home folder, format disks or shut down are refused either way.' },
                ]
            },
            {
                id: 'audit_log', title: 'Audit log', store: 'privacy',
                note: 'Lumi keeps a local, tamper-evident record of what the agent did: turns, model usage, tool calls and results, file changes, approvals and settings changes. Each record carries a hash of the one before, so an edited or removed record is detected.',
                fields: [
                    { key: 'audit_log', label: 'Keep an audit log', type: 'toggle', default: true },
                    { key: 'audit_capture', label: 'What the audit log captures', type: 'select', default: 'metadata',
                      options: [
                          { value: 'metadata', label: 'Metadata only' },
                          { value: 'redacted', label: 'Content, secrets removed' },
                          { value: 'full', label: 'Full content' },
                      ],
                      hint: 'Metadata keeps tool names, outcomes, file paths, sizes and digests, never prompts, file contents, commands or output. Content levels are truncated; saved keys are always removed.' },
                    { key: 'audit_retention_days', label: 'Keep audit records for (days)', type: 'number',
                      hint: 'Empty or 0 keeps them. Separate from transcript retention.' },
                ]
            },
            {
                id: 'audit', title: 'Send audit records to OpenTelemetry',
                fields: [
                    { key: 'otlp_endpoint', label: 'Collector (OTLP/HTTP)', type: 'text', placeholder: 'https://collector.example.com:4318',
                      hint: 'Records are exported as spans with the OpenTelemetry GenAI conventions. Put a collector token under Connections > API keys.' },
                    { key: 'otlp_auth_header', label: 'Token header', type: 'text', placeholder: 'Authorization' },
                ]
            },
            { id: 'audit_status', title: 'Audit log status', custom: true },
            { id: 'project_trust', title: 'Project trust', custom: true },
            { id: 'scheduled_tasks', title: 'Scheduled tasks', custom: true },
            {
                id: 'updates', title: 'Updates',
                note: 'Changes apply the next time Lumi starts. Your organization’s policy can set these for you.',
                fields: [
                    { key: 'mode', label: 'Check for updates', type: 'select', default: 'automatic',
                      options: [
                          { value: 'automatic', label: 'Automatically' },
                          { value: 'manual', label: 'Only when I check' },
                          { value: 'off', label: 'Never' },
                      ],
                      hint: 'Automatically checks once a day. Never is for organizations that deploy Lumi themselves.' },
                    { key: 'channel', label: 'Channel', type: 'select', default: 'stable',
                      options: [
                          { value: 'stable', label: 'Stable' },
                          { value: 'beta', label: 'Beta' },
                      ],
                      hint: 'Beta releases arrive first and may have rough edges. The beta channel also gets every stable release.' },
                    { key: 'pin', label: 'Stay on release line', type: 'text', placeholder: 'e.g. 0.20',
                      hint: 'Take only fixes for this line (0.20.x, say) and nothing newer. A pin wins over the channel. Empty follows the channel.' },
                ]
            },
            { id: 'update_status', title: 'This installation', custom: true },
            { id: 'about', title: 'About Lumi', custom: true },
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
            { id: 'capability_packs', title: 'Capability packs', custom: true },
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
            {heading:'Models', keys:['default_backend','default_model','fallback_models','role_models','big_context_profile']},
            {heading:'Workflow', keys:['auto_lint_after_edits','auto_test_after_edits','auto_test_command','max_model_requests','harness_enabled','autonomous_sessions']},
        ].map(group => ({...section, heading:group.heading, fields:section.fields.filter(field => group.keys.includes(field.key))})) : [section]) : [];
        this.settingsBody.innerHTML = '';
        if (this.settingsError) {
            const alert = document.createElement('div');
            alert.className = 'settings-error-banner';
            alert.setAttribute('role', 'alert');
            alert.textContent = this.settingsError;
            this.settingsBody.appendChild(alert);
        }

        for (const section of visibleSections) {
            // A section can show fields stored under another settings key.
            const store = section.store || section.id;
            const data = this.settings[store] || {};
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
            } else if (section.id === 'audit_status') {
                bodyHtml = this._renderAuditStatus();
            } else if (section.id === 'update_status') {
                bodyHtml = this._renderUpdateStatus();
            } else if (section.id === 'about') {
                bodyHtml = this._renderAbout();
            } else if (section.id === 'lumi_account') {
                bodyHtml = this._renderLumiAccount();
            } else if (section.id === 'org_policy') {
                bodyHtml = this._renderOrgPolicy();
            } else if (section.id === 'file_exclusions') {
                bodyHtml = this._renderFileExclusions();
            } else if (section.id === 'project_trust') {
                bodyHtml = this._renderProjectTrust();
            } else if (section.id === 'scheduled_tasks') {
                bodyHtml = this._renderScheduledTasks();
            } else if (section.id === 'code_editors') {
                bodyHtml = this._renderCodeEditors();
            } else if (section.id === 'model_comparisons') {
                bodyHtml = this._renderModelComparisons();
            } else if (section.id === 'capability_packs') {
                bodyHtml = this._renderCapabilityPacks();
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
                    <div class="settings-row-hint evaluation-hint">Runs use fresh temporary projects and the live Ollama models. Results persist under ~/.lumi/evaluations.</div>
                    <div class="settings-row-hint evaluation-hint"><strong>Interactive provider health</strong> — redacted outcomes, retries, and latency; prompts and responses are never stored.</div>
                    <div class="evaluation-records">${telemetryHtml || '<div class="settings-row"><span class="settings-row-label">No interactive telemetry yet.</span></div>'}</div>
                    <div class="evaluation-records">${recordHtml || '<div class="settings-row"><span class="settings-row-label">No evaluations yet.</span></div>'}</div>
                `;
            } else if (section.id === 'iteration_checkpoints') {
                const checkpoints = this.iterationCheckpoints || [];
                const comparison = this.checkpointComparison;
                bodyHtml = `
                    <div class="settings-row-hint checkpoint-hint">Each autonomous iteration snapshots tracked and untracked work without moving HEAD. Restore first preserves the failed state on a lumi-recovery/* branch.</div>
                    <div class="checkpoint-list">
                        ${checkpoints.length ? checkpoints.map(item => `
                            <div class="checkpoint-record">
                                <div><strong>${this.escapeHtml((item.message || 'Iteration checkpoint').replace('Lumi checkpoint ', ''))}</strong><small>${this.escapeHtml(item.commit?.slice(0, 10) || '')} · ${this.escapeHtml(item.created_at || '')}</small></div>
                                <button class="btn-sm checkpoint-compare" data-ref="${this.escapeHtml(item.ref)}">Compare</button>
                                <button class="btn-sm checkpoint-restore" data-ref="${this.escapeHtml(item.ref)}">Restore</button>
                            </div>
                        `).join('') : '<div class="settings-row"><span class="settings-row-label">No iteration checkpoints yet.</span></div>'}
                    </div>
                    ${comparison ? `<div class="checkpoint-comparison"><strong>Changes since checkpoint</strong><pre>${this.escapeHtml(comparison.name_status || 'No changes')}</pre><pre>${this.escapeHtml(comparison.stat || '')}</pre></div>` : ''}
                `;
            } else if (section.id === 'hooks') {
                bodyHtml = this._renderHooksList(Array.isArray(data) ? data : []);
                bodyHtml += `<div class="settings-row" style="margin-top:8px"><span class="settings-row-label" style="color:var(--dim);font-size:11px">Edit hooks in ~/.lumi/settings.json</span></div>`;
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
                if (section.note) bodyHtml += `<div class="settings-row settings-section-note"><div class="settings-row-copy"><span class="settings-row-hint">${this.escapeHtml(section.note)}</span></div></div>`;
                for (const field of section.fields) {
                    const val = data[field.key] ?? field.default ?? '';
                    // An organization policy can lock a field (lumi/policy.py); it
                    // then shows the managed value and can't be changed here.
                    const lockedBy = this.settings?._meta?.locked?.[`${store}.${field.key}`] || '';
                    const lock = lockedBy ? ' disabled' : '';
                    let input = '';
                    if (field.type === 'select') {
                        const opts = field.options.map(o =>
                            `<option value="${o.value}" ${val === o.value ? 'selected' : ''}>${o.label}</option>`
                        ).join('');
                        input = `<select class="settings-select" data-section="${store}" data-key="${field.key}" aria-label="${this.escapeHtml(field.label)}"${lock}>${opts}</select>`;
                    } else if (field.type === 'toggle') {
                        const checked = val ? 'checked' : '';
                        input = `<label class="settings-toggle"><input type="checkbox" ${checked} data-section="${store}" data-key="${field.key}" aria-label="${this.escapeHtml(field.label)}"${lock} /><span class="settings-toggle-track" aria-hidden="true"></span></label>`;
                    } else if (field.type === 'password') {
                        const hasSecret = Boolean(this.settings._meta?.api_keys_present?.[field.key]);
                        input = `
                            <div style="display:flex;align-items:center;gap:8px;">
                                <input class="settings-input" type="password" value="" data-section="${store}" data-key="${field.key}" aria-label="${this.escapeHtml(field.label)}" data-secret-field="true" placeholder="${hasSecret ? 'Stored key' : 'Enter key'}" style="flex:1"${lock} />
                                <span style="color:var(--muted);font-size:11px;white-space:nowrap">${hasSecret ? 'Stored' : 'Not set'}</span>
                                ${hasSecret && !lockedBy ? `<button class="btn-sm settings-clear-secret" data-section="${store}" data-key="${field.key}" aria-label="Clear ${this.escapeHtml(field.label)}" style="font-size:11px">Clear</button>` : ''}
                            </div>
                        `;
                    } else if (field.type === 'lines') {
                        // Typed lines stay until a save succeeds, so a refused save can be fixed.
                        const draft = this._settingsDrafts?.[`${store}.${field.key}`];
                        const text = draft ?? (Array.isArray(val) ? val.join('\n') : String(val || ''));
                        const ph = field.placeholder ? ` placeholder="${this.escapeHtml(field.placeholder)}"` : '';
                        input = `<textarea class="settings-input settings-textarea settings-lines" rows="3" spellcheck="false" data-section="${store}" data-key="${field.key}" aria-label="${this.escapeHtml(field.label)}"${ph}${lock}>${this.escapeHtml(text)}</textarea>`;
                    } else if (field.type === 'number') {
                        input = `<input class="settings-input" type="number" value="${val || ''}" data-section="${store}" data-key="${field.key}" aria-label="${this.escapeHtml(field.label)}" placeholder="None" style="width:80px"${lock} />`;
                    } else {
                        // Like lines, typed text stays until a save succeeds.
                        const draft = this._settingsDrafts?.[`${store}.${field.key}`];
                        const ph = field.placeholder ? ` placeholder="${this.escapeHtml(field.placeholder)}"` : '';
                        input = `<input class="settings-input" type="text" value="${this.escapeHtml(String(draft ?? val))}" data-section="${store}" data-key="${field.key}" aria-label="${this.escapeHtml(field.label)}"${ph}${lock} />`;
                    }
                    const managed = lockedBy ? `<div class="settings-row-hint settings-managed">Managed by ${this.escapeHtml(lockedBy)}</div>` : '';
                    const hint = field.hint ? `<div class="settings-row-hint">${this.escapeHtml(field.hint)}</div>` : '';
                    bodyHtml += `<div class="settings-row${lockedBy ? ' is-managed' : ''}"><div class="settings-row-copy"><span class="settings-row-label">${this.escapeHtml(field.label)}</span>${managed}${hint}</div><div class="settings-row-value">${input}</div></div>`;
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
        this._bindCustomConnections();
        const exclusions = document.getElementById('settings-exclusions');
        exclusions?.addEventListener('input', () => { this._exclusionDraft = exclusions.value; });
        document.getElementById('settings-exclusions-save')?.addEventListener('click', () => {
            this._exclusionDraft = null;
            this.send({command: 'update_settings', section: 'privacy', key: 'excluded_paths', value: exclusions.value.split('\n')});
        });
        document.getElementById('settings-exclusions-common')?.addEventListener('click', () => {
            const lines = exclusions.value.split('\n').map(line => line.trim()).filter(Boolean);
            const common = this.settings?._meta?.common_exclusions || [];
            exclusions.value = [...lines, ...common.filter(item => !lines.includes(item))].join('\n');
            this._exclusionDraft = exclusions.value;
            exclusions.focus();
        });
        document.getElementById('audit-verify')?.addEventListener('click', () => this.send({command: 'audit_status'}));
        this._bindUpdateCheck();
        this._bindLumiAccount();
        this._bindScheduledTasks();
        this._bindCodeEditors();
        this._bindModelComparisons();
        this.settingsBody.querySelectorAll('[data-trust-decision]').forEach(button => {
            button.addEventListener('click', () => {
                button.disabled = true;
                this.send({command: 'project_trust_set', decision: button.dataset.trustDecision, project_path: button.dataset.trustPath});
            });
        });
        this.settingsBody.querySelectorAll('[data-provider-action]').forEach(btn => {
            btn.addEventListener('click', () => {
                btn.disabled = true;
                btn.textContent = 'Connecting…';
                this.send({command: 'provider_connection', provider: btn.dataset.provider, action: btn.dataset.providerAction});
            });
        });
        this.settingsBody.querySelectorAll('input.settings-input[type="text"][data-section]').forEach(input => {
            input.addEventListener('input', () => {
                this._settingsDrafts = {...(this._settingsDrafts || {}), [`${input.dataset.section}.${input.dataset.key}`]: input.value};
            });
        });
        this.settingsBody.querySelectorAll('textarea.settings-lines').forEach(area => {
            area.addEventListener('input', () => {
                this._settingsDrafts = {...(this._settingsDrafts || {}), [`${area.dataset.section}.${area.dataset.key}`]: area.value};
            });
            area.addEventListener('blur', () => {
                const lines = area.value.split('\n').map(line => line.trim()).filter(Boolean);
                this.send({ command: 'update_settings', section: area.dataset.section, key: area.dataset.key, value: lines });
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

        const prices = document.getElementById('cost-price-overrides');
        prices?.addEventListener('input', () => { this._priceDraft = prices.value; });
        document.getElementById('cost-price-save')?.addEventListener('click', () => {
            this._priceDraft = prices.value;
            this._pricePending = true;
            this.send({command: 'update_settings', section: 'cost_tracking', key: 'price_overrides', value: prices.value.split('\n')});
            this.send({command: 'get_costs'});
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
        this.settingsBody.querySelectorAll('[data-pack-action]').forEach(btn => {
            btn.addEventListener('click', () => {
                const action = btn.dataset.packAction;
                if (action === 'remove') {
                    this.send({command: 'capability_pack_remove', pack_id: btn.dataset.packId});
                    btn.disabled = true;
                    btn.textContent = 'Removing…';
                    return;
                }
                const approve = action === 'approve';
                this.send({
                    command: approve ? 'capability_pack_approve' : 'capability_pack_revoke',
                    pack_id: btn.dataset.packId,
                    path: btn.dataset.packPath,
                    // The digest of what was on screen: the server refuses the
                    // approval if the pack changed after this list was drawn.
                    ...(approve ? {digest: btn.dataset.packDigest} : {}),
                });
                btn.disabled = true;
                btn.textContent = approve ? 'Approving…' : 'Revoking…';
            });
        });
        // Install from Git: typed values survive the redraws that follow.
        this.settingsBody.querySelectorAll('[data-pack-install]').forEach(input => {
            input.addEventListener('input', () => {
                this._packInstallDraft = {...(this._packInstallDraft || {}), [input.dataset.packInstall]: input.value};
            });
        });
        this.settingsBody.querySelector('#pack-install-form')?.addEventListener('submit', event => {
            event.preventDefault();
            const draft = this._packInstallDraft || {};
            if (!String(draft.url || '').trim() || !String(draft.ref || '').trim()) {
                this.showStatusMessage('Enter the repository and a commit, tag or branch.');
                return;
            }
            this._packInstalling = true;
            this.send({command: 'capability_pack_install', url: draft.url, ref: draft.ref, subdir: draft.subdir || ''});
            this.renderSettingsView({force: true});
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
                if (!confirm('Restore this checkpoint? Your current files will be preserved on a lumi-recovery/* branch first.')) return;
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

        popover.innerHTML = this._gitPopoverHtml(this.gitData);

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


    /** Configured hooks. Commands routinely contain <, > and quotes. */
    _renderHooksList(hooks) {
        if (hooks.length === 0) {
            return `<div class="settings-row"><span class="settings-row-label" style="color:var(--dim)">No hooks configured</span></div>`;
        }
        return hooks.map(h => `
            <div class="settings-row">
                <span class="settings-row-label">${this.escapeHtml(h.name || h.hook_type)}: <code style="font-size:11px">${this.escapeHtml(h.command)}</code></span>
                <span style="color:${h.enabled ? 'var(--ok)' : 'var(--muted)'}">${h.enabled ? '●' : '○'}</span>
            </div>
        `).join('');
    }

    /** Branch names, file names and commit messages come from the repository. */
    _gitPopoverHtml(data) {
        return `
            <div class="git-popover-header">
                <span>${this.escapeHtml(data.branch)}</span>
                <button class="icon-btn git-popover-close">&times;</button>
            </div>
            <div class="git-popover-tabs">
                <button class="git-popover-tab active" data-tab="changes">Changes (${(data.changes || []).length})</button>
                <button class="git-popover-tab" data-tab="commits">Commits</button>
            </div>
            <div class="git-popover-body" id="git-popover-body"></div>
        `;
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
                    <span class="git-status-code ${statusClass}">${this.escapeHtml(c.status)}</span>
                    <span>${this.escapeHtml(c.file)}</span>
                </div>`;
            }).join('');
        } else {
            body.innerHTML = (this.gitData.commits || []).map(c =>
                `<div class="git-commit-item">
                    <span class="git-commit-hash">${this.escapeHtml(c.hash)}</span>
                    <span class="git-commit-msg">${this.escapeHtml(c.message)}</span>
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
                <span>Project instructions</span>
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

window.LumiSettingsView = LumiSettingsView;
