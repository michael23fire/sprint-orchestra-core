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

from typing import Iterable, List, Optional, Tuple

import psycopg

_SCHEMA = "planning_workflows"
_TABLE = f"{_SCHEMA}.sprint_recovery_active_threads"


async def _ensure_schema(conn) -> None:
    """`CREATE TABLE ... IF NOT EXISTS` still fails outright if the *schema* is missing (verified:
    Postgres raises `InvalidSchemaName`), and until now the only thing that created it was
    `app/planning/checkpoint.py::build_checkpointer`. That made every table here quietly dependent on
    the checkpointer having been built first — true in the running app, but not true for a test that
    talks to a fresh database directly, which is exactly the shape CI runs (a clean `postgres:16`
    service container). Making the setup self-sufficient removes the ordering dependency rather than
    relying on it holding.
    """
    await conn.execute(f"CREATE SCHEMA IF NOT EXISTS {_SCHEMA}")


async def ensure_active_threads_table(db_url: str) -> None:
    async with await psycopg.AsyncConnection.connect(db_url, autocommit=True) as conn:
        await _ensure_schema(conn)
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


# --- Threads parked at `wait_for_reevaluation`, for the Kafka trigger -----------------------------
#
# **Found live, and the last remaining durability hole in a workflow whose entire premise is durable
# execution**: `SprintRecoveryKafkaTrigger._waiting_by_sprint` was an in-process dict. A sprint parked
# at `wait_for_reevaluation` — waiting for a real Jira change to wake it up — was only reachable by
# events for as long as the *same* ai-service process stayed alive. Restart it (deploy, crash, OOM)
# and the Postgres checkpoint was still perfectly intact and resumable, but nothing would ever
# resume it automatically again: the index saying "this sprint is waiting" lived only in RAM. The
# workflow didn't break, it went permanently deaf, which is worse — nothing surfaces as an error.
# This table is that index, made durable and reloaded at startup.
_WAITING_TABLE = f"{_SCHEMA}.sprint_recovery_waiting_threads"


async def ensure_waiting_threads_table(db_url: str) -> None:
    async with await psycopg.AsyncConnection.connect(db_url, autocommit=True) as conn:
        await _ensure_schema(conn)
        await conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {_WAITING_TABLE} (
                thread_id TEXT PRIMARY KEY,
                space_id BIGINT NOT NULL,
                sprint_id BIGINT NOT NULL,
                -- The issues this specific thread cares about, for the `move_out_of_sprint`
                -- self-trigger case where the event's own sprintId is already null. See
                -- kafka_trigger.py's docstring.
                tracked_issue_keys TEXT[] NOT NULL DEFAULT '{{}}',
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )


async def register_waiting_thread(
    db_url: str, space_id: int, sprint_id: int, thread_id: str, tracked_issue_keys: Iterable[str],
) -> None:
    """Keyed by thread_id, not by sprint: one sprint can legitimately have had several threads over
    time, and only the one actually parked right now should be woken."""
    async with await psycopg.AsyncConnection.connect(db_url, autocommit=True) as conn:
        await conn.execute(
            f"""
            INSERT INTO {_WAITING_TABLE} (thread_id, space_id, sprint_id, tracked_issue_keys, updated_at)
            VALUES (%s, %s, %s, %s, now())
            ON CONFLICT (thread_id) DO UPDATE SET
                space_id = EXCLUDED.space_id, sprint_id = EXCLUDED.sprint_id,
                tracked_issue_keys = EXCLUDED.tracked_issue_keys, updated_at = now()
            """,
            (thread_id, space_id, sprint_id, sorted(set(tracked_issue_keys))),
        )


async def clear_waiting_thread(db_url: str, thread_id: str) -> None:
    """Called once the pause has actually been consumed. Deleting rather than flagging: a row here
    means exactly "still parked, still worth waking", and a stale row would cause a pointless
    `trigger_reevaluation` on a thread that has already moved on."""
    async with await psycopg.AsyncConnection.connect(db_url, autocommit=True) as conn:
        await conn.execute(f"DELETE FROM {_WAITING_TABLE} WHERE thread_id = %s", (thread_id,))


async def load_waiting_threads(db_url: str) -> List[Tuple[int, int, str, List[str]]]:
    """Every still-parked thread, as `(space_id, sprint_id, thread_id, tracked_issue_keys)` — read once
    at startup to rebuild the trigger's in-memory index."""
    async with await psycopg.AsyncConnection.connect(db_url, autocommit=True) as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"SELECT space_id, sprint_id, thread_id, tracked_issue_keys FROM {_WAITING_TABLE}"
            )
            return [(r[0], r[1], r[2], list(r[3] or [])) for r in await cur.fetchall()]
