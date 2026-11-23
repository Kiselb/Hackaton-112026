from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

from clip_agent.agent import AgentLimits, BoundedAgent, run_agent


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
                                "sufficient": True,
                                "source_ids": [source_id],
                                "reason": "covered",
                            },
                        ]
                    }
                ), usage
            aspect_ids = ["a1", "a2"] if '"a1"' in prompt else ["aspect-1"]
            return json.dumps(
                {
                    "aspects": [
                        {
                            "aspect_id": aspect_id,
                            "sufficient": True,
                            "source_ids": [source_id],
                            "reason": "covered",
                        }
                        for aspect_id in aspect_ids
                    ]
                }
            ), usage
        if "Validate whether" in prompt:
            return '{"valid":true,"unsupported_claims":[]}', usage
        if "Черновик:" in prompt:
            return "Исправленный ответ [1].", usage
        return self.draft, usage


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


def test_multi_aspect_plan_preserves_original_question():
    question = "Чем отличаются первый и второй лимиты?"
    retriever, llm = Retriever(), ScriptedLlm()
    state = agent(retriever, llm).invoke(question, "u", "s", "r")

    assert state["search_queries"][0] == question
    assert len(state["plan"]) == 2
    assert state["trajectory"][0]["decision"] == "planned"


def test_gap_triggers_targeted_retrieval_and_second_assessment():
    retriever, llm = Retriever(), ScriptedLlm(gap_first=True)
    state = agent(retriever, llm).invoke(
        "Чем отличаются первый и второй лимиты?", "u", "s", "r"
    )

    nodes = [item["node"] for item in state["trajectory"]]
    assert "targeted_retrieve" in nodes
    assert llm.assessments == 2
    assert state["retries"] == 1
    assert state["search_calls"] <= 3


def test_budgets_are_strict_and_graph_finishes_without_unbounded_cycle():
    retriever, llm = Retriever(empty_first=True), ScriptedLlm(gap_first=True)
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
    assert "достаточно данных" in state["answer"].lower()


def test_invalid_citations_are_repaired_within_budget():
    retriever = Retriever()
    llm = ScriptedLlm(draft="Ответ с ошибочной ссылкой [99].")
    state = agent(retriever, llm, llm_calls=3).invoke(
        "Какой лимит установлен?", "u", "s", "r"
    )

    assert state["answer"] == "Исправленный ответ [1]."
    assert state["validation"]["citations_valid"] is True
    assert state["validation"]["citation_repair_attempted"] is True
    assert state["llm_calls"] == 3
    assert state["llm_calls"] <= 3


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


def test_agent_exception_falls_back_to_existing_rag(monkeypatch):
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

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {
                "message": {"content": "Baseline answer [1]."},
                "prompt_eval_count": 11,
                "eval_count": 4,
                "done_reason": "stop",
            }

    def fail_agent(*args, **kwargs):
        raise RuntimeError("agent failed")

    evidence = source()
    monkeypatch.setattr(main.settings, "agent_enabled", True)
    monkeypatch.setattr(main, "run_agent", fail_agent)
    monkeypatch.setattr(
        main,
        "retrieve",
        lambda question: ([evidence], 1, trace()),
    )
    monkeypatch.setattr(main.httpx, "post", lambda *args, **kwargs: Response())
    db = FakeDb()

    result = main.ask(
        main.AskRequest(question="Какой лимит?", user_id="u", session_id="s"),
        db,
    )

    assert result["answer"] == "Baseline answer [1]."
    assert result["metrics"]["agent_enabled"] is False
    assert result["metrics"]["agent_fallback_reason"] == "agent_runtime_error"
    assert db.trace.details["mode"] == "baseline"
    assert db.trace.details["agent_fallback_reason"] == "agent failed"
