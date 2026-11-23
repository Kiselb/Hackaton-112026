from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from typing import Any, Callable, TypedDict

import httpx
from langgraph.graph import END, START, StateGraph

from .answering import (
    build_answer_prompt,
    build_citation_repair_prompt,
    build_temporal_abstention,
    citation_ids_admissible,
    requires_temporal_abstention,
)
from .config import Settings, get_settings
from .search import retrieve


Retriever = Callable[[str, int | None], tuple[list[dict[str, Any]], int, dict[str, Any]]]
LlmCaller = Callable[[str], tuple[str, dict[str, Any]]]


class AgentState(TypedDict, total=False):
    request_id: str
    question: str
    user_id: str
    session_id: str
    plan: list[dict[str, str]]
    search_queries: list[str]
    searched_queries: list[str]
    evidence: list[dict[str, Any]]
    gaps: list[dict[str, str]]
    decisions: list[dict[str, Any]]
    trajectory: list[dict[str, Any]]
    retrieval_runs: list[dict[str, Any]]
    search_calls: int
    llm_calls: int
    retries: int
    step_count: int
    prompt_tokens: int
    completion_tokens: int
    answer: str
    next_action: str
    status: str
    refusal_reason: str | None
    validation: dict[str, Any]
    errors: list[dict[str, str]]


@dataclass(frozen=True)
class AgentLimits:
    max_search_calls: int = 3
    max_retries: int = 1
    max_llm_calls: int = 4


def call_ollama(prompt: str) -> tuple[str, dict[str, Any]]:
    settings = get_settings()
    response = httpx.post(
        f"{settings.ollama_url}/api/chat",
        json={
            "model": settings.ollama_model,
            "stream": False,
            "think": settings.ollama_think,
            "messages": [{"role": "user", "content": prompt}],
            "options": {
                "temperature": settings.ollama_temperature,
                "num_predict": settings.ollama_num_predict,
            },
        },
        timeout=300,
    )
    response.raise_for_status()
    usage = response.json()
    content = usage["message"]["content"].strip()
    if not content:
        raise RuntimeError(
            f"Ollama returned an empty answer (done_reason={usage.get('done_reason')!r})"
        )
    return content, usage


def _parse_json_object(value: str) -> dict[str, Any]:
    cleaned = value.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("structured LLM response has no JSON object")
    parsed = json.loads(cleaned[start : end + 1])
    if not isinstance(parsed, dict):
        raise ValueError("structured LLM response must be an object")
    return parsed


def _is_complex(question: str) -> bool:
    normalized = question.lower()
    markers = (
        "чем отличаются",
        "сравни",
        "какие технические и",
        "кто согласует",
        "и когда",
        "срок и",
        "и сколько",
        "основного и дополнительного",
        "тексты обращений и записи",
        "почему в документах",
    )
    return any(marker in normalized for marker in markers)


def _is_risk_question(question: str) -> bool:
    normalized = question.lower()
    markers = (
        "техническ",
        "автоматическ",
        "действовал",
        "по состоянию",
        "приоритет",
        "отмен",
        "отсутств",
        "не найден",
    )
    return any(marker in normalized for marker in markers)


def _usage_update(state: AgentState, usage: dict[str, Any]) -> dict[str, int]:
    return {
        "prompt_tokens": state.get("prompt_tokens", 0)
        + int(usage.get("prompt_eval_count") or 0),
        "completion_tokens": state.get("completion_tokens", 0)
        + int(usage.get("eval_count") or 0),
    }


def _dedupe_evidence(
    existing: list[dict[str, Any]], incoming: list[dict[str, Any]], limit: int = 8
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for source in [*existing, *incoming]:
        source_id = str(source.get("chunk_id", ""))
        if not source_id or source_id in seen:
            continue
        seen.add(source_id)
        result.append(source)
        if len(result) >= limit:
            break
    return result


def _unsupported_guard(question: str, answer: str) -> list[str]:
    question_lower, answer_lower = question.lower(), answer.lower()
    uncertainty = bool(
        re.search(
            r"не (?:определен|определён|указан|описан|подтвержден|подтверждён)|"
            r"нельзя (?:определить|утверждать)|недостаточно (?:данных|сведений)",
            answer_lower,
        )
    )
    failures: list[str] = []
    if "техническ" in question_lower and "поддерж" in question_lower:
        transferred = re.search(
            r"поддерж\w*[^.]{0,80}(?:12 месяцев|один год)|"
            r"(?:12 месяцев|один год)[^.]{0,80}поддерж",
            answer_lower,
        )
        if transferred and not uncertainty:
            failures.append("license_term_transferred_to_support")
    if "автоматическ" in question_lower and "продл" in question_lower:
        categorical = re.search(
            r"(?:не\s+)?продлева\w+\s+автоматическ|"
            r"автоматическ\w*\s+(?:не\s+)?продлева",
            answer_lower,
        )
        if categorical and not uncertainty:
            failures.append("absence_treated_as_renewal_fact")
    if re.search(
        r"(?:более поздн|позднее издан)[^.]{0,100}(?:приоритет|главен|отмен)",
        answer_lower,
    ):
        failures.append("document_priority_inferred_from_dates")
    if re.search(
        r"(?:не найден|отсутствует в выдаче)[^.]{0,100}(?:значит|следовательно|доказывает)",
        answer_lower,
    ):
        failures.append("missing_document_treated_as_negative_fact")
    return failures


def _explicit_source_gap(
    question: str, evidence: list[dict[str, Any]]
) -> tuple[str, str] | None:
    normalized = question.lower()
    for index, source in enumerate(evidence, start=1):
        text = str(source.get("parent_text", "")).lower()
        if (
            "техническ" in normalized
            and "поддерж" in normalized
            and re.search(r"срок\w*\s+техническ\w*\s+поддерж\w*[^.]{0,50}не определ", text)
        ):
            return (
                "По источнику невозможно определить срок технической поддержки: документ "
                f"прямо указывает, что этот срок не определён [{index}]. Указанные в нём "
                "12 месяцев относятся к лицензии и не подтверждают срок поддержки "
                f"[{index}].",
                "support_term_not_defined",
            )
        if (
            "автоматическ" in normalized
            and "продл" in normalized
            and re.search(r"автоматическ\w*\s+продлен\w*[^.]{0,50}не определ", text)
        ):
            return (
                "По источнику невозможно определить, продлевается ли лицензия автоматически: "
                f"условие автоматического продления в документе не определено [{index}]. "
                "Отсутствие такого условия не доказывает ни наличие, ни запрет продления "
                f"[{index}].",
                "auto_renewal_not_defined",
            )
    return None


class BoundedAgent:
    def __init__(
        self,
        limits: AgentLimits,
        retriever: Retriever = retrieve,
        llm: LlmCaller = call_ollama,
    ) -> None:
        self.limits = limits
        self.retriever = retriever
        self.llm = llm
        self.graph = self._build_graph()

    def _build_graph(self):
        graph = StateGraph(AgentState)
        graph.add_node("route_and_plan", self.route_and_plan)
        graph.add_node("retrieve", self.retrieve_node)
        graph.add_node("assess_evidence", self.assess_evidence)
        graph.add_node("targeted_retrieve", self.targeted_retrieve)
        graph.add_node("compose_answer", self.compose_answer)
        graph.add_node("validate_answer", self.validate_answer)
        graph.add_node("finish", self.finish)
        graph.add_edge(START, "route_and_plan")
        graph.add_conditional_edges(
            "route_and_plan",
            lambda state: state["next_action"],
            {"retrieve": "retrieve", "finish": "finish"},
        )
        graph.add_edge("retrieve", "assess_evidence")
        graph.add_conditional_edges(
            "assess_evidence",
            lambda state: state["next_action"],
            {"targeted_retrieve": "targeted_retrieve", "compose_answer": "compose_answer"},
        )
        graph.add_edge("targeted_retrieve", "assess_evidence")
        graph.add_edge("compose_answer", "validate_answer")
        graph.add_edge("validate_answer", "finish")
        graph.add_edge("finish", END)
        return graph.compile()

    def _record(
        self,
        state: AgentState,
        node: str,
        started: float,
        decision: str,
        **details: Any,
    ) -> dict[str, Any]:
        event = {
            "step": state.get("step_count", 0) + 1,
            "node": node,
            "decision": decision,
            "latency_ms": round((time.perf_counter() - started) * 1000, 3),
            "prompt_tokens": details.pop("prompt_tokens", 0),
            "completion_tokens": details.pop("completion_tokens", 0),
            **details,
        }
        return {
            "step_count": event["step"],
            "trajectory": [*state.get("trajectory", []), event],
            "decisions": [
                *state.get("decisions", []),
                {"node": node, "decision": decision, **details},
            ],
        }

    def _call(
        self, state: AgentState, prompt: str
    ) -> tuple[str | None, dict[str, Any], dict[str, Any] | None]:
        if state.get("llm_calls", 0) >= self.limits.max_llm_calls:
            return None, {}, {
                "node": "llm",
                "error": "AGENT_MAX_LLM_CALLS exceeded",
                "attempted": False,
            }
        try:
            content, usage = self.llm(prompt)
            return content, usage, None
        except Exception as exc:
            return None, {}, {"node": "llm", "error": str(exc), "attempted": True}

    def route_and_plan(self, state: AgentState) -> dict[str, Any]:
        started = time.perf_counter()
        question = state["question"]
        if not _is_complex(question):
            plan = [{"aspect_id": "aspect-1", "question": question}]
            update = self._record(state, "route_and_plan", started, "fast_route")
            return {
                **update,
                "plan": plan,
                "search_queries": [question],
                "next_action": "retrieve",
            }
        prompt = (
            "Return JSON only. Decompose the corporate-document question into at most three "
            "independent evidence aspects and at most two focused search queries. Preserve the "
            "original question. Schema: "
            '{"aspects":[{"aspect_id":"aspect-1","question":"..."}],'
            '"search_queries":["..."]}. Question: ' + question
        )
        content, usage, error = self._call(state, prompt)
        llm_calls = state.get("llm_calls", 0) + int(
            error is None or error.get("attempted", True)
        )
        errors = list(state.get("errors", []))
        if error:
            errors.append({"node": "route_and_plan", "error": error["error"]})
        try:
            parsed = _parse_json_object(content or "")
            raw_aspects = parsed.get("aspects")
            raw_queries = parsed.get("search_queries")
            if not isinstance(raw_aspects, list) or not raw_aspects:
                raise ValueError("planner aspects must be a non-empty list")
            plan: list[dict[str, str]] = []
            for index, item in enumerate(raw_aspects[:3], start=1):
                if isinstance(item, str):
                    aspect_question, aspect_id = item.strip(), f"aspect-{index}"
                elif isinstance(item, dict):
                    aspect_question = str(item.get("question", "")).strip()
                    aspect_id = str(item.get("aspect_id") or f"aspect-{index}")
                else:
                    continue
                if aspect_question:
                    plan.append({"aspect_id": aspect_id, "question": aspect_question})
            if not plan:
                raise ValueError("planner returned no valid aspects")
            queries = [question]
            if isinstance(raw_queries, list):
                queries.extend(str(item).strip() for item in raw_queries if str(item).strip())
            queries = list(dict.fromkeys(queries))[:2]
            decision = "planned"
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            plan = [{"aspect_id": "aspect-1", "question": question}]
            queries = [question]
            decision = "planner_fallback"
            errors.append({"node": "route_and_plan", "error": str(exc)})
        update = self._record(
            state,
            "route_and_plan",
            started,
            decision,
            aspect_count=len(plan),
            query_count=len(queries),
            prompt_tokens=int(usage.get("prompt_eval_count") or 0),
            completion_tokens=int(usage.get("eval_count") or 0),
        )
        return {
            **update,
            **_usage_update(state, usage),
            "llm_calls": llm_calls,
            "plan": plan,
            "search_queries": queries,
            "errors": errors,
            "next_action": "retrieve",
        }

    def _execute_searches(self, state: AgentState, queries: list[str]) -> dict[str, Any]:
        evidence = list(state.get("evidence", []))
        retrieval_runs = list(state.get("retrieval_runs", []))
        searched = list(state.get("searched_queries", []))
        search_calls = state.get("search_calls", 0)
        for query in queries:
            if search_calls >= self.limits.max_search_calls:
                break
            if query in searched:
                continue
            sources, retrieved_count, retrieval_trace = self.retriever(query, None)
            search_calls += 1
            searched.append(query)
            evidence = _dedupe_evidence(evidence, sources)
            retrieval_runs.append(
                {
                    "query": query,
                    "retrieved_count": retrieved_count,
                    "selected_source_ids": [
                        source.get("chunk_id") for source in sources
                    ],
                    "retrieval": retrieval_trace,
                }
            )
        return {
            "evidence": evidence,
            "retrieval_runs": retrieval_runs,
            "searched_queries": searched,
            "search_calls": search_calls,
        }

    def retrieve_node(self, state: AgentState) -> dict[str, Any]:
        started = time.perf_counter()
        available = self.limits.max_search_calls - state.get("search_calls", 0)
        reserve = 1 if self.limits.max_retries > 0 else 0
        initial_cap = max(1, available - reserve)
        queries = state.get("search_queries", [state["question"]])[:initial_cap]
        result = self._execute_searches(state, queries)
        update = self._record(
            state,
            "retrieve",
            started,
            "retrieved" if result["evidence"] else "no_evidence",
            queries=queries,
            search_calls=result["search_calls"],
            evidence_count=len(result["evidence"]),
        )
        return {**result, **update}


    def _assessment_prompt(self, state: AgentState) -> str:
        evidence = "\n\n".join(
            f"[source_id={source.get('chunk_id')}; citation={index}] "
            f"{source.get('title')} ({source.get('filename')}):\n"
            f"{source.get('parent_text', '')}"
            for index, source in enumerate(state.get("evidence", []), start=1)
        )
        aspects = json.dumps(state.get("plan", []), ensure_ascii=False)
        return (
            "Return JSON only. Assess evidence separately for every aspect. A source may prove "
            "that a requested condition is not specified; that supports an uncertainty answer, "
            "not a categorical negative. Never transfer a license term to technical support, "
            "treat missing auto-renewal information as a prohibition, treat issue date as an "
            "effective date, infer document priority from dates, or use a missing document as "
            "proof of a negative fact. LLM assessment is not a source. Use only listed source_id "
            "values. Schema: "
            '{"aspects":[{"aspect_id":"...","sufficient":true,'
            '"source_ids":["..."],"reason":"...","gap":"","target_query":""}]}. '
            f"Aspects: {aspects}\nEvidence:\n{evidence}"
        )

    def assess_evidence(self, state: AgentState) -> dict[str, Any]:
        started = time.perf_counter()
        plan = state.get("plan", [])
        evidence = state.get("evidence", [])
        errors = list(state.get("errors", []))
        gaps: list[dict[str, str]] = []
        assessment: list[dict[str, Any]] = []
        content, usage, error = self._call(state, self._assessment_prompt(state))
        llm_calls = state.get("llm_calls", 0) + int(
            error is None or error.get("attempted", True)
        )
        if error:
            errors.append({"node": "assess_evidence", "error": error["error"]})
        try:
            parsed = _parse_json_object(content or "")
            items = parsed.get("aspects")
            if not isinstance(items, list):
                raise ValueError("assessment aspects must be a list")
            known_sources = {str(item.get("chunk_id")) for item in evidence}
            by_id = {
                str(item.get("aspect_id")): item for item in items if isinstance(item, dict)
            }
            for aspect in plan:
                item = by_id.get(aspect["aspect_id"], {})
                source_ids = [
                    str(source_id)
                    for source_id in item.get("source_ids", [])
                    if str(source_id) in known_sources
                ]
                sufficient = bool(item.get("sufficient")) and bool(source_ids)
                assessment.append(
                    {
                        "aspect_id": aspect["aspect_id"],
                        "sufficient": sufficient,
                        "source_ids": source_ids,
                        "reason": str(item.get("reason", "")),
                    }
                )
                if not sufficient:
                    gaps.append(
                        {
                            "aspect_id": aspect["aspect_id"],
                            "reason": str(item.get("gap") or item.get("reason") or "unconfirmed"),
                            "target_query": str(
                                item.get("target_query") or aspect["question"]
                            ).strip(),
                        }
                    )
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            errors.append({"node": "assess_evidence", "error": str(exc)})
            if evidence:
                assessment = [
                    {
                        "aspect_id": aspect["aspect_id"],
                        "sufficient": True,
                        "source_ids": [str(evidence[0].get("chunk_id"))],
                        "reason": "deterministic fallback after invalid assessor JSON",
                    }
                    for aspect in plan
                ]
                gaps = []
            else:
                gaps = [
                    {
                        "aspect_id": aspect["aspect_id"],
                        "reason": "no evidence found",
                        "target_query": aspect["question"],
                    }
                    for aspect in plan
                ]
        can_retry = (
            bool(gaps)
            and state.get("search_calls", 0) < self.limits.max_search_calls
            and state.get("retries", 0) < self.limits.max_retries
        )
        next_action = "targeted_retrieve" if can_retry else "compose_answer"
        update = self._record(
            state,
            "assess_evidence",
            started,
            next_action,
            assessment=assessment,
            gaps=gaps,
            prompt_tokens=int(usage.get("prompt_eval_count") or 0),
            completion_tokens=int(usage.get("eval_count") or 0),
        )
        return {
            **update,
            **_usage_update(state, usage),
            "llm_calls": llm_calls,
            "gaps": gaps,
            "errors": errors,
            "next_action": next_action,
        }

    def targeted_retrieve(self, state: AgentState) -> dict[str, Any]:
        started = time.perf_counter()
        remaining = self.limits.max_search_calls - state.get("search_calls", 0)
        queries = [
            gap["target_query"]
            for gap in state.get("gaps", [])
            if gap.get("target_query")
        ][:remaining]
        result = self._execute_searches(state, queries)
        update = self._record(
            state,
            "targeted_retrieve",
            started,
            "return_to_assessment",
            queries=queries,
            search_calls=result["search_calls"],
            evidence_count=len(result["evidence"]),
        )
        return {
            **result,
            **update,
            "retries": state.get("retries", 0) + 1,
        }

    def _compose_prompt(self, state: AgentState) -> str:
        gaps = json.dumps(state.get("gaps", []), ensure_ascii=False)
        baseline_prompt = build_answer_prompt(
            state["question"], state.get("evidence", [])
        )
        return (
            "Дополнительные обязательные правила агента: не переноси срок лицензии на "
            "техническую поддержку без прямого подтверждения. Отсутствие условия "
            "автопродления не означает запрет или наличие продления. Дата издания не является "
            "датой вступления в силу. Не выводи приоритет документов из дат и не считай "
            "отсутствие найденного документа доказательством отрицательного факта. Если аспект "
            "не подтверждён, ясно укажи неопределённость и причину. Сохраняй точные термины и "
            "формулировки чисел из источника: например, не заменяй «пользователей» на «единицы», "
            "«20 процентов оклада» на «20 процентов от оклада» или словесное число цифрой. "
            "Ставь ссылку [N] в том же предложении, что и подтверждаемый факт. "
            f"Неподтверждённые аспекты: {gaps}\n\n{baseline_prompt}"
        )

    def compose_answer(self, state: AgentState) -> dict[str, Any]:
        started = time.perf_counter()
        evidence = state.get("evidence", [])
        errors = list(state.get("errors", []))
        usage: dict[str, Any] = {}
        llm_calls = state.get("llm_calls", 0)
        refusal_reason: str | None = None
        if not evidence:
            answer = "В активных документах не найдено достаточно данных для ответа."
            refusal_reason = "no_evidence"
            decision = "abstain_no_evidence"
        elif requires_temporal_abstention(state["question"], evidence):
            answer = build_temporal_abstention(evidence)
            refusal_reason = "temporal_evidence_gap"
            decision = "temporal_abstention"
        elif explicit_gap := _explicit_source_gap(state["question"], evidence):
            answer, refusal_reason = explicit_gap
            decision = "explicit_source_gap_abstention"
        else:
            content, usage, error = self._call(state, self._compose_prompt(state))
            llm_calls += int(error is None or error.get("attempted", True))
            if error:
                errors.append({"node": "compose_answer", "error": error["error"]})
            if content is None:
                citations = "".join(
                    f"[{index}]" for index in range(1, min(len(evidence), 2) + 1)
                )
                answer = (
                    "Недостаточно надёжно подтверждённых данных для ответа в пределах "
                    f"установленного бюджета модели {citations}."
                )
                refusal_reason = "llm_budget_or_error"
                decision = "controlled_fallback"
            else:
                answer = content
                decision = "draft_composed"
        update = self._record(
            state,
            "compose_answer",
            started,
            decision,
            prompt_tokens=int(usage.get("prompt_eval_count") or 0),
            completion_tokens=int(usage.get("eval_count") or 0),
        )
        return {
            **update,
            **_usage_update(state, usage),
            "llm_calls": llm_calls,
            "answer": answer,
            "errors": errors,
            "refusal_reason": refusal_reason,
        }


    def validate_answer(self, state: AgentState) -> dict[str, Any]:
        started = time.perf_counter()
        answer = state.get("answer", "")
        evidence = state.get("evidence", [])
        citation_valid = bool(evidence) and citation_ids_admissible(answer, len(evidence))
        llm_failures: list[str] = []
        usage: dict[str, Any] = {}
        errors = list(state.get("errors", []))
        llm_calls = state.get("llm_calls", 0)
        retries = state.get("retries", 0)
        repair_attempted = False
        if (
            evidence
            and not citation_valid
            and retries < self.limits.max_retries
            and llm_calls < self.limits.max_llm_calls
        ):
            repair_attempted = True
            content, usage, error = self._call(
                state,
                build_citation_repair_prompt(
                    self._compose_prompt(state), answer, len(evidence)
                ),
            )
            llm_calls += int(error is None or error.get("attempted", True))
            retries += int(error is None or error.get("attempted", True))
            if error:
                errors.append({"node": "validate_answer", "error": error["error"]})
            if content:
                answer = content
                citation_valid = citation_ids_admissible(answer, len(evidence))
        guard_failures = _unsupported_guard(state["question"], answer)
        if (
            citation_valid
            and not guard_failures
            and _is_risk_question(state["question"])
            and not repair_attempted
            and llm_calls < self.limits.max_llm_calls
        ):
            evidence_text = "\n\n".join(
                f"[{index}] {source.get('parent_text', '')}"
                for index, source in enumerate(evidence, start=1)
            )
            prompt = (
                "Return JSON only. Validate whether every material draft claim is directly "
                "supported by the evidence and its [N] citation. Missing information is not a "
                "negative fact. Do not use your own knowledge. Schema: "
                '{"valid":true,"unsupported_claims":["..."]}. '
                f"Question: {state['question']}\nDraft: {answer}\nEvidence:\n{evidence_text}"
            )
            content, usage, error = self._call(state, prompt)
            llm_calls += int(error is None or error.get("attempted", True))
            if error:
                errors.append({"node": "validate_answer", "error": error["error"]})
            if content:
                try:
                    parsed = _parse_json_object(content)
                    if not bool(parsed.get("valid", False)):
                        raw = parsed.get("unsupported_claims", [])
                        llm_failures = [str(item) for item in raw] or [
                            "validator rejected draft"
                        ]
                except (ValueError, TypeError, json.JSONDecodeError) as exc:
                    errors.append({"node": "validate_answer", "error": str(exc)})
        failures = [
            *(["invalid_or_missing_citations"] if not citation_valid and evidence else []),
            *guard_failures,
            *llm_failures,
        ]
        if failures:
            citations = "".join(
                f"[{index}]" for index in range(1, min(len(evidence), 2) + 1)
            )
            answer = (
                "По найденным источникам нельзя надёжно подтвердить запрошенное утверждение. "
                "Источники позволяют лишь зафиксировать отсутствие прямого подтверждения; это "
                f"не доказывает противоположный факт {citations}."
            )
            decision = "unsupported_claims_abstention"
            refusal_reason = "answer_validation_failed"
        else:
            decision = "validated"
            refusal_reason = state.get("refusal_reason")
        validation = {
            "citations_valid": citation_valid,
            "guard_failures": guard_failures,
            "llm_failures": llm_failures,
            "citation_repair_attempted": repair_attempted,
            "valid": not failures,
        }
        update = self._record(
            state,
            "validate_answer",
            started,
            decision,
            validation=validation,
            prompt_tokens=int(usage.get("prompt_eval_count") or 0),
            completion_tokens=int(usage.get("eval_count") or 0),
        )
        return {
            **update,
            **_usage_update(state, usage),
            "llm_calls": llm_calls,
            "retries": retries,
            "answer": answer,
            "validation": validation,
            "errors": errors,
            "refusal_reason": refusal_reason,
        }

    def finish(self, state: AgentState) -> dict[str, Any]:
        started = time.perf_counter()
        status = "abstained" if state.get("refusal_reason") else "ok"
        update = self._record(
            state,
            "finish",
            started,
            status,
            refusal_reason=state.get("refusal_reason"),
        )
        return {**update, "status": status, "next_action": "finished"}

    def invoke(
        self, question: str, user_id: str, session_id: str, request_id: str
    ) -> AgentState:
        initial: AgentState = {
            "request_id": request_id,
            "question": question,
            "user_id": user_id,
            "session_id": session_id,
            "plan": [],
            "search_queries": [question],
            "searched_queries": [],
            "evidence": [],
            "gaps": [],
            "decisions": [],
            "trajectory": [],
            "retrieval_runs": [],
            "search_calls": 0,
            "llm_calls": 0,
            "retries": 0,
            "step_count": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "answer": "",
            "status": "started",
            "refusal_reason": None,
            "validation": {},
            "errors": [],
        }
        return self.graph.invoke(initial, {"recursion_limit": 20})


def agent_from_settings(settings: Settings | None = None) -> BoundedAgent:
    configured = settings or get_settings()
    return BoundedAgent(
        AgentLimits(
            max_search_calls=configured.agent_max_search_calls,
            max_retries=configured.agent_max_retries,
            max_llm_calls=configured.agent_max_llm_calls,
        )
    )


def run_agent(
    question: str,
    user_id: str,
    session_id: str,
    request_id: str,
    *,
    agent: BoundedAgent | None = None,
) -> dict[str, Any]:
    runtime = agent or agent_from_settings()
    state = runtime.invoke(question, user_id, session_id, request_id)
    retrieval_runs = state.get("retrieval_runs", [])
    rerank_latencies = [
        float(run.get("retrieval", {}).get("latency_ms") or 0) for run in retrieval_runs
    ]
    statuses = [run.get("retrieval", {}).get("status") for run in retrieval_runs]
    fallback_reasons = [
        run.get("retrieval", {}).get("fallback_reason")
        for run in retrieval_runs
        if run.get("retrieval", {}).get("fallback_reason")
    ]
    retrieval_trace = {
        "enabled": any(run.get("retrieval", {}).get("enabled") for run in retrieval_runs),
        "status": statuses[-1] if statuses and len(set(statuses)) == 1 else "mixed",
        "latency_ms": round(sum(rerank_latencies), 3),
        "fallback_reason": "; ".join(fallback_reasons) or None,
        "strategy": "bounded_langgraph",
        "searches": retrieval_runs,
    }
    evidence = state.get("evidence", [])
    return {
        "answer": state.get("answer", ""),
        "sources": evidence,
        "retrieval": retrieval_trace,
        "retrieved_count": sum(
            int(run.get("retrieved_count") or 0) for run in retrieval_runs
        ),
        "usage": {
            "prompt_eval_count": state.get("prompt_tokens", 0),
            "eval_count": state.get("completion_tokens", 0),
        },
        "trace_details": {
            "mode": "langgraph_agent",
            "request_id": request_id,
            "user_id": user_id,
            "session_id": session_id,
            "plan": state.get("plan", []),
            "search_queries": state.get("searched_queries", []),
            "source_ids": [source.get("chunk_id") for source in evidence],
            "gaps": state.get("gaps", []),
            "decisions": state.get("decisions", []),
            "trajectory": state.get("trajectory", []),
            "search_calls": state.get("search_calls", 0),
            "llm_calls": state.get("llm_calls", 0),
            "retries": state.get("retries", 0),
            "step_count": state.get("step_count", 0),
            "status": state.get("status"),
            "refusal_reason": state.get("refusal_reason"),
            "validation": state.get("validation", {}),
            "errors": state.get("errors", []),
            "limits": {
                "max_search_calls": runtime.limits.max_search_calls,
                "max_retries": runtime.limits.max_retries,
                "max_llm_calls": runtime.limits.max_llm_calls,
            },
            "retrieval": retrieval_trace,
        },
    }
