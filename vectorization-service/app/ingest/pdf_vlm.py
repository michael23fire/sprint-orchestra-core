"""Selective PDF-page rendering for the VLM attachment fallback.

Docling remains the primary parser for PDF because born-digital documents normally have better
reading order and lower cost that way.  This module only identifies pages likely to have lost
meaningful content (scans, screenshots, image tables, charts, or diagrams), renders those pages to
PNG, and lets the caller ask a VLM to extract them.

PyMuPDF is deliberately imported inside the functions: text-only ingestion must still start when
the optional PDF renderer is unavailable, and tests can replace these functions without installing
native PDF tooling.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PdfPageCandidate:
    """One PDF page selected for VLM extraction, numbered from one for user-facing citations."""

    page_number: int
    image_data: bytes
    reason: str


def render_pdf_candidates(
    data: bytes,
    *,
    min_text_chars: int,
    include_visual_pages: bool,
    max_pages: int,
    dpi: int,
) -> tuple[list[PdfPageCandidate], int]:
    """Render only likely-low-quality PDF pages as PNG.

    Native page text is a cheap, per-page signal which remains available even when Docling's
    document-level Markdown cannot be attributed back to a particular page.  A page with little
    native text is almost always a scan/screenshot.  Optionally, pages containing substantial
    raster/vector visual content are included too so chart/diagram facts do not disappear from RAG.
    The deterministic sort means a page cap spends calls on scan-like pages before visual-only ones.
    """
    if max_pages < 1:
        return [], 0
    if dpi < 72:
        raise ValueError("vlm_pdf_render_dpi must be at least 72")

    import fitz  # PyMuPDF

    document = fitz.open(stream=data, filetype="pdf")
    try:
        selected: list[tuple[int, str, object]] = []
        for zero_index, page in enumerate(document):
            text_chars = len(page.get_text("text").strip())
            raster_images = len(page.get_images(full=True))
            # Vector-heavy pages commonly contain charts or diagrams but no embedded raster image.
            vector_drawings = len(page.get_drawings())

            if text_chars < min_text_chars:
                selected.append((zero_index, "little_or_no_embedded_text", page))
            elif include_visual_pages and (raster_images > 0 or vector_drawings >= 20):
                selected.append((zero_index, "visual_content", page))

        # Scan-like pages have higher recall priority.  Preserve page order within a class, making
        # output stable across re-ingestion and cache-friendly.
        selected.sort(key=lambda item: (item[1] == "visual_content", item[0]))
        zoom = dpi / 72
        candidates = []
        for zero_index, reason, page in selected[:max_pages]:
            pixmap = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
            candidates.append(
                PdfPageCandidate(
                    page_number=zero_index + 1,
                    image_data=pixmap.tobytes("png"),
                    reason=reason,
                )
            )
        return candidates, len(document)
    finally:
        document.close()
