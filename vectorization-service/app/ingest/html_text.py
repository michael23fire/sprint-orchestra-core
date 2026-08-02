"""HTML -> plain text.

Issue descriptions and comments are stored as rich-text HTML in jira-backend. Embedding raw HTML
wastes tokens on markup and dilutes the semantic signal, so strip tags to readable text before
chunking. Also drops inline-attachment ``<img>`` markers (their content is embedded separately as an
attachment chunk, so leaving the tag here would add noise).
"""
from __future__ import annotations

from bs4 import BeautifulSoup


def html_to_text(html: str | None) -> str:
    if not html:
        return ""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "img"]):
        tag.decompose()
    # separator=" " keeps words from adjacent block elements from being glued together.
    text = soup.get_text(separator=" ")
    # Collapse runs of whitespace the tag removal leaves behind.
    return " ".join(text.split())
