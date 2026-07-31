from __future__ import annotations

import asyncio
import os
import os.path
import shlex
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlparse

from opensandbox import Sandbox, SandboxManager
from opensandbox.config import ConnectionConfig
from opensandbox.exceptions.sandbox import SandboxApiException
from opensandbox.models.execd import ExecutionHandlers, RunCommandOpts
from opensandbox.models.filesystem import DirectoryListEntry
from opensandbox.models.sandboxes import (
    PVC,
    CredentialProxyConfig,
    SandboxFilter,
    Volume,
)

from efferva.config import Settings, get_settings
from efferva.sandbox.protocol import (
    CommandResult,
    DirectoryEntry,
    FileMetadata,
    SandboxCapabilities,
    SandboxContext,
    SandboxHandle,
    SessionVolumeHandle,
)

_SESSION_METADATA_KEY = "efferva.session"
_CREDENTIAL_PROXY_METADATA_KEY = "efferva.credential-proxy"


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

    def __init__(self, settings: Settings | None = None) -> None:
        settings = settings or get_settings()
        if not settings.opensandbox_server_url:
            raise ValueError(
                "EFFERVA_OPENSANDBOX_SERVER_URL is required for the OpenSandbox provider"
            )
        self._settings = settings
        self._lock = asyncio.Lock()
        self._manager: SandboxManager | None = None
        self._sandboxes: dict[str, Sandbox] = {}
        self._runtimes: dict[str, OpenSandboxRuntime] = {}

    async def ensure_session_volume(
        self,
        context: SandboxContext,
    ) -> SessionVolumeHandle:
        return SessionVolumeHandle(
            provider=self.name,
            external_ref=f"efferva-session-{context.session_id.hex}",
            state={
                "mountPath": self._settings.session_volume_path,
                "size": self._settings.session_volume_size,
                "deletedRetentionDays": (
                    self._settings.deleted_session_volume_retention_days
                ),
            },
        )

    async def start(
        self,
        context: SandboxContext,
        volume: SessionVolumeHandle,
    ) -> SandboxHandle:
        async with self._lock:
            info = await self._find_sandbox(context)
            credential_proxy = self._credential_proxy_target()
            credential_proxy_active = False
            if info is not None:
                credential_proxy_active = (
                    (info.metadata or {}).get(_CREDENTIAL_PROXY_METADATA_KEY) == "true"
                )
                if info.status.state.upper() == "PAUSED":
                    await (await self._get_manager()).resume_sandbox(info.id)
                sandbox = await Sandbox.connect(
                    info.id,
                    connection_config=self._connection_config(),
                )
            else:
                sandbox = await Sandbox.create(
                    self._settings.sandbox_image,
                    timeout=None,
                    metadata={
                        _SESSION_METADATA_KEY: str(context.session_id),
                        _CREDENTIAL_PROXY_METADATA_KEY: str(
                            credential_proxy is not None
                        ).lower(),
                    },
                    resource={
                        "cpu": self._settings.sandbox_cpu_limit,
                        "memory": self._settings.sandbox_memory_limit,
                    },
                    credential_proxy=(
                        CredentialProxyConfig(enabled=True)
                        if credential_proxy is not None
                        else None
                    ),
                    volumes=[
                        Volume(
                            name="session-data",
                            pvc=PVC(
                                claim_name=volume.external_ref,
                                create_if_not_exists=True,
                                delete_on_sandbox_termination=False,
                                storage=self._settings.session_volume_size,
                            ),
                            mount_path=self._settings.session_volume_path,
                        )
                    ],
                    connection_config=self._connection_config(),
                )
                if credential_proxy is not None:
                    await self._configure_credential_proxy(
                        sandbox,
                        host=credential_proxy[0],
                        scheme=credential_proxy[1],
                    )
                    credential_proxy_active = True
            self._sandboxes[sandbox.id] = sandbox
        return SandboxHandle(
            provider=self.name,
            external_ref=sandbox.id,
            session_id=context.session_id,
            state={"credentialProxy": credential_proxy_active},
        )

    async def connect(self, sandbox: SandboxHandle) -> OpenSandboxRuntime:
        runtime = self._runtimes.get(sandbox.external_ref)
        if runtime is not None:
            return runtime
        client = self._sandboxes.get(sandbox.external_ref)
        if client is None:
            client = await Sandbox.connect(
                sandbox.external_ref,
                connection_config=self._connection_config(),
            )
            self._sandboxes[sandbox.external_ref] = client
        runtime = OpenSandboxRuntime(client)
        self._runtimes[sandbox.external_ref] = runtime
        return runtime

    async def stop(self, sandbox: SandboxHandle) -> None:
        await self._close_runtime(sandbox.external_ref)
        await (await self._get_manager()).pause_sandbox(sandbox.external_ref)

    async def destroy(self, sandbox: SandboxHandle) -> None:
        await self._close_runtime(sandbox.external_ref)
        self._sandboxes.pop(sandbox.external_ref, None)
        await (await self._get_manager()).kill_sandbox(sandbox.external_ref)

    async def close(self) -> None:
        for sandbox_id in tuple(self._runtimes):
            await self._close_runtime(sandbox_id)
        for sandbox in tuple(self._sandboxes.values()):
            await sandbox.close()
        self._sandboxes.clear()
        if self._manager is not None:
            await self._manager.close()
            self._manager = None

    async def _find_sandbox(self, context: SandboxContext) -> Any | None:
        result = await (await self._get_manager()).list_sandbox_infos(
            SandboxFilter(
                states=["RUNNING", "PAUSED"],
                metadata={_SESSION_METADATA_KEY: str(context.session_id)},
                page_size=10,
                page=1,
            )
        )
        if not result.sandbox_infos:
            return None
        return max(result.sandbox_infos, key=lambda item: item.created_at)

    async def _get_manager(self) -> SandboxManager:
        if self._manager is None:
            self._manager = await SandboxManager.create(self._connection_config())
        return self._manager

    def _connection_config(self) -> ConnectionConfig:
        assert self._settings.opensandbox_server_url is not None
        return ConnectionConfig(
            domain=self._settings.opensandbox_server_url,
            api_key=self._settings.opensandbox_api_key,
            use_server_proxy=self._settings.opensandbox_use_server_proxy,
            disable_metrics=True,
        )

    async def _close_runtime(self, sandbox_id: str) -> None:
        runtime = self._runtimes.pop(sandbox_id, None)
        if runtime is not None:
            await runtime.close()
        self._sandboxes.pop(sandbox_id, None)

    def _credential_proxy_target(self) -> tuple[str, str] | None:
        if not self._settings.opensandbox_credential_proxy_enabled:
            return None
        if not os.environ.get("OPENAI_API_KEY"):
            return None
        parsed = urlparse(
            self._settings.codex_openai_base_url or "https://api.openai.com/v1"
        )
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return None
        default_port = 443 if parsed.scheme == "https" else 80
        if parsed.port not in {None, default_port}:
            return None
        return parsed.hostname, parsed.scheme

    async def _configure_credential_proxy(
        self,
        sandbox: Sandbox,
        *,
        host: str,
        scheme: str,
    ) -> None:
        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key:
            return
        await sandbox.credential_vault.create(
            credentials=[
                {
                    "name": "efferva-openai-api-key",
                    "source": {"type": "inline", "value": api_key},
                }
            ],
            bindings=[
                {
                    "name": "efferva-openai-bearer",
                    "match": {"schemes": [scheme], "hosts": [host]},
                    "auth": {
                        "type": "bearer",
                        "credential": "efferva-openai-api-key",
                    },
                }
            ],
        )
