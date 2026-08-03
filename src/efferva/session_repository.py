from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal
from uuid import UUID, uuid4

from efferva.db import Database
from efferva.identity import Capability, ForbiddenError, Principal


class NotFoundError(LookupError):
    pass


class AccessMode(StrEnum):
    READ = "read"
    WRITE = "write"


class SessionRepository:
    """PostgreSQL access for Principal-scoped Session metadata."""

    def __init__(self, database: Database) -> None:
        self._database = database

    @staticmethod
    def _scope(
        principal: Principal,
        mode: AccessMode,
        *,
        alias: str = "s",
    ) -> tuple[str, tuple[str, ...]]:
        capability = {
            AccessMode.READ: Capability.SESSIONS_READ_TENANT,
            AccessMode.WRITE: Capability.SESSIONS_WRITE_TENANT,
        }[mode]
        if principal.has(capability):
            return f"{alias}.tenant_id = %s", (principal.tenant_id,)
        return (
            f"{alias}.tenant_id = %s "
            f"AND {alias}.owner_issuer = %s "
            f"AND {alias}.owner_subject = %s",
            (principal.tenant_id, principal.issuer, principal.subject),
        )

    async def ping(self) -> None:
        async with self._database.connection() as connection:
            await connection.execute("SELECT 1")

    async def create_session(
        self,
        principal: Principal,
        name: str,
    ) -> dict[str, Any]:
        session_id = uuid4()
        async with self._database.connection() as connection:
            cursor = await connection.execute(
                """
                INSERT INTO app_sessions(
                    id, tenant_id, owner_issuer, owner_subject, name, last_active_at
                )
                VALUES (%s, %s, %s, %s, %s, now())
                RETURNING *
                """,
                (
                    session_id,
                    principal.tenant_id,
                    principal.issuer,
                    principal.subject,
                    name,
                ),
            )
            row = await cursor.fetchone()
            await connection.commit()
        return row

    async def list_sessions(
        self,
        principal: Principal,
        scope: Literal["mine", "tenant"] = "mine",
    ) -> list[dict[str, Any]]:
        if scope == "tenant":
            if not principal.has(Capability.SESSIONS_READ_TENANT):
                raise ForbiddenError("tenant Session visibility is not allowed")
            where = "tenant_id = %s"
            parameters = (principal.tenant_id,)
        else:
            where = "tenant_id = %s AND owner_issuer = %s AND owner_subject = %s"
            parameters = (
                principal.tenant_id,
                principal.issuer,
                principal.subject,
            )
        async with self._database.connection() as connection:
            cursor = await connection.execute(
                f"""
                SELECT * FROM app_sessions
                WHERE {where}
                ORDER BY last_active_at DESC, created_at DESC
                """,
                parameters,
            )
            return list(await cursor.fetchall())

    async def list_owner_sessions(
        self,
        *,
        tenant_id: str,
        owner_issuer: str,
        owner_subject: str,
    ) -> list[dict[str, Any]]:
        async with self._database.connection() as connection:
            cursor = await connection.execute(
                """
                SELECT * FROM app_sessions
                WHERE tenant_id = %s
                  AND owner_issuer = %s
                  AND owner_subject = %s
                ORDER BY last_active_at DESC, created_at DESC
                """,
                (tenant_id, owner_issuer, owner_subject),
            )
            return list(await cursor.fetchall())

    async def get_session(
        self,
        principal: Principal,
        session_id: UUID,
        *,
        mode: AccessMode = AccessMode.READ,
        touch: bool = False,
    ) -> dict[str, Any]:
        where, parameters = self._scope(principal, mode)
        async with self._database.connection() as connection:
            cursor = await connection.execute(
                f"SELECT * FROM app_sessions s WHERE s.id = %s AND {where}",
                (session_id, *parameters),
            )
            row = await cursor.fetchone()
            if row is not None and touch:
                await connection.execute(
                    """
                    UPDATE app_sessions
                    SET last_active_at = now(), updated_at = now()
                    WHERE id = %s
                    """,
                    (session_id,),
                )
                await connection.commit()
        if row is None:
            raise NotFoundError(f"session {session_id} not found")
        return row
