/* Explicit managed sharing. Local polling observes queued work, never discloses content. */
window.LumiManagedCollaborationView = class LumiManagedCollaborationView {
    _mountManagedCollaboration() {
        this._managedSharingPanels = new Map();
        const section = document.createElement('section');
        section.className = 'swarm-collaboration';
        section.setAttribute('aria-label', 'Managed conversation collaboration');
        this._swarmDialog.querySelector('.swarm-body').insertBefore(section, this._swarmNodes['run-section']);
        this._managedSharingContainer = section;
        this._renderManagedCollaboration();
    }

    _managedSharingKey() {
        const scope = this._swarmScope || {};
        return JSON.stringify([scope.project, scope.session_id, scope.run_id || 'setup']);
    }

    _managedSharingReadFields(action) {
        if (this._swarmExecutionMode !== 'managed' || action !== 'managed_sharing_inspect') return {};
        const panel = this._managedSharingPanels?.get(this._managedSharingKey());
        return panel ? {...(panel._grantId ? {grant_id: panel._grantId} : {}),
            ...(panel._beforeGrant ? {before_grant_id: panel._beforeGrant} : {}),
            ...(panel._beforeMessage ? {before_message_id: panel._beforeMessage} : {})} : {};
    }

    _renderManagedCollaboration() {
        const container = this._managedSharingContainer;
        if (!container) return;
        container.hidden = this._swarmExecutionMode !== 'managed';
        if (container.hidden) return;
        const key = this._managedSharingKey();
        let panel = this._managedSharingPanels.get(key);
        if (!panel) {
            panel = document.createElement('details');
            panel.innerHTML = `<summary>Share with another managed conversation</summary>
                <p class="swarm-help">Offer selected content to another explicitly configured team. Both owners and organization policies must permit sharing. Conversation history and credentials are never shared automatically.</p>
                <p data-ms="availability" role="status"></p><p data-ms="history"></p>
                <form data-ms="prepare"><label>Managed collaboration objective<textarea data-ms="objective" rows="2" maxlength="4000" required></textarea></label>
                <label>Managed collaboration request allowance<input data-ms="allowance" type="number" min="1" max="1000" value="8" step="1" required></label>
                <p class="swarm-help">Prepare an idle team and obtain its remote address. No model starts until you separately accept your own assignment. Preparing does not convert another saved team.</p>
                <button type="submit">Prepare managed collaboration team</button></form>
                <div data-ms="existing" hidden><label>This managed team’s address<input data-ms="address" readonly></label>
                <p class="swarm-help">Copy this opaque address to the other owner. It grants no access by itself. Each recipient uses its own model, policy and allowance.</p>
                <details><summary>Offer a managed sharing agreement</summary><form data-ms="offer">
                <label>Other managed team’s address<input data-ms="peer" required maxlength="36" placeholder="Binding UUID"></label>
                <label>Managed sharing purpose<textarea data-ms="purpose" rows="2" maxlength="1000" required></textarea></label>
                <fieldset><legend>Permitted message types</legend><label><input type="checkbox" data-ms-kind="question"> Questions</label>
                <label><input type="checkbox" data-ms-kind="finding" checked> Findings</label><label><input type="checkbox" data-ms-kind="work_request"> Work proposals</label>
                <label><input type="checkbox" data-ms-kind="work_result"> Work results</label><label><input type="checkbox" data-ms-kind="artifact_offer"> Artifact references</label></fieldset>
                <fieldset><legend>Permitted selected content</legend><label><input type="checkbox" data-ms-class="summary" checked> Selected summaries</label>
                <label><input type="checkbox" data-ms-class="code"> Selected code</label><label><input type="checkbox" data-ms-class="artifact_reference"> Exact artifact reference</label></fieldset>
                <div class="swarm-fields"><label>Managed agreement lifetime in minutes<input data-ms="minutes" type="number" min="1" max="1440" value="30" step="1" required></label>
                <label>Managed maximum messages<input data-ms="max-messages" type="number" min="1" max="1000" value="20" step="1" required></label>
                <label>Managed total disclosure bytes<input data-ms="total-bytes" type="number" min="1" max="1048576" value="65536" step="1" required></label>
                <label>Managed bytes per message<input data-ms="message-bytes" type="number" min="1" max="8192" value="8000" step="1" required></label>
                <label>Managed accepted request limit<input data-ms="requests" type="number" min="0" max="1000" value="4" step="1" required></label></div>
                <p class="swarm-help">Direct delivery only; forwarding is disabled. Either owner can revoke future delivery. The receiver pays from its own request allowance. Stopping or revoking the origin does not cancel already accepted receiver work.</p>
                <button type="submit">Offer managed sharing agreement</button></form></details>
                <button type="button" data-ms="refresh">Refresh managed agreements</button>
                <form data-ms="choose"><label>Managed sharing agreement<select data-ms="grants" aria-label="Managed sharing agreement" required></select></label><button type="submit">Inspect managed agreement</button></form>
                <div class="swarm-controls"><button type="button" data-ms="newest">Newest managed agreements</button><button type="button" data-ms="older">Older managed agreements</button></div>
                <p data-ms="operation" role="status"></p><div data-ms="detail"></div></div>`;
            panel._nodes = Object.fromEntries([...panel.querySelectorAll('[data-ms]')].map(node => [node.dataset.ms, node]));
            panel._details = new Map();
            const n = panel._nodes;
            n.prepare.addEventListener('submit', event => {
                event.preventDefault();
                if (n.prepare.reportValidity()) this.requestSwarm('managed_sharing_prepare', {objective: n.objective.value.trim(), request_limit: Number(n.allowance.value)});
            });
            n.offer.addEventListener('submit', event => {
                event.preventDefault();
                n.peer.setCustomValidity(/^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/.test(n.peer.value.trim()) ? '' : 'Use the exact managed team address shown to its owner.');
                if (!n.offer.reportValidity()) return;
                const kinds = [...panel.querySelectorAll('[data-ms-kind]:checked')].map(node => node.dataset.msKind);
                const data_classes = [...panel.querySelectorAll('[data-ms-class]:checked')].map(node => node.dataset.msClass);
                if (!kinds.length || !data_classes.length) { n.operation.textContent = 'Select at least one message type and content class.'; return; }
                this.requestSwarm('managed_sharing_offer', {receiver_binding: n.peer.value.trim(), terms: {
                    purpose: n.purpose.value.trim(), kinds, data_classes, expires_at: Math.floor(Date.now() / 1000) + Number(n.minutes.value) * 60,
                    payer: 'receiver', revokers: 'either_owner', max_messages: Number(n['max-messages'].value),
                    max_total_bytes: Number(n['total-bytes'].value), max_message_bytes: Number(n['message-bytes'].value),
                    max_requests: Number(n.requests.value), max_hops: 1, max_fanout: 1}});
            });
            n.peer.addEventListener('input', () => n.peer.setCustomValidity(''));
            n.choose.addEventListener('submit', event => {
                event.preventDefault();
                if (!n.choose.reportValidity()) return;
                panel._grantId = n.grants.value; panel._beforeMessage = null;
                this.requestSwarm('managed_sharing_inspect');
            });
            n.grants.addEventListener('change', () => this._renderManagedCollaboration());
            n.refresh.addEventListener('click', () => this.requestSwarm('managed_sharing_inspect'));
            n.newest.addEventListener('click', () => { panel._beforeGrant = null; this.requestSwarm('managed_sharing_inspect'); });
            n.older.addEventListener('click', () => { panel._beforeGrant = panel._nextGrant; this.requestSwarm('managed_sharing_inspect'); });
            this._managedSharingPanels.set(key, panel);
        }
        if (container.firstChild !== panel) container.replaceChildren(panel);
        const state = this._swarmState?.managed_collaboration;
        const available = state?.available || (!this._swarmScope.run_id && this._swarmState?.managed?.available);
        const run = this._swarmState?.run?.run;
        const online = this.ws?.readyState === WebSocket.OPEN;
        const commandPending = Boolean(this._swarmPending && !this._swarmIsRead(this._swarmPending.action));
        const operationPending = ['queued', 'running'].includes(state?.operation?.state);
        const busy = commandPending || operationPending;
        const live = online && !busy && run?.state === 'running' && !this._swarmState?.run?.recovery_needed;
        const n = panel._nodes;
        n.availability.textContent = available ? 'Sharing is opt-in. Agreement terms and message content require explicit inspection; the server checks organization permission for each operation.' : state?.history?.length ? 'Retained sharing receipts are available. Recovered agreements require fresh preparation; previous operations are never replayed automatically.' : 'Managed sharing is unavailable until the operator and organization permit it.';
        n.history.hidden = !state?.history?.length;
        n.history.textContent = (state?.history || []).map(row => `${String(row.action).replace(/_/g, ' ')}: ${({acknowledged: 'server acknowledgement retained', local_dispatch_queued: 'local dispatch queued; inspect worker outcome', unconfirmed: 'outcome unknown; no automatic replay'})[row.outcome] || 'inspect retained outcome'}`).join(' · ');
        n.prepare.hidden = Boolean(this._swarmScope.run_id);
        n.prepare.querySelector('button').disabled = !online || busy || !available || !this._swarmState?.enabled
            || this.currentSessionId !== this._swarmScope.session_id;
        n.existing.hidden = !this._swarmScope.run_id;
        n.address.value = typeof state?.address === 'string' ? state.address : '';
        n.address.placeholder = 'Address appears after successful managed preparation';
        n.offer.querySelector('button').disabled = !live || !state?.address;
        const operation = state?.operation;
        n.operation.textContent = operation ? `${({queued: 'Queued', running: 'In progress', completed: 'Completed', failed: 'Needs inspection', uncertain: 'Outcome unknown'})[operation.state] || 'Needs inspection'}: ${String(operation.action || '').replace(/^managed_sharing_/, '').replace(/_/g, ' ')}${operation.error ? ' · ' + operation.error : ''}.` : 'No sharing operation is running.';
        n.refresh.disabled = !online || busy || !state?.address;
        const grants = state?.grants?.items || [];
        const options = JSON.stringify(grants);
        if (panel._options !== options) {
            const previous = n.grants.value;
            n.grants.replaceChildren(new Option('Choose a managed agreement', ''), ...grants.map(grant => new Option(
                `${grant.direction === 'incoming' ? 'Incoming' : 'Outgoing'} · ${grant.state} · ${grant.grant_id}`, grant.grant_id)));
            n.grants.value = [...n.grants.options].some(option => option.value === previous) ? previous : '';
            panel._options = options;
        }
        n.choose.querySelector('button').disabled = !online || busy || !n.grants.value;
        panel._nextGrant = state?.grants?.next_cursor;
        n.older.disabled = !online || busy || !panel._nextGrant;
        n.newest.disabled = !online || busy || !panel._beforeGrant;
        const accepted = new Set(state?.accepted_work_item_ids || []);
        for (const row of this._swarmWorkers.values()) {
            const retry = row.querySelector('[data-work-retry]');
            if (accepted.has(retry?._target?.work_item_id)) retry.hidden = true;
        }
        for (const [id, row] of this._swarmRetryRows) {
            if (accepted.has(id)) { row.hidden = true; row.querySelector('input').checked = false; }
        }
        const grant = grants.find(item => item.grant_id === panel._grantId);
        if (n.grants.value === panel._grantId && state?.detail?.grant_id === panel._grantId && grant) this._renderManagedSharingDetail(panel, state, grant, live);
        else if (n.detail.childNodes.length) n.detail.replaceChildren();
    }

    _renderManagedSharingDetail(panel, state, grant, live) {
        const detail = state.detail;
        let node = panel._details.get(detail.grant_id);
        if (!node) {
            node = document.createElement('section');
            node.className = 'swarm-collaboration-agreement';
            node.innerHTML = `<h4 data-ms-title></h4><p data-ms-status></p><dl data-ms-terms></dl>
                <button type="button" data-ms-approve>Approve inspected managed agreement</button>
                <button type="button" data-ms-revoke>Revoke managed agreement</button>
                <details><summary>Compose selected managed content</summary><form data-ms-send>
                <label>Managed message type<select data-ms-send-kind aria-label="Managed message type"></select></label><label>Managed content class<select data-ms-send-class aria-label="Managed content class"></select></label>
                <label>Selected managed message content<textarea data-ms-send-body rows="3" maxlength="8192" required></textarea></label>
                <p data-ms-artifact-help class="swarm-help" hidden>Enter a known reference as JSON: {"artifact_id":"UUID","sha256":"64 lowercase hexadecimal characters","byte_size":0}. This shares only the reference. It does not upload a blob or grant artifact access.</p>
                <p class="swarm-help">Only the selected text is uploaded under this agreement. Secret screening can reject it. Sending does not attach it to the receiver’s model or authorize any work.</p>
                <button type="submit">Send selected managed content</button></form></details>
                <h4>Managed messages</h4><p class="swarm-help">This list contains metadata. Read is a separate, authorized content disclosure.</p><div data-ms-messages></div>
                <div class="swarm-controls"><button type="button" data-ms-newest-messages>Newest managed messages</button><button type="button" data-ms-older-messages>Older managed messages</button></div>`;
            node._messages = new Map();
            node.querySelector('[data-ms-approve]').addEventListener('click', () => this.requestSwarm('managed_sharing_approve',
                {grant_id: node._detail.grant_id, terms_sha256: node._detail.terms_sha256}));
            node.querySelector('[data-ms-revoke]').addEventListener('click', () => this.requestSwarm('managed_sharing_revoke', {grant_id: node._detail.grant_id}));
            node.querySelector('[data-ms-send]').addEventListener('submit', event => {
                event.preventDefault();
                const body = node.querySelector('[data-ms-send-body]');
                body.setCustomValidity('');
                if (node.querySelector('[data-ms-send-class]').value === 'artifact_reference') {
                    try {
                        const reference = JSON.parse(body.value);
                        if (!reference || Array.isArray(reference) || Object.keys(reference).sort().join(',') !== 'artifact_id,byte_size,sha256'
                            || typeof reference.artifact_id !== 'string' || !/^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/.test(reference.artifact_id)
                            || typeof reference.sha256 !== 'string' || !/^[0-9a-f]{64}$/.test(reference.sha256)
                            || !Number.isSafeInteger(reference.byte_size) || reference.byte_size < 0) throw new Error('reference');
                    } catch (_) { body.setCustomValidity('Enter the exact artifact UUID, SHA-256 and nonnegative byte size as JSON.'); }
                }
                if (event.target.reportValidity()) this.requestSwarm('managed_sharing_send', {grant_id: node._detail.grant_id,
                    kind: node.querySelector('[data-ms-send-kind]').value, data_class: node.querySelector('[data-ms-send-class]').value,
                    body: node.querySelector('[data-ms-send-body]').value});
            });
            node.querySelector('[data-ms-send-body]').addEventListener('input', event => event.target.setCustomValidity(''));
            node.querySelector('[data-ms-send-class]').addEventListener('change', () => {
                node.querySelector('[data-ms-artifact-help]').hidden = node.querySelector('[data-ms-send-class]').value !== 'artifact_reference';
                node.querySelector('[data-ms-send-body]').setCustomValidity('');
            });
            node.querySelector('[data-ms-newest-messages]').addEventListener('click', () => { panel._beforeMessage = null; this.requestSwarm('managed_sharing_inspect'); });
            node.querySelector('[data-ms-older-messages]').addEventListener('click', () => { panel._beforeMessage = node._nextMessage; this.requestSwarm('managed_sharing_inspect'); });
            panel._details.set(detail.grant_id, node);
        }
        node._detail = detail;
        node._nextMessage = state.messages?.next_cursor;
        if (panel._nodes.detail.firstChild !== node) panel._nodes.detail.replaceChildren(node);
        const terms = detail.terms;
        if (!terms) {
            node.querySelector('[data-ms-title]').textContent = 'Managed sharing agreement';
            node.querySelector('[data-ms-status]').textContent = `Agreement ${grant.state}. Selected content is unavailable.`;
            node.querySelector('[data-ms-terms]').replaceChildren();
            node.querySelector('[data-ms-approve]').hidden = true;
            node.querySelector('[data-ms-revoke]').disabled = true;
            node.querySelector('[data-ms-send]').hidden = true;
            node.querySelector('[data-ms-messages]').replaceChildren();
            node.querySelector('[data-ms-older-messages]').disabled = true;
            node.querySelector('[data-ms-newest-messages]').disabled = true;
            node._termsSha = null;
            return;
        }
        const usable = live && grant.state === 'approved' && terms.expires_at > Date.now() / 1000;
        const incoming = grant.direction === 'incoming';
        node.querySelector('[data-ms-title]').textContent = terms.purpose;
        node.querySelector('[data-ms-status]').textContent = `Agreement ${grant.state}${terms.expires_at <= Date.now() / 1000 ? ' · expired' : ''}. Approval permits selected disclosure; it starts no work.`;
        const termsNode = node.querySelector('[data-ms-terms]');
        if (node._termsSha !== detail.terms_sha256) {
            node._termsSha = detail.terms_sha256;
            termsNode.replaceChildren();
            const values = [['Message types', terms.kinds.join(', ')], ['Content classes', terms.data_classes.join(', ')],
                ['Expiry', new Date(terms.expires_at * 1000).toLocaleString()],
                ['Disclosure limit', `${terms.max_messages} messages; ${terms.max_total_bytes} total bytes; ${terms.max_message_bytes} bytes per message`],
                ['Receiver request limit', `${terms.max_requests}; receiver’s own allowance and model; no transfer of requests or money`],
                ['Revocation', 'Either owner; future deliveries close, accepted work remains receiver-controlled'],
                ['Forwarding', `Maximum ${terms.max_hops} hop; ${terms.max_fanout} direct recipient`], ['Inspected terms fingerprint', detail.terms_sha256]];
            for (const [label, value] of values) { const dt = document.createElement('dt'); dt.textContent = label; const dd = document.createElement('dd'); dd.textContent = value; termsNode.append(dt, dd); }
            node.querySelector('[data-ms-send-kind]').replaceChildren(...terms.kinds.map(value => new Option(value.replace(/_/g, ' '), value)));
            node.querySelector('[data-ms-send-class]').replaceChildren(...terms.data_classes.map(value => new Option(value, value)));
            node.querySelector('[data-ms-artifact-help]').hidden = node.querySelector('[data-ms-send-class]').value !== 'artifact_reference';
        }
        const approve = node.querySelector('[data-ms-approve]');
        approve.hidden = !incoming || grant.state !== 'offered'; approve.disabled = !live || terms.expires_at <= Date.now() / 1000;
        node.querySelector('[data-ms-revoke]').disabled = !live || ['revoked', 'expired', 'deleted'].includes(grant.state);
        node.querySelector('[data-ms-send]').hidden = incoming;
        node.querySelector('[data-ms-send] button').disabled = !usable || incoming || !node.querySelector('[data-ms-send-kind]').value || !node.querySelector('[data-ms-send-class]').value;
        const messages = state.messages?.items || [];
        const container = node.querySelector('[data-ms-messages]');
        for (const message of messages) {
            let row = node._messages.get(message.message_id);
            if (!row) {
                row = document.createElement('article'); row.className = 'swarm-collaboration-message';
                row.innerHTML = `<p data-ms-message-status></p><pre data-ms-content tabindex="0"></pre><button type="button" data-ms-read>Read selected managed message</button>
                    <form data-ms-work><h5>Choose your own managed investigation</h5><label>My managed investigation objective<textarea rows="2" maxlength="4000" required></textarea></label>
                    <label>My managed readable folders<input data-ms-roots value="." required></label><label>My managed request allowance<input data-ms-requests type="number" min="1" max="1000" value="3" step="1" required></label>
                    <label>My managed work acceptance notes<input data-ms-evidence maxlength="2000" required></label>
                    <p class="swarm-help">This starts your own scoped worker under your captured model and remaining allowance. The peer’s text does not define its permissions. Findings require your separate result review.</p>
                    <button type="submit">Accept and start my managed investigation</button></form>`;
                row.querySelector('[data-ms-read]').addEventListener('click', () => this.requestSwarm('managed_sharing_deliver', {grant_id: detail.grant_id, message_id: message.message_id}));
                row.querySelector('[data-ms-work]').addEventListener('submit', event => {
                    event.preventDefault();
                    if (event.target.reportValidity()) this.requestSwarm('managed_sharing_accept_work', {grant_id: detail.grant_id, message_id: message.message_id,
                        objective: row.querySelector('textarea').value.trim(), read_roots: row.querySelector('[data-ms-roots]').value.split(',').map(value => value.trim()).filter(Boolean),
                        requests: Number(row.querySelector('[data-ms-requests]').value), evidence: row.querySelector('[data-ms-evidence]').value.trim()});
                });
                node._messages.set(message.message_id, row);
            }
            const selected = message.state !== 'deleted' && state.selected_content?.message_id === message.message_id ? state.selected_content : null;
            row.querySelector('[data-ms-message-status]').textContent = `${incoming ? 'Incoming' : 'Sent'} · ${message.kind.replace(/_/g, ' ')} · ${message.state} · ${message.message_id}${message.acceptance ? ' · Work acceptance ' + message.acceptance.state + '; result review remains separate' : ''}`;
            row.querySelector('[data-ms-content]').textContent = selected?.body ?? 'Selected content is withheld until you explicitly read it.';
            row.querySelector('[data-ms-read]').hidden = !incoming;
            row.querySelector('[data-ms-read]').disabled = !usable || message.state === 'deleted';
            const form = row.querySelector('[data-ms-work]');
            form.hidden = !incoming || message.kind !== 'work_request' || !selected || Boolean(message.acceptance);
            form.querySelector('button').disabled = !usable || message.state === 'deleted' || Boolean(message.acceptance);
        }
        const visible = messages.map(message => node._messages.get(message.message_id));
        for (const row of [...container.children]) if (!visible.includes(row)) row.remove();
        visible.forEach((row, index) => { if (container.children[index] !== row) container.insertBefore(row, container.children[index] || null); });
        node.querySelector('[data-ms-older-messages]').disabled = !live || !node._nextMessage;
        node.querySelector('[data-ms-newest-messages]').disabled = !live || !panel._beforeMessage;
    }
};
