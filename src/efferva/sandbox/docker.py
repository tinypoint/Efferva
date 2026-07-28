from __future__ import annotations

import asyncio
import contextlib
import re
from collections.abc import AsyncIterator
from typing import Any

import aiodocker
from aiodocker.exceptions import DockerError

from efferva.config import Settings
from efferva.sandbox.runtime import (
    BufferedSandboxRuntime,
    ProcessTransport,
    TransportEvent,
    TransportExited,
    TransportOutput,
)
from efferva.sandbox.types import (
    ProcessHandle,
    ProcessSpec,
    SandboxCapabilities,
    SandboxContext,
    SandboxHandle,
    WorkspaceHandle,
)

_PROVIDER_LABEL = "efferva.provider-contract"
_PROVIDER_VERSION = "v1"
_MEMORY_PATTERN = re.compile(r"^(?P<value>\d+(?:\.\d+)?)(?P<unit>[kmgt]?i?b?)?$", re.I)
_MEMORY_UNITS = {
    "": 1,
    "b": 1,
    "k": 1024,
    "kb": 1024,
    "ki": 1024,
    "kib": 1024,
    "m": 1024**2,
    "mb": 1024**2,
    "mi": 1024**2,
    "mib": 1024**2,
    "g": 1024**3,
    "gb": 1024**3,
    "gi": 1024**3,
    "gib": 1024**3,
    "t": 1024**4,
    "tb": 1024**4,
    "ti": 1024**4,
    "tib": 1024**4,
}


class _DockerExecTransport(ProcessTransport):
    def __init__(
        self,
        *,
        process: Any,
        stream: Any,
        container: Any,
        pid_file: str,
        tty: bool,
    ) -> None:
        self._process = process
        self._stream = stream
        self._container = container
        self._pid_file = pid_file
        self._tty = tty
        self._closed = False
        self._forced_exit_code: int | None = None

    async def events(self) -> AsyncIterator[TransportEvent]:
        try:
            while message := await self._stream.read_out():
                stream = "pty" if self._tty else ("stderr" if message.stream == 2 else "stdout")
                if message.data:
                    yield TransportOutput(stream, message.data)
        except (ConnectionError, RuntimeError):
            if self._forced_exit_code is None:
                raise
        exit_code = (
            self._forced_exit_code
            if self._forced_exit_code is not None
            else await _wait_for_exec_exit(self._process)
        )
        yield TransportExited(exit_code)

    async def write(self, data: bytes) -> None:
        try:
            await self._stream.write_in(data)
        except (ConnectionError, RuntimeError) as error:
            raise BrokenPipeError("Docker Exec stdin is closed") from error

    async def resize(self, cols: int, rows: int) -> None:
        if not self._tty:
            raise RuntimeError("process has no PTY")
        if cols <= 0 or rows <= 0:
            raise ValueError("PTY size must be positive")
        await self._process.resize(w=cols, h=rows)

    async def terminate(self) -> None:
        # Mark the caller-requested exit before sending the signal. The stream reader
        # can observe EOF as soon as the process group dies; setting this afterwards
        # leaves a race where both coroutines try to inspect a hijacked Exec session.
        self._forced_exit_code = 143
        try:
            await _terminate_container_process(self._container, self._pid_file)
        except Exception:
            self._forced_exit_code = None
            raise
        await self._stream.close()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._stream.__aexit__(None, None, None)


class DockerSandboxRuntime(BufferedSandboxRuntime):
    def __init__(self, container: Any, workspace_path: str) -> None:
        super().__init__(workspace_path)
        self.container = container

    async def _launch(
        self,
        spec: ProcessSpec,
        handle: ProcessHandle,
    ) -> ProcessTransport:
        pid_file = f"/tmp/efferva-{handle.id}.pid"
        wrapper = (
            'umask 077; pid_file="$1"; shift; '
            'exec 3<&0; setsid "$@" <&3 3<&- & child="$!"; '
            'exec 3<&-; echo "$child" > "$pid_file"; wait "$child"'
        )
        process = await self.container.exec(
            [
                "/bin/sh",
                "-c",
                wrapper,
                "efferva-process",
                pid_file,
                *spec.argv,
            ],
            stdout=True,
            stderr=not spec.tty,
            stdin=True,
            tty=spec.tty,
            environment=spec.env,
            workdir=spec.cwd,
        )
        stream = process.start()
        await stream.__aenter__()
        return _DockerExecTransport(
            process=process,
            stream=stream,
            container=self.container,
            pid_file=pid_file,
            tty=spec.tty,
        )


class DockerSandboxProvider:
    name = "docker"
    capabilities = SandboxCapabilities(
        streaming_exec=True,
        interactive_pty=True,
        persistent_workspace=True,
        snapshots=False,
        suspend_resume=True,
        port_forwarding=False,
        network_policy=False,
    )

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._lock = asyncio.Lock()
        self._runtimes: dict[str, DockerSandboxRuntime] = {}
        self._client: aiodocker.Docker | None = None

    async def ensure_workspace(self, context: SandboxContext) -> WorkspaceHandle:
        volume = f"af-workspace-{context.session_id.hex}"
        client = self._docker()
        try:
            await client.volumes.get(volume)
        except DockerError as error:
            if error.status != 404:
                raise
            await client.volumes.create(
                {
                    "Name": volume,
                    "Labels": {
                        "efferva.session": str(context.session_id),
                        _PROVIDER_LABEL: _PROVIDER_VERSION,
                    },
                }
            )
        return WorkspaceHandle(
            provider=self.name,
            external_ref=volume,
            state={"mountPath": context.workspace_path},
        )

    async def start(
        self,
        context: SandboxContext,
        workspace: WorkspaceHandle,
    ) -> SandboxHandle:
        container_name = f"af-sandbox-{context.session_id.hex}"
        client = self._docker()
        async with self._lock:
            await self._ensure_network()
            container = await self._get_container(container_name)
            if container is not None:
                details = await container.show()
                labels = details.get("Config", {}).get("Labels") or {}
                if labels.get(_PROVIDER_LABEL) != _PROVIDER_VERSION:
                    await container.delete(force=True)
                    container = None
            if container is None:
                container = await client.containers.create(
                    {
                        "Image": self._settings.sandbox_image,
                        "Entrypoint": ["sleep"],
                        "Cmd": ["infinity"],
                        "Labels": {
                            "efferva.session": str(context.session_id),
                            _PROVIDER_LABEL: _PROVIDER_VERSION,
                        },
                        "HostConfig": {
                            "Binds": [f"{workspace.external_ref}:{context.workspace_path}"],
                            "CapDrop": ["ALL"],
                            "Memory": _parse_memory(self._settings.docker_sandbox_memory_limit),
                            "NanoCpus": int(
                                float(self._settings.sandbox_cpu_limit) * 1_000_000_000
                            ),
                            "NetworkMode": self._settings.docker_network,
                            "PidsLimit": self._settings.sandbox_pids_limit,
                            "RestartPolicy": {"Name": "unless-stopped"},
                            "SecurityOpt": ["no-new-privileges"],
                        },
                    },
                    name=container_name,
                )
                await container.start()
            else:
                details = await container.show()
                if not bool(details.get("State", {}).get("Running")):
                    await container.start()
        return SandboxHandle(
            provider=self.name,
            external_ref=container_name,
            workspace_id=context.workspace_id,
            state={"workspacePath": context.workspace_path},
        )

    async def connect(self, sandbox: SandboxHandle) -> DockerSandboxRuntime:
        runtime = self._runtimes.get(sandbox.external_ref)
        if runtime is None:
            container = await self._docker().containers.get(sandbox.external_ref)
            runtime = DockerSandboxRuntime(
                container,
                str(sandbox.state.get("workspacePath", self._settings.workspace_path)),
            )
            self._runtimes[sandbox.external_ref] = runtime
        return runtime

    async def stop(self, sandbox: SandboxHandle) -> None:
        runtime = self._runtimes.pop(sandbox.external_ref, None)
        if runtime is not None:
            await runtime.close()
        container = await self._get_container(sandbox.external_ref)
        if container is not None:
            with contextlib.suppress(DockerError):
                await container.stop(t=2)

    async def destroy(self, sandbox: SandboxHandle) -> None:
        runtime = self._runtimes.pop(sandbox.external_ref, None)
        if runtime is not None:
            await runtime.close()
        container = await self._get_container(sandbox.external_ref)
        if container is not None:
            await container.delete(force=True)

    async def destroy_workspace(self, workspace: WorkspaceHandle) -> None:
        try:
            volume = await self._docker().volumes.get(workspace.external_ref)
        except DockerError as error:
            if error.status == 404:
                return
            raise
        await volume.delete(force=True)

    async def close(self) -> None:
        for runtime in tuple(self._runtimes.values()):
            await runtime.close()
        self._runtimes.clear()
        if self._client is not None:
            await self._client.close()
            self._client = None

    def _docker(self) -> aiodocker.Docker:
        if self._client is None:
            self._client = aiodocker.Docker()
        return self._client

    async def _get_container(self, name: str) -> Any | None:
        try:
            return await self._docker().containers.get(name)
        except DockerError as error:
            if error.status == 404:
                return None
            raise

    async def _ensure_network(self) -> None:
        client = self._docker()
        try:
            await client.networks.get(self._settings.docker_network)
        except DockerError as error:
            if error.status != 404:
                raise
            await client.networks.create(
                {
                    "Name": self._settings.docker_network,
                    "CheckDuplicate": True,
                }
            )


async def _terminate_container_process(container: Any, pid_file: str) -> None:
    script = (
        'if [ -r "$1" ]; then pid="$(cat "$1")"; '
        '/bin/kill -TERM -- "-$pid" 2>/dev/null || true; sleep 0.2; '
        '/bin/kill -KILL -- "-$pid" 2>/dev/null || true; fi'
    )
    process = await container.exec(
        ["/bin/sh", "-c", script, "efferva-kill", pid_file],
        stdout=True,
        stderr=True,
        stdin=False,
        tty=False,
    )
    stream = process.start()
    async with stream:
        while await stream.read_out():
            pass


async def _wait_for_exec_exit(process: Any) -> int:
    async with asyncio.timeout(10):
        while True:
            details = await process.inspect()
            if not details["Running"]:
                exit_code = details.get("ExitCode")
                return int(exit_code) if exit_code is not None else 1
            await asyncio.sleep(0.01)


def _parse_memory(value: str) -> int:
    match = _MEMORY_PATTERN.fullmatch(value.strip())
    if match is None:
        raise ValueError(f"invalid Docker memory limit: {value!r}")
    unit = (match.group("unit") or "").lower()
    return int(float(match.group("value")) * _MEMORY_UNITS[unit])
