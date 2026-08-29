from pathlib import Path


APP_JS = Path(__file__).parents[1] / "resonant_client" / "gui" / "static" / "app.js"
STYLES_CSS = APP_JS.with_name("styles.css")


def handles_event(source: str, name: str) -> bool:
    """Whether the frontend dispatches `name`, by either mechanism.

    handleEvent resolves single-delegation events through
    RESONANT_EVENT_DELEGATES and everything else through its switch. Asserting
    on `case 'x':` alone would fail whenever an event moves between the two,
    which says nothing about whether the event is still handled.
    """
    return f"case '{name}':" in source or f"'{name}': '" in source


def frontend_source() -> str:
    """Every class-body script that contributes methods to ResonantApp.

    These assertions are about behaviour existing in the frontend, not about
    which file it sits in. `ResonantApp` is split across mixin files, so
    reading app.js alone would fail the moment a method moves — a refactor
    breaking a test that the refactor did not actually invalidate.

    Globbed rather than listed: naming the mixins here would mean every future
    split silently narrows what these assertions can see, which fails open.
    """
    return "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted(APP_JS.parent.glob("*.js"))
    )


def test_send_message_resets_task_state_before_creating_user_card():
    source = frontend_source()
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
    source = frontend_source()

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


def test_replayed_unfinished_turns_distinguish_retry_from_continue():
    source = frontend_source()

    assert "_interruptedReplayRecovery(events = [])" in source
    assert "const tail = events.slice(lastEnd + 1);" in source
    assert "kind: partial ? 'paused' : 'not_started'" in source
    assert "Response didn\\'t start" in source
    assert "Response paused" in source
    assert "notStarted && recovery.prompt" in source
    assert "Session was interrupted" not in source


def test_saved_false_positive_change_outcome_is_repaired_during_replay():
    source = frontend_source()

    assert "_requestExplicitlyForbidsWorkspaceChanges(text = '')" in source
    assert "Do not configure, install, or change anything." in source
    assert "outcome = 'answered';" in source


def test_running_task_has_persistent_progress_todos_and_subtask_visibility():
    source = frontend_source()
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
    assert "class=\"live-run-head\"" in source
    assert "class=\"live-run-toggle\"" in source
    assert "class=\"live-run-body\" hidden" in source
    assert "Working for <span data-live-elapsed>" in source
    assert "class=\"live-run-divider\"" in source
    assert "const stateLabels = {" in source
    assert "Starting: 'Thinking'" in source
    assert "Composing: 'Writing response'" in source
    assert "setDetailsOpen(!run.detailsOpen);" in source
    assert "toggle.setAttribute('aria-expanded', String(open));" in source
    assert ".live-run-body[hidden]" in styles
    assert '.live-run-toggle[aria-expanded="true"] .live-run-chevron' in styles
    assert "if (run.renderKey === renderKey) return;" in source
    assert "elapsed clocks update" in source
    assert ".task-activity > .live-run-surface" in styles
    assert ".task-activity > .live-run-surface .live-run-divider" in styles
    assert ".task-activity > .live-run-surface .live-run-detail-status" in styles
    assert '.task-activity:not(:has(.live-run-toggle[aria-expanded="true"]))' in styles
    assert "> :not(.live-run-surface)" in styles
    assert "scrollbar-gutter: stable" in styles
    live_dock_rule = styles[styles.index(".task-activity > .live-run-surface {"):]
    live_dock_rule = live_dock_rule[:live_dock_rule.index("}")]
    assert "border: 0" in live_dock_rule
    input_bar_override = styles.index(".input-bar {\n    position: absolute;")
    input_bar_body = styles[input_bar_override:styles.index("}", input_bar_override)]
    assert "flex-direction: column" in input_bar_body
    assert "align-items: stretch" in input_bar_body

    card_start = source.index("    _beginTaskCard(")
    card_end = source.index("\n    _ensureTaskCard", card_start)
    card_body = source[card_start:card_end]
    assert "activity.appendChild(this.liveRunSurface);" in card_body
    assert "liveEl: this.liveRunSurface" in card_body
    assert "activity?.querySelector(':scope > .live-run-surface')?.remove();" in source
    assert "if (this._liveRun === run)" in source


def test_quiet_long_runs_report_freshness_and_reconnect_restores_the_title():
    source = frontend_source()

    assert "lastEventAt: Date.now()" in source
    assert "Still working · waiting for ${worker}" in source
    assert "this._liveRun.lastEventAt = Date.now();" in source
    assert "this._syncSessionTitle();" in source
    assert "Array.isArray(current_display_events)" in source
    assert "this.replayDisplayEvents(current_display_events, { activeRun: run_active === true });" in source
    assert "activeRun: run_active === true" in source
    assert "Reconnected to the active run" in source
    assert "const replayRecovery = activeRun ? null" in source
    assert "queued_messages" in source
    assert "this._renderQueuedMessage(queued.message_id" in source
    assert "this._liveRun.completedTools = completed.length" in source


def test_reconnect_updates_an_existing_live_cards_original_start_time():
    source = frontend_source()

    assert "if (Number(event.started_at) > 0)" in source
    assert "this._liveRun.startedAt = Number(event.started_at) * 1000" in source


def test_parallel_subagent_events_have_independent_render_lanes():
    source = frontend_source()

    assert "this.subagentContainers = new Map();" in source
    assert "this._withRenderEvent(event, () => this[delegate](event));" in source
    assert "this.subagentContainers.set(event.agent_id, children);" in source
    assert "this.subagentContainers.get(event.agent_id || '')" in source
    assert "this.subagentContainers.delete(event.agent_id);" in source
    assert "event._subagent ? 'Delegating' : 'Using tools'" in source


def test_session_boundaries_clear_and_close_stale_preview_content():
    source = frontend_source()

    cleared_start = source.index("            case 'session_cleared':")
    cleared_end = source.index("            case 'session_forked':", cleared_start)
    cleared_body = source[cleared_start:cleared_end]
    assert "this.clearPreviewPanel();" in cleared_body
    assert "this.closePreviewPanel();" in cleared_body

    loaded_start = source.index("            case 'session_loaded':")
    loaded_end = source.index("            case 'harness_state':", loaded_start)
    loaded_body = source[loaded_start:loaded_end]
    assert "this.clearPreviewPanel();" in loaded_body
    assert "this.closePreviewPanel();" in loaded_body


def test_completed_chat_prioritizes_the_answer_over_success_telemetry():
    source = frontend_source()
    styles = STYLES_CSS.read_text(encoding="utf-8")

    assert "['answered', 'no_changes_needed'].includes(outcome)" in source
    assert "task.footerEl.hidden = true;" in source
    assert "`Worked for ${this._formatRunDuration(elapsed)}`" in source
    assert '<span class="task-activity-title">${this.escapeHtml(activityTitle)}</span>' in source
    assert ".task-card::before,\n.task-card::after" in styles
    assert '.task-card[data-user-message="synthetic"] .task-card-header' in styles
    assert "border-radius: 18px 18px 4px 18px" in styles
    assert "max-height: min(48vh, 440px)" in styles
    assert ": 'Message Resonant';" in source


def test_streaming_text_does_not_use_a_lonely_blinking_cursor():
    styles = STYLES_CSS.read_text(encoding="utf-8")
    cursor_start = styles.index(".streaming-cursor::after")
    cursor_end = styles.index("}", cursor_start)
    cursor_rule = styles[cursor_start:cursor_end]

    assert "content: '';" in cursor_rule


def test_screenshots_are_collapsed_inside_the_work_log_by_default():
    source = frontend_source()
    styles = STYLES_CSS.read_text(encoding="utf-8")

    start = source.index("    renderScreenshotImage(")
    end = source.index("\n    showLightbox", start)
    body = source[start:end]

    assert "const target = this.getRenderTarget();" in body
    assert "this.chatMessages" not in body
    assert "const gallery = this._screenshotGallery(target);" in body
    assert "details.className = 'screenshot-gallery';" in source
    assert "details.open" not in source[source.index("    _screenshotGallery("):start]
    assert ".screenshot-gallery-grid" in styles


def test_titlebar_grid_cells_preserve_a_native_drag_region():
    styles = STYLES_CSS.read_text(encoding="utf-8")
    start = styles.index(".titlebar-left,\n.titlebar-right {")
    end = styles.index(".titlebar-logo", start)
    titlebar_rules = styles[start:end]

    assert titlebar_rules.count("-webkit-app-region: drag;") >= 2
    assert ".app-titlebar button" in styles
    control_rule = styles[styles.index(".app-titlebar button"):]
    control_rule = control_rule[:control_rule.index("}")]
    assert "-webkit-app-region: no-drag;" in control_rule


def test_running_composer_supports_steering_and_visible_queue_state():
    source = frontend_source()
    styles = STYLES_CSS.read_text(encoding="utf-8")

    assert "this._queueFollowUpMessage(text);" in source
    assert "this._promoteQueuedMessage(messageId);" in source
    assert "this._removeQueuedMessage(messageId);" in source
    assert "command: 'steer_queued'" in source
    assert "command: 'remove_queued'" in source
    assert handles_event(source, "steer.applied")
    assert "Waiting for next step" in source
    assert "In current context" in source
    assert "Applied to current run" in source
    assert ".live-steer-note" in styles
    assert "Interrupting safely" not in source
    assert "this._steerInterrupted" not in source
    assert handles_event(source, "message.queued")
    assert handles_event(source, "message.started")
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
    source = frontend_source()
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
    source = frontend_source()
    styles = STYLES_CSS.read_text(encoding="utf-8")

    assert "_conciseAwaitUserQuestion(question)" in source
    assert "const conciseQuestion = this._conciseAwaitUserQuestion(question);" in source
    assert "await-user-option-key" in source
    assert "await-user-option-label" in source
    assert "grid-template-columns: minmax(0, 1fr);" in styles
    assert "grid-template-columns: 24px minmax(0, 1fr) auto;" in styles


def test_live_run_shell_is_stable_across_updates():
    source = frontend_source()

    assert "if (!run.domReady || !run.el.querySelector('.live-run-head'))" in source
    assert "run.el.querySelector('[data-live-now]')" in source
    assert "run.el.querySelector('[data-live-latest]')" in source
    assert "run.el.querySelector('.live-run-todos').innerHTML" in source


def test_live_run_quick_summary_preserves_concrete_tool_context():
    source = frontend_source()
    styles = STYLES_CSS.read_text(encoding="utf-8")

    assert "_liveRunToolActivity(name, args = {})" in source
    assert "run.toolActivities.set(callId, activity);" in source
    assert "run.completedTools += 1;" in source
    assert "Reviewing the result of" in source
    assert "Building the Director task graph" in source
    assert "Validating worker evidence" in source
    assert 'data-live-now' in source
    assert 'data-live-latest' in source
    assert "Latest \\u00b7" in source
    assert "tool${run.completedTools === 1 ? '' : 's'} finished" in source
    assert "Working through the next action" not in source
    assert ".live-run-latest" in styles


def test_exo_quiet_generation_status_is_informational_by_default():
    source = frontend_source()

    assert "progressWarningSeconds: 120" in source
    assert "EXO connection active" in source
    assert "last model progress ${idleFor}s ago${hardLimit}" in source
    assert "' · no automatic time limit'" in source
    assert "idle stop in" not in source


def test_check_status_is_immediate_deduplicated_and_non_interrupting():
    source = frontend_source()
    styles = STYLES_CSS.read_text(encoding="utf-8")

    assert "class=\"live-run-status-check\"" in source
    assert "_liveRunHealthText(run = this._liveRun)" in source
    assert "_requestLiveRunStatus()" in source
    assert "run.statusVisible = true;" in source
    assert "['sending', 'queued'].includes(run.statusRequestState)" in source
    assert "command: 'status_update'" in source
    assert handles_event(source, "status.update_queued")
    assert handles_event(source, "status.update_rejected")
    assert "Agent update was not acknowledged; local health is still live" in source
    assert "run.statusRequestId === event.message_id" in source
    assert ".live-run-status-check" in styles
    assert ".live-run-health" in styles


def test_stop_button_uses_acknowledged_cancel_lifecycle():
    source = frontend_source()
    styles = STYLES_CSS.read_text(encoding="utf-8")

    assert "this._requestCancel();" in source
    assert "command: 'cancel', cancel_id: cancelId" in source
    assert handles_event(source, "cancel.requested")
    assert handles_event(source, "cancel.completed")
    assert "handleCancelCompleted(event)" in source
    assert "this._finishCancelledTask();" in source
    assert "_removeEmptyCancelArtifacts()" in source
    assert "if (!activity && !result && !footer) card.remove();" in source
    assert ".stop-btn.is-stopping" in styles


def test_session_list_has_semantic_activity_indicators():
    source = frontend_source()
    styles = STYLES_CSS.read_text(encoding="utf-8")

    assert "this._sessionActivity = new Map();" in source
    assert "_sessionIndicator(session)" in source
    assert "_setSessionActivity(state, sessionId = this.currentSessionId)" in source
    assert "state: 'working', label: 'Resonant is working'" in source
    assert "state: 'needs-input', label: 'Needs your attention'" in source
    assert "return { state: 'idle', label: 'Idle' };" in source
    assert 'class="agent-row-status is-${indicator.state}"' in source
    assert 'role="img" aria-label="${indicator.label}"' in source

    # Selection and pinning are row attributes, not lifecycle states.
    session_row_start = source.index("    _createTreeSessionRow(session) {")
    session_row_end = source.index("\n    showSessionContextMenu", session_row_start)
    session_row = source[session_row_start:session_row_end]
    assert "is-active" not in session_row
    assert "is-pinned" not in session_row

    assert ".agent-row-status.is-idle" in styles
    assert ".agent-row-status.is-working::before" in styles
    assert ".agent-row-status.is-needs-input" in styles
    assert "@keyframes session-status-orbit" in styles


def test_user_blocking_events_update_session_indicator():
    source = frontend_source()

    await_start = source.index("    handleAwaitUser(event) {")
    await_end = source.index("\n    handleUserInputReceived", await_start)
    await_body = source[await_start:await_end]
    assert "this._setSessionActivity('needs-input');" in await_body
    assert "this._setSessionActivity('working');" in await_body

    permission_start = source.index("    handleToolPermission(event) {")
    permission_end = source.index("\n    /** Render an inline diff card", permission_start)
    assert "this._setSessionActivity('needs-input');" in source[permission_start:permission_end]

    activity_start = source.index("    handleAutonomousActivity(event) {")
    activity_end = source.index("\n    /**", activity_start)
    activity_body = source[activity_start:activity_end]
    assert "s.activity.phase === 'parked' ? 'needs-input' : 'working'" in activity_body


def test_settings_navigation_is_idempotent_and_background_events_cannot_close_it():
    source = frontend_source()

    assert "Navigation controls must be idempotent." in source
    assert "this.switchView('settings');" in source
    assert "this.switchView(view);" in source
    assert "view === 'settings' && this.currentView === 'settings'" not in source

    show_start = source.index("    showChatInterface({ force = false } = {}) {")
    show_end = source.index("\n    /** First-run onboarding card", show_start)
    show_body = source[show_start:show_end]
    assert "if (!force && this.currentView !== 'agents') return;" in show_body

    new_start = source.index("    startNewSession() {")
    new_end = source.index("\n    showNewSessionSetup()", new_start)
    new_session_body = source[new_start:new_end]
    assert "if (this.currentView !== 'agents') this.switchView('agents');" in new_session_body
    assert "if (this._newSessionInflight)" in new_session_body
    assert "request_id: this._newSessionRequestId" in new_session_body
    assert "button.disabled = true" in new_session_body


def test_model_selector_preserves_a_temporarily_unavailable_current_model():
    source = frontend_source()
    start = source.index("    _populateSelectWithGroupedModels(")
    end = source.index("\n    populateModelSelector(", start)
    body = source[start:end]

    assert "Current selection" in body
    assert "temporarily unavailable" in body
    assert "unavailable.selected = true" in body


def test_saved_history_is_tail_paged_and_keeps_a_bounded_dom_window():
    source = frontend_source()
    styles = STYLES_CSS.read_text(encoding="utf-8")

    assert handles_event(source, "session_history_page")
    assert "command: 'get_session_history_page'" in source
    assert "const MAX_MOUNTED_HISTORY_EVENTS = 1200;" in source
    assert "Return to latest activity" in source
    assert "this._loadedHistoryEvents = merged.slice(0, MAX_MOUNTED_HISTORY_EVENTS);" in source
    assert "Number.isInteger(event?._ledger_seq)" in source
    assert "this._loadedHistoryEvents.push({ event: 'user_message', text });" in source
    assert ".session-history-page-control" in styles


def test_tool_render_intent_drives_changed_files_and_clickable_deliverables():
    source = frontend_source()

    assert "event.presentation?.kind" in source
    assert "event.presentation?.locations" in source
    assert "command: 'open_workspace_path'" in source
    assert 'data-file-path="${this.escapeHtml(f.path)}"' in source
    assert "for (const rawPath of (evidence.changed_files || []))" in source
