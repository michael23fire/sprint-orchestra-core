-- Format-native attachment provenance. Keep this as JSONB because source locators differ by format:
-- PDF {source_type, page_number}, PPTX {source_type, slide_number}, XLSX
-- {source_type, sheet_name, cell_range}, and images {source_type, bbox}.
-- The existing page_number column remains the indexed fast path for exact PDF-page filters.
ALTER TABLE chunks ADD COLUMN provenance JSONB NOT NULL DEFAULT '{}'::jsonb;

-- Provenance is small metadata and is returned with hits; do not add a GIN index by default. Exact
-- PDF filtering uses the typed page_number column and its partial index from migration 010.
