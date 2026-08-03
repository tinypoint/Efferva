from __future__ import annotations

from uuid import UUID

from efferva.config import Settings
from efferva.db import Database
from efferva.sandbox.protocol import (
    SandboxContext,
    SandboxEnvironment,
    SandboxProvider,
)


class SessionSandboxService:
    def __init__(
        self,
        settings: Settings,
        provider: SandboxProvider,
        database: Database,
    ) -> None:
        if not provider.capabilities.coding_agent_compatible:
            raise ValueError(
                f"sandbox provider {provider.name!r} does not satisfy Coding Agent requirements"
            )
        self._settings = settings
        self._provider = provider
        self._database = database

    async def open(self) -> None:
        await self._provider.open()

    async def ensure(self, session_id: UUID) -> SandboxEnvironment:
        advisory_lock_key = f"efferva:sandbox-session:{session_id}"
        async with self._database.advisory_lock(advisory_lock_key):
            return await self._provider.ensure(
                SandboxContext(
                    session_id=session_id,
                    workspace_path=self._settings.workspace_path,
                )
            )

    async def close(self) -> None:
        await self._provider.close()


def create_session_sandbox_service(
    settings: Settings,
    provider: SandboxProvider,
    database: Database,
) -> SessionSandboxService:
    return SessionSandboxService(settings, provider, database)
