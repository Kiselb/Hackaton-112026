from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

import pytest

from clip_agent.agent import (
    EVIDENCE_ASSESSMENT_SCHEMA,
    AgentLimits,
    BoundedAgent,
    run_agent,
)
from clip_agent.llm import LlmProviderError


def source(chunk_id: str = "chunk-1", text: str = "Подтверждённый факт.") -> dict[str, Any]:
    return {
        "chunk_id": chunk_id,
        "document_id": f"doc-{chunk_id}",
        "title": "Источник",
        "filename": f"{chunk_id}.txt",
        "parent_text": text,
        "child_text": text,
        "child_start": 0,
        "child_end": len(text),
        "rrf_score": 0.03,
        "rank_before_rerank": 1,
        "reranker_score": None,
        "rank_after_rerank": 1,
    }


def trace() -> dict[str, Any]:
    return {
        "enabled": False,
        "status": "disabled",
        "latency_ms": 0.0,
        "fallback_reason": None,
    }


def assessment_payload(
    *,
    outcome: str = "answered",
    aspect_id: str = "aspect-1",
    source_ids: list[str] | None = None,
    sufficient: bool = True,
) -> dict[str, Any]:
    ids = ["chunk-1"] if source_ids is None else source_ids
    return {
        "outcome": outcome,
        "reason_code": "synthetic_assessment",
        "clarification_question": "",
        "conflicts": [],
        "aspects": [
            {
                "aspect_id": aspect_id,
                "sufficient": sufficient,
                "source_ids": ids,
                "reason": "synthetic reason",
                "gap": "" if sufficient else "missing evidence",
                "target_query": "" if sufficient else "targeted query",
            }
        ],
    }


class Retriever:
    def __init__(self, empty_first: bool = False):
        self.queries: list[str] = []
        self.empty_first = empty_first

    def __call__(self, query: str, limit: int | None):
        self.queries.append(query)
        if self.empty_first and len(self.queries) == 1:
            return [], 0, trace()
        item = source(f"chunk-{len(self.queries)}")
        return [item], 1, trace()


class ScriptedLlm:
    def __init__(
        self,
        *,
        invalid_planner: bool = False,
        gap_first: bool = False,
        draft: str = "Подтверждённый ответ [1].",
    ):
        self.invalid_planner = invalid_planner
        self.gap_first = gap_first
        self.draft = draft
        self.calls: list[str] = []
        self.assessments = 0

    def __call__(self, prompt: str):
        self.calls.append(prompt)
        usage = {"prompt_eval_count": 10, "eval_count": 3}
        if "Decompose" in prompt:
            if self.invalid_planner:
                return "not-json", usage
            return json.dumps(
                {
                    "aspects": [
                        {"aspect_id": "a1", "question": "Первый аспект"},
                        {"aspect_id": "a2", "question": "Второй аспект"},
                    ],
                    "search_queries": ["первый аспект", "второй аспект"],
                }
            ), usage
        if "Assess evidence" in prompt:
            self.assessments += 1
            ids = [
                value.split(";")[0]
                for value in prompt.split("source_id=")[1:]
            ]
            source_id = ids[0] if ids else ""
            if self.gap_first and self.assessments == 1:
                return json.dumps(
                    {
                        "outcome": "answered",
                        "reason_code": "evidence_gap",
                        "clarification_question": "",
                        "conflicts": [],
                        "aspects": [
                            {
                                "aspect_id": "a1",
                                "sufficient": False,
                                "source_ids": [],
                                "reason": "gap",
                                "gap": "missing aspect",
                                "target_query": "точечный запрос",
                            },
                            {
                                "aspect_id": "a2",
                                "sufficient": bool(source_id),
                                "source_ids": [source_id] if source_id else [],
                                "reason": "covered" if source_id else "no evidence",
                                "gap": "" if source_id else "missing evidence",
                                "target_query": "" if source_id else "точечный запрос",
                            },
                        ]
                    }
                ), usage
            aspect_ids = ["a1", "a2"] if '"a1"' in prompt else ["aspect-1"]
            return json.dumps(
                {
                    "outcome": "answered",
                    "reason_code": "evidence_sufficient",
                    "clarification_question": "",
                    "conflicts": [],
                    "aspects": [
                        {
                            "aspect_id": aspect_id,
                            "sufficient": bool(source_id),
                            "source_ids": [source_id] if source_id else [],
                            "reason": "covered" if source_id else "no evidence",
                            "gap": "" if source_id else "missing evidence",
                            "target_query": "" if source_id else "точечный запрос",
                        }
                        for aspect_id in aspect_ids
                    ]
                }
            ), usage
        if "Validate whether" in prompt:
            return (
                '{"valid":true,"unsupported_claims":[],"scope_errors":[]}',
                usage,
            )
        if "Черновик:" in prompt:
            return "Исправленный ответ [1].", usage
        return self.draft, usage


class AssessmentOverrideLlm(ScriptedLlm):
    def __init__(self, response: str | dict[str, Any]) -> None:
        super().__init__()
        self.response = response

    def __call__(self, prompt: str):
        if "Assess evidence" in prompt:
            self.calls.append(prompt)
            value = self.response
            return (
                json.dumps(value) if isinstance(value, dict) else value,
                {"prompt_eval_count": 12, "eval_count": 7, "done_reason": "stop"},
            )
        return super().__call__(prompt)


def agent(
    retriever: Retriever,
    llm: ScriptedLlm,
    *,
    search_calls: int = 3,
    retries: int = 1,
    llm_calls: int = 4,
) -> BoundedAgent:
    return BoundedAgent(
        AgentLimits(
            max_search_calls=search_calls,
            max_retries=retries,
            max_llm_calls=llm_calls,
        ),
        retriever=retriever,
        llm=llm,
    )


def test_simple_route_skips_planner():
    retriever, llm = Retriever(), ScriptedLlm()
    state = agent(retriever, llm).invoke("Какой лимит установлен?", "u", "s", "r")

    assert state["trajectory"][0]["decision"] == "fast_route"
    assert not any("Decompose" in prompt for prompt in llm.calls)
    assert [item["node"] for item in state["trajectory"]] == [
        "route_and_plan",
        "retrieve",
        "assess_evidence",
        "compose_answer",
        "validate_answer",
        "finish",
    ]
    assert state["outcome"] == "answered"


def test_evidence_assessment_uses_schema_and_operation_output_budget():
    class StructuredLlm(ScriptedLlm):
        def __init__(self) -> None:
            super().__init__()
            self.structured_calls: list[dict[str, Any]] = []

        def generate(self, prompt: str, **kwargs):
            self.structured_calls.append(kwargs)
            return self(prompt)

    llm = StructuredLlm()
    runtime = BoundedAgent(
        AgentLimits(),
        retriever=Retriever(),
        llm=llm,
        assessment_max_output_tokens=777,
    )

    state = runtime.invoke("Какой лимит установлен?", "u", "s", "r")

    assessment_call = next(
        item for item in llm.structured_calls if item["schema_name"] == "evidence_assessment"
    )
    assert assessment_call["json_schema"]["type"] == EVIDENCE_ASSESSMENT_SCHEMA["type"]
    aspect_schema = assessment_call["json_schema"]["properties"]["aspects"]["items"]
    assert aspect_schema["properties"]["aspect_id"]["enum"] == ["aspect-1"]
    assert aspect_schema["properties"]["source_ids"]["items"]["enum"] == [
        "chunk-1"
    ]
    assert assessment_call["max_output_tokens"] == 777
    assert state["outcome"] == "answered"


@pytest.mark.parametrize(
    ("payload", "error_fragment"),
    [
        (assessment_payload(source_ids=["unknown-source"]), "unknown source_id"),
        (assessment_payload(aspect_id="unknown-aspect"), "unknown aspect_id"),
        (assessment_payload(outcome="unexpected"), "outcome is not allowed"),
        (
            {
                **assessment_payload(),
                "aspects": [
                    {**assessment_payload()["aspects"][0], "sufficient": "true"}
                ],
            },
            "must be boolean",
        ),
        (
            {
                **assessment_payload(),
                "aspects": assessment_payload()["aspects"] * 2,
            },
            "duplicate aspect_id",
        ),
    ],
)
def test_invalid_evidence_assessment_semantics_fail_closed(payload, error_fragment):
    state = agent(Retriever(), AssessmentOverrideLlm(payload)).invoke(
        "Какой лимит установлен?", "u", "s", "r"
    )

    assert state["outcome"] == "technical_error"
    assert state["reason_code"] == "evidence_assessment_failed"
    assert any(error_fragment in item["error"] for item in state["errors"])


@pytest.mark.parametrize("response", ["", '{"outcome":"answered"'])
def test_empty_or_incomplete_evidence_assessment_fails_closed(response):
    state = agent(Retriever(), AssessmentOverrideLlm(response)).invoke(
        "Какой лимит установлен?", "u", "s", "r"
    )

    assert state["outcome"] == "technical_error"
    event = next(item for item in state["trajectory"] if item["node"] == "assess_evidence")
    assert event["structured_output"]["json_object_recognized"] is False
    assert event["structured_output"]["rejection_reason"] in {
        "empty_response",
        "malformed_json",
    }


@pytest.mark.parametrize(
    ("category", "attempted", "reason_code"),
    [
        ("output_limit", True, "evidence_assessment_output_limit"),
        ("token_budget_exceeded", False, "evidence_assessment_token_budget_exceeded"),
        ("network", True, "evidence_assessment_failed"),
    ],
)
def test_evidence_assessment_provider_failures_are_classified(
    category, attempted, reason_code
):
    class FailingLlm:
        def generate(self, prompt: str, **kwargs):
            del kwargs
            if "Assess evidence" in prompt:
                raise LlmProviderError(
                    category,
                    "controlled provider error",
                    attempted=attempted,
                    usage={
                        "prompt_eval_count": 20 if attempted else 0,
                        "eval_count": 64 if attempted else 0,
                        "done_reason": "length" if category == "output_limit" else None,
                        "output_limit_reached": category == "output_limit",
                        "max_output_tokens": 64,
                    },
                )
            raise AssertionError("no other LLM call is allowed after assessment failure")

    state = agent(Retriever(), FailingLlm()).invoke(
        "Какой лимит установлен?", "u", "s", "r"
    )

    assert state["outcome"] == "technical_error"
    assert state["reason_code"] == reason_code
    assert state["llm_calls"] == int(attempted)
    event = next(item for item in state["trajectory"] if item["node"] == "assess_evidence")
    assert event["structured_output"]["rejection_reason"] == category
    assert event["structured_output"]["output_limit_reached"] is (
        category == "output_limit"
    )


def test_quality_review_required_is_preserved_without_answer_fallback():
    payload = assessment_payload(
        outcome="quality_review_required", source_ids=[], sufficient=False
    )
    state = agent(
        Retriever(), AssessmentOverrideLlm(payload), retries=0
    ).invoke("Какой лимит установлен?", "u", "s", "r")

    assert state["outcome"] == "quality_review_required"
    assert state["status"] == "abstained"
    assert "службу качества" in state["answer"]


def test_unconfirmed_conflict_is_rejected():
    payload = assessment_payload(
        outcome="conflict_detected", source_ids=["chunk-1", "chunk-2"]
    )
    payload["conflicts"] = [
        {
            "source_ids": ["chunk-1", "chunk-2"],
            "summary": "Документы содержат разные сведения.",
        }
    ]

    class TwoGenericSources:
        def __call__(self, query: str, limit: int | None):
            return [source("chunk-1"), source("chunk-2")], 2, trace()

    state = agent(TwoGenericSources(), AssessmentOverrideLlm(payload)).invoke(
        "Какой срок установлен?", "u", "s", "r"
    )

    assert state["outcome"] == "technical_error"
    assert any("not confirmed" in item["error"] for item in state["errors"])


def test_assessment_trace_keeps_safe_structured_diagnostics():
    state = agent(Retriever(), ScriptedLlm()).invoke(
        "Какой лимит установлен?", "u", "s", "r"
    )
    event = next(item for item in state["trajectory"] if item["node"] == "assess_evidence")
    diagnostics = event["structured_output"]

    assert diagnostics["operation"] == "evidence_assessment"
    assert diagnostics["json_schema_requested"] is True
    assert diagnostics["json_object_recognized"] is True
    assert diagnostics["rejection_reason"] is None
    assert "content" not in diagnostics
    assert event["evidence_count"] == 1


def test_multi_query_expands_simple_question_and_preserves_grounding_budget():
    retriever, llm = Retriever(), ScriptedLlm()
    state = agent(retriever, llm).invoke(
        "Какой лимит установлен?", "u", "s", "r", multi_query_enabled=True
    )

    assert state["trajectory"][0]["decision"] == "planned_with_multi_query"
    assert retriever.queries[0] == "Какой лимит установлен?"
    assert len(retriever.queries) == 2
    assert state["multi_query_effective"] is True
    assert state["multi_query_status"] == "applied"
    assert state["llm_calls"] == 4
    assert "Validate whether" in llm.calls[-1]
    assert state["validation"]["valid"] is True


def test_multi_query_skips_expansion_when_validator_budget_would_be_lost():
    retriever, llm = Retriever(), ScriptedLlm()
    state = agent(retriever, llm, llm_calls=3).invoke(
        "Какой лимит установлен?", "u", "s", "r", multi_query_enabled=True
    )

    assert retriever.queries == ["Какой лимит установлен?"]
    assert state["multi_query_effective"] is False
    assert state["multi_query_status"] == "skipped_llm_budget"
    assert state["multi_query_fallback_reason"] == "grounding_validation_budget_reserved"
    assert state["validation"]["valid"] is True


def test_multi_query_removes_duplicates_and_rejects_scope_drift():
    class DuplicatePlanner(ScriptedLlm):
        def __call__(self, prompt: str):
            if "Decompose" in prompt:
                self.calls.append(prompt)
                return json.dumps(
                    {
                        "aspects": [{"aspect_id": "aspect-1", "question": "Кто назначает?"}],
                        "search_queries": [
                            "В Дирекции ИБ кто назначает руководителя проекта?",
                            "Кто назначает руководителя проекта в технической дирекции?",
                        ],
                    }
                ), {"prompt_eval_count": 2, "eval_count": 1}
            return super().__call__(prompt)

    question = "Кто назначает руководителя проекта в Дирекции ИБ?"
    retriever, llm = Retriever(), DuplicatePlanner()
    state = agent(retriever, llm).invoke(
        question, "u", "s", "r", multi_query_enabled=True
    )

    assert retriever.queries == [question]
    assert state["multi_query_status"] == "expansion_unavailable"
    assert state["multi_query_generated_count"] == 0


def test_multi_query_rrf_merges_ranks_and_deduplicates_chunk_ids():
    class RankedRetriever:
        def __init__(self):
            self.queries: list[str] = []

        def __call__(self, query: str, limit: int | None):
            self.queries.append(query)
            if len(self.queries) == 1:
                return [source("a"), source("b")], 2, trace()
            return [source("b"), source("c")], 2, trace()

    retriever, llm = RankedRetriever(), ScriptedLlm()
    state = agent(retriever, llm).invoke(
        "Какой лимит установлен?", "u", "s", "r", multi_query_enabled=True
    )

    assert [item["chunk_id"] for item in state["evidence"]] == ["b", "a", "c"]
    assert len(state["evidence"][0]["query_contributions"]) == 2
    assert state["multi_query_unique_sources"] == 3
    assert state["multi_query_deduplicated_count"] == 1


def test_multi_query_continues_with_original_when_alternative_retrieval_fails():
    class PartialRetriever(Retriever):
        def __call__(self, query: str, limit: int | None):
            self.queries.append(query)
            if len(self.queries) == 2:
                raise RuntimeError("synthetic retrieval failure")
            return [source("original")], 1, trace()

    retriever, llm = PartialRetriever(), ScriptedLlm()
    state = agent(retriever, llm).invoke(
        "Какой лимит установлен?", "u", "s", "r", multi_query_enabled=True
    )

    assert state["multi_query_status"] == "partial_retrieval"
    assert state["multi_query_fallback_reason"] == "one_or_more_retrievals_failed"
    assert [item["chunk_id"] for item in state["evidence"]] == ["original"]
    assert state["outcome"] == "answered"


def test_multi_aspect_plan_preserves_original_question():
    question = "Чем отличаются первый и второй лимиты?"
    retriever, llm = Retriever(), ScriptedLlm()
    state = agent(retriever, llm).invoke(question, "u", "s", "r")

    assert state["search_queries"][0] == question
    assert len(state["plan"]) == 2
    assert state["trajectory"][0]["decision"] == "planned"


def test_gap_triggers_targeted_retrieval_and_second_assessment():
    retriever, llm = Retriever(), ScriptedLlm(gap_first=True)
    state = agent(retriever, llm, llm_calls=5).invoke(
        "Чем отличаются первый и второй лимиты?", "u", "s", "r"
    )

    nodes = [item["node"] for item in state["trajectory"]]
    assert "targeted_retrieve" in nodes
    assert llm.assessments == 2
    assert state["retries"] == 1
    assert state["search_calls"] <= 3


def test_budgets_are_strict_and_graph_finishes_without_unbounded_cycle():
    retriever, llm = Retriever(empty_first=True), ScriptedLlm()
    state = agent(
        retriever, llm, search_calls=1, retries=0, llm_calls=1
    ).invoke("Какой лимит установлен?", "u", "s", "r")

    assert state["search_calls"] == 1
    assert state["llm_calls"] == 1
    assert state["step_count"] <= 8
    assert state["trajectory"][-1]["node"] == "finish"
    assert state["status"] == "abstained"


def test_invalid_planner_json_uses_original_question_fallback():
    retriever, llm = Retriever(), ScriptedLlm(invalid_planner=True)
    question = "Чем отличаются первый и второй лимиты?"
    state = agent(retriever, llm).invoke(question, "u", "s", "r")

    assert state["plan"] == [{"aspect_id": "aspect-1", "question": question}]
    assert state["search_queries"] == [question]
    assert state["trajectory"][0]["decision"] == "planner_fallback"


def test_support_term_is_not_copied_from_license():
    retriever = Retriever()
    llm = ScriptedLlm(
        draft="Техническая поддержка предоставляется на 12 месяцев [1]."
    )
    state = agent(retriever, llm).invoke(
        "На какой срок предоставляется техническая поддержка?", "u", "s", "r"
    )

    assert state["status"] == "abstained"
    assert "нельзя надёжно подтвердить" in state["answer"]
    assert "license_term_transferred_to_support" in state["validation"]["guard_failures"]


def test_missing_auto_renewal_is_not_a_categorical_prohibition():
    retriever = Retriever()
    llm = ScriptedLlm(draft="Лицензия не продлевается автоматически [1].")
    state = agent(retriever, llm).invoke(
        "Продлевается ли лицензия автоматически?", "u", "s", "r"
    )

    assert state["status"] == "abstained"
    assert "absence_treated_as_renewal_fact" in state["validation"]["guard_failures"]


def test_no_evidence_produces_controlled_abstention():
    retriever, llm = Retriever(empty_first=True), ScriptedLlm()
    state = agent(retriever, llm, retries=0).invoke(
        "Какой срок не указан?", "u", "s", "r"
    )

    assert state["status"] == "abstained"
    assert state["refusal_reason"] == "no_evidence"
    assert "достаточного подтверждения" in state["answer"].lower()


def test_invalid_citations_are_repaired_within_budget():
    retriever = Retriever()
    llm = ScriptedLlm(draft="Ответ с ошибочной ссылкой [99].")
    state = agent(retriever, llm, llm_calls=4).invoke(
        "Какой лимит установлен?", "u", "s", "r"
    )

    assert state["answer"] == "Исправленный ответ [1]."
    assert state["validation"]["citations_valid"] is True
    assert state["validation"]["citation_repair_attempted"] is True
    assert state["llm_calls"] == 4
    assert state["llm_calls"] <= 4


def test_trace_contains_identity_trajectory_and_counts():
    retriever, llm = Retriever(), ScriptedLlm()
    result = run_agent(
        "Какой лимит установлен?",
        "user-a",
        "session-a",
        "request-a",
        agent=agent(retriever, llm),
    )
    details = result["trace_details"]

    assert details["request_id"] == "request-a"
    assert details["user_id"] == "user-a"
    assert details["session_id"] == "session-a"
    assert details["trajectory"]
    assert all(
        "latency_ms" in event
        and "prompt_tokens" in event
        and "completion_tokens" in event
        for event in details["trajectory"]
    )
    assert details["search_calls"] == len(retriever.queries)
    assert details["llm_calls"] == len(llm.calls)


def test_invocations_do_not_share_user_or_session_state():
    retriever, llm = Retriever(), ScriptedLlm()
    runtime = agent(retriever, llm)
    first = runtime.invoke("Какой лимит установлен?", "u1", "s1", "r1")
    second = runtime.invoke("Какой лимит установлен?", "u2", "s2", "r2")

    assert (first["user_id"], first["session_id"]) == ("u1", "s1")
    assert (second["user_id"], second["session_id"]) == ("u2", "s2")
    assert first["request_id"] != second["request_id"]
    assert first["step_count"] == second["step_count"]


def test_agent_exception_returns_technical_error_without_baseline(monkeypatch):
    from clip_agent import main

    class FakeDb:
        def __init__(self):
            self.trace = None

        def add(self, value):
            self.trace = value
            if value.id is None:
                value.id = uuid4()

        def commit(self):
            pass

    def fail_agent(*args, **kwargs):
        raise RuntimeError("agent failed")

    monkeypatch.setattr(main.settings, "agent_enabled", True)
    monkeypatch.setattr(main, "run_agent", fail_agent)
    db = FakeDb()

    result = main.ask(
        main.AskRequest(question="Какой лимит?", user_id="u", session_id="s"),
        db,
    )

    assert result["outcome"] == "technical_error"
    assert result["metrics"]["agent_enabled"] is True
    assert result["reason_code"] == "agent_runtime_error"
    assert db.trace.details["mode"] == "langgraph_agent"
    assert db.trace.details["fallback_attempted"] is False
    assert db.trace.details["error"] == "agent failed"


def test_invalid_validator_json_fails_closed():
    class InvalidValidator(ScriptedLlm):
        def __call__(self, prompt: str):
            if "Validate whether" in prompt:
                self.calls.append(prompt)
                return "not-json", {"prompt_eval_count": 2, "eval_count": 1}
            return super().__call__(prompt)

    state = agent(Retriever(), InvalidValidator()).invoke(
        "Какой лимит установлен?", "u", "s", "r"
    )

    assert state["outcome"] == "technical_error"
    assert state["status"] == "failed"
    assert "grounding_validator_invalid_json" in state["validation"]["llm_failures"]


def test_inconsistent_validator_result_does_not_publish_agent_draft():
    class InconsistentValidator(ScriptedLlm):
        def __call__(self, prompt: str):
            if "Validate whether" in prompt:
                self.calls.append(prompt)
                return json.dumps(
                    {
                        "valid": True,
                        "unsupported_claims": ["draft contains an unsupported conclusion"],
                        "scope_errors": [],
                    }
                ), {"prompt_eval_count": 2, "eval_count": 1}
            return super().__call__(prompt)

    draft = "Неподтверждённый корпоративный вывод [1]."
    state = agent(Retriever(), InconsistentValidator(draft=draft)).invoke(
        "Какой лимит установлен?", "u", "s", "r"
    )

    assert state["outcome"] == "quality_review_required"
    assert state["status"] == "abstained"
    assert state["answer"] != draft
    assert "Неподтверждённый корпоративный вывод" not in state["answer"]
    assert state["validation"]["valid"] is False
    assert state["validation"]["llm_failures"] == [
        "draft contains an unsupported conclusion"
    ]


def test_unsupported_scope_conclusion_is_not_published():
    class RejectingValidator(ScriptedLlm):
        def __call__(self, prompt: str):
            if "Validate whether" in prompt:
                self.calls.append(prompt)
                return json.dumps(
                    {
                        "valid": False,
                        "unsupported_claims": [],
                        "scope_errors": ["правило другого подразделения"],
                    }
                ), {"prompt_eval_count": 2, "eval_count": 1}
            return super().__call__(prompt)

    state = agent(Retriever(), RejectingValidator()).invoke(
        "Применяется ли правило к другому подразделению?", "u", "s", "r"
    )

    assert state["outcome"] == "quality_review_required"
    assert "другого подразделения" not in state["answer"]
    assert state["refusal_reason"] == "answer_validation_failed"


def test_grounding_validator_error_never_starts_baseline_fallback(monkeypatch):
    from clip_agent import main

    class FakeDb:
        def __init__(self):
            self.trace = None

        def add(self, value):
            self.trace = value
            if value.id is None:
                value.id = uuid4()

        def commit(self):
            pass

    def validator_error(*args, **kwargs):
        return {
            "answer": (
                "Не удалось завершить автоматическую проверку ответа по источникам. "
                "Неподтверждённый ответ не опубликован."
            ),
            "sources": [],
            "retrieval": trace(),
            "retrieved_count": 1,
            "usage": {"prompt_eval_count": 10, "eval_count": 2},
            "trace_details": {
                "mode": "langgraph_agent",
                "outcome": "technical_error",
                "reason_code": "grounding_validator_invalid_json",
                "validation": {
                    "valid": False,
                    "citations_valid": True,
                    "llm_failures": ["grounding_validator_invalid_json"],
                },
                "search_calls": 1,
                "llm_calls": 3,
                "step_count": 6,
                "retries": 0,
            },
        }

    monkeypatch.setattr(main.settings, "agent_enabled", True)
    monkeypatch.setattr(main, "run_agent", validator_error)
    monkeypatch.setattr(
        main,
        "retrieve",
        lambda question: (_ for _ in ()).throw(AssertionError("baseline fallback ran")),
    )

    result = main.ask(
        main.AskRequest(question="Какой лимит?", user_id="u", session_id="s"), FakeDb()
    )

    assert result["outcome"] == "technical_error"
    assert result["reason_code"] == "grounding_validator_invalid_json"
    assert result["metrics"]["agent_enabled"] is True


def test_real_source_ambiguity_returns_specific_clarification():
    class TwoSources:
        def __call__(self, query: str, limit: int | None):
            return [source("presale"), source("delivery")], 2, trace()

    class AmbiguityLlm(ScriptedLlm):
        def __call__(self, prompt: str):
            if "Assess evidence" in prompt:
                self.calls.append(prompt)
                return json.dumps(
                    {
                        "outcome": "clarification_required",
                        "reason_code": "ambiguous_process_scope",
                        "clarification_question": (
                            "Уточните, пожалуйста, подразделение и этап процесса: "
                            "пресейл или реализация проекта?"
                        ),
                        "conflicts": [],
                        "aspects": [
                            {
                                "aspect_id": "aspect-1",
                                "sufficient": True,
                                "source_ids": ["presale", "delivery"],
                                "reason": "different process scopes",
                                "gap": "",
                                "target_query": "",
                            }
                        ],
                    }
                ), {"prompt_eval_count": 2, "eval_count": 1}
            return super().__call__(prompt)

    state = agent(TwoSources(), AmbiguityLlm()).invoke("Кто назначает РП?", "u", "s", "r")

    assert state["outcome"] == "clarification_required"
    assert "пресейл или реализация" in state["answer"]
    assert state["llm_calls"] == 1


def test_semantic_validation_catches_unscoped_role_ambiguity() -> None:
    class TwoAppointmentSources:
        def __call__(self, query: str, limit: int | None):
            return [
                source("infosec", "Координатор департамента назначает РП проекта."),
                source(
                    "technical",
                    "Руководитель отдела управления проектами назначает РП проекта.",
                ),
            ], 2, trace()

    llm = AssessmentOverrideLlm(
        assessment_payload(source_ids=["infosec"], outcome="answered")
    )
    state = agent(TwoAppointmentSources(), llm).invoke(
        "Кто назначает РП?", "u", "s", "r"
    )

    assert state["outcome"] == "clarification_required"
    assert "подразделение и этап" in state["answer"]
    assert state["llm_calls"] == 1


def test_two_source_conflict_is_reported_without_choosing_priority():
    class TwoSources:
        def __call__(self, query: str, limit: int | None):
            return [
                source("rule-a", "Срок исполнения задачи составляет 5 рабочих дней."),
                source("rule-b", "Срок исполнения задачи составляет 10 рабочих дней."),
            ], 2, trace()

    class ConflictLlm(ScriptedLlm):
        def __call__(self, prompt: str):
            if "Assess evidence" in prompt:
                self.calls.append(prompt)
                return json.dumps(
                    {
                        "outcome": "conflict_detected",
                        "reason_code": "conflicting_provisions",
                        "clarification_question": "",
                        "conflicts": [
                            {
                                "source_ids": ["rule-a", "rule-b"],
                                "summary": "Документы устанавливают разные сроки.",
                            }
                        ],
                        "aspects": [
                            {
                                "aspect_id": "aspect-1",
                                "sufficient": True,
                                "source_ids": ["rule-a", "rule-b"],
                                "reason": "conflict",
                                "gap": "",
                                "target_query": "",
                            }
                        ],
                    }
                ), {"prompt_eval_count": 2, "eval_count": 1}
            return super().__call__(prompt)

    state = agent(TwoSources(), ConflictLlm()).invoke("Какой срок установлен?", "u", "s", "r")

    assert state["outcome"] == "conflict_detected"
    assert "[1]" in state["answer"] and "[2]" in state["answer"]
    assert "приоритет документов нельзя" in state["answer"]
    assert "службу качества" in state["answer"]
