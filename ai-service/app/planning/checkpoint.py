"""Postgres-backed checkpointer setup for the epic-rollout workflow (app/planning/rollout_graph.py).

Isolated into a dedicated `planning_workflows` schema inside jira-backend's existing `postgres`
container (see Settings.epic_rollout_checkpoint_db_url) — this is workflow-lifecycle bookkeeping, not
product data, and does not belong alongside jira-backend's own Flyway-managed tables.
"""
from __future__ import annotations

from contextlib import AsyncExitStack
from urllib.parse import quote

import psycopg
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

_SCHEMA = "planning_workflows"


async def _ensure_schema(db_url: str) -> None:
    # A plain connection with no search_path override — `CREATE SCHEMA` has to run somewhere
    # unambiguous, before the checkpointer's own connection (below) can be pointed *into* that schema.
    async with await psycopg.AsyncConnection.connect(db_url, autocommit=True) as conn:
        await conn.execute(f"CREATE SCHEMA IF NOT EXISTS {_SCHEMA}")


async def build_checkpointer(db_url: str, exit_stack: AsyncExitStack) -> AsyncPostgresSaver:
    """Creates the schema (idempotent) and the checkpoint tables inside it (`setup()`, also
    idempotent), then hands back a saver bound for the process lifetime via `exit_stack` — the caller
    (app/main.py's lifespan) owns closing it, the same shape every other resource in that function
    already uses.
    """
    await _ensure_schema(db_url)
    scoped_url = f"{db_url}?options=-csearch_path%3D{quote(_SCHEMA)}"
    saver = await exit_stack.enter_async_context(AsyncPostgresSaver.from_conn_string(scoped_url))
    await saver.setup()
    return saver
