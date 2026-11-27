from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "frontend" / "src" / "App.tsx"


def test_completed_dialogue_is_submittable_and_closed_dialogue_is_not() -> None:
    source = APP.read_text(encoding="utf-8")

    helper = source[source.index("function canSubmitConversation") : source.index(
        "function userTurnKind"
    )]
    assert 'status !== "completed"' not in helper
    assert 'status !== "processing"' in helper
    assert 'status !== "closed"' in helper
    assert 'status !== "expired"' in helper
    assert "canSubmitConversation(busy, conversationStatus)" in source


def test_existing_dialogue_keeps_agent_mode_and_distinguishes_new_questions() -> None:
    source = APP.read_text(encoding="utf-8")

    assert (
        'return status === "awaiting_clarification" ? "clarification_answer" : "new_question"'
        in source
    )
    assert 'if (history.mode === "agent") setRequestedMode("agent")' in source
    assert '<select disabled={Boolean(conversationId)} value={requestedMode}' in source
    assert 'setRequestedMode(capabilities.default_mode)' in source


def test_reload_uses_owned_history_and_preserves_trace_bound_rendering() -> None:
    source = APP.read_text(encoding="utf-8")

    assert 'sessionStorage.getItem("clip-conversation-id")' in source
    assert 'sessionStorage.getItem("clip-conversation-token")' in source
    assert "[CONVERSATION_TOKEN_HEADER]: conversationToken" in source
    assert "setMessages(history.turns.map" in source
    assert "message.trace_id && <FeedbackForm" in source
    assert "message.sources.map" in source
