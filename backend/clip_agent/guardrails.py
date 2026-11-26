from __future__ import annotations

import re
from dataclasses import dataclass


OUT_OF_SCOPE_MESSAGE = (
    "Я могу помогать с вопросами по корпоративным регламентам и документам базы знаний. "
    "Пожалуйста, задайте вопрос по этим материалам."
)
INVALID_INPUT_MESSAGE = (
    "Не удалось распознать осмысленный вопрос на русском языке. Пожалуйста, "
    "сформулируйте вопрос по корпоративным регламентам и документам базы знаний."
)
INJECTION_MESSAGE = (
    "Запрос содержит инструкции, которые пытаются изменить правила работы AI-Агента. "
    "Пожалуйста, сформулируйте только вопрос по корпоративным регламентам."
)


@dataclass(frozen=True)
class InputAssessment:
    allowed: bool
    outcome: str | None = None
    reason_code: str | None = None
    message: str | None = None


@dataclass(frozen=True)
class ClarificationAssessment:
    allowed: bool
    reason_code: str | None = None
    message: str | None = None
    user_does_not_know: bool = False
    likely_new_question: bool = False


_ROLE_OVERRIDE = re.compile(
    r"\b(?:игнорир\w*|забуд\w*|отмен\w*|обойди\w*|не\s+следуй)\b.{0,120}"
    r"\b(?:инструкц\w*|правил\w*|ограничен\w*|провер\w*|документ\w*)\b|"
    r"\b(?:представь|считай|действуй|работай)\b.{0,80}"
    r"\b(?:системн\w*|администратор\w*|разработчик\w*|роль\w*)\b|"
    r"\b(?:покажи|выведи|раскрой)\b.{0,60}\b(?:system\s*prompt|системн\w*\s+промпт\w*)\b|"
    r"\b(?:ответь|отвечай|используй)\b.{0,80}"
    r"\b(?:без\s+проверки|вместо\s+регламент\w*|свои\s+знания)\b|"
    r"\bсчитай\b.{0,80}\bсистемн\w*\s+сообщени\w*\b",
    re.IGNORECASE | re.DOTALL,
)
_DISCUSSION_PREFIX = re.compile(
    r"^\s*(?:как\s+(?:распознать|обнаружить|цитировать|обработать)|"
    r"что\s+означает|почему\s+фраза|является\s+ли\s+фраза)",
    re.IGNORECASE,
)
_OUT_OF_SCOPE = (
    re.compile(
        r"\b(?:напиши|создай|сгенерируй|разработай)\b.{0,80}"
        r"\b(?:код|программ\w*|скрипт\w*)\b",
        re.IGNORECASE,
    ),
    re.compile(r"\b(?:сочини|напиши)\b.{0,50}\b(?:стих|рассказ|песн)\w*", re.IGNORECASE),
    re.compile(r"\b(?:погод\w*|прогноз\s+погоды)\b", re.IGNORECASE),
    re.compile(r"\b(?:последн\w*|свеж\w*)\s+новост\w*\b", re.IGNORECASE),
)
_DOCUMENT_INJECTION = re.compile(
    r"\b(?:игнорир\w*|забуд\w*|обойди\w*|не\s+следуй)\b.{0,120}"
    r"\b(?:инструкц\w*|правил\w*|ограничен\w*)\b|"
    r"\b(?:системн\w*\s+(?:prompt|промпт|сообщени\w*)|роль\s+ассистент\w*)\b|"
    r"\b(?:вызови|запусти|выполни|отправь)\b.{0,80}"
    r"\b(?:инструмент\w*|команд\w*|email|e-mail|http|powershell|python)\b",
    re.IGNORECASE | re.DOTALL,
)
_INTENT_CUE = re.compile(
    r"\b(?:кто|что|где|когда|как|какой|какая|какие|сколько|почему|можно|нужно|должен|"
    r"обязан|назнача\w*|соглас\w*|утвержд\w*|примен\w*|установ\w*|срок\w*|поряд\w*|"
    r"процедур\w*|правил\w*|регламент\w*|документ\w*|политик\w*|отдел\w*|"
    r"подразделени\w*|проект\w*|договор\w*|лицензи\w*|поддерж\w*|"
    r"SS|АМ|РМ|РП|ПА|CRM|SLA|RFI|SharePoint|Wiki)\b",
    re.IGNORECASE,
)


def assess_input(question: str) -> InputAssessment:
    normalized = " ".join(question.split())
    if not normalized or not re.search(r"[A-Za-zА-Яа-яЁё0-9]", normalized):
        return InputAssessment(False, "invalid_input", "empty_or_punctuation", INVALID_INPUT_MESSAGE)

    letters = re.findall(r"[A-Za-zА-Яа-яЁё]", normalized)
    cyrillic = re.findall(r"[А-Яа-яЁё]", normalized)
    if not cyrillic or (letters and len(cyrillic) / len(letters) < 0.25):
        return InputAssessment(False, "invalid_input", "unsupported_language", INVALID_INPUT_MESSAGE)

    compact = re.sub(r"\s+", "", normalized.lower())
    if re.fullmatch(r"(.)\1{4,}", compact) or re.search(r"[^\w\s]{5,}", normalized):
        return InputAssessment(False, "invalid_input", "meaningless_input", INVALID_INPUT_MESSAGE)

    if _ROLE_OVERRIDE.search(normalized) and not _DISCUSSION_PREFIX.search(normalized):
        return InputAssessment(False, "invalid_input", "prompt_injection_detected", INJECTION_MESSAGE)

    if any(pattern.search(normalized) for pattern in _OUT_OF_SCOPE):
        corporate_context = re.search(
            r"\b(?:регламент\w*|документ\w*|процедур\w*|поряд\w*|политик\w*|баз\w*\s+знани\w*)\b",
            normalized,
            re.IGNORECASE,
        )
        if not corporate_context:
            return InputAssessment(False, "out_of_scope", "outside_corporate_documents", OUT_OF_SCOPE_MESSAGE)

    words = re.findall(r"[A-Za-zА-Яа-яЁё0-9]+", normalized)
    if len(words) >= 3 and "?" not in normalized and not _INTENT_CUE.search(normalized):
        return InputAssessment(False, "invalid_input", "unrecognized_intent", INVALID_INPUT_MESSAGE)

    return InputAssessment(True)


_DOES_NOT_KNOW = re.compile(
    r"^\s*(?:не\s+знаю|не\s+помню|не\s+могу\s+(?:сказать|уточнить|определить)|"
    r"мне\s+неизвестно|нет\s+информации)(?:[,;:.!?\s].*)?$",
    re.IGNORECASE,
)


def assess_clarification_answer(
    answer: str,
    *,
    original_question: str,
    clarification_question: str,
) -> ClarificationAssessment:
    """Validate a bounded clarification as context data, not as a standalone question."""
    del original_question, clarification_question
    normalized = " ".join(answer.split())
    if not normalized or not re.search(r"[A-Za-zА-Яа-яЁё0-9]", normalized):
        return ClarificationAssessment(
            False,
            "empty_clarification",
            "Введите ответ на уточняющий вопрос или завершите диалог.",
        )
    letters = re.findall(r"[A-Za-zА-Яа-яЁё]", normalized)
    cyrillic = re.findall(r"[А-Яа-яЁё]", normalized)
    if letters and (not cyrillic or len(cyrillic) / len(letters) < 0.25):
        return ClarificationAssessment(
            False,
            "unsupported_language",
            INVALID_INPUT_MESSAGE,
        )
    compact = re.sub(r"\s+", "", normalized.lower())
    if re.fullmatch(r"(.)\1{4,}", compact) or re.search(r"[^\w\s]{5,}", normalized):
        return ClarificationAssessment(False, "meaningless_input", INVALID_INPUT_MESSAGE)
    if _ROLE_OVERRIDE.search(normalized) and not _DISCUSSION_PREFIX.search(normalized):
        return ClarificationAssessment(
            False,
            "prompt_injection_detected",
            INJECTION_MESSAGE,
        )
    user_does_not_know = bool(_DOES_NOT_KNOW.match(normalized))
    likely_new_question = (
        normalized.endswith("?")
        and len(re.findall(r"[A-Za-zА-Яа-яЁё0-9]+", normalized)) >= 3
        and bool(_INTENT_CUE.search(normalized))
    )
    return ClarificationAssessment(
        True,
        user_does_not_know=user_does_not_know,
        likely_new_question=likely_new_question,
    )


def sanitize_document_text(text: str) -> tuple[str, list[str]]:
    """Remove instruction-like source sentences from model context, preserving stored evidence."""
    flags: list[str] = []
    safe_parts: list[str] = []
    for part in re.split(r"(?<=[.!?])\s+|\r?\n", text):
        if _DOCUMENT_INJECTION.search(part):
            flags.append("instruction_like_text_removed")
            safe_parts.append("[Недоверенная инструкция из документа исключена из контекста модели.]")
        elif part.strip():
            safe_parts.append(part.strip())
    return " ".join(safe_parts), sorted(set(flags))


def prompt_evidence_text(source: dict[str, object]) -> str:
    sanitized, _ = sanitize_document_text(str(source.get("parent_text", "")))
    return sanitized
