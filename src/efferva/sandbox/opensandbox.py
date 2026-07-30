from __future__ import annotations

import asyncio
import contextlib
import os
import os.path
import shlex
from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Any
from urllib.parse import urlparse

from opensandbox import Sandbox, SandboxManager
from opensandbox.config import ConnectionConfig
from opensandbox.models.execd import ExecutionHandlers, RunCommandOpts
from opensandbox.models.filesystem import DirectoryListEntry
from opensandbox.models.sandboxes import (
    CredentialProxyConfig,
    PVC,
    SandboxFilter,
    Volume,
)

from efferva.config import Settings
from efferva.sandbox.runtime import (
    BufferedSandboxRuntime,
    ProcessTransport,
    TransportEvent,
    TransportExited,
    TransportOutput,
)
from efferva.sandbox.types import (
    DirectoryEntry,
    FileMetadata,
    ProcessHandle,
    ProcessSpec,
    SandboxCapabilities,
    SandboxContext,
    SandboxHandle,
    WorkspaceHandle,
)

_SESSION_METADATA_KEY = "efferva.session"
_WORKSPACE_METADATA_KEY = "efferva.workspace"
_CREDENTIAL_PROXY_METADATA_KEY = "efferva.credential-proxy"


class _OpenSandboxExecTransport(ProcessTransport):
    def __init__(self, sandbox: Sandbox, spec: ProcessSpec, handle: ProcessHandle) -> None:
        self._sandbox = sandbox
        self._spec = spec
        self._handle = handle
        self._queue: asyncio.Queue[TransportEvent | None] = asyncio.Queue()
        self._started = asyncio.Event()
        self._execution_id: str | None = None
        self._stdin_path = (
            f"/tmp/efferva-{handle.id}.stdin"
            if spec.pipe_stdin or spec.initial_stdin is not None
            else None
        )
        self._task = asyncio.create_task(self._run())

    async def wait_started(self) -> None:
        started = asyncio.create_task(self._started.wait())
        try:
            done, _ = await asyncio.wait(
                {started, self._task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if self._task in done and not self._started.is_set():
                await self._task
                raise RuntimeError("OpenSandbox command exited before it started")
            await started
        finally:
            if not started.done():
                started.cancel()

    async def _run(self) -> None:
        async def on_init(event: Any) -> None:
            self._execution_id = str(event.id)
            self._started.set()

        async def on_stdout(message: Any) -> None:
            await self._queue.put(TransportOutput("stdout", message.text.encode()))

        async def on_stderr(message: Any) -> None:
            await self._queue.put(TransportOutput("stderr", message.text.encode()))

        async def on_error(error: Any) -> None:
            message = getattr(error, "value", None) or str(error)
            await self._queue.put(TransportOutput("stderr", message.encode()))

        handlers = ExecutionHandlers(
            on_init=on_init,
            on_stdout=on_stdout,
            on_stderr=on_stderr,
            on_error=on_error,
            skip_accumulation=True,
        )
        command = shlex.join(self._spec.argv)
        if self._stdin_path is not None:
            command = (
                f"stdin_path={shlex.quote(self._stdin_path)}; "
                'rm -f "$stdin_path"; mkfifo -m 600 "$stdin_path"; '
                "trap 'rm -f \"$stdin_path\"' EXIT; "
                f'exec {command} < "$stdin_path"'
            )
        try:
            execution = await self._sandbox.commands.run(
                command,
                opts=RunCommandOpts(
                    working_directory=self._spec.cwd,
                    envs=dict(self._spec.env) or None,
                ),
                handlers=handlers,
            )
            if self._execution_id is None and execution.id is not None:
                self._execution_id = str(execution.id)
            self._started.set()
            exit_code = execution.exit_code
            if exit_code is None:
                exit_code = 1 if execution.error is not None else 0
            await self._queue.put(TransportExited(exit_code))
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self._started.set()
            await self._queue.put(
                TransportOutput("stderr", f"OpenSandbox command failed: {error}".encode())
            )
            await self._queue.put(TransportExited(1))
        finally:
            await self._queue.put(None)

    async def events(self) -> AsyncIterator[TransportEvent]:
        while (event := await self._queue.get()) is not None:
            yield event

    async def write(self, data: bytes) -> None:
        if self._stdin_path is None:
            raise BrokenPipeError("stdin is not enabled for this process")
        script = (
            "import base64,sys; "
            "data=base64.b64decode(sys.argv[2]); "
            "open(sys.argv[1], 'wb', buffering=0).write(data)"
        )
        execution = await self._sandbox.commands.run(
            shlex.join(("python3", "-c", script, self._stdin_path, _b64(data))),
            opts=RunCommandOpts(timeout=timedelta(seconds=10)),
        )
        if execution.exit_code not in {None, 0}:
            raise BrokenPipeError("OpenSandbox stdin writer failed")

    async def resize(self, cols: int, rows: int) -> None:
        raise RuntimeError("OpenSandbox execd does not expose PTY resize")

    async def terminate(self) -> None:
        if self._execution_id is not None:
            await self._sandbox.commands.interrupt(self._execution_id)

    async def close(self) -> None:
        if not self._task.done():
            with contextlib.suppress(Exception):
                await self.terminate()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await self._task


class OpenSandboxRuntime(BufferedSandboxRuntime):
    def __init__(self, sandbox: Sandbox, workspace_path: str) -> None:
        super().__init__(workspace_path)
        self.sandbox = sandbox

    async def _launch(
        self,
        spec: ProcessSpec,
        handle: ProcessHandle,
    ) -> ProcessTransport:
        if spec.tty:
            raise RuntimeError("OpenSandbox provider does not support interactive PTY")
        transport = _OpenSandboxExecTransport(self.sandbox, spec, handle)
        await transport.wait_started()
        return transport

    async def read_file(self, path: str) -> bytes:
        return await self.sandbox.files.read_bytes(path)

    async def write_file(self, path: str, data: bytes) -> None:
        await self.sandbox.files.write_file(path, data, mode=644)

    async def list_directory(self, path: str) -> list[DirectoryEntry]:
        entries = await self.sandbox.files.list_directory(DirectoryListEntry(path=path, depth=1))
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
        entries = await self.sandbox.files.get_file_info([path])
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

    async def close(self) -> None:
        await super().close()
        await self.sandbox.close()


class OpenSandboxProvider:
    name = "opensandbox"
    capabilities = SandboxCapabilities(
        streaming_exec=True,
        interactive_pty=False,
        persistent_workspace=True,
        snapshots=True,
        suspend_resume=True,
        port_forwarding=True,
        network_policy=True,
        stdin=True,
    )

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._lock = asyncio.Lock()
        self._manager: SandboxManager | None = None
        self._sandboxes: dict[str, Sandbox] = {}
        self._runtimes: dict[str, OpenSandboxRuntime] = {}

    async def ensure_workspace(self, context: SandboxContext) -> WorkspaceHandle:
        volume = f"efferva-workspace-{context.workspace_id.hex}"
        return WorkspaceHandle(
            provider=self.name,
            external_ref=volume,
            state={
                "mountPath": self._settings.session_volume_path,
                "workspacePath": context.workspace_path,
                "codexHomePath": self._settings.codex_home_path,
                "size": self._settings.session_volume_size,
                "deletedRetentionDays": (
                    self._settings.deleted_session_volume_retention_days
                ),
            },
        )

    async def start(
        self,
        context: SandboxContext,
        workspace: WorkspaceHandle,
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
                        _WORKSPACE_METADATA_KEY: str(context.workspace_id),
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
                            name="workspace",
                            pvc=PVC(
                                claim_name=workspace.external_ref,
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
            workspace_id=context.workspace_id,
            state={
                "workspacePath": context.workspace_path,
                "credentialProxy": credential_proxy_active,
            },
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
        runtime = OpenSandboxRuntime(
            client,
            str(sandbox.state.get("workspacePath", self._settings.workspace_path)),
        )
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
                metadata={_WORKSPACE_METADATA_KEY: str(context.workspace_id)},
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


def _b64(data: bytes) -> str:
    import base64

    return base64.b64encode(data).decode()
