from __future__ import annotations

import asyncio
from uuid import UUID

from efferva.config import Settings
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
    ) -> None:
        if not provider.capabilities.coding_agent_compatible:
            raise ValueError(
                f"sandbox provider {provider.name!r} does not satisfy Coding Agent requirements"
            )
        self._settings = settings
        self.provider = provider
        self._locks: dict[UUID, asyncio.Lock] = {}

    async def start(self) -> None:
        return None

    async def ensure(self, session_id: UUID) -> SandboxEnvironment:
        lock = self._locks.setdefault(session_id, asyncio.Lock())
        async with lock:
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
        close = getattr(self.provider, "close", None)
        if close is not None:
            await close()


def create_sandbox_control_plane(
    settings: Settings,
    provider: SandboxProvider,
) -> SandboxControlPlane:
    return SandboxControlPlane(settings, provider)
