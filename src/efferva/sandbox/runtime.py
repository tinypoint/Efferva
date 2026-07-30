from __future__ import annotations

import asyncio
import json
from abc import ABC, abstractmethod
from collections import deque
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Protocol
from uuid import uuid4

from efferva.sandbox.types import (
    DirectoryEntry,
    FileMetadata,
    OutputStream,
    ProcessHandle,
    ProcessOutput,
    ProcessOutputChunk,
    ProcessSpec,
)


@dataclass(frozen=True, slots=True)
class TransportOutput:
    stream: OutputStream
    data: bytes


@dataclass(frozen=True, slots=True)
class TransportExited:
    exit_code: int


TransportEvent = TransportOutput | TransportExited


class ProcessTransport(Protocol):
    async def events(self) -> AsyncIterator[TransportEvent]: ...

    async def write(self, data: bytes) -> None: ...

    async def resize(self, cols: int, rows: int) -> None: ...

    async def terminate(self) -> None: ...

    async def close(self) -> None: ...


class _ProcessState:
    def __init__(self, handle: ProcessHandle, spec: ProcessSpec) -> None:
        self.handle = handle
        self.spec = spec
        self.chunks: deque[ProcessOutputChunk] = deque()
        self.next_seq = 1
        self.exit_seq: int | None = None
        self.closed_seq: int | None = None
        self.exit_code: int | None = None
        self.failure: str | None = None
        self.condition = asyncio.Condition()

    async def append(self, stream: OutputStream, data: bytes) -> None:
        if not data:
            return
        async with self.condition:
            self.chunks.append(ProcessOutputChunk(self.next_seq, stream, data))
            self.next_seq += 1
            self.condition.notify_all()

    async def finish(self, exit_code: int, failure: str | None = None) -> None:
        async with self.condition:
            if self.exit_seq is not None:
                return
            self.exit_code = exit_code
            self.failure = failure
            self.exit_seq = self.next_seq
            self.next_seq += 1
            self.closed_seq = self.next_seq
            self.next_seq += 1
            self.condition.notify_all()

    async def output(self, cursor: int | None) -> ProcessOutput:
        after = cursor or 0
        async with self.condition:
            chunks = tuple(chunk for chunk in self.chunks if chunk.seq > after)
            return ProcessOutput(
                chunks=chunks,
                next_cursor=self.next_seq,
                exited=self.exit_seq is not None,
                exit_code=self.exit_code,
                closed=self.closed_seq is not None,
                failure=self.failure,
                exit_seq=self.exit_seq,
                closed_seq=self.closed_seq,
            )


class BufferedSandboxRuntime(ABC):
    """Provider runtime with shared cursor, buffering, and portable filesystem helpers."""

    def __init__(self, workspace_path: str) -> None:
        self.workspace_path = workspace_path
        self._states: dict[str, _ProcessState] = {}
        self._transports: dict[str, ProcessTransport] = {}
        self._tasks: set[asyncio.Task[None]] = set()
        self._closed = False

    async def start_process(self, spec: ProcessSpec) -> ProcessHandle:
        if self._closed:
            raise RuntimeError("sandbox runtime is closed")
        handle = ProcessHandle(uuid4().hex)
        state = _ProcessState(handle, spec)
        transport = await self._launch(spec, handle)
        self._states[handle.id] = state
        self._transports[handle.id] = transport
        task = asyncio.create_task(self._drain(handle, state, transport))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        if spec.initial_stdin:
            await transport.write(spec.initial_stdin)
        return handle

    async def _drain(
        self,
        handle: ProcessHandle,
        state: _ProcessState,
        transport: ProcessTransport,
    ) -> None:
        exited = False
        try:
            async for event in transport.events():
                if isinstance(event, TransportOutput):
                    await state.append(event.stream, event.data)
                else:
                    exited = True
                    await state.finish(event.exit_code)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            await state.finish(1, str(error))
        finally:
            if not exited:
                await state.finish(1, "process transport closed before reporting an exit")
            await transport.close()
            self._transports.pop(handle.id, None)

    async def read_process(
        self,
        process: ProcessHandle,
        cursor: int | None = None,
    ) -> ProcessOutput:
        state = self._states.get(process.id)
        if state is None:
            raise KeyError(f"unknown process {process.id}")
        return await state.output(cursor)

    async def write_stdin(self, process: ProcessHandle, data: bytes) -> None:
        state = self._states.get(process.id)
        transport = self._transports.get(process.id)
        if state is None:
            raise KeyError(f"unknown process {process.id}")
        if transport is None or (not state.spec.pipe_stdin and not state.spec.tty):
            raise BrokenPipeError(f"stdin is closed for process {process.id}")
        await transport.write(data)

    async def resize_pty(self, process: ProcessHandle, cols: int, rows: int) -> None:
        state = self._states.get(process.id)
        transport = self._transports.get(process.id)
        if state is None:
            raise KeyError(f"unknown process {process.id}")
        if not state.spec.tty or transport is None:
            raise RuntimeError(f"process {process.id} has no active PTY")
        await transport.resize(cols, rows)

    async def terminate_process(self, process: ProcessHandle) -> None:
        if process.id not in self._states:
            raise KeyError(f"unknown process {process.id}")
        transport = self._transports.get(process.id)
        if transport is not None:
            await transport.terminate()

    async def forget_process(self, process: ProcessHandle) -> None:
        self._states.pop(process.id, None)

    async def read_file(self, path: str) -> bytes:
        stdout, _ = await self._capture(
            (
                "python3",
                "-c",
                "import sys; sys.stdout.buffer.write(open(sys.argv[1], 'rb').read())",
                path,
            )
        )
        return stdout

    async def write_file(self, path: str, data: bytes) -> None:
        await self._capture(
            (
                "python3",
                "-c",
                (
                    "import os,sys; p=sys.argv[1]; n=int(sys.argv[2]); "
                    "parent=os.path.dirname(p); "
                    "parent and os.makedirs(parent, exist_ok=True); "
                    "open(p, 'wb').write(sys.stdin.buffer.read(n))"
                ),
                path,
                str(len(data)),
            ),
            stdin=data,
        )

    async def list_directory(self, path: str) -> list[DirectoryEntry]:
        stdout, _ = await self._capture(
            (
                "python3",
                "-c",
                (
                    "import json,os,sys; "
                    "print(json.dumps([{'name':e.name,'is_directory':e.is_dir(),"
                    "'is_file':e.is_file()} for e in os.scandir(sys.argv[1])]))"
                ),
                path,
            )
        )
        return [DirectoryEntry(**entry) for entry in json.loads(stdout)]

    async def stat(self, path: str) -> FileMetadata:
        stdout, _ = await self._capture(
            (
                "python3",
                "-c",
                (
                    "import json,os,stat,sys; p=sys.argv[1]; s=os.lstat(p); "
                    "print(json.dumps({'is_directory':stat.S_ISDIR(s.st_mode),"
                    "'is_file':stat.S_ISREG(s.st_mode),"
                    "'is_symlink':stat.S_ISLNK(s.st_mode),'size':s.st_size,"
                    "'created_at_ms':int(s.st_ctime*1000),"
                    "'modified_at_ms':int(s.st_mtime*1000)}))"
                ),
                path,
            )
        )
        return FileMetadata(**json.loads(stdout))

    async def canonicalize(self, path: str) -> str:
        stdout, _ = await self._capture(
            ("python3", "-c", "import os,sys; print(os.path.realpath(sys.argv[1]))", path)
        )
        return stdout.decode().strip()

    async def create_directory(self, path: str, *, recursive: bool = True) -> None:
        script = "os.makedirs(p, exist_ok=True)" if recursive else "os.mkdir(p)"
        await self._capture(("python3", "-c", f"import os,sys; p=sys.argv[1]; {script}", path))

    async def remove(self, path: str, *, recursive: bool, force: bool) -> None:
        await self._capture(
            (
                "python3",
                "-c",
                (
                    "import os,shutil,sys; p=sys.argv[1]; recursive=sys.argv[2]=='1'; "
                    "force=sys.argv[3]=='1'; "
                    "shutil.rmtree(p, ignore_errors=force) if os.path.isdir(p) "
                    "and not os.path.islink(p) and recursive else "
                    "(os.unlink(p) if os.path.lexists(p) else "
                    "(None if force else (_ for _ in ()).throw(FileNotFoundError(p))))"
                ),
                path,
                "1" if recursive else "0",
                "1" if force else "0",
            )
        )

    async def copy(self, source: str, destination: str, *, recursive: bool) -> None:
        await self._capture(
            (
                "python3",
                "-c",
                (
                    "import os,shutil,sys; s,d=sys.argv[1:3]; "
                    "shutil.copytree(s,d,dirs_exist_ok=True) if os.path.isdir(s) "
                    "and sys.argv[3]=='1' else shutil.copy2(s,d)"
                ),
                source,
                destination,
                "1" if recursive else "0",
            )
        )

    async def walk(
        self,
        path: str,
        *,
        max_depth: int,
        max_directories: int,
        max_entries: int,
        follow_symlinks: bool,
        prune_hidden: bool,
    ) -> dict[str, object]:
        stdout, _ = await self._capture(
            (
                "python3",
                "-c",
                _WALK_SCRIPT,
                path,
                str(max_depth),
                str(max_directories),
                str(max_entries),
                "1" if follow_symlinks else "0",
                "1" if prune_hidden else "0",
            )
        )
        return json.loads(stdout)

    async def _capture(
        self,
        argv: tuple[str, ...],
        *,
        stdin: bytes | None = None,
        cwd: str | None = None,
        deadline_seconds: float = 30,
    ) -> tuple[bytes, bytes]:
        spec = ProcessSpec(
            argv=argv,
            cwd=cwd or self.workspace_path,
            pipe_stdin=stdin is not None,
            initial_stdin=stdin,
        )
        handle = await self.start_process(spec)
        cursor = 0
        stdout = bytearray()
        stderr = bytearray()
        async with asyncio.timeout(deadline_seconds):
            while True:
                output = await self.read_process(handle, cursor)
                for chunk in output.chunks:
                    cursor = max(cursor, chunk.seq)
                    target = stderr if chunk.stream == "stderr" else stdout
                    target.extend(chunk.data)
                if output.exited:
                    if output.exit_code != 0:
                        message = (
                            stderr.decode(errors="replace") or output.failure or "command failed"
                        )
                        raise RuntimeError(message)
                    return bytes(stdout), bytes(stderr)
                await asyncio.sleep(0.01)

    async def close(self) -> None:
        self._closed = True
        for transport in tuple(self._transports.values()):
            await transport.terminate()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._states.clear()

    @abstractmethod
    async def _launch(self, spec: ProcessSpec, handle: ProcessHandle) -> ProcessTransport:
        raise NotImplementedError


_WALK_SCRIPT = """
import json, os, sys
root = os.path.realpath(sys.argv[1])
max_depth, max_dirs, max_entries = map(int, sys.argv[2:5])
follow, prune = sys.argv[5] == "1", sys.argv[6] == "1"
entries, errors, dirs, seen, truncated = [], [], 0, 0, False
for current, names, files in os.walk(root, followlinks=follow):
    depth = os.path.relpath(current, root).count(os.sep)
    dirs += 1
    if depth >= max_depth:
        names[:] = []
    if prune:
        names[:] = [name for name in names if not name.startswith(".")]
    if dirs > max_dirs:
        truncated = True
        break
    for name, kind in [(name, "directory") for name in names] + [(name, "file") for name in files]:
        seen += 1
        if seen > max_entries:
            truncated = True
            break
        entries.append({"path": os.path.join(current, name), "kind": kind})
    if truncated:
        break
print(json.dumps({"entries": entries, "errors": errors, "truncated": truncated}))
"""
