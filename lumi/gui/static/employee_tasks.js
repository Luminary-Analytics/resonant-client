/* Bounded SONN task controls. Private answers, journals and credentials stay on the host. */
window.LumiEmployeeTasks = class LumiEmployeeTasks {
    bindEmployeeTaskPanel() {
        document.getElementById('employee-task-button')?.addEventListener('click', () => this.openEmployeeTaskPanel());
    }

    openEmployeeTaskPanel() {
        document.getElementById('employee-task-dialog')?.remove();
        const overlay = document.createElement('div');
        overlay.id = 'employee-task-dialog';
        overlay.className = 'dialog-overlay';
        overlay.setAttribute('role', 'dialog');
        overlay.setAttribute('aria-modal', 'true');
        overlay.setAttribute('aria-label', 'Employee task');
        overlay.innerHTML = `<div class="dialog">
            <div class="dialog-header"><h2>Employee task</h2><button type="button" class="dialog-btn deny" data-close aria-label="Close employee task">×</button></div>
            <div class="dialog-body" data-task-content></div></div>`;
        // Styles go through element.style: the page's CSP refuses style="".
        Object.assign(overlay.querySelector('.dialog').style, {width: 'min(560px,94vw)', maxHeight: '90vh', overflow: 'auto'});
        const close = () => {
            overlay.remove();
            this._employeeTaskRequest = null;
            document.getElementById('employee-task-button')?.focus();
        };
        overlay.querySelector('[data-close]').addEventListener('click', close);
        overlay.addEventListener('keydown', event => {
            if (event.key === 'Escape') close();
            if (event.key === 'Tab') {
                const controls = [...overlay.querySelectorAll('button,input,select,textarea')].filter(node => !node.disabled && !node.hidden);
                const first = controls[0], last = controls[controls.length - 1];
                if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
                else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
            }
        });
        document.body.appendChild(overlay);
        overlay.querySelector('[data-close]').focus();
        if (!this.currentSessionId || this.currentBackendName !== 'sonn') {
            overlay.querySelector('[data-task-content]').textContent = 'Open a saved SONN conversation to manage its bounded employee task.';
            return;
        }
        this.requestEmployeeTask('view');
    }

    requestEmployeeTask(action, extra = {}) {
        const content = document.querySelector('#employee-task-dialog [data-task-content]');
        if (!content || this._employeeTaskRequest) return;
        const request = {command: 'employee_task', action, project: this.currentCwd,
            session_id: this.currentSessionId, request_id: crypto.randomUUID(), ...extra};
        this._employeeTaskRequest = request;
        content.querySelectorAll('button,input,select,textarea').forEach(node => {
            node.dataset.taskWasDisabled = String(node.disabled); node.disabled = true;
        });
        let status = content.querySelector('[data-status]');
        if (!status) { status = document.createElement('p'); status.dataset.status = ''; content.appendChild(status); }
        status.setAttribute('role', 'status');
        status.textContent = action === 'advice' ? 'Consulting the approved frontier model…' : 'Loading task state…';
        this.send(request);
    }

    receiveEmployeeTaskState(event) {
        const pending = this._employeeTaskRequest;
        if (!pending || event.request_id !== pending.request_id) return;
        this._employeeTaskRequest = null;
        const content = document.querySelector('#employee-task-dialog [data-task-content]');
        if (!content) return;
        if (event.session_id !== this.currentSessionId || this._normalizeProjectPath(event.project) !== this._normalizeProjectPath(this.currentCwd)) {
            content.textContent = 'The selected conversation changed. Close and reopen its task panel.';
            return;
        }
        if (event.error) {
            content.querySelectorAll('button,input,select,textarea').forEach(node => { node.disabled = node.dataset.taskWasDisabled === 'true'; });
            content.querySelector('[data-status]').textContent = event.error;
            if (!content.querySelector('button')) this.employeeTaskButton(content, 'Refresh task', () => this.requestEmployeeTask('view'));
            return;
        }
        content.replaceChildren();
        document.querySelector('#employee-task-dialog [data-close]')?.focus();
        const paragraph = text => { const p = document.createElement('p'); p.textContent = text; content.appendChild(p); };
        if (!event.available) { paragraph(event.message || 'Task controls are unavailable.'); return; }
        const task = event.task;
        if (!task) {
            if (event.used) {
                paragraph('This task is closed and the conversation uses ordinary per-request limits. Start a fresh conversation for another bounded task.');
                return;
            }
            paragraph('Set one total allowance for the next task. All execution, compression and advice calls share it: at most eight calls, including two consultations, within 15 minutes.');
            const form = document.createElement('form');
            form.innerHTML = `<label>Total task allowance (USD)<input class="settings-input" name="allowance" type="number" min="0.01" max="20" step="0.01" value="2" required></label>
                <label>Task complexity<select class="settings-select" name="complexity"><option value="unknown">Not specified</option><option value="routine">Routine</option><option value="complex">Complex with several dependencies</option></select></label>
                <p>Your saved project model policy applies. The server also enforces your project's request ceiling.</p>
                <button type="submit" class="dialog-btn allow">Start bounded task</button>`;
            form.querySelectorAll('label').forEach(label => { label.style.display = 'grid'; label.style.gap = '8px'; label.style.marginBottom = '16px'; });
            form.addEventListener('submit', event => {
                event.preventDefault();
                const amount = Number(form.elements.allowance.value);
                if (!Number.isFinite(amount) || amount <= 0 || amount > 20) return;
                const choice = form.elements.complexity.value;
                const descriptor = choice === 'unknown' ? {} : choice === 'routine'
                    ? {task_breadth: 1, dependency_breadth: 0, contract_ambiguity: 0}
                    : {task_breadth: 6, dependency_breadth: 6, contract_ambiguity: 0.5};
                this.requestEmployeeTask('start', {ceiling_microusd: Math.round(amount * 1000000), descriptor});
            });
            content.appendChild(form);
            return;
        }
        const quota = task.quota || {};
        paragraph(`Status: ${task.status}${task.deadline_passed ? ' · deadline passed' : ''}`);
        const identity = document.createElement('details');
        const summary = document.createElement('summary'); summary.textContent = 'Task identity';
        const identifier = document.createElement('code'); identifier.textContent = task.id;
        identity.append(summary, identifier); content.appendChild(identity);
        paragraph(`Charged $${((quota.charged_microusd || 0) / 1000000).toFixed(4)} of $${(task.ceiling_microusd / 1000000).toFixed(2)}. Reserved allowance: $${((quota.allocated_microusd || 0) / 1000000).toFixed(4)}.`);
        paragraph(`Calls used or reserved: ${quota.claimed_calls || 0}/${task.call_limit}. Consultations: ${quota.claimed_advisory || 0}/${task.advisory_limit}.`);
        paragraph(`Deadline: ${new Date(task.deadline * 1000).toLocaleString()}`);
        if (event.pending || task.unresolved_attempts || task.unresolved_preflights) paragraph('An original request needs reconciliation. Recovery checks its saved result and does not repeat a model call.');
        if (event.advice_status === 'awaiting') paragraph('Recorded advice is ready for the next execution. Compression will not consume it.');
        this.employeeTaskButton(content, 'Refresh task', () => this.requestEmployeeTask('view'));
        this.employeeTaskButton(content, 'Recover original requests', () => this.requestEmployeeTask('recover'));
        this.employeeTaskButton(content, 'Inspect assignments', () => this.requestEmployeeTask('graph'));
        if (Object.hasOwn(event, 'graph')) this.renderEmployeeAssignments(content, event.graph);
        if (task.status === 'active' && !task.deadline_passed) {
            this.employeeTaskButton(content, 'Cancel task', () => this.requestEmployeeTask('cancel'));
            const form = document.createElement('form');
            form.innerHTML = `<label>Consultation purpose<select class="settings-select" name="purpose"><option value="advise">Execution advice</option><option value="coordinate">Employee coordination</option></select></label>
                <label>Question for the frontier advisor<textarea class="settings-input" name="question" rows="4" maxlength="8192" required></textarea></label>
                <p>Requires your saved frontier-advice consent. One consultation uses the task's allowance. Its answer is unverified advice for the next execution.</p>
                <button type="submit" class="dialog-btn allow">Consult frontier advisor</button>`;
            Object.assign(form.querySelector('select').style, {display: 'block', width: '100%', margin: '8px 0 16px'});
            Object.assign(form.elements.question.style, {display: 'block', width: '100%', boxSizing: 'border-box'});
            const canConsult = !event.pending && event.advice_status !== 'awaiting' && event.advice_status !== 'following' && (quota.claimed_advisory || 0) < task.advisory_limit;
            form.querySelector('button').disabled = !canConsult;
            form.addEventListener('submit', submit => { submit.preventDefault(); this.requestEmployeeTask('advice', {
                question: form.elements.question.value, consultation_mode: form.elements.purpose.value}); });
            content.appendChild(form);
        } else {
            this.employeeTaskButton(content, 'Return to ordinary requests', () => this.requestEmployeeTask('detach'));
        }
    }

    renderEmployeeAssignments(content, graph) {
        const section = document.createElement('section');
        const heading = document.createElement('h3'); heading.textContent = 'Assignments';
        section.appendChild(heading); content.appendChild(section);
        const line = (parent, text) => { const p = document.createElement('p'); p.textContent = text; parent.appendChild(p); };
        if (!graph) { line(section, 'This task has no specialist assignments.'); return; }
        line(section, `Graph: ${graph.status}. Parent task: ${graph.root_status}${graph.deadline_passed ? ' · deadline passed' : ''}.`);
        const list = document.createElement('ol'); section.appendChild(list);
        for (const node of graph.nodes) {
            const item = document.createElement('li'); item.style.overflowWrap = 'anywhere'; list.appendChild(item);
            const title = document.createElement('strong');
            title.textContent = `${node.id}${node.integration ? ' · coordinator integration' : ' · specialist'}`;
            item.appendChild(title);
            line(item, `Employee: ${node.employee_id}. Status: ${node.status}. Execution: ${node.epoch}.`);
            if (node.status === 'reported') line(item, 'Output reported. Independent verification is still required.');
            if (node.status === 'failed') line(item, 'Verification failed. Host recovery is required before another execution.');
            if (node.lease_expired) line(item, 'Execution lease expired. The host must confirm the original worker stopped before reassigning it.');
            if (node.unresolved_file_actions) line(item, `${node.unresolved_file_actions} original file action(s) need host reconciliation.`);
            if (node.blocked_by.length) line(item, `Waiting for: ${node.blocked_by.join(', ')}.`);
            if (node.prior_executions) line(item, `Prior executions retained: ${node.prior_executions}.`);
            const details = document.createElement('details');
            const summary = document.createElement('summary'); summary.textContent = 'Assignment contract';
            details.appendChild(summary);
            line(details, node.input_contract);
            line(details, `Expected outputs: ${node.outputs.join(', ')}.`);
            line(details, `Dependencies: ${node.dependencies.join(', ') || 'None'}.`);
            item.appendChild(details);
        }
    }

    employeeTaskButton(parent, label, action) {
        const button = document.createElement('button');
        button.className = 'dialog-btn deny';
        button.type = 'button'; button.textContent = label;
        button.style.margin = '8px 8px 16px 0';
        button.addEventListener('click', action); parent.appendChild(button);
    }
};
