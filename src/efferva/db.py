from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool


class Database:
    def __init__(self, database_url: str) -> None:
        self._pool = AsyncConnectionPool(
            conninfo=database_url,
            min_size=1,
            max_size=20,
            open=False,
            kwargs={"autocommit": False, "row_factory": dict_row},
        )

    async def open(self) -> None:
        await self._pool.open()
        await self._pool.wait()

    async def close(self) -> None:
        await self._pool.close()

    @asynccontextmanager
    async def connection(self) -> AsyncIterator[AsyncConnection]:
        async with self._pool.connection() as connection:
            yield connection

    async def initialize(self, schema: str) -> None:
        async with self.connection() as connection:
            await connection.execute(
                "SELECT pg_advisory_xact_lock(hashtext('efferva:schema-initialization'))"
            )
            await connection.execute(schema)
            await connection.commit()
