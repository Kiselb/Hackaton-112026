from io import BytesIO
from pathlib import Path

from docx import Document as DocxDocument
from pypdf import PdfReader


def extract_text(filename: str, data: bytes) -> str:
    suffix = Path(filename).suffix.lower()
    if suffix in {".txt", ".md"}:
        return data.decode("utf-8-sig")
    if suffix == ".pdf":
        return "\n\n".join((page.extract_text() or "") for page in PdfReader(BytesIO(data)).pages)
    if suffix == ".docx":
        doc = DocxDocument(BytesIO(data))
        return "\n\n".join(p.text for p in doc.paragraphs if p.text.strip())
    raise ValueError("Поддерживаются только TXT, MD, PDF и DOCX")
