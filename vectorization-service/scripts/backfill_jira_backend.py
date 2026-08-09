"""One-time backfill: ingest jira-backend's EXISTING issues/comments into this service's index.

Why this needs to exist at all: Kafka events (jira.content.ingestion) only fire going forward, from
the moment ContentIngestionKafkaPublisher is enabled
(jira-backend's APP_KAFKA_CONTENT_INGESTION_ENABLED=true) -- they are not retroactive. Any issue or
comment created before that flag was ever turned on (which, in this project's own dev environment,
was *everything* -- confirmed by checking the running jira-backend process's env: only
APP_KAFKA_ATTACHMENT_INGESTION_ENABLED was set) has no corresponding Kafka message and never reaches
this service on its own.

This script closes that gap by reading directly from jira-backend's own Postgres and replaying each
row through the *exact same* `IngestPipeline.handle_issue` / `handle_comment` methods a real Kafka
message would have driven (see app/ingest/pipeline.py) -- so backfilled data is shaped identically to
data that arrives the normal way, with no separate/duplicated ingestion logic to drift out of sync.

Usage:
    JIRA_BACKEND_PG_DSN=postgresql://poc:poc123@localhost:5432/pocdb \
    VEC_EMBEDDING_PROVIDER=openai VEC_OPENAI_BASE_URL=http://localhost:1234/v1 \
    VEC_EMBEDDING_MODEL=text-embedding-qwen3-embedding-0.6b VEC_EMBEDDING_DIM=1024 \
    python -m scripts.backfill_jira_backend --space-ids 1,3 --truncate-first
"""
from __future__ import annotations

import argparse
import asyncio
import os
import uuid
from datetime import datetime, timezone
from typing import List, Optional

import asyncpg

from app.config import Settings
from app.db.pool import create_pool
from app.db.vector_store import VectorStore
from app.ingest.contextualizer import build_context_generator
from app.ingest.vlm_cache import CachedVlmImageDescriber
from app.ingest.vlm_describer import build_vlm_describer, vlm_cache_namespace
from app.ingest.embedder import build_embedder
from app.ingest.pipeline import IngestPipeline
from app.models import (
    AttachmentIngestionMessage,
    CommentIngestionMessage,
    IssueHistoryIngestionMessage,
    IssueIngestionMessage,
    SprintIngestionMessage,
)


async def _fetch_sprints(jira_pool: asyncpg.Pool, space_ids: Optional[List[int]]):
    query = """
        SELECT id, name, space_id, goal, start_date, end_date, status, updated_at,
               initial_committed_points, initial_completed_points, final_scope_points,
               completed_points, initial_issue_count, completed_issue_count, final_issue_count,
               unestimated_issue_count
        FROM sprints
    """
    if space_ids:
        query += " WHERE space_id = ANY($1::bigint[])"
        return await jira_pool.fetch(query, space_ids)
    return await jira_pool.fetch(query)


async def _fetch_issues(jira_pool: asyncpg.Pool, space_ids: Optional[List[int]]):
    # sprint_id + the sprint's display name (via join) travel with the issue so its current sprint
    # membership is ingested — without them every issue lands with a NULL sprint and the
    # /issues/query sprint filter can never match. LEFT JOIN: a backlog issue (no sprint) is kept
    # with NULLs, not dropped.
    #
    # issue_type/status/parent_key (via a self-join to resolve parent_id -> its issue_key) must be
    # selected here too: upsert_issue's ON CONFLICT unconditionally overwrites these columns with
    # whatever IssueIngestionMessage carries (by design — the live Kafka path always has the true
    # current value, so an unconditional overwrite is correct there). This query used to omit all
    # three, so IssueIngestionMessage silently defaulted them to None on every backfill run, and each
    # run quietly nulled out real, already-correct issue_type/status/parent_key in the vector store —
    # found live after running --include-attachments and then seeing query_issues(issue_types=['bug'])
    # return 0 for a space with 11 real bugs.
    query = """
        SELECT i.id, i.issue_key, i.space_id, i.title, i.description, i.created_at, i.updated_at,
               i.sprint_id, s.name AS sprint_name, i.issue_type, i.status, i.priority,
               i.parent_id, p.issue_key AS parent_key, p.title AS parent_title,
               -- Owner and size (see vectorization-service migrations/012). Same must-be-selected-here
               -- reasoning as issue_type/status above: upsert_issue's ON CONFLICT overwrites these
               -- columns unconditionally, so omitting them from this query would null out real values
               -- on every backfill run. LEFT JOIN so an unassigned issue keeps NULLs, not dropped.
               i.assignee_id, a.name AS assignee_name, i.story_points
        FROM issues i
        LEFT JOIN sprints s ON s.id = i.sprint_id
        LEFT JOIN issues p ON p.id = i.parent_id
        LEFT JOIN users a ON a.id = i.assignee_id
    """
    if space_ids:
        query += " WHERE i.space_id = ANY($1::bigint[])"
        return await jira_pool.fetch(query, space_ids)
    return await jira_pool.fetch(query)


async def _fetch_issue_sprints(jira_pool: asyncpg.Pool, space_ids: Optional[List[int]]):
    """Just each issue's current sprint membership (id + resolved name) from the authoritative
    source — the input to the fast, embed-free --issue-sprints-only correction."""
    query = """
        SELECT i.id, i.issue_key, i.space_id, i.sprint_id, s.name AS sprint_name
        FROM issues i
        LEFT JOIN sprints s ON s.id = i.sprint_id
    """
    if space_ids:
        query += " WHERE i.space_id = ANY($1::bigint[])"
        return await jira_pool.fetch(query, space_ids)
    return await jira_pool.fetch(query)


async def _fetch_issue_priority(jira_pool: asyncpg.Pool, space_ids: Optional[List[int]]):
    """Just each issue's current priority from the authoritative source — the input to the fast,
    embed-free --issue-priority-only correction.

    Why this correction was needed at all: priority is usually set once at issue creation and never
    changed again, so it has no field_change history row for jira-backend's history-stream self-heal
    to replay (confirmed live against real AtlasCart data — zero priority history rows exist for it).
    IssueIngestionMessage now carries priority on every upsert going forward (see that model's
    docstring), but issues ingested before that field existed still need this one-time correction —
    same shape as --issue-sprints-only, for the same underlying reason (a field added after the
    initial ingestion, not retroactively present on already-ingested rows).
    """
    query = "SELECT id, issue_key, space_id, priority FROM issues"
    if space_ids:
        query += " WHERE space_id = ANY($1::bigint[])"
        return await jira_pool.fetch(query, space_ids)
    return await jira_pool.fetch(query)


async def _fetch_comments(jira_pool: asyncpg.Pool, space_ids: Optional[List[int]]):
    query = """
        SELECT c.id, c.issue_id, i.issue_key, i.space_id, c.content, c.updated_at
        FROM comments c JOIN issues i ON i.id = c.issue_id
    """
    if space_ids:
        query += " WHERE i.space_id = ANY($1::bigint[])"
        return await jira_pool.fetch(query, space_ids)
    return await jira_pool.fetch(query)


async def _fetch_history(jira_pool: asyncpg.Pool, space_ids: Optional[List[int]]):
    """**A real, previously-unfilled gap, found live 2026-08-06**: this backfill script had a fetcher
    for every content type EXCEPT issue history — `handle_history` (app/ingest/pipeline.py) existed and
    worked, nothing ever called it here. Invisible for months because the live Kafka path
    (`jira.content.ingestion`) covers history going forward the same way it covers everything else; it
    only surfaced when a dataset reseed's raw-SQL writes (bypassing jira-backend's service layer
    entirely, so no Kafka events fire at all) left `issue_changes` at zero rows for the new space,
    which in turn made `/issues/history?reopened_only=true` — and therefore
    `sprint_recovery/graph.py`'s own `_gather_evidence` history evidence — silently return nothing.
    `actor_id` is joined to `users.name` (display name, matching jira-backend's own `IssueHistoryDto.
    actorName` convention) rather than `username`.
    """
    query = """
        SELECT h.id, h.issue_id, i.issue_key, i.space_id, h.event_type, h.field_name, h.from_value,
               h.to_value, h.description, h.created_at, u.name AS actor_name
        FROM issue_history h
        JOIN issues i ON i.id = h.issue_id
        LEFT JOIN users u ON u.id = h.actor_id
    """
    if space_ids:
        query += " WHERE i.space_id = ANY($1::bigint[])"
        return await jira_pool.fetch(query, space_ids)
    return await jira_pool.fetch(query)


async def _fetch_attachments(jira_pool: asyncpg.Pool, space_ids: Optional[List[int]]):
    query = """
        SELECT a.id, a.issue_id, i.issue_key, i.space_id, a.original_filename,
               a.storage_filename, a.content_type, a.size_bytes, a.created_at
        FROM issue_attachments a JOIN issues i ON i.id = a.issue_id
    """
    if space_ids:
        query += " WHERE i.space_id = ANY($1::bigint[])"
        return await jira_pool.fetch(query, space_ids)
    return await jira_pool.fetch(query)


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--space-ids", type=str, default="",
        help="Comma-separated jira-backend space ids to backfill (default: every space)",
    )
    parser.add_argument(
        "--truncate-first", action="store_true",
        help="Wipe the existing chunks table first (clears any prior eval/demo data before backfilling)",
    )
    parser.add_argument(
        "--include-attachments", action="store_true",
        help="Also backfill attachments: fetches binaries from S3/MinIO and parses via Docling "
             "(much slower than issues/comments — OCR model load alone takes several seconds, "
             "then real per-file conversion time on top).",
    )
    parser.add_argument(
        "--sprints-only", action="store_true",
        help="Backfill sprint metadata/goals only. Useful when sprint events were enabled after the "
             "existing sprint rows were created; avoids re-embedding every issue and comment.",
    )
    parser.add_argument(
        "--issue-sprints-only", action="store_true",
        help="Fast, embed-free correction of issues' current sprint membership (sprint_id/"
             "sprint_name) from the source DB. Use when issues were already ingested but landed with "
             "a NULL sprint; touches only those two columns, re-embeds nothing.",
    )
    parser.add_argument(
        "--history-only", action="store_true",
        help="Fast, embed-free backfill of just issue_changes (handle_history does no chunking/"
             "embedding by design — see _fetch_history's docstring for why this exists at all). Use "
             "when issues/comments were already ingested but history was never covered — e.g. after a "
             "raw-SQL dataset reseed that bypassed jira-backend's Kafka-publishing service layer.",
    )
    parser.add_argument(
        "--issue-priority-only", action="store_true",
        help="Fast, embed-free correction of issues' current priority from the source DB. Use when "
             "issues were already ingested before IssueIngestionMessage carried priority — most "
             "issues have it set once at creation with no field_change history row to self-heal "
             "from, so this is the only way to backfill the initial value; touches only that one "
             "column, re-embeds nothing.",
    )
    args = parser.parse_args()
    space_ids = [int(s) for s in args.space_ids.split(",") if s.strip()] or None

    jira_dsn = os.environ.get("JIRA_BACKEND_PG_DSN", "postgresql://poc:poc123@localhost:5432/pocdb")

    settings = Settings()
    print(f"embedder: provider={settings.embedding_provider} model={settings.embedding_model} "
          f"dim={settings.embedding_dim}")
    print(f"jira-backend source: {jira_dsn}  space_ids filter: {space_ids or '(all)'}")

    vec_pool = await create_pool(settings)
    jira_pool = await asyncpg.create_pool(jira_dsn, min_size=1, max_size=4)
    embedder = build_embedder(settings)
    store = VectorStore(vec_pool)
    # Was omitted here (defaulting IngestPipeline to a no-op context generator) even when
    # VEC_CONTEXTUAL_RETRIEVAL_ENABLED=true — the live Kafka path (app/main.py) already wires this up
    # correctly; the backfill script just never matched it, so every backfill-produced chunk silently
    # skipped contextualization regardless of the setting. Found live: re-ran the attachment backfill
    # expecting contextualized chunks and found byte-identical content to the pre-fix run.
    context_generator = build_context_generator(settings)
    # Same class of bug as the context_generator comment above, caught this time before it shipped:
    # this script builds its own IngestPipeline separately from app/main.py's, so any dependency added
    # there has to be matched here too or it silently no-ops for every backfill run.
    raw_vlm_describer = build_vlm_describer(settings)
    vlm_describer = (
        CachedVlmImageDescriber(raw_vlm_describer, store, vlm_cache_namespace(settings))
        if settings.vlm_ocr_enabled
        else raw_vlm_describer
    )
    pipeline = IngestPipeline(settings, embedder, store, context_generator, vlm_describer)

    try:
        if args.issue_sprints_only:
            rows = await _fetch_issue_sprints(jira_pool, space_ids)
            matched = assigned = 0
            for row in rows:
                # Structured-metadata-only fix: update just the two sprint columns, keyed on issue_id
                # (the issues table's PK), leaving type/status/title/timestamps and every embedded
                # chunk untouched. This is the same bare UPDATE the pipeline's own sprint self-heal
                # does (VectorStore.record_issue_change, field_name == "sprint"), so it's idempotent
                # and safe to re-run. Issues not present in the vec store are simply not matched.
                res = await vec_pool.execute(
                    "UPDATE issues SET sprint_id = $2, sprint_name = $3 WHERE issue_id = $1",
                    row["id"], row["sprint_id"], row["sprint_name"],
                )
                if res.rsplit(" ", 1)[-1] != "0":
                    matched += 1
                    if row["sprint_id"] is not None:
                        assigned += 1
            print(f"issue-sprints-only: matched {matched}/{len(rows)} issues in the vec store; "
                  f"{assigned} now have a sprint, the rest are backlog (NULL).")
            return

        if args.issue_priority_only:
            rows = await _fetch_issue_priority(jira_pool, space_ids)
            matched = has_priority = 0
            for row in rows:
                # Same shape as --issue-sprints-only above: a bare, idempotent UPDATE keyed on
                # issue_id, touching only the one column. Safe to re-run.
                res = await vec_pool.execute(
                    "UPDATE issues SET priority = $2 WHERE issue_id = $1",
                    row["id"], row["priority"],
                )
                if res.rsplit(" ", 1)[-1] != "0":
                    matched += 1
                    if row["priority"] is not None:
                        has_priority += 1
            print(f"issue-priority-only: matched {matched}/{len(rows)} issues in the vec store; "
                  f"{has_priority} now have a priority set.")
            return

        if args.history_only:
            rows = await _fetch_history(jira_pool, space_ids)
            for row in rows:
                msg = IssueHistoryIngestionMessage(
                    event_id=str(uuid.uuid4()),
                    event_type="issue_history_added",
                    emitted_at=row["created_at"] or datetime.now(timezone.utc),
                    history_id=row["id"],
                    issue_id=row["issue_id"],
                    issue_key=row["issue_key"],
                    space_id=row["space_id"],
                    change_event_type=row["event_type"],
                    field_name=row["field_name"],
                    from_value=row["from_value"],
                    to_value=row["to_value"],
                    description=row["description"],
                    actor_name=row["actor_name"],
                )
                await pipeline.handle_history(msg)
            print(f"history-only: backfilled {len(rows)} issue_changes rows.")
            return

        if args.truncate_first:
            await vec_pool.execute("TRUNCATE chunks")
            print("cleared existing chunks table\n")

        sprints = await _fetch_sprints(jira_pool, space_ids)
        print(f"backfilling {len(sprints)} sprints...")
        for row in sprints:
            msg = SprintIngestionMessage(
                event_id=str(uuid.uuid4()),
                event_type="sprint_upserted",
                emitted_at=row["updated_at"] or datetime.now(timezone.utc),
                sprint_id=row["id"],
                sprint_name=row["name"],
                space_id=row["space_id"],
                goal=row["goal"],
                start_date=row["start_date"],
                end_date=row["end_date"],
                status=row["status"],
                initial_committed_points=row["initial_committed_points"],
                initial_completed_points=row["initial_completed_points"],
                final_scope_points=row["final_scope_points"],
                completed_points=row["completed_points"],
                initial_issue_count=row["initial_issue_count"],
                completed_issue_count=row["completed_issue_count"],
                final_issue_count=row["final_issue_count"],
                unestimated_issue_count=row["unestimated_issue_count"],
            )
            await pipeline.handle_sprint(msg)

        if not args.sprints_only:
            issues = await _fetch_issues(jira_pool, space_ids)
            print(f"backfilling {len(issues)} issues...")
            for row in issues:
                msg = IssueIngestionMessage(
                    event_id=str(uuid.uuid4()),
                    event_type="issue_upserted",
                    emitted_at=row["updated_at"] or datetime.now(timezone.utc),
                    issue_id=row["id"],
                    issue_key=row["issue_key"],
                    space_id=row["space_id"],
                    title=row["title"],
                    description=row["description"],
                    created_at=row["created_at"],
                    updated_at=row["updated_at"],
                    sprint_id=row["sprint_id"],
                    sprint_name=row["sprint_name"],
                    issue_type=row["issue_type"],
                    status=row["status"],
                    priority=row["priority"],
                    parent_issue_id=row["parent_id"],
                    parent_key=row["parent_key"],
                    parent_title=row["parent_title"],
                    assignee_id=row["assignee_id"],
                    assignee_name=row["assignee_name"],
                    story_points=row["story_points"],
                )
                await pipeline.handle_issue(msg)

            comments = await _fetch_comments(jira_pool, space_ids)
            print(f"backfilling {len(comments)} comments...")
            for row in comments:
                msg = CommentIngestionMessage(
                    event_id=str(uuid.uuid4()),
                    event_type="comment_upserted",
                    emitted_at=row["updated_at"] or datetime.now(timezone.utc),
                    comment_id=row["id"],
                    issue_id=row["issue_id"],
                    issue_key=row["issue_key"],
                    space_id=row["space_id"],
                    content=row["content"],
                )
                await pipeline.handle_comment(msg)

            history = await _fetch_history(jira_pool, space_ids)
            print(f"backfilling {len(history)} issue_changes...")
            for row in history:
                msg = IssueHistoryIngestionMessage(
                    event_id=str(uuid.uuid4()),
                    event_type="issue_history_added",
                    emitted_at=row["created_at"] or datetime.now(timezone.utc),
                    history_id=row["id"],
                    issue_id=row["issue_id"],
                    issue_key=row["issue_key"],
                    space_id=row["space_id"],
                    change_event_type=row["event_type"],
                    field_name=row["field_name"],
                    from_value=row["from_value"],
                    to_value=row["to_value"],
                    description=row["description"],
                    actor_name=row["actor_name"],
                )
                await pipeline.handle_history(msg)

        if args.include_attachments and not args.sprints_only:
            attachment_bucket = os.environ.get("JIRA_ATTACHMENT_BUCKET", "jira-attachments")
            attachments = await _fetch_attachments(jira_pool, space_ids)
            print(f"backfilling {len(attachments)} attachments (bucket={attachment_bucket})...")
            for i, row in enumerate(attachments, 1):
                msg = AttachmentIngestionMessage(
                    event_id=str(uuid.uuid4()),
                    event_type="attachment_uploaded",
                    emitted_at=row["created_at"] or datetime.now(timezone.utc),
                    attachment_id=row["id"],
                    issue_id=row["issue_id"],
                    issue_key=row["issue_key"],
                    space_id=row["space_id"],
                    filename=row["original_filename"],
                    mime_type=row["content_type"],
                    byte_size=row["size_bytes"],
                    storage_backend="s3",
                    bucket=attachment_bucket,
                    storage_key=row["storage_filename"],
                )
                await pipeline.handle_attachment(msg)
                if i % 10 == 0 or i == len(attachments):
                    print(f"  ...{i}/{len(attachments)}")

        print(f"\ndone -> {await store.count()} chunks now in the vector store")
    finally:
        await embedder.aclose()
        await context_generator.aclose()
        await vlm_describer.aclose()
        await vec_pool.close()
        await jira_pool.close()


if __name__ == "__main__":
    asyncio.run(main())
