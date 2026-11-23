from dataclasses import dataclass


@dataclass(frozen=True)
class TextChunk:
    parent_text: str
    child_text: str
    child_start: int
    child_end: int
    page: int | None = None


def chunk_text(text: str, parent_size: int = 1700, child_size: int = 550, overlap: int = 100) -> list[TextChunk]:
    normalized = "\n\n".join(p.strip() for p in text.splitlines() if p.strip())
    if not normalized:
        return []
    parents: list[str] = []
    cursor = 0
    while cursor < len(normalized):
        end = min(cursor + parent_size, len(normalized))
        if end < len(normalized):
            boundary = normalized.rfind("\n\n", cursor, end)
            if boundary > cursor + parent_size // 2:
                end = boundary
        parents.append(normalized[cursor:end].strip())
        cursor = end
        while cursor < len(normalized) and normalized[cursor].isspace():
            cursor += 1
    result: list[TextChunk] = []
    for parent in parents:
        start = 0
        while start < len(parent):
            end = min(start + child_size, len(parent))
            result.append(TextChunk(parent, parent[start:end], start, end))
            if end == len(parent):
                break
            start = max(end - overlap, start + 1)
    return result
