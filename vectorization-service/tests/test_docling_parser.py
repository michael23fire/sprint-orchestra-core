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
from app.ingest.docling_parser import _ocr_reader, _xlsx_used_ranges, parse_attachment, parse_attachment_sections


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


class _FakeVlmDescriber:
    def __init__(self, result=""):
        self._result = result
        self.calls = []

    async def describe(self, image_data, mime_type):
        self.calls.append((image_data, mime_type))
        if isinstance(self._result, Exception):
            raise self._result
        return self._result

    async def aclose(self):
        return None


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


async def test_image_ocr_drops_low_confidence_detections():
    # Found live on a synthetically degraded (rotated/blurred/low-contrast/re-compressed) test image:
    # garbled OCR fragments ("Mx", the mangled read of "SKU:"+"Pallet") scored 0.006-0.06, while real
    # words from the SAME image ("62", "SKU") scored 0.7-1.0 — a clean gap. Unfiltered, an agent
    # confidently presented "Mx" as a real SKU value in a live test; a faithfulness check can't catch
    # this class of error since the answer WAS faithful to what got retrieved.
    _ocr_reader.cache_clear()
    settings = Settings(docling_do_ocr=True, ocr_min_confidence=0.4)
    detections = [
        ([[0, 0], [50, 0], [50, 10], [0, 10]], "SKU", 0.72),
        ([[0, 20], [50, 20], [50, 30], [0, 30]], "Mx", 0.02),  # garbled — must be dropped
        ([[0, 40], [50, 40], [50, 50], [0, 50]], "62", 0.98),
    ]
    with patch("easyocr.Reader", return_value=_FakeOcrReader(detections)):
        result = await parse_attachment(_fake_png_bytes(), "shot.png", settings)
    _ocr_reader.cache_clear()
    assert result == "SKU 62"
    assert "Mx" not in result


async def test_image_ocr_skipped_entirely_when_docling_do_ocr_disabled():
    # No text layer exists on a raw image without OCR, unlike a PDF that might have one — so this
    # correctly returns empty rather than attempting (and failing) some other extraction.
    settings = Settings(docling_do_ocr=False)
    result = await parse_attachment(_fake_png_bytes(), "shot.png", settings)
    assert result == ""


async def test_vlm_escalation_is_skipped_entirely_when_disabled_even_if_ocr_dropped_detections():
    # Default settings: vlm_ocr_enabled=False. A dropped detection alone must not trigger a VLM call
    # unless the feature is explicitly turned on — this is a real per-image LLM cost.
    _ocr_reader.cache_clear()
    settings = Settings(docling_do_ocr=True, ocr_min_confidence=0.4)
    detections = [
        ([[0, 0], [50, 0], [50, 10], [0, 10]], "SKU", 0.72),
        ([[0, 20], [50, 20], [50, 30], [0, 30]], "Mx", 0.02),
    ]
    vlm = _FakeVlmDescriber(result="should never be used")
    with patch("easyocr.Reader", return_value=_FakeOcrReader(detections)):
        result = await parse_attachment(_fake_png_bytes(), "shot.png", settings, vlm)
    _ocr_reader.cache_clear()
    assert result == "SKU"
    assert vlm.calls == []


async def test_vlm_escalation_fires_and_replaces_the_result_when_ocr_dropped_a_detection():
    """The exact scenario from Case Study 27: a confidence-filtered OCR result still has a gap where
    the garbled detection got dropped. With VLM escalation on, the whole image is re-read and its
    (better) transcription replaces the partial OCR text rather than being appended to it.
    """
    _ocr_reader.cache_clear()
    settings = Settings(docling_do_ocr=True, ocr_min_confidence=0.4, vlm_ocr_enabled=True)
    detections = [
        ([[0, 0], [50, 0], [50, 10], [0, 10]], "SKU", 0.72),
        ([[0, 20], [50, 20], [50, 30], [0, 30]], "Mx", 0.02),  # dropped -> triggers escalation
    ]
    vlm = _FakeVlmDescriber(result="SKU: W-7734\nPallet count: 62")
    with patch("easyocr.Reader", return_value=_FakeOcrReader(detections)):
        result = await parse_attachment(_fake_png_bytes(), "shot.png", settings, vlm)
    _ocr_reader.cache_clear()
    assert result == "SKU: W-7734\nPallet count: 62"
    assert len(vlm.calls) == 1
    image_data, mime_type = vlm.calls[0]
    assert mime_type == "image/png"
    assert image_data  # the real image bytes were forwarded, not some placeholder


async def test_vlm_escalation_fires_when_ocr_found_nothing_at_all():
    _ocr_reader.cache_clear()
    settings = Settings(docling_do_ocr=True, vlm_ocr_enabled=True)
    vlm = _FakeVlmDescriber(result="WAREHOUSE INTAKE NOTE")
    with patch("easyocr.Reader", return_value=_FakeOcrReader([])):
        result = await parse_attachment(_fake_png_bytes(), "shot.png", settings, vlm)
    _ocr_reader.cache_clear()
    assert result == "WAREHOUSE INTAKE NOTE"
    assert len(vlm.calls) == 1


async def test_vlm_escalation_does_not_fire_when_ocr_succeeded_cleanly():
    # No dropped detections and non-empty text -> OCR looked fine, no reason to spend a VLM call.
    _ocr_reader.cache_clear()
    settings = Settings(docling_do_ocr=True, vlm_ocr_enabled=True)
    detections = [([[0, 0], [50, 0], [50, 10], [0, 10]], "HELLO", 0.99)]
    vlm = _FakeVlmDescriber(result="should never be used")
    with patch("easyocr.Reader", return_value=_FakeOcrReader(detections)):
        result = await parse_attachment(_fake_png_bytes(), "shot.png", settings, vlm)
    _ocr_reader.cache_clear()
    assert result == "HELLO"
    assert vlm.calls == []


async def test_vlm_escalation_falls_back_to_the_ocr_result_when_the_vlm_says_no_text_found():
    # The VLM's explicit "nothing here" sentinel must not blank out a real (if partial) OCR result.
    _ocr_reader.cache_clear()
    settings = Settings(docling_do_ocr=True, ocr_min_confidence=0.4, vlm_ocr_enabled=True)
    detections = [
        ([[0, 0], [50, 0], [50, 10], [0, 10]], "SKU", 0.72),
        ([[0, 20], [50, 20], [50, 30], [0, 30]], "Mx", 0.02),
    ]
    vlm = _FakeVlmDescriber(result="(no text found)")
    with patch("easyocr.Reader", return_value=_FakeOcrReader(detections)):
        result = await parse_attachment(_fake_png_bytes(), "shot.png", settings, vlm)
    _ocr_reader.cache_clear()
    assert result == "SKU"


async def test_vlm_escalation_falls_back_to_the_ocr_result_when_the_vlm_call_itself_fails():
    # A VLM outage (network error, 5xx, auth failure) must degrade to the OCR result, never crash
    # ingestion of the one attachment that happened to trigger escalation.
    _ocr_reader.cache_clear()
    settings = Settings(docling_do_ocr=True, ocr_min_confidence=0.4, vlm_ocr_enabled=True)
    detections = [
        ([[0, 0], [50, 0], [50, 10], [0, 10]], "SKU", 0.72),
        ([[0, 20], [50, 20], [50, 30], [0, 30]], "Mx", 0.02),
    ]
    vlm = _FakeVlmDescriber(result=RuntimeError("hosted VLM endpoint unreachable"))
    with patch("easyocr.Reader", return_value=_FakeOcrReader(detections)):
        result = await parse_attachment(_fake_png_bytes(), "shot.png", settings, vlm)
    _ocr_reader.cache_clear()
    assert result == "SKU"


async def test_vlm_escalation_never_fires_for_non_image_attachments():
    settings = Settings(vlm_ocr_enabled=True)
    vlm = _FakeVlmDescriber(result="should never be used")
    result = await parse_attachment(b"issue,status\nATC-1,done\n", "data.csv", settings, vlm)
    assert result == "issue,status\nATC-1,done\n"
    assert vlm.calls == []


async def test_image_ocr_failure_returns_empty_without_crashing():
    _ocr_reader.cache_clear()
    settings = Settings(docling_do_ocr=True)
    with patch("easyocr.Reader", side_effect=RuntimeError("model load failed")):
        result = await parse_attachment(_fake_png_bytes(), "shot.png", settings)
    _ocr_reader.cache_clear()
    assert result == ""


async def test_pptx_sections_preserve_slide_provenance_from_docling(monkeypatch):
    expected = [
        {"content": "## Slide 1\nIntro", "provenance": {"source_type": "pptx", "slide_number": 1}},
        {"content": "## Slide 2\nArchitecture", "provenance": {"source_type": "pptx", "slide_number": 2}},
    ]
    monkeypatch.setattr(
        "app.ingest.docling_parser._convert_structured_sections_sync",
        lambda *args: [
            __import__("app.ingest.docling_parser", fromlist=["ParsedAttachmentSection"]).ParsedAttachmentSection(**item)
            for item in expected
        ],
    )

    sections = await parse_attachment_sections(b"pptx", "deck.pptx", Settings())

    assert [section.provenance for section in sections] == [item["provenance"] for item in expected]


async def test_image_sections_expose_a_pixel_bbox(monkeypatch):
    monkeypatch.setattr(
        "app.ingest.docling_parser.parse_attachment",
        lambda *args: _async_text("OCR text"),
    )

    sections = await parse_attachment_sections(_fake_png_bytes(), "screen.png", Settings(docling_do_ocr=True))

    assert sections[0].provenance["source_type"] == "image"
    assert sections[0].provenance["bbox"] == [0, 0, 10, 10]


async def _async_text(value):
    return value


def test_xlsx_provenance_uses_the_workbook_used_range():
    from openpyxl import Workbook

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Q2 Revenue"
    sheet["B4"] = "Total"
    sheet["F18"] = 42
    output = BytesIO()
    workbook.save(output)
    workbook.close()

    assert _xlsx_used_ranges(output.getvalue()) == {"Q2 Revenue": "B4:F18"}
