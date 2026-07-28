from __future__ import annotations

import asyncio
from uuid import UUID

from efferva.config import Settings
from efferva.repository import SystemRepository
from efferva.sandbox.docker import DockerSandboxProvider
from efferva.sandbox.gateway import ExecutorGateway
from efferva.sandbox.kubernetes import KubernetesSandboxProvider
from efferva.sandbox.registry import create_registered_provider
from efferva.sandbox.types import (
    SandboxContext,
    SandboxEnvironment,
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
        self.gateway = ExecutorGateway(
            settings.executor_gateway_host,
            settings.executor_gateway_port,
        )
        self._locks: dict[UUID, asyncio.Lock] = {}

    async def start(self) -> None:
        await self.gateway.start()

    async def ensure(
        self,
        session_id: UUID,
        workspace_ref: str,
        *,
        owner_id: str,
        fencing_token: int,
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
            await self._repository.upsert_sandbox_lease(
                workspace_id=context.workspace_id,
                provider=self.provider.name,
                external_ref=sandbox.external_ref,
                state=dict(sandbox.state),
                owner_id=owner_id,
                fencing_token=fencing_token,
                lease_ttl_seconds=self._settings.lease_ttl_seconds,
            )
            runtime = await self.provider.connect(sandbox)

            async def validate_fence() -> bool:
                return await self._repository.sandbox_fence_is_current(
                    context.workspace_id,
                    owner_id,
                    fencing_token,
                )

            return self.gateway.register(
                environment_id=str(session_id),
                runtime=runtime,
                workspace_path=context.workspace_path,
                sandbox=sandbox,
                validate_fence=validate_fence,
            )

    async def close(self) -> None:
        await self.gateway.close()
        close = getattr(self.provider, "close", None)
        if close is not None:
            await close()


def create_sandbox_provider(settings: Settings) -> SandboxProvider:
    name = settings.sandbox_provider.lower()
    if name == "docker":
        return DockerSandboxProvider(settings)
    if name in {"kubernetes", "kind"}:
        return KubernetesSandboxProvider(settings)
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
