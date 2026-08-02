from app.ingest.chunker import chunk_text, count_tokens


def test_blank_text_yields_no_chunks():
    assert chunk_text("", 500, 80) == []
    assert chunk_text("   \n  ", 500, 80) == []


def test_short_text_is_single_chunk():
    text = "connection pool leak in the payment service"
    assert chunk_text(text, 500, 80) == [text]


def test_long_text_splits_with_overlap():
    # ~1200 tokens of distinct words so windows don't collapse.
    text = " ".join(f"word{i}" for i in range(1200))
    chunks = chunk_text(text, 500, 80)

    assert len(chunks) > 1
    # Each chunk stays within the size budget.
    assert all(count_tokens(c) <= 500 for c in chunks)
    # Adjacent chunks overlap: the tail of chunk 0 reappears at the head of chunk 1.
    tail = chunks[0].split()[-1]
    assert tail in chunks[1].split()


def test_overlap_must_be_smaller_than_size():
    import pytest

    with pytest.raises(ValueError):
        chunk_text("anything", 100, 100)
