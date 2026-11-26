from io import BytesIO
from pathlib import Path

from docx import Document as DocxDocument
from docx.table import Table
from docx.text.paragraph import Paragraph
from pypdf import PdfReader


def extract_text(filename: str, data: bytes) -> str:
    suffix = Path(filename).suffix.lower()
    if suffix in {".txt", ".md"}:
        return data.decode("utf-8-sig")
    if suffix == ".pdf":
        return "\n\n".join((page.extract_text() or "") for page in PdfReader(BytesIO(data)).pages)
    if suffix == ".docx":
        doc = DocxDocument(BytesIO(data))
        blocks: list[str] = []
        for item in doc.iter_inner_content():
            if isinstance(item, Paragraph):
                text = item.text.strip()
                if text:
                    blocks.append(text)
                continue
            if isinstance(item, Table):
                for row in item.rows:
                    values: list[str] = []
                    seen_cells: set[int] = set()
                    for cell in row.cells:
                        cell_identity = id(cell._tc)
                        if cell_identity in seen_cells:
                            continue
                        seen_cells.add(cell_identity)
                        value = " ".join(
                            paragraph.text.strip()
                            for paragraph in cell.paragraphs
                            if paragraph.text.strip()
                        )
                        values.append(value)
                    if any(values):
                        blocks.append(" | ".join(values))
        return "\n\n".join(blocks)
    raise ValueError("Поддерживаются только TXT, MD, PDF и DOCX")
