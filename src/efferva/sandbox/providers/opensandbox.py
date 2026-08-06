from __future__ import annotations

import asyncio
import os
import os.path
import shlex
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from hashlib import sha256
from typing import Any, Literal
from uuid import UUID

from opensandbox import Sandbox, SandboxManager
from opensandbox.config import ConnectionConfig
from opensandbox.exceptions.sandbox import SandboxApiException
from opensandbox.models.execd import ExecutionHandlers, RunCommandOpts
from opensandbox.models.filesystem import DirectoryListEntry
from opensandbox.models.sandboxes import (
    PVC,
    CredentialProxyConfig,
    NetworkPolicy,
    NetworkRule,
    SandboxFilter,
    Volume,
)

from efferva.config import SandboxLayout
from efferva.sandbox.protocol import (
    CommandResult,
    DirectoryEntry,
    FileMetadata,
    SandboxCapabilities,
    SandboxContext,
    SandboxEnvironment,
    SandboxHandle,
)

_SESSION_METADATA_KEY = "efferva.session"
_TENANT_METADATA_KEY = "efferva.tenant"
_OWNER_ISSUER_METADATA_KEY = "efferva.owner-issuer"
_OWNER_SUBJECT_METADATA_KEY = "efferva.owner-subject"
_CREDENTIAL_PROXY_METADATA_KEY = "efferva.credential-proxy"
_CPU_METADATA_KEY = "efferva.cpu-limit"
_MEMORY_METADATA_KEY = "efferva.memory-limit"
_VOLUME_SIZE_METADATA_KEY = "efferva.session-volume-size"
_LAYOUT_METADATA_KEY = "efferva.sandbox-layout"


@dataclass(frozen=True, slots=True)
class OpenSandboxCredentialProxy:
    credential: str
    host: str
    scheme: Literal["http", "https"]
    auth_type: Literal["bearer", "api_key"]
    header_name: str | None = None

    def __post_init__(self) -> None:
        if self.auth_type == "api_key" and not self.header_name:
            raise ValueError("api_key credential proxy requires header_name")
        if self.auth_type == "bearer" and self.header_name is not None:
            raise ValueError("bearer credential proxy does not use header_name")


@dataclass(frozen=True, slots=True)
class OpenSandboxConnectionConfig:
    server_url: str
    api_key: str | None = None
    use_server_proxy: bool = True


@dataclass(frozen=True, slots=True)
class OpenSandboxCreateSpec:
    image: str = "python:3.13-slim-bookworm"
    cpu_limit: str = "2"
    memory_limit: str = "2g"
    session_volume_size: str = "10Gi"
    credential_proxy: OpenSandboxCredentialProxy | None = None
    layout: SandboxLayout | None = None


@dataclass(frozen=True, slots=True)
class OpenSandboxInventoryItem:
    id: str
    session_id: UUID
    state: str
    image: str | None
    cpu_limit: str | None
    memory_limit: str | None
    session_volume_size: str | None
    credential_proxy_enabled: bool
    created_at: datetime


@dataclass(frozen=True, slots=True)
class OpenSandboxCreateContext:
    session: SandboxContext
    active_sandboxes: tuple[OpenSandboxInventoryItem, ...]


OpenSandboxCreateSpecResolver = Callable[
    [OpenSandboxCreateContext], Awaitable[OpenSandboxCreateSpec]
]


class OpenSandboxRuntime:
    def __init__(self, sandbox: Sandbox) -> None:
        self.sandbox = sandbox

    async def run_command(
        self,
        argv: tuple[str, ...],
        *,
        cwd: str,
        env: Mapping[str, str] | None = None,
        uid: int | None = None,
        gid: int | None = None,
    ) -> CommandResult:
        stdout = bytearray()
        stderr = bytearray()

        async def on_stdout(message: Any) -> None:
            stdout.extend(message.text.encode())

        async def on_stderr(message: Any) -> None:
            stderr.extend(message.text.encode())

        async def on_error(error: Any) -> None:
            message = getattr(error, "value", None) or str(error)
            stderr.extend(message.encode())

        execution = await self.sandbox.commands.run(
            shlex.join(argv),
            opts=RunCommandOpts(
                working_directory=cwd,
                envs=dict(env or {}) or None,
                uid=uid,
                gid=gid,
            ),
            handlers=ExecutionHandlers(
                on_stdout=on_stdout,
                on_stderr=on_stderr,
                on_error=on_error,
                skip_accumulation=True,
            ),
        )
        exit_code = execution.exit_code
        if exit_code is None:
            exit_code = 1 if execution.error is not None else 0
        return CommandResult(bytes(stdout), bytes(stderr), exit_code)

    async def read_file(self, path: str) -> bytes:
        return await self.sandbox.files.read_bytes(path)

    async def write_file(self, path: str, data: bytes) -> None:
        await self.sandbox.files.write_file(path, data, mode=644)

    async def list_directory(self, path: str) -> list[DirectoryEntry]:
        entries = await self.sandbox.files.list_directory(
            DirectoryListEntry(path=path, depth=1)
        )
        return [
            DirectoryEntry(
                name=os.path.basename(entry.path.rstrip("/")),
                is_directory=entry.entry_type == "directory",
                is_file=entry.entry_type == "file",
            )
            for entry in entries
            if os.path.dirname(entry.path.rstrip("/")) == path.rstrip("/")
        ]

    async def stat(self, path: str) -> FileMetadata:
        try:
            entries = await self.sandbox.files.get_file_info([path])
        except SandboxApiException as error:
            error_code = getattr(getattr(error, "error", None), "code", None)
            if error.status_code == 404 or error_code == "FILE_NOT_FOUND":
                raise FileNotFoundError(path) from error
            raise
        entry = entries.get(path)
        if entry is None:
            raise FileNotFoundError(path)
        return FileMetadata(
            is_directory=entry.entry_type == "directory",
            is_file=entry.entry_type == "file",
            is_symlink=entry.entry_type == "symlink",
            size=entry.size,
            created_at_ms=int(entry.created_at.timestamp() * 1000),
            modified_at_ms=int(entry.modified_at.timestamp() * 1000),
        )

    async def get_endpoint(self, port: int) -> tuple[str, dict[str, str]]:
        endpoint = await self.sandbox.get_endpoint(port)
        return endpoint.endpoint, dict(endpoint.headers)

    async def close(self) -> None:
        await self.sandbox.close()


class OpenSandboxProvider:
    name = "opensandbox"
    capabilities = SandboxCapabilities(
        persistent_session_volume=True,
        port_forwarding=True,
        file_operations=True,
        suspend_resume=True,
        network_policy=True,
    )

    def __init__(
        self,
        connection: OpenSandboxConnectionConfig,
        *,
        layout: SandboxLayout,
        resolve_spec: OpenSandboxCreateSpecResolver,
    ) -> None:
        self._connection = connection
        self._layout = layout
        self._resolve_spec = resolve_spec
        self._manager: SandboxManager | None = None
        self._runtimes: dict[str, OpenSandboxRuntime] = {}

    async def open(self) -> None:
        if self._manager is None:
            self._manager = await SandboxManager.create(self._connection_config())

    async def ensure(self, context: SandboxContext) -> SandboxEnvironment:
        sandbox, runtime, layout = await self._ensure_runtime(context)
        return SandboxEnvironment(
            environment_id=str(context.session_id),
            endpoint=f"sandbox://{sandbox.external_ref}",
            workspace_path=layout.workspace_path,
            layout=layout,
            sandbox=sandbox,
            runtime=runtime,
        )

    async def _ensure_runtime(
        self,
        context: SandboxContext,
    ) -> tuple[SandboxHandle, OpenSandboxRuntime, SandboxLayout]:
        active_sandboxes = await self._list_active_sandboxes(context)
        spec = await self._resolve_spec(
            OpenSandboxCreateContext(
                session=context,
                active_sandboxes=active_sandboxes,
            )
        )
        layout = spec.layout or self._layout
        info = await self._find_sandbox(context.session_id)
        credential_proxy_active = False
        if info is not None:
            if (info.metadata or {}).get(_LAYOUT_METADATA_KEY) != (
                _layout_fingerprint(layout)
            ):
                raise RuntimeError(
                    "sandbox layout changed after creation; recreate the sandbox"
                )
            credential_proxy_active = (info.metadata or {}).get(
                _CREDENTIAL_PROXY_METADATA_KEY
            ) == "true"
            if info.status.state.upper() == "PAUSED":
                await self._require_manager().resume_sandbox(info.id)
            runtime = self._runtimes.get(info.id)
            if runtime is None:
                runtime = OpenSandboxRuntime(
                    await Sandbox.connect(
                        info.id,
                        connection_config=self._connection_config(),
                    )
                )
                self._runtimes[info.id] = runtime
            sandbox_id = info.id
        else:
            credential_proxy = spec.credential_proxy
            sandbox = await Sandbox.create(
                spec.image,
                timeout=None,
                metadata={
                    _SESSION_METADATA_KEY: str(context.session_id),
                    _TENANT_METADATA_KEY: context.tenant_id,
                    _OWNER_ISSUER_METADATA_KEY: context.owner_issuer,
                    _OWNER_SUBJECT_METADATA_KEY: context.owner_subject,
                    _CREDENTIAL_PROXY_METADATA_KEY: str(
                        credential_proxy is not None
                    ).lower(),
                    _CPU_METADATA_KEY: spec.cpu_limit,
                    _MEMORY_METADATA_KEY: spec.memory_limit,
                    _VOLUME_SIZE_METADATA_KEY: spec.session_volume_size,
                    _LAYOUT_METADATA_KEY: _layout_fingerprint(layout),
                },
                resource={
                    "cpu": spec.cpu_limit,
                    "memory": spec.memory_limit,
                },
                credential_proxy=(
                    CredentialProxyConfig(enabled=True)
                    if credential_proxy is not None
                    else None
                ),
                network_policy=(
                    NetworkPolicy(
                        default_action="allow",
                        egress=[
                            NetworkRule(
                                action="allow",
                                target=credential_proxy.host,
                            )
                        ],
                    )
                    if credential_proxy is not None
                    else None
                ),
                volumes=[
                    Volume(
                        name="session-data",
                        pvc=PVC(
                            claim_name=f"efferva-session-{context.session_id.hex}",
                            create_if_not_exists=True,
                            delete_on_sandbox_termination=False,
                            storage=spec.session_volume_size,
                        ),
                        mount_path=layout.identity.home_path,
                    )
                ],
                connection_config=self._connection_config(),
            )
            try:
                if credential_proxy is not None:
                    await self._configure_credential_proxy(
                        sandbox,
                        credential_proxy,
                    )
                    credential_proxy_active = True
            except BaseException:
                await sandbox.close()
                raise
            sandbox_id = sandbox.id
            runtime = OpenSandboxRuntime(sandbox)
            self._runtimes[sandbox_id] = runtime
        handle = SandboxHandle(
            provider=self.name,
            external_ref=sandbox_id,
            session_id=context.session_id,
            state={"credentialProxy": credential_proxy_active},
        )
        return handle, runtime, layout

    async def close(self) -> None:
        for runtime in tuple(self._runtimes.values()):
            await runtime.close()
        self._runtimes.clear()
        if self._manager is not None:
            await self._manager.close()
            self._manager = None

    async def _list_active_sandboxes(
        self,
        context: SandboxContext,
    ) -> tuple[OpenSandboxInventoryItem, ...]:
        infos = await asyncio.gather(
            *(
                self._find_sandbox(session.id)
                for session in context.owner_sessions
                if session.id != context.session_id
            )
        )
        return tuple(self._inventory_item(info) for info in infos if info is not None)

    async def _find_sandbox(self, session_id: UUID) -> Any | None:
        result = await self._require_manager().list_sandbox_infos(
            SandboxFilter(
                states=["RUNNING", "PAUSED"],
                metadata={_SESSION_METADATA_KEY: str(session_id)},
                page_size=10,
                page=1,
            )
        )
        if not result.sandbox_infos:
            return None
        return max(result.sandbox_infos, key=lambda item: item.created_at)

    @staticmethod
    def _inventory_item(info: Any) -> OpenSandboxInventoryItem:
        metadata = info.metadata or {}
        return OpenSandboxInventoryItem(
            id=info.id,
            session_id=UUID(metadata[_SESSION_METADATA_KEY]),
            state=info.status.state.upper(),
            image=getattr(info.image, "image", None),
            cpu_limit=metadata.get(_CPU_METADATA_KEY),
            memory_limit=metadata.get(_MEMORY_METADATA_KEY),
            session_volume_size=metadata.get(_VOLUME_SIZE_METADATA_KEY),
            credential_proxy_enabled=(
                metadata.get(_CREDENTIAL_PROXY_METADATA_KEY) == "true"
            ),
            created_at=info.created_at,
        )

    def _require_manager(self) -> SandboxManager:
        if self._manager is None:
            raise RuntimeError("OpenSandboxProvider is not open")
        return self._manager

    def _connection_config(self) -> ConnectionConfig:
        return ConnectionConfig(
            domain=self._connection.server_url,
            api_key=self._connection.api_key,
            request_timeout=timedelta(minutes=5),
            use_server_proxy=self._connection.use_server_proxy,
            disable_metrics=True,
        )

    async def _configure_credential_proxy(
        self,
        sandbox: Sandbox,
        config: OpenSandboxCredentialProxy,
    ) -> None:
        auth = (
            {
                "type": "bearer",
                "credential": "efferva-engine-credential",
            }
            if config.auth_type == "bearer"
            else {
                "type": "apiKey",
                "name": config.header_name,
                "credential": "efferva-engine-credential",
            }
        )
        await sandbox.credential_vault.create(
            credentials=[
                {
                    "name": "efferva-engine-credential",
                    "source": {"type": "inline", "value": config.credential},
                }
            ],
            bindings=[
                {
                    "name": "efferva-engine-auth",
                    "match": {
                        "schemes": [config.scheme],
                        "hosts": [config.host],
                    },
                    "auth": auth,
                }
            ],
        )


def _layout_fingerprint(layout: SandboxLayout) -> str:
    identity = layout.identity
    value = "\0".join(
        (
            identity.username or "",
            str(identity.uid),
            str(identity.gid),
            identity.home_path,
            layout.workspace_path,
        )
    )
    return sha256(value.encode()).hexdigest()[:32]
