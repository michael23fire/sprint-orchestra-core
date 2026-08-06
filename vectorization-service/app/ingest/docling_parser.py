"""Attachment binary -> reading-order plain text, via Docling — with two bypasses for cases where
Docling's own document pipeline does a bad (or nonexistent) job, found by actually running ingestion
against a real 132-file attachment corpus, not by inspection.

Docling parses PDFs/Office docs into a structured document, then ``export_to_markdown()`` renders
tables as Markdown in place — so a table stays next to the paragraph that introduces it instead of
being split into a separate bucket. PDF ingestion exports each Docling page separately when the
document exposes page provenance, so page metadata survives into the RAG chunks; non-paginated
formats and parser fallbacks remain document-level text. This is genuinely good for
PDF/DOCX/PPTX/XLSX/MD — verified live.

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
  * **A struggling OCR result can escalate to a vision-language model** (`VEC_VLM_OCR_ENABLED`, see
    `vlm_describer.py`) — found on a degraded (rotated/blurred/re-compressed) test photo: even with the
    confidence threshold above, OCR turning garbage into *nothing* still leaves a gap the answering
    model then guesses at. A VLM sees the whole image and can say "illegible" instead of emitting a
    wrong character sequence — see Case Study 27/30. Off by default; only escalates when this specific
    image's OCR result actually looked like it struggled (see `_maybe_escalate_to_vlm`).
"""
from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from functools import lru_cache
from io import BytesIO

from app.config import Settings
from app.ingest.pdf_vlm import PdfPageCandidate, render_pdf_candidates
from app.ingest.vlm_describer import NoopVlmImageDescriber, VlmImageDescriber
from app.models import AttachmentProvenance

logger = logging.getLogger(__name__)

# Docling 2.14's DocumentConverter has no InputFormat for these at all (confirmed: it raises
# ConversionError, not a graceful "unsupported" no-op) — both are already flat text, so route them
# around Docling instead of into it.
_PLAIN_TEXT_EXTENSIONS = (".csv", ".txt", ".log")

# Bypass Docling's document/layout pipeline for these — see module docstring for why raw OCR beats
# Docling's own IMAGE pipeline here. Limited to the formats actually observed in the real corpus this
# was tested against; extend if a new image type shows up.
_IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg")
_PDF_EXTENSION = ".pdf"

_MIME_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg"}


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


def _convert_pdf_sections_sync(
    data: bytes, filename: str, do_ocr: bool, do_tables: bool
) -> list[ParsedAttachmentSection]:
    """Export Docling PDF output page-by-page when its provenance map is available.

    Docling's document model stores page provenance, but a plain ``export_to_markdown()`` call
    flattens that structure. Exporting each ``page_no`` keeps the table/reading-order serializer while
    allowing the ingestion pipeline to persist a trustworthy 1-based PDF page. If a future Docling
    format/result has no page map, we deliberately return one unpaged section instead of guessing.
    """
    from docling.datamodel.base_models import DocumentStream

    converter = _build_converter(do_ocr, do_tables)
    source = DocumentStream(name=filename, stream=BytesIO(data))
    result = converter.convert(source)
    document = result.document
    page_numbers = sorted(getattr(document, "pages", {}) or {})
    if page_numbers:
        sections = []
        for page_number in page_numbers:
            content = document.export_to_markdown(page_no=page_number).strip()
            if content:
                sections.append(
                    ParsedAttachmentSection(
                        content=content,
                        page_number=page_number,
                        provenance={"source_type": "pdf", "page_number": page_number},
                    )
                )
        if sections:
            return sections
    content = document.export_to_markdown().strip()
    return [ParsedAttachmentSection(content=content)] if content else []


def _convert_structured_sections_sync(
    data: bytes, filename: str, do_ocr: bool, do_tables: bool
) -> list[ParsedAttachmentSection]:
    """Preserve native locators for Docling-supported Office formats when available.

    PPTX/DOCX documents can expose page-like provenance through Docling's page map. XLSX's
    serializer emits worksheet headings, which are split into sheet sections so a chunk can point to
    a stable sheet name instead of a renderer-dependent print page.
    """
    from docling.datamodel.base_models import DocumentStream

    converter = _build_converter(do_ocr, do_tables)
    source = DocumentStream(name=filename, stream=BytesIO(data))
    document = converter.convert(source).document
    lower_name = filename.lower()
    source_type = lower_name.rsplit(".", 1)[-1] if "." in lower_name else "document"

    if source_type in {"pptx", "docx"}:
        page_numbers = sorted(getattr(document, "pages", {}) or {})
        sections = []
        for page_number in page_numbers:
            content = document.export_to_markdown(page_no=page_number).strip()
            if content:
                locator = {"source_type": source_type}
                if source_type == "pptx":
                    locator["slide_number"] = page_number
                else:
                    locator["page_number"] = page_number
                sections.append(
                    ParsedAttachmentSection(
                        content=content,
                        # Chunk.page_number is reserved for physical PDF-page filtering; DOCX page
                        # provenance remains in the typed locator because pagination is renderer-
                        # dependent.
                        page_number=None,
                        provenance=locator,
                    )
                )
        if sections:
            return sections

    content = document.export_to_markdown().strip()
    if source_type == "xlsx":
        sheet_ranges = _xlsx_used_ranges(data)
        # Docling's document model groups worksheet nodes as `sheet: <name>`. Serialize each group
        # directly so multiple sheets never collapse into one unlocatable Markdown stream.
        try:
            from docling_core.transforms.serializer.markdown import MarkdownDocSerializer

            serializer = MarkdownDocSerializer(doc=document)
            sheet_groups = [
                group for group in getattr(document, "groups", [])
                if str(getattr(group, "name", "")).startswith("sheet:")
            ]
        except Exception:  # noqa: BLE001 - fallback below still preserves whole-workbook text
            sheet_groups = []
        if sheet_groups:
            sections = []
            for group in sheet_groups:
                sheet_name = str(group.name).removeprefix("sheet:").strip()
                sheet_content = serializer.serialize(item=group).text.strip()
                if not sheet_content:
                    continue
                locator = {"source_type": "xlsx", "sheet_name": sheet_name}
                if sheet_ranges.get(sheet_name):
                    locator["cell_range"] = sheet_ranges[sheet_name]
                sections.append(ParsedAttachmentSection(content=sheet_content, provenance=locator))
            if sections:
                return sections
        # Older Docling versions may not expose worksheet groups. If they emit worksheet headings,
        # retain that stable name; otherwise fall back to one workbook-level locator.
        matches = list(re.finditer(r"(?m)^##\s+(.+?)\s*$", content))
        if matches:
            sections = []
            for index, match in enumerate(matches):
                end = matches[index + 1].start() if index + 1 < len(matches) else len(content)
                sheet_name = match.group(1).strip()
                sheet_content = content[match.start():end].strip()
                if sheet_content:
                    locator = {"source_type": "xlsx", "sheet_name": sheet_name}
                    if sheet_ranges.get(sheet_name):
                        locator["cell_range"] = sheet_ranges[sheet_name]
                    sections.append(
                        ParsedAttachmentSection(
                            content=sheet_content,
                            provenance=locator,
                        )
                    )
            if sections:
                return sections
    return [
        ParsedAttachmentSection(
            content=content,
            provenance={"source_type": source_type},
        )
    ] if content else []


def _xlsx_used_ranges(data: bytes) -> dict[str, str]:
    """Return stable worksheet used ranges when openpyxl can read the workbook.

    This is deliberately supplemental to Docling: the Markdown serializer remains the source of
    searchable content, while openpyxl supplies the native workbook coordinate for citation. A
    missing range is valid (for example, a malformed or macro-only workbook) and is not guessed.
    """
    try:
        import openpyxl

        workbook = openpyxl.load_workbook(BytesIO(data), read_only=True, data_only=False)
        try:
            return {
                worksheet.title: worksheet.calculate_dimension()
                for worksheet in workbook.worksheets
            }
        finally:
            workbook.close()
    except Exception:  # noqa: BLE001 - coordinate enrichment must not break text ingestion
        return {}


@lru_cache(maxsize=1)
def _ocr_reader():
    """Build the EasyOCR reader once (model load takes ~1s) and reuse it across attachments."""
    import easyocr

    return easyocr.Reader(["en"], gpu=False)


@dataclass
class _OcrResult:
    text: str
    dropped_count: int  # detections discarded as low-confidence — a concrete "OCR struggled" signal


@dataclass(frozen=True)
class ParsedAttachmentSection:
    """Parsed text plus its precise source page, when the parser knows one.

    A missing page number means "not a paginated source" or "page boundary unavailable"; it never
    means page one. This avoids inventing provenance for Docling's document-level Markdown.
    """

    content: str
    page_number: int | None = None
    provenance: AttachmentProvenance | None = None


def _ocr_image_sync(data: bytes, min_confidence: float) -> _OcrResult:
    import numpy as np
    from PIL import Image

    reader = _ocr_reader()
    image = np.array(Image.open(BytesIO(data)).convert("RGB"))
    detections = reader.readtext(image)

    # Drop low-confidence detections instead of joining everything unconditionally — found live on a
    # degraded test image that garbled fragments ("Mx", "Mfate") scored 0.006-0.06 while real words
    # ("62", "SKU") scored 0.7-1.0, a clean gap, not a fuzzy one (see Settings.ocr_min_confidence).
    # Un-filtered, an agent answering from this text has no way to know "Mx" isn't a real value — it
    # confidently presented it as a SKU in a live test, and a faithfulness check can't catch this
    # (the answer WAS faithful to the retrieved text; the retrieved text itself was garbage).
    dropped = [(text, conf) for _bbox, text, conf in detections if conf < min_confidence]
    if dropped:
        logger.info(
            "dropped low-confidence OCR detections",
            extra={"dropped_count": len(dropped), "dropped_texts": [t for t, _ in dropped]},
        )
    detections = [d for d in detections if d[2] >= min_confidence]

    # EasyOCR returns detections in whatever order its internal region proposals happened to fire,
    # not reading order — approximate reading order by bucketing into rows (by vertical center,
    # rounded to a coarse band) and ordering left-to-right within each row.
    def row_key(item):
        bbox, _text, _conf = item
        y_center = sum(point[1] for point in bbox) / 4
        x_left = min(point[0] for point in bbox)
        return (round(y_center / 20), x_left)

    detections.sort(key=row_key)
    text = " ".join(text for _bbox, text, _conf in detections)
    return _OcrResult(text=text, dropped_count=len(dropped))


async def _maybe_escalate_to_vlm(
    data: bytes, mime_type: str, ocr_result: _OcrResult, settings: Settings, vlm_describer
) -> str:
    """Returns `ocr_result.text` unchanged unless VLM escalation is enabled AND EasyOCR showed a
    concrete sign of struggling on this specific image — some detections were confidently discarded
    as garbage, or nothing survived filtering at all. Not run on every image unconditionally: most
    attachment images in this project's corpus are clean synthetic screenshots EasyOCR already handles
    correctly (Case Study 6), and paying for a vision-model call on those would be pure waste.

    A non-empty, non-sentinel VLM transcription *replaces* the OCR text rather than being appended —
    the VLM re-read the whole image with the context OCR's per-character matching doesn't have, so a
    full re-transcription is a better source of truth than a partial one, not a supplement to it.
    """
    if not settings.vlm_ocr_enabled:
        return ocr_result.text
    struggled = ocr_result.dropped_count > 0 or not ocr_result.text.strip()
    if not struggled:
        return ocr_result.text
    try:
        vlm_text = await vlm_describer.describe(data, mime_type)
    except Exception as exc:  # noqa: BLE001 - a VLM outage must fall back to the OCR result, not crash ingestion
        logger.warning("VLM escalation failed; keeping the OCR result", extra={"error": str(exc)})
        return ocr_result.text
    if not vlm_text or vlm_text.strip() == "(no text found)":
        return ocr_result.text
    logger.info(
        "VLM escalation replaced a struggling OCR result",
        extra={"ocr_dropped_count": ocr_result.dropped_count, "ocr_text_len": len(ocr_result.text)},
    )
    return vlm_text


async def _pdf_sections_with_vlm(
    data: bytes,
    docling_sections: list[ParsedAttachmentSection],
    settings: Settings,
    vlm_describer: VlmImageDescriber,
) -> list[ParsedAttachmentSection]:
    """Supplement Docling with VLM-extracted PDF pages that are likely scans or visuals.

    When every page needs VLM, Docling's document-level Markdown is normally OCR noise or empty, so
    the page outputs replace it. For a mixed PDF, retain good Docling text for unaffected pages and
    replace selected page-aware sections with explicitly page-labelled VLM content. If Docling did
    not expose page boundaries, retain its document-level text and append the VLM sections instead.
    This keeps RAG provenance inspectable and avoids throwing away clean text.
    """
    docling_text = "\n\n".join(section.content for section in docling_sections).strip()
    if not (settings.vlm_ocr_enabled and settings.vlm_pdf_enabled):
        return docling_sections
    try:
        candidates, total_pages = await asyncio.to_thread(
            render_pdf_candidates,
            data,
            min_text_chars=settings.vlm_pdf_min_text_chars,
            include_visual_pages=settings.vlm_pdf_include_visual_pages,
            max_pages=settings.vlm_pdf_max_pages,
            dpi=settings.vlm_pdf_render_dpi,
        )
    except ImportError:
        logger.warning("PyMuPDF not installed; skipping PDF VLM fallback")
        return docling_sections
    except Exception as exc:  # noqa: BLE001 - preserve Docling text if inspection/rendering fails
        logger.warning("PDF VLM candidate rendering failed; keeping Docling result", extra={"error": str(exc)})
        return docling_sections

    if not candidates:
        return docling_sections

    rendered_pages: list[ParsedAttachmentSection] = []
    for candidate in candidates:
        try:
            text = await vlm_describer.describe(candidate.image_data, "image/png")
        except Exception as exc:  # noqa: BLE001 - one page failure must not discard its whole attachment
            logger.warning(
                "PDF page VLM extraction failed; keeping available source text",
                extra={"page_number": candidate.page_number, "error": str(exc)},
            )
            continue
        if not text or text.strip() == "(no text found)":
            continue
        rendered_pages.append(
            ParsedAttachmentSection(
                content=f"[VLM extracted PDF page {candidate.page_number} ({candidate.reason})]\n{text.strip()}",
                page_number=candidate.page_number,
                provenance={
                    "source_type": "pdf",
                    "page_number": candidate.page_number,
                    "extraction": "vlm",
                },
            )
        )

    if not rendered_pages:
        return docling_sections
    # A fully selected PDF is scan/visual-only (within the configured safe page cap): VLM is the
    # better source of truth, rather than appending it to unusable Docling OCR garbage.
    if (
        len(candidates) == total_pages
        and len(docling_text.strip()) < total_pages * settings.vlm_pdf_min_text_chars
    ):
        return rendered_pages
    # If Docling supplied page-aware sections, replace only the selected pages whose VLM extraction
    # succeeded. This avoids embedding a low-quality duplicate of page 7 as an unpaged document
    # section while preserving Docling's good text for every other page. If Docling had no page map,
    # retain its document-level text and append the explicitly labelled VLM pages.
    rendered_numbers = {section.page_number for section in rendered_pages}
    if any(section.page_number is not None for section in docling_sections):
        retained = [
            section for section in docling_sections if section.page_number not in rendered_numbers
        ]
        return retained + rendered_pages
    return ([ParsedAttachmentSection(docling_text)] if docling_text else []) + rendered_pages


async def _parse_pdf_sections(
    data: bytes, filename: str, settings: Settings, vlm_describer: VlmImageDescriber
) -> list[ParsedAttachmentSection]:
    """Run Docling first, retaining real VLM page provenance whenever it is available."""
    try:
        docling_sections = await asyncio.to_thread(
            _convert_pdf_sections_sync,
            data,
            filename,
            settings.docling_do_ocr,
            settings.docling_do_table_structure,
        )
    except ImportError:
        # VLM page rendering is independently useful for image-only PDFs even if Docling was not
        # installed in a lightweight deployment.
        logger.warning("docling not installed; trying configured PDF VLM fallback", extra={"attachment_filename": filename})
        docling_sections = []
    except Exception as exc:  # noqa: BLE001 - VLM can still recover a failed PDF conversion
        logger.warning("PDF parse failed; trying configured VLM fallback", extra={"attachment_filename": filename, "error": str(exc)})
        docling_sections = []
    return await _pdf_sections_with_vlm(data, docling_sections, settings, vlm_describer)


async def parse_attachment(
    data: bytes, filename: str, settings: Settings, vlm_describer: VlmImageDescriber | None = None
) -> str:
    """Return reading-order text for an attachment binary, or ``""`` if it can't be parsed.

    Never raises for a single bad attachment: a parse failure logs and yields empty text so the rest
    of ingestion keeps flowing.

    `vlm_describer` defaults to a no-op (VLM escalation is opt-in via `settings.vlm_ocr_enabled`) —
    only ever passed explicitly by `IngestPipeline`, which builds one real client at startup and reuses
    it across every attachment rather than opening a new one per call. See `vlm_describer.py`.
    """
    vlm_describer = vlm_describer or NoopVlmImageDescriber()
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
            ocr_result = await asyncio.to_thread(_ocr_image_sync, data, settings.ocr_min_confidence)
            ext = next(e for e in _IMAGE_EXTENSIONS if lower_name.endswith(e))
            return await _maybe_escalate_to_vlm(data, _MIME_TYPES[ext], ocr_result, settings, vlm_describer)
        except ImportError:
            logger.warning("easyocr not installed; skipping image OCR", extra={"attachment_filename": filename})
            return ""
        except Exception as exc:  # noqa: BLE001 - one bad file must not kill the consumer
            logger.warning("image OCR failed", extra={"attachment_filename": filename, "error": str(exc)})
            return ""

    if lower_name.endswith(_PDF_EXTENSION):
        sections = await _parse_pdf_sections(data, filename, settings, vlm_describer)
        return "\n\n".join(section.content for section in sections)

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


async def parse_attachment_sections(
    data: bytes, filename: str, settings: Settings, vlm_describer: VlmImageDescriber | None = None
) -> list[ParsedAttachmentSection]:
    """Return attachment text in provenance-preserving sections for ingestion.

    This is the pipeline-facing variant of :func:`parse_attachment`. It retains a 1-based page
    number for PDF output whenever Docling or VLM exposes trustworthy page provenance, while
    preserving the public string-returning helper used by older callers and tests. Non-PDF sources
    use their native locator in ``provenance`` (slide, sheet/range, DOCX page when available, or
    image bbox); the PDF-only ``page_number`` column stays null for those formats.
    """
    vlm_describer = vlm_describer or NoopVlmImageDescriber()
    if filename.lower().endswith(_PDF_EXTENSION):
        return await _parse_pdf_sections(data, filename, settings, vlm_describer)
    lower_name = filename.lower()
    if lower_name.endswith(_IMAGE_EXTENSIONS):
        text = await parse_attachment(data, filename, settings, vlm_describer)
        if not text:
            return []
        try:
            from PIL import Image

            with Image.open(BytesIO(data)) as image:
                width, height = image.size
            provenance = {
                "source_type": "image",
                "bbox": [0, 0, width, height],
                "coordinate_system": "pixels",
            }
        except Exception:  # noqa: BLE001 - text remains useful when image metadata is unavailable
            provenance = {"source_type": "image"}
        return [ParsedAttachmentSection(text, provenance=provenance)]
    if lower_name.endswith(_PLAIN_TEXT_EXTENSIONS):
        text = await parse_attachment(data, filename, settings, vlm_describer)
        return [ParsedAttachmentSection(text, provenance={"source_type": lower_name.rsplit(".", 1)[-1]})] if text else []
    try:
        return await asyncio.to_thread(
            _convert_structured_sections_sync,
            data,
            filename,
            settings.docling_do_ocr,
            settings.docling_do_table_structure,
        )
    except ImportError:
        logger.warning("docling not installed; skipping attachment parse", extra={"attachment_filename": filename})
        return []
    except Exception as exc:  # noqa: BLE001 - preserve per-attachment failure isolation
        logger.warning("attachment structured parse failed", extra={"attachment_filename": filename, "error": str(exc)})
        return []
