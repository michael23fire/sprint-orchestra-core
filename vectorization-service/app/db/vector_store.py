"""pgvector-backed store for chunk embeddings.

Writes and deletes are keyed on deterministic ids — the "owner + write path" for the index. Reads
(``search_vector`` / ``search_lexical`` / ``search_hybrid``) are the "owner + raw query path": this
service owns the index, so it's the one place that knows how to query it; callers (the /search
endpoint, and through it the ai-service) get ranked chunk hits back, not raw SQL access.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import List, Sequence

import asyncpg
import numpy as np

from app.db.rrf import fuse_rrf
from app.models import (
    Chunk,
    IssueAttachment,
    IssueAttachmentsQueryResult,
    IssueChange,
    IssueChangeQueryResult,
    IssueComment,
    IssueCommentsQueryResult,
    IssueDetail,
    IssueDetailsQueryResult,
    IssueHistoryIngestionMessage,
    IssueMetadata,
    IssueQueryResult,
    IssueRow,
    SearchHit,
    SprintMetadata,
    SprintQueryResult,
    SprintRow,
)

logger = logging.getLogger(__name__)


class VectorStore:
    def __init__(self, pool: asyncpg.Pool):
        self._pool = pool

    async def replace_source(
        self, chunk_type: str, source_id: int, chunks: List[Chunk], vectors: List[List[float]]
    ) -> None:
        """Atomically swap all chunks for one source (issue/comment/attachment).

        A re-embed can change how many chunks a source produces (e.g. an edited PDF splits
        differently), so we delete the source's existing chunks and insert the new set in one
        transaction rather than trying to align chunk indexes.
        """
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "DELETE FROM chunks WHERE chunk_type = $1 AND source_id = $2",
                    chunk_type,
                    source_id,
                )
                if chunks:
                    await conn.executemany(
                        """
                        INSERT INTO chunks
                            (id, embedding, chunk_type, issue_id, issue_key, space_id,
                             source_id, chunk_index, content, page_number, provenance, updated_at)
                        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, now())
                        """,
                        [
                            (
                                c.id,
                                np.asarray(v, dtype=np.float32),
                                c.chunk_type,
                                c.issue_id,
                                c.issue_key,
                                c.space_id,
                                c.source_id,
                                c.chunk_index,
                                c.content,
                                c.page_number,
                                json.dumps(c.provenance),
                            )
                            for c, v in zip(chunks, vectors)
                        ],
                    )

    async def delete_source(self, chunk_type: str, source_id: int) -> int:
        """Delete all chunks for one comment/attachment. Returns rows removed."""
        result = await self._pool.execute(
            "DELETE FROM chunks WHERE chunk_type = $1 AND source_id = $2",
            chunk_type,
            source_id,
        )
        return _rows_affected(result)

    async def get_vlm_result(self, cache_key: str) -> str | None:
        """Return a durable VLM extraction result by content-addressed key, if present.

        This table intentionally stores only a hash and extracted text, never the original attachment
        bytes.  It lets at-least-once Kafka replay reuse a completed paid VLM call after a crash.
        """
        return await self._pool.fetchval(
            "SELECT content FROM vlm_result_cache WHERE cache_key = $1", cache_key
        )

    async def put_vlm_result(self, cache_key: str, namespace: str, mime_type: str, content: str) -> None:
        """Persist the first completed VLM result; duplicates are equivalent and safely ignored."""
        await self._pool.execute(
            """
            INSERT INTO vlm_result_cache (cache_key, namespace, mime_type, content)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (cache_key) DO NOTHING
            """,
            cache_key,
            namespace,
            mime_type,
            content,
        )

    async def delete_issue(self, issue_id: int) -> int:
        """Delete every chunk belonging to an issue — its issue, comment, and attachment chunks — and
        its structured metadata row.

        This is why issue deletion needs an explicit event: the vector store is a separate copy, so
        Postgres cascade deletes in jira-backend never reach it. Both the semantic side (`chunks`) and
        the structured side (`issues`, see migrations/004) must be purged, or a deleted issue would
        keep being counted by /issues/query even after it stops appearing in search.
        """
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                result = await conn.execute("DELETE FROM chunks WHERE issue_id = $1", issue_id)
                await conn.execute("DELETE FROM issues WHERE issue_id = $1", issue_id)
                # jira-backend hard-deletes an issue's history rows on issue delete
                # (issueHistoryRepository.deleteByIssue_Id), so mirror that here — keeping change rows
                # for an issue that no longer exists would leak "deleted" content into history answers.
                await conn.execute("DELETE FROM issue_changes WHERE issue_id = $1", issue_id)
        return _rows_affected(result)

    async def upsert_issue(self, meta: "IssueMetadata") -> None:
        """Upsert one issue's structured metadata (idempotent on issue_id, like the chunk write path).

        Called on every issue_upserted event, independent of whether the issue produced any chunks —
        an issue with a blank description embeds to nothing but must still be recorded here so it's
        counted. ingested_at is bumped to now() on every upsert; created_at/updated_at are the issue's
        own lifecycle timestamps and are preserved as sent (may be null until the producer emits them).
        """
        # priority IS in this INSERT/UPDATE now (unlike the first version of this method) — see
        # IssueIngestionMessage.priority's docstring for why: it's commonly set once at creation and
        # never changed again, so relying solely on append_issue_change's history-stream self-heal
        # left every such issue permanently NULL here (found live against real AtlasCart data — zero
        # priority field_change history events existed for it at all, only for an unrelated older
        # seed dataset). ON CONFLICT overwrites priority with whatever this upsert carries, same as
        # status/sprint_id — safe because the source of truth (jira-backend) is what's driving this,
        # not a stale value this table invented itself.
        await self._pool.execute(
            """
            INSERT INTO issues
                (issue_id, issue_key, space_id, issue_type, status, priority, sprint_id, sprint_name,
                 title, parent_key, created_at, updated_at, ingested_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, now())
            ON CONFLICT (issue_id) DO UPDATE SET
                issue_key   = EXCLUDED.issue_key,
                space_id    = EXCLUDED.space_id,
                issue_type  = EXCLUDED.issue_type,
                status      = EXCLUDED.status,
                priority    = EXCLUDED.priority,
                sprint_id   = EXCLUDED.sprint_id,
                sprint_name = EXCLUDED.sprint_name,
                title       = EXCLUDED.title,
                parent_key  = EXCLUDED.parent_key,
                created_at  = EXCLUDED.created_at,
                updated_at  = EXCLUDED.updated_at,
                ingested_at = now()
            """,
            meta.issue_id, meta.issue_key, meta.space_id, meta.issue_type, meta.status, meta.priority,
            meta.sprint_id, meta.sprint_name, meta.title, meta.parent_key, meta.created_at,
            meta.updated_at,
        )

    async def upsert_sprint(self, meta: SprintMetadata) -> None:
        """Upsert one sprint's structured metadata (idempotent on sprint_id, mirrors upsert_issue)."""
        await self._pool.execute(
            """
            INSERT INTO sprints
                (sprint_id, sprint_name, space_id, goal, start_date, end_date, status,
                 initial_committed_points, initial_completed_points, final_scope_points,
                 completed_points, initial_issue_count, completed_issue_count,
                 final_issue_count, unestimated_issue_count, ingested_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, now())
            ON CONFLICT (sprint_id) DO UPDATE SET
                sprint_name              = EXCLUDED.sprint_name,
                space_id                 = EXCLUDED.space_id,
                goal                     = EXCLUDED.goal,
                start_date               = EXCLUDED.start_date,
                end_date                 = EXCLUDED.end_date,
                status                   = EXCLUDED.status,
                initial_committed_points = EXCLUDED.initial_committed_points,
                initial_completed_points = EXCLUDED.initial_completed_points,
                final_scope_points       = EXCLUDED.final_scope_points,
                completed_points         = EXCLUDED.completed_points,
                initial_issue_count      = EXCLUDED.initial_issue_count,
                completed_issue_count    = EXCLUDED.completed_issue_count,
                final_issue_count        = EXCLUDED.final_issue_count,
                unestimated_issue_count  = EXCLUDED.unestimated_issue_count,
                ingested_at              = now()
            """,
            meta.sprint_id, meta.sprint_name, meta.space_id, meta.goal, meta.start_date,
            meta.end_date, meta.status, meta.initial_committed_points, meta.initial_completed_points,
            meta.final_scope_points, meta.completed_points, meta.initial_issue_count,
            meta.completed_issue_count, meta.final_issue_count, meta.unestimated_issue_count,
        )

    async def delete_sprint(self, sprint_id: int) -> int:
        """Delete a sprint's metadata row and its goal chunk (see delete_issue for the same reasoning:
        this store is a separate copy Postgres cascade deletes never reach)."""
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                result = await conn.execute("DELETE FROM sprints WHERE sprint_id = $1", sprint_id)
                await conn.execute(
                    "DELETE FROM chunks WHERE chunk_type = 'sprint' AND source_id = $1", sprint_id
                )
        return _rows_affected(result)

    async def append_issue_change(self, msg: IssueHistoryIngestionMessage) -> None:
        """Append one change record; idempotent on jira-backend's history id (redelivery/backfill safe).

        Status / issueType field changes ALSO update the `issues` snapshot row: jira-backend
        deliberately doesn't fire a content event for metadata-only edits (no text changed, so no
        re-embed), which means this history stream is the only way the snapshot learns about a
        status-only edit. Applying it here keeps "which issues are blocked" fresh without spending an
        embedding call on a change that doesn't affect the vector. `updated_at` is bumped to the
        change time for the same reason ("most recently updated" must reflect status edits too).
        """
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                inserted = await conn.fetchval(
                    """
                    INSERT INTO issue_changes
                        (id, issue_id, issue_key, space_id, event_type, field_name,
                         from_value, to_value, description, actor_name, changed_at)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
                    ON CONFLICT (id) DO NOTHING
                    RETURNING id
                    """,
                    msg.history_id, msg.issue_id, msg.issue_key, msg.space_id,
                    msg.change_event_type, msg.field_name, msg.from_value, msg.to_value,
                    msg.description, msg.actor_name, msg.emitted_at,
                )
                # Only apply the snapshot side-effect on first insert — a redelivered old event must
                # not clobber a newer status with a stale one.
                if inserted is None or msg.change_event_type != "field_change":
                    return
                if msg.field_name == "status":
                    await conn.execute(
                        "UPDATE issues SET status = $2, updated_at = $3 WHERE issue_id = $1",
                        msg.issue_id, msg.to_value, msg.emitted_at,
                    )
                elif msg.field_name == "issueType":
                    await conn.execute(
                        "UPDATE issues SET issue_type = $2, updated_at = $3 WHERE issue_id = $1",
                        msg.issue_id, msg.to_value, msg.emitted_at,
                    )
                elif msg.field_name == "priority":
                    # Same self-heal as status/issueType above: jira-backend fires this field_change
                    # unconditionally on every priority edit (IssueService.java's `update()`), but a
                    # priority-only edit never triggers a content event, so this history stream is
                    # what keeps the snapshot current for LATER edits. The upsert path
                    # (IssueIngestionMessage now carries priority too) is what covers the initial
                    # value at creation — this branch and that path are complementary, not redundant.
                    # See migrations/008.
                    await conn.execute(
                        "UPDATE issues SET priority = $2, updated_at = $3 WHERE issue_id = $1",
                        msg.issue_id, msg.to_value, msg.emitted_at,
                    )
                elif msg.field_name == "sprint":
                    # jira-backend records this transition by sprint NAME, not id (see
                    # IssueHistoryService.recordFieldChange's "sprint" call site) — resolve sprint_id
                    # via a same-space name lookup rather than leaving it null. Self-healing: if the
                    # sprint's own upsert hasn't landed yet, this resolves to NULL now and gets fixed
                    # by the next event that touches this issue (or the next backfill), same as any
                    # other eventually-consistent cross-entity reference in this pipeline.
                    await conn.execute(
                        """
                        UPDATE issues SET
                            sprint_name = $2,
                            sprint_id = (SELECT sprint_id FROM sprints WHERE space_id = $3 AND sprint_name = $2),
                            updated_at = $4
                        WHERE issue_id = $1
                        """,
                        msg.issue_id, msg.to_value, msg.space_id, msg.emitted_at,
                    )
                else:
                    await conn.execute(
                        "UPDATE issues SET updated_at = $2 WHERE issue_id = $1 AND (updated_at IS NULL OR updated_at < $2)",
                        msg.issue_id, msg.emitted_at,
                    )

    async def query_issue_changes(
        self,
        space_ids: Sequence[int],
        *,
        issue_keys: Sequence[str] | None = None,
        fields: Sequence[str] | None = None,
        event_types: Sequence[str] | None = None,
        since=None,
        until=None,
        reopened_only: bool = False,
        limit: int = 20,
    ) -> IssueChangeQueryResult:
        """Structured history query over the append-only change stream.

        ``reopened_only`` encodes the one transition question worth a first-class filter: a status
        field_change that LEAVES 'done' — i.e. an issue that was finished and got reopened. Everything
        else composes from the generic filters. Exact ``total_count`` alongside a limited sample, same
        honesty contract as query_issues.
        """
        conds = ["space_id = ANY($1::bigint[])"]
        args: list = [list(space_ids)]

        def _add(clause_tmpl: str, value) -> None:
            args.append(value)
            conds.append(clause_tmpl.format(n=len(args)))

        if issue_keys:
            _add("issue_key = ANY(${n}::text[])", list(issue_keys))
        if fields:
            _add("field_name = ANY(${n}::text[])", list(fields))
        if event_types:
            _add("event_type = ANY(${n}::text[])", list(event_types))
        if since is not None:
            _add("changed_at >= ${n}", since)
        if until is not None:
            _add("changed_at < ${n}", until)
        if reopened_only:
            conds.append("field_name = 'status' AND from_value = 'done' AND to_value IS DISTINCT FROM 'done'")
        where = " AND ".join(conds)

        total = await self._pool.fetchval(f"SELECT count(*) FROM issue_changes WHERE {where}", *args)
        rows = await self._pool.fetch(
            f"""
            SELECT id, issue_id, issue_key, space_id, event_type, field_name,
                   from_value, to_value, description, actor_name, changed_at
            FROM issue_changes WHERE {where}
            ORDER BY changed_at DESC, id DESC
            LIMIT ${len(args) + 1}
            """,
            *args, limit,
        )
        return IssueChangeQueryResult(
            total_count=total or 0,
            changes=[
                IssueChange(
                    id=r["id"], issue_id=r["issue_id"], issue_key=r["issue_key"], space_id=r["space_id"],
                    event_type=r["event_type"], field_name=r["field_name"], from_value=r["from_value"],
                    to_value=r["to_value"], description=r["description"], actor_name=r["actor_name"],
                    changed_at=r["changed_at"],
                )
                for r in rows
            ],
        )

    async def get_issue_comments(
        self, space_ids: Sequence[int], issue_keys: Sequence[str], limit: int = 200
    ) -> IssueCommentsQueryResult:
        """Every comment chunk for specific issues, complete and in order — not a semantic-search
        top-K. This is the deterministic "read everything on this issue" primitive: a plain exact-
        match SELECT against `chunks`, no embedding call, no ranking, so a detail that a hybrid/vector
        query's top-K might bury (found live: a reopen reason sitting in one comment among many on the
        same issue) simply cannot be missed as long as the issue key is right. See
        app/agent/crag_loop.py's get_issue_comments tool for when the agent should reach for this
        instead of another search_knowledge_base reformulation.
        """
        total = await self._pool.fetchval(
            "SELECT count(*) FROM chunks WHERE space_id = ANY($1::bigint[]) AND chunk_type = 'comment' "
            "AND issue_key = ANY($2::text[])",
            list(space_ids), list(issue_keys),
        )
        rows = await self._pool.fetch(
            """
            SELECT id, issue_id, issue_key, source_id, content, chunk_index
            FROM chunks
            WHERE space_id = ANY($1::bigint[]) AND chunk_type = 'comment' AND issue_key = ANY($2::text[])
            ORDER BY issue_key, chunk_index
            LIMIT $3
            """,
            list(space_ids), list(issue_keys), limit,
        )
        return IssueCommentsQueryResult(
            total_count=total or 0,
            comments=[
                IssueComment(
                    id=r["id"], issue_id=r["issue_id"], issue_key=r["issue_key"],
                    source_id=r["source_id"], content=r["content"], chunk_index=r["chunk_index"],
                )
                for r in rows
            ],
        )

    async def get_issue_details(
        self, space_ids: Sequence[int], issue_keys: Sequence[str], limit: int = 200
    ) -> IssueDetailsQueryResult:
        """Every `chunk_type='issue'` chunk (an issue's own title+description) for specific issues,
        complete and in order — not a semantic-search top-K.

        The `get_issue_comments` sibling, over `chunk_type='issue'` instead of `'comment'`. Exists
        because search's top-K ranking is not guaranteed to surface an issue's own record even when
        its key is known exactly: found live (see docs/RAG_ACCURACY_CASE_STUDIES.md Case Study 11) —
        an issue with a dozen comments can have its single title+description chunk rank *below*
        several of those comments for a query that's just its own key, because `ts_rank_cd` has no
        length normalization and a comment that also happens to mention the key scores competitively
        with the issue's own (shorter, single-mention) record. `get_issue_comments` already solved
        this exact class of problem for comments; issue bodies had no equivalent primitive until now.
        """
        total = await self._pool.fetchval(
            "SELECT count(*) FROM chunks WHERE space_id = ANY($1::bigint[]) AND chunk_type = 'issue' "
            "AND issue_key = ANY($2::text[])",
            list(space_ids), list(issue_keys),
        )
        rows = await self._pool.fetch(
            """
            SELECT id, issue_id, issue_key, source_id, content, chunk_index
            FROM chunks
            WHERE space_id = ANY($1::bigint[]) AND chunk_type = 'issue' AND issue_key = ANY($2::text[])
            ORDER BY issue_key, chunk_index
            LIMIT $3
            """,
            list(space_ids), list(issue_keys), limit,
        )
        return IssueDetailsQueryResult(
            total_count=total or 0,
            details=[
                IssueDetail(
                    id=r["id"], issue_id=r["issue_id"], issue_key=r["issue_key"],
                    source_id=r["source_id"], content=r["content"], chunk_index=r["chunk_index"],
                )
                for r in rows
            ],
        )

    async def get_issue_attachments(
        self, space_ids: Sequence[int], issue_keys: Sequence[str], limit: int = 200
    ) -> IssueAttachmentsQueryResult:
        """Every `chunk_type='attachment'` chunk for specific issues, complete and in order — not a
        semantic-search top-K.

        The `get_issue_comments`/`get_issue_details` sibling, over parsed attachment text. Exists
        because a query for a fact the caller doesn't already know the exact wording of (a SKU, an
        ID, a code buried in a PDF table) cannot reliably be phrased as a search query that ranks the
        right chunk highly — found live, this made "what SKU does ATC-46's attachment use" flip
        between finding the answer and abstaining across otherwise-equivalent phrasings of the same
        question. Once the issue key is known (the question always names it), this makes the lookup
        exact instead of a ranking gamble.
        """
        total = await self._pool.fetchval(
            "SELECT count(*) FROM chunks WHERE space_id = ANY($1::bigint[]) AND chunk_type = 'attachment' "
            "AND issue_key = ANY($2::text[])",
            list(space_ids), list(issue_keys),
        )
        rows = await self._pool.fetch(
            """
            SELECT id, issue_id, issue_key, source_id, content, chunk_index, page_number, provenance
            FROM chunks
            WHERE space_id = ANY($1::bigint[]) AND chunk_type = 'attachment' AND issue_key = ANY($2::text[])
            ORDER BY issue_key, page_number NULLS FIRST, chunk_index
            LIMIT $3
            """,
            list(space_ids), list(issue_keys), limit,
        )
        return IssueAttachmentsQueryResult(
            total_count=total or 0,
            attachments=[
                IssueAttachment(
                    id=r["id"], issue_id=r["issue_id"], issue_key=r["issue_key"],
                    source_id=r["source_id"], content=r["content"], chunk_index=r["chunk_index"],
                    page_number=r["page_number"],
                    provenance=r["provenance"] or {},
                )
                for r in rows
            ],
        )

    async def query_issues(
        self,
        space_ids: Sequence[int],
        *,
        issue_keys: Sequence[str] | None = None,
        issue_types: Sequence[str] | None = None,
        statuses: Sequence[str] | None = None,
        priorities: Sequence[str] | None = None,
        sprint_ids: Sequence[int] | None = None,
        sprint_names: Sequence[str] | None = None,
        created_after=None,
        created_before=None,
        updated_after=None,
        updated_before=None,
        order_by: str = "updated_at",
        descending: bool = True,
        limit: int = 20,
    ) -> "IssueQueryResult":
        """Structured issue query: exact count + type/status breakdown + an ordered, limited sample.

        This is the deterministic, non-semantic path — plain SQL over the `issues` table, scoped to the
        caller's spaces (the same permission boundary search enforces). It answers the class of
        question top-K similarity structurally can't: "how many bugs", "list the stories", "what
        changed most recently". `total_count` and the breakdowns count ALL matches; `rows` is only the
        first `limit`, so a "500 issues, here are 20" answer stays honest about the full size.
        """
        # Build the shared WHERE once so the count, the breakdowns, and the row fetch all filter
        # identically — the breakdown numbers must sum to total_count or the answer is inconsistent.
        conds = ["space_id = ANY($1::bigint[])"]
        args: list = [list(space_ids)]

        def _add(clause_tmpl: str, value) -> None:
            args.append(value)
            conds.append(clause_tmpl.format(n=len(args)))

        if issue_keys:
            _add("issue_key = ANY(${n}::text[])", list(issue_keys))
        if issue_types:
            _add("issue_type = ANY(${n}::text[])", list(issue_types))
        if statuses:
            _add("status = ANY(${n}::text[])", list(statuses))
        if priorities:
            _add("priority = ANY(${n}::text[])", list(priorities))
        if sprint_ids:
            _add("sprint_id = ANY(${n}::bigint[])", list(sprint_ids))
        if sprint_names:
            _add("sprint_name = ANY(${n}::text[])", list(sprint_names))
        if created_after is not None:
            _add("created_at >= ${n}", created_after)
        if created_before is not None:
            _add("created_at < ${n}", created_before)
        if updated_after is not None:
            _add("updated_at >= ${n}", updated_after)
        if updated_before is not None:
            _add("updated_at < ${n}", updated_before)
        where = " AND ".join(conds)

        # order_by / descending are NOT user-free-text — the API layer validates them against a fixed
        # allowlist (see api/routes.py) before they reach here, so this f-string can't be an injection
        # vector: only 'created_at' | 'updated_at' and ASC | DESC ever get formatted in.
        order_col = order_by if order_by in {"created_at", "updated_at"} else "updated_at"
        direction = "DESC" if descending else "ASC"

        total = await self._pool.fetchval(f"SELECT count(*) FROM issues WHERE {where}", *args)
        type_rows = await self._pool.fetch(
            f"SELECT issue_type, count(*) AS n FROM issues WHERE {where} GROUP BY issue_type", *args
        )
        status_rows = await self._pool.fetch(
            f"SELECT status, count(*) AS n FROM issues WHERE {where} GROUP BY status", *args
        )
        # NULLS LAST so issues missing the order column (metadata not yet emitted) sink to the bottom
        # rather than masquerading as the most/least recent.
        rows = await self._pool.fetch(
            f"""
            SELECT issue_id, issue_key, space_id, issue_type, status, priority, sprint_id, sprint_name,
                   title, parent_key, created_at, updated_at
            FROM issues WHERE {where}
            ORDER BY {order_col} {direction} NULLS LAST, issue_id {direction}
            LIMIT ${len(args) + 1}
            """,
            *args, limit,
        )
        return IssueQueryResult(
            total_count=total or 0,
            counts_by_type={(r["issue_type"] or "unknown"): r["n"] for r in type_rows},
            counts_by_status={(r["status"] or "unknown"): r["n"] for r in status_rows},
            rows=[
                IssueRow(
                    issue_id=r["issue_id"], issue_key=r["issue_key"], space_id=r["space_id"],
                    issue_type=r["issue_type"], status=r["status"], priority=r["priority"],
                    sprint_id=r["sprint_id"], sprint_name=r["sprint_name"], title=r["title"],
                    parent_key=r["parent_key"], created_at=r["created_at"], updated_at=r["updated_at"],
                )
                for r in rows
            ],
        )

    async def query_sprints(
        self,
        space_ids: Sequence[int],
        *,
        statuses: Sequence[str] | None = None,
        order_by: str = "start_date",
        descending: bool = True,
        limit: int = 20,
    ) -> SprintQueryResult:
        """Structured sprint query — the counterpart to query_issues, over the `sprints` table
        (migrations/006). Answers "how many sprints", "which is active", "what's sprint N's goal/
        velocity" without needing semantic search to get lucky on wording.
        """
        conds = ["space_id = ANY($1::bigint[])"]
        args: list = [list(space_ids)]

        def _add(clause_tmpl: str, value) -> None:
            args.append(value)
            conds.append(clause_tmpl.format(n=len(args)))

        if statuses:
            _add("status = ANY(${n}::text[])", list(statuses))
        where = " AND ".join(conds)

        order_col = order_by if order_by in {"start_date", "end_date"} else "start_date"
        direction = "DESC" if descending else "ASC"

        total = await self._pool.fetchval(f"SELECT count(*) FROM sprints WHERE {where}", *args)
        status_rows = await self._pool.fetch(
            f"SELECT status, count(*) AS n FROM sprints WHERE {where} GROUP BY status", *args
        )
        rows = await self._pool.fetch(
            f"""
            SELECT sprint_id, sprint_name, space_id, goal, start_date, end_date, status,
                   initial_committed_points, initial_completed_points, final_scope_points,
                   completed_points, initial_issue_count, completed_issue_count,
                   final_issue_count, unestimated_issue_count
            FROM sprints WHERE {where}
            ORDER BY {order_col} {direction} NULLS LAST, sprint_id {direction}
            LIMIT ${len(args) + 1}
            """,
            *args, limit,
        )
        return SprintQueryResult(
            total_count=total or 0,
            counts_by_status={(r["status"] or "unknown"): r["n"] for r in status_rows},
            rows=[
                SprintRow(
                    sprint_id=r["sprint_id"], sprint_name=r["sprint_name"], space_id=r["space_id"],
                    goal=r["goal"], start_date=r["start_date"], end_date=r["end_date"], status=r["status"],
                    initial_committed_points=r["initial_committed_points"],
                    initial_completed_points=r["initial_completed_points"],
                    final_scope_points=r["final_scope_points"], completed_points=r["completed_points"],
                    initial_issue_count=r["initial_issue_count"],
                    completed_issue_count=r["completed_issue_count"],
                    final_issue_count=r["final_issue_count"],
                    unestimated_issue_count=r["unestimated_issue_count"],
                )
                for r in rows
            ],
        )

    async def count(self) -> int:
        return await self._pool.fetchval("SELECT count(*) FROM chunks")

    async def search_vector(
        self, embedding: List[float], space_ids: Sequence[int], limit: int
    ) -> List[SearchHit]:
        """Dense retrieval: nearest neighbours by cosine distance, scoped to the caller's spaces.

        Space filtering mirrors jira-backend's FTS scoping (SearchService only searches spaces the
        user is an active member of) — the same permission boundary applies to semantic search.
        """
        rows = await self._pool.fetch(
            """
            SELECT id, chunk_type, issue_id, issue_key, space_id, source_id, content, page_number, provenance,
                   1 - (embedding <=> $1) AS score
            FROM chunks
            WHERE space_id = ANY($2::bigint[])
            ORDER BY embedding <=> $1
            LIMIT $3
            """,
            np.asarray(embedding, dtype=np.float32),
            list(space_ids),
            limit,
        )
        return [_row_to_hit(r, retrievers=["vector"]) for r in rows]

    async def search_lexical(
        self, query: str, space_ids: Sequence[int], limit: int
    ) -> List[SearchHit]:
        """Lexical retrieval over the same chunk rows, via Postgres FTS (ts_rank_cd)."""
        rows = await self._pool.fetch(
            """
            SELECT id, chunk_type, issue_id, issue_key, space_id, source_id, content, page_number, provenance,
                   ts_rank_cd(search_vector, plainto_tsquery('english', $1)) AS score
            FROM chunks
            WHERE space_id = ANY($2::bigint[])
              AND search_vector @@ plainto_tsquery('english', $1)
            ORDER BY score DESC
            LIMIT $3
            """,
            query,
            list(space_ids),
            limit,
        )
        return [_row_to_hit(r, retrievers=["lexical"]) for r in rows]

    async def search_hybrid(
        self,
        embedding: List[float],
        query: str,
        space_ids: Sequence[int],
        limit: int,
        candidate_pool: int = 20,
    ) -> List[SearchHit]:
        """Fuse dense + lexical retrieval via Reciprocal Rank Fusion (see app/db/rrf.py)."""
        vector_hits, lexical_hits = await asyncio.gather(
            self.search_vector(embedding, space_ids, candidate_pool),
            self.search_lexical(query, space_ids, candidate_pool),
        )
        return fuse_rrf(vector_hits, lexical_hits, limit=limit)


def _rows_affected(command_tag: str) -> int:
    # asyncpg returns tags like "DELETE 3"; the trailing integer is the row count.
    try:
        return int(command_tag.split()[-1])
    except (ValueError, IndexError):
        return 0


def _row_to_hit(row: asyncpg.Record, retrievers: List[str]) -> SearchHit:
    return SearchHit(
        id=row["id"],
        chunk_type=row["chunk_type"],
        issue_id=row["issue_id"],
        issue_key=row["issue_key"],
        space_id=row["space_id"],
        source_id=row["source_id"],
        content=row["content"],
        score=float(row["score"]),
        retrievers=retrievers,
        page_number=row["page_number"],
        provenance=row["provenance"] or {},
    )
