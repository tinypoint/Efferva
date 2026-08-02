from __future__ import annotations

import asyncio
import logging
import os
import signal
from contextlib import suppress
from typing import Any
from uuid import uuid4

from prometheus_client import Gauge, start_http_server

from efferva.codex_appserver import CodexAppServerManager
from efferva.codex_release import prepare_official_codex
from efferva.codex_transport import CodexTransport
from efferva.codex_tunnel import (
    CodexTunnelBackpressureError,
    RedisCodexTunnel,
)
from efferva.config import Settings, get_settings
from efferva.sandbox import SandboxProvider
from efferva.sandbox.manager import create_sandbox_control_plane

LOGGER = logging.getLogger(__name__)

WORKER_READY = Gauge(
    "efferva_worker_ready",
    "Whether this worker can accept Codex connections",
)
ACTIVE_CODEX_CONNECTIONS = Gauge(
    "efferva_worker_active_codex_connections",
    "Active Codex connections held by this worker",
)
CODEX_CONNECTION_CAPACITY = Gauge(
    "efferva_worker_codex_connection_capacity",
    "Maximum Codex connections accepted by this worker",
)


class CodexConnectionWorker:
    """Claims browser connections and relays their Codex WebSocket frames."""

    def __init__(
        self,
        transport: CodexTransport,
        tunnel: RedisCodexTunnel,
        settings: Settings | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._transport = transport
        self._tunnel = tunnel
        self._worker_id = os.environ.get("HOSTNAME") or f"worker-{uuid4()}"
        self._connections: dict[str, asyncio.Task[None]] = {}
        self._stopping = asyncio.Event()

    async def run(self) -> None:
        CODEX_CONNECTION_CAPACITY.set(
            self._settings.worker_connection_capacity
        )
        await self._tunnel.ping()
        WORKER_READY.set(1)

        loop = asyncio.get_running_loop()
        for name in (signal.SIGTERM, signal.SIGINT):
            with suppress(NotImplementedError):
                loop.add_signal_handler(name, self._stopping.set)

        try:
            while not self._stopping.is_set():
                self._remove_finished_connections()
                capacity = (
                    self._settings.worker_connection_capacity
                    - len(self._connections)
                )
                if capacity <= 0:
                    await self._wait_for_capacity()
                    continue

                claimed = await self._tunnel.reclaim_stale_connections(
                    self._worker_id,
                    min_idle_ms=(
                        self._settings.worker_connection_claim_idle_seconds
                        * 1000
                    ),
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
                await self._start_connections(claimed)
        finally:
            WORKER_READY.set(0)
            self._stopping.set()
            await self._drain_connections()

    async def _start_connections(
        self,
        claimed: list[tuple[str, dict[str, Any]]],
    ) -> None:
        for dispatch_id, command in claimed:
            connection_id = str(command.get("connectionId") or "")
            session_id = str(command.get("sessionId") or "")
            if not connection_id or not session_id:
                await self._tunnel.acknowledge_connection(dispatch_id)
                continue
            if connection_id in self._connections:
                continue
            self._connections[connection_id] = asyncio.create_task(
                self._serve_connection(dispatch_id, command),
                name=f"efferva-codex:{connection_id}",
            )
        ACTIVE_CODEX_CONNECTIONS.set(len(self._connections))

    async def _serve_connection(
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
            self._renew_lease(connection_id, dispatch_id, owner)
        )
        try:
            state = await self._tunnel.get_state(connection_id)
            if state.get("status") in {"closed", "failed"}:
                await self._tunnel.acknowledge_connection(dispatch_id)
                return
            if state.get("status") == "ready":
                await self._finish_connection(
                    connection_id,
                    dispatch_id,
                    status="failed",
                    error="the worker holding this Codex connection was lost",
                )
                return
            if not await self._tunnel.client_is_connected(connection_id):
                await self._finish_connection(
                    connection_id,
                    dispatch_id,
                    status="closed",
                )
                return

            await self._tunnel.set_state(
                connection_id,
                {"status": "connecting", "workerId": self._worker_id},
            )
            async with self._transport.connect({"id": session_id}) as upstream:
                if not await self._tunnel.client_is_connected(connection_id):
                    await self._finish_connection(
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
                        self._client_to_server(connection_id, upstream)
                    ),
                    asyncio.create_task(
                        self._server_to_client(connection_id, upstream)
                    ),
                    asyncio.create_task(self._watch_client(connection_id)),
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
            await self._finish_connection(
                connection_id,
                dispatch_id,
                status="closed",
            )
        except asyncio.CancelledError:
            await self._finish_connection(
                connection_id,
                dispatch_id,
                status="failed",
                error="the worker closed this Codex connection",
            )
            raise
        except Exception as error:
            await self._finish_connection(
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

    async def _client_to_server(
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

    async def _server_to_client(
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

    async def _watch_client(self, connection_id: str) -> None:
        while True:
            await asyncio.sleep(self._tunnel.heartbeat_interval_seconds)
            if not await self._tunnel.client_is_connected(connection_id):
                return

    async def _renew_lease(
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

    async def _finish_connection(
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

    def _remove_finished_connections(self) -> None:
        for connection_id, task in self._connections.items():
            if task.done() and not task.cancelled():
                try:
                    task.result()
                except Exception:
                    LOGGER.exception(
                        "Codex connection %s failed",
                        connection_id,
                    )
        self._connections = {
            connection_id: task
            for connection_id, task in self._connections.items()
            if not task.done()
        }
        ACTIVE_CODEX_CONNECTIONS.set(len(self._connections))

    async def _wait_for_capacity(self) -> None:
        if not self._connections:
            return
        await asyncio.wait(
            self._connections.values(),
            timeout=1,
            return_when=asyncio.FIRST_COMPLETED,
        )

    async def _drain_connections(self) -> None:
        if not self._connections:
            return
        _, pending = await asyncio.wait(
            self._connections.values(),
            timeout=self._settings.worker_shutdown_grace_seconds,
        )
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)


async def serve_worker(
    *,
    sandbox: SandboxProvider,
    settings: Settings | None = None,
) -> None:
    settings = settings or get_settings()
    WORKER_READY.set(0)
    tunnel = RedisCodexTunnel(
        settings.redis_url,
        prefix=settings.redis_prefix,
        ttl_seconds=settings.codex_connection_ttl_seconds,
        lease_seconds=settings.codex_connection_lease_seconds,
        dispatch_queue_capacity=settings.codex_connection_queue_capacity,
        frame_queue_capacity=settings.codex_frame_queue_capacity,
        frame_max_bytes=settings.codex_frame_max_bytes,
    )
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
        start_http_server(settings.worker_metrics_port)
        worker = CodexConnectionWorker(
            CodexTransport(app_servers),
            tunnel,
            settings,
        )
        await worker.run()
    finally:
        WORKER_READY.set(0)
        await sandboxes.close()
        await tunnel.close()
