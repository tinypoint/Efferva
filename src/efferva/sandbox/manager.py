from __future__ import annotations

from uuid import UUID

from efferva.config import Settings
from efferva.db import Database
from efferva.sandbox.protocol import (
    SandboxContext,
    SandboxEnvironment,
    SandboxProvider,
)


class SandboxControlPlane:
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
        self._database = database
        self.provider = provider

    async def start(self) -> None:
        await self.provider.open()

    async def ensure(self, session_id: UUID) -> SandboxEnvironment:
        advisory_lock_key = f"efferva:sandbox-session:{session_id}"
        async with self._database.advisory_lock(advisory_lock_key):
            context = SandboxContext(
                session_id=session_id,
                workspace_path=self._settings.workspace_path,
            )
            volume = await self.provider.ensure_session_volume(context)
            sandbox = await self.provider.start(context, volume)
            runtime = await self.provider.connect(sandbox)
            return SandboxEnvironment(
                environment_id=str(session_id),
                endpoint=f"sandbox://{sandbox.external_ref}",
                workspace_path=context.workspace_path,
                sandbox=sandbox,
                runtime=runtime,
            )

    async def close(self) -> None:
        await self.provider.close()


def create_sandbox_control_plane(
    settings: Settings,
    provider: SandboxProvider,
    database: Database,
) -> SandboxControlPlane:
    return SandboxControlPlane(settings, provider, database)
