"""Regression tests for the CSV/txt and image-OCR bypass fixes (app/ingest/docling_parser.py).

Found live against a real 132-file attachment corpus: Docling 2.14 has no CSV/plain-text
InputFormat at all and raises ConversionError for them — which the broad except-and-log in
parse_attachment silently turned into empty text for every CSV/txt attachment. These tests lock in
that CSV/txt now bypass Docling entirely rather than round-tripping through (and failing) it, and
that standalone images now go through direct EasyOCR instead of Docling's document/layout pipeline
(which was found, live, to classify real screenshot content as an un-OCR'd "Picture" region).
"""
from io import BytesIO
from unittest.mock import patch

from app.config import Settings
from app.ingest.docling_parser import _ocr_reader, parse_attachment


def _fake_png_bytes() -> bytes:
    from PIL import Image

    img = Image.new("RGB", (10, 10), color="white")
    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


class _FakeOcrReader:
    def __init__(self, detections):
        self._detections = detections

    def readtext(self, image):
        return self._detections


async def test_csv_bypasses_docling_and_returns_raw_text():
    settings = Settings()
    data = b"issue,status\nATC-1,done\nATC-2,in_progress\n"
    result = await parse_attachment(data, "work-plan.csv", settings)
    assert result == "issue,status\nATC-1,done\nATC-2,in_progress\n"


async def test_txt_bypasses_docling_and_returns_raw_text():
    settings = Settings()
    result = await parse_attachment(b"plain notes here", "notes.txt", settings)
    assert result == "plain notes here"


async def test_csv_filename_is_case_insensitive():
    settings = Settings()
    result = await parse_attachment(b"a,b\n1,2\n", "DATA.CSV", settings)
    assert result == "a,b\n1,2\n"


async def test_invalid_utf8_in_plain_text_does_not_raise():
    settings = Settings()
    result = await parse_attachment(b"\xff\xfe not valid utf-8", "bad.txt", settings)
    assert isinstance(result, str)  # errors="replace" -> some decodable string, not an exception


async def test_log_extension_bypasses_docling_too():
    settings = Settings()
    result = await parse_attachment(b"2026-07-14T09:00Z checkout retry x3\n", "duplicate-checkout.log", settings)
    assert result == "2026-07-14T09:00Z checkout retry x3\n"


async def test_docling_rejecting_a_format_logs_and_returns_empty_without_crashing():
    """Regression test for a real bug found live: the except-branch's own logging call used to raise
    KeyError("Attempt to overwrite 'filename' in LogRecord") — `filename` collides with a reserved
    stdlib LogRecord attribute — which crashed the exception handler itself instead of the one bad
    attachment it was trying to skip past. This exercises that exact code path end to end (a
    genuinely Docling-unsupported extension, not one of the plain-text bypass ones) to prove the fix.
    """
    settings = Settings()
    result = await parse_attachment(b"whatever bytes", "mystery.xyz123", settings)
    assert result == ""


async def test_image_ocr_extracts_text_via_direct_easyocr_bypassing_docling():
    _ocr_reader.cache_clear()
    settings = Settings(docling_do_ocr=True)
    # bbox corners: top-left, top-right, bottom-right, bottom-left (EasyOCR's convention)
    detections = [
        ([[0, 0], [50, 0], [50, 10], [0, 10]], "HELLO", 0.99),
        ([[0, 20], [50, 20], [50, 30], [0, 30]], "WORLD", 0.95),
    ]
    with patch("easyocr.Reader", return_value=_FakeOcrReader(detections)):
        result = await parse_attachment(_fake_png_bytes(), "shot.png", settings)
    _ocr_reader.cache_clear()
    assert result == "HELLO WORLD"


async def test_image_ocr_sorts_into_reading_order_not_raw_detection_order():
    # Regression-relevant: real EasyOCR output ordering is not reading order (see module docstring's
    # "row_key" sort rationale) — detections here are deliberately shuffled (row 2 before row 1,
    # right-to-left within a row) to prove the sort, not just that concatenation happens.
    _ocr_reader.cache_clear()
    settings = Settings(docling_do_ocr=True)
    detections = [
        ([[60, 20], [100, 20], [100, 30], [60, 30]], "WORLD", 0.9),  # row 2, right
        ([[50, 0], [90, 0], [90, 10], [50, 10]], "THERE", 0.9),      # row 1, right
        ([[0, 20], [50, 20], [50, 30], [0, 30]], "HELLO", 0.9),      # row 2, left
        ([[0, 0], [40, 0], [40, 10], [0, 10]], "HI", 0.9),           # row 1, left
    ]
    with patch("easyocr.Reader", return_value=_FakeOcrReader(detections)):
        result = await parse_attachment(_fake_png_bytes(), "shot.png", settings)
    _ocr_reader.cache_clear()
    assert result == "HI THERE HELLO WORLD"


async def test_image_ocr_skipped_entirely_when_docling_do_ocr_disabled():
    # No text layer exists on a raw image without OCR, unlike a PDF that might have one — so this
    # correctly returns empty rather than attempting (and failing) some other extraction.
    settings = Settings(docling_do_ocr=False)
    result = await parse_attachment(_fake_png_bytes(), "shot.png", settings)
    assert result == ""


async def test_image_ocr_failure_returns_empty_without_crashing():
    _ocr_reader.cache_clear()
    settings = Settings(docling_do_ocr=True)
    with patch("easyocr.Reader", side_effect=RuntimeError("model load failed")):
        result = await parse_attachment(_fake_png_bytes(), "shot.png", settings)
    _ocr_reader.cache_clear()
    assert result == ""
