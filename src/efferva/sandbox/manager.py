from __future__ import annotations

import asyncio
from uuid import UUID

from efferva.config import Settings
from efferva.repository import SystemRepository
from efferva.sandbox.registry import create_registered_provider
from efferva.sandbox.types import (
    SandboxContext,
    SandboxEnvironment,
    SandboxHandle,
    SandboxProvider,
)


class SandboxControlPlane:
    def __init__(
        self,
        settings: Settings,
        repository: SystemRepository,
        provider: SandboxProvider,
    ) -> None:
        if not provider.capabilities.coding_agent_compatible:
            raise ValueError(
                f"sandbox provider {provider.name!r} does not satisfy Coding Agent requirements"
            )
        self._settings = settings
        self._repository = repository
        self.provider = provider
        self._locks: dict[UUID, asyncio.Lock] = {}

    async def start(self) -> None:
        return None

    async def ensure(
        self,
        session_id: UUID,
        workspace_ref: str,
    ) -> SandboxEnvironment:
        lock = self._locks.setdefault(session_id, asyncio.Lock())
        async with lock:
            context = SandboxContext(
                session_id=session_id,
                workspace_id=session_id,
                workspace_ref=workspace_ref,
                workspace_path=self._settings.workspace_path,
            )
            workspace = await self.provider.ensure_workspace(context)
            await self._repository.upsert_workspace_binding(
                session_id,
                workspace_id=context.workspace_id,
                provider=self.provider.name,
                external_ref=workspace.external_ref,
                state=dict(workspace.state),
                status="ready",
            )
            sandbox = await self.provider.start(context, workspace)
            await self._repository.upsert_sandbox_binding(
                workspace_id=context.workspace_id,
                provider=self.provider.name,
                external_ref=sandbox.external_ref,
                state=dict(sandbox.state),
            )
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

    async def reap_idle(self) -> int:
        rows = await self._repository.claim_idle_sandboxes(
            self._settings.sandbox_idle_timeout_seconds
        )
        reaped = 0
        for row in rows:
            succeeded = False
            try:
                if row["provider"] != self.provider.name:
                    continue
                await self.provider.destroy(
                    SandboxHandle(
                        provider=row["provider"],
                        external_ref=row["external_ref"],
                        workspace_id=row["workspace_id"],
                        state=dict(row["state_json"] or {}),
                    )
                )
                succeeded = True
                reaped += 1
            except Exception:
                __import__("logging").getLogger(__name__).exception(
                    "failed to reap idle Session sandbox %s",
                    row["external_ref"],
                )
            finally:
                await self._repository.finish_sandbox_reap(
                    row["workspace_id"],
                    row["external_ref"],
                    succeeded=succeeded,
                )
        return reaped


def create_sandbox_provider(settings: Settings) -> SandboxProvider:
    name = settings.sandbox_provider.lower()
    if name == "opensandbox":
        try:
            from efferva.sandbox.opensandbox import OpenSandboxProvider
        except ImportError as error:
            raise RuntimeError(
                "The OpenSandbox provider requires the 'efferva[opensandbox]' extra"
            ) from error
        return OpenSandboxProvider(settings)
    return create_registered_provider(name)


def create_sandbox_control_plane(
    settings: Settings,
    repository: SystemRepository,
) -> SandboxControlPlane:
    return SandboxControlPlane(
        settings,
        repository,
        create_sandbox_provider(settings),
    )
