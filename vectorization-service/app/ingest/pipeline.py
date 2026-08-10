"""Ingestion orchestration: Kafka message -> chunks -> embeddings -> vector store.

One entry point per event kind. Each builds the chunks for a single source, embeds them, and does a
keyed replace/delete in the vector store. Keeping this provider- and transport-agnostic makes it
directly unit-testable (see tests/test_pipeline_idempotency.py) with a fake embedder and store.
"""
from __future__ import annotations

import logging
from typing import List

from app.config import Settings
from app.db.vector_store import VectorStore
from app.ingest.attachment_fetcher import fetch_attachment
from app.ingest.chunker import chunk_text
from app.ingest.contextualizer import ContextGenerator, NoopContextGenerator
from app.ingest.docling_parser import parse_attachment_sections
from app.ingest.embedder import Embedder
from app.ingest.html_text import html_to_text
from app.ingest.vlm_describer import NoopVlmImageDescriber, VlmImageDescriber
from app.models import (
    AttachmentIngestionMessage,
    Chunk,
    CommentIngestionMessage,
    IssueHistoryIngestionMessage,
    IssueIngestionMessage,
    IssueMetadata,
    SprintIngestionMessage,
    SprintMetadata,
)

logger = logging.getLogger(__name__)


class IngestPipeline:
    def __init__(
        self,
        settings: Settings,
        embedder: Embedder,
        store: VectorStore,
        context_generator: ContextGenerator | None = None,
        vlm_describer: VlmImageDescriber | None = None,
    ):
        self._settings = settings
        self._embedder = embedder
        self._store = store
        self._context_generator = context_generator or NoopContextGenerator()
        self._vlm_describer = vlm_describer or NoopVlmImageDescriber()

    async def _chunks(
        self, text: str, *, chunk_type, issue_id, issue_key, space_id, source_id, key_prefix,
        source_label: str | None = None, page_number: int | None = None,
        provenance: dict | None = None, chunk_index_offset: int = 0, force_numbered_ids: bool = False,
    ) -> List[Chunk]:
        pieces = chunk_text(
            text, self._settings.chunk_size_tokens, self._settings.chunk_overlap_tokens
        )
        chunks = []
        for i, piece in enumerate(pieces):
            content = piece
            # Only worth contextualizing multi-chunk sources — a single-chunk issue/comment already
            # IS its own context, so there's nothing to situate it within. See contextualizer.py.
            if self._settings.contextual_retrieval_enabled and len(pieces) > 1:
                content = await self._contextualize(text, piece, issue_key, i)
            # Deterministic source tag, independent of contextual retrieval: a chunk read in isolation
            # needs some way to say which source it came from, and whether it already has one depends
            # entirely on what the ORIGINAL text looked like before splitting:
            #   - issue: handle_issue prepends f"{issue_key} {body}" into the text BEFORE chunking, so
            #     chunk #0 always naturally starts with its own key — but chunk #1+ (past the split
            #     point) has lost that prefix. Found live: ATC-43's own body is long enough to produce
            #     exactly this case in this corpus.
            #   - comment/sprint: the raw text (html_to_text(msg.content) / html_to_text(msg.goal)) has
            #     NO issue_key/sprint_name prefix at all — confirmed live, a real comment's chunk #0
            #     reads as plain prose with zero mention of which issue it's on. This is true for EVERY
            #     comment, not just multi-chunk ones.
            #   - attachment: parsed file content (a CSV row, a PDF's body text) essentially never
            #     mentions its own filename or issue key naturally, chunk #0 included.
            # So: attachment and comment/sprint chunks are tagged unconditionally; issue chunks only
            # past chunk #0, where the source text's own natural prefix no longer reaches. Contextual
            # retrieval's LLM-written sentence is optional (off by default) so it can't be the only
            # thing carrying this — this costs nothing (no LLM call) and always fires where needed.
            if chunk_type == "attachment" and source_label:
                location = provenance or {}
                if page_number is not None:
                    location_label = f"PDF page {page_number}"
                elif location.get("slide_number") is not None:
                    location_label = f"PPTX slide {location['slide_number']}"
                elif location.get("sheet_name"):
                    cell_range = f" ({location['cell_range']})" if location.get("cell_range") else ""
                    location_label = f"XLSX sheet {location['sheet_name']}{cell_range}"
                elif location.get("page_number") is not None:
                    location_label = f"DOCX page {location['page_number']}"
                else:
                    location_label = ""
                location_suffix = f" — {location_label}" if location_label else ""
                content = f"[Attachment: {source_label} — issue {issue_key}{location_suffix}]\n{content}"
            elif chunk_type == "comment":
                content = f"[Comment on {issue_key}]\n{content}"
            elif chunk_type == "sprint":
                content = f"[Sprint {issue_key}]\n{content}"
            elif chunk_type == "issue" and i > 0:
                content = f"[Issue {issue_key}]\n{content}"
            chunks.append(
                Chunk(
                    id=(f"{key_prefix}#{chunk_index_offset + i}"
                        if force_numbered_ids or len(pieces) > 1 else key_prefix),
                    chunk_type=chunk_type,
                    issue_id=issue_id,
                    issue_key=issue_key,
                    space_id=space_id,
                    source_id=source_id,
                    chunk_index=chunk_index_offset + i,
                    content=content,
                    page_number=page_number,
                    provenance=provenance or {},
                )
            )
        return chunks

    async def _contextualize(self, document: str, chunk: str, issue_key: str, chunk_index: int) -> str:
        try:
            context = await self._context_generator.generate(document, chunk)
        except Exception as exc:  # noqa: BLE001 - one context-generation failure must not fail ingestion
            logger.warning(
                "context generation failed; using chunk as-is",
                extra={"issue_key": issue_key, "chunk_index": chunk_index, "error": str(exc)},
            )
            return chunk
        return f"{context}\n\n{chunk}" if context else chunk

    async def _replace(self, chunk_type: str, source_id: int, chunks: List[Chunk]) -> None:
        if not chunks:
            # Nothing embeddable (blank title+description, empty comment) — clear any stale rows.
            await self._store.delete_source(chunk_type, source_id)
            return
        vectors = await self._embedder.embed([c.content for c in chunks])
        await self._store.replace_source(chunk_type, source_id, chunks, vectors)

    # --- Issues ---
    async def handle_issue(self, msg: IssueIngestionMessage) -> None:
        if msg.event_type == "issue_deleted":
            removed = await self._store.delete_issue(msg.issue_id)
            logger.info("issue deleted", extra={"issue_key": msg.issue_key, "removed": removed})
            return

        # Structured metadata first, and unconditionally: this must happen even when the issue has no
        # embeddable text (blank title+description -> zero chunks), or an empty-description issue would
        # silently vanish from counts/filters while still existing in jira-backend. It's the structured
        # half of ingestion (see migrations/004), independent of whether there's anything to embed.
        await self._store.upsert_issue(
            IssueMetadata(
                issue_id=msg.issue_id,
                issue_key=msg.issue_key,
                space_id=msg.space_id,
                issue_type=msg.issue_type,
                status=msg.status,
                priority=msg.priority,
                # The message carries current sprint membership (IssueIngestionMessage.sprint_id/
                # sprint_name); forward it, or the /issues/query sprint_ids/sprint_names filter — the
                # whole "how many issues in sprint N" path — sees NULL for every issue and answers 0.
                # The record_issue_change "sprint" self-heal only fires for issues that get a sprint
                # field-change event, so it can't be the sole source of this column.
                sprint_id=msg.sprint_id,
                sprint_name=msg.sprint_name,
                title=msg.title,
                # Null unless this issue is a subtask — see migrations/007 and Case Study 17. The same
                # denormalized snapshot Case Study 15 already threaded into the embedded text, now also
                # persisted structurally so "is X actually the parent of these subtasks" is a plain
                # query_issues lookup, not a heuristic guess.
                parent_key=msg.parent_key,
                # Owner and size — see migrations/012. Same "carry it on the upsert" reasoning as
                # priority: both are typically set once at creation, so a history-only sync would
                # never observe the initial value.
                assignee_id=msg.assignee_id,
                assignee_name=msg.assignee_name,
                story_points=msg.story_points,
                created_at=msg.created_at,
                updated_at=msg.updated_at,
            )
        )

        # Title + description form one logical document; embed them together. The key itself is
        # prefixed in too — not just for the lexical index (migrations/003 already covers that via
        # `search_vector`'s generated column, independent of this text), but for the EMBEDDING/vector
        # side, which has no equivalent: the embedding is computed straight from this string, and
        # before this fix it never contained the issue's own key at all. Found live: an issue with
        # many comments can have its single title+description chunk rank below several of its own
        # comments for a query that's just its key, in BOTH lexical (ts_rank_cd has no length
        # normalization) and vector search —
        # this closes the vector half of that gap at the source, for newly-ingested/re-ingested
        # issues. (get_issue_details is still the decisive, ranking-independent fix — this is a
        # complementary improvement to the ranking signal itself, not a replacement for it.)
        body = " ".join(filter(None, [msg.title, html_to_text(msg.description)])).strip()
        # Only prefix the key (and, below, the parent context) onto REAL content — a blank
        # title+description must still produce zero chunks (see test_blank_issue_clears_stale_vectors),
        # not one lone "ISSUE-KEY"-only chunk with nothing to actually search on.
        if not body:
            text = ""
        elif msg.parent_key:
            # A subtask's own title+description is often too terse to be findable, or correctly
            # characterized, on its own — "Reject repeated checkout requests" carries no hint it
            # belongs to a specific checkout incident. Found live: the agent repeatedly mischaracterized
            # a RELATED-but-separate issue as a subtask, and separately needed a post-generation code
            # check because retrieval alone couldn't reliably connect a subtask back to its parent.
            # Embedding the parent's key+title directly into the subtask's own chunk means that context
            # is present unconditionally — not contingent on the parent's own chunk also landing in the
            # same top-K, which Case Study 11 already showed is not guaranteed even for an issue's OWN
            # key. This is a denormalized snapshot (see IssueIngestionMessage.parent_title's docstring)
            # — it can go stale if the parent is later renamed without this subtask itself changing;
            # accepted for the same reason sprint_name's snapshot already is.
            parent_label = f"{msg.parent_key} ({msg.parent_title})" if msg.parent_title else msg.parent_key
            text = f"{msg.issue_key} Subtask of {parent_label}. {body}".strip()
        else:
            text = f"{msg.issue_key} {body}".strip()
        chunks = await self._chunks(
            text,
            chunk_type="issue",
            issue_id=msg.issue_id,
            issue_key=msg.issue_key,
            space_id=msg.space_id,
            source_id=msg.issue_id,
            key_prefix=f"issue:{msg.issue_id}",
        )
        await self._replace("issue", msg.issue_id, chunks)
        logger.info("issue upserted", extra={"issue_key": msg.issue_key, "chunks": len(chunks)})

    # --- Sprints (structured metadata + one embeddable chunk for the goal text; see migrations/006) ---
    async def handle_sprint(self, msg: SprintIngestionMessage) -> None:
        if msg.event_type == "sprint_deleted":
            removed = await self._store.delete_sprint(msg.sprint_id)
            logger.info("sprint deleted", extra={"sprint_name": msg.sprint_name, "removed": removed})
            return

        await self._store.upsert_sprint(
            SprintMetadata(
                sprint_id=msg.sprint_id,
                sprint_name=msg.sprint_name,
                space_id=msg.space_id,
                goal=msg.goal,
                start_date=msg.start_date,
                end_date=msg.end_date,
                status=msg.status,
                initial_committed_points=msg.initial_committed_points,
                initial_completed_points=msg.initial_completed_points,
                final_scope_points=msg.final_scope_points,
                completed_points=msg.completed_points,
                initial_issue_count=msg.initial_issue_count,
                completed_issue_count=msg.completed_issue_count,
                final_issue_count=msg.final_issue_count,
                unestimated_issue_count=msg.unestimated_issue_count,
            )
        )

        # A sprint has no parent issue, so issue_id/issue_key are reused as the sprint's own id/name
        # (see Chunk's docstring). Goal-only text: name isn't embedded alongside it because a query
        # like "which sprint is about accessibility" is asking about the goal's meaning, not matching
        # the sprint's own label.
        text = html_to_text(msg.goal)
        chunks = await self._chunks(
            text,
            chunk_type="sprint",
            issue_id=msg.sprint_id,
            issue_key=msg.sprint_name,
            space_id=msg.space_id,
            source_id=msg.sprint_id,
            key_prefix=f"sprint:{msg.sprint_id}",
        )
        await self._replace("sprint", msg.sprint_id, chunks)
        logger.info("sprint upserted", extra={"sprint_name": msg.sprint_name, "chunks": len(chunks)})

    # --- Issue history (append-only change stream; see migrations/005) ---
    async def handle_history(self, msg: IssueHistoryIngestionMessage) -> None:
        """No chunking/embedding — history is structured data, not retrievable prose. The store call
        both appends the change record and (for status/issueType field changes) refreshes the issue
        snapshot, since metadata-only edits never fire a content event."""
        await self._store.append_issue_change(msg)
        logger.info(
            "issue change recorded",
            extra={"issue_key": msg.issue_key, "history_id": msg.history_id,
                   "change": msg.change_event_type, "field": msg.field_name},
        )

    # --- Comments ---
    async def handle_comment(self, msg: CommentIngestionMessage) -> None:
        if msg.event_type == "comment_deleted":
            removed = await self._store.delete_source("comment", msg.comment_id)
            logger.info(
                "comment deleted",
                extra={"issue_key": msg.issue_key, "comment_id": msg.comment_id, "removed": removed},
            )
            return

        text = html_to_text(msg.content)
        chunks = await self._chunks(
            text,
            chunk_type="comment",
            issue_id=msg.issue_id,
            issue_key=msg.issue_key,
            space_id=msg.space_id,
            source_id=msg.comment_id,
            key_prefix=f"comment:{msg.comment_id}",
        )
        await self._replace("comment", msg.comment_id, chunks)
        logger.info(
            "comment upserted",
            extra={"issue_key": msg.issue_key, "comment_id": msg.comment_id, "chunks": len(chunks)},
        )

    # --- Attachments ---
    async def handle_attachment(self, msg: AttachmentIngestionMessage) -> None:
        if msg.event_type == "attachment_deleted":
            removed = await self._store.delete_source("attachment", msg.attachment_id)
            logger.info(
                "attachment deleted",
                extra={"issue_key": msg.issue_key, "attachment_id": msg.attachment_id, "removed": removed},
            )
            return

        if msg.byte_size and msg.byte_size > self._settings.attachment_max_bytes:
            logger.info(
                "attachment too large; skipping",
                extra={"attachment_id": msg.attachment_id, "byte_size": msg.byte_size},
            )
            return

        data = await fetch_attachment(self._settings, msg.storage_uri, msg.bucket, msg.storage_key)
        if not data:
            return
        # Kafka metadata is a useful early rejection, but it is producer-supplied. Check fetched
        # bytes too before parsing/rendering so a malformed message cannot make PDF VLM processing
        # exceed the configured attachment bound.
        if len(data) > self._settings.attachment_max_bytes:
            logger.info(
                "fetched attachment too large; skipping",
                extra={"attachment_id": msg.attachment_id, "byte_size": len(data)},
            )
            return
        sections = await parse_attachment_sections(
            data, msg.filename or "attachment", self._settings, self._vlm_describer
        )
        chunks = []
        chunk_index_offset = 0
        # Number every section-derived attachment chunk deterministically. This matters for a PDF
        # where page 2 and page 3 can each be short one-chunk sections; without the offset both
        # would otherwise claim `attachment:{id}` and overwrite one another.
        force_numbered_ids = len(sections) > 1 or any(s.page_number is not None for s in sections)
        for section in sections:
            section_chunks = await self._chunks(
                section.content,
                chunk_type="attachment",
                issue_id=msg.issue_id,
                issue_key=msg.issue_key,
                space_id=msg.space_id,
                source_id=msg.attachment_id,
                key_prefix=f"attachment:{msg.attachment_id}",
                source_label=msg.filename,
                page_number=section.page_number,
                provenance=section.provenance,
                chunk_index_offset=chunk_index_offset,
                force_numbered_ids=force_numbered_ids,
            )
            chunks.extend(section_chunks)
            chunk_index_offset += len(section_chunks)
        await self._replace("attachment", msg.attachment_id, chunks)
        logger.info(
            "attachment upserted",
            extra={"issue_key": msg.issue_key, "attachment_id": msg.attachment_id, "chunks": len(chunks)},
        )
