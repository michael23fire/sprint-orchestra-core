"""Tests for app/sprint_recovery/active_threads.py — real Postgres, same gating pattern as the
crash-resume tests in test_sprint_recovery_graph.py (skips if the dev Postgres isn't reachable).

**Found live**: crash-resume was correct at the graph/checkpointer level (same thread_id, /retry picks
up exactly where a killed process left off) but the UI had no way to discover that thread_id again once
lost from browser memory — every route requires already knowing it, and starting fresh always minted a
new one. This module is the one small side table that makes an in-progress thread findable by
(space_id, sprint_id) instead of only by a thread_id someone already has.
"""
import uuid

import pytest

from app.sprint_recovery.active_threads import (
    ensure_active_threads_table,
    find_active_thread_id,
    register_active_thread,
)

DB_URL = "postgresql://poc:poc123@localhost:5432/pocdb"


async def _skip_if_postgres_unreachable():
    psycopg = pytest.importorskip("psycopg")
    try:
        conn = await psycopg.AsyncConnection.connect(DB_URL, connect_timeout=2)
        await conn.close()
    except Exception:
        pytest.skip("dev Postgres (poc-postgres) not reachable")


def _fresh_ids() -> tuple[int, int]:
    # A fixed (space_id, sprint_id) pair collides with whatever a *previous* run of this same test
    # left behind — found live, re-running the suite after an earlier standalone run failed the
    # "should start as None" assertion on stale data. This table has no test-only reset, so uniqueness
    # per run is the idempotent-by-construction fix, same reasoning the fixture seed script uses.
    n = uuid.uuid4().int
    return (900_000_000 + (n % 90_000_000), 900_000_000 + ((n >> 32) % 90_000_000))


async def test_register_then_find_round_trips_to_the_same_thread_id():
    await _skip_if_postgres_unreachable()
    await ensure_active_threads_table(DB_URL)
    space_id, sprint_id = _fresh_ids()
    thread_id = str(uuid.uuid4())

    assert await find_active_thread_id(DB_URL, space_id, sprint_id) is None

    await register_active_thread(DB_URL, space_id, sprint_id, thread_id)
    assert await find_active_thread_id(DB_URL, space_id, sprint_id) == thread_id


async def test_a_second_registration_for_the_same_sprint_replaces_the_pointer():
    """One row per sprint — a new check replaces whatever thread was there before, the same way only
    the latest checkpoint of a thread itself matters, not every one that ever existed."""
    await _skip_if_postgres_unreachable()
    await ensure_active_threads_table(DB_URL)
    space_id, sprint_id = _fresh_ids()
    first_thread, second_thread = str(uuid.uuid4()), str(uuid.uuid4())

    await register_active_thread(DB_URL, space_id, sprint_id, first_thread)
    await register_active_thread(DB_URL, space_id, sprint_id, second_thread)

    assert await find_active_thread_id(DB_URL, space_id, sprint_id) == second_thread
