"""Durable lookup index for the latest in-progress Plan Epic workflow per user and space.

LangGraph checkpoints are addressed by ``thread_id``.  That is enough to resume a workflow when the
browser still has the id, but not after the Plan Epic modal is closed or the page is reloaded.  This
small side table supplies the missing product-level lookup: "does this user have an unfinished Plan
Epic workflow in this space?"  It is deliberately separate from Sprint Recovery's index because the
two features have different natural keys and lifecycles.
"""
from __future__ import annotations

from typing import Optional

import psycopg

_SCHEMA = "planning_workflows"
_TABLE = f"{_SCHEMA}.plan_epic_active_threads"


async def ensure_plan_epic_active_threads_table(db_url: str) -> None:
    async with await psycopg.AsyncConnection.connect(db_url, autocommit=True) as conn:
        await conn.execute(f"CREATE SCHEMA IF NOT EXISTS {_SCHEMA}")
        await conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {_TABLE} (
                space_id BIGINT NOT NULL,
                user_id TEXT NOT NULL,
                thread_id TEXT NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                PRIMARY KEY (space_id, user_id)
            )
            """
        )


async def register_plan_epic_active_thread(
    db_url: str, space_id: int, user_id: str, thread_id: str,
) -> None:
    """Point this user's space-level lookup at the newly-created workflow."""
    async with await psycopg.AsyncConnection.connect(db_url, autocommit=True) as conn:
        await conn.execute(
            f"""
            INSERT INTO {_TABLE} (space_id, user_id, thread_id, updated_at)
            VALUES (%s, %s, %s, now())
            ON CONFLICT (space_id, user_id) DO UPDATE SET
                thread_id = EXCLUDED.thread_id,
                updated_at = now()
            """,
            (space_id, user_id, thread_id),
        )


async def find_plan_epic_active_thread_id(
    db_url: str, space_id: int, user_id: str,
) -> Optional[str]:
    async with await psycopg.AsyncConnection.connect(db_url, autocommit=True) as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"SELECT thread_id FROM {_TABLE} WHERE space_id = %s AND user_id = %s",
                (space_id, user_id),
            )
            row = await cur.fetchone()
            return row[0] if row else None
