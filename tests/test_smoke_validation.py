from scripts.smoke_test import check_answer


SOURCES = [
    {"filename": "order_101.txt"},
    {"filename": "order_102.txt"},
]


def test_answer_validation_requires_rates_sources_and_uncertainty():
    answer = (
        "Приказ № 101 устанавливает 0,10 процента за день [1]. "
        "Приказ № 102 устанавливает 0,05 процента за день [2]. "
        "Приоритет не определён, поэтому нельзя выбрать одну ставку."
    )
    checks = check_answer(answer, SOURCES, {"citations_valid": True})
    assert all(check["ok"] for check in checks.values())


def test_answer_validation_rejects_invented_precedence():
    answer = (
        "Приказ № 101 устанавливает 0,10 процента [1]. "
        "Приказ № 102 устанавливает 0,05 процента [2]. "
        "Приказ № 102 имеет приоритет."
    )
    checks = check_answer(answer, SOURCES, {"citations_valid": True})
    assert checks["precedence_is_uncertain"]["ok"] is False


def test_answer_validation_accepts_plural_no_precedence_wording():
    answer = (
        "Ставка 0,10 процента указана в приказе [1]. "
        "Ставка 0,05 процента указана в приказе [2]. "
        "Документы не содержат прямой ссылки на отмену или определения приоритета."
    )
    checks = check_answer(answer, SOURCES, {"citations_valid": True})
    assert all(check["ok"] for check in checks.values())
