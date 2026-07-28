from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
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

    async def migrate(self, migrations: Sequence[tuple[str, str]]) -> None:
        async with self.connection() as connection:
            await connection.execute(
                "SELECT pg_advisory_xact_lock(hashtext('agentframe:schema-migrations'))"
            )
            await connection.execute(
                """
                CREATE TABLE IF NOT EXISTS agentframe_schema_migrations (
                    version text PRIMARY KEY,
                    applied_at timestamptz NOT NULL DEFAULT now()
                )
                """
            )
            for version, sql in migrations:
                existing = await connection.execute(
                    "SELECT 1 FROM agentframe_schema_migrations WHERE version = %s",
                    (version,),
                )
                if await existing.fetchone():
                    continue
                await connection.execute(sql)
                await connection.execute(
                    "INSERT INTO agentframe_schema_migrations(version) VALUES (%s)",
                    (version,),
                )
            await connection.commit()
