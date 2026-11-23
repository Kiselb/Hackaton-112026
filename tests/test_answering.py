from clip_agent.answering import (
    build_answer_prompt,
    build_citation_repair_prompt,
    build_temporal_abstention,
    citation_ids_admissible,
    merge_ollama_usage,
    requires_temporal_abstention,
)


def sources() -> list[dict[str, str]]:
    return [
        {
            "title": "Приказ № 102 от 15 февраля 2026 года",
            "filename": "order_102.txt",
            "parent_text": "5. Приказ относится к сделкам направления Альфа.",
        },
        {
            "title": "Приказ № 101 от 10 января 2026 года",
            "filename": "order_101.txt",
            "parent_text": "4. Приказ применяется к договорам отдела продаж.",
        },
    ]


def test_prompt_requires_uncertainty_without_effective_period():
    prompt = build_answer_prompt("Какой приказ действовал 1 февраля?", sources())

    assert "дата издания документа не доказывает" in prompt
    assert "невозможно определить действовавший документ" in prompt
    assert "не выбирай приказ или ставку" in prompt
    assert "не распространяй её на все сделки" in prompt
    assert "Не считай более поздний документ автоматически отменяющим" in prompt


def test_prompt_and_repair_restrict_citations_to_actual_source_labels():
    prompt = build_answer_prompt("К каким сделкам относится приказ № 102?", sources())
    repair = build_citation_repair_prompt(prompt, "Пункт 5 подтверждает ответ [5].", 2)

    assert "Допустимы только ссылки [1]...[2]" in prompt
    assert "номер пункта или приказа не является номером ссылки" in prompt
    assert "Недопустимые номера: [5]" in repair
    assert "используя только ссылки [1]...[2]" in repair


def test_citation_validation_and_usage_merge_cover_repair_pass():
    assert citation_ids_admissible("Факт [1][2].", 2) is True
    assert citation_ids_admissible("Факт [5], исправление [1].", 2) is False
    assert citation_ids_admissible("Факт без ссылки.", 2) is False
    merged = merge_ollama_usage(
        {"prompt_eval_count": 10, "eval_count": 3, "done_reason": "stop"},
        {"prompt_eval_count": 20, "eval_count": 4, "done_reason": "stop"},
    )
    assert merged["prompt_eval_count"] == 30
    assert merged["eval_count"] == 7


def test_temporal_gate_abstains_only_without_effective_period_evidence():
    question = "Какой приказ действовал 1 февраля 2026 года?"
    assert requires_temporal_abstention(question, sources()) is True
    assert requires_temporal_abstention("Когда датирован приказ № 101?", sources()) is False
    effective_sources = sources()
    effective_sources[0]["parent_text"] += " Приказ вступает в силу 15 февраля 2026 года."
    assert requires_temporal_abstention(question, effective_sources) is False
    answer = build_temporal_abstention(sources())
    assert "невозможно определить" in answer
    assert "Дата издания сама по себе не доказывает" in answer
    assert "для всех сделок [1][2]" in answer
