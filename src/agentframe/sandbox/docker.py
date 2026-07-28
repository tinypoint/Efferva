from __future__ import annotations

import asyncio
import contextlib
import fcntl
import os
import pty
import signal
import struct
import termios
from collections.abc import AsyncIterator

from agentframe.config import Settings
from agentframe.sandbox.command import command
from agentframe.sandbox.runtime import (
    BufferedSandboxRuntime,
    ProcessTransport,
    TransportEvent,
    TransportExited,
    TransportOutput,
)
from agentframe.sandbox.types import (
    ProcessHandle,
    ProcessSpec,
    SandboxCapabilities,
    SandboxContext,
    SandboxHandle,
    WorkspaceHandle,
)

_PROVIDER_LABEL = "agentframe.provider-contract"
_PROVIDER_VERSION = "v1"


class _DockerPipeTransport(ProcessTransport):
    def __init__(
        self,
        process: asyncio.subprocess.Process,
        container: str,
        pid_file: str,
    ) -> None:
        self._process = process
        self._container = container
        self._pid_file = pid_file
        self._queue: asyncio.Queue[TransportEvent | None] = asyncio.Queue()
        self._task = asyncio.create_task(self._collect())

    async def _collect(self) -> None:
        async def pump(
            reader: asyncio.StreamReader | None,
            stream: str,
        ) -> None:
            if reader is None:
                return
            while chunk := await reader.read(64 * 1024):
                await self._queue.put(TransportOutput(stream, chunk))  # type: ignore[arg-type]

        stdout_task = asyncio.create_task(pump(self._process.stdout, "stdout"))
        stderr_task = asyncio.create_task(pump(self._process.stderr, "stderr"))
        exit_code = await self._process.wait()
        await asyncio.gather(stdout_task, stderr_task)
        await self._queue.put(TransportExited(exit_code))
        await self._queue.put(None)

    async def events(self) -> AsyncIterator[TransportEvent]:
        while (event := await self._queue.get()) is not None:
            yield event

    async def write(self, data: bytes) -> None:
        if self._process.stdin is None or self._process.stdin.is_closing():
            raise BrokenPipeError("docker exec stdin is closed")
        self._process.stdin.write(data)
        await self._process.stdin.drain()

    async def resize(self, cols: int, rows: int) -> None:
        raise RuntimeError("process has no PTY")

    async def terminate(self) -> None:
        await _terminate_container_process(self._container, self._pid_file, self._process)

    async def close(self) -> None:
        if not self._task.done():
            await self._task


class _DockerPtyTransport(ProcessTransport):
    def __init__(
        self,
        process: asyncio.subprocess.Process,
        master_fd: int,
        container: str,
        pid_file: str,
    ) -> None:
        self._process = process
        self._master_fd = master_fd
        self._container = container
        self._pid_file = pid_file
        self._queue: asyncio.Queue[TransportEvent | None] = asyncio.Queue()
        self._task = asyncio.create_task(self._collect())

    async def _collect(self) -> None:
        try:
            while True:
                try:
                    chunk = await asyncio.to_thread(os.read, self._master_fd, 64 * 1024)
                except OSError:
                    break
                if not chunk:
                    break
                await self._queue.put(TransportOutput("pty", chunk))
        finally:
            exit_code = await self._process.wait()
            await self._queue.put(TransportExited(exit_code))
            await self._queue.put(None)

    async def events(self) -> AsyncIterator[TransportEvent]:
        while (event := await self._queue.get()) is not None:
            yield event

    async def write(self, data: bytes) -> None:
        await asyncio.to_thread(os.write, self._master_fd, data)

    async def resize(self, cols: int, rows: int) -> None:
        if cols <= 0 or rows <= 0:
            raise ValueError("PTY size must be positive")
        packed = struct.pack("HHHH", rows, cols, 0, 0)
        fcntl.ioctl(self._master_fd, termios.TIOCSWINSZ, packed)
        self._process.send_signal(signal.SIGWINCH)

    async def terminate(self) -> None:
        await _terminate_container_process(self._container, self._pid_file, self._process)

    async def close(self) -> None:
        with contextlib.suppress(OSError):
            os.close(self._master_fd)
        if not self._task.done():
            await self._task


async def _terminate_container_process(
    container: str,
    pid_file: str,
    client_process: asyncio.subprocess.Process,
) -> None:
    if client_process.returncode is not None:
        return
    kill_script = (
        'if [ -r "$1" ]; then pid="$(cat "$1")"; /bin/kill -TERM -- "-$pid" 2>/dev/null || true; fi'
    )
    await command(
        "docker",
        "exec",
        container,
        "/bin/sh",
        "-c",
        kill_script,
        "agentframe-kill",
        pid_file,
        check=False,
    )
    try:
        await asyncio.wait_for(client_process.wait(), 2)
        return
    except TimeoutError:
        pass
    await command(
        "docker",
        "exec",
        container,
        "/bin/sh",
        "-c",
        (
            'if [ -r "$1" ]; then pid="$(cat "$1")"; '
            '/bin/kill -KILL -- "-$pid" 2>/dev/null || true; fi'
        ),
        "agentframe-kill",
        pid_file,
        check=False,
    )
    with contextlib.suppress(ProcessLookupError):
        client_process.terminate()


class DockerSandboxRuntime(BufferedSandboxRuntime):
    def __init__(self, container: str, workspace_path: str) -> None:
        super().__init__(workspace_path)
        self.container = container

    async def _launch(
        self,
        spec: ProcessSpec,
        handle: ProcessHandle,
    ) -> ProcessTransport:
        pid_file = f"/tmp/agentframe-{handle.id}.pid"
        wrapper = (
            'umask 077; pid_file="$1"; shift; '
            'exec 3<&0; setsid "$@" <&3 3<&- & child="$!"; '
            'exec 3<&-; echo "$child" > "$pid_file"; wait "$child"'
        )
        argv = [
            "docker",
            "exec",
            "--interactive",
            "--workdir",
            spec.cwd,
        ]
        for key, value in spec.env.items():
            argv.extend(["--env", f"{key}={value}"])
        if spec.tty:
            argv.append("--tty")
        argv.extend(
            [
                self.container,
                "/bin/sh",
                "-c",
                wrapper,
                "agentframe-process",
                pid_file,
                *spec.argv,
            ]
        )

        if spec.tty:
            master_fd, slave_fd = pty.openpty()
            try:
                process = await asyncio.create_subprocess_exec(
                    *argv,
                    stdin=slave_fd,
                    stdout=slave_fd,
                    stderr=slave_fd,
                )
            finally:
                os.close(slave_fd)
            return _DockerPtyTransport(process, master_fd, self.container, pid_file)

        process = await asyncio.create_subprocess_exec(
            *argv,
            stdin=(
                asyncio.subprocess.PIPE
                if spec.pipe_stdin or spec.initial_stdin is not None
                else asyncio.subprocess.DEVNULL
            ),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        return _DockerPipeTransport(process, self.container, pid_file)


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

    async def ensure_workspace(self, context: SandboxContext) -> WorkspaceHandle:
        volume = f"af-workspace-{context.session_id.hex}"
        await command("docker", "volume", "create", volume)
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
        container = f"af-sandbox-{context.session_id.hex}"
        async with self._lock:
            await self._ensure_network()
            exists, _, _ = await command("docker", "inspect", container, check=False)
            if exists == 0:
                _, contract, _ = await command(
                    "docker",
                    "inspect",
                    "--format",
                    f'{{{{index .Config.Labels "{_PROVIDER_LABEL}"}}}}',
                    container,
                    check=False,
                )
                if contract != _PROVIDER_VERSION:
                    await command("docker", "rm", "--force", container)
                    exists = 1
            if exists != 0:
                await command(
                    "docker",
                    "run",
                    "--detach",
                    "--name",
                    container,
                    "--restart",
                    "unless-stopped",
                    "--cap-drop",
                    "ALL",
                    "--security-opt",
                    "no-new-privileges",
                    "--cpus",
                    self._settings.sandbox_cpu_limit,
                    "--memory",
                    self._settings.docker_sandbox_memory_limit,
                    "--pids-limit",
                    str(self._settings.sandbox_pids_limit),
                    "--network",
                    self._settings.docker_network,
                    "--volume",
                    f"{workspace.external_ref}:{context.workspace_path}",
                    "--label",
                    f"agentframe.session={context.session_id}",
                    "--label",
                    f"{_PROVIDER_LABEL}={_PROVIDER_VERSION}",
                    "--entrypoint",
                    "sleep",
                    self._settings.sandbox_image,
                    "infinity",
                )
            else:
                _, running, _ = await command(
                    "docker",
                    "inspect",
                    "--format",
                    "{{.State.Running}}",
                    container,
                )
                if running != "true":
                    await command("docker", "start", container)
        return SandboxHandle(
            provider=self.name,
            external_ref=container,
            workspace_id=context.workspace_id,
            state={"workspacePath": context.workspace_path},
        )

    async def connect(self, sandbox: SandboxHandle) -> DockerSandboxRuntime:
        runtime = self._runtimes.get(sandbox.external_ref)
        if runtime is None:
            runtime = DockerSandboxRuntime(
                sandbox.external_ref,
                str(sandbox.state.get("workspacePath", self._settings.workspace_path)),
            )
            self._runtimes[sandbox.external_ref] = runtime
        return runtime

    async def stop(self, sandbox: SandboxHandle) -> None:
        runtime = self._runtimes.pop(sandbox.external_ref, None)
        if runtime is not None:
            await runtime.close()
        await command("docker", "stop", sandbox.external_ref, check=False)

    async def destroy(self, sandbox: SandboxHandle) -> None:
        runtime = self._runtimes.pop(sandbox.external_ref, None)
        if runtime is not None:
            await runtime.close()
        await command("docker", "rm", "--force", sandbox.external_ref, check=False)

    async def destroy_workspace(self, workspace: WorkspaceHandle) -> None:
        await command("docker", "volume", "rm", workspace.external_ref, check=False)

    async def close(self) -> None:
        for runtime in tuple(self._runtimes.values()):
            await runtime.close()
        self._runtimes.clear()

    async def _ensure_network(self) -> None:
        exists, _, _ = await command(
            "docker",
            "network",
            "inspect",
            self._settings.docker_network,
            check=False,
        )
        if exists != 0:
            await command("docker", "network", "create", self._settings.docker_network)
