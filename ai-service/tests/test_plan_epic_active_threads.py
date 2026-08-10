"""Tests for the Plan Epic discovery index used after modal close or browser reload."""
import uuid

import pytest

from app.planning.active_threads import (
    ensure_plan_epic_active_threads_table,
    find_plan_epic_active_thread_id,
    register_plan_epic_active_thread,
)

DB_URL = "postgresql://poc:poc123@localhost:5432/pocdb"


async def _skip_if_postgres_unreachable() -> None:
    psycopg = pytest.importorskip("psycopg")
    try:
        conn = await psycopg.AsyncConnection.connect(DB_URL, connect_timeout=2)
        await conn.close()
    except Exception:
        pytest.skip("dev Postgres (poc-postgres) not reachable")


async def test_register_and_find_are_scoped_by_space_and_user():
    await _skip_if_postgres_unreachable()
    await ensure_plan_epic_active_threads_table(DB_URL)
    seed = uuid.uuid4().int % 90_000_000
    space_id = 900_000_000 + seed
    thread_id = str(uuid.uuid4())

    await register_plan_epic_active_thread(DB_URL, space_id, "user-a", thread_id)

    assert await find_plan_epic_active_thread_id(DB_URL, space_id, "user-a") == thread_id
    assert await find_plan_epic_active_thread_id(DB_URL, space_id, "user-b") is None
    assert await find_plan_epic_active_thread_id(DB_URL, space_id + 1, "user-a") is None


async def test_newer_workflow_replaces_only_the_same_users_space_pointer():
    await _skip_if_postgres_unreachable()
    await ensure_plan_epic_active_threads_table(DB_URL)
    space_id = 900_000_000 + (uuid.uuid4().int % 90_000_000)
    first, second = str(uuid.uuid4()), str(uuid.uuid4())

    await register_plan_epic_active_thread(DB_URL, space_id, "user-a", first)
    await register_plan_epic_active_thread(DB_URL, space_id, "user-a", second)

    assert await find_plan_epic_active_thread_id(DB_URL, space_id, "user-a") == second
