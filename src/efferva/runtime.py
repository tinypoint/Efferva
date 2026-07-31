from __future__ import annotations

import asyncio
import json
import os
import posixpath
import shlex
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from copy import deepcopy
from hashlib import sha256
from pathlib import Path
from typing import Any
from uuid import UUID

from websockets.asyncio.client import ClientConnection, connect

from efferva.config import Settings
from efferva.sandbox import SandboxControlPlane, SandboxEnvironment


class CodexRpcError(RuntimeError):
    def __init__(self, method: str, error: dict[str, Any]) -> None:
        self.method = method
        self.error = error
        super().__init__(f"{method}: {error.get('message', error)}")


class CodexProxy:
    """A stateless router to the long-lived Codex app-server inside each Sandbox."""

    def __init__(
        self,
        binary: Path,
        settings: Settings,
        sandboxes: SandboxControlPlane,
        *,
        developer_instructions: str | None = None,
        codex_config: Mapping[str, Any] | None = None,
        native_memory_enabled: bool = False,
    ) -> None:
        self._binary_bytes = binary.read_bytes()
        self._binary_sha256 = sha256(self._binary_bytes).hexdigest()
        self._settings = settings
        self._sandboxes = sandboxes
        self._developer_instructions = developer_instructions
        self._codex_config = deepcopy(dict(codex_config or {}))
        self._native_memory_enabled = native_memory_enabled
        self._locks: dict[UUID, asyncio.Lock] = {}
        self._next_id = 1
        self._id_lock = asyncio.Lock()

    @property
    def healthy(self) -> bool:
        return True

    async def request(
        self,
        session: Mapping[str, Any],
        method: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        sandbox = await self._ensure_session(session)
        async with self._connection(sandbox) as websocket:
            return await self._rpc(websocket, method, params or {})

    async def list_threads(self, session: Mapping[str, Any]) -> list[dict[str, Any]]:
        result = await self.request(
            session,
            "thread/list",
            {
                "limit": 100,
                "sortKey": "updated_at",
                "sortDirection": "desc",
            },
        )
        return list(result.get("data") or [])

    async def start_thread(
        self,
        session: Mapping[str, Any],
        *,
        title: str | None = None,
        workspace: str | None = None,
        model: str | None = None,
        reasoning_effort: str | None = None,
    ) -> dict[str, Any]:
        workspace = posixpath.normpath(workspace or self._settings.workspace_path)
        if not workspace.startswith("/"):
            raise ValueError("Thread workspace must be an absolute Sandbox path")
        sandbox = await self._ensure_session(session)
        await self._run_command(
            sandbox,
            (
                "sh",
                "-lc",
                (
                    f"mkdir -p {shlex.quote(workspace)} && "
                    f"chown {self._settings.sandbox_uid}:{self._settings.sandbox_gid} "
                    f"{shlex.quote(workspace)}"
                ),
            ),
        )
        params = self._thread_params(model=model, reasoning_effort=reasoning_effort)
        params["cwd"] = workspace
        async with self._connection(sandbox) as websocket:
            result = await self._rpc(websocket, "thread/start", params)
        thread = dict(result["thread"])
        if title:
            async with self._connection(sandbox) as websocket:
                await self._rpc(
                    websocket,
                    "thread/name/set",
                    {"threadId": thread["id"], "name": title},
                )
            thread["name"] = title
        return thread

    async def read_thread(
        self,
        session: Mapping[str, Any],
        thread_id: str,
    ) -> dict[str, Any]:
        result = await self.request(
            session,
            "thread/read",
            {"threadId": thread_id, "includeTurns": True},
        )
        return dict(result["thread"])

    async def interrupt_turn(
        self,
        session: Mapping[str, Any],
        thread_id: str,
        turn_id: str,
    ) -> None:
        await self.request(
            session,
            "turn/interrupt",
            {"threadId": thread_id, "turnId": turn_id},
        )

    async def stream_turn(
        self,
        session: Mapping[str, Any],
        thread_id: str,
        prompt: str,
        *,
        model: str | None = None,
        reasoning_effort: str | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        sandbox = await self._ensure_session(session)
        async with self._connection(sandbox) as websocket:
            await self._rpc(
                websocket,
                "thread/resume",
                {
                    "threadId": thread_id,
                    **self._thread_params(
                        model=model,
                        reasoning_effort=reasoning_effort,
                    ),
                },
            )
            request_id = await self._request_id()
            params: dict[str, Any] = {
                "threadId": thread_id,
                "input": [{"type": "text", "text": prompt, "textElements": []}],
            }
            if model:
                params["model"] = model
            if reasoning_effort:
                params["effort"] = reasoning_effort
            await websocket.send(
                json.dumps(
                    {"method": "turn/start", "id": request_id, "params": params},
                    separators=(",", ":"),
                )
            )
            turn_id: str | None = None
            while True:
                message = json.loads(await websocket.recv())
                if message.get("id") == request_id:
                    if "error" in message:
                        raise CodexRpcError("turn/start", message["error"])
                    turn = message.get("result", {}).get("turn", {})
                    turn_id = str(turn.get("id")) if turn.get("id") else None
                    yield {
                        "method": "efferva/turn-started",
                        "params": {
                            "threadId": thread_id,
                            "turnId": turn_id,
                        },
                    }
                    continue
                if "method" in message and "id" in message:
                    await self._reject_server_request(websocket, message)
                    continue
                if "method" not in message:
                    continue
                yield message
                if message["method"] == "turn/completed":
                    completed = message.get("params", {}).get("turn", {})
                    completed_id = completed.get("id")
                    if turn_id is None or completed_id in {None, turn_id}:
                        return

    async def _ensure_session(
        self,
        session: Mapping[str, Any],
    ) -> SandboxEnvironment:
        session_id = UUID(str(session["id"]))
        lock = self._locks.setdefault(session_id, asyncio.Lock())
        async with lock:
            sandbox = await self._sandboxes.ensure(
                session_id,
            )
            await self._install_and_start(sandbox)
            return sandbox

    async def _install_and_start(self, sandbox: SandboxEnvironment) -> None:
        runtime_root = posixpath.join(
            self._settings.codex_runtime_dir,
            self._binary_sha256,
        )
        sandbox_binary = posixpath.join(runtime_root, "codex")
        try:
            await sandbox.runtime.stat(sandbox_binary)
        except FileNotFoundError:
            await self._run_command(
                sandbox,
                (
                    "sh",
                    "-lc",
                    f"mkdir -p {shlex.quote(runtime_root)}",
                ),
            )
            await sandbox.runtime.write_file(sandbox_binary, self._binary_bytes)

        codex_home = self._settings.codex_home_path
        pid_file = "/tmp/efferva-app-server.pid"
        log_file = posixpath.join(codex_home, "app-server.log")
        start_lock = "/tmp/efferva-app-server-start.lock"
        listen = f"ws://0.0.0.0:{self._settings.codex_appserver_port}"
        bootstrap = (
            f"if [ -d {shlex.quote(posixpath.join(self._settings.session_volume_path, 'codex-home'))} ] "
            f"&& [ ! -e {shlex.quote(codex_home)} ]; then "
            f"mv {shlex.quote(posixpath.join(self._settings.session_volume_path, 'codex-home'))} "
            f"{shlex.quote(codex_home)}; fi; "
            "if ! id sandbox >/dev/null 2>&1; then "
            "if command -v useradd >/dev/null 2>&1; then "
            f"useradd -u {self._settings.sandbox_uid} "
            f"-d {shlex.quote(self._settings.session_volume_path)} "
            "-M -s /bin/sh sandbox 2>/dev/null || true; "
            "elif command -v adduser >/dev/null 2>&1; then "
            f"adduser -D -u {self._settings.sandbox_uid} "
            f"-h {shlex.quote(self._settings.session_volume_path)} "
            "sandbox 2>/dev/null || true; fi; fi; "
            f"mkdir -p {shlex.quote(codex_home)} "
            f"{shlex.quote(self._settings.workspace_path)} && "
            f"chmod 755 {shlex.quote(sandbox_binary)} && "
            f"chown {self._settings.sandbox_uid}:{self._settings.sandbox_gid} "
            f"{shlex.quote(self._settings.session_volume_path)} "
            f"{shlex.quote(codex_home)} "
            f"{shlex.quote(self._settings.workspace_path)}"
        )
        await self._run_command(sandbox, ("sh", "-lc", bootstrap))
        command = (
            f"if ! mkdir {shlex.quote(start_lock)} 2>/dev/null; then exit 0; fi; "
            f"trap 'rmdir {shlex.quote(start_lock)} 2>/dev/null || true' EXIT; "
            f"if [ -s {shlex.quote(pid_file)} ]; then "
            f"read -r efferva_pid efferva_sha <{shlex.quote(pid_file)}; "
            f"if kill -0 \"$efferva_pid\" 2>/dev/null && "
            f"[ \"$efferva_sha\" = {shlex.quote(self._binary_sha256)} ]; then "
            "exit 0; fi; "
            f"kill \"$efferva_pid\" 2>/dev/null || true; fi; "
            f"cd {shlex.quote(self._settings.workspace_path)} && "
            f"{shlex.quote(sandbox_binary)} app-server "
            f"--listen {shlex.quote(listen)} "
            f"</dev/null >>{shlex.quote(log_file)} 2>&1 & "
            f"echo \"$! {self._binary_sha256}\" >{shlex.quote(pid_file)}"
        )
        environment = {
            "CODEX_HOME": codex_home,
            "HOME": self._settings.session_volume_path,
        }
        api_key = os.environ.get("OPENAI_API_KEY")
        if api_key:
            environment["OPENAI_API_KEY"] = (
                "efferva-credential-proxy"
                if sandbox.sandbox.state.get("credentialProxy")
                else api_key
            )
        await self._run_command(
            sandbox,
            ("sh", "-lc", command),
            env=environment,
        )

    async def _run_command(
        self,
        sandbox: SandboxEnvironment,
        argv: tuple[str, ...],
        *,
        env: Mapping[str, str] | None = None,
        uid: int | None = None,
        gid: int | None = None,
    ) -> None:
        result = await sandbox.runtime.run_command(
            argv,
            cwd="/",
            env=env,
            uid=uid,
            gid=gid,
        )
        if result.exit_code != 0:
            detail = (result.stderr or result.stdout).decode(errors="replace")
            raise RuntimeError(detail or f"sandbox command exited {result.exit_code}")

    @asynccontextmanager
    async def _connection(
        self,
        sandbox: SandboxEnvironment,
    ) -> AsyncIterator[ClientConnection]:
        endpoint, headers = await sandbox.runtime.get_endpoint(
            self._settings.codex_appserver_port
        )
        url = _websocket_url(endpoint)
        last_error: Exception | None = None
        for attempt in range(8):
            try:
                websocket = await connect(
                    url,
                    additional_headers=dict(headers),
                    open_timeout=10,
                    ping_interval=20,
                    ping_timeout=20,
                    max_size=16 * 1024 * 1024,
                )
                break
            except Exception as error:
                last_error = error
                if attempt == 7:
                    raise RuntimeError(
                        f"Codex app-server is not reachable at {url}: {error}"
                    ) from error
                await asyncio.sleep(0.1 * (2**attempt))
        else:
            raise RuntimeError(str(last_error))
        try:
            await self._initialize(websocket)
            yield websocket
        finally:
            await websocket.close()

    async def _initialize(self, websocket: ClientConnection) -> None:
        await self._rpc(
            websocket,
            "initialize",
            {
                "clientInfo": {
                    "name": "efferva",
                    "title": "Efferva",
                    "version": "0.1.0",
                },
                "capabilities": {
                    "experimentalApi": True,
                    "requestAttestation": False,
                },
            },
        )
        await websocket.send(
            json.dumps(
                {"method": "initialized", "params": {}},
                separators=(",", ":"),
            )
        )

    async def _rpc(
        self,
        websocket: ClientConnection,
        method: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        request_id = await self._request_id()
        await websocket.send(
            json.dumps(
                {"method": method, "id": request_id, "params": params},
                separators=(",", ":"),
            )
        )
        while True:
            message = json.loads(await websocket.recv())
            if message.get("id") == request_id:
                if "error" in message:
                    raise CodexRpcError(method, message["error"])
                return dict(message.get("result") or {})
            if "method" in message and "id" in message:
                await self._reject_server_request(websocket, message)

    async def _reject_server_request(
        self,
        websocket: ClientConnection,
        message: Mapping[str, Any],
    ) -> None:
        await websocket.send(
            json.dumps(
                {
                    "id": message["id"],
                    "error": {
                        "code": -32601,
                        "message": f"unsupported server request: {message['method']}",
                    },
                },
                separators=(",", ":"),
            )
        )

    async def _request_id(self) -> int:
        async with self._id_lock:
            value = self._next_id
            self._next_id += 1
            return value

    def _thread_params(
        self,
        *,
        model: str | None = None,
        reasoning_effort: str | None = None,
    ) -> dict[str, Any]:
        config = deepcopy(self._codex_config)
        if self._settings.codex_openai_base_url:
            providers = config.setdefault("model_providers", {})
            providers["efferva_proxy"] = {
                "name": "Efferva LLM proxy",
                "base_url": self._settings.codex_openai_base_url,
                "env_key": "OPENAI_API_KEY",
                "wire_api": "responses",
            }
            config["model_provider"] = "efferva_proxy"
        if not self._native_memory_enabled:
            config.setdefault("features", {})["memories"] = False
        params: dict[str, Any] = {
            "approvalPolicy": "never",
            "sandbox": "dangerFullAccess",
        }
        selected_model = model or self._settings.codex_model
        if selected_model:
            params["model"] = selected_model
        if reasoning_effort:
            params["effort"] = reasoning_effort
        if self._settings.codex_openai_base_url:
            params["modelProvider"] = "efferva_proxy"
        if self._developer_instructions:
            params["developerInstructions"] = self._developer_instructions
        if config:
            params["config"] = config
        return params


def _websocket_url(endpoint: str) -> str:
    if endpoint.startswith("https://"):
        return "wss://" + endpoint.removeprefix("https://")
    if endpoint.startswith("http://"):
        return "ws://" + endpoint.removeprefix("http://")
    if endpoint.startswith(("ws://", "wss://")):
        return endpoint
    return f"ws://{endpoint}"
