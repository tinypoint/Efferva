from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal
from uuid import UUID, uuid4

from efferva.db import Database
from efferva.identity import Capability, ForbiddenError, Principal


class NotFoundError(LookupError):
    pass


class ConflictError(RuntimeError):
    pass


class AccessMode(StrEnum):
    READ = "read"
    WRITE = "write"


class AuthorizedRepository:
    """Principal-scoped access to Efferva-owned Session metadata."""

    def __init__(
        self,
        database: Database,
        principal: Principal,
        *,
        codex_version: str,
        codex_runtime_sha256: str,
    ) -> None:
        self._database = database
        self.principal = principal
        self._codex_version = codex_version
        self._codex_runtime_sha256 = codex_runtime_sha256

    def _scope(
        self,
        mode: AccessMode,
        *,
        alias: str = "s",
    ) -> tuple[str, tuple[str, ...]]:
        capability = {
            AccessMode.READ: Capability.SESSIONS_READ_TENANT,
            AccessMode.WRITE: Capability.SESSIONS_WRITE_TENANT,
        }[mode]
        if self.principal.has(capability):
            return f"{alias}.tenant_id = %s", (self.principal.tenant_id,)
        return (
            f"{alias}.tenant_id = %s "
            f"AND {alias}.owner_issuer = %s "
            f"AND {alias}.owner_subject = %s",
            (
                self.principal.tenant_id,
                self.principal.issuer,
                self.principal.subject,
            ),
        )

    async def create_session(self, name: str) -> dict[str, Any]:
        session_id = uuid4()
        async with self._database.connection() as connection:
            cursor = await connection.execute(
                """
                INSERT INTO app_sessions(
                    id, tenant_id, owner_issuer, owner_subject, name,
                    codex_version, codex_runtime_sha256, last_active_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, now())
                RETURNING *
                """,
                (
                    session_id,
                    self.principal.tenant_id,
                    self.principal.issuer,
                    self.principal.subject,
                    name,
                    self._codex_version,
                    self._codex_runtime_sha256,
                ),
            )
            row = await cursor.fetchone()
            await connection.commit()
        return row

    async def list_sessions(
        self,
        scope: Literal["mine", "tenant"] = "mine",
    ) -> list[dict[str, Any]]:
        if scope == "tenant":
            if not self.principal.has(Capability.SESSIONS_READ_TENANT):
                raise ForbiddenError("tenant Session visibility is not allowed")
            where = "tenant_id = %s"
            parameters = (self.principal.tenant_id,)
        else:
            where = "tenant_id = %s AND owner_issuer = %s AND owner_subject = %s"
            parameters = (
                self.principal.tenant_id,
                self.principal.issuer,
                self.principal.subject,
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

    async def get_session(
        self,
        session_id: UUID,
        *,
        mode: AccessMode = AccessMode.READ,
        touch: bool = False,
    ) -> dict[str, Any]:
        where, parameters = self._scope(mode)
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


class SystemRepository:
    """System entry point for health checks and Principal-scoped Session access."""

    def __init__(
        self,
        database: Database,
        *,
        codex_version: str,
        codex_runtime_sha256: str,
    ) -> None:
        self._database = database
        self._codex_version = codex_version
        self._codex_runtime_sha256 = codex_runtime_sha256

    def for_principal(self, principal: Principal) -> AuthorizedRepository:
        return AuthorizedRepository(
            self._database,
            principal,
            codex_version=self._codex_version,
            codex_runtime_sha256=self._codex_runtime_sha256,
        )

    async def ping(self) -> None:
        async with self._database.connection() as connection:
            await connection.execute("SELECT 1")
