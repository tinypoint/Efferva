from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any
from uuid import UUID

from efferva.db import Database
from efferva.sandbox.protocol import (
    SandboxContext,
    SandboxEnvironment,
    SandboxProvider,
    SessionSummary,
)
from efferva.session_repository import SessionRepository


class SessionSandboxService:
    def __init__(
        self,
        workspace_path: str,
        provider: SandboxProvider,
        database: Database,
        sessions: SessionRepository,
    ) -> None:
        if not provider.capabilities.coding_agent_compatible:
            raise ValueError(
                f"sandbox provider {provider.name!r} does not satisfy Coding Agent requirements"
            )
        self._workspace_path = workspace_path
        self._provider = provider
        self._database = database
        self._sessions = sessions

    async def open(self) -> None:
        await self._provider.open()

    async def ensure(self, session: Mapping[str, Any]) -> SandboxEnvironment:
        session_id = UUID(str(session["id"]))
        tenant_id = str(session["tenant_id"])
        owner_issuer = str(session["owner_issuer"])
        owner_subject = str(session["owner_subject"])
        advisory_lock_key = "efferva:sandbox-owner:" + json.dumps(
            [tenant_id, owner_issuer, owner_subject],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        async with self._database.advisory_lock(advisory_lock_key):
            owner_sessions = await self._sessions.list_owner_sessions(
                tenant_id=tenant_id,
                owner_issuer=owner_issuer,
                owner_subject=owner_subject,
            )
            return await self._provider.ensure(
                SandboxContext(
                    session_id=session_id,
                    tenant_id=tenant_id,
                    owner_issuer=owner_issuer,
                    owner_subject=owner_subject,
                    workspace_path=self._workspace_path,
                    owner_sessions=tuple(
                        SessionSummary(
                            id=UUID(str(item["id"])),
                            name=str(item["name"]),
                            status=str(item["status"]),
                            last_active_at=item["last_active_at"],
                            created_at=item["created_at"],
                            updated_at=item["updated_at"],
                        )
                        for item in owner_sessions
                    ),
                )
            )

    async def close(self) -> None:
        await self._provider.close()
