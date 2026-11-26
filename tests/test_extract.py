from io import BytesIO

from docx import Document

from clip_agent.extract import extract_text


def test_docx_extraction_preserves_paragraph_and_table_order():
    document = Document()
    document.add_paragraph("До таблицы")
    table = document.add_table(rows=2, cols=2)
    table.cell(0, 0).text = "Обязательно"
    table.cell(0, 1).text = "При наличии"
    table.cell(1, 0).text = "Итоговый расчёт"
    table.cell(1, 1).text = "Матрица ответственности"
    document.add_paragraph("После таблицы")
    payload = BytesIO()
    document.save(payload)

    extracted = extract_text("regulation.docx", payload.getvalue())

    assert extracted == (
        "До таблицы\n\n"
        "Обязательно | При наличии\n\n"
        "Итоговый расчёт | Матрица ответственности\n\n"
        "После таблицы"
    )


def test_docx_extraction_avoids_duplicate_merged_cells():
    document = Document()
    table = document.add_table(rows=1, cols=2)
    table.cell(0, 0).merge(table.cell(0, 1)).text = "Общее требование"
    payload = BytesIO()
    document.save(payload)

    extracted = extract_text("regulation.docx", payload.getvalue())

    assert extracted == "Общее требование"
