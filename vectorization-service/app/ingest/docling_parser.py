"""Attachment binary -> reading-order plain text, via Docling — with two bypasses for cases where
Docling's own document pipeline does a bad (or nonexistent) job, found by actually running ingestion
against a real 132-file attachment corpus, not by inspection.

Docling parses PDFs/Office docs into a structured document, then ``export_to_markdown()`` flattens it
back to text **in original reading order** with tables rendered as Markdown in place — so a table
stays next to the paragraph that introduces it instead of being split into a separate bucket. That
markdown is what we chunk and embed. This is genuinely good for PDF/DOCX/PPTX/XLSX/MD — verified live.

Design choices (see also README "Attachment handling"):
  * OCR and table-structure models are **off by default** — they're slow and only pay off for scanned
    PDFs / screenshots / complex tables. Enable per-deployment via ``VEC_DOCLING_DO_OCR`` etc.
  * **CSV, `.txt`, and `.log` bypass Docling entirely** — Docling 2.14's `DocumentConverter` has no
    `InputFormat` for plain text at all; it raises `ConversionError: File format not allowed`, which
    the broad `except Exception` below used to silently turn into empty text (~13% of that real
    corpus, indexed as nothing). These formats have no layout to preserve, so they're just decoded
    directly.
  * **Standalone images bypass Docling's document pipeline too, in favor of raw OCR** (see below) —
    a second, different kind of gap from the CSV one: Docling *does* have an `IMAGE` InputFormat, but
    tested live against real synthetic screenshot attachments (crisp, high-contrast rendered text —
    about as easy a case as OCR gets), Docling's document pipeline recovered only a short header
    fragment (`"## SHELL AC1 BEFORE"`) and treated the rest of the image as an opaque "Picture"
    element, never OCR'd — across every pipeline knob tried (`do_ocr` on/off, an explicitly wired
    `ImageFormatOption`, `force_full_page_ocr`, 3x upscaling, a lowered OCR confidence threshold).
    Root cause, isolated by testing: Docling runs a **document layout classifier** on standalone
    images before deciding what to OCR, and it was classifying the whole bordered content block as a
    "Picture" region rather than text — a reasonable heuristic for real-world documents (photos,
    diagrams embedded in a page), wrong for a plain synthetic screenshot with a colored border, and
    not something any of the OCR-specific settings above could override, since they configure *how*
    OCR runs, not *whether* a region gets OCR'd at all after layout classification says "picture."
    Confirmed the fix by testing **raw EasyOCR directly on the same image, with no document-layout
    step in front of it at all**: it recovered essentially the whole image (`"CURRENT PROBLEM"`,
    `"STAGING BEFORE ATC-7"`, `"SHARED HEADER LAYOUT SHELL"`, `"API ERROR BOUNDARY"`,
    `"SYNTHETIC STAGING SCREENSHOT"` — a few words OCR-garbled, e.g. "AC1" as "ACI", but essentially
    complete coverage vs. ~10% before). So images now skip Docling's structured-document pipeline
    entirely and go straight to EasyOCR on the full canvas — same underlying OCR engine Docling itself
    uses, just without the layout-classification step deciding not to run it.
  * That same real backfill run also surfaced a third, independent bug: the except-branches below used
    to log via ``extra={"filename": filename}``, but ``filename`` is a **reserved `LogRecord`
    attribute** (Python's stdlib sets it to the source file of the log call itself) — passing it in
    `extra` raised `KeyError: "Attempt to overwrite 'filename' in LogRecord"` *from inside the
    exception handler*, crashing the whole ingestion run instead of the one bad attachment it was
    trying to log and skip past. Fixed by using `attachment_filename` as the key instead.
  * Docling/EasyOCR are synchronous and CPU-heavy, so conversion runs in a thread via
    ``asyncio.to_thread`` to avoid blocking the event loop / Kafka consumer.
  * Docling and EasyOCR are large dependencies; both are imported lazily so the service starts (and
    the issue/comment path runs) even if they aren't installed.
"""
from __future__ import annotations

import asyncio
import logging
from functools import lru_cache
from io import BytesIO

from app.config import Settings

logger = logging.getLogger(__name__)

# Docling 2.14's DocumentConverter has no InputFormat for these at all (confirmed: it raises
# ConversionError, not a graceful "unsupported" no-op) — both are already flat text, so route them
# around Docling instead of into it.
_PLAIN_TEXT_EXTENSIONS = (".csv", ".txt", ".log")

# Bypass Docling's document/layout pipeline for these — see module docstring for why raw OCR beats
# Docling's own IMAGE pipeline here. Limited to the formats actually observed in the real corpus this
# was tested against; extend if a new image type shows up.
_IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg")


@lru_cache(maxsize=1)
def _build_converter(do_ocr: bool, do_tables: bool):
    """Construct a Docling converter once and reuse it (model load is expensive).

    Only wires PdfFormatOption now — images no longer go through Docling's converter at all (see
    module docstring), so there's nothing useful to configure for InputFormat.IMAGE here anymore.
    ``do_ocr`` still matters for PDFs: a *scanned* PDF (no embedded text layer) genuinely needs it,
    unlike the standalone-image case this module works around separately.
    """
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import PdfPipelineOptions
    from docling.document_converter import DocumentConverter, PdfFormatOption

    pipeline_options = PdfPipelineOptions()
    pipeline_options.do_ocr = do_ocr
    pipeline_options.do_table_structure = do_tables

    return DocumentConverter(
        format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=pipeline_options)}
    )


def _convert_sync(data: bytes, filename: str, do_ocr: bool, do_tables: bool) -> str:
    from docling.datamodel.base_models import DocumentStream

    converter = _build_converter(do_ocr, do_tables)
    source = DocumentStream(name=filename, stream=BytesIO(data))
    result = converter.convert(source)
    return result.document.export_to_markdown()


@lru_cache(maxsize=1)
def _ocr_reader():
    """Build the EasyOCR reader once (model load takes ~1s) and reuse it across attachments."""
    import easyocr

    return easyocr.Reader(["en"], gpu=False)


def _ocr_image_sync(data: bytes) -> str:
    import numpy as np
    from PIL import Image

    reader = _ocr_reader()
    image = np.array(Image.open(BytesIO(data)).convert("RGB"))
    detections = reader.readtext(image)

    # EasyOCR returns detections in whatever order its internal region proposals happened to fire,
    # not reading order — approximate reading order by bucketing into rows (by vertical center,
    # rounded to a coarse band) and ordering left-to-right within each row.
    def row_key(item):
        bbox, _text, _conf = item
        y_center = sum(point[1] for point in bbox) / 4
        x_left = min(point[0] for point in bbox)
        return (round(y_center / 20), x_left)

    detections.sort(key=row_key)
    return " ".join(text for _bbox, text, _conf in detections)


async def parse_attachment(data: bytes, filename: str, settings: Settings) -> str:
    """Return reading-order text for an attachment binary, or ``""`` if it can't be parsed.

    Never raises for a single bad attachment: a parse failure logs and yields empty text so the rest
    of ingestion keeps flowing.
    """
    lower_name = filename.lower()

    if lower_name.endswith(_PLAIN_TEXT_EXTENSIONS):
        try:
            return data.decode("utf-8", errors="replace")
        except Exception as exc:  # noqa: BLE001 - one bad file must not kill the consumer
            logger.warning(
                "plain-text attachment decode failed",
                extra={"attachment_filename": filename, "error": str(exc)},
            )
            return ""

    if lower_name.endswith(_IMAGE_EXTENSIONS):
        if not settings.docling_do_ocr:
            return ""  # no OCR requested, and an image has no other extractable text layer
        try:
            return await asyncio.to_thread(_ocr_image_sync, data)
        except ImportError:
            logger.warning("easyocr not installed; skipping image OCR", extra={"attachment_filename": filename})
            return ""
        except Exception as exc:  # noqa: BLE001 - one bad file must not kill the consumer
            logger.warning("image OCR failed", extra={"attachment_filename": filename, "error": str(exc)})
            return ""

    try:
        return await asyncio.to_thread(
            _convert_sync, data, filename, settings.docling_do_ocr, settings.docling_do_table_structure
        )
    except ImportError:
        logger.warning("docling not installed; skipping attachment parse", extra={"attachment_filename": filename})
        return ""
    except Exception as exc:  # noqa: BLE001 - one bad file must not kill the consumer
        logger.warning("attachment parse failed", extra={"attachment_filename": filename, "error": str(exc)})
        return ""
