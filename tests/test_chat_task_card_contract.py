from pathlib import Path


APP_JS = Path(__file__).parents[1] / "resonant_client" / "gui" / "static" / "app.js"
STYLES_CSS = APP_JS.with_name("styles.css")


def test_send_message_resets_task_state_before_creating_user_card():
    source = APP_JS.read_text(encoding="utf-8")
    start = source.index("    _prepareTurnUI(text, images = []) {")
    end = source.index("\n    _clearComposerAfterSend()", start)
    body = source[start:end]

    reset_index = body.index("this._resetTaskCardState();")
    add_index = body.index("this.addUserMessage(text, images);")

    assert reset_index < add_index
    assert body.count("this._resetTaskCardState();") == 1
    assert body.count("this.addUserMessage(text, images);") == 1

    send_start = source.index("    sendMessage(options = {}) {")
    send_end = source.index("\n    /**", send_start)
    send_body = source[send_start:send_end]
    assert "this._prepareTurnUI(text, this.attachedImages);" in send_body


def test_chat_supports_structured_outcomes_recovery_and_model_fallback():
    source = APP_JS.read_text(encoding="utf-8")

    for outcome in (
        "answered",
        "changed_verified",
        "changed_unverified",
        "no_changes_needed",
        "incomplete",
        "failed",
    ):
        assert outcome in source
    assert 'data-recovery="retry"' in source
    assert 'data-recovery="alternate"' in source
    assert "this.permissionMode === 'bypass'" in source
    assert "_selectAlternateModelValue()" in source


def test_running_task_has_persistent_progress_todos_and_subtask_visibility():
    source = APP_JS.read_text(encoding="utf-8")
    styles = STYLES_CSS.read_text(encoding="utf-8")

    assert "live-run-surface" in source
    assert "_startLiveRun()" in source
    assert "_setLiveRunTodos(items, done, total)" in source
    assert "_updateLiveSubtask(id, patch)" in source
    assert "Finish the response" in source
    assert "Reason through the next action" in source
    assert "finalStep.text = fallbackText;" in source
    assert "item.text = text || item.text;" in source
    assert "this._advanceLiveMilestone('reason', 'Reason through the next agent step');" in source
    assert "this._advanceLiveMilestone('delegate', 'Coordinate sub-tasks');" in source
    assert ".live-run-orbit" in styles
    assert ".live-run-subtasks" in styles
    assert "this.liveRunSurface = document.getElementById('live-run-surface');" in source
    assert "detailsOpen: false" in source
    assert "class=\"live-run-head live-run-toggle\"" in source
    assert "class=\"live-run-body\" hidden" in source
    assert "setDetailsOpen(!run.detailsOpen);" in source
    assert "toggle.setAttribute('aria-expanded', String(open));" in source
    assert ".live-run-body[hidden]" in styles
    assert '.live-run-head[aria-expanded="true"] .live-run-chevron' in styles
    assert "if (run.renderKey === renderKey) return;" in source
    assert "elapsed clocks update" in source
    assert ".input-bar > .live-run-surface" in styles
    assert "scrollbar-gutter: stable" in styles
    live_dock_rule = styles[styles.index(".input-bar > .live-run-surface {"):]
    live_dock_rule = live_dock_rule[:live_dock_rule.index("}")]
    assert "order: -1" in live_dock_rule
    input_bar_override = styles.index(".input-bar {\n    position: absolute;")
    input_bar_body = styles[input_bar_override:styles.index("}", input_bar_override)]
    assert "flex-direction: column" in input_bar_body
    assert "align-items: stretch" in input_bar_body

    card_start = source.index("    _beginTaskCard(")
    card_end = source.index("\n    _ensureTaskCard", card_start)
    card_body = source[card_start:card_end]
    assert "card.appendChild(live);" not in card_body
    assert "liveEl: this.liveRunSurface" in card_body


def test_streaming_text_does_not_use_a_lonely_blinking_cursor():
    styles = STYLES_CSS.read_text(encoding="utf-8")
    cursor_start = styles.index(".streaming-cursor::after")
    cursor_end = styles.index("}", cursor_start)
    cursor_rule = styles[cursor_start:cursor_end]

    assert "content: '';" in cursor_rule


def test_running_composer_supports_steering_and_visible_queue_state():
    source = APP_JS.read_text(encoding="utf-8")
    styles = STYLES_CSS.read_text(encoding="utf-8")

    assert "this._queueFollowUpMessage(text);" in source
    assert "this._promoteQueuedMessage(messageId);" in source
    assert "this._removeQueuedMessage(messageId);" in source
    assert "command: 'steer_queued'" in source
    assert "command: 'remove_queued'" in source
    assert "case 'steer.applied':" in source
    assert "Waiting for next step" in source
    assert "In current context" in source
    assert "Applied to current run" in source
    assert ".live-steer-note" in styles
    assert "Interrupting safely" not in source
    assert "this._steerInterrupted" not in source
    assert "case 'message.queued':" in source
    assert "case 'message.started':" in source
    assert "this.userInput.disabled = false;" in source
    assert "Write a follow-up for the running agent" in source
    assert "Queue follow-up (Enter)" in source
    assert "steer-queue-promote" in source
    assert ".composer-queue" in styles
    assert ".steer-queue-item" in styles
    assert ".steer-queue-promote" in styles
    assert ".steer-queue-remove" in styles
    assert ".send-btn.is-steering" not in styles


def test_await_user_marks_and_cleans_recommended_option():
    source = APP_JS.read_text(encoding="utf-8")
    styles = STYLES_CSS.read_text(encoding="utf-8")

    assert "await-user-recommended" in source
    assert "Recommended</span>" in source
    assert "reply(option.value);" in source
    assert ".await-user-chip.is-recommended" in styles
    assert ".await-user-recommended" in styles
    assert "await-user-confirmation" in source
    assert "Resuming agent&hellip;" in source
    assert "if (answered) return;" in source


def test_await_user_renders_concise_question_and_aligned_choice_rows():
    source = APP_JS.read_text(encoding="utf-8")
    styles = STYLES_CSS.read_text(encoding="utf-8")

    assert "_conciseAwaitUserQuestion(question)" in source
    assert "const conciseQuestion = this._conciseAwaitUserQuestion(question);" in source
    assert "await-user-option-key" in source
    assert "await-user-option-label" in source
    assert "grid-template-columns: minmax(0, 1fr);" in styles
    assert "grid-template-columns: 24px minmax(0, 1fr) auto;" in styles


def test_live_run_shell_is_stable_across_updates():
    source = APP_JS.read_text(encoding="utf-8")

    assert "if (!run.domReady || !run.el.querySelector('.live-run-head'))" in source
    assert "run.el.querySelector('.live-run-copy small').textContent" in source
    assert "run.el.querySelector('.live-run-todos').innerHTML" in source


def test_stop_button_uses_acknowledged_cancel_lifecycle():
    source = APP_JS.read_text(encoding="utf-8")
    styles = STYLES_CSS.read_text(encoding="utf-8")

    assert "this._requestCancel();" in source
    assert "command: 'cancel', cancel_id: cancelId" in source
    assert "case 'cancel.requested':" in source
    assert "case 'cancel.completed':" in source
    assert "handleCancelCompleted(event)" in source
    assert "this._finishCancelledTask();" in source
    assert "_removeEmptyCancelArtifacts()" in source
    assert "if (!activity && !result && !footer) card.remove();" in source
    assert ".stop-btn.is-stopping" in styles
