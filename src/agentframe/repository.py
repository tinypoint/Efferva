from __future__ import annotations

from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Literal
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb

from agentframe.db import Database
from agentframe.events import TERMINAL_EVENTS, run_started
from agentframe.identity import Capability, ForbiddenError, Principal


class NotFoundError(LookupError):
    pass


class ConflictError(RuntimeError):
    pass


class _AccessMode(StrEnum):
    READ = "read"
    WRITE = "write"


def _public_run(row: dict[str, Any]) -> dict[str, Any]:
    row["input"] = row.pop("input_json")
    return row


class AuthorizedRepository:
    """Request-scoped repository whose every lookup is constrained by one Principal."""

    def __init__(self, database: Database, principal: Principal) -> None:
        self._database = database
        self.principal = principal

    def _session_scope(
        self,
        mode: _AccessMode,
        *,
        alias: str = "s",
    ) -> tuple[str, tuple[str, ...]]:
        capability = {
            _AccessMode.READ: Capability.SESSIONS_READ_TENANT,
            _AccessMode.WRITE: Capability.SESSIONS_WRITE_TENANT,
        }[mode]
        if self.principal.has(capability):
            return f"{alias}.tenant_id = %s", (self.principal.tenant_id,)
        return (
            (
                f"{alias}.tenant_id = %s "
                f"AND {alias}.owner_issuer = %s "
                f"AND {alias}.owner_subject = %s"
            ),
            (
                self.principal.tenant_id,
                self.principal.issuer,
                self.principal.subject,
            ),
        )

    async def create_session(self, name: str) -> dict[str, Any]:
        session_id = uuid4()
        workspace_ref = f"session-{session_id}"
        async with self._database.connection() as connection:
            cursor = await connection.execute(
                """
                INSERT INTO app_sessions(
                    id,
                    tenant_id,
                    owner_issuer,
                    owner_subject,
                    name,
                    workspace_ref
                )
                VALUES (%s, %s, %s, %s, %s, %s)
                RETURNING *
                """,
                (
                    session_id,
                    self.principal.tenant_id,
                    self.principal.issuer,
                    self.principal.subject,
                    name,
                    workspace_ref,
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
                ORDER BY updated_at DESC, created_at DESC
                """,
                parameters,
            )
            return list(await cursor.fetchall())

    async def get_session(
        self,
        session_id: UUID,
        *,
        mode: _AccessMode = _AccessMode.READ,
    ) -> dict[str, Any]:
        where, scope_parameters = self._session_scope(mode)
        async with self._database.connection() as connection:
            cursor = await connection.execute(
                f"SELECT * FROM app_sessions s WHERE s.id = %s AND {where}",
                (session_id, *scope_parameters),
            )
            row = await cursor.fetchone()
        if row is None:
            raise NotFoundError(f"session {session_id} not found")
        return row

    async def create_thread(self, session_id: UUID, title: str | None) -> dict[str, Any]:
        await self.get_session(session_id, mode=_AccessMode.WRITE)
        async with self._database.connection() as connection:
            cursor = await connection.execute(
                """
                INSERT INTO app_threads(id, session_id, title)
                VALUES (%s, %s, %s)
                RETURNING *
                """,
                (uuid4(), session_id, title),
            )
            row = await cursor.fetchone()
            await connection.execute(
                "UPDATE app_sessions SET updated_at = now() WHERE id = %s",
                (session_id,),
            )
            await connection.commit()
        return row

    async def list_threads(self, session_id: UUID) -> list[dict[str, Any]]:
        await self.get_session(session_id)
        async with self._database.connection() as connection:
            cursor = await connection.execute(
                """
                SELECT * FROM app_threads
                WHERE session_id = %s
                ORDER BY updated_at DESC, created_at DESC
                """,
                (session_id,),
            )
            return list(await cursor.fetchall())

    async def get_thread(
        self,
        thread_id: UUID,
        *,
        mode: _AccessMode = _AccessMode.READ,
    ) -> dict[str, Any]:
        where, scope_parameters = self._session_scope(mode)
        async with self._database.connection() as connection:
            cursor = await connection.execute(
                f"""
                SELECT t.*
                FROM app_threads t
                JOIN app_sessions s ON s.id = t.session_id
                WHERE t.id = %s AND {where}
                """,
                (thread_id, *scope_parameters),
            )
            row = await cursor.fetchone()
        if row is None:
            raise NotFoundError(f"thread {thread_id} not found")
        return row

    async def get_thread_detail(self, thread_id: UUID) -> dict[str, Any]:
        thread = await self.get_thread(thread_id)
        async with self._database.connection() as connection:
            cursor = await connection.execute(
                """
                SELECT * FROM messages
                WHERE thread_id = %s
                ORDER BY created_at, id
                """,
                (thread_id,),
            )
            thread["messages"] = list(await cursor.fetchall())
            runs_cursor = await connection.execute(
                """
                SELECT * FROM runs
                WHERE thread_id = %s
                ORDER BY created_at DESC
                LIMIT 20
                """,
                (thread_id,),
            )
            thread["runs"] = [_public_run(row) for row in await runs_cursor.fetchall()]
        return thread

    async def create_run(
        self,
        thread_id: UUID,
        prompt: str,
        *,
        agui_run_id: str | None = None,
        input_payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        await self.get_thread(thread_id, mode=_AccessMode.WRITE)
        run_id = uuid4()
        public_run_id = agui_run_id or str(run_id)
        payload = input_payload or {"prompt": prompt}
        started_event = run_started(thread_id, public_run_id, payload)
        async with self._database.connection() as connection:
            cursor = await connection.execute(
                """
                INSERT INTO runs(id, agui_run_id, thread_id, status, input_json, last_seq)
                VALUES (%s, %s, %s, 'queued', %s, 1)
                ON CONFLICT (thread_id, agui_run_id) DO NOTHING
                RETURNING *
                """,
                (run_id, public_run_id, thread_id, Jsonb(payload)),
            )
            row = await cursor.fetchone()
            if row is None:
                existing = await connection.execute(
                    """
                    SELECT * FROM runs
                    WHERE thread_id = %s AND agui_run_id = %s
                    """,
                    (thread_id, public_run_id),
                )
                row = await existing.fetchone()
                await connection.commit()
                return _public_run(row)
            await connection.execute(
                """
                INSERT INTO run_events(run_id, seq, event_json)
                VALUES (%s, 1, %s)
                """,
                (run_id, Jsonb(started_event)),
            )
            await connection.execute(
                """
                INSERT INTO messages(
                    id, external_id, thread_id, run_id, role, content, status, first_seq, last_seq
                )
                VALUES (%s, %s, %s, %s, 'user', %s, 'completed', 1, 1)
                """,
                (uuid4(), f"{public_run_id}:user", thread_id, run_id, prompt),
            )
            await connection.execute(
                "UPDATE app_threads SET updated_at = now() WHERE id = %s",
                (thread_id,),
            )
            await connection.commit()
        return _public_run(row)

    async def get_run(self, run_id: UUID) -> dict[str, Any]:
        where, scope_parameters = self._session_scope(_AccessMode.READ)
        async with self._database.connection() as connection:
            cursor = await connection.execute(
                f"""
                SELECT r.*
                FROM runs r
                JOIN app_threads t ON t.id = r.thread_id
                JOIN app_sessions s ON s.id = t.session_id
                WHERE r.id = %s AND {where}
                """,
                (run_id, *scope_parameters),
            )
            row = await cursor.fetchone()
        if row is None:
            raise NotFoundError(f"run {run_id} not found")
        return _public_run(row)

    async def get_run_by_agui_id(
        self,
        thread_id: UUID,
        agui_run_id: str,
    ) -> dict[str, Any]:
        where, scope_parameters = self._session_scope(_AccessMode.WRITE)
        async with self._database.connection() as connection:
            cursor = await connection.execute(
                f"""
                SELECT r.*
                FROM runs r
                JOIN app_threads t ON t.id = r.thread_id
                JOIN app_sessions s ON s.id = t.session_id
                WHERE r.thread_id = %s
                  AND r.agui_run_id = %s
                  AND {where}
                """,
                (thread_id, agui_run_id, *scope_parameters),
            )
            row = await cursor.fetchone()
        if row is None:
            raise NotFoundError(f"run {agui_run_id} not found")
        return _public_run(row)

    async def list_run_events(self, run_id: UUID, after: int) -> list[dict[str, Any]]:
        await self.get_run(run_id)
        async with self._database.connection() as connection:
            cursor = await connection.execute(
                """
                SELECT seq, event_json, created_at
                FROM run_events
                WHERE run_id = %s AND seq > %s
                ORDER BY seq
                """,
                (run_id, after),
            )
            rows = await cursor.fetchall()
        return [
            {"seq": row["seq"], "event": row["event_json"], "created_at": row["created_at"]}
            for row in rows
        ]

    async def run_is_terminal(self, run_id: UUID) -> bool:
        run = await self.get_run(run_id)
        return run["terminal_seq"] is not None


class SystemRepository:
    """Unscoped control-plane access used only by workers and sandbox management."""

    def __init__(self, database: Database) -> None:
        self._database = database

    def for_principal(self, principal: Principal) -> AuthorizedRepository:
        return AuthorizedRepository(self._database, principal)

    async def ping(self) -> None:
        async with self._database.connection() as connection:
            await connection.execute("SELECT 1")

    async def set_codex_thread_id(self, thread_id: UUID, codex_thread_id: UUID) -> None:
        async with self._database.connection() as connection:
            await connection.execute(
                """
                UPDATE app_threads
                SET codex_thread_id = %s, updated_at = now()
                WHERE id = %s
                """,
                (codex_thread_id, thread_id),
            )
            await connection.commit()

    async def append_event(
        self,
        run_id: UUID,
        event: dict[str, Any],
        *,
        owner_id: str | None = None,
        fencing_epoch: int | None = None,
    ) -> int:
        event_type = event["type"]
        async with self._database.connection() as connection:
            cursor = await connection.execute(
                """
                SELECT r.*, l.owner_id AS lease_owner, l.fencing_epoch AS current_epoch
                FROM runs r
                JOIN app_threads t ON t.id = r.thread_id
                LEFT JOIN session_leases l ON l.session_id = t.session_id
                WHERE r.id = %s
                FOR UPDATE OF r
                """,
                (run_id,),
            )
            run = await cursor.fetchone()
            if run is None:
                raise NotFoundError(f"run {run_id} not found")
            if owner_id is not None and (
                run["owner_id"] != owner_id
                or run["lease_owner"] != owner_id
                or run["lease_epoch"] != fencing_epoch
                or run["current_epoch"] != fencing_epoch
            ):
                raise ConflictError(f"run {run_id} worker lease is stale")
            if run["terminal_seq"] is not None:
                raise ConflictError(f"run {run_id} is already terminal")
            seq = run["last_seq"] + 1
            await connection.execute(
                "INSERT INTO run_events(run_id, seq, event_json) VALUES (%s, %s, %s)",
                (run_id, seq, Jsonb(event)),
            )
            await self._project_event(connection, run, seq, event)
            terminal = event_type in TERMINAL_EVENTS
            status = {
                "RUN_FINISHED": "completed",
                "RUN_ERROR": "failed",
            }.get(event_type, "running")
            error = event.get("message") if event_type == "RUN_ERROR" else None
            await connection.execute(
                """
                UPDATE runs
                SET last_seq = %s,
                    terminal_seq = CASE WHEN %s THEN %s ELSE terminal_seq END,
                    status = %s,
                    error = COALESCE(%s, error),
                    updated_at = now()
                WHERE id = %s
                """,
                (seq, terminal, seq, status, error, run_id),
            )
            await connection.commit()
        return seq

    async def _project_event(
        self,
        connection: Any,
        run: dict[str, Any],
        seq: int,
        event: dict[str, Any],
    ) -> None:
        event_type = event["type"]
        if event_type == "TEXT_MESSAGE_START":
            await connection.execute(
                """
                INSERT INTO messages(
                    id, external_id, thread_id, run_id, role, status, first_seq, last_seq
                )
                VALUES (%s, %s, %s, %s, 'assistant', 'streaming', %s, %s)
                ON CONFLICT (thread_id, external_id) DO NOTHING
                """,
                (
                    uuid4(),
                    event["messageId"],
                    run["thread_id"],
                    run["id"],
                    seq,
                    seq,
                ),
            )
        elif event_type == "TEXT_MESSAGE_CONTENT":
            await connection.execute(
                """
                UPDATE messages
                SET content = content || %s, last_seq = %s, updated_at = now()
                WHERE thread_id = %s AND external_id = %s
                """,
                (event["delta"], seq, run["thread_id"], event["messageId"]),
            )
        elif event_type == "TEXT_MESSAGE_END":
            await connection.execute(
                """
                UPDATE messages
                SET status = 'completed', last_seq = %s, updated_at = now()
                WHERE thread_id = %s AND external_id = %s
                """,
                (seq, run["thread_id"], event["messageId"]),
            )

    async def claim_run(
        self,
        owner_id: str,
        lease_ttl_seconds: int,
        max_parallel_threads_per_session: int,
    ) -> dict[str, Any] | None:
        async with self._database.connection() as connection:
            cursor = await connection.execute(
                """
                SELECT r.*,
                       t.session_id,
                       t.codex_thread_id,
                       s.workspace_ref,
                       (
                           SELECT content
                           FROM messages
                           WHERE run_id = r.id AND role = 'user'
                           ORDER BY created_at
                           LIMIT 1
                       ) AS prompt
                FROM runs r
                JOIN app_threads t ON t.id = r.thread_id
                JOIN app_sessions s ON s.id = t.session_id
                LEFT JOIN session_leases l ON l.session_id = t.session_id
                WHERE r.status = 'queued'
                  AND (l.session_id IS NULL OR l.owner_id = %s OR l.expires_at < now())
                  AND NOT EXISTS (
                      SELECT 1 FROM runs active
                      WHERE active.thread_id = r.thread_id AND active.status = 'running'
                  )
                  AND (
                      SELECT count(*)
                      FROM runs active
                      JOIN app_threads active_thread ON active_thread.id = active.thread_id
                      WHERE active.status = 'running'
                        AND active_thread.session_id = t.session_id
                  ) < %s
                ORDER BY r.created_at
                FOR UPDATE OF r SKIP LOCKED
                LIMIT 1
                """,
                (owner_id, max_parallel_threads_per_session),
            )
            run = await cursor.fetchone()
            if run is None:
                await connection.rollback()
                return None
            lease_cursor = await connection.execute(
                """
                INSERT INTO session_leases(
                    session_id, owner_id, fencing_epoch, expires_at, updated_at
                )
                VALUES (
                    %s, %s, 1, now() + make_interval(secs => %s), now()
                )
                ON CONFLICT (session_id) DO UPDATE
                SET owner_id = EXCLUDED.owner_id,
                    fencing_epoch = CASE
                        WHEN session_leases.owner_id = EXCLUDED.owner_id
                            THEN session_leases.fencing_epoch
                        ELSE session_leases.fencing_epoch + 1
                    END,
                    expires_at = EXCLUDED.expires_at,
                    updated_at = now()
                WHERE session_leases.owner_id = EXCLUDED.owner_id
                   OR session_leases.expires_at < now()
                RETURNING fencing_epoch
                """,
                (run["session_id"], owner_id, lease_ttl_seconds),
            )
            lease = await lease_cursor.fetchone()
            if lease is None:
                await connection.rollback()
                return None
            await connection.execute(
                """
                UPDATE runs
                SET status = 'running',
                    owner_id = %s,
                    lease_epoch = %s,
                    updated_at = now()
                WHERE id = %s
                """,
                (owner_id, lease["fencing_epoch"], run["id"]),
            )
            await connection.commit()
            run["fencing_epoch"] = lease["fencing_epoch"]
            run["input"] = run.pop("input_json")
            return run

    async def renew_owned_leases(self, owner_id: str, lease_ttl_seconds: int) -> None:
        async with self._database.connection() as connection:
            await connection.execute(
                """
                UPDATE session_leases
                SET expires_at = now() + make_interval(secs => %s), updated_at = now()
                WHERE owner_id = %s
                """,
                (lease_ttl_seconds, owner_id),
            )
            await connection.execute(
                """
                UPDATE sandbox_leases sandbox
                SET expires_at = now() + make_interval(secs => %s),
                    updated_at = now()
                FROM workspace_bindings workspace, session_leases session
                WHERE sandbox.workspace_id = workspace.workspace_id
                  AND session.session_id = workspace.session_id
                  AND sandbox.owner_id = %s
                  AND sandbox.status = 'ready'
                  AND session.owner_id = sandbox.owner_id
                  AND session.fencing_epoch = sandbox.fencing_token
                  AND session.expires_at > now()
                """,
                (lease_ttl_seconds, owner_id),
            )
            await connection.commit()

    async def requeue_abandoned_runs(self) -> int:
        async with self._database.connection() as connection:
            cursor = await connection.execute(
                """
                UPDATE runs r
                SET status = 'queued',
                    owner_id = NULL,
                    lease_epoch = NULL,
                    updated_at = now()
                FROM app_threads t
                LEFT JOIN session_leases l ON l.session_id = t.session_id
                WHERE r.thread_id = t.id
                  AND r.status = 'running'
                  AND (l.session_id IS NULL OR l.expires_at < now())
                RETURNING r.id
                """
            )
            rows = await cursor.fetchall()
            await connection.commit()
        return len(rows)

    async def set_codex_turn_id(self, run_id: UUID, codex_turn_id: str) -> None:
        async with self._database.connection() as connection:
            await connection.execute(
                "UPDATE runs SET codex_turn_id = %s, updated_at = now() WHERE id = %s",
                (codex_turn_id, run_id),
            )
            await connection.commit()

    async def run_is_terminal(self, run_id: UUID) -> bool:
        async with self._database.connection() as connection:
            cursor = await connection.execute(
                "SELECT terminal_seq IS NOT NULL AS terminal FROM runs WHERE id = %s",
                (run_id,),
            )
            row = await cursor.fetchone()
        if row is None:
            raise NotFoundError(f"run {run_id} not found")
        return row["terminal"]

    async def fail_run(self, run_id: UUID, event: dict[str, Any]) -> None:
        if not await self.run_is_terminal(run_id):
            await self.append_event(run_id, event)

    async def get_workspace_binding(self, session_id: UUID) -> dict[str, Any] | None:
        async with self._database.connection() as connection:
            cursor = await connection.execute(
                "SELECT * FROM workspace_bindings WHERE session_id = %s",
                (session_id,),
            )
            return await cursor.fetchone()

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
                    workspace_id, session_id, provider, external_ref, state_json, status
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

    async def upsert_sandbox_lease(
        self,
        *,
        workspace_id: UUID,
        provider: str,
        external_ref: str,
        state: dict[str, Any],
        owner_id: str,
        fencing_token: int,
        lease_ttl_seconds: int,
    ) -> dict[str, Any]:
        async with self._database.connection() as connection:
            cursor = await connection.execute(
                """
                INSERT INTO sandbox_leases(
                    sandbox_id,
                    workspace_id,
                    provider,
                    external_ref,
                    state_json,
                    owner_id,
                    status,
                    fencing_token,
                    expires_at
                )
                VALUES (
                    %s, %s, %s, %s, %s, %s, 'ready', %s,
                    now() + make_interval(secs => %s)
                )
                ON CONFLICT (workspace_id) DO UPDATE
                SET provider = EXCLUDED.provider,
                    external_ref = EXCLUDED.external_ref,
                    state_json = EXCLUDED.state_json,
                    owner_id = EXCLUDED.owner_id,
                    status = 'ready',
                    fencing_token = EXCLUDED.fencing_token,
                    expires_at = EXCLUDED.expires_at,
                    updated_at = now()
                WHERE sandbox_leases.fencing_token <= EXCLUDED.fencing_token
                RETURNING *
                """,
                (
                    workspace_id,
                    workspace_id,
                    provider,
                    external_ref,
                    Jsonb(state),
                    owner_id,
                    fencing_token,
                    lease_ttl_seconds,
                ),
            )
            row = await cursor.fetchone()
            await connection.commit()
        if row is None:
            raise ConflictError(f"workspace {workspace_id} sandbox lease is stale")
        return row

    async def sandbox_fence_is_current(
        self,
        workspace_id: UUID,
        owner_id: str,
        fencing_token: int,
    ) -> bool:
        async with self._database.connection() as connection:
            cursor = await connection.execute(
                """
                SELECT 1
                FROM sandbox_leases sandbox
                JOIN workspace_bindings workspace
                  ON workspace.workspace_id = sandbox.workspace_id
                JOIN session_leases session
                  ON session.session_id = workspace.session_id
                WHERE sandbox.workspace_id = %s
                  AND sandbox.owner_id = %s
                  AND sandbox.fencing_token = %s
                  AND sandbox.status = 'ready'
                  AND sandbox.expires_at > now()
                  AND session.owner_id = sandbox.owner_id
                  AND session.fencing_epoch = sandbox.fencing_token
                  AND session.expires_at > now()
                """,
                (workspace_id, owner_id, fencing_token),
            )
            return await cursor.fetchone() is not None

    async def release_session_if_idle(self, session_id: UUID, owner_id: str) -> None:
        async with self._database.connection() as connection:
            await connection.execute(
                """
                UPDATE session_leases lease
                SET expires_at = now() - interval '1 microsecond',
                    updated_at = now()
                WHERE lease.session_id = %s AND lease.owner_id = %s
                  AND NOT EXISTS (
                      SELECT 1
                      FROM runs active
                      JOIN app_threads thread ON thread.id = active.thread_id
                      WHERE thread.session_id = lease.session_id
                        AND active.status = 'running'
                  )
                """,
                (session_id, owner_id),
            )
            await connection.execute(
                """
                UPDATE sandbox_leases sandbox
                SET status = 'idle',
                    expires_at = now() - interval '1 microsecond',
                    updated_at = now()
                FROM workspace_bindings workspace
                WHERE sandbox.workspace_id = workspace.workspace_id
                  AND workspace.session_id = %s
                  AND sandbox.owner_id = %s
                  AND NOT EXISTS (
                      SELECT 1
                      FROM runs active
                      JOIN app_threads thread ON thread.id = active.thread_id
                      WHERE thread.session_id = workspace.session_id
                        AND active.status = 'running'
                  )
                """,
                (session_id, owner_id),
            )
            await connection.commit()

    @staticmethod
    def lease_deadline(ttl_seconds: int) -> datetime:
        return datetime.now(UTC) + timedelta(seconds=ttl_seconds)
