/*
 * Send feedback: the dialog behind Help > Send Feedback…, the command palette,
 * About Lumi and the profile menu (lumi/feedback.py, docs/feedback.md).
 *
 * The page never builds the report: lumi/feedback.py does. It removes secrets,
 * applies the organization's DLP rules and, with "Include diagnostics",
 * returns the report exactly as it would be sent (feedback_preview). This view
 * shows that report, and Send passes its preview_id, so what was shown is what
 * goes. Any change to the form makes the shown report stale, and Send waits
 * for a fresh one.
 *
 * A refusal (offline mode, DLP, too many reports, a full queue, or Lumi Cloud
 * saying no) is shown with its reason, and Copy to clipboard offers the
 * report as text instead.
 *
 * window.LumiFeedback holds the checks and wording, so tests can run them
 * without a page (tests/feedback_view.test.cjs). LumiFeedbackView is mixed
 * into LumiApp by applyMixin in app.js, so this file loads before app.js.
 */
(function () {
    'use strict';

    const KINDS = ['bug', 'idea', 'other'];
    const MAX_MESSAGE = 5000;
    const MAX_REPLY_TO = 254;
    // lumi/feedback.py's EMAIL, so the dialog can say what's wrong before anything is sent.
    const EMAIL = /^(?=.{3,254}$)(?!\.)(?!.*\.\.)[A-Za-z0-9._%+'-]{1,64}(?<!\.)@(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,63}$/;
    const PREVIEW_DELAY_MS = 400;
    const FIELDS = {kind: 'feedback-kind-bug', message: 'feedback-message', reply_to: 'feedback-reply-to'};

    const number = value => Number(value || 0).toLocaleString('en-US');
    const plural = (count, word) => `${number(count)} ${word}${Number(count) === 1 ? '' : 's'}`;

    /** The form as lumi/feedback.py reads it, and what's wrong with it: {form, errors, ok}. */
    function check(fields) {
        const values = fields || {};
        const form = {
            kind: KINDS.includes(values.kind) ? values.kind : '',
            message: String(values.message ?? ''),
            reply_to: String(values.reply_to ?? '').trim(),
            include_diagnostics: values.include_diagnostics === true,
        };
        const errors = {};
        if (!form.kind) errors.kind = 'Choose Bug, Idea or Other.';
        const message = form.message.trim();
        // Characters as Python counts them (code points), not UTF-16 units.
        if (!message) errors.message = 'Write what happened, or what you’d like.';
        else if (Array.from(message).length > MAX_MESSAGE) errors.message = `Keep the message to ${number(MAX_MESSAGE)} characters.`;
        if (form.reply_to && (form.reply_to.length > MAX_REPLY_TO || !EMAIL.test(form.reply_to))) {
            errors.reply_to = 'Enter an email address such as you@example.com, or leave it empty.';
        }
        return {form, errors, ok: Object.keys(errors).length === 0};
    }

    function countLabel(message) {
        return `${number(Array.from(String(message ?? '')).length)} / ${number(MAX_MESSAGE)}`;
    }

    /** The report as the dialog shows it: the JSON Lumi sends, indented. */
    function previewText(body) {
        return JSON.stringify(body ?? null, null, 2);
    }

    function alwaysSent(app) {
        if (!app || !app.version) return '';
        return `Always sent: Lumi ${app.version} (${app.channel}), ${app.os}, ${app.arch}, and a random id for this install.`;
    }

    /** Where a report goes, and as whom, before anything is sent. */
    function destination(status) {
        const s = status || {};
        if (s.offline) return s.offline;
        if (!s.destination) {
            return 'No Lumi Cloud address is set, so your report waits on this computer until one is (Settings > Lumi account).';
        }
        return s.account ? `It goes to ${s.destination} as ${s.account}.` : `It goes to ${s.destination}, without your account.`;
    }

    function previewMeta(data) {
        const d = data || {};
        if (d.offline) return d.offline;
        const where = d.destination ? `To ${d.destination}` : 'Kept on this computer until a Lumi Cloud address is set';
        return `${where}, ${d.account ? `as ${d.account}` : 'without your account'}.`;
    }

    function queueText(count) {
        const n = Number(count || 0);
        return n > 0 ? `${plural(n, 'report')} waiting on this computer.` : '';
    }

    /** What Send now did: counts from lumi/feedback.flush. */
    function flushText(result) {
        const r = result || {};
        if (r.busy) return 'Lumi is already sending the waiting reports.';
        if (r.failed) return 'Lumi couldn’t send the waiting reports. It will try again later.';
        const parts = [];
        if (r.sent) parts.push(`Sent ${plural(r.sent, 'report')}.`);
        if (r.dropped) parts.push(`${plural(r.dropped, 'report')} couldn’t be sent and ${Number(r.dropped) === 1 ? 'was' : 'were'} removed.`);
        if (r.waiting) parts.push(`${plural(r.waiting, 'report')} still waiting: Lumi Cloud can’t take ${Number(r.waiting) === 1 ? 'it' : 'them'} now.`);
        return parts.join(' ') || 'Nothing is waiting.';
    }

    const byId = id => document.getElementById(id);

    class LumiFeedbackView {
        /** Open the dialog; focus returns to ``returnFocus`` (or what had it) when it closes. */
        openFeedbackDialog(returnFocus = null) {
            const dialog = byId('feedback-dialog');
            if (!dialog) return;
            this._feedbackReturnFocus = returnFocus || document.activeElement;
            this._wireFeedbackDialog(dialog);
            this._showFeedbackForm();
            this._feedbackSignature = JSON.stringify(this._feedbackFields());
            dialog.style.display = 'flex';
            this.send({command: 'feedback_status'});
            this._feedbackCountChanged();
            if (byId('feedback-diagnostics')?.checked) this._requestFeedbackPreview();
            byId('feedback-message')?.focus();
        }

        closeFeedbackDialog() {
            const dialog = byId('feedback-dialog');
            if (!dialog || dialog.style.display === 'none') return;
            dialog.style.display = 'none';
            clearTimeout(this._feedbackPreviewTimer);
            const target = this._feedbackReturnFocus;
            this._feedbackReturnFocus = null;
            if (target && target.isConnected !== false && typeof target.focus === 'function') target.focus();
        }

        _feedbackOpen() {
            const dialog = byId('feedback-dialog');
            return !!dialog && dialog.style.display === 'flex';
        }

        _wireFeedbackDialog(dialog) {
            if (dialog.dataset.wired) return;
            dialog.dataset.wired = '1';
            const on = (id, type, handler) => byId(id)?.addEventListener(type, handler);
            on('feedback-dialog-close', 'click', () => this.closeFeedbackDialog());
            on('feedback-cancel', 'click', () => this.closeFeedbackDialog());
            on('feedback-done-close', 'click', () => this.closeFeedbackDialog());
            on('feedback-another', 'click', () => { this._showFeedbackForm(); byId('feedback-message')?.focus(); });
            dialog.addEventListener('click', event => { if (event.target === dialog) this.closeFeedbackDialog(); });
            dialog.addEventListener('keydown', event => this._feedbackKeydown(event, dialog));
            on('feedback-form', 'submit', event => { event.preventDefault(); this._sendFeedback(); });
            on('feedback-form', 'input', event => this._feedbackEdited(event));
            on('feedback-form', 'change', event => this._feedbackEdited(event));
            on('feedback-message', 'keydown', event => {
                if (event.key === 'Enter' && (event.ctrlKey || event.metaKey)) { event.preventDefault(); this._sendFeedback(); }
            });
            on('feedback-copy', 'click', () => this._copyFeedback());
            on('feedback-flush', 'click', () => {
                // Not disabled while it works: a disabled button would drop the focus out of the dialog.
                if (this._feedbackFlushing) return;
                this._feedbackFlushing = true;
                byId('feedback-flush')?.setAttribute('aria-busy', 'true');
                this._feedbackNote('Sending the waiting reports…');
                this.send({command: 'feedback_flush'});
            });
            // Escape still closes the dialog when focus fell out of it (a button that was hidden).
            document.addEventListener('keydown', event => {
                if (event.key === 'Escape' && this._feedbackOpen() && !dialog.contains(event.target)) {
                    event.preventDefault();
                    event.stopPropagation();
                    this.closeFeedbackDialog();
                }
            }, true);
            on('feedback-discard', 'click', () => {
                const count = Number(this._feedbackStatus?.waiting || 0);
                if (!count || !window.confirm(`Delete ${plural(count, 'report')} waiting on this computer? They won’t be sent.`)) return;
                this.send({command: 'feedback_discard'});
            });
        }

        /** Escape closes; Tab and Shift+Tab stay inside the dialog. */
        _feedbackKeydown(event, dialog) {
            if (event.key === 'Escape') {
                event.preventDefault();
                event.stopPropagation();
                this.closeFeedbackDialog();
                return;
            }
            if (event.key !== 'Tab') return;
            const stops = Array.from(dialog.querySelectorAll('button, input, textarea, select, [tabindex]:not([tabindex="-1"])'))
                .filter(el => !el.disabled && !el.closest('[hidden]') && el.getClientRects().length > 0
                    && (el.type !== 'radio' || el.checked));
            if (!stops.length) return;
            const first = stops[0];
            const last = stops[stops.length - 1];
            if (event.shiftKey && (document.activeElement === first || !dialog.contains(document.activeElement))) {
                event.preventDefault();
                last.focus();
            } else if (!event.shiftKey && (document.activeElement === last || !dialog.contains(document.activeElement))) {
                event.preventDefault();
                first.focus();
            }
        }

        _feedbackFields() {
            const kind = Array.from(document.getElementsByName('feedback-kind')).find(input => input.checked);
            return {
                kind: kind ? kind.value : '',
                message: byId('feedback-message')?.value || '',
                reply_to: byId('feedback-reply-to')?.value || '',
                include_diagnostics: !!byId('feedback-diagnostics')?.checked,
            };
        }

        _feedbackCountChanged() {
            const count = byId('feedback-message-count');
            if (count) count.textContent = countLabel(byId('feedback-message')?.value);
        }

        _feedbackEdited(event) {
            if (event?.target?.id === 'feedback-message') this._feedbackCountChanged();
            // "change" also fires when a field only loses focus (on the way to Send):
            // only a real change makes the shown report stale.
            const fields = this._feedbackFields();
            const signature = JSON.stringify(fields);
            if (signature === this._feedbackSignature) return;
            this._feedbackSignature = signature;
            // An error clears as soon as its field is right; a new one waits for Send.
            const {errors} = check(fields);
            for (const field of Object.keys(FIELDS)) if (!errors[field]) this._setFeedbackFieldError(field, '');
            if (this._feedbackNoteField) {
                // The line that announced an error goes once that field is right.
                if (!errors[this._feedbackNoteField]) this._feedbackNote('');
            } else if (!this._feedbackFlushing) {
                this._feedbackNote('');  // "Copied." and the like were about the form as it was
            }
            this._hideFeedbackAlert();
            this._scheduleFeedbackPreview();
        }

        _setFeedbackFieldError(field, message) {
            const error = byId(`feedback-${field.replace('_', '-')}-error`);
            const input = field === 'kind' ? byId('feedback-kind-bug') : byId(FIELDS[field]);
            if (error) { error.textContent = message; error.hidden = !message; }
            if (input) {
                if (message) input.setAttribute('aria-invalid', 'true');
                else input.removeAttribute('aria-invalid');
            }
        }

        _showFeedbackFieldErrors(errors) {
            for (const field of Object.keys(FIELDS)) this._setFeedbackFieldError(field, errors[field] || '');
            const first = Object.keys(FIELDS).find(field => errors[field]);
            if (!first) return;
            // Focus may already be in that field, where its description isn't read again: say it.
            this._feedbackNote(errors[first]);
            this._feedbackNoteField = first;
            byId(FIELDS[first])?.focus();
        }

        /** A short, polite line under the form: progress and what happened. */
        _feedbackNote(text) {
            const note = byId('feedback-progress');
            if (note) note.textContent = text || '';
            this._feedbackNoteField = '';
        }

        _hideFeedbackAlert() {
            const alert = byId('feedback-alert');
            if (alert) { alert.textContent = ''; alert.hidden = true; }
            const copy = byId('feedback-copy');
            if (copy) copy.hidden = true;
            const text = byId('feedback-copy-text');
            if (text) { text.value = ''; text.hidden = true; }
            this._feedbackCopyText = '';
        }

        /** A refusal: its reason, and the report as text to copy instead when there is one. */
        _showFeedbackRefusal(message, copyText) {
            const alert = byId('feedback-alert');
            if (alert) { alert.textContent = message || 'The report couldn’t be sent.'; alert.hidden = false; }
            this._feedbackCopyText = copyText || '';
            const copy = byId('feedback-copy');
            if (copy) {
                copy.hidden = !this._feedbackCopyText;
                if (!copy.hidden) copy.focus();
            }
        }

        _showFeedbackForm() {
            const form = byId('feedback-form');
            const done = byId('feedback-done');
            if (form) form.hidden = false;
            if (done) done.hidden = true;
        }

        // ── The report, as it will be sent ─────────────────────────────────

        _scheduleFeedbackPreview() {
            clearTimeout(this._feedbackPreviewTimer);
            this._feedbackPreviewId = null;
            // An answer on its way describes the form as it was: drop it when it comes.
            this._feedbackPreviewRequest = (this._feedbackPreviewRequest || 0) + 1;
            const section = byId('feedback-preview');
            const wanted = !!byId('feedback-diagnostics')?.checked;
            if (section) section.hidden = !wanted;
            if (!wanted) return;
            const meta = byId('feedback-preview-meta');
            if (meta) meta.textContent = 'Updating the report…';
            // The report shown is now an older one: dimmed until the new one arrives, and never sent.
            byId('feedback-preview-body')?.classList?.add('is-stale');
            this._feedbackPreviewTimer = setTimeout(() => this._requestFeedbackPreview(), PREVIEW_DELAY_MS);
        }

        _requestFeedbackPreview() {
            clearTimeout(this._feedbackPreviewTimer);
            this._feedbackPreviewId = null;
            this._feedbackPreviewRequest = (this._feedbackPreviewRequest || 0) + 1;
            const {form, ok} = check(this._feedbackFields());
            const section = byId('feedback-preview');
            if (section) section.hidden = !form.include_diagnostics;
            if (!form.include_diagnostics) return;
            const meta = byId('feedback-preview-meta');
            const body = byId('feedback-preview-body');
            const notices = byId('feedback-preview-notices');
            if (notices) notices.replaceChildren();
            if (!ok) {
                if (meta) meta.textContent = 'Fill in the form to see the report.';
                if (body) { body.textContent = ''; body.hidden = true; }
                return;
            }
            if (meta) meta.textContent = 'Preparing the report…';
            this.send({command: 'feedback_preview', request: this._feedbackPreviewRequest, form});
        }

        handleFeedbackPreview(event) {
            if (!this._feedbackOpen() || event?.request !== this._feedbackPreviewRequest) return;  // an older form's report
            if (!byId('feedback-diagnostics')?.checked) return;
            const meta = byId('feedback-preview-meta');
            const body = byId('feedback-preview-body');
            const notices = byId('feedback-preview-notices');
            if (notices) notices.replaceChildren();
            if (event.error) {
                this._feedbackPreviewId = null;
                // No report was made: no empty box to tab to, and the reason is in the alert or on the field.
                if (meta) meta.textContent = event.error.code === 'invalid' ? 'Fill in the form to see the report.'
                    : 'Nothing to show: this report can’t be sent now.';
                if (body) { body.textContent = ''; body.hidden = true; }
                if (event.error.code === 'invalid') this._showFeedbackFieldErrors({[event.error.field || 'message']: event.error.message});
                else this._showFeedbackRefusal(event.error.message, event.copy_text);
                return;
            }
            const data = event.data || {};
            this._feedbackPreviewId = data.preview_id || null;
            if (meta) meta.textContent = previewMeta(data);
            if (body) {
                body.textContent = previewText(data.body);
                body.classList?.remove('is-stale');
                body.hidden = false;
            }
            for (const text of Array.isArray(data.notices) ? data.notices : []) {
                const item = document.createElement('li');
                item.textContent = text;
                notices?.appendChild(item);
            }
            if (this._feedbackAwaitingReview) {
                this._feedbackAwaitingReview = false;
                this._feedbackNote('This is the report Send sends. Review it, then choose Send.');
            }
        }

        // ── Sending ────────────────────────────────────────────────────────

        _sendFeedback() {
            if (this._feedbackSending) return;
            const {form, errors, ok} = check(this._feedbackFields());
            this._showFeedbackFieldErrors(errors);
            if (!ok) return;
            if (form.include_diagnostics && !this._feedbackPreviewId) {
                // What goes is only ever a report the person has seen.
                this._feedbackAwaitingReview = true;
                this._feedbackNote('Preparing the report for you to review before it’s sent…');
                this._requestFeedbackPreview();
                return;
            }
            this._feedbackSending = true;
            const button = byId('feedback-send');
            if (button) { button.disabled = true; button.textContent = 'Sending…'; }
            this._hideFeedbackAlert();
            this._feedbackNote('Sending…');
            this.send({command: 'feedback_send', form, preview_id: form.include_diagnostics ? this._feedbackPreviewId : ''});
        }

        handleFeedbackResult(event) {
            this._feedbackSending = false;
            const button = byId('feedback-send');
            if (button) { button.disabled = false; button.textContent = 'Send'; }
            this._feedbackNote('');
            if (!event) return;
            if (event.ok) {
                this._resetFeedbackForm();
                const done = byId('feedback-done');
                const text = byId('feedback-done-text');
                const form = byId('feedback-form');
                if (form) form.hidden = true;
                // Shown before its text is set, so the status line is announced.
                if (done) done.hidden = false;
                if (text) text.textContent = event.message || 'Sent.';
                if (this._feedbackOpen()) byId('feedback-done-close')?.focus();
                else this.showToastMessage?.(event.message || 'Sent.');
                return;
            }
            if (!this._feedbackOpen()) {
                // Closed while it was on its way: the refusal still reaches the person.
                this.showToastMessage?.(event.message || 'The report couldn’t be sent.');
                return;
            }
            if (event.code === 'invalid') {
                this._showFeedbackFieldErrors({[event.field || 'message']: event.message});
            } else if (event.code === 'preview') {
                this._feedbackAwaitingReview = true;
                this._feedbackNote(event.message);
                this._requestFeedbackPreview();
            } else {
                this._showFeedbackRefusal(event.message, event.copy_text);
            }
        }

        _resetFeedbackForm() {
            const message = byId('feedback-message');
            if (message) message.value = '';
            const bug = byId('feedback-kind-bug');
            if (bug) bug.checked = true;
            const diagnostics = byId('feedback-diagnostics');
            if (diagnostics) diagnostics.checked = false;
            const section = byId('feedback-preview');
            if (section) section.hidden = true;
            // The sent report isn't shown again as the next one's.
            for (const id of ['feedback-preview-body', 'feedback-preview-meta']) {
                const node = byId(id);
                if (node) node.textContent = '';
            }
            const shown = byId('feedback-preview-body');
            if (shown) shown.hidden = true;  // until the next report arrives
            byId('feedback-preview-notices')?.replaceChildren();
            this._feedbackPreviewId = null;
            this._feedbackPreviewRequest = (this._feedbackPreviewRequest || 0) + 1;
            this._feedbackSignature = JSON.stringify(this._feedbackFields());
            this._feedbackCountChanged();
            this._hideFeedbackAlert();
            this._showFeedbackFieldErrors({});
        }

        async _copyFeedback() {
            const text = this._feedbackCopyText;
            if (!text) return;
            const area = byId('feedback-copy-text');
            try {
                await navigator.clipboard.writeText(text);
                if (area) area.hidden = true;
                this._feedbackNote('Copied. Paste it wherever you report problems.');
            } catch (_err) {
                // No clipboard here (or it was refused): the text, selected, for Ctrl+C.
                if (area) { area.value = text; area.hidden = false; area.focus(); area.select(); }
                this._feedbackNote('Select the text below and copy it.');
            }
        }

        handleFeedbackStatus(event) {
            const data = event?.data || {};
            this._feedbackStatus = data;
            const where = byId('feedback-destination');
            if (where) where.textContent = destination(data);
            const always = byId('feedback-always');
            if (always) always.textContent = alwaysSent(data.app);
            const count = Number(data.waiting || 0);
            const queue = byId('feedback-queue');
            if (queue) queue.hidden = count <= 0;
            const text = byId('feedback-queue-text');
            if (text) text.textContent = queueText(count);
            const flush = byId('feedback-flush');
            if (flush) {
                flush.disabled = !data.destination || !!data.offline;
                flush.removeAttribute('aria-busy');
            }
            this._feedbackFlushing = false;
            if (data.flushed) this._feedbackNote(flushText(data.flushed));
            if (typeof data.discarded === 'number') {
                let note = data.discarded ? `Deleted ${plural(data.discarded, 'waiting report')}.` : 'Nothing was deleted.';
                if (count) note += ` ${plural(count, 'report')} ${count === 1 ? 'was' : 'were'} already being sent.`;
                this._feedbackNote(note);
            }
            this._keepFeedbackFocus();
        }

        /** Focus stays in the open dialog: when the control that had it is hidden or disabled, the message box takes it. */
        _keepFeedbackFocus() {
            if (!this._feedbackOpen()) return;
            const dialog = byId('feedback-dialog');
            const active = document.activeElement;
            if (active && active !== document.body && dialog.contains(active) && !active.disabled
                && !active.closest('[hidden]')) return;
            const form = byId('feedback-form');
            (form && !form.hidden ? byId('feedback-message') : byId('feedback-done-close'))?.focus();
        }
    }

    window.LumiFeedback = Object.freeze({
        KINDS, MAX_MESSAGE, MAX_REPLY_TO, check, countLabel, previewText, alwaysSent, destination, previewMeta,
        queueText, flushText,
    });
    window.LumiFeedbackView = LumiFeedbackView;
})();
