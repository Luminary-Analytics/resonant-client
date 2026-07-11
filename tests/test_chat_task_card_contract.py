from pathlib import Path


APP_JS = Path(__file__).parents[1] / "resonant_client" / "gui" / "static" / "app.js"


def test_send_message_resets_task_state_before_creating_user_card():
    source = APP_JS.read_text(encoding="utf-8")
    start = source.index("    sendMessage(options = {}) {")
    end = source.index("\n    /**", start)
    body = source[start:end]

    reset_index = body.index("this._resetTaskCardState();")
    add_index = body.index("this.addUserMessage(text, this.attachedImages);")

    assert reset_index < add_index
    assert body.count("this._resetTaskCardState();") == 1
    assert body.count("this.addUserMessage(text, this.attachedImages);") == 1


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
