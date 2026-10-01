/**
 * Lumi's terms in the app (lumi/terms.py): the End User License Agreement and,
 * on a pre-release build, the Alpha and Beta Test Terms.
 *
 * At first launch, and whenever the terms in force change version, a dialog
 * shows them. Until the person accepts with the dialog's own Accept button (a
 * click, or Enter or Space on it, which the browser reports as the person's:
 * never a script's click, a status push or a timer, and focus never moves onto
 * the button by itself), the message box stays locked
 * (settings_view.js _applyComposerLock) and the server refuses every turn path
 * (oversight.gate). Decline, Escape or the dialog's close button leave a notice
 * above the message box whose Review terms opens the dialog again. Accepting in
 * one window unlocks every open window of the app (the server tells each one).
 * A message sent while the terms wait is refused before any turn starts, and
 * comes back to the message box (app.js _endRefusedTurn). When the machine
 * policy accepted Lumi's terms for the organization's people, there is nothing
 * to accept, and About Lumi says who accepted. While an administrator's machine
 * policy can't be used (status.policy_error: lumi/terms.py
 * machine_policy_error), the notice shows its error instead, with nothing to
 * accept, and the dialog doesn't open by itself: that policy may accept for the
 * person once it can be read, and the server records no acceptance meanwhile.
 * The message box stays open, as for the policy's other refusals: a message is
 * refused with the policy's error and comes back to the box.
 *
 * About Lumi opens the same dialog to read any of the texts this build ships,
 * offline: the agreement, the test terms on a pre-release build, the privacy
 * notice and the third-party notices.
 */
class LumiTermsView {
    _initTermsView() {
        const dialog = document.getElementById('terms-dialog');
        if (!dialog || this._termsViewReady) return;
        this._termsViewReady = true;
        this._legalDocuments = this._legalDocuments || {};
        this._legalRequested = this._legalRequested || {};
        document.getElementById('terms-notice-review')?.addEventListener('click', event => {
            this.openTermsDialog({returnFocus: event.currentTarget});
        });
        document.getElementById('terms-dialog-accept')?.addEventListener('click', event => {
            // Only the person's own click, or Enter or Space on the button: never one a script made.
            if (event?.isTrusted) this._acceptTerms();
        });
        for (const id of ['terms-dialog-decline', 'terms-dialog-close', 'terms-dialog-done']) {
            document.getElementById(id)?.addEventListener('click', () => this.closeTermsDialog());
        }
        document.getElementById('terms-dialog-privacy')?.addEventListener('click', () => this._showTermsDocument('privacy'));
        document.getElementById('terms-dialog-back')?.addEventListener('click', () => this._showTermsDocument(''));
        dialog.addEventListener('keydown', event => this._termsDialogKey(event));
    }

    /** The terms in force and whether they're accepted here: the lock, the notice, and the dialog at first launch. */
    _applyTerms(status) {
        if (!status) return;
        this._initTermsView();
        this.termsStatus = status;
        const pending = Boolean(status.pending);
        const policyError = String(status.policy_error || '');
        this._termsPolicyError = policyError;
        const notice = document.getElementById('terms-notice');
        const text = document.getElementById('terms-notice-text');
        if (text) text.textContent = policyError || status.error || this._termsNoticeText(status);
        const title = document.getElementById('terms-notice-title');
        if (title) title.textContent = policyError ? 'Your organization’s policy' : 'Lumi’s terms';
        // Nothing to review or accept until the organization's policy can be read.
        const review = document.getElementById('terms-notice-review');
        if (review) review.hidden = Boolean(policyError);
        if (notice) notice.hidden = !pending && !policyError;
        const accepting = this._termsDialog?.mode === 'accept';
        if (!pending && accepting) this._termsFocusAfterAccept = true;
        // Once accepted, terms that wait again (an acceptance that no longer counts) ask again,
        // even if this page declined the same versions before.
        if (!pending) this._termsDeclinedFor = null;
        this._termsLocked = pending && !policyError;
        this._applyComposerLock?.();
        if (!pending && !policyError && accepting) this.closeTermsDialog({accepted: true});
        // First launch, or terms that changed since this page last declined them: ask,
        // unless the organization's policy can't be read (the notice says so instead).
        const asked = String(status.acceptance_value || status.error || '');
        if (pending && !policyError && !this._termsDialog && this._termsDeclinedFor !== asked) this.openTermsDialog();
        else if (this._termsDialog) this._renderTermsDialog();
        if (this.currentView === 'settings') this.renderSettingsView?.();
        // Accepted: the first launch's offer to sign in to Lumi Cloud comes next (settings_view.js).
        if (!this._termsDialog && this.cloudStatus) this._applyCloudStatus?.(this.cloudStatus);
    }

    _termsNoticeText(status) {
        const changed = (status.required || []).some(doc => (doc.changed || doc.previous_version) && !doc.accepted);
        return changed
            ? 'Lumi’s terms have changed. Lumi won’t send anything to a model until you accept the new version.'
            : 'Lumi won’t send anything to a model until you accept its terms.';
    }

    /** Who accepted the terms in force, for About Lumi (HTML). */
    _termsAcceptanceText(status) {
        const esc = value => this.escapeHtml(String(value ?? ''));
        if (status?.policy_error) return esc(status.policy_error);
        if (!status || status.error) return esc(status?.error || '');
        if (status.organization) {
            return `Accepted for everyone who uses Lumi on this computer by ${esc(status.organization)}, through its machine policy.`;
        }
        if (status.pending) return 'Not accepted yet: Lumi won’t send anything to a model until you accept them.';
        const accepted = (status.required || []).map(doc => {
            const when = doc.accepted_at ? ` on ${esc(this._termsDate(doc.accepted_at))}`
                : doc.accepted_via === 'environment' ? ` (${esc(status.environment || 'LUMI_ACCEPT_TERMS')})` : '';
            return `the ${esc(doc.title)} ${esc(doc.version)}${when}`;
        });
        return accepted.length ? `You accepted ${accepted.join(' and ')}.` : '';
    }

    _termsDate(iso) {
        const value = new Date(iso);
        return Number.isNaN(value.getTime()) ? String(iso || '')
            : value.toLocaleDateString(undefined, {year: 'numeric', month: 'long', day: 'numeric'});
    }

    /**
     * Open the dialog: ``mode`` "accept" (the terms in force; the default) or "read"
     * (one text: ``doc`` is eula, alpha_terms, privacy or notices).
     */
    openTermsDialog(options = {}) {
        this._initTermsView();
        const dialog = document.getElementById('terms-dialog');
        if (!dialog) return;
        const mode = options.mode === 'read' ? 'read' : 'accept';
        const returnFocus = options.returnFocus || this._termsDialog?.returnFocus || document.activeElement;
        this._termsDialog = {mode, doc: mode === 'read' ? String(options.doc || 'eula') : '', returnFocus};
        dialog.style.display = 'flex';
        this._renderTermsDialog();
        // Every opening starts at the top of the text and of the dialog, not where the
        // person left it when they closed it. Reading starts in the text itself, where the
        // arrow keys scroll it. Focus never lands on Accept by itself, so a key meant for
        // something else can't accept.
        this._termsDialogToTop();
    }

    /** The dialog and its text scrolled to the top, and the text focused without scrolling. */
    _termsDialogToTop() {
        const body = document.getElementById('terms-dialog-body');
        const card = document.getElementById('terms-dialog')?.querySelector?.('.terms-dialog');
        if (card) card.scrollTop = 0;
        if (!body) return;
        body.scrollTop = 0;
        body.focus({preventScroll: true});
    }

    /** Close the dialog. Closed without accepting, the notice above the message box opens it again. */
    closeTermsDialog(options = {}) {
        const dialog = document.getElementById('terms-dialog');
        const state = this._termsDialog;
        if (dialog) dialog.style.display = 'none';
        this._termsDialog = null;
        if (!state) return;
        if (state.mode === 'accept' && !options.accepted) {
            this._termsDeclinedFor = String(this.termsStatus?.acceptance_value || this.termsStatus?.error || '');
            const review = document.getElementById('terms-notice-review');
            if (review && !review.hidden && this.termsStatus?.pending) {
                review.focus();
                return;
            }
        }
        if (options.accepted) {
            // The message box takes the focus (_applyComposerLock), or the
            // organization's notice when it still waits.
            if (this._oversightLocked) this._focusComposerLock?.();
            else if (!this.userInput?.disabled) this.userInput?.focus();
            return;
        }
        if (state.returnFocus && document.contains(state.returnFocus)) state.returnFocus.focus?.();
    }

    /** In accept mode, show another text (the privacy notice) or, with '', the terms again. */
    _showTermsDocument(doc) {
        if (!this._termsDialog) return;
        this._termsDialog.doc = doc || '';
        this._renderTermsDialog();
        this._termsDialogToTop();
    }

    _requestLegalDocument(id) {
        if (this._legalDocuments[id] || this._legalRequested[id]) return;
        this._legalRequested[id] = true;
        this.send?.({command: 'legal_document', id});
    }

    /** A text the server sent (legal_document). */
    _receiveLegalDocument(data) {
        if (!data || !data.id) return;
        this._initTermsView();
        this._legalDocuments[data.id] = data;
        delete this._legalRequested[data.id];
        if (this._termsDialog) this._renderTermsDialog();
    }

    _legalHtml(doc) {
        const esc = value => this.escapeHtml(String(value ?? ''));
        if (doc.error) return `<p class="editor-help">${esc(doc.error)}</p>`;
        const text = String(doc.text || '');
        // The texts come with Lumi, but they pass the page's Markdown sanitizer
        // like everything else it renders; without it they show as plain text.
        if (doc.format === 'markdown' && typeof marked !== 'undefined' && typeof DOMPurify !== 'undefined') {
            // The texts are wrapped at 80 columns: a line break inside a paragraph is a space
            // here, not the <br> chat replies get (app.js sets breaks: true for those).
            const html = marked.parse(text, {gfm: true, breaks: false});
            return `<article class="terms-document">${this.sanitizeMarkdownHtml(html)}</article>`;
        }
        return `<pre class="terms-document terms-document-plain">${esc(text)}</pre>`;
    }

    _renderTermsDialog() {
        const state = this._termsDialog;
        if (!state) return;
        const status = this.termsStatus || {};
        const esc = value => this.escapeHtml(String(value ?? ''));
        const accepting = state.mode === 'accept';
        const required = Array.isArray(status.required) ? status.required : [];
        const ids = state.doc ? [state.doc] : required.map(doc => doc.id);
        ids.forEach(id => this._requestLegalDocument(id));
        const known = {...(status.documents || {})};
        const about = id => this._legalDocuments[id] || known[id] || {};
        const title = document.getElementById('terms-dialog-title');
        const intro = document.getElementById('terms-dialog-intro');
        const body = document.getElementById('terms-dialog-body');
        const error = document.getElementById('terms-dialog-error');
        const consent = document.getElementById('terms-dialog-consent');
        // A version applies from the day it's accepted; its date is when its text was published (lumi/terms.py).
        const named = doc => `the ${esc(doc.title)} (version ${esc(doc.version)}, published ${esc(doc.published_text || doc.published)})`;
        if (title) {
            title.textContent = accepting ? 'Lumi’s terms'
                : state.doc === 'notices' ? 'Third-party notices' : (about(state.doc).title || 'Lumi’s terms');
        }
        // An administrator's policy that can't be used comes first: nothing can be accepted until it's fixed.
        const blocked = String(status.policy_error || status.error || '');
        if (intro) {
            let words;
            if (accepting && blocked) words = '';
            else if (accepting) {
                const changed = required.some(doc => (doc.changed || doc.previous_version) && !doc.accepted);
                words = (changed ? 'Lumi’s terms have changed. Read the new version and accept it to keep using Lumi.'
                    : 'Please read and accept Lumi’s terms to use Lumi. Lumi won’t send anything to a model until you do.')
                    + (status.prerelease ? ' This is a pre-release build, so the Alpha and Beta Test Terms apply along with the End User License Agreement.' : '');
                if (status.organization) words = `${status.organization} accepted Lumi’s terms for everyone on this computer.`;
            } else if (state.doc === 'notices') {
                words = 'The licenses of the third-party components in this copy of Lumi.';
            } else {
                const doc = about(state.doc);
                const published = doc.published_text || doc.published;
                // The privacy notice is read, not accepted; the agreement and test terms apply once accepted.
                words = !doc.version ? ''
                    : state.doc === 'privacy' ? `Version ${doc.version}, published ${published}.`
                    : `Version ${doc.version}, published ${published}. It applies to you from the day you accept it.`;
            }
            intro.textContent = words;
            intro.hidden = !words;
        }
        if (body) {
            const waiting = ids.filter(id => !this._legalDocuments[id]);
            const html = waiting.length ? '<p class="editor-help">Loading…</p>'
                : ids.map(id => this._legalHtml(this._legalDocuments[id])).join('<hr class="terms-document-break">');
            if (body.dataset.shown !== `${state.mode}:${ids.join(',')}:${waiting.length}`) {
                body.innerHTML = html;
                body.scrollTop = 0;
                body.dataset.shown = `${state.mode}:${ids.join(',')}:${waiting.length}`;
            }
            body.setAttribute('aria-label', accepting && !state.doc ? 'The terms' : (title?.textContent || 'Document'));
        }
        if (error) {
            error.textContent = accepting ? blocked : '';
            error.hidden = !error.textContent;
        }
        const pending = Boolean(status.pending) && !blocked && required.length > 0;
        if (consent) {
            consent.innerHTML = accepting && pending
                ? `By choosing Accept, you agree to ${required.map(named).join(' and ')}.` : '';
            consent.hidden = !(accepting && pending);
        }
        const show = (id, visible) => {
            const control = document.getElementById(id);
            if (control) control.hidden = !visible;
        };
        show('terms-dialog-accept', accepting && pending);
        show('terms-dialog-decline', accepting && pending);
        show('terms-dialog-done', !(accepting && pending));
        show('terms-dialog-privacy', accepting && state.doc !== 'privacy');
        show('terms-dialog-back', accepting && Boolean(state.doc));
    }

    _acceptTerms() {
        const status = this.termsStatus;
        const required = Array.isArray(status?.required) ? status.required : [];
        if (!status?.pending || status.error || status.policy_error || !required.length) return;
        // The versions this dialog showed: terms that changed meanwhile accept nothing (lumi/terms.py).
        const documents = {};
        for (const doc of required) documents[doc.id] = doc.version;
        this.send({command: 'terms_accept', documents});
    }

    /** Escape closes (never accepts); Tab stays inside the dialog. */
    _termsDialogKey(event) {
        if (event.key === 'Escape') {
            event.preventDefault();
            event.stopPropagation();
            this.closeTermsDialog();
            return;
        }
        if (event.key !== 'Tab') return;
        const dialog = document.getElementById('terms-dialog');
        const focusable = [...dialog.querySelectorAll('button, a[href], [tabindex="0"]')]
            .filter(element => !element.hidden && !element.disabled && element.getClientRects().length);
        if (!focusable.length) return;
        const first = focusable[0];
        const last = focusable[focusable.length - 1];
        if (event.shiftKey && (document.activeElement === first || !dialog.contains(document.activeElement))) {
            event.preventDefault();
            last.focus();
        } else if (!event.shiftKey && (document.activeElement === last || !dialog.contains(document.activeElement))) {
            event.preventDefault();
            first.focus();
        }
    }
}

window.LumiTermsView = LumiTermsView;
