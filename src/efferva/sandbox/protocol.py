from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol
from uuid import UUID

JsonObject = Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class SandboxCapabilities:
    persistent_session_volume: bool
    port_forwarding: bool
    file_operations: bool
    suspend_resume: bool = False
    network_policy: bool = False

    @property
    def coding_agent_compatible(self) -> bool:
        return all(
            (
                self.persistent_session_volume,
                self.port_forwarding,
                self.file_operations,
            )
        )


@dataclass(frozen=True, slots=True)
class SandboxContext:
    session_id: UUID
    workspace_path: str = "/home/sandbox/workspace"
    metadata: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class SessionVolumeHandle:
    provider: str
    external_ref: str
    state: JsonObject = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class SandboxHandle:
    provider: str
    external_ref: str
    session_id: UUID
    state: JsonObject = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class CommandResult:
    stdout: bytes
    stderr: bytes
    exit_code: int


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
    async def run_command(
        self,
        argv: tuple[str, ...],
        *,
        cwd: str,
        env: Mapping[str, str] | None = None,
        uid: int | None = None,
        gid: int | None = None,
    ) -> CommandResult: ...

    async def read_file(self, path: str) -> bytes: ...

    async def write_file(self, path: str, data: bytes) -> None: ...

    async def list_directory(self, path: str) -> list[DirectoryEntry]: ...

    async def stat(self, path: str) -> FileMetadata: ...

    async def get_endpoint(self, port: int) -> tuple[str, Mapping[str, str]]: ...


@dataclass(frozen=True, slots=True)
class SandboxEnvironment:
    environment_id: str
    endpoint: str
    workspace_path: str
    sandbox: SandboxHandle
    runtime: SandboxRuntime = field(repr=False, compare=False)


class SandboxProvider(Protocol):
    name: str
    capabilities: SandboxCapabilities

    async def open(self) -> None: ...

    async def ensure_session_volume(
        self,
        context: SandboxContext,
    ) -> SessionVolumeHandle: ...

    async def start(
        self,
        context: SandboxContext,
        volume: SessionVolumeHandle,
    ) -> SandboxHandle: ...

    async def connect(self, sandbox: SandboxHandle) -> SandboxRuntime: ...

    async def stop(self, sandbox: SandboxHandle) -> None: ...

    async def destroy(self, sandbox: SandboxHandle) -> None: ...

    async def close(self) -> None: ...
