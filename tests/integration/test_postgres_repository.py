from __future__ import annotations

import os
from importlib.resources import files
from uuid import UUID, uuid4

import pytest
from psycopg import sql

from efferva import Capability, Principal
from efferva.db import Database
from efferva.events import (
    run_finished,
    text_message_content,
    text_message_end,
    text_message_start,
)
from efferva.identity import (
    LEGACY_ISSUER,
    LEGACY_TENANT_ID,
    ForbiddenError,
)
from efferva.repository import ConflictError, NotFoundError, SystemRepository


def _database_url() -> str:
    value = os.getenv("EFFERVA_TEST_DATABASE_URL")
    if not value:
        pytest.skip("EFFERVA_TEST_DATABASE_URL is not set")
    return value


async def _migrate(database: Database) -> None:
    migrations_dir = files("efferva.migrations")
    migrations = [
        (path.name, path.read_text())
        for path in sorted(migrations_dir.iterdir(), key=lambda item: item.name)
        if path.name.endswith(".sql")
    ]
    await database.migrate(migrations)


@pytest.mark.integration
async def test_multi_tenant_migration_backfills_existing_sessions() -> None:
    database = Database(_database_url())
    await database.open()
    schema_name = f"efferva_migration_{uuid4().hex}"
    migrations_dir = files("efferva.migrations")
    try:
        async with database.connection() as connection:
            await connection.execute(
                sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema_name))
            )
            await connection.execute(
                sql.SQL("SET LOCAL search_path TO {}").format(sql.Identifier(schema_name))
            )
            await connection.execute(migrations_dir.joinpath("001_initial.sql").read_text())
            session_id = uuid4()
            await connection.execute(
                """
                INSERT INTO app_sessions(id, name, workspace_ref)
                VALUES (%s, 'legacy', %s)
                """,
                (session_id, f"session-{session_id}"),
            )
            await connection.execute(
                """
                INSERT INTO sandbox_bindings(
                    session_id,
                    backend,
                    sandbox_id,
                    endpoint,
                    workspace_ref,
                    status
                )
                VALUES (%s, 'legacy', 'legacy-sandbox', 'ws://legacy', 'legacy-workspace', 'ready')
                """,
                (session_id,),
            )
            await connection.execute(
                migrations_dir.joinpath("002_multi_tenant_identity.sql").read_text()
            )
            await connection.execute(
                migrations_dir.joinpath("003_provider_neutral_sandboxes.sql").read_text()
            )
            row = await (
                await connection.execute(
                    """
                    SELECT tenant_id, owner_issuer, owner_subject
                    FROM app_sessions
                    WHERE id = %s
                    """,
                    (session_id,),
                )
            ).fetchone()
            assert row == {
                "tenant_id": LEGACY_TENANT_ID,
                "owner_issuer": LEGACY_ISSUER,
                "owner_subject": "unowned",
            }
            workspace = await (
                await connection.execute(
                    """
                    SELECT provider, external_ref, state_json
                    FROM workspace_bindings
                    WHERE session_id = %s
                    """,
                    (session_id,),
                )
            ).fetchone()
            sandbox = await (
                await connection.execute(
                    """
                    SELECT provider, external_ref, state_json
                    FROM sandbox_leases
                    WHERE workspace_id = %s
                    """,
                    (session_id,),
                )
            ).fetchone()
            assert workspace == {
                "provider": "legacy",
                "external_ref": "legacy-workspace",
                "state_json": {},
            }
            assert sandbox == {
                "provider": "legacy",
                "external_ref": "legacy-sandbox",
                "state_json": {},
            }
            await connection.rollback()
    finally:
        await database.close()


@pytest.mark.integration
async def test_repository_isolates_tenants_and_projects_worker_events() -> None:
    database = Database(_database_url())
    await database.open()
    await _migrate(database)
    system = SystemRepository(database)
    alice = system.for_principal(Principal(tenant_id="acme", issuer="integration", subject="alice"))
    bob = system.for_principal(Principal(tenant_id="acme", issuer="integration", subject="bob"))
    admin = system.for_principal(
        Principal(
            tenant_id="acme",
            issuer="integration",
            subject="admin",
            capabilities=frozenset({Capability.SESSIONS_READ_TENANT}),
        )
    )
    other_tenant_admin = system.for_principal(
        Principal(
            tenant_id="globex",
            issuer="integration",
            subject="admin",
            capabilities=frozenset(
                {
                    Capability.SESSIONS_READ_TENANT,
                    Capability.SESSIONS_WRITE_TENANT,
                }
            ),
        )
    )
    session = await alice.create_session("integration")
    try:
        assert [row["id"] for row in await alice.list_sessions()] == [session["id"]]
        assert await bob.list_sessions() == []
        with pytest.raises(ForbiddenError):
            await bob.list_sessions("tenant")
        with pytest.raises(NotFoundError):
            await bob.get_session(session["id"])

        tenant_sessions = await admin.list_sessions("tenant")
        assert session["id"] in {row["id"] for row in tenant_sessions}
        assert (await admin.get_session(session["id"]))["owner_subject"] == "alice"
        with pytest.raises(NotFoundError):
            await admin.create_thread(session["id"], "read-only admin")

        assert await other_tenant_admin.list_sessions("tenant") == []
        with pytest.raises(NotFoundError):
            await other_tenant_admin.get_session(session["id"])

        first_thread = await alice.create_thread(session["id"], "first")
        second_thread = await alice.create_thread(session["id"], "second")
        with pytest.raises(NotFoundError):
            await admin.create_run(first_thread["id"], "read-only admin bypass")
        with pytest.raises(NotFoundError):
            await other_tenant_admin.create_run(first_thread["id"], "cross-tenant bypass")
        first_run = await alice.create_run(
            first_thread["id"],
            "hello",
            agui_run_id="shared-client-run-id",
        )
        repeated_run = await alice.create_run(
            first_thread["id"],
            "ignored idempotent retry",
            agui_run_id="shared-client-run-id",
        )
        second_run = await alice.create_run(
            second_thread["id"],
            "parallel",
            agui_run_id="shared-client-run-id",
        )
        assert repeated_run["id"] == first_run["id"]
        assert second_run["id"] != first_run["id"]

        with pytest.raises(NotFoundError):
            await bob.get_thread_detail(first_thread["id"])
        with pytest.raises(NotFoundError):
            await bob.get_run(first_run["id"])
        with pytest.raises(NotFoundError):
            await bob.list_run_events(first_run["id"], 0)

        first_claim = await system.claim_run("worker-a", 30, 4)
        second_claim = await system.claim_run("worker-a", 30, 4)
        assert first_claim is not None
        assert second_claim is not None
        assert {first_claim["id"], second_claim["id"]} == {
            first_run["id"],
            second_run["id"],
        }
        assert await system.claim_run("worker-b", 30, 4) is None
        await system.upsert_workspace_binding(
            session["id"],
            workspace_id=session["id"],
            provider="test",
            external_ref=f"workspace-{session['id']}",
            state={"opaque": "workspace"},
            status="ready",
        )
        await system.upsert_sandbox_lease(
            workspace_id=session["id"],
            provider="test",
            external_ref=f"sandbox-{session['id']}",
            state={"opaque": "sandbox"},
            owner_id="worker-a",
            fencing_token=first_claim["fencing_epoch"],
            lease_ttl_seconds=30,
        )
        assert await system.sandbox_fence_is_current(
            session["id"],
            "worker-a",
            first_claim["fencing_epoch"],
        )
        assert not await system.sandbox_fence_is_current(
            session["id"],
            "worker-b",
            first_claim["fencing_epoch"],
        )

        message_id = "integration:assistant"
        epoch = first_claim["fencing_epoch"]
        await system.append_event(
            first_claim["id"],
            text_message_start(message_id),
            owner_id="worker-a",
            fencing_epoch=epoch,
        )
        await system.append_event(
            first_claim["id"],
            text_message_content(message_id, "durable"),
            owner_id="worker-a",
            fencing_epoch=epoch,
        )
        await system.append_event(
            first_claim["id"],
            text_message_end(message_id),
            owner_id="worker-a",
            fencing_epoch=epoch,
        )
        await system.append_event(
            first_claim["id"],
            run_finished(first_claim["thread_id"], first_claim["agui_run_id"]),
            owner_id="worker-a",
            fencing_epoch=epoch,
        )

        detail = await alice.get_thread_detail(UUID(str(first_claim["thread_id"])))
        assert detail["messages"][-1]["content"] == "durable"
        assert detail["runs"][0]["status"] == "completed"
        assert (await admin.get_thread_detail(first_thread["id"]))["id"] == first_thread["id"]

        await system.release_session_if_idle(session["id"], "worker-a")
        async with database.connection() as connection:
            lease = await (
                await connection.execute(
                    "SELECT 1 FROM session_leases WHERE session_id = %s",
                    (session["id"],),
                )
            ).fetchone()
        assert lease is not None

        await system.append_event(
            second_claim["id"],
            run_finished(second_claim["thread_id"], second_claim["agui_run_id"]),
            owner_id="worker-a",
            fencing_epoch=second_claim["fencing_epoch"],
        )
        await system.release_session_if_idle(session["id"], "worker-a")
        async with database.connection() as connection:
            released = await (
                await connection.execute(
                    "SELECT 1 FROM session_leases WHERE session_id = %s",
                    (session["id"],),
                )
            ).fetchone()
        assert released is not None
        async with database.connection() as connection:
            idle_sandbox = await (
                await connection.execute(
                    """
                    SELECT status, expires_at < now() AS expired
                    FROM sandbox_leases
                    WHERE workspace_id = %s
                    """,
                    (session["id"],),
                )
            ).fetchone()
        assert idle_sandbox == {"status": "idle", "expired": True}

        async with database.connection() as connection:
            await connection.execute(
                """
                UPDATE session_leases
                SET owner_id = 'worker-b',
                    fencing_epoch = fencing_epoch + 1,
                    expires_at = now() + interval '30 seconds'
                WHERE session_id = %s
                """,
                (session["id"],),
            )
            await connection.execute(
                """
                UPDATE sandbox_leases
                SET status = 'ready',
                    expires_at = now() - interval '1 second'
                WHERE workspace_id = %s
                """,
                (session["id"],),
            )
            await connection.commit()
        await system.renew_owned_leases("worker-a", 30)
        assert not await system.sandbox_fence_is_current(
            session["id"],
            "worker-a",
            first_claim["fencing_epoch"],
        )
        async with database.connection() as connection:
            stale_sandbox = await (
                await connection.execute(
                    """
                    SELECT expires_at < now() AS expired
                    FROM sandbox_leases
                    WHERE workspace_id = %s
                    """,
                    (session["id"],),
                )
            ).fetchone()
        assert stale_sandbox == {"expired": True}

        with pytest.raises(ConflictError, match="already terminal"):
            await system.append_event(first_claim["id"], text_message_end(message_id))
    finally:
        async with database.connection() as connection:
            await connection.execute("DELETE FROM app_sessions WHERE id = %s", (session["id"],))
            await connection.commit()
        await database.close()
