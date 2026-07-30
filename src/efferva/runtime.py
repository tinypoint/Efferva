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

from efferva.capabilities import SkillRoot
from efferva.sandbox import ProcessHandle, ProcessSpec, SandboxEnvironment
from efferva.tools import Tool, ToolContext, tool_result_text

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


class _SessionCodexRuntime:
    def __init__(
        self,
        binary: Path,
        sandbox: SandboxEnvironment,
        *,
        codex_home_path: str,
        developer_instructions: str | None = None,
        openai_base_url: str | None = None,
        model: str | None = None,
        codex_config: Mapping[str, Any] | None = None,
        tools: tuple[Tool, ...] | list[Tool] | None = None,
        skill_roots: tuple[SkillRoot, ...] | list[SkillRoot] | None = None,
        native_memory_enabled: bool = False,
    ) -> None:
        self._binary = binary
        self._binary_bytes = binary.read_bytes()
        self._sandbox = sandbox
        self._sandbox_runtime = sandbox.runtime
        self._sandbox_binary = "/tmp/efferva-codex-runtime"
        self._codex_home_path = codex_home_path
        self._developer_instructions = developer_instructions
        self._openai_base_url = openai_base_url
        self._model = model
        self._codex_config = deepcopy(dict(codex_config or {}))
        self._tools = {tool.name: tool for tool in tools or ()}
        self._skill_roots = {root.id: root for root in skill_roots or ()}
        if len(self._skill_roots) != len(skill_roots or ()):
            raise ValueError("Efferva SkillRoot ids must be unique")
        self._native_memory_enabled = native_memory_enabled
        self._process: ProcessHandle | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._pending: dict[int, tuple[str, asyncio.Future[dict[str, Any]]]] = {}
        self._thread_queues: dict[str, asyncio.Queue[dict[str, Any]]] = {}
        self._thread_sandboxes: dict[str, SandboxEnvironment] = {}
        self._thread_run_contexts: dict[str, dict[str, Any]] = {}
        self._server_request_tasks: set[asyncio.Task[None]] = set()
        self._write_lock = asyncio.Lock()
        self._start_lock = asyncio.Lock()
        self._next_id = 1
        self._closed = False

    async def start(self) -> None:
        async with self._start_lock:
            if self.healthy:
                return
            previous_process = self._process
            if previous_process is not None:
                with contextlib.suppress(Exception):
                    await self._sandbox_runtime.terminate_process(previous_process)
            for task in (self._reader_task,):
                if task is not None and not task.done():
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await task
            self._thread_sandboxes.clear()
            self._thread_run_contexts.clear()
            await self._cancel_server_request_tasks()
            self._closed = False
            await self._sandbox_runtime.write_file(
                self._sandbox_binary,
                self._binary_bytes,
            )
            await self._run_sandbox_command(
                (
                    "sh",
                    "-lc",
                    (
                        f"mkdir -p {self._codex_home_path} "
                        f"{self._sandbox.workspace_path} && "
                        f"chmod 755 {self._sandbox_binary}"
                    ),
                ),
                cwd="/",
            )
            environment = {
                "CODEX_HOME": self._codex_home_path,
                "HOME": self._codex_home_path,
            }
            api_key = os.environ.get("OPENAI_API_KEY")
            if api_key:
                environment["OPENAI_API_KEY"] = api_key
            process = await self._sandbox_runtime.start_process(
                ProcessSpec(
                    argv=(self._sandbox_binary, "app-server"),
                    cwd=self._sandbox.workspace_path,
                    env=environment,
                    pipe_stdin=True,
                )
            )
            self._process = process
            self._reader_task = asyncio.create_task(self._read_loop(process))
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
        return [self._sandbox_binary, "app-server"]

    def _thread_config(
        self,
        sandbox: SandboxEnvironment,
        runtime_config: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        config = _resolve_sandbox_config(self._codex_config, sandbox)
        thread_config = runtime_config or {}
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
            config.setdefault("model_provider", "efferva_proxy")
        model_provider = thread_config.get("model_provider")
        if model_provider is not None:
            if not isinstance(model_provider, str) or not model_provider.strip():
                raise ValueError("model_provider must be a non-empty string")
            providers = config.get("model_providers", {})
            if model_provider not in providers:
                raise ValueError(f"Unknown Codex model provider: {model_provider}")
            config["model_provider"] = model_provider
        if not self._native_memory_enabled:
            features = config.setdefault("features", {})
            if not isinstance(features, dict):
                raise ValueError("Codex config features must be a table")
            features["memories"] = False
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
        if sandbox.sandbox.external_ref != self._sandbox.sandbox.external_ref:
            raise RuntimeError("Codex runtime is bound to another Session sandbox")
        await self.start()

    async def start_thread(
        self,
        sandbox: SandboxEnvironment,
        runtime_config: Mapping[str, Any] | None = None,
    ) -> str:
        thread_config = runtime_config or {}
        params = {
            "cwd": sandbox.workspace_path,
            "approvalPolicy": "never",
            "sandbox": "danger-full-access",
        }
        config = self._thread_config(sandbox, thread_config)
        if config:
            params["config"] = config
        model_provider = thread_config.get("model_provider")
        if model_provider or self._openai_base_url:
            params["modelProvider"] = model_provider or "efferva_proxy"
        model = thread_config.get("model") or self._model
        if model:
            params["model"] = model
        effort = thread_config.get("reasoning_effort")
        if effort:
            params["reasoningEffort"] = effort
        if self._developer_instructions is not None:
            params["developerInstructions"] = self._developer_instructions
        if self._tools:
            params["dynamicTools"] = [tool.codex_spec() for tool in self._tools.values()]
        response = await self.request("thread/start", params)
        thread_id = str(response["thread"]["id"])
        self._thread_sandboxes[thread_id] = sandbox
        return thread_id

    async def resume_thread(
        self,
        thread_id: str,
        sandbox: SandboxEnvironment,
        runtime_config: Mapping[str, Any] | None = None,
    ) -> None:
        thread_config = runtime_config or {}
        params = {
            "threadId": thread_id,
            "cwd": sandbox.workspace_path,
            "approvalPolicy": "never",
            "sandbox": "danger-full-access",
        }
        config = self._thread_config(sandbox, thread_config)
        if config:
            params["config"] = config
        model_provider = thread_config.get("model_provider")
        if model_provider or self._openai_base_url:
            params["modelProvider"] = model_provider or "efferva_proxy"
        model = thread_config.get("model") or self._model
        if model:
            params["model"] = model
        if self._developer_instructions is not None:
            params["developerInstructions"] = self._developer_instructions
        await self.request("thread/resume", params)
        self._thread_sandboxes[thread_id] = sandbox

    async def start_turn(
        self,
        thread_id: str,
        prompt: str,
        sandbox: SandboxEnvironment,
        *,
        model: str | None = None,
        reasoning_effort: str | None = None,
    ) -> str:
        params: dict[str, Any] = {
            "threadId": thread_id,
            "input": [
                {
                    "type": "text",
                    "text": prompt,
                    "textElements": [],
                }
            ],
            "cwd": sandbox.workspace_path,
        }
        if model:
            params["model"] = model
        if reasoning_effort:
            params["effort"] = reasoning_effort
        response = await self.request(
            "turn/start",
            params,
        )
        return str(response["turn"]["id"])

    def bind_run_context(self, thread_id: str, run: Mapping[str, Any]) -> None:
        self._thread_run_contexts[thread_id] = {
            "run_id": run.get("id"),
            "app_thread_id": run.get("thread_id"),
            "session_id": run.get("session_id"),
            "tenant_id": run.get("tenant_id"),
            "owner_issuer": run.get("owner_issuer"),
            "owner_subject": run.get("owner_subject"),
            "worker_owner_id": run.get("owner_id"),
            "fencing_epoch": run.get("fencing_epoch"),
        }

    async def set_goal(
        self,
        thread_id: str,
        *,
        objective: str | None = None,
        status: str | None = None,
        token_budget: int | None | object = ...,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"threadId": thread_id}
        if objective is not None:
            params["objective"] = objective
        if status is not None:
            params["status"] = status
        if token_budget is not ...:
            params["tokenBudget"] = token_budget
        response = await self.request("thread/goal/set", params)
        return dict(response["goal"])

    async def get_goal(self, thread_id: str) -> dict[str, Any] | None:
        response = await self.request("thread/goal/get", {"threadId": thread_id})
        goal = response.get("goal")
        return dict(goal) if isinstance(goal, Mapping) else None

    async def clear_goal(self, thread_id: str) -> bool:
        response = await self.request("thread/goal/clear", {"threadId": thread_id})
        return bool(response.get("cleared"))

    async def set_memory_mode(self, thread_id: str, mode: str) -> None:
        if mode == "enabled" and not self._native_memory_enabled:
            raise ValueError(
                "Native Codex memory is disabled by product configuration"
            )
        await self.request(
            "thread/memoryMode/set",
            {"threadId": thread_id, "mode": mode},
        )

    async def compact_thread(self, thread_id: str) -> None:
        await self.request("thread/compact/start", {"threadId": thread_id})

    async def interrupt_turn(self, thread_id: str, turn_id: str) -> None:
        await self.request(
            "turn/interrupt",
            {"threadId": thread_id, "turnId": turn_id},
        )

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
            and self._reader_task is not None
            and not self._reader_task.done()
            and not self._closed
        )

    async def unload_thread(self, thread_id: str) -> None:
        try:
            await self.request("thread/unsubscribe", {"threadId": thread_id})
        finally:
            self._thread_sandboxes.pop(thread_id, None)
            self._thread_run_contexts.pop(thread_id, None)

    async def close(self) -> None:
        process = self._process
        self._process = None
        self._closed = True
        if process is None:
            return
        with contextlib.suppress(Exception):
            await self._sandbox_runtime.terminate_process(process)
        for task in (self._reader_task,):
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        await self._cancel_server_request_tasks()
        self._fail_pending(RuntimeError("Codex runtime closed"))

    async def _ensure_process(self) -> None:
        if self._process is None or self._closed:
            raise RuntimeError("Codex runtime is not running")

    async def _write(self, message: dict[str, Any]) -> None:
        assert self._process is not None
        payload = json.dumps(message, separators=(",", ":")).encode() + b"\n"
        await self._sandbox_runtime.write_stdin(self._process, payload)

    async def _read_loop(self, process: ProcessHandle) -> None:
        cursor = 0
        stdout_buffer = b""
        try:
            while True:
                output = await self._sandbox_runtime.read_process(process, cursor)
                cursor = output.next_cursor
                for chunk in output.chunks:
                    if chunk.stream == "stderr":
                        logger.info(
                            "codex-runtime[%s]: %s",
                            self._sandbox.environment_id,
                            chunk.data.decode(errors="replace").rstrip(),
                        )
                        continue
                    stdout_buffer += chunk.data
                    while b"\n" in stdout_buffer:
                        line, stdout_buffer = stdout_buffer.split(b"\n", 1)
                        if line.strip():
                            self._handle_message(json.loads(line))
                if output.exited or output.closed:
                    if stdout_buffer.strip():
                        self._handle_message(json.loads(stdout_buffer))
                    break
                await asyncio.sleep(0.02)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Codex runtime reader failed")
        finally:
            error = RuntimeError("Codex runtime stream ended")
            self._fail_pending(error)
            self._fail_subscribers(error)

    def _handle_message(self, message: dict[str, Any]) -> None:
        if "id" in message and ("result" in message or "error" in message):
            self._resolve_response(message)
        elif "id" in message and "method" in message:
            task = asyncio.create_task(self._handle_server_request(message))
            self._server_request_tasks.add(task)
            task.add_done_callback(self._server_request_completed)
        elif "method" in message:
            self._route_notification(message)

    async def _run_sandbox_command(
        self,
        argv: tuple[str, ...],
        *,
        cwd: str,
        timeout: float = 30,
    ) -> None:
        process = await self._sandbox_runtime.start_process(
            ProcessSpec(argv=argv, cwd=cwd)
        )
        deadline = asyncio.get_running_loop().time() + timeout
        cursor = 0
        output = None
        while asyncio.get_running_loop().time() < deadline:
            output = await self._sandbox_runtime.read_process(process, cursor)
            cursor = output.next_cursor
            if output.exited or output.closed:
                break
            await asyncio.sleep(0.02)
        if output is None or not output.exited:
            with contextlib.suppress(Exception):
                await self._sandbox_runtime.terminate_process(process)
            raise TimeoutError(f"sandbox command timed out: {argv[0]}")
        if output.exit_code != 0:
            detail = b"".join(chunk.data for chunk in output.chunks).decode(
                errors="replace"
            )
            raise RuntimeError(f"sandbox command failed ({output.exit_code}): {detail}")

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

    async def _handle_server_request(self, message: dict[str, Any]) -> None:
        if message["method"] != "item/tool/call":
            await self._send_server_error(
                message["id"],
                -32601,
                f"Efferva cannot handle server request {message['method']}",
            )
            return

        params = message.get("params") or {}
        thread_id = str(params.get("threadId") or "")
        turn_id = str(params.get("turnId") or "")
        call_id = str(params.get("callId") or "")
        namespace = params.get("namespace")
        tool_name = str(params.get("tool") or "")
        arguments = params.get("arguments")
        tool = self._tools.get(tool_name) if namespace is None else None
        sandbox = self._thread_sandboxes.get(thread_id)

        if tool is None:
            await self._send_tool_result(
                message["id"],
                f"Unknown Efferva tool: {tool_name}",
                success=False,
            )
            return
        if sandbox is None:
            await self._send_tool_result(
                message["id"],
                f"No active sandbox is bound to Codex thread {thread_id}",
                success=False,
            )
            return
        if not isinstance(arguments, Mapping):
            await self._send_tool_result(
                message["id"],
                f"Tool {tool_name} arguments must be a JSON object",
                success=False,
            )
            return

        context = ToolContext(
            thread_id=thread_id,
            turn_id=turn_id,
            call_id=call_id,
            sandbox=sandbox,
            **self._thread_run_contexts.get(thread_id, {}),
        )
        try:
            result = await tool.invoke(context, arguments)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.exception("Efferva tool %s failed", tool_name)
            await self._send_tool_result(
                message["id"],
                f"{type(error).__name__}: {error}",
                success=False,
            )
            return
        await self._send_tool_result(message["id"], tool_result_text(result), success=True)

    async def _send_tool_result(self, request_id: Any, text: str, *, success: bool) -> None:
        await self._send_server_result(
            request_id,
            {
                "contentItems": [{"type": "inputText", "text": text}],
                "success": success,
            },
        )

    async def _send_server_result(self, request_id: Any, result: dict[str, Any]) -> None:
        async with self._write_lock:
            await self._write({"id": request_id, "result": result})

    async def _send_server_error(
        self,
        request_id: Any,
        code: int,
        message: str,
    ) -> None:
        async with self._write_lock:
            await self._write({"id": request_id, "error": {"code": code, "message": message}})

    async def _cancel_server_request_tasks(self) -> None:
        tasks = tuple(self._server_request_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._server_request_tasks.clear()

    def _server_request_completed(self, task: asyncio.Task[None]) -> None:
        self._server_request_tasks.discard(task)
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            logger.error(
                "Efferva failed to handle a Codex server request",
                exc_info=(type(error), error, error.__traceback__),
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

    def _selected_skill_roots(
        self,
        sandbox: SandboxEnvironment,
        runtime_config: Mapping[str, Any],
    ) -> list[dict[str, object]]:
        requested = runtime_config.get("skill_roots")
        if requested is None:
            roots = [root for root in self._skill_roots.values() if root.enabled_by_default]
        else:
            if not isinstance(requested, list) or not all(
                isinstance(item, str) for item in requested
            ):
                raise ValueError("skill_roots must be a list of registered root ids")
            unknown = set(requested) - self._skill_roots.keys()
            if unknown:
                names = ", ".join(sorted(unknown))
                raise ValueError(f"Unknown SkillRoot ids: {names}")
            roots = [self._skill_roots[root_id] for root_id in requested]
        return [root.codex_spec(sandbox.environment_id) for root in roots]


class CodexRuntime:
    """Application-side pool of Codex app-servers, one per Session sandbox."""

    def __init__(
        self,
        binary: Path,
        *,
        codex_home_path: str,
        developer_instructions: str | None = None,
        openai_base_url: str | None = None,
        model: str | None = None,
        codex_config: Mapping[str, Any] | None = None,
        tools: tuple[Tool, ...] | list[Tool] | None = None,
        skill_roots: tuple[SkillRoot, ...] | list[SkillRoot] | None = None,
        native_memory_enabled: bool = False,
    ) -> None:
        self._binary = binary
        self._codex_home_path = codex_home_path
        self._options = {
            "developer_instructions": developer_instructions,
            "openai_base_url": openai_base_url,
            "model": model,
            "codex_config": deepcopy(dict(codex_config or {})),
            "tools": tuple(tools or ()),
            "skill_roots": tuple(skill_roots or ()),
            "native_memory_enabled": native_memory_enabled,
        }
        self._sessions: dict[str, _SessionCodexRuntime] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._started = False

    async def start(self) -> None:
        self._started = True

    @property
    def healthy(self) -> bool:
        return self._started

    async def ensure_environment(
        self,
        sandbox: SandboxEnvironment,
    ) -> _SessionCodexRuntime:
        if not self._started:
            raise RuntimeError("Codex runtime pool is not running")
        key = sandbox.environment_id
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            current = self._sessions.get(key)
            if (
                current is not None
                and current.healthy
                and current._sandbox.sandbox.external_ref
                == sandbox.sandbox.external_ref
            ):
                return current
            if current is not None:
                await current.close()
            runtime = _SessionCodexRuntime(
                self._binary,
                sandbox,
                codex_home_path=self._codex_home_path,
                **self._options,
            )
            await runtime.start()
            self._sessions[key] = runtime
            return runtime

    async def release_environment(self, session_id: str) -> None:
        runtime = self._sessions.pop(session_id, None)
        self._locks.pop(session_id, None)
        if runtime is not None:
            await runtime.close()

    async def close(self) -> None:
        self._started = False
        runtimes = tuple(self._sessions.values())
        self._sessions.clear()
        self._locks.clear()
        if runtimes:
            await asyncio.gather(
                *(runtime.close() for runtime in runtimes),
                return_exceptions=True,
            )
