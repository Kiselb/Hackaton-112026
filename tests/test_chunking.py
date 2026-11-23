from clip_agent.chunking import chunk_text


def test_offsets_match_child_text():
    chunks = chunk_text("Первый абзац.\n\nВторой абзац.", parent_size=100, child_size=12, overlap=3)
    assert chunks
    for chunk in chunks:
        assert chunk.parent_text[chunk.child_start : chunk.child_end] == chunk.child_text


def test_empty_text_has_no_chunks():
    assert chunk_text(" \n\n ") == []
