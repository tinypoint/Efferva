from __future__ import annotations

import json
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
                    id, tenant_id, owner_issuer, owner_subject, name,
                    codex_version, codex_runtime_sha256, last_active_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, now())
                RETURNING *
                """,
                (
                    session_id,
                    principal.tenant_id,
                    principal.issuer,
                    principal.subject,
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


class ExecutionSettingsRepository:
    def __init__(self, database: Database) -> None:
        self._database = database

    async def get_session(self, session_id: UUID) -> dict[str, str | None]:
        async with self._database.connection() as connection:
            cursor = await connection.execute(
                """
                SELECT default_model, default_reasoning_effort
                FROM app_sessions
                WHERE id = %s
                """,
                (session_id,),
            )
            row = await cursor.fetchone()
        if row is None:
            raise NotFoundError(f"session {session_id} not found")
        return {
            "model": row["default_model"],
            "reasoning_effort": row["default_reasoning_effort"],
        }

    async def set_session(
        self,
        session_id: UUID,
        *,
        model: str,
        reasoning_effort: str,
    ) -> dict[str, str]:
        async with self._database.connection() as connection:
            await connection.execute(
                """
                UPDATE app_sessions
                SET default_model = %s,
                    default_reasoning_effort = %s,
                    updated_at = now()
                WHERE id = %s
                """,
                (model, reasoning_effort, session_id),
            )
            await connection.commit()
        return {"model": model, "reasoning_effort": reasoning_effort}

    async def get_thread(
        self,
        session_id: UUID,
        thread_id: str,
    ) -> dict[str, str | None]:
        async with self._database.connection() as connection:
            cursor = await connection.execute(
                """
                SELECT model, reasoning_effort
                FROM thread_execution_settings
                WHERE session_id = %s AND thread_id = %s
                """,
                (session_id, thread_id),
            )
            row = await cursor.fetchone()
        if row is not None:
            return {
                "model": row["model"],
                "reasoning_effort": row["reasoning_effort"],
            }
        return await self.get_session(session_id)

    async def set_thread(
        self,
        session_id: UUID,
        thread_id: str,
        *,
        model: str,
        reasoning_effort: str,
    ) -> dict[str, str]:
        async with self._database.connection() as connection:
            await connection.execute(
                """
                INSERT INTO thread_execution_settings(
                    session_id, thread_id, model, reasoning_effort
                )
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (session_id, thread_id) DO UPDATE
                SET model = EXCLUDED.model,
                    reasoning_effort = EXCLUDED.reasoning_effort,
                    updated_at = now()
                """,
                (session_id, thread_id, model, reasoning_effort),
            )
            await connection.commit()
        return {"model": model, "reasoning_effort": reasoning_effort}

    async def delete_thread(self, session_id: UUID, thread_id: str) -> None:
        async with self._database.connection() as connection:
            await connection.execute(
                """
                DELETE FROM thread_execution_settings
                WHERE session_id = %s AND thread_id = %s
                """,
                (session_id, thread_id),
            )
            await connection.commit()


class RunRepository:
    def __init__(self, database: Database) -> None:
        self._database = database

    async def ping(self) -> None:
        async with self._database.connection() as connection:
            await connection.execute("SELECT 1")

    async def create(
        self,
        run_id: str,
        session_id: UUID,
        thread_id: str,
        command: dict[str, Any],
    ) -> dict[str, Any]:
        async with self._database.connection() as connection:
            cursor = await connection.execute(
                """
                INSERT INTO agent_runs(id, session_id, thread_id, command)
                VALUES (%s, %s, %s, %s::jsonb)
                RETURNING *
                """,
                (run_id, session_id, thread_id, json.dumps(command)),
            )
            row = await cursor.fetchone()
            await connection.commit()
        return row

    async def get(
        self,
        run_id: str,
        session_id: UUID,
    ) -> dict[str, Any]:
        async with self._database.connection() as connection:
            cursor = await connection.execute(
                "SELECT * FROM agent_runs WHERE id = %s AND session_id = %s",
                (run_id, session_id),
            )
            row = await cursor.fetchone()
        if row is None:
            raise NotFoundError(f"run {run_id} not found")
        return row

    async def find_by_turn(
        self,
        session_id: UUID,
        thread_id: str,
        turn_id: str,
    ) -> dict[str, Any] | None:
        async with self._database.connection() as connection:
            cursor = await connection.execute(
                """
                SELECT * FROM agent_runs
                WHERE session_id = %s AND thread_id = %s AND turn_id = %s
                ORDER BY updated_at DESC
                LIMIT 1
                """,
                (session_id, thread_id, turn_id),
            )
            return await cursor.fetchone()

    async def find_latest_for_thread(
        self,
        session_id: UUID,
        thread_id: str,
    ) -> dict[str, Any] | None:
        async with self._database.connection() as connection:
            cursor = await connection.execute(
                """
                SELECT * FROM agent_runs
                WHERE session_id = %s AND thread_id = %s
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (session_id, thread_id),
            )
            return await cursor.fetchone()

    async def list_queued(self, *, limit: int = 100) -> list[dict[str, Any]]:
        async with self._database.connection() as connection:
            cursor = await connection.execute(
                """
                SELECT * FROM agent_runs
                WHERE status = 'queued'
                ORDER BY created_at
                LIMIT %s
                """,
                (limit,),
            )
            return list(await cursor.fetchall())

    async def update(
        self,
        run_id: str,
        *,
        status: str | None = None,
        worker_id: str | None = None,
        thread_id: str | None = None,
        turn_id: str | None = None,
        error: str | None = None,
    ) -> None:
        assignments = ["updated_at = now()"]
        parameters: list[Any] = []
        for column, value in (
            ("status", status),
            ("worker_id", worker_id),
            ("thread_id", thread_id),
            ("turn_id", turn_id),
            ("error", error),
        ):
            if value is not None:
                assignments.append(f"{column} = %s")
                parameters.append(value)
        if status == "running":
            assignments.append("started_at = COALESCE(started_at, now())")
        if status in {"completed", "failed", "interrupted"}:
            assignments.append("completed_at = now()")
        async with self._database.connection() as connection:
            await connection.execute(
                f"UPDATE agent_runs SET {', '.join(assignments)} WHERE id = %s",
                (*parameters, run_id),
            )
            await connection.commit()
