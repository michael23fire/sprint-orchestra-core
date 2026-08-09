"""Tracks the most recent sprint-recovery thread per (space_id, sprint_id) so the UI can find its way
back to an in-progress workflow, not just resume one it already has the thread_id for.

**Found live, from a direct product question**: crash-resume at the graph/API level was verified
correct — same thread_id, `/retry` picks up exactly where a killed process left off. But the *frontend*
had no way to discover that thread_id in the first place. Closing the "AI recovery" modal (or reloading
the page, or a real process crash) loses `thread_id` from browser memory, and every route under this
router requires it. `handleStart` always calls `POST /start`, which always mints a fresh
`uuid.uuid4()` thread — with no lookup path, "resume" only worked if you already knew the exact
thread_id, e.g. from server logs. The checkpoint was never actually lost; nothing could find it again.

LangGraph's own checkpoint tables are keyed by thread_id only — no `space_id`/`sprint_id` columns to
query against (the state that would let you filter is inside an opaque, per-checkpoint blob). This is
a small, separate side table for exactly the one query the checkpointer doesn't support:
"what's the current thread for this sprint." Same schema, same raw-psycopg pattern
`app/planning/checkpoint.py` already uses for schema setup outside the checkpointer's own tables.
"""
from __future__ import annotations

from typing import Optional

import psycopg

_SCHEMA = "planning_workflows"
_TABLE = f"{_SCHEMA}.sprint_recovery_active_threads"


async def ensure_active_threads_table(db_url: str) -> None:
    async with await psycopg.AsyncConnection.connect(db_url, autocommit=True) as conn:
        await conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {_TABLE} (
                space_id BIGINT NOT NULL,
                sprint_id BIGINT NOT NULL,
                thread_id TEXT NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                PRIMARY KEY (space_id, sprint_id)
            )
            """
        )


async def register_active_thread(db_url: str, space_id: int, sprint_id: int, thread_id: str) -> None:
    """Called once, right when a new thread is minted (`/start`). One row per sprint — a new check
    replaces the pointer to whatever thread was there before, same as how only the latest checkpoint
    of a thread itself matters, not every one that ever existed.
    """
    async with await psycopg.AsyncConnection.connect(db_url, autocommit=True) as conn:
        await conn.execute(
            f"""
            INSERT INTO {_TABLE} (space_id, sprint_id, thread_id, updated_at)
            VALUES (%s, %s, %s, now())
            ON CONFLICT (space_id, sprint_id) DO UPDATE SET thread_id = EXCLUDED.thread_id, updated_at = now()
            """,
            (space_id, sprint_id, thread_id),
        )


async def find_active_thread_id(db_url: str, space_id: int, sprint_id: int) -> Optional[str]:
    async with await psycopg.AsyncConnection.connect(db_url, autocommit=True) as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"SELECT thread_id FROM {_TABLE} WHERE space_id = %s AND sprint_id = %s",
                (space_id, sprint_id),
            )
            row = await cur.fetchone()
            return row[0] if row else None
