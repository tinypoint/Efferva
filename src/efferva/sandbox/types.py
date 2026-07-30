from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol
from uuid import UUID

JsonObject = Mapping[str, Any]
OutputStream = Literal["stdout", "stderr", "pty"]


@dataclass(frozen=True, slots=True)
class SandboxCapabilities:
    streaming_exec: bool
    interactive_pty: bool
    persistent_workspace: bool
    snapshots: bool
    suspend_resume: bool
    port_forwarding: bool
    network_policy: bool
    stdin: bool = True
    process_termination: bool = True
    file_operations: bool = True
    concurrent_processes: bool = True

    @property
    def coding_agent_compatible(self) -> bool:
        return all(
            (
                self.streaming_exec,
                self.stdin,
                self.process_termination,
                self.file_operations,
                self.persistent_workspace,
                self.concurrent_processes,
            )
        )


@dataclass(frozen=True, slots=True)
class SandboxContext:
    session_id: UUID
    workspace_id: UUID
    workspace_ref: str
    workspace_path: str = "/session/workspace"
    metadata: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class WorkspaceHandle:
    provider: str
    external_ref: str
    state: JsonObject = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class SandboxHandle:
    provider: str
    external_ref: str
    workspace_id: UUID
    state: JsonObject = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class SandboxEnvironment:
    environment_id: str
    endpoint: str
    workspace_path: str
    sandbox: SandboxHandle
    runtime: SandboxRuntime = field(repr=False, compare=False)
    files: SandboxFiles | None = field(default=None, repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class ProcessSpec:
    argv: tuple[str, ...]
    cwd: str
    env: Mapping[str, str] = field(default_factory=dict)
    tty: bool = False
    pipe_stdin: bool = False
    arg0: str | None = None
    initial_stdin: bytes | None = None

    def __post_init__(self) -> None:
        if not self.argv:
            raise ValueError("process argv must not be empty")
        if not self.cwd.startswith("/"):
            raise ValueError("process cwd must be an absolute path")


@dataclass(frozen=True, slots=True)
class ProcessHandle:
    id: str


@dataclass(frozen=True, slots=True)
class ProcessOutputChunk:
    seq: int
    stream: OutputStream
    data: bytes


@dataclass(frozen=True, slots=True)
class ProcessOutput:
    chunks: tuple[ProcessOutputChunk, ...]
    next_cursor: int
    exited: bool
    exit_code: int | None
    closed: bool
    failure: str | None = None
    exit_seq: int | None = None
    closed_seq: int | None = None


@dataclass(frozen=True, slots=True)
class DirectoryEntry:
    name: str
    is_directory: bool
    is_file: bool


@dataclass(frozen=True, slots=True)
class FileMetadata:
    is_directory: bool
    is_file: bool
    is_symlink: bool
    size: int
    created_at_ms: int
    modified_at_ms: int


class SandboxRuntime(Protocol):
    async def start_process(self, spec: ProcessSpec) -> ProcessHandle: ...

    async def read_process(
        self,
        process: ProcessHandle,
        cursor: int | None = None,
    ) -> ProcessOutput: ...

    async def write_stdin(self, process: ProcessHandle, data: bytes) -> None: ...

    async def resize_pty(self, process: ProcessHandle, cols: int, rows: int) -> None: ...

    async def terminate_process(self, process: ProcessHandle) -> None: ...

    async def read_file(self, path: str) -> bytes: ...

    async def write_file(self, path: str, data: bytes) -> None: ...

    async def list_directory(self, path: str) -> list[DirectoryEntry]: ...

    async def stat(self, path: str) -> FileMetadata: ...


@dataclass(frozen=True, slots=True)
class SandboxFiles:
    """Fence-checked file access for trusted application-side tools."""

    runtime: SandboxRuntime = field(repr=False, compare=False)
    validate_fence: Callable[[], Awaitable[bool]] = field(repr=False, compare=False)

    async def read_file(self, path: str) -> bytes:
        await self._require_current_fence()
        value = await self.runtime.read_file(path)
        await self._require_current_fence()
        return value

    async def stat(self, path: str) -> FileMetadata:
        await self._require_current_fence()
        value = await self.runtime.stat(path)
        await self._require_current_fence()
        return value

    async def _require_current_fence(self) -> None:
        if not await self.validate_fence():
            raise PermissionError("sandbox fencing token is stale")


class SandboxProvider(Protocol):
    name: str
    capabilities: SandboxCapabilities

    async def ensure_workspace(self, context: SandboxContext) -> WorkspaceHandle: ...

    async def start(
        self,
        context: SandboxContext,
        workspace: WorkspaceHandle,
    ) -> SandboxHandle: ...

    async def connect(self, sandbox: SandboxHandle) -> SandboxRuntime: ...

    async def stop(self, sandbox: SandboxHandle) -> None: ...

    async def destroy(self, sandbox: SandboxHandle) -> None: ...
