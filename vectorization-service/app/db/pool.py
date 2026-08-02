"""asyncpg connection pool lifecycle, registered with pgvector's type codec."""
from __future__ import annotations

import asyncpg
from pgvector.asyncpg import register_vector

from app.config import Settings


async def _init_connection(conn: asyncpg.Connection) -> None:
    # Teach asyncpg how to encode/decode the pgvector `vector` type on every pooled connection.
    await register_vector(conn)


async def create_pool(settings: Settings) -> asyncpg.Pool:
    return await asyncpg.create_pool(
        dsn=settings.pg_dsn,
        min_size=settings.pg_pool_min_size,
        max_size=settings.pg_pool_max_size,
        init=_init_connection,
    )
