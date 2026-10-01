/* Explicit personal conversation sharing; every request uses Team's captured scope. */
window.LumiCollaborationView = class LumiCollaborationView {
    _mountCollaboration() {
        this._collaborationPanels = new Map();
        const section = document.createElement('section');
        section.className = 'swarm-collaboration';
        section.setAttribute('aria-label', 'Personal conversation collaboration');
        this._swarmDialog.querySelector('.swarm-body').insertBefore(section, this._swarmNodes['run-section']);
        this._collaborationContainer = section;
        this._renderCollaboration();
    }

    _collaborationPanel() {
        return this._collaborationPanels?.get(this._swarmScope?.run_id || 'setup');
    }

    _collaborationReadFields(action) {
        const panel = this._collaborationPanel();
        if (!['view', 'events', 'collaboration_inspect'].includes(action) || !panel) return {};
        return {...(panel._grantId ? {grant_id: panel._grantId} : {}),
            ...(panel._beforeGrant ? {before_grant_id: panel._beforeGrant} : {}),
            ...(panel._beforeMessage ? {before_message_id: panel._beforeMessage} : {})};
    }

    _renderCollaboration() {
        if (!this._collaborationContainer) return;
        this._collaborationContainer.hidden = this._swarmExecutionMode === 'managed';
        if (this._collaborationContainer.hidden) return;
        const scope = this._swarmScope;
        const snapshot = this._swarmState?.run;
        const run = snapshot?.run;
        const online = this.ws?.readyState === WebSocket.OPEN;
        const pending = Boolean(this._swarmPending && !this._swarmIsRead(this._swarmPending.action));
        const live = online && !pending && run?.state === 'running' && !snapshot.recovery_needed;
        const key = scope.run_id || 'setup';
        let panel = this._collaborationPanels.get(key);
        if (!panel) {
            panel = document.createElement('details');
            panel.innerHTML = `<summary>Share with another personal conversation</summary>
                <p class="swarm-help">Explicit sharing with your other conversations in this project. Each team keeps its own model, credentials and request allowance. No conversation history is shared automatically.</p>
                <form data-collab="prepare"><label>Collaboration objective<textarea data-collab="objective" required maxlength="4000" rows="2"></textarea></label>
                <label>Collaboration total request allowance<input data-collab="allowance" type="number" value="8" min="1" max="1000" step="1" required></label>
                <p class="swarm-help">Prepare an idle, read-only team. No model runs until you separately accept work. You can switch saved conversations while these teams have no active or uncertain work.</p>
                <button type="submit">Prepare collaboration team</button></form>
                <div data-collab="existing" hidden><label>This conversation’s team address<input data-collab="address" readonly></label>
                <p class="swarm-help">Copy this address into the other saved conversation’s team panel. Controls here always belong to this conversation.</p>
                <details><summary>Offer a sharing agreement</summary><form data-collab="offer">
                <label>Other conversation’s team address<input data-collab="peer" required placeholder="conversation-id/team-id"></label>
                <label>Sharing purpose<textarea data-collab="purpose" required rows="2" maxlength="1000"></textarea></label>
                <fieldset><legend>Allowed messages</legend><label><input type="checkbox" data-kind="question"> Questions</label><label><input type="checkbox" data-kind="finding" checked> Findings</label>
                <label><input type="checkbox" data-kind="work_request"> Work proposals</label><label><input type="checkbox" data-kind="work_result"> Work results</label><label><input type="checkbox" data-kind="artifact_offer"> Artifact reference metadata</label></fieldset>
                <fieldset><legend>Allowed content</legend><label><input type="checkbox" data-class="summary" checked> Selected summaries</label><label><input type="checkbox" data-class="code"> Selected code</label>
                <label><input type="checkbox" data-class="artifact_reference"> Artifact IDs, hashes and sizes only</label></fieldset>
                <div class="swarm-fields"><label>Expires after minutes<input data-collab="minutes" type="number" min="1" max="1440" value="30" required></label>
                <label>Maximum messages<input data-collab="messages" type="number" min="1" max="1000" value="20" required></label>
                <label>Total disclosure bytes<input data-collab="bytes" type="number" min="1" max="1048576" value="65536" required></label>
                <label>Bytes per message<input data-collab="message-bytes" type="number" min="1" max="65536" value="8000" required></label>
                <label>Maximum accepted requests<input data-collab="requests" type="number" min="0" max="1000" value="4" required></label></div>
                <p class="swarm-help">One direct recipient; forwarding is disabled. Either team can revoke. The receiving team pays from its own allowance; no money or requests transfer. Stop or revocation closes future deliveries, and does not cancel already accepted peer work.</p>
                <button type="submit">Offer sharing agreement</button></form></details>
                <form data-collab="choose"><label>Sharing agreement<select data-collab="grants" aria-label="Sharing agreement" required></select></label><button type="submit">Inspect agreement</button></form>
                <div class="swarm-controls"><button type="button" data-collab="newer">Newest agreements</button><button type="button" data-collab="older">Older agreements</button></div>
                <p data-collab="status" role="status"></p><div data-collab="detail"></div></div>`;
            panel._nodes = Object.fromEntries([...panel.querySelectorAll('[data-collab]')].map(node => [node.dataset.collab, node]));
            panel._details = new Map();
            const n = panel._nodes;
            n.prepare.addEventListener('submit', event => {
                event.preventDefault();
                if (n.prepare.reportValidity()) this.requestSwarm('collaboration_prepare', {objective: n.objective.value.trim(), request_limit: Number(n.allowance.value)});
            });
            n.offer.addEventListener('submit', event => {
                event.preventDefault();
                const address = n.peer.value.trim().split('/');
                n.peer.setCustomValidity(address.length === 2 && address.every(Boolean) ? '' : 'Use the exact conversation/team address shown in its panel.');
                if (!n.offer.reportValidity()) return;
                const kinds = [...panel.querySelectorAll('[data-kind]:checked')].map(node => node.dataset.kind);
                const data_classes = [...panel.querySelectorAll('[data-class]:checked')].map(node => node.dataset.class);
                if (!kinds.length || !data_classes.length) { n.status.textContent = 'Choose at least one message and content type.'; return; }
                this.requestSwarm('collaboration_offer', {receiver_session_id: address[0], receiver_run_id: address[1], terms: {
                    purpose: n.purpose.value.trim(), kinds, data_classes, expires_at: Date.now() / 1000 + Number(n.minutes.value) * 60,
                    revoker_run_ids: [scope.run_id, address[1]], max_messages: Number(n.messages.value), max_total_bytes: Number(n.bytes.value),
                    max_message_bytes: Number(n['message-bytes'].value), max_requests: Number(n.requests.value), max_hops: 1, max_fanout: 1,
                    cost_limit_usd: null, payer: 'receiving_run', cancellation: 'close_future_admission'}});
            });
            n.peer.addEventListener('input', () => n.peer.setCustomValidity(''));
            n.choose.addEventListener('submit', event => {
                event.preventDefault();
                if (!n.choose.reportValidity()) return;
                panel._grantId = n.grants.value; panel._beforeMessage = null;
                this.requestSwarm('collaboration_inspect');
            });
            n.older.addEventListener('click', () => { panel._beforeGrant = panel._nextGrant; this.requestSwarm('collaboration_inspect'); });
            n.newer.addEventListener('click', () => { panel._beforeGrant = null; this.requestSwarm('collaboration_inspect'); });
            this._collaborationPanels.set(key, panel);
        }
        if (this._collaborationContainer.firstChild !== panel) this._collaborationContainer.replaceChildren(panel);
        const n = panel._nodes;
        n.prepare.hidden = Boolean(scope.run_id);
        n.prepare.querySelector('button').disabled = !online || pending || !this._swarmState?.enabled || !this._swarmState?.available
            || this.currentSessionId !== scope.session_id;
        n.existing.hidden = !scope.run_id;
        n.address.value = `${scope.session_id}/${scope.run_id}`;
        for (const button of n.offer.querySelectorAll('button')) button.disabled = !live;
        const state = this._swarmState?.collaboration;
        if (state?.address?.run_id !== scope.run_id) return;
        const boundWork = new Set(state.accepted_work_item_ids || []);
        for (const row of this._swarmWorkers.values()) {
            const retry = row.querySelector('[data-work-retry]');
            if (boundWork.has(retry?._target?.work_item_id)) retry.hidden = true;
        }
        for (const [id, row] of this._swarmRetryRows) {
            if (boundWork.has(id)) { row.hidden = true; row.querySelector('input').checked = false; }
        }
        const options = JSON.stringify(state.grants);
        if (panel._options !== options) {
            const previous = n.grants.value;
            n.grants.replaceChildren(new Option('Choose an agreement', ''), ...state.grants.map(grant => new Option(
                `${grant.receiver_run_id === scope.run_id ? 'Incoming' : 'Outgoing'} · ${grant.purpose.slice(0, 80)} · ${grant.state}`, grant.grant_id)));
            n.grants.value = [...n.grants.options].some(option => option.value === previous) ? previous : '';
            panel._options = options;
        }
        panel._nextGrant = state.next_before_grant_id;
        n.older.disabled = !online || pending || !panel._nextGrant;
        n.newer.disabled = !online || pending || !panel._beforeGrant;
        n.choose.querySelector('button').disabled = !online || pending;
        n.status.textContent = `${state.grants.length} agreements on this page. Pending message content is disclosed only after Read message.`;
        if (state.detail && state.detail.grant_id === panel._grantId) this._renderCollaborationDetail(panel, state.detail, live);
        else if (!panel._grantId) n.detail.replaceChildren();
    }

    _renderCollaborationDetail(panel, detail, live) {
        let node = panel._details.get(detail.grant_id);
        if (!node) {
            node = document.createElement('section');
            node.className = 'swarm-collaboration-agreement';
            node.innerHTML = `<h4 data-c-detail-title></h4><p data-c-detail-status role="status"></p><dl data-c-terms></dl>
                <button type="button" data-c-approve>Accept sharing agreement</button>
                <form data-c-revoke><label>Reason to revoke<input required maxlength="2000"></label><button type="submit">Revoke sharing agreement</button></form>
                <details><summary>Compose an explicit message</summary><form data-c-send>
                <label>Message type<select data-c-kind aria-label="Message type"></select></label><label>Content class<select data-c-class aria-label="Content class"></select></label>
                <label>Selected message content<textarea data-c-body rows="3" maxlength="32000"></textarea></label>
                <label>Artifact reference IDs, separated by commas<input data-c-artifacts></label>
                <p class="swarm-help">Artifact offers disclose IDs, hashes and sizes only. They give no blob access or visual interpretation. No worker receives this text automatically.</p>
                <button type="submit">Send selected content</button></form></details>
                <h4>Messages and explicit receipts</h4><div data-c-messages></div>
                <div class="swarm-controls"><button type="button" data-c-newest>Newest messages</button><button type="button" data-c-older>Older messages</button></div>`;
            node._messages = new Map();
            node.querySelector('[data-c-approve]').addEventListener('click', () => this.requestSwarm('collaboration_approve', {grant_id: detail.grant_id, terms_sha256: detail.terms_sha256}));
            node.querySelector('[data-c-revoke]').addEventListener('submit', event => {
                event.preventDefault();
                if (event.target.reportValidity()) this.requestSwarm('collaboration_revoke', {grant_id: detail.grant_id, evidence: event.target.querySelector('input').value.trim()});
            });
            node.querySelector('[data-c-send]').addEventListener('submit', event => {
                event.preventDefault();
                if (!event.target.reportValidity()) return;
                this.requestSwarm('collaboration_send', {grant_id: detail.grant_id, kind: node.querySelector('[data-c-kind]').value,
                    data_class: node.querySelector('[data-c-class]').value, body: node.querySelector('[data-c-body]').value,
                    artifact_ids: node.querySelector('[data-c-artifacts]').value.split(',').map(value => value.trim()).filter(Boolean)});
            });
            node.querySelector('[data-c-newest]').addEventListener('click', () => { panel._beforeMessage = null; this.requestSwarm('collaboration_inspect'); });
            node.querySelector('[data-c-older]').addEventListener('click', () => { panel._beforeMessage = node._nextMessage; this.requestSwarm('collaboration_inspect'); });
            panel._details.set(detail.grant_id, node);
        }
        if (panel._nodes.detail.firstChild !== node) panel._nodes.detail.replaceChildren(node);
        const terms = detail.terms;
        const runId = this._swarmScope.run_id;
        const usable = live && detail.state === 'active' && terms.expires_at > Date.now() / 1000;
        node.querySelector('[data-c-detail-title]').textContent = terms.purpose;
        node.querySelector('[data-c-detail-status]').textContent = `Agreement ${detail.state}${terms.expires_at <= Date.now() / 1000 ? ' · expired' : ''}. Receiver approval grants no work execution.`;
        const termsNode = node.querySelector('[data-c-terms]');
        if (!termsNode.childNodes.length) {
            const entries = [['Messages', terms.kinds.join(', ')], ['Content', terms.data_classes.join(', ')], ['Expires', new Date(terms.expires_at * 1000).toLocaleString()],
                ['Disclosure limits', `${terms.max_messages} messages; ${terms.max_total_bytes} bytes total; ${terms.max_message_bytes} bytes each`],
                ['Work allowance', `${terms.max_requests} requests, paid by receiving team from its own total; no dollar limit`],
                ['Forwarding', terms.max_hops === 1 ? 'Direct delivery only; no forwarding' : `Up to ${terms.max_hops} deliveries along a forwarding chain; ${terms.max_fanout} recipients per message`],
                ['Revokers', terms.revoker_run_ids.map(id => id === detail.origin_run_id ? 'Origin team' : 'Receiving team').join(', ')],
                ['Cancellation', 'Future deliveries close; accepted peer work continues under its own controls'], ['Agreement fingerprint', detail.terms_sha256]];
            for (const [label, value] of entries) { const dt = document.createElement('dt'); dt.textContent = label; const dd = document.createElement('dd'); dd.textContent = value; termsNode.append(dt, dd); }
            const labels = {question: 'Question', finding: 'Finding', work_request: 'Work proposal', work_result: 'Work result', artifact_offer: 'Artifact reference metadata', summary: 'Selected summary', code: 'Selected code', artifact_reference: 'Artifact reference metadata'};
            node.querySelector('[data-c-kind]').replaceChildren(...terms.kinds.map(value => new Option(labels[value] || value, value)));
            node.querySelector('[data-c-class]').replaceChildren(...terms.data_classes.map(value => new Option(labels[value] || value, value)));
        }
        const approve = node.querySelector('[data-c-approve]');
        approve.hidden = detail.receiver_run_id !== runId || detail.state !== 'offered';
        approve.disabled = !live || terms.expires_at <= Date.now() / 1000;
        node.querySelector('[data-c-revoke] button').disabled = !live || detail.state === 'revoked' || !terms.revoker_run_ids.includes(runId);
        node.querySelector('[data-c-send] button').disabled = !usable;
        const container = node.querySelector('[data-c-messages]');
        for (const message of detail.messages) {
            let row = node._messages.get(message.id);
            if (!row) {
                row = document.createElement('article'); row.className = 'swarm-collaboration-message';
                row.innerHTML = `<p data-c-message-status></p><pre data-c-content tabindex="0"></pre><p data-c-references></p><button type="button" data-c-deliver>Read message</button>
                    <form data-c-work><h5>Choose your own read-only assignment</h5><label>Accepted investigation<textarea required rows="2" maxlength="4000"></textarea></label>
                    <label>Accepted readable folders<input data-c-roots required value="."></label><label>Accepted request allowance<input data-c-requests type="number" value="3" min="1" max="1000" step="1" required></label>
                    <label>Work acceptance notes<input data-c-evidence required maxlength="2000"></label><p class="swarm-help">Uses this conversation’s captured model and remaining allowance. Findings require separate owner review. Accepting may start a worker immediately.</p><button type="submit">Accept and start my investigation</button></form>`;
                row.querySelector('[data-c-deliver]').addEventListener('click', () => this.requestSwarm('collaboration_deliver', {grant_id: detail.grant_id, message_id: message.id}));
                row.querySelector('[data-c-work]').addEventListener('submit', event => {
                    event.preventDefault();
                    if (event.target.reportValidity()) this.requestSwarm('collaboration_accept_work', {grant_id: detail.grant_id, message_id: message.id,
                        objective: row.querySelector('textarea').value.trim(), read_roots: row.querySelector('[data-c-roots]').value.split(',').map(value => value.trim()).filter(Boolean),
                        requests: Number(row.querySelector('[data-c-requests]').value), evidence: row.querySelector('[data-c-evidence]').value.trim()});
                });
                node._messages.set(message.id, row);
            }
            const incoming = message.recipient_run_id === runId;
            const kindLabel = {question: 'Question', finding: 'Finding', work_request: 'Work proposal', work_result: 'Work result', artifact_offer: 'Artifact reference metadata'}[message.kind] || 'Message';
            row.querySelector('[data-c-message-status]').textContent = `${incoming ? 'Incoming message' : 'Sent content'} · ${kindLabel} · ${message.acceptance ? 'Work accepted under receiver ownership' : message.delivered_at ? 'Explicitly opened; no automatic model delivery' : 'Queued; not delivered'}`;
            row.querySelector('[data-c-content]').textContent = message.body ?? 'Content is withheld until explicit delivery.';
            row.querySelector('[data-c-references]').textContent = (message.artifacts || []).map(ref => `Reference ${ref.id} · SHA-256 ${ref.sha256} · ${ref.size} bytes`).join('\n');
            const read = row.querySelector('[data-c-deliver]'); read.hidden = !incoming || Boolean(message.delivered_at); read.disabled = !usable;
            const work = row.querySelector('[data-c-work]'); work.hidden = !incoming || message.kind !== 'work_request' || !message.delivered_at || Boolean(message.acceptance);
            work.querySelector('button').disabled = !usable;
        }
        const visible = detail.messages.map(message => node._messages.get(message.id));
        // Insert new arrivals without detaching retained forms: a peer's poll
        // update must not take focus from the owner's assignment draft.
        for (const row of [...container.children]) if (!visible.includes(row)) row.remove();
        visible.forEach((row, index) => { if (container.children[index] !== row) container.insertBefore(row, container.children[index] || null); });
        node._nextMessage = detail.next_before_message_id;
        node.querySelector('[data-c-older]').disabled = !node._nextMessage || Boolean(this._swarmPending);
        node.querySelector('[data-c-newest]').disabled = !panel._beforeMessage || Boolean(this._swarmPending);
    }
};
