from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import secrets
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import quote, unquote, urlsplit

from efferva.sandbox.types import (
    ProcessHandle,
    ProcessSpec,
    SandboxEnvironment,
    SandboxHandle,
    SandboxRuntime,
)

FenceValidator = Callable[[], Awaitable[bool]]


@dataclass(slots=True)
class _EnvironmentBinding:
    environment_id: str
    token: str
    runtime: SandboxRuntime
    workspace_path: str
    sandbox: SandboxHandle
    validate_fence: FenceValidator
    processes: dict[str, ProcessHandle] = field(default_factory=dict)
    write_ids: dict[str, set[str]] = field(default_factory=dict)
    file_handles: dict[str, bytes] = field(default_factory=dict)


class ExecutorGateway:
    """Loopback exec-server protocol bridge backed by provider-neutral runtimes."""

    def __init__(self, host: str = "127.0.0.1", port: int = 0) -> None:
        self._host = host
        self._port = port
        self._server: Any = None
        self._bindings_by_environment: dict[str, _EnvironmentBinding] = {}
        self._bindings_by_token: dict[str, _EnvironmentBinding] = {}

    async def start(self) -> None:
        if self._server is not None:
            return
        from websockets.asyncio.server import serve

        self._server = await serve(
            self._handle_connection,
            self._host,
            self._port,
            max_size=32 * 1024 * 1024,
            ping_interval=20,
            ping_timeout=20,
        )
        sockets = self._server.sockets
        if not sockets:
            raise RuntimeError("Executor Gateway did not bind a socket")
        self._port = int(sockets[0].getsockname()[1])

    def register(
        self,
        *,
        environment_id: str,
        runtime: SandboxRuntime,
        workspace_path: str,
        sandbox: SandboxHandle,
        validate_fence: FenceValidator,
    ) -> SandboxEnvironment:
        if self._server is None:
            raise RuntimeError("Executor Gateway has not started")
        current = self._bindings_by_environment.get(environment_id)
        if (
            current is not None
            and current.sandbox.external_ref == sandbox.external_ref
            and current.runtime is runtime
        ):
            current.validate_fence = validate_fence
            binding = current
        else:
            if current is not None:
                self._bindings_by_token.pop(current.token, None)
            binding = _EnvironmentBinding(
                environment_id=environment_id,
                token=secrets.token_urlsafe(32),
                runtime=runtime,
                workspace_path=workspace_path,
                sandbox=sandbox,
                validate_fence=validate_fence,
            )
            self._bindings_by_environment[environment_id] = binding
            self._bindings_by_token[binding.token] = binding
        return SandboxEnvironment(
            environment_id=environment_id,
            endpoint=f"ws://127.0.0.1:{self._port}/v1/environments/{binding.token}",
            workspace_path=workspace_path,
            sandbox=sandbox,
        )

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        self._bindings_by_environment.clear()
        self._bindings_by_token.clear()

    async def _handle_connection(self, websocket: Any) -> None:
        token = websocket.request.path.rsplit("/", 1)[-1].split("?", 1)[0]
        binding = self._bindings_by_token.get(token)
        if binding is None:
            await websocket.close(code=1008, reason="unknown environment")
            return
        send_lock = asyncio.Lock()
        watchers: set[asyncio.Task[None]] = set()
        initialized = False
        try:
            async for raw_message in websocket:
                message: Any = {}
                try:
                    message = json.loads(raw_message)
                    method = message.get("method")
                    request_id = message.get("id")
                    params = message.get("params") or {}
                    if method == "initialized" and request_id is None:
                        initialized = True
                        continue
                    if request_id is None:
                        continue
                    if method == "initialize":
                        initialized = True
                        result = {"sessionId": binding.environment_id}
                    else:
                        if not initialized:
                            raise GatewayRpcError(-32002, "environment is not initialized")
                        if not await binding.validate_fence():
                            raise GatewayRpcError(-32003, "sandbox fencing token is stale")
                        result = await self._dispatch(
                            binding,
                            method,
                            params,
                            websocket,
                            send_lock,
                            watchers,
                        )
                    await _send(
                        websocket,
                        send_lock,
                        {"jsonrpc": "2.0", "id": request_id, "result": result},
                    )
                except GatewayRpcError as error:
                    await _send_error(websocket, send_lock, message.get("id"), error)
                except Exception as error:
                    await _send_error(
                        websocket,
                        send_lock,
                        message.get("id") if isinstance(message, dict) else None,
                        GatewayRpcError(-32603, str(error)),
                    )
        finally:
            for watcher in watchers:
                watcher.cancel()
            if watchers:
                await asyncio.gather(*watchers, return_exceptions=True)

    async def _dispatch(
        self,
        binding: _EnvironmentBinding,
        method: str,
        params: dict[str, Any],
        websocket: Any,
        send_lock: asyncio.Lock,
        watchers: set[asyncio.Task[None]],
    ) -> dict[str, Any]:
        if method == "environment/info":
            return {
                "shell": {"name": "bash", "path": "/bin/bash"},
                "cwd": _path_to_uri(binding.workspace_path),
                "capabilities": {"networkProxyLaunch": False},
            }
        if method == "environment/status":
            return {"status": "ready"}
        if method == "process/start":
            logical_id = str(params["processId"])
            if logical_id not in binding.processes:
                spec = ProcessSpec(
                    argv=tuple(str(value) for value in params["argv"]),
                    cwd=_uri_to_path(params["cwd"]),
                    env={str(key): str(value) for key, value in params.get("env", {}).items()},
                    tty=bool(params.get("tty", False)),
                    pipe_stdin=bool(params.get("pipeStdin", False)),
                    arg0=params.get("arg0"),
                )
                handle = await binding.runtime.start_process(spec)
                binding.processes[logical_id] = handle
                binding.write_ids[logical_id] = set()
                watcher = asyncio.create_task(
                    self._watch_process(
                        binding,
                        logical_id,
                        handle,
                        websocket,
                        send_lock,
                    )
                )
                watchers.add(watcher)
                watcher.add_done_callback(watchers.discard)
            return {"processId": logical_id}
        if method == "process/read":
            return await self._read_process(binding, params)
        if method == "process/write":
            logical_id = str(params["processId"])
            handle = binding.processes.get(logical_id)
            if handle is None:
                return {"status": "unknownProcess"}
            write_id = str(params["writeId"])
            if write_id in binding.write_ids.setdefault(logical_id, set()):
                return {"status": "accepted"}
            try:
                await binding.runtime.write_stdin(
                    handle,
                    base64.b64decode(params["chunk"], validate=True),
                )
            except BrokenPipeError:
                return {"status": "stdinClosed"}
            binding.write_ids[logical_id].add(write_id)
            return {"status": "accepted"}
        if method in {"process/signal", "process/terminate"}:
            logical_id = str(params["processId"])
            handle = binding.processes.get(logical_id)
            if handle is None:
                if method == "process/terminate":
                    return {"running": False}
                return {}
            output = await binding.runtime.read_process(handle)
            running = not output.exited
            if running:
                await binding.runtime.terminate_process(handle)
            return {"running": running} if method == "process/terminate" else {}
        if method == "fs/readFile":
            data = await binding.runtime.read_file(_uri_to_path(params["path"]))
            return {"dataBase64": base64.b64encode(data).decode()}
        if method == "fs/writeFile":
            await binding.runtime.write_file(
                _uri_to_path(params["path"]),
                base64.b64decode(params["dataBase64"], validate=True),
            )
            return {}
        if method == "fs/open":
            handle_id = str(params["handleId"])
            binding.file_handles[handle_id] = await binding.runtime.read_file(
                _uri_to_path(params["path"])
            )
            return {"handleId": handle_id}
        if method == "fs/readBlock":
            data = binding.file_handles.get(str(params["handleId"]))
            if data is None:
                raise GatewayRpcError(-32602, "unknown file handle")
            offset = int(params["offset"])
            length = int(params["len"])
            chunk = data[offset : offset + length]
            return {
                "chunk": base64.b64encode(chunk).decode(),
                "eof": offset + len(chunk) >= len(data),
            }
        if method == "fs/close":
            binding.file_handles.pop(str(params["handleId"]), None)
            return {}
        if method == "fs/createDirectory":
            await _runtime_method(binding.runtime, "create_directory")(
                _uri_to_path(params["path"]),
                recursive=params.get("recursive", True),
            )
            return {}
        if method == "fs/getMetadata":
            metadata = await binding.runtime.stat(_uri_to_path(params["path"]))
            return {
                "isDirectory": metadata.is_directory,
                "isFile": metadata.is_file,
                "isSymlink": metadata.is_symlink,
                "size": metadata.size,
                "createdAtMs": metadata.created_at_ms,
                "modifiedAtMs": metadata.modified_at_ms,
            }
        if method == "fs/canonicalize":
            path = await _runtime_method(binding.runtime, "canonicalize")(
                _uri_to_path(params["path"])
            )
            return {"path": _path_to_uri(path)}
        if method == "fs/readDirectory":
            entries = await binding.runtime.list_directory(_uri_to_path(params["path"]))
            return {
                "entries": [
                    {
                        "fileName": entry.name,
                        "isDirectory": entry.is_directory,
                        "isFile": entry.is_file,
                    }
                    for entry in entries
                ]
            }
        if method == "fs/walk":
            options = params["options"]
            result = await _runtime_method(binding.runtime, "walk")(
                _uri_to_path(params["path"]),
                max_depth=int(options["maxDepth"]),
                max_directories=int(options["maxDirectories"]),
                max_entries=int(options["maxEntries"]),
                follow_symlinks=bool(options.get("followDirectorySymlinks", False)),
                prune_hidden=bool(options.get("pruneHiddenDirectories", False)),
            )
            return {
                **result,
                "entries": [
                    {**entry, "path": _path_to_uri(str(entry["path"]))}
                    for entry in result["entries"]
                ],
            }
        if method == "fs/remove":
            await _runtime_method(binding.runtime, "remove")(
                _uri_to_path(params["path"]),
                recursive=params.get("recursive", True),
                force=params.get("force", True),
            )
            return {}
        if method == "fs/copy":
            await _runtime_method(binding.runtime, "copy")(
                _uri_to_path(params["sourcePath"]),
                _uri_to_path(params["destinationPath"]),
                recursive=bool(params["recursive"]),
            )
            return {}
        if method == "capabilityRoots/discoverV1":
            return {
                "roots": [
                    {
                        "id": root["id"],
                        "path": root["path"],
                        "skills": [],
                        "namespaceManifests": [],
                        "warnings": [],
                    }
                    for root in params["roots"]
                ]
            }
        raise GatewayRpcError(-32601, f"unsupported exec-server method: {method}")

    async def _read_process(
        self,
        binding: _EnvironmentBinding,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        logical_id = str(params["processId"])
        handle = binding.processes.get(logical_id)
        if handle is None:
            raise GatewayRpcError(-32602, f"unknown process id {logical_id}")
        after = int(params.get("afterSeq") or 0)
        deadline = asyncio.get_running_loop().time() + int(params.get("waitMs") or 0) / 1000
        while True:
            output = await binding.runtime.read_process(handle, after)
            has_terminal = (
                output.exit_seq is not None
                and output.exit_seq > after
                or output.closed_seq is not None
                and output.closed_seq > after
            )
            if output.chunks or has_terminal or asyncio.get_running_loop().time() >= deadline:
                break
            await asyncio.sleep(min(0.05, max(0, deadline - asyncio.get_running_loop().time())))
        chunks = list(output.chunks)
        max_bytes = params.get("maxBytes")
        if max_bytes is not None:
            retained = []
            total = 0
            for chunk in chunks:
                if retained and total + len(chunk.data) > int(max_bytes):
                    break
                retained.append(chunk)
                total += len(chunk.data)
            chunks = retained
        next_seq = chunks[-1].seq + 1 if chunks and max_bytes is not None else output.next_cursor
        return {
            "chunks": [
                {
                    "seq": chunk.seq,
                    "stream": chunk.stream,
                    "chunk": base64.b64encode(chunk.data).decode(),
                }
                for chunk in chunks
            ],
            "nextSeq": next_seq,
            "exited": output.exited,
            "exitCode": output.exit_code,
            "closed": output.closed,
            "failure": output.failure,
            "sandboxDenied": False,
        }

    async def _watch_process(
        self,
        binding: _EnvironmentBinding,
        logical_id: str,
        handle: ProcessHandle,
        websocket: Any,
        send_lock: asyncio.Lock,
    ) -> None:
        cursor = 0
        while True:
            output = await binding.runtime.read_process(handle, cursor)
            for chunk in output.chunks:
                await _send(
                    websocket,
                    send_lock,
                    {
                        "jsonrpc": "2.0",
                        "method": "process/output",
                        "params": {
                            "processId": logical_id,
                            "seq": chunk.seq,
                            "stream": chunk.stream,
                            "chunk": base64.b64encode(chunk.data).decode(),
                        },
                    },
                )
                cursor = max(cursor, chunk.seq)
            if output.exit_seq is not None and output.exit_seq > cursor:
                await _send(
                    websocket,
                    send_lock,
                    {
                        "jsonrpc": "2.0",
                        "method": "process/exited",
                        "params": {
                            "processId": logical_id,
                            "seq": output.exit_seq,
                            "exitCode": output.exit_code or 0,
                            "sandboxDenied": False,
                        },
                    },
                )
                cursor = output.exit_seq
            if output.closed_seq is not None and output.closed_seq > cursor:
                await _send(
                    websocket,
                    send_lock,
                    {
                        "jsonrpc": "2.0",
                        "method": "process/closed",
                        "params": {"processId": logical_id, "seq": output.closed_seq},
                    },
                )
                return
            await asyncio.sleep(0.02)


class GatewayRpcError(RuntimeError):
    def __init__(self, code: int, message: str) -> None:
        self.code = code
        super().__init__(message)


def _runtime_method(runtime: SandboxRuntime, name: str) -> Any:
    method = getattr(runtime, name, None)
    if method is None:
        raise GatewayRpcError(-32601, f"sandbox runtime does not implement {name}")
    return method


def _uri_to_path(uri: str) -> str:
    parsed = urlsplit(uri)
    if parsed.scheme != "file" or parsed.netloc not in {"", "localhost"}:
        raise GatewayRpcError(-32602, f"unsupported file URI: {uri}")
    path = unquote(parsed.path)
    if not path.startswith("/"):
        raise GatewayRpcError(-32602, f"file URI is not absolute: {uri}")
    return str(PurePosixPath(path))


def _path_to_uri(path: str) -> str:
    normalized = str(PurePosixPath(path))
    if not normalized.startswith("/"):
        raise GatewayRpcError(-32603, f"runtime returned a non-absolute path: {path}")
    return f"file://{quote(normalized, safe='/')}"


async def _send(websocket: Any, lock: asyncio.Lock, message: dict[str, Any]) -> None:
    async with lock:
        await websocket.send(json.dumps(message, separators=(",", ":")))


async def _send_error(
    websocket: Any,
    lock: asyncio.Lock,
    request_id: Any,
    error: GatewayRpcError,
) -> None:
    with contextlib.suppress(Exception):
        await _send(
            websocket,
            lock,
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": error.code, "message": str(error)},
            },
        )
