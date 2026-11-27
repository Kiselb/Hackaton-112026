from uuid import uuid4

import pytest

from clip_agent import llm as llm_module
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


def test_multi_query_capability_defaults_are_safe():
    assert Settings.model_fields["multi_query_enabled"].default is True
    assert Settings.model_fields["multi_query_default_enabled"].default is False
    assert Settings.model_fields["multi_query_max_variants"].default == 2


def test_capabilities_publish_only_safe_multi_query_configuration(monkeypatch):
    monkeypatch.setattr(main.settings, "multi_query_enabled", True)
    monkeypatch.setattr(main.settings, "multi_query_default_enabled", False)
    monkeypatch.setattr(main.settings, "multi_query_max_variants", 2)

    result = main.capabilities()

    assert result["multi_query"] == {
        "available": True,
        "default_enabled": False,
        "max_variants": 2,
    }


def test_capabilities_publish_actual_agent_default_and_demo_override(monkeypatch):
    monkeypatch.setattr(main.settings, "agent_enabled", True)
    monkeypatch.setattr(main.settings, "agent_demo_override_enabled", True)
    monkeypatch.setattr(main.settings, "multi_query_default_enabled", False)

    result = main.capabilities()

    assert result["default_mode"] == "agent"
    assert result["agent_demo_override_enabled"] is True
    assert result["multi_query"]["default_enabled"] is False


def test_capabilities_do_not_hide_backend_baseline_configuration(monkeypatch):
    monkeypatch.setattr(main.settings, "agent_enabled", False)

    assert main.capabilities()["default_mode"] == "baseline"


def test_baseline_explicitly_rejects_multi_query(monkeypatch):
    monkeypatch.setattr(main.settings, "agent_demo_override_enabled", True)
    monkeypatch.setattr(main.settings, "multi_query_enabled", True)

    with pytest.raises(main.HTTPException) as error:
        main.ask(
            main.AskRequest(
                question="Какой срок?", mode="baseline", multi_query_enabled=True
            ),
            db=object(),
        )

    assert error.value.status_code == 409


def test_server_disabled_multi_query_is_not_silently_ignored(monkeypatch):
    monkeypatch.setattr(main.settings, "multi_query_enabled", False)

    with pytest.raises(main.HTTPException) as error:
        main.ask(
            main.AskRequest(question="Какой срок?", multi_query_enabled=True),
            db=object(),
        )

    assert error.value.status_code == 409


def test_openapi_ask_contract_contains_optional_multi_query_field():
    schema = main.app.openapi()
    request_schema = schema["components"]["schemas"]["AskRequest"]

    assert "multi_query_enabled" in request_schema["properties"]
    assert "multi_query_enabled" not in request_schema.get("required", [])


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
    monkeypatch.setattr(llm_module.httpx, "post", lambda *args, **kwargs: next(responses))
    db = FakeDb()

    result = main.ask(main.AskRequest(question="Какой лимит установлен?"), db)

    assert result["outcome"] == "answered"
    assert result["answer"] == "Лимит составляет 10 рублей [1]."
    assert result["metrics"]["citations_valid"] is True
    assert result["llm_provider"] == "ollama"
    assert result["llm_model"] == main.settings.ollama_model
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
    monkeypatch.setattr(llm_module.httpx, "post", lambda *args, **kwargs: next(responses))
    db = FakeDb()

    result = main.ask(main.AskRequest(question="Кому применяется правило?"), db)

    assert result["outcome"] == "quality_review_required"
    assert "применяется всем" not in result["answer"]
    assert result["reason_code"] == "answer_validation_failed"


def test_baseline_inconsistent_validator_result_does_not_publish_draft(monkeypatch):
    monkeypatch.setattr(main.settings, "agent_enabled", False)
    monkeypatch.setattr(main, "retrieve", lambda question: ([evidence()], 1, retrieval_trace()))
    draft = "Лимит применяется всем подразделениям [1]."
    responses = iter(
        [
            Response(draft),
            Response(
                '{"valid":true,"unsupported_claims":["unsupported scope"],'
                '"scope_errors":[]}'
            ),
        ]
    )
    monkeypatch.setattr(llm_module.httpx, "post", lambda *args, **kwargs: next(responses))
    db = FakeDb()

    result = main.ask(main.AskRequest(question="Кому применяется лимит?"), db)

    assert result["outcome"] == "quality_review_required"
    assert result["reason_code"] == "answer_validation_failed"
    assert result["answer"] != draft
    assert "применяется всем подразделениям" not in result["answer"]
    assert db.trace.details["validation_failures"] == ["unsupported scope"]


def test_baseline_invalid_validator_json_is_a_technical_error(monkeypatch):
    monkeypatch.setattr(main.settings, "agent_enabled", False)
    monkeypatch.setattr(main, "retrieve", lambda question: ([evidence()], 1, retrieval_trace()))
    responses = iter([Response("Лимит составляет 10 рублей [1]."), Response("not-json")])
    monkeypatch.setattr(llm_module.httpx, "post", lambda *args, **kwargs: next(responses))
    db = FakeDb()

    result = main.ask(main.AskRequest(question="Какой лимит установлен?"), db)

    assert result["outcome"] == "technical_error"
    assert result["reason_code"] == "baseline_runtime_error"
    assert db.trace.status == "failed"
