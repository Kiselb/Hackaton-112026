from __future__ import annotations

import json
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi import Response
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from clip_agent import main
from clip_agent.config import Settings
from clip_agent.conversations import CONVERSATION_TOKEN_HEADER
from clip_agent.db import Base, SearchTrace
from clip_agent.glossary import (
    GlossaryConfigError,
    GlossaryExpander,
    GlossaryTerm,
    get_glossary_expander,
    glossary_checksum,
    load_glossary,
)


def glossary_payload(terms: list[dict], *, version: str = "synthetic-v1") -> dict:
    payload = {
        "version": version,
        "source": "synthetic_test",
        "content_sha256": "0" * 64,
        "terms": terms,
    }
    payload["content_sha256"] = glossary_checksum(payload)
    return payload


def write_glossary(path: Path, terms: list[dict]) -> Path:
    path.write_text(
        json.dumps(glossary_payload(terms), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return path


@pytest.fixture
def glossary_path(tmp_path: Path) -> Path:
    return write_glossary(
        tmp_path / "glossary.json",
        [
            {"abbreviation": "ЗНП", "expansion": "Заявка на пресейл"},
            {
                "abbreviation": "ДИБ",
                "expansion": "Дирекция информационной безопасности",
            },
            {"abbreviation": "ДС", "expansion": "Дополнительное соглашение"},
            {"abbreviation": "ЗНИ", "expansion": "Запрос на изменение"},
            {"abbreviation": "SLA", "expansion": "Service Level Agreement"},
            {"abbreviation": "КС-2", "expansion": "Акт о завершении работ формы КС-2"},
            {"abbreviation": "КС-3", "expansion": "Акт о завершении работ формы КС-3"},
            {
                "abbreviation": "ПО",
                "expansion": "Программное обеспечение",
                "ambiguous": True,
                "context_terms": ["лиценз", "установ", "оборудован"],
                "case_sensitive": True,
            },
        ],
    )


def test_expands_single_and_multiple_terms_without_mutating_input(glossary_path: Path):
    expander = load_glossary(glossary_path)
    original = "Кто согласует ДС после ЗНИ?"

    result = expander.expand(original)

    assert original == "Кто согласует ДС после ЗНИ?"
    assert "Дополнительное соглашение (ДС)" in result.search_query
    assert "Запрос на изменение (ЗНИ)" in result.search_query
    assert result.found_count == result.applied_count == 2
    assert result.skipped_count == 0
    assert result.used and result.changed


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        ("Какой SLA действует?", "Service Level Agreement (SLA)"),
        ("какой sla действует?", "Service Level Agreement (SLA)"),
        ("Где оформить КС-2 и КС-3?", "Акт о завершении работ формы КС-2 (КС-2)"),
    ],
)
def test_latin_hyphenated_and_case_insensitive_matching(
    glossary_path: Path, question: str, expected: str
):
    result = load_glossary(glossary_path).expand(question)
    assert expected in result.search_query


def test_word_boundaries_do_not_replace_substrings(glossary_path: Path):
    result = load_glossary(glossary_path).expand("ПОСЛЕ согласования и SLAService")
    assert result.search_query == result.original_query
    assert result.found_count == 0


def test_lowercase_preposition_po_is_never_expanded(glossary_path: Path):
    result = load_glossary(glossary_path).expand("Кто отвечает по договору?")
    assert result.search_query == result.original_query
    assert result.found_count == 1
    assert result.skip_reasons == {"case_sensitive_term": 1}


def test_ambiguous_term_requires_declared_context(glossary_path: Path):
    expander = load_glossary(glossary_path)
    skipped = expander.expand("Кто согласует ПО?")
    applied = expander.expand("Кто согласует лицензию на ПО?")

    assert skipped.search_query == skipped.original_query
    assert skipped.skip_reasons == {"ambiguous_without_context": 1}
    assert "Программное обеспечение (ПО)" in applied.search_query


def test_repeated_and_already_expanded_terms_are_not_disclosed_twice(
    glossary_path: Path,
):
    expander = load_glossary(glossary_path)
    repeated = expander.expand("ДС согласуют до регистрации ДС")
    already = expander.expand("Дополнительное соглашение (ДС) согласует РП")

    assert repeated.search_query.count("Дополнительное соглашение") == 1
    assert repeated.skip_reasons == {"repeated_term": 1}
    assert already.search_query == already.original_query
    assert already.skip_reasons == {"already_expanded": 1}


def test_query_length_limit_keeps_input_unchanged(glossary_path: Path):
    query = "ЗНП " + "а" * 4000
    result = load_glossary(glossary_path).expand(query)
    assert result.search_query == query
    assert result.skip_reasons == {"query_too_long": 1}


@pytest.mark.parametrize("contents", ["", "{not-json"])
def test_empty_and_invalid_files_fail_closed(tmp_path: Path, contents: str):
    path = tmp_path / "invalid.json"
    path.write_text(contents, encoding="utf-8")
    with pytest.raises(GlossaryConfigError):
        load_glossary(path)


def test_checksum_mismatch_fails_closed(glossary_path: Path):
    payload = json.loads(glossary_path.read_text(encoding="utf-8"))
    payload["terms"][0]["expansion"] = "Подменённое значение"
    glossary_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(GlossaryConfigError, match="checksum"):
        load_glossary(glossary_path)


def test_conflicting_aliases_fail_the_whole_glossary(tmp_path: Path):
    path = write_glossary(
        tmp_path / "conflict.json",
        [
            {"abbreviation": "РП", "expansion": "Руководитель проекта"},
            {"abbreviation": "рп", "expansion": "Рабочий процесс"},
        ],
    )
    with pytest.raises(GlossaryConfigError) as error:
        load_glossary(path)
    assert error.value.code == "conflicting_alias"


def test_settings_keep_glossary_disabled_by_default():
    configured = Settings(_env_file=None)
    assert configured.glossary_enabled is False
    assert configured.glossary_path == Path("storage/glossary/customer_v1.json")
    assert configured.glossary_mode == "retrieval_only"


def agent_result(outcome: str = "answered") -> dict:
    sources = []
    if outcome == "answered":
        sources = [
            {
                "chunk_id": "real-document-chunk",
                "document_id": "document-1",
                "title": "Синтетический регламент",
                "filename": "rules.txt",
                "page": None,
                "parent_text": "Заявку утверждает директор.",
                "child_text": "утверждает директор",
                "child_start": 8,
                "child_end": 28,
                "source_type": "corporate_document",
            }
        ]
    return {
        "answer": (
            "Заявку утверждает директор [1]."
            if outcome == "answered"
            else "Уточните подразделение."
        ),
        "sources": sources,
        "retrieval": {
            "enabled": False,
            "status": "disabled",
            "latency_ms": 0.0,
            "fallback_reason": None,
        },
        "retrieved_count": len(sources),
        "usage": {"prompt_eval_count": 0, "eval_count": 0},
        "trace_details": {
            "mode": "langgraph_agent",
            "outcome": outcome,
            "reason_code": "grounded_answer" if outcome == "answered" else "ambiguous_scope",
            "validation": {"citations_valid": outcome == "answered"},
            "source_ids": [source["chunk_id"] for source in sources],
            "search_calls": 1,
            "llm_calls": 1,
            "step_count": 5,
            "retries": 0,
        },
    }


class FakeDb:
    def __init__(self) -> None:
        self.trace = None

    def add(self, value) -> None:
        self.trace = value
        if value.id is None:
            value.id = uuid4()

    def commit(self) -> None:
        pass


@pytest.mark.parametrize("multi_query_enabled", [False, True])
def test_agent_retrieval_receives_expanded_query_without_extra_search(
    monkeypatch: pytest.MonkeyPatch,
    glossary_path: Path,
    multi_query_enabled: bool,
):
    seen: list[tuple[str, str, bool]] = []

    def run(question: str, *args, **kwargs):
        seen.append(
            (question, kwargs["retrieval_query"], kwargs["multi_query_enabled"])
        )
        return agent_result()

    get_glossary_expander.cache_clear()
    monkeypatch.setattr(main.settings, "agent_enabled", True)
    monkeypatch.setattr(main.settings, "glossary_enabled", True)
    monkeypatch.setattr(main.settings, "glossary_path", glossary_path)
    monkeypatch.setattr(main.settings, "glossary_mode", "retrieval_only")
    monkeypatch.setattr(main, "run_agent", run)
    db = FakeDb()

    result = main.ask(
        main.AskRequest(
            question="Кто утверждает ЗНП?",
            multi_query_enabled=multi_query_enabled,
        ),
        db,
    )

    assert seen[0][0] == "Кто утверждает ЗНП?"
    assert "Заявка на пресейл (ЗНП)" in seen[0][1]
    assert seen[0][2] is multi_query_enabled
    assert result["metrics"]["agent_search_calls"] == 1
    assert result["metrics"]["glossary_used"] is True
    assert result["metrics"]["glossary_applied_expansions"] == 1
    assert result["metrics"]["glossary_additional_retrieval_calls"] == 0
    assert db.trace.question == "Кто утверждает ЗНП?"
    assert db.trace.details["glossary_used"] is True
    assert db.trace.details["resolved_query"] == seen[0][0]
    assert db.trace.details["retrieval_query"] == seen[0][1]
    assert db.trace.details["glossary_search_query"] == seen[0][1]
    assert db.trace.details["glossary_matches"] == [
        {
            "abbreviation": "ЗНП",
            "expansion": "Заявка на пресейл",
            "status": "applied",
        }
    ]
    assert all(source["source_type"] == "corporate_document" for source in result["sources"])
    assert all(source.get("source_type") != "glossary" for source in result["sources"])


def test_question_without_abbreviation_keeps_reasoning_and_retrieval_identical(
    monkeypatch: pytest.MonkeyPatch, glossary_path: Path
):
    seen: list[tuple[str, str]] = []

    def run(question: str, *args, **kwargs):
        seen.append((question, kwargs["retrieval_query"]))
        return agent_result()

    get_glossary_expander.cache_clear()
    monkeypatch.setattr(main.settings, "agent_enabled", True)
    monkeypatch.setattr(main.settings, "glossary_enabled", True)
    monkeypatch.setattr(main.settings, "glossary_path", glossary_path)
    monkeypatch.setattr(main.settings, "glossary_mode", "retrieval_only")
    monkeypatch.setattr(main, "run_agent", run)

    result = main.ask(main.AskRequest(question="Кто утверждает договор?"), FakeDb())

    assert seen == [("Кто утверждает договор?", "Кто утверждает договор?")]
    assert result["metrics"]["glossary_used"] is False
    assert result["metrics"]["agent_search_calls"] == 1


def test_legacy_mode_reproduces_expanded_agent_question(
    monkeypatch: pytest.MonkeyPatch, glossary_path: Path
):
    seen: list[tuple[str, str]] = []

    def run(question: str, *args, **kwargs):
        seen.append((question, kwargs["retrieval_query"]))
        return agent_result()

    get_glossary_expander.cache_clear()
    monkeypatch.setattr(main.settings, "agent_enabled", True)
    monkeypatch.setattr(main.settings, "glossary_enabled", True)
    monkeypatch.setattr(main.settings, "glossary_path", glossary_path)
    monkeypatch.setattr(main.settings, "glossary_mode", "legacy_agent_query")
    monkeypatch.setattr(main, "run_agent", run)

    main.ask(main.AskRequest(question="Кто утверждает ЗНП?"), FakeDb())

    assert seen[0][0] == seen[0][1]
    assert "Заявка на пресейл (ЗНП)" in seen[0][0]


def test_missing_enabled_glossary_stops_before_retrieval(monkeypatch, tmp_path: Path):
    get_glossary_expander.cache_clear()
    monkeypatch.setattr(main.settings, "agent_enabled", True)
    monkeypatch.setattr(main.settings, "glossary_enabled", True)
    monkeypatch.setattr(main.settings, "glossary_path", tmp_path / "missing.json")
    monkeypatch.setattr(
        main,
        "run_agent",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("agent called")),
    )
    db = FakeDb()

    result = main.ask(main.AskRequest(question="Кто утверждает ЗНП?"), db)

    assert result["outcome"] == "technical_error"
    assert result["reason_code"] == "glossary_configuration_error"
    assert db.trace.details["glossary_error_code"] == "file_unavailable"
    assert db.trace.details.get("search_calls", 0) == 0


def test_glossary_applies_to_resolved_clarification_query(
    monkeypatch: pytest.MonkeyPatch, glossary_path: Path
):
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    seen: list[tuple[str, str]] = []

    def run(question: str, *args, **kwargs):
        seen.append((question, kwargs["retrieval_query"]))
        return agent_result("clarification_required" if len(seen) == 1 else "answered")

    get_glossary_expander.cache_clear()
    monkeypatch.setattr(main.settings, "agent_enabled", True)
    monkeypatch.setattr(main.settings, "glossary_enabled", True)
    monkeypatch.setattr(main.settings, "glossary_path", glossary_path)
    monkeypatch.setattr(main.settings, "glossary_mode", "retrieval_only")
    monkeypatch.setattr(main, "run_agent", run)
    with Session(engine, expire_on_commit=False) as db:
        response = Response()
        first = main.ask(
            main.AskRequest(question="Кто утверждает ЗНП?", session_id="tab"),
            db,
            response=response,
        )
        second = main.ask(
            main.AskRequest(
                question="ДИБ",
                conversation_id=UUID(first["conversation_id"]),
                session_id="tab",
            ),
            db,
            conversation_token=response.headers[CONVERSATION_TOKEN_HEADER],
        )
        traces = list(db.scalars(select(SearchTrace).order_by(SearchTrace.created_at)))

    assert second["outcome"] == "answered"
    assert "Заявка на пресейл (ЗНП)" not in seen[1][0]
    assert "Дирекция информационной безопасности (ДИБ)" not in seen[1][0]
    assert "Заявка на пресейл (ЗНП)" in seen[1][1]
    assert "Дирекция информационной безопасности (ДИБ)" in seen[1][1]
    assert traces[1].details["original_question"] == "Кто утверждает ЗНП?"
    assert "Уточнённые пользователем условия" in traces[1].details["resolved_query"]
    assert "Заявка на пресейл (ЗНП)" not in traces[1].details["resolved_query"]


def test_term_model_rejects_invalid_abbreviation():
    with pytest.raises(ValueError):
        GlossaryTerm(abbreviation="BAD TERM", expansion="Invalid")


def test_expander_rejects_duplicate_synonym_even_without_file_loading():
    payload = glossary_payload(
        [
            {"abbreviation": "РП", "expansion": "Руководитель проекта"},
            {
                "abbreviation": "PM",
                "expansion": "Project Manager",
                "synonyms": ["РП"],
            },
        ]
    )
    from clip_agent.glossary import GlossaryDocument

    with pytest.raises(GlossaryConfigError):
        GlossaryExpander(GlossaryDocument.model_validate(payload))
