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
    assert ".live-run-orbit" in styles
    assert ".live-run-subtasks" in styles
    assert "this.liveRunSurface = document.getElementById('live-run-surface');" in source
    assert "detailsOpen: true" in source
    assert "run.detailsOpen = event.currentTarget.open;" in source
    assert "if (run.renderKey === renderKey) return;" in source
    assert "elapsed clocks update" in source
    assert ".input-bar > .live-run-surface" in styles
    assert "scrollbar-gutter: stable" in styles

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

    assert "this._queueSteerMessage(text);" in source
    assert "command: 'steer'" in source
    assert "case 'message.queued':" in source
    assert "case 'message.started':" in source
    assert "this.userInput.disabled = false;" in source
    assert "Steer the running agent or queue a follow-up" in source
    assert ".steer-queue-item" in styles
    assert ".send-btn.is-steering" in styles


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
