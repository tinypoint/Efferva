from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb

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
    """Principal-scoped access to Efferva-owned Session routing metadata."""

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
                    workspace_ref, codex_version, codex_runtime_sha256,
                    last_active_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, now())
                RETURNING *
                """,
                (
                    session_id,
                    self.principal.tenant_id,
                    self.principal.issuer,
                    self.principal.subject,
                    name,
                    f"session-{session_id}",
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
                await connection.execute(
                    """
                    UPDATE sandbox_leases
                    SET status = 'ready', updated_at = now()
                    WHERE workspace_id = %s
                    """,
                    (session_id,),
                )
                await connection.commit()
        if row is None:
            raise NotFoundError(f"session {session_id} not found")
        return row


class SystemRepository:
    """System access for Session routing and Sandbox lifecycle only."""

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

    async def upsert_workspace_binding(
        self,
        session_id: UUID,
        *,
        workspace_id: UUID,
        provider: str,
        external_ref: str,
        state: dict[str, Any],
        status: str,
    ) -> dict[str, Any]:
        async with self._database.connection() as connection:
            cursor = await connection.execute(
                """
                INSERT INTO workspace_bindings(
                    workspace_id, session_id, provider, external_ref,
                    state_json, status
                )
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (session_id) DO UPDATE
                SET provider = EXCLUDED.provider,
                    external_ref = EXCLUDED.external_ref,
                    state_json = EXCLUDED.state_json,
                    status = EXCLUDED.status,
                    updated_at = now()
                RETURNING *
                """,
                (
                    workspace_id,
                    session_id,
                    provider,
                    external_ref,
                    Jsonb(state),
                    status,
                ),
            )
            row = await cursor.fetchone()
            await connection.commit()
        return row

    async def upsert_sandbox_binding(
        self,
        *,
        workspace_id: UUID,
        provider: str,
        external_ref: str,
        state: dict[str, Any],
    ) -> dict[str, Any]:
        async with self._database.connection() as connection:
            cursor = await connection.execute(
                """
                INSERT INTO sandbox_leases(
                    sandbox_id, workspace_id, provider, external_ref,
                    state_json, owner_id, status, fencing_token, expires_at
                )
                VALUES (%s, %s, %s, %s, %s, NULL, 'ready', 0, now())
                ON CONFLICT (workspace_id) DO UPDATE
                SET provider = EXCLUDED.provider,
                    external_ref = EXCLUDED.external_ref,
                    state_json = EXCLUDED.state_json,
                    owner_id = NULL,
                    status = 'ready',
                    fencing_token = 0,
                    expires_at = now(),
                    updated_at = now()
                RETURNING *
                """,
                (
                    workspace_id,
                    workspace_id,
                    provider,
                    external_ref,
                    Jsonb(state),
                ),
            )
            row = await cursor.fetchone()
            await connection.commit()
        return row

    async def claim_idle_sandboxes(self, idle_timeout_seconds: int) -> list[dict[str, Any]]:
        async with self._database.connection() as connection:
            cursor = await connection.execute(
                """
                WITH idle AS (
                    SELECT sandbox.workspace_id
                    FROM sandbox_leases sandbox
                    JOIN workspace_bindings workspace
                      ON workspace.workspace_id = sandbox.workspace_id
                    JOIN app_sessions session ON session.id = workspace.session_id
                    WHERE sandbox.status = 'ready'
                      AND session.last_active_at
                          < now() - make_interval(secs => %s)
                    FOR UPDATE OF sandbox SKIP LOCKED
                )
                UPDATE sandbox_leases sandbox
                SET status = 'reaping', updated_at = now()
                FROM idle
                WHERE sandbox.workspace_id = idle.workspace_id
                RETURNING sandbox.*
                """,
                (idle_timeout_seconds,),
            )
            rows = list(await cursor.fetchall())
            await connection.commit()
        return rows

    async def finish_sandbox_reap(
        self,
        workspace_id: UUID,
        external_ref: str,
        *,
        succeeded: bool,
    ) -> None:
        async with self._database.connection() as connection:
            if succeeded:
                await connection.execute(
                    """
                    DELETE FROM sandbox_leases
                    WHERE workspace_id = %s AND external_ref = %s
                    """,
                    (workspace_id, external_ref),
                )
            else:
                await connection.execute(
                    """
                    UPDATE sandbox_leases
                    SET status = 'ready', updated_at = now()
                    WHERE workspace_id = %s AND external_ref = %s
                    """,
                    (workspace_id, external_ref),
                )
            await connection.commit()
