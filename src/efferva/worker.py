from __future__ import annotations

import asyncio
import json
import os
import signal
import time
from collections.abc import AsyncIterator, Awaitable, Mapping
from contextlib import suppress
from contextvars import ContextVar
from importlib.resources import files
from typing import Any
from uuid import uuid4

from prometheus_client import Gauge, start_http_server

from efferva.agui import resume_agui_turn, stream_agui_turn
from efferva.broker import RedisRunBroker, RunQueueFullError
from efferva.codex import CodexGateway
from efferva.codex_appserver import CodexAppServerManager
from efferva.codex_release import prepare_official_codex
from efferva.codex_rpc import (
    CodexRpcClient,
    ServerRequestHandler,
    default_server_response,
)
from efferva.codex_tunnel import (
    CodexTunnelBackpressureError,
    RedisCodexTunnel,
)
from efferva.config import Settings, get_settings, load_codex_config, merge_codex_config
from efferva.db import Database
from efferva.repository import RunRepository
from efferva.sandbox import SandboxProvider
from efferva.sandbox.manager import create_sandbox_control_plane

ACTIVE_RUNS = Gauge(
    "efferva_worker_active_runs",
    "Active Codex runs held by this worker",
)
RUN_CAPACITY = Gauge(
    "efferva_worker_run_capacity",
    "Maximum concurrent Codex runs accepted by this worker",
)
WORKER_READY = Gauge(
    "efferva_worker_ready",
    "Whether this worker has initialized all required dependencies",
)
ACTIVE_CODEX_CONNECTIONS = Gauge(
    "efferva_worker_active_codex_connections",
    "Active native Codex connections held by this worker",
)
CODEX_CONNECTION_CAPACITY = Gauge(
    "efferva_worker_codex_connection_capacity",
    "Maximum native Codex connections accepted by this worker",
)

_CURRENT_RUN_ID: ContextVar[str | None] = ContextVar(
    "efferva_current_run_id",
    default=None,
)

_INTERACTIVE_SERVER_REQUESTS = {
    "item/tool/requestUserInput",
    "item/commandExecution/requestApproval",
    "item/fileChange/requestApproval",
    "item/permissions/requestApproval",
    "mcpServer/elicitation/request",
}


def _interrupt_from_server_request(
    interrupt_id: str,
    method: str,
    params: Mapping[str, Any],
) -> dict[str, Any]:
    reason = (
        "input_required"
        if method in {
            "item/tool/requestUserInput",
            "mcpServer/elicitation/request",
        }
        else "confirmation"
    )
    message = str(params.get("reason") or params.get("message") or "").strip()
    if not message:
        if method == "item/tool/requestUserInput":
            questions = params.get("questions") or []
            message = "\n".join(
                str(question.get("question") or "")
                for question in questions
                if isinstance(question, Mapping)
            ).strip()
        elif method == "item/commandExecution/requestApproval":
            message = str(params.get("command") or "Approve command execution?")
        elif method == "item/fileChange/requestApproval":
            message = "Approve the requested file changes?"
        elif method == "item/permissions/requestApproval":
            message = "Approve the requested sandbox permissions?"
        else:
            message = "Input is required to continue."
    return {
        "id": interrupt_id,
        "reason": reason,
        "message": message,
        "toolCallId": str(params.get("itemId") or "") or None,
        "metadata": {"method": method, "params": dict(params)},
    }


def _server_response_from_interrupt(
    method: str,
    params: Mapping[str, Any],
    response: Mapping[str, Any],
) -> dict[str, Any]:
    if response.get("status") == "resolved" and isinstance(
        response.get("payload"), Mapping
    ):
        return dict(response["payload"])
    default = default_server_response(method, params)
    if default is None:
        raise RuntimeError(f"interrupt {method} was cancelled without a default")
    return default


class EffervaWorker:
    def __init__(
        self,
        gateway: CodexGateway,
        broker: RedisRunBroker,
        runs: RunRepository,
        rpc: CodexRpcClient,
        tunnel: RedisCodexTunnel,
        settings: Settings | None = None,
        server_request_handler: ServerRequestHandler | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._gateway = gateway
        self._broker = broker
        self._runs = runs
        self._rpc = rpc
        self._tunnel = tunnel
        self._worker_id = os.environ.get("HOSTNAME") or f"worker-{uuid4()}"
        self._server_request_handler = server_request_handler
        self._active: dict[str, asyncio.Task[None]] = {}
        self._connections: dict[str, asyncio.Task[None]] = {}
        self._pending_interrupts: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._stopping = asyncio.Event()

    async def handle_server_request(
        self,
        session: Mapping[str, Any],
        method: str,
        params: Mapping[str, Any],
    ) -> dict[str, Any]:
        if method not in _INTERACTIVE_SERVER_REQUESTS:
            if self._server_request_handler is not None:
                response = self._server_request_handler(session, method, params)
                if isinstance(response, Awaitable):
                    response = await response
                return dict(response)
            default = default_server_response(method, params)
            if default is None:
                raise NotImplementedError(f"unsupported server request: {method}")
            return default

        run_id = _CURRENT_RUN_ID.get()
        if run_id is None:
            raise RuntimeError(f"server request {method} is not attached to a Run")
        state = await self._broker.get_run_state(run_id)
        session_id = str(session["id"])
        thread_id = str(params.get("threadId") or state.get("threadId") or "")
        if not thread_id or thread_id == "new":
            raise RuntimeError(f"server request {method} has no materialized thread")

        interrupt_id = str(uuid4())
        future: asyncio.Future[dict[str, Any]] = (
            asyncio.get_running_loop().create_future()
        )
        self._pending_interrupts[interrupt_id] = future
        interrupt = _interrupt_from_server_request(interrupt_id, method, params)
        event_id = await self._broker.publish_event(
            run_id,
            {
                "type": "RUN_FINISHED",
                "runId": str(state.get("clientRunId") or run_id),
                "threadId": thread_id,
                "outcome": {"type": "interrupt", "interrupts": [interrupt]},
            },
            state={"status": "waiting_input"},
        )
        await self._broker.set_pending_interrupt(
            session_id,
            thread_id,
            run_id,
            event_id,
        )
        await self._runs.update(run_id, status="waiting_input")
        try:
            response = await future
            return _server_response_from_interrupt(method, params, response)
        finally:
            self._pending_interrupts.pop(interrupt_id, None)
            await self._broker.clear_pending_interrupt(session_id, thread_id)
            await self._broker.set_run_state(run_id, {"status": "running"})
            await self._runs.update(run_id, status="running")

    async def run(self) -> None:
        RUN_CAPACITY.set(self._settings.worker_concurrency)
        CODEX_CONNECTION_CAPACITY.set(self._settings.worker_concurrency)
        await self._runs.ping()
        await self._broker.ping()
        await self._tunnel.ping()
        await self._reconcile_queued_runs()
        WORKER_READY.set(1)
        loop = asyncio.get_running_loop()
        next_health_check = loop.time() + 5
        for name in (signal.SIGTERM, signal.SIGINT):
            with suppress(NotImplementedError):
                loop.add_signal_handler(name, self._stopping.set)

        connection_dispatch = asyncio.create_task(
            self._accept_codex_connections(),
            name="efferva-codex-connections",
        )
        try:
            while not self._stopping.is_set():
                if connection_dispatch.done():
                    connection_dispatch.result()
                if loop.time() >= next_health_check:
                    await self._runs.ping()
                    await self._broker.ping()
                    await self._tunnel.ping()
                    await self._reconcile_queued_runs()
                    next_health_check = loop.time() + 5
                self._remove_finished()
                capacity = self._settings.worker_concurrency - len(self._active)
                if capacity <= 0:
                    await self._wait_for_capacity()
                    continue

                claimed = await self._broker.reclaim_stale_runs(
                    self._worker_id,
                    min_idle_ms=self._settings.worker_claim_idle_seconds * 1000,
                    count=capacity,
                )
                capacity -= len(claimed)
                if capacity > 0:
                    claimed.extend(
                        await self._broker.claim_new_runs(
                            self._worker_id,
                            count=capacity,
                        )
                    )
                for dispatch_id, command in claimed:
                    run_id = str(command.get("runId") or "")
                    if not run_id:
                        await self._broker.acknowledge_run(dispatch_id)
                        continue
                    if run_id in self._active:
                        continue
                    task = asyncio.create_task(
                        self._execute(dispatch_id, command),
                        name=f"efferva-run:{run_id}",
                    )
                    self._active[run_id] = task
                    ACTIVE_RUNS.set(len(self._active))
        finally:
            self._stopping.set()
            connection_dispatch.cancel()
            with suppress(asyncio.CancelledError):
                await connection_dispatch
            await self._drain()
            await self._drain_codex_connections()

    async def _accept_codex_connections(self) -> None:
        while not self._stopping.is_set():
            self._remove_finished_codex_connections()
            capacity = self._settings.worker_concurrency - len(self._connections)
            if capacity <= 0:
                await self._wait_for_codex_connection_capacity()
                continue

            claimed = await self._tunnel.reclaim_stale_connections(
                self._worker_id,
                min_idle_ms=self._settings.worker_claim_idle_seconds * 1000,
                count=capacity,
            )
            capacity -= len(claimed)
            if capacity > 0:
                claimed.extend(
                    await self._tunnel.claim_new_connections(
                        self._worker_id,
                        count=capacity,
                    )
                )
            for dispatch_id, command in claimed:
                connection_id = str(command.get("connectionId") or "")
                session_id = str(command.get("sessionId") or "")
                if not connection_id or not session_id:
                    await self._tunnel.acknowledge_connection(dispatch_id)
                    continue
                if connection_id in self._connections:
                    continue
                task = asyncio.create_task(
                    self._serve_codex_connection(dispatch_id, command),
                    name=f"efferva-codex:{connection_id}",
                )
                self._connections[connection_id] = task
                ACTIVE_CODEX_CONNECTIONS.set(len(self._connections))

    async def _serve_codex_connection(
        self,
        dispatch_id: str,
        command: dict[str, Any],
    ) -> None:
        connection_id = str(command["connectionId"])
        session_id = str(command["sessionId"])
        acquired = await self._tunnel.acquire_connection_lease(
            connection_id,
            self._worker_id,
        )
        if not acquired:
            return

        owner = asyncio.current_task()
        lease_task = asyncio.create_task(
            self._renew_codex_connection_lease(
                connection_id,
                dispatch_id,
                owner,
            )
        )
        try:
            state = await self._tunnel.get_state(connection_id)
            if state.get("status") in {"closed", "failed"}:
                await self._tunnel.acknowledge_connection(dispatch_id)
                return
            if state.get("status") == "ready":
                await self._finish_codex_connection(
                    connection_id,
                    dispatch_id,
                    status="failed",
                    error="the worker holding this Codex connection was lost",
                )
                return
            if not await self._tunnel.client_is_connected(connection_id):
                await self._finish_codex_connection(
                    connection_id,
                    dispatch_id,
                    status="closed",
                )
                return

            await self._tunnel.set_state(
                connection_id,
                {"status": "connecting", "workerId": self._worker_id},
            )
            async with self._rpc.raw_connection({"id": session_id}) as upstream:
                if not await self._tunnel.client_is_connected(connection_id):
                    await self._finish_codex_connection(
                        connection_id,
                        dispatch_id,
                        status="closed",
                    )
                    return
                await self._tunnel.set_state(
                    connection_id,
                    {"status": "ready", "workerId": self._worker_id},
                )
                tasks = {
                    asyncio.create_task(
                        self._codex_client_to_server(connection_id, upstream)
                    ),
                    asyncio.create_task(
                        self._codex_server_to_client(connection_id, upstream)
                    ),
                    asyncio.create_task(self._watch_codex_client(connection_id)),
                }
                done, pending = await asyncio.wait(
                    tasks,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
                for task in done:
                    task.result()
            await self._finish_codex_connection(
                connection_id,
                dispatch_id,
                status="closed",
            )
        except asyncio.CancelledError:
            await self._finish_codex_connection(
                connection_id,
                dispatch_id,
                status="failed",
                error="the worker closed this Codex connection",
            )
            raise
        except Exception as error:
            await self._finish_codex_connection(
                connection_id,
                dispatch_id,
                status="failed",
                error=str(error),
            )
        finally:
            lease_task.cancel()
            with suppress(asyncio.CancelledError):
                await lease_task
            await self._tunnel.release_connection_lease(
                connection_id,
                self._worker_id,
            )

    async def _codex_client_to_server(
        self,
        connection_id: str,
        upstream: Any,
    ) -> None:
        cursor = "0-0"
        while True:
            frames = await self._tunnel.read_frames(
                connection_id,
                "client",
                after=cursor,
            )
            for frame_id, kind, payload in frames:
                cursor = frame_id
                if kind == "close":
                    await self._tunnel.acknowledge_frame(
                        connection_id,
                        "client",
                        frame_id,
                    )
                    return
                await upstream.send(payload)
                await self._tunnel.acknowledge_frame(
                    connection_id,
                    "client",
                    frame_id,
                )

    async def _codex_server_to_client(
        self,
        connection_id: str,
        upstream: Any,
    ) -> None:
        async for payload in upstream:
            if not isinstance(payload, str):
                raise TypeError("Codex JSON-RPC requires text frames")
            await self._tunnel.send_frame(
                connection_id,
                "server",
                payload=payload,
            )

    async def _watch_codex_client(self, connection_id: str) -> None:
        while True:
            await asyncio.sleep(self._tunnel.heartbeat_interval_seconds)
            if not await self._tunnel.client_is_connected(connection_id):
                return

    async def _renew_codex_connection_lease(
        self,
        connection_id: str,
        dispatch_id: str,
        owner: asyncio.Task[Any] | None,
    ) -> None:
        while True:
            await asyncio.sleep(self._tunnel.heartbeat_interval_seconds)
            try:
                await self._tunnel.touch_connection_claim(
                    self._worker_id,
                    dispatch_id,
                )
                renewed = await self._tunnel.renew_connection_lease(
                    connection_id,
                    self._worker_id,
                )
            except Exception:
                renewed = False
            if not renewed:
                if owner is not None:
                    owner.cancel()
                return

    async def _finish_codex_connection(
        self,
        connection_id: str,
        dispatch_id: str,
        *,
        status: str,
        error: str | None = None,
    ) -> None:
        values: dict[str, Any] = {"status": status}
        if error:
            values["error"] = error
        await self._tunnel.set_state(connection_id, values)
        with suppress(CodexTunnelBackpressureError):
            await self._tunnel.send_frame(
                connection_id,
                "server",
                kind="close",
            )
        await self._tunnel.acknowledge_connection(dispatch_id)

    async def _reconcile_queued_runs(self) -> None:
        for run in await self._runs.list_queued():
            try:
                await self._broker.enqueue_run(dict(run["command"]))
            except RunQueueFullError:
                return
            except ValueError as error:
                await self._runs.update(
                    str(run["id"]),
                    status="failed",
                    error=str(error),
                )

    async def _execute(self, dispatch_id: str, command: dict[str, Any]) -> None:
        run_id = str(command["runId"])
        acquired = await self._broker.acquire_lease(
            run_id,
            self._worker_id,
            ttl_seconds=self._settings.worker_lease_seconds,
        )
        if not acquired:
            return

        session_id = str(command["sessionId"])
        requested_thread_id = str(command["threadId"])
        previous_state = await self._broker.get_run_state(run_id)
        previous_status = str(previous_state.get("status") or "")
        if previous_status in {"completed", "failed", "interrupted"}:
            try:
                await self._runs.update(run_id, status=previous_status)
                await self._broker.acknowledge_run(dispatch_id)
            finally:
                await self._broker.release_lease(run_id, self._worker_id)
            return
        lease_thread_id = str(
            previous_state.get("threadId") or requested_thread_id
        )
        owns_thread = lease_thread_id != "new"
        if owns_thread:
            while not await self._broker.acquire_thread_lease(
                session_id,
                lease_thread_id,
                run_id,
                ttl_seconds=self._settings.worker_lease_seconds,
            ):
                await self._broker.touch_run_claim(self._worker_id, dispatch_id)
                renewed = await self._broker.renew_lease(
                    run_id,
                    self._worker_id,
                    ttl_seconds=self._settings.worker_lease_seconds,
                )
                if not renewed:
                    return
                await asyncio.sleep(1)

        await self._runs.update(
            run_id,
            status="running",
            worker_id=self._worker_id,
            thread_id=_optional_string(previous_state.get("threadId")),
            turn_id=_optional_string(previous_state.get("turnId")),
        )

        owner = asyncio.current_task()
        lease_context: dict[str, str | None] = {
            "threadId": lease_thread_id if owns_thread else None
        }
        lease_task = asyncio.create_task(
            self._renew_lease(
                run_id,
                owner,
                dispatch_id=dispatch_id,
                session_id=session_id,
                lease_context=lease_context,
            )
        )
        command_task: asyncio.Task[None] | None = None
        context: dict[str, Any] = {
            "sessionId": str(command["sessionId"]),
            "threadId": str(command["threadId"]),
            "turnId": command.get("turnId"),
            "startedAt": previous_state.get("startedAt") or time.time(),
            "finished": False,
            "openMessages": set(previous_state.get("openMessages") or []),
            "openReasoning": set(previous_state.get("openReasoning") or []),
            "openToolCalls": set(previous_state.get("openToolCalls") or []),
        }
        try:
            command_task = asyncio.create_task(self._consume_commands(run_id, context))
            token = _CURRENT_RUN_ID.set(run_id)
            try:
                await self._stream_run(command, context, lease_context)
            finally:
                _CURRENT_RUN_ID.reset(token)
            await self._broker.acknowledge_run(dispatch_id)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            await self._broker.publish_event(
                run_id,
                {
                    "type": "RUN_ERROR",
                    "code": "WORKER_ERROR",
                    "message": str(error),
                },
                state={"status": "failed"},
            )
            await self._runs.update(run_id, status="failed", error=str(error))
            await self._broker.acknowledge_run(dispatch_id)
        finally:
            context["finished"] = True
            if command_task is not None:
                command_task.cancel()
                with suppress(asyncio.CancelledError):
                    await command_task
            lease_task.cancel()
            with suppress(asyncio.CancelledError):
                await lease_task
            await self._broker.release_lease(run_id, self._worker_id)
            leased_thread_id = lease_context.get("threadId")
            if leased_thread_id is not None:
                await self._broker.release_thread_lease(
                    session_id,
                    leased_thread_id,
                    run_id,
                )

    async def _stream_run(
        self,
        command: dict[str, Any],
        context: dict[str, Any],
        lease_context: dict[str, str | None],
    ) -> None:
        run_id = str(command["runId"])
        kind = str(command.get("kind") or "")
        if kind not in {"start", "resume"}:
            raise ValueError(f"unknown Run kind: {kind or '<missing>'}")
        agui_run_id = str(command.get("clientRunId") or run_id)
        session = {"id": command["sessionId"]}
        state = await self._broker.get_run_state(run_id)
        resumed_thread_id = state.get("threadId")
        resumed_turn_id = state.get("turnId")
        recovery_thread_id = str(resumed_thread_id or command["threadId"])
        if (
            state.get("status") == "running"
            and not resumed_turn_id
            and recovery_thread_id != "new"
        ):
            resumed_turn_id = await self._gateway.find_active_turn(
                session,
                recovery_thread_id,
            )
            if resumed_turn_id:
                resumed_thread_id = recovery_thread_id
                context["threadId"] = recovery_thread_id
                context["turnId"] = resumed_turn_id
                recovery_event = {
                    "type": "RAW",
                    "event": {
                        "method": "efferva/turn-started",
                        "params": {
                            "threadId": recovery_thread_id,
                            "turnId": resumed_turn_id,
                            "recovered": True,
                        },
                    },
                }
                await self._broker.publish_event(
                    run_id,
                    recovery_event,
                    state={
                        "threadId": recovery_thread_id,
                        "turnId": resumed_turn_id,
                    },
                    session_id=str(command["sessionId"]),
                    thread_id=recovery_thread_id,
                    turn_id=resumed_turn_id,
                )
                await self._runs.update(
                    run_id,
                    thread_id=recovery_thread_id,
                    turn_id=resumed_turn_id,
                )
        events: AsyncIterator[dict[str, Any]]
        if resumed_thread_id and resumed_turn_id and state.get("status") == "running":
            stream = resume_agui_turn(
                self._gateway,
                session,
                str(resumed_thread_id),
                str(resumed_turn_id),
                run_id=agui_run_id,
                continuation=True,
                open_message_ids=set(state.get("openMessages") or []),
                open_reasoning_ids=set(state.get("openReasoning") or []),
                started_tool_call_ids=set(state.get("openToolCalls") or []),
            )
            events = (_event_from_sse(chunk) async for chunk in stream)
        elif kind == "resume":
            stream = resume_agui_turn(
                self._gateway,
                session,
                str(command["threadId"]),
                str(command["turnId"]),
                run_id=agui_run_id,
            )
            events = (_event_from_sse(chunk) async for chunk in stream)
        elif kind == "start":
            stream = stream_agui_turn(
                self._gateway,
                session,
                str(command["threadId"]),
                str(command["prompt"]),
                run_id=agui_run_id,
                model=_optional_string(command.get("model")),
                reasoning_effort=_optional_string(command.get("reasoningEffort")),
                workspace=_optional_string(command.get("workspace")),
                tools=list(command.get("tools") or []),
                inputs=list(command.get("inputs") or []),
            )
            events = (_event_from_sse(chunk) async for chunk in stream)

        await self._broker.set_run_state(
            run_id,
            {
                "status": "running",
                "sessionId": command["sessionId"],
                "threadId": context["threadId"],
                "clientRunId": agui_run_id,
                "startedAt": context["startedAt"],
            },
        )
        async for event in events:
            if event.get("type") == "STATE_SNAPSHOT":
                snapshot = event.setdefault("snapshot", {})
                snapshot.setdefault("startedAt", context["startedAt"])
            updates = self._track_event(event, context)
            if updates.get("threadId") and lease_context.get("threadId") is None:
                thread_id = str(updates["threadId"])
                acquired_thread = await self._broker.acquire_thread_lease(
                    str(context["sessionId"]),
                    thread_id,
                    run_id,
                    ttl_seconds=self._settings.worker_lease_seconds,
                )
                if not acquired_thread:
                    raise RuntimeError(
                        f"thread {thread_id} is already owned by another run"
                    )
                lease_context["threadId"] = thread_id
            bind_turn = "turnId" in updates
            await self._broker.publish_event(
                run_id,
                event,
                state=updates,
                session_id=str(context["sessionId"]) if bind_turn else None,
                thread_id=str(context["threadId"]) if bind_turn else None,
                turn_id=str(context["turnId"]) if bind_turn else None,
            )
            if updates:
                await self._runs.update(
                    run_id,
                    status=updates.get("status"),
                    thread_id=updates.get("threadId"),
                    turn_id=updates.get("turnId"),
                    error=updates.get("error"),
                )

    def _track_event(
        self,
        event: dict[str, Any],
        context: dict[str, Any],
    ) -> dict[str, Any]:
        updates: dict[str, Any] = {}
        event_type = event.get("type")
        if event_type == "TEXT_MESSAGE_START":
            context["openMessages"].add(str(event["messageId"]))
            updates["openMessages"] = sorted(context["openMessages"])
        elif event_type == "TEXT_MESSAGE_END":
            context["openMessages"].discard(str(event["messageId"]))
            updates["openMessages"] = sorted(context["openMessages"])
        elif event_type == "REASONING_MESSAGE_START":
            context["openReasoning"].add(str(event["messageId"]))
            updates["openReasoning"] = sorted(context["openReasoning"])
        elif event_type == "REASONING_MESSAGE_END":
            context["openReasoning"].discard(str(event["messageId"]))
            updates["openReasoning"] = sorted(context["openReasoning"])
        elif event_type == "TOOL_CALL_START":
            context["openToolCalls"].add(str(event["toolCallId"]))
            updates["openToolCalls"] = sorted(context["openToolCalls"])
        elif event_type == "TOOL_CALL_END":
            context["openToolCalls"].discard(str(event["toolCallId"]))
            updates["openToolCalls"] = sorted(context["openToolCalls"])
        if event.get("type") == "RAW":
            raw = event.get("event") or {}
            method = raw.get("method")
            params = raw.get("params") or {}
            if method == "efferva/thread-created":
                thread = params.get("thread") or {}
                context["threadId"] = str(thread.get("id"))
                updates["threadId"] = context["threadId"]
            elif method == "efferva/turn-started":
                if not params.get("turnId"):
                    raise RuntimeError("Codex started a turn without a turn id")
                context["turnId"] = str(params["turnId"])
                if params.get("threadId"):
                    context["threadId"] = str(params["threadId"])
                updates.update(
                    {
                        "threadId": context["threadId"],
                        "turnId": context["turnId"],
                        **(
                            {"startedAt": params["startedAt"]}
                            if params.get("startedAt") is not None
                            else {}
                        ),
                    }
                )
        if event_type == "RUN_FINISHED":
            result = event.get("result") or {}
            updates["status"] = (
                "interrupted"
                if result.get("status") == "interrupted"
                else "completed"
            )
        elif event_type == "RUN_ERROR":
            updates["status"] = "failed"
            updates["error"] = str(event.get("message") or "run failed")
        return updates

    async def _consume_commands(
        self,
        run_id: str,
        context: dict[str, Any],
    ) -> None:
        cursor = "0-0"
        while not context["finished"]:
            commands = await self._broker.read_commands(run_id, cursor)
            for cursor, command in commands:
                await self._apply_command(command)

    async def _apply_command(
        self,
        command: dict[str, Any],
    ) -> None:
        if command.get("kind") != "resume_interrupt":
            return
        for response in command.get("responses") or []:
            interrupt_id = str(response.get("interruptId") or "")
            future = self._pending_interrupts.get(interrupt_id)
            if future is not None and not future.done():
                future.set_result(dict(response))

    async def _renew_lease(
        self,
        run_id: str,
        owner: asyncio.Task[Any] | None,
        *,
        dispatch_id: str,
        session_id: str,
        lease_context: dict[str, str | None],
    ) -> None:
        interval = max(1, self._settings.worker_lease_seconds // 3)
        while True:
            await asyncio.sleep(interval)
            await self._broker.touch_run_claim(self._worker_id, dispatch_id)
            renewed = await self._broker.renew_lease(
                run_id,
                self._worker_id,
                ttl_seconds=self._settings.worker_lease_seconds,
            )
            thread_id = lease_context.get("threadId")
            if renewed and thread_id is not None:
                renewed = await self._broker.renew_thread_lease(
                    session_id,
                    thread_id,
                    run_id,
                    ttl_seconds=self._settings.worker_lease_seconds,
                )
            if not renewed:
                if owner is not None:
                    owner.cancel()
                return

    def _remove_finished(self) -> None:
        for task in self._active.values():
            if task.done() and not task.cancelled():
                with suppress(Exception):
                    task.result()
        self._active = {
            run_id: task for run_id, task in self._active.items() if not task.done()
        }
        ACTIVE_RUNS.set(len(self._active))

    def _remove_finished_codex_connections(self) -> None:
        for task in self._connections.values():
            if task.done() and not task.cancelled():
                with suppress(Exception):
                    task.result()
        self._connections = {
            connection_id: task
            for connection_id, task in self._connections.items()
            if not task.done()
        }
        ACTIVE_CODEX_CONNECTIONS.set(len(self._connections))

    async def _wait_for_capacity(self) -> None:
        if not self._active:
            return
        await asyncio.wait(self._active.values(), return_when=asyncio.FIRST_COMPLETED)

    async def _wait_for_codex_connection_capacity(self) -> None:
        if not self._connections:
            return
        await asyncio.wait(
            self._connections.values(),
            timeout=1,
            return_when=asyncio.FIRST_COMPLETED,
        )

    async def _drain(self) -> None:
        if not self._active:
            return
        done, pending = await asyncio.wait(
            self._active.values(),
            timeout=self._settings.worker_shutdown_grace_seconds,
        )
        del done
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    async def _drain_codex_connections(self) -> None:
        if not self._connections:
            return
        done, pending = await asyncio.wait(
            self._connections.values(),
            timeout=self._settings.worker_shutdown_grace_seconds,
        )
        del done
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)


async def serve_worker(
    *,
    sandbox: SandboxProvider,
    settings: Settings | None = None,
    codex_config: dict[str, Any] | None = None,
    developer_instructions: str | None = None,
    native_memory_enabled: bool = True,
    server_request_handler: ServerRequestHandler | None = None,
) -> None:
    settings = settings or get_settings()
    WORKER_READY.set(0)
    broker = RedisRunBroker(
        settings.redis_url,
        prefix=settings.redis_prefix,
        event_ttl_seconds=settings.redis_run_ttl_seconds,
        event_stream_maxlen=settings.redis_event_stream_maxlen,
        command_stream_maxlen=settings.redis_command_stream_maxlen,
        dispatch_queue_capacity=settings.redis_dispatch_queue_capacity,
        event_max_bytes=settings.redis_event_max_bytes,
        command_max_bytes=settings.redis_command_max_bytes,
    )
    tunnel = RedisCodexTunnel(
        settings.redis_url,
        prefix=settings.redis_prefix,
        ttl_seconds=settings.redis_run_ttl_seconds,
        lease_seconds=settings.worker_lease_seconds,
        dispatch_queue_capacity=settings.redis_dispatch_queue_capacity,
    )
    database = Database(settings.database_url)
    await database.open()
    await database.initialize(files("efferva").joinpath("schema.sql").read_text())
    await broker.open()
    await tunnel.open()
    release = await prepare_official_codex(settings)
    sandboxes = create_sandbox_control_plane(settings, sandbox)
    await sandboxes.start()
    try:
        app_servers = CodexAppServerManager(
            release.binary,
            settings,
            sandboxes,
        )
        rpc = CodexRpcClient(app_servers)
        gateway = CodexGateway(
            settings,
            rpc,
            developer_instructions=developer_instructions,
            codex_config=merge_codex_config(
                load_codex_config(settings.codex_config_file),
                codex_config or {},
            ),
            native_memory_enabled=native_memory_enabled,
        )
        start_http_server(settings.worker_metrics_port)
        worker = EffervaWorker(
            gateway,
            broker,
            RunRepository(database),
            rpc,
            tunnel,
            settings,
            server_request_handler=server_request_handler,
        )
        gateway.set_server_request_handler(worker.handle_server_request)
        await worker.run()
    finally:
        WORKER_READY.set(0)
        await sandboxes.close()
        await tunnel.close()
        await broker.close()
        await database.close()


def _event_from_sse(chunk: str) -> dict[str, Any]:
    payload = "\n".join(
        line[5:].lstrip()
        for line in chunk.replace("\r\n", "\n").splitlines()
        if line.startswith("data:")
    )
    if not payload:
        raise ValueError("AG-UI stream emitted an empty SSE event")
    return dict(json.loads(payload))


def _optional_string(value: Any) -> str | None:
    normalized = str(value or "").strip()
    return normalized or None
