-- Page is first-class provenance for PDF chunks.  It is nullable because issue/comment/sprint
-- chunks have no document page, and legacy Docling-only attachment chunks cannot be assigned a page
-- without guessing.  Values are user-facing, one-based PDF page numbers.
ALTER TABLE chunks ADD COLUMN page_number INT NULL;
ALTER TABLE chunks
    ADD CONSTRAINT chunks_page_number_positive CHECK (page_number IS NULL OR page_number >= 1);

-- Exact attachment-page reads and page-aware citations need this small partial index; vector/HNSW
-- retrieval remains unchanged because it indexes only `embedding`.
CREATE INDEX idx_chunks_attachment_page
    ON chunks (source_id, page_number, chunk_index)
    WHERE chunk_type = 'attachment' AND page_number IS NOT NULL;
