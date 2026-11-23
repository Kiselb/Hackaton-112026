from __future__ import annotations

import re
from typing import Any


CITATION_PATTERN = re.compile(r"\[(\d+)\]")
TEMPORAL_STATUS_PATTERN = re.compile(
    r"\bдействовал(?:а|о|и)?\b|\bдействующ\w*\s+(?:на|по состоянию)", re.IGNORECASE
)
EFFECTIVE_PERIOD_PATTERN = re.compile(
    r"вступа\w+\s+в\s+силу|действу\w+\s+(?:с|до)\s+\d|"
    r"утрачива\w+\s+силу|effective_(?:from|to)",
    re.IGNORECASE,
)


def requires_temporal_abstention(question: str, sources: list[dict[str, Any]]) -> bool:
    if not TEMPORAL_STATUS_PATTERN.search(question):
        return False
    evidence = "\n".join(str(source.get("parent_text", "")) for source in sources)
    return not EFFECTIVE_PERIOD_PATTERN.search(evidence)


def build_temporal_abstention(sources: list[dict[str, Any]]) -> str:
    citations = "".join(f"[{number}]" for number in range(1, min(len(sources), 2) + 1))
    return (
        "По предоставленным источникам невозможно определить, какой приказ действовал на "
        "указанную дату и какая ставка применялась ко всем сделкам. Документы сообщают даты "
        "издания и области применения, но не содержат прямых сведений о вступлении норм в "
        f"силу или периодах их действия {citations}. Дата издания сама по себе не доказывает "
        "действие нормы на конкретную дату, а разные области применения не подтверждают "
        f"универсальную ставку для всех сделок {citations}."
    )


def build_answer_prompt(question: str, sources: list[dict[str, Any]]) -> str:
    evidence = "\n\n".join(
        f"[{number}] {source['title']} ({source['filename']}):\n{source['parent_text']}"
        for number, source in enumerate(sources, start=1)
    )
    last_citation = len(sources)
    return (
        "Ты корпоративный поисковый ассистент. Отвечай только по источникам ниже. "
        "Каждое существенное утверждение сопровождай ссылкой [N]. "
        f"Допустимы только ссылки [1]...[{last_citation}], соответствующие списку источников; "
        "номер пункта или приказа не является номером ссылки. "
        "Если источники противоречат друг другу, явно назови обе позиции и не выдумывай "
        "приоритет. Учитывай область применения каждой нормы и не распространяй её на все "
        "сделки без прямого подтверждения. Не считай более поздний документ автоматически "
        "отменяющим более ранний. Для вопроса о состоянии на определённую дату применяй "
        "обязательное правило: дата издания документа не доказывает дату вступления нормы в "
        "силу или период её действия. Если источники не содержат прямого указания «вступает в "
        "силу», «действует с/до» либо эквивалентных временных сведений, ответь, что невозможно "
        "определить действовавший документ и применимую ставку. В таком случае не выбирай "
        "приказ или ставку на запрошенную дату и не делай вывод о том, что документ уже или "
        "ещё не действовал. Ответ на русском языке.\n\n"
        f"Вопрос: {question}\n\nИсточники:\n{evidence}"
    )


def citation_ids_admissible(answer: str, source_count: int) -> bool:
    citations = [int(value) for value in CITATION_PATTERN.findall(answer)]
    return bool(citations) and all(1 <= value <= source_count for value in citations)


def build_citation_repair_prompt(original_prompt: str, draft: str, source_count: int) -> str:
    invalid = sorted(
        {
            int(value)
            for value in CITATION_PATTERN.findall(draft)
            if not 1 <= int(value) <= source_count
        }
    )
    return (
        f"{original_prompt}\n\n"
        "Ниже приведён черновик ответа с недопустимыми номерами ссылок. Перепиши весь ответ, "
        f"используя только ссылки [1]...[{source_count}]. Недопустимые номера: {invalid}. "
        "Не путай номер пункта документа с номером источника. Сохрани только утверждения, "
        "которые подтверждаются перечисленными источниками, и не добавляй комментарий о "
        "процессе исправления.\n\n"
        f"Черновик:\n{draft}"
    )


def merge_ollama_usage(primary: dict[str, Any], repair: dict[str, Any]) -> dict[str, Any]:
    merged = {**primary, **repair}
    for name in (
        "prompt_eval_count",
        "eval_count",
        "total_duration",
        "load_duration",
        "prompt_eval_duration",
        "eval_duration",
    ):
        values = [payload.get(name) for payload in (primary, repair)]
        numeric = [value for value in values if isinstance(value, int | float)]
        if numeric:
            merged[name] = sum(numeric)
    return merged
