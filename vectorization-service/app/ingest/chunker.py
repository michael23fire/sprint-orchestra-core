"""Token-based text chunking with overlap.

A single embedding vector is a semantic centroid of its input; if a chunk spans several unrelated
topics the vector blurs and retrieval quality drops. So we split long text into ~``chunk_size``-token
windows and keep a small ``overlap`` between adjacent windows, so a key sentence that lands on a
chunk boundary still appears whole in at least one chunk.

Short text (a typical issue title+description or a one-line comment) yields a single chunk and is
left untouched — no point paying for a splitter when the whole thing fits in one window.
"""
from __future__ import annotations

from typing import List

import tiktoken

# cl100k_base is a reasonable, provider-agnostic tokenizer for *sizing* chunks. It does not need to
# match the embedding model's exact tokenizer — we only use it to keep chunks in a sane token range.
_ENCODING = tiktoken.get_encoding("cl100k_base")


def count_tokens(text: str) -> int:
    return len(_ENCODING.encode(text))


def chunk_text(text: str, chunk_size: int, overlap: int) -> List[str]:
    """Split ``text`` into overlapping token windows.

    Returns ``[]`` for blank input and ``[text]`` when it already fits in one window.
    """
    if not text or not text.strip():
        return []
    if overlap >= chunk_size:
        raise ValueError("overlap must be smaller than chunk_size")

    tokens = _ENCODING.encode(text)
    if len(tokens) <= chunk_size:
        return [text.strip()]

    step = chunk_size - overlap
    chunks: List[str] = []
    for start in range(0, len(tokens), step):
        window = tokens[start : start + chunk_size]
        if not window:
            break
        piece = _ENCODING.decode(window).strip()
        if piece:
            chunks.append(piece)
        if start + chunk_size >= len(tokens):
            break
    return chunks
