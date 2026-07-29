from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
from collections.abc import Mapping
from copy import deepcopy
from pathlib import Path
from typing import Any

from efferva.sandbox import SandboxEnvironment

logger = logging.getLogger(__name__)

SANDBOX_ENVIRONMENT_ID = "$EFFERVA_SANDBOX_ENVIRONMENT_ID"
SANDBOX_WORKSPACE_PATH = "$EFFERVA_SANDBOX_WORKSPACE_PATH"


def _resolve_sandbox_config(
    value: Any,
    sandbox: SandboxEnvironment,
) -> Any:
    if value == SANDBOX_ENVIRONMENT_ID:
        return sandbox.environment_id
    if value == SANDBOX_WORKSPACE_PATH:
        return sandbox.workspace_path
    if isinstance(value, Mapping):
        return {key: _resolve_sandbox_config(item, sandbox) for key, item in value.items()}
    if isinstance(value, list):
        return [_resolve_sandbox_config(item, sandbox) for item in value]
    return deepcopy(value)


class CodexRpcError(RuntimeError):
    def __init__(self, method: str, error: dict[str, Any]) -> None:
        self.method = method
        self.error = error
        super().__init__(f"{method}: {error.get('message', error)}")


class CodexRuntime:
    def __init__(
        self,
        binary: Path,
        database_url: str,
        *,
        developer_instructions: str | None = None,
        openai_base_url: str | None = None,
        model: str | None = None,
        codex_config: Mapping[str, Any] | None = None,
    ) -> None:
        self._binary = binary
        self._database_url = database_url
        self._developer_instructions = developer_instructions
        self._openai_base_url = openai_base_url
        self._model = model
        self._codex_config = deepcopy(dict(codex_config or {}))
        self._process: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._pending: dict[int, tuple[str, asyncio.Future[dict[str, Any]]]] = {}
        self._thread_queues: dict[str, asyncio.Queue[dict[str, Any]]] = {}
        self._write_lock = asyncio.Lock()
        self._start_lock = asyncio.Lock()
        self._next_id = 1
        self._environments: dict[str, str] = {}
        self._environment_locks: dict[str, asyncio.Lock] = {}

    async def start(self) -> None:
        async with self._start_lock:
            if self.healthy:
                return
            previous_process = self._process
            if previous_process is not None and previous_process.returncode is None:
                previous_process.terminate()
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(previous_process.wait(), 10)
            for task in (self._reader_task, self._stderr_task):
                if task is not None and not task.done():
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await task
            self._environments.clear()
            self._environment_locks.clear()
            environment = os.environ.copy()
            environment["EFFERVA_DATABASE_URL"] = self._database_url
            process = await asyncio.create_subprocess_exec(
                *self._runtime_command(),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=environment,
            )
            self._process = process
            self._reader_task = asyncio.create_task(self._read_loop(process))
            self._stderr_task = asyncio.create_task(self._read_stderr(process))
            await self.request(
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
            await self.notify("notifications/initialized")

    def _runtime_command(self) -> list[str]:
        return [str(self._binary)]

    def _thread_config(self, sandbox: SandboxEnvironment) -> dict[str, Any]:
        config = _resolve_sandbox_config(self._codex_config, sandbox)
        if self._openai_base_url:
            providers = config.setdefault("model_providers", {})
            if not isinstance(providers, dict):
                raise ValueError("Codex config model_providers must be a table")
            providers["efferva_proxy"] = {
                "name": "Efferva LLM proxy",
                "base_url": self._openai_base_url,
                "env_key": "OPENAI_API_KEY",
                "wire_api": "responses",
            }
            config["model_provider"] = "efferva_proxy"
        return config

    async def request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        request_timeout: float = 120,
    ) -> dict[str, Any]:
        await self._ensure_process()
        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        async with self._write_lock:
            request_id = self._next_id
            self._next_id += 1
            self._pending[request_id] = (method, future)
            message: dict[str, Any] = {"id": request_id, "method": method}
            if params is not None:
                message["params"] = params
            await self._write(message)
        try:
            return await asyncio.wait_for(future, request_timeout)
        finally:
            self._pending.pop(request_id, None)

    async def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        await self._ensure_process()
        message: dict[str, Any] = {"method": method}
        if params is not None:
            message["params"] = params
        async with self._write_lock:
            await self._write(message)

    async def ensure_environment(self, sandbox: SandboxEnvironment) -> None:
        lock = self._environment_locks.setdefault(sandbox.environment_id, asyncio.Lock())
        async with lock:
            if self._environments.get(sandbox.environment_id) == sandbox.endpoint:
                return
            # environment/add registers the endpoint but the connection is lazy. Force
            # the handshake before a thread can select it, otherwise a fast first turn
            # can race the remote executor. Re-register after a failed handshake because
            # Codex intentionally caches the failed environment connection.
            deadline = asyncio.get_running_loop().time() + 30
            while True:
                await self.request(
                    "environment/add",
                    {
                        "environmentId": sandbox.environment_id,
                        "execServerUrl": sandbox.endpoint,
                        "connectTimeoutMs": 30_000,
                    },
                )
                try:
                    await self.request(
                        "environment/info",
                        {"environmentId": sandbox.environment_id},
                        request_timeout=5,
                    )
                    break
                except CodexRpcError:
                    if asyncio.get_running_loop().time() >= deadline:
                        raise
                    await asyncio.sleep(0.25)
            self._environments[sandbox.environment_id] = sandbox.endpoint

    async def start_thread(self, sandbox: SandboxEnvironment) -> str:
        params = {
            "cwd": sandbox.workspace_path,
            "approvalPolicy": "never",
            "sandbox": "danger-full-access",
            "environments": [self._environment_selection(sandbox)],
            "runtimeWorkspaceRoots": [sandbox.workspace_path],
        }
        config = self._thread_config(sandbox)
        if config:
            params["config"] = config
        if self._openai_base_url:
            params["modelProvider"] = "efferva_proxy"
        if self._model:
            params["model"] = self._model
        if self._developer_instructions is not None:
            params["developerInstructions"] = self._developer_instructions
        response = await self.request("thread/start", params)
        return str(response["thread"]["id"])

    async def resume_thread(self, thread_id: str, sandbox: SandboxEnvironment) -> None:
        params = {
            "threadId": thread_id,
            "cwd": sandbox.workspace_path,
            "approvalPolicy": "never",
            "sandbox": "danger-full-access",
            "runtimeWorkspaceRoots": [sandbox.workspace_path],
        }
        config = self._thread_config(sandbox)
        if config:
            params["config"] = config
        if self._openai_base_url:
            params["modelProvider"] = "efferva_proxy"
        if self._model:
            params["model"] = self._model
        await self.request("thread/resume", params)

    async def start_turn(
        self,
        thread_id: str,
        prompt: str,
        sandbox: SandboxEnvironment,
    ) -> str:
        response = await self.request(
            "turn/start",
            {
                "threadId": thread_id,
                "input": [
                    {
                        "type": "text",
                        "text": prompt,
                        "textElements": [],
                    }
                ],
                "environments": [self._environment_selection(sandbox)],
                "cwd": sandbox.workspace_path,
                "runtimeWorkspaceRoots": [sandbox.workspace_path],
            },
        )
        return str(response["turn"]["id"])

    def subscribe(self, thread_id: str) -> asyncio.Queue[dict[str, Any]]:
        if thread_id in self._thread_queues:
            raise RuntimeError(f"thread {thread_id} already has an active run")
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._thread_queues[thread_id] = queue
        return queue

    def unsubscribe(self, thread_id: str) -> None:
        self._thread_queues.pop(thread_id, None)

    @property
    def healthy(self) -> bool:
        return (
            self._process is not None
            and self._process.returncode is None
            and self._reader_task is not None
            and not self._reader_task.done()
        )

    async def unload_thread(self, thread_id: str) -> None:
        await self.request("thread/unsubscribe", {"threadId": thread_id})

    async def close(self) -> None:
        process = self._process
        self._process = None
        if process is None:
            return
        if process.stdin is not None:
            process.stdin.close()
            with contextlib.suppress(Exception):
                await process.stdin.wait_closed()
        try:
            await asyncio.wait_for(process.wait(), 10)
        except TimeoutError:
            process.terminate()
            with contextlib.suppress(Exception):
                await process.wait()
        for task in (self._reader_task, self._stderr_task):
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        self._fail_pending(RuntimeError("Codex runtime closed"))

    async def _ensure_process(self) -> None:
        if self._process is None or self._process.returncode is not None:
            raise RuntimeError("Codex runtime is not running")

    async def _write(self, message: dict[str, Any]) -> None:
        assert self._process is not None and self._process.stdin is not None
        payload = json.dumps(message, separators=(",", ":")).encode() + b"\n"
        self._process.stdin.write(payload)
        await self._process.stdin.drain()

    async def _read_loop(self, process: asyncio.subprocess.Process) -> None:
        assert process.stdout is not None
        try:
            while line := await process.stdout.readline():
                message = json.loads(line)
                if "id" in message and ("result" in message or "error" in message):
                    self._resolve_response(message)
                elif "id" in message and "method" in message:
                    await self._reject_server_request(message)
                elif "method" in message:
                    self._route_notification(message)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Codex runtime reader failed")
        finally:
            error = RuntimeError("Codex runtime stream ended")
            self._fail_pending(error)
            self._fail_subscribers(error)

    async def _read_stderr(self, process: asyncio.subprocess.Process) -> None:
        assert process.stderr is not None
        while line := await process.stderr.readline():
            logger.info("codex-runtime: %s", line.decode(errors="replace").rstrip())

    def _resolve_response(self, message: dict[str, Any]) -> None:
        pending = self._pending.get(message["id"])
        if pending is None:
            return
        method, future = pending
        if future.done():
            return
        if "error" in message:
            future.set_exception(CodexRpcError(method, message["error"]))
        else:
            future.set_result(message.get("result") or {})

    async def _reject_server_request(self, message: dict[str, Any]) -> None:
        async with self._write_lock:
            await self._write(
                {
                    "id": message["id"],
                    "error": {
                        "code": -32601,
                        "message": f"Efferva cannot handle server request {message['method']}",
                    },
                }
            )

    def _route_notification(self, message: dict[str, Any]) -> None:
        params = message.get("params") or {}
        thread_id = params.get("threadId")
        if thread_id is None:
            return
        queue = self._thread_queues.get(str(thread_id))
        if queue is not None:
            queue.put_nowait(message)

    def _fail_pending(self, error: Exception) -> None:
        for _, future in self._pending.values():
            if not future.done():
                future.set_exception(error)

    def _fail_subscribers(self, error: Exception) -> None:
        for thread_id, queue in self._thread_queues.items():
            queue.put_nowait(
                {
                    "method": "efferva/runtimeError",
                    "params": {
                        "threadId": thread_id,
                        "message": str(error),
                    },
                }
            )

    @staticmethod
    def _environment_selection(sandbox: SandboxEnvironment) -> dict[str, Any]:
        return {
            "environmentId": sandbox.environment_id,
            "cwd": sandbox.workspace_path,
            "runtimeWorkspaceRoots": [sandbox.workspace_path],
        }
