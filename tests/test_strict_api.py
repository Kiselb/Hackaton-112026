from uuid import uuid4

from clip_agent import main
from clip_agent.config import Settings


class FakeDb:
    def __init__(self):
        self.trace = None

    def add(self, value):
        self.trace = value
        if value.id is None:
            value.id = uuid4()

    def commit(self):
        pass


def evidence() -> dict:
    return {
        "chunk_id": "chunk-1",
        "document_id": "doc-1",
        "title": "Регламент",
        "filename": "rule.txt",
        "parent_text": "Лимит составляет 10 рублей.",
        "child_text": "Лимит составляет 10 рублей.",
        "child_start": 0,
        "child_end": 29,
    }


def retrieval_trace() -> dict:
    return {
        "enabled": False,
        "status": "disabled",
        "latency_ms": 0.0,
        "fallback_reason": None,
    }


class Response:
    def __init__(self, content: str):
        self.content = content

    def raise_for_status(self):
        pass

    def json(self):
        return {
            "message": {"content": self.content},
            "prompt_eval_count": 5,
            "eval_count": 2,
            "done_reason": "stop",
        }


def test_agent_is_default_only_when_configuration_does_not_override_it():
    assert Settings.model_fields["agent_enabled"].default is True
    assert Settings(_env_file=None, agent_enabled=False).agent_enabled is False


def test_invalid_input_is_structured_and_skips_retrieval(monkeypatch):
    monkeypatch.setattr(main.settings, "agent_enabled", True)
    monkeypatch.setattr(main, "retrieve", lambda question: (_ for _ in ()).throw(AssertionError()))
    db = FakeDb()

    result = main.ask(main.AskRequest(question=""), db)

    assert result["outcome"] == "invalid_input"
    assert result["reason_code"] == "empty_or_punctuation"
    assert db.trace.details["input_guard"] == "rejected"


def test_baseline_answer_requires_semantic_grounding_validation(monkeypatch):
    monkeypatch.setattr(main.settings, "agent_enabled", False)
    monkeypatch.setattr(main, "retrieve", lambda question: ([evidence()], 1, retrieval_trace()))
    responses = iter(
        [
            Response("Лимит составляет 10 рублей [1]."),
            Response('{"valid":true,"unsupported_claims":[],"scope_errors":[]}'),
        ]
    )
    monkeypatch.setattr(main.httpx, "post", lambda *args, **kwargs: next(responses))
    db = FakeDb()

    result = main.ask(main.AskRequest(question="Какой лимит установлен?"), db)

    assert result["outcome"] == "answered"
    assert result["answer"] == "Лимит составляет 10 рублей [1]."
    assert result["metrics"]["citations_valid"] is True
    assert db.trace.details["outcome"] == "answered"


def test_baseline_unsupported_conclusion_is_replaced_with_safe_outcome(monkeypatch):
    monkeypatch.setattr(main.settings, "agent_enabled", False)
    monkeypatch.setattr(main, "retrieve", lambda question: ([evidence()], 1, retrieval_trace()))
    responses = iter(
        [
            Response("Правило применяется всем подразделениям [1]."),
            Response(
                '{"valid":false,"unsupported_claims":[],"scope_errors":'
                '["scope not supported"]}'
            ),
        ]
    )
    monkeypatch.setattr(main.httpx, "post", lambda *args, **kwargs: next(responses))
    db = FakeDb()

    result = main.ask(main.AskRequest(question="Кому применяется правило?"), db)

    assert result["outcome"] == "quality_review_required"
    assert "применяется всем" not in result["answer"]
    assert result["reason_code"] == "answer_validation_failed"


def test_baseline_invalid_validator_json_is_a_technical_error(monkeypatch):
    monkeypatch.setattr(main.settings, "agent_enabled", False)
    monkeypatch.setattr(main, "retrieve", lambda question: ([evidence()], 1, retrieval_trace()))
    responses = iter([Response("Лимит составляет 10 рублей [1]."), Response("not-json")])
    monkeypatch.setattr(main.httpx, "post", lambda *args, **kwargs: next(responses))
    db = FakeDb()

    result = main.ask(main.AskRequest(question="Какой лимит установлен?"), db)

    assert result["outcome"] == "technical_error"
    assert result["reason_code"] == "baseline_runtime_error"
    assert db.trace.status == "failed"
